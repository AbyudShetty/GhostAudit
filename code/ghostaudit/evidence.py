"""Common evidence representation shared by every parser.

Parsers turn each source format into :class:`Observation` objects. An
observation is a *fact as stated by the source*: it preserves raw field values
(no coercion, no state inference). Identity resolution, policy decisions and
status classification live elsewhere and consume these records.

Conventions
-----------
* ``observed_at`` is the record's own stated timestamp, or ``None``. It is never
  filled in from the file's capture time (that lives in the manifest).
* ``system_id`` is whatever the source explicitly labels (``origin_system``,
  ``data_store``, ``system``, service name, ...), or ``None``. Use
  ``schema.system_for_resource`` for the prefix-derived owner.
* Values keep their source types. Log ``key=null`` stays the string ``"null"``;
  JSON/SQL NULL stays ``None``. :func:`is_null` treats both as empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .schema import RESOURCE_ID_RE

# Format labels used in Observation.fmt / FileResult.fmt
FMT_SQL, FMT_JSON, FMT_LOG, FMT_TXT, FMT_PDF, FMT_IMAGE = "sql", "json", "log", "txt", "pdf", "image"


@dataclass(frozen=True)
class ParseFailure:
    """A unit of input that could not be parsed. Always reported, never dropped."""

    file_id: str
    path: str
    locator: str  # e.g. "line:42", "stmt:7@line:90", "$.records[3]"
    reason: str
    snippet: str = ""

    def __str__(self) -> str:
        return f"{self.file_id} {self.path} {self.locator}: {self.reason}"


@dataclass
class Observation:
    file_id: str
    fmt: str
    locator: str
    resource_id: Optional[str]
    system_id: Optional[str] = None
    collection: Optional[str] = None  # collection / table / record_type / log event name
    copy: Optional[str] = None  # which copy of the data this is (SQL table, JSON container)
    observed_at: Optional[str] = None
    source_time: Optional[str] = None  # container-level stated time (exported_at, header, snapshot)
    fields: Dict[str, Any] = field(default_factory=dict)
    trace_events: List[Dict[str, Any]] = field(default_factory=list)  # sibling log lines of the same trace
    parent: Optional[str] = None  # locator of an enclosing record, for nested records
    raw: str = ""

    def resource_ids(self) -> Tuple[str, ...]:
        """Own resource id plus any ids mentioned by document-level observations (PDF)."""
        out: List[str] = []
        if self.resource_id:
            out.append(self.resource_id)
        for r in self.fields.get("resource_ids", ()) or ():
            if r not in out:
                out.append(r)
        return tuple(out)

    def identifiers(self) -> List["Identifier"]:
        return extract_identifiers(self)


@dataclass(frozen=True)
class ContainerInfo:
    """A collection/table/snapshot that was parsed, including *empty* ones.

    An empty container is evidence in its own right (e.g. a snapshot of
    ``customer_profiles`` with zero records), so it is preserved explicitly.
    """

    file_id: str
    kind: str  # "json_collection" | "sql_table"
    name: str
    locator: str
    record_count: int
    system_id: Optional[str] = None
    stated_time: Optional[str] = None
    details: Tuple[Tuple[str, Any], ...] = ()


@dataclass
class FileResult:
    file_id: str
    path: str
    fmt: str
    captured_at: Optional[str]  # from evidence_files.csv (manifest); parser does not infer it
    parsed: bool = True  # False for formats intentionally not parsed (images)
    unit_label: str = "units"
    input_units: int = 0  # source units seen (statements, lines, resource-bearing dicts, entries, pages)
    observations: List[Observation] = field(default_factory=list)
    failures: List[ParseFailure] = field(default_factory=list)
    # Recoverable oddities (duplicate keys, unparseable record timestamp): the record is kept, the issue is reported.
    warnings: List[ParseFailure] = field(default_factory=list)
    containers: List[ContainerInfo] = field(default_factory=list)
    non_evidence_units: int = 0  # units parsed fine but carrying no resource record (prose, noise, wrappers)
    failed_units: int = 0  # input units that failed to parse (each also has a ParseFailure in ``failures``)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return len(self.observations)

    def accounting_ok(self) -> bool:
        """Every input unit is either an observation, a counted non-evidence unit, or a failure.

        For formats where one unit yields one observation (SQL rows, log lines,
        TXT entries). JSON/PDF use their own accounting (see their parsers).
        """
        return self.input_units == len(self.observations) + self.non_evidence_units + self.failed_units


# --------------------------------------------------------------------------- identifier extraction

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
USER_RE = re.compile(r"(?<![A-Za-z0-9_])user_\d+(?![A-Za-z0-9_])")
HASH_RE = re.compile(r"(?<![0-9A-Za-z:])[0-9a-f]{64}(?![0-9A-Za-z])")
PHONE_RE = re.compile(r"\+\d[\d ()\-]{5,}\d")
_NULLS = {"", "null", "none", "n/a"}


def is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NULLS)


@dataclass(frozen=True)
class Identifier:
    kind: str  # email | user_ref | email_hash | phone | resource_id
    value: str  # as written in the source
    norm: str  # normalised for joining (lower-cased email/hash, digits-only phone)
    field: str  # where it was found ("fields.email", "trace[0].lookup_key", ...)


def _norm(kind: str, value: str) -> str:
    if kind == "phone":
        return "+" + re.sub(r"\D", "", value)
    if kind in ("email", "email_hash", "user_ref"):
        return value.lower()
    return value


def _walk(prefix: str, obj: Any) -> Iterator[Tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(f"{prefix}.{k}" if prefix else str(k), v)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(f"{prefix}[{i}]", v)
    elif isinstance(obj, str):
        yield prefix, obj


def _scan(text: str, where: str) -> Iterator[Identifier]:
    for m in EMAIL_RE.finditer(text):
        yield Identifier("email", m.group(0), _norm("email", m.group(0)), where)
    for m in USER_RE.finditer(text):
        yield Identifier("user_ref", m.group(0), _norm("user_ref", m.group(0)), where)
    for m in HASH_RE.finditer(text):
        yield Identifier("email_hash", m.group(0), _norm("email_hash", m.group(0)), where)
    for m in PHONE_RE.finditer(text):
        yield Identifier("phone", m.group(0), _norm("phone", m.group(0)), where)


def extract_identifiers(obs: Observation) -> List[Identifier]:
    """Identifiers literally present in an observation's values (fields + trace events).

    Pure text extraction: no matching against the identity tables happens here.
    Order is deterministic (source order, de-duplicated by (kind, norm, field)).
    """
    seen = set()
    out: List[Identifier] = []
    sources = [("fields", obs.fields)] + [(f"trace[{i}]", e) for i, e in enumerate(obs.trace_events)]
    for root, payload in sources:
        for path, text in _walk(root, payload):
            if path.endswith(".integrity_seal"):
                continue  # "sha256:<64 hex>" seals are not identifiers
            for ident in _scan(text, path):
                key = (ident.kind, ident.norm, ident.field)
                if key not in seen:
                    seen.add(key)
                    out.append(ident)
    return out


def mentioned_resource_ids(text: str) -> List[str]:
    """Resource ids literally mentioned in ``text`` (first-seen order, de-duplicated)."""
    out: List[str] = []
    for m in RESOURCE_ID_RE.finditer(text):
        rid = m.group(0)
        if rid not in out:
            out.append(rid)
    return out
