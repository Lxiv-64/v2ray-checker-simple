"""One-off source migration: wrap user-facing string literals with ``t(...)``.

The application is written with Russian literals.  To make it bilingual the
literals are routed through :func:`app.i18n.t`; because Russian is the source
language this is a *behaviour-preserving* transformation (``t(x) == x`` for the
default language).

What the tool does, for every ``*.py`` module under ``app/`` and ``simple/``
(except the i18n machinery itself):

* wraps every string constant that contains Cyrillic characters with ``t(...)``
  (module/class/function docstrings and the runtime *status* values that are
  persisted and compared are left untouched);
* rewrites f-strings that contain Cyrillic into ``t("…").format(…)`` templates;
* inserts ``from app.i18n import t`` when at least one literal was wrapped.

It is idempotent enough to be re-run: a file that already imports ``t`` is left
alone.
"""

from __future__ import annotations

import ast
import os
import re
from typing import List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = ("app", "simple")
SKIP = {"app/i18n.py", "app/locale_en.py"}
IMPORT_LINE = "from app.i18n import t"

_CYR = re.compile("[" + chr(0x400) + "-" + chr(0x4FF) + "]")


def _has_cyr(text: str) -> bool:
    return bool(_CYR.search(text))


def _docstring_ids(tree: ast.AST) -> Set[int]:
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _status_value_ids(tree: ast.AST) -> Set[int]:
    """Ids of constants assigned to a *stored* status field.

    Such values (``preset.last_status``, ``source["last_status"]``) are written
    to ``settings.json`` and compared as plain data, so they must not change
    with the interface language; they are translated only on display, via
    :func:`app.i18n.status_text`.
    """
    def is_status_target(tgt: ast.AST) -> bool:
        if isinstance(tgt, ast.Attribute):
            return tgt.attr.lower().endswith("status")
        if isinstance(tgt, ast.Subscript):
            key = tgt.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return "status" in key.value.lower()
        return False

    ids: Set[int] = set()
    for node in ast.walk(tree):
        targets: List[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        if not targets or not any(is_status_target(t) for t in targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, (ast.Constant, ast.JoinedStr)):
                ids.add(id(sub))
    return ids


class _Converter:
    def __init__(self, src: str) -> None:
        self.src = src
        self.lines = src.splitlines(keepends=True)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line.encode("utf-8")))

    def span(self, node: ast.AST) -> Tuple[int, int]:
        start = self.starts[node.lineno - 1] + node.col_offset
        end = self.starts[node.end_lineno - 1] + node.end_col_offset
        return start, end

    def segment(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.src.encode("utf-8")[start:end].decode("utf-8")


def _format_spec_text(joined: ast.JoinedStr) -> Optional[str]:
    parts: List[str] = []
    for part in joined.values:
        if isinstance(part, ast.Constant):
            parts.append(str(part.value))
        else:
            return None            # nested {…:{…}} — too exotic, leave as is
    return "".join(parts)


def _fstring_template(conv: _Converter,
                      node: ast.JoinedStr) -> Optional[Tuple[str, List[str]]]:
    template: List[str] = []
    args: List[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            template.append(str(part.value).replace("{", "{{").replace("}", "}}"))
        elif isinstance(part, ast.FormattedValue):
            spec = ""
            if part.format_spec is not None:
                rendered = _format_spec_text(part.format_spec)  # type: ignore[arg-type]
                if rendered is None:
                    return None
                spec = ":" + rendered
            conversion = ""
            if part.conversion is not None and part.conversion != -1:
                conversion = "!" + chr(part.conversion)
            template.append("{" + conversion + spec + "}")
            args.append(conv.segment(part.value))
        else:
            return None
    return "".join(template), args


def transform_source(path: str, src: str) -> Tuple[str, int]:
    """Return ``(new_source, wrapped_count)`` for one module."""
    del path  # retained for signature parity / future per-file tweaks
    tree = ast.parse(src)
    skip = _docstring_ids(tree) | _status_value_ids(tree)
    conv = _Converter(src)

    wraps: List[Tuple[int, int, bytes]] = []
    joined_raw: List[Tuple[int, int, bytes]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or id(node) in skip:
            continue
        if not any(isinstance(p, ast.Constant) and isinstance(p.value, str)
                   and _has_cyr(p.value) for p in node.values):
            continue
        start, end = conv.span(node)
        built = _fstring_template(conv, node)
        if built is None:
            joined_raw.append((start, end,
                               b"t(" + conv.segment(node).encode("utf-8") + b")"))
            continue
        template, args = built
        replacement = "t(%r)" % template
        if args:
            replacement += ".format(" + ", ".join(args) + ")"
        joined_raw.append((start, end, replacement.encode("utf-8")))

    joined_spans = [(start, end) for start, end, _text in joined_raw]
    # Keep only the outermost f-strings: an f-string nested in another one (or
    # the Constant that Python reports for a mixed "…" f"…" concatenation, whose
    # span can even overlap its parent) is already contained in the outer edit.
    for start, end, text in joined_raw:
        if any(js <= start and end <= je and (js, je) != (start, end)
               for js, je in joined_spans):
            continue
        wraps.append((start, end, text))
    joined_spans = [(start, end) for start, end, _text in wraps]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip or not _has_cyr(node.value):
            continue
        start, end = conv.span(node)
        if any(js <= start and end <= je for js, je in joined_spans):
            continue
        raw = conv.segment(node).encode("utf-8")
        wraps.append((start, end, b"t(" + raw + b")"))

    filtered = wraps

    if not filtered:
        return src, 0

    last_import_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_end = max(last_import_end, conv.span(node)[1])
    if last_import_end == 0:
        first = tree.body[0] if tree.body else None
        if (first is not None and isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            last_import_end = conv.span(first)[1]

    if IMPORT_LINE not in src:
        insertion = b"\n" + IMPORT_LINE.encode("utf-8") + b"\n"
        offset = last_import_end
        filtered.append((offset, offset, insertion))

    data = bytearray(src.encode("utf-8"))
    for start, end, text in sorted(filtered, key=lambda item: item[0],
                                   reverse=True):
        data[start:end] = text
    return data.decode("utf-8"), len(filtered)


def iter_modules() -> List[str]:
    result: List[str] = []
    for package in PACKAGES:
        base = os.path.join(ROOT, package)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                rel = rel.replace(os.sep, "/")
                if rel in SKIP:
                    continue
                result.append(rel)
    return sorted(result)


def main() -> int:
    total = 0
    for rel in iter_modules():
        path = os.path.join(ROOT, rel)
        with open(path, "r", encoding="utf-8-sig") as handle:
            src = handle.read()
        if IMPORT_LINE in src:
            print("skip (already wrapped): %s" % rel)
            continue
        new_src, count = transform_source(path, src)
        if count == 0:
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_src)
        total += count
        print("wrapped %4d literals: %s" % (count, rel))
    print("total wrapped: %d" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
