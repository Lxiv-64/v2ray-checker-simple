"""Discovery, download and lifecycle management of the proxy cores.

The checker never re-implements the proxy protocols.  Instead it drives a real
Xray-core / sing-box process for every configuration that survives the cheap
reachability stage.  This module is responsible for finding those binaries,
optionally downloading them, and starting/stopping one instance per check.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .constants import CORE_DIR, TMP_DIR
from .settings import get_logger
from app.i18n import t


log = get_logger()

XRAY_RELEASE_API = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"
SINGBOX_RELEASE_API = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"

_IS_WINDOWS = sys.platform.startswith("win")
_BIN_SUFFIX = ".exe" if _IS_WINDOWS else ""

# When True the per-check temporary config/log files are kept in ``tmp/`` for
# debugging.  Default: False — the files are removed as soon as a probe ends,
# so long runs do not leave hundreds of artefacts behind.
_KEEP_DEBUG_FILES = False


def set_keep_debug_files(enabled: bool) -> None:
    """Turn retention of per-check temporary files on or off."""
    global _KEEP_DEBUG_FILES
    _KEEP_DEBUG_FILES = bool(enabled)


def keep_debug_files() -> bool:
    return _KEEP_DEBUG_FILES


_TEMP_PREFIXES = ("xray_", "singbox_", "sing-box_", "verify_")


def cleanup_temp_dir(force: bool = False) -> int:
    """Delete leftover temporary configs/logs created under ``tmp/``.

    Only files created by this module (``xray_*``, ``sing-box_*``, ``verify_*``)
    are touched, and nothing is removed while ``keep_debug_files`` is enabled
    unless ``force`` is set.  Returns the number of removed files.
    """
    if _KEEP_DEBUG_FILES and not force:
        return 0
    removed = 0
    try:
        names = os.listdir(TMP_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(_TEMP_PREFIXES):
            continue
        path = os.path.join(TMP_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed



# ---------------------------------------------------------------------------
# Binary discovery / download
# ---------------------------------------------------------------------------

def _arch_tag() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "64"
    if machine in ("aarch64", "arm64"):
        return "arm64-v8a" if not _IS_WINDOWS else "arm64"
    if machine in ("i386", "i686", "x86"):
        return "32"
    return "64"


def _download(url: str, timeout: float = 120.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "v2ray-checker"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _github_asset_url(api_url: str, keywords: List[str]) -> Optional[str]:
    try:
        payload = json.loads(_download(api_url, timeout=30.0).decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        log.warning(t("Не удалось получить список релизов %s: %s"), api_url, exc)
        return None
    for asset in payload.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if all(k.lower() in name for k in keywords):
            return asset.get("browser_download_url")
    return None


def _extract_archive(data: bytes, archive_name: str, target_dir: str) -> List[str]:
    extracted: List[str] = []
    os.makedirs(target_dir, exist_ok=True)
    lower = archive_name.lower()
    wanted = ("xray", "xray.exe", "sing-box", "sing-box.exe")
    if lower.endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                filename = os.path.basename(member)
                if filename in wanted:
                    out_path = os.path.join(target_dir, filename)
                    with zf.open(member) as src, open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted.append(out_path)
    elif lower.endswith((".tar.gz", ".tgz")) or data[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                filename = os.path.basename(member.name)
                if filename in wanted:
                    src = tf.extractfile(member)
                    if not src:
                        continue
                    out_path = os.path.join(target_dir, filename)
                    with open(out_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted.append(out_path)
    for path in extracted:
        try:
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
    return extracted


def download_core(name: str, target_dir: str = CORE_DIR) -> Optional[str]:
    """Download the latest release of ``xray`` or ``sing-box``."""
    if name == "xray":
        api = XRAY_RELEASE_API
        keywords = ["windows" if _IS_WINDOWS else "linux", _arch_tag()]
        binary_name = "xray" + _BIN_SUFFIX
    elif name == "singbox":
        api = SINGBOX_RELEASE_API
        arch = "amd64" if _arch_tag() == "64" else _arch_tag()
        keywords = ["windows" if _IS_WINDOWS else "linux", arch]
        binary_name = "sing-box" + _BIN_SUFFIX
    else:
        return None

    url = _github_asset_url(api, keywords)
    if not url:
        log.error(t("Не найден подходящий релиз для %s"), name)
        return None
    log.info(t("Скачивание %s из %s"), name, url)
    try:
        data = _download(url)
    except (urllib.error.URLError, OSError) as exc:
        log.error(t("Ошибка скачивания %s: %s"), name, exc)
        return None

    extracted = _extract_archive(data, url.split("/")[-1], target_dir)
    for path in extracted:
        if os.path.basename(path) == binary_name:
            log.info(t("Ядро %s установлено: %s"), name, path)
            return path
    if extracted:
        log.info(t("Ядро %s установлено: %s"), name, extracted[0])
        return extracted[0]
    log.error(t("В архиве %s не найден исполняемый файл"), name)
    return None


def verify_core(core_path: str, core: str, timeout: float = 8.0) -> Tuple[bool, str]:
    """Run a minimal config through the core to prove the binary works.

    This catches the common failure mode where a core is present but cannot be
    executed (missing DLLs, wrong architecture, permission denied) before the
    user waits through a long check run and sees every server fail.
    """
    port = free_port()
    config_path = None
    log_path = None
    process = None
    try:
        if core == "xray":
            config = {
                "log": {"loglevel": "warning"},
                "inbounds": [{
                    "listen": "127.0.0.1", "port": port, "protocol": "socks",
                    "settings": {"auth": "noauth", "udp": False},
                }],
                "outbounds": [{"protocol": "freedom"}],
            }
        else:
            config = {
                "log": {"level": "warn"},
                "inbounds": [{
                    "type": "socks", "listen": "127.0.0.1",
                    "listen_port": port,
                }],
                "outbounds": [{"type": "direct"}],
            }
        fd, config_path = tempfile.mkstemp(prefix="verify_", suffix=".json",
                                           dir=TMP_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        log_path = config_path + ".log"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0
        try:
            log_file = open(log_path, "wb")
            parent_owns_log = True
        except OSError:
            log_file = subprocess.DEVNULL
            parent_owns_log = False
        try:
            process = subprocess.Popen(
                [core_path, "run", "-c", config_path],
                stdout=log_file, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        finally:
            # Release the parent's copy of the log handle (see _start_once).
            if parent_owns_log:
                try:
                    log_file.close()
                except OSError:
                    pass
        if wait_for_port(port, timeout=timeout):
            return True, t("работает")
        tail = ""
        try:
            if log_path and os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    tail = fh.read(400).strip()
        except OSError:
            pass
        code = process.poll()
        if code is not None:
            return False, t('процесс завершился с кодом {}. {}').format(code, tail)
        return False, t('порт не открылся. {}').format(tail)
    except OSError as exc:
        return False, t('не удалось запустить: {}').format(exc)
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        for path in (config_path, log_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _which(candidates: List[str]) -> Optional[str]:
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.expanduser(candidate)
        if os.path.isfile(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def resolve_core(name: str, configured_path: str = "", auto_download: bool = True,
                 target_dir: str = CORE_DIR) -> Optional[str]:
    """Return a usable path to the requested core binary, downloading if needed."""
    binary_name = ("xray" if name == "xray" else "sing-box") + _BIN_SUFFIX
    candidates = [
        configured_path,
        os.path.join(target_dir, binary_name),
        os.path.join(target_dir, name, binary_name),
        binary_name,
    ]
    if name == "xray":
        candidates += ["xray-core/xray" + _BIN_SUFFIX, "core/xray" + _BIN_SUFFIX]
    else:
        candidates += ["sing-box/sing-box" + _BIN_SUFFIX, "core/sing-box" + _BIN_SUFFIX]

    found = _which(candidates)
    if found:
        return found
    if not auto_download:
        return None

    if os.path.isdir(target_dir):
        for sub in os.listdir(target_dir):
            sub_path = os.path.join(target_dir, sub)
            if os.path.isdir(sub_path):
                nested = _which([os.path.join(sub_path, binary_name)])
                if nested:
                    return nested

    return download_core(name, target_dir)


# ---------------------------------------------------------------------------
# Running instances
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float = 5.0, host: str = "127.0.0.1") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


@dataclass
class CoreProcess:
    process: "subprocess.Popen"
    port: int
    config_path: str
    log_path: str
    core: str
    keep_files: bool = False
    # Last known log tail.  Captured before the file is deleted so that the
    # diagnostic messages still work after cleanup.
    log_tail: str = ""
    #: Все локальные порты процесса.  У обычной проверки он один (одна
    #: конфигурация — один процесс), у пакетной перепроверки — по одному на
    #: каждый сервер пачки: один процесс ядра обслуживает сразу несколько
    #: конфигураций, и результат каждой читается со своего inbound-порта.
    ports: List[int] = field(default_factory=list)

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2.0)
            except OSError:
                pass
        # Remember the tail before the file disappears.
        if not self.log_tail:
            try:
                if self.log_path and os.path.exists(self.log_path):
                    with open(self.log_path, "r", encoding="utf-8",
                              errors="replace") as fh:
                        self.log_tail = fh.read(4000).strip()
            except OSError:
                pass
        # The generated config is always disposable; the log is only kept when
        # the user explicitly asked for debug artefacts.
        paths = [self.config_path]
        if not (self.keep_files or _KEEP_DEBUG_FILES):
            paths.append(self.log_path)
        for path in paths:
            if not path:
                continue
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


class CoreRunner:
    """Starts a core with a generated config and exposes its local SOCKS port."""

    def __init__(self, base_port: int = 0) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self.base_port = base_port
        # Запущенные ядра: нужны, чтобы гарантированно остановить их при выходе
        # приложения (иначе оборванные проверки оставляют висящие процессы).
        self._instances: List["CoreProcess"] = []
        # После stop_all() новые ядра не запускаются: иначе оборванная проверка
        # успела бы создать процесс уже после остановки приложения.
        self._stopping = False

    def allow_start(self) -> None:
        """Разрешить запуск ядер (при старте или перезапуске цикла)."""
        with self._lock:
            self._stopping = False
            self._instances = []

    def _track(self, instance: Optional["CoreProcess"]) -> None:
        if instance is None:
            return
        with self._lock:
            self._instances = [item for item in self._instances
                               if item.process.poll() is None]
            self._instances.append(instance)

    def stop_all(self) -> int:
        """Остановить все запущенные ядра. Возвращает число живых процессов.

        После вызова запуск новых ядер запрещён до :meth:`allow_start`.
        """
        with self._lock:
            self._stopping = True
            instances = list(self._instances)
            self._instances = []
        alive = 0
        for instance in instances:
            try:
                if instance.process.poll() is None:
                    alive += 1
                instance.stop()
            except Exception:  # noqa: BLE001 - остановка не должна падать
                pass
        return alive

    def _next_port(self) -> int:
        with self._lock:
            self._counter += 1
            offset = self._counter % 4000
        if self.base_port:
            return self.base_port + offset
        return free_port()

    def start(self, core_path: str, core: str, config: Dict,
              timeout: float = 6.0,
              ports: Optional[Sequence[int]] = None) -> Optional[CoreProcess]:
        """Start a core, retrying on a fresh local port if it is already taken.

        With thousands of checks and several concurrent workers a fixed local
        port can occasionally collide; retrying with another port prevents a
        spurious "address already in use" from marking a good config as broken.

        ``ports`` — готовый список локальных портов для inbound-ов конфига.
        Обычная проверка его не передаёт (порт выбирается сам), а пакетная
        перепроверка передаёт по порту на каждый сервер пачки: один процесс
        обслуживает несколько конфигураций сразу.
        """
        attempts = 3
        instance = None
        if self._stopping:
            # Приложение останавливается: новые ядра не запускаем.
            return None
        for attempt in range(attempts):
            instance = self._start_once(core_path, core, config, timeout, ports)
            if instance is None:
                return None
            if self._stopping:
                # Остановка пришла, пока ядро поднималось.
                instance.stop()
                return None
            if instance.process.poll() is None:
                self._track(instance)
                return instance
            log_tail = self.read_log(instance).lower()
            instance.stop()
            if attempt < attempts - 1 and "address already in use" in log_tail:
                continue
            self._track(instance)
            return instance
        self._track(instance)
        return instance

    def _start_once(self, core_path: str, core: str, config: Dict,
                    timeout: float = 6.0,
                    ports: Optional[Sequence[int]] = None) -> Optional[CoreProcess]:
        """Поднять ядро с готовым конфигом.

        ``ports`` (необязательно) — локальные порты inbound-ов: у Xray это
        ``port``, у sing-box — ``listen_port``.  Порт первого inbound-а
        используется для ожидания готовности.  Без ``ports`` порт берётся
        автоматически (обычный режим «один сервер — один процесс»).
        """
        config = json.loads(json.dumps(config))  # deep copy
        inbounds = config.get("inbounds") or []
        if ports:
            bound = [int(item) for item in ports]
        else:
            bound = [self._next_port()]
        port = bound[0]
        for index, inbound in enumerate(inbounds):
            if index >= len(bound):
                break
            if core == "xray":
                inbound["port"] = bound[index]
            else:
                inbound["listen_port"] = bound[index]

        os.makedirs(TMP_DIR, exist_ok=True)
        fd, config_path = tempfile.mkstemp(prefix=f"{core}_", suffix=".json", dir=TMP_DIR)
        log_path = config_path + ".log"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)

        creationflags = 0
        if _IS_WINDOWS:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        command = [core_path, "run", "-c", config_path]
        try:
            log_file = open(log_path, "wb")
            parent_owns_log = True
        except OSError:
            log_file = subprocess.DEVNULL
            parent_owns_log = False

        try:
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                cwd=os.path.dirname(core_path) or None,
            )
        except OSError as exc:
            log.error(t("Не удалось запустить %s: %s"), core, exc)
            for path in (config_path, log_path):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            return None
        finally:
            # The child inherited the handle while spawning; the parent must
            # release its own copy, otherwise the log file stays locked on
            # Windows and can never be cleaned up.
            if parent_owns_log:
                try:
                    log_file.close()
                except OSError:
                    pass

        instance = CoreProcess(process, port, config_path, log_path, core,
                               keep_files=_KEEP_DEBUG_FILES, ports=bound)
        if self._stopping:
            # Приложение останавливается: только что созданное ядро не нужно.
            instance.stop()
            return None
        if wait_for_port(port, timeout=timeout):
            return instance
        instance.stop()
        return instance  # return anyway so the caller can read the log

    @staticmethod
    def read_log(instance: CoreProcess, limit: int = 4000) -> str:
        try:
            with open(instance.log_path, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read(limit).strip()
            if text:
                instance.log_tail = text
            return text
        except OSError:
            # The file may already have been cleaned up; fall back to the tail
            # captured by stop().
            return instance.log_tail