"""Eligible-evidence lookup: what may be used as *state evidence* or *citation* at ``audit_at``.

Deliberately separate from :mod:`ghostaudit.linker`. The linker may use a later file to learn that
a resource belongs to a user (identity); this index never returns anything captured after
``audit_at``, so a post-audit association can never become admissible state or a citation.

M2 provides only the lookup. Interpreting the observations (present/absent/exposed ...) is M3.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Mapping, Sequence

from .dataset import Activity, EvidenceFile
from .evidence import FileResult, Observation
from .timeutil import observation_usable


class EvidenceIndex:
    def __init__(self, results: Sequence[FileResult], manifest: Mapping[str, EvidenceFile], activities: Sequence[Activity] = ()):
        self._by_resource: Dict[str, List[Observation]] = defaultdict(list)
        self._captured = {fid: ev.captured_at for fid, ev in manifest.items()}
        self._activity: Dict[str, List[Activity]] = defaultdict(list)
        for r in sorted(results, key=lambda r: r.file_id):
            for o in r.observations:
                for rid in o.resource_ids():
                    self._by_resource[rid].append(o)
        for a in activities:
            self._activity[a.resource_id].append(a)

    def observations(self, resource_id: str, audit_at: str) -> List[Observation]:
        """Observations mentioning the resource whose file *and* record time are usable at ``audit_at``."""
        return [o for o in self._by_resource.get(resource_id, ())
                if observation_usable(self._captured[o.file_id], o.observed_at, audit_at)]

    def citable_ids(self, resource_id: str, audit_at: str) -> List[str]:
        """File ids that could legitimately be cited for the resource at ``audit_at`` (sorted, de-duplicated)."""
        return sorted({o.file_id for o in self.observations(resource_id, audit_at)})

    def activity(self, resource_id: str, audit_at: str) -> List[Activity]:
        """Workflow/activity rows for the resource up to ``audit_at``. These are *claims*, not proof of state."""
        return sorted((a for a in self._activity.get(resource_id, ()) if a.event_time <= audit_at), key=lambda a: (a.event_time, a.activity_id))
