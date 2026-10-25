"""Steps: turn an M4 verdict into schema-valid ``steps_json`` rows, emitting only what the evidence justifies.

Three things are kept apart (they are different claims):

* **status**         - COMPLETE / INCOMPLETE / NOT_APPLICABLE (M4), possibly with ``determination=PARTIAL``;
* **findings**       - violations we can substantiate with a usable, cited record (INCOMPLETE only);
* **verification**   - in a COMPLETE case a *proof* that must cite the evidence of the current compliant state; in an
                       INCOMPLETE case a *future check* (no evidence) of the state remediation must reach.

Emission rules (AGENTS.md 6.3 / 6.6, problem_statement "Rules")
-------------------------------------------------------------
NOT_APPLICABLE  ``[]``.
INCOMPLETE      findings (cited) for every counted violation; one remediation step per finding (operation chosen by the
                required treatment); verification (empty ``evidence_id``) for each remediated item, for the subject's account
                when it is identified, and for each retained record that must remain available, including one shown missing (it
                gets no finding and no remediation, because the vocabulary has no operation for either, but its check is still owed). Verification of
                already-compliant resources that need no remediation is NOT added: the specification asks verification to
                cover "each remediated record or process" and no sample answer contains such an item.
COMPLETE        verification steps only, each citing one usable evidence id. A requirement becomes a step only if it is
                PROVEN, names a resource, has usable evidence, and is *direct proof*. A proof can rest on up to three kinds
                of inference (``Requirement.inferences``): an account identity attributed from activity, absence read from a
                subject-scoped snapshot, or state read from document/OCR text. Each kind has its own switch in ``PlanPolicy``
                (inferred identity on by default, the other two off), and a proof is emitted only when *every* inference it rests
                on is allowed (``LATER_OWNERSHIP`` never is): a scoped-snapshot
                proof for an activity-attributed account needs both switches, so a direct state reading never stands in for an
                uncertain owner. Set-wise proofs name no resource and can never become a step. A COMPLETE case with
                ``determination=PARTIAL`` therefore carries only the steps that are genuinely proven (possibly none).
Unresolved accounts are never named. Ordering is canonical (``schema.step_sort_key``: phase, operation table order,
``system_id``, ``resource_id``), which also satisfies "disable flows and block restores before changing records".

``CasePlan.withheld`` lists every step that was *not* emitted and why, so nothing is dropped silently.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .classification import (COMPLETE, DIRECT, DOCUMENT_OR_OCR, INCOMPLETE, INFERRED_IDENTITY, NOT_APPLICABLE, SCOPED_SNAPSHOT, CaseVerdict, Requirement,
                             Violation)
from .dataset import EvidenceFile
from .schema import (OPERATION_STATE, OUTPUT_COLUMNS, REMEDIATE_FOR_ACTION, VERIFY_FOR_ACTION, case_sort_key, step_sort_key, system_for_resource)
from .timeutil import parse_ts


@dataclass(frozen=True)
class PlanPolicy:
    # COMPLETE proofs that rest on an inference; each switch allows one kind, and a proof needs all of its kinds allowed
    emit_inferred_identity_proofs: bool = True  # the account is the subject's only by deletion-worker activity (unique attribution at audit_at)
    emit_scoped_snapshot_proofs: bool = False  # "no profile" read from a snapshot that lists the subject's other records but not a profile
    emit_document_or_ocr_proofs: bool = False  # the state is read from PDF text or screenshot OCR
    include_inferred_account_checks: bool = True  # INCOMPLETE: future "account absent" check for an activity-attributed account

    @classmethod
    def all_inferred(cls, **kw) -> "PlanPolicy":
        """Every inferred proof allowed (the ``--emit-inferred-proofs`` ablation)."""
        return cls(emit_inferred_identity_proofs=True, emit_scoped_snapshot_proofs=True, emit_document_or_ocr_proofs=True, **kw)

    def blocked(self, inferences: Iterable[str]) -> Tuple[str, ...]:
        """The inferences in ``inferences`` that this policy does not allow (empty: the proof may become a step)."""
        allowed = {INFERRED_IDENTITY: self.emit_inferred_identity_proofs, SCOPED_SNAPSHOT: self.emit_scoped_snapshot_proofs,
                   DOCUMENT_OR_OCR: self.emit_document_or_ocr_proofs}
        return tuple(k for k in inferences if not allowed.get(k, False))


@dataclass(frozen=True)
class Withheld:
    phase: str
    resource_id: Optional[str]
    operation: str
    reason: str
    would_cite: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CasePlan:
    case_id: str
    status: str
    determination: str
    steps: Tuple[Dict[str, object], ...]  # numbered, ordered, schema-shaped
    withheld: Tuple[Withheld, ...] = ()

    def steps_json(self) -> str:
        return json.dumps(list(self.steps), ensure_ascii=False, separators=(",", ":"))


def choose_citation(evidence_ids: Iterable[str], manifest: Mapping[str, EvidenceFile]) -> str:
    """One id for a step: the most recently captured usable file, ties by file id (deterministic)."""
    ids = sorted(set(evidence_ids))
    if not ids:
        return ""
    return max(ids, key=lambda i: (parse_ts(manifest[i].captured_at) if i in manifest else parse_ts("1970-01-01T00:00:00Z"), _num(i), i))


def _num(file_id: str) -> int:
    digits = "".join(c for c in file_id if c.isdigit())
    return -int(digits) if digits else 0  # on equal capture time prefer the lower id


def _step(phase: str, resource_id: str, op: str, evidence_id: str) -> Dict[str, object]:
    return {"step_no": 0, "phase": phase, "system_id": system_for_resource(resource_id) or "", "resource_id": resource_id,
            "operation": op, "state": OPERATION_STATE[op], "evidence_id": evidence_id}


def _finish(raw: List[Dict[str, object]]) -> Tuple[Dict[str, object], ...]:
    seen, out = set(), []
    for s in sorted(raw, key=step_sort_key):
        key = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    for i, s in enumerate(out, start=1):
        s["step_no"] = i
    return tuple(out)


def build_plan(verdict: CaseVerdict, manifest: Mapping[str, EvidenceFile], policy: PlanPolicy = PlanPolicy()) -> CasePlan:
    cid, withheld = verdict.case_id, []
    if verdict.status == NOT_APPLICABLE:
        return CasePlan(cid, verdict.status, verdict.determination, ())
    raw: List[Dict[str, object]] = []
    if verdict.status == INCOMPLETE:
        _incomplete(verdict, manifest, policy, raw, withheld)
    else:
        _complete(verdict, manifest, policy, raw, withheld)
    return CasePlan(cid, verdict.status, verdict.determination, _finish(raw), tuple(withheld))


# ------------------------------------------------------------------------------------------------ INCOMPLETE

def _incomplete(v: CaseVerdict, manifest, policy: PlanPolicy, raw: List[Dict[str, object]], withheld: List[Withheld]) -> None:
    remediated: Dict[str, str] = {}
    for x in v.counted_violations:
        if x.finding_op is None:
            withheld.append(Withheld("FINDING", x.resource_id, "(retained record missing)", "the output vocabulary has no operation for a missing retained record"))
            if x.required_action == "RETAIN":
                # no finding or recovery operation exists for it, but the check that must pass afterwards does (AGENTS.md 6.3)
                raw.append(_step("VERIFICATION", x.resource_id, VERIFY_FOR_ACTION["RETAIN"], ""))
            continue
        cite = choose_citation(x.evidence_ids, manifest)
        if not cite:
            withheld.append(Withheld("FINDING", x.resource_id, x.finding_op, "no usable evidence id to cite"))
            continue
        rem = REMEDIATE_FOR_ACTION.get(x.required_action)
        ver = VERIFY_FOR_ACTION.get(x.required_action)
        if rem is None or ver is None:
            withheld.append(Withheld("FINDING", x.resource_id, x.finding_op, f"no remediation defined for required action {x.required_action}"))
            continue
        raw.append(_step("FINDING", x.resource_id, x.finding_op, cite))
        raw.append(_step("REMEDIATION", x.resource_id, rem, ""))
        raw.append(_step("VERIFICATION", x.resource_id, ver, ""))
        remediated[x.resource_id] = ver
    for r in v.requirements:
        if not r.resource_id or r.resource_id in remediated or r.key == "RETAINED_EXPIRED":
            continue
        if r.key == "ACCOUNT_ABSENT":
            if INFERRED_IDENTITY in (r.inferences or (r.kind,)) and not policy.include_inferred_account_checks:
                withheld.append(Withheld("VERIFICATION", r.resource_id, r.expected_op, "account identity is inferred from activity and the policy excludes such checks"))
                continue
            raw.append(_step("VERIFICATION", r.resource_id, "VERIFY_RECORD_ABSENT", ""))
        elif r.key == "RETAINED_PRESENT":
            raw.append(_step("VERIFICATION", r.resource_id, "VERIFY_RECORD_PRESENT", ""))
    _note_unresolved_accounts(v, withheld)


# -------------------------------------------------------------------------------------------------- COMPLETE

def _complete(v: CaseVerdict, manifest, policy: PlanPolicy, raw: List[Dict[str, object]], withheld: List[Withheld]) -> None:
    for r in v.requirements:
        if r.key == "RETAINED_EXPIRED":
            continue
        if r.resource_id is None:
            g = next((g for g in v.gaps if g.code in ("ACCOUNT_AMBIGUOUS", "ACCOUNT_UNATTRIBUTED")), None)
            why = f"{g.code} ({r.kind}): {g.message}" if g else f"names no resource ({r.kind})"
            withheld.append(Withheld("VERIFICATION", None, r.expected_op, f"{why}; an unresolved account is never guessed, so no account step is produced", r.evidence_ids))
            continue
        if r.status != "PROVEN":
            withheld.append(Withheld("VERIFICATION", r.resource_id, r.expected_op, f"unproven: {r.reason}"))
            continue
        cite = choose_citation(r.evidence_ids, manifest)
        if not cite:
            withheld.append(Withheld("VERIFICATION", r.resource_id, r.expected_op, "no usable evidence id to cite"))
            continue
        blocked = policy.blocked(r.inferences or ((r.kind,) if r.kind != DIRECT else ()))  # fail closed if only ``kind`` says it is indirect
        if blocked:
            withheld.append(Withheld("VERIFICATION", r.resource_id, r.expected_op, f"proof rests on {'+'.join(blocked)}, not direct evidence", r.evidence_ids))
            continue
        raw.append(_step("VERIFICATION", r.resource_id, r.expected_op, cite))
    _note_unresolved_accounts(v, withheld)


def _note_unresolved_accounts(v: CaseVerdict, withheld: List[Withheld]) -> None:
    for g in v.gaps:
        if g.code in ("ACCOUNT_AMBIGUOUS", "ACCOUNT_UNATTRIBUTED") and not any(w.resource_id is None and w.operation == "VERIFY_RECORD_ABSENT" for w in withheld):
            withheld.append(Withheld("VERIFICATION", None, "VERIFY_RECORD_ABSENT", f"{g.code}: no account step is produced; {g.message}"))
            break


# ------------------------------------------------------------------------------------------------ rows / CSV

def plan_all(verdicts: Mapping[str, CaseVerdict], manifest: Mapping[str, EvidenceFile], policy: PlanPolicy = PlanPolicy()) -> Dict[str, CasePlan]:
    return {cid: build_plan(v, manifest, policy) for cid, v in verdicts.items()}


def rows_for(plans: Mapping[str, CasePlan], case_ids: Sequence[str]) -> List[Dict[str, str]]:
    return [{"case_id": c, "deletion_status": plans[c].status, "steps_json": plans[c].steps_json()} for c in sorted(case_ids, key=case_sort_key)]


def rows_to_csv(rows: Sequence[Mapping[str, str]]) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(OUTPUT_COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in OUTPUT_COLUMNS])
    return buf.getvalue()
