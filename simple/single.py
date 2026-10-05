"""Защита от второго запуска программы.

Два одновременно работающих экземпляра портят друг другу состояние: пул,
настройки и выходной файл перезаписываются целиком, поэтому «кто последний,
тот и прав».  Здесь это предотвращается штатным механизмом Windows — именованным
мьютексом (``kernel32.CreateMutexW``): он не требует файлов, а его освобождение
делает сама система, даже если процесс упал.

Второй запуск не просто отклоняется: он будит первый экземпляр именованным
событием, и тот показывает окно (полезно, когда программа ушла в трей).

Вне Windows проверки нет: функции возвращают «можно запускаться».
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Callable, Optional

#: Имена объектов ядра.  Префикс ``Local\\`` — в пределах сеанса пользователя.
MUTEX_NAME = r"Local\XrayConfigCheckerSimple"
EVENT_NAME = r"Local\XrayConfigCheckerSimpleWake"

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_ALREADY_EXISTS = 183


def supported() -> bool:
    """Работает ли защита в этой системе (сейчас — только Windows)."""
    return sys.platform.startswith("win")


_KERNEL32 = None


def _api():
    """Типизированный доступ к функциям ядра.

    Без ``restype = c_void_p`` ctypes считает результат 32-битным и обрезает
    хэндлы (на 64-битной Windows они 64-битные): мьютекс тогда «работает» по
    имени, а события молча не срабатывают.
    """
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                      ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                      ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.SetEvent.restype = ctypes.c_bool
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    _KERNEL32 = kernel32
    return kernel32


class InstanceGuard:
    """Держит «место» единственного экземпляра и будит работающий.

    Использование::

        guard = InstanceGuard()
        if not guard.acquire():
            guard.notify_running()      # попросить первый экземпляр показать окно
            ...
        guard.watch(callback)           # первый экземпляр: ждать сигнала
    """

    def __init__(self, name: str = MUTEX_NAME,
                 event_name: str = EVENT_NAME) -> None:
        self.name = name
        self.event_name = event_name
        self._handle: Optional[int] = None
        self._event: Optional[int] = None
        self._acquired = False
        self._watcher: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[], None]] = None
        self._stop = threading.Event()

    # -- место единственного экземпляра ---------------------------------
    def acquire(self) -> bool:
        """``True`` — мы первые; ``False`` — программа уже работает."""
        if not supported():
            self._acquired = True
            return True
        kernel32 = _api()
        handle = kernel32.CreateMutexW(None, False, str(self.name))
        if not handle:
            # Не смогли создать объект — не мешаем работать.
            self._acquired = True
            return True
        self._handle = handle
        already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        self._acquired = not already
        return self._acquired

    @property
    def owns(self) -> bool:
        """Были ли мы первым экземпляром (место занято нами)."""
        return self._acquired

    # -- сигнал работающему экземпляру ----------------------------------
    def _open_event(self) -> Optional[int]:
        if not supported():
            return None
        if self._event:
            return self._event
        handle = _api().CreateEventW(None, False, False, str(self.event_name))
        if not handle:
            return None
        # Хэндл обязательно запоминается: поток-наблюдатель ждёт именно его.
        self._event = handle
        return handle

    def signal(self) -> bool:
        """Попросить работающий экземпляр показать окно."""
        handle = self._open_event()
        if not handle:
            return False
        return bool(_api().SetEvent(handle))

    def watch(self, callback: Callable[[], None]) -> bool:
        """Ждать сигнала «покажи окно» и вызывать ``callback`` в потоке.

        Вызывается тем экземпляром, который занял место.
        """
        if not supported() or not self._acquired:
            return False
        handle = self._open_event()
        if not handle:
            return False
        self._callback = callback
        self._stop.clear()
        self._watcher = threading.Thread(target=self._watch_loop,
                                         name="single-instance", daemon=True)
        self._watcher.start()
        return True

    def _watch_loop(self) -> None:
        kernel32 = _api()
        while not self._stop.is_set():
            result = kernel32.WaitForSingleObject(self._event, 400)
            if result == WAIT_OBJECT_0:
                callback = self._callback
                if callback is not None:
                    try:
                        callback()
                    except Exception:  # noqa: BLE001 - сигнал не должен падать
                        pass
            elif result not in (WAIT_TIMEOUT, WAIT_OBJECT_0):
                break

    # -- завершение ------------------------------------------------------
    def close(self) -> None:
        """Освободить место и остановить наблюдателя (обычно не требуется)."""
        self._stop.set()
        watcher = self._watcher
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=1.0)
        self._watcher = None
        if supported():
            kernel32 = _api()
            for handle in (self._event, self._handle):
                if handle:
                    try:
                        kernel32.CloseHandle(handle)
                    except Exception:  # noqa: BLE001
                        pass
        self._event = None
        self._handle = None
        self._acquired = False


#: Захваченные объекты живут до конца процесса: так место не освобождается
#: раньше времени даже если ссылку на экземпляр никто не сохранил.
_KEPT: list = []


def keep(guard: InstanceGuard) -> InstanceGuard:
    """Оставить защиту в глобальном списке (чтобы её не собрал сборщик мусора)."""
    _KEPT.append(guard)
    return guard
