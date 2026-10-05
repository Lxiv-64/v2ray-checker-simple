"""Shared helpers for the Tkinter interface."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional
from app.i18n import t


# ---------------------------------------------------------------------------
# Горячие клавиши, независимые от раскладки
# ---------------------------------------------------------------------------

#: Виртуальные коды клавиш Windows (``event.keycode``).  В отличие от keysym они
#: одинаковы для всех раскладок: при русской раскладке Ctrl+V приходит с чужим
#: keysym (а иногда и вовсе с ``'??'``), поэтому привязки вида ``<Control-v>``
#: молча не срабатывают.
VK_ACTIONS = {
    65: "select_all",   # A (в русской раскладке — Ф)
    67: "copy",         # C (С)
    84: "theme",        # T (Е) — быстрая смена день/ночь
    86: "paste",        # V (М)
    88: "cut",          # X (Ч)
    46: "delete",       # Delete
    45: "insert",       # Insert
    113: "rename",      # F2
    116: "refresh",     # F5
    112: "help",        # F1
}

#: Действия, которые Tk умеет выполнять в полях ввода сам.
VIRTUAL_EVENTS = {
    "select_all": "<<SelectAll>>",
    "copy": "<<Copy>>",
    "cut": "<<Cut>>",
    "paste": "<<Paste>>",
}

#: Классы виджетов, где Ctrl-действия должны оставаться стандартными.
TEXT_WIDGET_CLASSES = {"Entry", "TEntry", "Text", "Spinbox", "TSpinbox",
                       "TCombobox", "Listbox", "TListbox"}

#: Действия, которые допустимы и вместе с Ctrl: остальные Ctrl-комбинации в
#: таблицах игнорируются, чтобы Ctrl+Del, например, не удалял строки.
CONTROL_ACTIONS = {"theme"}

MASK_SHIFT = 0x0001
MASK_CONTROL = 0x0004
MASK_ALT = 0x0008 | 0x20000


def pressed_action(event: tk.Event) -> str:
    """Логическое действие нажатой клавиши — по коду, а не по раскладке."""
    try:
        keycode = int(getattr(event, "keycode", 0) or 0)
    except (TypeError, ValueError):
        keycode = 0
    action = VK_ACTIONS.get(keycode, "")
    if action:
        return action
    keysym = str(getattr(event, "keysym", "") or "").lower()
    return {"f2": "rename", "f5": "refresh", "delete": "delete"}.get(keysym, "")


def has_control(event: tk.Event) -> bool:
    return bool(int(getattr(event, "state", 0) or 0) & MASK_CONTROL)


def has_shift(event: tk.Event) -> bool:
    return bool(int(getattr(event, "state", 0) or 0) & MASK_SHIFT)


def has_alt(event: tk.Event) -> bool:
    return bool(int(getattr(event, "state", 0) or 0) & MASK_ALT)


def is_text_widget(widget: Any) -> bool:
    """Поле ввода/многострочный текст — там свои стандартные действия."""
    try:
        return widget.winfo_class() in TEXT_WIDGET_CLASSES
    except Exception:  # noqa: BLE001 - виджет мог быть уже уничтожен
        return False


def bind_hotkeys(root: tk.Misc,
                 handlers: Dict[str, Callable[[tk.Event], Any]]) -> None:
    """Привязать типовые клавиши так, чтобы раскладка не мешала.

    ``handlers`` — словарь «логическое действие → обработчик» (см.
    :data:`VK_ACTIONS`).  В полях ввода Ctrl-действия не перехватываются, а
    превращаются в стандартные виртуальные события Tk, поэтому копирование и
    вставка работают и при русской раскладке.  Обработчик может вернуть
    ``"break"``, чтобы остановить дальнейшую обработку события.
    """
    def dispatch(event: tk.Event) -> Optional[str]:
        action = pressed_action(event)
        if not action:
            return None
        widget = event.widget
        control = has_control(event)
        if is_text_widget(widget):
            if control and action in VIRTUAL_EVENTS:
                try:
                    widget.event_generate(VIRTUAL_EVENTS[action])
                except tk.TclError:
                    return None
                return "break"
            return None                     # Delete и прочее — как в Tk
        if action in VIRTUAL_EVENTS and not control:
            return None
        if (action not in VIRTUAL_EVENTS and control
                and action not in CONTROL_ACTIONS):
            return None
        handler = handlers.get(action)
        if handler is None:
            return None
        result = handler(event)
        return "break" if result is None else result

    for sequence in ("<Control-KeyPress>", "<KeyPress>"):
        root.bind_all(sequence, dispatch, add="+")


STATE_COLORS = {
    "new": "#6c757d",
    "unreachable": "#b02a37",
    "reachable": "#6f42c1",
    "suspect": "#cc8800",
    "connected": "#0d6efd",
    "working": "#198754",
    "dead": "#495057",
}

STATE_LABELS = {
    "new": t("Новая"),
    "unreachable": t("Адрес недоступен"),
    "reachable": t("Пинг OK (кандидат)"),
    "suspect": t("Сомнительная"),
    "connected": t("Соединение есть"),
    "working": t("Рабочая"),
    "dead": t("Мёртвая"),
}


def human_time(timestamp: float) -> str:
    if not timestamp:
        return "—"
    delta = time.time() - timestamp
    if delta < 0:
        return time.strftime("%d.%m %H:%M", time.localtime(timestamp))
    if delta < 60:
        return t("только что")
    if delta < 3600:
        return t('{} мин назад').format(int(delta // 60))
    if delta < 86400:
        return t('{} ч назад').format(int(delta // 3600))
    return time.strftime("%d.%m %H:%M", time.localtime(timestamp))


def human_latency(latency: Optional[int]) -> str:
    if latency is None:
        return "—"
    if latency < 1000:
        return t('{} мс').format(latency)
    return t('{:.2f} с').format(latency / 1000)


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


#: Цвета всплывающих подсказок: интерфейс меняет их при смене темы (день/ночь),
#: поэтому это словарь, а не константы в разметке.
TOOLTIP_COLORS = {"background": "#ffffe0", "foreground": "#000000"}


class ToolTip:
    """Small hover tooltip for arbitrary widgets."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 550) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self._tip,
            text=self.text,
            justify="left",
            background=TOOLTIP_COLORS["background"],
            foreground=TOOLTIP_COLORS["foreground"],
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=6,
            pady=3,
            wraplength=420,
        )
        label.pack()

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def fit_window(window: tk.Misc, width: int, height: int,
               min_width: int = 0, min_height: int = 0,
               margin_x: int = 60, margin_y: int = 110) -> None:
    """Задать размер окна, который заведомо помещается на экране.

    На небольших экранах (например, 1366x768) окно «по паспорту» оказывается
    больше рабочей области: нижняя часть с кнопками и строка состояния уезжают
    за границу, и добраться до них можно только растягиванием окна вслепую.
    Поэтому желаемый размер уменьшается до доступного, и во столько же
    уменьшается минимальный размер окна.
    """
    try:
        screen_w = int(window.winfo_screenwidth())
        screen_h = int(window.winfo_screenheight())
    except Exception:  # noqa: BLE001 - окно могло быть уже уничтожено
        return
    avail_w = max(480, screen_w - int(margin_x))
    avail_h = max(320, screen_h - int(margin_y))
    actual_w = min(int(width), avail_w)
    actual_h = min(int(height), avail_h)
    limit_w = min(max(320, int(min_width or 0)), actual_w)
    limit_h = min(max(240, int(min_height or 0)), actual_h)
    window.geometry(f"{actual_w}x{actual_h}")
    window.minsize(limit_w, limit_h)


def adaptive_wraplength(widget: tk.Widget, container: Optional[tk.Widget] = None,
                        margin: int = 40, minimum: int = 220) -> Callable:
    """Переносить текст метки по фактической ширине контейнера.

    ``wraplength`` в Tkinter задаётся в пикселях и сам не подстраивается: при
    узком окне метка с фиксированным ``wraplength`` оказывается шире контейнера
    и её правый край уезжает за границу.  Возвращает функцию пересчёта — её
    можно вызвать вручную после изменения текста.
    """
    holder = container if container is not None else widget.master
    if holder is None:
        return lambda: None

    def apply(_event: object = None) -> None:
        try:
            width = int(holder.winfo_width())
        except Exception:  # noqa: BLE001
            return
        if width <= 1:
            return
        try:
            widget.configure(wraplength=max(int(minimum), width - int(margin)))
        except Exception:  # noqa: BLE001 - виджет мог быть уничтожен
            return

    holder.bind("<Configure>", apply, add="+")
    apply()
    return apply


class AdaptiveBar(ttk.Frame):
    """Панель кнопок с «переносом строк»: элементы не уезжают за край окна.

    В Tkinter нет автоматического переноса виджетов, поэтому при узком окне
    правая часть панели просто исчезает за границей.  Панель хранит порядок
    элементов и при каждой смене ширины раскладывает их по строкам так, чтобы
    каждый остался видимым.

    Раскладка идёт через ``grid`` прямо в панели: элементы должны иметь эту
    панель своим родителем.  Промежуточные фреймы-строки здесь не годятся —
    виджеты, упакованные «внутрь» фрейма-соседа через ``pack(in_=...)``,
    оказываются под ним и перестают быть видимыми.
    """

    def __init__(self, parent: tk.Widget, padding: Any = 0, gap: int = 4,
                 **kwargs: Any) -> None:
        super().__init__(parent, padding=padding, **kwargs)
        self.gap = int(gap)
        self._items: List[tk.Widget] = []
        self._rendered: List[List[int]] = []
        self._busy = False
        self._last_width = 0
        self.bind("<Configure>", self._on_configure, add="+")

    # -- сборка ----------------------------------------------------------
    def add(self, widget: tk.Widget) -> tk.Widget:
        """Добавить элемент в поток (порядок — порядок добавления)."""
        self._items.append(widget)
        # Ширина кнопки меняется вместе с текстом («Показать вывод» →
        # «Скрыть вывод»), поэтому следим и за размерами самих элементов.
        try:
            widget.bind("<Configure>", self._on_child_configure, add="+")
        except Exception:  # noqa: BLE001
            pass
        self.after_idle(self.relayout)
        return widget

    def items(self) -> List[tk.Widget]:
        """Элементы панели в порядке добавления."""
        return list(self._items)

    # -- раскладка -------------------------------------------------------
    def _on_configure(self, event: tk.Event) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width == self._last_width:
            return
        self._last_width = width
        self.relayout()

    def _on_child_configure(self, _event: object = None) -> None:
        self.relayout()

    def relayout(self) -> None:
        """Пересобрать строки, если текущая ширина этого требует."""
        if self._busy:
            return
        try:
            if not self.winfo_exists():
                return
            width = int(self.winfo_width())
        except Exception:  # noqa: BLE001 - окно могло быть уже закрыто
            return
        if width <= 1:
            return
        plan = self._plan_for(width)
        if plan == self._rendered:
            return
        self._render(plan)

    def _plan_for(self, width: int) -> List[List[int]]:
        """Разложить элементы по строкам «жадно»: хотя бы один в строке."""
        plan: List[List[int]] = []
        row: List[int] = []
        used = 0
        for index, widget in enumerate(self._items):
            try:
                needed = int(widget.winfo_reqwidth()) + self.gap
            except Exception:  # noqa: BLE001
                needed = self.gap
            if row and used + needed > width:
                plan.append(row)
                row, used = [], 0
            row.append(index)
            used += needed
        if row or not plan:
            plan.append(row)
        return plan

    def _render(self, plan: List[List[int]]) -> None:
        """Разложить элементы по строкам сетки прямо в панели."""
        self._busy = True
        try:
            for widget in self._items:
                widget.grid_forget()
            for row_index, members in enumerate(plan):
                for column, item_index in enumerate(members):
                    self._items[item_index].grid(
                        row=row_index, column=column, sticky="w",
                        padx=(0, self.gap))
            self._rendered = plan
        except tk.TclError:
            # Часть виджетов могла быть уничтожена (окно закрывается) —
            # раскладка больше не нужна.
            self._rendered = []
        finally:
            self._busy = False



class ScrollableFrame(ttk.Frame):
    """A vertically scrollable frame that fills its parent width.

    Полоса прокрутки показывается только когда содержимое выше видимой части, а
    колесо мыши не отбирается у виджетов со своей прокруткой: над
    ``Text``/``Treeview``/``Listbox`` событие достаётся им, и лишь на границе
    их прокрутки двигается внешний контейнер.
    """

    #: Виджеты, у которых есть собственная вертикальная прокрутка.
    SELF_SCROLLING = {"Text", "Treeview", "Listbox", "Canvas"}

    def __init__(self, parent: tk.Widget, height: int = 260, **kwargs: Any) -> None:
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0,
                                yscrollincrement=24)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self._window_size = (0, 0)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._bar_visible = False
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def set_palette(self, background: str) -> None:
        """Перекрасить фон области прокрутки (используется ночной темой).

        ``Canvas`` — классический виджет Tk и не наследует стили ``ttk``,
        поэтому его фон задаётся явно.
        """
        try:
            self.canvas.configure(background=background,
                                  highlightbackground=background)
        except tk.TclError:
            pass

    def _on_scroll(self, first: Any, last: Any) -> None:
        """Отдать положение полосе и убрать её, если прокручивать нечего."""
        self.scrollbar.set(first, last)
        try:
            visible = float(first) <= 0.0 and float(last) >= 1.0
        except (TypeError, ValueError):
            visible = True
        if visible and self._bar_visible:
            self.scrollbar.pack_forget()
            self._bar_visible = False
        elif not visible and not self._bar_visible:
            self.scrollbar.pack(side="right", fill="y")
            self._bar_visible = True

    def _on_inner_configure(self, _event: object = None) -> None:
        self._sync_window_size()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._sync_window_size()

    def _sync_window_size(self) -> None:
        """Содержимое заполняет видимую часть, если оно туда помещается.

        Иначе вкладка выглядела бы «прокручиваемой», даже когда места хватает,
        и внутренние таблицы с ``expand`` не растягивались бы на большое окно.
        Если содержимое выше — высота остаётся естественной и появляется
        полоса прокрутки.
        """
        try:
            canvas_w = int(self.canvas.winfo_width())
            canvas_h = int(self.canvas.winfo_height())
            needed_h = int(self.inner.winfo_reqheight())
        except (tk.TclError, ValueError):
            return
        if canvas_w <= 1 or canvas_h <= 1:
            return
        target = (0, canvas_w) if needed_h >= canvas_h else (canvas_h, canvas_w)
        if target == self._window_size:
            return
        self._window_size = target
        try:
            self.canvas.itemconfigure(self._window, width=canvas_w,
                                      height=target[0])
        except tk.TclError:
            return

    # -- колесо мыши -----------------------------------------------------
    def bind_wheel(self, widget: Optional[tk.Widget] = None) -> None:
        """Прокручивать содержимое колесом мыши.

        Вызывается после того, как содержимое собрано: обработчик вешается на
        сам контейнер и на все вложенные виджеты, иначе колесо работает только
        над пустым местом, а над полями — нет.
        """
        target = widget if widget is not None else self.inner
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            target.bind(sequence, self._on_mousewheel, add="+")
        for child in target.winfo_children():
            self.bind_wheel(child)

    @staticmethod
    def _wheel_step(event: tk.Event) -> Optional[int]:
        number = getattr(event, "num", None)
        if number == 4:            # X11: вверх
            return -1
        if number == 5:            # X11: вниз
            return 1
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return None
        return -1 if delta > 0 else 1

    def _scrolls_itself(self, widget: tk.Widget, step: int) -> bool:
        """Может ли виджет сам прокрутиться в нужную сторону."""
        try:
            if widget.winfo_class() not in self.SELF_SCROLLING:
                return False
            first, last = widget.yview()
        except Exception:  # noqa: BLE001
            return False
        if step < 0:
            return float(first) > 0.0
        return float(last) < 1.0

    def _on_mousewheel(self, event: tk.Event):
        step = self._wheel_step(event)
        if step is None:
            return None
        if self._scrolls_itself(event.widget, step):
            return None            # у виджета есть куда прокрутиться — не мешаем
        self.canvas.yview_scroll(step, "units")
        return "break"             # не отдавать событие виджету (например, комбобоксу)


def make_treeview(parent: tk.Widget, columns: list, height: int = 14,
                  show: str = "headings") -> tuple:
    """Create a treeview with vertical + horizontal scrollbars in a frame."""
    container = ttk.Frame(parent)
    container.rowconfigure(0, weight=1)
    container.columnconfigure(0, weight=1)

    tree = ttk.Treeview(container, columns=[c[0] for c in columns],
                        show=show, height=height, selectmode="extended")
    for key, title, width, anchor in columns:
        tree.heading(key, text=title)
        tree.column(key, width=width, anchor=anchor, stretch=False)

    vbar = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
    hbar = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)

    tree.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    hbar.grid(row=1, column=0, sticky="ew")
    return container, tree


class Debouncer:
    """Run a callback only after a quiet period."""

    def __init__(self, widget: tk.Widget, delay_ms: int = 300) -> None:
        self.widget = widget
        self.delay_ms = delay_ms
        self._after_id: Optional[str] = None

    def call(self, func: Callable[[], None]) -> None:
        self.cancel()
        self._after_id = self.widget.after(self.delay_ms, func)

    def cancel(self) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None