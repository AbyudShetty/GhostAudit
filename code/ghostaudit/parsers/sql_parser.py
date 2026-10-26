"""SQLite-compatible SQL snapshot exports -> Observations.

The ``.sql`` files are plain text. Each file is replayed statement by statement
into a throw-away in-memory SQLite database (the source file is never touched)
so that a bad statement is reported individually instead of aborting the file.

Topologies seen in the dataset:
* ``customer_data_primary`` + one or more replica tables, rows stored as
  ``record_json`` (read / regional / reporting / failover replicas);
* flat per-collection tables (``customer_profiles`` ...) plus ``snapshot_scope``
  (inventory snapshots);
* small hand-built tables (``customer_profiles_primary`` / ``_failover_replica``,
  ``search_documents_primary``).

Each row of a table that has a ``resource_id`` column becomes one Observation
whose ``copy`` is the table name; the replica/primary distinction is therefore
preserved for drift analysis. Tables without ``resource_id`` (``snapshot_scope``)
are preserved verbatim in ``FileResult.meta['tables_without_resource_id']``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..evidence import FMT_SQL, ContainerInfo, FileResult, Observation, ParseFailure
from ..timeutil import try_parse_ts
from .common import TIME_KEYS, first_present, split_lines

_HEADER = re.compile(r"^--\s*([A-Za-z ]+?):\s*(.+?)\s*$")
_TRAILER = re.compile(r"^--\s*(snapshot_completed_at)=(\S+)\s*$")
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+", re.I)


def _statements(text: str) -> List[Tuple[int, str]]:
    """Split into complete statements with the 1-based line each starts on."""
    out: List[Tuple[int, str]] = []
    buf: List[str] = []
    start = 0
    for lineno, line in split_lines(text):
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        if not buf:
            start = lineno
        buf.append(line)
        joined = "\n".join(buf)
        if sqlite3.complete_statement(joined):
            out.append((start, joined))
            buf = []
    if buf:
        out.append((start, "\n".join(buf)))  # incomplete trailing statement -> will fail to execute, reported
    return out


def parse_sql_text(file_id: str, path: str, text: str, captured_at: Optional[str] = None) -> FileResult:
    res = FileResult(file_id, path, FMT_SQL, captured_at, unit_label="INSERT statements")
    for _, line in split_lines(text):
        h = _HEADER.match(line) or _TRAILER.match(line)
        if h:
            res.meta[h.group(1).strip().lower().replace(" ", "_")] = h.group(2)
    topology = res.meta.get("database_topology")
    header_time = res.meta.get("captured_at") or res.meta.get("snapshot_completed_at")

    con = sqlite3.connect(":memory:")
    stmt_line_of_rowid: Dict[Tuple[str, int], int] = {}
    inserted: Dict[str, List[int]] = {}  # table -> statement start lines, in order
    try:
        for start, stmt in _statements(text):
            is_insert = bool(_INSERT.match(stmt))
            if is_insert:
                res.input_units += 1
            try:
                con.execute(stmt)
            except sqlite3.Error as exc:
                loc = f"stmt@line:{start}"
                res.failures.append(ParseFailure(file_id, path, loc, f"sqlite error: {exc}", stmt[:160]))
                res.failed_units += 1 if is_insert else 0
                continue
            if is_insert:
                m = re.match(r'^\s*INSERT\s+INTO\s+"?([A-Za-z_]\w*)"?', stmt, re.I)
                if m:
                    inserted.setdefault(m.group(1), []).append(start)
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        for table in tables:
            cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
            rows = con.execute(f'select rowid, * from "{table}" order by rowid').fetchall()
            lines = inserted.get(table, [])
            if "resource_id" not in cols:
                res.meta.setdefault("tables_without_resource_id", {})[table] = [dict(zip(cols, r[1:])) for r in rows]
                res.non_evidence_units += len(rows)
                res.containers.append(ContainerInfo(file_id, "sql_table", table, f"table:{table}", len(rows), None, header_time))
                continue
            res.containers.append(
                ContainerInfo(file_id, "sql_table", table, f"table:{table}", len(rows), None, header_time,
                              (("topology", topology),))
            )
            for idx, r in enumerate(rows):
                rowdict = dict(zip(cols, r[1:]))
                loc = f"table:{table}#{idx + 1}@line:{lines[idx]}" if idx < len(lines) else f"table:{table}#{idx + 1}"
                fields: Dict[str, Any] = dict(rowdict)
                collection = table
                system = rowdict.get("origin_system")
                raw_json = fields.pop("record_json", None)
                if raw_json is not None:
                    try:
                        fields["record"] = json.loads(raw_json)
                    except json.JSONDecodeError as exc:
                        res.failures.append(ParseFailure(file_id, path, loc, f"record_json invalid: {exc}", str(raw_json)[:160]))
                        res.failed_units += 1
                        continue
                    collection = rowdict.get("collection_name") or table
                    if not isinstance(fields["record"], dict):
                        res.failures.append(ParseFailure(file_id, path, loc, "record_json is not an object", str(raw_json)[:160]))
                        res.failed_units += 1
                        continue
                stated = first_present(rowdict, TIME_KEYS)
                if stated and try_parse_ts(stated) is None:
                    res.warnings.append(ParseFailure(file_id, path, loc, f"unparseable timestamp {stated!r}", ""))
                res.observations.append(
                    Observation(
                        file_id=file_id, fmt=FMT_SQL, locator=loc, resource_id=str(rowdict["resource_id"]),
                        system_id=system, collection=collection, copy=table, observed_at=stated,
                        source_time=header_time, fields=fields,
                        raw=json.dumps(rowdict, ensure_ascii=False, sort_keys=True, default=str),
                    )
                )
    finally:
        con.close()
    return res
