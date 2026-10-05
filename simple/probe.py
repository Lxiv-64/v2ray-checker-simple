"""Этапы проверки поверх готового чекера из ядра (``app``).

Здесь нет собственной реализации протоколов: уровень 1 выполняется функцией
:func:`app.checker.check_level1`, а уровни 2 и 3 — существующим
:class:`app.checker.CoreChecker`, который поднимает реальное ядро Xray-core или
sing-box и прогоняет рукопожатие и контрольный трафик.  Задача модуля —
привести результат к простому виду (:class:`Outcome`) и отделить два особых
случая:

* **не поддерживается** — конфигурацию не соберёт ни одно ядро (экзотический
  транспорт, битая ссылка): повторять такие проверки бессмысленно;
* **ядро не установлено** — это не вина конфигурации, поэтому результат не
  должен считаться неудачей и «съедать» попытки сервера.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from app import core_config, core_manager
from app.checker import CoreChecker, ProbeResult, check_level1
from app.constants import CheckMode, ConfigState
from app.core_manager import CoreRunner
from app.models import Config

from .state import LEVEL_CORE, LEVEL_PING, LEVEL_TRAFFIC
from app.i18n import t


# Ошибки, означающие принципиальную несовместимость конфигурации с ядром.
_PERMANENT_MARKERS = (
    t("неподдерживаемый протокол"),
    t("не поддержан"),
    t("ошибка сборки конфигурации"),
)


@dataclass
class Outcome:
    """Унифицированный результат одного этапа проверки."""

    ok: bool
    level: int = 0
    error: str = ""
    latency_ms: Optional[int] = None
    permanent: bool = False
    no_core: bool = False
    state: str = ""


def _is_permanent(result: ProbeResult) -> bool:
    if result.detail.get("core") == t("нет"):
        return True
    error = (result.error or "").lower()
    return any(marker in error for marker in _PERMANENT_MARKERS)


def _is_no_core(result: ProbeResult) -> bool:
    error = (result.error or "").lower()
    return t("ядро") in error and t("не найдено") in error


def _number(settings, key: str, default: float) -> float:
    """Числовая настройка с гарантированным откатом на значение по умолчанию."""
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class Prober:
    """Драйвер реальных проверок (уровни 1–3)."""

    def __init__(self, settings) -> None:
        self.settings = settings
        core_manager.set_keep_debug_files(
            bool(settings.get("keep_core_debug_files", False)))
        self.runner = CoreRunner(
            base_port=int(settings.get("core_base_port", 0) or 0))
        self.checker = CoreChecker(self.runner, settings)

    # -- ядра ------------------------------------------------------------
    def ensure_cores(self) -> None:
        self.checker.ensure_cores()

    def has_core(self) -> bool:
        """Доступен ли хотя бы один бинарник ядра."""
        self.ensure_cores()
        return bool(self.checker.core_path(core_config.XRAY)
                    or self.checker.core_path(core_config.SINGBOX))

    def reset_cores(self) -> None:
        """Сбросить кэш поиска ядер (после смены пути в настройках)."""
        self.checker._resolved = False

    def stop_cores(self) -> int:
        """Остановить все запущенные ядра (при выходе или остановке цикла)."""
        return self.runner.stop_all()

    def allow_cores(self) -> None:
        """Разрешить запуск ядер заново (после остановки цикла)."""
        self.runner.allow_start()

    def core_paths(self) -> Tuple[Optional[str], Optional[str]]:
        """Пути найденных ядер: ``(xray, sing-box)``."""
        self.ensure_cores()
        return (self.checker.core_path(core_config.XRAY),
                self.checker.core_path(core_config.SINGBOX))

    # -- этапы -----------------------------------------------------------
    def probe(self, cfg: Config, level: int) -> Outcome:
        if level == LEVEL_PING:
            return self.ping(cfg)
        return self._core_probe(cfg, level)

    def ping(self, cfg: Config) -> Outcome:
        """Уровень 1: DNS + TCP-подключение к адресу."""
        dns_timeout = _number(self.settings, "dns_timeout", 4.0)
        tcp_timeout = _number(self.settings, "tcp_timeout", 4.0)
        try:
            result = check_level1(cfg, dns_timeout, tcp_timeout)
        except Exception as exc:  # noqa: BLE001 - сбой не должен ломать цикл
            return Outcome(ok=False, level=LEVEL_PING,
                           error=t('ошибка пинга: {}').format(exc))
        return Outcome(
            ok=bool(result.success),
            level=LEVEL_PING,
            error=result.error or "",
            latency_ms=result.latency_ms,
        )

    def _core_probe(self, cfg: Config, level: int) -> Outcome:
        wanted = {2} if level == LEVEL_CORE else {2, 3}
        dns_timeout = _number(self.settings, "dns_timeout", 4.0)
        tcp_timeout = _number(self.settings, "tcp_timeout", 4.0)
        try:
            base = check_level1(cfg, dns_timeout, tcp_timeout)
        except Exception as exc:  # noqa: BLE001
            base = ProbeResult()
            base.error = t('ошибка пинга: {}').format(exc)
        try:
            result = self.checker.check(cfg, 0, CheckMode.CUSTOM, base,
                                        levels=wanted)
        except Exception as exc:  # noqa: BLE001
            return Outcome(ok=False, level=level,
                           error=t('внутренняя ошибка проверки: {}').format(exc))

        ok = result.state == ConfigState.WORKING.value or (
            level == LEVEL_CORE and result.state == ConfigState.CONNECTED.value)
        return Outcome(
            ok=ok,
            level=level if level != LEVEL_TRAFFIC else LEVEL_TRAFFIC,
            error="" if ok else (result.error or t("проверка не прошла")),
            latency_ms=result.latency_ms,
            permanent=not ok and _is_permanent(result),
            no_core=not ok and _is_no_core(result),
            state=result.state,
        )
