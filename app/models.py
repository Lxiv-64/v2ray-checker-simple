"""Normalised in-memory representation of a proxy configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .constants import Protocol


@dataclass
class Config:
    """A single proxy configuration normalised to one internal shape.

    ``raw`` keeps the original URI so that it can always be round-tripped back
    to Xray/sing-box verbatim, while the individual fields exist for indexing,
    filtering, deduplication and display.
    """

    protocol: Protocol
    address: str
    port: int
    uri: str

    # Common credentials / identifiers -----------------------------------
    uuid: str = ""            # vless/vmess
    password: str = ""        # trojan/ss/hysteria2/tuic
    method: str = ""          # ss cipher / ssr method
    flow: str = ""            # vless flow (xtls-rprx-vision)

    # Transport -----------------------------------------------------------
    network: str = "tcp"      # tcp | ws | grpc | http | h2 | quic | kcp
    security: str = "none"    # none | tls | reality
    sni: str = ""
    alpn: List[str] = field(default_factory=list)
    fingerprint: str = ""     # uTLS fingerprint (chrome, firefox, ...)
    allow_insecure: bool = False

    # WebSocket / HTTP / gRPC --------------------------------------------
    ws_path: str = ""
    ws_host: str = ""
    service_name: str = ""    # grpc
    header_type: str = ""     # http obfuscation / kcp header

    # Reality -------------------------------------------------------------
    pbk: str = ""             # public key
    sid: str = ""             # short id
    spx: str = ""             # spiderX

    # Shadowsocks specific ------------------------------------------------
    plugin: str = ""
    plugin_opts: str = ""

    # Hysteria2 / TUIC specific ------------------------------------------
    obfs: str = ""
    obfs_password: str = ""
    sni_hysteria: str = ""

    # Bookkeeping ---------------------------------------------------------
    label: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"

    def dedup_key(self) -> str:
        """Stable identity used for deduplication.

        Two configurations that would produce the same connection are treated
        as identical regardless of cosmetic differences (label, query order,
        default values).  The fingerprint deliberately ignores the human
        label and the ``extra`` bag.
        """
        payload = {
            "p": self.protocol.value,
            "a": self.address.lower(),
            "o": self.port,
            "u": self.uuid.lower(),
            "pw": self.password,
            "m": self.method.lower(),
            "fl": self.flow,
            "n": self.network.lower(),
            "s": self.security.lower(),
            "sni": self.sni.lower(),
            "alpn": sorted(a.lower() for a in self.alpn),
            "fp": self.fingerprint.lower(),
            "ws_path": self.ws_path,
            "ws_host": self.ws_host.lower(),
            "svc": self.service_name,
            "ht": self.header_type.lower(),
            "pbk": self.pbk,
            "sid": self.sid,
            "spx": self.spx,
            "plugin": self.plugin,
            "popt": self.plugin_opts,
            "obfs": self.obfs,
            "obfspw": self.obfs_password,
            "ins": self.allow_insecure,
            # VMess specifics that change the connection but live in ``extra``.
            "aid": str(self.extra.get("aid", "")),
            "scy": str(self.extra.get("scy", "")),
            "proto": str(self.extra.get("proto", "")),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["protocol"] = self.protocol.value
        d["alpn"] = json.dumps(self.alpn, ensure_ascii=False)
        d["extra"] = json.dumps(self.extra, ensure_ascii=False)
        d["fp"] = self.dedup_key()
        return d

    def summary(self) -> str:
        bits = [self.protocol.label, self.endpoint]
        if self.security and self.security != "none":
            bits.append(self.security.upper())
        if self.network and self.network != "tcp":
            bits.append(self.network)
        return " · ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["protocol"] = self.protocol.value
        return d


@dataclass
class CheckResult:
    """Outcome of a single probing run, persisted to the history table."""

    config_id: int
    stage: str = "none"           # CheckStage value
    success: bool = False
    latency_ms: Optional[int] = None
    error: str = ""
    level_reached: int = 0
    checked_at: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> Dict[str, Any]:
        return {
            "config_id": self.config_id,
            "stage": self.stage,
            "success": 1 if self.success else 0,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "level_reached": self.level_reached,
            "checked_at": self.checked_at,
            "detail": json.dumps(self.detail, ensure_ascii=False),
        }