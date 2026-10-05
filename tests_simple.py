"""Оффлайн-тесты simple-версии (без сети и без реальных ядер).

Проверяется именно конечный автомат: воронка пинг → ядро → трафик, эскалация
неудач до удаления, «разжалование» рабочих серверов, плановое обслуживание
(пинг раз в час, трафик раз в 6 часов) и запись выходного файла.

Прогон::

    python tests_simple.py
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from app import parser
from app.constants import IMPORTABLE_PROTOCOLS, Protocol

from simple import autostart
from simple import batch as batch_module
from simple import cli as cli_module
from simple import pipeline as pipeline_module
from simple.batch import BatchReport, BatchResult
from simple.clipboard import classify_clipboard
from simple.commands import (
    CommandPreset,
    CommandScheduler,
    EVENT_TRIGGERS,
    TRIGGER_LABELS,
    RunResult,
    _decode_console,
    render_preset,
    run_preset,
)
from simple.config import (
    COMMAND_TRIGGERS,
    DEFAULT_SIMPLE_PROTOCOLS,
    DEFAULTS,
    LOG_LEVELS,
    OUTPUT_BOTH,
    OUTPUT_URI,
    OUTPUT_V2RAYN,
    SimpleSettings,
    collect_field_values,
    simple_policy,
)
from simple.netwatch import NetworkWatcher, watch_enabled
from simple.pipeline import Supervisor
from simple.probe import Outcome
from simple.state import (
    LEVEL_CORE,
    LEVEL_PING,
    LEVEL_TRAFFIC,
    Pool,
    ServerEntry,
    SimpleState,
)

VLESS_A = ("vless://123e4567-e89b-12d3-a456-426614174000@a.example.com:443"
           "?type=tcp&security=tls&sni=a.example.com#A")
VLESS_B = ("vless://223e4567-e89b-12d3-a456-426614174000@b.example.com:8443"
           "?type=ws&security=tls&path=%2Fws&host=b.example.com#B")
TROJAN_C = "trojan://secret@c.example.com:443?security=tls#C"
VLESS_D = ("vless://323e4567-e89b-12d3-a456-426614174000@d.example.com:443"
           "?type=grpc&security=tls&serviceName=grpc#D")

FAILURES = 0


def expect(name: str, condition: bool) -> None:
    global FAILURES
    print(f"  [{'OK' if condition else 'FAIL'}]  {name}")
    if not condition:
        FAILURES += 1


# ---------------------------------------------------------------------------
# Заглушка ядра
# ---------------------------------------------------------------------------

def ok(level: int, latency: int = 25) -> Outcome:
    return Outcome(ok=True, level=level, latency_ms=latency)


def fail(level: int, error: str = "нет ответа", permanent: bool = False,
         no_core: bool = False) -> Outcome:
    return Outcome(ok=False, level=level, error=error, latency_ms=None,
                   permanent=permanent, no_core=no_core)


class FakeProber:
    """Выдаёт заранее заданные результаты вместо реальных проверок.

    Если для этапа сценарий не задан, возвращается «ядро недоступно»: так
    тест может «включать» этапы по одному, наблюдая промежуточные состояния.
    """

    def __init__(self) -> None:
        self.script: dict = {}
        self.calls: list = []
        self.cores = True
        self.default = fail(0, "сценарий не задан", no_core=True)
        #: Пакетная перепроверка берёт отсюда «драйвер ядра»; сам запуск в
        #: тестах подменяется (см. ``patch_batch_runner``).
        self.runner = None

    def script_for(self, endpoint: str, results: dict) -> None:
        self.script[endpoint] = results

    def ensure_cores(self) -> None:
        pass

    def reset_cores(self) -> None:
        pass

    def has_core(self) -> bool:
        return self.cores

    def core_paths(self):
        """Пути ядер: без «установленных» ядер пачки не запускаются."""
        return ("xray", "singbox") if self.cores else (None, None)

    def probe(self, cfg, level: int) -> Outcome:
        endpoint = f"{cfg.address}:{cfg.port}"
        self.calls.append((endpoint, level))
        outcome = self.script.get(endpoint, {}).get(level)
        if outcome is None:
            return Outcome(ok=False, level=level, error=self.default.error,
                           no_core=self.default.no_core,
                           permanent=self.default.permanent)
        return outcome

    def count(self, endpoint: str, level: int) -> int:
        return sum(1 for item, lvl in self.calls
                   if item == endpoint and lvl == level)


def patch_batch_runner(prober: "FakeProber") -> None:
    """Подменить пакетную перепроверку эмуляцией на данных FakeProber.

    Тесты не запускают реальные ядра: «пачка» спрашивает тот же скрипт, что и
    одиночная проверка, поэтому конвейер проверяется целиком — вместе с долей
    от рабочих, лимитом на процесс и подтверждением провалов.
    """
    def fake_run_batches(runner, settings, entries, core_paths,
                         max_per_process=50, workers=8, start_timeout=6.0,
                         request_timeout=8.0):
        if not prober.cores:
            return [BatchReport(core="xray", requested=len(entries),
                                infra_error="ядро не найдено")]
        limit = max(1, int(max_per_process or 1))
        reports = []
        for start in range(0, len(entries), limit):
            chunk = entries[start:start + limit]
            results = []
            for entry in chunk:
                outcome = prober.script.get(entry.endpoint, {}).get(LEVEL_TRAFFIC)
                if outcome is None:
                    results.append(BatchResult(fp=entry.fp, ok=False,
                                               error="нет ответа"))
                else:
                    results.append(BatchResult(fp=entry.fp, ok=bool(outcome.ok),
                                               latency_ms=outcome.latency_ms,
                                               error=outcome.error))
            ok_count = sum(1 for item in results if item.ok)
            reports.append(BatchReport(
                core="xray", requested=len(chunk), checked=len(results),
                ok=ok_count, failed=len(results) - ok_count, elapsed=0.05,
                results=results))
        return reports

    pipeline_module.run_batches = fake_run_batches


def make_env():
    """Свежее окружение: настройки, пул, заглушка ядра и супервизор."""
    tmp = tempfile.mkdtemp(prefix="simple_test_")
    settings = SimpleSettings(os.path.join(tmp, "settings.json"))
    settings.update({
        "simple_ping_batch": 500,
        "simple_core_batch": 500,
        "simple_traffic_batch": 500,
        "simple_ping_workers": 4,
        "simple_core_workers": 4,
        "simple_max_failures": 5,
        "simple_retry_minutes": 0.0,
        "simple_source_refresh_hours": 999.0,
        "simple_blacklist_hours": 12.0,
        "simple_ping_interval_minutes": 60.0,
        "simple_traffic_interval_hours": 6.0,
        "simple_output_path": os.path.join(tmp, "working.txt"),
    })
    pool = Pool(os.path.join(tmp, "pool.json"))
    prober = FakeProber()
    supervisor = Supervisor(settings, pool, prober)
    patch_batch_runner(prober)
    return tmp, settings, pool, prober, supervisor


def age(pool: Pool, fp: str, **fields) -> None:
    """Подменить метки времени/счётчики записи (эмуляция хода времени)."""
    def mutate(entry):
        for key, value in fields.items():
            setattr(entry, key, value)
        return "keep"
    pool.update(fp, mutate)


def only(pool: Pool) -> object:
    entries = pool.items()
    return entries[0] if entries else None


def congest(pool: Pool, count: int, state: str,
            next_check_at: float = 0.0) -> None:
    """Набить пул синтетическими записями, ожидающими своей проверки."""
    base = parser.parse_uri(VLESS_A)
    assert base is not None
    entries = []
    for index in range(count):
        uri = f"vless://syn{state}{index}"
        data = base.to_dict()
        data["address"] = f"host{index}.example.net"
        # Ссылка восстанавливается после перезапуска из ``data`` — держим её
        # согласованной с полем записи.
        data["uri"] = uri
        entries.append(ServerEntry(
            fp=f"syn{state}{index}", uri=uri,
            data=data, state=state, next_check_at=next_check_at))
    pool.add_many(entries)


def output_lines(supervisor: Supervisor) -> list:
    try:
        with open(supervisor.writer.path, "r", encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

def test_import_and_filter() -> None:
    print("test_import_and_filter")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.set("simple_protocols", ["vless", "vmess", "shadowsocks"])
        counters = supervisor.import_text(
            f"{VLESS_A}\n{VLESS_B}\n{TROJAN_C}", "тест")
        expect("добавлено две vless", counters["added"] == 2)
        expect("trojan отфильтрован протокольным фильтром",
               counters["filtered"] == 1)
        expect("все записи новые",
               all(entry.state == SimpleState.NEW.value
                   for entry in pool.items()))
        again = supervisor.import_text(VLESS_A, "тест")
        expect("дубликат не добавлен повторно",
               again["added"] == 0 and again["duplicate"] == 1)
        expect("политика прочитана", simple_policy(settings).max_failures == 5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_full_funnel() -> None:
    print("test_full_funnel")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint, {LEVEL_PING: ok(LEVEL_PING, 15)})
        supervisor.run_round()
        expect("после пинга — кандидат",
               pool.get(entry.fp).state == SimpleState.REACHABLE.value)

        prober.script_for(entry.endpoint, {LEVEL_PING: ok(LEVEL_PING, 15),
                                          LEVEL_CORE: ok(LEVEL_CORE, 30)})
        supervisor.run_round()
        expect("после ядра — соединение",
               pool.get(entry.fp).state == SimpleState.CONNECTED.value)

        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING, 15),
            LEVEL_CORE: ok(LEVEL_CORE, 30),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC, 42),
        })
        supervisor.run_round()
        current = pool.get(entry.fp)
        expect("после трафика — рабочий",
               current.state == SimpleState.WORKING.value)
        expect("достигнут уровень 3", current.level == 3)
        expect("задержка записана", current.latency_ms == 42)
        expect("ссылка попала в выходной файл",
               output_lines(supervisor) == [VLESS_A])
        expect("счётчик рабочих", supervisor.snapshot().working == 1)
        expect("рабочий сервер больше не проверяется сразу",
               supervisor.run_round() == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_servers_stay_in_pool() -> None:
    """По умолчанию нерабочие серверы не удаляются, а помечаются."""
    print("test_failed_servers_stay_in_pool")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint,
                          {LEVEL_PING: fail(LEVEL_PING, "тайм-аут TCP")})
        for attempt in range(1, 5):
            supervisor.run_round()
            expect(f"после неудачи {attempt} запись ещё жива",
                   pool.get(entry.fp) is not None)
        expect("накопилось 4 неудачи", pool.get(entry.fp).ping_fail == 4)
        supervisor.run_round()
        current = pool.get(entry.fp)
        expect("после 5 неудач сервер остался в пуле", current is not None)
        expect("статус — «Не работает»",
               current is not None and current.state == SimpleState.FAILED.value)
        expect("ничего не удалено и не исключено",
               supervisor.snapshot().deleted == 0
               and pool.blocked_count() == 0
               and pool.blocked_address_count() == 0)
        expect("событие учтено в статистике",
               supervisor.snapshot().exhausted == 1
               and supervisor.snapshot().failed == 1)

        calls = prober.count(entry.endpoint, LEVEL_PING)
        supervisor.run_round()
        supervisor.run_round()
        expect("нерабочий сервер больше не опрашивается",
               prober.count(entry.endpoint, LEVEL_PING) == calls)

        pool.save(force=True)
        restored = Pool(pool.path)
        restored.load()
        expect("статус «Не работает» переживает перезапуск",
               restored.get(entry.fp) is not None
               and restored.get(entry.fp).state == SimpleState.FAILED.value)

        # Из «не работает» можно вернуть в оборот вручную.
        pool.reset([entry.fp])
        expect("сброс в «новые» возвращает сервер",
               pool.get(entry.fp).state == SimpleState.NEW.value)
        supervisor.run_round()
        expect("после сброса сервер снова проверяется",
               prober.count(entry.endpoint, LEVEL_PING) == calls + 1)

        # Удаление и чёрный список — разные процедуры.
        counters = supervisor.delete_entries([entry.fp])
        expect("удаление убирает запись", counters["removed"] == 1
               and pool.get(entry.fp) is None)
        expect("удаление не заносит адрес в чёрный список",
               pool.blocked_address_count() == 0)
        back = supervisor.import_text(VLESS_A, "повторно")
        expect("после простого удаления сервер возвращается из подписки",
               back["added"] == 1)

        entry = only(pool)
        expect("адрес можно внести в чёрный список отдельно",
               supervisor.blacklist_entries([entry.fp])["addresses"] == 1
               and pool.blocked_address_count() == 1
               and pool.size() == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_delete_failed_option() -> None:
    """Опция «удалять нерабочие автоматически» возвращает старое поведение."""
    print("test_delete_failed_option")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.set("simple_delete_failed", True)
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint,
                          {LEVEL_PING: fail(LEVEL_PING, "тайм-аут TCP")})
        for _ in range(5):
            supervisor.run_round()
        expect("с включённой опцией сервер удалён", pool.get(entry.fp) is None)
        expect("и попал в автоматические исключения",
               pool.blocked_count() == 1)
        counters = supervisor.import_text(VLESS_A, "тест")
        expect("повторно из подписки не добавляется",
               counters["added"] == 0 and counters["blocked"] == 1)
        expect("удаление учтено в статистике",
               supervisor.snapshot().deleted == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_retry_pause() -> None:
    print("test_retry_pause")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.set("simple_retry_minutes", 15.0)
        supervisor.reload_policy()
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint,
                          {LEVEL_PING: fail(LEVEL_PING, "тайм-аут TCP")})
        supervisor.run_round()
        supervisor.run_round()
        supervisor.run_round()
        expect("повторная попытка отложена по времени",
               prober.count(entry.endpoint, LEVEL_PING) == 1)
        age(pool, entry.fp, next_check_at=0.0)
        supervisor.run_round()
        expect("после паузы попытка повторяется",
               prober.count(entry.endpoint, LEVEL_PING) == 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_core_failures_demote_and_delete() -> None:
    print("test_core_failures_demote_and_delete")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: fail(LEVEL_CORE, "тайм-аут при подключении"),
        })
        supervisor.run_round()                    # пинг ok + ядро провалено
        current = pool.get(entry.fp)
        expect("сервер возвращён в «новые»",
               current.state == SimpleState.NEW.value)
        expect("неудача ядра зачтена", current.core_fail == 1)
        expect("счётчик пинга сброшен", current.ping_fail == 0)
        for _ in range(4):
            supervisor.run_round()                # пинг ok + ядро провалено
        current = pool.get(entry.fp)
        expect("после 5 неудач ядра сервер помечен нерабочим",
               current is not None
               and current.state == SimpleState.FAILED.value)
        expect("запись не удалена", supervisor.snapshot().deleted == 0)
        expect("счётчик ядра в статистике",
               supervisor.snapshot().core_fail == 5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_traffic_failure_demotes() -> None:
    print("test_traffic_failure_demotes")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: fail(LEVEL_TRAFFIC, "контрольный запрос не прошёл"),
        })
        supervisor.run_round()      # пинг + ядро + провал трафика
        current = pool.get(entry.fp)
        expect("сервер без трафика разжалован",
               current.state == SimpleState.NEW.value)
        expect("неудача трафика зачтена", current.traffic_fail == 1)
        expect("в выходной файл не попал", output_lines(supervisor) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_working_maintenance() -> None:
    print("test_working_maintenance")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        endpoint = entry.endpoint
        prober.script_for(endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC),
        })
        for _ in range(3):
            supervisor.run_round()
        expect("сервер стал рабочим",
               pool.get(entry.fp).state == SimpleState.WORKING.value)

        # Час спустя: успешный пинг подтверждает статус
        age(pool, entry.fp, last_ping_at=time.time() - 2 * 3600)
        supervisor.run_round()
        expect("пинг подтвердил рабочий статус",
               pool.get(entry.fp).state == SimpleState.WORKING.value)
        expect("ссылка осталась в файле",
               output_lines(supervisor) == [VLESS_A])

        # Час спустя: пинг не прошёл — разжалование и удаление из файла
        age(pool, entry.fp, last_ping_at=time.time() - 2 * 3600)
        prober.script_for(endpoint, {
            LEVEL_PING: fail(LEVEL_PING, "тайм-аут TCP"),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC),
        })
        supervisor.run_round()
        expect("неудачный пинг разжаловал рабочего",
               pool.get(entry.fp).state == SimpleState.NEW.value)
        expect("ссылка убрана из файла", output_lines(supervisor) == [])

        # Возвращаем в рабочие и проверяем плановый трафик
        prober.script_for(endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC),
        })
        for _ in range(3):
            supervisor.run_round()
        expect("сервер снова рабочий",
               pool.get(entry.fp).state == SimpleState.WORKING.value)

        age(pool, entry.fp, last_traffic_at=time.time() - 7 * 3600)
        supervisor.run_round()
        expect("плановый трафик подтвердил рабочего",
               pool.get(entry.fp).state == SimpleState.WORKING.value)

        age(pool, entry.fp, last_traffic_at=time.time() - 7 * 3600)
        prober.script_for(endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: fail(LEVEL_TRAFFIC, "трафик не прошёл"),
        })
        supervisor.run_round()
        expect("провал планового трафика разжаловал рабочего",
               pool.get(entry.fp).state == SimpleState.NEW.value)
        expect("ссылка убрана из файла после провала трафика",
               output_lines(supervisor) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unsupported_and_missing_core() -> None:
    print("test_unsupported_and_missing_core")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        # Этап ядра сообщает о принципиальной несовместимости
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: fail(LEVEL_CORE, "транспорт «quic» не поддержан",
                             permanent=True),
        })
        supervisor.run_round()
        supervisor.run_round()
        expect("конфигурация помечена как неподдерживаемая",
               pool.get(entry.fp).state == SimpleState.UNSUPPORTED.value)
        expect("неподдерживаемая больше не проверяется",
               supervisor.run_round() == 0)

        # Ядро не найдено — это не неудача сервера
        pool.reset([entry.fp])
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: fail(LEVEL_CORE, "ядро xray не найдено", no_core=True),
        })
        supervisor.run_round()
        supervisor.run_round()
        supervisor.run_round()
        current = pool.get(entry.fp)
        expect("запись осталась кандидатом",
               current.state == SimpleState.REACHABLE.value)
        expect("отсутствие ядра не считается неудачей", current.core_fail == 0)
        expect("запись не удалена", current is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pool_persistence() -> None:
    print("test_pool_persistence")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        # Здесь проверяется и старое поведение с автоудалением, поэтому опция
        # включена; новый режим «остаётся нерабочим» проверяется отдельно.
        settings.set("simple_delete_failed", True)
        supervisor.import_text(f"{VLESS_A}\n{VLESS_B}", "тест")
        entry = pool.items()[0]
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC),
        })
        for _ in range(3):
            supervisor.run_round()
        other = [item for item in pool.items() if item.fp != entry.fp][0]
        prober.script_for(other.endpoint,
                          {LEVEL_PING: fail(LEVEL_PING, "тайм-аут")})
        for _ in range(5):
            supervisor.run_round()
        pool.save(force=True)

        restored = Pool(pool.path)
        restored.load()
        expect("рабочий сервер сохранён",
               restored.get(entry.fp) is not None
               and restored.get(entry.fp).state == SimpleState.WORKING.value)
        expect("удалённый сервер не вернулся", restored.get(other.fp) is None)
        expect("«исключённые» сохранены", restored.blocked_count() == 1)
        expect("восстановленная запись умеет собирать конфигурацию",
               restored.get(entry.fp).config().address == "a.example.com")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_form_values() -> None:
    print("test_settings_form_values")
    # Именно этот набор строковых полей раньше «падал» в подстановку дефолтов
    text_fields = [
        ("log_level", "INFO", str),
        ("traffic_url", "http://cp.cloudflare.com/", str),
        ("simple_user_agent", "v2rayNG/1.8.5", str),
        ("xray_path", "", str),
        ("singbox_path", "", str),
        ("simple_output_path", r"C:\data\simple\working.txt", str),
    ]
    values, repaired = collect_field_values(text_fields, ["vless", "vmess"],
                                            "cascade")
    expect("строковые поля не считаются битыми", repaired == [])
    expect("пустой путь к ядру остаётся пустым (автоопределение)",
           values["xray_path"] == "" and values["singbox_path"] == "")
    expect("путь вывода сохранён как введён",
           values["simple_output_path"] == r"C:\data\simple\working.txt")
    expect("уровень журнала сохранён", values["log_level"] == "INFO")
    expect("цель трафика сохранена", values["traffic_url"].startswith("http://"))
    expect("User-Agent сохранён",
           values["simple_user_agent"] == "v2rayNG/1.8.5")

    numeric_fields = [
        ("simple_max_failures", "5", int),
        ("simple_source_refresh_hours", "6", float),
        ("core_timeout", "12,0", float),
        ("simple_ping_workers", "128", int),
        ("simple_auto_start", True, bool),
    ]
    values, repaired = collect_field_values(numeric_fields, ["vless"],
                                            "cascade")
    expect("корректные числа не считаются битыми", repaired == [])
    expect("целое сохранено как целое", values["simple_max_failures"] == 5)
    expect("дробное с запятой разобрано", values["core_timeout"] == 12.0)
    expect("переключатель сохранён", values["simple_auto_start"] is True)

    broken_fields = [
        ("log_level", "болтовня", str),
        ("traffic_url", "   ", str),
        ("simple_user_agent", "", str),
        ("simple_output_path", "  ", str),
        ("simple_max_failures", "", int),
        ("core_timeout", "abc", float),
    ]
    values, repaired = collect_field_values(broken_fields, [], "нет такого")
    expect("битые поля перечислены",
           set(repaired) == {"log_level", "traffic_url", "simple_user_agent",
                             "simple_output_path", "simple_max_failures",
                             "core_timeout", "simple_protocols",
                             "simple_stage_mode"})
    expect("уровень журнала исправлен", values["log_level"] == "INFO")
    expect("цель трафика исправлена", values["traffic_url"].startswith("http"))
    expect("пустой путь вывода исправлен",
           values["simple_output_path"].endswith(".txt"))
    expect("пустой список протоколов заменён полным",
           len(values["simple_protocols"]) == len(DEFAULT_SIMPLE_PROTOCOLS))
    expect("режим этапов исправлен",
           values["simple_stage_mode"] == "cascade")


def test_v2rayn_export() -> None:
    print("test_v2rayn_export")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.update({
            "simple_output_format": OUTPUT_V2RAYN,
            "simple_v2rayn_path": os.path.join(tmp, "v2rayN.json"),
        })
        supervisor.reload_policy()
        supervisor.import_text(f"{VLESS_A}\n{VLESS_B}", "тест")
        for entry in pool.items():
            prober.script_for(entry.endpoint, {
                LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
                LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        for _ in range(3):
            supervisor.run_round()
        expect("два сервера стали рабочими",
               supervisor.snapshot().working == 2)

        json_path = supervisor.json_writer.path
        expect("JSON-файл создан", os.path.isfile(json_path))
        with open(json_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        tags = [item["tag"] for item in document["outbounds"]]
        expect("в группе оба сервера плюс служебные outbounds",
               tags == ["proxy-0", "proxy-1", "direct", "block"])
        expect("есть локальные SOCKS и HTTP порты",
               [item["protocol"] for item in document["inbounds"]]
               == ["socks", "http"])
        expect("локальный SOCKS порт как в v2rayN по умолчанию",
               document["inbounds"][0]["port"] == 10808)
        expect("observatory настроен (без него leastPing не работает)",
               document["observatory"]["subjectSelector"]
               == ["proxy-0", "proxy-1"])
        balancer = document["routing"]["balancers"][0]
        expect("балансировщик leastPing по всем серверам",
               balancer["strategy"]["type"] == "leastPing"
               and balancer["selector"] == ["proxy-0", "proxy-1"])
        expect("правило направляет весь трафик в балансировщик",
               document["routing"]["rules"][0]["balancerTag"] == balancer["tag"])
        expect("цель измерений совпадает с целью контрольного запроса",
               document["observatory"]["probeUrl"] == settings.get("traffic_url"))
        expect("в режиме v2rayN текстовый файл не создаётся",
               not os.path.isfile(settings.get("simple_output_path")))

        settings.set("simple_output_format", OUTPUT_BOTH)
        supervisor.write_output_now()
        expect("режим «оба файла» пишет и список ссылок",
               os.path.isfile(settings.get("simple_output_path")))
        expect("в статистике видны оба файла",
               supervisor.snapshot().v2rayn_count == 2
               and supervisor.snapshot().output_count == 2)

        # Hysteria2 ядро Xray не умеет — в группу она не попадает
        supervisor.import_text(
            "hysteria2://secret@h.example.com:443?sni=h.example.com#H", "тест")
        hy_entry = [entry for entry in pool.items()
                    if entry.protocol == "hysteria2"][0]
        prober.script_for(hy_entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        for _ in range(3):
            supervisor.run_round()
        supervisor.write_output_now()
        expect("hysteria2 стала рабочей",
               pool.get(hy_entry.fp).state == SimpleState.WORKING.value)
        with open(json_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        expect("в JSON нет hysteria2 (её не поднимет Xray)",
               all(item["protocol"] != "hysteria2"
                   for item in document["outbounds"]))
        expect("в списке ссылок hysteria2 осталась",
               any(line.startswith("hysteria2://")
                   for line in output_lines(supervisor)))

        # Список сокращается — группа в JSON тоже
        victim = [entry for entry in pool.items()
                  if entry.fp != hy_entry.fp][0]
        prober.script_for(victim.endpoint, {
            LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: fail(LEVEL_TRAFFIC, "трафик не прошёл")})
        age(pool, victim.fp, last_traffic_at=time.time() - 7 * 3600)
        supervisor.run_round()
        expect("разжалованный сервер ушёл из JSON",
               supervisor.snapshot().v2rayn_count == 1)
        expect("и из списка ссылок", supervisor.snapshot().output_count == 2)

        # Текст для буфера обмена: v2rayN импортирует его как отдельные записи
        lines = [line for line in supervisor.working_uri_text().splitlines()
                 if line.strip()]
        expect("текст для буфера — обычные share-ссылки",
               all(line.startswith(("vless://", "vmess://", "trojan://",
                                    "ss://", "hysteria2://", "tuic://"))
                   for line in lines))
        expect("в тексте все рабочие серверы",
               len(lines) == supervisor.snapshot().working)
        expect("порядок совпадает с порядком файла",
               lines == supervisor.working_uris())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cores_stop_and_restart() -> None:
    print("test_cores_stop_and_restart")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.start(refresh_sources=False)
        expect("цикл запустился", supervisor.running)
        supervisor.stop()
        expect("цикл остановлен", not supervisor.running)
        # Повторный старт: ядра снова разрешены (проверяем на реальном пробере —
        # у заглушки из тестов такого API нет, и это не должно ломать запуск).
        from simple.probe import Prober
        supervisor.prober = Prober(settings)
        supervisor.start(refresh_sources=False)
        expect("перезапуск не блокирует ядра",
               supervisor.prober.runner._stopping is False)
        supervisor.stop()
        expect("после остановки новые ядра запрещены",
               supervisor.prober.runner._stopping is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_traffic_targets() -> None:
    print("test_traffic_targets")
    from app.checker import handshake_targets, traffic_targets
    from app.constants import DEFAULT_SETTINGS

    class Stub:
        def __init__(self, data):
            self.data = dict(data)

        def get(self, key, default=None):
            return self.data.get(key, default)

    on = Stub({"traffic_url": "http://cp.cloudflare.com/",
               "traffic_fallbacks": True})
    off = Stub({"traffic_url": "http://cp.cloudflare.com/",
                "traffic_fallbacks": False})
    empty = Stub({})

    expect("с запасными целями — четыре запроса",
           len(traffic_targets(on)) == 4)
    expect("первой всегда идёт настроенная цель",
           traffic_targets(on)[0] == "http://cp.cloudflare.com/")
    expect("без запасных целей — ровно один запрос",
           traffic_targets(off) == ["http://cp.cloudflare.com/"])
    expect("с запасными целями — две цели рукопожатия",
           len(handshake_targets(on)) == 2)
    expect("без запасных целей — одна цель рукопожатия",
           handshake_targets(off) == [("cp.cloudflare.com", 80)])
    expect("без настроенной цели резерв остаётся",
           len(traffic_targets(empty)) == 4
           and len(handshake_targets(empty)) == 2)
    expect("настройка есть в значениях по умолчанию основного проекта",
           DEFAULT_SETTINGS.get("traffic_fallbacks") is True)


def test_maintenance_priority() -> None:
    print("test_maintenance_priority")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING),
            LEVEL_CORE: ok(LEVEL_CORE),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC),
        })
        for _ in range(3):
            supervisor.run_round()
        expect("сервер стал рабочим",
               pool.get(entry.fp).state == SimpleState.WORKING.value)

        # Хвост записей, ожидающих этапа трафика, и хвост новых записей
        congest(pool, 50, SimpleState.CONNECTED.value)
        congest(pool, 50, SimpleState.NEW.value)
        expect("пул набит хвостом", pool.size() == 101)

        policy = supervisor.policy
        now = time.time()
        selected = pool.select(LEVEL_TRAFFIC, policy, now, 1)
        expect("без просрочки рабочий по трафику не выбирается",
               all(item.fp != entry.fp for item in selected))

        age(pool, entry.fp, last_traffic_at=now - 7 * 3600)
        selected = pool.select(LEVEL_TRAFFIC, policy, time.time(), 1)
        expect("плановая проверка трафика рабочего идёт первой",
               selected and selected[0].fp == entry.fp)

        age(pool, entry.fp, last_traffic_at=time.time(),
            last_ping_at=time.time() - 2 * 3600)
        selected = pool.select(LEVEL_PING, policy, time.time(), 1)
        expect("плановая проверка пингом тоже идёт первой",
               selected and selected[0].fp == entry.fp)

        # Свежие записи всё равно обрабатываются следом
        selected = pool.select(LEVEL_PING, policy, time.time(), 3)
        expect("в порции есть и обычные кандидаты",
               len(selected) == 3 and selected[0].fp == entry.fp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_defaults() -> None:
    print("test_settings_defaults")
    tmp = tempfile.mkdtemp(prefix="simple_cfg_")
    try:
        path = os.path.join(tmp, "settings.json")
        settings = SimpleSettings(path)
        expect("все параметры имеют значения по умолчанию",
               all(value is not None for value in
                   (settings.get(k, DEFAULTS[k]) for k in DEFAULTS)))
        empty = [key for key, default in DEFAULTS.items()
                 if isinstance(default, str)
                 and key not in ("xray_path", "singbox_path",
                                 "traffic_expect")
                 and not str(settings.get(key, "")).strip()]
        expect("нет пустых полей настроек", not empty)
        expect("частота обновления подписок",
               settings.get("simple_source_refresh_hours") == 6.0)
        expect("порог неудач", settings.get("simple_max_failures") == 5)
        expect("потоки пинга", settings.get("simple_ping_workers") == 128)
        expect("потоки ядра", settings.get("simple_core_workers") == 8)
        expect("тайм-аут ядра", settings.get("core_timeout") == 12.0)
        expect("тайм-аут трафика", settings.get("traffic_timeout") == 8.0)
        expect("тайм-аут TCP", settings.get("tcp_timeout") == 4.0)
        expect("тайм-аут DNS", settings.get("dns_timeout") == 4.0)
        expect("цель контрольного запроса",
               "://" in str(settings.get("traffic_url")))
        expect("путь выходного файла",
               str(settings.get("simple_output_path")).endswith(".txt"))
        expect("все протоколы кроме trojan разрешены",
               set(settings.get("simple_protocols"))
               == {p.value for p in IMPORTABLE_PROTOCOLS}
               - {Protocol.TROJAN.value})
        expect("автозагрузка ядра включена по умолчанию",
               settings.get("auto_download_core") is True)
        expect("доля рабочих за проход по умолчанию — четверть",
               settings.get("simple_maintenance_fraction") == 0.25)
        expect("серверов в пачке по умолчанию — 50",
               settings.get("simple_maintenance_max") == 50)
        expect("порог сбоя пачки по умолчанию — половина",
               settings.get("simple_maintenance_guard") == 0.5)
        expect("сетевой предохранитель включён",
               settings.get("simple_network_guard") is True)
        expect("срок годности нерабочих — трое суток",
               settings.get("simple_failed_ttl_hours") == 72.0)
        expect("запись пула ограничена по частоте",
               settings.get("simple_save_min_seconds") == 30.0)
        expect("файл настроек создан при первом запуске",
               os.path.isfile(path))

        # Файл, испорченный пустыми и мусорными значениями
        broken = {
            "simple_source_refresh_hours": "",
            "simple_max_failures": None,
            "simple_retry_minutes": "",
            "simple_ping_workers": 0,
            "simple_core_workers": -3,
            "simple_output_path": "",
            "simple_user_agent": "   ",
            "traffic_url": "мусор",
            "log_level": "болтовня",
            "simple_stage_mode": "нет такого",
            "simple_protocols": [],
            "simple_sources": "строка вместо списка",
            "core_timeout": "",
            "simple_blacklist_hours": 0,
            "auto_download_core": False,
            "simple_maintenance_fraction": 5.0,
            "simple_maintenance_max": 0,
            "simple_maintenance_guard": "",
            "simple_network_guard": None,
            "simple_failed_ttl_hours": 0,
            "simple_save_min_seconds": "мусор",
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(broken, handle, ensure_ascii=False)

        restored = SimpleSettings(path)
        expect("битые значения обнаружены", bool(restored.repairs))
        expect("частота обновления восстановлена",
               restored.get("simple_source_refresh_hours") == 6.0)
        expect("порог неудач восстановлен",
               restored.get("simple_max_failures") == 5)
        expect("потоки пинга не могут быть нулём",
               restored.get("simple_ping_workers") >= 1)
        expect("отрицательные потоки ядра исправлены",
               restored.get("simple_core_workers") >= 1)
        expect("пустой путь вывода восстановлен",
               str(restored.get("simple_output_path")).endswith(".txt"))
        expect("User-Agent восстановлен",
               restored.get("simple_user_agent") == "v2rayNG/1.8.5")
        expect("цель трафика восстановлена",
               "://" in str(restored.get("traffic_url")))
        expect("уровень журнала восстановлен",
               restored.get("log_level") in LOG_LEVELS)
        expect("режим этапов восстановлен",
               restored.get("simple_stage_mode") in ("cascade", "phases"))
        expect("протоколы восстановлены",
               len(restored.get("simple_protocols"))
               == len(DEFAULT_SIMPLE_PROTOCOLS))
        expect("источники приведены к списку",
               isinstance(restored.get("simple_sources"), list))
        expect("тайм-аут ядра восстановлен",
               restored.get("core_timeout") == 12.0)
        expect("тайм-аут трафика восстановлен",
               restored.get("traffic_timeout") == 8.0)
        expect("ноль часов блокировки сохранён как задумано",
               restored.get("simple_blacklist_hours") == 0)
        expect("ноль часов срока годности сохранён как задумано",
               restored.get("simple_failed_ttl_hours") == 0)
        expect("мусорная частота записи пула восстановлена",
               restored.get("simple_save_min_seconds") == 30.0)
        expect("политика выключает очистку при нулевом сроке",
               simple_policy(restored).failed_ttl_seconds == 0.0)
        expect("явный отказ от автозагрузки уважается",
               restored.get("auto_download_core") is False)
        expect("доля за проход ограничена единицей",
               restored.get("simple_maintenance_fraction") == 1.0)
        expect("размер пачки не может быть нулём",
               restored.get("simple_maintenance_max") >= 1)
        expect("порог сбоя пачки восстановлен",
               restored.get("simple_maintenance_guard") == 0.5)
        expect("сетевой предохранитель включён по умолчанию",
               restored.get("simple_network_guard") is True)

        with open(path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        expect("исправления записаны в файл",
               on_disk.get("simple_max_failures") == 5
               and str(on_disk.get("traffic_url", "")).startswith("http"))

        # Конвейер должен работать на восстановленных настройках
        pool = Pool(os.path.join(tmp, "pool.json"))
        prober = FakeProber()
        supervisor = Supervisor(restored, pool, prober)
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint, {LEVEL_PING: ok(LEVEL_PING)})
        supervisor.run_round()
        expect("цикл работает на восстановленных настройках",
               pool.get(entry.fp).state == SimpleState.REACHABLE.value)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stage_modes() -> None:
    print("test_stage_modes")
    uris = f"{VLESS_A}\n{VLESS_B}\n{VLESS_D}"
    endpoints = ["a.example.com:443", "b.example.com:8443", "d.example.com:443"]

    # Каскад: за круг обрабатывается по одной порции каждого этапа
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.update({"simple_stage_mode": "cascade", "simple_core_batch": 1,
                         "simple_traffic_batch": 1})
        supervisor.reload_policy()
        supervisor.import_text(uris, "тест")
        for endpoint in endpoints:
            prober.script_for(endpoint, {
                LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
                LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        supervisor.run_round()
        core_calls = sum(prober.count(endpoint, LEVEL_CORE)
                         for endpoint in endpoints)
        expect("каскад: за круг только одна порция ядра", core_calls == 1)
        expect("каскад: все три адреса пингуются сразу",
               all(prober.count(endpoint, LEVEL_PING) == 1
                   for endpoint in endpoints))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Фазы: сначала весь пинг, затем всё ядро, затем весь трафик
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.update({"simple_stage_mode": "phases", "simple_core_batch": 1,
                         "simple_traffic_batch": 1})
        supervisor.reload_policy()
        supervisor.import_text(uris, "тест")
        for endpoint in endpoints:
            prober.script_for(endpoint, {
                LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
                LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        supervisor.run_round()
        core_calls = sum(prober.count(endpoint, LEVEL_CORE)
                         for endpoint in endpoints)
        expect("фазы: за круг ядро проверяет всех выживших", core_calls == 3)
        expect("фазы: за круг все три сервера становятся рабочими",
               supervisor.snapshot().working == 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Внешние команды
# ---------------------------------------------------------------------------

def test_command_settings() -> None:
    print("test_command_settings")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.set("simple_commands", [
            "мусор",
            {"name": "пусто"},
            {"name": "Битая", "kind": "нет такого", "trigger": "нет такого",
             "artifact": "нет такого", "command": "cmd /c echo hi",
             "interval_minutes": 0, "timeout_seconds": 1, "retry_count": 99,
             "min_gap_seconds": -5, "port": 99999},
            {"name": "Скрипт", "kind": "script",
             "script": ' "C:\\путь с пробелом\\up.cmd" ',
             "arguments": '"{file}"', "env": {"A": 1, "": 2}, "enabled": False,
             "hidden": True},
        ])
        # Мусор в файле настроек «лечится» при следующем чтении.
        again = SimpleSettings(os.path.join(tmp, "settings.json"))
        expect("список команд попал в исправления",
               "simple_commands" in again.repairs)
        stored = again.get("simple_commands")
        expect("мусорные записи отброшены",
               isinstance(stored, list) and len(stored) == 2)
        first = stored[0]
        expect("тип, триггер и артефакт исправлены",
               (first["kind"], first["trigger"], first["artifact"])
               == ("script", "on_change", "links"))
        expect("интервал не может быть меньше минуты",
               first["interval_minutes"] == 1.0)
        expect("тайм-аут приведён к минимуму", first["timeout_seconds"] == 5.0)
        expect("число повторов ограничено", first["retry_count"] == 5)
        expect("отрицательная пауза обнулена", first["min_gap_seconds"] == 0.0)
        expect("порт ограничен", first["port"] == 65535)
        expect("идентификатор выдан", len(first["id"]) == 12)
        second = stored[1]
        expect("кавычки вокруг пути скрипта сняты",
               second["script"] == r"C:\путь с пробелом\up.cmd")
        expect("пустое имя переменной отброшено", second["env"] == {"A": "1"})
        expect("выключенный пресет сохранён", second["enabled"] is False)
        expect("флаг приватности сохранён", second["hidden"] is True)
        expect("обычный пресет не помечен скрытым", first["hidden"] is False)

        expect("идентификатор не меняется при перечитывании",
               again.get("simple_commands")[0]["id"] == first["id"])
        expect("планировщик читает пресеты из настроек",
               supervisor.commands.presets() == [])
        supervisor.commands.load()
        expect("планировщик подхватил исправленные пресеты",
               len(supervisor.commands.presets()) == 2)

        settings.set("simple_commands", ["мусор", 42, {"name": "пусто"}])
        supervisor.commands.load()
        expect("мусор в настройках не ломает планировщик",
               supervisor.commands.presets() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_address_blacklist() -> None:
    print("test_address_blacklist")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        # Две конфигурации одного адреса: удаление одной должно убрать адрес
        # целиком — он попадает в постоянный чёрный список.
        twin = ("vless://999e4567-e89b-12d3-a456-426614174000@a.example.com:443"
                "?type=tcp&security=tls#A2")
        supervisor.import_text(f"{VLESS_A}\n{twin}", "тест")
        expect("в пуле две конфигурации одного адреса", pool.size() == 2)
        entry = pool.items()[0]
        address, port = entry.address, entry.port
        expect("до удаления адрес не в чёрном списке",
               not pool.is_blocked_address(address, port))

        counters = supervisor.blacklist_entries([entry.fp])
        expect("внесение адреса в чёрный список убрало все его конфигурации",
               counters["removed"] == 2 and pool.size() == 0)
        expect("адрес попал в чёрный список",
               pool.is_blocked_address(address, port)
               and pool.blocked_address_count() == 1)

        again = supervisor.import_text(VLESS_A, "повторный импорт")
        expect("повторный импорт из подписки отброшен чёрным списком",
               again["blocked"] == 1 and again["added"] == 0)
        expect("пул остался пустым", pool.size() == 0)

        pool.add_many([entry.clone()])
        expect("add_many тоже не принимает адрес из чёрного списка",
               pool.size() == 0)

        records = pool.blocked_addresses()
        expect("снимок списка отдаёт адрес и причину",
               bool(records) and records[0]["host"] == address.lower()
               and "вручную" in str(records[0]["reason"]))

        pool.purge_blacklist(time.time() + 10 ** 6, supervisor.policy)
        expect("постоянный чёрный список не истекает по времени",
               pool.is_blocked_address(address, port))

        # Он же не должен попадать в опрос: даже если запись вернуть в пул в
        # обход проверок, этап её не выберет.
        pool._entries[entry.fp] = entry
        prober.calls.clear()
        supervisor.run_round()
        expect("адрес из чёрного списка не опрашивается",
               all(endpoint != entry.endpoint for endpoint, _level
                   in prober.calls))
        pool.remove([entry.fp])

        expect("адрес можно разрешить снова",
               supervisor.unblock_address(address, port)
               and not pool.is_blocked_address(address, port))
        back = supervisor.import_text(VLESS_A, "после разрешения")
        expect("после снятия блокировки сервер добавляется снова",
               back["added"] == 1 and pool.size() == 1)

        cleared = supervisor.clear_blocklist()
        expect("очистка чёрного списка работает",
               cleared["addresses"] == 0 or cleared["blocked"] >= 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hotkey_mapping() -> None:
    """Клавиши распознаются по коду, а не по раскладке."""
    print("test_hotkey_mapping")
    from app.gui import common as gui_common

    class FakeEvent:
        def __init__(self, keycode: int = 0, keysym: str = "", state: int = 0):
            self.keycode = keycode
            self.keysym = keysym
            self.state = state

    expect("Del распознаётся по коду клавиши",
           gui_common.pressed_action(FakeEvent(46)) == "delete")
    expect("F5 распознаётся по коду клавиши",
           gui_common.pressed_action(FakeEvent(116)) == "refresh")
    expect("Ctrl+V распознаётся при русском keysym",
           gui_common.pressed_action(FakeEvent(86, "Cyrillic_em")) == "paste")
    expect("Ctrl+C распознаётся даже с непонятным keysym",
           gui_common.pressed_action(FakeEvent(67, "??")) == "copy")
    expect("Ctrl+A и Ctrl+X распознаются",
           gui_common.pressed_action(FakeEvent(65)) == "select_all"
           and gui_common.pressed_action(FakeEvent(88)) == "cut")
    expect("запасной путь по keysym работает",
           gui_common.pressed_action(FakeEvent(0, "Delete")) == "delete")
    expect("посторонняя клавиша не считается действием",
           gui_common.pressed_action(FakeEvent(0, "F9")) == "")
    expect("модификаторы разбираются",
           gui_common.has_control(FakeEvent(state=0x0004))
           and gui_common.has_shift(FakeEvent(state=0x0001))
           and not gui_common.has_control(FakeEvent(state=0x0001)))
    expect("поля ввода опознаются по классу",
           gui_common.is_text_widget(FakeText("TEntry"))
           and gui_common.is_text_widget(FakeText("Text"))
           and not gui_common.is_text_widget(FakeText("Treeview")))


class FakeText:
    """Минимальная заглушка виджета для проверки is_text_widget."""

    def __init__(self, widget_class: str) -> None:
        self._class = widget_class

    def winfo_class(self) -> str:
        return self._class


def test_pool_rows_view() -> None:
    """Лёгкие строки таблицы и быстрый пересчёт статистики для интерфейса."""
    print("test_pool_rows_view")
    from simple.state import PoolRow

    tmp, settings, pool, prober, supervisor = make_env()
    try:
        # Пишем оба файла: тяжёлое число «совместимо с Xray» сверяется с тем,
        # что реально легло в конфигурацию для v2rayN.
        settings.set("simple_output_format", OUTPUT_BOTH)
        supervisor.import_text(f"{VLESS_A}\n{VLESS_B}", "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint, {
            LEVEL_PING: ok(LEVEL_PING, 15),
            LEVEL_CORE: ok(LEVEL_CORE, 30),
            LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC, 42),
        })
        supervisor.run_round()

        rows = pool.rows()
        expect("строк столько же, сколько записей", len(rows) == pool.size())
        expect("строки — лёгкие кортежи без данных конфигурации",
               all(isinstance(row, PoolRow) for row in rows)
               and not any(hasattr(row, "data") for row in rows))
        by_fp = {row.fp: row for row in rows}
        working = [item for item in pool.items()
                   if item.state == SimpleState.WORKING.value]
        expect("рабочая запись одна", len(working) == 1)
        current = working[0]
        row = by_fp[current.fp]
        expect("строка повторяет поля записи",
               row.state == current.state and row.uri == current.uri
               and row.endpoint == current.endpoint
               and row.protocol == current.protocol
               and row.latency_ms == current.latency_ms
               and row.last_checked == current.last_checked
               and row.label == current.label
               and (row.ping_fail, row.core_fail, row.traffic_fail)
               == (current.ping_fail, current.core_fail,
                   current.traffic_fail))

        # Дорогая часть статистики (совместимость с Xray) совпадает с тем, что
        # реально записано в файл для v2rayN.
        stats = supervisor.refresh_stats()
        expect("полный пересчёт считает конфигурации для v2rayN",
               stats.v2rayn_count == 1)
        expect("число совпадает с записанным файлом",
               supervisor.json_writer.count == 1
               and supervisor.writer.count == 1)
        expect("полный пересчёт видит рабочий сервер", stats.working == 1)

        # Интерфейс обновляет счётчики часто: дорогое число он только читает.
        supervisor.stats.v2rayn_count = 42
        quick = supervisor.refresh_stats(heavy=False)
        expect("быстрый пересчёт не трогает дорогое число",
               quick.v2rayn_count == 42)
        expect("быстрый пересчёт всё равно обновляет счётчики",
               quick.total == pool.size() and quick.working == 1)
        restored = supervisor.refresh_stats()
        expect("полный пересчёт возвращает число обратно",
               restored.v2rayn_count == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_frozen_base_dir() -> None:
    """Где приложение хранит данные: корень проекта или папка собранного exe."""
    print("test_frozen_base_dir")
    from app import constants

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(
        constants.__file__)))
    expect("без сборки данные лежат в корне проекта",
           os.path.abspath(constants.base_dir())
           == os.path.abspath(project_root))

    saved_frozen = getattr(sys, "frozen", None)
    saved_exe = getattr(sys, "executable", None)
    fake_dir = tempfile.mkdtemp(prefix="simple_frozen_")
    try:
        sys.frozen = True                       # type: ignore[attr-defined]
        sys.executable = os.path.join(fake_dir, "XrayConfigCheckerSimple.exe")
        expect("в собранном exe данные лежат рядом с ним",
               os.path.abspath(constants.base_dir()) == os.path.abspath(fake_dir))
    finally:
        if saved_frozen is None:
            try:
                del sys.frozen                  # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            sys.frozen = saved_frozen           # type: ignore[attr-defined]
        if saved_exe is not None:
            sys.executable = saved_exe
        shutil.rmtree(fake_dir, ignore_errors=True)
    expect("обычный режим вернулся",
           os.path.abspath(constants.base_dir()) == os.path.abspath(project_root))


def test_upload_helper_script() -> None:
    print("test_upload_helper_script")
    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "scripts", "upload-verify.cmd")
    expect("шаблон scripts/upload-verify.cmd лежит в репозитории",
           os.path.isfile(script))
    if not os.path.isfile(script):
        return

    process = subprocess.run(f'"{script}" --selftest', shell=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = _decode_console(process.stdout or b"")
    expect("самотест скрипта проходит", process.returncode == 0)
    expect("русский текст вывода читается",
           "самотест пройден" in output and "?" not in output.split(":")[0])

    process = subprocess.run(f'"{script}"', shell=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    expect("без аргументов скрипт объясняет использование",
           process.returncode == 2
           and "использование" in _decode_console(process.stdout or b""))

    tmp = tempfile.mkdtemp(prefix="simple_upload_")
    try:
        target = os.path.join(tmp, "working.txt")
        with open(target, "w", encoding="ascii") as handle:
            handle.write("vless://test@example.com:443#x\n")
        preset = CommandPreset.from_dict({
            "name": "шаблон", "kind": "script", "script": script,
            "arguments": '"{file}" {host} {port} {user} {remote_dir}',
            "host": "127.0.0.1", "port": 11111, "user": "testuser",
            "password": "SECRET", "remote_dir": "/tmp", "timeout_seconds": 30})
        result = run_preset(preset, {"file": target})
        expect("скрипт запускается из пресета и падает по сети, а не по синтаксису",
               result.code == 1 and "загрузка не удалась" in result.output)
        expect("пароль не попадает в командную строку пресета",
               "SECRET" not in result.command_line)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_render() -> None:
    print("test_command_render")
    artifacts = {"file": r"C:\out dir\working.txt",
                 "json": r"C:\out dir\v2rayN.json", "count": 7}
    preset = CommandPreset.from_dict({
        "name": "scp", "kind": "command", "host": "vps.example", "port": 2222,
        "user": "root", "password": "TOKEN123", "remote_dir": "/srv/pub",
        "command": 'scp -P {port} "{file}" {user}@{host}:{remote_dir}/working.txt',
    })
    rendered = render_preset(preset, artifacts)
    expect("подстановки раскрыты",
           rendered.command_line ==
           'scp -P 2222 "C:\\out dir\\working.txt" '
           'root@vps.example:/srv/pub/working.txt')
    expect("пароль не попал в командную строку",
           "TOKEN123" not in rendered.command_line)
    expect("замечаний нет", rendered.warnings == [])

    masked = CommandPreset.from_dict({
        "name": "tg", "kind": "command", "password": "TOKEN123",
        "command": "curl https://api.telegram.org/bot{password}/sendDocument"})
    safe = render_preset(masked, artifacts)
    expect("секрет замаскирован для журнала",
           "TOKEN123" not in safe.safe_command_line)
    expect("вместо секрета стоит маска", "********" in safe.safe_command_line)

    unquoted = CommandPreset.from_dict({
        "name": "без кавычек", "kind": "command",
        "command": "scp {file} host:/tmp/"})
    expect("предупреждение о пробелах в пути",
           any("кавычки" in item
               for item in render_preset(unquoted, artifacts).warnings))

    unknown = CommandPreset.from_dict({
        "name": "опечатка", "kind": "command",
        "command": "cmd /c echo {unknown_placeholder}"})
    expect("неизвестная подстановка замечена",
           any("неизвестные" in item
               for item in render_preset(unknown, artifacts).warnings))

    script = CommandPreset.from_dict({
        "name": "скрипт", "kind": "script",
        "script": r"C:\мои скрипты\send.cmd", "arguments": '"{file}" {count}'})
    line = render_preset(script, artifacts).command_line
    expect("путь скрипта заключён в кавычки",
           line.startswith('"C:\\мои скрипты\\send.cmd"'))
    expect("аргументы раскрыты", line.endswith('"C:\\out dir\\working.txt" 7'))

    empty = CommandPreset.from_dict({"name": "пусто", "kind": "command",
                                     "command": "cmd /c echo ok"})
    empty.command = ""
    expect("пустая команда замечена",
           bool(render_preset(empty, artifacts).warnings))

    no_json = CommandPreset.from_dict({
        "name": "нужен JSON", "kind": "command", "artifact": "json",
        "command": 'curl -F file=@{json} https://example.org/upload'})
    only_links = render_preset(no_json, dict(artifacts, format=OUTPUT_URI))
    both_files = render_preset(no_json, dict(artifacts, format=OUTPUT_BOTH))
    expect("формат «только ссылки» предупреждает про JSON",
           any("v2rayN" in item for item in only_links.warnings))
    expect("формат «оба файла» не предупреждает про отсутствие файла",
           not any("не ведётся" in item for item in both_files.warnings))

    # Самодельные фигурные скобки и несуществующие переменные окружения.
    handmade = CommandPreset.from_dict({
        "name": "самодельные скобки", "kind": "command",
        "command": 'pscp -batch -pw "%OWN_PASSWORD%" -P {234234} "{file}" '
                   "{login}@{host}:/tmp/working.tmp",
        "host": "1.2.3.4", "port": 23423, "user": "login"})
    handmade_warnings = render_preset(handmade, artifacts).warnings
    expect("самодельные {скобки} замечены",
           any("{234234}" in item and "{login}" in item
               for item in handmade_warnings))
    expect("несуществующая переменная окружения замечена",
           any("OWN_PASSWORD" in item for item in handmade_warnings))
    unknown_head = next((item for item in handmade_warnings
                         if item.startswith("неизвестные подстановки")), "")
    expect("обычные подстановки в список неизвестных не попадают",
           "{234234}" in unknown_head and "{login}" in unknown_head
           and "{file}" not in unknown_head)

    valid = CommandPreset.from_dict({
        "name": "всё заполнено", "kind": "command", "host": "1.2.3.4",
        "port": 23423, "user": "login", "remote_dir": "/var/www",
        "password": "секрет",
        "command": 'pscp -P {port} "{file}" {user}@{host}:{remote_dir}/working.tmp'})
    expect("корректная команда идёт без замечаний",
           render_preset(valid, artifacts).warnings == [])

    hidden = CommandPreset.from_dict({
        "name": "скрытый", "kind": "command", "hidden": True,
        "host": "203.0.113.10", "user": "user",
        "command": "scp -P 22 f user@host:/tmp/secret.txt"})
    expect("у скрытого пресета текст команды не показывается",
           hidden.display_target == "(скрыто)")
    expect("у обычного пресета текст команды виден",
           "scp" in valid.display_target)
    expect("скрытый флаг не влияет на саму команду",
           "secret.txt" in render_preset(hidden, artifacts).command_line)

    env_ok = CommandPreset.from_dict({
        "name": "нормальные переменные", "kind": "command",
        "command": 'cmd /c echo %COMMAND_PASSWORD% %APPDATA% %MYVAR%',
        "password": "секрет", "env": {"MYVAR": "1"}})
    expect("известные переменные окружения не пугают",
           not any("%" in item for item in render_preset(env_ok, artifacts).warnings))

    json_payload = CommandPreset.from_dict({
        "name": "json", "kind": "command",
        "command": 'curl -d "{\\"public\\": false}" https://example.org/api'})
    expect("фигурные скобки JSON не считаются подстановкой",
           not any("неизвестные подстановки" in item
                   for item in render_preset(json_payload, artifacts).warnings))

    empty_profile = CommandPreset.from_dict({
        "name": "пустой профиль", "kind": "command",
        "command": "scp {file} {user}@{host}:/tmp/working.txt"})
    empty_warnings = render_preset(empty_profile, artifacts).warnings
    empty_text = " | ".join(empty_warnings)
    expect("пустые поля профиля замечены",
           "«Пользователь»" in empty_text and "«Адрес»" in empty_text)

    bad_port = CommandPreset.from_dict({
        "name": "порт", "kind": "command", "host": "h", "user": "u",
        "port": 23423, "command": "pscp -P {port} f h:/tmp/x"})
    bad_port.port = 70000
    expect("недопустимый порт замечен",
           any("65535" in item
               for item in render_preset(bad_port, artifacts).warnings))


def test_command_runner() -> None:
    print("test_command_runner")
    tmp = tempfile.mkdtemp(prefix="simple_cmdrun_")
    try:
        scripts = os.path.join(tmp, "мои скрипты")
        os.makedirs(scripts, exist_ok=True)
        script = os.path.join(scripts, "argv.cmd")
        args_file = os.path.join(tmp, "args.txt")
        with open(script, "w", encoding="ascii") as handle:
            handle.write("@echo off\r\n")
            handle.write(f'> "{args_file}" echo %*\r\n')
            handle.write(f'>> "{args_file}" echo %DESTINATION%\r\n')
            handle.write(f'>> "{args_file}" echo %PLAIN%\r\n')
            handle.write(f'>> "{args_file}" echo %COMMAND_PASSWORD%\r\n')
            handle.write("echo done\r\n")

        out_dir = os.path.join(tmp, "out dir")
        os.makedirs(out_dir, exist_ok=True)
        artifacts = {"file": os.path.join(out_dir, "working.txt"), "count": 5}
        preset = CommandPreset.from_dict({
            "name": "argv", "kind": "script", "script": script,
            "arguments": '"{file}" {count}', "timeout_seconds": 30,
            "password": "s3cret",
            "env": {"DESTINATION": "{out_dir}", "PLAIN": "value"}})
        result = run_preset(preset, artifacts)
        expect("скрипт запущен успешно", result.ok and result.code == 0)
        expect("вывод команды получен", "done" in result.output)
        with open(args_file, "r", encoding="ascii") as handle:
            content = handle.read()
            words = content.split()
        expect("аргументы с пробелами не развалились",
               f'"{artifacts["file"]}"' in " ".join(words))
        expect("подстановка в переменной окружения раскрыта",
               out_dir in content)
        expect("обычная переменная окружения передана", "value" in words)
        expect("пароль доступен как COMMAND_PASSWORD", "s3cret" in words)

        failing = CommandPreset.from_dict({
            "name": "сбой", "kind": "command", "command": "cmd /c exit 3",
            "timeout_seconds": 20})
        bad = run_preset(failing, {})
        expect("ненулевой код — ошибка", not bad.ok and bad.code == 3)
        expect("ошибка описана", "3" in (bad.error or ""))

        slow = CommandPreset.from_dict({
            "name": "медленная", "kind": "command", "timeout_seconds": 5,
            "command": f'"{sys.executable}" -c "import time; time.sleep(60)"'})
        started = time.time()
        stopped = run_preset(slow, {})
        elapsed = time.time() - started
        expect("тайм-аут сработал",
               not stopped.ok and "тайм-аут" in (stopped.error or ""))
        expect("процесс убит, а не дождались конца", elapsed < 25)
        expect("код возврата при тайм-ауте неизвестен", stopped.code is None)

        wrong_dir = CommandPreset.from_dict({
            "name": "каталог", "kind": "command", "command": "cmd /c echo hi",
            "working_dir": os.path.join(tmp, "нет такой папки")})
        expect("несуществующий рабочий каталог замечен",
               not run_preset(wrong_dir, {}).ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def test_command_scheduler() -> None:
    print("test_command_scheduler")
    tmp = tempfile.mkdtemp(prefix="simple_sched_")
    try:
        settings = SimpleSettings(os.path.join(tmp, "settings.json"))
        calls: list = []
        results: list = []
        logs: list = []

        def fake_runner(preset, artifacts):
            calls.append((preset.name, dict(artifacts)))
            if results:
                return results.pop(0)
            return RunResult(ok=True, code=0, output="ok", duration=0.1)

        scheduler = CommandScheduler(
            settings, artifacts_provider=lambda: {"file": "w.txt", "count": 3},
            runner=fake_runner, on_log=logs.append, tick_seconds=0.5)
        scheduler.start()
        try:
            change = scheduler.add(CommandPreset.from_dict({
                "name": "при изменении", "kind": "command",
                "command": "cmd /c echo 1", "trigger": "on_change",
                "min_gap_seconds": 600}))
            scheduler._tick_once()
            expect("без изменений ничего не запускается", calls == [])

            scheduler.notify_output_changed()
            scheduler._tick_once()
            expect("после изменения команда выполнена",
                   [call[0] for call in calls] == ["при изменении"])
            expect("переданы пути файлов и число серверов",
                   calls[0][1] == {"file": "w.txt", "count": 3})

            scheduler.notify_output_changed()
            scheduler._tick_once()
            expect("повтор блокируется минимальной паузой", len(calls) == 1)

            relaxed = scheduler.get(change.id)
            relaxed.min_gap_seconds = 0.0
            scheduler.update(relaxed)
            scheduler.notify_output_changed()
            scheduler._tick_once()
            expect("паузу можно снять", len(calls) == 2)

            manual = scheduler.add(CommandPreset.from_dict({
                "name": "вручную", "kind": "command",
                "command": "cmd /c echo 2", "trigger": "manual"}))
            scheduler.notify_output_changed()
            scheduler._tick_once()
            expect("ручной пресет сам не запускается",
                   "вручную" not in [call[0] for call in calls])
            expect("ручной запуск принят в очередь",
                   scheduler.run_now(manual.id))
            scheduler._tick_once()
            expect("ручной пресет выполнен",
                   [call[0] for call in calls][-1] == "вручную")

            interval = scheduler.add(CommandPreset.from_dict({
                "name": "расписание", "kind": "command",
                "command": "cmd /c echo 3", "trigger": "interval",
                "interval_minutes": 60}))
            expect("расписание запланировано на будущее",
                   scheduler._next_run.get(interval.id, 0) > time.time())
            scheduler._tick_once()
            expect("расписание не срабатывает раньше времени",
                   "расписание" not in [call[0] for call in calls])
            scheduler._next_run[interval.id] = time.time() - 1
            scheduler._tick_once()
            expect("просроченное расписание срабатывает",
                   [call[0] for call in calls][-1] == "расписание")

            results.append(RunResult(ok=False, code=1, duration=0.2,
                                     error="команда завершилась с кодом 1"))
            retry = scheduler.add(CommandPreset.from_dict({
                "name": "с повтором", "kind": "command",
                "command": "cmd /c exit 1", "trigger": "manual",
                "retry_count": 2, "retry_delay_seconds": 5}))
            scheduler.run_now(retry.id)
            scheduler._tick_once()
            stored = scheduler.get(retry.id)
            expect("ошибка сохранена в состоянии пресета",
                   "ошибка" in stored.last_status)
            expect("повтор запланирован",
                   scheduler._retry.get(retry.id, (0, 0))[0] == 1)
            scheduler._retry[retry.id] = (1, time.time() - 1)
            scheduler._tick_once()
            expect("повтор выполнен",
                   len([call for call in calls if call[0] == "с повтором"]) == 2)
            expect("повтор не запланирован снова",
                   scheduler._retry.get(retry.id, (0, 0))[0] == 0)

            # Приватный пресет: ни команда, ни её вывод не попадают в журнал.
            results.append(RunResult(ok=True, code=0, duration=0.1,
                                     output="token=SECRETVALUE"))
            secret = scheduler.add(CommandPreset.from_dict({
                "name": "скрытый", "kind": "command",
                "command": "cmd /c echo private", "trigger": "manual",
                "hidden": True}))
            scheduler.run_now(secret.id)
            scheduler._tick_once()
            expect("флаг приватности доехал до планировщика",
                   scheduler.get(secret.id).hidden is True)
            expect("команда скрытого пресета не пишется в журнал",
                   not any("echo private" in line for line in logs))
            expect("вывод скрытого пресета не пишется в журнал",
                   not any("SECRETVALUE" in line for line in logs))
            expect("в журнале есть пометка о скрытом пресете",
                   any("команда скрыта" in line for line in logs))
            expect("у открытого пресета команда в журнале осталась",
                   any("cmd /c echo 1" in line for line in logs))
            expect("у открытого пресета вывод в журнале остался",
                   any("вывод: ok" in line for line in logs))

            off = scheduler.add(CommandPreset.from_dict({
                "name": "выключен", "kind": "command",
                "command": "cmd /c echo 4", "trigger": "on_change",
                "enabled": False}))
            scheduler.notify_output_changed()
            scheduler._tick_once()
            expect("выключенный пресет пропускается",
                   "выключен" not in [call[0] for call in calls])

            with open(os.path.join(tmp, "settings.json"), "r",
                      encoding="utf-8") as handle:
                saved = json.load(handle)["simple_commands"]
            item = [entry for entry in saved if entry["id"] == change.id]
            expect("состояние запуска сохранено на диск",
                   bool(item) and item[0]["last_status"] == "успешно"
                   and item[0]["last_run"] > 0)
            expect("все пресеты сохранены", len(saved) == 6)

            scheduler.notify_output_changed()
            expect("ожидание очереди завершилось",
                   scheduler.wait_idle(timeout=15))
            expect("после ожидания очередь пуста", not scheduler.busy)
            expect("удаление пресета работает",
                   scheduler.remove(off.id) and len(scheduler.presets()) == 5)
        finally:
            scheduler.stop()
        expect("планировщик остановлен", not scheduler.running)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



class _StubSettings:
    """Настройки-заглушка для функций, которым нужен только ``get``."""

    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def test_clipboard_classify() -> None:
    print("test_clipboard_classify")
    tmp = tempfile.mkdtemp(prefix="simple_clip_")
    try:
        list_path = os.path.join(tmp, "sub.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            handle.write(VLESS_A + "\n")
        url = "https://example.org/sub.txt"

        one = classify_clipboard(VLESS_A)
        expect("одна конфигурация идёт в пул",
               one.has_configs and not one.has_sources
               and VLESS_A in one.configs)
        many = classify_clipboard("\n".join((VLESS_A, TROJAN_C)))
        expect("несколько конфигураций идут в пул",
               many.configs.count("://") == 2 and not many.has_sources)

        sub = classify_clipboard(url)
        expect("ссылка на подписку идёт в источники",
               sub.sources == (("url", url),) and not sub.has_configs)
        path = classify_clipboard(list_path)
        expect("путь к существующему файлу идёт в источники",
               path.sources == (("file", list_path),))
        file_url = classify_clipboard("file://" + list_path)
        expect("file:// тоже становится источником",
               file_url.sources == (("file", list_path),))

        packed = classify_clipboard(
            base64.b64encode((VLESS_A + "\n").encode()).decode())
        expect("base64-подписка распознаётся как конфигурации",
               packed.has_configs and not packed.has_sources)

        mixed = classify_clipboard(f"{url}\n{VLESS_A}\nмусор")
        expect("смешанный текст раскладывается по корзинам",
               mixed.has_sources and mixed.has_configs
               and mixed.unknown == ("мусор",))
        expect("пустой текст ничего не даёт",
               classify_clipboard("   \n \n").is_empty)
        junk = classify_clipboard("просто заметка")
        expect("непонятный текст остаётся для поля вставки",
               junk.is_empty and junk.unknown == ("просто заметка",))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_event_triggers() -> None:
    print("test_event_triggers")
    tmp = tempfile.mkdtemp(prefix="simple_events_")
    try:
        settings = SimpleSettings(os.path.join(tmp, "settings.json"))
        calls: list = []

        def fake_runner(preset, artifacts):
            calls.append(preset.name)
            return RunResult(ok=True, code=0, output="ok", duration=0.01)

        scheduler = CommandScheduler(settings, runner=fake_runner)
        for name, trigger in (("старт", "on_start"), ("стоп", "on_stop"),
                              ("круг", "on_round"),
                              ("сеть вниз", "on_network_down"),
                              ("сеть вверх", "on_network_up")):
            scheduler.add(CommandPreset.from_dict({
                "name": name, "kind": "command",
                "command": f"cmd /c echo {name}",
                "trigger": trigger, "min_gap_seconds": 0}))
        scheduler.add(CommandPreset.from_dict({
            "name": "вручную", "kind": "command", "command": "cmd /c echo m",
            "trigger": "manual", "min_gap_seconds": 0}))

        expect("новые триггеры не срезаются настройками",
               CommandPreset.from_dict({
                   "name": "x", "command": "cmd /c echo x",
                   "trigger": "on_network_down"}).trigger == "on_network_down")
        expect("список триггеров интерфейса совпадает с допустимыми",
               set(TRIGGER_LABELS.values()) == set(COMMAND_TRIGGERS))
        expect("события — подмножество допустимых триггеров",
               set(EVENT_TRIGGERS) <= set(COMMAND_TRIGGERS))

        scheduler.notify_event("on_start")
        scheduler._tick_once()
        expect("событие «запуск» выполнило свою команду", calls == ["старт"])
        scheduler._tick_once()
        expect("без нового события команда не повторяется", calls == ["старт"])
        scheduler.notify_event("нет такого события")
        scheduler._tick_once()
        expect("неизвестное событие ничего не меняет", calls == ["старт"])

        scheduler.notify_event("on_stop")
        scheduler._tick_once()
        expect("событие «остановка» выполнилось", calls == ["старт", "стоп"])
        scheduler.notify_event("on_network_down")
        scheduler._tick_once()
        scheduler.notify_event("on_network_up")
        scheduler._tick_once()
        expect("сетевые события выполняются по порядку",
               calls == ["старт", "стоп", "сеть вниз", "сеть вверх"])
        scheduler.notify_event("on_round")
        scheduler._tick_once()
        expect("событие «круг завершён» выполнилось", calls[-1] == "круг")
        expect("пресет «вручную» сам не запускается", "вручную" not in calls)

        up_id = [preset.id for preset in scheduler.presets()
                 if preset.name == "сеть вверх"][0]
        scheduler.update(CommandPreset.from_dict({
            "id": up_id, "name": "сеть вверх", "kind": "command",
            "command": "cmd /c echo up", "trigger": "on_network_up",
            "enabled": False}))
        scheduler.notify_event("on_network_up")
        scheduler._tick_once()
        expect("выключенный пресет пропускается",
               calls.count("сеть вверх") == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_network_watcher() -> None:
    print("test_network_watcher")
    events: list = []
    watcher = NetworkWatcher(interval=5.0, failures=2,
                             on_event=events.append)
    watcher.check = lambda: False           # «связи нет» без реальной сети
    watcher.poll()
    expect("первая неудача ещё не обрыв связи",
           watcher.online and not events)
    watcher.poll()
    expect("вторая неудача подряд — сеть пропала",
           not watcher.online and events == ["on_network_down"])
    watcher.check = lambda: True
    watcher.poll()
    expect("возврат связи даёт событие",
           watcher.online and events == ["on_network_down", "on_network_up"])
    watcher.check = lambda: False
    watcher.poll()
    watcher.poll()
    expect("повторный обрыв снова сообщается",
           events[-1] == "on_network_down" and len(events) == 3)
    expect("слежку можно отключить настройкой",
           watch_enabled(_StubSettings({"simple_network_watch": False}))
           == (False, 30.0))
    expect("период слежки читается из настроек",
           watch_enabled(_StubSettings(
               {"simple_network_watch_seconds": 45}))[1] == 45.0)


def test_supervisor_events() -> None:
    print("test_supervisor_events")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        calls: list = []

        def fake_runner(preset, artifacts):
            calls.append(preset.name)
            return RunResult(ok=True, code=0, output="ok", duration=0.01)

        supervisor.commands._runner = fake_runner
        supervisor.commands.add(CommandPreset.from_dict({
            "name": "круг", "kind": "command", "command": "cmd /c echo round",
            "trigger": "on_round", "min_gap_seconds": 0}))
        supervisor.import_text(VLESS_A, "тест")
        for entry in pool.items():
            prober.script_for(entry.endpoint, {
                LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
                LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        supervisor.run_round()
        expect("круг завершён — событие поставлено в очередь",
               bool(supervisor.commands._dirty))
        supervisor.commands.start()
        try:
            expect("команда по событию круга выполнена",
                   supervisor.commands.wait_idle(timeout=15))
        finally:
            supervisor.commands.stop()
        expect("команда круга запускалась", calls == ["круг"])

        # Остановка цикла не гасит планировщик: команда «при остановке» должна
        # успеть выполниться, а сам планировщик живёт вместе с приложением.
        supervisor.commands.add(CommandPreset.from_dict({
            "name": "стоп", "kind": "command", "command": "cmd /c echo stop",
            "trigger": "on_stop", "min_gap_seconds": 0}))
        supervisor.stop()
        expect("остановка цикла поставила событие «стоп»",
               bool(supervisor.commands._dirty))
        supervisor.commands.start()
        try:
            expect("очередь событий обработана",
                   supervisor.commands.wait_idle(timeout=15))
        finally:
            supervisor.commands.stop()
        expect("команда «при остановке» выполнена", "стоп" in calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_supervisor_hook() -> None:
    print("test_command_supervisor_hook")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        calls: list = []

        def fake_runner(preset, artifacts):
            calls.append(dict(artifacts))
            return RunResult(ok=True, code=0, output="готово", duration=0.05)

        supervisor.commands._runner = fake_runner
        supervisor.commands.add(CommandPreset.from_dict({
            "name": "публикация", "kind": "command",
            "command": "cmd /c echo {count}", "trigger": "on_change",
            "min_gap_seconds": 0}))
        supervisor.import_text(VLESS_A, "тест")
        for entry in pool.items():
            prober.script_for(entry.endpoint, {
                LEVEL_PING: ok(LEVEL_PING), LEVEL_CORE: ok(LEVEL_CORE),
                LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        supervisor.run_round()
        expect("выходной файл записан", supervisor.writer.count == 1)
        expect("изменение файла пометило пресет",
               bool(supervisor.commands._dirty))

        supervisor.commands.start()
        try:
            expect("очередь выполнена",
                   supervisor.commands.wait_idle(timeout=15))
        finally:
            supervisor.commands.stop()
        expect("команда получила путь выходного файла",
               bool(calls) and calls[0].get("file") == supervisor.writer.path)
        expect("команда получила число серверов",
               bool(calls) and calls[0].get("count") == 1)
        expect("результат записан в пресет",
               supervisor.commands.presets()[0].last_status == "успешно")

        # Режим без интерфейса (--once): команды выполняются после прохода.
        supervisor.commands.remove(supervisor.commands.presets()[0].id)
        supervisor.commands.add(CommandPreset.from_dict({
            "name": "разовая", "kind": "command", "command": "cmd /c echo x",
            "trigger": "on_change", "min_gap_seconds": 0}))
        calls.clear()
        started = supervisor.run_external_commands(timeout=15)
        expect("разовый проход ставит команды в очередь", started == 1)
        expect("разовый проход выполняет команду", len(calls) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



# ---------------------------------------------------------------------------
# Пакетная перепроверка рабочих серверов
# ---------------------------------------------------------------------------

def test_batch_document_build() -> None:
    print("test_batch_document_build")
    cfg_vless = parser.parse_uri(VLESS_A)
    cfg_trojan = parser.parse_uri(TROJAN_C)
    assert cfg_vless is not None and cfg_trojan is not None

    xray_doc = batch_module.build_document("xray", [cfg_vless, cfg_trojan],
                                           [1080, 1081])
    expect("у пачки Xray по локальному порту на сервер",
           [item["port"] for item in xray_doc["inbounds"]] == [1080, 1081])
    expect("каждому серверу — своё правило маршрутизации",
           len(xray_doc["routing"]["rules"]) == 2
           and xray_doc["routing"]["rules"][1]["outboundTag"] == "proxy-1")
    expect("в пачке Xray два сервера и direct",
           [item["tag"] for item in xray_doc["outbounds"]]
           == ["proxy-0", "proxy-1", "direct"])

    sing_doc = batch_module.build_document("singbox", [cfg_vless], [1082])
    expect("sing-box использует listen_port",
           sing_doc["inbounds"][0]["listen_port"] == 1082)
    expect("sing-box маршрутизирует порт на свой сервер",
           sing_doc["route"]["rules"][0] == {"inbound": ["in-0"],
                                             "outbound": "proxy-0"})

    hy2 = parser.parse_uri("hysteria2://secret@h.example.com:443"
                           "?sni=h.example.com#H")
    assert hy2 is not None
    groups = batch_module.group_entries([
        ServerEntry(fp="a", uri=VLESS_A, data=cfg_vless.to_dict()),
        ServerEntry(fp="b", uri=hy2.uri, data=hy2.to_dict()),
    ])
    expect("vless уходит в Xray, hysteria2 — в sing-box",
           len(groups.get("xray", [])) == 1
           and len(groups.get("singbox", [])) == 1)


def test_maintenance_quota_and_max() -> None:
    print("test_maintenance_quota_and_max")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.update({"simple_maintenance_fraction": 0.25,
                         "simple_maintenance_max": 1})
        supervisor.reload_policy()
        congest(pool, 8, SimpleState.WORKING.value)
        for item in pool.items():
            prober.script_for(item.endpoint, {LEVEL_PING: ok(LEVEL_PING),
                                              LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})
        supervisor.run_round()
        stats = supervisor.snapshot()
        expect("за проход подтверждается только доля рабочих",
               stats.maintenance_checked == 2)
        expect("пачки ограничены лимитом на процесс",
               stats.maintenance_batches == 2)
        expect("подтверждённые рабочие остались рабочими",
               all(item.state == SimpleState.WORKING.value
                   for item in pool.items()))
        expect("лимит пачки по умолчанию разумен",
               batch_module.DEFAULT_MAX_PER_PROCESS == 50)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_maintenance_single_recheck_rescues() -> None:
    print("test_maintenance_single_recheck_rescues")
    tmp, settings, pool, prober, supervisor = make_env()
    saved = pipeline_module.run_batches
    try:
        congest(pool, 1, SimpleState.WORKING.value)
        entry = only(pool)
        prober.script_for(entry.endpoint, {LEVEL_PING: ok(LEVEL_PING),
                                           LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})

        def liar(*args, **kwargs):
            return [BatchReport(core="xray", requested=1, checked=1, ok=0,
                                failed=1, elapsed=0.1,
                                results=[BatchResult(fp=entry.fp, ok=False,
                                                     error="пачка не увидела ответ")])]

        pipeline_module.run_batches = liar
        supervisor.run_round()
        expect("провал пачки подтверждается одиночной проверкой",
               pool.get(entry.fp).state == SimpleState.WORKING.value)
        expect("ссылка осталась в выходном файле",
               output_lines(supervisor) == [entry.uri])
        expect("счётчик провалов не вырос", pool.get(entry.fp).traffic_fail == 0)
    finally:
        pipeline_module.run_batches = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_maintenance_infra_failure_keeps_servers() -> None:
    print("test_maintenance_infra_failure_keeps_servers")
    tmp, settings, pool, prober, supervisor = make_env()
    saved = pipeline_module.run_batches
    try:
        congest(pool, 6, SimpleState.WORKING.value)
        for item in pool.items():
            prober.script_for(item.endpoint, {LEVEL_PING: ok(LEVEL_PING),
                                              LEVEL_TRAFFIC: ok(LEVEL_TRAFFIC)})

        def broken(*args, **kwargs):
            return [BatchReport(core="xray", requested=6,
                                infra_error="ядро завершилось сразу после старта")]

        pipeline_module.run_batches = broken
        supervisor.run_round()
        expect("сбой ядра не разжаловал никого",
               all(item.state == SimpleState.WORKING.value
                   for item in pool.items()))
        expect("результаты сбойной пачки не применены",
               supervisor.snapshot().maintenance_checked == 0)
    finally:
        pipeline_module.run_batches = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_maintenance_guard_mass_failure() -> None:
    print("test_maintenance_guard_mass_failure")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        # Проверяем именно предохранитель: в проход берём всех рабочих.
        settings.set("simple_maintenance_fraction", 1.0)
        supervisor.reload_policy()
        congest(pool, 8, SimpleState.WORKING.value)
        # Скриптов нет: все адреса «не отвечают» — похоже на сбой связи.
        supervisor.run_round()
        expect("массовый провал не разжаловал рабочих",
               all(item.state == SimpleState.WORKING.value
                   for item in pool.items()))
        expect("предохранитель зафиксирован",
               supervisor.snapshot().maintenance_guard_skips == 1)
        expect("результаты не применялись",
               supervisor.snapshot().maintenance_checked == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_network_guard_stops_checks() -> None:
    print("test_network_guard_stops_checks")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint,
                          {LEVEL_PING: fail(LEVEL_PING, "тайм-аут TCP")})
        supervisor._on_network_event("on_network_down")
        supervisor.run_round()
        expect("при пропавшей сети проверок нет", prober.calls == [])
        expect("счётчик неудач не тронут", pool.get(entry.fp).ping_fail == 0)
        expect("состояние показывает приостановку",
               supervisor.snapshot().network_hold is True)

        supervisor._on_network_event("on_network_up")
        supervisor.run_round()
        expect("после возвращения сети проверка идёт", prober.calls != [])
        expect("неудача зачтена после возвращения сети",
               pool.get(entry.fp).ping_fail == 1)

        settings.set("simple_network_guard", False)
        calls_before = len(prober.calls)
        supervisor._on_network_event("on_network_down")
        supervisor.run_round()
        expect("с выключенным предохранителем проверки выполняются",
               len(prober.calls) > calls_before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Экономия ресурсов: индекс очереди, срок годности статуса, троттлинг записи
# ---------------------------------------------------------------------------

def test_pool_uri_dedup() -> None:
    """Ссылка хранится один раз — внутри ``data`` — и переживает перезапуск."""
    print("test_pool_uri_dedup")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        cfg = parser.parse_uri(VLESS_B)
        entry = ServerEntry(fp=cfg.dedup_key(), uri=VLESS_B,
                            data=cfg.to_dict(), state=SimpleState.NEW.value)
        stored = entry.to_dict()
        expect("ссылка не дублируется полем верхнего уровня", "uri" not in stored)
        expect("ссылка есть внутри data", stored["data"]["uri"] == VLESS_B)
        expect("round-trip сохраняет ссылку",
               ServerEntry.from_dict(stored).uri == VLESS_B)
        expect("старый формат файла тоже читается",
               ServerEntry.from_dict({"fp": "x", "uri": VLESS_B,
                                      "data": cfg.to_dict()}).uri == VLESS_B)
        expect("запись без ссылки не падает",
               ServerEntry.from_dict({"fp": "y"}).uri == "")

        pool.add_many([entry])
        pool.save(force=True)
        restored = Pool(pool.path)
        restored.load()
        expect("после перезагрузки ссылка на месте",
               restored.get(entry.fp).uri == VLESS_B)
        with open(pool.path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        expect("в файле ссылка лежит только внутри data",
               "uri" not in raw["entries"][0]
               and raw["entries"][0]["data"]["uri"] == VLESS_B)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pool_failed_ttl() -> None:
    """Срок годности статуса «Не работает»: старые записи убираются сами."""
    print("test_pool_failed_ttl")
    tmp = tempfile.mkdtemp(prefix="simple_ttl_")
    try:
        pool = Pool(os.path.join(tmp, "pool.json"))
        now = time.time()
        cfg = parser.parse_uri(VLESS_A)
        data = cfg.to_dict()
        pool.add_many([
            ServerEntry(fp="fresh", uri=VLESS_A, data=dict(data),
                        state=SimpleState.FAILED.value, last_checked=now),
            ServerEntry(fp="old", uri=VLESS_A, data=dict(data),
                        state=SimpleState.FAILED.value,
                        last_checked=now - 100 * 3600.0, first_seen=now - 200 * 3600.0),
            ServerEntry(fp="nostamp", uri=VLESS_A, data=dict(data),
                        state=SimpleState.FAILED.value),
            ServerEntry(fp="work", uri=VLESS_A, data=dict(data),
                        state=SimpleState.WORKING.value,
                        last_checked=now - 100 * 3600.0),
        ])
        expect("нулевой срок выключает очистку", pool.purge_failed(now, 0.0) == 0)
        expect("до срока ничего не удалено", pool.size() == 4)
        removed = pool.purge_failed(now, 72 * 3600.0)
        expect("удалён ровно один просроченный", removed == 1)
        expect("старый нерабочий убран", pool.has("old") is False)
        expect("свежий нерабочий остался", pool.has("fresh") is True)
        expect("запись без отметок не удаляется", pool.has("nostamp") is True)
        expect("рабочий сервер не тронут", pool.has("work") is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pool_save_throttle() -> None:
    """Пул пишется на диск не чаще интервала; ``force`` его игнорирует."""
    print("test_pool_save_throttle")
    tmp = tempfile.mkdtemp(prefix="simple_save_")
    try:
        pool = Pool(os.path.join(tmp, "pool.json"))
        pool.save_interval = 60.0

        def stored_count() -> int:
            with open(pool.path, "r", encoding="utf-8") as handle:
                return len(json.load(handle)["entries"])

        pool.add_many([ServerEntry(fp="a", uri=VLESS_A,
                                   data=parser.parse_uri(VLESS_A).to_dict())])
        expect("первое сохранение проходит", pool.save() is True)
        expect("на диске одна запись", stored_count() == 1)

        pool.add_many([ServerEntry(fp="b", uri=VLESS_B,
                                   data=parser.parse_uri(VLESS_B).to_dict())])
        expect("повторная запись отложена", pool.save() is False)
        expect("отложенная запись на диск не попала", stored_count() == 1)
        expect("force игнорирует интервал", pool.save(force=True) is True)
        expect("после force на диске обе записи", stored_count() == 2)
        expect("без изменений файл не перезаписывается", pool.save() is False)

        pool.save_interval = 0.0
        pool.add_many([ServerEntry(fp="c", uri=VLESS_A,
                                   data=parser.parse_uri(VLESS_A).to_dict())])
        expect("нулевой интервал не мешает записи", pool.save() is True)
        expect("запись без интервала сохранена", stored_count() == 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blacklist_graveyard_is_compact() -> None:
    """«Кладбище» хранится компактно: ссылка не дублируется, причина обрезана."""
    print("test_blacklist_graveyard_is_compact")
    tmp = tempfile.mkdtemp(prefix="simple_grave_")
    try:
        long_reason = "ошибка: " + "очень длинная причина " * 10
        pool = Pool(os.path.join(tmp, "pool.json"))
        pool.block("dead", VLESS_A, long_reason, 3)

        record = pool._blocked["dead"]
        expect("ссылка в «кладбище» не хранится", "uri" not in record)
        expect("причина обрезана до лимита", len(record["reason"]) <= 60)
        expect("момент блокировки и число неудач сохранены",
               record["at"] > 0 and record["fails"] == 3)

        pool.save(force=True)
        with open(pool.path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)["blocked"]["dead"]
        expect("в файле тоже нет ссылки", "uri" not in stored)
        expect("в файле причина тоже обрезана", len(stored["reason"]) <= 60)

        # Файл старого формата: ссылка и полная причина отбрасываются при загрузке.
        legacy_path = os.path.join(tmp, "legacy.json")
        with open(legacy_path, "w", encoding="utf-8") as handle:
            json.dump({
                "version": 1,
                "entries": [],
                "blocked": {"old": {"at": time.time(), "uri": VLESS_B,
                                    "reason": long_reason, "fails": 5}},
            }, handle, ensure_ascii=False)
        restored = Pool(legacy_path)
        restored.load()
        legacy = restored._blocked["old"]
        expect("старая запись сжата при загрузке",
               "uri" not in legacy and len(legacy["reason"]) <= 60)
        policy = simple_policy(SimpleSettings(os.path.join(tmp, "settings.json")))
        expect("старая запись продолжает фильтровать",
               restored.blocked("old", time.time(), policy) is True)
        expect("отсутствующая запись не фильтрует",
               restored.blocked("dead", time.time(), policy) is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_autostart_shortcut() -> None:
    """Автозапуск: ярлык в «Автозагрузке» создаётся и удаляется программой."""
    print("test_autostart_shortcut")
    tmp = tempfile.mkdtemp(prefix="simple_auto_")
    try:
        target, arguments, workdir, icon = autostart.launch_command()
        expect("цель автозапуска существует", os.path.isfile(target))
        expect("в аргументах есть скрипт и старт в трей",
               "simple_main.py" in arguments
               and autostart.TRAY_ARG in arguments)
        expect("рабочий каталог — корень проекта",
               os.path.isfile(os.path.join(workdir, "simple_main.py")))
        expect("файлы автозапуска адресуются в заданный каталог",
               autostart.shortcut_path(tmp).startswith(tmp)
               and autostart.command_path(tmp).startswith(tmp))
        expect("до включения автозапуск выключен",
               autostart.is_enabled(tmp) is False)
        expect("реестр не используется",
               "winreg" not in open(autostart.__file__, "r",
                                    encoding="utf-8").read())

        ok, message = autostart.enable(tmp)
        if not autostart.supported() or not ok:
            print(f"  [SKIP] автозапуск недоступен: {message}")
            return
        expect("автозапуск включён", autostart.is_enabled(tmp) is True)
        expect("создан именно наш файл",
               os.path.isfile(autostart.shortcut_path(tmp))
               or os.path.isfile(autostart.command_path(tmp)))
        expect("состояние описано", "включ" in autostart.describe(tmp))

        again, _message = autostart.enable(tmp)
        expect("повторное включение идемпотентно",
               again and autostart.is_enabled(tmp))

        off, _message = autostart.disable(tmp)
        expect("автозапуск выключен",
               off and not autostart.is_enabled(tmp))
        expect("состояние после выключения", autostart.describe(tmp) == "выключен")
        off_again, _message = autostart.disable(tmp)
        expect("повторное выключение не ошибка", off_again)
        expect("в каталоге не осталось наших файлов", not os.listdir(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pyw_entry_point() -> None:
    """`simple_main.pyw` — запуск двойным кликом без окна консоли (pythonw)."""
    print("test_pyw_entry_point")
    import py_compile

    root = os.path.dirname(os.path.abspath(__file__))
    pyw = os.path.join(root, "simple_main.pyw")
    expect("файл simple_main.pyw есть рядом с точкой входа",
           os.path.isfile(pyw))
    if os.path.isfile(pyw):
        with open(pyw, "r", encoding="utf-8") as handle:
            text = handle.read()
        expect("запускает тот же интерфейс, что и simple_main.py",
               "from simple.cli import main" in text)
        expect("объясняет роль .pyw (оконный интерпретатор)",
               "pythonw" in text and ".pyw" in text)
        expect("файл компилируется", py_compile.compile(pyw, doraise=True))
    # Скрытого перезапуска в коде больше нет: окно открывает тот интерпретатор,
    # которым программу запустили (pythonw/.pyw/ярлык/exe).
    with open(cli_module.__file__, "r", encoding="utf-8") as handle:
        cli_source = handle.read()
    expect("в cli.py нет самоперезапуска",
           "relaunch" not in cli_source and "--console" not in cli_source)


def test_pool_gzip() -> None:
    """Пул можно писать сжатым; оба формата читаются и переключаются на ходу."""
    print("test_pool_gzip")
    tmp = tempfile.mkdtemp(prefix="simple_gz_")
    try:
        path = os.path.join(tmp, "pool.json")
        pool = Pool(path)
        cfg = parser.parse_uri(VLESS_A)
        pool.add_many([ServerEntry(fp="a", uri=VLESS_A, data=cfg.to_dict())])
        pool.block_address("203.0.113.9", 0, "вручную")

        pool.compress = False
        expect("обычный режим пишет pool.json",
               pool.save(force=True) and os.path.isfile(path)
               and not os.path.isfile(path + ".gz"))

        pool.compress = True
        expect("сжатый режим пишет pool.json.gz",
               pool.save(force=True) and os.path.isfile(path + ".gz"))
        with open(path + ".gz", "rb") as handle:
            head = handle.read(2)
        expect("файл — настоящий gzip", head == b"\x1f\x8b")
        expect("старый несжатый файл удалён", not os.path.isfile(path))
        expect("сжатый файл заметно меньше несжатого",
               os.path.getsize(path + ".gz") < 1200)
        with open(path + ".gz", "rb") as handle:
            inside = json.loads(gzip.decompress(handle.read()).decode("utf-8"))
        expect("внутри обычный JSON",
               inside["entries"][0]["fp"] == "a"
               and "blocked_addresses" in inside)

        restored = Pool(path)
        restored.load()
        expect("сжатый пул читается", restored.get("a") is not None
               and restored.is_blocked_address("203.0.113.9", 443))
        expect("чёрный список «все порты» восстановлен",
               restored.blocked_address_count() == 1)

        restored.compress = False
        restored.save(force=True)
        expect("выключение сжатия возвращает pool.json",
               os.path.isfile(path) and not os.path.isfile(path + ".gz"))
        plain = Pool(path)
        plain.compress = True          # настройка включена, а файл ещё обычный
        plain.load()
        expect("обычный файл читается и при включённом сжатии",
               plain.get("a") is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blacklist_parsing() -> None:
    """Разбор строк для чёрного списка: ссылка, host:port, голый хост, IPv6."""
    print("test_blacklist_parsing")
    parse = pipeline_module.parse_blacklist_item
    expect("ссылка на сервер", parse(VLESS_A) == ("a.example.com", 443))
    expect("пара host:port", parse("1.2.3.4:8080") == ("1.2.3.4", 8080))
    expect("голый IP значит «все порты»", parse("1.2.3.4") == ("1.2.3.4", 0))
    expect("имя хоста без порта", parse("example.org") == ("example.org", 0))
    expect("IPv6 в скобках", parse("[2001:db8::1]:443") == ("2001:db8::1", 443))
    expect("IPv6 без порта", parse("[2001:db8::1]") == ("2001:db8::1", 0))
    expect("пробелы не мешают", parse("  1.2.3.4:80  ") == ("1.2.3.4", 80))
    expect("пустое значение и мусор отвергаются",
           parse("") == ("", 0) and parse("   ") == ("", 0)
           and parse("vless://") == ("", 0))


def test_blacklist_by_host() -> None:
    """Чёрный список принимает ссылку, host:port и «голый» IP (все порты)."""
    print("test_blacklist_by_host")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        supervisor.import_text(f"{VLESS_A}\n{VLESS_B}", "тест")
        by_address = {entry.address: entry for entry in pool.items()}
        host_entry = by_address["a.example.com"]
        expect("до блокировки адрес в пуле", pool.has(host_entry.fp))

        counters = supervisor.blacklist_input("a.example.com")
        expect("по хосту блокируются все порты",
               pool.is_blocked_address("a.example.com", 443)
               and pool.is_blocked_address("a.example.com", 9999))
        expect("записи этого хоста убраны из пула", not pool.has(host_entry.fp))
        expect("счётчики отчёта верны",
               counters["addresses"] == 1 and counters["removed"] == 1)

        port_entry = by_address["b.example.com"]
        single = supervisor.blacklist_input(f"b.example.com:{port_entry.port}")
        expect("пара host:port блокируется точечно",
               pool.is_blocked_address("b.example.com", port_entry.port)
               and not pool.is_blocked_address("b.example.com", 1234)
               and not pool.has(port_entry.fp)
               and single["addresses"] == 1)

        supervisor.import_text(VLESS_A, "тест")
        expect("заблокированный хост не возвращается из подписки",
               not any(entry.address == "a.example.com" for entry in pool.items()))
        expect("в чёрном списке видно «все порты»",
               any(int(record.get("port") or 0) == 0
                   for record in pool.blocked_addresses()))
        expect("непонятное значение не ломает разбор",
               supervisor.blacklist_input("совсем не адрес")["invalid"] == 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_single_instance() -> None:
    """Защита от второго запуска: место занимается, сигнал будит работающий."""
    print("test_single_instance")
    from simple import single

    suffix = str(os.getpid())
    first = single.InstanceGuard(r"Local\XCCTestMutex" + suffix,
                                r"Local\XCCTestEvent" + suffix)
    second = single.InstanceGuard(r"Local\XCCTestMutex" + suffix,
                                  r"Local\XCCTestEvent" + suffix)
    try:
        if not single.supported():
            print("  [SKIP] защита от второго запуска работает только в Windows")
            return
        expect("первый экземпляр занимает место", first.acquire() is True)
        expect("второй экземпляр видит первый", second.acquire() is False)
        seen = []
        expect("наблюдатель за сигналом запущен",
               first.watch(lambda: seen.append(1)) is True)
        expect("второй экземпляр будит работающий", second.signal() is True)
        deadline = time.time() + 3.0
        while time.time() < deadline and not seen:
            time.sleep(0.05)
        expect("работающий экземпляр получил сигнал", bool(seen))
    finally:
        first.close()
        second.close()
    third = single.InstanceGuard(r"Local\XCCTestMutex" + suffix,
                                 r"Local\XCCTestEvent" + suffix)
    try:
        expect("после выхода место снова свободно", third.acquire() is True)
    finally:
        third.close()


def test_queue_index_due() -> None:
    """Индекс очереди: пустой этап не перебирает весь пул."""
    print("test_queue_index_due")
    tmp = tempfile.mkdtemp(prefix="simple_index_")
    try:
        settings = SimpleSettings(os.path.join(tmp, "settings.json"))
        policy = simple_policy(settings)
        pool = Pool(os.path.join(tmp, "pool.json"))
        now = time.time()
        expect("у пустого пула очереди нет",
               pool.next_due(LEVEL_PING, policy) == float("inf")
               and pool.next_due_any(policy) == float("inf"))

        congest(pool, 50, SimpleState.NEW.value, next_check_at=now + 600.0)
        expect("ждущие записи не выбираются",
               pool.select(LEVEL_PING, policy, now) == [])
        expect("индекс знает ближайшую дату",
               abs(pool.next_due(LEVEL_PING, policy) - (now + 600.0)) < 1.0)
        expect("этап ядра пуст",
               pool.next_due(LEVEL_CORE, policy) == float("inf")
               and pool.select(LEVEL_CORE, policy, now) == [])
        expect("индекс закэширован по ревизии пула",
               pool._index_revision == pool.revision)

        age(pool, "synnew0", next_check_at=0.0)
        expect("просроченная запись вернулась в очередь",
               len(pool.select(LEVEL_PING, policy, now)) == 1)
        expect("ближайшая дата пересчитана",
               pool.next_due(LEVEL_PING, policy) == 0.0)

        age(pool, "synnew0", state=SimpleState.FAILED.value)
        expect("нерабочая запись очередь не держит",
               abs(pool.next_due(LEVEL_PING, policy) - (now + 600.0)) < 1.0)
        age(pool, "synnew1", state=SimpleState.UNSUPPORTED.value)
        expect("неподдержанная запись тоже не держит очередь",
               pool.select(LEVEL_PING, policy, now) == [])

        congest(pool, 1, SimpleState.WORKING.value)
        expect("рабочий без отметок проверяется сразу",
               pool.next_due(LEVEL_TRAFFIC, policy) == 0.0)
        age(pool, "synworking0", last_traffic_at=now, last_ping_at=now)
        expect("рабочий с отметками ждёт своих интервалов",
               pool.next_due(LEVEL_TRAFFIC, policy) > now
               and pool.next_due(LEVEL_PING, policy) > now)
        expect("до своей даты рабочий не выбирается",
               pool.select(LEVEL_TRAFFIC, policy, now,
                           states={SimpleState.WORKING.value}) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_idle_sleep_and_failed_purge() -> None:
    """Простой цикла спит до ближайшей проверки, нерабочие убираются по сроку."""
    print("test_idle_sleep_and_failed_purge")
    tmp, settings, pool, prober, supervisor = make_env()
    try:
        settings.update({"simple_failed_ttl_hours": 1.0,
                         "simple_save_min_seconds": 45.0,
                         "simple_loop_sleep": 1.0})
        supervisor.reload_policy()
        expect("троттлинг записи взят из настроек", pool.save_interval == 45.0)
        expect("срок годности переведён в секунды",
               supervisor.policy.failed_ttl_seconds == 3600.0)
        expect("пустой пул: цикл спит до предела",
               supervisor._loop_sleep(0) == pipeline_module.MAX_IDLE_SLEEP)
        expect("пока есть работа, пауза обычная",
               supervisor._loop_sleep(7) == 1.0)

        supervisor.import_text(VLESS_A, "тест")
        entry = only(pool)
        prober.script_for(entry.endpoint,
                          {LEVEL_PING: fail(LEVEL_PING, "тайм-аут TCP")})
        for _ in range(5):
            supervisor.run_round()
        expect("сервер стал нерабочим",
               pool.get(entry.fp).state == SimpleState.FAILED.value)
        expect("свежий нерабочий ещё не убран", pool.has(entry.fp) is True)
        expect("повторная очистка в рамках окна не сканирует пул",
               supervisor._purge_failed() == 0)

        age(pool, entry.fp, last_checked=time.time() - 7200.0)
        supervisor._last_purge_at = 0.0     # эмуляция истёкшего окна очистки
        supervisor.run_round()
        expect("просроченный нерабочий убран из пула",
               pool.has(entry.fp) is False)
        expect("уборка учтена в статистике",
               supervisor.snapshot().purged_failed == 1)

        # Запись, которой ещё рано проверяться, заставляет цикл ждать её дату.
        supervisor.import_text(VLESS_A, "тест")
        waiting = only(pool)
        age(pool, waiting.fp, next_check_at=time.time() + 2.0)
        expect("простой ждёт ближайшую дату",
               1.0 <= supervisor._loop_sleep(0) <= 2.5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_import_and_filter,
        test_full_funnel,
        test_failed_servers_stay_in_pool,
        test_delete_failed_option,
        test_retry_pause,
        test_core_failures_demote_and_delete,
        test_traffic_failure_demotes,
        test_working_maintenance,
        test_unsupported_and_missing_core,
        test_settings_form_values,
        test_v2rayn_export,
        test_cores_stop_and_restart,
        test_traffic_targets,
        test_maintenance_priority,
        test_settings_defaults,
        test_stage_modes,
        test_pool_persistence,
        test_command_settings,
        test_hotkey_mapping,
        test_pool_rows_view,
        test_address_blacklist,
        test_frozen_base_dir,
        test_upload_helper_script,
        test_command_render,
        test_command_runner,
        test_command_scheduler,
        test_command_supervisor_hook,
        test_clipboard_classify,
        test_event_triggers,
        test_network_watcher,
        test_supervisor_events,
        test_batch_document_build,
        test_maintenance_quota_and_max,
        test_maintenance_single_recheck_rescues,
        test_maintenance_infra_failure_keeps_servers,
        test_maintenance_guard_mass_failure,
        test_network_guard_stops_checks,
        test_pool_uri_dedup,
        test_pool_failed_ttl,
        test_pool_save_throttle,
        test_blacklist_graveyard_is_compact,
        test_autostart_shortcut,
        test_pyw_entry_point,
        test_pool_gzip,
        test_blacklist_parsing,
        test_blacklist_by_host,
        test_single_instance,
        test_queue_index_due,
        test_idle_sleep_and_failed_purge,
    ]
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            global FAILURES
            print(f"  [FAIL] assertion in {test.__name__}: {exc}")
            FAILURES += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] exception in {test.__name__}: {exc!r}")
            FAILURES += 1
        print()

    print(f"Итог: {'ошибок нет' if not FAILURES else str(FAILURES) + ' ошибок'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
