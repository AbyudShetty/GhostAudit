"""Shared timestamp parsing and audit-time eligibility.

All timestamps in the dataset are UTC, formatted ``YYYY-MM-DDTHH:MM:SSZ``.
Parsing is strict: anything else raises ``ValueError`` rather than guessing.

Audit-time rule (problem_statement.md / AGENTS.md sections 6.2 and 6.6):
evidence captured *after* ``audit_at`` must not change the answer and must
never be cited. The boundary is inclusive: ``captured_at == audit_at`` is
eligible (evidence_51 is captured exactly at case_31's audit time and is cited
by the sample answer).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

_ISO_Z = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]00:?00)$")


def parse_ts(value: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp into an aware ``datetime``."""
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be str, got {type(value).__name__}")
    m = _ISO_Z.match(value.strip())
    if not m:
        raise ValueError(f"unsupported timestamp format: {value!r}")
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def try_parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Like :func:`parse_ts` but returns ``None`` for missing/invalid input."""
    if value is None:
        return None
    try:
        return parse_ts(value)
    except ValueError:
        return None


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_eligible(captured_at: str, audit_at: str) -> bool:
    """True when evidence captured at ``captured_at`` may be used at ``audit_at`` (inclusive)."""
    return parse_ts(captured_at) <= parse_ts(audit_at)


def observation_usable(file_captured_at: str, obs_time: Optional[str], audit_at: str) -> bool:
    """Strictest usable test for a single observation.

    The containing file must be eligible; if the record carries its own
    parseable timestamp, that must also be at or before ``audit_at``. A record
    without a timestamp is judged by its file alone (nothing is invented).
    """
    if not is_eligible(file_captured_at, audit_at):
        return False
    t = try_parse_ts(obs_time)
    return True if t is None else t <= parse_ts(audit_at)
