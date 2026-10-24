"""Subject-scoped snapshot absence: a weaker, separately labelled way to support "this subject has no account profile".

Why it exists
-------------
Only a record that *names* the resource (404 / NOT_FOUND / ``rows=0`` / ``profile: null``) is an explicit negative, and
``state.interpret`` produces ``ABSENT`` from nothing else. But a lot of the data are snapshot bundles. Non-listing in a
snapshot is *not* proof in general: measured, 7 of 9 accounts that other evidence shows present near a profile snapshot are
missing from it, because these snapshots are partial extracts.

What can still be said
----------------------
If one eligible snapshot (a) declares an account-profile collection, (b) lists at least one record linked to *this subject*
in another collection (so it demonstrably enumerates this subject's data), and (c) lists no profile for the subject, then
the snapshot supports "the subject has no profile at that capture time". It is scoped to the subject by (b), not to the
whole table. Properties, all enforced here:

* the snapshot must be usable at ``audit_at`` (captured at or before it, inclusive); later snapshots can neither prove
  nor disprove, which is exactly how a later "account is back" bundle is kept out of the historical state;
* the subject's records in the snapshot are returned so the claim is auditable;
* confidence is MEDIUM and the requirement is labelled ``scoped_snapshot``, never ``explicit``;
* an explicit reading of the account (present or absent) always takes precedence; this is used only when the account has
  no usable reading, so it can never override or contradict explicit evidence;
* a table that lists only accounts (``evidence_54``) cannot satisfy (b) and gives no support.

Reliability check (``code/analysis/m4_evaluation.py``): 179 (snapshot, subject) pairs satisfy (a)-(c); no independent
explicit PRESENT reading exists *before* any of them; the 5 pairs with a PRESENT reading nearby all have it *after* the
snapshot and, in 2 of them, an explicit ABSENT reading in between, i.e. a recreation after the audit, the trap the
audit-time rule exists for. The test has low power (5 pairs), which is why the tier is MEDIUM and switchable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .dataset import EvidenceFile
from .evidence import FileResult
from .linker import LinkIndex
from .timeutil import parse_ts

ACCOUNT_COLLECTION_PREFIX = "customer_profiles"


@dataclass(frozen=True)
class ScopedAbsence:
    file_id: str
    captured_at: str
    containers: Tuple[str, ...]  # the account-profile collections the snapshot declares
    subject_records: Tuple[str, ...]  # the subject's records the snapshot lists elsewhere (proof that it covers the subject)

    def describe(self) -> str:
        return (f"{self.file_id} (captured {self.captured_at}) declares {', '.join(self.containers)} and lists the subject's "
                f"{', '.join(self.subject_records[:4])}{'...' if len(self.subject_records) > 4 else ''} but no profile for the subject")


class SnapshotCatalog:
    """Which files declare an account-profile collection (computed once)."""

    def __init__(self, results: Sequence[FileResult], manifest: Mapping[str, EvidenceFile]):
        self.manifest = manifest
        self.declares: Dict[str, Tuple[str, ...]] = {}
        for r in results:
            names = tuple(sorted({c.name for c in r.containers if c.name.startswith(ACCOUNT_COLLECTION_PREFIX)}))
            if names:
                self.declares[r.file_id] = names


def find_scoped_absence(catalog: SnapshotCatalog, links: LinkIndex, user_id: str, audit_at: str) -> Optional[ScopedAbsence]:
    """The latest usable subject-scoped snapshot absence for ``user_id`` at ``audit_at``, or ``None``."""
    by_file: Dict[str, List[str]] = {}
    listed_profile = set()
    for l in links.links_for_user(user_id):
        if l.source_kind != "file" or l.source_id not in catalog.declares or not links.eligible(l, audit_at):
            continue
        if l.resource_id.startswith("account_"):
            listed_profile.add(l.source_id)
        else:
            by_file.setdefault(l.source_id, []).append(l.resource_id)
    best: Optional[ScopedAbsence] = None
    for fid, recs in by_file.items():
        if fid in listed_profile:
            continue
        cap = catalog.manifest[fid].captured_at
        cand = ScopedAbsence(fid, cap, catalog.declares[fid], tuple(sorted(set(recs))))
        if best is None or (parse_ts(cap), fid) > (parse_ts(best.captured_at), best.file_id):
            best = cand
    return best
