"""Пакетная перепроверка уже рабочих серверов одним процессом ядра.

Зачем отдельный модуль.  Обычная проверка поднимает **отдельный процесс ядра на
каждый сервер**: это оправдано на этапе отбора (изоляция ошибок, точная
диагностика, сертификатный ретрай), но для планового подтверждения уже
проверенных серверов избыточно — там нужен только контрольный запрос.

Измерения на реальных бинарниках показали: процесс с 50 inbound-портами
стартует ровно столько же, сколько процесс на один сервер (+1 МБ памяти).
Поэтому рабочие серверы подтверждаются пачками: один процесс ядра, по одному
локальному SOCKS-порту на сервер и по одному контрольному запросу через
каждый порт.

Уровень 2 (рукопожатие) здесь сознательно пропускается: он ничего не
доказывает про креденшелы (см. комментарий в :mod:`app.checker`) — рабочим
сервер делает только контрольный трафик.

Модуль не знает ни о пуле, ни о политике: он собирает конфиг пачки, запускает
процесс, опрашивает порты и возвращает отчёт.  Решение «применять ли
результаты» принимает вызывающая сторона.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import core_config
from app.checker import (
    _expected_status,
    _http_get_via_socks,
    _parse_traffic_target,
    traffic_targets,
)
from app.core_manager import CoreRunner
from app.models import Config

from .state import ServerEntry
from app.i18n import t


#: Сколько серверов кладётся в один процесс ядра, если не задано иное.
DEFAULT_MAX_PER_PROCESS = 50


@dataclass
class BatchResult:
    """Результат одного сервера внутри пачки."""

    fp: str
    ok: bool
    latency_ms: Optional[int] = None
    error: str = ""


@dataclass
class BatchReport:
    """Отчёт по одной пачке (одному процессу ядра)."""

    core: str = ""
    requested: int = 0
    checked: int = 0
    ok: int = 0
    failed: int = 0
    elapsed: float = 0.0
    results: List[BatchResult] = field(default_factory=list)
    #: Непустая строка означает сбой инфраструктуры (ядро не поднялось, порты
    #: не открылись, процесс упал): такие результаты применять нельзя — они
    #: говорят о проблеме окружения, а не о серверах.
    infra_error: str = ""

    @property
    def infra(self) -> bool:
        return bool(self.infra_error)

    def failed_ratio(self) -> float:
        """Доля провалов среди проверенных (для «предохранителя»)."""
        if not self.checked:
            return 0.0
        return self.failed / float(self.checked)


# ---------------------------------------------------------------------------
# Сборка пачки
# ---------------------------------------------------------------------------

def group_entries(entries: Sequence[ServerEntry]
                  ) -> Dict[str, List[Tuple[ServerEntry, Config]]]:
    """Разложить записи по ядрам: смешивать Xray и sing-box нельзя.

    Записи с неподдерживаемой конфигурацией пропускаются — их обработает
    обычный путь (там они и помечаются как «не поддерживается»).
    """
    groups: Dict[str, List[Tuple[ServerEntry, Config]]] = {}
    for entry in entries:
        try:
            cfg = entry.config()
        except Exception:  # noqa: BLE001 - битую запись проверяет обычный путь
            continue
        core = core_config.pick_core(cfg)
        if core is None:
            continue
        groups.setdefault(core, []).append((entry, cfg))
    return groups


def build_document(core: str, configs: Sequence[Config],
                   ports: Sequence[int]) -> Dict[str, Any]:
    """Собрать конфиг ядра: по одному локальному inbound на сервер.

    Каждый сервер получает свой SOCKS-порт и своё правило маршрутизации, так
    что результат проверки однозначно привязан к серверу, а серверы не влияют
    друг на друга.
    """
    if len(configs) != len(ports):
        raise ValueError(t("число конфигураций и портов должно совпадать"))
    inbounds: List[Dict[str, Any]] = []
    outbounds: List[Dict[str, Any]] = []
    rules: List[Dict[str, Any]] = []
    for index, cfg in enumerate(configs):
        tag_in = f"in-{index}"
        tag_out = f"proxy-{index}"
        if core == core_config.XRAY:
            inbounds.append({
                "tag": tag_in,
                "listen": "127.0.0.1",
                "port": int(ports[index]),
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            })
            outbound = dict(core_config.build_xray(cfg, ports[index])["outbounds"][0])
            rules.append({"type": "field", "inboundTag": [tag_in],
                          "outboundTag": tag_out})
        else:
            inbounds.append({
                "type": "socks",
                "tag": tag_in,
                "listen": "127.0.0.1",
                "listen_port": int(ports[index]),
            })
            outbound = dict(core_config.build_singbox(cfg, ports[index])["outbounds"][0])
            rules.append({"inbound": [tag_in], "outbound": tag_out})
        outbound["tag"] = tag_out
        outbounds.append(outbound)

    if core == core_config.XRAY:
        outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})
        return {
            "log": {"loglevel": "warning"},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "routing": {"rules": rules},
        }
    outbounds.append({"type": "direct", "tag": "direct"})
    return {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": rules, "final": "direct"},
    }


def free_ports(count: int) -> List[int]:
    """Список свободных локальных портов (резервируются на время поиска)."""
    holders: List[socket.socket] = []
    ports: List[int] = []
    try:
        for _ in range(max(0, count)):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            ports.append(int(sock.getsockname()[1]))
            holders.append(sock)
    finally:
        for sock in holders:
            try:
                sock.close()
            except OSError:
                pass
    return ports


def wait_ports(ports: Sequence[int], deadline: float) -> bool:
    """Дождаться, пока ядро откроет все локальные порты пачки."""
    pending = list(ports)
    while pending and time.time() < deadline:
        still_closed: List[int] = []
        for port in pending:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.25):
                    pass
            except OSError:
                still_closed.append(port)
        pending = still_closed
        if pending:
            time.sleep(0.05)
    return not pending


# ---------------------------------------------------------------------------
# Проверка
# ---------------------------------------------------------------------------

def probe_port(port: int, settings, timeout: float
               ) -> Tuple[bool, str, Optional[int]]:
    """Один контрольный запрос через локальный SOCKS-порт процесса.

    Логика целей и ожидаемого ответа повторяет одиночную проверку уровня 3
    (см. :meth:`app.checker.CoreChecker._check_traffic`): первая же цель,
    ответившая правильным HTTP-статусом, подтверждает сервер.
    """
    configured_status = int(settings.get("traffic_status", 0) or 0)
    expected_substr = str(settings.get("traffic_expect", "") or "")
    last_error = t("контрольный запрос не прошёл")
    for url in traffic_targets(settings):
        host, target_port, path, secure = _parse_traffic_target(url)
        if not host:
            continue
        started = time.time()
        ok, text, status = _http_get_via_socks(
            port, host, target_port, path, timeout, secure=secure)
        elapsed = int((time.time() - started) * 1000)
        if not ok:
            last_error = t('трафик ({}): {}').format(host, text or 'нет HTTP-ответа')
            continue
        expected_status = _expected_status(url, configured_status)
        if expected_status:
            if status != expected_status:
                last_error = t('HTTP {} от {} (ожидался {})').format(status, host, expected_status)
                continue
        elif not (200 <= status < 300):
            last_error = t('HTTP {} от {} (ожидался 2xx)').format(status, host)
            continue
        if expected_substr and expected_substr not in text:
            last_error = t("ответ не содержит ожидаемых данных")
            continue
        return True, "", elapsed
    return False, last_error, None



def run_group(runner: CoreRunner, settings, core: str, core_path: str,
              items: Sequence[Tuple[ServerEntry, Config]],
              workers: int = 8, start_timeout: float = 6.0,
              request_timeout: float = 8.0) -> BatchReport:
    """Поднять один процесс ядра на пачку и опросить каждый её сервер."""
    report = BatchReport(core=core, requested=len(items))
    if not items:
        return report

    ports = free_ports(len(items))
    document = build_document(core, [cfg for _entry, cfg in items], ports)
    started = time.time()
    instance = runner.start(core_path, core, document, timeout=start_timeout,
                            ports=ports)
    if instance is None:
        report.infra_error = t('не удалось запустить ядро {}').format(core)
        report.elapsed = time.time() - started
        return report
    try:
        if instance.process.poll() is not None:
            report.infra_error = t('ядро {} завершилось сразу после старта').format(core)
            return report
        if not wait_ports(ports, deadline=time.time() + start_timeout):
            report.infra_error = t('ядро {} не открыло локальные порты пачки').format(core)
            return report

        queue: List[Tuple[ServerEntry, int]] = [
            (entry, port) for (entry, _cfg), port in zip(items, ports)]
        lock = threading.Lock()
        results: List[BatchResult] = []

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    entry, port = queue.pop(0)
                try:
                    ok, error, latency = probe_port(port, settings,
                                                    request_timeout)
                except Exception as exc:  # noqa: BLE001 - один сбой не рушит пачку
                    ok, error, latency = False, t('ошибка проверки: {}').format(exc), None
                with lock:
                    results.append(BatchResult(fp=entry.fp, ok=ok,
                                               latency_ms=latency, error=error))

        threads = [threading.Thread(target=worker, name=f"batch-{core}",
                                    daemon=True)
                   for _ in range(min(max(1, int(workers)), max(1, len(queue))))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=request_timeout * 3 + 30.0)

        report.results = results
        report.checked = len(results)
        report.ok = sum(1 for item in results if item.ok)
        report.failed = report.checked - report.ok
        if report.checked < report.requested:
            # Часть потоков не успела ответить: это уже не «серверы плохие»,
            # а незавершённая проверка — результаты пачки не применяем.
            report.infra_error = (t('пачка {} не завершилась вовремя ({} из {})').format(core, report.checked, report.requested))
        return report
    finally:
        instance.stop()
        report.elapsed = time.time() - started



def run_batches(runner: CoreRunner, settings, entries: Sequence[ServerEntry],
                core_paths: Dict[str, Optional[str]],
                max_per_process: int = DEFAULT_MAX_PER_PROCESS,
                workers: int = 8, start_timeout: float = 6.0,
                request_timeout: float = 8.0) -> List[BatchReport]:
    """Проверить записи пачками по ядрам.

    Каждая пачка — не больше ``max_per_process`` серверов одного ядра.  При
    первом же сбое инфраструктуры проверка прекращается: гонять остальные
    пачки «вслепую» (например, при пропавшей сети) смысла нет.
    """
    limit = max(1, int(max_per_process or DEFAULT_MAX_PER_PROCESS))
    reports: List[BatchReport] = []
    groups = group_entries(entries)
    for core in sorted(groups):
        items = groups[core]
        path = core_paths.get(core)
        if not path:
            reports.append(BatchReport(core=core, requested=len(items),
                                       infra_error=t('ядро {} не найдено').format(core)))
            return reports
        for start in range(0, len(items), limit):
            chunk = items[start:start + limit]
            report = run_group(runner, settings, core, path, chunk,
                               workers=workers, start_timeout=start_timeout,
                               request_timeout=request_timeout)
            reports.append(report)
            if report.infra:
                return reports
    return reports


def reports_summary(reports: Sequence[BatchReport]) -> Dict[str, Any]:
    """Сводка по отчётам — для журнала и интерфейса."""
    checked = sum(report.checked for report in reports)
    ok = sum(report.ok for report in reports)
    failed = sum(report.failed for report in reports)
    infra = next((report.infra_error for report in reports if report.infra), "")
    elapsed = sum(report.elapsed for report in reports)
    return {"batches": len(reports), "checked": checked, "ok": ok,
            "failed": failed, "infra_error": infra, "elapsed": elapsed}
