"""Минималистичный графический интерфейс simple-версии.

Окно состоит из панели управления, четырёх вкладок («Списки», «Серверы»,
«Настройки», «Журнал») и строки состояния.  Всё управление сведено к одной
кнопке «Запустить»:
после запуска программа работает сама, подтягивает подписки по расписанию,
прогоняет серверы по этапам и постоянно поддерживает выходной файл в актуальном
состоянии.  Вставлять конфигурации можно в любой момент — в том числе по
``Ctrl+V`` из любого места окна.
"""

from __future__ import annotations

import os
import queue
import heapq
import subprocess
import sys
import textwrap
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

from app import core_manager
from app.gui.common import (
    AdaptiveBar,
    Debouncer,
    ScrollableFrame,
    TOOLTIP_COLORS,
    ToolTip,
    adaptive_wraplength,
    bind_hotkeys,
    fit_window,
    fmt_int,
    has_shift,
    human_latency,
    human_time,
    make_treeview,
)
from app.gui.tray import TrayIcon
from app.settings import (
    buffer_logs,
    get_logger,
    setup_logging,
    subscribe_logs,
    unsubscribe_logs,
)

from .clipboard import classify_clipboard
from . import autostart
from .commands import (
    ARTIFACT_LABELS,
    EXAMPLES,
    KIND_LABELS,
    PLACEHOLDERS,
    TRIGGER_LABELS,
    CommandPreset,
    RunResult,
    render_preset,
    run_preset,
)
from .config import (
    DEFAULT_THEME,
    DEFAULTS,
    OUTPUT_BOTH,
    OUTPUT_URI,
    OUTPUT_V2RAYN,
    SIMPLE_OUTPUT_PATH,
    SIMPLE_POOL_PATH,
    SIMPLE_V2RAYN_PATH,
    SimpleSettings,
    collect_field_values,
    enabled_simple_protocols,
    ensure_defaults,
    protocol_labels,
)
from .pipeline import SimpleStats, Supervisor
from .state import Pool, PoolRow, SimpleState
from app.i18n import (
    DEFAULT_LANGUAGE,
    available_languages,
    language_code,
    language_label,
    status_text,
    t,
)


APP_TITLE = "Xray Config Checker — Simple"

#: Имя файла иконки приложения (лежит рядом с exe/скриптом).
ICON_NAME = "ico.ico"

#: Как часто таблица внешних команд перерисовывается «по времени»: давность
#: («5 мин назад») должна обновляться сама, а не застывать до следующего
#: изменения состояния пресетов.
COMMANDS_REFRESH_SECONDS = 5.0

log = get_logger()


def ago_text(timestamp: float) -> str:
    """Давность события словами; пустая строка, если она неинформативна.

    Ровно один «срез» относительного времени: месяц назад полезнее дата, чем
    «719 ч назад», поэтому дальше суток давность не показывается.
    """
    if not timestamp:
        return ""
    delta = time.time() - timestamp
    if delta < 0:
        return ""
    if delta < 60:
        return t("только что")
    if delta < 3600:
        return t('{} мин назад').format(int(delta // 60))
    if delta < 86400:
        return t('{} ч назад').format(int(delta // 3600))
    return ""


def run_time_text(timestamp: float) -> str:
    """Время запуска для таблицы: «26.09 07:33 · 22 мин назад».

    Одной давности мало: пока таблица не перерисована, «только что» остаётся
    в ней и час спустя — точная метка рядом делает столбец честным всегда.
    """
    if not timestamp:
        return ""
    exact = time.strftime("%d.%m %H:%M", time.localtime(timestamp))
    ago = ago_text(timestamp)
    return f"{exact} · {ago}" if ago else exact


def run_time_full(timestamp: float) -> str:
    """Полная метка запуска: «26.09.2026 07:33:12 · 22 мин назад»."""
    if not timestamp:
        return t("время запуска неизвестно")
    exact = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(timestamp))
    ago = ago_text(timestamp)
    return f"{exact} · {ago}" if ago else exact



def project_icon_path() -> str:
    """Путь к иконке приложения: внутри сборки, рядом с exe или скриптом."""
    candidates: List[str] = []
    base = getattr(sys, "_MEIPASS", "")
    if base:
        candidates.append(os.path.join(base, ICON_NAME))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, os.pardir, ICON_NAME))
    candidates.append(os.path.join(here, ICON_NAME))
    candidates.append(os.path.join(os.getcwd(), ICON_NAME))
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""

STATE_ORDER = (
    SimpleState.WORKING,
    SimpleState.CONNECTED,
    SimpleState.REACHABLE,
    SimpleState.NEW,
    SimpleState.FAILED,
    SimpleState.UNSUPPORTED,
)

STATE_COLORS = {
    SimpleState.WORKING.value: "#198754",
    SimpleState.CONNECTED.value: "#0d6efd",
    SimpleState.REACHABLE.value: "#6f42c1",
    SimpleState.NEW.value: "#6c757d",
    SimpleState.FAILED.value: "#8b0000",
    SimpleState.UNSUPPORTED.value: "#b02a37",
}

#: Палитры дневной и ночной темы.  Тема — только оформление: она меняется
#: пунктом меню «Вид» или сочетанием Ctrl+T и сохраняется в настройке
#: ``simple_theme``.  Цвета состояний в дневной теме совпадают с прежними.
THEMES = {
    "day": {
        "panel": "#f0f0f0",
        "fg": "#1f1f1f",
        "muted": "#666666",
        "field_bg": "#ffffff",
        "field_fg": "#1f1f1f",
        "button_bg": "#f0f0f0",
        "button_active": "#e2e2e2",
        "border": "#b9b9b9",
        "select_bg": "#cfe2ff",
        "select_fg": "#1f1f1f",
        "accent": "#0d6efd",
        "heading_bg": "#e9ecef",
        "tree_bg": "#ffffff",
        "tree_fg": "#1f1f1f",
        "menu_bg": "#f5f5f5",
        "menu_fg": "#1f1f1f",
        "menu_active_bg": "#cfe2ff",
        "text_bg": "#ffffff",
        "text_fg": "#1f1f1f",
        "tip_bg": "#ffffe0",
        "tip_fg": "#1f1f1f",
        "states": {
            "working": "#198754",
            "connected": "#0d6efd",
            "reachable": "#6f42c1",
            "new": "#6c757d",
            "failed": "#8b0000",
            "unsupported": "#b02a37",
        },
    },
    "night": {
        "panel": "#25272b",
        "fg": "#e4e6e8",
        "muted": "#9ba1a6",
        "field_bg": "#2d3035",
        "field_fg": "#e4e6e8",
        "button_bg": "#33373d",
        "button_active": "#3e434a",
        "border": "#4a4f57",
        "select_bg": "#2f5d8f",
        "select_fg": "#ffffff",
        "accent": "#4c8dff",
        "heading_bg": "#33373d",
        "tree_bg": "#25272b",
        "tree_fg": "#e4e6e8",
        "menu_bg": "#25272b",
        "menu_fg": "#e4e6e8",
        "menu_active_bg": "#3e434a",
        "text_bg": "#1c1e21",
        "text_fg": "#d7dadd",
        "tip_bg": "#33373d",
        "tip_fg": "#e4e6e8",
        "states": {
            "working": "#4cd07d",
            "connected": "#6ea8fe",
            "reachable": "#c39bf5",
            "new": "#a4abb2",
            "failed": "#ff7b7b",
            "unsupported": "#ff9db0",
        },
    },
}

#: Ширина колонки значений в настройках (пиксели): поля с короткими числами не
#: растягиваются, а колонки остаются выровненными между строками.
FIELD_CELL_MIN = 96

#: Ширина конкретных полей настроек: «пара символов» не должна занимать пол-окна.
FIELD_WIDTHS = {
    "simple_max_failures": 6,
    "simple_retry_minutes": 6,
    "simple_ping_interval_minutes": 6,
    "simple_traffic_interval_hours": 6,
    "simple_blacklist_hours": 6,
    "simple_failed_ttl_hours": 6,
    "simple_save_min_seconds": 6,
    "simple_ping_workers": 6,
    "simple_core_workers": 6,
    "simple_ping_batch": 6,
    "simple_core_batch": 6,
    "simple_traffic_batch": 6,
    "simple_loop_sleep": 6,
    "simple_maintenance_fraction": 6,
    "simple_maintenance_max": 6,
    "simple_maintenance_guard": 6,
    "simple_source_refresh_hours": 6,
    "simple_fetch_timeout": 6,
    "simple_network_watch_seconds": 6,
    "simple_view_limit": 8,
    "max_log_lines": 8,
    "dns_timeout": 6,
    "tcp_timeout": 6,
    "core_timeout": 6,
    "traffic_timeout": 6,
}

#: Поля с длинными значениями (пути, адреса, User-Agent): только они тянутся по
#: ширине блока — коротким это не нужно.
FIELD_STRETCH = {"simple_user_agent", "xray_path", "singbox_path", "traffic_url"}


def style_text(widget: tk.Text, palette: dict) -> None:
    """Покрасить классическое текстовое поле под палитру темы.

    ``tk.Text`` не наследует стили ``ttk``, поэтому цвета задаются явно — в том
    числе для полей в диалогах вроде редактора внешней команды.
    """
    try:
        widget.configure(background=palette["text_bg"],
                         foreground=palette["text_fg"],
                         insertbackground=palette["text_fg"],
                         selectbackground=palette["select_bg"],
                         selectforeground=palette["select_fg"])
    except tk.TclError:
        pass


def palette_for(widget: tk.Misc) -> dict:
    """Палитра темы, действующей в окне приложения (для диалогов)."""
    try:
        top = widget.winfo_toplevel()
    except tk.TclError:
        return THEMES[DEFAULT_THEME]
    palette = getattr(top, "palette", None)
    return palette if isinstance(palette, dict) else THEMES[DEFAULT_THEME]


FILTER_NONE = t("Все состояния")
# Особый «фильтр» вкладки «Серверы»: показывает постоянный чёрный список адресов.
FILTER_BLACKLIST = t("Чёрный список адресов (не опрашивать)")

# Режим обработки этапов внутри круга (см. simple/pipeline.py:_run_stage).
STAGE_MODE_LABELS = {
    t("Каскад (порциями)"): "cascade",
    t("Фазы (этап целиком)"): "phases",
}
STAGE_MODE_LABEL_BY_VALUE = {value: label
                             for label, value in STAGE_MODE_LABELS.items()}

# Формат выходных файлов (см. simple/v2rayn.py).
OUTPUT_FORMAT_LABELS = {
    t("Список ссылок (txt)"): OUTPUT_URI,
    t("Конфигурация для v2rayN (json)"): OUTPUT_V2RAYN,
    t("Оба файла"): OUTPUT_BOTH,
}
OUTPUT_FORMAT_LABEL_BY_VALUE = {value: label
                                for label, value in OUTPUT_FORMAT_LABELS.items()}

# Словари раздела «Внешние команды» (см. simple/commands.py).
KIND_LABEL_BY_VALUE = {value: label for label, value in KIND_LABELS.items()}
TRIGGER_LABEL_BY_VALUE = {value: label
                          for label, value in TRIGGER_LABELS.items()}
ARTIFACT_LABEL_BY_VALUE = {value: label
                           for label, value in ARTIFACT_LABELS.items()}

# Быстрые словари для таблицы серверов: ранг состояния для сортировки и подписи.
STATE_RANK = {state.value: index for index, state in enumerate(STATE_ORDER)}
STATE_LABEL_BY_VALUE = {state.value: state.label for state in SimpleState}
STATE_VALUE_BY_LABEL = {state.label: state.value for state in STATE_ORDER}

V2RAYN_HINT = (
    t("Как импортировать в v2rayN офлайн: меню «Серверы» → «Добавить "
    "пользовательскую конфигурацию» → укажите этот JSON-файл. В списке появится "
    "одна запись, внутри которой все рабочие серверы: ядро само выбирает "
    "быстрейший (балансировка leastPing). Локальные порты внутри файла — "
    "SOCKS 10808 и HTTP 10809.")
)


class PresetDialog(tk.Toplevel):
    """Редактор одного пресета внешней команды.

    Диалог самодостаточен: он собирает поля, показывает предпросмотр готовой
    командной строки (с маскировкой пароля) и умеет запустить команду «здесь и
    сейчас», ничего не сохраняя.  Результат доступен в ``self.result``.
    """

    def __init__(self, master: tk.Misc, preset: CommandPreset,
                 artifacts_provider=None) -> None:
        super().__init__(master)
        self.title(t("Пресет внешней команды"))
        fit_window(self, 900, 760, min_width=640, min_height=460,
                   margin_y=140)
        self.transient(master)
        self.result: Optional[CommandPreset] = None
        self._preset = preset
        self._artifacts = artifacts_provider or (lambda: {})
        self._running = False
        self._test_warnings: List[str] = []
        # Доставка результата в главный поток: у главного окна есть своя очередь
        # (в неё пишут и фоновые потоки приложения), поэтому берём её, если она
        # есть.  Вызов after() из чужого потока работает только когда главный
        # поток уже внутри mainloop.
        self._dispatch = getattr(master, "_ui", None)
        self._build()
        self._load(preset)
        self._update_preview()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())

    # -- сборка ----------------------------------------------------------
    def _build(self) -> None:
        hint = ttk.Label(
            self, style="Hint.TLabel", justify="left",
            text=(t("Соберите команду из подстановок: {file} — список рабочих, "
                  "{json} — конфиг для v2rayN, {count} — число серверов, "
                  "{host} {port} {user} {password} {remote_dir} — профиль "
                  "ниже. Полная справка с примерами — на вкладке "
                  "«Внешние команды».")))
        hint.pack(anchor="w", padx=10, pady=(8, 4))
        adaptive_wraplength(hint, self, margin=30, minimum=260)

        scroller = ScrollableFrame(self, height=420)
        scroller.pack(fill="both", expand=True, padx=10)
        inner = scroller.inner

        what = ttk.LabelFrame(inner, text=t("Что запускать"), padding=8)
        what.pack(fill="x", pady=(0, 8))
        self.name_var = tk.StringVar()
        self.kind_var = tk.StringVar()
        self.command_var = tk.StringVar()
        self.script_var = tk.StringVar()
        self.arguments_var = tk.StringVar()
        self.artifact_var = tk.StringVar()
        self.working_dir_var = tk.StringVar()
        self._row(what, 0, t("Имя пресета:"), ttk.Entry(what, textvariable=self.name_var))
        self.kind_box = ttk.Combobox(what, textvariable=self.kind_var,
                                     values=list(KIND_LABELS), state="readonly",
                                     width=22)
        self._row(what, 1, t("Тип запуска:"), self.kind_box)
        self.kind_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_kind())
        self.command_entry = ttk.Entry(what, textvariable=self.command_var)
        self._row(what, 2, t("Командная строка:"), self.command_entry)
        script_row = ttk.Frame(what)
        self.script_entry = ttk.Entry(script_row, textvariable=self.script_var)
        self.script_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(script_row, text=t("Обзор…"), width=10,
                   command=self._browse_script).pack(side="left", padx=(4, 0))
        self._row(what, 3, t("Файл скрипта:"), script_row)
        self.arguments_entry = ttk.Entry(what, textvariable=self.arguments_var)
        self._row(what, 4, t("Аргументы скрипта:"), self.arguments_entry)
        self.artifact_box = ttk.Combobox(what, textvariable=self.artifact_var,
                                         values=list(ARTIFACT_LABELS),
                                         state="readonly", width=24)
        self._row(what, 5, t("Что передаём:"), self.artifact_box)
        dir_row = ttk.Frame(what)
        ttk.Entry(dir_row, textvariable=self.working_dir_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(dir_row, text=t("Обзор…"), width=10,
                   command=self._browse_dir).pack(side="left", padx=(4, 0))
        self._row(what, 6, t("Рабочий каталог:"), dir_row)

        when = ttk.LabelFrame(inner, text=t("Когда запускать"), padding=8)
        when.pack(fill="x", pady=(0, 8))
        self.trigger_var = tk.StringVar()
        self.interval_var = tk.StringVar()
        self.gap_var = tk.StringVar()
        self.timeout_var = tk.StringVar()
        self.retry_var = tk.StringVar()
        self.retry_delay_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar(value=True)
        self.trigger_box = ttk.Combobox(when, textvariable=self.trigger_var,
                                        values=list(TRIGGER_LABELS),
                                        state="readonly", width=24)
        self._row(when, 0, t("Триггер:"), self.trigger_box)
        ToolTip(self.trigger_box,
                t("Что запускает пресет:\n"
                "• изменение списка — выходной файл перезаписан;\n"
                "• по расписанию — раз в N минут;\n"
                "• вручную — только кнопкой;\n"
                "• запуск/остановка автоматизации — кнопки «Запустить» и «Стоп»;\n"
                "• круг проверок завершён — после каждого круга;\n"
                "• сеть пропала/восстановилась — по слежке за связью."))
        grid = ttk.Frame(when)
        grid.grid(row=1, column=1, sticky="ew", pady=3)
        # Поля времени раскладываются по три в строку: одной строкой из пяти
        # полей они не помещаются в узкое окно и уезжали за правый край.
        for index, (label, var) in enumerate((
                (t("Интервал, мин:"), self.interval_var),
                (t("Пауза, с:"), self.gap_var),
                (t("Тайм-аут, с:"), self.timeout_var),
                (t("Повторов:"), self.retry_var),
                (t("Пауза повтора, с:"), self.retry_delay_var))):
            row, column = divmod(index, 3)
            ttk.Label(grid, text=label).grid(row=row, column=column * 2,
                                             sticky="w", padx=(0, 4), pady=1)
            ttk.Entry(grid, textvariable=var, width=8).grid(
                row=row, column=column * 2 + 1, sticky="w", padx=(0, 10),
                pady=1)
        self._row(when, 2, t("Включён:"), ttk.Checkbutton(when,
                                                       variable=self.enabled_var))

        profile = ttk.LabelFrame(
            inner, text=t("Профиль подключения (необязательно)"), padding=8)
        profile.pack(fill="x", pady=(0, 8))
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.remote_dir_var = tk.StringVar()
        pair = ttk.Frame(profile)
        pair.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(pair, text=t("Адрес:")).grid(row=0, column=0, sticky="w",
                                            padx=(0, 4))
        ttk.Entry(pair, textvariable=self.host_var, width=26).grid(
            row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Label(pair, text=t("Порт:")).grid(row=0, column=2, sticky="w",
                                           padx=(0, 4))
        ttk.Entry(pair, textvariable=self.port_var, width=8).grid(
            row=0, column=3, sticky="w")
        self._row(profile, 1, t("Пользователь:"),
                  ttk.Entry(profile, textvariable=self.user_var))
        self._row(profile, 2, t("Пароль/токен:"),
                  ttk.Entry(profile, textvariable=self.password_var, show="*"))
        self._row(profile, 3, t("Удалённый каталог:"),
                  ttk.Entry(profile, textvariable=self.remote_dir_var))
        profile.columnconfigure(1, weight=1)
        profile_hint = ttk.Label(profile, style="Hint.TLabel", justify="left",
                                 text=(t("Пароль не попадает в журнал "
                                       "(маскируется) и дополнительно "
                                       "передаётся команде как переменная "
                                       "окружения COMMAND_PASSWORD.")))
        profile_hint.grid(row=4, column=1, sticky="w", pady=(4, 0))
        adaptive_wraplength(profile_hint, profile, margin=24, minimum=240)

        env_box = ttk.LabelFrame(inner, text=t("Переменные окружения"), padding=8)
        env_box.pack(fill="x", pady=(0, 8))
        ttk.Label(env_box, style="Hint.TLabel",
                  text=t("По одной на строку в виде ИМЯ=значение. "
                       "Значения с {подстановками} тоже раскрываются.")).pack(
            anchor="w")
        self.env_text = tk.Text(env_box, height=3, wrap="none")
        style_text(self.env_text, palette_for(self))
        self.env_text.pack(fill="x", pady=(4, 0))

        privacy = ttk.LabelFrame(inner, text=t("Приватность"), padding=8)
        privacy.pack(fill="x", pady=(0, 8))
        # Режим пресета — один из двух (радиокнопки).  Раньше здесь стояли два
        # независимых флажка «скрыть» и «показать»: их можно было включить
        # одновременно, и было непонятно, что получится.  Показ содержимого —
        # это разовое действие в текущем окне, а не второе состояние пресета,
        # поэтому он вынесен в отдельную кнопку-тумблер.
        self.mode_var = tk.StringVar(value="visible")
        self.reveal_var = tk.BooleanVar(value=True)
        ttk.Label(privacy, text=t("Режим пресета:")).pack(anchor="w")
        ttk.Radiobutton(
            privacy, variable=self.mode_var, value="visible",
            text=t("Обычный: команда и вывод видны в интерфейсе"),
            command=self._on_mode_changed).pack(anchor="w", padx=(14, 0))
        ttk.Radiobutton(
            privacy, variable=self.mode_var, value="hidden",
            text=t("Скрытый: команда и вывод не показываются без запроса"),
            command=self._on_mode_changed).pack(anchor="w", padx=(14, 0),
                                                pady=(2, 0))
        # Кнопка-тумблер: в теме clam стиль Toolbutton есть, но если тема его не
        # определяет (бывает на Linux), остаётся обычный флажок — он тоже
        # переключает только показ содержимого, а не режим пресета.
        try:
            toggle_style = ("Toolbutton"
                            if ttk.Style(self).layout("Toolbutton")
                            else "TCheckbutton")
        except tk.TclError:
            toggle_style = "TCheckbutton"
        self.reveal_button = ttk.Checkbutton(
            privacy, variable=self.reveal_var, style=toggle_style,
            text=t("Показать содержимое"), command=self._sync_privacy)
        self.reveal_button.pack(anchor="w", pady=(6, 0))
        privacy_hint = ttk.Label(
            privacy, style="Hint.TLabel", justify="left",
            text=(t("У скрытого пресета в таблице вместо команды стоит "
                  "«(скрыто)», вывод в панели скрыт до нажатия «Показать "
                  "вывод», а хвост вывода не пишется в журнал. Кнопка "
                  "«Показать содержимое» действует только в этом окне и "
                  "режим пресета не меняет; пароль и адрес сервера маскируются "
                  "в любом случае.")))
        privacy_hint.pack(anchor="w", pady=(4, 0))
        adaptive_wraplength(privacy_hint, privacy, margin=24, minimum=240)

        preview = ttk.LabelFrame(inner, text=t("Предпросмотр"), padding=8)
        preview.pack(fill="both", expand=True)
        self.preview_text = tk.Text(preview, height=6, wrap="word",
                                    state="disabled")
        style_text(self.preview_text, palette_for(self))
        self.preview_text.pack(fill="both", expand=True)
        row = ttk.Frame(preview)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text=t("Обновить предпросмотр"),
                   command=self._update_preview).pack(side="left")
        self.test_button = ttk.Button(row, text=t("Выполнить сейчас"),
                                      command=self._test_run)
        self.test_button.pack(side="left", padx=4)
        ttk.Label(row, style="Hint.TLabel",
                  text=t("Запуск проверочный: пресет при этом не сохраняется.")).pack(
            side="left", padx=8)

        scroller.bind_wheel()

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text=t("Сохранить"), style="Big.TButton",
                   command=self._save).pack(side="left")
        ttk.Button(bar, text=t("Отмена"),
                   command=self._cancel).pack(side="left", padx=6)
        self.status_label = ttk.Label(bar, text="", style="Hint.TLabel")
        self.status_label.pack(side="right")

    @staticmethod
    def _row(parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(0, 8), pady=3)
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        parent.columnconfigure(1, weight=1)

    # -- данные ----------------------------------------------------------
    @staticmethod
    def _number(text: str, default: float) -> float:
        try:
            return float(str(text).replace(",", ".").strip())
        except (TypeError, ValueError):
            return default

    def _load(self, preset: CommandPreset) -> None:
        """Заполнить поля из пресета."""
        self.name_var.set(preset.name)
        self.kind_var.set(KIND_LABEL_BY_VALUE.get(preset.kind,
                                                  t("Командная строка")))
        self.command_var.set(preset.command)
        self.script_var.set(preset.script)
        self.arguments_var.set(preset.arguments)
        self.artifact_var.set(ARTIFACT_LABEL_BY_VALUE.get(
            preset.artifact, t("Список ссылок ({file})")))
        self.trigger_var.set(TRIGGER_LABEL_BY_VALUE.get(
            preset.trigger, t("При изменении списка")))
        self.interval_var.set(f"{preset.interval_minutes:g}")
        self.gap_var.set(f"{preset.min_gap_seconds:g}")
        self.timeout_var.set(f"{preset.timeout_seconds:g}")
        self.retry_var.set(str(preset.retry_count))
        self.retry_delay_var.set(f"{preset.retry_delay_seconds:g}")
        self.enabled_var.set(bool(preset.enabled))
        self.host_var.set(preset.host)
        self.port_var.set(str(preset.port or ""))
        self.user_var.set(preset.user)
        self.password_var.set(preset.password)
        self.remote_dir_var.set(preset.remote_dir)
        if preset.env:
            self.env_text.insert("1.0", "\n".join(
                f"{name}={value}" for name, value in preset.env.items()))
        self.mode_var.set("hidden" if preset.hidden else "visible")
        self.reveal_var.set(not preset.hidden)
        self._sync_privacy()
        self._sync_kind()
        for var in (self.name_var, self.command_var, self.script_var,
                    self.arguments_var, self.host_var, self.port_var,
                    self.user_var, self.remote_dir_var):
            var.trace_add("write", lambda *_args: self._update_preview())

    # -- приватность ------------------------------------------------------
    def _hidden_selected(self) -> bool:
        """Выбран ли режим «скрытый» (радиокнопка, а не отдельный флажок)."""
        return self.mode_var.get() == "hidden"

    def _privacy_masked(self) -> bool:
        """Нужно ли прятать текст команды от глаз прямо сейчас."""
        return bool(self._hidden_selected() and not self.reveal_var.get())

    def _on_mode_changed(self) -> None:
        """Сменили режим пресета: разовый показ сбрасывается.

        У «обычного» режима прятать нечего, у «скрытого» содержимое снова
        маскируется — так состояние однозначно, без двух конкурирующих флажков.
        """
        self.reveal_var.set(not self._hidden_selected())
        self._sync_privacy()

    def _sync_privacy(self) -> None:
        """Скрытый пресет показывает содержимое только по явному запросу.

        Поля ввода и предпросмотр маскируются символами, пока пользователь не
        нажмёт «Показать содержимое»: так команда с адресом сервера не светится
        на экране при случайном открытии окна.  Показ — действие этого окна:
        при следующем открытии редактора содержимое снова скрыто.
        """
        hidden = self._hidden_selected()
        mask = "•" if self._privacy_masked() else ""
        for entry in (self.command_entry, self.script_entry,
                      self.arguments_entry):
            try:
                entry.configure(show=mask)
            except tk.TclError:
                pass
        try:
            self.reveal_button.state(["!disabled"] if hidden else ["disabled"])
            if hidden:
                self.reveal_button.configure(
                    text=t("Скрыть содержимое") if self.reveal_var.get()
                    else t("Показать содержимое"))
            else:
                self.reveal_button.configure(text=t("Показать содержимое"))
        except tk.TclError:
            pass
        self._update_preview()

    def _sync_kind(self) -> None:
        """Подсказка о том, что именно будет запущено."""
        script_kind = KIND_LABELS.get(self.kind_var.get()) == "script"
        self.status_label.configure(
            text=(t("Запускается файл скрипта с аргументами.")
                  if script_kind else
                  t("Запускается командная строка как есть.")))

    def _browse_script(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=t("Выберите скрипт"),
            initialdir=os.path.dirname(self.script_var.get() or os.getcwd()),
            filetypes=[(t("Скрипты"), "*.cmd *.bat *.ps1 *.py *.exe *.sh"),
                       (t("Все файлы"), "*.*")])
        if path:
            self.script_var.set(path)
            if self.name_var.get() in ("", t("Новая команда")):
                self.name_var.set(os.path.basename(path))

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(
            parent=self, title=t("Рабочий каталог"),
            initialdir=self.working_dir_var.get() or os.getcwd())
        if path:
            self.working_dir_var.set(path)

    def _collect(self, validate: bool = True) -> Optional[CommandPreset]:
        """Собрать пресет из полей (``validate`` — предупредить о пустом)."""
        env: Dict[str, str] = {}
        try:
            raw_env = self.env_text.get("1.0", "end")
        except tk.TclError:
            raw_env = ""
        for line in raw_env.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip():
                env[key.strip()] = value.strip()

        data = self._preset.to_dict()
        data.update({
            "name": self.name_var.get().strip() or t("Без имени"),
            "kind": KIND_LABELS.get(self.kind_var.get(), "command"),
            "command": self.command_var.get().strip(),
            "script": self.script_var.get().strip(),
            "arguments": self.arguments_var.get(),
            "artifact": ARTIFACT_LABELS.get(self.artifact_var.get(), "links"),
            "trigger": TRIGGER_LABELS.get(self.trigger_var.get(), "on_change"),
            "interval_minutes": self._number(self.interval_var.get(), 60.0),
            "min_gap_seconds": self._number(self.gap_var.get(), 60.0),
            "timeout_seconds": self._number(self.timeout_var.get(), 600.0),
            "retry_count": int(self._number(self.retry_var.get(), 0.0)),
            "retry_delay_seconds": self._number(self.retry_delay_var.get(),
                                                60.0),
            "working_dir": self.working_dir_var.get().strip(),
            "host": self.host_var.get().strip(),
            "port": int(self._number(self.port_var.get(), 0.0)),
            "user": self.user_var.get().strip(),
            "password": self.password_var.get(),
            "remote_dir": self.remote_dir_var.get().strip(),
            "env": env,
            "enabled": bool(self.enabled_var.get()),
            "hidden": self._hidden_selected(),
        })
        if not (data["script"] or data["command"]):
            if validate:
                messagebox.showwarning(
                    t("Пресет"),
                    t("Укажите командную строку или файл скрипта."), parent=self)
            return None
        return CommandPreset.from_dict(data)

    def _set_preview(self, text: str) -> None:
        try:
            self.preview_text.configure(state="normal")
            self.preview_text.delete("1.0", "end")
            self.preview_text.insert("1.0", text)
            self.preview_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _update_preview(self) -> None:
        if self._privacy_masked():
            self._set_preview(
                t("Предпросмотр скрыт: у пресета выбран режим «скрытый».\n"
                "Нажмите «Показать содержимое», чтобы увидеть командную строку "
                "с адресом сервера."))
            return
        preset = self._collect(validate=False)
        if preset is None:
            self._set_preview(t("Укажите командную строку или файл скрипта."))
            return
        artifacts = self._artifacts()
        rendered = render_preset(preset, artifacts)
        lines = [rendered.safe_command_line or t("(команда пуста)")]
        if preset.env:
            lines += ["", t("Переменные окружения: ") + ", ".join(preset.env)]
        if rendered.warnings:
            lines += ["", t("Замечания:")]
            lines += [f"  • {warning}" for warning in rendered.warnings]
        lines += ["", t('Файлы: {}').format(artifacts.get('file', ''))]
        if artifacts.get("count") is not None:
            lines.append(t('Серверов в файле: {}').format(artifacts.get('count')))
        self._set_preview("\n".join(lines))

    def _test_run(self) -> None:
        """Проверочный запуск текущих (ещё не сохранённых) полей."""
        preset = self._collect()
        if preset is None or self._running:
            return
        self._running = True
        self._test_warnings = render_preset(preset, self._artifacts()).warnings
        try:
            self.test_button.state(["disabled"])
        except tk.TclError:
            pass
        threading.Thread(target=self._test_worker, args=(preset,),
                         daemon=True).start()

    def _test_worker(self, preset: CommandPreset) -> None:
        result = run_preset(preset, self._artifacts())

        def done() -> None:
            self._test_done(result)

        dispatch = self._dispatch
        try:
            if callable(dispatch):
                dispatch(done)
            else:
                self.after(0, done)
        except Exception:  # noqa: BLE001 - окно могло уже закрыться
            pass

    def _test_done(self, result: RunResult) -> None:
        self._running = False
        try:
            self.test_button.state(["!disabled"])
            head = (t('успешно за {:.1f} с').format(result.duration) if result.ok
                    else t('ошибка: {}').format(result.error))
            text = ["", t("--- проверочный запуск ---"), head]
            for warning in self._test_warnings:
                text.append(t('замечание: {}').format(warning))
            if result.output:
                if self._privacy_masked():
                    text.append(t("[вывод скрыт: пресет в режиме «скрытый»]"))
                else:
                    text.append(result.output)
            self.preview_text.configure(state="normal")
            self.preview_text.insert("end", "\n".join(text) + "\n")
            self.preview_text.see("end")
            self.preview_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _save(self) -> None:
        preset = self._collect()
        if preset is None:
            return
        self.result = preset
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        try:
            self.destroy()
        except tk.TclError:
            pass


def _open_path(path: str) -> None:
    """Открыть файл или папку системным способом."""
    if not path:
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------

class SimpleApplication(tk.Tk):
    """Единственное окно приложения."""

    #: Как часто разрешено переставлять строки таблицы серверов (секунды).
    #: Во время круга порядок «замораживается»: иначе строки прыгают под
    #: курсором, а пересборка таблицы заметна на глаз.
    ORDER_INTERVAL = 1.5
    #: Задержка поиска по таблице серверов (мс): чтобы не искать на каждую букву.
    SEARCH_DELAY_MS = 250

    def __init__(self, start_in_tray: bool = False, guard=None) -> None:
        super().__init__()
        self.title(APP_TITLE)
        # Размер окна подгоняется под экран: на 1366x768 окно окажется ниже
        # «паспортных» 1240x800 и не уедет нижней частью за панель задач.
        fit_window(self, 1240, 800, min_width=860, min_height=560)
        # Иконка окна и трея: файл ico.ico рядом с exe или скриптом.
        self._icon_path = project_icon_path()
        if self._icon_path:
            try:
                self.iconbitmap(self._icon_path)
            except tk.TclError:
                pass

        self.settings = SimpleSettings()
        self.pool = Pool(SIMPLE_POOL_PATH)
        self.pool.load()
        self.supervisor = Supervisor(self.settings, self.pool)

        self._vars: Dict[str, tk.Variable] = {}
        self._field_types: List[Tuple[str, tk.Variable, type]] = []
        self._protocol_vars: Dict[str, tk.BooleanVar] = {}
        self._log_handler = None
        self._log_lines = 0
        self._pool_revision = -1
        self._commands_revision = -1
        self._commands_refreshed_at = 0.0
        self._revealed_for = ""
        # Таблица серверов обновляется по месту: держим порядок строк, их
        # значения и кэш отфильтрованного списка (см. _sync_tree/_rows_for_view).
        self._pool_order: List[str] = []
        self._pool_values: Dict[str, Tuple] = {}
        self._pool_rows: Optional[List[PoolRow]] = None
        self._pool_rows_key: Optional[Tuple] = None
        self._last_order_at = 0.0
        self._search_needle = ""
        self._sources_signature: Optional[Tuple] = None
        self._search_debounce = Debouncer(self, self.SEARCH_DELAY_MS)
        self._busy_round = False
        self._closing = False
        # Иконка в трее: объект создаётся лениво (см. _setup_tray), а меню —
        # при первом обращении, чтобы не тратить ресурсы на старте.
        self.tray: Optional[TrayIcon] = None
        self._tray_menu: Optional[tk.Menu] = None
        self._tray_tooltip = ""
        self._tray_hint_shown = False
        # Обмен с фоновыми потоками идёт только через очередь: обращения к Tk
        # из чужих потоков могут подвесить окно при закрытии.
        self._events: "queue.Queue" = queue.Queue()
        self._stats = SimpleStats()
        self._stats.output_path = str(
            self.settings.get("simple_output_path") or SIMPLE_OUTPUT_PATH)
        # Смена темы «день/ночь» перекрашивает меню и текстовые поля: их списки
        # заполняются по мере создания виджетов (см. _apply_theme).
        self._text_widgets: List[tk.Text] = []
        self._menu_widgets: List[tk.Menu] = []
        #: Виджеты полей настроек по ключу (нужны, например, тестам раскладки).
        self._field_widgets: Dict[str, tk.Widget] = {}
        #: Защита от второго запуска программы (см. simple/single.py).
        self._guard = guard

        self._build_style()
        self._build_toolbar()
        self._build_statusbar()
        self._build_notebook()
        self._build_menu()
        self._bind_hotkeys()
        self._wire_supervisor()
        # Второй запуск программы не создаёт второе окно, а будит это
        # (см. simple/single.py): сигнал приходит из другого процесса.
        if self._guard is not None:
            self._guard.watch(self._on_second_launch)

        # Планировщик внешних команд работает всё время, пока открыто окно:
        # события «запуск», «остановка», «круг» и «сеть» приходят и без цикла
        # проверок, поэтому программа ведёт себя как служба.
        self.supervisor.commands.start()
        # Окно можно закрыть или свернуть в трей — автоматизация продолжит работу.
        self.bind("<Unmap>", self._on_unmap)
        # Иконка создаётся чуть позже: окно должно появиться мгновенно, а не
        # ждать системный трей.
        self.after(150, self._setup_tray)

        # Поля настроек заполняются сразу при сборке окна: интерфейс не должен
        # выглядеть пустым даже в первые миллисекунды после холодного старта.
        self._load_settings_fields()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._initial_refresh)
        self.after(700, self._tick)
        if bool(self.settings.get("simple_auto_start", True)):
            self.after(400, self._start)
        if start_in_tray:
            # Так запускает автозагрузка (``--tray``): окно не должно всплывать
            # поверх остальных программ при входе в систему.
            self.after(900, self._start_hidden)

    # ------------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        font = ("Segoe UI", 9)
        self.option_add("*Font", font)
        style.configure("Treeview", rowheight=21, font=font)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Stats.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Big.TButton", font=("Segoe UI", 10, "bold"))
        # Тема (день/ночь) применяется здесь же — до создания виджетов, чтобы
        # окно сразу рисовалось в сохранённом оформлении.
        self._apply_theme()

    # -- тема оформления (день/ночь) ---------------------------------------
    def _palette(self) -> dict:
        """Палитра выбранной темы (неизвестное значение — дневная)."""
        name = str(self.settings.get("simple_theme", DEFAULT_THEME)).lower()
        return THEMES.get(name, THEMES[DEFAULT_THEME])

    def _theme_is_night(self) -> bool:
        return (str(self.settings.get("simple_theme", DEFAULT_THEME)).lower()
                == "night")

    def _sync_theme_var(self) -> None:
        """Отметить пункт «Ночная тема» в меню по текущей настройке."""
        var = getattr(self, "theme_var", None)
        if var is not None:
            try:
                var.set(self._theme_is_night())
            except tk.TclError:
                pass

    def _style_menu(self, menu: tk.Menu) -> tk.Menu:
        """Покрасить меню под тему и запомнить его для переключений."""
        palette = getattr(self, "palette", THEMES[DEFAULT_THEME])
        try:
            menu.configure(background=palette["menu_bg"],
                           foreground=palette["menu_fg"],
                           activebackground=palette["menu_active_bg"],
                           activeforeground=palette["menu_fg"],
                           selectcolor=palette["accent"],
                           borderwidth=0)
        except tk.TclError:
            pass
        if menu not in self._menu_widgets:
            self._menu_widgets.append(menu)
        return menu

    def _register_text(self, widget: tk.Text) -> tk.Text:
        """Запомнить текстовое поле, чтобы перекрашивать его при смене темы."""
        style_text(widget, getattr(self, "palette", THEMES[DEFAULT_THEME]))
        if widget not in self._text_widgets:
            self._text_widgets.append(widget)
        return widget

    def _toggle_theme(self) -> None:
        """Быстрая смена день/ночь (меню «Вид» или Ctrl+T)."""
        night = not self._theme_is_night()
        self.settings.set("simple_theme", "night" if night else "day")
        self._apply_theme()
        log.info(t("Тема переключена: %s"), t("ночная") if night else t("дневная"))

    def _apply_theme(self) -> None:
        """Перекрасить окно под текущую тему.

        Тема — только оформление: ни проверки, ни пул она не затрагивает.
        Выбор хранится в настройке ``simple_theme`` и восстанавливается при
        запуске.
        """
        palette = self._palette()
        self.palette = palette
        style = ttk.Style(self)
        style.configure("TFrame", background=palette["panel"])
        style.configure("TLabel", background=palette["panel"],
                        foreground=palette["fg"])
        style.configure("Hint.TLabel", background=palette["panel"],
                        foreground=palette["muted"])
        style.configure("Stats.TLabel", background=palette["panel"],
                        foreground=palette["fg"])
        style.configure("TLabelframe", background=palette["panel"],
                        bordercolor=palette["border"],
                        lightcolor=palette["panel"],
                        darkcolor=palette["panel"])
        style.configure("TLabelframe.Label", background=palette["panel"],
                        foreground=palette["fg"])
        style.configure("TButton", background=palette["button_bg"],
                        foreground=palette["fg"],
                        bordercolor=palette["border"],
                        lightcolor=palette["button_bg"],
                        darkcolor=palette["button_bg"],
                        focuscolor=palette["panel"], padding=(7, 3))
        style.map("TButton",
                  background=[("active", palette["button_active"]),
                              ("pressed", palette["button_active"]),
                              ("disabled", palette["panel"])],
                  foreground=[("disabled", palette["muted"])])
        style.configure("Big.TButton", font=("Segoe UI", 10, "bold"),
                        padding=(10, 4))
        style.configure("TEntry", fieldbackground=palette["field_bg"],
                        foreground=palette["field_fg"],
                        insertcolor=palette["field_fg"],
                        bordercolor=palette["border"],
                        lightcolor=palette["border"],
                        darkcolor=palette["border"])
        style.map("TEntry",
                  fieldbackground=[("disabled", palette["panel"])],
                  foreground=[("disabled", palette["muted"])])
        style.configure("TCheckbutton", background=palette["panel"],
                        foreground=palette["fg"],
                        indicatorcolor=palette["field_bg"],
                        focuscolor=palette["panel"])
        style.map("TCheckbutton",
                  background=[("active", palette["panel"])],
                  indicatorcolor=[("selected", palette["accent"])],
                  foreground=[("disabled", palette["muted"])])
        style.configure("TCombobox", fieldbackground=palette["field_bg"],
                        background=palette["button_bg"],
                        foreground=palette["fg"],
                        arrowcolor=palette["fg"],
                        bordercolor=palette["border"],
                        lightcolor=palette["button_bg"],
                        darkcolor=palette["button_bg"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", palette["field_bg"])],
                  foreground=[("readonly", palette["fg"])])
        style.configure("TNotebook", background=palette["panel"],
                        bordercolor=palette["border"],
                        tabmargins=(2, 3, 2, 0))
        style.configure("TNotebook.Tab", background=palette["button_bg"],
                        foreground=palette["muted"], padding=(12, 4),
                        bordercolor=palette["border"])
        style.map("TNotebook.Tab",
                  background=[("selected", palette["panel"])],
                  foreground=[("selected", palette["fg"])])
        style.configure("Treeview", background=palette["tree_bg"],
                        fieldbackground=palette["tree_bg"],
                        foreground=palette["tree_fg"],
                        bordercolor=palette["border"])
        style.map("Treeview",
                  background=[("selected", palette["select_bg"])],
                  foreground=[("selected", palette["select_fg"])])
        style.configure("Treeview.Heading", background=palette["heading_bg"],
                        foreground=palette["fg"], relief="flat",
                        padding=(4, 3))
        style.map("Treeview.Heading",
                  background=[("active", palette["button_active"])])
        style.configure("TScrollbar", background=palette["button_bg"],
                        troughcolor=palette["panel"],
                        bordercolor=palette["border"],
                        arrowcolor=palette["fg"],
                        lightcolor=palette["button_bg"],
                        darkcolor=palette["button_bg"])
        style.map("TScrollbar",
                  background=[("active", palette["button_active"])])
        style.configure("TSeparator", background=palette["border"])
        style.configure("TProgressbar", background=palette["accent"],
                        troughcolor=palette["panel"])
        self._apply_theme_classic(palette)

    def _apply_theme_classic(self, palette: dict) -> None:
        """Перекрасить «классические» виджеты Tk: окно, меню, поля, таблицу.

        Они не наследуют стили ``ttk``, поэтому цвета задаются явно (или через
        опции по умолчанию — тогда новые меню и диалоги сразу попадают в тему).
        """
        try:
            self.configure(background=palette["panel"])
        except tk.TclError:
            pass
        for pattern, value in (
                ("*Menu.background", palette["menu_bg"]),
                ("*Menu.foreground", palette["menu_fg"]),
                ("*Menu.activeBackground", palette["menu_active_bg"]),
                ("*Menu.activeForeground", palette["menu_fg"]),
                ("*Menu.selectColor", palette["fg"]),
                ("*Text.background", palette["text_bg"]),
                ("*Text.foreground", palette["text_fg"]),
                ("*Text.insertBackground", palette["text_fg"]),
                ("*Text.selectBackground", palette["select_bg"]),
                ("*Text.selectForeground", palette["select_fg"]),
                ("*Listbox.background", palette["text_bg"]),
                ("*Listbox.foreground", palette["text_fg"]),
                ("*Canvas.background", palette["panel"])):
            try:
                self.option_add(pattern, value)
            except tk.TclError:
                pass
        TOOLTIP_COLORS["background"] = palette["tip_bg"]
        TOOLTIP_COLORS["foreground"] = palette["tip_fg"]
        for menu in list(getattr(self, "_menu_widgets", [])):
            self._style_menu(menu)
        for widget in list(getattr(self, "_text_widgets", [])):
            style_text(widget, palette)
        for name in ("sources_scroller", "pool_scroller", "commands_scroller",
                     "settings_scroller"):
            frame = getattr(self, name, None)
            setter = getattr(frame, "set_palette", None)
            if callable(setter):
                setter(palette["panel"])
        tree = getattr(self, "pool_tree", None)
        if tree is not None:
            for state, color in palette["states"].items():
                tree.tag_configure(state, foreground=color)
            tree.tag_configure("blocklist",
                               foreground=palette["states"]["unsupported"])
        self._sync_theme_var()

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6, 8, 4))
        bar.pack(fill="x")
        # Кнопки живут в панели с переносом строк: при сужении окна они
        # переезжают на вторую строку, а не уезжают за правый край.
        self.toolbar = AdaptiveBar(bar)
        self.toolbar.pack(side="left", fill="x", expand=True)
        self.btn_start = ttk.Button(self.toolbar, text=t("▶ Запустить"),
                                    style="Big.TButton", command=self._start)
        self.btn_pause = ttk.Button(self.toolbar, text=t("⏸ Пауза"),
                                    command=self._toggle_pause)
        self.btn_stop = ttk.Button(self.toolbar, text=t("■ Стоп"),
                                   command=self._stop)
        self.btn_round = ttk.Button(self.toolbar, text=t("Один проход"),
                                    command=self._run_once)
        self.btn_refresh = ttk.Button(self.toolbar, text=t("Обновить списки"),
                                      command=self._refresh_sources)
        self.btn_save = ttk.Button(self.toolbar, text=t("Сохранить вывод"),
                                   command=self._write_output)
        self.btn_open = ttk.Button(self.toolbar, text=t("Открыть файл"),
                                   command=self._open_output)
        self.btn_copy = ttk.Button(self.toolbar, text=t("Скопировать рабочие"),
                                   command=self._copy_working)
        for widget in (self.btn_start, self.btn_pause, self.btn_stop,
                       ttk.Separator(self.toolbar, orient="vertical"),
                       self.btn_round, self.btn_refresh,
                       ttk.Separator(self.toolbar, orient="vertical"),
                       self.btn_save, self.btn_open, self.btn_copy):
            self.toolbar.add(widget)

        # Состояние цикла всегда на виду: оно вынесено из панели кнопок, потому
        # что при узком окне строки кнопок переносятся и метка «плавала» бы.
        self.state_label = ttk.Label(bar, text=t("Остановлено"),
                                     style="Stats.TLabel")
        self.state_label.pack(side="right", padx=(8, 0))
        ToolTip(self.btn_round, t("Обработать по одной порции каждого этапа прямо "
                                "сейчас, не дожидаясь следующего круга (F5)."))
        ToolTip(self.btn_refresh, t("Скачать все включённые подписки немедленно."))
        ToolTip(self.btn_save, t("Перезаписать выходные файлы: список ссылок "
                               "и/или групповую конфигурацию для v2rayN."))
        ToolTip(self.btn_stop, t("Остановить автоматический цикл и сохранить пул."))
        ToolTip(self.btn_copy,
                t("Скопировать все рабочие ссылки в буфер обмена.\n"
                "В v2rayN: «Серверы» → «Добавить сервер(а) из буфера обмена» "
                "(или Ctrl+V) — появится отдельная запись на каждый сервер, "
                "а не одна группа."))

    def _build_statusbar(self) -> None:
        # Строка состояния тоже переносится: счётчики, этап и пути к файлам
        # вместе шире окна, и крайняя надпись раньше обрезалась.
        bar = ttk.Frame(self, padding=(8, 2, 8, 6))
        bar.pack(fill="x", side="bottom")
        self.status_bar = AdaptiveBar(bar, gap=10)
        self.status_bar.pack(fill="x")
        self.status_counts = ttk.Label(self.status_bar, text="")
        self.status_bar.add(self.status_counts)
        self.status_stage = ttk.Label(self.status_bar, text="",
                                      style="Stats.TLabel")
        self.status_bar.add(self.status_stage)
        self.status_network = ttk.Label(self.status_bar, text="",
                                        style="Stats.TLabel")
        self.status_bar.add(self.status_network)
        self.status_file = ttk.Label(self.status_bar, text="",
                                     style="Hint.TLabel")
        self.status_bar.add(self.status_file)

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        self.tab_sources = ttk.Frame(self.notebook, padding=8)
        self.tab_pool = ttk.Frame(self.notebook, padding=8)
        self.tab_commands = ttk.Frame(self.notebook, padding=8)
        self.tab_settings = ttk.Frame(self.notebook, padding=8)
        self.tab_log = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_sources, text=t("Списки и вставка"))
        self.notebook.add(self.tab_pool, text=t("Серверы"))
        self.notebook.add(self.tab_commands, text=t("Внешние команды"))
        self.notebook.add(self.tab_settings, text=t("Настройки"))
        self.notebook.add(self.tab_log, text=t("Журнал"))
        self._build_sources_tab(self.tab_sources)
        self._build_pool_tab(self.tab_pool)
        self._build_commands_tab(self.tab_commands)
        self._build_settings_tab(self.tab_settings)
        self._build_log_tab(self.tab_log)

    def _bind_hotkeys(self) -> None:
        # Ctrl+V в любом месте окна вставляет список конфигураций; если фокус
        # стоит в поле ввода, работает обычная вставка Tk.
        self.bind_all("<Control-v>", self._on_ctrl_v)
        self.bind_all("<Control-V>", self._on_ctrl_v)
        # Типовые клавиши ловим по коду клавиши: при русской раскладке Ctrl+V,
        # Ctrl+C и подобные приходят с другими keysym, и обычные привязки молчат
        # (см. app/gui/common.py).  Del — удалить строку, Shift+Del — в чёрный
        # список, Ctrl+A — выделить всё, Insert — включить/выключить, F2 —
        # изменить, F5 — один проход.
        bind_hotkeys(self, {
            "paste": self._guard_modal(self._on_ctrl_v),
            "copy": self._guard_modal(self._hotkey_copy),
            "select_all": self._guard_modal(self._hotkey_select_all),
            "delete": self._guard_modal(self._hotkey_delete),
            "insert": self._guard_modal(self._hotkey_toggle),
            "rename": self._guard_modal(self._hotkey_rename),
            "refresh": self._guard_modal(lambda _event: self._run_once()),
            "help": self._guard_modal(lambda _event: self._show_hotkeys_help()),
            "theme": self._guard_modal(lambda _event: self._toggle_theme()),
        })

    # -- строка меню ------------------------------------------------------
    def _build_menu(self) -> None:
        """Классическое меню окна: Файл / Правка / Вид / Справка.

        Меню повторяет кнопки и контекстные действия — к ним привычно
        добираться и мышью, и с клавиатуры (Alt+Ф, Alt+П, Alt+В, Alt+С).

        Штатную строку меню Windows Tk рисует светлой независимо от заданных
        цветов (проверено снимками экрана), поэтому в ночной теме она остаётся
        системной — как и окна справки и диалоги.  Перекрасить её можно только
        своей полосой кнопок, а это лишний код и расхождение с системным видом;
        выпадающие же меню цвета слушают и темнеют вместе с окном.
        """
        menubar = tk.Menu(self, tearoff=0)
        self._style_menu(menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label=t("Добавить подписку (URL)…"),
                              command=self._add_url_source)
        file_menu.add_command(label=t("Добавить файл-источник…"),
                              command=self._add_file_source)
        file_menu.add_command(label=t("Вставить список из буфера"),
                              command=self._add_clipboard_source)
        file_menu.add_separator()
        file_menu.add_command(label=t("Обновить подписки сейчас"),
                              command=self._refresh_sources)
        file_menu.add_separator()
        file_menu.add_command(label=t("Сохранить выходные файлы"),
                              command=self._write_output)
        file_menu.add_command(label=t("Открыть выходной файл"),
                              command=self._open_output)
        file_menu.add_command(label=t("Скопировать рабочие ссылки"),
                              command=self._copy_working)
        file_menu.add_separator()
        file_menu.add_command(label=t("Выход"), accelerator="Alt+F4",
                              command=lambda: self._quit(ask=True))
        menubar.add_cascade(label=t("Файл"), menu=file_menu)
        self._style_menu(file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label=t("Проверить выделенные сейчас"),
                              command=self._probe_selected)
        edit_menu.add_command(label=t("Сбросить в «новые»"),
                              command=self._reset_selected)
        edit_menu.add_command(label=t("Копировать ссылки выделенных"),
                              accelerator="Ctrl+C",
                              command=self._copy_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label=t("Удалить выделенные"), accelerator="Del",
                              command=self._delete_selected)
        edit_menu.add_command(label=t("В чёрный список (не проверять)"),
                              accelerator="Shift+Del",
                              command=self._blacklist_selected)
        edit_menu.add_command(label=t("Очистить чёрный список"),
                              command=self._clear_blacklist)
        edit_menu.add_separator()
        edit_menu.add_command(
            label=t("Настройки…"),
            command=lambda: self.notebook.select(str(self.tab_settings)))
        menubar.add_cascade(label=t("Правка"), menu=edit_menu)
        self._style_menu(edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self.theme_var = tk.BooleanVar(value=self._theme_is_night())
        view_menu.add_checkbutton(label=t("Ночная тема"), accelerator="Ctrl+T",
                                  variable=self.theme_var,
                                  command=self._toggle_theme)
        view_menu.add_separator()
        view_menu.add_command(label=t("Обновить таблицу серверов"),
                              accelerator="F5",
                              command=self._refresh_pool_now)
        view_menu.add_command(label=t("Один проход этапов"),
                              command=self._run_once)
        view_menu.add_separator()
        for index, tab in enumerate(self.notebook.tabs()):
            title = self.notebook.tab(tab, "text")
            view_menu.add_command(
                label=title,
                command=lambda i=index: self.notebook.select(
                    str(self.notebook.tabs()[i])))
        menubar.add_cascade(label=t("Вид"), menu=view_menu)
        self._style_menu(view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label=t("Справка по внешним командам"),
                              command=self._show_commands_help)
        help_menu.add_command(label=t("Горячие клавиши"), accelerator="F1",
                              command=self._show_hotkeys_help)
        help_menu.add_separator()
        help_menu.add_command(label=t("О программе"), command=self._show_about)
        menubar.add_cascade(label=t("Справка"), menu=help_menu)
        self._style_menu(help_menu)

        self.menubar = menubar
        self.configure(menu=menubar)


    def _show_hotkeys_help(self) -> None:
        """Краткая справка по клавишам (меню «Справка», F1)."""
        messagebox.showinfo(
            t("Горячие клавиши"),
            t("Ctrl+V — вставить список конфигураций (в любом месте окна)\n"
            "Ctrl+C — скопировать ссылки выделенных серверов\n"
            "Ctrl+A — выделить всё в таблице\n"
            "Del — удалить выделенные записи\n"
            "Shift+Del — внести адреса в постоянный чёрный список\n"
            "Insert — включить или выключить источник или пресет\n"
            "F2 — изменить выбранный пресет\n"
            "F5 — один проход по этапам проверки\n"
            "Ctrl+T — быстрая смена день/ночь\n\n"
            "Те же действия есть в меню «Файл», «Правка» и «Вид»."),
            parent=self)

    def _show_about(self) -> None:
        """Коротко о программе, путях данных и выходном файле."""
        output_path = str(self.settings.get("simple_output_path") or "")
        messagebox.showinfo(
            t("О программе"),
            t('{}\n\nАвтоматический цикл: подписки → пинг → ядро → контрольный трафик.\nСписок рабочих серверов постоянно поддерживается в актуальном состоянии.\n\nНастройки и пул: {}\nВыходной файл: {}\n\nСправка по внешним командам — в меню «Справка».').format(APP_TITLE, os.path.dirname(SIMPLE_POOL_PATH), output_path),
            parent=self)

    def _guard_modal(self, handler):
        """Не трогать главное окно, пока открыт модальный диалог.

        Диалог берёт grab, и клавиши (в том числе Del и F5) адресованы ему.
        """
        def run(event):
            if self.grab_current() is not None:
                return "break"
            return handler(event)
        return run

    # -- горячие клавиши -------------------------------------------------
    def _focus_area(self, widget=None) -> str:
        """С какой таблицей работают клавиши: pool / blacklist / commands / sources.

        Ориентируемся на **активную вкладку**, а не на виджет с фокусом: Tk
        оставляет фокус на прежней таблице после переключения вкладки, и тогда
        Del срабатывал бы не там, где смотрит пользователь.
        """
        try:
            current = str(self.notebook.select())
        except tk.TclError:
            return ""
        areas = {str(self.tab_pool): "pool",
                 str(self.tab_commands): "commands",
                 str(self.tab_sources): "sources"}
        area = areas.get(current, "")
        if area == "pool" and self._in_blacklist_mode():
            return "blacklist"
        return area

    def _hotkey_delete(self, event) -> str:
        """Del: удалить выбранное в активной таблице."""
        area = self._focus_area()
        if area == "blacklist":
            self._unblock_selected()
        elif area == "pool":
            if has_shift(event):
                self._blacklist_selected()
            else:
                self._delete_selected()
        elif area == "commands":
            self._remove_preset()
        elif area == "sources":
            self._remove_source()
        return "break"

    def _hotkey_copy(self, event) -> str:
        """Ctrl+C: ссылки на серверы или текст панели вывода."""
        area = self._focus_area()
        if area == "pool":
            self._copy_selected()
        elif area == "commands":
            self._copy_commands_output()
        return "break"

    def _hotkey_select_all(self, event) -> str:
        """Ctrl+A: выделить всё в активной таблице."""
        area = self._focus_area()
        if area in ("pool", "blacklist"):
            self.pool_tree.selection_set(self.pool_tree.get_children())
        elif area == "commands":
            self.commands_tree.selection_set(
                self.commands_tree.get_children())
        elif area == "sources":
            self.sources_tree.selection_set(
                self.sources_tree.get_children())
        return "break"

    def _hotkey_toggle(self, event) -> str:
        """Insert: включить/выключить выбранное."""
        area = self._focus_area()
        if area == "commands":
            self._toggle_preset()
        elif area == "sources":
            self._toggle_source()
        return "break"

    def _hotkey_rename(self, event) -> str:
        """F2: изменить выбранный пресет."""
        if self._focus_area() == "commands":
            self._edit_preset()
        return "break"

    def _wire_supervisor(self) -> None:
        self.supervisor.on_stats(lambda stats: self._ui(self._apply_stats, stats))
        self.supervisor.on_output(
            lambda path, count: self._ui(self._on_output_written, path, count))
        self._log_handler = self._on_log_record
        subscribe_logs(self._log_handler)

    def _ui(self, func, *args) -> None:
        """Отложить действие для главного потока Tk (безопасно из любого потока)."""
        if self._closing:
            return
        try:
            self._events.put((func, args))
        except Exception:
            pass

    def _drain_events(self, limit: int = 400) -> None:
        for _ in range(limit):
            try:
                func, args = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                func(*args)
            except tk.TclError:
                return
            except Exception:
                continue

    def _initial_refresh(self) -> None:
        self._load_settings_fields()
        self._reload_sources()
        self._reload_pool()
        self._apply_stats(self.supervisor.refresh_stats())
        self._background(self._detect_cores)

    # -- ядра ------------------------------------------------------------
    def _detect_cores(self) -> None:
        """Найти ядра (в фоне: при включённой автозагрузке они скачиваются)."""
        try:
            xray, singbox = self.supervisor.prober.core_paths()
        except Exception as exc:  # noqa: BLE001
            self._ui(self._show_cores, "", "",
                     t('Не удалось определить ядро: {}').format(exc))
            return
        self._ui(self._show_cores, xray or "", singbox or "", "")

    def _show_cores(self, xray: str, singbox: str, error: str) -> None:
        if not hasattr(self, "cores_info"):
            return
        if error:
            self.cores_info.configure(text=error)
            return
        parts = []
        if xray:
            parts.append(f"Xray: {xray}")
        if singbox:
            parts.append(f"sing-box: {singbox}")
        if not parts:
            self.cores_info.configure(
                text=t("Ядро Xray / sing-box не найдено, поэтому этапы 2 и 3 "
                     "(соединение и трафик) выполняться не будут. Укажите путь "
                     "выше или нажмите «Скачать ядро Xray»."))
            return
        self.cores_info.configure(
            text=t("Ядро найдено, этапы 2 и 3 доступны. ") + " · ".join(parts))

    def _download_core(self, name: str) -> None:
        log.info(t("Загрузка ядра %s…"), name)
        key = "xray_path" if name == "xray" else "singbox_path"

        def worker() -> None:
            try:
                path = core_manager.resolve_core(
                    name, configured_path=str(self.settings.get(key, "")),
                    auto_download=True)
            except Exception as exc:  # noqa: BLE001
                log.error(t("Не удалось загрузить ядро %s: %s"), name, exc)
                path = None
            if path:
                self.settings.set(key, path)
                log.info(t("Ядро %s готово: %s"), name, path)
            else:
                log.error(t("Ядро %s установить не удалось."), name)
            self.supervisor.prober.reset_cores()
            self._detect_cores()

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        try:
            self._drain_events()
            self._refresh_pool_if_changed()
            self._refresh_source_status()
            self._refresh_commands_if_changed()
            # Счётчики пересчитываются от пула: они двигаются даже во время
            # длинного круга, а не только после его завершения.  Дорогая часть
            # (совместимость с ядром Xray) здесь не нужна: её обновляет запись
            # выходных файлов.
            self._apply_stats(self.supervisor.refresh_stats(heavy=False))
        except Exception:
            pass
        if not self._closing:
            self.after(400, self._tick)

    def _refresh_pool_if_changed(self) -> None:
        if self.pool.revision == self._pool_revision:
            return
        # Во время круга порядок строк переставляем редко, а когда работа
        # остановлена — сразу: так строки не прыгают под курсором, а выделение и
        # прокрутка не сбрасываются.
        reorder = (not self._stats.running
                   or time.time() - self._last_order_at >= self.ORDER_INTERVAL)
        self._reload_pool(reorder=reorder)

    def _apply_stats(self, stats: SimpleStats) -> None:
        self._stats = stats
        if stats.running:
            mode = t("Пауза") if stats.paused else t("Работает")
        else:
            mode = t("Остановлено")
        self.state_label.configure(
            text=t('{} · круг {}').format(mode, fmt_int(stats.round_index)))
        self.status_counts.configure(text=(
            t('Всего {} · Рабочих {} · Ядро {} · Пинг {} · Новые {} · Нерабочих {} · Не поддерж. {} · ЧС адресов {} · Исключено {} · Удалено {}').format(fmt_int(stats.total), fmt_int(stats.working), fmt_int(stats.connected), fmt_int(stats.reachable), fmt_int(stats.new), fmt_int(stats.failed), fmt_int(stats.unsupported), fmt_int(stats.blocked_addresses), fmt_int(stats.blocked), fmt_int(stats.deleted))))
        self.status_stage.configure(text=t('Этап: {}').format(stats.stage or 'ожидание'))
        self.status_network.configure(
            text="" if stats.network_online
            else t("нет сети — проверки приостановлены"))
        self.status_file.configure(text=self._output_summary(stats))
        self.btn_start.state(["disabled"] if stats.running else ["!disabled"])
        self.btn_pause.state(["!disabled"] if stats.running else ["disabled"])
        self.btn_stop.state(["!disabled"] if stats.running else ["disabled"])
        self.btn_pause.configure(
            text=t("▶ Продолжить") if stats.paused else t("⏸ Пауза"))
        self._update_tray_tooltip(stats)

    @staticmethod
    def _output_summary(stats: SimpleStats) -> str:
        """Краткая строка о том, какие файлы сейчас ведутся."""
        txt = (f"{os.path.basename(stats.output_path or SIMPLE_OUTPUT_PATH)} "
               f"({fmt_int(stats.output_count)})")
        json_file = (f"{os.path.basename(stats.v2rayn_path or SIMPLE_V2RAYN_PATH)} "
                     f"({fmt_int(stats.v2rayn_count)})")
        if stats.output_format == OUTPUT_V2RAYN:
            return f"v2rayN: {json_file}"
        if stats.output_format == OUTPUT_BOTH:
            return t('Файлы: {} + {}').format(txt, json_file)
        return t('Файл: {}').format(txt)

    def _on_log_record(self, message: str) -> None:
        self._ui(self._append_log_line, message)

    def _append_log_line(self, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self._log_lines += 1
            limit = int(self.settings.get("max_log_lines", 2000) or 2000)
            if self._log_lines > limit + 200:
                self.log_text.delete("1.0", f"{self._log_lines - limit}.0")
                self._log_lines = limit
            if self.log_scroll_var.get():
                self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _on_output_written(self, path: str, count: int) -> None:
        self._apply_stats(self.supervisor.snapshot())

    # ------------------------------------------------------------------
    # Вкладка «Списки и вставка»
    # ------------------------------------------------------------------
    def _build_sources_tab(self, parent: ttk.Frame) -> None:
        # Вкладка целиком прокручивается: список источников и большое поле
        # вставки вместе выше низкого окна, и без прокрутки нижние кнопки
        # («Добавить в пул») уезжали за границу окна.
        scroller = ScrollableFrame(parent, height=380)
        scroller.pack(fill="both", expand=True)
        self.sources_scroller = scroller
        body = scroller.inner

        top = ttk.LabelFrame(body, text=t("Подписки и файлы со списками"),
                             padding=8)
        top.pack(fill="x")

        row = ttk.Frame(top)
        row.pack(fill="x")
        ttk.Label(row, text=t("Ссылка или путь:")).pack(side="left")
        self.source_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.source_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _event: self._add_url_source())

        add_bar = AdaptiveBar(top)
        add_bar.pack(fill="x", pady=(4, 0))
        add_bar.add(ttk.Button(add_bar, text=t("Добавить URL"),
                               command=self._add_url_source))
        add_bar.add(ttk.Button(add_bar, text=t("Добавить файл…"),
                               command=self._add_file_source))
        add_bar.add(ttk.Button(add_bar, text=t("Из буфера"),
                               command=self._add_clipboard_source))

        container, self.sources_tree = make_treeview(top, [
            ("enabled", t("Вкл"), 40, "center"),
            ("kind", t("Тип"), 60, "w"),
            ("value", t("Значение"), 520, "w"),
            ("count", t("Добавлено"), 90, "e"),
            ("status", t("Статус"), 200, "w"),
        ], height=5)
        container.pack(fill="x", pady=(6, 6))
        self._bind_context_menu(self.sources_tree, self._sources_context_menu)

        buttons = AdaptiveBar(top)
        buttons.pack(fill="x")
        toggle_source = ttk.Button(buttons, text=t("Включить/выключить"),
                                   command=self._toggle_source)
        buttons.add(toggle_source)
        ToolTip(toggle_source, t("Insert — включить или выключить источник."))
        remove_source = ttk.Button(buttons, text=t("Удалить источник"),
                                   command=self._remove_source)
        buttons.add(remove_source)
        ToolTip(remove_source, t("Del — удалить источник из списка."))
        buttons.add(ttk.Button(buttons, text=t("Обновить сейчас"),
                               command=self._refresh_sources))
        self.sources_info = ttk.Label(top, text="", style="Hint.TLabel")
        self.sources_info.pack(anchor="w", pady=(4, 0))
        adaptive_wraplength(self.sources_info, top, margin=24, minimum=200)

        paste = ttk.LabelFrame(body, text=t("Вставка по Ctrl+V (с разбором)"),
                               padding=8)
        paste.pack(fill="x", pady=(8, 0))
        paste_hint = ttk.Label(
            paste,
            text=(t("Ctrl+V работает из любого места окна. Приложение само решает,"
                  " куда вставить: ссылка на подписку или путь к файлу со "
                  "списком уйдёт в источники выше, конфигурации (vless://, "
                  "vmess://, trojan://, ss:// ...) — сразу в список серверов. "
                  "Текст, который не удалось разобрать, остаётся в этом поле.")),
            style="Hint.TLabel", justify="left")
        paste_hint.pack(anchor="w")
        adaptive_wraplength(paste_hint, paste, margin=24, minimum=200)

        box = ttk.Frame(paste)
        box.pack(fill="both", expand=True, pady=(6, 6))
        self.paste_text = self._register_text(
            tk.Text(box, height=7, wrap="none", undo=True))
        scroll = ttk.Scrollbar(box, orient="vertical",
                               command=self.paste_text.yview)
        self.paste_text.configure(yscrollcommand=scroll.set)
        self.paste_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._bind_text_menu(self.paste_text, (
            (t("Вставить из буфера (с разбором)"), self._paste_to_pool_text),
            (t("Добавить в пул"), self._add_pasted),
            (t("Очистить поле"), self._clear_paste_text),
        ))

        actions = AdaptiveBar(paste)
        actions.pack(fill="x")
        actions.add(ttk.Button(actions, text=t("Добавить в пул"),
                               style="Big.TButton", command=self._add_pasted))
        actions.add(ttk.Button(actions, text=t("Вставить из буфера (с разбором)"),
                               command=self._paste_to_pool_text))
        actions.add(ttk.Button(actions, text=t("Очистить"),
                               command=self._clear_paste_text))
        self.paste_info = ttk.Label(paste, text="", style="Hint.TLabel")
        self.paste_info.pack(anchor="w", pady=(4, 0))
        adaptive_wraplength(self.paste_info, paste, margin=24, minimum=200)

        scroller.bind_wheel()

    # -- действия вкладки «Списки» ---------------------------------------
    def _background(self, func, *args) -> None:
        """Выполнить длительную операцию вне потока интерфейса."""
        def runner() -> None:
            try:
                func(*args)
            except Exception as exc:  # noqa: BLE001
                log.error(t("Фоновая операция: %s"), exc)
                self._ui(self._append_log_line, t('Ошибка: {}').format(exc))
        threading.Thread(target=runner, daemon=True).start()

    def _reload_sources(self, sources: Optional[List[Dict]] = None) -> None:
        if sources is None:
            sources = self.supervisor.sources()
        self._sources_signature = self._sources_signature_of(sources)
        try:
            self.sources_tree.delete(*self.sources_tree.get_children())
            for index, source in enumerate(sources):
                kind = str(source.get("kind") or "url")
                self.sources_tree.insert("", "end", iid=str(index), values=(
                    "✔" if source.get("enabled", True) else "✘",
                    t("Файл") if kind == "file" else "URL",
                    str(source.get("value") or ""),
                    fmt_int(source.get("last_count") or 0),
                    status_text(source.get("last_status")),
                ))
        except tk.TclError:
            return
        self.sources_info.configure(text=t('Источников: {}').format(len(sources)))

    @staticmethod
    def _sources_signature_of(sources: List[Dict]) -> Tuple:
        """Дешёвая подпись списка источников для сравнения (без json.dumps)."""
        return tuple(
            (str(source.get("value") or ""),
             bool(source.get("enabled", True)),
             int(source.get("last_count") or 0),
             str(source.get("last_status") or ""))
            for source in sources
        )

    def _refresh_source_status(self) -> None:
        sources = self.supervisor.sources()
        if self._sources_signature_of(sources) == self._sources_signature:
            return
        self._reload_sources(sources)

    def _selected_source_index(self) -> Optional[int]:
        selection = self.sources_tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except (TypeError, ValueError):
            return None

    def _add_url_source(self) -> None:
        value = self.source_var.get().strip()
        if not value:
            messagebox.showinfo(t("Источник"), t("Введите ссылку на подписку."),
                                parent=self)
            return
        # В поле могли вставить конфигурацию или путь к файлу со списком —
        # разберём так же, как буфер обмена, вместо «это не URL».
        plan = classify_clipboard(value)
        if plan.has_configs or any(kind == "file" for kind, _ in plan.sources):
            self.source_var.set("")
            self._smart_paste(value)
            return
        if not self.supervisor.add_source("url", value):
            messagebox.showinfo(t("Источник"), t("Такой источник уже добавлен."),
                                parent=self)
            return
        self.source_var.set("")
        self._reload_sources()
        self._background(self.supervisor.refresh_sources_now)

    def _add_file_source(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=t("Файл со списком конфигураций"),
            filetypes=[(t("Текстовые файлы"), "*.txt"), (t("Все файлы"), "*.*")])
        if not path:
            return
        if not self.supervisor.add_source("file", path):
            messagebox.showinfo(t("Источник"), t("Такой источник уже добавлен."),
                                parent=self)
            return
        self._reload_sources()
        self._background(self.supervisor.refresh_sources_now)

    def _add_clipboard_source(self) -> None:
        text = self._clipboard_text()
        if not text:
            messagebox.showinfo(t("Источник"), t("Буфер обмена пуст."), parent=self)
            return
        # Тот же разбор, что и у Ctrl+V: ссылка/файл станут источником,
        # конфигурации уйдут в пул — пользователю не нужно выбирать кнопку.
        if self._smart_paste(text):
            return
        messagebox.showinfo(
            t("Источник"),
            t("В буфере нет ни ссылки на подписку, ни конфигураций."),
            parent=self)

    def _toggle_source(self) -> None:
        index = self._selected_source_index()
        if index is None:
            return
        sources = self.supervisor.sources()
        if index >= len(sources):
            return
        enabled = not bool(sources[index].get("enabled", True))
        self.supervisor.set_source_enabled(index, enabled)
        self._reload_sources()

    def _remove_source(self) -> None:
        index = self._selected_source_index()
        if index is None:
            return
        if not messagebox.askyesno(t("Источник"), t("Удалить выбранный источник?"),
                                   parent=self):
            return
        self.supervisor.remove_source(index)
        self._reload_sources()

    # -- вставка конфигураций --------------------------------------------
    def _clipboard_text(self) -> str:
        """Текст из буфера обмена (пустая строка, если он недоступен)."""
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return ""
        return (text or "").strip()

    def _smart_paste(self, text: str) -> bool:
        """Разложить буфер по назначению: конфигурации в пул, ссылки в источники.

        Возвращает ``False``, если разобрать ничего не удалось: тогда вызывающий
        код решает, что делать (например, показать текст в поле вставки).
        """
        plan = classify_clipboard(text)
        if plan.is_empty:
            return False
        notes: List[str] = []
        if plan.sources:
            added = 0
            for kind, value in plan.sources:
                if self.supervisor.add_source(kind, value):
                    added += 1
            if added:
                notes.append(t('в источники добавлено: {}').format(added))
                self._reload_sources()
                self._background(self.supervisor.refresh_sources_now)
            else:
                notes.append(t("такие источники уже есть"))
        if plan.has_configs:
            counters = self.supervisor.import_text(plan.configs, t("вставка"))
            notes.append(t('конфигураций добавлено: {} (повторно {}, битых {})').format(counters['added'], counters['duplicate'], counters['invalid']))
            if counters["added"]:
                self._reload_pool()
        if plan.unknown:
            notes.append(t('нераспознанных строк: {}').format(len(plan.unknown)))
        note = "; ".join(notes)
        self.paste_info.configure(text=note)
        log.info(t("Вставка: %s"), note)
        if plan.has_configs and not self.supervisor.running:
            log.info(t("Нажмите «Запустить» (или F5), чтобы начать проверку "
                     "добавленных конфигураций."))
        return True

    def _paste_to_pool_text(self) -> None:
        """Вставить из буфера с разбором (кнопка и Ctrl+V из любого места)."""
        text = self._clipboard_text()
        if not text:
            return
        if self._smart_paste(text):
            return
        # Ничего не распознали — показываем текст в поле: его можно поправить
        # вручную и отправить в пул кнопкой «Добавить в пул».
        self.paste_text.insert(
            "end", text if text.endswith("\n") else text + "\n")
        self.paste_text.see("end")
        self.notebook.select(self.tab_sources)

    def _clear_paste_text(self) -> None:
        self.paste_text.delete("1.0", "end")
        self.paste_info.configure(text="")

    def _on_ctrl_v(self, event=None):
        """Ctrl+V в любом месте окна вставляет список конфигураций."""
        widget = self.focus_get()
        if isinstance(widget, (tk.Entry, tk.Text)):
            return None      # в поле ввода работает обычная вставка
        self._paste_to_pool_text()
        return "break"

    def _add_pasted(self) -> None:
        text = self.paste_text.get("1.0", "end")
        if not text.strip():
            messagebox.showinfo(t("Вставка"), t("Вставьте ссылки в поле."),
                                parent=self)
            return
        self._import_text(text)

    def _import_text(self, text: str) -> None:
        counters = self.supervisor.import_text(text, t("вставка"))
        note = (t('получено {}, добавлено {}, повторно {}, отфильтровано {}, исключено ранее {}, битых {}').format(counters['received'], counters['added'], counters['duplicate'], counters['filtered'], counters['blocked'], counters['invalid']))
        self.paste_info.configure(text=note)
        log.info(t("Вставка: %s"), note)
        if counters["added"]:
            self.paste_text.delete("1.0", "end")
            self._reload_pool()
            if not self.supervisor.running:
                log.info(t("Нажмите «Запустить» (или F5), чтобы начать проверку "
                         "добавленных конфигураций."))

    # ------------------------------------------------------------------
    # Вкладка «Серверы»
    # ------------------------------------------------------------------
    def _build_pool_tab(self, parent: ttk.Frame) -> None:
        # Вкладка прокручивается: при низком окне нижние панели кнопок иначе
        # обрезались. Когда места хватает, содержимое растягивается на всю
        # высоту (см. ScrollableFrame._sync_window_size) — таблица серверов
        # по-прежнему занимает всё свободное место.
        scroller = ScrollableFrame(parent, height=420)
        scroller.pack(fill="both", expand=True)
        self.pool_scroller = scroller
        body = scroller.inner

        # Панель фильтра собирается в AdaptiveBar: при узком окне элементы
        # переносятся на следующую строку вместо того, чтобы уехать за край.
        top = AdaptiveBar(body, gap=6)
        top.pack(fill="x")

        top.add(ttk.Label(top, text=t("Состояние:")))
        self.filter_var = tk.StringVar(value=FILTER_NONE)
        combo = ttk.Combobox(
            top, textvariable=self.filter_var, state="readonly", width=34,
            values=[FILTER_NONE] + [state.label for state in STATE_ORDER]
                   + [FILTER_BLACKLIST])
        top.add(combo)
        combo.bind("<<ComboboxSelected>>",
                   lambda _event: self._reload_pool(reorder=True))

        top.add(ttk.Label(top, text=t("Поиск:")))
        self.search_var = tk.StringVar()
        search = ttk.Entry(top, textvariable=self.search_var, width=30)
        top.add(search)
        # Поиск идёт с задержкой (см. SEARCH_DELAY_MS): фильтровать и
        # сортировать весь пул на каждое нажатие клавиши слишком дорого.
        search.bind("<KeyRelease>", self._on_search_changed)

        refresh_button = ttk.Button(top, text=t("Обновить"),
                                    command=self._refresh_pool_now)
        top.add(refresh_button)
        ToolTip(refresh_button,
                t("Перечитать пул и пересортировать таблицу (F5 — один проход)."))
        # Информационная строка вынесена под панель: она длинная и в потоке
        # элементов мешала бы раскладке.
        self.pool_info = ttk.Label(body, text="", style="Hint.TLabel")
        self.pool_info.pack(anchor="w", pady=(4, 0))
        # Таблица — живые данные из памяти; пул на диске нужен только для
        # восстановления, поэтому его интервал записи сюда не влияет.
        ToolTip(self.pool_info,
                t("Таблица берёт серверы и их статусы из памяти работающей "
                "программы: задержки, счётчики и очередь проверок всегда "
                "актуальны.\n"
                "Файл pool.json рядом с программой — резервный снимок на случай "
                "перезагрузки, сбоя или закрытия программы.  Как часто он "
                "перезаписывается, задаётся в настройках («Писать пул "
                "не чаще, с»)."))

        container, self.pool_tree = make_treeview(body, [
            ("state", t("Состояние"), 170, "w"),
            ("protocol", t("Протокол"), 100, "w"),
            ("endpoint", t("Адрес"), 230, "w"),
            ("latency", t("Задержка"), 90, "e"),
            ("fails", t("Пинг / Ядро / Трафик"), 150, "center"),
            ("checked", t("Проверен"), 120, "w"),
            ("error", t("Ошибка / заметка"), 340, "w"),
        ], height=10)
        container.pack(fill="both", expand=True, pady=(8, 8))
        # Цвета состояний настраиваются один раз: перерисовка их не меняет.
        for state, color in STATE_COLORS.items():
            self.pool_tree.tag_configure(state, foreground=color)
        self.pool_tree.tag_configure("blocklist", foreground="#b02a37")
        self._bind_context_menu(self.pool_tree, self._pool_context_menu)

        buttons = AdaptiveBar(body, gap=6)
        buttons.pack(fill="x")
        buttons.add(ttk.Button(buttons, text=t("Проверить сейчас"),
                               command=self._probe_selected))
        buttons.add(ttk.Button(buttons, text=t("Сбросить в «новые»"),
                               command=self._reset_selected))
        copy_button = ttk.Button(buttons, text=t("Копировать ссылки"),
                                 command=self._copy_selected)
        buttons.add(copy_button)
        ToolTip(copy_button, t("Ctrl+C — скопировать ссылки выделенных серверов."))
        delete_button = ttk.Button(buttons, text=t("Удалить выбранные"),
                                   command=self._delete_selected)
        buttons.add(delete_button)
        ToolTip(delete_button,
                t("Del — удалить выделенные записи из пула.\n"
                "Адреса в чёрный список не заносятся: сервер сможет вернуться "
                "из подписки."))
        blacklist_button = ttk.Button(buttons,
                                      text=t("В чёрный список (не проверять)"),
                                      command=self._blacklist_selected)
        buttons.add(blacklist_button)
        ToolTip(blacklist_button,
                t("Shift+Del — внести адреса выделенных серверов в постоянный "
                "чёрный список:\nих больше не добавят подписки и они никогда не "
                "будут проверяться."))
        # Подсказка о клавишах идёт отдельной строкой с переносом по ширине:
        # раньше она стояла в одной строке с кнопками и при узком окне уезжала
        # за правый край.
        keys_hint = ttk.Label(body, style="Hint.TLabel", justify="left",
                              text=(t("Клавиши: Del — удалить, Shift+Del — в "
                                    "чёрный список, Ctrl+A — выделить всё, "
                                    "F5 — один проход, ПКМ — меню действий")))
        keys_hint.pack(anchor="w", pady=(4, 0))
        adaptive_wraplength(keys_hint, body, margin=24, minimum=200)

        right = AdaptiveBar(body, gap=6)
        right.pack(fill="x", pady=(6, 0))
        right.add(ttk.Button(right, text=t("Удалить по фильтру"),
                             command=self._delete_filtered))
        unblock_button = ttk.Button(right, text=t("Убрать из чёрного списка"),
                                    command=self._unblock_selected)
        right.add(unblock_button)
        ToolTip(unblock_button,
                t("Del — разрешить адрес снова: в фильтре «Чёрный список "
                "адресов» выберите строки и нажмите Del."))
        right.add(ttk.Button(right, text=t("Очистить чёрный список"),
                             command=self._clear_blacklist))
        right.add(ttk.Button(right, text=t("Удалить все конфигурации"),
                             command=self._clear_all))
        # Ручное пополнение чёрного списка: ссылка, «host:port» или просто IP
        # (тогда блокируются все порты адреса).
        manual = ttk.Frame(body)
        manual.pack(fill="x", pady=(6, 0))
        ttk.Label(manual, text=t("Добавить в чёрный список:")).pack(side="left")
        self.blacklist_input_var = tk.StringVar()
        manual_entry = ttk.Entry(manual, textvariable=self.blacklist_input_var,
                                 width=34)
        manual_entry.pack(side="left", padx=(6, 0))
        manual_button = ttk.Button(manual, text=t("Добавить"),
                                   command=self._blacklist_manual)
        manual_button.pack(side="left", padx=(6, 0))
        manual_entry.bind("<Return>", lambda _event: self._blacklist_manual())
        ToolTip(manual_entry,
                t("Ссылка на сервер, «host:port» или просто IP — тогда "
                "блокируются все порты этого адреса (удобно для заведомо "
                "«мусорных» узлов).  Несколько значений можно перечислить "
                "через пробел, запятую или с новой строки."))
        self.blacklist_info = ttk.Label(body, text="", style="Hint.TLabel")
        self.blacklist_info.pack(anchor="w", pady=(4, 0))

        scroller.bind_wheel()

    # -- действия вкладки «Серверы» --------------------------------------
    def _in_blacklist_mode(self) -> bool:
        """Открыт ли фильтр постоянного чёрного списка адресов."""
        return self.filter_var.get() == FILTER_BLACKLIST

    def _on_search_changed(self, _event=None) -> None:
        """Поиск с задержкой: пересчёт всего пула на каждую букву дорог."""
        self._search_debounce.call(self._apply_search)

    def _apply_search(self) -> None:
        self._search_needle = self.search_var.get().strip().lower()
        self._reload_pool(reorder=True)

    def _refresh_pool_now(self) -> None:
        """Кнопка «Обновить»: перерисовать таблицу с новым порядком строк."""
        self._reload_pool(reorder=True)

    def _filtered_entries(self, limit: Optional[int] = None) -> List[PoolRow]:
        """Строки таблицы с фильтром и поиском (обёртка над кэшем).

        В режиме «Чёрный список адресов» в таблице адреса, а не конфигурации:
        списка строк пула там нет, и кнопка «Удалить по фильтру» не должна
        трогать пул.
        """
        if self._in_blacklist_mode():
            return []
        rows = self._rows_for_view()
        if limit is not None and limit > 0:
            return rows[:limit]
        return rows

    def _view_limit(self) -> int:
        """Сколько строк показывать (0 — без ограничения)."""
        try:
            return int(self.settings.get("simple_view_limit", 1000) or 0)
        except (TypeError, ValueError):
            return 1000

    def _rows_for_view(self) -> List[PoolRow]:
        """Отфильтрованные и отсортированные строки таблицы (с кэшем).

        Пересчёт на тысяче записей стоит десятки миллисекунд, поэтому результат
        переиспользуется, пока не изменились пул, фильтр, поиск или лимит.
        """
        limit = self._view_limit()
        key = (self.pool.revision, self.filter_var.get(),
               self._search_needle, limit)
        if key == self._pool_rows_key and self._pool_rows is not None:
            return self._pool_rows
        wanted = STATE_VALUE_BY_LABEL.get(self.filter_var.get())
        needle = self._search_needle
        rows = self.pool.rows()
        if wanted:
            rows = [row for row in rows if row.state == wanted]
        if needle:
            rows = [row for row in rows
                    if needle in
                    f"{row.uri} {row.endpoint} {row.protocol}".lower()]
        order_key = (lambda row: (STATE_RANK.get(row.state, 99),
                                  row.latency_ms is None,
                                  row.latency_ms or 0, row.endpoint))
        if limit > 0 and len(rows) > limit:
            # В пуле живут и нерабочие серверы (их могут быть десятки тысяч),
            # поэтому вместо полной сортировки выбираем только лучшие строки:
            # O(n log k) вместо O(n log n) — таблица остаётся отзывчивой.
            rows = heapq.nsmallest(limit, rows, key=order_key)
        else:
            rows.sort(key=order_key)
        self._pool_rows = rows
        self._pool_rows_key = key
        return rows

    @staticmethod
    def _row_values(row: PoolRow) -> Tuple[str, ...]:
        """Значения столбцов строки для :class:`ttk.Treeview`."""
        return (
            STATE_LABEL_BY_VALUE.get(row.state, row.state),
            row.protocol,
            row.endpoint,
            human_latency(row.latency_ms),
            f"{row.ping_fail} / {row.core_fail} / {row.traffic_fail}",
            human_time(row.last_checked),
            row.last_error or row.label or "",
        )

    def _sync_pool_tree(self, rows: List[PoolRow], reorder: bool) -> None:
        """Обновить таблицу по месту: меняются только изменившиеся строки.

        Прежняя реализация каждый раз удаляла и заново вставляла все строки: это
        сбрасывало выделение, фокус и прокрутку, а на 1000 строк стоило около
        0,1 с (копии записей пула плюс пересборка таблицы).  Теперь значения
        строк переписываются точечно (единицы миллисекунд), поэтому выделение и
        прокрутка сохраняются сами собой.
        """
        wanted = {row.fp: (row.state, self._row_values(row)) for row in rows}
        if reorder:
            # Порядок строк пересматриваем редко (см. ORDER_INTERVAL): иначе
            # строки прыгают под курсором, пока идёт проверка.
            self._last_order_at = time.time()
            if list(wanted) != self._pool_order:
                self._rebuild_pool_tree(wanted)
                return
        for fp in list(self._pool_values):
            if fp in wanted:
                continue
            self._delete_pool_row(fp)
        for fp, item in wanted.items():
            previous = self._pool_values.get(fp)
            if previous is None:
                if self._insert_pool_row(fp, item[0], item[1]):
                    self._pool_order.append(fp)
            elif previous != item:
                self._set_pool_row(fp, item[0], item[1])
            self._pool_values[fp] = item

    def _insert_pool_row(self, fp: str, state: str,
                         values: Tuple[str, ...]) -> bool:
        """Добавить строку в конец таблицы."""
        try:
            self.pool_tree.insert("", "end", iid=fp, values=values,
                                  tags=(state,))
            return True
        except tk.TclError:
            return False

    def _set_pool_row(self, fp: str, state: str,
                      values: Tuple[str, ...]) -> None:
        """Переписать значения и цвет существующей строки."""
        try:
            self.pool_tree.item(fp, values=values, tags=(state,))
        except tk.TclError:
            pass

    def _delete_pool_row(self, fp: str) -> None:
        self._pool_values.pop(fp, None)
        if fp in self._pool_order:
            self._pool_order.remove(fp)
        try:
            self.pool_tree.delete(fp)
        except tk.TclError:
            pass

    def _rebuild_pool_tree(self,
                           wanted: Dict[str, Tuple[str, Tuple[str, ...]]]
                           ) -> None:
        """Пересобрать таблицу в новом порядке, вернув выделение и прокрутку."""
        tree = self.pool_tree
        try:
            selection = [fp for fp in tree.selection() if fp in wanted]
            focus = tree.focus()
            offset = tree.yview()[0]
            tree.delete(*tree.get_children())
            for fp, (state, values) in wanted.items():
                tree.insert("", "end", iid=fp, values=values, tags=(state,))
        except tk.TclError:
            return
        self._pool_order = list(wanted)
        self._pool_values = dict(wanted)
        try:
            if selection:
                tree.selection_set(selection)
            if focus in wanted:
                tree.focus(focus)
            tree.yview_moveto(offset)
        except tk.TclError:
            pass
        self._last_order_at = time.time()

    def _reload_pool(self, reorder: bool = True) -> None:
        """Обновить таблицу серверов и подписи под ней.

        ``reorder=False`` — переписать значения строк, не переставляя их: во
        время круга порядок «замораживается», чтобы строки не прыгали под
        курсором и не терялось выделение.
        """
        if self._in_blacklist_mode():
            self._reload_blocklist()
            return
        rows = self._rows_for_view()
        self._sync_pool_tree(rows, reorder=reorder)
        self.pool_info.configure(
            text=t('Показано {} из {}').format(fmt_int(len(rows)), fmt_int(self.pool.size())))
        self.blacklist_info.configure(
            text=(t('Исключённых конфигураций: {} · адресов в чёрном списке: {}').format(fmt_int(self.pool.blocked_count()), fmt_int(self.pool.blocked_address_count()))))
        self._pool_revision = self.pool.revision

    def _reload_blocklist(self) -> None:
        """Показать постоянный чёрный список адресов вместо конфигураций."""
        # В таблице строки другого вида: при возврате к списку серверов
        # собираем её заново, иначе по «fp» строк не найти.
        self._pool_order = []
        self._pool_values = {}
        self._pool_rows_key = None
        try:
            self.pool_tree.delete(*self.pool_tree.get_children())
        except tk.TclError:
            return
        records = self.pool.blocked_addresses()
        for record in records:
            key = str(record.get("key") or "")
            display = str(record.get("display") or key)
            try:
                self.pool_tree.insert("", "end", iid=key, values=(
                    t("Чёрный список"),
                    "—",
                    display,
                    "—",
                    "—",
                    human_time(float(record.get("at") or 0)),
                    str(record.get("reason") or ""),
                ), tags=("blocklist",))
            except tk.TclError:
                continue
        self.pool_tree.tag_configure("blocklist", foreground="#b02a37")
        self.pool_info.configure(
            text=(t('Адресов в чёрном списке: {} — они не опрашиваются, не тестируются и не добавляются даже из подписок').format(fmt_int(len(records)))))
        self.blacklist_info.configure(
            text=(t('Исключённых конфигураций: {}').format(fmt_int(self.pool.blocked_count()))))
        self._pool_revision = self.pool.revision

    def _selected_addresses(self) -> List[Tuple[str, int]]:
        """Выбранные строки чёрного списка: пары ``(адрес, порт)``."""
        pairs: List[Tuple[str, int]] = []
        for key in self.pool_tree.selection():
            host, _sep, port = str(key).rpartition(":")
            try:
                pairs.append((host, int(port)))
            except ValueError:
                continue
        return pairs

    def _blacklist_manual(self) -> None:
        """Внести в чёрный список введённое руками: ссылку, host:port или IP.

        Работает и в обычном режиме таблицы (не только в фильтре чёрного
        списка): адреса уходят в список сразу, а их записи — из пула.
        """
        text = self.blacklist_input_var.get().strip()
        if not text:
            messagebox.showinfo(
                t("Чёрный список"),
                t("Введите ссылку на сервер, «host:port» или просто IP.\n"
                "Несколько значений можно перечислить через пробел, запятую "
                "или с новой строки."),
                parent=self)
            return
        counters = self.supervisor.blacklist_input(text)
        if counters["addresses"] or counters["removed"]:
            self.blacklist_input_var.set("")
        if counters["invalid"]:
            messagebox.showwarning(
                t("Чёрный список"),
                t('Не удалось разобрать значений: {} из {}.').format(counters['invalid'], counters['values']),
                parent=self)
        self._reload_pool()

    def _unblock_selected(self) -> None:
        pairs = self._selected_addresses()
        if not pairs:
            messagebox.showinfo(
                t("Чёрный список"),
                t('Выберите адреса в таблице — для этого включите фильтр «{}».').format(FILTER_BLACKLIST), parent=self)
            return
        if not messagebox.askyesno(
                t("Чёрный список"),
                t('Разрешить снова добавлять и проверять {} адрес(ов)?').format(len(pairs)), parent=self):
            return
        for host, port in pairs:
            self.supervisor.unblock_address(host, port)
        self._reload_pool()

    def _selected_fps(self) -> List[str]:
        if self._in_blacklist_mode():
            return []                     # это адреса, а не конфигурации
        return [fp for fp in self.pool_tree.selection() if fp]

    def _probe_selected(self) -> None:
        """Сбросить выбранные в «новые» и запустить проверку немедленно."""
        fps = self._selected_fps()
        if not fps:
            messagebox.showinfo(t("Проверка"), t("Выберите строки в списке."),
                                parent=self)
            return
        changed = self.pool.reset(fps)
        self.pool.save(force=True)
        self._reload_pool()
        log.info(t("Отмечено для немедленной проверки: %s"), fmt_int(changed))
        if not self.supervisor.running:
            self._background(self.supervisor.run_round)

    def _reset_selected(self) -> None:
        fps = self._selected_fps()
        if not fps:
            return
        changed = self.pool.reset(fps)
        self.pool.save(force=True)
        self._reload_pool()
        log.info(t("Сброшено в «новые»: %s"), fmt_int(changed))

    def _copy_selected(self) -> None:
        fps = set(self._selected_fps())
        if not fps:
            return
        lines = [entry.uri for entry in self.pool.items() if entry.fp in fps]
        if not lines:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        messagebox.showinfo(t("Буфер обмена"),
                            t('Скопировано ссылок: {}').format(fmt_int(len(lines))),
                            parent=self)

    def _delete_selected(self) -> None:
        """Удалить выбранные конфигурации из пула (без чёрного списка)."""
        fps = self._selected_fps()
        if not fps:
            return
        if not messagebox.askyesno(
                t("Удаление"),
                t('Удалить выбранные конфигурации ({})?\n\nЭто простая чистка: адреса в чёрный список не заносятся, поэтому сервер сможет вернуться из подписки. Чтобы адрес больше не проверялся, используйте «В чёрный список».').format(len(fps)),
                parent=self):
            return
        counters = self.supervisor.delete_entries(fps)
        self._reload_pool()
        self._append_log_line(
            t('Удалено вручную: {} конфигураций (в чёрный список не заносились)').format(fmt_int(counters['removed'])))

    def _blacklist_selected(self) -> None:
        """Внести адреса выбранных серверов в постоянный чёрный список."""
        fps = self._selected_fps()
        if not fps:
            messagebox.showinfo(
                t("Чёрный список"),
                t("Выберите конфигурации в таблице: их адреса будут внесены в "
                "чёрный список и больше не будут добавляться и проверяться."),
                parent=self)
            return
        if not messagebox.askyesno(
                t("Чёрный список"),
                t('Внести адреса выбранных конфигураций ({}) в чёрный список?\n\nИз пула уйдут все конфигурации этих адресов, повторно они не добавятся и проверяться не будут, пока вы не уберёте адрес в фильтре «{}».').format(len(fps), FILTER_BLACKLIST),
                parent=self):
            return
        counters = self.supervisor.blacklist_entries(fps)
        self._reload_pool()
        self._append_log_line(
            t('В чёрный список: адресов {}, из пула убрано {}, всего в списке {}').format(fmt_int(counters['addresses']), fmt_int(counters['removed']), fmt_int(counters['blacklist'])))

    def _delete_filtered(self) -> None:
        entries = self._filtered_entries()
        if not entries:
            return
        if not messagebox.askyesno(
                t("Удаление"),
                t('Удалить все конфигурации под текущим фильтром ({})?\n\nЭто чистка пула: адреса в чёрный список не заносятся. Если адрес подозрительный, выделите его и нажмите «В чёрный список».').format(fmt_int(len(entries))), parent=self):
            return
        removed = self.pool.remove([entry.fp for entry in entries])
        self.pool.save(force=True)
        self._reload_pool()
        log.info(t("Удалено по фильтру: %s"), fmt_int(removed))

    def _clear_blacklist(self) -> None:
        blocked = self.pool.blocked_count()
        addresses = self.pool.blocked_address_count()
        if not blocked and not addresses:
            return
        if not messagebox.askyesno(
                t("Чёрный список"),
                t('Очистить чёрный список полностью?\n\nАдресов: {} — они снова смогут добавляться и проверяться.\nИсключённых конфигураций: {} — они снова смогут вернуться из подписок.').format(fmt_int(addresses), fmt_int(blocked)), parent=self):
            return
        counters = self.supervisor.clear_blocklist()
        self._reload_pool()
        self._append_log_line(
            t('Чёрный список очищен: адресов {}, конфигураций {}').format(fmt_int(counters['addresses']), fmt_int(counters['blocked'])))

    def _clear_all(self) -> None:
        if not messagebox.askyesno(
                t("Удаление"), t("Удалить ВСЕ конфигурации из пула? "
                "Источники останутся."), parent=self):
            return
        removed = self.pool.clear()
        self.pool.save(force=True)
        self._reload_pool()
        log.info(t("Пул очищен: %s"), fmt_int(removed))

    # ------------------------------------------------------------------
    # Контекстные меню (правая кнопка мыши)
    # ------------------------------------------------------------------
    def _bind_context_menu(self, tree: ttk.Treeview, builder) -> None:
        """ПКМ по таблице: выделить строку под курсором и открыть меню.

        Меню собираются из тех же действий, что кнопки и горячие клавиши
        (:meth:`_pool_context_menu` и соседи): смысл действия живёт в одном
        месте, а меню лишь даёт ему подпись.
        """
        def popup(event: tk.Event) -> str:
            self._select_row_at(tree, event.y)
            return self._show_menu(builder(), event)

        for sequence in ("<Button-3>", "<Control-Button-1>"):
            tree.bind(sequence, popup)

    def _bind_text_menu(self, widget: tk.Text,
                        extra: Tuple[Tuple[str, object], ...] = ()) -> None:
        """ПКМ по текстовому полю: правка, выделение и дополнительные пункты."""
        def popup(event: tk.Event) -> str:
            return self._show_menu(self._edit_menu(widget, extra), event)

        for sequence in ("<Button-3>", "<Control-Button-1>"):
            widget.bind(sequence, popup)

    def _edit_menu(self, widget: tk.Text,
                   extra: Tuple[Tuple[str, object], ...] = ()) -> tk.Menu:
        """Меню правки для :class:`tk.Text` (в Tk оно не встроено)."""
        menu = tk.Menu(self, tearoff=0)
        for label, virtual in ((t("Вырезать"), "<<Cut>>"),
                               (t("Копировать"), "<<Copy>>"),
                               (t("Вставить"), "<<Paste>>"),
                               (t("Выделить всё"), "<<SelectAll>>")):
            menu.add_command(
                label=label,
                command=lambda seq=virtual: widget.event_generate(seq))
        for label, command in extra:
            menu.add_separator()
            menu.add_command(label=label, command=command)
        return menu

    def _show_menu(self, menu: tk.Menu, event: tk.Event) -> str:
        """Показать меню в точке курсора (и снять захват после закрытия)."""
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _select_row_at(self, tree: ttk.Treeview, y: int) -> bool:
        """Выделить строку под курсором: без этого действия бьют по старому выбору."""
        row = tree.identify_row(y)
        if not row:
            return False
        try:
            if row not in tree.selection():
                tree.selection_set(row)
            tree.focus(row)
        except tk.TclError:
            return False
        return True

    def _pool_context_menu(self) -> tk.Menu:
        """Меню правой кнопки для списка серверов (или чёрного списка)."""
        menu = tk.Menu(self, tearoff=0)
        if self._in_blacklist_mode():
            picked = "normal" if self.pool_tree.selection() else "disabled"
            menu.add_command(label=t("Убрать из чёрного списка\tDel"),
                             command=self._unblock_selected, state=picked)
            menu.add_separator()
            menu.add_command(label=t("Очистить чёрный список"),
                             command=self._clear_blacklist)
            return menu
        picked = "normal" if self.pool_tree.selection() else "disabled"
        for label, command in (
            (t("Проверить сейчас"), self._probe_selected),
            (t("Сбросить в «новые»"), self._reset_selected),
            ("", None),
            (t("Копировать ссылки\tCtrl+C"), self._copy_selected),
            ("", None),
            (t("В чёрный список (не проверять)\tShift+Del"),
             self._blacklist_selected),
            (t("Удалить из пула\tDel"), self._delete_selected),
        ):
            if command is None:
                menu.add_separator()
            else:
                menu.add_command(label=label, command=command, state=picked)
        menu.add_separator()
        menu.add_command(label=t("Удалить по фильтру…"),
                         command=self._delete_filtered)
        menu.add_command(label=t("Удалить все конфигурации…"),
                         command=self._clear_all)
        return menu

    def _sources_context_menu(self) -> tk.Menu:
        """Меню правой кнопки для списка источников."""
        menu = tk.Menu(self, tearoff=0)
        picked = ("normal" if self._selected_source_index() is not None
                  else "disabled")
        menu.add_command(label=t("Включить / выключить\tInsert"),
                         command=self._toggle_source, state=picked)
        menu.add_command(label=t("Копировать значение"),
                         command=self._copy_source_value, state=picked)
        menu.add_command(label=t("Удалить источник\tDel"),
                         command=self._remove_source, state=picked)
        menu.add_separator()
        menu.add_command(label=t("Обновить подписки сейчас"),
                         command=self._refresh_sources)
        return menu

    def _copy_source_value(self) -> None:
        """Скопировать значение выбранного источника в буфер обмена."""
        index = self._selected_source_index()
        if index is None:
            return
        sources = self.supervisor.sources()
        if index >= len(sources):
            return
        value = str(sources[index].get("value") or "")
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.sources_info.configure(text=t("Значение источника скопировано"))

    def _commands_context_menu(self) -> tk.Menu:
        """Меню правой кнопки для списка внешних команд."""
        menu = tk.Menu(self, tearoff=0)
        picked = "normal" if self._selected_preset_id() else "disabled"
        menu.add_command(label=t("Добавить команду…"), command=self._new_preset)
        menu.add_command(label=t("Изменить…\tF2"), command=self._edit_preset,
                         state=picked)
        menu.add_command(label=t("Дублировать"), command=self._duplicate_preset,
                         state=picked)
        menu.add_separator()
        menu.add_command(label=t("Выполнить сейчас"), command=self._run_preset_now,
                         state=picked)
        menu.add_command(label=t("Включить / выключить\tInsert"),
                         command=self._toggle_preset, state=picked)
        menu.add_separator()
        menu.add_command(label=t("Удалить команду\tDel"),
                         command=self._remove_preset, state=picked)
        return menu

    # ------------------------------------------------------------------
    # Вкладка «Внешние команды»
    # ------------------------------------------------------------------
    def _build_commands_tab(self, parent: ttk.Frame) -> None:
        # Вкладка целиком прокручивается: подсказка, список пресетов и вывод
        # команды вместе выше низкого окна, и без прокрутки нижняя часть
        # (кнопки и вывод) оказывалась за границей окна.
        scroller = ScrollableFrame(parent, height=420)
        scroller.pack(fill="both", expand=True)
        self.commands_scroller = scroller
        body = scroller.inner

        hint = ttk.LabelFrame(body, text=t("Зачем этот раздел"), padding=8)
        hint.pack(fill="x")
        hint_text = ttk.Label(
            hint, style="Hint.TLabel", justify="left",
            text=(
                t("Ваши команды и скрипты, которые приложение запускает само: "
                "что, когда и куда передавать — описывается в пресете.\n"
                "Триггеры, подстановки и готовые примеры — в справке ниже.")
            ))
        hint_text.pack(anchor="w")
        self.commands_hint = hint_text          # для тестов: текст подсказки раздела
        adaptive_wraplength(hint_text, hint, margin=24, minimum=240)
        help_row = AdaptiveBar(hint)
        help_row.pack(fill="x", pady=(6, 0))
        help_row.add(ttk.Button(help_row, text=t("Подробная справка и примеры"),
                                command=self._show_commands_help))
        self.commands_info = ttk.Label(hint, text="", style="Hint.TLabel")
        self.commands_info.pack(anchor="w", pady=(4, 0))
        adaptive_wraplength(self.commands_info, hint, margin=24, minimum=200)

        container, self.commands_tree = make_treeview(body, [
            ("enabled", t("Вкл"), 40, "center"),
            ("name", t("Имя"), 180, "w"),
            ("target", t("Что запускаем"), 420, "w"),
            ("trigger", t("Когда"), 150, "w"),
            ("interval", t("Период"), 70, "e"),
            ("last", t("Последний запуск"), 170, "w"),
            ("status", t("Результат"), 330, "w"),
        ], height=8)
        container.pack(fill="both", expand=True, pady=(8, 6))
        self.commands_tree.bind("<Double-1>", lambda _event: self._edit_preset())
        self.commands_tree.bind("<<TreeviewSelect>>", self._on_commands_select)
        self._bind_context_menu(self.commands_tree, self._commands_context_menu)

        buttons = AdaptiveBar(body)
        buttons.pack(fill="x")
        buttons.add(ttk.Button(buttons, text=t("Добавить…"),
                               command=self._new_preset))
        edit_button = ttk.Button(buttons, text=t("Изменить…"),
                                 command=self._edit_preset)
        buttons.add(edit_button)
        ToolTip(edit_button, t("F2 — изменить выбранный пресет."))
        buttons.add(ttk.Button(buttons, text=t("Дублировать"),
                               command=self._duplicate_preset))
        remove_button = ttk.Button(buttons, text=t("Удалить"),
                                   command=self._remove_preset)
        buttons.add(remove_button)
        ToolTip(remove_button, t("Del — удалить выбранный пресет."))
        buttons.add(ttk.Button(buttons, text=t("Выполнить сейчас"),
                               command=self._run_preset_now))
        toggle_button = ttk.Button(buttons, text=t("Включить/выключить"),
                                   command=self._toggle_preset)
        buttons.add(toggle_button)
        ToolTip(toggle_button, t("Insert — включить или выключить пресет."))
        self.btn_examples = ttk.Button(buttons, text=t("Примеры…"),
                                       command=self._show_example_choices)
        buttons.add(self.btn_examples)

        output = ttk.LabelFrame(body, text=t("Вывод последней команды"), padding=8)
        output.pack(fill="both", expand=True, pady=(8, 0))
        box = ttk.Frame(output)
        box.pack(fill="both", expand=True)
        self.commands_output = self._register_text(
            tk.Text(box, height=5, wrap="word", state="disabled"))
        scroll = ttk.Scrollbar(box, orient="vertical",
                               command=self.commands_output.yview)
        self.commands_output.configure(yscrollcommand=scroll.set)
        self.commands_output.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        actions = AdaptiveBar(output)
        actions.pack(fill="x", pady=(6, 0))
        actions.add(ttk.Button(actions, text=t("Скопировать вывод"),
                               command=self._copy_commands_output))
        self.btn_reveal_output = ttk.Button(actions, text=t("Показать вывод"),
                                            command=self._toggle_output_reveal)
        actions.add(self.btn_reveal_output)
        actions.add(ttk.Button(actions, text=t("Открыть журнал"),
                               command=self._open_log_tab))
        reveal_hint = ttk.Label(
            output, style="Hint.TLabel", justify="left",
            text=(t("для пресетов в режиме «скрытый» вывод и команда "
                  "показываются кнопкой «Показать вывод»")))
        reveal_hint.pack(anchor="w", pady=(4, 0))
        adaptive_wraplength(reveal_hint, output, margin=24, minimum=200)

        scroller.bind_wheel()

    def _toggle_output_reveal(self) -> None:
        """Показать/спрятать вывод пресета, помеченного как скрытый."""
        preset = self._selected_preset()
        if preset is None:
            return
        if self._revealed_for == preset.id:
            self._revealed_for = ""
        else:
            self._revealed_for = preset.id
        self._show_last_command_output(self.supervisor.commands.presets())

    def _on_commands_select(self, _event=None) -> None:
        """Смена выбранного пресета снова прячет его скрытый вывод."""
        current = self._selected_preset_id()
        if self._revealed_for and self._revealed_for != current:
            self._revealed_for = ""
        self._show_last_command_output(self.supervisor.commands.presets())

    def _open_log_tab(self) -> None:
        try:
            self.notebook.select(self.tab_log)
        except tk.TclError:
            pass

    def _copy_commands_output(self) -> None:
        try:
            text = self.commands_output.get("1.0", "end").strip()
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(text)

    def _refresh_commands_if_changed(self) -> None:
        scheduler = self.supervisor.commands
        now = time.time()
        if (scheduler.revision == self._commands_revision
                and now - self._commands_refreshed_at < COMMANDS_REFRESH_SECONDS):
            return
        # Таблица перерисовывается и без изменений — раз в COMMANDS_REFRESH_SECONDS:
        # иначе давность запуска («5 мин назад») застывала бы в виде, который
        # был на момент последнего изменения состояния пресетов.
        self._commands_revision = scheduler.revision
        self._commands_refreshed_at = now
        self._reload_commands()

    def _reload_commands(self) -> None:
        presets = self.supervisor.commands.presets()
        selected = self._selected_preset_id()
        try:
            self.commands_tree.delete(*self.commands_tree.get_children())
            for preset in presets:
                last = run_time_text(preset.last_run)
                interval = ""
                if preset.trigger == "interval":
                    interval = t('{:g} мин').format(preset.interval_minutes)
                self.commands_tree.insert("", "end", iid=preset.id, values=(
                    "✔" if preset.enabled else "✘",
                    ("🔒 " if preset.hidden else "") + preset.name,
                    preset.display_target,
                    preset.trigger_label,
                    interval,
                    last,
                    status_text(preset.last_status) or "—",
                ))
        except tk.TclError:
            return
        if selected and self.commands_tree.exists(selected):
            self.commands_tree.selection_set(selected)
        scheduler = self.supervisor.commands
        state = t("работает") if scheduler.running else t("остановлен")
        if scheduler.busy:
            state = t('выполняется: {}').format(scheduler.current_name)
        self.commands_info.configure(
            text=t('Пресетов: {} · планировщик {}').format(len(presets), state))
        self._show_last_command_output(presets)

    def _show_last_command_output(self, presets: List[CommandPreset]) -> None:
        preset = self._selected_preset(presets)
        revealed = preset is not None and self._revealed_for == preset.id
        hidden = bool(preset is not None and preset.hidden and not revealed)
        if preset is None:
            text = t("Пресетов пока нет: нажмите «Добавить…» или выберите пример.")
        else:
            # Метка времени показывается всегда — и у «скрытого» пресета тоже:
            # сама дата запуска секретом не является, а без неё непонятно,
            # когда команда выполнялась в последний раз.
            lines = [f"{preset.name}: {status_text(preset.last_status) or t('ещё не запускался')}"]
            if preset.last_run:
                lines.append(t('Последний запуск: {} · {:.1f} с').format(run_time_full(preset.last_run), preset.last_duration))
            else:
                lines.append(t("Последний запуск: не выполнялась"))
            if hidden:
                # Приватный пресет: ни команды, ни вывода, пока пользователь
                # явно не попросил показать их.
                lines.append("")
                lines.append(t("Команда и вывод скрыты: у пресета выбран режим "
                             "«скрытый» (меняется в редакторе пресета)."))
                lines.append(t("Нажмите «Показать вывод», чтобы увидеть их "
                             "в этом сеансе."))
            else:
                if preset.last_run:
                    rendered = render_preset(preset, self._artifacts())
                    lines.append(t('Команда: {}').format(rendered.safe_command_line))
                if preset.last_output:
                    lines.append(t("--- вывод ---"))
                    lines.append(preset.last_output)
            text = "\n".join(lines)

        try:
            if preset is not None and preset.hidden:
                self.btn_reveal_output.state(["!disabled"])
                self.btn_reveal_output.configure(
                    text=t("Скрыть вывод") if revealed else t("Показать вывод"))
            else:
                self.btn_reveal_output.state(["disabled"])
                self.btn_reveal_output.configure(text=t("Показать вывод"))
        except tk.TclError:
            pass

        try:
            self.commands_output.configure(state="normal")
            self.commands_output.delete("1.0", "end")
            self.commands_output.insert("1.0", text)
            self.commands_output.configure(state="disabled")
        except tk.TclError:
            pass

    def _artifacts(self) -> Dict[str, object]:
        """Пути выходных файлов — для предпросмотра и запуска по кнопке."""
        try:
            return self.supervisor.artifacts()
        except Exception:  # noqa: BLE001
            return {}

    def _selected_preset_id(self) -> str:
        try:
            selection = self.commands_tree.selection()
        except tk.TclError:
            return ""
        return selection[0] if selection else ""

    def _selected_preset(self, presets: Optional[List[CommandPreset]] = None
                         ) -> Optional[CommandPreset]:
        """Выбранный пресет (или первый, если ничего не выбрано)."""
        if presets is None:
            presets = self.supervisor.commands.presets()
        preset_id = self._selected_preset_id()
        if not preset_id:
            return presets[0] if presets else None
        for preset in presets:
            if preset.id == preset_id:
                return preset
        return presets[0] if presets else None

    # -- действия вкладки «Внешние команды» ------------------------------
    def _new_preset(self) -> None:
        preset = CommandPreset.from_dict({
            "name": t("Новая команда"), "kind": "command",
            "command": 'cmd /c echo "{count}" > "{out_dir}\\last-upload.txt"',
        })
        self._open_preset_dialog(preset)

    def _edit_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            messagebox.showinfo(t("Внешние команды"),
                                t("Сначала добавьте хотя бы один пресет."),
                                parent=self)
            return
        self._open_preset_dialog(preset)

    def _open_preset_dialog(self, preset: CommandPreset) -> None:
        """Открыть редактор пресета и сохранить изменения."""
        dialog = PresetDialog(self, preset, artifacts_provider=self._artifacts)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        saved = dialog.result
        if self.supervisor.commands.get(saved.id) is None:
            self.supervisor.commands.add(saved)
            self._append_log_line(t('Внешние команды: добавлен пресет «{}»').format(saved.name))
        else:
            self.supervisor.commands.update(saved)
            self._append_log_line(t('Внешние команды: изменён пресет «{}»').format(saved.name))

    def _duplicate_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        clone = self.supervisor.commands.duplicate(preset.id)
        if clone is not None:
            self._reload_commands()
            try:
                self.commands_tree.selection_set(clone.id)
            except tk.TclError:
                pass

    def _remove_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        if not messagebox.askyesno(
                t("Удаление пресета"),
                t('Удалить пресет «{}»?').format(preset.name), parent=self):
            return
        self.supervisor.commands.remove(preset.id)
        self._append_log_line(t('Внешние команды: удалён пресет «{}»').format(preset.name))

    def _run_preset_now(self) -> None:
        """Ручной запуск выбранной команды (в фоне, через планировщик)."""
        preset = self._selected_preset()
        if preset is None:
            return
        scheduler = self.supervisor.commands
        if not scheduler.running:
            # Планировщик останавливается вместе с циклом — поднимем его, чтобы
            # кнопка работала и при выключенной автоматизации.
            scheduler.start()
        if scheduler.run_now(preset.id):
            self._append_log_line(t('Внешние команды: «{}» запущена вручную').format(preset.name))

    def _toggle_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        preset.enabled = not preset.enabled
        self.supervisor.commands.update(preset)

    def _show_example_choices(self) -> None:
        """Выпадающий список готовых примеров (и пустой пресет)."""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=t("Пустой пресет"), command=self._new_preset)
        menu.add_separator()
        for index, example in enumerate(EXAMPLES):
            menu.add_command(
                label=str(example.get("title") or t('Пример {}').format(index + 1)),
                command=lambda i=index: self._new_preset_from_example(i))
        try:
            x = self.btn_examples.winfo_rootx()
            y = (self.btn_examples.winfo_rooty()
                 + self.btn_examples.winfo_height())
            menu.tk_popup(x, y)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def _new_preset_from_example(self, index: int) -> None:
        if not 0 <= index < len(EXAMPLES):
            return
        example = EXAMPLES[index]
        data: Dict[str, object] = {
            "name": str(example.get("title") or t("Пример")),
            "kind": str(example.get("kind") or "command"),
            "artifact": str(example.get("artifact") or "links"),
        }
        for key in ("command", "script", "arguments"):
            value = example.get(key)
            if value:
                data[key] = value
        self._open_preset_dialog(CommandPreset.from_dict(data))

    def _commands_help_text(self) -> str:
        """Краткая справка по разделу: триггеры, подстановки, примеры."""
        lines = [
            t("ВНЕШНИЕ КОМАНДЫ — ЧТО ЭТО"),
            "",
            t("Раздел запускает ваши команды и скрипты. Куда и как передавать файл"),
            t("(VPS, Telegram, GitHub, облако, локальная папка, обменник) — решаете"),
            t("вы: приложение только вовремя вызывает команду, подставляет в неё"),
            t("пути к файлам и записывает результат в журнал."),
            "",
            t("КОГДА ЗАПУСКАЕТСЯ (ТРИГГЕР)"),
            t("  • При изменении списка — только когда выходной файл действительно"),
            t("    перезаписан, но не чаще указанной паузы."),
            t("  • По расписанию — раз в N минут, даже если файл не менялся."),
            t("  • Только вручную — кнопкой «Выполнить сейчас»."),
            t("  • При запуске автоматизации — когда нажата «Запустить» (в том числе"),
            t("    при автозапуске): удобно для «поднять» внешние сервисы."),
            t("  • При остановке автоматизации — когда нажата «Стоп»: например,"),
            t("    погасить удалённый приёмник или отправить итоговый отчёт."),
            t("  • Круг проверок завершён — после каждого круга: агрегатор может"),
            t("    раз в круг делать свою работу, не дожидаясь изменения файла."),
            t("  • Пропала сеть / Сеть восстановилась — по слежке за связью"),
            t("    (раздел «Настройки» → «Окно, трей и сеть»): например, переключить"),
            t("    выходной файл на резервный сервер или уведомить себя."),
            t("Одновременно выполняется одна команда: остальные ждут в очереди,"),
            t("поэтому всплеск изменений не превращается в лавину запусков."),
            "",
            t("ПОДСТАНОВКИ В КОМАНДЕ"),
        ]
        for name, note in PLACEHOLDERS:
            lines.append(f"  {name:14} {note}")
        lines += [
            "",
            t("ПРАВИЛА ОФОРМЛЕНИЯ"),
            t("  • подстановки подставляются как есть: если значение может содержать"),
            t("    пробелы, берите его в кавычки — scp \"{file}\" user@host:{remote_dir}/"),
            t("  • путь к скрипту в кавычки заключается автоматически;"),
            t("  • поля профиля (host, port, user, password, remote_dir)"),
            t("    подставляются в команду и удобны для ssh/scp/rclone;"),
            t("  • неизвестные подстановки остаются в тексте — в журнал попадёт"),
            t("    предупреждение, запуск всё равно состоится."),
            "",
            t("БЕЗОПАСНОСТЬ И НАДЁЖНОСТЬ"),
            t("  • пароль не попадает в журнал и интерфейс: в выводе он заменяется"),
            t("    на ***, дополнительно команда получает его как переменную"),
            t("    окружения COMMAND_PASSWORD. Надёжнее держать секреты в самом"),
            t("    скрипте, в ~/.ssh или в конфиге rclone;"),
            t("  • режим «скрытый» у пресета маскирует команду и вывод в самом"),
            t("    интерфейсе: их видно только по кнопке «Показать вывод», а в"),
            t("    журнал уходит пометка вместо текста;"),
            t("  • команда запускается без окна консоли; используйте неинтерактивные"),
            t("    ключи (scp -o BatchMode=yes, curl --fail --max-time …), иначе запуск"),
            t("    провисит до тайм-аута;"),
            t("  • по тайм-ауту процесс уничтожается вместе с потомками;"),
            t("  • при ошибке выполняется до N повторов с паузой (тайм-ауты не"),
            t("    повторяются);"),
            t("  • файл настроек — JSON в папке профиля: он содержит ваши команды и"),
            t("    пароли профиля, не передавайте его посторонним;"),
            t("  • «Стоп» в шапке окна останавливает и проверки, и внешние команды."),
            "",
            t("ГОТОВЫЕ ПРИМЕРЫ (кнопка «Примеры…» создаст из них пресет)"),
        ]
        for example in EXAMPLES:
            lines.append("")
            lines.append(f"• {example.get('title')}")
            note = str(example.get("note") or "")
            for chunk in textwrap.wrap(note, 92):
                lines.append(f"  {chunk}")
            body = example.get("command") or (
                f"{example.get('script', '')} {example.get('arguments', '')}")
            lines.append(f"  {body.strip()}")
        return "\n".join(lines)

    def _show_commands_help(self) -> None:
        window = tk.Toplevel(self)
        window.title(t("Внешние команды — справка"))
        fit_window(window, 920, 640, min_width=560, min_height=360)
        window.transient(self)
        bar = ttk.Frame(window, padding=(8, 4))
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text=t("Закрыть"),
                   command=window.destroy).pack(side="right")
        ttk.Label(bar, style="Hint.TLabel",
                  text=t("Справка собрана по текущей версии раздела.")).pack(
            side="left")
        box = ttk.Frame(window)
        box.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))
        text = self._register_text(tk.Text(box, wrap="word", padx=6, pady=4))
        scroll = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        text.insert("1.0", self._commands_help_text())
        text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Вкладка «Настройки»
    # ------------------------------------------------------------------
    def _section(self, parent: ttk.Frame, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.pack(fill="x", pady=(0, 8))
        return frame

    def _field(self, parent: ttk.Frame, row: int, column: int, label: str,
               key: str, kind: type, hint: str = "", width: int = 10,
               on_change: Optional[Callable[[], None]] = None,
               stretch: bool = False):
        """Поле настройки: подпись слева, значение справа.

        ``stretch`` нужен только для длинных значений (пути, URL, User-Agent):
        остальные поля остаются короткими и выровненными по колонкам — иначе
        пара цифр занимала бы пол-окна, а второй столбец настроек уезжал к
        правому краю.
        """
        ttk.Label(parent, text=label).grid(
            row=row, column=column * 2, sticky="w", padx=(0, 6), pady=2)
        # Ширина и растяжение задаются по ключу настройки: короткие значения
        # (числа) остаются компактными, длинные (пути, URL) тянутся по ширине.
        width = FIELD_WIDTHS.get(key, width)
        stretch = stretch or key in FIELD_STRETCH
        if kind is bool:
            var: tk.Variable = tk.BooleanVar(
                value=bool(self.settings.get(key, DEFAULTS.get(key, False))))
            widget: tk.Widget = ttk.Checkbutton(parent, variable=var,
                                                command=on_change)
            sticky = "w"
        else:
            # Значение по умолчанию подставляется сразу, поэтому в поле никогда
            # не может оказаться пустая строка.
            initial = self.settings.get(key, DEFAULTS.get(key, ""))
            var = tk.StringVar(value="" if initial is None else str(initial))
            widget = ttk.Entry(parent, textvariable=var, width=width)
            if stretch:
                # Длинное значение тянется по доступной ширине, а при узком окне
                # сжимается (путь виден прокруткой внутри поля).
                parent.columnconfigure(column * 2 + 1, weight=1, minsize=90)
                sticky = "ew"
            else:
                # Колонка значений одной ширины: столбцы полей остаются
                # выровненными и не разъезжаются при смене подписей.
                parent.columnconfigure(column * 2 + 1, minsize=FIELD_CELL_MIN)
                sticky = "w"
        widget.grid(row=row, column=column * 2 + 1, sticky=sticky, pady=2,
                    padx=(0, 12))
        self._vars[key] = var
        self._field_types.append((key, var, kind))
        self._field_widgets[key] = widget
        if hint:
            ToolTip(widget, hint)
        return widget

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        tab_hint = ttk.Label(
            parent, style="Hint.TLabel", justify="left",
            text=(t("Настройки разделены по блокам; если блок не помещается, "
                  "прокручивайте список колесом мыши или ползунком справа.")))
        tab_hint.pack(anchor="w", pady=(0, 4))
        adaptive_wraplength(tab_hint, parent, margin=24, minimum=240)
        scroller = ScrollableFrame(parent, height=420)
        scroller.pack(fill="both", expand=True)
        inner = scroller.inner

        interface = self._section(inner, t("Интерфейс"))
        lang_row = ttk.Frame(interface)
        lang_row.pack(fill="x", pady=2)
        ttk.Label(lang_row, text=t("Язык интерфейса:")).pack(side="left",
                                                           padx=(0, 8))
        self.language_var = tk.StringVar(
            value=language_label(self.settings.get("language",
                                                   DEFAULT_LANGUAGE)))
        lang_combo = ttk.Combobox(
            lang_row, state="readonly", width=16,
            textvariable=self.language_var,
            values=[name for _code, name in available_languages()])
        lang_combo.pack(side="left")
        lang_combo.bind("<<ComboboxSelected>>",
                        lambda _event: self._apply_language_setting())
        ttk.Label(lang_row, style="Hint.TLabel",
                  text=t("Смена языка применится после перезапуска программы.")
                  ).pack(side="left", padx=(10, 0))

        sources = self._section(inner, t("Источники и расписание"))
        self._field(sources, 0, 0, t("Обновлять подписки, ч:"),
                    "simple_source_refresh_hours", float,
                    t("Как часто заново скачивать списки и добавлять новые серверы."))
        self._field(sources, 0, 1, t("Тайм-аут загрузки, с:"),
                    "simple_fetch_timeout", float)
        self._field(sources, 1, 0, "User-Agent:", "simple_user_agent", str,
                    t("Некоторые панели отдают списки только известным клиентам."))
        self._field(sources, 1, 1, t("Запускать цикл при старте:"),
                    "simple_auto_start", bool)

        policy = self._section(inner, t("Политика повторных проверок"))
        self._field(policy, 0, 0, t("Неудач подряд до удаления:"),
                    "simple_max_failures", int,
                    t("Сколько раз сервер может подряд провалить этап, прежде "
                    "чем он получит статус «Не работает» (или будет удалён, "
                    "если включено автоудаление)."))
        self._field(policy, 0, 1, t("Удалять нерабочие автоматически:"),
                    "simple_delete_failed", bool,
                    t("Выключено: сервер, исчерпавший попытки, остаётся в списке "
                    "со статусом «Не работает» — его видно, можно удалить "
                    "вручную или внести адрес в чёрный список. Включено: "
                    "записи удаляются сами, как раньше."))
        self._field(policy, 2, 1, t("Пауза между попытками, мин:"),
                    "simple_retry_minutes", float)
        self._field(policy, 1, 0, t("Пинг рабочих серверов, мин:"),
                    "simple_ping_interval_minutes", float)
        self._field(policy, 1, 1, t("Трафик рабочих серверов, ч:"),
                    "simple_traffic_interval_hours", float)
        self._field(policy, 2, 0, t("Не добавлять удалённые, ч:"),
                    "simple_blacklist_hours", float,
                    t("0 — выключить блокировку повторного добавления."))
        self._field(policy, 5, 0, t("Доля рабочих за проход:"),
                    "simple_maintenance_fraction", float,
                    t("Какую часть уже рабочих серверов подтверждать за один "
                    "проход (0.25 — четверть). Остальные в этом проходе не "
                    "проверяются: сбой пачки не может оставить выходной файл "
                    "без серверов."))
        self._field(policy, 5, 1, t("Серверов в пачке:"),
                    "simple_maintenance_max", int,
                    t("Сколько серверов кладётся в один процесс ядра при "
                    "плановой перепроверке рабочих: один процесс обслуживает "
                    "всю пачку, у каждого сервера свой локальный порт."))
        self._field(policy, 6, 0, t("Порог сбоя пачки:"),
                    "simple_maintenance_guard", float,
                    t("Если за проход провалилась большая доля рабочих (0.5 — "
                    "половина), это считается сбоем связи или ядра: статусы "
                    "серверов не меняются, результаты не применяются."))
        self._field(policy, 6, 1, t("Сетевой предохранитель:"),
                    "simple_network_guard", bool,
                    t("Включено: при пропавшей сети проверки приостанавливаются, "
                    "а их результаты не применяются — иначе обрыв связи "
                    "«разжаловал» бы все рабочие серверы."))

        ttk.Label(policy, text=t("Порядок этапов:")).grid(
            row=3, column=0, sticky="w", padx=(0, 6), pady=2)
        self.stage_mode_var = tk.StringVar(
            value=STAGE_MODE_LABEL_BY_VALUE.get(
                str(self.settings.get("simple_stage_mode", "cascade")).lower(),
                list(STAGE_MODE_LABELS)[0]))
        stage_combo = ttk.Combobox(
            policy, textvariable=self.stage_mode_var, state="readonly",
            width=28, values=list(STAGE_MODE_LABELS))
        stage_combo.grid(row=3, column=1, sticky="w", pady=2, padx=(0, 18))
        ToolTip(stage_combo,
                t("«Каскад» — за один круг по одной порции каждого этапа: список "
                "быстро чистится пингом, ядро получает только выживших.\n"
                "«Фазы» — за круг этап доводится до конца: сначала весь пинг, "
                "затем всё ядро, затем трафик."))

        fallbacks = self._field(policy, 4, 0, t("Пробовать запасные цели:"),
                                "traffic_fallbacks", bool, "", width=34)
        ToolTip(fallbacks,
                t("Включено: если цель контрольного запроса не ответила, "
                "проверяются ещё три резервные (gstatic, connectivitycheck, "
                "cp.cloudflare) — меньше ложных провалов, но больше исходящих "
                "подключений.\n"
                "Выключено: запрос идёт только на цель из настроек (ниже, в "
                "разделе «Ядра и контрольный трафик») — заметно меньше сетевой "
                "активности, но цель, заблокированная на стороне сервера, будет "
                "выглядеть как нерабочий сервер."))

        queue = self._section(inner, t("Очередь и производительность"))
        self._field(queue, 0, 0, t("Потоков пинга:"), "simple_ping_workers", int)
        self._field(queue, 0, 1, t("Порция пинга:"), "simple_ping_batch", int)
        self._field(queue, 1, 0, t("Потоков ядра:"), "simple_core_workers", int,
                    t("Каждый поток запускает отдельный процесс ядра — "
                    "не завышайте."))
        self._field(queue, 1, 1, t("Порция ядра:"), "simple_core_batch", int)
        self._field(queue, 2, 0, t("Порция трафика:"), "simple_traffic_batch", int)
        self._field(queue, 2, 1, t("Пауза цикла, с:"), "simple_loop_sleep", float,
                    t("Пауза между кругами, пока идут проверки.  Если проверять "
                    "нечего, цикл спит дольше — до ближайшей даты в очереди "
                    "(не более 5 секунд)."))
        self._field(queue, 3, 0, t("Срок «Не работает», ч:"),
                    "simple_failed_ttl_hours", float,
                    t("Через сколько часов автоматически убирать из пула серверы "
                    "со статусом «Не работает»: они больше не опрашиваются и "
                    "без срока годности копятся в pool.json вечно.\n"
                     "Копить их полезно: видно масштаб и можно выбрать адреса "
                     "для чёрного списка (особенно при сжатом пуле).\n"
                    "0 — не удалять такие записи автоматически."))
        self._field(queue, 3, 1, t("Писать пул не чаще, с:"),
                    "simple_save_min_seconds", float,
                    t("Пул — это резервный снимок на случай перезагрузки, сбоя или "
                    "закрытия программы.  Пока цикл работает, актуальные статусы "
                    "серверов хранятся в оперативной памяти, поэтому интервал "
                    "записи не влияет ни на проверки, ни на таблицу «Серверы», ни "
                    "на выходной файл — он ограничивает только частоту сброса "
                    "снимка на диск.\n"
                    "Файл переписывается целиком (тысячи серверов — десятки "
                    "мегабайт за одну запись), поэтому частая запись зря "
                    "изнашивает SSD при работе месяцами.  По умолчанию 30 с "
                    "(≈40 ГБ в сутки в худшем случае), 300 с — около 4 ГБ.\n"
                    "При остановке пул сохраняется всегда, каким бы ни был "
                    "интервал.  0 — писать сразу после каждого круга."))

        self._field(queue, 4, 0, t("Сжимать пул (gzip):"), "simple_pool_gzip",
                    bool,
                    t("Файл пула пишется как pool.json.gz: он примерно в 6 раз "
                    "меньше, поэтому меньше запись на диск и место, а проверки "
                    "не страдают — сжатие идёт редко и вне блокировки пула.\n"
                    "Заглянуть внутрь можно 7-Zip/WinRAR или командой "
                    "«python simple_main.py --pool-dump».  При выключении "
                    "вернётся обычный pool.json; при переключении данные "
                    "не теряются — оба формата читаются всегда."))
        cores = self._section(inner, t("Ядра и контрольный трафик"))
        self._field(cores, 0, 0, t("Путь к xray:"), "xray_path", str,
                    t("Пусто — ядро ищется в папке core проекта и в PATH."))
        self._field(cores, 0, 1, t("Путь к sing-box:"), "singbox_path", str)
        self._field(cores, 1, 0, t("Скачивать ядра автоматически:"),
                    "auto_download_core", bool)
        self._field(cores, 1, 1, t("Тайм-аут проверки ядром, с:"),
                    "core_timeout", float)
        self._field(cores, 2, 0, t("Цель контрольного запроса:"),
                    "traffic_url", str,
                    t("Основная цель контрольного запроса. Если она не ответит, "
                    "будут проверены резервные — это включает переключатель "
                    "«Пробовать запасные цели» в разделе «Политика повторных "
                    "проверок»."), width=34)

        self.cores_info = ttk.Label(
            cores, text=t("Ядро: определяется…"), style="Hint.TLabel",
            justify="left")
        self.cores_info.grid(row=3, column=0, columnspan=4, sticky="w",
                             pady=(8, 2))
        adaptive_wraplength(self.cores_info, cores, margin=24, minimum=240)
        ttk.Button(cores, text=t("Определить ядра"),
                   command=lambda: self._background(self._detect_cores)).grid(
            row=4, column=0, sticky="w", pady=2)
        ttk.Button(cores, text=t("Скачать ядро Xray"),
                   command=lambda: self._download_core("xray")).grid(
            row=4, column=1, sticky="w", pady=2)
        ttk.Button(cores, text=t("Скачать sing-box"),
                   command=lambda: self._download_core("singbox")).grid(
            row=4, column=2, sticky="w", pady=2)

        protocols = self._section(inner, t("Протоколы для импорта"))
        allowed = enabled_simple_protocols(self.settings)
        for index, (value, label) in enumerate(protocol_labels()):
            var = tk.BooleanVar(value=value in allowed)
            self._protocol_vars[value] = var
            ttk.Checkbutton(protocols, text=label, variable=var).grid(
                row=index // 4, column=index % 4, sticky="w",
                padx=(0, 16), pady=1)

        output = self._section(inner, t("Выходной файл"))
        # Пути к файлам занимают всю доступную ширину: при узком окне поле
        # растягивается, а не уезжает за правый край (длинный путь показывается
        # прокруткой внутри поля).
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text=t("Что сохранять:")).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        self.output_format_var = tk.StringVar(
            value=OUTPUT_FORMAT_LABEL_BY_VALUE.get(
                str(self.settings.get("simple_output_format",
                                      OUTPUT_URI)).lower(),
                list(OUTPUT_FORMAT_LABELS)[0]))
        format_combo = ttk.Combobox(
            output, textvariable=self.output_format_var, state="readonly",
            width=28, values=list(OUTPUT_FORMAT_LABELS))
        format_combo.grid(row=0, column=1, sticky="w", pady=2, padx=(0, 18))
        ToolTip(format_combo,
                t("«Список ссылок» — обычный текстовый файл со ссылками.\n"
                "«Конфигурация для v2rayN» — готовый JSON, который v2rayN "
                "импортирует офлайн как пользовательскую конфигурацию: одна "
                "запись со всеми рабочими серверами внутри.\n"
                "«Оба файла» — и то, и другое."))

        ttk.Label(output, text=t("Файл со списком ссылок:")).grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        list_var = tk.StringVar()
        ttk.Entry(output, textvariable=list_var).grid(
            row=1, column=1, sticky="ew", pady=2)
        ttk.Button(output, text=t("Обзор…"),
                   command=lambda: self._browse_output(
                       list_var, t("Список рабочих конфигураций"), "working.txt",
                       [(t("Текстовые файлы"), "*.txt"), (t("Все файлы"), "*.*")])
                   ).grid(row=1, column=2, padx=(6, 0))
        self._vars["simple_output_path"] = list_var
        self._field_types.append(("simple_output_path", list_var, str))

        ttk.Label(output, text=t("Файл для v2rayN (json):")).grid(
            row=2, column=0, sticky="w", padx=(0, 6), pady=2)
        json_var = tk.StringVar()
        ttk.Entry(output, textvariable=json_var).grid(
            row=2, column=1, sticky="ew", pady=2)
        ttk.Button(output, text=t("Обзор…"),
                   command=lambda: self._browse_output(
                       json_var, t("Конфигурация для v2rayN"), "v2rayN.json",
                       [(t("JSON-файлы"), "*.json"), (t("Все файлы"), "*.*")])
                   ).grid(row=2, column=2, padx=(6, 0))
        self._vars["simple_v2rayn_path"] = json_var
        self._field_types.append(("simple_v2rayn_path", json_var, str))

        v2rayn_hint = ttk.Label(output, text=V2RAYN_HINT, style="Hint.TLabel",
                                justify="left")
        v2rayn_hint.grid(row=3, column=0, columnspan=3, sticky="w",
                         pady=(6, 0))
        adaptive_wraplength(v2rayn_hint, output, margin=24, minimum=240)

        copy_hint = ttk.Label(
            output,
            text=(t("Нужны отдельные записи на каждый сервер, а не одна группа? "
                  "Нажмите «Скопировать рабочие» на панели сверху и в v2rayN "
                  "нажмите Ctrl+V («Серверы» → «Добавить сервер(а) из буфера "
                  "обмена») — v2rayN создаст по записи на каждую ссылку.")),
            style="Hint.TLabel", justify="left")
        copy_hint.grid(row=4, column=0, columnspan=3, sticky="w",
                       pady=(4, 0))
        adaptive_wraplength(copy_hint, output, margin=24, minimum=240)
        self._field(output, 5, 0, t("Строк в таблице серверов:"),
                    "simple_view_limit", int)

        window = self._section(inner, t("Окно, трей и сеть"))
        ttk.Label(window, style="Hint.TLabel", justify="left",
                  text=(t("Галочки этого раздела применяются сразу — нажимать "
                        "«Сохранить настройки» не нужно."))).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self._field(window, 1, 0, t("Иконка в трее:"), "simple_tray_icon", bool,
                    t("Иконка рядом с часами: окно можно убрать с панели задач, "
                    "а проверки и внешние команды продолжат работать."),
                    on_change=self._apply_tray_settings)
        self._field(window, 1, 1, t("Закрывать окно в трей:"),
                    "simple_close_to_tray", bool,
                    t("Крестик окна убирает его в трей вместо выхода (без "
                    "вопросов); выход — через меню иконки."),
                    on_change=self._apply_tray_settings)
        self._field(window, 2, 0, t("Сворачивать в трей:"),
                    "simple_minimize_to_tray", bool,
                    t("Кнопка «свернуть» убирает окно с панели задач."),
                    on_change=self._apply_tray_settings)
        self._field(window, 2, 1, t("Следить за сетью:"),
                    "simple_network_watch", bool,
                    t("Источник событий «Пропала сеть» и «Сеть восстановилась»: "
                    "короткая TCP-проверка раз в интервал."),
                    on_change=self._apply_network_settings)
        self._field(window, 3, 0, t("Проверка сети, с:"),
                    "simple_network_watch_seconds", float)
        self._field(window, 3, 1, t("Запускать при входе в Windows:"),
                    "simple_start_with_windows", bool,
                    t("Автозапуск без правки реестра: программа создаёт ярлык в "
                    "папке «Автозагрузка» пользователя (его видно в Проводнике "
                    "и в Диспетчере задач → «Автозагрузка»).  При входе в "
                    "систему она стартует свёрнутой в трей, а снятие галочки "
                    "этот ярлык удаляет."),
                    width=6, on_change=self._apply_autostart_settings)

        misc = self._section(inner, t("Журнал"))
        self._field(misc, 0, 0, t("Уровень:"), "log_level", str)
        self._field(misc, 0, 1, t("Писать журнал в файл:"), "log_to_file", bool)
        self._field(misc, 1, 0, t("Строк журнала:"), "max_log_lines", int)

        actions = ttk.Frame(inner)
        actions.pack(fill="x", pady=(0, 12))
        ttk.Button(actions, text=t("Сохранить настройки"), style="Big.TButton",
                   command=self._save_settings).pack(side="left")
        ttk.Button(actions, text=t("Обновить поля"),
                   command=self._load_settings_fields).pack(side="left", padx=6)

        # Прокрутка колесом мыши по всем полям вкладки.
        self.settings_scroller = scroller
        scroller.bind_wheel()

    def _browse_output(self, var: tk.StringVar, title: str,
                       initialfile: str, filetypes: list) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title=title,
            defaultextension=os.path.splitext(initialfile)[1],
            initialfile=initialfile,
            initialdir=os.path.dirname(var.get() or SIMPLE_OUTPUT_PATH),
            filetypes=filetypes)
        if path:
            var.set(path)

    def _apply_language_setting(self) -> None:
        """Запомнить выбранный язык; он применится после перезапуска."""
        code = language_code(self.language_var.get())
        self.settings.set("language", code)
        messagebox.showinfo(
            t("Язык интерфейса"),
            t("Язык будет изменён после перезапуска программы."),
            parent=self)

    def _load_settings_fields(self) -> None:
        for key, var, kind in self._field_types:
            value = self.settings.get(key, DEFAULTS.get(key))
            if kind is bool or isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("" if value is None else str(value))
        mode = str(self.settings.get("simple_stage_mode", "cascade")).lower()
        self.stage_mode_var.set(
            STAGE_MODE_LABEL_BY_VALUE.get(mode, list(STAGE_MODE_LABELS)[0]))
        fmt = str(self.settings.get("simple_output_format",
                                    OUTPUT_URI)).lower()
        self.output_format_var.set(
            OUTPUT_FORMAT_LABEL_BY_VALUE.get(fmt, list(OUTPUT_FORMAT_LABELS)[0]))
        allowed = enabled_simple_protocols(self.settings)
        for value, protocol_var in self._protocol_vars.items():
            protocol_var.set(value in allowed)
        self.language_var.set(
            language_label(self.settings.get("language", DEFAULT_LANGUAGE)))

    def _save_settings(self) -> None:
        """Сохранить настройки, попутно починив пустые и некорректные поля."""
        fields = [(key, var.get(), kind)
                  for key, var, kind in self._field_types]
        protocols = [value for value, _label in protocol_labels()
                     if self._protocol_vars[value].get()]
        stage_mode = STAGE_MODE_LABELS.get(self.stage_mode_var.get(), "cascade")
        output_format = OUTPUT_FORMAT_LABELS.get(self.output_format_var.get(),
                                                 OUTPUT_URI)
        values, repaired = collect_field_values(fields, protocols, stage_mode)
        values["simple_output_format"] = output_format

        for key, value in values.items():
            self.settings.set(key, value)
        # Страховка: те же правила применяются ко всем настройкам целиком.
        repaired = sorted(set(repaired) | set(ensure_defaults(self.settings)))

        self.supervisor.reload_policy()
        self.supervisor.prober.reset_cores()
        self.supervisor.reload_network_watch()
        self._apply_autostart_settings()
        self._setup_tray()
        core_manager.set_keep_debug_files(
            bool(self.settings.get("keep_core_debug_files", False)))
        setup_logging(
            str(self.settings.get("log_level", "INFO")),
            int(self.settings.get("max_log_lines", 2000)),
            bool(self.settings.get("log_to_file", False)),
        )
        # Поля показывают ровно то, что ушло в файл.
        self._load_settings_fields()
        self._apply_stats(self.supervisor.refresh_stats())
        log.info(t("Настройки сохранены."))
        if repaired:
            log.warning(t("Настройки: исправлены пустые или некорректные поля: %s"),
                        ", ".join(repaired))
            messagebox.showwarning(
                t("Настройки"),
                t("В следующих полях были пустые или некорректные значения — в них "
                "и в файл настроек записаны значения по умолчанию:\n\n")
                + "\n".join(repaired)
                + t("\n\nОстальные значения сохранены без изменений."),
                parent=self)
        else:
            messagebox.showinfo(t("Настройки"), t("Настройки сохранены."), parent=self)

    # ------------------------------------------------------------------
    # Вкладка «Журнал»
    # ------------------------------------------------------------------
    def _build_log_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        self.log_scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text=t("Прокручивать автоматически"),
                        variable=self.log_scroll_var).pack(side="left")
        ttk.Button(top, text=t("Копировать журнал"),
                   command=self._copy_log).pack(side="right")
        ttk.Button(top, text=t("Очистить"),
                   command=self._clear_log).pack(side="right", padx=4)

        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = self._register_text(
            tk.Text(box, wrap="word", state="disabled"))
        scroll = ttk.Scrollbar(box, orient="vertical",
                               command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._bind_text_menu(self.log_text, (
            (t("Копировать весь журнал"), self._copy_log),
            (t("Очистить журнал"), self._clear_log),
        ))

        self.log_text.configure(state="normal")
        for line in buffer_logs()[-300:]:
            self.log_text.insert("end", line + "\n")
            self._log_lines += 1
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _copy_log(self) -> None:
        text = self.log_text.get("1.0", "end")
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo(t("Журнал"), t("Журнал скопирован в буфер обмена."),
                            parent=self)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_lines = 0

    # ------------------------------------------------------------------
    # Панель управления
    # ------------------------------------------------------------------
    def _start(self) -> None:
        if self.supervisor.running:
            if self.supervisor.paused:
                self.supervisor.resume()
            return
        if self.supervisor.start(refresh_sources=True):
            log.info(t("Автоматический цикл запущен: подписки обновляются по "
                     "расписанию, серверы проверяются по этапам."))
        self._apply_stats(self.supervisor.snapshot())

    def _toggle_pause(self) -> None:
        if not self.supervisor.running:
            return
        if self.supervisor.paused:
            self.supervisor.resume()
        else:
            self.supervisor.pause()

    def _stop(self) -> None:
        self.supervisor.stop()
        self.pool.save(force=True)
        self._reload_pool()
        self._apply_stats(self.supervisor.snapshot())

    def _run_once(self) -> None:
        if self.supervisor.running:
            log.info(t("Цикл уже выполняется — он проверяет серверы "
                     "самостоятельно."))
            return
        if self._busy_round:
            return
        self._busy_round = True
        log.info(t("Запущен одиночный проход."))

        def runner() -> None:
            try:
                self.supervisor.run_round()
            except Exception as exc:  # noqa: BLE001
                log.error(t("Одиночный проход: %s"), exc)
            finally:
                self._ui(self._round_finished)

        threading.Thread(target=runner, daemon=True).start()

    def _round_finished(self) -> None:
        self._busy_round = False
        self._reload_pool()
        self._apply_stats(self.supervisor.snapshot())

    def _refresh_sources(self) -> None:
        log.info(t("Обновление списков из источников…"))
        self._background(self.supervisor.refresh_sources_now)

    def _write_output(self) -> None:
        self.supervisor.write_output_now()
        self._apply_stats(self.supervisor.snapshot())

    def _copy_working(self) -> None:
        """Скопировать рабочие ссылки — их v2rayN импортирует как отдельные серверы."""
        text = self.supervisor.working_uri_text()
        if not text:
            messagebox.showinfo(
                t("Буфер обмена"),
                t("Пока нет рабочих серверов: список пуст."), parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        count = len(self.supervisor.working_uris())
        log.info(t("В буфер обмена скопировано ссылок: %s. В v2rayN: «Серверы» → "
                 "«Добавить сервер(а) из буфера обмена» (Ctrl+V) — появится "
                 "отдельная запись на каждый сервер."), count)
        messagebox.showinfo(
            t("Буфер обмена"),
            t('Скопировано ссылок: {}.\n\nВ v2rayN нажмите Ctrl+V (или «Серверы» → «Добавить сервер(а) из буфера обмена») — каждый сервер добавится отдельной записью.').format(fmt_int(count)),
            parent=self)

    def _open_output(self) -> None:
        """Открыть файл, соответствующий выбранному формату вывода."""
        fmt = self.supervisor.output_format()
        candidates = [self.supervisor.writer.path,
                      self.supervisor.json_writer.path]
        if fmt == OUTPUT_V2RAYN:
            candidates.reverse()
        for path in candidates:
            if path and os.path.isfile(path):
                _open_path(path)
                return
        messagebox.showinfo(
            t("Вывод"),
            t("Файлы ещё не созданы:\n") + "\n".join(
                path for path in candidates if path), parent=self)

    # ------------------------------------------------------------------
    # Иконка в трее
    # ------------------------------------------------------------------
    def _store_setting(self, key: str) -> None:
        """Сохранить одну галочку сразу (раздел трея работает без «Сохранить»)."""
        var = self._vars.get(key)
        if var is None:
            return
        try:
            self.settings.set(key, var.get())
        except Exception:  # noqa: BLE001
            pass

    def _apply_tray_settings(self) -> None:
        """Галочки трея применяются и сохраняются немедленно."""
        for key in ("simple_tray_icon", "simple_close_to_tray",
                    "simple_minimize_to_tray"):
            self._store_setting(key)
        self._setup_tray()
        log.info(t("Трей: иконка %s, закрывать в трей %s, сворачивать %s"),
                 t("вкл") if self.settings.get("simple_tray_icon") else t("выкл"),
                 t("вкл") if self.settings.get("simple_close_to_tray") else t("выкл"),
                 t("вкл") if self.settings.get("simple_minimize_to_tray")
                 else t("выкл"))

    def _apply_network_settings(self) -> None:
        """Галочка слежки за сетью применяется сразу, без «Сохранить»."""
        self._store_setting("simple_network_watch")
        self.supervisor.reload_network_watch()

    def _apply_autostart_settings(self) -> None:
        """Галочка автозапуска: создать или убрать ярлык в «Автозагрузке».

        Реестр не используется: меняется ровно один файл в папке автозагрузки
        пользователя, поэтому операция обратима и видна в Проводнике.
        """
        self._store_setting("simple_start_with_windows")
        enabled = bool(self.settings.get("simple_start_with_windows", False))
        ok, message = autostart.sync(enabled)
        if ok:
            log.info(t("Автозапуск при входе в Windows: %s"), message)
            return
        log.warning(t("Автозапуск при входе в Windows не изменён: %s"), message)
        messagebox.showwarning(
            t("Автозапуск"),
            t('Не удалось изменить автозапуск: {}\n\nГалочка сохранена, но изменить автозагрузку системы не получилось.').format(message),
            parent=self)

    def _setup_tray(self, force: bool = False) -> None:
        """Создать (или убрать) иконку в трее по текущим настройкам."""
        if self._closing:
            return
        want = bool(self.settings.get("simple_tray_icon", True))
        current = self.tray
        if not want:
            self._stop_tray()
            return
        if current is not None and current.active and not force:
            return
        self._stop_tray()
        tray = TrayIcon(tooltip=APP_TITLE, icon_path=self._icon_path,
                        on_activate=lambda: self._ui(self._toggle_window),
                        on_menu=lambda: self._ui(self._show_tray_menu))
        if not tray.start(timeout=2.0):
            log.info(t("Иконка в трее недоступна — окно работает как обычно."))
            return
        self.tray = tray
        self._tray_tooltip = ""
        self._tray_hint_shown = False
        self._update_tray_tooltip()

    def _stop_tray(self) -> None:
        tray = self.tray
        self.tray = None
        if tray is not None:
            try:
                tray.stop()
            except Exception:  # noqa: BLE001
                pass

    def _start_hidden(self) -> None:
        """Старт свёрнутым в трей (автозагрузка): окна не показываем вовсе.

        Если иконка в трее недоступна, окно остаётся видимым: иначе программу
        нельзя было бы ни найти, ни закрыть, кроме как через Диспетчер задач.
        """
        if not bool(self.settings.get("simple_tray_icon", True)):
            return
        self._setup_tray()
        tray = self.tray
        if tray is None or not tray.active:
            log.warning(t("Автозапуск: трей недоступен — окно будет открыто."))
            return
        self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        """Убрать окно с панели задач: программа продолжает работать."""
        try:
            self.withdraw()
        except tk.TclError:
            return
        tray = self.tray
        if tray is None:
            return
        log.info(t("Окно убрано в трей — автоматизация продолжается."))
        if not self._tray_hint_shown:
            # Как у большинства программ: один раз объясняем, куда делось окно.
            self._tray_hint_shown = True
            try:
                tray.notify(
                    t("Программа продолжает работать"),
                    t("Двойной клик по иконке вернёт окно, меню — по правой "
                    "кнопке. Выход — пункт «Выход» в этом меню."))
            except Exception:  # noqa: BLE001
                pass

    def _on_second_launch(self) -> None:
        """Второй экземпляр попросил показаться: поднять окно из трея."""
        self._ui(self._show_from_tray)

    def _show_from_tray(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except tk.TclError:
            return

    def _toggle_window(self) -> None:
        try:
            hidden = self.state() == "withdrawn"
        except tk.TclError:
            return
        if hidden:
            self._show_from_tray()
        else:
            self._hide_to_tray()

    def _on_unmap(self, _event=None) -> None:
        """Сворачивание окна: по настройке сразу убираем его в трей."""
        if self._closing:
            return
        tray = self.tray
        if tray is None or not tray.active:
            return
        if not bool(self.settings.get("simple_minimize_to_tray", False)):
            return
        try:
            if self.state() != "iconic":
                return
        except tk.TclError:
            return
        # Окно уже свёрнуто: сразу убираем его и с панели задач.
        self.after(1, self._hide_to_tray)

    def _show_tray_menu(self) -> None:
        """Меню иконки: те же действия, что и в шапке окна."""
        self._tray_menu = self._build_tray_menu()
        try:
            self._tray_menu.tk_popup(self.winfo_pointerx(),
                                     self.winfo_pointery())
        finally:
            try:
                self._tray_menu.grab_release()
            except tk.TclError:
                pass

    def _build_tray_menu(self) -> tk.Menu:
        """Собрать меню иконки (отдельно от показа — так его видно в тестах)."""
        menu = tk.Menu(self, tearoff=0)
        self._style_menu(menu)
        menu.add_command(label=t("Показать окно"), command=self._show_from_tray)
        menu.add_separator()
        menu.add_command(label=t("▶ Запустить"), command=self._start)
        menu.add_command(label=t("⏸ Пауза / продолжить"),
                         command=self._toggle_pause)
        menu.add_command(label=t("■ Остановить"), command=self._stop)
        menu.add_command(label=t("Сохранить вывод"), command=self._write_output)
        menu.add_separator()
        menu.add_checkbutton(label=t("Ночная тема"), variable=self.theme_var,
                             command=self._toggle_theme)
        menu.add_separator()
        menu.add_command(label=t("Выход"), command=lambda: self._quit(ask=False))
        return menu

    def _update_tray_tooltip(self, stats: Optional[SimpleStats] = None) -> None:
        """Подсказка иконки: состояние цикла и число рабочих серверов."""
        tray = self.tray
        if tray is None:
            return
        if stats is None:
            stats = self._stats
        if stats.running:
            state = t("пауза") if stats.paused else t("работает")
        else:
            state = t("остановлено")
        text = (t('{}: {}, круг {}, рабочих {}').format(APP_TITLE, state, stats.round_index, stats.working))
        if not stats.network_online:
            text += t(" · нет сети, проверки на паузе")
        if text != self._tray_tooltip:
            self._tray_tooltip = text
            tray.update(text)

    def _on_close(self, ask: bool = True) -> None:
        """Крестик окна: уйти в трей (если так настроено) или выйти совсем."""
        # «Закрывать окно в трей» — ожидаемое поведение настройки, поэтому
        # никаких вопросов про остановку цикла здесь не задаём.
        if (not self._closing
                and bool(self.settings.get("simple_close_to_tray", False))):
            if self.tray is None or not self.tray.active:
                # Иконки может ещё не быть — пробуем поднять её прямо сейчас.
                self._setup_tray()
            if self.tray is not None and self.tray.active:
                self._hide_to_tray()
                return
            log.warning(t("Закрытие в трей включено, но трей недоступен: "
                        "приложение будет закрыто."))
            messagebox.showwarning(
                t("Трей недоступен"),
                t("Иконку в трее создать не удалось, поэтому сворачивать окно "
                "некуда — приложение будет закрыто."), parent=self)
        self._quit(ask=ask)

    def _quit(self, ask: bool = True) -> None:
        """Полный выход: остановить цикл, убрать иконку, закрыть окно.

        Вызывается пунктом «Выход» в меню иконки и (при выключенной настройке)
        крестиком окна; настройка «закрывать в трей» здесь уже не действует.
        """
        if ask and self.supervisor.running and not messagebox.askyesno(
                t("Выход"), t("Цикл проверки выполняется. Остановить и выйти?"),
                parent=self):
            return
        self._closing = True
        self._stop_tray()
        try:
            self.supervisor.stop()
        except Exception:
            pass
        try:
            self.supervisor.commands.stop(timeout=5.0)
        except Exception:
            pass
        if self._log_handler is not None:
            try:
                unsubscribe_logs(self._log_handler)
            except Exception:
                pass
            self._log_handler = None
        try:
            self.pool.save(force=True)
        except Exception:
            pass
        if self._guard is not None:
            # Место единственного экземпляра освобождаем явно (обычно это
            # делает система при выходе процесса).
            try:
                self._guard.close()
            except Exception:
                pass
        try:
            self.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------

def run(start_in_tray: bool = False, guard=None) -> None:
    """Запустить графический интерфейс simple-версии.

    ``start_in_tray`` используется автозагрузкой (``--tray``): окно сразу
    убирается в трей, а цикл проверок продолжает работать.  ``guard`` — защита
    от второго запуска (см. ``simple/single.py``): она же будит это окно, если
    программу запустили повторно.
    """
    app = SimpleApplication(start_in_tray=start_in_tray, guard=guard)
    app.mainloop()
