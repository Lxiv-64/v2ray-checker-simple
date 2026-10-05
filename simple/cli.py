"""Командная строка simple-версии.

    python simple_main.py                  # графический интерфейс
    python simple_main.py --headless       # цикл без интерфейса (Ctrl+C — стоп)
    python simple_main.py --once           # один проход и выход
    python simple_main.py --import FILE    # добавить список из файла
    python simple_main.py --stats          # статистика пула

Логика вынесена отдельно, чтобы её одинаково использовали и ``simple_main.py``,
и запуск пакетом (``python -m simple``).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from . import __version__
from . import single
from .config import SIMPLE_POOL_PATH, SimpleSettings
from .pipeline import Supervisor
from .state import Pool, SimpleState
from app.i18n import set_language, t



def _utf8_console() -> None:
    """Русский текст в консоли Windows без ошибок кодировки.

    Собранный без консоли exe не имеет ни ``stdout``, ни ``stderr``: вместо них
    подставляем «пустышку», чтобы служебные сообщения не превращались в ошибку.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name,
                        open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simple_main",
        description=t("Упрощённая версия проверяльщика: автоматический цикл "
                    "сбора и многоэтапной проверки конфигураций Xray."),
    )
    parser.add_argument("--version", action="version",
                        version=f"Xray Checker Simple {__version__}")
    parser.add_argument("--headless", action="store_true",
                        help=t("Работать без графического интерфейса"))
    parser.add_argument("--once", action="store_true",
                        help=t("Выполнить один проход и выйти"))
    parser.add_argument("--no-refresh", action="store_true",
                        help=t("Не обновлять подписки при запуске"))
    parser.add_argument("--import", dest="import_path", default="",
                        help=t("Добавить конфигурации из указанного файла"))
    parser.add_argument("--export", default="",
                        help=t("Путь выходного файла со списком рабочих"))
    parser.add_argument("--stats", action="store_true",
                        help=t("Показать статистику пула и выйти"))
    parser.add_argument("--tray", action="store_true",
                        help=t("Запустить окно свёрнутым в трей "
                             "(используется автозагрузкой)"))
    parser.add_argument("--pool-dump", action="store_true",
                        help=t("Напечатать содержимое пула (JSON) и выйти — "
                             "работает и со сжатым pool.json.gz"))
    return parser


def _print_stats(supervisor: Supervisor) -> None:
    stats = supervisor.refresh_stats()
    print(t('Всего конфигураций: {}').format(stats.total))
    for state in SimpleState:
        value = getattr(stats, state.value, 0)
        print(f"  {state.label:24} {value}")
    print(t('Исключено (не добавлять): {}').format(stats.blocked))
    print(t('Адресов в чёрном списке: {}').format(stats.blocked_addresses))
    if stats.purged_failed:
        print(t('Убрано по сроку годности: {}').format(stats.purged_failed))
    print(t('Рабочих в файле: {} → {}').format(stats.output_count, stats.output_path))


def _run_headless(args: argparse.Namespace) -> int:
    settings = SimpleSettings()
    if args.export:
        settings.set("simple_output_path", args.export)
    pool = Pool(SIMPLE_POOL_PATH)
    pool.load()
    if args.pool_dump:
        # Читаемый вид пула (в том числе когда он сжат в pool.json.gz):
        # внешние архиваторы для этого не нужны.
        print(pool.dump_text(), flush=True)
        return 0
    supervisor = Supervisor(settings, pool)
    supervisor.on_log(lambda message: print(message, flush=True))

    if args.import_path:
        try:
            with open(args.import_path, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(t('Не удалось прочитать файл: {}').format(exc), file=sys.stderr)
            return 1
        counters = supervisor.import_text(text, args.import_path)
        print(t("Импорт: ") + ", ".join(
            f"{key}={value}" for key, value in counters.items()))
        supervisor.save_pool()          # запись пула ограничена по частоте
        supervisor.write_output_now()
        return 0

    if args.stats:
        _print_stats(supervisor)
        return 0

    if args.once:
        done = supervisor.run_round()
        supervisor.save_pool()          # запись пула ограничена по частоте
        supervisor.write_output_now()
        print(t('Выполнено проверок: {}').format(done))
        started = supervisor.run_external_commands()
        if started:
            print(t('Внешних команд в очереди: {}').format(started))
        _print_stats(supervisor)
        return 0

    supervisor.start(refresh_sources=not args.no_refresh)
    print(t("Цикл запущен. Ctrl+C — остановить."))
    try:
        while supervisor.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(t("\nОстановка…"))
    finally:
        supervisor.stop()
        supervisor.write_output_now()
        # Команда «при остановке» должна успеть выполниться, и только потом
        # гасим планировщик (в интерфейсе он живёт до закрытия окна).
        supervisor.commands.wait_idle(timeout=20)
        supervisor.commands.stop(timeout=5.0)
        _print_stats(supervisor)
    return 0


def _report_already_running(headless: bool) -> None:
    """Сообщить, что программа уже работает (первому экземпляру послан сигнал)."""
    message = (t("Программа уже запущена — работающему экземпляру отправлен "
               "запрос показать окно."))
    if not headless:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(t("Уже запущено"), message, parent=root)
            root.destroy()
            return
        except Exception:  # noqa: BLE001 - без интерфейса скажем в консоль
            pass
    print(message, file=sys.stderr)


def _apply_language() -> None:
    """Активировать язык интерфейса из настроек до построения интерфейса.

    Язык читается раньше разбора аргументов, чтобы справка ``--help`` тоже была
    на выбранном языке.
    """
    try:
        language = SimpleSettings().get("language", "ru")
    except Exception:  # noqa: BLE001 - настройки могут быть недоступны
        language = "ru"
    set_language(language)


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_console()
    _apply_language()
    args = build_parser().parse_args(argv)
    console_only = bool(args.headless or args.once or args.import_path)
    read_only = bool(args.stats or args.pool_dump)

    # Одновременно может работать только один экземпляр: иначе пул, настройки и
    # выходной файл перезаписывают друг друга.  Режимы «только чтение»
    # (--stats, --pool-dump) — исключение: они ничего не меняют.
    guard = None if read_only else single.keep(single.InstanceGuard())
    if guard is not None and not guard.acquire():
        guard.signal()          # попросить первый экземпляр показать окно
        _report_already_running(headless=console_only)
        return 0

    if console_only or read_only:
        return _run_headless(args)

    try:
        from .gui import run as run_gui
    except ImportError as exc:
        print(t('Не удалось загрузить интерфейс: {}').format(exc), file=sys.stderr)
        print(t("Используйте --headless для работы без GUI."), file=sys.stderr)
        return 1
    run_gui(start_in_tray=bool(args.tray), guard=guard)
    return 0
