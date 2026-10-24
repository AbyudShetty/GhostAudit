"""Small builders for synthetic Observations / FileResults / manifests / activities used by the M3 tests."""

from ghostaudit.dataset import Activity, DeletionRequest, EvidenceFile
from ghostaudit.evidence import FileResult, Observation


def obs(file_id, rid, fields=None, *, fmt="json", collection=None, copy=None, observed_at=None, trace=None, locator=None):
    return Observation(file_id, fmt, locator or f"$.{rid}", rid, collection=collection, copy=copy, observed_at=observed_at,
                       fields=dict(fields or {}), trace_events=list(trace or []), raw="")


def log(file_id, rid, event, fields=None, observed_at=None, trace=None):
    f = {"event": event, **(fields or {})}
    return obs(file_id, rid, f, fmt="log", collection=event, observed_at=observed_at, trace=trace, locator=f"line:{event}:{rid}")


def fr(file_id, observations, fmt="json"):
    r = FileResult(file_id, f"{file_id}.x", fmt, None)
    r.observations = list(observations)
    return r


def manifest(**captured):
    return {fid: EvidenceFile(fid, f"{fid}.x", "t", ts) for fid, ts in captured.items()}


def worker(aid, rid, t, result="SUCCESS", op="DELETE_RECORD", target=None):
    target = target or {"account": "account_service", "search": "search_service", "support": "support_platform", "analytics": "analytics_platform",
                        "data_flow": "integration_service", "backup": "backup_service", "export": "export_service"}[rid.rsplit("_", 1)[0]]
    return Activity(aid, t, op, "deletion_worker", target, rid, result, f"Deletion worker submitted {rid}.")


def case(cid, email, requested, reported, audit, status="COMPLETE"):
    return DeletionRequest(cid, email, requested, status, reported, audit, "delete my account")
