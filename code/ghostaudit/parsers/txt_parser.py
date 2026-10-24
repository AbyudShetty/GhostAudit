"""Plain-text handover records -> Observations.

Format (evidence/records/customer_account_handover_*.txt)::

    CUSTOMER PLATFORM - SHIFT HANDOVER
    Snapshot: 2026-10-01T12:00:00Z
    ...prose...
    - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | user@example.test

Lines starting with ``- `` are entries and must have exactly six ``|``-separated
fields with a valid timestamp; anything else starting with ``- `` is a failure.
Other non-blank lines (title, ``Snapshot:`` header, prose) are counted as
non-evidence units; the ``Snapshot:`` value is kept in ``meta``.
"""

from __future__ import annotations

import re
from typing import Optional

from ..evidence import FMT_TXT, FileResult, Observation, ParseFailure
from ..timeutil import try_parse_ts
from .common import split_lines

_SNAPSHOT = re.compile(r"^Snapshot:\s*(\S+)\s*$")


def parse_txt_text(file_id: str, path: str, text: str, captured_at: Optional[str] = None) -> FileResult:
    res = FileResult(file_id, path, FMT_TXT, captured_at, unit_label="non-blank lines")
    for n, line in split_lines(text):
        if not line.strip():
            continue
        res.input_units += 1
        if not line.startswith("- "):
            m = _SNAPSHOT.match(line)
            if m:
                res.meta["snapshot"] = m.group(1)
            res.non_evidence_units += 1
            continue
        parts = [p.strip() for p in line[2:].split("|")]
        if len(parts) != 6:
            res.failures.append(ParseFailure(file_id, path, f"line:{n}", f"entry has {len(parts)} fields, expected 6", line[:160]))
            res.failed_units += 1
            continue
        ts, system, rid, category, state, ident = parts
        if try_parse_ts(ts) is None or not rid:
            res.failures.append(ParseFailure(file_id, path, f"line:{n}", "invalid timestamp or empty resource id", line[:160]))
            res.failed_units += 1
            continue
        res.observations.append(
            Observation(
                file_id=file_id, fmt=FMT_TXT, locator=f"line:{n}", resource_id=rid, system_id=system or None,
                collection=category, observed_at=ts, source_time=res.meta.get("snapshot"),
                fields={"category": category, "state": state, "identifier": ident}, raw=line,
            )
        )
    return res
