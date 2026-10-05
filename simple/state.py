"""Состояния серверов, пул проверяемых конфигураций и запись выходного файла.

Пул — единственный источник правды о том, на каком этапе проверки находится
каждая конфигурация.  Состояния образуют строгую воронку, из которой сервер
может быть «разжалован» назад::

    new ──пинг OK──► reachable ──ядро OK──► connected ──трафик OK──► working
     ▲                    │                    │                      │
     └──── неудача ───────┴──── неудача ───────┘   (разжалование) ────┘

Отдельным терминальным состоянием живёт ``unsupported``: конфигурация, которую
наше ядро в принципе не умеет собрать (неизвестный транспорт, битый конфиг).
Такие записи не проверяются повторно и никогда не попадают в выходной файл.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from app.constants import Protocol
from app.models import Config
from app.settings import get_logger

from .config import Policy
from app.i18n import t


#: Имена полей :class:`Config` — считаем один раз, а не при каждом восстановлении.
CONFIG_FIELDS = frozenset(f.name for f in dataclasses.fields(Config))

log = get_logger()

# Номера этапов проверки (совпадают с уровнями ядра).
LEVEL_PING = 1      # DNS + TCP-пинг
LEVEL_CORE = 2      # рукопожатие реального ядра
LEVEL_TRAFFIC = 3   # контрольный трафик через туннель


class SimpleState(str, Enum):
    """Состояние сервера в конвейере проверки."""

    NEW = "new"                  # добавлен, ни разу не проверялся
    REACHABLE = "reachable"      # пинг прошёл — кандидат на проверку ядром
    CONNECTED = "connected"      # ядро установило соединение
    WORKING = "working"          # контрольный трафик проходит — в выходном файле
    FAILED = "failed"            # исчерпал попытки: нерабочий, но остаётся в пуле
    UNSUPPORTED = "unsupported"  # ядро не может работать с этой конфигурацией

    @property
    def label(self) -> str:
        return {
            self.NEW: t("Новый (не проверен)"),
            self.REACHABLE: t("Пинг OK (кандидат)"),
            self.CONNECTED: t("Ядро OK (соединение)"),
            self.WORKING: t("Рабочий (трафик OK)"),
            self.FAILED: t("Не работает (проверен)"),
            self.UNSUPPORTED: t("Не поддерживается"),
        }[self]

    @property
    def is_active(self) -> bool:
        """Остаётся ли конфигурация в обороте (нерабочие и неподдержанные — нет)."""
        return self not in (SimpleState.FAILED, SimpleState.UNSUPPORTED)


# ---------------------------------------------------------------------------
# Запись пула
# ---------------------------------------------------------------------------

@dataclass
class ServerEntry:
    """Одна проверяемая конфигурация вместе с историей проверок.

    ``data`` — сериализованное :class:`app.models.Config` (все параметры
    подключения сохранены, поэтому конфигурацию можно воссоздать в любой момент
    и всегда записать обратно в выходной файл без потерь).
    """

    fp: str
    uri: str
    data: Dict[str, Any] = field(default_factory=dict)
    state: str = SimpleState.NEW.value

    level: int = 0
    latency_ms: Optional[int] = None

    ping_fail: int = 0
    core_fail: int = 0
    traffic_fail: int = 0

    last_error: str = ""
    source: str = ""
    first_seen: float = 0.0
    last_checked: float = 0.0
    last_ok: float = 0.0
    last_ping_at: float = 0.0
    last_traffic_at: float = 0.0
    next_check_at: float = 0.0

    # -- проекции на поля конфигурации (для интерфейса и статистики) -----
    @property
    def protocol(self) -> str:
        return str(self.data.get("protocol") or Protocol.UNKNOWN.value)

    @property
    def address(self) -> str:
        return str(self.data.get("address") or "")

    @property
    def port(self) -> int:
        try:
            return int(self.data.get("port") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def label(self) -> str:
        return str(self.data.get("label") or "")

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"

    @property
    def state_enum(self) -> SimpleState:
        try:
            return SimpleState(self.state)
        except ValueError:
            return SimpleState.NEW

    def config(self) -> Config:
        """Восстановить :class:`Config` из сохранённого словаря."""
        names = CONFIG_FIELDS
        payload = {k: v for k, v in self.data.items() if k in names}
        try:
            payload["protocol"] = Protocol(str(payload.get("protocol")))
        except ValueError:
            payload["protocol"] = Protocol.UNKNOWN
        return Config(**payload)

    # -- расписание ------------------------------------------------------
    @property
    def maintenance(self) -> bool:
        """Запись уже в выходном файле: её плановые проверки — в приоритете."""
        return self.state == SimpleState.WORKING.value

    @property
    def is_working(self) -> bool:
        return self.state == SimpleState.WORKING.value

    def due_at(self, now: float, policy: Policy) -> float:
        """Момент, когда запись стала «просроченной» — ключ честной очереди.

        Для записей воронки это ``next_check_at``.  Для рабочих серверов
        ``next_check_at`` смотрит в будущее (время следующей плановой проверки),
        поэтому берётся самый ранний момент, когда их вообще надо перепроверить:
        иначе они навсегда уезжали бы в конец очереди за свежими записями.
        """
        if self.state == SimpleState.WORKING.value:
            marks: List[float] = []
            if self.last_ping_at:
                marks.append(self.last_ping_at + policy.ping_interval)
            if self.last_traffic_at:
                marks.append(self.last_traffic_at + policy.traffic_interval)
            if marks:
                return min(marks)
        return self.next_check_at

    def due_level(self, now: float, policy: Policy) -> Optional[int]:
        """Какой этап проверки пора выполнить сейчас (1/2/3) или ``None``."""
        state = self.state
        if state == SimpleState.UNSUPPORTED.value:
            return None
        if state == SimpleState.FAILED.value:
            # Проверку сервер не прошёл: сам больше не опрашивается (и не висит
            # в системах защиты), вернуть его можно кнопкой «Сбросить в «новые»».
            return None
        if state == SimpleState.WORKING.value:
            # Плановое обслуживание: сначала контрольный трафик (он надёжнее и
            # включает проверку доступности адреса), затем обычный пинг.
            if self.last_traffic_at + policy.traffic_interval <= now:
                return LEVEL_TRAFFIC
            if self.last_ping_at + policy.ping_interval <= now:
                return LEVEL_PING
            return None
        if now < self.next_check_at:
            return None
        if state == SimpleState.NEW.value:
            return LEVEL_PING
        if state == SimpleState.REACHABLE.value:
            return LEVEL_CORE
        if state == SimpleState.CONNECTED.value:
            return LEVEL_TRAFFIC
        return None

    # -- сериализация ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Сериализовать запись для ``pool.json``.

        Ссылка остаётся только внутри ``data``: отдельное поле верхнего уровня
        дублировало её, а у vmess-ссылок длина легко превышает 300 символов —
        на подписках в тысячи строк это давало лишние мегабайты в файле пула.
        """
        payload = dataclasses.asdict(self)
        payload.pop("uri", None)
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ServerEntry":
        names = {f.name for f in dataclasses.fields(cls)}
        payload = {k: v for k, v in raw.items() if k in names}
        if not isinstance(payload.get("data"), dict):
            payload["data"] = {}
        if not str(payload.get("uri") or "").strip():
            # Совместимость: и новые файлы (ссылка только в ``data``), и старые
            # (ссылка ещё и в поле верхнего уровня) читаются одинаково.
            payload["uri"] = str(payload["data"].get("uri") or "").strip()
        return cls(**payload)

    def clone(self) -> "ServerEntry":
        return ServerEntry.from_dict(self.to_dict())


# ---------------------------------------------------------------------------
# Пул
# ---------------------------------------------------------------------------

def address_key(host: str, port: int) -> str:
    """Ключ чёрного списка адресов: ``host:port`` в нижнем регистре."""
    return f"{str(host or '').strip().lower()}:{int(port or 0)}"


def _due_moment(stamp: float, interval: float) -> float:
    """Момент плановой проверки по метке времени; ``0`` — «пора сейчас».

    Отсутствие метки означает, что проверки ещё не было: запись обязана
    попасть в очередь немедленно, а не через полный интервал.
    """
    return stamp + interval if stamp else 0.0


#: Ограничение длины причины в «кладбище».  Строка нужна только для чтения
#: человеком, но на подписках в тысячи строк длинные причины заметно раздувают
#: ``pool.json`` (файл переписывается целиком при каждом сохранении).
BLOCK_REASON_LIMIT = 60

#: Пул можно писать сжатым (``pool.json.gz``).  Формат — обычный gzip: файл
#: открывается 7-Zip/WinRAR, а посмотреть содержимое без них можно командой
#: ``python simple_main.py --pool-dump``.  Уровень 6 даёт ~6x на реальном пуле
#: при ~50 мс на сжатие (сохранение идёт раз в несколько минут — это незаметно).
GZIP_MAGIC = b"\x1f\x8b"
GZIP_LEVEL = 6


def _compact_block_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Привести запись «кладбища» к компактному виду.

    Сохраняются только те поля, которые действительно читаются: момент
    блокировки (``at``) и число неудач (``fails``), плюс обрезанная причина.
    Ссылка на конфигурацию не хранится вовсе: фильтр «не добавлять повторно»
    работает по отпечатку ``fp``, а полные ссылки (у vmess легко 500+ символов)
    делали «кладбище» самой крупной частью ``pool.json`` — больше половины
    файла, который затем перезаписывался при каждом сохранении пула.

    Так запись занимает ~100 байт вместо ~360 при том же поведении.
    """
    try:
        at = float(record.get("at") or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    try:
        fails = int(record.get("fails") or 0)
    except (TypeError, ValueError):
        fails = 0
    reason = str(record.get("reason") or "").strip()
    if len(reason) > BLOCK_REASON_LIMIT:
        reason = reason[:BLOCK_REASON_LIMIT]
    return {"at": at, "fails": fails, "reason": reason}


class PoolRow(NamedTuple):
    """Строка таблицы серверов — без глубокой копии записи пула.

    Интерфейсу нужны только эти поля, а ``ServerEntry.clone()`` делает дорогую
    копию (``asdict`` + повторный разбор).  На тысячах записей разница заметна:
    именно на копировании уходила половина времени перерисовки таблицы.
    """

    fp: str
    state: str
    protocol: str
    endpoint: str
    uri: str
    latency_ms: Optional[int]
    ping_fail: int
    core_fail: int
    traffic_fail: int
    last_checked: float
    last_error: str
    label: str


class Pool:
    """Потокобезопасный пул конфигураций с сохранением в JSON.

    Пул держит все записи в памяти (списки подписок обычно измеряются тысячами,
    а не миллионами строк) и периодически сбрасывает их на диск одним атомарным
    ``os.replace``.  Память — источник правды: пока программа работает, статусы,
    очередь, счётчики и выходной файл считаются из ``_entries``, а ``pool.json``
    — лишь резервный снимок для перезапуска и восстановления после сбоя.  Поэтому
    интервал записи (``save_interval``) не влияет ни на актуальность данных, ни на
    скорость проверок.  Есть два независимых списка исключений:

    * ``_blocked`` — «кладбище» конфигураций: сервер удалён после серии неудач и
      не добавляется заново ``blacklist_seconds`` (по умолчанию 12 часов);
      хранится компактно (отпечаток ``fp``, момент и обрезанная причина, без
      самой ссылки) — иначе «кладбище» раздувало бы ``pool.json``;
    * ``_blocked_addresses`` — **постоянный чёрный список адресов**: попадает
      туда при ручном удалении и никогда не истекает сам.  Такой адрес не
      опрашивается, не тестируется и не возвращается ни из подписок, ни из
      вставки, пока пользователь не уберёт его из списка вручную.
    """

    #: Ограничение частоты записи по умолчанию: 0 — сохранять по требованию.
    DEFAULT_SAVE_INTERVAL = 0.0

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._entries: Dict[str, ServerEntry] = {}
        self._blocked: Dict[str, Dict[str, Any]] = {}
        self._blocked_addresses: Dict[str, Dict[str, Any]] = {}
        self._revision = 0
        self._dirty = False
        #: Минимальный интервал между записями (секунды); выставляет супервизор
        #: из настроек.  Пока он не задан, пул сохраняется при каждом вызове.
        self.save_interval = self.DEFAULT_SAVE_INTERVAL
        self._saved_at = 0.0
        #: Писать пул сжатым (``pool.json.gz``).  Ставится супервизором из
        #: настройки ``simple_pool_gzip``, поэтому переключается на ходу.
        self.compress = False
        # Индекс очереди: минимальный момент готовности для каждого этапа,
        # посчитанный для текущего состояния пула и политики (см. _ensure_index).
        self._index_revision = -1
        self._index_policy: Optional[Policy] = None
        self._due_by_level: Dict[int, float] = {
            LEVEL_PING: math.inf, LEVEL_CORE: math.inf,
            LEVEL_TRAFFIC: math.inf}

    # -- пути файлов --------------------------------------------------------
    def gzip_path(self) -> str:
        """Путь сжатой копии пула (``pool.json.gz``)."""
        return self.path + ".gz"

    def existing_path(self) -> str:
        """Файл, который надо читать: под текущий формат, иначе — любой живой."""
        if self.compress and os.path.isfile(self.gzip_path()):
            return self.gzip_path()
        if os.path.isfile(self.path):
            return self.path
        if os.path.isfile(self.gzip_path()):
            return self.gzip_path()
        return ""

    def write_path(self) -> str:
        """Куда писать: сжатый или обычный файл (зависит от настройки)."""
        return self.gzip_path() if self.compress else self.path

    # -- доступ ----------------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def revision(self) -> int:
        """Счётчик изменений: интерфейс перерисовывается только при отличии."""
        return self._revision

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def has(self, fp: str) -> bool:
        with self._lock:
            return fp in self._entries

    def get(self, fp: str) -> Optional[ServerEntry]:
        with self._lock:
            entry = self._entries.get(fp)
            return entry.clone() if entry is not None else None

    def items(self) -> List[ServerEntry]:
        """Снимок всех записей — безопасно отдавать в интерфейс."""
        with self._lock:
            return [entry.clone() for entry in self._entries.values()]

    def rows(self) -> List[PoolRow]:
        """Лёгкий снимок для таблицы: без копирования записей.

        Используется перерисовкой списка серверов: ей нужны семь полей, а не
        полная копия записи вместе с её словарём конфигурации.
        """
        with self._lock:
            return [PoolRow(entry.fp, entry.state, entry.protocol,
                            entry.endpoint, entry.uri, entry.latency_ms,
                            entry.ping_fail, entry.core_fail, entry.traffic_fail,
                            entry.last_checked, entry.last_error, entry.label)
                    for entry in self._entries.values()]

    def counts(self) -> Dict[str, int]:
        result = {state.value: 0 for state in SimpleState}
        with self._lock:
            for entry in self._entries.values():
                key = (entry.state if entry.state in result
                       else SimpleState.NEW.value)
                result[key] += 1
        return result

    def working(self) -> List[ServerEntry]:
        with self._lock:
            return [entry.clone() for entry in self._entries.values()
                    if entry.state == SimpleState.WORKING.value]

    def working_count(self) -> int:
        """Сколько записей готово к выгрузке — без копирования записей.

        Интерфейс обновляет счётчики часто, а ``working()`` отдаёт глубокие
        копии: на тысяче рабочих серверов это десятки миллисекунд на каждый
        тик, хотя нужен всего лишь размер списка.
        """
        with self._lock:
            return sum(1 for entry in self._entries.values()
                       if entry.state == SimpleState.WORKING.value
                       and entry.uri.strip())

    def blocked_count(self) -> int:
        with self._lock:
            return len(self._blocked)

    # -- изменения -------------------------------------------------------
    def _touch(self) -> None:
        self._revision += 1
        self._dirty = True
        self._index_revision = -1      # индекс очереди пересчитается лениво

    def add_many(self, entries: Sequence[ServerEntry]) -> int:
        added = 0
        with self._lock:
            for entry in entries:
                if entry.fp in self._entries:
                    continue
                if self._address_blocked(entry.address, entry.port):
                    continue          # постоянный чёрный список: не трогаем адрес
                self._entries[entry.fp] = entry
                added += 1
            if added:
                self._touch()
        return added

    def remove(self, fps: Sequence[str]) -> int:
        removed = 0
        with self._lock:
            for fp in fps:
                if self._entries.pop(fp, None) is not None:
                    removed += 1
            if removed:
                self._touch()
        return removed

    def clear(self) -> int:
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            if removed:
                self._touch()
        return removed

    def reset(self, fps: Sequence[str]) -> int:
        """Вернуть выбранные записи в состояние «Новый» и обнулить счётчики."""
        changed = 0
        with self._lock:
            for fp in fps:
                entry = self._entries.get(fp)
                if entry is None:
                    continue
                entry.state = SimpleState.NEW.value
                entry.level = 0
                entry.ping_fail = 0
                entry.core_fail = 0
                entry.traffic_fail = 0
                entry.last_error = ""
                entry.next_check_at = 0.0
                changed += 1
            if changed:
                self._touch()
        return changed

    def update(self, fp: str,
               mutator: Callable[[ServerEntry], Optional[str]]) -> Optional[str]:
        """Применить мутатор к записи под блокировкой.

        Мутатор возвращает действие (``keep``/``delete``/``demote``/...);
        если действие равно ``delete``, запись удаляется.  ``None`` означает,
        что записи уже нет (её успели удалить параллельно) и результат устарел.
        """
        with self._lock:
            entry = self._entries.get(fp)
            if entry is None:
                return None
            action = mutator(entry) or "keep"
            if action == "delete":
                del self._entries[fp]
            self._touch()
            return action

    def select(self, level: int, policy: Policy, now: float,
               limit: int = 0,
               states: Optional[Sequence[str]] = None) -> List[ServerEntry]:
        """Выбрать записи, которым пора пройти этап ``level``.

        Порядок очереди: сначала уже рабочие серверы (их подтверждение важно —
        они лежат в выходном файле), затем самые «просроченные» записи.  Без
        такого приоритета плановые проверки рабочих серверов могли бы стоять в
        конце очереди за тысячами свежих записей и откладываться на часы.

        ``states`` — при необходимости ограничить выборку состояниями
        (например, пакетная перепроверка берёт только ``working``, а обычный
        этап трафика — только ``connected``).
        """
        allowed = None if states is None else {str(item) for item in states}
        if now < self.next_due(level, policy):
            # Очередь этапа пуста: ни одной записи ещё не пора.  Без этой
            # проверки каждый круг (и каждая порция) перебирал бы весь пул —
            # на десяти тысячах записей это заметная нагрузка на пустом ходу.
            return []
        chosen: List[Tuple[int, float, float, ServerEntry]] = []
        with self._lock:
            for entry in self._entries.values():
                if allowed is not None and entry.state not in allowed:
                    continue
                if entry.due_level(now, policy) != level:
                    continue
                if self._address_blocked(entry.address, entry.port):
                    continue          # адрес в чёрном списке: не опрашиваем
                chosen.append((0 if entry.maintenance else 1,
                               entry.due_at(now, policy),
                               entry.last_checked,
                               entry.clone()))
        chosen.sort(key=lambda item: (item[0], item[1], item[2]))
        if limit and limit > 0:
            chosen = chosen[:limit]
        return [entry for _prio, _due, _checked, entry in chosen]

    # -- индекс очереди ---------------------------------------------------
    def next_due(self, level: int, policy: Policy) -> float:
        """Ближайший момент, когда этапу ``level`` будет что проверять.

        ``inf`` означает «проверять нечего»: в пуле нет активных записей либо
        они все «спят» до своих дат.  Значение считается по кэшу индекса —
        повторные вызовы на неизменном пуле бесплатны.
        """
        self._ensure_index(policy)
        return self._due_by_level.get(level, math.inf)

    def next_due_any(self, policy: Policy) -> float:
        """Ближайшая проверка любого этапа (``inf`` — проверять нечего)."""
        self._ensure_index(policy)
        return min(self._due_by_level.values())

    def _ensure_index(self, policy: Policy) -> None:
        """Пересчитать индекс очереди, если пул или политика изменились.

        Индекс хранит по каждому этапу минимальный момент, когда запись станет
        «просроченной».  Пересчёт — один линейный проход без копирования
        записей, поэтому он дешевле полноценного :meth:`select`; на неизменном
        пуле (пустой круг) не делается вообще.
        """
        with self._lock:
            if (self._index_revision == self._revision
                    and self._index_policy is policy):
                return
            due: Dict[int, float] = {LEVEL_PING: math.inf,
                                     LEVEL_CORE: math.inf,
                                     LEVEL_TRAFFIC: math.inf}
            for entry in self._entries.values():
                state = entry.state
                if state == SimpleState.WORKING.value:
                    # Рабочий сервер подтверждается планово и по двум
                    # независимым датам: контрольный трафик и обычный пинг.
                    due[LEVEL_TRAFFIC] = min(
                        due[LEVEL_TRAFFIC],
                        _due_moment(entry.last_traffic_at, policy.traffic_interval))
                    due[LEVEL_PING] = min(
                        due[LEVEL_PING],
                        _due_moment(entry.last_ping_at, policy.ping_interval))
                    continue
                if state == SimpleState.NEW.value:
                    level = LEVEL_PING
                elif state == SimpleState.REACHABLE.value:
                    level = LEVEL_CORE
                elif state == SimpleState.CONNECTED.value:
                    level = LEVEL_TRAFFIC
                else:
                    continue       # нерабочие и неподдержанные не опрашиваются
                if entry.next_check_at < due[level]:
                    due[level] = entry.next_check_at
            self._index_revision = self._revision
            self._index_policy = policy
            self._due_by_level = due

    def purge_failed(self, now: float, ttl_seconds: float) -> int:
        """Убрать нерабочие записи, провалившиеся раньше ``ttl_seconds`` назад.

        Статус «Не работает» терминальный: такие записи больше не опрашиваются,
        поэтому без срока годности они копились бы в ``pool.json`` вечно.
        Момент провала — ``last_checked`` (после него запись не трогается),
        для записей без отметок — ``first_seen``.  ``0`` выключает очистку.
        """
        if ttl_seconds <= 0:
            return 0
        removed = 0
        with self._lock:
            for fp, entry in list(self._entries.items()):
                if entry.state != SimpleState.FAILED.value:
                    continue
                stamp = entry.last_checked or entry.first_seen
                if stamp and now - stamp >= ttl_seconds:
                    del self._entries[fp]
                    removed += 1
            if removed:
                self._touch()
        return removed

    # -- «кладбище» ------------------------------------------------------
    def block(self, fp: str, uri: str, reason: str, fails: int = 0) -> None:
        """Запомнить удалённый сервер, чтобы не добавлять его заново.

        ``uri`` остаётся в сигнатуре для совместимости с вызывающим кодом, но
        не сохраняется: фильтр работает по отпечатку ``fp``, а «кладбище» с
        полными ссылками занимало больше половины ``pool.json``.  Запись
        хранится в компактном виде (см. :func:`_compact_block_record`).
        """
        with self._lock:
            self._blocked[fp] = _compact_block_record({
                "at": time.time(),
                "reason": reason,
                "fails": fails,
            })
            self._touch()

    def blocked(self, fp: str, now: float, policy: Policy) -> bool:
        if policy.blacklist_seconds <= 0:
            return False
        with self._lock:
            record = self._blocked.get(fp)
        if not record:
            return False
        try:
            at = float(record.get("at") or 0.0)
        except (TypeError, ValueError):
            return False
        return now - at < policy.blacklist_seconds

    def purge_blacklist(self, now: float, policy: Policy) -> int:
        """Убрать из «кладбища» записи, у которых истёк срок блокировки.

        Чёрный список адресов здесь не трогается: он постоянный и очищается
        только вручную.
        """
        if policy.blacklist_seconds <= 0:
            return 0
        with self._lock:
            stale = [fp for fp, record in self._blocked.items()
                     if now - float(record.get("at") or 0.0)
                     >= policy.blacklist_seconds]
            for fp in stale:
                del self._blocked[fp]
            if stale:
                self._touch()
        return len(stale)

    def clear_blacklist(self) -> int:
        with self._lock:
            count = len(self._blocked)
            self._blocked.clear()
            if count:
                self._touch()
        return count

    # -- постоянный чёрный список адресов --------------------------------
    def blocked_address_count(self) -> int:
        with self._lock:
            return len(self._blocked_addresses)

    def is_blocked_address(self, host: str, port: int) -> bool:
        """Адрес в постоянном чёрном списке (его нельзя ни опрашивать, ни добавлять).

        Учитывается и запись «все порты» (``host:0``): адрес можно занести
        целиком, не зная портов (например, сниферская или фишинговая площадка).
        """
        with self._lock:
            return self._address_blocked(host, port)

    def _address_blocked(self, host: str, port: int) -> bool:
        """Проверка по чёрному списку (вызывать под ``self._lock``)."""
        return (address_key(host, port) in self._blocked_addresses
                or address_key(host, 0) in self._blocked_addresses)

    def blocked_addresses(self) -> List[Dict[str, Any]]:
        """Снимок чёрного списка адресов — для интерфейса и статистики."""
        with self._lock:
            snapshot = []
            for key, record in sorted(self._blocked_addresses.items()):
                item = dict(record, key=key)
                port = int(record.get("port") or 0)
                host = str(record.get("host") or key.rpartition(":")[0])
                # Понятная подпись: «1.2.3.4:443» или «1.2.3.4 (все порты)».
                item["display"] = f"{host}:{port}" if port else t('{} (все порты)').format(host)
                snapshot.append(item)
            return snapshot

    def block_address(self, host: str, port: int = 0, reason: str = t("вручную"),
                      note: str = "") -> Tuple[bool, int]:
        """Внести адрес в постоянный чёрный список.

        Возвращает ``(адрес_был_новым, сколько_записей_убрано_из_пула)``: вместе
        с адресом из пула убираются все конфигурации с тем же ``host:port``,
        чтобы они больше не попадали в опрос и в проверки.

        ``port = 0`` означает «весь хост»: блокируются все порты этого адреса,
        поэтому занести можно и один IP, не зная портов.
        """
        key = address_key(host, port)
        with self._lock:
            is_new = key not in self._blocked_addresses
            self._blocked_addresses[key] = {
                "host": str(host or "").strip().lower(),
                "port": int(port or 0),
                "at": time.time(),
                "reason": reason,
                "note": note,
            }
            dropped = 0
            for fp, entry in list(self._entries.items()):
                if not self._address_blocked(entry.address, entry.port):
                    continue
                del self._entries[fp]
                dropped += 1
            self._touch()
        return is_new, dropped

    def unblock_address(self, host: str, port: int) -> bool:
        """Убрать адрес из чёрного списка (он снова может добавляться)."""
        key = address_key(host, port)
        with self._lock:
            removed = self._blocked_addresses.pop(key, None) is not None
            if removed:
                self._touch()
        return removed

    def clear_blocked_addresses(self) -> int:
        with self._lock:
            count = len(self._blocked_addresses)
            self._blocked_addresses.clear()
            if count:
                self._touch()
        return count

    # -- персистенция ----------------------------------------------------
    def snapshot_payload(self) -> Dict[str, Any]:
        """Слепок пула — ровно то, что уходит в файл (и в ``--pool-dump``)."""
        with self._lock:
            return {
                "version": 1,
                "saved_at": time.time(),
                "entries": [entry.to_dict() for entry in self._entries.values()],
                "blocked": {k: dict(v) for k, v in self._blocked.items()},
                "blocked_addresses": {k: dict(v)
                                      for k, v in self._blocked_addresses.items()},
            }

    def dump_text(self) -> str:
        """Читаемый JSON пула — для команды ``--pool-dump``."""
        return json.dumps(self.snapshot_payload(), ensure_ascii=False, indent=2)

    def load(self) -> None:
        """Прочитать пул; формат (обычный JSON или gzip) определяется по файлу.

        Оба варианта читаются всегда, поэтому переключение настройки «сжимать
        пул» не теряет накопленные данные и не требует ручной конвертации.
        """
        path = self.existing_path()
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return
        try:
            if raw[:2] == GZIP_MAGIC:
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        entries: Dict[str, ServerEntry] = {}
        for raw in payload.get("entries") or []:
            if not isinstance(raw, dict) or not raw.get("fp"):
                continue
            entries[str(raw["fp"])] = ServerEntry.from_dict(raw)
        blocked = payload.get("blocked")
        if not isinstance(blocked, dict):
            blocked = {}
        blocked_addresses = payload.get("blocked_addresses")
        if not isinstance(blocked_addresses, dict):
            blocked_addresses = {}
        with self._lock:
            self._entries = entries
            # Старые файлы хранили ссылку и полную причину: при загрузке запись
            # сжимается, поэтому «кладбище» уменьшается уже при первом
            # сохранении после обновления.
            self._blocked = {str(k): _compact_block_record(v)
                             for k, v in blocked.items()
                             if isinstance(v, dict)}
            self._blocked_addresses = {str(k): v
                                       for k, v in blocked_addresses.items()
                                       if isinstance(v, dict)}
            self._revision += 1
            self._dirty = False

    def save(self, force: bool = False) -> bool:
        """Сохранить пул на диск. ``True`` — файл действительно записан.

        Запись — только резервный снимок: пока программа работает, актуальные
        статусы живут в памяти, поэтому задержка сохранения не влияет ни на
        проверки, ни на таблицу, ни на выходной файл.

        Обычный вызов может быть отложен: ``save_interval`` ограничивает частоту
        записи.  Файл пула переписывается целиком, а при тысячах серверов это
        десятки мегабайт за раз: пока идут проверки, в пуле меняется почти
        каждый круг, поэтому без ограничения получались десятки тысяч
        бессмысленных перезаписей в сутки — лишний износ SSD при работе
        службой месяцами.  Изменения при этом не теряются: они остаются в памяти
        и попадают в следующий сброс, а перед остановкой и выходом всегда
        вызывается ``save(force=True)``.
        """
        now = time.time()
        with self._lock:
            if not self._dirty and not force:
                return False
            if (not force and self.save_interval > 0
                    and now - self._saved_at < self.save_interval):
                return False           # запись отложена, данные остаются в памяти
            payload = self.snapshot_payload()
            self._dirty = False
            self._saved_at = now
        text = json.dumps(payload, ensure_ascii=False)
        if self.compress:
            # Сжатие идёт вне блокировки пула: проверки оно не задерживает.
            data = gzip.compress(text.encode("utf-8"),
                                 compresslevel=GZIP_LEVEL, mtime=0)
        else:
            data = text.encode("utf-8")
        target = self.write_path()
        stale = self.path if self.compress else self.gzip_path()
        tmp = target + ".tmp"
        with self._save_lock:
            try:
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)
                if stale != target and os.path.isfile(stale):
                    # Смена формата (обычный JSON ↔ gzip): старая копия больше
                    # не нужна — иначе при загрузке можно прочитать именно её.
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            except OSError as exc:
                log.error(t("Не удалось сохранить пул %s: %s"), target, exc)
                with self._lock:
                    self._dirty = True
                    self._saved_at = 0.0   # следующая попытка — сразу
                return False
        return True


# ---------------------------------------------------------------------------
# Выходной файл
# ---------------------------------------------------------------------------

class OutputWriter:
    """Пишет список рабочих конфигураций (одна URI на строку).

    Файл всегда отражает текущее содержимое состояния ``working``, поэтому его
    можно сразу подключить как обычную подписку.  Запись выполняется атомарно и
    только при фактическом изменении набора — никаких перезаписей на каждый
    проверенный сервер.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last: Any = None
        self.written_at = 0.0
        self.count = 0

    def write(self, payload: str, signature: Any = None,
              count: Optional[int] = None, force: bool = False) -> bool:
        """Записать файл. ``True`` — файл действительно обновился.

        ``signature`` — признак содержимого: пока он не меняется, файл не
        перезаписывается (это важно, когда вывод обновляется каждый круг).
        """
        if signature is None:
            signature = payload
        with self._lock:
            if not force and signature == self._last:
                return False
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.error(t("Не удалось записать файл вывода %s: %s"),
                          self.path, exc)
                return False
            self._last = signature
            self.written_at = time.time()
            if count is not None:
                self.count = int(count)
            return True

    def write_lines(self, lines: Sequence[str], force: bool = False) -> bool:
        """Записать список строк (одна конфигурация на строку)."""
        items = [str(line).strip() for line in lines if str(line).strip()]
        payload = "".join(item + "\n" for item in items)
        return self.write(payload, tuple(items), len(items), force)

    def reset(self) -> None:
        with self._lock:
            self._last = None
