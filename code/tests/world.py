"""A tiny synthetic dataset builder for classification tests (temp directory, real CSV/log/JSON files, real pipeline)."""

import atexit
import csv
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from ghostaudit.classification import ClassificationPolicy, classify_all
from ghostaudit.dataset import load_dataset
from ghostaudit.reconstruction import CaseReconstructor

H = lambda e: hashlib.sha256(e.encode()).hexdigest()  # noqa: E731
REQ, REP, AUD = "2026-08-20T00:00:00Z", "2026-08-22T12:00:00Z", "2026-09-15T12:00:00Z"
PRE, AT, POST = "2026-09-15T11:00:00Z", AUD, "2026-09-16T11:00:00Z"
RULE_HEADER = ["rule_id", "data_category", "condition", "required_action"]


def rule_rows():
    """A deletion_rules.csv that agrees with the program (every synthetic dataset needs one; see state.check_rules)."""
    from ghostaudit.state import RULE_ACTIONS

    return [[f"rule_{i:02d}", c, "Always", a] for i, (c, a) in enumerate(RULE_ACTIONS.items(), start=1)]


ACT_HEADER = ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id", "reported_result", "details"]


def lf(ts, service, event, rid=None, **kv):
    """One logfmt line: ts level=INFO service=S [resource_id=R] event=E k="v" ..."""
    parts = [ts, "level=INFO", f"service={service}"]
    if "trace" in kv:
        parts.append(f"trace_id={kv.pop('trace')}")
    if rid:
        parts.append(f"resource_id={rid}")
    parts.append(f"event={event}")
    parts += [f'{k}="{v}"' for k, v in kv.items()]
    return " ".join(parts)


def lookup(ts, rid, email, rows=0, trace="tr-1"):
    """Account lookup by e-mail: started line (subject key) + completed line (resource, rows)."""
    return [lf(ts, "account_service", "profile_lookup_started", trace=trace, lookup_key=email),
            lf(ts, "account_service", "profile_lookup_completed", rid, trace=trace, rows=rows, http_status=200 if rows else 404)]


def audit_line(ts, rid, user, retention="2031-12-31T23:59:59Z", rows=1):
    return lf(ts, "security_service", "audit_record_read_completed", rid, rows=rows, subject_reference=user, retention_until=retention, event_code="ACCOUNT_DELETION_RECORDED")


class World:
    def __init__(self, users=(("user_1", "ann@x.com"), ("user_2", "bob@x.com"))):
        self.users, self.cases, self.files, self.acts = list(users), [], {}, []

    def case(self, cid, email="ann@x.com", requested=REQ, reported=REP, audit=AUD, status="COMPLETE", text="Please delete my account and personal data."):
        self.cases.append([cid, email, requested, status, reported, audit, text])
        return self

    def log(self, fid, captured, lines):
        self.files[fid] = (f"evidence/logs/{fid}.log", captured, "\n".join(lines) + "\n")
        return self

    def json(self, fid, captured, doc):
        self.files[fid] = (f"evidence/records/{fid}.json", captured, json.dumps(doc))
        return self

    def worker(self, aid, rid, ts, result="SUCCESS", event="DELETE_RECORD", target="account_service"):
        self.acts.append([aid, ts, event, "deletion_worker", target, rid, result, f"worker {rid}"])
        return self

    def build(self, policy=ClassificationPolicy()):
        self._dir = tempfile.mkdtemp(prefix="ghostaudit_test_")
        atexit.register(shutil.rmtree, self._dir, True)
        root = Path(self._dir)
        (root / "evidence/logs").mkdir(parents=True)
        (root / "evidence/records").mkdir(parents=True)

        def w(name, header, rows):
            with open(root / name, "w", encoding="utf-8", newline="") as fh:
                c = csv.writer(fh)
                c.writerow(header)
                c.writerows(rows)

        w("user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"], [[u, u, e, "+1 " + u, H(e), "2026-01-01T00:00:00Z"] for u, e in self.users])
        w("user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
        hdr = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
        w("deletion_requests.csv", hdr, self.cases)
        w("sample_deletion_requests.csv", hdr, [])
        w("sample_output.csv", ["case_id", "deletion_status", "steps_json"], [])
        w("deletion_rules.csv", RULE_HEADER, rule_rows())
        w("system_activity.csv", ACT_HEADER, self.acts)
        w("evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], [[f, p, "o", c] for f, (p, c, _) in self.files.items()])
        for f, (p, c, body) in self.files.items():
            (root / p).write_text(body, encoding="utf-8", newline="\n")
        self.ds = load_dataset(root)
        assert self.ds.issues() == [], self.ds.issues()
        self.cr = CaseReconstructor(self.ds, use_screenshots=False)
        self.policy = policy
        return self

    def verdicts(self, policy=None):
        return classify_all(self.cr, self.ds.requests.rows, policy or self.policy)

    def verdict(self, cid="case_1", policy=None):
        return self.verdicts(policy)[cid]

    def close(self):
        shutil.rmtree(self._dir, ignore_errors=True)


def clean_world(extra_lines=(), email="ann@x.com"):
    """A case whose account is content-linked and explicitly absent, with the retained audit record present: COMPLETE, proven."""
    w = World().case("case_1", email)
    w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
    w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", email) + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")] + list(extra_lines))
    return w
