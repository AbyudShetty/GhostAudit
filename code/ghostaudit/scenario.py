"""Scenario relevance: does a resource that belongs to the user also belong to *this* request?

A user can have several deletion requests and unrelated background activity. Ownership (M2) says the
resource is the user's; it does not say it concerns this request. This module answers the second
question from three independent kinds of evidence, never from a time window alone:

1. **workflow** - deletion-worker rows for the resource: inside the request lifecycle
   (``requested_at <= t <= audit_at``) and in the same batch as ``reported_at``, in the lifecycle but off
   batch, or only outside the lifecycle (a different timeline), or none;
2. **freshness** - when did usable sources (state readings and eligible link sources) last mention it:
   during the lifecycle (fresh), only before ``requested_at`` (stale), or never before ``audit_at``;
3. **role** - retained records (security audit log, legal hold) have no workflow rows by design and are
   in scope through their owner.

Decision table (first match wins; every match is recorded in ``reasons``, with a confidence)::

    workflow IN_BATCH                              -> RELEVANT   HIGH    (this request's own workflow touched it)
    freshness FRESH                                -> RELEVANT   HIGH    (observed while the request was open)
    role RETAINED & unambiguous owner              -> RELEVANT   MEDIUM  (retained record of the subject)
    claimed by a *sibling case* of the same user   -> UNRELATED  HIGH    (its evidence/workflow lies in another
                                                                         request's lifecycle, none in this one)
    workflow OUTSIDE_LIFECYCLE and nothing fresh   -> UNRELATED  MEDIUM  (all we know about it is another timeline)
    otherwise (stale only, off-batch only, nothing)-> UNCERTAIN  LOW     (kept and flagged; never silently dropped)

A RELEVANT outcome is downgraded to UNCERTAIN when ownership is contested: competing owners, or a screenshot that shows the
resource under someone else's identifiers.

Staleness alone is *not* grounds for exclusion: without a competing request or explicit workflow activity
elsewhere there is no positive evidence that the resource belongs to a different timeline.

``BATCH_HOURS`` is an empirical regularity, not a rule of the specification: 253 of 263 content-linked
accounts have their delete row within -90..+15 min of ``reported_at`` and 95-99% of every other resource
type within 4 h; the exceptions are exactly the other-timeline resources. It is a parameter so tests and
ablations can change it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .dataset import Activity, DeletionRequest
from .linker import Association
from .state import ResourceState
from .timeutil import parse_ts

BATCH_HOURS = 4.0
WORKER_OPS = frozenset({"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                        "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "RETAIN_RECORD"})
RETAINED_CATEGORIES = frozenset({"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"})

IN_BATCH, OFF_BATCH, OUTSIDE, NO_WORKFLOW = "IN_BATCH", "IN_LIFECYCLE_OFF_BATCH", "OUTSIDE_LIFECYCLE", "NONE"
FRESH, STALE, NO_SOURCE = "FRESH", "STALE", "NONE"
RELEVANT, UNRELATED, UNCERTAIN = "RELEVANT", "UNRELATED", "UNCERTAIN"


class WorkflowIndex:
    """Deletion-worker activity by resource (claims, used only to place a resource in a timeline)."""

    def __init__(self, activities: Iterable[Activity]):
        self.rows: Dict[str, List[Activity]] = defaultdict(list)
        for a in activities:
            if a.source_system == "deletion_worker" and a.event_type in WORKER_OPS:
                self.rows[a.resource_id].append(a)
        for v in self.rows.values():
            v.sort(key=lambda a: (a.event_time, a.activity_id))

    def classify(self, resource_id: str, case: DeletionRequest, batch_hours: float = BATCH_HOURS) -> Tuple[str, Tuple[Activity, ...]]:
        rows = self.rows.get(resource_id, [])
        if not rows:
            return NO_WORKFLOW, ()
        req, aud, rep = parse_ts(case.requested_at), parse_ts(case.audit_at), parse_ts(case.reported_at)
        inside = [a for a in rows if req <= parse_ts(a.event_time) <= aud]
        if not inside:
            return OUTSIDE, tuple(rows)
        near = [a for a in inside if abs((parse_ts(a.event_time) - rep).total_seconds()) <= batch_hours * 3600]
        return (IN_BATCH, tuple(near)) if near else (OFF_BATCH, tuple(inside))


@dataclass(frozen=True)
class Relevance:
    resource_id: str
    label: str
    reasons: Tuple[str, ...]
    workflow: str
    freshness: str
    role: str  # "RETAINED" | "-"
    workflow_rows: Tuple[str, ...] = ()  # activity ids
    confidence: str = "LOW"  # HIGH | MEDIUM | LOW
    claimed_by: Tuple[str, ...] = ()  # sibling case ids that claim the resource

    @property
    def included(self) -> bool:
        return self.label != UNRELATED


def freshness(case: DeletionRequest, state: Optional[ResourceState], association: Optional[Association], link_times: Sequence[str] = ()) -> str:
    """FRESH if any usable source mentions the resource during the lifecycle; STALE if all usable sources predate it."""
    req, aud = parse_ts(case.requested_at), parse_ts(case.audit_at)
    times: List[str] = []
    if state is not None:
        times += [e.effective_time for e in state.history]
    times += list(link_times)
    times = [t for t in times if parse_ts(t) <= aud]
    if not times:
        return NO_SOURCE
    return FRESH if any(parse_ts(t) >= req for t in times) else STALE


def _in_lifecycle(t: str, case: DeletionRequest) -> bool:
    return parse_ts(case.requested_at) <= parse_ts(t) <= parse_ts(case.audit_at)


def assess_relevance(case: DeletionRequest, resource_id: str, workflow: WorkflowIndex, state: Optional[ResourceState],
                     association: Optional[Association], link_times: Sequence[str] = (), batch_hours: float = BATCH_HOURS,
                     siblings: Sequence[DeletionRequest] = ()) -> Relevance:
    """``siblings``: the *other* deletion requests of the same user (their lifecycles are what makes "another timeline" provable)."""
    wf, rows = workflow.classify(resource_id, case, batch_hours)
    fr = freshness(case, state, association, link_times)
    category = state.category if state is not None else ""
    role = "RETAINED" if category in RETAINED_CATEGORIES else "-"
    unambiguous = association is None or association.status == "ASSOCIATED"
    ids = tuple(a.activity_id for a in rows)
    usable_times = [e.effective_time for e in (state.history if state else ())] + [t for t in link_times if parse_ts(t) <= parse_ts(case.audit_at)]
    row_times = [a.event_time for a in workflow.rows.get(resource_id, [])]
    claimed = tuple(sorted(s.case_id for s in siblings if s.case_id != case.case_id and any(_in_lifecycle(t, s) for t in usable_times + row_times)))
    reasons: List[str] = []
    contested = state is not None and any(e.basis == "screenshot.conflicting_subject" for e in state.history)

    def done(label, conf, *more):
        r = list(reasons) + list(more)
        if contested and label == RELEVANT:
            return Relevance(resource_id, UNCERTAIN, tuple(r + ["downgraded: a screenshot shows this resource under someone else's identifiers (conflicting ownership)"]), wf, fr, role, ids, "LOW", claimed)
        if not unambiguous and label == RELEVANT:
            return Relevance(resource_id, UNCERTAIN, tuple(r + ["downgraded: resource has competing owners"]), wf, fr, role, ids, "LOW", claimed)
        return Relevance(resource_id, label, tuple(r), wf, fr, role, ids, conf, claimed)

    if wf == IN_BATCH:
        reasons.append(f"workflow: deletion-worker row(s) {', '.join(ids)} within {batch_hours:g}h of reported_at and inside the request lifecycle")
        return done(RELEVANT, "HIGH")
    if fr == FRESH:
        reasons.append("freshness: a usable source mentions it during the request lifecycle")
        if wf == OUTSIDE:
            reasons.append("note: its workflow rows lie outside the lifecycle (background activity); fresh evidence takes precedence")
        return done(RELEVANT, "HIGH")
    if role == "RETAINED" and unambiguous:
        reasons.append("role: retained record of the subject (no workflow rows by design)")
        return done(RELEVANT, "MEDIUM")
    if claimed:
        reasons.append(f"another request of the same user claims it ({', '.join(claimed)}): its evidence/workflow lies in that lifecycle and none in this one")
        return done(UNRELATED, "HIGH")
    if wf == OUTSIDE:
        reasons.append("workflow: every deletion-worker row lies outside [requested_at, audit_at] and no usable source falls inside it: another timeline")
        return done(UNRELATED, "MEDIUM")
    why = {STALE: "every usable source predates requested_at (stale), but no competing request or workflow row proves another timeline",
           OFF_BATCH: "worker row is inside the lifecycle but not in the request's batch",
           NO_WORKFLOW: "no workflow row and no usable source inside the lifecycle"}.get(fr if fr == STALE else wf, "cannot place it in this request")
    reasons.append("uncertain: " + why)
    return done(UNCERTAIN, "LOW")
