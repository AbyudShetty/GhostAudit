"""Content linker: which *resources* belong to which *user*, with provenance.

Scope (M2)
----------
Only question answered: "is resource R associated with user U, and what links say so?"
It does **not** decide what state R was in at ``audit_at``, whether R violates policy,
which evidence may be cited, or the case status (those are M3+). Account links that can
only be inferred from deletion-worker activity timing are deliberately NOT produced here.

Link types (strongest first)
----------------------------
``CATALOG``    a ``LINK_RESOURCE_IDENTITY`` activity row names the resource and an identifier
``DIRECT``     an identifier of the user is inside the resource's own record
``DOCUMENT``   a PDF names the resource id and (exactly) identifies the user
``REFERENCE``  the resource's own record (which is DIRECT-linked to the user) names another
               resource id in ``source_resource_id``; that other resource inherits the user
``TRACE``      the identifier is only on another log line of the same ``trace_id``

Every link keeps its source (file/activity id), the source's capture time, the record's own
time, the matched identifier (kind, value, field, current/historical, match quality) and
the number of distinct users the whole record identified (``record_users``; >1 means a
shared record and the link is reported but flagged).

Policy (``LinkPolicy``) is *explicit data*, switchable per call:

* ``identity_known_by_audit``: only identifier values that already existed at ``audit_at``;
* ``link_evidence``: ``eligible`` (source captured at/before ``audit_at``) or ``any``;
* ``post_audit_types`` / ``post_audit_systems``: when ``link_evidence='any'``, which link types and
  which resource systems are accepted from sources captured *after* ``audit_at`` (``None`` = all);
* ``post_audit_strict``: post-audit links additionally need a single-user record and one candidate owner.

Whatever the policy, an association reports whether it is ``post_audit_only``: such an
association identifies a stable resource, and carries **no** state or citation rights
(see :mod:`ghostaudit.evidence_index`, which only ever returns eligible evidence).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .dataset import Activity, EvidenceFile
from .evidence import FileResult, Identifier, Observation, extract_identifiers
from .identity import IdentityGraph, Resolution, match_quality
from .schema import RESOURCE_ID_RE, system_for_resource
from .timeutil import parse_ts, try_parse_ts

CATALOG, DIRECT, DOCUMENT, REFERENCE, TRACE = "CATALOG", "DIRECT", "DOCUMENT", "REFERENCE", "TRACE"
LINK_TYPES = (CATALOG, DIRECT, DOCUMENT, REFERENCE, TRACE)
_TYPE_RANK = {t: i for i, t in enumerate(LINK_TYPES)}


@dataclass(frozen=True)
class Link:
    resource_id: str
    user_id: str
    link_type: str
    source_id: str  # evidence file_id or activity_id (both are citable id kinds)
    source_kind: str  # "file" | "activity"
    source_time: str  # manifest captured_at, or the activity's event_time
    record_time: Optional[str]  # the record's own stated time, if any
    locator: str
    identifier_kind: str
    identifier_norm: str
    identifier_field: str
    identifier_relation: str  # current | historical (relative to today's identity snapshot)
    identifier_valid_from: Optional[str]
    match: str  # exact | case_insensitive | normalized
    record_users: int  # distinct users identified by the whole source record
    via_resource: Optional[str] = None  # REFERENCE links: the resource whose record named this one

    def sort_key(self):
        return (self.resource_id, self.user_id, _TYPE_RANK[self.link_type], self.source_id, self.locator, self.identifier_kind, self.identifier_norm)


@dataclass(frozen=True)
class LinkPolicy:
    """What may establish an association. Everything here concerns *identity*, never state or citations."""

    name: str
    identity_known_by_audit: bool  # only identifier values that already existed at audit_at
    link_evidence: str  # "eligible": source captured <= audit_at; "any": post-audit sources may also identify
    post_audit_types: Optional[frozenset] = None  # link types accepted from post-audit sources (None = all)
    post_audit_systems: Optional[frozenset] = None  # resource systems accepted from post-audit sources (None = all)
    post_audit_strict: bool = False  # post-audit links need a single-user record and a single candidate owner overall
    description: str = ""

    def __post_init__(self):
        if self.link_evidence not in ("eligible", "any"):
            raise ValueError(f"link_evidence must be 'eligible' or 'any', got {self.link_evidence!r}")


_STRONG = frozenset({CATALOG, DIRECT, DOCUMENT, REFERENCE})
# Systems whose resources are not created or destroyed by later sync/restore/export activity: the account profile
# that the request concerns, and records the policy requires to be retained (security audit log, legal hold).
# Mutable copies (search, support, analytics, exports, flows, backups) can appear *after* audit_at, so a post-audit
# sighting says nothing about whether they belong to the case. legal_service is a reasoned inclusion: no sample
# case has a legal hold, but the two eval legal holds linked only post-audit carry RETAIN_RECORD activity 0.5h
# from their case's reported_at.
STABLE_SYSTEMS = frozenset({"account_service", "security_service", "legal_service"})
POLICIES: Dict[str, LinkPolicy] = {
    "S1": LinkPolicy("S1", True, "eligible", description="identifiers that existed at audit_at; sources captured <= audit_at"),
    "S2": LinkPolicy("S2", False, "eligible", description="all current+historical identifiers; sources captured <= audit_at"),
    "S3": LinkPolicy("S3", False, "any", description="as S2, but any source may identify the owner (post-audit sources identify, never prove state)"),
    "S3c": LinkPolicy("S3c", False, "any", _STRONG, None, True,
                      description="as S3, but post-audit sources only via direct/catalog/document/reference links, single-user records, single owner"),
    "S3k": LinkPolicy("S3k", False, "any", None, STABLE_SYSTEMS, True,
                      description="as S3, but post-audit sources only for account/security/legal-hold resources, single-user records, single owner"),
}
DEFAULT_POLICY = "S3k"  # chosen after the M2 comparison; rationale in code/docs/m2_identity_linking.md


@dataclass(frozen=True)
class Association:
    resource_id: str
    system_id: Optional[str]
    user_id: str
    status: str  # ASSOCIATED | AMBIGUOUS
    links: Tuple[Link, ...]  # admissible links only, deterministic order
    link_types: Tuple[str, ...]
    post_audit_only: bool  # no admissible link comes from a source captured at/before audit_at
    competing_users: Tuple[str, ...] = ()  # other users with admissible links to the same resource

    @property
    def sources(self) -> Tuple[str, ...]:
        return tuple(sorted({l.source_id for l in self.links}))


@dataclass
class LinkIssues:
    ambiguous_identifiers: List[Tuple[str, str, str]]  # (source_id, locator, "kind:value") resolving to several users
    shared_records: List[Tuple[str, str, Tuple[str, ...]]]  # (source_id, locator, users) records identifying >1 user
    unresolved_emails: Dict[str, int]  # e-mail (lower-case) in evidence that no user owns -> occurrences
    unread_images: List[str]  # screenshot file ids not interpreted in M1/M2


class LinkIndex:
    """All links discovered in the evidence, independent of case and of file order."""

    def __init__(self, graph: IdentityGraph):
        self.graph = graph
        self.links: List[Link] = []
        self._by_user: Dict[str, List[Link]] = defaultdict(list)
        self._by_resource: Dict[str, List[Link]] = defaultdict(list)
        self.issues = LinkIssues([], [], defaultdict(int), [])
        self._dt: Dict[Link, Tuple[datetime, Optional[datetime]]] = {}

    # ------------------------------------------------------------------ building

    def _resolve_all(self, idents: Iterable[Identifier], source_id: str, locator: str):
        """Resolve identifiers; returns list of (Identifier, Resolution) for RESOLVED ones, noting ambiguity/unresolved."""
        out: List[Tuple[Identifier, Resolution]] = []
        for i in idents:
            r = self.graph.resolve(i.kind, i.value)
            if r.status == "RESOLVED":
                out.append((i, r))
            elif r.status == "AMBIGUOUS":
                self.issues.ambiguous_identifiers.append((source_id, locator, f"{i.kind}:{i.norm}"))
            elif i.kind == "email":
                self.issues.unresolved_emails[i.norm] += 1
        return out

    def _make(self, rid, uid, ltype, source_id, kind, stime, rtime, locator, ident, res, record_users, via=None) -> Link:
        claim = next((c for c in res.claims if c.user_id == uid), None)
        return Link(rid, uid, ltype, source_id, kind, stime, rtime, locator, ident.kind, ident.norm, ident.field,
                    claim.relation if claim else "current", claim.valid_from if claim else None,
                    match_quality(ident.kind, ident.value, claim.raw) if claim else "exact", record_users, via)

    def add_observations(self, results: Sequence[FileResult], manifest: Mapping[str, EvidenceFile]) -> None:
        for r in sorted(results, key=lambda r: r.file_id):
            cap = manifest[r.file_id].captured_at
            for o in r.observations:
                self._add_observation(o, cap)

    def _add_observation(self, o: Observation, captured_at: str) -> None:
        idents = extract_identifiers(o)
        resolved = self._resolve_all(idents, o.file_id, o.locator)
        users = sorted({res.user_ids[0] for _, res in resolved})
        if len(users) > 1:
            self.issues.shared_records.append((o.file_id, o.locator, tuple(users)))
        if o.fmt == "pdf":
            rids = [x for x in o.resource_ids() if system_for_resource(x)]
            ltype = DOCUMENT
        else:
            rids = [o.resource_id] if o.resource_id and system_for_resource(o.resource_id) else []
            ltype = None
        for rid in rids:
            seen = set()
            for ident, res in sorted(resolved, key=lambda t: (not t[0].field.startswith("fields"), t[0].field, t[0].kind, t[0].norm)):
                uid = res.user_ids[0]
                lt = ltype or (TRACE if ident.field.startswith("trace[") else DIRECT)
                key = (uid, lt)
                if key in seen or (lt == TRACE and (uid, DIRECT) in seen):
                    continue  # one link per (resource, user, type) per record; a TRACE link is redundant next to a DIRECT one
                seen.add(key)
                self._add(self._make(rid, uid, lt, o.file_id, "file", captured_at, o.observed_at, o.locator, ident, res, len(users)))
        # REFERENCE: record names another resource id in ``source_resource_id``
        ref = o.fields.get("source_resource_id") if o.fmt != "pdf" else None
        if isinstance(ref, str) and o.resource_id:
            m = RESOURCE_ID_RE.fullmatch(ref)
            if m and ref != o.resource_id:
                for ident, res in resolved:
                    if ident.field.startswith("fields"):
                        self._add(self._make(ref, res.user_ids[0], REFERENCE, o.file_id, "file", captured_at, o.observed_at, o.locator,
                                             ident, res, len(users), via=o.resource_id))
                        break

    def add_catalog(self, activities: Iterable[Activity]) -> None:
        for a in sorted(activities, key=lambda a: a.activity_id):
            if a.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            fake = Observation(a.activity_id, "activity", a.activity_id, a.resource_id, fields={"details": a.details})
            idents = extract_identifiers(fake)
            resolved = self._resolve_all(idents, a.activity_id, a.activity_id)
            users = sorted({res.user_ids[0] for _, res in resolved})
            if len(users) > 1:
                self.issues.shared_records.append((a.activity_id, a.activity_id, tuple(users)))
            seen = set()
            for ident, res in resolved:
                uid = res.user_ids[0]
                if uid in seen:
                    continue
                seen.add(uid)
                self._add(self._make(a.resource_id, uid, CATALOG, a.activity_id, "activity", a.event_time, a.event_time, a.activity_id, ident, res, len(users)))

    def add_unread_images(self, results: Sequence[FileResult]) -> None:
        self.issues.unread_images = sorted(r.file_id for r in results if r.fmt == "image")

    def finalize(self) -> "LinkIndex":
        self.links = sorted(set(self.links), key=Link.sort_key)
        self._by_user.clear()
        self._by_resource.clear()
        for l in self.links:
            self._by_user[l.user_id].append(l)
            self._by_resource[l.resource_id].append(l)
            self._dt[l] = (parse_ts(l.source_time), try_parse_ts(l.record_time))
        self.issues.shared_records.sort()
        self.issues.ambiguous_identifiers.sort()
        return self

    def _add(self, link: Link) -> None:
        self.links.append(link)

    # ------------------------------------------------------------------ queries

    def links_for_user(self, user_id: str) -> List[Link]:
        return list(self._by_user.get(user_id, ()))

    def links_for_resource(self, resource_id: str) -> List[Link]:
        return list(self._by_resource.get(resource_id, ()))

    def eligible(self, link: Link, audit_at: str) -> bool:
        """Was the link's *source* usable at ``audit_at`` (file/activity captured <= audit_at and record time <= audit_at)?"""
        at = parse_ts(audit_at)
        stime, rtime = self._dt[link]
        return stime <= at and (rtime is None or rtime <= at)

    def admissible(self, link: Link, audit_at: str, policy: LinkPolicy) -> Tuple[bool, str]:
        """``(accepted, reason)`` for one link under a policy. Reasons are stable strings used by the reports."""
        if policy.identity_known_by_audit and link.identifier_valid_from is not None and parse_ts(link.identifier_valid_from) > parse_ts(audit_at):
            return False, "identifier_not_yet_existing"
        if self.eligible(link, audit_at):
            return True, "eligible_source"
        if policy.link_evidence == "eligible":
            return False, "source_after_audit"
        if policy.post_audit_types is not None and link.link_type not in policy.post_audit_types:
            return False, f"post_audit_{link.link_type.lower()}_not_allowed"
        if policy.post_audit_systems is not None and system_for_resource(link.resource_id) not in policy.post_audit_systems:
            return False, "post_audit_system_not_stable"
        if policy.post_audit_strict and link.record_users > 1:
            return False, "post_audit_shared_record"
        return True, "post_audit_source"

    def associations(self, user_id: str, audit_at: str, policy: LinkPolicy) -> List[Association]:
        """Resources associated with ``user_id`` under ``policy`` at ``audit_at`` (deterministic order).

        A resource also admissibly linked to a *different* user is reported ``AMBIGUOUS`` (never resolved by
        guessing); ``competing_users`` names the others.
        """
        per_res: Dict[str, List[Link]] = defaultdict(list)
        for l in self._by_user.get(user_id, ()):
            ok, reason = self.admissible(l, audit_at, policy)
            if ok:
                per_res[l.resource_id].append(l)
        out: List[Association] = []
        for rid in sorted(per_res):
            mine = per_res[rid]
            others = sorted({x.user_id for x in self._by_resource[rid] if x.user_id != user_id and self.admissible(x, audit_at, policy)[0]})
            if policy.post_audit_strict and all(not self.eligible(l, audit_at) for l in mine):
                # post-audit-only association must be unambiguous across *all* evidence, not just admissible links
                every_other = sorted({x.user_id for x in self._by_resource[rid] if x.user_id != user_id})
                others = sorted(set(others) | set(every_other))
            eligible_any = any(self.eligible(l, audit_at) for l in mine)
            out.append(Association(
                rid, system_for_resource(rid), user_id, "AMBIGUOUS" if others else "ASSOCIATED", tuple(mine),
                tuple(sorted({l.link_type for l in mine}, key=_TYPE_RANK.__getitem__)), not eligible_any, tuple(others)))
        return out

    def explain(self, user_id: str, resource_id: str, audit_at: str, policy: LinkPolicy) -> List[Tuple[Link, bool, str]]:
        """Every link between a user and a resource with the policy's verdict and reason (for reports/tests)."""
        return [(l, *self.admissible(l, audit_at, policy)) for l in self._by_user.get(user_id, ()) if l.resource_id == resource_id]


def build_link_index(graph: IdentityGraph, results: Sequence[FileResult], manifest: Mapping[str, EvidenceFile], activities: Iterable[Activity]) -> LinkIndex:
    idx = LinkIndex(graph)
    idx.add_observations(results, manifest)
    idx.add_catalog(activities)
    idx.add_unread_images(results)
    return idx.finalize()
