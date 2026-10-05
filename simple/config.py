"""Пути, значения по умолчанию и политика повторных проверок simple-версии.

Настройки хранятся в отдельном файле ``data/simple/settings.json``, чтобы его
не путать с настройками ядра.  Базовые
ключи (``xray_path``, ``core_timeout``, ``traffic_url`` ...) берутся из
:data:`app.constants.DEFAULT_SETTINGS` — их читает сам чекер, поэтому
дублировать их здесь не нужно.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.constants import (
    DEFAULT_SETTINGS as APP_DEFAULTS,
    IMPORTABLE_PROTOCOLS,
    PROJECT_DIR,
    Protocol,
)
from app.settings import Settings
from app.i18n import DEFAULT_LANGUAGE, LANGUAGES, t


# Протоколы, включённые в simple-версии по умолчанию.  Trojan выключен: он
# обычно не работает в нашем регионе, и такие ссылки незачем вообще подтягивать
# из подписок.  Галочка в настройках включает его обратно.
DEFAULT_SIMPLE_PROTOCOLS = [p.value for p in IMPORTABLE_PROTOCOLS
                            if p != Protocol.TROJAN]

# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------

SIMPLE_DIR = os.path.join(PROJECT_DIR, "data", "simple")
SIMPLE_SETTINGS_PATH = os.path.join(SIMPLE_DIR, "settings.json")
SIMPLE_POOL_PATH = os.path.join(SIMPLE_DIR, "pool.json")
SIMPLE_OUTPUT_PATH = os.path.join(SIMPLE_DIR, "working.txt")
SIMPLE_V2RAYN_PATH = os.path.join(SIMPLE_DIR, "v2rayN.json")

# Форматы выходного файла: обычный список ссылок и/или готовая пользовательская
# конфигурация Xray, которую v2rayN умеет импортировать офлайн.
OUTPUT_URI = "uri"
OUTPUT_V2RAYN = "v2rayn"
OUTPUT_BOTH = "both"
OUTPUT_FORMATS = (OUTPUT_URI, OUTPUT_V2RAYN, OUTPUT_BOTH)

try:
    os.makedirs(SIMPLE_DIR, exist_ok=True)
except OSError:
    pass


# ---------------------------------------------------------------------------
# Значения по умолчанию (только специфичные для simple-версии ключи)
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_REFRESH_HOURS = 6.0
DEFAULT_RETRY_MINUTES = 15.0
DEFAULT_PING_INTERVAL_MINUTES = 60.0
DEFAULT_TRAFFIC_INTERVAL_HOURS = 6.0
DEFAULT_MAX_FAILURES = 5
DEFAULT_BLACKLIST_HOURS = 12.0
# Срок годности статуса «Не работает»: такие записи больше не опрашиваются, и
# без очистки они копились бы в pool.json вечно.
DEFAULT_FAILED_TTL_HOURS = 72.0
# Частота записи pool.json: файл переписывается целиком (десятки мегабайт), а
# при работе службой месяцами частая запись — это бессмысленный износ SSD.
DEFAULT_SAVE_MIN_SECONDS = 30.0

# Пакетная перепроверка рабочих серверов: за один проход подтверждается лишь
# часть рабочих (по умолчанию четверть), поэтому сбой пачки не может оставить
# выходной файл без серверов — остальные в это время не проверяются вообще.
DEFAULT_MAINTENANCE_FRACTION = 0.25
DEFAULT_MAINTENANCE_MAX = 50
# Доля провалов внутри пачки, при которой проход считается сбоем сети или
# инфраструктуры: результаты не применяются, статусы серверов не меняются.
DEFAULT_MAINTENANCE_GUARD = 0.5

# Оформление окна: темы «день» и «ночь» (палитры — в simple/gui.py).  Тема
# переключается пунктом меню «Вид» или Ctrl+T и запоминается между запусками.
THEME_NAMES = ("day", "night")
DEFAULT_THEME = "day"

SIMPLE_DEFAULTS: Dict[str, Any] = {
    # -- интерфейс -------------------------------------------------------
    # Язык интерфейса; применяется при старте (см. app.i18n.set_language).
    "language": DEFAULT_LANGUAGE,

    # -- источники ------------------------------------------------------
    # [{"kind": "url"|"file", "value": str, "name": str, "enabled": bool,
    #   "last_fetch": float, "last_status": str, "last_count": int}, ...]
    "simple_sources": [],
    "simple_source_refresh_hours": DEFAULT_SOURCE_REFRESH_HOURS,
    "simple_fetch_timeout": 30.0,
    "simple_user_agent": "v2rayNG/1.8.5",

    # -- политика проверок ----------------------------------------------
    "simple_max_failures": DEFAULT_MAX_FAILURES,
    "simple_retry_minutes": DEFAULT_RETRY_MINUTES,
    "simple_ping_interval_minutes": DEFAULT_PING_INTERVAL_MINUTES,
    "simple_traffic_interval_hours": DEFAULT_TRAFFIC_INTERVAL_HOURS,
    # Удалённый сервер не добавляется заново из подписок это время
    # (0 — отключить блокировку).
    "simple_blacklist_hours": DEFAULT_BLACKLIST_HOURS,
    # Автоочистка статуса «Не работает»: нерабочие записи больше не
    # опрашиваются, поэтому без срока годности копятся в pool.json вечно
    # (0 — не удалять их никогда).
    "simple_failed_ttl_hours": DEFAULT_FAILED_TTL_HOURS,
    # Пул сохраняется на диск не чаще этого интервала, секунды: файл
    # переписывается целиком, поэтому пачка изменений пишется одной записью
    # (0 — сохранять при каждом обращении).
    "simple_save_min_seconds": DEFAULT_SAVE_MIN_SECONDS,

    # -- очередь и параллельность ---------------------------------------
    "simple_ping_workers": 128,
    "simple_core_workers": 8,
    "simple_ping_batch": 512,
    "simple_core_batch": 48,
    "simple_traffic_batch": 24,
    "simple_loop_sleep": 1.0,
    "simple_auto_start": True,
    # Порядок обработки этапов внутри круга:
    #   cascade — за круг по одной порции каждого этапа (пинг → ядро → трафик);
    #   phases  — за круг этап обрабатывается целиком, прежде чем перейти к
    #             следующему (сначала весь пинг, потом всё ядро, потом трафик).
    "simple_stage_mode": "cascade",

    # -- плановая перепроверка рабочих серверов --------------------------
    # Уже проверенные серверы подтверждаются одним процессом ядра на пачку:
    # запрошенный уровень 2 пропускается (он ничего не доказывает про
    # креденшелы, см. app/checker.py), выполняется сразу контрольный запрос.
    # Доля берётся от числа рабочих, остальные в этом проходе не трогаются —
    # сбой пачки не может обнулить выходной файл.
    "simple_maintenance_fraction": DEFAULT_MAINTENANCE_FRACTION,
    "simple_maintenance_max": DEFAULT_MAINTENANCE_MAX,
    "simple_maintenance_guard": DEFAULT_MAINTENANCE_GUARD,
    # При потере сети проверки приостанавливаются, а их результаты не
    # применяются: иначе единичный обрыв связи «разжаловал» бы рабочие серверы.
    "simple_network_guard": True,

    # -- фильтр протоколов при импорте ----------------------------------
    "simple_protocols": list(DEFAULT_SIMPLE_PROTOCOLS),

    # -- вывод -----------------------------------------------------------
    # "uri"    — только список ссылок (working.txt);
    # "v2rayn" — только готовая конфигурация Xray для импорта в v2rayN;
    # "both"   — оба файла.
    "simple_output_format": OUTPUT_URI,
    "simple_output_path": SIMPLE_OUTPUT_PATH,
    "simple_v2rayn_path": SIMPLE_V2RAYN_PATH,
    "simple_view_limit": 1000,

    # -- что делать с серверами, не прошедшими проверку -------------------
    # По умолчанию выключено: исчерпавший попытки сервер остаётся в пуле со
    # статусом «Не работает» — так его видно в списке, и его можно осознанно
    # удалить или внести адрес в чёрный список.  Включайте, если пул должен
    # чиститься сам (тогда работает и «Не добавлять удалённые»).
    "simple_delete_failed": False,

    # -- внешние команды -------------------------------------------------
    # Список пресетов: любые консольные команды и скрипты, которые запускаются
    # по триггеру (изменение списка / расписание / события автоматизации).
    # Приложение не знает о транспорте: пользователь сам описывает, что и куда
    # передавать.
    "simple_commands": [],

    # -- слежка за сетью (события «пропала/вернулась») --------------------
    "simple_network_watch": True,
    "simple_network_watch_seconds": 30.0,

    # -- окно и трей -----------------------------------------------------
    # Иконка в области уведомлений делает программу похожей на службу: окно
    # можно закрыть или свернуть, а автоматизация продолжит работать.
    "simple_tray_icon": True,
    "simple_close_to_tray": False,
    "simple_minimize_to_tray": False,
    # Автозапуск при входе в Windows: программа сама создаёт (и удаляет) ярлык в
    # папке «Автозагрузка» пользователя — реестр не трогается (см. simple/autostart.py).
    "simple_start_with_windows": False,
    # Оформление окна: "day" — дневная тема, "night" — ночная.
    "simple_theme": DEFAULT_THEME,
    # Сжатие файла пула (gzip).  Переключаемо: выключенное — обычный JSON,
    # включённое — ``pool.json.gz`` (~6x меньше записи на диск).  Читается
    # всегда оба формата, поэтому переключение безопасно в любой момент.
    "simple_pool_gzip": True,
}

# Ключи, которые читает ядро проверки (``app``): их канонические значения
# берутся из :data:`app.constants.DEFAULT_SETTINGS`, чтобы не дублировать.
BASE_KEYS = (
    "xray_path", "singbox_path", "auto_download_core", "core_base_port",
    "core_timeout", "dns_timeout", "tcp_timeout", "traffic_url",
    "traffic_timeout", "traffic_status", "traffic_expect", "traffic_fallbacks",
    "log_level", "log_to_file", "max_log_lines", "keep_core_debug_files",
    "user_agent",
)
BASE_DEFAULTS: Dict[str, Any] = {key: APP_DEFAULTS[key] for key in BASE_KEYS
                                 if key in APP_DEFAULTS}

# Значения, которые проект задаёт иначе, чем ядро. Применяются
# только если пользователь сам ещё не сохранял этот ключ.
SIMPLE_OVERRIDES: Dict[str, Any] = {
    # Без рабочего ядра этапы 2 и 3 (соединение и трафик) выполнить невозможно,
    # поэтому автозагрузка включена: иначе на чистой машине ни один сервер не
    # станет рабочим.
    "auto_download_core": True,
    # Тайм-ауты подобраны по реальной практике: со стоковыми 3/3/8/5 медленные
    # серверы отваливаются раньше, чем успевают ответить.
    "dns_timeout": 4.0,
    "tcp_timeout": 4.0,
    "core_timeout": 12.0,
    "traffic_timeout": 8.0,
}

# Полный набор ожидаемых настроек: по нему проверяются и «лечатся» значения.
# Порядок важен: битое значение заменяется настройкой проекта, а не стоковой
# из ядра.
DEFAULTS: Dict[str, Any] = {
    **BASE_DEFAULTS,
    **SIMPLE_OVERRIDES,
    **SIMPLE_DEFAULTS,
}

# Строки, которые могут быть пустыми: пустое значение означает «определить
# автоматически» и не заменяется значением по умолчанию.
OPTIONAL_STRINGS = {"xray_path", "singbox_path", "traffic_expect"}
# Числовые интервалы, для которых ноль — осмысленное значение (отключение паузы
# или блокировки), а не признак пустого поля.
ZERO_ALLOWED = {"simple_blacklist_hours", "simple_retry_minutes",
                "simple_failed_ttl_hours", "simple_save_min_seconds"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
STAGE_MODES = ("cascade", "phases")
LANGUAGE_CODES = tuple(code for code, _name in LANGUAGES)

# Внешние команды (нейтральный слой автоматизации).
COMMAND_KINDS = ("script", "command")
COMMAND_TRIGGERS = ("on_change", "interval", "manual", "on_start", "on_stop",
                    "on_round", "on_network_down", "on_network_up")
COMMAND_ARTIFACTS = ("links", "json", "both")
COMMAND_LIMITS = {
    "interval_minutes": (1.0, 10080.0, 60.0),
    "min_gap_seconds": (0.0, 86400.0, 60.0),
    "timeout_seconds": (5.0, 3600.0, 600.0),
    "retry_delay_seconds": (5.0, 3600.0, 60.0),
}
_MISSING = object()


def read_stored_settings(path: str) -> Dict[str, Any]:
    """Прочитать «сырой» файл настроек (без подстановки значений по умолчанию)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean_sources(value: Any) -> List[Dict[str, Any]]:
    items = value if isinstance(value, list) else []
    cleaned: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "url").strip().lower()
        if kind not in ("url", "file"):
            kind = "url"
        source_value = str(item.get("value") or "").strip()
        if not source_value:
            continue
        number = _as_number(item.get("last_count")) or 0
        stamp = _as_number(item.get("last_fetch")) or 0.0
        cleaned.append({
            "kind": kind,
            "value": source_value,
            "name": str(item.get("name") or ""),
            "enabled": bool(item.get("enabled", True)),
            "last_fetch": float(stamp),
            "last_status": str(item.get("last_status") or ""),
            "last_count": int(number),
        })
    return cleaned


def _clamp(value: Any, limits: Tuple[float, float, float]) -> float:
    """Привести число к диапазону ``(минимум, максимум, значение_по_умолчанию)``."""
    minimum, maximum, default = limits
    number = _as_number(value)
    if number is None:
        number = default
    return float(min(max(minimum, number), maximum))


def _clean_commands(value: Any) -> List[Dict[str, Any]]:
    """Проверить и нормализовать список внешних команд."""
    items = value if isinstance(value, list) else []
    cleaned: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "script").strip().lower()
        if kind not in COMMAND_KINDS:
            kind = "script"
        trigger = str(item.get("trigger") or "on_change").strip().lower()
        if trigger not in COMMAND_TRIGGERS:
            trigger = "on_change"
        artifact = str(item.get("artifact") or "links").strip().lower()
        if artifact not in COMMAND_ARTIFACTS:
            artifact = "links"
        script = str(item.get("script") or "").strip().strip('"')
        command = str(item.get("command") or "").strip()
        if not script and not command:
            continue                      # запускать нечего — запись бессмысленна
        env = item.get("env")
        env_clean: Dict[str, str] = {}
        if isinstance(env, dict):
            for name, env_value in env.items():
                key = str(name).strip()
                if key:
                    env_clean[key] = str(env_value)
        try:
            retries = int(_as_number(item.get("retry_count")) or 0)
        except (TypeError, ValueError):
            retries = 0
        code = item.get("last_code")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        cleaned.append({
            "id": str(item.get("id") or uuid.uuid4().hex[:12]),
            "name": str(item.get("name") or "").strip() or t("Без имени"),
            "kind": kind,
            "script": script,
            "command": command,
            "arguments": str(item.get("arguments") or ""),
            "artifact": artifact,
            "trigger": trigger,
            "interval_minutes": _clamp(item.get("interval_minutes"),
                                       COMMAND_LIMITS["interval_minutes"]),
            "min_gap_seconds": _clamp(item.get("min_gap_seconds"),
                                      COMMAND_LIMITS["min_gap_seconds"]),
            "timeout_seconds": _clamp(item.get("timeout_seconds"),
                                      COMMAND_LIMITS["timeout_seconds"]),
            "retry_count": int(min(max(0, retries), 5)),
            "retry_delay_seconds": _clamp(
                item.get("retry_delay_seconds"),
                COMMAND_LIMITS["retry_delay_seconds"]),
            "working_dir": str(item.get("working_dir") or "").strip(),
            "host": str(item.get("host") or "").strip(),
            "port": int(_clamp(item.get("port"), (0.0, 65535.0, 0.0))),
            "user": str(item.get("user") or "").strip(),
            "password": str(item.get("password") or ""),
            "remote_dir": str(item.get("remote_dir") or "").strip(),
            "env": env_clean,
            "enabled": bool(item.get("enabled", True)),
            "hidden": bool(item.get("hidden", False)),
            "last_run": float(_as_number(item.get("last_run")) or 0.0),
            "last_status": str(item.get("last_status") or ""),
            "last_code": code,
            "last_duration": float(_as_number(item.get("last_duration")) or 0.0),
            "last_output": str(item.get("last_output") or "")[-4000:],
        })
    return cleaned


def _clean_protocols(value: Any) -> List[str]:
    known = {p.value for p in IMPORTABLE_PROTOCOLS}
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        name = str(item).strip().lower()
        if name in known and name not in result:
            result.append(name)
    return result


def _repair(key: str, value: Any, default: Any) -> Any:
    """Привести значение настройки к ожидаемому виду.

    Пустые строки, ``null``, мусор и отрицательные тайм-ауты превращаются в
    значение по умолчанию: интерфейс никогда не должен показывать пустые поля,
    а движок — падать из-за отсутствующего параметра.
    """
    if isinstance(default, bool):
        if value is None or value is _MISSING:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    if isinstance(default, int) and not isinstance(default, bool):
        number = _as_number(value)
        if number is None:
            return default
        number = int(number)
        if key in ("simple_ping_workers", "simple_core_workers",
                   "max_log_lines", "simple_max_failures",
                   "simple_maintenance_max"):
            return max(1, number)
        if key in ("simple_ping_batch", "simple_core_batch",
                   "simple_traffic_batch", "simple_view_limit",
                   "core_base_port", "traffic_status"):
            return max(0, number)
        return number if number >= 0 else default

    if isinstance(default, float):
        number = _as_number(value)
        if number is None or number < 0:
            return default
        if number == 0 and key not in ZERO_ALLOWED:
            return default
        if key in ("simple_maintenance_fraction", "simple_maintenance_guard"):
            # Доля пачки и порог сбоя имеют смысл только внутри (0, 1].
            return float(min(1.0, max(0.05, number)))
        return float(number)

    if isinstance(default, str):
        if value is None or value is _MISSING:
            return default
        text = str(value).strip()
        if not text:
            return text if key in OPTIONAL_STRINGS else default
        if key in OPTIONAL_STRINGS:
            return text
        if key == "simple_stage_mode":
            return text.lower() if text.lower() in STAGE_MODES else default
        if key == "simple_output_format":
            return text.lower() if text.lower() in OUTPUT_FORMATS else default
        if key == "log_level":
            upper = text.upper()
            return upper if upper in LOG_LEVELS else default
        if key == "language":
            return text.lower() if text.lower() in LANGUAGE_CODES else default
        if key == "traffic_url":
            return text if "://" in text else default
        return text

    if isinstance(default, list):
        if key == "simple_sources":
            return _clean_sources(value)
        if key == "simple_commands":
            return _clean_commands(value)
        if key == "simple_protocols":
            return _clean_protocols(value) or list(default)
        return list(value) if isinstance(value, list) else list(default)

    return default if value is _MISSING else value


def repair_value(key: str, value: Any, default: Any = _MISSING) -> Any:
    """Проверить и при необходимости починить одно значение настройки.

    Публичная обёртка над :func:`_repair`: интерфейс использует те же правила,
    что и автолечение файла настроек, поэтому поведение не может разойтись.
    """
    if default is _MISSING:
        default = DEFAULTS.get(key, _MISSING)
    return _repair(key, value, default)


def collect_field_values(fields, protocols, stage_mode
                         ) -> Tuple[Dict[str, Any], List[str]]:
    """Преобразовать значения полей интерфейса в корректные настройки.

    ``fields`` — последовательность ``(ключ, значение поля, тип)``, где тип —
    ``bool``, ``int``, ``float`` или ``str``.  Возвращается пара
    ``(значения для записи, список исправленных ключей)``: пустые и некорректные
    поля заменяются значениями по умолчанию, поэтому сохранить «пустоту»
    невозможно.  Строковые поля при этом **не** пытаются стать числом.
    """
    values: Dict[str, Any] = {}
    repaired: List[str] = []
    for key, raw, kind in fields:
        value: Any = None
        if kind is bool:
            value = bool(raw)
        elif kind is str:
            value = str(raw).strip()
        else:
            text = str(raw).strip().replace(",", ".")
            if text:
                try:
                    value = int(float(text)) if kind is int else float(text)
                except (TypeError, ValueError):
                    value = None
        fixed = repair_value(key, value, DEFAULTS.get(key, _MISSING))
        if fixed != value:
            repaired.append(key)
        values[key] = fixed

    cleaned = _clean_protocols(protocols)
    if not cleaned:
        cleaned = list(DEFAULTS.get("simple_protocols", ()))
        repaired.append("simple_protocols")
    values["simple_protocols"] = cleaned

    mode = str(stage_mode).strip().lower()
    if mode not in STAGE_MODES:
        mode = str(DEFAULTS.get("simple_stage_mode", "cascade"))
        repaired.append("simple_stage_mode")
    values["simple_stage_mode"] = mode
    return values, repaired


def ensure_defaults(settings) -> List[str]:
    """Дополнить и починить настройки, вернув список исправленных ключей.

    Вызывается при каждом создании :class:`SimpleSettings`: после первого запуска
    файл настроек содержит все параметры с рабочими значениями, а мусор в
    исправленном вручную файле не останавливает программу.
    """
    repairs: Dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value = settings.get(key, _MISSING)
        repaired = _repair(key, value, default)
        if repaired != value:
            repairs[key] = repaired
    if repairs:
        settings.update(repairs)
    return sorted(repairs)


class SimpleSettings(Settings):
    """Хранилище настроек simple-версии.

    Отличается от базового тремя вещами:

    * добавляет ключи ``simple_*`` вместе с их значениями по умолчанию;
    * применяет :data:`SIMPLE_OVERRIDES` (например, автозагрузку ядра), но
      только если пользователь сам ещё не сохранял этот ключ;
    * «лечит» настройки через :func:`ensure_defaults`, поэтому пустые или битые
      значения не могут сломать ни интерфейс, ни цикл проверки.

    После первого запуска файл ``data/simple/settings.json`` содержит все
    параметры с рабочими значениями — никаких пустых полей.
    """

    def __init__(self, path: str = SIMPLE_SETTINGS_PATH) -> None:
        stored = read_stored_settings(path)
        super().__init__(path)
        for key, value in SIMPLE_OVERRIDES.items():
            if key not in stored:
                self._data[key] = value
        self.repairs: List[str] = ensure_defaults(self)


# ---------------------------------------------------------------------------
# Политика
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """Пороги и интервалы, выведенные из настроек.

    ``max_failures`` — сколько неудач подряд (с разносом во времени) допустимо,
    прежде чем сервер удаляется; ``retry_seconds`` — пауза перед следующей
    попыткой этапа; ``ping_interval`` / ``traffic_interval`` — период плановой
    перепроверки уже рабочих серверов; ``blacklist_seconds`` — на сколько
    удалённый сервер исключается из повторного импорта.
    """

    max_failures: int = DEFAULT_MAX_FAILURES
    retry_seconds: float = DEFAULT_RETRY_MINUTES * 60.0
    ping_interval: float = DEFAULT_PING_INTERVAL_MINUTES * 60.0
    traffic_interval: float = DEFAULT_TRAFFIC_INTERVAL_HOURS * 3600.0
    blacklist_seconds: float = DEFAULT_BLACKLIST_HOURS * 3600.0
    # Срок годности статуса «Не работает» (0 — не удалять такие записи) и
    # минимальный интервал записи pool.json (0 — сохранять сразу).
    failed_ttl_seconds: float = DEFAULT_FAILED_TTL_HOURS * 3600.0
    save_interval: float = DEFAULT_SAVE_MIN_SECONDS
    # Пакетная перепроверка рабочих: какая доля рабочих подтверждается за один
    # проход, сколько серверов кладётся в один процесс ядра и при какой доле
    # провалов пачка считается сбоем (результаты не применяются).
    maintenance_fraction: float = DEFAULT_MAINTENANCE_FRACTION
    maintenance_max: int = DEFAULT_MAINTENANCE_MAX
    maintenance_guard: float = DEFAULT_MAINTENANCE_GUARD


def _positive(settings, key: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def simple_policy(settings) -> Policy:
    """Собрать :class:`Policy` из хранилища настроек."""
    try:
        max_failures = int(settings.get("simple_max_failures",
                                        DEFAULT_MAX_FAILURES))
    except (TypeError, ValueError):
        max_failures = DEFAULT_MAX_FAILURES
    try:
        maintenance_max = int(settings.get("simple_maintenance_max",
                                           DEFAULT_MAINTENANCE_MAX))
    except (TypeError, ValueError):
        maintenance_max = DEFAULT_MAINTENANCE_MAX
    fraction = _clamp(settings.get("simple_maintenance_fraction",
                                   DEFAULT_MAINTENANCE_FRACTION),
                      (0.05, 1.0, DEFAULT_MAINTENANCE_FRACTION))
    guard = _clamp(settings.get("simple_maintenance_guard",
                                DEFAULT_MAINTENANCE_GUARD),
                   (0.05, 1.0, DEFAULT_MAINTENANCE_GUARD))
    return Policy(
        max_failures=max(1, max_failures),
        retry_seconds=_positive(settings, "simple_retry_minutes",
                                DEFAULT_RETRY_MINUTES) * 60.0,
        ping_interval=_positive(settings, "simple_ping_interval_minutes",
                                DEFAULT_PING_INTERVAL_MINUTES) * 60.0,
        traffic_interval=_positive(settings, "simple_traffic_interval_hours",
                                   DEFAULT_TRAFFIC_INTERVAL_HOURS) * 3600.0,
        blacklist_seconds=_positive(settings, "simple_blacklist_hours",
                                    DEFAULT_BLACKLIST_HOURS) * 3600.0,
        failed_ttl_seconds=_positive(settings, "simple_failed_ttl_hours",
                                     DEFAULT_FAILED_TTL_HOURS) * 3600.0,
        save_interval=_positive(settings, "simple_save_min_seconds",
                                DEFAULT_SAVE_MIN_SECONDS),
        maintenance_fraction=fraction,
        maintenance_max=max(1, maintenance_max),
        maintenance_guard=guard,
    )


def pool_gzip_enabled(settings) -> bool:
    """Сжимать ли файл пула (настройка ``simple_pool_gzip``).

    Строковые значения трактуются как булевы, чтобы значение, вписанное в
    ``settings.json`` руками, не включало сжатие случайно.
    """
    value = settings.get("simple_pool_gzip", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def enabled_simple_protocols(settings) -> set:
    """Набор протоколов, которые разрешено импортировать.

    Пустой/битый список трактуется как «все поддерживаемые протоколы», чтобы
    свежие настройки работали без миграции.
    """
    all_supported = {p.value for p in IMPORTABLE_PROTOCOLS}
    raw = settings.get("simple_protocols")
    if raw is None:
        return all_supported
    if isinstance(raw, str):
        raw = [raw]
    try:
        allowed = {str(item) for item in raw}
    except TypeError:
        return all_supported
    allowed.discard(Protocol.UNKNOWN.value)
    if not allowed:
        return all_supported
    return allowed


def protocol_labels() -> List[tuple]:
    """Пары ``(значение, подпись)`` для списка протоколов в интерфейсе."""
    return [(p.value, p.label) for p in IMPORTABLE_PROTOCOLS]
