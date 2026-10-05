"""Иконка в области уведомлений (трей) для окна Tkinter.

Реализовано на ``ctypes``: собственное скрытое окно в отдельном потоке и
``Shell_NotifyIconW``.  Внешних библиотек не требуется, а на не-Windows модуль
просто сообщает, что трей недоступен, и приложение работает как обычно.

Использование::

    tray = TrayIcon(tooltip="Проверка работает",
                    on_activate=lambda: ..., on_menu=lambda: ...)
    tray.start()
    tray.update(tooltip="Проверка остановлена")
    tray.stop()

Обработчики вызываются из потока трея, поэтому GUI обязан передавать туда
функции, которые перекладывают работу в главный поток Tk (например,
``lambda: self._ui(self._show_from_tray)``).
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional
from app.i18n import t


IS_WINDOWS = sys.platform.startswith("win")

# Сообщения и константы Windows.
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

#: Вид всплывающего уведомления от иконки.
NIIF_INFO = 0x00000001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

#: Сколько символов подсказки помещается в NOTIFYICONDATA.
TIP_LENGTH = 128


if IS_WINDOWS:  # pragma: no cover - проверяется на Windows
    import ctypes
    from ctypes import wintypes

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = (
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        )

    class NOTIFYICONDATAW(ctypes.Structure):
        """Структура версии 2: иконка, подсказка и всплывающее уведомление."""

        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * TIP_LENGTH),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        )

    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
    _kernel32 = ctypes.windll.kernel32

    # Явные прототипы: без них ctypes считает параметры 32-битными и падает на
    # 64-битных значениях (например, lparam в оконной процедуре).
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)
    _user32.DefWindowProcW.restype = LRESULT
    _user32.DestroyWindow.argtypes = (wintypes.HWND,)
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.PostQuitMessage.argtypes = (ctypes.c_int,)
    _user32.LoadIconW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR)
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR,
                                   wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT)
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),
                                    wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT)
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    _user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    _user32.DispatchMessageW.restype = LRESULT
    _shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD,
                                           ctypes.POINTER(NOTIFYICONDATAW))
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL



class TrayIcon:
    """Иконка в трее с подсказкой и двумя действиями (клик и меню)."""

    def __init__(self, tooltip: str = "", icon_path: str = "",
                 on_activate: Optional[Callable[[], None]] = None,
                 on_menu: Optional[Callable[[], None]] = None) -> None:
        self.tooltip = (tooltip or "")[:TIP_LENGTH - 1]
        self.icon_path = icon_path or ""
        self.on_activate = on_activate
        self.on_menu = on_menu
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._hwnd = None
        self._class_name = f"SimpleCheckerTray{os.getpid()}"
        self._nid = None
        self._wndproc = None
        self._ok = False
        self._lock = threading.RLock()
        #: Появилась ли иконка на самом деле (проверяется тестами и GUI).
        self.active = False

    @staticmethod
    def supported() -> bool:
        return bool(IS_WINDOWS)

    # -- жизненный цикл ---------------------------------------------------
    def start(self, timeout: float = 3.0) -> bool:
        """Создать иконку. Возвращает ``False``, если трей недоступен."""
        if not IS_WINDOWS:
            return False
        with self._lock:
            if self._thread is not None:
                return bool(self._ok)
            self._thread = threading.Thread(target=self._run,
                                            name="simple-tray", daemon=True)
            self._thread.start()
        self._ready.wait(timeout)
        return bool(self._ok)

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            thread = self._thread
            hwnd = self._hwnd
            self._thread = None
        if thread is None:
            return
        try:
            if hwnd:
                _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout)
        self.active = False

    def update(self, tooltip: Optional[str] = None) -> None:
        """Обновить подсказку иконки (текст состояния)."""
        if not IS_WINDOWS:
            return
        if tooltip is not None:
            self.tooltip = (tooltip or "")[:TIP_LENGTH - 1]
        with self._lock:
            nid = self._nid
        if nid is None:
            return
        try:
            nid.szTip = self.tooltip
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:  # noqa: BLE001 - подсказка не критична
            pass

    def notify(self, title: str, message: str) -> None:
        """Показать всплывающее уведомление от иконки.

        Нужно ровно один раз: объяснить, куда делось окно и как его вернуть.
        """
        if not IS_WINDOWS:
            return
        with self._lock:
            nid = self._nid
        if nid is None:
            return
        try:
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
            nid.szInfoTitle = (title or "")[:63]
            nid.szInfo = (message or "")[:255]
            nid.dwInfoFlags = NIIF_INFO
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:  # noqa: BLE001 - уведомление не критично
            pass

    # -- поток с окном ----------------------------------------------------
    def _run(self) -> None:
        try:
            self._create_window()
            self._add_icon()
            self._ok = True
            self.active = True
        except Exception as exc:  # noqa: BLE001 - трей не обязателен
            self._ok = False
            self.active = False
            print(t('трей недоступен: {}').format(exc), file=sys.stderr)
        finally:
            self._ready.set()
        if not self._ok:
            return
        message = wintypes.MSG()
        while True:
            try:
                result = _user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            except Exception:  # noqa: BLE001
                break
            if result <= 0:
                break
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
        self._remove_icon()

    def _create_window(self) -> None:
        hinstance = _kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._handle_message)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = self._class_name
        _user32.RegisterClassW(ctypes.byref(wc))
        self._hwnd = _user32.CreateWindowExW(
            0, self._class_name, "Xray Config Checker", 0, 0, 0, 0, 0,
            None, None, hinstance, None)
        if not self._hwnd:
            raise OSError(t("CreateWindowExW не создал окно трея"))

    def _load_icon(self):
        if self.icon_path and os.path.isfile(self.icon_path):
            handle = _user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                        LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        return _user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _add_icon(self) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self.tooltip
        with self._lock:
            self._nid = nid
        if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError(t("Shell_NotifyIconW не добавил иконку"))

    def _remove_icon(self) -> None:
        with self._lock:
            nid = self._nid
            self._nid = None
        self.active = False
        if nid is None:
            return
        try:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass

    def _handle_message(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._call(self.on_activate)
                elif event == WM_RBUTTONUP:
                    self._call(self.on_menu)
            elif message == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            elif message == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - обработчик не должен ломать цикл
            pass
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    @staticmethod
    def _call(callback: Optional[Callable[[], None]]) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            pass

