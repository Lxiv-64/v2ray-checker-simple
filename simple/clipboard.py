"""Разбор текста из буфера: подписка/файл или конкретные конфигурации.

Задача — не заставлять пользователя выбирать кнопку: если в буфере ссылка на
подписку или путь к файлу со списком, значение должно попасть в источники; если
в буфере сами конфигурации (``vless://``, ``vmess://`` и прочие), они должны
сразу уйти в пул.

Модуль не зависит от интерфейса: он только раскладывает строки по трём корзинам,
а решение «что с этим делать» принимает вызывающая сторона.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Tuple

from app.parser import extract_uris

#: Схемы, которые считаются конкретной конфигурацией (а не подпиской).
CONFIG_SCHEMES: Tuple[str, ...] = (
    "vless://", "vmess://", "trojan://", "trojan-go://", "ss://", "ssr://",
    "hysteria2://", "hy2://", "tuic://",
)

#: Схемы ссылок на подписки/файлы.
SOURCE_SCHEMES: Tuple[str, ...] = ("http://", "https://", "file://")

#: Расширения, которые считаются «файлом со списком» даже если файла рядом нет.
SOURCE_SUFFIXES: Tuple[str, ...] = (".txt", ".list", ".lst", ".sub", ".json",
                                    ".yaml", ".yml")

#: Строка файла длиннее этого — почти наверняка не путь, а текст.
PATH_LIMIT = 320


@dataclass(frozen=True)
class ClipboardPlan:
    """Что лежало в буфере: конфигурации, источники и всё непонятное."""

    configs: str = ""
    sources: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    unknown: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_configs(self) -> bool:
        return bool(self.configs.strip())

    @property
    def has_sources(self) -> bool:
        return bool(self.sources)

    @property
    def is_empty(self) -> bool:
        return not (self.has_configs or self.has_sources)


def _looks_like_path(text: str, file_exists: Callable[[str], bool]) -> bool:
    """Похоже ли на путь к файлу со списком (и существует ли он)."""
    if not text or len(text) > PATH_LIMIT or "\n" in text:
        return False
    if text.startswith("-") or "://" in text:
        return False
    if file_exists(text):
        return True
    lowered = text.lower()
    if lowered.endswith(SOURCE_SUFFIXES) and os.sep in text:
        return True
    return False


def classify_clipboard(text: str,
                       file_exists: Callable[[str], bool] = os.path.isfile
                       ) -> ClipboardPlan:
    """Разложить текст буфера по корзинам: конфигурации, источники, мусор.

    Построчный разбор: каждая строка проверяется по порядку «конфигурация →
    ссылка → файл → распаковка base64».  Если ничего распознать не удалось,
    строка попадает в ``unknown`` — интерфейс покажет её в поле вставки, чтобы
    пользователь мог поправить вручную.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ClipboardPlan()

    configs: List[str] = []
    sources: List[Tuple[str, str]] = []
    unknown: List[str] = []

    for line in lines:
        lowered = line.lower()
        if lowered.startswith(CONFIG_SCHEMES):
            configs.append(line)
            continue
        if lowered.startswith(("http://", "https://")):
            sources.append(("url", line))
            continue
        if lowered.startswith("file://"):
            path = line[len("file://"):].strip()
            if path:
                sources.append(("file", path))
            continue
        if _looks_like_path(line, file_exists):
            sources.append(("file", line))
            continue
        # Строка без схемы: возможно, это base64-подписка целиком.
        if extract_uris(line):
            configs.append(line)
            continue
        unknown.append(line)

    if not configs and not sources and unknown:
        # Ничего не распознали — возможно, это документ с ссылками внутри
        # (несколько адресов в одной строке или текст с вкраплениями).
        if extract_uris(text):
            configs.append(text)

    return ClipboardPlan(configs="\n".join(configs),
                         sources=tuple(sources),
                         unknown=tuple(unknown))
