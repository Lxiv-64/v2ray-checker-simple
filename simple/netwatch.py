"""Слежка за доступностью сети — источник событий «сеть пропала/вернулась».

Автономному агрегатору нужно уметь реагировать на обрыв связи: переключить
выходной файл, уведомить себя, приостановить внешние отправки.  Реализовано
минималистично: обычный TCP-коннект к паре публичных адресов раз в N секунд.

Внешних зависимостей нет, проверка идёт в отдельном потоке и никогда не мешает
циклу проверок (короткий тайм-аут, исключения глушатся).
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Iterable, Optional, Sequence, Tuple

#: Куда «стучимся» для проверки связи: два независимых адреса.
DEFAULT_TARGETS: Tuple[Tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
)

#: Сколько неудач подряд считаются обрывом связи.
DEFAULT_FAILURES = 2


class NetworkWatcher:
    """Периодическая проверка связи с событиями о её потере и возврате."""

    def __init__(self, targets: Iterable[Tuple[str, int]] = DEFAULT_TARGETS,
                 interval: float = 30.0, timeout: float = 4.0,
                 failures: int = DEFAULT_FAILURES,
                 on_event: Optional[Callable[[str], None]] = None) -> None:
        self.targets: Sequence[Tuple[str, int]] = tuple(targets)
        self.interval = max(5.0, float(interval))
        self.timeout = max(1.0, float(timeout))
        self.failures = max(1, int(failures))
        self.on_event = on_event
        #: Текущее представление о связи (по умолчанию считаем, что она есть).
        self.online = True
        self._misses = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- управление -------------------------------------------------------
    def start(self) -> bool:
        """Запустить слежку. ``False`` — если она уже идёт."""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="simple-netwatch", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if (thread is not None and thread.is_alive()
                and threading.current_thread() is not thread):
            thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- проверка ---------------------------------------------------------
    def check(self) -> bool:
        """Одна проверка: есть ли связь (используется и в тестах)."""
        for host, port in self.targets:
            try:
                with socket.create_connection((host, port), self.timeout):
                    return True
            except OSError:
                continue
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.interval)

    def poll(self) -> bool:
        """Одна проверка с выдачей событий (в тестах вызывается вручную)."""
        ok = self.check()
        if ok:
            self._misses = 0
            if not self.online:
                self.online = True
                self._fire("on_network_up")
        else:
            self._misses += 1
            if self.online and self._misses >= self.failures:
                self.online = False
                self._fire("on_network_down")
        return ok

    def _fire(self, event: str) -> None:
        callback = self.on_event
        if not callable(callback):
            return
        try:
            callback(event)
        except Exception:  # noqa: BLE001 - слежка не должна падать
            pass


def watch_enabled(settings, key_on: str = "simple_network_watch",
                  key_interval: str = "simple_network_watch_seconds"
                  ) -> Tuple[bool, float]:
    """Прочитать настройки слежки: включена ли она и с каким периодом."""
    enabled = bool(settings.get(key_on, True))
    try:
        interval = float(settings.get(key_interval, 30.0) or 30.0)
    except (TypeError, ValueError):
        interval = 30.0
    return enabled, interval
