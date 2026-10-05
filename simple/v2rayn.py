"""Готовая конфигурация Xray для офлайн-импорта в v2rayN.

Офлайн v2rayN умеет принимать только «пользовательскую конфигурацию» (меню
**«Серверы» → «Добавить пользовательскую конфигурацию»**): клиент читает полный
JSON-конфиг ядра и запускает Xray с ним.  Поэтому, чтобы получить в v2rayN всю
нашу группу рабочих серверов одной записью, мы собираем именно такой конфиг:

* ``inbounds`` — локальные SOCKS и HTTP порты;
* ``outbounds`` — по одному outbound на каждый рабочий сервер (+ ``direct``);
* ``observatory`` — измеритель задержек (``probeUrl`` — цель контрольного
  запроса из настроек);
* ``routing`` — правило «весь tcp/udp → балансировщик» со стратегией
  ``leastPing``.

``observatory`` обязателен: без него ``leastPing`` не работает и ядро вообще
отказывается стартовать («not all dependencies are resolved») — проверено на
Xray-core 26.3.27, в том числе тем бинарником, который использует ваш v2rayN.

Конфигурации, которые ядро Xray не умеет собирать (Hysteria2, TUIC), в JSON не
попадают и остаются в текстовом списке ссылок.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence, Tuple

from app import core_config
from app.models import Config
from app.settings import get_logger
from app.i18n import t


log = get_logger()

DEFAULT_SOCKS_PORT = 10808
DEFAULT_HTTP_PORT = 10809
DEFAULT_PROBE_URL = "https://www.google.com/generate_204"
BALANCER_TAG = "auto"
BALANCER_STRATEGY = "leastPing"


def is_supported(cfg: Config) -> bool:
    """Может ли ядро Xray поднять эту конфигурацию."""
    return core_config.pick_core(cfg) == core_config.XRAY


def xray_configs(configs: Sequence[Config]) -> Tuple[List[Config], int]:
    """Отфильтровать конфигурации, понятные ядру Xray.

    Возвращает ``(подходящие, сколько_пропущено)``: для v2rayN годятся только
    те протоколы, которые умеет Xray-core (Hysteria2 и TUIC — нет).
    """
    kept = [cfg for cfg in configs if is_supported(cfg)]
    return kept, len(configs) - len(kept)


def build_outbounds(configs: Sequence[Config],
                    tag_prefix: str = "proxy") -> Tuple[List[Dict], int]:
    """Собрать outbounds для конфигураций, понятных ядру Xray.

    Возвращает ``(outbounds, сколько_пропущено)``.
    """
    outbounds: List[Dict] = []
    skipped = 0
    for index, cfg in enumerate(configs):
        if not is_supported(cfg):
            skipped += 1
            continue
        try:
            generated = core_config.build_xray(cfg, 0)
            outbound = generated["outbounds"][0]
        except Exception as exc:  # noqa: BLE001
            log.warning(t("v2rayN: конфигурация %s пропущена: %s"),
                        cfg.endpoint, exc)
            skipped += 1
            continue
        outbound = dict(outbound)
        outbound["tag"] = f"{tag_prefix}-{index}"
        outbounds.append(outbound)
    return outbounds, skipped


def build_document(configs: Sequence[Config], socks_port: int = DEFAULT_SOCKS_PORT,
                   http_port: int = DEFAULT_HTTP_PORT,
                   probe_url: str = DEFAULT_PROBE_URL) -> Tuple[Dict, int, int]:
    """Собрать документ конфигурации. Возвращает ``(документ, принято, пропущено)``."""
    proxies, skipped = build_outbounds(configs)
    tags = [item["tag"] for item in proxies]
    probe = (probe_url or "").strip()
    if "://" not in probe:
        probe = DEFAULT_PROBE_URL

    document: Dict[str, Any] = {
        "log": {"loglevel": "warning", "access": "", "error": ""},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": int(socks_port),
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": True,
                             "destOverride": ["http", "tls"],
                             "routeOnly": False},
            },
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": int(http_port),
                "protocol": "http",
                "settings": {},
            },
        ],
        "outbounds": proxies + [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
    }

    if proxies:
        document["observatory"] = {
            "subjectSelector": tags,
            "probeUrl": probe,
            "probeInterval": "60s",
            "enableConcurrency": True,
        }
        document["routing"] = {
            "domainStrategy": "AsIs",
            "rules": [{"type": "field", "network": "tcp,udp",
                       "balancerTag": BALANCER_TAG}],
            "balancers": [{
                "tag": BALANCER_TAG,
                "selector": tags,
                "strategy": {"type": BALANCER_STRATEGY},
            }],
        }
    else:
        # Рабочих серверов нет: конфиг остаётся валидным и выпускает трафик
        # напрямую, чтобы файл никогда не был «сломанным».
        document["routing"] = {"domainStrategy": "AsIs", "rules": [],
                               "final": "direct"}
    return document, len(proxies), skipped


def build_client_config(configs: Sequence[Config], socks_port: int = DEFAULT_SOCKS_PORT,
                        http_port: int = DEFAULT_HTTP_PORT,
                        probe_url: str = DEFAULT_PROBE_URL) -> Tuple[str, int, int]:
    """Собрать текст JSON для импорта в v2rayN.

    Возвращает ``(текст_файла, принято, пропущено)``.
    """
    document, included, skipped = build_document(
        configs, socks_port=socks_port, http_port=http_port, probe_url=probe_url)
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    return text, included, skipped
