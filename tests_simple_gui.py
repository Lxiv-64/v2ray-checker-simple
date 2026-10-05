"""Проверка вкладок «Внешние команды» и «Серверы» в реальном (скрытом) окне Tkinter.

Тест офлайн: он не запускает проверки, не трогает сеть и не открывает окно
надолго — создаёт главное окно приложения на временных настройках, проверяет
вкладку, редактор пресета, справку, ручной запуск команды, отрисовку таблицы
серверов (выделение, прокрутка, обновление по месту) и контекстные меню, после
чего закрывает окно.

Прогон::

    python tests_simple_gui.py

Требуется графическая среда (Tkinter); на сервере без дисплея тест сообщит об
этом и завершится успешно.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import simple.gui as gui  # noqa: E402
from simple.commands import CommandPreset  # noqa: E402

FAILURES = 0


def expect(name: str, condition: bool) -> None:
    global FAILURES
    print(f"  [{'OK' if condition else 'FAIL'}]  {name}")
    if not condition:
        FAILURES += 1


def uris(count: int, offset: int = 0) -> str:
    """Набор простых vless-ссылок: наполнить таблицу серверов в тесте."""
    lines = []
    for index in range(offset, offset + count):
        lines.append(
            "vless://123e4567-e89b-12d3-a456-426614174000@"
            f"h{index}.example.net:443?type=tcp&security=tls#n{index}")
    return "\n".join(lines)


def menu_entries(menu) -> list:
    """Пары «подпись, состояние» для пунктов меню (разделители пропущены)."""
    result = []
    for index in range(menu.index("end") + 1):
        if menu.type(index) == "command":
            result.append((menu.entrycget(index, "label"),
                           menu.entrycget(index, "state")))
    return result


def menu_labels(menu) -> list:
    return [label for label, _state in menu_entries(menu)]


def menu_index(menu, prefix: str) -> int:
    """Номер пункта меню по началу подписи (для вызова из теста)."""
    for index in range(menu.index("end") + 1):
        if menu.type(index) == "command" and \
                menu.entrycget(index, "label").startswith(prefix):
            return index
    raise AssertionError(f"в меню нет пункта «{prefix}»")


def write_settings(path: str, **values) -> None:
    """Заготовка файла настроек для теста.

    Иконка в трее выключена: системный трей в тестах не нужен, а лишние окна
    мешали бы проверкам видимости кнопок.
    """
    payload = {"simple_tray_icon": False}
    payload.update(values)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def press(app, widget, keycode: int, state: int = 0,
          sequence: str = "<KeyPress>") -> None:
    """Синтетическое нажатие клавиши в тесте.

    Tk доставляет события клавиатуры только активному окну, поэтому окно и
    виджет сначала получают фокус: иначе результат зависел бы от того, какое
    окно в системе оказалось активным в этот момент.
    """
    try:
        app.focus_force()
        widget.focus_set()
        app.update()
    except Exception:  # noqa: BLE001 - окно могло быть уже закрыто
        pass
    widget.event_generate(sequence, keycode=keycode, state=state)
    app.update()


def check_commands_tab() -> None:
    work = tempfile.mkdtemp(prefix="simple_gui_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)
    output_path = os.path.join(work, "working.txt")

    # Окно строится на временных файлах: настоящие настройки и пул не трогаем,
    # автозапуск и загрузку ядер отключаем заглушкой.
    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None

    original_ask_yes = gui.messagebox.askyesno
    original_show_info = gui.messagebox.showinfo
    app = None
    try:
        # Диалоги подтверждения в тесте отвечают «да» и не блокируют прогон.
        gui.messagebox.askyesno = lambda *args, **kwargs: True
        gui.messagebox.showinfo = lambda *args, **kwargs: None
        app = gui.SimpleApplication()
        app.settings.set("simple_output_path", output_path)
        app.settings.set("simple_v2rayn_path",
                         os.path.join(work, "v2rayn.json"))
        app.supervisor.write_output_now()
        app.update_idletasks()
        app.update()

        tabs = [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()]
        expect("вкладка «Внешние команды» есть в окне",
               "Внешние команды" in tabs)

        help_text = app._commands_help_text()
        expect("справка содержит подстановки и примеры",
               "{file}" in help_text and "{json}" in help_text
               and "ГОТОВЫЕ ПРИМЕРЫ" in help_text)
        app._show_commands_help()
        app.update()
        for child in app.winfo_children():
            if isinstance(child, gui.tk.Toplevel):
                child.destroy()
        app.update()

        # Подсказка самого раздела — одно предложение и одна строка: всё
        # остальное живёт в редакторе пресета и в подробной справке.
        short_hint = app.commands_hint.cget("text")
        expect("подсказка раздела короткая",
               short_hint.count("\n") == 1 and len(short_hint) < 180
               and "описывается в пресете" in short_hint)

        # Время запуска: давность пересчитывается от текущего момента, поэтому
        # «только что» не может застыть в таблице на часы.
        now = time.time()
        expect("давность запуска считается по текущему времени",
               gui.ago_text(now - 5) == "только что"
               and gui.ago_text(now - 300) == "5 мин назад"
               and gui.ago_text(now - 7200) == "2 ч назад"
               and gui.ago_text(now - 3 * 86400) == "")
        expect("в таблице есть и точное время, и давность",
               gui.run_time_text(now - 300).endswith("· 5 мин назад"))
        expect("давнее время показывается только датой",
               "·" not in gui.run_time_text(now - 3 * 86400))
        expect("полная метка содержит дату, секунды и давность",
               gui.run_time_full(now - 90).startswith(
                   time.strftime("%d.%m.%Y", time.localtime(now)))
               and gui.run_time_full(now - 90).endswith("· 1 мин назад"))
        expect("без данных — понятная пометка вместо пустоты",
               gui.run_time_text(0) == ""
               and gui.run_time_full(0) == "время запуска неизвестно")

        example = gui.EXAMPLES[0]
        preset = CommandPreset.from_dict({
            "name": example["title"], "kind": example["kind"],
            "command": example["command"], "host": "example.org", "port": 22,
            "user": "root", "password": "TOPSECRET", "remote_dir": "/var/www",
            "env": {"DEST": "{remote_dir}/download"},
        })
        dialog = gui.PresetDialog(app, preset,
                                  artifacts_provider=app._artifacts)
        app.update()
        dialog._update_preview()
        collected = dialog._collect()
        preview = dialog.preview_text.get("1.0", "end")
        expect("редактор сохраняет поля пресета",
               collected.name == example["title"] and collected.port == 22
               and collected.remote_dir == "/var/www")
        expect("редактор сохраняет пароль профиля",
               collected.password == "TOPSECRET")
        expect("предпросмотр скрывает пароль", "TOPSECRET" not in preview)
        expect("предпросмотр показывает раскрытую команду",
               "example.org" in preview)
        dialog._save()
        expect("после сохранения редактор возвращает пресет",
               dialog.result is not None and dialog.result.id == preset.id)

        # Проверочный запуск показывает замечания прямо в отчёте.
        noisy = gui.CommandPreset.from_dict({
            "name": "проверка замечаний", "kind": "command",
            "command": 'cmd /c echo %NOSUCHVAR% {234234}',
            "timeout_seconds": 30})
        noisy_dialog = gui.PresetDialog(app, noisy,
                                        artifacts_provider=app._artifacts)
        app.update()
        noisy_dialog._test_run()
        deadline = time.time() + 30
        while noisy_dialog._running and time.time() < deadline:
            app.update()
            time.sleep(0.2)
        app.update()
        report = noisy_dialog.preview_text.get("1.0", "end")
        expect("отчёт запуска содержит замечания",
               "замечание:" in report and "%NOSUCHVAR%" in report
               and "{234234}" in report)
        noisy_dialog._cancel()
        app.update()

        quiet = CommandPreset.from_dict({
            "name": "Тест: записать файл", "kind": "command",
            "trigger": "manual", "min_gap_seconds": 0, "timeout_seconds": 30,
            "command": 'cmd /c echo {count} > "{out_dir}\\run.txt"',
        })
        stored = app.supervisor.commands.add(quiet)
        app._refresh_commands_if_changed()
        app.update()
        rows = app.commands_tree.get_children()
        expect("пресет появился в таблице", len(rows) == 1)
        expect("в таблице видно триггер",
               bool(rows) and app.commands_tree.item(rows[0], "values")[3]
               == "Только вручную")

        app.commands_tree.selection_set(stored.id)
        app._run_preset_now()
        deadline = time.time() + 30
        current = None
        while time.time() < deadline:
            app.update()
            app._refresh_commands_if_changed()
            current = app.supervisor.commands.get(stored.id)
            if current is not None and current.last_status:
                break
            time.sleep(0.2)
        expect("ручной запуск выполнился",
               bool(current) and current.last_status == "успешно")
        expect("вывод команды виден в интерфейсе",
               "успешно" in app.commands_output.get("1.0", "end"))
        panel = app.commands_output.get("1.0", "end")
        tail = panel.split("Последний запуск: ", 1)[1].split("\n", 1)[0]
        expect("в панели вывода есть время последнего запуска",
               time.strftime("%d.%m.%Y", time.localtime()) in tail
               and tail.endswith(" с"))
        row = app.commands_tree.item(stored.id, "values")
        expect("в таблице видно точное время запуска, а не только давность",
               time.strftime("%d.%m", time.localtime()) in str(row[5]))

        # Таблица команд обновляется и «по времени»: иначе давность запуска
        # застывала бы в том виде, который был при последнем изменении пресетов.
        app._commands_revision = app.supervisor.commands.revision
        stamp = app._commands_refreshed_at
        app._refresh_commands_if_changed()
        expect("до истечения интервала таблица не перерисовывается заново",
               app._commands_refreshed_at == stamp)
        app._commands_refreshed_at = time.time() - gui.COMMANDS_REFRESH_SECONDS - 1
        app._refresh_commands_if_changed()
        expect("после интервала таблица перерисовывается и без изменений",
               app._commands_refreshed_at > time.time() - 1)
        expect("команда получила подстановки",
               os.path.isfile(os.path.join(work, "run.txt")))
        with open(settings_path, encoding="utf-8") as handle:
            saved = json.load(handle)["simple_commands"]
        expect("состояние записано в настройки",
               bool(saved) and saved[0]["id"] == stored.id)
        # Приватность: скрытый пресет не показывает ни команду, ни вывод.
        marker = "SECRETMARKER42"
        secret = CommandPreset.from_dict({
            "name": "приватный пресет", "kind": "command", "hidden": True,
            "trigger": "manual", "min_gap_seconds": 0, "timeout_seconds": 30,
            "host": "203.0.113.10", "user": "user",
            "command": f"cmd /c echo token={marker}"})
        stored_secret = app.supervisor.commands.add(secret)
        app._refresh_commands_if_changed()
        app.update()

        expect("для обычного пресета кнопка показа не нужна",
               "disabled" in app.btn_reveal_output.state())
        targets = [app.commands_tree.item(item, "values")[2]
                   for item in app.commands_tree.get_children()]
        expect("в таблице у скрытого пресета стоит «(скрыто)»",
               targets.count("(скрыто)") == 1)
        expect("текст команды не виден в таблице",
               not any(marker in str(value) for value in targets))

        app.commands_tree.selection_set(stored_secret.id)
        app.update()
        panel = app.commands_output.get("1.0", "end")
        expect("панель вывода по умолчанию скрыта",
               "скрыты" in panel and marker not in panel)
        expect("кнопка «Показать вывод» доступна у скрытого пресета",
               "disabled" not in app.btn_reveal_output.state())

        app._run_preset_now()
        deadline = time.time() + 30
        current = None
        while time.time() < deadline:
            app.update()
            app._refresh_commands_if_changed()
            current = app.supervisor.commands.get(stored_secret.id)
            if current is not None and current.last_status:
                break
            time.sleep(0.2)
        panel = app.commands_output.get("1.0", "end")
        expect("после запуска содержимое всё ещё скрыто",
               bool(current) and current.last_status == "успешно"
               and marker not in panel)
        expect("журнал не содержит ни команды, ни вывода скрытого пресета",
               marker not in app.log_text.get("1.0", "end"))

        app._toggle_output_reveal()
        app.update()
        panel = app.commands_output.get("1.0", "end")
        expect("кнопка «Показать вывод» раскрывает команду и вывод",
               marker in panel)

        # Удаление и чёрный список — две разные процедуры.
        sample = ("vless://11111111-2222-4333-8444-555555555555@203.0.113.10:443"
                  "?type=tcp&security=tls#docs")
        app.supervisor.import_text(sample, "тест")
        app._reload_pool()
        app.update()
        rows = list(app.pool_tree.get_children())
        expect("тестовая конфигурация попала в таблицу", len(rows) == 1)

        app.pool_tree.selection_set(rows)
        app._delete_selected()
        app.update()
        expect("удаление убрало запись из пула", app.pool.size() == 0)
        expect("удаление не тронуло чёрный список",
               app.pool.blocked_address_count() == 0)
        back = app.supervisor.import_text(sample, "повтор")
        expect("после простого удаления сервер возвращается из подписки",
               back["added"] == 1)

        app._reload_pool()
        app.update()
        app.pool_tree.selection_set(list(app.pool_tree.get_children()))
        app._blacklist_selected()
        app.update()
        expect("чёрный список убрал запись из пула", app.pool.size() == 0)
        expect("адрес попал в чёрный список",
               app.pool.is_blocked_address("203.0.113.10", 443))
        missed = app.supervisor.import_text(sample, "подписка")
        expect("пока адрес в чёрном списке, подписка его не возвращает",
               missed["added"] == 0 and missed["blocked"] == 1)

        app.filter_var.set(gui.FILTER_BLACKLIST)
        app._reload_pool()
        app.update()
        keys = list(app.pool_tree.get_children())
        expect("фильтр чёрного списка показывает адрес",
               keys == ["203.0.113.10:443"])
        expect("в этом фильтре нет конфигураций пула",
               app._filtered_entries() == [])
        expect("кнопки пула ищут конфигурации, а не адреса",
               app._selected_fps() == [])

        app.pool_tree.selection_set(keys)
        app._unblock_selected()
        app.update()
        expect("адрес убран из чёрного списка",
               not app.pool.is_blocked_address("203.0.113.10", 443))
        expect("таблица чёрного списка опустела",
               list(app.pool_tree.get_children()) == [])

        back = app.supervisor.import_text(sample, "повтор")
        expect("после снятия блокировки конфигурация возвращается",
               back["added"] == 1)
        app.filter_var.set(gui.FILTER_NONE)
        app._reload_pool()
        app.update()

        # Горячие клавиши по коду клавиши: раскладка значения не имеет.
        app.notebook.select(app.tab_pool)
        app._reload_pool()
        app.update()
        rows = list(app.pool_tree.get_children())
        expect("есть строка для проверки клавиш", len(rows) == 1)
        press(app, app.pool_tree, 65, state=4,
              sequence="<Control-KeyPress>")
        expect("Ctrl+A выделяет все строки",
               set(app.pool_tree.selection()) == set(rows))

        press(app, app.pool_tree, 46, state=0)
        expect("Del удалил строку и не тронул чёрный список",
               app.pool.size() == 0 and app.pool.blocked_address_count() == 0)

        app.supervisor.import_text(sample, "снова")
        app._reload_pool()
        app.update()
        app.pool_tree.selection_set(list(app.pool_tree.get_children()))
        press(app, app.pool_tree, 46, state=1)
        expect("Shift+Del внёс адрес в чёрный список",
               app.pool.size() == 0
               and app.pool.is_blocked_address("203.0.113.10", 443))

        app.filter_var.set(gui.FILTER_BLACKLIST)
        app._reload_pool()
        app.update()
        app.pool_tree.selection_set(list(app.pool_tree.get_children()))
        press(app, app.pool_tree, 46, state=0)
        expect("Del в списке адресов снимает блокировку",
               not app.pool.is_blocked_address("203.0.113.10", 443))
        app.filter_var.set(gui.FILTER_NONE)
        app._reload_pool()
        app.update()

        # Insert и Del во вкладке «Внешние команды» управляют пресетом.
        app.notebook.select(app.tab_commands)
        app.update()
        preset_id = stored.id
        app.commands_tree.selection_set([preset_id])
        app.update()
        expect("пресет выделен для проверки клавиш",
               tuple(app.commands_tree.selection()) == (preset_id,)
               and app._selected_preset() is not None
               and app._selected_preset().id == preset_id)
        press(app, app.commands_tree, 45)
        expect("Insert выключил пресет",
               app.supervisor.commands.get(preset_id).enabled is False)
        press(app, app.commands_tree, 45)
        expect("Insert включил пресет обратно",
               app.supervisor.commands.get(preset_id).enabled is True)
        press(app, app.commands_tree, 46)
        expect("Del удалил пресет",
               app.supervisor.commands.get(preset_id) is None)
        app.notebook.select(app.tab_pool)
        app.update()
    finally:
        if app is not None:
            try:
                app.supervisor.commands.stop()
                app._closing = True
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        gui.messagebox.askyesno = original_ask_yes
        gui.messagebox.showinfo = original_show_info
        shutil.rmtree(work, ignore_errors=True)

def check_pool_view() -> None:
    """Таблица серверов: отрисовка по месту, выделение, прокрутка и меню ПКМ."""
    work = tempfile.mkdtemp(prefix="simple_view_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)

    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None
    original_ask_yes = gui.messagebox.askyesno
    original_show_info = gui.messagebox.showinfo
    app = None
    try:
        gui.messagebox.askyesno = lambda *args, **kwargs: True
        gui.messagebox.showinfo = lambda *args, **kwargs: None
        app = gui.SimpleApplication()
        app.update()

        counters = app.supervisor.import_text(uris(60), "тест")
        expect("все ссылки приняты в пул", counters["added"] == 60)
        app.notebook.select(app.tab_pool)
        app._reload_pool()
        app.update()
        rows = list(app.pool_tree.get_children())
        expect("в таблице все строки пула", len(rows) == 60)

        # Тик интерфейса: обновляет таблицу и счётчики (дорогую часть статистики
        # он не пересчитывает).
        app._pool_revision = -1
        app._tick()
        app.update()
        expect("тик интерфейса обновил таблицу и счётчики",
               app._pool_revision == app.pool.revision
               and f"Всего {app.pool.size()}" in app.status_counts.cget("text")
               and len(app.pool_tree.get_children()) == 60)

        # Считаем обращения к таблице: обновление «по месту» не должно ни
        # удалять, ни вставлять строки — только править изменившиеся.
        stats = {"insert": 0, "delete": 0, "item": 0}
        real_insert = app.pool_tree.insert
        real_delete = app.pool_tree.delete
        real_item = app.pool_tree.item

        def count(name: str, func):
            def wrapper(*args, **kwargs):
                stats[name] += 1
                return func(*args, **kwargs)
            return wrapper

        app.pool_tree.insert = count("insert", real_insert)
        app.pool_tree.delete = count("delete", real_delete)
        app.pool_tree.item = count("item", real_item)
        try:
            target = rows[30]
            app.pool_tree.selection_set([target])
            app.pool_tree.focus(target)
            app.pool_tree.yview_moveto(0.5)
            app.update()
            scrolled = app.pool_tree.yview()[0]

            def to_connected(entry):
                entry.state = gui.SimpleState.CONNECTED.value
                entry.latency_ms = 42
                entry.last_checked = time.time()
                return "keep"

            app.pool.update(target, to_connected)
            app._reload_pool(reorder=False)
            app.update()
            # Снимок счётчика до чтения значений: само чтение тоже зовёт item().
            written = stats["item"]
            values = app.pool_tree.item(target, "values")
            expect("обновление не удаляло и не вставляло строки",
                   stats["insert"] == 0 and stats["delete"] == 0)
            expect("переписана ровно изменившаяся строка", written == 1)
            expect("значения строки обновились",
                   values[0] == gui.SimpleState.CONNECTED.label
                   and values[3] not in ("", "—"))
            expect("выделение сохранилось",
                   tuple(app.pool_tree.selection()) == (target,))
            expect("прокрутка не сброшена",
                   scrolled > 0.05
                   and abs(app.pool_tree.yview()[0] - scrolled) < 0.02)

            stats.update(insert=0, delete=0, item=0)
            app._reload_pool(reorder=True)
            app.update()
            expect("перестановка строк тоже вернула выделение",
                   tuple(app.pool_tree.selection()) == (target,))
            expect("перестановка строк не сбросила прокрутку",
                   abs(app.pool_tree.yview()[0] - scrolled) < 0.02)
        finally:
            app.pool_tree.insert = real_insert
            app.pool_tree.delete = real_delete
            app.pool_tree.item = real_item

        # Таблица обновляется точечно: новая строка появляется, удалённая уходит.
        known = set(app.pool_tree.get_children())
        app.supervisor.import_text(uris(1, offset=60), "тест")
        app._reload_pool()
        app.update()
        added = [fp for fp in app.pool_tree.get_children() if fp not in known]
        expect("новая запись появилась ровно одной строкой", len(added) == 1)
        expect("подпись новой строки — её адрес",
               added and app.pool_tree.item(added[0], "values")[2]
               == "h60.example.net:443")

        app.pool.remove(added)
        app._reload_pool()
        app.update()
        expect("удалённая запись исчезла из таблицы",
               len(app.pool_tree.get_children()) == len(known))
        # Режим чёрного списка показывает адреса в той же таблице.
        app.pool.block_address("198.51.100.7", 443, "тест")
        app.filter_var.set(gui.FILTER_BLACKLIST)
        app._reload_pool()
        app.update()
        expect("фильтр чёрного списка показал адрес",
               list(app.pool_tree.get_children()) == ["198.51.100.7:443"])
        expect("в этом режиме строк пула для действий нет",
               app._filtered_entries() == [] and app._selected_fps() == [])
        app.filter_var.set(gui.FILTER_NONE)
        app._reload_pool()
        app.update()
        expect("возврат к списку серверов собрал таблицу заново",
               len(app.pool_tree.get_children()) == len(known))
        app.pool.unblock_address("198.51.100.7", 443)

        # Кэш отфильтрованного списка и поиск с задержкой.
        first = app._rows_for_view()
        expect("список строк переиспользуется, пока ничего не изменилось",
               app._rows_for_view() is first)
        app.search_var.set("h7.")
        app._on_search_changed()
        app.update()
        expect("поиск не срабатывает на первое же нажатие",
               len(app.pool_tree.get_children()) == len(known)
               and app._rows_for_view() is first)

        deadline = time.time() + 4
        while time.time() < deadline and \
                len(app.pool_tree.get_children()) == len(known):
            app.update()
            time.sleep(0.05)
        narrowed = list(app.pool_tree.get_children())
        expect("через задержку поиск сузил таблицу",
               len(narrowed) == 1
               and app.pool_tree.item(narrowed[0], "values")[2]
               == "h7.example.net:443")
        expect("после поиска список пересобран заново",
               app._rows_for_view() is not first
               and len(app._rows_for_view()) == 1)
        app.search_var.set("")
        app._apply_search()
        app.update()
        expect("сброс поиска возвращает все строки",
               len(app.pool_tree.get_children()) == len(known))

        # Меню правой кнопки: действия те же, что у кнопок и клавиш.
        rows = list(app.pool_tree.get_children())
        target = rows[10]
        app.pool_tree.selection_set([target])
        app.update()
        menu = app._pool_context_menu()
        labels = menu_labels(menu)
        expect("меню пула предлагает проверку, копирование и удаление",
               "Проверить сейчас" in labels
               and any(label.startswith("Копировать ссылки") for label in labels)
               and any(label.startswith("Удалить из пула") for label in labels))

        def to_connected_again(entry):
            entry.state = gui.SimpleState.CONNECTED.value
            return "keep"

        app.pool.update(target, to_connected_again)
        app._reload_pool()
        menu.invoke(menu_index(menu, "Сбросить в «новые»"))
        app.update()
        expect("пункт меню выполнил то же действие, что кнопка",
               app.pool.get(target).state == gui.SimpleState.NEW.value)

        box = app.pool_tree.bbox(rows[2])
        expect("ПКМ по строке выделяет строку под курсором",
               bool(box) and app._select_row_at(app.pool_tree, box[1] + 2)
               and tuple(app.pool_tree.selection()) == (rows[2],))
        expect("ПКМ по пустому месту выделение не сбрасывает",
               not app._select_row_at(app.pool_tree, 100000)
               and tuple(app.pool_tree.selection()) == (rows[2],))

        edit_labels = menu_labels(app._edit_menu(app.paste_text, (
            ("Добавить в пул", app._add_pasted),
            ("Очистить поле", app._clear_paste_text))))
        expect("меню текстового поля умеет правку, выделение и свои пункты",
               "Копировать" in edit_labels and "Выделить всё" in edit_labels
               and "Добавить в пул" in edit_labels)
        log_labels = menu_labels(app._edit_menu(app.log_text, (
            ("Копировать весь журнал", app._copy_log),
            ("Очистить журнал", app._clear_log))))
        expect("меню журнала умеет копировать и очищать",
               "Копировать весь журнал" in log_labels
               and "Очистить журнал" in log_labels)

        sources_menu = app._sources_context_menu()
        source_entries = dict(menu_entries(sources_menu))
        expect("меню источников предлагает удаление и обновление",
               any(label.startswith("Удалить источник")
                   for label in source_entries)
               and "Обновить подписки сейчас" in source_entries)
        expect("без выбранного источника действия отключены",
               source_entries["Удалить источник\tDel"] == "disabled")
        command_labels = menu_labels(app._commands_context_menu())
        expect("меню команд предлагает добавление и изменение",
               "Добавить команду…" in command_labels
               and any(label.startswith("Изменить…")
                       for label in command_labels))


    finally:
        if app is not None:
            try:
                app.supervisor.commands.stop()
                app._closing = True
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        gui.messagebox.askyesno = original_ask_yes
        gui.messagebox.showinfo = original_show_info
        shutil.rmtree(work, ignore_errors=True)




def descendants(root) -> list:
    """Все вложенные виджеты, включая сам корень (в порядке создания)."""
    result = [root]
    for child in root.winfo_children():
        try:
            result.extend(descendants(child))
        except Exception:  # noqa: BLE001 - виджет мог быть уже уничтожен
            continue
    return result


def right_edge(widget) -> int:
    return widget.winfo_rootx() + widget.winfo_width()


def raise_window(app) -> None:
    """Поднять окно приложения над другими.

    Проверки вида «что находится в точке кнопки» (`winfo_containing`) зависят от
    Z-порядка окон: если окно теста перекрыто чужим окном, Tk вернёт ``None``.
    """
    try:
        app.lift()
        app.attributes("-topmost", True)
        app.update()
        app.attributes("-topmost", False)
        app.update()
    except Exception:  # noqa: BLE001 - окно могло быть уже закрыто
        pass


def pump(app, seconds: float = 0.5) -> None:
    """Прокрутить цикл Tk заданное время (для отложенных обработчиков)."""
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        app.update()
        time.sleep(0.05)


def button_point(app, widget):
    """Точка в центре кнопки, если она реально видна на экране.

    Возвращает ``None``, если кнопка за границей окна или за пределами
    прокручиваемой вкладки: там проверять перекрытие бессмысленно.
    """
    x = widget.winfo_rootx() + widget.winfo_width() // 2
    y = widget.winfo_rooty() + widget.winfo_height() // 2
    if not (app.winfo_rootx() <= x <= app.winfo_rootx() + app.winfo_width()
            and app.winfo_rooty() <= y
            <= app.winfo_rooty() + app.winfo_height()):
        return None
    parent = widget.master
    while parent is not None:
        if parent.winfo_class() == "Canvas":
            top = parent.winfo_rooty()
            if not (top <= y <= top + parent.winfo_height()):
                return None
        parent = parent.master
    return x, y


def check_layout() -> None:
    """Элементы интерфейса не теряются при минимальном размере окна.

    Проверяется то, что раньше доставляло больше всего неудобств: «что-то
    уехало за правый край окна» и «до нижней части вкладки не добраться».
    Окно ставится на минимально допустимый размер — самый неудобный случай.
    """
    work = tempfile.mkdtemp(prefix="simple_layout_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)

    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None
    app = None
    try:
        app = gui.SimpleApplication()
        app.settings.set("simple_output_path",
                         os.path.join(work, "working.txt"))
        app.settings.set("simple_v2rayn_path",
                         os.path.join(work, "v2rayN.json"))
        app.geometry("860x560")          # минимально допустимый размер
        app.update_idletasks()
        app.update()
        raise_window(app)
        win_width = app.winfo_width()
        expect("окно приняло минимальный размер", win_width <= 900)
        expect("окно не выше экрана",
               app.winfo_height() <= app.winfo_screenheight()
               and win_width <= app.winfo_screenwidth())

        # 1. Ни один видимый элемент не выходит за правый край окна, а
        #    подсказки переносятся по фактической ширине контейнера.
        for tab in app.notebook.tabs():
            title = app.notebook.tab(tab, "text")
            app.notebook.select(tab)
            app.update_idletasks()
            app.update()
            raise_window(app)
            over = []
            wide = []
            for widget in descendants(app.nametowidget(tab)):
                if not widget.winfo_ismapped() or widget.winfo_width() <= 1:
                    continue
                if right_edge(widget) > app.winfo_rootx() + win_width + 2:
                    over.append(widget.winfo_class())
                try:
                    wrap = int(widget.cget("wraplength"))
                except Exception:  # noqa: BLE001
                    continue
                parent = widget.master
                if wrap <= 0 or parent is None or not parent.winfo_ismapped():
                    continue
                if parent.winfo_width() > 1 and wrap > parent.winfo_width() + 2:
                    wide.append((widget.winfo_class(), wrap,
                                 parent.winfo_width()))
            expect(f"вкладка «{title}»: ничего не ушло за правый край", not over)
            expect(f"вкладка «{title}»: подсказки переносятся по ширине",
                   not wide)
            # Подпись кнопки не должна обрезаться: ширина меньше запрошенной.
            clipped = []
            for widget in descendants(app.nametowidget(tab)):
                if widget.winfo_class() != "TButton" or not widget.winfo_ismapped():
                    continue
                if widget.winfo_width() + 1 < widget.winfo_reqwidth():
                    clipped.append(str(widget.cget("text")))
            expect(f"вкладка «{title}»: подписи кнопок не обрезаны", not clipped)
            # Кнопка может быть «упакована», но перекрыта соседним виджетом:
            # в точке её центра должна находиться она сама.
            covered = []
            for widget in descendants(app.nametowidget(tab)):
                if widget.winfo_class() != "TButton":
                    continue
                point = button_point(app, widget)
                if point is None:
                    continue
                if app.winfo_containing(*point) is not widget:
                    covered.append(str(widget.cget("text")))
            expect(f"вкладка «{title}»: кнопки видны на своих местах"
                   f"{' (' + ', '.join(covered) + ')' if covered else ''}",
                   not covered)
        # 2. Прокручиваемые вкладки: содержимое учтено и достижимо ползунком.
        for attr, title in (("sources_scroller", "Списки и вставка"),
                            ("pool_scroller", "Серверы"),
                            ("commands_scroller", "Внешние команды"),
                            ("settings_scroller", "Настройки")):
            scroller = getattr(app, attr, None)
            expect(f"вкладка «{title}» умеет прокручиваться",
                   scroller is not None)
            if scroller is None:
                continue
            app.update_idletasks()
            app.update()
            region = [float(value) for value in
                      str(scroller.canvas.cget("scrollregion")).split()]
            expect(f"«{title}»: содержимое попало в область прокрутки",
                   len(region) == 4
                   and region[3] >= scroller.inner.winfo_reqheight() - 1)
            scroller.canvas.yview_moveto(1.0)
            app.update_idletasks()
            expect(f"«{title}»: до нижней части можно доскроллить",
                   scroller.canvas.yview()[1] >= 0.999)

        # 3. Панель кнопок: элементы остаются видимыми и внутри панели.
        bar = app.toolbar
        app.update_idletasks()
        app.update()
        raise_window(app)
        expect("панель кнопок разложила элементы по строкам",
               bool(bar._rendered))
        expect("все кнопки панели видны",
               all(widget.winfo_ismapped() for widget in bar.items()
                   if widget.winfo_manager()))
        expect("кнопки панели не выходят за её границы",
               all(right_edge(widget) <= right_edge(bar) + 2
                   for widget in bar.items() if widget.winfo_ismapped()))
        clipped = [str(widget.cget("text")) for widget in bar.items()
                   if widget.winfo_class() == "TButton"
                   and widget.winfo_ismapped()
                   and widget.winfo_width() + 1 < widget.winfo_reqwidth()]
        expect("подписи кнопок шапки не обрезаны", not clipped)
        # Кнопка может быть «упакована», но перекрыта соседним фреймом: в
        # точке её центра должна находиться она сама.
        hidden = []
        for widget in bar.items():
            if widget.winfo_class() != "TButton":
                continue
            point = button_point(app, widget)
            if point is None:
                continue
            if app.winfo_containing(*point) is not widget:
                hidden.append(str(widget.cget("text")))
        expect("в центре каждой кнопки панели находится сама кнопка",
               not hidden)

        # 4. Узкая панель сама переносит кнопки на следующую строку.
        probe = tk.Toplevel(app)
        probe.geometry("300x220")
        probe.update()
        panel = gui.AdaptiveBar(probe)
        panel.pack(fill="x")
        for text in ("Кнопка один", "Кнопка два", "Кнопка три",
                     "Кнопка четыре"):
            panel.add(gui.ttk.Button(panel, text=text))
        probe.update_idletasks()
        probe.update()
        expect("узкая панель перенесла кнопки на несколько строк",
               len(panel._rendered) >= 2)
        expect("перенесённые кнопки остались в пределах панели",
               all(right_edge(widget) <= right_edge(panel) + 2
                   for widget in panel.items()))
        probe.destroy()

        # 5. Настройки: два поля не могут стоять в одной ячейке сетки —
        #    раньше именно так «Пауза между попытками» накладывалась на другой
        #    переключатель и была не видна.
        app.notebook.select(app.tab_settings)
        app.update_idletasks()
        app.update()
        overlaps = []
        for section in descendants(app.settings_scroller.inner):
            if section.winfo_class() not in ("TLabelframe", "Labelframe"):
                continue
            used = {}
            for child in section.winfo_children():
                info = child.grid_info()
                if not info:
                    continue
                key = (int(info.get("row", 0)), int(info.get("column", 0)))
                if key in used:
                    overlaps.append((section.cget("text"), key))
                used[key] = child
        expect("в настройках нет двух полей в одной ячейке сетки", not overlaps)
    finally:
        if app is not None:
            try:
                app.supervisor.commands.stop()
                app._closing = True
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        shutil.rmtree(work, ignore_errors=True)


def check_menu_and_theme() -> None:
    """Строка меню «Файл/Правка/Вид/Справка», тема «день/ночь» и поля настроек."""
    work = tempfile.mkdtemp(prefix="simple_menu_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)

    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None
    original_info = gui.messagebox.showinfo
    gui.messagebox.showinfo = lambda *args, **kwargs: None
    original_sync = gui.autostart.sync
    app = None

    class FakeGuard:
        """Заглушка защиты от второго запуска: запоминает наблюдателя."""

        def __init__(self):
            self.callbacks = []

        def watch(self, callback):
            self.callbacks.append(callback)
            return True

        def close(self):
            pass

    guard = FakeGuard()
    try:
        app = gui.SimpleApplication(guard=guard)
        app.update_idletasks()
        app.update()

        expect("интерфейс подключил защиту от второго запуска",
               len(guard.callbacks) == 1)
        shown = []
        app._show_from_tray = lambda: shown.append(1)
        if guard.callbacks:
            guard.callbacks[0]()
        pump(app, 0.5)
        expect("сигнал второго запуска поднимает окно", bool(shown))

        menubar = app.menubar
        cascades = [menubar.entrycget(index, "label")
                    for index in range(menubar.index("end") + 1)
                    if menubar.type(index) == "cascade"]
        expect("в окне есть системная строка меню", bool(cascades))
        expect("меню: Файл / Правка / Вид / Справка",
               cascades == ["Файл", "Правка", "Вид", "Справка"])

        def cascade_menu(label: str):
            """Подменю верхнего уровня по подписи (индексы трогать нельзя)."""
            for index in range(menubar.index("end") + 1):
                if (menubar.type(index) == "cascade"
                        and menubar.entrycget(index, "label") == label):
                    return menubar.nametowidget(menubar.entrycget(index, "menu"))
            return None

        file_menu = cascade_menu("Файл")
        expect("в «Файл» есть сохранение вывода и выход",
               file_menu is not None
               and "Сохранить выходные файлы" in menu_labels(file_menu)
               and "Выход" in menu_labels(file_menu))
        view_menu = cascade_menu("Вид")
        night_items = []
        if view_menu is not None:
            night_items = [view_menu.entrycget(index, "label")
                           for index in range(view_menu.index("end") + 1)
                           if view_menu.type(index) == "checkbutton"]
        expect("в «Вид» есть ночная тема и переходы по вкладкам",
               view_menu is not None and "Ночная тема" in night_items
               and "Настройки" in menu_labels(view_menu))
        help_menu = cascade_menu("Справка")
        expect("в «Справка» есть разделы помощи",
               help_menu is not None
               and "О программе" in menu_labels(help_menu)
               and "Горячие клавиши" in menu_labels(help_menu))

        # Смена темы: цвета окна и полей меняются, выбор сохраняется.
        day_bg = str(app.cget("background"))
        app._toggle_theme()
        app.update_idletasks()
        expect("ночная тема включена", app._theme_is_night())
        expect("фон окна изменился",
               str(app.cget("background")).lower() != day_bg.lower())
        expect("настройка темы сохранена",
               app.settings.get("simple_theme") == "night")
        expect("журнал перекрашен под тему",
               str(app.log_text.cget("background")).lower()
               == gui.THEMES["night"]["text_bg"].lower())
        expect("выпадающие меню перекрашиваются темой",
               view_menu is not None
               and str(view_menu.cget("background")).lower()
               == gui.THEMES["night"]["menu_bg"].lower())
        expect("галочка в меню отражает тему", bool(app.theme_var.get()))
        app._toggle_theme()
        app.update_idletasks()
        expect("дневная тема возвращается",
               not app._theme_is_night()
               and str(app.cget("background")).lower() == day_bg.lower())

        # Поля настроек: короткие значения компактны, длинные тянутся.
        app.notebook.select(app.tab_settings)
        app.update_idletasks()
        app.update()
        short = app._field_widgets.get("simple_network_watch_seconds")
        long_field = app._field_widgets.get("traffic_url")
        expect("короткое поле не растягивается",
               short is not None and short.winfo_reqwidth() < 120)
        expect("длинное поле (URL) шире короткого",
               long_field is not None and short is not None
               and long_field.winfo_reqwidth() > short.winfo_reqwidth())
        first = app._field_widgets.get("simple_max_failures")
        second = app._field_widgets.get("simple_ping_interval_minutes")
        expect("колонка значений выровнена по вертикали",
               first is not None and second is not None
               and first.winfo_rootx() == second.winfo_rootx())

        # Галочка автозапуска: настройка сохраняется, файл создаёт модуль
        # simple/autostart.py (здесь он подменён, реальную автозагрузку не трогаем).
        calls = []

        def fake_sync(enabled, *args, **kwargs):
            calls.append(bool(enabled))
            return True, "подмена"

        gui.autostart.sync = fake_sync
        app._vars["simple_start_with_windows"].set(False)
        app._apply_autostart_settings()
        expect("галочка автозапуска сохранена",
               app.settings.get("simple_start_with_windows") is False)
        expect("автозапуск синхронизируется сразу", calls == [False])
        expect("поле автозапуска есть в настройках",
               "simple_start_with_windows" in app._field_widgets)

        # Старт из автозагрузки (--tray): если трей недоступен, окно остаётся
        # видимым — иначе программу не найти и не закрыть.
        auto_app = gui.SimpleApplication(start_in_tray=True)
        try:
            auto_app.update_idletasks()
            auto_app.update()
            auto_app._start_hidden()
            auto_app.update_idletasks()
            expect("без трея автозапуск не прячет окно в никуда",
                   auto_app.state() != "withdrawn")
        finally:
            try:
                auto_app.supervisor.commands.stop()
                auto_app._closing = True
                auto_app.destroy()
            except Exception:  # noqa: BLE001
                pass
    finally:
        gui.autostart.sync = original_sync
        gui.messagebox.showinfo = original_info
        if app is not None:
            try:
                app.supervisor.commands.stop()
                app._closing = True
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        shutil.rmtree(work, ignore_errors=True)


def blacklist_rows_text(app) -> str:
    """Тексты строк таблицы в режиме «Чёрный список адресов»."""
    app.filter_var.set(gui.FILTER_BLACKLIST)
    app._reload_pool()
    app.update_idletasks()
    rows = []
    for iid in app.pool_tree.get_children():
        values = app.pool_tree.item(iid, "values")
        rows.append(" ".join(str(item) for item in values))
    return "\n".join(rows)


def check_blacklist_manual() -> None:
    """Ручное добавление в чёрный список (ссылка, host:port, голый IP)."""
    work = tempfile.mkdtemp(prefix="simple_bl_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)

    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None
    original_info = gui.messagebox.showinfo
    original_warning = gui.messagebox.showwarning
    gui.messagebox.showinfo = lambda *args, **kwargs: None
    gui.messagebox.showwarning = lambda *args, **kwargs: None
    app = None
    try:
        app = gui.SimpleApplication()
        app.settings.set("simple_output_path", os.path.join(work, "working.txt"))
        app.update_idletasks()
        app.update()

        expect("поле ручного ввода в чёрный список есть",
               hasattr(app, "blacklist_input_var"))
        expect("галочка сжатия пула есть в настройках",
               "simple_pool_gzip" in app._field_widgets)

        app.blacklist_input_var.set("198.51.100.77")
        app._blacklist_manual()
        app.update_idletasks()
        records = app.pool.blocked_addresses()
        expect("голый IP добавлен и блокирует все порты",
               app.pool.is_blocked_address("198.51.100.77", 443)
               and records and int(records[0].get("port") or 0) == 0)
        expect("после добавления поле очищено",
               app.blacklist_input_var.get() == "")
        expect("в таблице чёрного списка понятная подпись",
               "все порты" in blacklist_rows_text(app))

        app.blacklist_input_var.set("мусор абракадабра")
        app._blacklist_manual()
        expect("мусор в чёрный список не попадает",
               app.pool.blocked_address_count() == 1)
    finally:
        gui.messagebox.showinfo = original_info
        gui.messagebox.showwarning = original_warning
        if app is not None:
            try:
                app.supervisor.commands.stop()
                app._closing = True
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        shutil.rmtree(work, ignore_errors=True)


def check_privacy_dialog() -> None:
    """Режим «скрытый» — один из двух, а показ содержимого — отдельное действие.

    Раньше в редакторе стояли два независимых флажка («скрыть» и «показать»),
    которые можно было включить одновременно, и было непонятно, что получится.
    Теперь режим выбирается радиокнопками, а разовый показ содержимого —
    кнопкой-тумблером.
    """
    work = tempfile.mkdtemp(prefix="simple_privacy_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)

    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None
    app = None
    dialog = None
    try:
        app = gui.SimpleApplication()
        app.update()
        preset = CommandPreset.from_dict({
            "name": "приватный", "kind": "command",
            "command": "scp -P {port} {file} {user}@{host}:{remote_dir}",
            "host": "example.org", "port": 22, "user": "root",
            "password": "TOPSECRET", "hidden": True,
        })
        dialog = gui.PresetDialog(app, preset, artifacts_provider=app._artifacts)
        dialog.update()
        app.update()

        expect("у пресета из файла выбран режим «скрытый»",
               dialog._hidden_selected() and dialog.mode_var.get() == "hidden")
        expect("скрытый пресет маскирует поля сразу",
               dialog.command_entry.cget("show") == "•"
               and dialog._privacy_masked())
        expect("тумблер показа доступен у скрытого пресета",
               "disabled" not in dialog.reveal_button.state())

        # Вместо двух конкурирующих флажков — два взаимоисключающих режима и
        # один тумблер разового показа именно в блоке «Приватность».
        privacy_frame = None
        for widget in descendants(dialog):
            if widget.winfo_class() in ("TLabelframe", "Labelframe") and \
                    widget.cget("text") == "Приватность":
                privacy_frame = widget
                break
        expect("блок «Приватность» найден", privacy_frame is not None)
        toggles = [widget for widget in descendants(privacy_frame)
                   if widget.winfo_class() == "TCheckbutton"]
        radios = [widget for widget in descendants(privacy_frame)
                  if widget.winfo_class() == "TRadiobutton"]
        expect("в блоке приватности два режима и один тумблер показа",
               len(toggles) == 1 and len(radios) == 2)
        expect("тумблер показа не участвует в выборе режима",
               "Показать содержимое" in str(toggles[0].cget("text")))
        expect("режимы — радиокнопки одной группы",
               len({str(widget.cget("variable")) for widget in radios}) == 1)

        dialog.reveal_var.set(True)
        dialog._sync_privacy()
        expect("показ содержимого снимает маску",
               dialog.command_entry.cget("show") == ""
               and not dialog._privacy_masked())
        expect("тумблер предлагает снова скрыть содержимое",
               dialog.reveal_button.cget("text") == "Скрыть содержимое")
        expect("показ не меняет режим пресета", dialog._hidden_selected())

        dialog.reveal_var.set(False)
        dialog._sync_privacy()
        expect("снятие показа возвращает маску",
               dialog.command_entry.cget("show") == "•")

        dialog.mode_var.set("visible")
        dialog._on_mode_changed()
        expect("обычный режим показывает поля без маски",
               dialog.command_entry.cget("show") == ""
               and not dialog._hidden_selected())
        expect("в обычном режиме тумблер показа заблокирован",
               "disabled" in dialog.reveal_button.state())
        expect("обычный режим сохраняется как hidden=False",
               dialog._collect(validate=False).hidden is False)

        dialog.mode_var.set("hidden")
        dialog._on_mode_changed()
        expect("переход в «скрытый» сразу маскирует содержимое",
               dialog.command_entry.cget("show") == "•"
               and not dialog.reveal_var.get())
        expect("скрытый режим сохраняется как hidden=True",
               dialog._collect(validate=False).hidden is True)

        # Повторное открытие редактора: содержимое снова скрыто.
        dialog.reveal_var.set(True)
        dialog._sync_privacy()
        dialog._load(preset)
        expect("новое открытие редактора снова скрывает содержимое",
               dialog._hidden_selected() and not dialog.reveal_var.get())
    finally:
        if dialog is not None:
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
        if app is not None:
            try:
                app.supervisor.commands.stop()
                app._closing = True
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        shutil.rmtree(work, ignore_errors=True)


def check_smart_paste_and_tray() -> None:
    """Умная вставка буфера и работа окна с иконкой в трее."""
    work = tempfile.mkdtemp(prefix="simple_paste_")
    settings_path = os.path.join(work, "settings.json")
    pool_path = os.path.join(work, "pool.json")
    write_settings(settings_path)

    real_settings, real_pool = gui.SimpleSettings, gui.Pool
    gui.SimpleSettings = lambda *args, **kwargs: real_settings(settings_path)
    gui.Pool = lambda *args, **kwargs: real_pool(pool_path)
    original_start = gui.SimpleApplication._start
    gui.SimpleApplication._start = lambda self: None
    # Диалоги в тесте не показываем: модальное окно остановило бы прогон.
    original_ask = gui.messagebox.askyesno
    original_warning = gui.messagebox.showwarning
    gui.messagebox.askyesno = lambda *args, **kwargs: True
    gui.messagebox.showwarning = lambda *args, **kwargs: None
    app = None
    try:
        app = gui.SimpleApplication()
        app.settings.set("simple_output_path", os.path.join(work, "working.txt"))
        app.update()

        for key in ("simple_tray_icon", "simple_close_to_tray",
                    "simple_minimize_to_tray", "simple_network_watch",
                    "simple_network_watch_seconds",
                    "simple_maintenance_fraction", "simple_maintenance_max",
                    "simple_maintenance_guard", "simple_network_guard",
                    "simple_failed_ttl_hours", "simple_save_min_seconds"):
            expect(f"настройка {key} есть в форме", key in app._vars)

        # 1. Ссылка на подписку из буфера уходит в источники, а не в пул.
        url = "https://example.org/sub.txt"
        app.clipboard_get = lambda: url
        before = len(app.supervisor.sources())
        app._paste_to_pool_text()
        app.update()
        sources = app.supervisor.sources()
        expect("ссылка из буфера попала в источники",
               len(sources) == before + 1 and app.pool.size() == 0)
        expect("в источники записан именно URL",
               sources[-1]["value"] == url and sources[-1]["kind"] == "url")

        # 2. Конфигурации из буфера сразу попадают в пул.
        app.clipboard_get = lambda: uris(3, offset=400)
        app._paste_to_pool_text()
        app.update()
        expect("конфигурации из буфера попали в пул", app.pool.size() == 3)
        expect("отчёт о вставке виден в интерфейсе",
               "конфигураций добавлено" in app.paste_info.cget("text"))

        # 3. Непонятный текст остаётся в поле для ручного разбора.
        app.clipboard_get = lambda: "просто заметка"
        pool_before = app.pool.size()
        app._paste_to_pool_text()
        app.update()
        expect("непонятный текст остался в поле вставки",
               "просто заметка" in app.paste_text.get("1.0", "end")
               and app.pool.size() == pool_before)

        class FakeTray:
            """Заглушка иконки: в тесте системный трей не нужен."""

            active = True

            def __init__(self) -> None:
                self.tooltips = []
                self.notes = []
                self.stopped = False

            def update(self, tooltip=None) -> None:
                self.tooltips.append(tooltip)

            def notify(self, title, message) -> None:
                self.notes.append((title, message))

            def stop(self) -> None:
                self.active = False
                self.stopped = True

        fake = FakeTray()

        # Галочки трея применяются и сохраняются сразу, без «Сохранить настройки».
        # (Иконка в тестовых настройках выключена, поэтому реальный трей не
        # создаётся — дальше поведение окна проверяем на заглушке.)
        app._vars["simple_close_to_tray"].set(True)
        app._vars["simple_minimize_to_tray"].set(True)
        app._apply_tray_settings()
        expect("галочки трея применены и сохранены сразу",
               bool(app.settings.get("simple_close_to_tray"))
               and bool(app.settings.get("simple_minimize_to_tray")))

        app.tray = fake
        app._on_close()
        app.update()
        expect("крестик убирает окно в трей, а не закрывает программу",
               app.state() == "withdrawn" and not app._closing)
        expect("при первом сворачивании показана подсказка", bool(fake.notes))

        app._show_from_tray()
        app.update()
        expect("окно возвращается из трея", app.state() == "normal")
        app.iconify()
        pump(app, 1.0)
        expect("сворачивание окна тоже уходит в трей",
               app.state() == "withdrawn")
        app._show_from_tray()
        app.update()

        app._toggle_window()
        app.update()
        expect("клик по иконке скрывает окно", app.state() == "withdrawn")
        app._toggle_window()
        app.update()
        expect("повторный клик показывает окно", app.state() == "normal")
        app._update_tray_tooltip()
        expect("подсказка иконки обновляется",
               bool(fake.tooltips) and "рабочих" in fake.tooltips[-1])

        labels = menu_labels(app._build_tray_menu())
        expect("меню иконки содержит управление и выход",
               "Показать окно" in labels and "Выход" in labels
               and any("Запустить" in label for label in labels))

        # 4. «Выход» из меню иконки закрывает программу даже при включённом
        #    «закрывать в трей».
        app._quit(ask=False)
        app.update()
        expect("выход из меню иконки закрывает программу",
               app._closing and fake.stopped)
    finally:
        if app is not None:
            try:
                app._closing = True
                app._stop_tray()
                app.destroy()
            except Exception:  # noqa: BLE001
                pass
        gui.SimpleSettings, gui.Pool = real_settings, real_pool
        gui.SimpleApplication._start = original_start
        gui.messagebox.askyesno = original_ask
        gui.messagebox.showwarning = original_warning
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    print("tests_simple_gui")
    try:
        tk.Tk().destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] графическая среда недоступна: {exc}")
        return 0
    try:
        check_commands_tab()
        check_pool_view()
        check_layout()
        check_menu_and_theme()
        check_blacklist_manual()
        check_privacy_dialog()
        check_smart_paste_and_tray()
    except AssertionError as exc:
        print(f"  [FAIL] assertion: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] exception: {exc!r}")
        return 1
    print(f"Итог: {'ошибок нет' if not FAILURES else str(FAILURES) + ' ошибок'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
