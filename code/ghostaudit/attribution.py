"""Activity-based account attribution (kept separate from content linking, by design).

Question: for a case whose account is not linked by any identifier, which account did the deletion
workflow act on? The only evidence is ``system_activity.csv`` (deletion-worker DELETE_RECORD rows on
``account_service``), which are *claims*. Attribution therefore only ever says "this account's delete row
is the unique one compatible with this case", never "this account was deleted".

Hard filters (all must hold for a candidate):

1. the row is a deletion-worker ``DELETE_RECORD`` on an ``account_service`` resource;
2. it lies inside the request lifecycle ``[requested_at, audit_at]`` **and** within
   ``WINDOW_MIN`` minutes of ``reported_at`` (empirical regularity, see ``scenario.py``). An account can have several rows (a
   retry); all are kept, and for each case the *last* row inside that case's window speaks for the account (a retry supersedes the
   attempt before it; rows outside the window belong to another timeline). Rows are ordered by (event_time, activity_id), so the
   order of ``system_activity.csv`` cannot change the result;
3. its result is compatible with the case's ``reported_status`` (COMPLETE/SUCCESS, PARTIALLY_COMPLETE/
   PARTIAL, NOT_STARTED/FAILED). Both are claims of the same workflow, so agreement is a *matching key*,
   not proof of state (validated: 252 of 253 content-linked pairs agree);
4. the account is not owned (by any content link) by a different user. Ownership is *case-specific*: only links the link policy
   admits at that case's ``audit_at`` count (by default: sources captured at or before it), so evidence captured later can
   neither make an account the subject's nor rule a candidate out, for this case or for any other.

Resolution: a case whose own account is already linked by content and whose delete row passes 1-2 is
``CONTENT`` (high confidence; filter 3 is not applied to it, because the identifier already says whose account it is and a
partially completed workflow can contain a successful account delete). Otherwise, candidates are the unowned accounts passing 1-3. Unique
candidates are assigned and removed from every other case's pool, repeatedly, until nothing changes
(order-independent, because only forced moves are made). What remains with several candidates is
``AMBIGUOUS`` and keeps the whole candidate list; with none, ``NONE``. Nothing is ever guessed.

Tested and rejected tie-breakers: case_id order vs delete-row order (holds in only 8 of 59 groups).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .dataset import Activity, DeletionRequest
from .linker import DEFAULT_POLICY, POLICIES, LinkIndex, LinkPolicy
from .timeutil import parse_ts

WINDOW_MIN = (-90.0, 15.0)
RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

CONTENT, STRONG, AMBIGUOUS, NONE = "CONTENT", "STRONG", "AMBIGUOUS", "NONE"
HIGH, MEDIUM, NO_CONFIDENCE = "HIGH", "MEDIUM", "NONE"


@dataclass(frozen=True)
class AccountCandidate:
    resource_id: str
    activity_id: str
    event_time: str
    offset_min: float
    result: str


@dataclass(frozen=True)
class AccountAttribution:
    case_id: str
    user_id: str
    status: str  # CONTENT | STRONG | AMBIGUOUS | NONE
    confidence: str  # HIGH | MEDIUM | NONE
    account: Optional[str]
    candidates: Tuple[AccountCandidate, ...]  # unowned, filter-passing accounts still possible (empty for CONTENT)
    reasons: Tuple[str, ...]
    content_accounts: Tuple[str, ...] = ()  # accounts owned by the user, in or out of the window
    rejected: Tuple[Tuple[str, str], ...] = ()  # (resource_id, why) for accounts seen in the window and ruled out
    later_ownership: bool = False  # a STRONG assignment that needs ownership known only from sources captured after audit_at


class AccountAttributor:
    def __init__(self, activities: Iterable[Activity], links: LinkIndex, window_min: Tuple[float, float] = WINDOW_MIN,
                 require_result_match: bool = True, policy: LinkPolicy = POLICIES[DEFAULT_POLICY]):
        self.window_min = window_min
        self.require_result_match = require_result_match
        self.policy = policy
        self.links = links
        events: Dict[str, List[Activity]] = defaultdict(list)
        for a in activities:
            if a.source_system == "deletion_worker" and a.target_system == "account_service" and a.event_type == "DELETE_RECORD":
                events[a.resource_id].append(a)
        # every deletion-worker row per account, in time order (ties by activity id): independent of the file's row order
        self.events: Dict[str, List[Activity]] = {r: sorted(v, key=lambda a: (parse_ts(a.event_time), a.activity_id)) for r, v in events.items()}
        self._account_links = [l for l in links.links if l.resource_id.startswith("account_")]
        self._owners: Dict[Tuple[str, str], Dict[str, Set[str]]] = {}

    def owners_at(self, audit_at: str, policy: Optional[LinkPolicy] = None) -> Dict[str, Set[str]]:
        """Account -> users whose links to it the link policy admits at ``audit_at`` (cached per audit time)."""
        policy = policy or self.policy
        key = (policy.name, audit_at)
        if key not in self._owners:
            owners: Dict[str, Set[str]] = defaultdict(set)
            for l in self._account_links:
                if self.links.admissible(l, audit_at, policy)[0]:
                    owners[l.resource_id].add(l.user_id)
            self._owners[key] = owners
        return self._owners[key]

    def row_for(self, case: DeletionRequest, resource_id: str) -> Optional[Tuple[Activity, float]]:
        """The deletion-worker row that speaks for ``resource_id`` in ``case`` and its offset (minutes from ``reported_at``): the
        last of the account's rows inside the case's window, or ``None``."""
        hits = [(a, off) for a in self.events.get(resource_id, ()) for inwin, off in [self._window(case, a)] if inwin]
        return hits[-1] if hits else None

    def _window(self, case: DeletionRequest, a: Activity) -> Tuple[bool, float]:
        t = parse_ts(a.event_time)
        off = (t - parse_ts(case.reported_at)).total_seconds() / 60.0
        lifecycle = parse_ts(case.requested_at) <= t <= parse_ts(case.audit_at)
        return (lifecycle and self.window_min[0] <= off <= self.window_min[1]), off

    def attribute(self, cases: Sequence[Tuple[DeletionRequest, str]], hide_content_for: Iterable[str] = (),
                  policy: Optional[LinkPolicy] = None) -> Dict[str, AccountAttribution]:
        """``cases``: (request, user_id) pairs for deletion requests. ``hide_content_for``: validation only - pretend these
        cases' own content links are unknown (the accounts then count as unowned). ``policy``: the link policy (default: the
        attributor's), applied at each case's own ``audit_at``."""
        by_case = {c.case_id: (c, u) for c, u in cases}
        hidden = {by_case[cid][1] for cid in hide_content_for}
        out: Dict[str, AccountAttribution] = {}
        pools: Dict[str, Dict[str, AccountCandidate]] = {}
        rejected: Dict[str, List[Tuple[str, str]]] = {}
        content: Dict[str, List[str]] = {}
        for c, u in cases:
            owners = {r: us - hidden for r, us in self.owners_at(c.audit_at, policy).items()}  # what was knowable for *this* case
            pools[c.case_id], rejected[c.case_id], content[c.case_id] = {}, [], sorted(r for r, us in owners.items() if u in us)
            want = RESULT_FOR_STATUS.get(c.reported_status)
            own_hits: List[str] = []
            own_rows: Dict[str, Activity] = {}
            for rid in sorted(self.events):
                hit = self.row_for(c, rid)
                if hit is None:
                    continue
                a, off = hit
                if owners.get(rid) and u not in owners[rid]:
                    rejected[c.case_id].append((rid, "owned by another user"))
                    continue
                if u in owners.get(rid, ()):
                    # the subject's own identifier already names this account: the result key only chooses among *unowned* accounts,
                    # and a partial workflow can contain a successful account delete
                    own_hits.append(rid)
                    own_rows[rid] = a
                    continue
                if self.require_result_match and want is not None and a.reported_result != want:
                    rejected[c.case_id].append((rid, f"result {a.reported_result} incompatible with reported_status {c.reported_status}"))
                    continue
                pools[c.case_id][rid] = AccountCandidate(rid, a.activity_id, a.event_time, round(off, 1), a.reported_result)
            if len(own_hits) == 1:
                out[c.case_id] = AccountAttribution(c.case_id, u, CONTENT, HIGH, own_hits[0], (), (
                    f"content link + deletion-worker row {own_rows[own_hits[0]].activity_id} inside the lifecycle window with a result compatible with reported_status",),
                    tuple(content[c.case_id]), tuple(rejected[c.case_id]))
            elif len(own_hits) > 1:
                out[c.case_id] = AccountAttribution(c.case_id, u, AMBIGUOUS, NO_CONFIDENCE, None,
                                                    tuple(AccountCandidate(r, own_rows[r].activity_id, own_rows[r].event_time, 0.0, own_rows[r].reported_result) for r in own_hits),
                                                    ("several content-linked accounts of this user fit the window",), tuple(content[c.case_id]), tuple(rejected[c.case_id]))
        # accounts already settled by content are not available to anyone else
        taken: Dict[str, str] = {r.account: cid for cid, r in out.items() if r.status == CONTENT and r.account}
        unresolved = sorted(cid for cid in pools if cid not in out)
        assigned: Dict[str, str] = {}
        conflicts: Set[str] = set()
        while True:
            taken_now = set(taken) | set(assigned.values())
            avail = {cid: [r for r in pools[cid] if r not in taken_now] for cid in unresolved if cid not in assigned}
            forced = {cid: a[0] for cid, a in avail.items() if len(a) == 1}
            demand = Counter(forced.values())
            newly = {cid: r for cid, r in forced.items() if demand[r] == 1}
            conflicts = {cid for cid, r in forced.items() if demand[r] > 1}
            if not newly:
                break
            assigned.update(newly)  # simultaneous forced moves only: order-independent
        for cid in unresolved:
            c, u = by_case[cid]
            if cid in assigned:
                r = assigned[cid]
                out[cid] = AccountAttribution(cid, u, STRONG, MEDIUM, r, (pools[cid][r],), (
                    "unique account passing window + result-compatibility + ownership filters after eliminating accounts settled for other cases",),
                    tuple(content[cid]), tuple(rejected[cid]))
                continue
            avail = tuple(pools[cid][r] for r in sorted(pools[cid]) if r not in taken and r not in assigned.values())
            if avail:
                why = ("conflict: another case's only remaining option is the same account, so neither is assigned" if cid in conflicts
                       else f"{len(avail)} accounts remain possible; a plausible temporal match is not an attribution")
                out[cid] = AccountAttribution(cid, u, AMBIGUOUS, NO_CONFIDENCE, None, avail, (why,), tuple(content[cid]), tuple(rejected[cid]))
            else:
                out[cid] = AccountAttribution(cid, u, NONE, NO_CONFIDENCE, None, (), ("no deletion-worker account row passes the filters",),
                                              tuple(content[cid]), tuple(rejected[cid]))
        return out
