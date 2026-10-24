"""State reconstruction: what each resource looked like *as of* ``audit_at``, and why.

Pipeline position (M3)::

    parsers -> Observation  --interpret-->  Reading  --reconstruct(as_of)-->  ResourceState
                                   (what one record says)      (what we conclude, with provenance)

* ``interpret`` is a table of record shapes -> one *Reading* (category + state + the rule used). It never
  looks at the audit time and never at other records.
* ``StateReconstructor.reconstruct`` applies the time rule. Only readings from sources usable at ``audit_at``
  (file captured <= audit_at **and** record time <= audit_at) can decide the state or be cited; every other
  reading of the same resource is kept in ``excluded`` with the reason, so the answer to "why was X
  excluded?" is always on the object. Later evidence never changes ``state``.
* ``assess`` maps (category, state) to the policy verdict. It is a separate function on purpose: reading a
  record, reconstructing a time slice and judging policy are different questions.

States: PRESENT, ABSENT, IDENTIFIABLE (record exists and exposes a direct identifier), DEIDENTIFIED (record
exists with no direct identifiers: redacted / anonymized / tombstone), ACTIVE, DISABLED (data flows),
RESTORE_ALLOWED, RESTORE_BLOCKED (backups), UNKNOWN.

Activity rows are claims, not evidence of state: they never enter ``state``. They are only surfaced as
``claims_after_evidence`` so callers can see where a workflow claim post-dates the last real observation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .dataset import Activity, EvidenceFile
from .evidence import EMAIL_RE, PHONE_RE, FileResult, Observation, is_null
from .evidence_index import EvidenceIndex
from .schema import resource_prefix
from .timeutil import observation_usable, parse_ts, try_parse_ts

PRESENT, ABSENT, IDENTIFIABLE, DEIDENTIFIED = "PRESENT", "ABSENT", "IDENTIFIABLE", "DEIDENTIFIED"
ACTIVE, DISABLED, RESTORE_ALLOWED, RESTORE_BLOCKED, UNKNOWN = "ACTIVE", "DISABLED", "RESTORE_ALLOWED", "RESTORE_BLOCKED", "UNKNOWN"

# worst case first: when several copies are read at the same instant (primary vs replicas) the most
# problematic one wins, because one surviving copy is enough for the data to still exist.
_SEVERITY = [IDENTIFIABLE, RESTORE_ALLOWED, ACTIVE, PRESENT, UNKNOWN, DEIDENTIFIED, DISABLED, RESTORE_BLOCKED, ABSENT]
_RANK = {s: i for i, s in enumerate(_SEVERITY)}

_PREFIX_CATEGORY = {
    "account": "USER_PROFILE", "search": "SEARCH_INDEX_ENTRY", "support": "SUPPORT_MESSAGE", "analytics": "ANALYTICS_PROFILE",
    "export": "USER_EXPORT", "data_flow": "DATA_FLOW", "backup": "BACKUP_ARCHIVE", "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD", "tombstone": "ACCOUNT_TOMBSTONE", "marketing": "MARKETING_PROFILE",
    "object": "SUPPORT_ATTACHMENT", "cache": "CACHE_ENTRY", "email_delivery": "EMAIL_DELIVERY_LOG",
}
# explicit labels written by evidence records (record_type/result, or TXT category/state)
_LABELS = {
    ("USER_PROFILE", "FOUND"): PRESENT, ("USER_PROFILE", "NOT_FOUND"): ABSENT,
    ("SEARCH_INDEX_ENTRY", "FOUND"): PRESENT, ("SECURITY_AUDIT_LOG", "FOUND"): PRESENT, ("LEGAL_HOLD_RECORD", "FOUND"): PRESENT,
    ("USER_EXPORT", "FOUND"): PRESENT, ("SUPPORT_ATTACHMENT", "FOUND"): PRESENT,
    ("SUPPORT_MESSAGE", "IDENTIFIABLE"): IDENTIFIABLE, ("SUPPORT_MESSAGE", "REDACTED"): DEIDENTIFIED,
    ("ANALYTICS_PROFILE", "IDENTIFIABLE"): IDENTIFIABLE, ("ANALYTICS_PROFILE", "ANONYMIZED"): DEIDENTIFIED,
    ("BACKUP_ARCHIVE", "ACTIVE"): RESTORE_ALLOWED, ("BACKUP_ARCHIVE", "BLOCKED"): RESTORE_BLOCKED,
    ("DATA_FLOW", "ACTIVE"): ACTIVE, ("DATA_FLOW", "DISABLED"): DISABLED,
}
_DIRECT_ID_FIELDS = ("email", "phone", "email_hash", "full_name", "name", "requester_email", "requester_name", "requester_phone",
                     "owner_email", "owner_phone", "customer_email", "user_id", "customer_reference", "display_name")


@dataclass(frozen=True)
class Reading:
    """What one observation says about one resource."""

    resource_id: str
    category: str
    state: str
    basis: str  # the interpretation rule, e.g. "log.profile_lookup_completed.rows"
    exposed: Tuple[str, ...] = ()  # direct-identifier fields that carried a value
    detail: Tuple[Tuple[str, Any], ...] = ()


def _flat(o: Observation) -> Dict[str, Any]:
    f = dict(o.fields)
    rec = f.pop("record", None)
    if isinstance(rec, dict):
        f.update(rec)
    prof = f.get("profile")
    if isinstance(prof, dict):
        for k, v in prof.items():
            f.setdefault(k, v)
    return f


def _truthy(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "0", "no"):
        return False
    return None


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _exposed(f: Mapping[str, Any], names: Sequence[str] = _DIRECT_ID_FIELDS) -> Tuple[str, ...]:
    return tuple(n for n in names if n in f and not is_null(f[n]))


def category_of(resource_id: str) -> str:
    return _PREFIX_CATEGORY.get(resource_prefix(resource_id) or "", "UNKNOWN")


def _mk(rid, category, state, basis, exposed=(), **detail) -> Reading:
    return Reading(rid, category, state, basis, tuple(exposed), tuple(sorted(detail.items())))


def interpret(o: Observation, resource_id: Optional[str] = None) -> Optional[Reading]:
    """Read one observation. ``None`` means the record carries no state (e.g. a ``*_started`` log line)."""
    rid = resource_id or o.resource_id
    prefix = resource_prefix(rid or "")
    cat = _PREFIX_CATEGORY.get(prefix or "", "UNKNOWN")
    f = _flat(o)
    event = f.get("event") if o.fmt == "log" else None
    coll = o.collection if o.fmt != "log" else event

    if o.fmt == "pdf":
        return _pdf(o, rid, cat)
    if o.fmt == "image":
        return _image(o, rid, cat)
    # explicit label records (JSON records/history, log record_lookup, TXT handover)
    label_cat = f.get("record_type") or f.get("category")
    label_val = f.get("result") if "record_type" in f else f.get("state")
    if label_cat and label_val is not None and (("record_type" in f and "result" in f) or o.fmt == "txt"):
        st = _LABELS.get((str(label_cat), str(label_val)))
        if st is None:
            return _mk(rid, str(label_cat), UNKNOWN, "label.unrecognised", label=f"{label_cat}/{label_val}")
        return _mk(rid, str(label_cat), st, "label.record_type_result" if o.fmt != "txt" else "label.txt_category_state")

    if event and event.endswith("_started"):
        return None
    if prefix == "account":
        return _account(o, rid, f, event, coll)
    if prefix == "search":
        if event == "index_query_completed":
            return _mk(rid, cat, PRESENT if str(f.get("result")).lower() == "hit" else ABSENT, "log.index_query_completed.result")
        if coll == "index_queries":
            total = (f.get("hits") or {}).get("total") if isinstance(f.get("hits"), dict) else None
            if total is not None:
                return _mk(rid, cat, PRESENT if _int(total) and _int(total) > 0 else ABSENT, "json.index_queries.hits.total")
            return _mk(rid, cat, PRESENT, "json.index_queries.document")
        if coll in ("customer_search_documents", "search_documents_primary", "documents"):
            return _mk(rid, cat, PRESENT, "record.search_document")
    if prefix == "support":
        return _support(o, rid, f, event, coll)
    if prefix == "analytics":
        return _analytics(o, rid, f, event, coll)
    if prefix == "export":
        avail = _truthy(f.get("available") if event else f.get("download_available"))
        if event in ("export_download_check_completed", "export_metadata_read_completed") or coll in ("customer_export_jobs", "customer_exports", "exports"):
            return _mk(rid, cat, PRESENT, "export.record", exposed=_exposed(f), download_available=avail)
    if prefix == "data_flow":
        return _flow(o, rid, f, event)
    if prefix == "backup":
        return _backup(o, rid, f, event)
    if prefix == "audit_record":
        if event == "audit_record_read_completed":
            n = _int(f.get("rows"))
            return _mk(rid, cat, PRESENT if n and n > 0 else ABSENT, "log.audit_record_read_completed.rows", retention_until=f.get("retention_until"))
        if coll in ("security_audit_events", "security_audit_records", "audit_records"):
            return _mk(rid, cat, PRESENT, "record.security_audit", retention_until=f.get("retention_until"))
    if prefix == "legal_hold" and (coll in ("retained_order_records",) or event == "retained_order_loaded"):
        return _mk(rid, cat, PRESENT, "record.legal_hold", retention_until=f.get("retained_until"))
    if prefix == "tombstone" and coll == "deleted_customer_references":
        ex = _exposed(f, ("name", "email", "phone", "email_hash"))
        return _mk(rid, cat, IDENTIFIABLE if ex else DEIDENTIFIED, "record.tombstone.direct_identifiers", exposed=ex, account_status=f.get("account_status"))
    return _mk(rid, cat, UNKNOWN, "shape.unrecognised", collection=str(coll))


def _account(o, rid, f, event, coll) -> Reading:
    cat = "USER_PROFILE"
    if event == "profile_lookup_completed":
        rows, http = _int(f.get("rows")), _int(f.get("http_status"))
        if rows is not None and http is not None and ((rows > 0) != (http == 200)):
            return _mk(rid, cat, UNKNOWN, "log.profile_lookup_completed.conflict", rows=rows, http_status=http)
        return _mk(rid, cat, PRESENT if (rows or 0) > 0 else ABSENT, "log.profile_lookup_completed.rows", http_status=http)
    if coll == "profile_lookups":
        prof = o.fields.get("profile")
        code = _int(o.fields.get("status_code"))
        if prof is None:
            return _mk(rid, cat, ABSENT, "json.profile_lookups.profile_null", status_code=code)
        return _mk(rid, cat, PRESENT, "json.profile_lookups.profile", exposed=_exposed(prof if isinstance(prof, dict) else {}), status_code=code)
    if coll and coll.startswith("customer_profiles"):
        return _mk(rid, cat, PRESENT, "record.customer_profile", exposed=_exposed(f), account_status=f.get("account_status"))
    return _mk(rid, cat, UNKNOWN, "shape.unrecognised", collection=str(coll))


def _support(o, rid, f, event, coll) -> Optional[Reading]:
    if event == "message_pii_scan":
        return _mk(rid, "SUPPORT_MESSAGE", IDENTIFIABLE, "log.message_pii_scan.matched_value", exposed=("matched_value",))
    if event == "attachment_pii_scan":
        return _mk(rid, "SUPPORT_ATTACHMENT", PRESENT, "log.attachment_pii_scan", exposed=("matched_value",))
    if event == "message_read":
        red = "REDACTED" in str(f.get("message_text", ""))
        return _mk(rid, "SUPPORT_MESSAGE", DEIDENTIFIED if red else IDENTIFIABLE, "log.message_read.message_text")
    if event in ("deletion_marker_check", "thumbnail_failed"):
        return None
    if event == "contact_query_completed":
        n = _int(f.get("rows"))
        return _mk(rid, "SUPPORT_RECORD", PRESENT if (n or 0) > 0 else ABSENT, "log.contact_query_completed.rows")
    if event == "support_ticket_read_completed" or coll in ("tickets", "support_records"):
        ex = _exposed(f, ("requester_email", "requester_name", "requester_phone"))
        return _mk(rid, "SUPPORT_MESSAGE", IDENTIFIABLE if ex else DEIDENTIFIED, "ticket.requester_fields", exposed=ex)
    if coll == "support_conversations":
        text = str(f.get("message_text", ""))
        if "REDACTED" in text:
            return _mk(rid, "SUPPORT_MESSAGE", DEIDENTIFIED, "conversation.message_text.redacted")
        ex = ("message_text",) if (EMAIL_RE.search(text) or PHONE_RE.search(text)) else ()
        return _mk(rid, "SUPPORT_MESSAGE", IDENTIFIABLE if ex else DEIDENTIFIED, "conversation.message_text.identifiers", exposed=ex)
    if coll == "ticket_attachments":
        return _mk(rid, "SUPPORT_ATTACHMENT", PRESENT, "record.ticket_attachment", exposed=_exposed(f, ("uploaded_by",)))
    return _mk(rid, "SUPPORT_MESSAGE", UNKNOWN, "shape.unrecognised", collection=str(coll))


def _analytics(o, rid, f, event, coll) -> Optional[Reading]:
    if event == "contact_lookup_completed":
        n = _int(f.get("rows"))
        return _mk(rid, "MARKETING_PROFILE", PRESENT if (n or 0) > 0 else ABSENT, "log.contact_lookup_completed.rows",
                   exposed=("correlation_key",) if f.get("correlation_key") else ())
    if event in ("analytics_profile_read", "analytics_identity_scan"):
        n = _int(f.get("direct_identifiers"))
        return _mk(rid, "ANALYTICS_PROFILE", IDENTIFIABLE if (n or 0) > 0 else DEIDENTIFIED, f"log.{event}.direct_identifiers")
    if event == "analytics_profile_read_completed" or coll in ("customer_analytics_reports", "customer_analytics_profiles", "profiles"):
        ex = _exposed(f, ("email", "phone", "email_hash", "user_id", "customer_reference"))
        return _mk(rid, "ANALYTICS_PROFILE", IDENTIFIABLE if ex else DEIDENTIFIED, "analytics.identifier_fields", exposed=ex)
    return _mk(rid, "ANALYTICS_PROFILE", UNKNOWN, "shape.unrecognised", collection=str(coll))


def _flow(o, rid, f, event) -> Optional[Reading]:
    en = _truthy(f.get("enabled"))
    if en is None:
        return _mk(rid, "DATA_FLOW", UNKNOWN, "shape.unrecognised")
    stopped = str(f.get("last_delivery_status", "")).lower() == "stopped"
    delivered = str(f.get("last_delivery_status", "")).lower() == "delivered"
    if (en and stopped) or (not en and delivered):
        return _mk(rid, "DATA_FLOW", UNKNOWN, "flow.enabled_conflicts_with_last_delivery", enabled=en, last_delivery_status=f.get("last_delivery_status"))
    return _mk(rid, "DATA_FLOW", ACTIVE if en else DISABLED, "flow.enabled", exposed=_exposed(f, ("customer_email", "subject_selector")))


def _backup(o, rid, f, event) -> Optional[Reading]:
    cat = "BACKUP_ARCHIVE"
    if event == "restore_candidate_evaluated":
        el, blk = _truthy(f.get("eligible")), _truthy(f.get("deletion_block"))
        if el is None or blk is None:
            return _mk(rid, cat, UNKNOWN, "log.restore_candidate_evaluated.missing")
        return _mk(rid, cat, RESTORE_ALLOWED if (el and not blk) else RESTORE_BLOCKED, "log.restore_candidate_evaluated")
    if event == "restore_authorization_completed":
        d = str(f.get("decision", "")).lower()
        return _mk(rid, cat, RESTORE_BLOCKED if d == "deny" else RESTORE_ALLOWED if d == "allow" else UNKNOWN, "log.restore_authorization_completed.decision")
    ra = _truthy(f.get("restore_allowed"))
    if ra is None:
        return _mk(rid, cat, UNKNOWN, "shape.unrecognised")
    return _mk(rid, cat, RESTORE_ALLOWED if ra else RESTORE_BLOCKED, "backup.restore_allowed", exposed=_exposed(f, ("customer_email",)))


def _pdf(o, rid, cat) -> Reading:
    text = o.raw or ""
    if cat in ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT") and re.search(r"attachment", text, re.I):
        return _mk(rid, "SUPPORT_ATTACHMENT", PRESENT, "pdf.attachment_document", exposed=("document_text",))
    return _mk(rid, cat, PRESENT, "pdf.document_mentions_resource")


def _image(o, rid, cat) -> Reading:
    """Screenshot-derived (OCR) observation; fields were produced by :mod:`ghostaudit.screenshots`.

    The meaning of what is on screen is a *hypothesis* (no sample answer uses a screenshot): a record shown in a
    live console exists; identifiers shown are exposed. A stable ``user_N`` reference is not an exposure for
    support records or tombstones (the policy allows it there) but it is for analytics (must be unlinked).
    """
    f = o.fields
    if f.get("conflict"):
        return _mk(rid, cat, UNKNOWN, "screenshot.conflicting_subject", ocr=True, foreign_email_count=f.get("foreign_email_count"))
    vis = tuple(f.get("visible_identifiers") or ())
    direct = tuple(v for v in vis if v != "user_ref")
    status = set(f.get("status_words") or ())
    if cat == "DATA_FLOW":
        st = ACTIVE if {"Continuous", "Healthy"} & status and not {"Disabled", "Paused", "Stopped"} & status else UNKNOWN
        return _mk(rid, cat, st, "screenshot.data_flow.status_words", exposed=vis, ocr=True)
    if cat == "ANALYTICS_PROFILE":
        return _mk(rid, cat, IDENTIFIABLE if vis else PRESENT, "screenshot.identifier_visibility", exposed=vis, ocr=True)
    if cat in ("SUPPORT_MESSAGE", "MARKETING_PROFILE"):
        return _mk(rid, cat, IDENTIFIABLE if direct else PRESENT, "screenshot.identifier_visibility", exposed=direct, ocr=True)
    if cat == "ACCOUNT_TOMBSTONE":
        return _mk(rid, cat, IDENTIFIABLE if direct else (DEIDENTIFIED if "Retained reference" in status else PRESENT),
                   "screenshot.identifier_visibility", exposed=direct, ocr=True)
    if cat == "BACKUP_ARCHIVE":
        return _mk(rid, cat, PRESENT, "screenshot.backup_listed(restore_flag_not_shown)", exposed=vis, ocr=True)
    return _mk(rid, cat, PRESENT, "screenshot.record_visible", exposed=vis, ocr=True)


# --------------------------------------------------------------------------------------------- assessment

@dataclass(frozen=True)
class Assessment:
    verdict: str  # VIOLATION | COMPLIANT | RETAINED_OK | RETAINED_MISSING | UNKNOWN
    finding_op: Optional[str]  # REPORT_* operation for violations
    required_action: str  # DELETE | REDACT | ANONYMIZE | RETAIN | DISABLE | BLOCK_RESTORE | UNKNOWN (deletion_rules.csv vocabulary)
    reason: str


_REQUIRED = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE", "CACHE_ENTRY": "DELETE",
    "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE", "SUPPORT_RECORD": "DELETE", "ANALYTICS_PROFILE": "ANONYMIZE",
    "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE", "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT",
    "SECURITY_AUDIT_LOG": "RETAIN", "LEGAL_HOLD_RECORD": "RETAIN", "ACCOUNT_TOMBSTONE": "REDACT", "DATA_FLOW": "DISABLE",
    "BACKUP_ARCHIVE": "BLOCK_RESTORE",
}


def assess(category: str, state: str) -> Assessment:
    """Policy verdict for a (category, state). ``SUPPORT_RECORD`` (a support item of unspecified type) is treated as a record to delete."""
    action = _REQUIRED.get(category, "UNKNOWN")
    if state == UNKNOWN or action == "UNKNOWN":
        return Assessment("UNKNOWN", None, action, "state or category not determinable")
    if action == "DELETE":
        if state == ABSENT:
            return Assessment("COMPLIANT", None, action, "record is gone")
        return Assessment("VIOLATION", "REPORT_REMAINING_RECORD", action, "record that must be deleted still exists")
    if action in ("REDACT", "ANONYMIZE"):
        if state == IDENTIFIABLE:
            return Assessment("VIOLATION", "REPORT_EXPOSED_PII", action, "record is kept but still exposes identifiers")
        if state == PRESENT:
            return Assessment("UNKNOWN", None, action, "record exists; identifier exposure not determinable")
        return Assessment("COMPLIANT", None, action, "no direct identifiers" if state == DEIDENTIFIED else "record is gone")
    if action == "RETAIN":
        return Assessment("RETAINED_OK" if state == PRESENT else "RETAINED_MISSING", None, action, "retained record present" if state == PRESENT else "retained record missing")
    if action == "DISABLE":
        return (Assessment("VIOLATION", "REPORT_ACTIVE_DATA_FLOW", action, "data flow is active") if state == ACTIVE
                else Assessment("COMPLIANT", None, action, "data flow is disabled"))
    if action == "BLOCK_RESTORE":
        return (Assessment("VIOLATION", "REPORT_RESTORE_RISK", action, "backup can still restore the user's data") if state == RESTORE_ALLOWED
                else Assessment("UNKNOWN", None, action, "restore access not determinable") if state == PRESENT
                else Assessment("COMPLIANT", None, action, "restore is blocked"))
    return Assessment("UNKNOWN", None, action, "unhandled")


# --------------------------------------------------------------------------------------------- reconstruction

@dataclass(frozen=True)
class StateEvidence:
    file_id: str
    locator: str
    state: str
    basis: str
    effective_time: str
    copy: Optional[str]
    eligible: bool
    reason: str  # "usable" | "file_captured_after_audit" | "record_time_after_audit"
    exposed: Tuple[str, ...] = ()
    ocr: bool = False
    fmt: str = ""  # source format of the reading (sql/json/log/txt/pdf/image)
    grounding: Optional[str] = None  # screenshots only: catalog+ocr | catalog_time_only | conflict
    retention_until: Optional[str] = None  # retained records: end of the retention period, if the record states one


@dataclass(frozen=True)
class ResourceState:
    resource_id: str
    category: str
    as_of: str
    state: str  # UNKNOWN when no usable evidence
    basis: str
    deciding: Tuple[StateEvidence, ...]  # the readings that decided ``state`` (latest instant; replicas/copies together)
    history: Tuple[StateEvidence, ...]  # every usable reading, oldest first
    excluded: Tuple[StateEvidence, ...]  # readings from sources not usable at ``as_of`` (never used, never cited)
    notes: Tuple[str, ...] = ()
    claims_after_evidence: Tuple[str, ...] = ()  # workflow rows (<= as_of) newer than the deciding evidence: claims only

    @property
    def citable(self) -> Tuple[str, ...]:
        """File ids that may be cited as proof of this state: the deciding, usable sources. Nothing is citable for an UNKNOWN state."""
        return () if self.state == UNKNOWN else tuple(sorted({e.file_id for e in self.deciding}))

    @property
    def has_evidence(self) -> bool:
        return bool(self.deciding)

    @property
    def assessment(self) -> Assessment:
        return assess(self.category, self.state)


class StateReconstructor:
    def __init__(self, results: Sequence[FileResult], manifest: Mapping[str, EvidenceFile], activities: Sequence[Activity] = ()):
        self.index = EvidenceIndex(results, manifest, activities)
        self._captured = {fid: ev.captured_at for fid, ev in manifest.items()}

    def _effective(self, o: Observation) -> str:
        return o.observed_at if try_parse_ts(o.observed_at) else self._captured[o.file_id]

    def reconstruct(self, resource_id: str, as_of: str) -> ResourceState:
        usable: List[Tuple[Observation, Reading, StateEvidence]] = []
        excluded: List[StateEvidence] = []
        for o in sorted(self.index._by_resource.get(resource_id, ()), key=lambda x: (self._captured[x.file_id], x.file_id, x.locator)):
            rd = interpret(o, resource_id)
            if rd is None:
                continue
            ok = observation_usable(self._captured[o.file_id], o.observed_at, as_of)
            if ok:
                reason = "usable"
            elif parse_ts(self._captured[o.file_id]) > parse_ts(as_of):
                reason = "file_captured_after_audit"
            else:
                reason = "record_time_after_audit"
            det = dict(rd.detail)
            ev = StateEvidence(o.file_id, o.locator, rd.state, rd.basis, self._effective(o), o.copy, ok, reason, rd.exposed, bool(det.get("ocr")),
                               o.fmt, o.fields.get("grounding") if o.fmt == "image" else None, det.get("retention_until"))
            (usable if ok else excluded).append((o, rd, ev) if ok else ev)  # type: ignore[arg-type]
        category = category_of(resource_id)
        notes: List[str] = []
        if not usable:
            note = "no evidence usable at as_of" + (f"; {len(excluded)} later reading(s) ignored" if excluded else "")
            return ResourceState(resource_id, category, as_of, UNKNOWN, "no_usable_evidence", (), (), tuple(excluded), (note,))
        usable.sort(key=lambda t: (parse_ts(t[2].effective_time), t[2].file_id, t[2].locator))
        latest_t = parse_ts(usable[-1][2].effective_time)
        latest = [t for t in usable if parse_ts(t[2].effective_time) == latest_t]
        cats = {t[1].category for t in latest}
        category = sorted(cats)[0] if len(cats) == 1 else category
        states = {t[1].state for t in latest}
        state = min(states, key=_RANK.__getitem__)
        if len(states) > 1:
            notes.append("copies disagree at the latest instant (" + ", ".join(sorted(states)) + "): worst case kept")
        if any(t[1].state == UNKNOWN for t in latest) and state == UNKNOWN:
            notes.append("latest reading could not be interpreted")
        deciding = tuple(t[2] for t in latest if t[1].state == state)
        if any(t[1].category != category for t in latest):
            notes.append("readings disagree on category: " + ", ".join(sorted(cats)))
        stale_claims = tuple(
            f"{a.activity_id} {a.event_type} {a.reported_result} @ {a.event_time}"
            for a in self.index.activity(resource_id, as_of)
            if a.source_system == "deletion_worker" and parse_ts(a.event_time) > latest_t
        )
        if excluded and any(e.state != state for e in excluded):
            notes.append(f"{sum(1 for e in excluded if e.state != state)} later reading(s) with a different state ignored (not usable at as_of)")
        return ResourceState(resource_id, category, as_of, state, deciding[0].basis if deciding else "n/a", deciding,
                             tuple(t[2] for t in usable), tuple(excluded), tuple(notes), stale_claims)
