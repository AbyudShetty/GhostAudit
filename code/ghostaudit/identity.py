"""Identity graph: which identifiers belong to which user, with provenance and validity windows.

Built only from ``user_identities.csv`` (current snapshot) and ``user_audit_log.csv``
(historical changes). It answers *"whose identifier is this?"* and nothing else:
no resource, state, policy or status logic lives here.

Model
-----
An :class:`IdentityClaim` says "identifier ``norm`` of ``kind`` belongs to ``user_id``"
and records where that was learned (``source``), whether the value is the user's
``current`` one or a ``historical`` one, and the window in which the value was held
(``valid_from`` / ``valid_until``; ``None`` means *unknown / still open*, never guessed).

Identifier kinds: ``email``, ``email_hash``, ``phone``, ``user_ref`` (the stable
``user_id``). Names are **not** identifiers: they are not unique (3 collisions in the
data), so they are kept only for diagnostics and are never matched.

Matching is exact after a fixed normalisation (lower-case e-mail/hash, digits-only
phone). There is no fuzzy or approximate matching, by design: near-miss addresses such
as ``g.silva76.jr@gmail.com`` vs ``g.silva76@gmail.com`` are different identifiers.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .dataset import AuditLogEntry, Dataset, Identity
from .timeutil import parse_ts

KINDS = ("email", "email_hash", "phone", "user_ref")
_FIELD_TO_KIND = {"email": "email", "email_hash": "email_hash", "phone": "phone"}


def normalise(kind: str, value: str) -> str:
    """Canonical join form of an identifier (never fuzzy)."""
    v = value.strip()
    if kind == "phone":
        return "+" + re.sub(r"\D", "", v)
    return v.lower()


def match_quality(kind: str, query: str, stored_raw: str) -> str:
    """How ``query`` matched a stored value: ``exact``, ``case_insensitive`` or ``normalized``."""
    if query == stored_raw:
        return "exact"
    if kind in ("email", "email_hash", "user_ref") and query.strip() == stored_raw:
        return "normalized"
    if kind in ("email", "email_hash", "user_ref") and query.lower() == stored_raw.lower():
        return "case_insensitive"
    return "normalized"


@dataclass(frozen=True)
class IdentityClaim:
    kind: str
    norm: str
    user_id: str
    relation: str  # "current" | "historical"
    raw: str  # value exactly as stored in the source file
    source: str  # "user_identities.csv:user_43" | "user_audit_log.csv:audit_12"
    valid_from: Optional[str] = None  # when the user started holding this value (None = unknown)
    valid_until: Optional[str] = None  # when it was replaced (None = still held / unknown)

    def known_by(self, at: str) -> bool:
        """True when this value already existed at ``at`` (``valid_from <= at``), whether or not it was since replaced.

        This is the time gate for *identity resolution*: a historical e-mail replaced long
        before ``at`` still identifies its user (old systems keep old values), whereas a
        value that only came into being after ``at`` could not have been known then.
        """
        return self.valid_from is None or parse_ts(self.valid_from) <= parse_ts(at)

    def active_at(self, at: str) -> bool:
        """True when the user *held* this value at ``at`` (``valid_from <= at < valid_until``). Diagnostic."""
        t = parse_ts(at)
        if self.valid_from is not None and parse_ts(self.valid_from) > t:
            return False
        if self.valid_until is not None and parse_ts(self.valid_until) <= t:
            return False
        return True


@dataclass(frozen=True)
class IdentityIssue:
    code: str  # cross_user_claim | chain_break | snapshot_mismatch | hash_mismatch | unknown_user
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True)
class Resolution:
    kind: str
    query: str
    status: str  # RESOLVED | AMBIGUOUS | UNRESOLVED
    user_ids: Tuple[str, ...] = ()
    claims: Tuple[IdentityClaim, ...] = ()
    match: str = "none"  # best match quality over the matching claims
    relation: str = "none"  # current | historical | mixed | none
    reason: str = ""

    @property
    def user_id(self) -> Optional[str]:
        return self.user_ids[0] if self.status == "RESOLVED" else None


_MATCH_RANK = {"exact": 0, "normalized": 1, "case_insensitive": 2}


class IdentityGraph:
    def __init__(self, identities: Sequence[Identity], audit_log: Sequence[AuditLogEntry]):
        self.issues: List[IdentityIssue] = []
        self._by_key: Dict[Tuple[str, str], List[IdentityClaim]] = defaultdict(list)
        self._by_user: Dict[str, List[IdentityClaim]] = defaultdict(list)
        self._names: Dict[str, str] = {}
        self._users = {i.user_id: i for i in identities}
        events: Dict[str, List[AuditLogEntry]] = defaultdict(list)
        for a in audit_log:
            if a.user_id not in self._users:
                self.issues.append(IdentityIssue("unknown_user", f"{a.audit_id} refers to unknown {a.user_id}"))
                continue
            events[a.user_id].append(a)
        for uid in sorted(self._users):
            ident = self._users[uid]
            self._names[uid] = ident.name
            self._add(IdentityClaim("user_ref", normalise("user_ref", uid), uid, "current", uid, f"user_identities.csv:{uid}"))
            snapshot = {"email": ident.email, "email_hash": ident.email_hash, "phone": ident.phone}
            for field_name, kind in _FIELD_TO_KIND.items():
                chain = sorted((e for e in events[uid] if e.field_name == field_name), key=lambda e: (parse_ts(e.event_time), e.audit_id))
                self._add_chain(uid, kind, field_name, snapshot[field_name], chain)
            for e in events[uid]:
                if e.field_name not in _FIELD_TO_KIND:
                    self.issues.append(IdentityIssue("unknown_field", f"{e.audit_id}: unsupported field_name {e.field_name!r}"))
            if hashlib.sha256(ident.email.encode("utf-8")).hexdigest() != ident.email_hash:
                self.issues.append(IdentityIssue("hash_mismatch", f"{uid}: email_hash is not sha256(email)"))
        for key, claims in self._by_key.items():
            users = sorted({c.user_id for c in claims})
            if len(users) > 1:
                self.issues.append(IdentityIssue("cross_user_claim", f"{key[0]} {key[1]!r} claimed by {users}"))

    # ------------------------------------------------------------------ construction

    def _add(self, claim: IdentityClaim) -> None:
        self._by_key[(claim.kind, claim.norm)].append(claim)
        self._by_user[claim.user_id].append(claim)

    def _add_chain(self, uid: str, kind: str, field_name: str, snapshot_value: str, chain: List[AuditLogEntry]) -> None:
        if not chain:
            self._add(IdentityClaim(kind, normalise(kind, snapshot_value), uid, "current", snapshot_value, f"user_identities.csv:{uid}"))
            return
        for a, b in zip(chain, chain[1:]):
            if a.new_value != b.old_value:
                self.issues.append(IdentityIssue("chain_break", f"{uid}.{field_name}: {a.audit_id}.new != {b.audit_id}.old"))
        if chain[-1].new_value != snapshot_value:
            self.issues.append(IdentityIssue("snapshot_mismatch", f"{uid}.{field_name}: last change {chain[-1].audit_id} != current snapshot"))
        first = chain[0]
        self._add(IdentityClaim(kind, normalise(kind, first.old_value), uid, "historical", first.old_value,
                                f"user_audit_log.csv:{first.audit_id}", None, first.event_time))
        for i, e in enumerate(chain):
            last = i == len(chain) - 1
            until = None if last else chain[i + 1].event_time
            self._add(IdentityClaim(kind, normalise(kind, e.new_value), uid, "current" if last else "historical", e.new_value,
                                    f"user_audit_log.csv:{e.audit_id}", e.event_time, until))

    # ------------------------------------------------------------------ queries

    def users(self) -> List[str]:
        return sorted(self._users)

    def name_of(self, user_id: str) -> str:
        return self._names[user_id]

    def name_collisions(self) -> Dict[str, List[str]]:
        """Names shared by several users. Diagnostic only: names are never used for linking."""
        by: Dict[str, List[str]] = defaultdict(list)
        for u, n in self._names.items():
            by[n].append(u)
        return {n: sorted(us) for n, us in sorted(by.items()) if len(us) > 1}

    def claims_of(self, user_id: str, known_by: Optional[str] = None) -> List[IdentityClaim]:
        """All claims of a user (deterministic order); with ``known_by`` only values that existed by then."""
        cl = self._by_user.get(user_id, [])
        if known_by is not None:
            cl = [c for c in cl if c.known_by(known_by)]
        return sorted(cl, key=lambda c: (c.kind, c.norm, c.source))

    def claims_for(self, kind: str, value: str) -> List[IdentityClaim]:
        return list(self._by_key.get((kind, normalise(kind, value)), ()))

    def resolve(self, kind: str, value: str, known_by: Optional[str] = None) -> Resolution:
        """Map an identifier to its owner.

        ``AMBIGUOUS`` when several users claim it (never silently picks one);
        ``UNRESOLVED`` when nobody claims it, or when ``known_by`` is given and the value
        only came into existence after that time (the reason says which).
        """
        if kind not in KINDS:
            return Resolution(kind, value, "UNRESOLVED", reason=f"unsupported identifier kind {kind!r}")
        all_claims = self.claims_for(kind, value)
        if not all_claims:
            return Resolution(kind, value, "UNRESOLVED", reason="no such identifier")
        claims = [c for c in all_claims if known_by is None or c.known_by(known_by)]
        if not claims:
            return Resolution(kind, value, "UNRESOLVED", claims=tuple(all_claims), reason=f"identifier only came into existence after {known_by}")
        users = tuple(sorted({c.user_id for c in claims}))
        rels = {c.relation for c in claims}
        relation = rels.pop() if len(rels) == 1 else "mixed"
        match = min((match_quality(kind, value, c.raw) for c in claims), key=_MATCH_RANK.__getitem__)
        status = "RESOLVED" if len(users) == 1 else "AMBIGUOUS"
        return Resolution(kind, value, status, users, tuple(sorted(claims, key=lambda c: (c.user_id, c.source))), match, relation,
                          "" if status == "RESOLVED" else f"claimed by {len(users)} users")

    def resolve_request_email(self, email: str, known_by: Optional[str] = None) -> Resolution:
        """Resolve a request e-mail (current or historical form, any letter case)."""
        return self.resolve("email", email, known_by)

    def find_users_by_any(self, identifiers: Iterable[Tuple[str, str]]) -> Dict[str, List[Resolution]]:
        """Resolve many ``(kind, value)`` pairs; group resolutions by user. Ambiguous ones are grouped under ``"*ambiguous*"``."""
        out: Dict[str, List[Resolution]] = defaultdict(list)
        for kind, value in identifiers:
            r = self.resolve(kind, value)
            if r.status == "RESOLVED":
                out[r.user_ids[0]].append(r)
            elif r.status == "AMBIGUOUS":
                out["*ambiguous*"].append(r)
        return dict(out)


def build_identity_graph(ds: Dataset) -> IdentityGraph:
    return IdentityGraph(ds.identities.rows, ds.audit_log.rows)
