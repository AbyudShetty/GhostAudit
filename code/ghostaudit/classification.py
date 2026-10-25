"""Deletion status: ``COMPLETE`` / ``INCOMPLETE`` / ``NOT_APPLICABLE``, with the reasoning and evidence behind it.

Specification (AGENTS.md 6.5, 6.6) - verified wording
  * ``NOT_APPLICABLE`` when the request text does not authorise deletion.
  * "A deletion is ``COMPLETE`` only when every relevant copy has the required treatment, every required retained record
    remains available, and no active sync, import, or restore path can recreate prohibited data."
  * Otherwise ``INCOMPLETE``: report every relevant policy violation and recreation risk supported by visible evidence.
  * A verification step in a ``COMPLETE`` case "must cite the evidence that proves the current compliant state".

Consequences implemented here
  * ``reported_status`` and ``reported_result`` are claims and never decide anything. (``reported_status`` is used only
    upstream, as a *matching key* between two claims of one workflow, when choosing among candidate accounts.)
  * COMPLETE is not "no violation found". It needs an explicit, resource-scoped, eligible record for every requirement
    (account absent, each copy treated, retained records present, flows disabled, restores blocked). A record that merely
    does not list the resource is not such a record (measured: 7 of 9 accounts shown present by other evidence are missing
    from the nearest profile snapshot, so non-listing proves nothing). ``state.interpret`` therefore only ever yields
    ``ABSENT`` from a record that names the resource.
  * Audit-time strictness: only state readings usable at ``audit_at`` are used, and only they are citable. Identity
    from later evidence leaves the state ``UNKNOWN`` and the requirement unproven.
  * Evidence confidence: HIGH = structured record/log; MEDIUM = PDF text, screenshot OCR confirmed by readable content, or a
    link that rests on activity-attributed account; LOW = screenshot tied only by the catalog time offset, or a conflicting
    screenshot. LOW never counts as proof or as a violation (it becomes a blocking gap).
  * Uncertainty is preserved: ambiguous accounts, ownership conflicts, uncertain relevance and unreadable evidence become
    ``Gap`` objects. A blocking gap stops the verdict being PROVEN; it never turns into a guessed resource id.

Determination (how well the status is supported)
  INCOMPLETE: ``PROVEN`` (>=1 HIGH-confidence violation) | ``SUPPORTED`` (only MEDIUM violations)
  COMPLETE:   ``PROVEN`` (every requirement proven at HIGH) | ``INFERRED`` (all proven, some via activity-attributed account
              or PDF/OCR) | ``PARTIAL`` (no violation found, but >=1 requirement unproven -> status taken from the policy)
  NOT_APPLICABLE: ``TEXT``

The one genuine policy choice is the status of a ``PARTIAL`` case (``ClassificationPolicy.unproven_status``); see
``code/docs/m4_classification.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .absence import ScopedAbsence, SnapshotCatalog, find_scoped_absence
from .attribution import AMBIGUOUS, CONTENT, NONE, STRONG
from .dataset import DeletionRequest
from .intent import DELETION, NON_DELETION, UNCERTAIN, IntentResult, classify_intent
from .reconstruction import CaseReconstruction, CaseReconstructor, ResourceDecision
from .scenario import RELEVANT, UNCERTAIN as REL_UNCERTAIN, UNRELATED
from .schema import VERIFY_FOR_ACTION
from .state import StateEvidence, StateReconstructor
from .timeutil import parse_ts, try_parse_ts

COMPLETE, INCOMPLETE, NOT_APPLICABLE = "COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"
# What a proof rests on. Only DIRECT may become a COMPLETE verification step by default (see ghostaudit/plan.py).
DIRECT, INFERRED_IDENTITY, SCOPED_SNAPSHOT, DOCUMENT_OR_OCR, SET_WISE = "direct", "inferred_identity", "scoped_snapshot", "document_or_ocr", "set_wise"
# An activity attribution that holds only because a file captured after audit_at ruled out a rival account. No plan switch allows it, so a
# proof that rests on it is never emitted: an inferred identity may use only evidence available at audit_at.
LATER_OWNERSHIP = "later_ownership"
HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}


@dataclass(frozen=True)
class ClassificationPolicy:
    name: str = "default"
    unproven_status: str = COMPLETE  # status when no violation exists but a requirement cannot be proven
    uncertain_intent_status: str = NOT_APPLICABLE  # UNCERTAIN intent never authorises deletion
    accept_inferred_attribution: bool = True  # an activity-attributed (STRONG) account may be proven by an explicit negative record
    accept_medium_violations: bool = True  # PDF- or OCR-derived violations count towards INCOMPLETE
    accept_scoped_snapshot_absence: bool = True  # a subject-enumerating snapshot with no profile supports "no account" (MEDIUM, INFERRED)

    def __post_init__(self):
        if self.unproven_status not in (COMPLETE, INCOMPLETE):
            raise ValueError("unproven_status must be COMPLETE or INCOMPLETE")
        if self.uncertain_intent_status not in (NOT_APPLICABLE, COMPLETE, INCOMPLETE):
            raise ValueError("bad uncertain_intent_status")


@dataclass(frozen=True)
class Violation:
    resource_id: str
    system_id: Optional[str]
    category: str
    finding_op: Optional[str]  # REPORT_*; None for a missing retained record (no operation exists for it)
    required_action: str
    evidence_ids: Tuple[str, ...]
    confidence: str
    basis: str
    caveats: Tuple[str, ...] = ()
    inferred_identity: bool = False  # the resource is attributed to the subject from activity, not from an identifier


@dataclass(frozen=True)
class Requirement:
    key: str  # ACCOUNT_ABSENT | RESOURCE_TREATED | RETAINED_PRESENT
    resource_id: Optional[str]  # None for a set-wise account proof (several candidate accounts, all absent)
    category: str
    expected_op: str  # the VERIFY_* operation this proves
    status: str  # PROVEN | UNPROVEN
    confidence: str  # HIGH | MEDIUM | LOW (LOW when UNPROVEN)
    evidence_ids: Tuple[str, ...]
    basis: str
    reason: str
    candidates: Tuple[str, ...] = ()
    kind: str = DIRECT  # what the proof rests on: direct | inferred_identity | scoped_snapshot | document_or_ocr | set_wise
    # every non-direct element of the proof (a scoped-snapshot proof of an activity-attributed account has two); empty = direct.
    # A COMPLETE step is emitted only when the plan policy allows each one (ghostaudit/plan.py).
    inferences: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Gap:
    code: str
    resource_id: Optional[str]
    message: str
    blocking: bool = True  # blocks a PROVEN COMPLETE


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    status: str
    determination: str
    confidence: str
    intent: IntentResult
    violations: Tuple[Violation, ...] = ()
    requirements: Tuple[Requirement, ...] = ()
    gaps: Tuple[Gap, ...] = ()
    excluded: Tuple[Tuple[str, str, str], ...] = ()  # (resource_id, relevance label, reason)
    reasons: Tuple[str, ...] = ()
    policy: str = "default"
    counted_violations: Tuple[Violation, ...] = ()  # the violations that decided INCOMPLETE under the policy (the findings M5 may report)

    @property
    def proven(self) -> Tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if r.status == "PROVEN")

    @property
    def unproven(self) -> Tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if r.status != "PROVEN")


# --------------------------------------------------------------------------------------------- evidence confidence

def evidence_confidence(ev: StateEvidence) -> str:
    if ev.ocr or ev.fmt == "image":
        return MEDIUM if ev.grounding == "catalog+ocr" else LOW
    if ev.fmt == "pdf":
        return MEDIUM
    return HIGH


def _best(evs: Iterable[StateEvidence]) -> str:
    c = [evidence_confidence(e) for e in evs]
    return min(c, key=_RANK.__getitem__) if c else LOW


def _worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def _kind(conf_cap: str, evs: Sequence[StateEvidence]) -> str:
    if conf_cap != HIGH:
        return INFERRED_IDENTITY
    return DOCUMENT_OR_OCR if evs and _best(evs) != HIGH else DIRECT


def _inferences(conf_cap: str, evs: Sequence[StateEvidence]) -> Tuple[str, ...]:
    """What a resource proof infers: the owner (activity-attributed account) and/or the state (PDF text or screenshot OCR)."""
    return tuple(k for k, on in ((INFERRED_IDENTITY, conf_cap != HIGH), (DOCUMENT_OR_OCR, bool(evs) and _best(evs) != HIGH)) if on)


def _ids(d: ResourceDecision) -> Tuple[str, ...]:
    return d.state.citable


def _retention_expired(d: ResourceDecision, audit_at: str) -> bool:
    ends = [try_parse_ts(e.retention_until) for e in d.state.deciding if e.retention_until]
    return bool(ends) and all(t is not None and t < parse_ts(audit_at) for t in ends)


# --------------------------------------------------------------------------------------------- classification

class _Builder:
    def __init__(self, recon: CaseReconstruction, states: StateReconstructor, policy: ClassificationPolicy, scoped: Optional[ScopedAbsence] = None):
        self.r, self.states, self.policy, self.scoped = recon, states, policy, scoped
        self.violations: List[Violation] = []
        self.requirements: List[Requirement] = []
        self.gaps: List[Gap] = []
        self.excluded: List[Tuple[str, str, str]] = []
        self.account_done = False

    # ---- per-resource -------------------------------------------------------------------------------------
    def resource(self, d: ResourceDecision, account_conf_cap: str = HIGH, extra_inferences: Tuple[str, ...] = ()) -> None:
        rid, a = d.resource_id, d.assessment
        att = d.association
        if att is not None and att.status != "ASSOCIATED":
            # also enforces rule_06 ("attachment belongs only to the deletion subject"): an attachment naming several subjects is a competing-owner resource
            self.gaps.append(Gap("OWNERSHIP_CONFLICT", rid, f"{rid} is linked to several users ({', '.join(att.competing_users)} also); not used as a violation or as proof"))
            return
        if d.relevance.label == REL_UNCERTAIN:
            blocking = a.verdict in ("VIOLATION", "UNKNOWN")
            self.gaps.append(Gap("RELEVANCE_UNCERTAIN", rid, f"{rid} ({d.state.state}) cannot be placed in this request: " + "; ".join(d.relevance.reasons[-1:]), blocking))
            return
        evs = d.state.deciding
        conf = _worse(_best(evs), account_conf_cap) if evs else LOW
        caveats = tuple(self._caveats(d))
        if a.verdict == "VIOLATION":
            if conf == LOW:
                self.gaps.append(Gap("LOW_CONFIDENCE_EVIDENCE", rid, f"{rid}: a {a.finding_op} reading rests on evidence that is not reliably tied to the resource"))
            else:
                self.violations.append(Violation(rid, d.system_id, d.state.category, a.finding_op, a.required_action, _ids(d), conf, d.state.basis, caveats,
                                                 inferred_identity=account_conf_cap != HIGH))
        elif a.verdict in ("COMPLIANT", "RETAINED_OK"):
            if a.verdict == "RETAINED_OK" and _retention_expired(d, self.r.audit_at):
                self.requirements.append(Requirement("RETAINED_EXPIRED", rid, d.state.category, "", "PROVEN", conf, _ids(d), d.state.basis,
                                                     "retention period ended before audit_at; not required"))
                return
            if conf == LOW:
                self.gaps.append(Gap("LOW_CONFIDENCE_EVIDENCE", rid, f"{rid}: compliant-looking reading rests on evidence that is not reliably tied to the resource; not accepted as proof"))
                self.requirements.append(Requirement("RESOURCE_TREATED", rid, d.state.category, VERIFY_FOR_ACTION.get(a.required_action, ""), "UNPROVEN", LOW, (), d.state.basis, "low-confidence evidence"))
                return
            key = "RETAINED_PRESENT" if a.verdict == "RETAINED_OK" else ("ACCOUNT_ABSENT" if d.state.category == "USER_PROFILE" else "RESOURCE_TREATED")
            self.requirements.append(Requirement(key, rid, d.state.category, VERIFY_FOR_ACTION.get(a.required_action, ""), "PROVEN", conf, _ids(d), d.state.basis, a.reason,
                                                 kind=_kind(account_conf_cap, evs), inferences=_inferences(account_conf_cap, evs) + extra_inferences))
        elif a.verdict == "RETAINED_MISSING":
            if _retention_expired(d, self.r.audit_at):
                return
            self.violations.append(Violation(rid, d.system_id, d.state.category, None, a.required_action, _ids(d), conf if conf != LOW else MEDIUM, d.state.basis,
                                             ("a required retained record is absent; the output vocabulary has no operation for this",)))
        else:  # UNKNOWN
            why = self._unknown_reason(d)
            code = "STATE_UNKNOWN" if not any(e.grounding == "conflict" for e in d.state.history) else "OWNERSHIP_CONFLICT"
            self.gaps.append(Gap(code, rid, f"{rid} ({d.state.category}): {why}"))
            self.requirements.append(Requirement("RETAINED_PRESENT" if a.required_action == "RETAIN" else
                                                 ("ACCOUNT_ABSENT" if d.state.category == "USER_PROFILE" else "RESOURCE_TREATED"),
                                                 rid, d.state.category, VERIFY_FOR_ACTION.get(a.required_action, ""), "UNPROVEN", LOW, (), d.state.basis, why,
                                                 kind=INFERRED_IDENTITY if account_conf_cap != HIGH else DIRECT,
                                                 inferences=(INFERRED_IDENTITY,) if account_conf_cap != HIGH else ()))

    @staticmethod
    def _caveats(d: ResourceDecision) -> List[str]:
        out = []
        if any(e.ocr for e in d.state.deciding):
            out.append("state read from screenshot OCR (meaning rules unvalidated)")
        if any(e.fmt == "pdf" for e in d.state.deciding):
            out.append("state read from PDF text")
        if d.origin == "activity":
            out.append("account attributed from activity, not from an identifier")
        if d.state.category in ("SUPPORT_RECORD", "MARKETING_PROFILE"):
            out.append("category inferred from record shape (sample-informed; see m4_classification.md)")
        return out

    @staticmethod
    def _unknown_reason(d: ResourceDecision) -> str:
        if d.state.has_evidence:
            return f"latest usable reading could not be interpreted ({d.state.basis})"
        later = f"; {len(d.state.excluded)} reading(s) captured after audit_at ignored" if d.state.excluded else ""
        post = "; identified only by evidence captured after audit_at" if d.association is not None and d.association.post_audit_only else ""
        return "no evidence usable at audit_at" + later + post

    def _scoped_requirement(self, resource_id: Optional[str], candidates: Tuple[str, ...] = (), identity_inferred: bool = False,
                            extra_inferences: Tuple[str, ...] = ()) -> bool:
        """Support 'no account profile for the subject' by a subject-scoped snapshot, if the policy allows and one exists."""
        if not (self.policy.accept_scoped_snapshot_absence and self.scoped):
            return False
        sc = self.scoped
        self.requirements.append(Requirement("ACCOUNT_ABSENT", resource_id, "USER_PROFILE", "VERIFY_RECORD_ABSENT", "PROVEN", MEDIUM, (sc.file_id,), "scoped_snapshot",
                                             "no explicit record names the account; " + sc.describe(), candidates,
                                             kind=SCOPED_SNAPSHOT, inferences=(SCOPED_SNAPSHOT,) + ((INFERRED_IDENTITY,) if identity_inferred else ()) + extra_inferences))
        return True

    # ---- the account ---------------------------------------------------------------------------------------
    def account(self) -> None:
        att = self.r.account
        acct_decisions = [d for d in self.r.included if d.state.category == "USER_PROFILE" or d.system_id == "account_service"]
        if att.status in (CONTENT, STRONG) and att.account:
            d = self.r.decision(att.account)
            cap = HIGH if att.status == CONTENT else MEDIUM
            extra = (LATER_OWNERSHIP,) if att.later_ownership else ()
            if att.status == STRONG and not self.policy.accept_inferred_attribution:
                self.gaps.append(Gap("ACCOUNT_INFERRED", att.account, f"{att.account} is attributed from activity only; the policy does not accept that as proof"))
                self.requirements.append(Requirement("ACCOUNT_ABSENT", att.account, "USER_PROFILE", "VERIFY_RECORD_ABSENT", "UNPROVEN", LOW, (), "activity", "attribution not accepted"))
            elif d is not None and d.included:
                if d.state.state == "UNKNOWN" and d.relevance.label == RELEVANT and (d.association is None or d.association.status == "ASSOCIATED")                         and self._scoped_requirement(att.account, identity_inferred=att.status == STRONG, extra_inferences=extra):
                    self.gaps.append(Gap("ACCOUNT_STATE_NOT_EXPLICIT", att.account, f"{att.account}: no record names it at audit_at; absence rests on a subject-scoped snapshot", False))
                else:
                    self.resource(d, cap, extra)
            self.account_done = True
            return
        cands = list(att.candidates)
        if att.status == AMBIGUOUS and cands:
            sts = {c.resource_id: self.states.reconstruct(c.resource_id, self.r.audit_at) for c in cands}
            present = [r for r, s in sts.items() if s.assessment.verdict == "VIOLATION"]
            if present:
                pass
            elif not all(s.assessment.verdict == "COMPLIANT" and s.has_evidence for s in sts.values()) and self._scoped_requirement(None, tuple(sorted(sts))):
                self.gaps.append(Gap("ACCOUNT_AMBIGUOUS", None, "which account is the subject's is undetermined (" + ", ".join(sorted(sts)) + "); no account step can be produced", False))
                return
            if all(s.assessment.verdict == "COMPLIANT" and s.has_evidence for s in sts.values()):
                ids = tuple(sorted({i for s in sts.values() for i in s.citable}))
                self.requirements.append(Requirement("ACCOUNT_ABSENT", None, "USER_PROFILE", "VERIFY_RECORD_ABSENT", "PROVEN", MEDIUM, ids, "explicit negative record for every candidate",
                                                     "the account could not be identified among the candidates, but every candidate has an explicit absence record at audit_at; no resource id is claimed",
                                                     tuple(sorted(sts)), kind=SET_WISE))
                self.gaps.append(Gap("ACCOUNT_AMBIGUOUS", None, "which account is the subject's is undetermined (" + ", ".join(sorted(sts)) + "); no account step can be produced", False))
            elif present:
                self.gaps.append(Gap("ACCOUNT_AMBIGUOUS", None, "account undetermined among " + ", ".join(sorted(sts)) + f"; {', '.join(present)} is present at audit_at, so the subject's account may still exist"))
                self.requirements.append(Requirement("ACCOUNT_ABSENT", None, "USER_PROFILE", "VERIFY_RECORD_ABSENT", "UNPROVEN", LOW, (), "ambiguous", "candidate present", tuple(sorted(sts))))
            else:
                unk = sorted(r for r, s in sts.items() if not s.has_evidence)
                self.gaps.append(Gap("ACCOUNT_AMBIGUOUS", None, "account undetermined among " + ", ".join(sorted(sts)) + (f"; no usable state for {', '.join(unk)}" if unk else "")))
                self.requirements.append(Requirement("ACCOUNT_ABSENT", None, "USER_PROFILE", "VERIFY_RECORD_ABSENT", "UNPROVEN", LOW, (), "ambiguous", "no usable absence evidence", tuple(sorted(sts))))
            return
        if self._scoped_requirement(None):
            self.gaps.append(Gap("ACCOUNT_UNATTRIBUTED", None, "no account id could be attributed; absence rests on a subject-scoped snapshot, so no account step can be produced", False))
            return
        self.gaps.append(Gap("ACCOUNT_UNATTRIBUTED", None, "no deletion-worker account row passes the attribution filters and no identifier links an account in the request window"
                             + (f" (content-linked accounts outside the window: {', '.join(att.content_accounts)})" if att.content_accounts else "")))
        self.requirements.append(Requirement("ACCOUNT_ABSENT", None, "USER_PROFILE", "VERIFY_RECORD_ABSENT", "UNPROVEN", LOW, (), "none", "account not identified"))

    # ---- retained records ------------------------------------------------------------------------------------
    def retained_located(self) -> None:
        has = any(d.state.category == "SECURITY_AUDIT_LOG" for d in self.r.decisions if d.relevance.label != UNRELATED)
        if not has:
            self.gaps.append(Gap("RETAINED_NOT_LOCATED", None, "no security audit record could be linked to the subject, so the required retained record cannot be verified"))


def classify_case(case: DeletionRequest, intent: IntentResult, recon: Optional[CaseReconstruction], states: Optional[StateReconstructor],
                  policy: ClassificationPolicy = ClassificationPolicy(), scoped: Optional[ScopedAbsence] = None) -> CaseVerdict:
    if intent.outcome != DELETION:
        if intent.outcome == NON_DELETION or policy.uncertain_intent_status == NOT_APPLICABLE:
            why = "request text does not authorise deletion: " + intent.reason if intent.outcome == NON_DELETION else \
                  "request text is ambiguous and ambiguity never authorises deletion: " + intent.reason
            return CaseVerdict(case.case_id, NOT_APPLICABLE, "TEXT", HIGH if intent.outcome == NON_DELETION else LOW, intent, reasons=(why,), policy=policy.name)
    assert recon is not None and states is not None, "a deletion request needs a reconstruction"
    b = _Builder(recon, states, policy, scoped)
    b.account()
    account_ids = {r.resource_id for r in b.requirements if r.resource_id} | {g.resource_id for g in b.gaps if g.resource_id} | {v.resource_id for v in b.violations}
    for d in recon.decisions:
        if not d.included:
            b.excluded.append((d.resource_id, d.relevance.label, d.relevance.reasons[0] if d.relevance.reasons else ""))
            continue
        if d.resource_id in account_ids:
            continue
        b.resource(d)
    b.retained_located()
    return _decide(case, intent, b, policy)


def _decide(case: DeletionRequest, intent: IntentResult, b: _Builder, policy: ClassificationPolicy) -> CaseVerdict:
    counted = [v for v in b.violations if v.confidence == HIGH or (v.confidence == MEDIUM and policy.accept_medium_violations)]
    blocking = [g for g in b.gaps if g.blocking]
    unproven = [r for r in b.requirements if r.status != "PROVEN"]
    proven = [r for r in b.requirements if r.status == "PROVEN"]
    reasons: List[str] = []
    if counted:
        high = any(v.confidence == HIGH for v in counted)
        status, det, conf = INCOMPLETE, ("PROVEN" if high else "SUPPORTED"), (HIGH if high else MEDIUM)
        for v in sorted(counted, key=lambda v: (v.resource_id)):
            reasons.append(f"{v.resource_id}: {v.finding_op or 'retained record missing'} ({v.confidence}) cited {', '.join(v.evidence_ids) or '-'}")
    elif not blocking and not unproven and proven:
        allhigh = all(r.confidence == HIGH for r in proven if r.key != "RETAINED_EXPIRED")
        status, det, conf = COMPLETE, ("PROVEN" if allhigh else "INFERRED"), (HIGH if allhigh else MEDIUM)
        n_med = sum(1 for r in proven if r.confidence != HIGH and r.key != "RETAINED_EXPIRED")
        reasons.append(f"{len(proven)} requirement(s) proven by eligible evidence ({len(proven) - n_med} explicit/structured, {n_med} inferred: activity-attributed account, PDF/OCR or subject-scoped snapshot); no violation, no unproven item")
    else:
        status, det, conf = policy.unproven_status, "PARTIAL", LOW
        reasons.append("no violation found, but the deletion cannot be proven: " + ("; ".join(sorted({g.code for g in blocking})) or "no requirement proven"))
        reasons.append(f"status taken from policy '{policy.name}' (unproven_status={policy.unproven_status})")
    if b.violations and not counted:
        reasons.append("violation(s) exist only at a confidence the policy does not count")
    return CaseVerdict(case.case_id, status, det, conf, intent, tuple(counted + [v for v in b.violations if v not in counted]), tuple(b.requirements), tuple(b.gaps),
                       tuple(b.excluded), tuple(reasons), policy.name, tuple(counted))


def classify_all(cr: CaseReconstructor, cases: Sequence[DeletionRequest], policy: ClassificationPolicy = ClassificationPolicy()) -> Dict[str, CaseVerdict]:
    """Intent first (text only), then reconstruct *only* the deletion requests together (account attribution is global)."""
    intents = {c.case_id: classify_intent(c.request_text) for c in cases}
    deletion = [c for c in cases if intents[c.case_id].outcome == DELETION or
                (intents[c.case_id].outcome == UNCERTAIN and policy.uncertain_intent_status != NOT_APPLICABLE)]
    recs = cr.reconstruct_all(deletion)
    catalog = SnapshotCatalog(cr.results, cr.manifest)
    out = {}
    for c in cases:
        rc = recs.get(c.case_id)
        scoped = find_scoped_absence(catalog, cr.links, rc.user_id, c.audit_at) if rc is not None else None
        out[c.case_id] = classify_case(c, intents[c.case_id], rc, cr.states, policy, scoped)
    return out
