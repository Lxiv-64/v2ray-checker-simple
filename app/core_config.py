"""Translate a normalised :class:`Config` into runnable core configuration.

Two engines are supported and their capabilities differ:

* **Xray-core** — VLESS, VMess, Trojan, Shadowsocks (incl. 2022), SOCKS.
* **sing-box** — everything Xray can do *plus* Hysteria2 and TUIC.

Each builder produces a complete configuration file with a single local SOCKS
inbound and one outbound pointing at the remote server, so the checker can
drive real protocol handshakes and real traffic through it instead of
simulating the protocols in-process.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .constants import Protocol
from .models import Config
from app.i18n import t


XRAY = "xray"
SINGBOX = "singbox"

# Transports the checker can actually configure for Xray-core.  Anything else
# must be reported as unsupported instead of being silently downgraded to TCP,
# which would probe a completely different configuration than the link means.
_XRAY_NETWORKS = {
    "tcp", "ws", "grpc", "http", "h2", "kcp", "quic",
    "xhttp", "splithttp", "httpupgrade",
}


class UnsupportedTransport(ValueError):
    """Raised when a configuration uses a transport the checker cannot build."""

# Protocols each engine is able to run.
XRAY_PROTOCOLS = {
    Protocol.VLESS,
    Protocol.VMESS,
    Protocol.TROJAN,
    Protocol.SHADOWSOCKS,
}
SINGBOX_PROTOCOLS = {
    Protocol.VLESS,
    Protocol.VMESS,
    Protocol.TROJAN,
    Protocol.SHADOWSOCKS,
    Protocol.HYSTERIA2,
    Protocol.TUIC,
}


def pick_core(cfg: Config) -> Optional[str]:
    """Return the engine best suited for ``cfg`` (Hysteria2/TUIC need sing-box)."""
    if cfg.protocol in (Protocol.HYSTERIA2, Protocol.TUIC):
        return SINGBOX
    if cfg.protocol in XRAY_PROTOCOLS:
        return XRAY
    if cfg.protocol in SINGBOX_PROTOCOLS:
        return SINGBOX
    return None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _sni(cfg: Config) -> str:
    return cfg.sni or cfg.ws_host or cfg.address


def _tls_alpn(cfg: Config) -> list:
    return list(cfg.alpn) if cfg.alpn else []


# ---------------------------------------------------------------------------
# Xray
# ---------------------------------------------------------------------------

def build_xray(cfg: Config, socks_port: int) -> Dict[str, Any]:
    outbound = {
        "tag": "proxy",
        "protocol": cfg.protocol.value,
        "streamSettings": _xray_stream(cfg),
        "settings": {},
    }
    if cfg.protocol == Protocol.VLESS:
        outbound["settings"] = {
            "vnext": [{
                "address": cfg.address,
                "port": cfg.port,
                "users": [{
                    "id": cfg.uuid,
                    "encryption": "none",
                    "flow": cfg.flow or "",
                    "level": 0,
                }],
            }]
        }
    elif cfg.protocol == Protocol.VMESS:
        try:
            alter_id = int(cfg.extra.get("aid", 0) or 0)
        except (TypeError, ValueError):
            alter_id = 0
        outbound["settings"] = {
            "vnext": [{
                "address": cfg.address,
                "port": cfg.port,
                "users": [{
                    "id": cfg.uuid,
                    "alterId": alter_id,
                    "security": str(cfg.extra.get("scy", "auto") or "auto"),
                    "level": 0,
                }],
            }]
        }
    elif cfg.protocol == Protocol.TROJAN:
        outbound["settings"] = {
            "servers": [{
                "address": cfg.address,
                "port": cfg.port,
                "password": cfg.password,
                "level": 0,
            }]
        }
    elif cfg.protocol == Protocol.SHADOWSOCKS:
        outbound["settings"] = {
            "servers": [{
                "address": cfg.address,
                "port": cfg.port,
                "method": cfg.method or "aes-128-gcm",
                "password": cfg.password,
                "level": 0,
            }]
        }

    return {
        "log": {"loglevel": "warning", "access": "", "error": ""},
        "inbounds": [{
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "port": socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
            "sniffing": {"enabled": False},
        }],
        "outbounds": [
            outbound,
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
        "routing": {"domainStrategy": "AsIs", "rules": []},
    }


def _xray_stream(cfg: Config) -> Dict[str, Any]:
    network = (cfg.network or "tcp").lower()
    if network not in _XRAY_NETWORKS:
        raise UnsupportedTransport(
            t('транспорт «{}» не поддержан Xray-проверкой').format(cfg.network)
        )
    stream: Dict[str, Any] = {"network": network}
    sec = cfg.security if cfg.security in ("tls", "reality", "none") else "none"

    if sec == "tls":
        tls: Dict[str, Any] = {
            "serverName": _sni(cfg),
            "allowInsecure": bool(cfg.allow_insecure),
        }
        if cfg.fingerprint:
            tls["fingerprint"] = cfg.fingerprint
        if cfg.alpn:
            tls["alpn"] = _tls_alpn(cfg)
        stream["security"] = "tls"
        stream["tlsSettings"] = tls
    elif sec == "reality":
        reality: Dict[str, Any] = {
            "serverName": _sni(cfg),
            "publicKey": cfg.pbk,
            "shortId": cfg.sid,
            "spiderX": cfg.spx or "/",
        }
        if cfg.fingerprint:
            reality["fingerprint"] = cfg.fingerprint
        if cfg.alpn:
            reality["alpn"] = _tls_alpn(cfg)
        stream["security"] = "reality"
        stream["realitySettings"] = reality
    else:
        stream["security"] = "none"

    if network == "ws":
        ws: Dict[str, Any] = {"path": cfg.ws_path or "/"}
        if cfg.ws_host:
            ws["headers"] = {"Host": cfg.ws_host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": cfg.service_name or cfg.ws_path or "",
        }
    elif network in ("http", "h2"):
        stream["httpSettings"] = {
            "path": cfg.ws_path or "/",
            "host": [cfg.ws_host] if cfg.ws_host else [],
        }
    elif network in ("xhttp", "splithttp"):
        # XHTTP (a.k.a. SplitHTTP) carries the stream over HTTP POST/GET pairs.
        # ``mode`` selects the framing (auto / packet-up / stream-up) and must
        # match what the server expects, otherwise the probe tests a different
        # protocol than the link describes.
        xhttp: Dict[str, Any] = {"path": cfg.ws_path or "/"}
        if cfg.ws_host:
            xhttp["host"] = cfg.ws_host
        mode = str(cfg.extra.get("mode") or "").strip()
        if mode:
            xhttp["mode"] = mode
        stream["xhttpSettings"] = xhttp
    elif network == "httpupgrade":
        upgrade: Dict[str, Any] = {"path": cfg.ws_path or "/"}
        if cfg.ws_host:
            upgrade["host"] = cfg.ws_host
        stream["httpupgradeSettings"] = upgrade
    elif network == "kcp":
        stream["kcpSettings"] = {
            "header": {"type": cfg.header_type or "none"},
        }
    elif network == "quic":
        stream["quicSettings"] = {
            "security": "none",
            "key": "",
            "header": {"type": cfg.header_type or "none"},
        }
    elif network == "tcp" and cfg.header_type == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {
                    "path": [cfg.ws_path or "/"],
                    "headers": {"Host": [cfg.ws_host] if cfg.ws_host else []},
                },
            }
        }
    return stream


# ---------------------------------------------------------------------------
# sing-box
# ---------------------------------------------------------------------------

def build_singbox(cfg: Config, socks_port: int) -> Dict[str, Any]:
    outbound: Dict[str, Any] = {
        "type": cfg.protocol.value,
        "tag": "proxy",
        "server": cfg.address,
        "server_port": cfg.port,
    }

    if cfg.protocol == Protocol.VLESS:
        outbound["uuid"] = cfg.uuid
        if cfg.flow:
            outbound["flow"] = cfg.flow
        outbound["tls"] = _sb_tls(cfg)
    elif cfg.protocol == Protocol.VMESS:
        outbound["uuid"] = cfg.uuid
        try:
            outbound["alter_id"] = int(cfg.extra.get("aid", 0) or 0)
        except (TypeError, ValueError):
            outbound["alter_id"] = 0
        outbound["security"] = str(cfg.extra.get("scy", "auto") or "auto")
        outbound["tls"] = _sb_tls(cfg)
    elif cfg.protocol == Protocol.TROJAN:
        outbound["password"] = cfg.password
        outbound["tls"] = _sb_tls(cfg, force=True)
    elif cfg.protocol == Protocol.SHADOWSOCKS:
        outbound["method"] = cfg.method or "aes-128-gcm"
        outbound["password"] = cfg.password
        if cfg.plugin:
            outbound["plugin"] = cfg.plugin
            if cfg.plugin_opts:
                outbound["plugin_opts"] = cfg.plugin_opts
    elif cfg.protocol == Protocol.HYSTERIA2:
        outbound["password"] = cfg.password
        outbound["tls"] = _sb_tls(cfg, force=True)
        if cfg.obfs:
            outbound["obfs"] = {"type": cfg.obfs, "password": cfg.obfs_password}
    elif cfg.protocol == Protocol.TUIC:
        outbound["uuid"] = cfg.uuid
        outbound["password"] = cfg.password
        outbound["tls"] = _sb_tls(cfg, force=True)
        outbound["congestion_control"] = "bbr"

    transport = _sb_transport(cfg)
    if transport and cfg.protocol not in (Protocol.HYSTERIA2, Protocol.TUIC):
        outbound["transport"] = transport

    return {
        "log": {"level": "warn", "timestamp": False},
        "inbounds": [{
            "type": "socks",
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "listen_port": socks_port,
        }],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"rules": [], "final": "proxy", "auto_detect_interface": False},
    }


def _sb_tls(cfg: Config, force: bool = False) -> Dict[str, Any]:
    if cfg.security not in ("tls", "reality") and not force:
        return {}
    tls: Dict[str, Any] = {"enabled": True, "server_name": _sni(cfg)}
    if cfg.allow_insecure:
        tls["insecure"] = True
    if cfg.alpn:
        tls["alpn"] = _tls_alpn(cfg)
    if cfg.security == "reality":
        tls["reality"] = {
            "enabled": True,
            "public_key": cfg.pbk,
            "short_id": cfg.sid,
        }
    if cfg.fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": cfg.fingerprint}
    return tls


def _sb_transport(cfg: Config) -> Dict[str, Any]:
    network = cfg.network or "tcp"
    if network == "ws":
        transport: Dict[str, Any] = {"type": "ws", "path": cfg.ws_path or "/"}
        if cfg.ws_host:
            transport["headers"] = {"Host": cfg.ws_host}
        return transport
    if network == "grpc":
        return {"type": "grpc", "service_name": cfg.service_name or ""}
    if network in ("http", "h2"):
        transport = {"type": "http", "path": cfg.ws_path or "/"}
        if cfg.ws_host:
            transport["host"] = [cfg.ws_host]
        return transport
    return {}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def build(cfg: Config, core: str, socks_port: int) -> Dict[str, Any]:
    if core == XRAY:
        return build_xray(cfg, socks_port)
    if core == SINGBOX:
        return build_singbox(cfg, socks_port)
    raise ValueError(f"unknown core: {core}")


def dumps(config: Dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2)