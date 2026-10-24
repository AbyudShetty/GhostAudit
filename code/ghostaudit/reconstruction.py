"""Per-case reconstruction: which resources belong to the request, what state each was in at ``audit_at``, and why.

This is the M3 integration layer. It only *composes* the earlier pieces and keeps their questions separate:

    identity (who)      -> IdentityGraph / LinkIndex          (M2, policy-controlled)
    account attribution -> AccountAttributor                  (activity claims, never state)
    relevance           -> scenario.assess_relevance          (does it concern THIS request?)
    state               -> StateReconstructor                 (usable evidence only, with exclusions listed)
    policy verdict      -> state.assess                       (category x state)

Nothing here builds steps or decides COMPLETE / INCOMPLETE (that is M4+). Every decision carries an
``explanation`` listing the rule that included or excluded it, the evidence that decided its state, and what
was ignored because it was not usable at ``audit_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .attribution import AMBIGUOUS, CONTENT, NONE, STRONG, WINDOW_MIN, AccountAttribution, AccountAttributor
from .dataset import Dataset, DeletionRequest
from .evidence import FileResult
from .identity import IdentityGraph, Resolution, build_identity_graph
from .linker import POLICIES, Association, LinkIndex, LinkPolicy, build_link_index, DEFAULT_POLICY
from .parsers import parse_all
from .scenario import BATCH_HOURS, RELEVANT, UNCERTAIN, UNRELATED, Relevance, WorkflowIndex, assess_relevance
from .schema import system_for_resource
from .screenshots import attach_catalog_aliases, load_cache, screenshot_observations
from .state import Assessment, ResourceState, StateReconstructor, assess


@dataclass(frozen=True)
class ResourceDecision:
    resource_id: str
    system_id: Optional[str]
    origin: str  # "content" (identifier link) | "activity" (account attributed from deletion-worker rows)
    association: Optional[Association]
    relevance: Relevance
    state: ResourceState
    assessment: Assessment
    explanation: Tuple[str, ...]

    @property
    def included(self) -> bool:
        return self.relevance.included


@dataclass(frozen=True)
class CaseReconstruction:
    case_id: str
    user_id: str
    audit_at: str
    policy: str
    resolution: Resolution
    account: AccountAttribution
    decisions: Tuple[ResourceDecision, ...]  # every candidate, included or not (sorted by resource id)
    notes: Tuple[str, ...] = ()

    @property
    def included(self) -> Tuple[ResourceDecision, ...]:
        return tuple(d for d in self.decisions if d.included)

    def decision(self, resource_id: str) -> Optional[ResourceDecision]:
        return next((d for d in self.decisions if d.resource_id == resource_id), None)


def load_evidence(ds: Dataset, graph: IdentityGraph, use_screenshots: bool = True) -> Tuple[List[FileResult], List[str]]:
    """All parsed evidence; image FileResults are replaced by grounded OCR observations when a verified cache exists."""
    results = parse_all(ds)
    notes: List[str] = []
    if use_screenshots:
        shots, notes = screenshot_observations(ds, graph, load_cache())
        by = {r.file_id: r for r in shots}
        results = [by.get(r.file_id, r) if r.fmt == "image" else r for r in results]
    notes += attach_catalog_aliases(ds, results)
    return results, notes


class CaseReconstructor:
    def __init__(self, ds: Dataset, use_screenshots: bool = True, batch_hours: float = BATCH_HOURS,
                 window_min: Tuple[float, float] = WINDOW_MIN, require_result_match: bool = True):
        self.batch_hours = batch_hours
        self.ds = ds
        self.graph = build_identity_graph(ds)
        self.results, self.screenshot_notes = load_evidence(ds, self.graph, use_screenshots)
        self.manifest = ds.evidence_by_id()
        self.links: LinkIndex = build_link_index(self.graph, self.results, self.manifest, ds.activity.rows)
        self.states = StateReconstructor(self.results, self.manifest, ds.activity.rows)
        self.workflow = WorkflowIndex(ds.activity.rows)
        self.attributor = AccountAttributor(ds.activity.rows, self.links, window_min, require_result_match)

    def user_of(self, case: DeletionRequest) -> str:
        r = self.graph.resolve_request_email(case.email)
        if r.status != "RESOLVED":
            raise ValueError(f"{case.case_id}: request e-mail does not resolve to one user ({r.status})")
        return r.user_ids[0]

    def reconstruct_all(self, cases: Sequence[DeletionRequest], policy: str = DEFAULT_POLICY) -> Dict[str, CaseReconstruction]:
        """Reconstruct every case. Account attribution is global (it eliminates accounts settled for other cases), so the
        whole deletion-request population should be passed together."""
        pairs = [(c, self.user_of(c)) for c in cases]
        attribution = self.attributor.attribute(pairs)
        by_user: Dict[str, List[DeletionRequest]] = {}
        for c, u in pairs:
            by_user.setdefault(u, []).append(c)
        return {c.case_id: self._one(c, u, self.links_policy(policy), attribution[c.case_id], by_user[u]) for c, u in pairs}

    @staticmethod
    def links_policy(name: str) -> LinkPolicy:
        return POLICIES[name]

    def _one(self, case: DeletionRequest, user: str, policy: LinkPolicy, att: AccountAttribution,
             same_user_cases: Sequence[DeletionRequest] = ()) -> CaseReconstruction:
        assoc = {a.resource_id: a for a in self.links.associations(user, case.audit_at, policy)}
        notes: List[str] = []
        candidates: Dict[str, Tuple[str, Optional[Association]]] = {rid: ("content", a) for rid, a in assoc.items()}
        if att.status == STRONG and att.account and att.account not in candidates:
            candidates[att.account] = ("activity", None)
        elif att.status in (AMBIGUOUS, NONE):
            notes.append(f"account unresolved ({att.status}): " + "; ".join(att.reasons) +
                         (f" candidates: {', '.join(c.resource_id for c in att.candidates)}" if att.candidates else ""))
        decisions: List[ResourceDecision] = []
        for rid in sorted(candidates):
            origin, a = candidates[rid]
            st = self.states.reconstruct(rid, case.audit_at)
            link_times = [l.source_time for l in a.links if self.links.eligible(l, case.audit_at)] if a else []
            rel = assess_relevance(case, rid, self.workflow, st, a, link_times, batch_hours=self.batch_hours, siblings=same_user_cases)
            expl = list(rel.reasons)
            if a is not None:
                expl.append(f"identity: {'+'.join(a.link_types)} via {', '.join(a.sources)}" + (" (identified only by post-audit evidence; no state or citation rights)" if a.post_audit_only else ""))
            else:
                expl.append(f"identity: attributed from activity ({att.confidence}): {att.reasons[0]}")
            if st.has_evidence:
                expl.append(f"state {st.state} from {', '.join(st.citable)} ({st.basis})" + (" [screenshot OCR]" if any(e.ocr for e in st.deciding) else ""))
            else:
                expl.append("state UNKNOWN: " + (st.notes[0] if st.notes else "no usable evidence"))
            expl += list(st.notes)
            decisions.append(ResourceDecision(rid, system_for_resource(rid), origin, a, rel, st, st.assessment, tuple(expl)))
        return CaseReconstruction(case.case_id, user, case.audit_at, policy.name, self.graph.resolve_request_email(case.email), att, tuple(decisions), tuple(notes))
