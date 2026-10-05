"""Multi-level configuration checker.

The design goal is to spend as little work as possible on hopeless entries and
only pay for the expensive protocol handshakes on configurations that already
proved their address is reachable.  Three levels are used:

1. **Reachability** (cheap, in-process).  DNS resolution and a TCP connect to
   the configured ``address:port``.  Absence of ICMP replies is *never* treated
   as failure because the relevant transports do not rely on ICMP at all.
2. **Protocol handshake** (expensive, real core).  A real Xray-core / sing-box
   process is started with an outbound for this exact configuration; success is
   only declared once the local SOCKS inbound accepts a connection and the core
   reports the outbound is up (i.e. a real TLS/Reality/WS/gRPC handshake
   completed).
3. **Control traffic** (end-to-end).  A tiny HTTP request is issued through the
   local SOCKS proxy so a successful handshake without a working tunnel is not
   mistaken for a working configuration.

Every level yields a distinct :class:`CheckStage` so the four notions
"address reachable", "protocol answers", "connection established" and "traffic
passes" are kept separate.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import core_config
from .constants import CheckMode, CheckStage, ConfigState
from .core_manager import CoreProcess, CoreRunner, resolve_core
from .models import Config
from .settings import get_logger
from app.i18n import t


log = get_logger()


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    config_id: int = 0
    state: str = ConfigState.NEW.value
    stage: str = CheckStage.NONE.value
    success: bool = False
    level_reached: int = 0
    latency_ms: Optional[int] = None
    error: str = ""
    # Per-level diagnostics, kept separate on purpose.
    dns_ok: bool = False
    tcp_ok: bool = False
    handshake_ok: bool = False
    traffic_ok: bool = False
    resolved_ips: List[str] = field(default_factory=list)
    detail: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 1 — reachability
# ---------------------------------------------------------------------------

def resolve_host(address: str, timeout: float) -> Tuple[bool, List[str], str]:
    """Resolve ``address`` to a list of IPs. Literal IPs resolve instantly."""
    address = address.strip().strip("[]")
    # IPv4 / IPv6 literal?
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, address)
            return True, [address], ""
        except OSError:
            continue
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(address, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, [], f"DNS: {exc}"
    except OSError as exc:
        return False, [], f"DNS: {exc}"
    finally:
        socket.setdefaulttimeout(previous)
    ips: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        return False, [], t("DNS: записей не найдено")
    return True, ips, ""


def tcp_probe(ips: List[str], port: int, timeout: float) -> Tuple[bool, str, Optional[int]]:
    """Try a TCP connect to any resolved IP. Returns ``(ok, error, latency_ms)``."""
    last_error = t("нет адресов для проверки")
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        started = time.time()
        try:
            sock.connect((ip, port))
            elapsed = int((time.time() - started) * 1000)
            return True, "", elapsed
        except socket.timeout:
            last_error = t("тайм-аут TCP")
        except ConnectionRefusedError:
            last_error = t("соединение отклонено")
        except OSError as exc:
            last_error = str(exc)
        finally:
            try:
                sock.close()
            except OSError:
                pass
    return False, last_error, None


def check_level1(cfg: Config, dns_timeout: float, tcp_timeout: float) -> ProbeResult:
    result = ProbeResult()
    ok, ips, error = resolve_host(cfg.address, dns_timeout)
    result.resolved_ips = ips
    result.dns_ok = ok
    if not ok:
        result.stage = CheckStage.DNS_FAIL.value
        result.error = error
        result.state = ConfigState.UNREACHABLE.value
        return result

    ok, error, latency = tcp_probe(ips, cfg.port, tcp_timeout)
    result.tcp_ok = ok
    if not ok:
        result.stage = CheckStage.TCP_FAIL.value
        result.error = error
        result.state = ConfigState.UNREACHABLE.value
        return result

    result.stage = CheckStage.TCP_OK.value
    result.level_reached = 1
    result.success = True
    result.latency_ms = latency
    return result


# ---------------------------------------------------------------------------
# Minimal SOCKS5 client used for the control-traffic stage
# ---------------------------------------------------------------------------

def _socks5_connect(sock: socket.socket, host: str, port: int) -> None:
    # greeting: version 5, one method (no auth)
    sock.sendall(b"\x05\x01\x00")
    response = sock.recv(2)
    if len(response) < 2 or response[0] != 5 or response[1] != 0:
        raise OSError(t("SOCKS5: метод аутентификации отклонён"))
    try:
        host_bytes = host.encode("idna")
    except (UnicodeError, UnicodeDecodeError):
        host_bytes = host.encode("utf-8")
    if len(host_bytes) > 255:
        raise OSError(t("SOCKS5: слишком длинное имя хоста"))
    request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes
    request += struct.pack(">H", port)
    sock.sendall(request)
    reply = sock.recv(4)
    if len(reply) < 4:
        raise OSError(t("SOCKS5: пустой ответ"))
    if reply[1] != 0:
        raise OSError(t('SOCKS5: отказ, код {}').format(reply[1]))
    atyp = reply[3]
    if atyp == 1:
        _ = sock.recv(4 + 2)
    elif atyp == 4:
        _ = sock.recv(16 + 2)
    elif atyp == 3:
        length = sock.recv(1)
        if length:
            _ = sock.recv(length[0] + 2)
    else:
        raise OSError(t("SOCKS5: неизвестный тип адреса"))


def _http_get_via_socks(proxy_port: int, target_host: str, target_port: int,
                        path: str, timeout: float, host_header: str = "",
                        secure: bool = False) -> Tuple[bool, str, int]:
    """Perform a minimal HTTP GET through the local SOCKS proxy.

    When ``secure`` is set the tunnel is wrapped in TLS after the SOCKS
    handshake so that HTTPS control targets can be used too; that is important
    because many servers happily carry TLS but are unreliable for plain HTTP.
    """
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    sock: socket.socket = raw
    try:
        raw.connect(("127.0.0.1", proxy_port))
        _socks5_connect(raw, target_host, target_port)
        if secure:
            context = ssl.create_default_context()
            sock = context.wrap_socket(
                raw, server_hostname=host_header or target_host)
        host_value = host_header or target_host
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_value}\r\n"
            "User-Agent: Mozilla/5.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii", errors="replace"))
        chunks = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            if sum(len(c) for c in chunks) > 65536:
                break
        blob = b"".join(chunks)
        if not blob:
            return False, "", 0
        text = blob.decode("utf-8", errors="replace")
        first_line = text.split("\r\n", 1)[0].split("\n", 1)[0].strip()
        parts = first_line.split()
        # Only a real HTTP status line proves that the tunnel carried the
        # request all the way to the target.  A TLS alert, an HTTP/2 frame or
        # arbitrary bytes returned by a non-proxy endpoint must not be mistaken
        # for a response.
        if (len(parts) < 2 or not parts[0].upper().startswith("HTTP/")
                or not parts[1].isdigit()):
            return False, first_line[:160], 0
        return True, text, int(parts[1])
    except (OSError, ssl.SSLError) as exc:
        return False, str(exc), 0
    finally:
        # Close both the TLS wrapper and the underlying socket when present.
        for connection in (sock, raw):
            try:
                connection.close()
            except OSError:
                pass


def _expected_status(url: str, configured: int) -> int:
    """Status code that proves the control request reached its target.

    ``configured`` (the ``traffic_status`` setting) always wins when non-zero.
    Some well-known targets return a fixed code (``generate_204`` -> 204).
    Otherwise ``0`` is returned, which the caller interprets as "any 2xx" — a
    far stricter rule than accepting arbitrary bytes or error pages.
    """
    if configured and configured > 0:
        return configured
    path = _parse_traffic_target(url)[2].lower()
    if "generate_204" in path:
        return 204
    return 0


def _parse_traffic_target(url: str) -> Tuple[str, int, str, bool]:
    url = url.strip()
    secure = url.lower().startswith("https://")
    if "://" in url:
        url = url.split("://", 1)[1]
    if "/" in url:
        hostport, path = url.split("/", 1)
        path = "/" + path
    else:
        hostport, path = url, "/"
    if ":" in hostport:
        host, _, port_s = hostport.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            port = 443 if secure else 80
    else:
        host = hostport
        port = 443 if secure else 80
    return host, port, path, secure


# ---------------------------------------------------------------------------
# Traffic / handshake targets
# ---------------------------------------------------------------------------

# Canonical, unambiguous connectivity targets.  A broken endpoint cannot fake
# their 204 response; the HTTPS variant is included because many servers carry
# TLS reliably but treat plain HTTP:80 unfavourably.
FALLBACK_TRAFFIC_TARGETS = (
    "https://www.gstatic.com/generate_204",
    "http://www.gstatic.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://cp.cloudflare.com/",
)

# Reliable destinations used to force a real outbound handshake.
FALLBACK_HANDSHAKE_TARGETS = (
    ("cp.cloudflare.com", 80),
    ("www.gstatic.com", 80),
)


def traffic_targets(settings) -> List[str]:
    """Цели контрольного запроса: настроенная плюс запасные.

    ``traffic_fallbacks`` (настройка, по умолчанию включена) отключает запасные
    цели: тогда проверяется только ``traffic_url``, что заметно сокращает число
    исходящих подключений — ценой того, что одна заблокированная цель на стороне
    сервера будет выглядеть как нерабочий сервер.
    """
    configured = str(settings.get("traffic_url", "") or "").strip()
    candidates: List[str] = [configured] if configured else []
    if bool(settings.get("traffic_fallbacks", True)):
        for url in FALLBACK_TRAFFIC_TARGETS:
            if url not in candidates:
                candidates.append(url)
    elif not candidates:
        candidates.append(FALLBACK_TRAFFIC_TARGETS[-1])
    return candidates


def handshake_targets(settings) -> List[Tuple[str, int]]:
    """Цели, которыми форсируется реальное рукопожатие через ядро."""
    targets: List[Tuple[str, int]] = []
    configured = str(settings.get("traffic_url", "") or "").strip()
    if configured:
        host, port, _path, _secure = _parse_traffic_target(configured)
        if host:
            targets.append((host, port))
    if bool(settings.get("traffic_fallbacks", True)):
        for host, port in FALLBACK_HANDSHAKE_TARGETS:
            if (host, port) not in targets:
                targets.append((host, port))
    if not targets:
        targets.extend(FALLBACK_HANDSHAKE_TARGETS)
    return targets


# ---------------------------------------------------------------------------
# Level 2 & 3 — real protocol handshake and control traffic
# ---------------------------------------------------------------------------

class CoreChecker:
    """Drives a real core process for protocol-level validation."""

    def __init__(self, runner: CoreRunner, settings) -> None:
        self.runner = runner
        self.settings = settings
        self._xray_path: Optional[str] = None
        self._singbox_path: Optional[str] = None
        self._resolved = False
        self._lock = None

    # -- core discovery ------------------------------------------------
    def ensure_cores(self) -> None:
        if self._resolved:
            return
        self._xray_path = resolve_core(
            "xray",
            configured_path=self.settings.get("xray_path", ""),
            auto_download=bool(self.settings.get("auto_download_core", False)),
        )
        self._singbox_path = resolve_core(
            "singbox",
            configured_path=self.settings.get("singbox_path", ""),
            auto_download=bool(self.settings.get("auto_download_core", False)),
        )
        self._resolved = True

    def core_path(self, core: str) -> Optional[str]:
        self.ensure_cores()
        return self._xray_path if core == core_config.XRAY else self._singbox_path

    # -- main entry ----------------------------------------------------
    def check(self, cfg: Config, config_id: int, mode: CheckMode,
              level1: ProbeResult, levels: Optional[set] = None) -> ProbeResult:
        """Run the requested core-based levels for a configuration.

        ``levels`` is the set of probe levels to execute (1/2/3).  When it is
        omitted the mode's default set is used.  A configuration that has no
        deeper level requested is reported as ``REACHABLE`` (a mere candidate),
        which is deliberately distinct from a verified connection.
        """
        result = level1
        result.config_id = config_id
        wanted = levels if levels is not None else mode.level_set

        if 2 not in wanted and 3 not in wanted:
            # No core-based stage requested: the address merely answered.
            result.state = ConfigState.REACHABLE.value
            return result

        core = core_config.pick_core(cfg)
        if core is None:
            result.state = ConfigState.SUSPECT.value
            result.error = t("неподдерживаемый протокол")
            result.detail["core"] = t("нет")
            return result

        core_path = self.core_path(core)
        if not core_path:
            result.state = ConfigState.SUSPECT.value
            result.error = (
                t('ядро {} не найдено. Укажите путь в настройках или разрешите автозагрузку.').format(core)
            )
            result.detail["core"] = core
            return result
        result.detail["core"] = core

        # ---- level 2: build config, start core, verify handshake ----
        try:
            config_json = core_config.build(cfg, core, 1080)
        except core_config.UnsupportedTransport as exc:
            # The link uses a transport the checker cannot reproduce; probing it
            # as something else would give a meaningless verdict.
            result.state = ConfigState.SUSPECT.value
            result.stage = CheckStage.HANDSHAKE_FAIL.value
            result.error = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 - bad config must not crash run
            result.state = ConfigState.SUSPECT.value
            result.stage = CheckStage.HANDSHAKE_FAIL.value
            result.error = t('ошибка сборки конфигурации: {}').format(exc)
            return result

        core_timeout = float(self.settings.get("core_timeout", 12.0))
        started = time.time()
        instance: Optional[CoreProcess] = self.runner.start(
            core_path, core, config_json, timeout=min(6.0, core_timeout)
        )
        if instance is None:
            result.state = ConfigState.SUSPECT.value
            result.stage = CheckStage.HANDSHAKE_FAIL.value
            result.error = t('не удалось запустить {}').format(core)
            return result

        # ``instance`` may be replaced by a certificate-tolerant retry, so the
        # active process is tracked here and stopped exactly once at the end.
        active: Optional[CoreProcess] = instance
        try:
            if active.process.poll() is not None:
                log_tail = CoreRunner.read_log(active)
                result.stage = CheckStage.HANDSHAKE_FAIL.value
                result.error = self._diagnose_core_error(log_tail)
                result.state = ConfigState.SUSPECT.value
                result.detail["core_log"] = log_tail[-500:]
                return result

            # Force the core to actually dial the outbound: opening a SOCKS
            # connection through the local inbound makes the core perform the
            # real TLS/Reality/WebSocket/gRPC handshake with the remote host.
            # Several targets are tried because a single blocked destination
            # must not make a valid configuration look dead.
            handshake_ok, handshake_error, active, insecure_required = \
                self._run_handshake(active, cfg, core)
            if not handshake_ok or active is None:
                log_tail = CoreRunner.read_log(instance)
                message = self._diagnose_core_error(log_tail) or handshake_error
                result.stage = CheckStage.HANDSHAKE_FAIL.value
                result.error = message
                result.state = ConfigState.SUSPECT.value
                result.detail["core_log"] = log_tail[-500:]
                return result

            result.handshake_ok = True
            result.stage = CheckStage.HANDSHAKE_OK.value
            result.level_reached = 2
            if result.latency_ms is None:
                result.latency_ms = int((time.time() - started) * 1000)

            # ---- level 3: control traffic through the tunnel ----
            if 3 in wanted:
                traffic_ok, traffic_error, measured = self._check_traffic(active)
                if traffic_ok:
                    result.traffic_ok = True
                    result.stage = CheckStage.TRAFFIC_OK.value
                    result.success = True
                    result.level_reached = 3
                    result.state = ConfigState.WORKING.value
                    result.latency_ms = measured
                else:
                    result.stage = CheckStage.TRAFFIC_FAIL.value
                    # The strict control request failed against every canonical
                    # target: the transport came up, but no usable data passed
                    # through it (a plain server that answers an error, wrong
                    # credentials, or a mismatched transport).  This must not be
                    # reported as working.
                    result.success = False
                    result.state = ConfigState.SUSPECT.value
                    result.error = traffic_error or t("контрольный запрос не прошёл")
            else:
                # FULL mode stops at level 2.  A successful SOCKS handshake
                # only proves the transport came up; it does NOT prove the
                # credentials work (VLESS/VMess servers accept the TCP/TLS
                # part even with a wrong key, dropping the client only once
                # data flows).  Therefore level 2 is reported as "Соединение
                # есть", and only a real control request (level 3) may yield
                # "Рабочая".
                result.success = True
                result.level_reached = 2
                result.state = ConfigState.CONNECTED.value

            if insecure_required:
                # The tunnel only came up after certificate verification was
                # disabled; a default client would refuse to connect, so this is
                # explicitly demoted instead of being advertised as working.
                result.success = False
                result.state = ConfigState.SUSPECT.value
                result.error = (t("TLS-сертификат не проходит строгую проверку — "
                                "подключение возможно только с allowInsecure"))
                result.detail["tls_insecure"] = "1"
            return result
        finally:
            if active is not None:
                active.stop()
            if instance is not None and instance is not active:
                instance.stop()

    # -- helpers -------------------------------------------------------
    def _handshake_targets(self) -> List[Tuple[str, int]]:
        """Reliable destinations used to force a real outbound handshake.

        The first entry is the user-configured URL; the rest are well-known
        hosts so that a configuration is not rejected merely because one
        destination happens to be blocked by the server.  The whole fallback
        list can be switched off with the ``traffic_fallbacks`` setting.
        """
        return handshake_targets(self.settings)

    def _probe_handshake(self, instance: CoreProcess) -> Tuple[bool, str]:
        """Establish a real SOCKS5 connection through the running core.

        Merely seeing the inbound port open only proves that the core started.
        Opening a proxied connection to a target forces the outbound handshake
        (TLS / Reality / WebSocket / gRPC / ShadowSocks) to complete; if the
        remote credentials or transport are wrong the SOCKS negotiation fails,
        which is exactly the signal level 2 needs.

        Several targets are attempted so that a single blocked destination does
        not cause a valid configuration to be marked as broken.
        """
        # The handshake probe drives the core to dial the remote, so its
        # timeout bounds how long one dead server can hold a worker.  Keep it
        # tight to avoid a single broken target stalling the whole queue.
        timeout = min(float(self.settings.get("tcp_timeout", 3.0)) + 1.0, 5.0)
        last_error = t("нет цели для проверки")
        for host, port in self._handshake_targets():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(("127.0.0.1", instance.port))
                _socks5_connect(sock, host, port)
                return True, ""
            except (OSError, ssl.SSLError) as exc:
                last_error = t('рукопожатие ({}): {}').format(host, exc)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        return False, last_error

    def _run_handshake(self, instance: CoreProcess, cfg: Config, core: str
                       ) -> Tuple[bool, str, Optional[CoreProcess], bool]:
        """Try the normal handshake, then a certificate-tolerant retry.

        Some real-world configurations use self-signed or otherwise invalid TLS
        certificates.  A mainstream client accepts them only when the user
        disables certificate verification, so such a configuration is *not*
        reported as working: this method still performs the tolerant retry to
        confirm the tunnel and to produce a precise diagnosis, but the success
        flag is returned separately so the caller can demote the verdict.

        Returns ``(ok, error, active_instance, insecure_required)``.  On success
        the returned instance is the one still running, so the caller can keep
        using it for the traffic stage; on failure it is ``None``.
        """
        error = t("рукопожатие не удалось")
        if instance.process.poll() is None:
            ok, error = self._probe_handshake(instance)
            if ok:
                return True, "", instance, False

        if not self._should_retry_insecure(cfg):
            return False, error, None, False
        # A TLS certificate mismatch does not kill the core, it only makes the
        # SOCKS connect fail, so the live instance is stopped before a fresh
        # certificate-tolerant one is started.
        instance.stop()

        secure_cfg = Config(**{**cfg.__dict__})
        secure_cfg.allow_insecure = True
        try:
            tolerant_json = core_config.build(secure_cfg, core, 1080)
        except Exception:
            return False, error, None, False

        core_path = self.core_path(core)
        if not core_path:
            return False, error, None, False
        core_timeout = float(self.settings.get("core_timeout", 12.0))
        retry_instance = self.runner.start(
            core_path, core, tolerant_json, timeout=min(6.0, core_timeout),
        )
        if retry_instance is None:
            return False, error, None, False
        if retry_instance.process.poll() is not None:
            retry_instance.stop()
            return False, error, None, False
        ok, retry_error = self._probe_handshake(retry_instance)
        if not ok:
            retry_instance.stop()
            return False, retry_error, None, False
        # Hand the running retry instance back to the caller together with the
        # flag marking that certificate verification had to be disabled.
        return True, "", retry_instance, True

    @staticmethod
    def _should_retry_insecure(cfg: Config) -> bool:
        return cfg.security == "tls" and not cfg.allow_insecure

    def _check_traffic(self, instance: CoreProcess) -> Tuple[bool, str, Optional[int]]:
        """Confirm that a control request actually passes through the tunnel.

        A positive result now requires a *well-formed* HTTP response with an
        expected status code:

        * the first bytes must be a real ``HTTP/1.x NNN`` status line — raw TLS
          alerts, HTTP/2 frames or arbitrary bytes from a non-proxy endpoint are
          rejected;
        * ``traffic_status`` (when non-zero) fixes the exact code; otherwise a
          ``generate_204`` target must answer ``204`` and any other target must
          answer with a 2xx code, so an error page served by a plain web server
          is no longer mistaken for a working tunnel.

        The list of targets comes from :func:`traffic_targets`: the configured
        URL first, then the built-in fallbacks unless ``traffic_fallbacks`` is
        switched off.
        """
        candidates = traffic_targets(self.settings)

        timeout = float(self.settings.get("traffic_timeout", 8.0))
        configured_status = int(self.settings.get("traffic_status", 0) or 0)
        expected_substr = str(self.settings.get("traffic_expect", "") or "")
        last_error = t("контрольный запрос не прошёл")
        for url in candidates:
            host, port, path, secure = _parse_traffic_target(url)
            if not host:
                continue
            expected_status = _expected_status(url, configured_status)
            started = time.time()
            ok, text, status = _http_get_via_socks(
                instance.port, host, port, path, timeout, secure=secure
            )
            elapsed = int((time.time() - started) * 1000)
            if not ok:
                last_error = t('трафик ({}): {}').format(host, text or 'нет HTTP-ответа')
                continue
            if expected_status:
                if status != expected_status:
                    last_error = (t('HTTP {} от {} (ожидался {})').format(status, host, expected_status))
                    continue
            elif not (200 <= status < 300):
                last_error = t('HTTP {} от {} (ожидался 2xx)').format(status, host)
                continue
            if expected_substr and expected_substr not in text:
                last_error = t("ответ не содержит ожидаемых данных")
                continue
            return True, "", elapsed
        return False, last_error, None

    @staticmethod
    def _diagnose_core_error(log_tail: str) -> str:
        lowered = log_tail.lower()
        if "address already in use" in lowered:
            return t("локальный порт занят")
        if "failed to dial" in lowered or "connection refused" in lowered:
            return t("сервер отклонил соединение")
        if "timeout" in lowered or "i/o timeout" in lowered:
            return t("тайм-аут при подключении")
        if "invalid user" in lowered or "invalid password" in lowered:
            return t("неверные учётные данные")
        if "unknown cipher" in lowered or "unsupported" in lowered:
            return t("неподдерживаемый шифр/протокол")
        if "tls" in lowered and "handshake" in lowered:
            return t("ошибка TLS-рукопожатия")
        if "reality" in lowered:
            return t("ошибка Reality")
        if "unexpected" in lowered and "eof" in lowered:
            return t("соединение закрыто сервером")
        for line in reversed(log_tail.splitlines()):
            if line.strip():
                return line.strip()[:300]
        return t("ядро не приняло соединение")


def classify(mode: CheckMode, level1: ProbeResult) -> str:
    """Map a level-1 only result to a state for reachability-only runs."""
    if not level1.success:
        return level1.state
    return ConfigState.REACHABLE.value
