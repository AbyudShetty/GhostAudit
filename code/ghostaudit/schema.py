"""Output/input schema constants and validation.

Single home for: phases, operations (with their phase/state and canonical
order), resource-id prefixes, case ordering and ``output.csv`` validation.
Everything downstream imports these tables instead of re-declaring them.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------- phases / operations

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
OUTPUT_COLUMNS = ("case_id", "deletion_status", "steps_json")

# (phase, operation, state) in the exact order of docs/challenge/problem_statement.md "Allowed Operations".
_OPERATION_TABLE = (
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)
OPERATION_PHASE: Dict[str, str] = {op: ph for ph, op, _ in _OPERATION_TABLE}
OPERATION_STATE: Dict[str, str] = {op: st for _, op, st in _OPERATION_TABLE}
OPERATION_ORDER: Dict[str, int] = {op: i for i, (_, op, _) in enumerate(_OPERATION_TABLE)}
PHASE_ORDER: Dict[str, int] = {p: i for i, p in enumerate(PHASES)}

# Required treatment (deletion_rules.csv vocabulary) -> the verification operation that proves it was applied.
VERIFY_FOR_ACTION: Dict[str, str] = {
    "DELETE": "VERIFY_RECORD_ABSENT", "REDACT": "VERIFY_RECORD_REDACTED", "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED",
    "RETAIN": "VERIFY_RECORD_PRESENT", "DISABLE": "VERIFY_DATA_FLOW_DISABLED", "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED",
}

# Required treatment -> the remediation operation that applies it.
REMEDIATE_FOR_ACTION: Dict[str, str] = {
    "DELETE": "DELETE_RECORD", "REDACT": "REDACT_RECORD", "ANONYMIZE": "ANONYMIZE_RECORD",
    "DISABLE": "DISABLE_DATA_FLOW", "BLOCK_RESTORE": "BLOCK_RESTORE",
}

# --------------------------------------------------------------------------- resource ids

# Prefix of a resource id -> the system that owns it. Derived from activity
# target_system values and the handover/catalog evidence; verified by tests.
RESOURCE_PREFIX_SYSTEM: Dict[str, str] = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "export": "export_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "marketing": "marketing_platform",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "legal_hold": "legal_service",
    "object": "object_storage",
}
RESOURCE_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(sorted(RESOURCE_PREFIX_SYSTEM, key=len, reverse=True)) + r")_(\d+)(?![A-Za-z0-9_])"
)
_RID_FULL = re.compile(r"^([a-z_]+?)_(\d+)$")


def resource_prefix(resource_id: str) -> Optional[str]:
    m = _RID_FULL.match(resource_id or "")
    return m.group(1) if m and m.group(1) in RESOURCE_PREFIX_SYSTEM else None


def system_for_resource(resource_id: str) -> Optional[str]:
    """System that owns ``resource_id`` by prefix, or ``None`` for unknown prefixes."""
    p = resource_prefix(resource_id)
    return RESOURCE_PREFIX_SYSTEM[p] if p else None


# --------------------------------------------------------------------------- ordering

_CASE_RE = re.compile(r"^case_(\d+)$")


# How output.csv rows are ordered. The contract only says "sorted by case_id" and never says how, so this is a decision, not a fact:
#   "numeric"       case_33 before case_100: the order of dataset/deletion_requests.csv (and of every other organizer table whose ids mix widths)
#   "lexicographic" plain text order: case_100 before case_33
# The evidence for each reading is in docs/design_decisions.md ("Row order"). Everything that writes, validates or scores rows goes
# through case_sort_key, so this one constant is the only thing to change (tests/test_row_order.py then needs its pinned expectations updated).
CASE_ORDER = "numeric"


def case_sort_key(case_id: str):
    """Sort key for case ids under CASE_ORDER. Unknown shapes sort last, by string, in numeric mode."""
    if CASE_ORDER == "lexicographic":
        return case_id
    m = _CASE_RE.match(case_id)
    return (0, int(m.group(1)), case_id) if m else (1, 0, case_id)


def step_sort_key(step: Dict[str, Any]):
    """Canonical order of a whole step list: phase, operation, system_id, resource_id."""
    return (
        PHASE_ORDER[step["phase"]],
        OPERATION_ORDER[step["operation"]],
        step["system_id"],
        step["resource_id"],
    )


# --------------------------------------------------------------------------- validation

@dataclass(frozen=True)
class Violation:
    case_id: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.case_id}: [{self.code}] {self.message}"


def validate_steps(
    case_id: str,
    status: str,
    steps: Sequence[Dict[str, Any]],
    valid_evidence_ids: Optional[Iterable[str]] = None,
    check_order: bool = True,
    allow_empty_deletion_steps: bool = False,
) -> List[Violation]:
    """Structural validation of one case's steps against the output contract.

    Returns violations (empty list means valid). Checks: field set, enums,
    phase/operation/state consistency, consecutive step_no, canonical order,
    and the evidence-id convention per status. Semantic correctness (are these
    the *right* steps?) is out of scope here.
    """
    v: List[Violation] = []

    def add(code: str, msg: str) -> None:
        v.append(Violation(case_id, code, msg))

    if status not in STATUSES:
        add("status", f"unknown deletion_status {status!r}")
        return v
    if status == "NOT_APPLICABLE":
        if steps:
            add("not_applicable_steps", "NOT_APPLICABLE must have an empty steps array")
        return v
    if not steps:
        # The spec only forbids *unsupported* steps; a COMPLETE case with nothing provable may legitimately have none, so callers
        # that build predictions pass allow_empty_deletion_steps=True and report such cases separately.
        if not allow_empty_deletion_steps:
            add("empty_steps", f"{status} case must have steps")
        return v
    known = set(valid_evidence_ids) if valid_evidence_ids is not None else None
    structural_error = False
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict) or set(s) != set(STEP_FIELDS):
            got = sorted(s) if isinstance(s, dict) else type(s).__name__
            add("fields", f"step {i}: fields must be exactly {STEP_FIELDS}, got {got}")
            structural_error = True
            continue
        if isinstance(s["step_no"], bool) or s["step_no"] != i:
            add("step_no", f"step {i}: step_no is {s['step_no']!r}, expected {i}")
        if s["phase"] not in PHASE_ORDER:
            add("phase", f"step {i}: bad phase {s['phase']!r}")
            structural_error = True
        if s["operation"] not in OPERATION_PHASE:
            add("operation", f"step {i}: bad operation {s['operation']!r}")
            structural_error = True
            continue
        if OPERATION_PHASE[s["operation"]] != s["phase"]:
            add("operation_phase", f"step {i}: {s['operation']} belongs to {OPERATION_PHASE[s['operation']]}, not {s['phase']}")
        if OPERATION_STATE[s["operation"]] != s["state"]:
            add("operation_state", f"step {i}: {s['operation']} requires state {OPERATION_STATE[s['operation']]}, got {s['state']!r}")
        for f in ("system_id", "resource_id"):
            if not isinstance(s[f], str) or not s[f]:
                add(f, f"step {i}: {f} must be a non-empty string")
                structural_error = True
        if not isinstance(s["evidence_id"], str):
            add("evidence_id", f"step {i}: evidence_id must be a string (use '' when not applicable)")
            continue
        eid = s["evidence_id"]
        needs_evidence = s["phase"] == "FINDING" or status == "COMPLETE"
        if needs_evidence and not eid:
            add("evidence_required", f"step {i}: {s['phase']} step in a {status} case must cite evidence")
        if not needs_evidence and eid:
            add("evidence_forbidden", f"step {i}: future {s['phase']} step must use empty evidence_id")
        if eid and known is not None and eid not in known:
            add("evidence_unknown", f"step {i}: evidence_id {eid!r} is not a known file/activity/audit id")
    if status == "COMPLETE" and any(isinstance(s, dict) and s.get("phase") != "VERIFICATION" for s in steps):
        add("complete_phases", "COMPLETE cases may contain only VERIFICATION steps")
    if check_order and not structural_error:
        keys = [step_sort_key(s) for s in steps]
        if keys != sorted(keys):
            add("order", "steps are not in canonical order (phase, operation table order, system_id, resource_id)")
    return v


def parse_output_csv(text: str) -> List[Dict[str, str]]:
    """Parse ``output.csv`` text into row dicts (header must match exactly)."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != OUTPUT_COLUMNS:
        raise ValueError(f"output header must be {OUTPUT_COLUMNS}, got {reader.fieldnames}")
    return list(reader)


def validate_output_rows(
    rows: Sequence[Dict[str, str]],
    expected_case_ids: Optional[Sequence[str]] = None,
    valid_evidence_ids: Optional[Iterable[str]] = None,
    check_order: bool = True,
    allow_empty_deletion_steps: bool = False,
) -> List[Violation]:
    """Validate a whole ``output.csv``: coverage, uniqueness, sort order and every row's steps."""
    v: List[Violation] = []
    ids = [r["case_id"] for r in rows]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1}, key=case_sort_key)
        v.append(Violation("*", "duplicate_case", f"duplicate case_ids: {dup[:5]}"))
    if expected_case_ids is not None:
        missing = sorted(set(expected_case_ids) - set(ids), key=case_sort_key)
        extra = sorted(set(ids) - set(expected_case_ids), key=case_sort_key)
        if missing:
            v.append(Violation("*", "missing_case", f"{len(missing)} missing, e.g. {missing[:5]}"))
        if extra:
            v.append(Violation("*", "extra_case", f"{len(extra)} unexpected, e.g. {extra[:5]}"))
    if ids != sorted(ids, key=case_sort_key):
        v.append(Violation("*", "row_order", f"rows are not in {CASE_ORDER} case_id order"))
    evidence = list(valid_evidence_ids) if valid_evidence_ids is not None else None
    for r in rows:
        try:
            steps = json.loads(r["steps_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            v.append(Violation(r["case_id"], "json", f"steps_json is not valid JSON: {exc}"))
            continue
        if not isinstance(steps, list):
            v.append(Violation(r["case_id"], "json", "steps_json must be a JSON array"))
            continue
        v.extend(validate_steps(r["case_id"], r["deletion_status"], steps, evidence, check_order, allow_empty_deletion_steps))
    return v
