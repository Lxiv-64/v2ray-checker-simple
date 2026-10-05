"""Фоновый бесконечный цикл проверок и автомат состояний (simple-версия).

Один круг цикла делает ровно то, что требует воронка: сначала массовый дешёвый
пинг (этап 1) всех «новых», затем проверку ядром (этап 2) тех, кто пинг прошёл, и
только потом контрольный трафик (этап 3) для лучших.  Такая последовательность
не даёт тратить дорогие запуски ядра на заведомо мёртвые адреса.

Отдельно, по расписанию, обслуживаются уже рабочие серверы: раз в час — пинг,
раз в шесть часов — полноценная проверка трафиком.  Любая неудача «разжаловывает»
сервер обратно в состояние «новый», а серия из ``max_failures`` неудач с разносом
во времени приводит к удалению (сервер попадает в «кладбище» и не возвращается из
подписок заданное время).
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from app import core_config, core_manager, fetcher, parser
from app.models import Config
from app.settings import get_logger, setup_logging

from .batch import reports_summary, run_batches
from .commands import CommandScheduler
from .config import (
    OUTPUT_BOTH,
    OUTPUT_FORMATS,
    OUTPUT_URI,
    OUTPUT_V2RAYN,
    SIMPLE_OUTPUT_PATH,
    SIMPLE_POOL_PATH,
    SIMPLE_V2RAYN_PATH,
    Policy,
    enabled_simple_protocols,
    pool_gzip_enabled,
    simple_policy,
)
from .probe import Outcome, Prober
from .netwatch import NetworkWatcher, watch_enabled
from .state import (
    LEVEL_CORE,
    LEVEL_PING,
    LEVEL_TRAFFIC,
    OutputWriter,
    Pool,
    ServerEntry,
    SimpleState,
)
from .v2rayn import DEFAULT_PROBE_URL, build_client_config, xray_configs
from app.i18n import t


log = get_logger()


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

@dataclass
class SimpleStats:
    """Снимок состояния конвейера, который показывает интерфейс."""

    # содержимое пула
    total: int = 0
    new: int = 0
    reachable: int = 0
    connected: int = 0
    working: int = 0
    unsupported: int = 0
    failed: int = 0                 # «Не работает (проверен)» — лежат в пуле
    exhausted: int = 0              # сколько раз сервер «сдался» за сеанс
    purged_failed: int = 0          # убрано из пула по сроку годности
    blocked: int = 0
    blocked_addresses: int = 0

    # импорт
    added: int = 0
    duplicates: int = 0
    filtered: int = 0
    invalid: int = 0
    deleted: int = 0
    demoted: int = 0
    unsupported_total: int = 0

    # результаты этапов
    ping_ok: int = 0
    ping_fail: int = 0
    core_ok: int = 0
    core_fail: int = 0
    traffic_ok: int = 0
    traffic_fail: int = 0

    # работа цикла
    round_index: int = 0
    stage: str = ""
    core_ready: bool = False
    running: bool = False
    paused: bool = False
    last_round_at: float = 0.0
    network_online: bool = True
    #: Проверки приостановлены из-за пропавшей связи (не путать с «Паузой»).
    network_hold: bool = False
    last_round_seconds: float = 0.0
    last_refresh_at: float = 0.0
    next_refresh_at: float = 0.0

    # пакетная перепроверка рабочих серверов
    maintenance_checked: int = 0
    maintenance_ok: int = 0
    maintenance_failed: int = 0
    maintenance_guard_skips: int = 0
    maintenance_batches: int = 0

    # вывод
    output_format: str = OUTPUT_URI
    output_path: str = ""
    output_count: int = 0
    output_written_at: float = 0.0
    v2rayn_path: str = ""
    v2rayn_count: int = 0

    def snapshot(self) -> "SimpleStats":
        return SimpleStats(**dataclasses.asdict(self))


STAGE_LABELS = {
    LEVEL_PING: t("Пинг (DNS/TCP)"),
    LEVEL_CORE: t("Ядро (рукопожатие)"),
    LEVEL_TRAFFIC: t("Трафик (контрольный запрос)"),
}

#: Минимальный размер выборки, при котором «провалились почти все» считается
#: сбоем окружения (сети, ядра), а не проблемой серверов: на пачке из одного
#: сервера любая неудача даёт 100% и не должна выглядеть как обрыв связи.
GUARD_MIN_SAMPLE = 4

#: Как часто пул чистится от записей со статусом «Не работает»: проверка
#: дешёвая, но на десяти тысячах записей нет смысла делать её каждый круг.
PURGE_MIN_GAP = 300.0

#: Верхняя граница паузы простаивающего цикла: пока проверять нечего, цикл спит
#: до ближайшей даты в очереди, но не дольше — так новые события (вставка,
#: импорт, «Обновить списки») обрабатываются с задержкой не более этих секунд.
MAX_IDLE_SLEEP = 5.0


def _number_setting(settings, key: str, default: float) -> float:
    """Числовая настройка с гарантированным откатом на значение по умолчанию."""
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ---------------------------------------------------------------------------
# Супервизор
# ---------------------------------------------------------------------------

#: Допустимые имена хостов: буквы, цифры, точки и дефисы (без пробелов).
HOST_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def valid_host(host: str) -> bool:
    """Похоже ли значение на адрес (IP или домен), а не на случайный текст.

    Нужно, чтобы опечатка в поле чёрного списка не превращалась в запись:
    «совсем не адрес» не должно блокироваться как имя хоста.
    """
    text = (host or "").strip()
    if not text or " " in text:
        return False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, text)
            return True
        except OSError:
            continue
    if "." not in text:
        return False          # домен без точки — почти всегда опечатка
    return bool(HOST_NAME_RE.match(text))


def parse_blacklist_item(item: str) -> Tuple[str, int]:
    """Разобрать строку для чёрного списка в пару ``(host, port)``.

    Понимает три вида записей:

    * ссылка на сервер (``vless://…``, ``vmess://…``) — берётся её адрес и порт;
    * ``host:port`` (IPv6 — в квадратных скобках: ``[::1]:443``);
    * просто ``host`` — тогда порт ``0``, что значит «все порты этого адреса».

    Пустая строка в ответе означает, что запись разобрать не удалось.
    """
    text = (item or "").strip()
    if not text:
        return "", 0
    if "://" in text:
        cfg = parser.parse_uri(text)
        if cfg is None or not cfg.address or not valid_host(cfg.address):
            return "", 0
        return str(cfg.address), int(cfg.port or 0)
    if text.startswith("["):
        host, sep, tail = text[1:].partition("]")
        if not sep or not valid_host(host):
            return "", 0
        port_text = tail.lstrip(":") if tail.startswith(":") else ""
        return host.strip(), int(port_text) if port_text.isdigit() else 0
    host, sep, port_text = text.rpartition(":")
    if sep and port_text.isdigit():
        host = host.strip()
        return (host, int(port_text)) if valid_host(host) else ("", 0)
    return (text, 0) if valid_host(text) else ("", 0)


class Supervisor:
    """Независимый от интерфейса управляющий цикл проверок.

    Класс не знает ни о tkinter, ни о хранилище ядра: он работает
    с :class:`~simple.state.Pool`, :class:`~simple.probe.Prober` и настройками, а
    наблюдателям отдаёт события (журнал, статистика, изменение записи).
    """

    def __init__(self, settings, pool: Optional[Pool] = None,
                 prober: Optional[Prober] = None) -> None:
        self.settings = settings
        setup_logging(
            str(settings.get("log_level", "INFO")),
            int(settings.get("max_log_lines", 2000)),
            bool(settings.get("log_to_file", False)),
        )
        core_manager.set_keep_debug_files(
            bool(settings.get("keep_core_debug_files", False)))
        # Временные конфиги и журналы ядер от прежних запусков удаляются на старте,
        # если пользователь не просил их сохранять.
        core_manager.cleanup_temp_dir()
        self.policy: Policy = simple_policy(settings)
        self.pool = pool if pool is not None else Pool(SIMPLE_POOL_PATH)
        self.prober = prober if prober is not None else Prober(settings)
        self.writer = OutputWriter(self._output_path())
        self.json_writer = OutputWriter(self._v2rayn_path())
        # Внешние команды: независимый от проверок слой автоматизации.
        self.commands = CommandScheduler(
            self.settings, artifacts_provider=self.artifacts,
            on_log=self._emit_log)
        # Слежка за сетью: источник событий «пропала/вернулась» для команд.
        enabled, interval = watch_enabled(settings)
        self.netwatch = NetworkWatcher(interval=interval,
                                       on_event=self._on_network_event)
        self._netwatch_enabled = bool(enabled)
        self.stats = SimpleStats()

        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()          # set == «работаем», clear == «пауза»
        self._refresh_on_start = True
        self._warned_no_core = False
        self._refresh_lock = threading.Lock()
        # Активные потоки проверок: их нужно дождаться при остановке, иначе ядра
        # не успеют убрать свои временные файлы из tmp/.
        self._workers: Set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        # Пул сохраняется не чаще ``simple_save_min_seconds`` (см. Pool.save):
        # файл переписывается целиком, и частая запись — это износ SSD без пользы.
        self.pool.save_interval = max(0.0, self.policy.save_interval)
        self._last_purge_at = 0.0

        self._on_log: List[Callable[[str], None]] = []
        self._on_stats: List[Callable[[SimpleStats], None]] = []
        self._on_change: List[Callable[[str], None]] = []
        self._on_output: List[Callable[[str, int], None]] = []

    # -- пути ------------------------------------------------------------
    def _output_path(self) -> str:
        """Путь выходного файла (с защитой от совпадения с файлом пула)."""
        path = str(self.settings.get("simple_output_path") or "").strip()
        if not path:
            return SIMPLE_OUTPUT_PATH
        try:
            if os.path.abspath(path) == os.path.abspath(self.pool.path):
                self._emit_log(t("Путь вывода совпадает с файлом пула — "
                               "используется путь по умолчанию."))
                return SIMPLE_OUTPUT_PATH
        except OSError:
            pass
        return path

    def _v2rayn_path(self) -> str:
        """Путь JSON-файла для импорта в v2rayN."""
        path = str(self.settings.get("simple_v2rayn_path") or "").strip()
        if not path:
            # Не задан — кладём рядом с текстовым списком.
            base = os.path.splitext(self._output_path())[0]
            return (base + "-v2rayN.json") if base else SIMPLE_V2RAYN_PATH
        try:
            if os.path.abspath(path) == os.path.abspath(self.pool.path):
                self._emit_log(t("Путь v2rayN совпадает с файлом пула — "
                               "используется путь по умолчанию."))
                return SIMPLE_V2RAYN_PATH
        except OSError:
            pass
        return path

    def output_format(self) -> str:
        """Выбранный формат файла вывода (``uri`` / ``v2rayn`` / ``both``)."""
        value = str(self.settings.get("simple_output_format",
                                      OUTPUT_URI)).strip().lower()
        return value if value in OUTPUT_FORMATS else OUTPUT_URI

    def _probe_url(self) -> str:
        """Цель контрольного запроса — она же цель измерений в v2rayN."""
        url = str(self.settings.get("traffic_url") or "").strip()
        return url if "://" in url else DEFAULT_PROBE_URL

    # -- наблюдатели -----------------------------------------------------
    def on_log(self, callback: Callable[[str], None]) -> None:
        self._on_log.append(callback)

    def on_stats(self, callback: Callable[[SimpleStats], None]) -> None:
        self._on_stats.append(callback)

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._on_change.append(callback)

    def on_output(self, callback: Callable[[str, int], None]) -> None:
        self._on_output.append(callback)

    def _emit_log(self, message: str) -> None:
        log.info(message)
        for callback in list(self._on_log):
            try:
                callback(message)
            except Exception:
                pass

    def _emit_stats(self) -> None:
        with self._lock:
            snapshot = self.stats.snapshot()
        for callback in list(self._on_stats):
            try:
                callback(snapshot)
            except Exception:
                pass

    def _notify(self, fp: str) -> None:
        for callback in list(self._on_change):
            try:
                callback(fp)
            except Exception:
                pass

    # -- состояние -------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def snapshot(self) -> SimpleStats:
        with self._lock:
            return self.stats.snapshot()

    def reload_policy(self) -> None:
        """Перечитать настройки (интерфейс вызывает после «Сохранить»)."""
        self.policy = simple_policy(self.settings)
        self.pool.save_interval = max(0.0, self.policy.save_interval)
        # Сжатие пула — переключаемая настройка: применяется со следующей
        # записи, чтение же понимает оба формата (см. Pool.load).
        self.pool.compress = pool_gzip_enabled(self.settings)

    def save_pool(self, force: bool = True) -> bool:
        """Сбросить пул на диск прямо сейчас.

        Пул в памяти всегда актуален сам по себе: запись — резервный снимок на
        случай перезапуска, сбоя или закрытия программы.

        Обычная запись может быть отложена ограничением частоты, поэтому
        короткоживущие сценарии (``--import``, ``--once``) и явные действия
        пользователя просят принудительное сохранение — иначе импорт или
        результаты прохода потерялись бы при выходе из программы.
        """
        return self.pool.save(force=force)

    # -- управление ------------------------------------------------------
    def start(self, refresh_sources: bool = True) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._pause.set()
            self._refresh_on_start = bool(refresh_sources)
            self.stats.running = True
            self.stats.paused = False
            thread = threading.Thread(target=self._loop,
                                      name="simple-supervisor", daemon=True)
            self._thread = thread
        # Цикл запускается (возможно, после остановки) — ядра снова разрешены.
        allow = getattr(self.prober, "allow_cores", None)
        if callable(allow):
            allow()
        thread.start()
        # Внешние команды — самостоятельный слой автоматизации: им нужны события
        # «запуск», «остановка», «сеть» и «круг», а не только изменение файла.
        self.commands.start()
        self.commands.notify_event("on_start")
        if self._netwatch_enabled:
            self.netwatch.start()
        return True

    def _stop_cores(self) -> None:
        """Погасить запущенные ядра (интерфейс пробера не обязателен)."""
        stop = getattr(self.prober, "stop_cores", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:  # noqa: BLE001
            pass

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        self._pause.set()          # разбудить возможные ожидания паузы
        self.netwatch.stop()
        # Команда «при остановке» должна успеть выполниться, поэтому планировщик
        # не гасим: он живёт вместе с приложением, а останавливает его выход
        # (см. SimpleApplication._on_close и simple/cli.py).
        self.commands.start()
        self.commands.notify_event("on_stop")
        # Сразу гасим запущенные ядра: иначе оборванный тест трафика (до
        # 30 секунд) оставил бы висящие процессы и файлы в tmp/.
        self._stop_cores()
        thread = self._thread
        if (thread is not None and thread.is_alive()
                and threading.current_thread() is not thread):
            thread.join(timeout=timeout)
        # Дать завершиться уже запущенным проверкам: их временные файлы удалит
        # CoreProcess.stop(), вызванный из самого потока проверки.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._workers_lock:
                alive = [worker for worker in self._workers
                         if worker.is_alive()]
            if not alive:
                break
            time.sleep(0.1)
        self._stop_cores()
        with self._lock:
            self.stats.running = False
            self.stats.paused = False
            self.stats.stage = ""
        self.pool.save(force=True)

    def _on_network_event(self, event: str) -> None:
        """Сеть пропала или вернулась: сообщаем внешним командам и в журнал."""
        online = event != "on_network_down"
        with self._lock:
            self.stats.network_online = online
            self.stats.network_hold = not online
        self.commands.notify_event(event)
        if online:
            self._emit_log(t("Сеть восстановилась — проверки продолжаются."))
        else:
            self._emit_log(
                t("Сеть недоступна: проверки приостановлены, их результаты не "
                "применяются — статусы серверов не меняются."))

    def reload_network_watch(self) -> None:
        """Перечитать настройки слежки за сетью (после «Сохранить»)."""
        enabled, interval = watch_enabled(self.settings)
        self._netwatch_enabled = bool(enabled)
        self.netwatch.interval = max(5.0, float(interval))
        if not enabled:
            self.netwatch.stop()
        elif self.running:
            self.netwatch.start()

    def pause(self) -> None:
        self._pause.clear()
        with self._lock:
            self.stats.paused = True
        self._emit_log(t("Цикл приостановлен."))

    def resume(self) -> None:
        self._pause.set()
        with self._lock:
            self.stats.paused = False
        self._emit_log(t("Цикл продолжен."))

    def _await_pause(self) -> bool:
        """Ждать снятия паузы. ``False`` — поступила команда остановки."""
        while True:
            if self._stop.is_set():
                return False
            if self._pause.wait(timeout=0.2):
                return not self._stop.is_set()

    # -- источники -------------------------------------------------------
    def sources(self) -> List[Dict]:
        raw = self.settings.get("simple_sources")
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

    def set_sources(self, sources: Sequence[Dict]) -> None:
        # Копия через JSON обязательна: Settings.set игнорирует присваивание
        # того же самого объекта, и изменения не попали бы на диск.
        self.settings.set("simple_sources",
                          json.loads(json.dumps(list(sources),
                                                ensure_ascii=False)))

    def add_source(self, kind: str, value: str, name: str = "",
                   enabled: bool = True) -> bool:
        value = (value or "").strip()
        if not value:
            return False
        sources = self.sources()
        for source in sources:
            if (source.get("kind") == kind
                    and str(source.get("value", "")).strip() == value):
                return False
        sources.append({
            "kind": kind,
            "value": value,
            "name": name or "",
            "enabled": bool(enabled),
            "last_fetch": 0.0,
            "last_status": "",
            "last_count": 0,
        })
        self.set_sources(sources)
        self._emit_log(t('Источник добавлен: {}').format(value))
        return True

    def remove_source(self, index: int) -> bool:
        sources = self.sources()
        if not 0 <= index < len(sources):
            return False
        removed = sources.pop(index)
        self.set_sources(sources)
        self._emit_log(t('Источник удалён: {}').format(removed.get('value', '')))
        return True

    def set_source_enabled(self, index: int, enabled: bool) -> bool:
        sources = self.sources()
        if not 0 <= index < len(sources):
            return False
        sources[index]["enabled"] = bool(enabled)
        self.set_sources(sources)
        return True

    # -- основной цикл ---------------------------------------------------
    def _loop(self) -> None:
        self._emit_log(t("Автоматический цикл проверки запущен."))
        try:
            if self._refresh_on_start:
                self._maybe_refresh_sources(force=True)
            while not self._stop.is_set():
                if not self._await_pause():
                    break
                done = 0
                try:
                    done = self.run_round()
                except Exception as exc:  # noqa: BLE001 - цикл не должен падать
                    self._emit_log(t('Ошибка круга проверки: {}').format(exc))
                    time.sleep(2.0)
                self._stop.wait(timeout=self._loop_sleep(done))
        finally:
            self.pool.save(force=True)
            self._flush_output(force=True)
            with self._lock:
                self.stats.running = False
                self.stats.stage = ""
            self._emit_log(t("Цикл проверки остановлен."))
            self._emit_stats()

    def _loop_sleep(self, done: int) -> float:
        """Пауза перед следующим кругом.

        Пока проверки идут, берётся обычная пауза из настроек.  Пустой круг не
        должен крутиться на полной скорости: если проверять нечего, цикл спит до
        ближайшей даты в очереди (но не дольше :data:`MAX_IDLE_SLEEP`), а новая
        работа обрабатывается по факту её появления.
        """
        try:
            base = max(0.2, float(self.settings.get("simple_loop_sleep", 1.0)))
        except (TypeError, ValueError):
            base = 1.0
        if done:
            return base
        due = self.pool.next_due_any(self.policy)
        if not math.isfinite(due):
            return MAX_IDLE_SLEEP      # проверять нечего и нечего ждать
        return max(base, min(MAX_IDLE_SLEEP, due - time.time()))

    def run_round(self) -> int:
        """Один проход воронки: пинг → ядро → трафик.

        Возвращает число выполненных проверок.  Метод можно вызывать и вручную
        (кнопка «Один проход»), и из фонового цикла.

        При пропавшей связи круг не делает проверок вообще: массовые тайм-ауты
        не должны выглядеть как «серверы перестали работать», поэтому пул и
        источники не трогаются до возвращения сети.
        """
        started = time.time()
        self.reload_policy()
        if not self._network_online():
            with self._lock:
                self.stats.round_index += 1
                self.stats.last_round_at = time.time()
                self.stats.last_round_seconds = time.time() - started
                self.stats.stage = t("нет сети — проверки приостановлены")
            self._refresh_stats(heavy=False)
            return 0

        self._maybe_refresh_sources()
        self._purge_failed()

        done = self._run_stage(LEVEL_PING)
        if not self._stop.is_set() and self._cores_ready():
            done += self._run_stage(LEVEL_CORE)
            # Рабочие серверы подтверждаются пачками (один процесс ядра на
            # пачку), остальные проходят обычный одиночный этап трафика.
            done += self._run_maintenance()
            done += self._run_stage(LEVEL_TRAFFIC,
                                    states={SimpleState.CONNECTED.value})

        self._flush_output()
        self.pool.save()
        elapsed = time.time() - started
        with self._lock:
            self.stats.round_index += 1
            self.stats.last_round_at = time.time()
            self.stats.last_round_seconds = elapsed
            self.stats.stage = ""
            rounds = self.stats.round_index
        if done:
            self._emit_log(
                t('Круг #{} завершён: {} проверок за {:.1f} с').format(rounds, done, elapsed))
        # Событие для внешних команд: агрегатор может раз в круг отправлять
        # отчёт, чистить логи, пересобирать подписки на своей стороне.
        self.commands.notify_event("on_round")
        self._refresh_stats()
        return done

    def _purge_failed(self) -> int:
        """Убрать из пула нерабочие записи, у которых истёк срок годности.

        Статус «Не работает» терминальный: такие записи больше не опрашиваются,
        и без автоочистки они копились бы в ``pool.json`` вечно.  Проверка идёт
        не каждый круг (``PURGE_MIN_GAP``), а ``simple_failed_ttl_hours = 0``
        выключает её полностью — тогда записи удаляются только вручную.
        """
        ttl = self.policy.failed_ttl_seconds
        if ttl <= 0:
            return 0
        now = time.time()
        with self._lock:
            if now - self._last_purge_at < PURGE_MIN_GAP:
                return 0
            self._last_purge_at = now
        removed = self.pool.purge_failed(now, ttl)
        if removed:
            with self._lock:
                self.stats.purged_failed += removed
            self._emit_log(
                t('Убрано нерабочих серверов по сроку годности (>{:g} ч): {}').format(ttl / 3600.0, removed))
            self.pool.save(force=True)
        return removed

    def _cores_ready(self) -> bool:
        """Проверить наличие ядра; предупредить один раз, если его нет."""
        ready = bool(self.prober.has_core())
        warn = False
        with self._lock:
            self.stats.core_ready = ready
            if ready:
                self._warned_no_core = False
            elif not self._warned_no_core:
                self._warned_no_core = True
                warn = True
        if warn:
            self._emit_log(
                t("ВНИМАНИЕ: ядра Xray/sing-box не найдены — этапы 2 и 3 "
                "пропускаются. Укажите путь к ядру в настройках или "
                "включите автозагрузку."))
        return ready

    # -- этапы -----------------------------------------------------------
    def _phase_mode(self) -> bool:
        """``True`` — за круг этап обрабатывается целиком (режим «фазы»)."""
        return str(self.settings.get("simple_stage_mode",
                                     "cascade")).lower() == "phases"

    def _run_stage(self, level: int, states: Optional[Sequence[str]] = None) -> int:
        """Обработать один этап.

        В режиме ``cascade`` это ровно одна порция за круг: список быстро
        чистится дешёвым пингом, а дорогие проверки ядром получают лишь
        небольшую часть выживших.  В режиме ``phases`` этап доводится до конца
        (все порции подряд), и только потом начинается следующий этап.

        ``states`` ограничивает выборку состояниями записей (например, этап
        трафика обрабатывает только ``connected``: рабочие серверы проходят
        отдельную пакетную перепроверку).
        """
        total = 0
        rounds = 0
        while not self._stop.is_set() and self._await_pause():
            processed = self._run_batch(level, states)
            total += processed
            rounds += 1
            if processed == 0 or not self._phase_mode():
                break
            if rounds >= 500:          # предохранитель от бесконечного цикла
                break
        return total

    def _batch_settings(self, level: int) -> Tuple[int, int]:
        """``(размер порции, число потоков)`` для указанного этапа."""
        try:
            core_workers = int(self.settings.get("simple_core_workers", 8) or 8)
        except (TypeError, ValueError):
            core_workers = 8
        if level == LEVEL_PING:
            try:
                limit = int(self.settings.get("simple_ping_batch", 512) or 0)
            except (TypeError, ValueError):
                limit = 512
            try:
                workers = int(self.settings.get("simple_ping_workers", 128) or 1)
            except (TypeError, ValueError):
                workers = 128
        elif level == LEVEL_CORE:
            try:
                limit = int(self.settings.get("simple_core_batch", 48) or 0)
            except (TypeError, ValueError):
                limit = 48
            workers = core_workers
        else:
            try:
                limit = int(self.settings.get("simple_traffic_batch", 24) or 0)
            except (TypeError, ValueError):
                limit = 24
            workers = core_workers
        return max(0, limit), max(1, workers)

    def _run_batch(self, level: int,
                   states: Optional[Sequence[str]] = None) -> int:
        """Обработать одну порцию серверов, которым пора пройти этап."""
        limit, workers = self._batch_settings(level)
        items = self.pool.select(level, self.policy, time.time(), limit,
                                 states=states)
        if not items:
            return 0
        label = STAGE_LABELS.get(level, str(level))
        with self._lock:
            self.stats.stage = label
        self._emit_log(t('Этап «{}»: {} сервер(ов) в очереди').format(label, len(items)))

        queue = list(items)
        queue_lock = threading.Lock()
        processed = 0

        def worker() -> None:
            nonlocal processed
            while True:
                if self._stop.is_set() or not self._await_pause():
                    return
                with queue_lock:
                    if not queue:
                        return
                    entry = queue.pop(0)
                try:
                    self._process(level, entry)
                except Exception as exc:  # noqa: BLE001
                    self._emit_log(t('Ошибка проверки {}: {}').format(entry.endpoint, exc))
                with queue_lock:
                    processed += 1

        threads = [
            threading.Thread(target=worker, name=f"simple-l{level}", daemon=True)
            for _ in range(min(workers, len(items)))
        ]
        with self._workers_lock:
            self._workers.update(threads)
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                if self._stop.is_set():
                    break
                time.sleep(0.1)
            for thread in threads:
                thread.join(timeout=0.5)
        finally:
            with self._workers_lock:
                self._workers.difference_update(threads)
        with self._lock:
            self.stats.stage = ""
        return processed

    # -- пакетная перепроверка рабочих -----------------------------------
    def _network_online(self) -> bool:
        """Есть ли связь (предохранитель можно выключить настройкой)."""
        if not bool(self.settings.get("simple_network_guard", True)):
            return True
        with self._lock:
            return bool(self.stats.network_online)

    def _core_paths(self) -> Dict[str, Optional[str]]:
        """Пути найденных ядер — для пакетной перепроверки."""
        getter = getattr(self.prober, "core_paths", None)
        if not callable(getter):
            return {}
        try:
            xray, singbox = getter()
        except Exception:  # noqa: BLE001 - без ядер пачки не запускаем
            return {}
        return {core_config.XRAY: xray, core_config.SINGBOX: singbox}

    def _ping_workers(self) -> int:
        try:
            return max(1, int(self.settings.get("simple_ping_workers", 128) or 1))
        except (TypeError, ValueError):
            return 128

    def _track_workers(self, threads: Sequence[threading.Thread]) -> None:
        """Зарегистрировать рабочие потоки: остановка ждёт их завершения."""
        with self._workers_lock:
            self._workers.update(threads)

    def _maintenance_quota(self, due: Sequence[ServerEntry]
                           ) -> List[ServerEntry]:
        """Сколько рабочих подтверждаем за один проход.

        Доля берётся от **всех** рабочих, а не от «просроченных»: так часть
        выходного списка гарантированно не проверяется в этом проходе, и сбой
        пачки не может оставить файл вообще без серверов.
        """
        total = max(self.pool.working_count(), len(due))
        quota = max(1, int(round(total * self.policy.maintenance_fraction)))
        return list(due[:quota])

    def _run_maintenance(self) -> int:
        """Подтвердить рабочие серверы пачками: уровень 3 без уровня 2.

        Уровень 2 (рукопожатие) здесь не нужен: он ничего не доказывает про
        креденшелы, а рабочим сервер делает именно контрольный запрос.
        """
        if not self._network_online():
            return 0
        due = self.pool.select(LEVEL_TRAFFIC, self.policy, time.time(), 0,
                              states={SimpleState.WORKING.value})
        if not due:
            return 0
        entries = self._maintenance_quota(due)
        policy = self.policy
        with self._lock:
            self.stats.stage = t("Пакетная перепроверка рабочих")
        self._emit_log(
            t('Пакетная перепроверка: {} из {} рабочих (доля {:g}, до {} серверов на процесс)').format(len(entries), max(self.pool.working_count(), len(due)), policy.maintenance_fraction, policy.maintenance_max))
        alive = self._ping_gate(entries)
        if not alive or self._stop.is_set() or not self._network_online():
            with self._lock:
                self.stats.stage = ""
            return 0
        return self._run_maintenance_batches(alive)

    def _ping_gate(self, entries: Sequence[ServerEntry]) -> List[ServerEntry]:
        """Дешёвый пинг перед дорогой проверкой пачки.

        Контрольный запрос бессмыслен для адреса, который не отвечает по TCP:
        такой сервер обрабатывается обычными правилами уровня 1 (пауза
        ``simple_retry_minutes`` и счётчик неудач), а пачка за него не платит.

        Если «не ответили» почти все — это похоже на общую проблему со связью:
        результаты не применяются, статусы серверов не меняются.
        """
        if not entries:
            return []
        queue = list(entries)
        outcomes: List[Tuple[ServerEntry, Outcome]] = []
        lock = threading.Lock()

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    entry = queue.pop(0)
                try:
                    outcome = self.prober.probe(entry.config(), LEVEL_PING)
                except Exception as exc:  # noqa: BLE001 - сбой не рушит пачку
                    outcome = Outcome(ok=False, level=LEVEL_PING,
                                      error=t('ошибка пинга: {}').format(exc))
                with lock:
                    outcomes.append((entry, outcome))

        threads = [threading.Thread(target=worker, name="maint-ping",
                                    daemon=True)
                   for _ in range(min(self._ping_workers(),
                                      max(1, len(queue))))]
        self._track_workers(threads)
        for thread in threads:
            thread.start()
        deadline = time.time() + 60.0
        for thread in threads:
            thread.join(timeout=max(0.1, deadline - time.time()))
        with self._workers_lock:
            self._workers.difference_update(threads)
        if self._stop.is_set() or not self._network_online():
            return []

        dead = [(entry, outcome) for entry, outcome in outcomes
                if not outcome.ok]
        alive = [entry for entry, outcome in outcomes if outcome.ok]
        total = len(outcomes)
        ratio = (len(dead) / float(total)) if total else 0.0
        if total >= GUARD_MIN_SAMPLE and ratio >= self.policy.maintenance_guard:
            with self._lock:
                self.stats.maintenance_guard_skips += 1
            self._emit_log(
                t('Пакетная перепроверка отменена: не ответили {} из {} ({:.0%}) — похоже на сбой связи. Статусы серверов не меняются.').format(len(dead), total, ratio))
            return []
        for entry, outcome in dead:
            action = self._apply_outcome(entry.fp, LEVEL_PING, outcome)
            if action:
                self._account(LEVEL_PING, outcome, action, entry)
                self._notify(entry.fp)
        return alive

    def _run_maintenance_batches(self, entries: Sequence[ServerEntry]) -> int:
        """Проверить пачки и применить результаты.

        Сбой отдельного сервера сначала подтверждается одиночной проверкой: у
        пакетной схемы выше цена ошибки, а «разжалование» рабочего сервера
        убирает его из выходного файла на время паузы.
        """
        paths = self._core_paths()
        if not paths:
            self._emit_log(t("Пакетная перепроверка невозможна: ядра не найдены."))
            with self._lock:
                self.stats.stage = ""
            return 0
        policy = self.policy
        try:
            workers = max(1, int(self.settings.get("simple_core_workers", 8) or 8))
        except (TypeError, ValueError):
            workers = 8
        reports = run_batches(
            self.prober.runner, self.settings, entries, paths,
            max_per_process=policy.maintenance_max, workers=workers,
            start_timeout=min(6.0, _number_setting(self.settings,
                                                   "core_timeout", 12.0)),
            request_timeout=_number_setting(self.settings, "traffic_timeout",
                                            8.0))
        summary = reports_summary(reports)
        checked = int(summary["checked"])
        with self._lock:
            self.stats.maintenance_batches = int(summary["batches"])
            self.stats.stage = ""
        if summary["infra_error"]:
            self._emit_log(
                t('Пакетная перепроверка прервана: {}. Проверено {} — результаты не применены.').format(summary['infra_error'], checked))
            return 0
        if self._stop.is_set() or not self._network_online():
            self._emit_log(t("Пакетная перепроверка прервана: пропала связь. "
                           "Результаты не применены."))
            return 0
        failed = int(summary["failed"])
        if checked and checked >= GUARD_MIN_SAMPLE \
                and failed / checked >= policy.maintenance_guard:
            with self._lock:
                self.stats.maintenance_guard_skips += 1
            self._emit_log(
                t('Пакетная перепроверка отменена: провалились {} из {} ({:.0%}) — похоже на сбой связи или ядра. Статусы серверов не меняются.').format(failed, checked, failed / checked))
            return 0
        return self._apply_batch_results(entries, reports, summary)

    def _apply_batch_results(self, entries: Sequence[ServerEntry],
                             reports: Sequence[Any],
                             summary: Dict[str, Any]) -> int:
        """Применить результаты пачек: успехи фиксируются, провалы — с проверкой."""
        by_fp: Dict[str, Any] = {}
        for report in reports:
            for item in report.results:
                by_fp[item.fp] = item
        applied = 0
        confirmed = 0
        demoted = 0
        for entry in entries:
            item = by_fp.get(entry.fp)
            if item is None:
                continue
            if item.ok:
                outcome = Outcome(ok=True, level=LEVEL_TRAFFIC,
                                  latency_ms=item.latency_ms)
                action = self._apply_outcome(entry.fp, LEVEL_TRAFFIC, outcome)
                if action:
                    self._account(LEVEL_TRAFFIC, outcome, action, entry)
                    self._notify(entry.fp)
                    applied += 1
                    confirmed += 1
                continue
            verdict = self._recheck_before_demote(entry, item.error)
            if verdict == "skipped":
                continue
            applied += 1
            if verdict == "kept":
                confirmed += 1
            else:
                demoted += 1
        with self._lock:
            self.stats.maintenance_checked += applied
            self.stats.maintenance_ok += confirmed
            self.stats.maintenance_failed += demoted
        self._emit_log(
            t('Пакетная перепроверка завершена: подтверждено {}, провалились {}, процессов ядра {} за {:.1f} с').format(confirmed, demoted, summary['batches'], summary['elapsed']))
        return applied

    def _recheck_before_demote(self, entry: ServerEntry,
                               batch_error: str) -> str:
        """Подтвердить провал пакетной проверки одиночным тестом.

        Пакетная схема дешевле, но у неё выше цена ошибки: сбой процесса,
        локального порта или связи не должен «разжаловать» рабочий сервер.
        Поэтому перед понижением статуса сервер проверяется обычным контуром.

        Возвращает ``"kept"`` (сервер жив, зафиксирован успех), ``"demoted"``
        (неудача подтверждена и применена) или ``"skipped"`` (ничего не
        менялось: проверка не удалась или пропала связь).
        """
        try:
            cfg = entry.config()
        except Exception:  # noqa: BLE001 - битую конфигурацию понижаем сразу
            outcome = Outcome(ok=False, level=LEVEL_TRAFFIC, error=batch_error,
                              permanent=True)
            action = self._apply_outcome(entry.fp, LEVEL_TRAFFIC, outcome)
            if action:
                self._account(LEVEL_TRAFFIC, outcome, action, entry)
                self._notify(entry.fp)
            return "demoted"
        try:
            confirm = self.prober.probe(cfg, LEVEL_TRAFFIC)
        except Exception as exc:  # noqa: BLE001 - не уверены — не понижаем
            self._emit_log(t('Повторная проверка не удалась ({}): {}').format(entry.endpoint, exc))
            return "skipped"
        if self._stop.is_set() or not self._network_online():
            return "skipped"
        if confirm.ok:
            self._emit_log(
                t('Сервер подтверждён повторной проверкой: {}').format(entry.endpoint))
            action = self._apply_outcome(entry.fp, LEVEL_TRAFFIC, confirm)
            if action:
                self._account(LEVEL_TRAFFIC, confirm, action, entry)
                self._notify(entry.fp)
            return "kept"
        if confirm.no_core:
            # Ядро пропало — это не вина сервера: статус не меняем.
            return "skipped"
        outcome = Outcome(ok=False, level=LEVEL_TRAFFIC,
                          error=confirm.error or batch_error,
                          permanent=confirm.permanent)
        action = self._apply_outcome(entry.fp, LEVEL_TRAFFIC, outcome)
        if action:
            self._account(LEVEL_TRAFFIC, outcome, action, entry)
            self._notify(entry.fp)
        return "demoted"

    # -- обработка одного сервера ----------------------------------------
    def _process(self, level: int, entry: ServerEntry) -> None:
        """Прогнать один сервер по указанному этапу и применить результат."""
        try:
            cfg = entry.config()
        except Exception as exc:  # noqa: BLE001
            self._fail_permanent(entry, t('некорректная конфигурация: {}').format(exc))
            return
        outcome = self.prober.probe(cfg, level)
        if self._stop.is_set() or not self._network_online():
            # Проверку оборвала остановка цикла или пропала связь: результат не
            # применяем, иначе серверы получали бы «неудачи» из-за обрыва сети
            # или выключения приложения.
            return
        action = self._apply_outcome(entry.fp, level, outcome)
        if action is None:          # запись успели удалить параллельно
            return
        self._account(level, outcome, action, entry)
        self._notify(entry.fp)

    def _fail_permanent(self, entry: ServerEntry, error: str) -> None:
        """Пометить конфигурацию как неподдерживаемую (повторно не проверяем)."""
        now = time.time()

        def mutate(live: ServerEntry) -> str:
            live.state = SimpleState.UNSUPPORTED.value
            live.level = 0
            live.last_error = error
            live.last_checked = now
            return "unsupported"

        action = self.pool.update(entry.fp, mutate)
        if action == "unsupported":
            with self._lock:
                self.stats.unsupported_total += 1
            self._emit_log(
                t('Исключено (не поддерживается): {} — {}').format(entry.endpoint, error))
            self._notify(entry.fp)

    def _apply_outcome(self, fp: str, level: int,
                       outcome: Outcome) -> Optional[str]:
        """Применить результат этапа к записи пула; вернуть действие."""
        now = time.time()
        policy = self.policy
        if level == LEVEL_PING:
            handler = self._apply_ping
        elif level == LEVEL_CORE:
            handler = self._apply_core
        else:
            handler = self._apply_traffic

        def mutator(live: ServerEntry) -> str:
            return handler(live, outcome, now, policy)

        return self.pool.update(fp, mutator)

    # -- переходы состояний ----------------------------------------------
    def _exhausted(self, entry: ServerEntry, fails: int) -> str:
        """Сервер исчерпал попытки: удалить или пометить нерабочим.

        По умолчанию (``simple_delete_failed`` выключен) запись остаётся в пуле
        со статусом «Не работает»: её видно в таблице, и её можно осознанно
        удалить или внести адрес в чёрный список — раньше автоматическое
        удаление съедало сервер раньше, чем это становилось возможным.
        Дальше такая запись не опрашивается; вернуть её можно кнопкой
        «Сбросить в «новые»».
        """
        if bool(self.settings.get("simple_delete_failed", False)):
            self.pool.block(entry.fp, entry.uri, entry.last_error, fails)
            return "delete"
        entry.state = SimpleState.FAILED.value
        entry.level = 0
        entry.next_check_at = 0.0
        entry.last_error = entry.last_error or t("проверку не прошёл")
        return "failed"

    def _apply_ping(self, entry: ServerEntry, outcome: Outcome, now: float,
                    policy: Policy) -> str:
        """Этап 1: доступность адреса."""
        entry.last_checked = now
        if outcome.ok:
            entry.ping_fail = 0
            if outcome.latency_ms is not None:
                entry.latency_ms = outcome.latency_ms
            entry.last_ok = now
            entry.last_ping_at = now
            entry.last_error = ""
            if entry.state == SimpleState.WORKING.value:
                # Плановая перепроверка рабочего сервера: статус подтверждён.
                entry.next_check_at = now + policy.ping_interval
                return "working_ping_ok"
            entry.state = SimpleState.REACHABLE.value
            entry.level = max(entry.level, 1)
            entry.next_check_at = now
            return "ping_ok"

        entry.ping_fail += 1
        entry.last_error = outcome.error or t("пинг не прошёл")
        if entry.ping_fail >= policy.max_failures:
            return self._exhausted(entry, entry.ping_fail)
        if entry.state == SimpleState.WORKING.value:
            # Рабочий сервер перестал отвечать — разжалуем в «новый».
            entry.state = SimpleState.NEW.value
            entry.level = 0
        entry.next_check_at = now + policy.retry_seconds
        return "ping_fail"

    def _apply_core(self, entry: ServerEntry, outcome: Outcome, now: float,
                    policy: Policy) -> str:
        """Этап 2: рукопожатие реального ядра."""
        entry.last_checked = now
        if outcome.ok:
            entry.core_fail = 0
            entry.state = SimpleState.CONNECTED.value
            entry.level = max(entry.level, 2)
            if outcome.latency_ms is not None:
                entry.latency_ms = outcome.latency_ms
            entry.last_ok = now
            entry.last_error = ""
            entry.next_check_at = now
            return "core_ok"
        if outcome.no_core:
            # Ядро не установлено — это не неудача конфигурации.
            entry.last_error = outcome.error
            entry.next_check_at = now + policy.retry_seconds
            return "skip"
        if outcome.permanent:
            entry.state = SimpleState.UNSUPPORTED.value
            entry.level = 0
            entry.last_error = outcome.error
            return "unsupported"
        entry.core_fail += 1
        entry.last_error = outcome.error or t("ядро не смогло подключиться")
        if entry.core_fail >= policy.max_failures:
            return self._exhausted(entry, entry.core_fail)
        # Возвращаем в общий пул: сервер снова пройдёт пинг и путь заново.
        entry.state = SimpleState.NEW.value
        entry.level = 0
        entry.next_check_at = now + policy.retry_seconds
        return "core_fail"

    def _apply_traffic(self, entry: ServerEntry, outcome: Outcome, now: float,
                       policy: Policy) -> str:
        """Этап 3: контрольный трафик через туннель."""
        entry.last_checked = now
        if outcome.ok:
            was_working = entry.state == SimpleState.WORKING.value
            entry.traffic_fail = 0
            entry.state = SimpleState.WORKING.value
            entry.level = 3
            if outcome.latency_ms is not None:
                entry.latency_ms = outcome.latency_ms
            entry.last_ok = now
            entry.last_ping_at = now
            entry.last_traffic_at = now
            entry.last_error = ""
            entry.next_check_at = now
            return "working_ok" if was_working else "traffic_ok"
        if outcome.no_core:
            entry.last_error = outcome.error
            entry.next_check_at = now + policy.retry_seconds
            return "skip"
        if outcome.permanent:
            entry.state = SimpleState.UNSUPPORTED.value
            entry.level = 0
            entry.last_error = outcome.error
            return "unsupported"
        entry.traffic_fail += 1
        entry.last_error = outcome.error or t("контрольный трафик не прошёл")
        if entry.traffic_fail >= policy.max_failures:
            return self._exhausted(entry, entry.traffic_fail)
        entry.state = SimpleState.NEW.value
        entry.level = 0
        entry.next_check_at = now + policy.retry_seconds
        return "traffic_fail"

    def _account(self, level: int, outcome: Outcome, action: str,
                 entry: ServerEntry) -> None:
        """Обновить счётчики и записать в журнал значимые события."""
        with self._lock:
            stats = self.stats
            if action == "delete":
                stats.deleted += 1
            elif action == "failed":
                stats.exhausted += 1
            elif action == "unsupported":
                stats.unsupported_total += 1
            elif action in ("ping_fail", "core_fail", "traffic_fail"):
                stats.demoted += 1
            if level == LEVEL_PING:
                if outcome.ok:
                    stats.ping_ok += 1
                else:
                    stats.ping_fail += 1
            elif level == LEVEL_CORE:
                if outcome.ok:
                    stats.core_ok += 1
                else:
                    stats.core_fail += 1
            else:
                if outcome.ok:
                    stats.traffic_ok += 1
                else:
                    stats.traffic_fail += 1

        limit = self.policy.max_failures
        error = outcome.error or t("ошибка")
        if action == "delete":
            self._emit_log(
                t('Удалён после {} неудач: {} — {}').format(limit, entry.endpoint, error))
        elif action == "failed":
            self._emit_log(
                t('Помечен как нерабочий после {} неудач: {} — {}. Запись осталась в пуле: её можно удалить или внести адрес в чёрный список').format(limit, entry.endpoint, error))
        elif action == "unsupported":
            self._emit_log(
                t('Исключено (не поддерживается): {} — {}').format(entry.endpoint, error))
        elif action == "ping_fail":
            self._emit_log(
                t('Пинг не прошёл ({}/{}): {} — {}').format(entry.ping_fail + 1, limit, entry.endpoint, error))
        elif action == "core_fail":
            self._emit_log(
                t('Ядро не смогло подключиться ({}/{}): {} — {}').format(entry.core_fail + 1, limit, entry.endpoint, error))
        elif action == "traffic_fail":
            self._emit_log(
                t('Трафик не прошёл ({}/{}): {} — {}').format(entry.traffic_fail + 1, limit, entry.endpoint, error))
        elif action == "traffic_ok":
            latency = outcome.latency_ms or 0
            self._emit_log(
                t('Рабочий сервер найден: {} ({} мс)').format(entry.endpoint, latency))

    # -- источники и импорт ----------------------------------------------
    def _maybe_refresh_sources(self, force: bool = False) -> bool:
        """Раз в N часов скачать включённые подписки и добавить новые записи."""
        now = time.time()
        try:
            interval = float(self.settings.get("simple_source_refresh_hours",
                                               6.0)) * 3600.0
        except (TypeError, ValueError):
            interval = 6 * 3600.0
        interval = max(60.0, interval)
        with self._lock:
            if not force and now < self.stats.next_refresh_at:
                return False
            self.stats.next_refresh_at = now + interval

        # Обновление может быть запрошено одновременно кнопкой в интерфейсе и
        # фоновым циклом — второй запрос тихо пропускается.
        if not self._refresh_lock.acquire(blocking=False):
            return False
        try:
            return self._refresh_sources_locked(now)
        finally:
            self._refresh_lock.release()

    def _refresh_sources_locked(self, now: float) -> bool:
        self.pool.purge_blacklist(now, self.policy)
        sources = self.sources()
        enabled = [s for s in sources if s.get("enabled", True)]
        if not enabled:
            with self._lock:
                self.stats.last_refresh_at = now
            return False

        timeout = float(self.settings.get("simple_fetch_timeout", 30.0))
        user_agent = str(self.settings.get("simple_user_agent",
                                           "v2rayNG/1.8.5"))
        seen: Set[str] = set()
        for source in enabled:
            kind = str(source.get("kind") or "url")
            value = str(source.get("value") or "")
            label = str(source.get("name") or value)
            result = fetcher.fetch_source(kind, value, timeout=timeout,
                                          user_agent=user_agent)
            source["last_fetch"] = time.time()
            if not result.ok:
                source["last_status"] = f"ошибка: {result.error}"
                source["last_count"] = 0
                self._emit_log(t('Источник «{}»: {}').format(label, result.error))
                continue
            text = fetcher.normalise_source_payload(result.text)
            uris = parser.extract_uris(text)
            counters = self.import_uris(uris, label, seen=seen)
            source["last_status"] = "ок"
            source["last_count"] = int(counters["added"])
            self._emit_log(
                t('Источник «{}»: получено {}, добавлено {}, повторно {}, отфильтровано {}, исключено ранее {}, битых {}').format(label, len(uris), counters['added'], counters['duplicate'], counters['filtered'], counters['blocked'], counters['invalid']))

        self.set_sources(sources)
        self.pool.save()
        with self._lock:
            self.stats.last_refresh_at = time.time()
        self._refresh_stats()
        return True

    def refresh_sources_now(self) -> bool:
        """Принудительно обновить подписки (кнопка «Обновить списки»)."""
        return self._maybe_refresh_sources(force=True)

    def import_uris(self, uris: Sequence[str], label: str = t("вставка"),
                    seen: Optional[Set[str]] = None) -> Dict[str, int]:
        """Разобрать, отфильтровать и добавить share-URI в пул."""
        now = time.time()
        policy = self.policy
        allowed = enabled_simple_protocols(self.settings)
        counters = {"received": 0, "added": 0, "duplicate": 0,
                    "filtered": 0, "invalid": 0, "blocked": 0}
        known = seen if seen is not None else set()
        batch: List[ServerEntry] = []

        for uri in uris:
            counters["received"] += 1
            cfg = parser.parse_uri(uri)
            if cfg is None or parser.validate(cfg):
                counters["invalid"] += 1
                continue
            if cfg.protocol.value not in allowed:
                counters["filtered"] += 1
                continue
            if self.pool.is_blocked_address(cfg.address, cfg.port):
                # Постоянный чёрный список адресов: такой сервер не добавляем и
                # никогда не опрашиваем, даже если он снова пришёл из подписки.
                counters["blocked"] += 1
                continue
            fp = cfg.dedup_key()
            if fp in known:
                counters["duplicate"] += 1
                continue
            known.add(fp)
            if self.pool.blocked(fp, now, policy):
                counters["blocked"] += 1
                continue
            if self.pool.has(fp):
                counters["duplicate"] += 1
                continue
            data = cfg.to_dict()
            if not str(data.get("uri") or "").strip():
                # Ссылка восстанавливается после перезапуска именно из ``data``,
                # поэтому пустое поле подстраховываем исходной строкой вставки.
                data["uri"] = uri.strip()
            batch.append(ServerEntry(
                fp=fp,
                uri=str(data.get("uri") or "").strip(),
                data=data,
                state=SimpleState.NEW.value,
                source=label,
                first_seen=now,
                next_check_at=0.0,
            ))

        counters["added"] = self.pool.add_many(batch)
        with self._lock:
            self.stats.added += counters["added"]
            self.stats.filtered += counters["filtered"]
            self.stats.invalid += counters["invalid"]
            self.stats.duplicates += counters["duplicate"]
        if counters["added"]:
            self.pool.save()
        return counters

    def import_text(self, text: str, label: str = t("вставка")) -> Dict[str, int]:
        """Добавить конфигурации из произвольного текста (в т.ч. base64)."""
        return self.import_uris(parser.extract_uris(text or ""), label)

    # -- удаление и чёрный список ----------------------------------------
    def delete_entries(self, fps: Sequence[str]) -> Dict[str, int]:
        """Удалить конфигурации из пула.

        Это именно чистка рабочей области: ни адреса, ни конфигурации в
        исключения не попадают, поэтому сервер может вернуться из подписки.
        Для «больше не трогать этот адрес» есть отдельное действие —
        :meth:`blacklist_entries`.
        """
        removed = self.pool.remove(list(fps))
        if removed:
            self.pool.save(force=True)
            with self._lock:
                self.stats.deleted += removed
            self._emit_log(t('Удалено вручную: {} конфигураций (в чёрный список не заносились)').format(removed))
        return {"removed": removed,
                "blacklist": self.pool.blocked_address_count()}

    def blacklist_entries(self, fps: Sequence[str],
                          reason: str = t("внесён вручную")) -> Dict[str, int]:
        """Внести адреса выбранных серверов в постоянный чёрный список.

        Отдельная от удаления процедура: адрес попадает в чёрный список по
        ``host:port``, из пула убираются все конфигурации этого адреса, и
        больше он не добавляется из подписок и не опрашивается (пока не уберёте
        его вручную в фильтре «Чёрный список адресов»).
        """
        before = self.pool.size()
        selected = 0
        addresses = 0
        for fp in fps:
            entry = self.pool.get(fp)
            if entry is None:
                continue
            is_new, _dropped = self.pool.block_address(entry.address,
                                                       entry.port, reason)
            selected += 1
            addresses += 1 if is_new else 0
        removed = before - self.pool.size()
        counters = {"selected": selected, "removed": removed,
                    "addresses": addresses,
                    "blacklist": self.pool.blocked_address_count()}
        if selected:
            self.pool.save(force=True)
            self._emit_log(
                t('В чёрный список: {} новых адресов (всего {}), из пула убрано {} записей — адреса больше не проверяются').format(addresses, self.pool.blocked_address_count(), removed))
        return counters

    def blacklist_input(self, text: str,
                        reason: str = t("внесён вручную")) -> Dict[str, int]:
        """Внести в чёрный список адреса, введённые руками.

        Принимает список, разделённый пробелами, запятыми или переводами строк.
        Элемент может быть:

        * ссылкой на сервер (``vless://…``, ``vmess://…`` и т.п.) — берётся её
          ``host:port``;
        * парой ``host:port`` — блокируется только этот порт;
        * просто ``host`` (IP или имя, IPv6 — в квадратных скобках) — тогда
          блокируются **все** порты этого адреса: удобно для заведомо «мусорных»
          площадок вроде фишинговых или сниферских узлов.
        """
        counters = {"values": 0, "addresses": 0, "removed": 0, "invalid": 0}
        for item in re.split(r"[\s,;]+", (text or "").strip()):
            if not item:
                continue
            counters["values"] += 1
            host, port = parse_blacklist_item(item)
            if not host:
                counters["invalid"] += 1
                continue
            is_new, dropped = self.pool.block_address(host, port, reason)
            counters["addresses"] += 1 if is_new else 0
            counters["removed"] += int(dropped)
        if counters["addresses"] or counters["removed"]:
            self.pool.save(force=True)
        if counters["addresses"] or counters["removed"]:
            self._emit_log(
                t('В чёрный список вручную: {} новых адрес(ов), из пула убрано {} записей (всего адресов: {})').format(counters['addresses'], counters['removed'], self.pool.blocked_address_count()))
        elif counters["invalid"]:
            self._emit_log(
                t('В чёрный список: не удалось разобрать {} значение(й)').format(counters['invalid']))
        return counters

    def unblock_address(self, host: str, port: int) -> bool:
        """Убрать адрес из чёрного списка: сервер снова можно добавлять."""
        removed = self.pool.unblock_address(host, port)
        if removed:
            self.pool.save(force=True)
            self._emit_log(t('Адрес убран из чёрного списка: {}:{}').format(host, port))
        return removed

    def clear_blocklist(self) -> Dict[str, int]:
        """Полностью очистить чёрный список адресов (и «кладбище»)."""
        addresses = self.pool.clear_blocked_addresses()
        blocked = self.pool.clear_blacklist()
        if addresses or blocked:
            self.pool.save(force=True)
            self._emit_log(t('Чёрный список очищен: адресов {}, конфигураций {}').format(addresses, blocked))
        return {"addresses": addresses, "blocked": blocked}

    # -- выходной файл ----------------------------------------------------
    def _notify_output(self, path: str, count: int) -> None:
        for callback in list(self._on_output):
            try:
                callback(path, count)
            except Exception:
                pass

    def _flush_output(self, force: bool = False) -> bool:
        """Обновить выходные файлы, если список рабочих серверов изменился."""
        fmt = self.output_format()
        path = self._output_path()
        if path != self.writer.path:
            self.writer = OutputWriter(path)
            force = True
        json_path = self._v2rayn_path()
        if json_path != self.json_writer.path:
            self.json_writer = OutputWriter(json_path)
            force = True

        entries = [entry for entry in self.pool.working() if entry.uri.strip()]
        entries.sort(key=lambda item: (item.latency_ms is None,
                                       item.latency_ms or 0, item.endpoint))
        uris = [entry.uri.strip() for entry in entries]
        changed = False

        if fmt in (OUTPUT_URI, OUTPUT_BOTH):
            if self.writer.write_lines(uris, force=force):
                changed = True
                self._emit_log(t('Файл вывода обновлён: {} конфигураций → {}').format(len(uris), self.writer.path))
                self._notify_output(self.writer.path, len(uris))

        if fmt in (OUTPUT_V2RAYN, OUTPUT_BOTH):
            text, included, skipped = build_client_config(
                self._configs(entries), probe_url=self._probe_url())
            if self.json_writer.write(text, text, included, force=force):
                changed = True
                note = t(', групповая конфигурация: {} сервер(ов)').format(included)
                if skipped:
                    note += t(', пропущено {} (не поддерживаются Xray)').format(skipped)
                self._emit_log(t('Файл для v2rayN обновлён: {}{}').format(self.json_writer.path, note))
                self._notify_output(self.json_writer.path, included)

        with self._lock:
            stats = self.stats
            stats.output_format = fmt
            stats.output_path = self.writer.path
            stats.output_count = self.writer.count
            stats.v2rayn_path = self.json_writer.path
            stats.v2rayn_count = self.json_writer.count
            if changed:
                stats.output_written_at = max(self.writer.written_at,
                                              self.json_writer.written_at)
        if changed:
            # Файлы обновились — сообщаем внешним командам (триггер on_change).
            self.commands.notify_output_changed()
        return changed

    def write_output_now(self) -> bool:
        """Принудительно перезаписать выходные файлы."""
        return self._flush_output(force=True)

    def artifacts(self) -> Dict[str, Any]:
        """Пути выходных файлов и число серверов — для внешних команд."""
        return {
            "file": self.writer.path,
            "json": self.json_writer.path,
            "count": self.writer.count,
            "v2rayn_count": self.json_writer.count,
            "format": self.output_format(),
        }

    def run_external_commands(self, timeout: float = 300.0) -> int:
        """Выполнить внешние команды «здесь и сейчас» (режим без интерфейса).

        Помечает пресеты с триггером «при изменении» как готовые (файлы только
        что перезаписаны), ждёт завершения очереди и останавливает планировщик.
        """
        ready = [preset for preset in self.commands.presets()
                 if preset.enabled and preset.trigger != "manual"]
        if not ready:
            return 0
        self.commands.start()
        self.commands.notify_output_changed()
        try:
            if not self.commands.wait_idle(timeout=timeout):
                self._emit_log(t("Внешние команды: не дождались завершения "
                               "очереди — останавливаем планировщик."))
        finally:
            self.commands.stop()
        return len(ready)

    def working_uris(self) -> List[str]:
        """Ссылки рабочих серверов (по возрастанию задержки)."""
        entries = [entry for entry in self.pool.working() if entry.uri.strip()]
        entries.sort(key=lambda item: (item.latency_ms is None,
                                       item.latency_ms or 0, item.endpoint))
        return [entry.uri.strip() for entry in entries]

    def working_uri_text(self) -> str:
        """Тот же список одной строкой — для буфера обмена.

        Такой текст v2rayN принимает командой «Серверы → добавить сервер(а) из
        буфера обмена» (Ctrl+V) и создаёт отдельную запись на каждую ссылку.
        """
        return "\n".join(self.working_uris())

    # -- статистика -------------------------------------------------------
    def _refresh_stats(self, heavy: bool = True) -> None:
        """Пересчитать статистику; ``heavy=False`` — только дешёвая часть.

        Дорогая часть — «сколько рабочих конфигураций поймёт ядро Xray»: для
        этого восстанавливаются объекты :class:`Config` по всем рабочим записям.
        Интерфейс обновляет счётчики почти каждую секунду, поэтому он просит
        ``heavy=False``: число уже посчитано при записи выходных файлов (или
        явным вызовом ``refresh_stats()``).
        """
        counts = self.pool.counts()
        output_count = self.pool.working_count()
        supported = None
        if heavy:
            supported, _skipped = xray_configs(self._configs(self.pool.working()))
        with self._lock:
            stats = self.stats
            stats.total = self.pool.size()
            stats.new = counts.get(SimpleState.NEW.value, 0)
            stats.reachable = counts.get(SimpleState.REACHABLE.value, 0)
            stats.connected = counts.get(SimpleState.CONNECTED.value, 0)
            stats.working = counts.get(SimpleState.WORKING.value, 0)
            stats.failed = counts.get(SimpleState.FAILED.value, 0)
            stats.unsupported = counts.get(SimpleState.UNSUPPORTED.value, 0)
            stats.blocked = self.pool.blocked_count()
            stats.blocked_addresses = self.pool.blocked_address_count()
            stats.output_format = self.output_format()
            stats.output_path = self.writer.path
            stats.v2rayn_path = self.json_writer.path
            # Числа берутся из пула, а не из файлов: они верны и до первой
            # записи, и во время длинного круга.
            stats.output_count = output_count
            if supported is not None:
                stats.v2rayn_count = len(supported)
        self._emit_stats()

    @staticmethod
    def _configs(entries: Sequence[ServerEntry]) -> List[Config]:
        """Восстановить объекты конфигураций из записей пула."""
        configs: List[Config] = []
        for entry in entries:
            try:
                configs.append(entry.config())
            except Exception:  # noqa: BLE001
                continue
        return configs

    def refresh_stats(self, heavy: bool = True) -> SimpleStats:
        """Публичный пересчёт статистики (для интерфейса и тестов).

        ``heavy=False`` — быстрый режим для частого обновления из интерфейса:
        не пересчитывается, сколько рабочих конфигураций понятно ядру Xray
        (это число обновляется при записи выходных файлов).
        """
        self._refresh_stats(heavy=heavy)
        return self.snapshot()
