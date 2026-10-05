"""Persistent application settings and the shared application log."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

from .constants import APP_NAME, DEFAULT_SETTINGS, LOG_DIR, SETTINGS_PATH

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings:
    """Thread-safe, persisted key/value store for application settings."""

    def __init__(self, path: str = SETTINGS_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._listeners: List[Callable[[str, Any], None]] = []
        self.load()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
        self.save()
        self._emit(key, value)

    def update(self, values: Dict[str, Any]) -> None:
        changed = []
        with self._lock:
            for key, value in values.items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    changed.append((key, value))
        if changed:
            self.save()
            for key, value in changed:
                self._emit(key, value)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def on_change(self, listener: Callable[[str, Any], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, key: str, value: Any) -> None:
        for listener in list(self._listeners):
            try:
                listener(key, value)
            except Exception:
                pass

    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                with self._lock:
                    merged = dict(DEFAULT_SETTINGS)
                    merged.update(stored)
                    self._data = merged
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        with self._lock:
            payload = dict(self._data)
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_lock = threading.RLock()
_log_buffer: List[str] = []
_log_handlers: List[Callable[[str], None]] = []
_logger: Optional[logging.Logger] = None
_file_handler: Optional[logging.Handler] = None
_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)


class _BufferHandler(logging.Handler):
    def __init__(self, max_lines: int = 2000) -> None:
        super().__init__()
        self.max_lines = max_lines

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        with _log_lock:
            _log_buffer.append(message)
            overflow = len(_log_buffer) - self.max_lines
            if overflow > 0:
                del _log_buffer[:overflow]
        for handler in list(_log_handlers):
            try:
                handler(message)
            except Exception:
                pass


def _apply_file_handler(logger: logging.Logger, log_to_file: bool) -> None:
    """Add or remove the ``logs/checker.log`` handler to match the setting.

    Writing the journal to disk is opt-in: by default nothing is created, which
    keeps the project tree free of log files the user never asked for.
    """
    global _file_handler
    if log_to_file and _file_handler is None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            handler = logging.FileHandler(
                os.path.join(LOG_DIR, "checker.log"), encoding="utf-8"
            )
            handler.setFormatter(_formatter)
            logger.addHandler(handler)
            _file_handler = handler
        except OSError:
            _file_handler = None
    elif not log_to_file and _file_handler is not None:
        try:
            logger.removeHandler(_file_handler)
            _file_handler.close()
        except Exception:
            pass
        _file_handler = None


def setup_logging(level: str = "INFO", max_lines: int = 2000,
                  log_to_file: bool = False) -> logging.Logger:
    global _logger
    with _log_lock:
        if _logger is not None:
            _logger.setLevel(getattr(logging, level.upper(), logging.INFO))
            _apply_file_handler(_logger, log_to_file)
            return _logger
        logger = logging.getLogger(APP_NAME)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.propagate = False

        buffer_handler = _BufferHandler(max_lines)
        buffer_handler.setFormatter(_formatter)
        logger.addHandler(buffer_handler)

        _apply_file_handler(logger, log_to_file)

        _logger = logger
        return logger


def get_logger() -> logging.Logger:
    return _logger or setup_logging()


def buffer_logs() -> List[str]:
    with _log_lock:
        return list(_log_buffer)


def subscribe_logs(handler: Callable[[str], None]) -> None:
    with _log_lock:
        _log_handlers.append(handler)


def unsubscribe_logs(handler: Callable[[str], None]) -> None:
    with _log_lock:
        try:
            _log_handlers.remove(handler)
        except ValueError:
            pass