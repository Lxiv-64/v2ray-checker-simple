"""Download and read configuration sources.

Sources come in three flavours:

* **URL** — anything ``urllib`` can fetch: plain text lists, base64 subscription
  bodies, HTML pages that embed links, or JSON subscription payloads.
* **FILE** — a local text/base64 file, re-read on every refresh.
* **MANUAL** — individual URIs entered through the GUI.

Only the raw bytes are handled here; parsing and normalisation happen elsewhere
so that downloads can be parallelised independently of the CPU-bound parsing.
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Dict, List

from .settings import get_logger
from app.i18n import t


log = get_logger()


@dataclass
class FetchResult:
    ok: bool
    text: str = ""
    error: str = ""
    status: int = 0
    headers: Dict[str, str] = field(default_factory=dict)


def _decode_body(raw: bytes, headers: Dict[str, str]) -> str:
    encoding = (headers.get("content-encoding") or "").lower()
    try:
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        pass

    charset = ""
    content_type = headers.get("content-type", "")
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
    for candidate in (charset, "utf-8", "utf-8-sig", "cp1251", "latin-1"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_url(url: str, timeout: float = 30.0,
              user_agent: str = "v2rayNG/1.8.5") -> FetchResult:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return FetchResult(ok=False, error=t("поддерживаются только http/https"))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        },
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read()
            headers = {k.lower(): v for k, v in response.headers.items()}
            status = getattr(response, "status", 200) or 200
        text = _decode_body(raw, headers)
        return FetchResult(ok=True, text=text, status=status, headers=headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
            text = _decode_body(body, {})
            if text.strip():
                return FetchResult(ok=True, text=text, status=exc.code,
                                   error=f"HTTP {exc.code}")
        except Exception:
            pass
        return FetchResult(ok=False, error=f"HTTP {exc.code}", status=exc.code)
    except urllib.error.URLError as exc:
        return FetchResult(ok=False, error=t('сеть: {}').format(exc.reason))
    except socket.timeout:
        return FetchResult(ok=False, error=t("тайм-аут"))
    except ssl.SSLError as exc:
        return FetchResult(ok=False, error=f"TLS: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface any transport error
        return FetchResult(ok=False, error=str(exc))


def fetch_file(path: str) -> FetchResult:
    path = os.path.expanduser(path.strip().strip('"'))
    if not os.path.isfile(path):
        return FetchResult(ok=False, error=t("файл не найден"))
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return FetchResult(ok=False, error=str(exc))
    return FetchResult(ok=True, text=_decode_body(raw, {}))


def fetch_source(kind: str, value: str, timeout: float = 30.0,
                 user_agent: str = "v2rayNG/1.8.5") -> FetchResult:
    kind = (kind or "").lower()
    if kind == "url":
        return fetch_url(value, timeout=timeout, user_agent=user_agent)
    if kind == "file":
        return fetch_file(value)
    if kind == "manual":
        return FetchResult(ok=True, text=value)
    return FetchResult(ok=False, error=t('неизвестный тип источника: {}').format(kind))


def normalise_source_payload(text: str) -> str:
    """Unwrap JSON subscription envelopes that some panels emit.

    Many providers return ``{"servers": [...], "data": "base64..."}`` or a bare
    JSON array of URIs.  This helper turns those into a newline separated list
    without damaging ordinary text.
    """
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return text

    collected: List[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            if "://" in node:
                collected.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(data)
    if collected:
        return "\n".join(collected)
    return text