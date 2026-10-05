"""Parsing of share URIs (vless://, vmess://, trojan://, ss://, ...).

The parser is intentionally forgiving: internet subscription lists are full of
slightly malformed links and the goal is to salvage everything that can still
be turned into a working Xray/sing-box outbound.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .constants import MAX_PORT, MIN_PORT, Protocol
from .models import Config
from app.i18n import t


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)
_SS_METHODS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "chacha20-ietf-poly1305", "chacha20-poly1305", "chacha20-ietf",
    "chacha20", "xchacha20-ietf-poly1305", "xchacha20-poly1305",
    "rc4-md5", "none", "plain", "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305",
}


def _b64decode(data: str, urlsafe: bool = True) -> Optional[str]:
    """Tolerant base64 decoder (handles missing padding / url-safe alphabet)."""
    if not data:
        return None
    candidate = data.strip().replace("\n", "").replace("\r", "")
    candidate = candidate.replace("-", "+").replace("_", "/")
    pad = (-len(candidate)) % 4
    candidate += "=" * pad
    try:
        raw = base64.b64decode(candidate, validate=False)
    except (binascii.Error, ValueError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _first(qs: Dict[str, List[str]], key: str, default: str = "") -> str:
    vals = qs.get(key) or qs.get(key.lower())
    if not vals:
        return default
    return vals[0]


def _boolish(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int_port(value: object, default: int = 443) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if port < MIN_PORT or port > MAX_PORT:
        return default
    return port


def _split_host_port(authority: str, default_port: int) -> tuple:
    """Split ``host:port`` allowing bracketed IPv6 literals."""
    authority = authority.strip()
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return authority, default_port
        host = authority[1:end]
        rest = authority[end + 1:]
        if rest.startswith(":") and rest[1:]:
            return host, _int_port(rest[1:], default_port)
        return host, default_port
    if authority.count(":") == 1:
        host, port = authority.rsplit(":", 1)
        return host, _int_port(port, default_port)
    return authority, default_port


def _normalise_alpn(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[,|]", value)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def looks_like_uri(line: str) -> bool:
    return bool(_URI_RE.match(line.strip()))


def extract_uris(text: str) -> List[str]:
    """Pull every share URI out of a blob of text.

    Handles plain lists, base64-encoded subscription bodies and mixed
    documents.  Returns URIs in their original (non-normalised) form.
    """
    uris: List[str] = []
    stripped = text.strip()
    if not stripped:
        return uris

    # Whole-document base64 subscription (single long token, no scheme)
    if "://" not in stripped:
        decoded = _b64decode(stripped)
        if decoded and "://" in decoded:
            stripped = decoded

    for raw_line in stripped.splitlines():
        line = raw_line.strip().strip("\",'")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        for match in re.finditer(r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S+", line):
            candidate = match.group(0).rstrip(",;")
            if candidate:
                uris.append(candidate)
    return uris


def parse_uri(uri: str) -> Optional[Config]:
    """Parse one share URI into a :class:`Config`, or ``None`` on failure."""
    uri = uri.strip()
    if not uri or not _URI_RE.match(uri):
        return None
    scheme = uri.split("://", 1)[0].lower()
    try:
        if scheme == "vless":
            return _parse_vless(uri)
        if scheme == "vmess":
            return _parse_vmess(uri)
        if scheme in ("trojan", "trojan-go"):
            return _parse_trojan(uri)
        if scheme == "ss":
            return _parse_ss(uri)
        if scheme == "ssr":
            return _parse_ssr(uri)
        if scheme in ("hysteria2", "hy2"):
            return _parse_hysteria2(uri)
        if scheme == "tuic":
            return _parse_tuic(uri)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Per-protocol parsers
# ---------------------------------------------------------------------------

def _parse_vless(uri: str) -> Optional[Config]:
    split = urlsplit(uri)
    if not split.hostname:
        return None
    qs = parse_qs(split.query, keep_blank_values=True)
    cfg = Config(
        protocol=Protocol.VLESS,
        address=split.hostname,
        port=_int_port(split.port, 443),
        uri=uri,
        uuid=unquote(split.username or ""),
        flow=_first(qs, "flow"),
        network=(_first(qs, "type", "tcp") or "tcp").lower(),
        security=(_first(qs, "security", "none") or "none").lower(),
        sni=_first(qs, "sni") or _first(qs, "peer") or _first(qs, "host"),
        alpn=_normalise_alpn(_first(qs, "alpn")),
        fingerprint=_first(qs, "fp"),
        allow_insecure=_boolish(_first(qs, "allowInsecure") or _first(qs, "insecure")),
        ws_path=_first(qs, "path"),
        ws_host=_first(qs, "host"),
        service_name=_first(qs, "serviceName"),
        header_type=_first(qs, "headerType"),
        pbk=_first(qs, "pbk"),
        sid=_first(qs, "sid"),
        spx=_first(qs, "spx"),
        label=unquote(split.fragment or ""),
        extra={k: v[0] for k, v in qs.items() if v},
    )
    if not cfg.sni:
        cfg.sni = cfg.ws_host or cfg.address
    if cfg.network == "grpc" and not cfg.service_name:
        cfg.service_name = cfg.ws_path or ""
    return cfg


def _parse_vmess(uri: str) -> Optional[Config]:
    """VMess share links are base64-encoded JSON (vmess://<base64>)."""
    body = uri.split("://", 1)[1].strip()
    decoded = _b64decode(body)
    if not decoded:
        return None
    decoded = decoded.strip()
    if not decoded.startswith("{"):
        inner = _b64decode(decoded)
        if inner and inner.strip().startswith("{"):
            decoded = inner.strip()
        else:
            return None
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    address = str(data.get("add") or data.get("address") or "").strip()
    if not address:
        return None
    port = _int_port(data.get("port"), 443)
    tls = str(data.get("tls") or "").strip().lower()
    network = str(data.get("net") or "tcp").strip().lower()
    host = str(data.get("host") or "").strip()
    path = str(data.get("path") or "").strip()
    sni = str(data.get("sni") or "").strip()
    if tls == "reality":
        security = "reality"
    elif tls in ("tls", "true", "1"):
        security = "tls"
    else:
        security = "none"

    cfg = Config(
        protocol=Protocol.VMESS,
        address=address,
        port=port,
        uri=uri,
        uuid=str(data.get("id") or "").strip(),
        network=network or "tcp",
        security=security,
        sni=sni or host or address,
        alpn=_normalise_alpn(str(data.get("alpn") or "")),
        fingerprint=str(data.get("fp") or "").strip(),
        ws_path=path,
        ws_host=host,
        service_name=path if network == "grpc" else "",
        header_type=str(data.get("type") or "").strip(),
        label=str(data.get("ps") or "").strip(),
        extra={"aid": data.get("aid", 0), "scy": data.get("scy", "auto")},
    )
    return cfg


def _parse_trojan(uri: str) -> Optional[Config]:
    split = urlsplit(uri)
    if not split.hostname:
        return None
    qs = parse_qs(split.query, keep_blank_values=True)
    security = (_first(qs, "security", "tls") or "tls").lower()
    if security not in ("tls", "none"):
        security = "tls"
    cfg = Config(
        protocol=Protocol.TROJAN,
        address=split.hostname,
        port=_int_port(split.port, 443),
        uri=uri,
        password=unquote(split.username or ""),
        network=(_first(qs, "type", "tcp") or "tcp").lower(),
        security=security,
        sni=_first(qs, "sni") or _first(qs, "peer") or _first(qs, "host"),
        alpn=_normalise_alpn(_first(qs, "alpn")),
        fingerprint=_first(qs, "fp"),
        allow_insecure=_boolish(_first(qs, "allowInsecure") or _first(qs, "insecure")),
        ws_path=_first(qs, "path"),
        ws_host=_first(qs, "host"),
        service_name=_first(qs, "serviceName"),
        header_type=_first(qs, "headerType"),
        label=unquote(split.fragment or ""),
        extra={k: v[0] for k, v in qs.items() if v},
    )
    if not cfg.sni:
        cfg.sni = cfg.ws_host or cfg.address
    return cfg


def _parse_ss(uri: str) -> Optional[Config]:
    """Shadowsocks: ``ss://base64(method:pass)@host:port`` or legacy full-b64."""
    body = uri.split("://", 1)[1]
    fragment = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    qs = parse_qs(query, keep_blank_values=True)

    userinfo = ""
    host_port = ""
    if "@" in body:
        userinfo, host_port = body.rsplit("@", 1)
        if ":" not in userinfo:
            dec = _b64decode(userinfo)
            if dec:
                userinfo = dec
    else:
        dec = _b64decode(body)
        if not dec or "@" not in dec:
            return None
        userinfo, host_port = dec.rsplit("@", 1)

    if ":" not in userinfo:
        return None
    method, password = userinfo.split(":", 1)
    method = method.strip()
    password = password.strip()
    address, port = _split_host_port(host_port, 8388)
    if not address:
        return None

    plugin_raw = _first(qs, "plugin")
    cfg = Config(
        protocol=Protocol.SHADOWSOCKS,
        address=address,
        port=port,
        uri=uri,
        method=method,
        password=password,
        label=unquote(fragment),
        extra={k: v[0] for k, v in qs.items() if v},
    )
    if plugin_raw:
        if ";" in plugin_raw:
            cfg.plugin, cfg.plugin_opts = plugin_raw.split(";", 1)
        else:
            cfg.plugin = plugin_raw
    return cfg


def _parse_ssr(uri: str) -> Optional[Config]:
    """ShadowsocksR: base64(host:port:proto:method:obfs:base64pass/?params)."""
    body = uri.split("://", 1)[1]
    fragment = ""
    if "#" in body:
        body, fragment = body.split("#", 1)
    decoded = _b64decode(body)
    if not decoded:
        return None
    main, _, params = decoded.partition("/?")
    parts = main.split(":")
    if len(parts) < 6:
        return None
    address, port_s, proto, method, obfs, pass_b64 = parts[:6]
    password = _b64decode(pass_b64)
    if password is None:
        password = pass_b64
    qs = parse_qs(params, keep_blank_values=True)
    return Config(
        protocol=Protocol.SHADOWSOCKS_R,
        address=address,
        port=_int_port(port_s, 8388),
        uri=uri,
        method=method,
        password=password,
        obfs=obfs,
        label=unquote(fragment),
        extra={
            "proto": proto,
            "obfsparam": _first(qs, "obfsparam"),
            "protoparam": _first(qs, "protoparam"),
            "remarks": _first(qs, "remarks"),
        },
    )


def _parse_hysteria2(uri: str) -> Optional[Config]:
    split = urlsplit(uri)
    if not split.hostname:
        return None
    qs = parse_qs(split.query, keep_blank_values=True)
    auth = unquote(split.username or "")
    password = unquote(split.password or "") if split.password else ""
    if not password and auth:
        password = auth
    cfg = Config(
        protocol=Protocol.HYSTERIA2,
        address=split.hostname,
        port=_int_port(split.port, 443),
        uri=uri,
        password=password,
        security="tls",
        sni=_first(qs, "sni") or _first(qs, "peer"),
        alpn=_normalise_alpn(_first(qs, "alpn")),
        allow_insecure=_boolish(_first(qs, "insecure")),
        obfs=_first(qs, "obfs"),
        obfs_password=_first(qs, "obfs-password") or _first(qs, "obfs_password"),
        label=unquote(split.fragment or ""),
        extra={k: v[0] for k, v in qs.items() if v},
    )
    if not cfg.sni:
        cfg.sni = cfg.address
    return cfg


def _parse_tuic(uri: str) -> Optional[Config]:
    split = urlsplit(uri)
    if not split.hostname:
        return None
    qs = parse_qs(split.query, keep_blank_values=True)
    cfg = Config(
        protocol=Protocol.TUIC,
        address=split.hostname,
        port=_int_port(split.port, 443),
        uri=uri,
        uuid=unquote(split.username or ""),
        password=unquote(split.password or ""),
        security="tls",
        sni=_first(qs, "sni") or _first(qs, "peer"),
        alpn=_normalise_alpn(_first(qs, "alpn")),
        allow_insecure=_boolish(_first(qs, "allow_insecure") or _first(qs, "insecure")),
        label=unquote(split.fragment or ""),
        extra={k: v[0] for k, v in qs.items() if v},
    )
    if not cfg.sni:
        cfg.sni = cfg.address
    return cfg


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(cfg: Config) -> Optional[str]:
    """Return an error string if the configuration is obviously unusable."""
    if not cfg.address:
        return t("пустой адрес")
    if not (MIN_PORT <= cfg.port <= MAX_PORT):
        return t("некорректный порт")
    if cfg.protocol in (Protocol.VLESS, Protocol.VMESS):
        if not _UUID_RE.match(cfg.uuid or ""):
            return t("некорректный UUID")
    if cfg.protocol == Protocol.TROJAN and not cfg.password:
        return t("отсутствует пароль")
    if cfg.protocol == Protocol.SHADOWSOCKS and not cfg.password:
        return t("отсутствует пароль")
    if cfg.protocol == Protocol.HYSTERIA2 and not cfg.password:
        return t("отсутствует пароль")
    if cfg.protocol == Protocol.TUIC and (not cfg.uuid or not cfg.password):
        return t("отсутствуют данные TUIC")
    if cfg.protocol == Protocol.SHADOWSOCKS_R and not cfg.password:
        return t("отсутствует пароль")
    return None