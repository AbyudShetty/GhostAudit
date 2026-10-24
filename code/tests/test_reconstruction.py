import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import needs_dataset, real_dataset
from tests.world import RULE_HEADER, rule_rows

from ghostaudit.dataset import load_dataset
from ghostaudit.reconstruction import CaseReconstructor
from ghostaudit.scenario import RELEVANT, UNCERTAIN, UNRELATED

H = lambda e: hashlib.sha256(e.encode()).hexdigest()  # noqa: E731


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def build_dataset(root: Path):
    """A miniature dataset with the behaviours M3 must get right (see test names)."""
    (root / "evidence/logs").mkdir(parents=True)
    (root / "evidence/records").mkdir(parents=True)
    write_csv(root / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
              [["user_1", "Ann Lee", "ann@x.com", "+1 1", H("ann@x.com"), "2026-01-01T00:00:00Z"],
               ["user_2", "Bob Roy", "bob@x.com", "+1 2", H("bob@x.com"), "2026-01-01T00:00:00Z"]])
    write_csv(root / "user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
    req = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
    write_csv(root / "deletion_requests.csv", req, [
        ["case_1", "ann@x.com", "2026-08-20T00:00:00Z", "COMPLETE", "2026-08-22T12:00:00Z", "2026-09-15T12:00:00Z", "please delete my account"],
        ["case_2", "ann@x.com", "2026-03-01T00:00:00Z", "COMPLETE", "2026-03-03T12:00:00Z", "2026-03-10T12:00:00Z", "please delete my account"]])
    write_csv(root / "sample_deletion_requests.csv", req, [])
    write_csv(root / "sample_output.csv", ["case_id", "deletion_status", "steps_json"], [])
    write_csv(root / "deletion_rules.csv", RULE_HEADER, rule_rows())
    act = ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id", "reported_result", "details"]
    w = lambda a, t, rid, tgt, res="SUCCESS", ev="DELETE_RECORD": [a, t, ev, "deletion_worker", tgt, rid, res, f"worker {rid}"]  # noqa: E731
    write_csv(root / "system_activity.csv", act, [
        w("activity_1", "2026-08-22T11:00:00Z", "account_10", "account_service"),
        w("activity_2", "2026-08-22T13:00:00Z", "search_1", "search_service", "FAILED"),
        w("activity_3", "2026-03-03T13:00:00Z", "search_900", "search_service", "FAILED"),
        w("activity_4", "2026-03-03T11:00:00Z", "account_20", "account_service")])
    write_csv(root / "evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], [
        ["evidence_1", "evidence/logs/pre.log", "o", "2026-09-15T11:00:00Z"],
        ["evidence_2", "evidence/records/post.json", "o", "2026-09-17T12:00:00Z"],
        ["evidence_3", "evidence/logs/old.log", "o", "2026-03-09T09:00:00Z"]])
    (root / "evidence/logs/pre.log").write_text("\n".join([
        '2026-09-15T10:00:00Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started lookup_key="ann@x.com"',
        "2026-09-15T10:00:02Z level=INFO service=account_service trace_id=tr-1 resource_id=account_10 event=profile_lookup_completed rows=0 http_status=404",
        '2026-09-15T10:05:00Z level=INFO service=search_service trace_id=tr-2 resource_id=search_1 event=index_query_completed result=hit email="ann@x.com"',
        '2026-09-15T10:10:00Z level=INFO service=integration_service trace_id=tr-3 resource_id=data_flow_1 event=connector_config_read_completed enabled=true customer_email="ann@x.com"',
        # conflicting ownership: one resource claimed by two users in two records
        '2026-09-15T10:20:00Z level=INFO service=search_service trace_id=tr-4 resource_id=search_2 event=index_query_completed result=hit email="ann@x.com"',
        '2026-09-15T10:21:00Z level=INFO service=search_service trace_id=tr-5 resource_id=search_2 event=index_query_completed result=hit email="bob@x.com"',
        ""]), encoding="utf-8", newline="\n")
    (root / "evidence/records/post.json").write_text(json.dumps({"exported_at": "2026-09-17T12:00:00Z", "collection": "index_queries", "records": [
        {"resource_id": "audit_record_1", "subject_reference": "user_1", "event_code": "ACCOUNT_DELETION_RECORDED", "retention_until": "2031-12-31T23:59:59Z"},
        {"resource_id": "search_1", "owner": "ann@x.com", "observed_at": "2026-09-17T11:00:00Z", "hits": {"total": 0}}]}), encoding="utf-8")
    (root / "evidence/logs/old.log").write_text(
        '2026-03-09T08:00:00Z level=INFO service=search_service trace_id=tr-9 resource_id=search_900 event=index_query_completed result=hit email="ann@x.com"\n', encoding="utf-8", newline="\n")


class MiniDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        build_dataset(root)
        cls.ds = load_dataset(root)
        cls.cr = CaseReconstructor(cls.ds, use_screenshots=False)
        cls.recs = cls.cr.reconstruct_all(cls.ds.requests.rows)
        cls.r1, cls.r2 = cls.recs["case_1"], cls.recs["case_2"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_mini_dataset_parses_cleanly(self):
        self.assertEqual(self.ds.issues(), [])
        self.assertEqual(self.cr.graph.issues, [])

    def test_historical_state_ignores_the_later_reading_and_cites_only_usable_evidence(self):
        d = self.r1.decision("search_1")
        self.assertEqual((d.state.state, d.state.citable, d.assessment.finding_op), ("PRESENT", ("evidence_1",), "REPORT_REMAINING_RECORD"))
        self.assertEqual([(e.file_id, e.state, e.reason) for e in d.state.excluded], [("evidence_2", "ABSENT", "file_captured_after_audit")])

    def test_post_audit_association_gives_identity_but_no_state(self):
        d = self.r1.decision("audit_record_1")
        self.assertTrue(d.association.post_audit_only)
        self.assertEqual((d.state.state, d.state.citable, d.state.has_evidence), ("UNKNOWN", (), False))
        self.assertEqual(d.relevance.label, RELEVANT)  # retained record of the subject, by role
        self.assertTrue(any("no state or citation rights" in e for e in d.explanation))

    def test_account_is_content_linked_via_trace_and_cited_from_the_lookup(self):
        d = self.r1.decision("account_10")
        self.assertEqual((d.state.state, d.assessment.verdict, self.r1.account.status), ("ABSENT", "COMPLIANT", "CONTENT"))
        self.assertEqual(d.association.link_types, ("TRACE",))

    def test_data_flow_fresh_evidence_is_a_violation(self):
        d = self.r1.decision("data_flow_1")
        self.assertEqual((d.state.state, d.assessment.finding_op, d.relevance.label), ("ACTIVE", "REPORT_ACTIVE_DATA_FLOW", RELEVANT))

    def test_other_scenario_of_the_same_user_is_excluded_and_attributed_to_the_other_case(self):
        d = self.r1.decision("search_900")
        self.assertEqual((d.relevance.label, d.relevance.claimed_by, d.included), (UNRELATED, ("case_2",), False))
        mine = self.r2.decision("search_900")
        self.assertEqual((mine.relevance.label, mine.state.state), (RELEVANT, "PRESENT"))
        self.assertNotIn("search_1", {x.resource_id for x in self.r2.decisions})  # case_2's audit is before search_1 existed in evidence

    def test_conflicting_ownership_is_surfaced_and_never_resolved_by_guessing(self):
        d = self.r1.decision("search_2")
        self.assertEqual((d.association.status, d.association.competing_users, d.relevance.label), ("AMBIGUOUS", ("user_2",), UNCERTAIN))
        self.assertIn("downgraded", " ".join(d.explanation))

    def test_every_decision_is_explained(self):
        for r in self.recs.values():
            for d in r.decisions:
                self.assertTrue(d.explanation and all(d.explanation), (r.case_id, d.resource_id))

    def test_missing_account_identifier_keeps_the_uncertainty(self):
        # case_2's account (account_20) has no identifier anywhere: one in-window candidate -> attributed, and the note says how
        self.assertEqual((self.r2.account.status, self.r2.account.account, self.r2.account.confidence), ("STRONG", "account_20", "MEDIUM"))
        d = self.r2.decision("account_20")
        self.assertEqual((d.origin, d.state.state, d.relevance.label), ("activity", "UNKNOWN", RELEVANT))
        self.assertTrue(any("attributed from activity" in e for e in d.explanation))

    def test_reconstruction_is_deterministic(self):
        again = CaseReconstructor(self.ds, use_screenshots=False).reconstruct_all(list(reversed(self.ds.requests.rows)))
        self.assertEqual(again, self.recs)


@needs_dataset
class RealDataRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = real_dataset()
        cls.cr = CaseReconstructor(cls.ds)
        non = {f"case_{i}" for i in range(533, 543)}
        cls.cases = [q for q in cls.ds.requests.rows + cls.ds.sample_requests.rows if q.case_id not in non]
        cls.recs = cls.cr.reconstruct_all(cls.cases)

    def test_samples_scope_and_state_snapshot(self):
        # Snapshot of measured values reported in code/docs/m3_state_and_scenario.md (not a requirement).
        gold = {r["case_id"]: json.loads(r["steps_json"]) for r in self.ds.sample_output}
        tp = fp = fn = agree = total = 0
        for q in self.ds.sample_requests.rows:
            scope = {s["resource_id"] for s in gold[q.case_id]}
            fnd = {s["resource_id"]: s["operation"] for s in gold[q.case_id] if s["phase"] == "FINDING"}
            inc = {d.resource_id: d for d in self.recs[q.case_id].included}
            tp += len(set(inc) & scope); fp += len(set(inc) - scope); fn += len(scope - set(inc))
            for rid, d in inc.items():
                if rid in scope:
                    total += 1
                    pred = d.assessment.finding_op if d.assessment.verdict == "VIOLATION" else None
                    agree += pred == fnd.get(rid)
        self.assertEqual((tp, fp, fn), (97, 0, 14))
        self.assertEqual((agree, total), (97, 97))

    def test_sample_other_scenarios_are_excluded_with_the_right_reason(self):
        for case, rid, other in (("case_07", "object_19", "case_61"), ("case_17", "account_6523", "case_46"), ("case_17", "tombstone_17", "case_46")):
            d = self.recs[case].decision(rid)
            self.assertEqual((d.relevance.label, d.relevance.claimed_by), (UNRELATED, (other,)), (case, rid))

    def test_screenshot_case_resource_is_present_from_the_ocr_and_cited_to_the_image(self):
        d = self.recs["case_35"].decision("search_428")
        self.assertEqual((d.state.state, d.state.citable, d.relevance.label), ("PRESENT", ("evidence_09",), RELEVANT))
        self.assertTrue(any("screenshot OCR" in e for e in d.explanation))

    def test_screenshot_case_without_the_ocr_cache_has_no_state(self):
        d = CaseReconstructor(self.ds, use_screenshots=False).reconstruct_all(self.cases)["case_35"].decision("search_428")
        self.assertEqual((d.state.state, d.state.has_evidence), ("UNKNOWN", False))

    def test_conflicting_screenshot_gives_no_state(self):
        d = self.recs["case_63"].decision("marketing_001")
        self.assertEqual((d.state.state, d.assessment.verdict), ("UNKNOWN", "UNKNOWN"))

    def test_no_decision_ever_cites_evidence_captured_after_its_audit_time(self):
        from ghostaudit.timeutil import is_eligible

        ev = self.ds.evidence_by_id()
        for r in self.recs.values():
            for d in r.decisions:
                for fid in d.state.citable:
                    self.assertTrue(is_eligible(ev[fid].captured_at, r.audit_at), (r.case_id, d.resource_id, fid))
                for e in d.state.history:
                    self.assertTrue(e.eligible)

    def test_only_one_owner_per_resource_in_every_case(self):
        for r in self.recs.values():
            for d in r.included:
                if d.association:
                    self.assertEqual(d.association.status, "ASSOCIATED", (r.case_id, d.resource_id))

    def test_determinism_across_two_full_runs(self):
        again = CaseReconstructor(self.ds).reconstruct_all(self.cases)
        self.assertEqual(again, self.recs)


if __name__ == "__main__":
    unittest.main()
