"""Global constants, enums and default settings of the checking core."""

from __future__ import annotations

import os
import sys
from enum import Enum
from app.i18n import t


APP_NAME = "Xray Config Checker Simple"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def base_dir() -> str:
    """Папка, рядом с которой приложение хранит свои данные.

    Обычный запуск — корень проекта.  Собранный одним файлом exe
    распаковывается во временный каталог, который удаляется после выхода,
    поэтому и настройки, и ядра, и журналы должны лежать рядом с самим exe,
    а не рядом с кодом.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(PACKAGE_DIR)


PROJECT_DIR = base_dir()
DATA_DIR = os.path.join(PROJECT_DIR, "data")
CORE_DIR = os.path.join(PROJECT_DIR, "core")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
TMP_DIR = os.path.join(PROJECT_DIR, "tmp")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

for _d in (DATA_DIR, CORE_DIR, LOG_DIR, TMP_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CheckMode(str, Enum):
    """How aggressively configurations are validated."""

    QUICK = "quick"          # Level 1 only: reachability (DNS/TCP)
    FULL = "full"            # Levels 1-2: real handshake via core
    DEEP = "deep"            # Levels 1-3: handshake + control traffic
    RECHECK = "recheck"      # Only previously failed/suspect configs, full pipeline
    CUSTOM = "custom"        # Arbitrary subset of levels chosen by the user

    @property
    def label(self) -> str:
        return {
            self.QUICK: t("Быстрая очистка (пинг)"),
            self.FULL: t("Полная (соединение через ядро)"),
            self.DEEP: t("Глубокая (реальный трафик)"),
            self.RECHECK: t("Повторная проверка сомнительных"),
            self.CUSTOM: t("Выборочная (свой набор уровней)…"),
        }[self]

    @property
    def levels(self) -> int:
        """Deepest level this mode reaches."""
        return max(self.level_set) if self.level_set else 1

    @property
    def level_set(self) -> set:
        """Which probe levels (1/2/3) this mode runs.

        Level 1 = DNS/TCP reachability, level 2 = real core handshake,
        level 3 = control traffic through the tunnel.  CUSTOM is resolved by
        the caller from the user's selection.
        """
        return {
            self.QUICK: {1},
            self.FULL: {1, 2},
            self.DEEP: {1, 2, 3},
            self.RECHECK: {1, 2, 3},
            self.CUSTOM: {1, 2, 3},
        }[self]


class ConfigState(str, Enum):
    """Life-cycle state of a configuration.

    States are deliberately hierarchical and never collapsed into a single
    boolean: an address may be reachable while the protocol still refuses to
    connect, and a protocol may connect while no data actually flows.
    """

    NEW = "new"                  # parsed, not checked yet
    UNREACHABLE = "unreachable"  # level 1 failed
    REACHABLE = "reachable"      # level 1 ok (ping/TCP only) — a mere candidate
    SUSPECT = "suspect"          # temporary errors / partial success
    CONNECTED = "connected"      # level 2 ok (real core handshake)
    WORKING = "working"          # level 3 ok (control traffic passes)
    DEAD = "dead"                # failed beyond retry policy

    @property
    def label(self) -> str:
        return {
            self.NEW: t("Новая"),
            self.UNREACHABLE: t("Адрес недоступен"),
            self.REACHABLE: t("Пинг OK (кандидат)"),
            self.SUSPECT: t("Сомнительная"),
            self.CONNECTED: t("Соединение есть"),
            self.WORKING: t("Рабочая"),
            self.DEAD: t("Мёртвая"),
        }[self]

    @property
    def is_alive(self) -> bool:
        return self in (ConfigState.WORKING, ConfigState.CONNECTED,
                        ConfigState.REACHABLE)


class CheckStage(str, Enum):
    """Result of an individual probing stage."""

    NONE = "none"
    DNS_FAIL = "dns_fail"
    TCP_FAIL = "tcp_fail"
    TCP_OK = "tcp_ok"
    HANDSHAKE_FAIL = "handshake_fail"
    HANDSHAKE_OK = "handshake_ok"
    TRAFFIC_FAIL = "traffic_fail"
    TRAFFIC_OK = "traffic_ok"


class Protocol(str, Enum):
    VLESS = "vless"
    VMESS = "vmess"
    TROJAN = "trojan"
    SHADOWSOCKS = "shadowsocks"
    SHADOWSOCKS_R = "ssr"
    HYSTERIA2 = "hysteria2"
    TUIC = "tuic"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            self.VLESS: "VLESS",
            self.VMESS: "VMess",
            self.TROJAN: "Trojan",
            self.SHADOWSOCKS: "Shadowsocks",
            self.SHADOWSOCKS_R: "ShadowsocksR",
            self.HYSTERIA2: "Hysteria2",
            self.TUIC: "TUIC",
            self.UNKNOWN: t("Неизвестный"),
        }[self]


# Protocols that may be imported from sources.  ``UNKNOWN`` is never offered
# because such entries cannot be turned into a working outbound anyway.
IMPORTABLE_PROTOCOLS = [p for p in Protocol if p != Protocol.UNKNOWN]


def enabled_import_protocols(settings) -> set:
    """Return the set of protocol values the user allows to be imported.

    A missing/empty selection is treated as "all supported protocols" so that
    an old settings file or a freshly created :class:`Settings` keeps working
    without an extra migration step.
    """
    raw = settings.get("import_protocols")
    if raw is None:
        return {p.value for p in IMPORTABLE_PROTOCOLS}
    if isinstance(raw, str):
        raw = [raw]
    try:
        allowed = {str(item) for item in raw}
    except TypeError:
        return {p.value for p in IMPORTABLE_PROTOCOLS}
    allowed.discard(Protocol.UNKNOWN.value)
    if not allowed:
        return {p.value for p in IMPORTABLE_PROTOCOLS}
    return allowed


# ---------------------------------------------------------------------------
# Defaults (editable from the GUI, persisted to settings.json)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    # concurrency --------------------------------------------------------
    "level1_workers": 128,      # cheap socket probes
    "core_workers": 12,         # heavy xray/sing-box instances
    "fetch_workers": 8,         # source downloaders

    # timeouts (seconds) -------------------------------------------------
    "dns_timeout": 3.0,
    "tcp_timeout": 3.0,
    "core_timeout": 8.0,        # max wall-clock for a single core check
    "traffic_timeout": 5.0,

    # retry policy -------------------------------------------------------
    "max_consecutive_failures": 4,   # after this many -> DEAD
    "suspect_backoff_hours": 6,      # don't retry a suspect entry before this
    "dead_recheck_hours": 24,        # periodic revival attempt for dead entries
    "stale_working_hours": 24,       # re-verify working configs after this

    # core binaries ------------------------------------------------------
    "xray_path": "",
    "singbox_path": "",
    # Off by default: nothing is downloaded behind the user's back. Cores are
    # installed explicitly from the Settings tab when needed.
    "auto_download_core": False,
    # 0 = let the OS pick a free local SOCKS port for every check, which avoids
    # collisions when thousands of configurations are probed in a long run.
    "core_base_port": 0,

    # traffic control target --------------------------------------------
    "traffic_url": "http://cp.cloudflare.com/",
    "traffic_expect": "",            # optional substring expected in response
    # 0 = automatic: a "generate_204" target must answer 204, any other target
    # must answer with a 2xx code.  Set a specific code to require it instead.
    # The control request must always be a well-formed HTTP response, so a
    # non-proxy endpoint that merely returns an error page no longer passes.
    "traffic_status": 0,
    # Try the built-in fallback targets when the configured one does not answer.
    # On (default) a single blocked destination cannot fail a valid server, but
    # a broken server is probed with up to four requests.  Off = only the
    # configured target is used, which noticeably reduces the number of outgoing
    # connections (useful when a local antivirus/firewall complains).
    "traffic_fallbacks": True,

    # misc ---------------------------------------------------------------
    "log_level": "INFO",
    "max_log_lines": 2000,
    # Off by default: the journal lives in memory (the "Журнал" tab); writing
    # it to logs/checker.log is opt-in so no files are created behind the
    # user's back.
    "log_to_file": False,
    "fetch_timeout": 30.0,
    "user_agent": "v2rayNG/1.8.5",
    "dedup_keep_sources": True,
    "export_template": "uri",        # uri | json

    # Import filter ------------------------------------------------------
    # Protocols that are accepted when importing sources.  Non-working
    # protocols (e.g. ssr/tuic) can be switched off to keep the pool clean.
    "import_protocols": [p.value for p in IMPORTABLE_PROTOCOLS],

    # housekeeping -------------------------------------------------------
    # Core principle: an address that fails the cheapest reachability stage
    # will not pass a harder stage either, so it is removed immediately rather
    # than lingering as "unreachable".  This keeps the pool small and speeds up
    # successive passes over a large initial list.
    "auto_delete_unreachable": True,

    # Debugging ----------------------------------------------------------
    # When enabled, the per-check temporary config/log files created in tmp/
    # are kept for inspection.  Off by default: the checker removes them as
    # soon as a probe finishes, which keeps tmp/ empty.
    "keep_core_debug_files": False,
}

# IP/port sanity limits used during normalisation
MIN_PORT = 1
MAX_PORT = 65535