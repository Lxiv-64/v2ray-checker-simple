"""Runtime translation (i18n) with built-in Russian and English catalogs.

The application is authored in Russian and Russian is treated as the *source*
language: :func:`t` returns its argument unchanged whenever the active language
is ``ru``.  That keeps the original behaviour (and the whole self-test suite)
byte-for-byte identical while English is delivered purely as a translation
layer on top.

The English catalog lives in :mod:`app.locale_en` as a flat
``{russian: english}`` mapping.  A missing entry falls back to the original
Russian string, so an incomplete catalog can never produce an empty label.

Language selection is persisted in ``settings.json`` under the ``language`` key
and applied once, before the interface is built, with :func:`set_language`.
Changing the language therefore requires a restart (the widgets are created
once); the Settings tab prompts the user accordingly.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

#: Language used when nothing else is configured.
DEFAULT_LANGUAGE = "ru"

#: ``(code, native name)`` pairs shown in the language selector.  The names are
#: intentionally *not* translated: a language is always listed in its own form.
LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("ru", "Русский"),
    ("en", "English"),
)

_LANGUAGE_CODES = tuple(code for code, _name in LANGUAGES)

# The catalog is imported eagerly so that frozen builds (PyInstaller) always
# bundle it, even though lookups only happen for the English language.
try:  # pragma: no cover - defensive, the module ships with the package
    from .locale_en import ENGLISH as _ENGLISH
except Exception:  # noqa: BLE001
    _ENGLISH = {}

_catalog: Dict[str, str] = dict(_ENGLISH)
_language: str = DEFAULT_LANGUAGE


def set_language(code: object) -> str:
    """Activate ``code`` (``"ru"``/``"en"``); unknown values fall back to RU.

    Returns the language that ended up active, which lets callers store the
    normalised value back into the settings.
    """
    global _language
    normalised = str(code or DEFAULT_LANGUAGE).strip().lower()
    if normalised not in _LANGUAGE_CODES:
        normalised = DEFAULT_LANGUAGE
    _language = normalised
    return _language


def get_language() -> str:
    """Return the currently active language code."""
    return _language


def available_languages() -> List[Tuple[str, str]]:
    """Return the selectable ``(code, native name)`` pairs."""
    return list(LANGUAGES)


def language_label(code: object) -> str:
    """Return the native name of a language code (or the code itself)."""
    text = str(code)
    for candidate, name in LANGUAGES:
        if candidate == text:
            return name
    return text


def language_code(label: object) -> str:
    """Inverse of :func:`language_label`; unknown labels map to the default."""
    text = str(label)
    for code, name in LANGUAGES:
        if name == text:
            return code
    return DEFAULT_LANGUAGE


def t(text: str) -> str:
    """Translate a source (Russian) string into the active language."""
    if _language == "en" and text:
        return _catalog.get(text, text)
    return text


def status_text(value: object) -> str:
    """Translate a stored status value produced by the runtime.

    Statuses such as ``"ок"`` / ``"успешно"`` / ``"ошибка: …"`` are written to
    ``settings.json`` and compared as plain data, so they are deliberately kept
    out of the source-string wrapping.  They are translated here, at the point
    where they are shown, which keeps stored data language-neutral.
    """
    text = str(value or "")
    if not text:
        return ""
    if text == "ок":
        return t("ок")
    if text == "успешно":
        return t("успешно")
    if text.startswith("ошибка"):
        tail = text[len("ошибка"):].lstrip(": ")
        return t("ошибка") + (" " + tail if tail else "")
    return t(text)


__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "available_languages",
    "get_language",
    "language_code",
    "language_label",
    "set_language",
    "status_text",
    "t",
]
