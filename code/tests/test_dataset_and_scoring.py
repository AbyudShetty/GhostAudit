import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import DATASET, needs_dataset

from ghostaudit.dataset import load_dataset, read_csv_dicts
from ghostaudit.scoring import format_report, load_cases, score


def step(no, phase, system, rid, op, state, ev=""):
    return dict(step_no=no, phase=phase, system_id=system, resource_id=rid, operation=op, state=state, evidence_id=ev)


REF = {
    "case_1": ("INCOMPLETE", [
        step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
        step(2, "REMEDIATION", "search_service", "search_1", "DELETE_RECORD", "ABSENT"),
        step(3, "VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", "ABSENT"),
        step(4, "VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ]),
    "case_2": ("COMPLETE", [step(1, "VERIFICATION", "account_service", "account_2", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_9")]),
    "case_3": ("NOT_APPLICABLE", []),
}


class Scoring(unittest.TestCase):
    def test_perfect_score(self):
        rep = score(copy.deepcopy(REF), REF)
        self.assertEqual((rep["status_accuracy"], rep["row_exact"], rep["sequence_exact"], rep["field_accuracy_all"]), (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(rep["step_exact"]["f1"], 1.0)
        self.assertEqual(rep["mismatched_cases"], [])

    def test_wrong_status_and_missing_case(self):
        pred = copy.deepcopy(REF)
        pred["case_2"] = ("INCOMPLETE", pred["case_2"][1])
        del pred["case_3"]
        rep = score(pred, REF)
        self.assertAlmostEqual(rep["status_accuracy"], 1 / 3)
        self.assertEqual(rep["missing_cases"], ["case_3"])
        self.assertEqual([c[:2] for c in rep["status_confusion"]], [["COMPLETE", "INCOMPLETE"], ["INCOMPLETE", "INCOMPLETE"], ["NOT_APPLICABLE", "<MISSING>"]])

    def test_order_only_error_keeps_step_f1_but_breaks_sequence(self):
        pred = copy.deepcopy(REF)
        s = pred["case_1"][1]
        s[2], s[3] = s[3], s[2]
        rep = score(pred, REF)
        self.assertEqual(rep["step_identity"]["f1"], 1.0)
        self.assertLess(rep["sequence_exact"], 1.0)
        self.assertTrue(rep["mismatched_cases"][0]["same_multiset_wrong_order"])
        self.assertLess(rep["field_accuracy"]["resource_id"], 1.0)

    def test_wrong_evidence_only_hurts_with_evidence_metrics(self):
        pred = copy.deepcopy(REF)
        pred["case_2"][1][0]["evidence_id"] = "evidence_8"
        rep = score(pred, REF)
        self.assertEqual(rep["step_identity"]["f1"], 1.0)
        self.assertLess(rep["step_exact"]["f1"], 1.0)
        self.assertEqual(rep["sequence_exact"], 1.0)
        self.assertLess(rep["sequence_exact_with_evidence"], 1.0)
        self.assertLess(rep["field_accuracy"]["evidence_id"], 1.0)

    def test_extra_and_missing_steps(self):
        pred = copy.deepcopy(REF)
        pred["case_1"][1].append(step(5, "VERIFICATION", "search_service", "search_2", "VERIFY_RECORD_ABSENT", "ABSENT"))
        rep = score(pred, REF)
        self.assertLess(rep["step_exact"]["precision"], 1.0)
        self.assertEqual(rep["step_exact"]["recall"], 1.0)
        pred = copy.deepcopy(REF)
        pred["case_1"][1].pop()
        rep = score(pred, REF)
        self.assertLess(rep["step_exact"]["recall"], 1.0)

    def test_report_formats(self):
        txt = format_report(score(copy.deepcopy(REF), REF))
        self.assertIn("status_accuracy        1.0000", txt)

    def test_load_cases_utf8_and_embedded_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.csv"
            body = json.dumps(REF["case_2"][1], ensure_ascii=False).replace('"', '""')
            p.write_bytes(('﻿case_id,deletion_status,steps_json\r\ncase_2,COMPLETE,"' + body + '"\r\ncase_3,NOT_APPLICABLE,[]\r\n').encode("utf-8"))
            cases = load_cases(p)
        self.assertEqual(cases["case_2"], REF["case_2"])
        self.assertEqual(cases["case_3"], ("NOT_APPLICABLE", []))

    def test_unparseable_steps_json_scores_as_empty_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.csv"
            p.write_text("case_id,deletion_status,steps_json\ncase_2,COMPLETE,not-json\n", encoding="utf-8")
            self.assertEqual(load_cases(p)["case_2"], ("COMPLETE", []))


@needs_dataset
class SampleScoring(unittest.TestCase):
    def test_reference_scores_perfectly_against_itself(self):
        ref = load_cases(DATASET / "sample_output.csv")
        self.assertEqual(len(ref), 32)
        rep = score(ref, ref)
        self.assertEqual((rep["status_accuracy"], rep["row_exact"], rep["sequence_exact_with_evidence"]), (1.0, 1.0, 1.0))

    def test_all_deletion_status_constant_prediction_is_penalised(self):
        ref = load_cases(DATASET / "sample_output.csv")
        pred = {k: ("INCOMPLETE", []) for k in ref}
        rep = score(pred, ref)
        self.assertAlmostEqual(rep["status_accuracy"], 22 / 32)
        self.assertEqual(rep["row_exact"], 0.0)


class CsvLoading(unittest.TestCase):
    def test_utf8_roundtrip_and_embedded_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_bytes('a,b\r\n"line1\r\nline2","日本語 é"\r\n'.encode("utf-8"))
            header, rows = read_csv_dicts(p)
        self.assertEqual(header, ["a", "b"])
        self.assertEqual(rows[0]["b"], "日本語 é")
        self.assertIn("line2", rows[0]["a"])

    @needs_dataset
    def test_loader_flags_duplicates_and_bad_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("deletion_requests.csv", "sample_deletion_requests.csv", "user_identities.csv", "user_audit_log.csv", "system_activity.csv", "deletion_rules.csv", "sample_output.csv"):
                (root / name).write_bytes((DATASET / name).read_bytes())
            (root / "evidence_files.csv").write_text(
                "file_id,file_path,origin,captured_at\nevidence_1,a.log,x,2026-01-01T00:00:00Z\nevidence_1,b.log,x,not-a-time\n", encoding="utf-8")
            ds = load_dataset(root)
        reasons = [i.reason for i in ds.evidence_files.issues]
        self.assertTrue(any("unparseable timestamp" in r for r in reasons))
        self.assertTrue(any("duplicate file_id" in r for r in reasons))

    def test_wrong_header_is_reported_not_crashed(self):
        from ghostaudit.dataset import DeletionRequest, _load

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.csv"
            p.write_text("case,email\n1,a@b.co\n", encoding="utf-8")
            t = _load(p, DeletionRequest, ("audit_at",), "case_id")
        self.assertEqual((len(t.rows), len(t.issues)), (0, 1))


if __name__ == "__main__":
    unittest.main()
