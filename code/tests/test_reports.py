"""The submitted reports must state what the code actually produces (so they cannot drift from the numbers)."""

import json
import unittest
from pathlib import Path

from tests.helpers import CODE_DIR, needs_dataset, real_dataset

EVAL = CODE_DIR / "evaluation" / "evaluation_report.md"
USAGE = CODE_DIR / "evaluation" / "usage_report.md"
README = CODE_DIR.parent / "README.md"


def text(p):
    return p.read_text(encoding="utf-8")


class ReportStructure(unittest.TestCase):
    def test_required_sections_are_present(self):
        low = text(EVAL).lower()
        for kw in ("status accuracy", "step-level", "field-level", "sequence", "additional evaluation cases", "failures found", "improvements made", "no official score"):
            self.assertIn(kw, low)
        low = text(USAGE).lower()
        for kw in ("provider", "model calls", "input tokens", "output tokens", "per request", "cost", "per-model"):
            self.assertIn(kw, low)

    def test_reports_never_contain_credentials_or_local_paths(self):
        import re

        for p in (EVAL, USAGE, README):
            t = text(p)
            self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY", t), p.name)
            bs = chr(92)  # a backslash, built here so no escaping can go wrong
            self.assertIsNone(re.search("[A-Za-z]:" + bs * 2 + "(?:Users|ABYUD)" + bs * 2 + "|/(?:Users|home)/" + bs + "w+/", t), p.name)

    def test_readme_documents_the_commands_that_exist(self):
        t = text(README)
        for cmd in ("python code/main.py", "pip install -r code/requirements.txt", "python -m unittest discover -s tests -t .", "python code/tools/package.py", "--dataset"):
            self.assertIn(cmd, t)
        self.assertTrue((CODE_DIR / "requirements.txt").is_file())

    def test_reports_do_not_claim_an_official_score(self):
        for p in (EVAL, README):
            self.assertIn("official", text(p).lower())
        self.assertNotIn("official score of", text(EVAL).lower())


@needs_dataset
class ReportNumbers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ghostaudit.classification import classify_all
        from ghostaudit.plan import plan_all
        from ghostaudit.reconstruction import CaseReconstructor
        from ghostaudit.scoring import score

        ds = real_dataset()
        cr = CaseReconstructor(ds)
        cls.v = classify_all(cr, ds.requests.rows + ds.sample_requests.rows)
        cls.plans = plan_all(cls.v, cr.manifest)
        cls.ds = ds
        gold = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in ds.sample_output}
        samples = [q.case_id for q in ds.sample_requests.rows]
        cls.gold, cls.samples = gold, samples
        cls.rep = score({c: (cls.plans[c].status, list(cls.plans[c].steps)) for c in samples}, gold)

    def test_sample_figures(self):
        t = text(EVAL)
        rep = self.rep
        self.assertEqual(rep["status_accuracy"], 1.0)
        self.assertIn("32/32", t)
        for v in (rep["step_exact"]["precision"], rep["step_exact"]["recall"], rep["step_exact"]["f1"], rep["sequence_lcs"], rep["field_accuracy_all"], rep["row_exact"]):
            self.assertIn(f"{v:.4f}", t)
        for f, v in rep["field_accuracy"].items():
            self.assertIn(f"| {f} | {v:.4f} |", t)
        gold_steps = sum(len(self.gold[c][1]) for c in self.samples)
        ours = sum(len(self.plans[c].steps) for c in self.samples)
        self.assertIn(f"| **All** | **{gold_steps}** | **{ours}** | **{gold_steps - ours}** | **0** |", t)
        self.assertIn(f"{round(rep['row_exact'] * 32)}/32", t)

    def test_missing_sample_steps_are_all_verification_and_none_unsupported(self):
        for c in self.samples:
            g = {(s["phase"], s["resource_id"], s["operation"]) for s in self.gold[c][1]}
            p = {(s["phase"], s["resource_id"], s["operation"]) for s in self.plans[c].steps}
            self.assertEqual(p - g, set(), c)
            self.assertTrue(all(ph == "VERIFICATION" for ph, _, _ in g - p), c)

    def test_evaluation_set_figures(self):
        from collections import Counter

        t = text(EVAL)
        ids = [q.case_id for q in self.ds.requests.rows]
        self.assertEqual(len(ids), 518)
        st = Counter(self.plans[c].status for c in ids)
        self.assertIn(f"{st['INCOMPLETE']} INCOMPLETE, {st['COMPLETE']} COMPLETE, {st['NOT_APPLICABLE']} NOT_APPLICABLE", t)
        n = Counter((self.plans[c].status, s["phase"]) for c in ids for s in self.plans[c].steps)
        self.assertIn(f"{n[('INCOMPLETE', 'FINDING')]} findings, {n[('INCOMPLETE', 'REMEDIATION')]} remediation, {n[('INCOMPLETE', 'VERIFICATION')]:,} verification", t)
        self.assertIn(f"COMPLETE: {n[('COMPLETE', 'VERIFICATION')]} verification", t)
        det = Counter((self.plans[c].status, self.plans[c].determination) for c in ids)
        self.assertIn(f"{det[('INCOMPLETE', 'PROVEN')]} PROVEN, {det[('INCOMPLETE', 'SUPPORTED')]} SUPPORTED", t)
        self.assertIn(f"{det[('COMPLETE', 'PROVEN')]} PROVEN, {det[('COMPLETE', 'INFERRED')]} INFERRED, {det[('COMPLETE', 'PARTIAL')]} PARTIAL", t)
        withheld = sum(len(self.plans[c].withheld) for c in ids)
        self.assertGreaterEqual(withheld, 179)  # the report's 179 counts steps withheld, not notes
        empty = sorted(c for c in ids if self.plans[c].status == "COMPLETE" and not self.plans[c].steps)
        self.assertEqual(empty, ["case_45", "case_55", "case_63"])
        for c in empty:
            self.assertIn(c, t)
        acct = lambda status: sum(1 for c in ids if self.plans[c].status == status and any(s["resource_id"].startswith("account_") for s in self.plans[c].steps))  # noqa: E731
        self.assertIn(f"in {acct('COMPLETE')} of {st['COMPLETE']} COMPLETE and {acct('INCOMPLETE')} of {st['INCOMPLETE']} INCOMPLETE", t)


if __name__ == "__main__":
    unittest.main()
