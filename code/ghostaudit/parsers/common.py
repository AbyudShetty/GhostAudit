"""Helpers shared by the text-based parsers."""

from __future__ import annotations

import re
from typing import Dict, Iterator, List, Optional, Tuple

_KV_TOKEN = re.compile(r'\s*([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')


def decode_text(data: bytes) -> str:
    """Decode evidence bytes as UTF-8, tolerating a BOM. Never falls back to cp1252.

    A decoding error is raised (and reported by the caller as a parse failure)
    instead of silently producing mojibake.
    """
    return data.decode("utf-8-sig")


def split_lines(text: str) -> List[Tuple[int, str]]:
    """``(1-based line number, line)`` pairs, CRLF- and LF-safe, trailing CR stripped."""
    return [(i, ln.rstrip("\r")) for i, ln in enumerate(text.split("\n"), start=1)]


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


def parse_kv(text: str) -> Tuple[Dict[str, str], str]:
    """Parse ``key=value key="quoted value"`` pairs.

    Returns ``(pairs, residue)``. ``residue`` is any non-whitespace text that
    could not be consumed as a pair (empty string on a clean parse); callers
    must treat a non-empty residue as a parse failure. Duplicate keys keep the
    first value and are reported through the ``__dup__`` key (comma list).
    """
    out: Dict[str, str] = {}
    dups: List[str] = []
    pos = 0
    while pos < len(text):
        m = _KV_TOKEN.match(text, pos)
        if not m:
            break
        k, v = m.group(1), _unquote(m.group(2))
        if k in out:
            dups.append(k)
        else:
            out[k] = v
        pos = m.end()
    residue = text[pos:].strip()
    if dups:
        out["__dup__"] = ",".join(dups)
    return out, residue


def first_present(d: Dict[str, object], keys: Tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


TIME_KEYS = ("observed_at", "recorded_at", "updated_at", "indexed_at")
