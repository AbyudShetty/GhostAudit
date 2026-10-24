"""Activity-based account attribution (kept separate from content linking, by design).

Question: for a case whose account is not linked by any identifier, which account did the deletion
workflow act on? The only evidence is ``system_activity.csv`` (deletion-worker DELETE_RECORD rows on
``account_service``), which are *claims*. Attribution therefore only ever says "this account's delete row
is the unique one compatible with this case", never "this account was deleted".

Hard filters (all must hold for a candidate):

1. the row is a deletion-worker ``DELETE_RECORD`` on an ``account_service`` resource;
2. it lies inside the request lifecycle ``[requested_at, audit_at]`` **and** within
   ``WINDOW_MIN`` minutes of ``reported_at`` (empirical regularity, see ``scenario.py``);
3. its result is compatible with the case's ``reported_status`` (COMPLETE/SUCCESS, PARTIALLY_COMPLETE/
   PARTIAL, NOT_STARTED/FAILED). Both are claims of the same workflow, so agreement is a *matching key*,
   not proof of state (validated: 252 of 253 content-linked pairs agree);
4. the account is not owned (by any content link) by a different user.

Resolution: a case whose own account is already linked by content and whose delete row passes 1-3 is
``CONTENT`` (high confidence). Otherwise, candidates are the unowned accounts passing 1-3. Unique
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
from .linker import LinkIndex
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


class AccountAttributor:
    def __init__(self, activities: Iterable[Activity], links: LinkIndex, window_min: Tuple[float, float] = WINDOW_MIN,
                 require_result_match: bool = True):
        self.window_min = window_min
        self.require_result_match = require_result_match
        self.events: Dict[str, Activity] = {}
        for a in activities:
            if a.source_system == "deletion_worker" and a.target_system == "account_service" and a.event_type == "DELETE_RECORD":
                self.events[a.resource_id] = a
        self.owners: Dict[str, Set[str]] = defaultdict(set)
        for l in links.links:
            if l.resource_id.startswith("account_"):
                self.owners[l.resource_id].add(l.user_id)

    def _window(self, case: DeletionRequest, a: Activity) -> Tuple[bool, float]:
        t = parse_ts(a.event_time)
        off = (t - parse_ts(case.reported_at)).total_seconds() / 60.0
        lifecycle = parse_ts(case.requested_at) <= t <= parse_ts(case.audit_at)
        return (lifecycle and self.window_min[0] <= off <= self.window_min[1]), off

    def attribute(self, cases: Sequence[Tuple[DeletionRequest, str]], hide_content_for: Iterable[str] = ()) -> Dict[str, AccountAttribution]:
        """``cases``: (request, user_id) pairs for deletion requests. ``hide_content_for``: validation only - pretend these
        cases' own content links are unknown (the accounts then count as unowned)."""
        hide = set(hide_content_for)
        by_case = {c.case_id: (c, u) for c, u in cases}
        owners = {r: set(u) for r, u in self.owners.items()}
        for cid in hide:
            c, u = by_case[cid]
            for r, us in owners.items():
                if u in us:
                    us.discard(u)
        out: Dict[str, AccountAttribution] = {}
        pools: Dict[str, Dict[str, AccountCandidate]] = {}
        rejected: Dict[str, List[Tuple[str, str]]] = {}
        content: Dict[str, List[str]] = {}
        for c, u in cases:
            pools[c.case_id], rejected[c.case_id], content[c.case_id] = {}, [], sorted(r for r, us in owners.items() if u in us)
            want = RESULT_FOR_STATUS.get(c.reported_status)
            own_hits: List[str] = []
            for rid, a in sorted(self.events.items()):
                inwin, off = self._window(c, a)
                if not inwin:
                    continue
                if owners.get(rid) and u not in owners[rid]:
                    rejected[c.case_id].append((rid, "owned by another user"))
                    continue
                if self.require_result_match and want is not None and a.reported_result != want:
                    rejected[c.case_id].append((rid, f"result {a.reported_result} incompatible with reported_status {c.reported_status}"))
                    continue
                cand = AccountCandidate(rid, a.activity_id, a.event_time, round(off, 1), a.reported_result)
                if u in owners.get(rid, ()):
                    own_hits.append(rid)
                else:
                    pools[c.case_id][rid] = cand
            if len(own_hits) == 1:
                out[c.case_id] = AccountAttribution(c.case_id, u, CONTENT, HIGH, own_hits[0], (), (
                    f"content link + deletion-worker row {self.events[own_hits[0]].activity_id} inside the lifecycle window with a result compatible with reported_status",),
                    tuple(content[c.case_id]), tuple(rejected[c.case_id]))
            elif len(own_hits) > 1:
                out[c.case_id] = AccountAttribution(c.case_id, u, AMBIGUOUS, NO_CONFIDENCE, None,
                                                    tuple(AccountCandidate(r, self.events[r].activity_id, self.events[r].event_time, 0.0, self.events[r].reported_result) for r in own_hits),
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
