"""Read-only loading and structural validation of the participant-facing CSV files.

Files are opened explicitly as UTF-8 (BOM tolerated, ``newline=''`` so embedded
CR/LF inside quoted fields survive) -- never with the platform default
encoding, which on Windows is cp1252 and corrupts the multilingual request text.
Nothing here writes to ``dataset/``.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Type, TypeVar

from .schema import case_sort_key
from .timeutil import try_parse_ts

T = TypeVar("T")

REQUEST_FIELDS = ("case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text")
REPORTED_STATUSES = ("COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED")


@dataclass(frozen=True)
class DeletionRequest:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditLogEntry:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: str


@dataclass(frozen=True)
class DatasetIssue:
    file: str
    row: int
    reason: str

    def __str__(self) -> str:
        return f"{self.file} row {self.row}: {self.reason}"


@dataclass
class Table:
    name: str
    rows: List = field(default_factory=list)
    issues: List[DatasetIssue] = field(default_factory=list)


def read_csv_dicts(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    return header, rows


def _load(path: Path, cls: Type[T], ts_fields: Sequence[str], id_field: str) -> Table:
    expected = [f for f in cls.__dataclass_fields__]  # type: ignore[attr-defined]
    # evidence_files.csv uses ``file_path``; all other CSV headers equal the dataclass fields
    header, rows = read_csv_dicts(path)
    t = Table(path.name)
    if header != expected:
        t.issues.append(DatasetIssue(path.name, 0, f"header {header} != expected {expected}"))
        return t
    seen: Dict[str, int] = {}
    for i, r in enumerate(rows, start=2):  # row 1 is the header
        if None in r or any(v is None for v in r.values()):
            t.issues.append(DatasetIssue(path.name, i, "row has missing or extra columns"))
            continue
        for f in ts_fields:
            if try_parse_ts(r[f]) is None:
                t.issues.append(DatasetIssue(path.name, i, f"unparseable timestamp in {f}: {r[f]!r}"))
        key = r[id_field]
        if key in seen:
            t.issues.append(DatasetIssue(path.name, i, f"duplicate {id_field} {key!r} (first at row {seen[key]})"))
        else:
            seen[key] = i
        t.rows.append(cls(**r))  # type: ignore[call-arg]
    return t


@dataclass
class Dataset:
    root: Path
    requests: Table
    sample_requests: Table
    identities: Table
    audit_log: Table
    activity: Table
    rules: Table
    evidence_files: Table
    sample_output: List[Dict[str, str]]

    def tables(self) -> List[Table]:
        return [self.requests, self.sample_requests, self.identities, self.audit_log, self.activity, self.rules, self.evidence_files]

    def issues(self) -> List[DatasetIssue]:
        out: List[DatasetIssue] = []
        for t in self.tables():
            out.extend(t.issues)
        return out

    def evidence_by_id(self) -> Dict[str, EvidenceFile]:
        return {f.file_id: f for f in self.evidence_files.rows}


REQUIRED_FILES = ("deletion_requests.csv", "sample_deletion_requests.csv", "user_identities.csv", "user_audit_log.csv",
                  "system_activity.csv", "deletion_rules.csv", "evidence_files.csv")
# sample_output.csv holds the supplied sample answers. Predictions never read it; it is loaded when present for the local scorer and tests.


class DatasetNotFound(FileNotFoundError):
    """The dataset directory is missing or incomplete; the message says which path was tried and how it was resolved."""


def check_dataset_dir(root: os.PathLike) -> Path:
    root = Path(root)
    absolute = Path(os.path.abspath(root))
    if not root.is_dir():
        raise DatasetNotFound(f"dataset directory not found: {absolute} (a relative path is resolved against the current directory, {os.getcwd()})")
    missing = [n for n in REQUIRED_FILES if not (root / n).is_file()]
    if missing:
        raise DatasetNotFound(f"{absolute} is not a complete dataset directory; missing: {', '.join(missing)}")
    return root


def load_dataset(root: os.PathLike) -> Dataset:
    root = check_dataset_dir(root)
    ds = Dataset(
        root=root,
        requests=_load(root / "deletion_requests.csv", DeletionRequest, ("requested_at", "reported_at", "audit_at"), "case_id"),
        sample_requests=_load(root / "sample_deletion_requests.csv", DeletionRequest, ("requested_at", "reported_at", "audit_at"), "case_id"),
        identities=_load(root / "user_identities.csv", Identity, ("updated_at",), "user_id"),
        audit_log=_load(root / "user_audit_log.csv", AuditLogEntry, ("event_time",), "audit_id"),
        activity=_load(root / "system_activity.csv", Activity, ("event_time",), "activity_id"),
        rules=_load(root / "deletion_rules.csv", Rule, (), "rule_id"),
        evidence_files=_load(root / "evidence_files.csv", EvidenceFile, ("captured_at",), "file_id"),
        sample_output=read_csv_dicts(root / "sample_output.csv")[1] if (root / "sample_output.csv").is_file() else [],
    )
    for t in (ds.requests, ds.sample_requests):
        for i, r in enumerate(t.rows, start=2):
            if r.reported_status not in REPORTED_STATUSES:
                t.issues.append(DatasetIssue(t.name, i, f"unknown reported_status {r.reported_status!r}"))
            if not (try_parse_ts(r.requested_at) and try_parse_ts(r.reported_at) and try_parse_ts(r.audit_at)):
                continue
            if not (try_parse_ts(r.requested_at) <= try_parse_ts(r.reported_at) <= try_parse_ts(r.audit_at)):
                t.issues.append(DatasetIssue(t.name, i, "timestamps not ordered requested_at <= reported_at <= audit_at"))
    return ds


def sorted_case_ids(table: Table) -> List[str]:
    return sorted((r.case_id for r in table.rows), key=case_sort_key)


def evidence_disk_check(ds: Dataset) -> Tuple[List[str], List[str]]:
    """Return ``(manifest paths missing on disk, files on disk absent from manifest)`` (POSIX-style relative paths)."""
    ev_root = ds.root / "evidence"
    on_disk = {p.relative_to(ds.root).as_posix() for p in ev_root.rglob("*") if p.is_file()} if ev_root.exists() else set()
    listed = {f.file_path for f in ds.evidence_files.rows}
    return sorted(listed - on_disk), sorted(on_disk - listed)
