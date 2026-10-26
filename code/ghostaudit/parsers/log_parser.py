"""Service log exports -> Observations.

Every line starts with a UTC timestamp, then one of these dialects (all present
in dataset/evidence/logs):

==================  ===========================================================
``logfmt``          ``TS level=INFO service=S trace_id=T event=E k=v ...``
``logfmt-poslevel`` ``TS INFO service=S event=record_lookup k="v" ...``
``jsonl``           ``TS {"severity":..,"service":..,"trace_id":..,"event":..}``
``bracket``         ``TS [LEVEL] [service] [trace] event: k="v" ...``
``pipe``            ``TS | LEVEL | service | trace | event | k="v" ...``
``syslog``          ``TS <PRI>1 host service trace - - level=L k="v" ...``
``requestid``       ``TS requestId=T level=L function=F message="k=\\"v\\" ..."``
==================  ===========================================================

One Observation is produced per line that carries a ``resource_id``. Lines that
belong to the same ``trace_id`` (interleaved, anywhere in the file) are attached
to it as ``trace_events`` so that the *subject key* logged by a ``*_started``
line (``lookup_key``, ``subject_ref`` ...) stays linked to the ``resource_id``
reported by the ``*_completed`` line. Lines without a ``resource_id``
(connection, retry, cache, scheduler noise) are counted in
``non_evidence_units`` and remain reachable through ``trace_events``.

Accounting: ``non-blank lines == observations + non_evidence_units + failed_units``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..evidence import FMT_LOG, FileResult, Observation, ParseFailure, is_null
from ..timeutil import try_parse_ts
from .common import parse_kv, split_lines

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (.*)$")
_TRACE = re.compile(r"^(?:tr|rb)-[\w\-]+$")
_BRACKET = re.compile(r"^\[(?P<level>[A-Z]+)\] \[(?P<service>[^\]]+)\] \[(?P<tid>[^\]]+)\] (?P<event>[A-Za-z_]\w*):\s*(?P<kv>.*)$")
_PIPE = re.compile(r"^\|\s*(?P<level>[A-Z]+)\s*\|\s*(?P<service>[^|]+?)\s*\|\s*(?P<tid>[^|]+?)\s*\|\s*(?P<event>[A-Za-z_]\w*)\s*(?:\|\s*(?P<kv>.*))?$")
_SYSLOG = re.compile(r"^<(?P<pri>\d+)>1 (?P<host>\S+) (?P<service>\S+) (?P<tid>\S+) - - (?P<kv>.*)$")
_POSLEVEL = re.compile(r"^(?P<level>DEBUG|INFO|WARN|ERROR) (?P<kv>.*)$")


class LineError(Exception):
    pass


class _Line:
    __slots__ = ("n", "ts", "dialect", "fields", "raw", "warn")

    def __init__(self, n: int, ts: str, dialect: str, fields: Dict[str, Any], raw: str, warn: List[str]):
        self.n, self.ts, self.dialect, self.fields, self.raw, self.warn = n, ts, dialect, fields, raw, warn


def _kv_checked(kv_text: str, warn: List[str]) -> Dict[str, Any]:
    pairs, residue = parse_kv(kv_text)
    if residue:
        raise LineError(f"unparseable key/value residue: {residue[:60]!r}")
    dup = pairs.pop("__dup__", None)
    if dup:
        warn.append(f"duplicate keys (first value kept): {dup}")
    return dict(pairs)


def _trace_or_instance(tid: str, fields: Dict[str, Any]) -> None:
    if _TRACE.match(tid):
        fields.setdefault("trace_id", tid)
    else:
        fields.setdefault("instance", tid)


def parse_log_line(n: int, line: str) -> _Line:
    m = _LINE.match(line)
    if not m:
        raise LineError("line does not start with a UTC timestamp")
    ts, rest = m.group(1), m.group(2)
    if try_parse_ts(ts) is None:
        raise LineError(f"invalid timestamp {ts!r}")
    warn: List[str] = []
    if rest.startswith("{"):
        try:
            obj = json.loads(rest)
        except json.JSONDecodeError as exc:
            raise LineError(f"invalid JSON payload: {exc.msg}")
        if not isinstance(obj, dict):
            raise LineError("JSON payload is not an object")
        fields = dict(obj)
        if "severity" in fields:
            fields.setdefault("level", fields["severity"])
        return _Line(n, ts, "jsonl", fields, line, warn)
    if rest.startswith("requestId="):
        outer = _kv_checked(rest, warn)
        if "message" not in outer:
            raise LineError("requestId line without message=")
        inner = _kv_checked(outer["message"], warn)
        fields = dict(inner)
        for k in ("level", "function"):
            if k in outer:
                fields.setdefault(k, outer[k])
        fields.setdefault("service", outer.get("function", ""))
        _trace_or_instance(outer["requestId"], fields)
        fields["requestId"] = outer["requestId"]
        return _Line(n, ts, "requestid", fields, line, warn)
    if rest.startswith("["):
        mm = _BRACKET.match(rest)
        if not mm:
            raise LineError("malformed bracket-format line")
        fields = _kv_checked(mm["kv"], warn)
        fields.update(level=mm["level"], service=mm["service"], event=mm["event"])
        _trace_or_instance(mm["tid"], fields)
        return _Line(n, ts, "bracket", fields, line, warn)
    if rest.startswith("|"):
        mm = _PIPE.match(rest)
        if not mm:
            raise LineError("malformed pipe-format line")
        fields = _kv_checked(mm["kv"] or "", warn)
        fields.update(level=mm["level"], service=mm["service"], event=mm["event"])
        _trace_or_instance(mm["tid"], fields)
        return _Line(n, ts, "pipe", fields, line, warn)
    if rest.startswith("<"):
        mm = _SYSLOG.match(rest)
        if not mm:
            raise LineError("malformed syslog-format line")
        fields = _kv_checked(mm["kv"], warn)
        fields.update(service=mm["service"], pri=mm["pri"], host=mm["host"])
        _trace_or_instance(mm["tid"], fields)
        return _Line(n, ts, "syslog", fields, line, warn)
    mm = _POSLEVEL.match(rest)
    if mm:
        fields = _kv_checked(mm["kv"], warn)
        fields["level"] = mm["level"]
        return _Line(n, ts, "logfmt-poslevel", fields, line, warn)
    if rest.startswith("level="):
        return _Line(n, ts, "logfmt", _kv_checked(rest, warn), line, warn)
    raise LineError("unrecognised log dialect")


def _system_of(fields: Dict[str, Any]) -> Optional[str]:
    # record_lookup lines name the audited system in ``system=`` while ``service=``
    # is only the reporter; otherwise the emitting service is the best explicit label.
    for k in ("system", "service"):
        v = fields.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def parse_log_text(file_id: str, path: str, text: str, captured_at: Optional[str] = None) -> FileResult:
    res = FileResult(file_id, path, FMT_LOG, captured_at, unit_label="non-blank lines")
    parsed: List[_Line] = []
    for n, line in split_lines(text):
        if not line.strip():
            continue
        res.input_units += 1
        try:
            pl = parse_log_line(n, line)
        except LineError as exc:
            res.failures.append(ParseFailure(file_id, path, f"line:{n}", str(exc), line[:160]))
            res.failed_units += 1
            continue
        for w in pl.warn:
            res.warnings.append(ParseFailure(file_id, path, f"line:{n}", w, line[:160]))
        parsed.append(pl)

    by_trace: Dict[str, List[_Line]] = {}
    for pl in parsed:
        t = pl.fields.get("trace_id")
        if isinstance(t, str) and t:
            by_trace.setdefault(t, []).append(pl)

    dialects: Dict[str, int] = {}
    for pl in parsed:
        dialects[pl.dialect] = dialects.get(pl.dialect, 0) + 1
        rid = pl.fields.get("resource_id")
        if rid is None:
            res.non_evidence_units += 1
            continue
        if not isinstance(rid, str) or is_null(rid):
            res.warnings.append(ParseFailure(file_id, path, f"line:{pl.n}", f"empty/null resource_id {rid!r}; line kept as non-evidence", pl.raw[:160]))
            res.non_evidence_units += 1
            continue
        t = pl.fields.get("trace_id")
        siblings: List[Dict[str, Any]] = []
        if isinstance(t, str) and t:
            for other in by_trace.get(t, ()):
                if other is not pl:
                    siblings.append({"line": other.n, "ts": other.ts, **other.fields})
        res.observations.append(
            Observation(
                file_id=file_id, fmt=FMT_LOG, locator=f"line:{pl.n}", resource_id=rid,
                system_id=_system_of(pl.fields), collection=pl.fields.get("event"), copy=None,
                observed_at=pl.ts, fields=pl.fields, trace_events=siblings, raw=pl.raw,
            )
        )
    res.meta["dialects"] = dict(sorted(dialects.items()))
    res.meta["traces"] = len(by_trace)
    return res
