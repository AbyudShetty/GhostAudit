"""Row order of output.csv: the contract says "sorted by case_id" without saying how. This module pins (1) the evidence we found,
(2) the choice made on it, and (3) that the choice is one constant, so a later decision costs one line and touches nothing else.

Evidence for each reading is written up in docs/design_decisions.md ("Row order")."""

import csv
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import CODE_DIR, DATASET, needs_dataset, real_dataset

from ghostaudit import schema
from ghostaudit.classification import classify_all
from ghostaudit.plan import plan_all, rows_for
from ghostaudit.reconstruction import CaseReconstructor
from ghostaudit.schema import case_sort_key, parse_output_csv, validate_output_rows

MIXED = ["case_100", "case_33", "case_9", "case_550", "case_1000"]
NUMERIC = ["case_9", "case_33", "case_100", "case_550", "case_1000"]
LEXICOGRAPHIC = ["case_100", "case_1000", "case_33", "case_550", "case_9"]
STATEMENT = CODE_DIR.parent / "docs/challenge/problem_statement.md"


def row(cid):
    return {"case_id": cid, "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"}


def numeric_suffix(i):
    return int(re.search(r"(\d+)$", i).group(1))


class Decision(unittest.TestCase):
    """The pinned choice. If you change schema.CASE_ORDER, update the expectations in this class (and only here)."""

    def test_the_pinned_choice_is_numeric(self):
        self.assertEqual(schema.CASE_ORDER, "numeric")
        self.assertEqual(sorted(MIXED, key=case_sort_key), NUMERIC)

    def test_rows_and_validator_follow_the_choice(self):
        rows = rows_for({c: type("P", (), {"status": "NOT_APPLICABLE", "steps_json": lambda self: "[]"})() for c in MIXED}, MIXED)
        self.assertEqual([r["case_id"] for r in rows], NUMERIC)
        self.assertEqual(validate_output_rows(rows, MIXED, None), [])
        wrong = [row(c) for c in LEXICOGRAPHIC]
        self.assertEqual([v.code for v in validate_output_rows(wrong, MIXED, None)], ["row_order"])

    def test_unknown_shapes_sort_after_the_regular_ids_in_numeric_mode(self):
        self.assertEqual(sorted(["odd", "case_2", "case_10"], key=case_sort_key), ["case_2", "case_10", "odd"])


class TheSwitch(unittest.TestCase):
    """Flipping the constant must change the order and nothing else."""

    def test_lexicographic_mode_is_followed_by_writer_validator_and_message(self):
        with mock.patch.object(schema, "CASE_ORDER", "lexicographic"):
            self.assertEqual(sorted(MIXED, key=case_sort_key), LEXICOGRAPHIC)
            self.assertEqual(sorted(MIXED, key=case_sort_key), sorted(MIXED))
            ok = [row(c) for c in LEXICOGRAPHIC]
            self.assertEqual(validate_output_rows(ok, MIXED, None), [])
            bad = validate_output_rows([row(c) for c in NUMERIC], MIXED, None)
            self.assertEqual([v.code for v in bad], ["row_order"])
            self.assertIn("lexicographic", bad[0].message)

    def test_back_to_numeric_after_the_patch(self):
        with mock.patch.object(schema, "CASE_ORDER", "lexicographic"):
            pass
        self.assertEqual(sorted(MIXED, key=case_sort_key), NUMERIC)


class Evidence(unittest.TestCase):
    """What the supplied materials do and do not say (these pass today; a failure means the materials changed and the choice deserves a second look)."""

    @unittest.skipUnless(STATEMENT.is_file(), "challenge problem statement is not available")
    def test_the_statement_gives_no_qualifier(self):
        text = STATEMENT.read_text(encoding="utf-8")
        sentences = [l for l in text.splitlines() if "sorted by `case_id`" in l]
        self.assertEqual(len(sentences), 1, sentences)
        for word in ("numeric", "natural", "lexicograph", "alphabet", "string"):
            self.assertNotIn(word, sentences[0].lower())
        self.assertIn("ascending", text)  # said only about system_id / resource_id inside steps

    @needs_dataset
    def test_every_organizer_table_whose_ids_mix_widths_is_in_numeric_order_never_text_order(self):
        checked = {}
        for f in sorted(DATASET.glob("*.csv")):
            with open(f, encoding="utf-8-sig", newline="") as fh:
                body = list(csv.reader(fh))[1:]
            ids = [r[0] for r in body]
            if len(ids) < 2 or not all(re.search(r"\d+$", i) for i in ids) or len({len(i) for i in ids}) < 2:
                continue
            checked[f.name] = (ids == sorted(ids, key=numeric_suffix), ids == sorted(ids))
        self.assertEqual(checked, {"deletion_requests.csv": (True, False), "system_activity.csv": (True, False),
                                   "user_audit_log.csv": (True, False), "user_identities.csv": (True, False)})

    @needs_dataset
    def test_the_sample_files_and_the_template_cannot_tell_the_two_readings_apart(self):
        for name in ("sample_output.csv", "sample_deletion_requests.csv"):
            with open(DATASET / name, encoding="utf-8-sig", newline="") as fh:
                ids = [r["case_id"] for r in csv.DictReader(fh)]
            self.assertEqual(ids, sorted(ids))  # zero-padded to the same width: text order == numeric order
            self.assertEqual(ids, sorted(ids, key=numeric_suffix))
        self.assertEqual((DATASET / "output.csv").read_text(encoding="utf-8-sig").strip(), "case_id,deletion_status,steps_json")  # header only

    @needs_dataset
    def test_the_sample_answers_never_tie_two_resources_on_phase_operation_and_system(self):
        with open(DATASET / "sample_output.csv", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                keys = [(s["phase"], s["operation"], s["system_id"]) for s in json.loads(r["steps_json"])]
                self.assertEqual(len(keys), len(set(keys)), r["case_id"])  # so "ascending resource_id" is never exercised in the answers


@needs_dataset
class FlipChangesOnlyTheOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = real_dataset()
        cr = CaseReconstructor(ds)
        cls.ids = [q.case_id for q in ds.requests.rows]
        cls.plans = plan_all(classify_all(cr, ds.requests.rows), cr.manifest)

    def test_518_rows_in_either_mode_with_identical_content(self):
        numeric = rows_for(self.plans, self.ids)
        with mock.patch.object(schema, "CASE_ORDER", "lexicographic"):
            lex = rows_for(self.plans, self.ids)
            self.assertEqual(validate_output_rows(lex, self.ids, None, allow_empty_deletion_steps=True), [])
        self.assertEqual((len(numeric), len(lex)), (518, 518))
        self.assertEqual([r["case_id"] for r in numeric], sorted(self.ids, key=numeric_suffix))
        self.assertEqual([r["case_id"] for r in lex], sorted(self.ids))
        self.assertNotEqual([r["case_id"] for r in numeric], [r["case_id"] for r in lex])
        self.assertEqual({r["case_id"]: r for r in numeric}, {r["case_id"]: r for r in lex})  # same rows, only the order differs
        self.assertEqual(validate_output_rows(numeric, self.ids, None, allow_empty_deletion_steps=True), [])

    def test_the_default_output_file_text_is_in_the_pinned_order(self):
        rows = parse_output_csv(__import__("ghostaudit.plan", fromlist=["rows_to_csv"]).rows_to_csv(rows_for(self.plans, self.ids)))
        self.assertEqual([r["case_id"] for r in rows][:3], ["case_33", "case_34", "case_35"])
        self.assertEqual(rows[-1]["case_id"], "case_550")


if __name__ == "__main__":
    unittest.main()
