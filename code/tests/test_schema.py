import copy
import json
import unittest

from tests.helpers import needs_dataset, real_dataset

from ghostaudit import schema
from ghostaudit.schema import (
    OPERATION_ORDER, OPERATION_PHASE, OPERATION_STATE, case_sort_key, parse_output_csv, system_for_resource,
    validate_output_rows, validate_steps,
)


def step(no, phase, system, rid, op, state, ev=""):
    return dict(step_no=no, phase=phase, system_id=system, resource_id=rid, operation=op, state=state, evidence_id=ev)


GOOD_INCOMPLETE = [
    step(1, "FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_1"),
    step(2, "REMEDIATION", "search_service", "search_1", "DELETE_RECORD", "ABSENT"),
    step(3, "VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", "ABSENT"),
    step(4, "VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", "ABSENT"),
]


class Tables(unittest.TestCase):
    def test_fifteen_operations_match_problem_statement(self):
        self.assertEqual(len(OPERATION_PHASE), 15)
        self.assertEqual(sum(1 for p in OPERATION_PHASE.values() if p == "FINDING"), 4)
        self.assertEqual(sum(1 for p in OPERATION_PHASE.values() if p == "REMEDIATION"), 5)
        self.assertEqual(sum(1 for p in OPERATION_PHASE.values() if p == "VERIFICATION"), 6)
        self.assertEqual(OPERATION_STATE["BLOCK_RESTORE"], "BLOCKED")
        self.assertEqual(OPERATION_STATE["REPORT_RESTORE_RISK"], "ACTIVE")
        self.assertEqual(list(OPERATION_ORDER)[0], "REPORT_REMAINING_RECORD")

    def test_case_order_follows_the_documented_choice(self):
        # The contract does not say how "sorted by case_id" sorts; the choice and its evidence are pinned in tests/test_row_order.py.
        ids = ["case_100", "case_33", "case_1000", "case_9", "case_550"]
        self.assertEqual(sorted(ids, key=case_sort_key), ["case_9", "case_33", "case_100", "case_550", "case_1000"])
        self.assertNotEqual(sorted(ids, key=case_sort_key), sorted(ids))  # the two readings differ on mixed-width ids, so the choice matters

    def test_resource_prefix_mapping(self):
        self.assertEqual(system_for_resource("audit_record_2016"), "security_service")
        self.assertEqual(system_for_resource("data_flow_9522"), "integration_service")
        self.assertEqual(system_for_resource("tombstone_440"), "identity_service")
        self.assertIsNone(system_for_resource("widget_1"))
        self.assertIsNone(system_for_resource(""))

    def test_resource_id_regex_does_not_match_inside_words(self):
        found = [m.group(0) for m in schema.RESOURCE_ID_RE.finditer("see account_12, xaccount_9 and data_flow_3x audit_record_77")]
        self.assertEqual(found, ["account_12", "audit_record_77"])


class StepValidation(unittest.TestCase):
    def codes(self, status, steps, **kw):
        return {v.code for v in validate_steps("case_x", status, steps, **kw)}

    def test_valid_incomplete(self):
        self.assertEqual(self.codes("INCOMPLETE", GOOD_INCOMPLETE), set())

    def test_not_applicable_must_be_empty(self):
        self.assertEqual(self.codes("NOT_APPLICABLE", []), set())
        self.assertEqual(self.codes("NOT_APPLICABLE", GOOD_INCOMPLETE), {"not_applicable_steps"})

    def test_empty_steps_for_deletion_cases(self):
        self.assertEqual(self.codes("COMPLETE", []), {"empty_steps"})

    def test_bad_status(self):
        self.assertEqual(self.codes("DONE", []), {"status"})

    def test_missing_field_and_extra_field(self):
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        del bad[0]["evidence_id"]
        self.assertIn("fields", self.codes("INCOMPLETE", bad))
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[0]["extra"] = 1
        self.assertIn("fields", self.codes("INCOMPLETE", bad))

    def test_step_numbers_must_be_consecutive_from_one(self):
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[2]["step_no"] = 9
        self.assertIn("step_no", self.codes("INCOMPLETE", bad))
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[0]["step_no"] = True
        self.assertIn("step_no", self.codes("INCOMPLETE", bad))

    def test_operation_phase_state_consistency(self):
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[1]["state"] = "REDACTED"
        self.assertIn("operation_state", self.codes("INCOMPLETE", bad))
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[1]["phase"] = "VERIFICATION"
        self.assertIn("operation_phase", self.codes("INCOMPLETE", bad))
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[1]["operation"] = "WIPE_EVERYTHING"
        self.assertIn("operation", self.codes("INCOMPLETE", bad))

    def test_evidence_conventions(self):
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[0]["evidence_id"] = ""
        self.assertIn("evidence_required", self.codes("INCOMPLETE", bad))
        bad = copy.deepcopy(GOOD_INCOMPLETE)
        bad[1]["evidence_id"] = "evidence_2"
        self.assertIn("evidence_forbidden", self.codes("INCOMPLETE", bad))
        self.assertIn("evidence_unknown", self.codes("INCOMPLETE", GOOD_INCOMPLETE, valid_evidence_ids=["evidence_9"]))

    def test_complete_requires_verification_only_with_evidence(self):
        ok = [step(1, "VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_1")]
        self.assertEqual(self.codes("COMPLETE", ok), set())
        self.assertIn("evidence_required", self.codes("COMPLETE", [dict(ok[0], evidence_id="")]))
        mixed = [step(1, "FINDING", "a", "account_1", "REPORT_REMAINING_RECORD", "PRESENT", "e"), dict(ok[0], step_no=2)]
        self.assertIn("complete_phases", self.codes("COMPLETE", mixed))

    def test_order_violations(self):
        swapped = copy.deepcopy(GOOD_INCOMPLETE)
        swapped[2], swapped[3] = swapped[3], swapped[2]
        swapped[2]["step_no"], swapped[3]["step_no"] = 3, 4
        self.assertIn("order", self.codes("INCOMPLETE", swapped))
        self.assertNotIn("order", self.codes("INCOMPLETE", swapped, check_order=False))


class OutputCsv(unittest.TestCase):
    HEADER = "case_id,deletion_status,steps_json\r\n"

    def test_header_must_match(self):
        with self.assertRaises(ValueError):
            parse_output_csv("case_id,status,steps\r\n")

    def test_coverage_duplicates_and_order(self):
        rows = parse_output_csv(self.HEADER + "case_100,NOT_APPLICABLE,[]\r\ncase_33,NOT_APPLICABLE,[]\r\ncase_33,NOT_APPLICABLE,[]\r\n")
        codes = {v.code for v in validate_output_rows(rows, ["case_33", "case_100", "case_101"])}
        self.assertEqual(codes, {"duplicate_case", "missing_case", "row_order"})

    def test_bad_json(self):
        rows = parse_output_csv(self.HEADER + 'case_1,COMPLETE,"not json"\r\ncase_2,COMPLETE,"{}"\r\n')
        self.assertEqual([v.code for v in validate_output_rows(rows)], ["json", "json"])

    def test_embedded_quotes_roundtrip(self):
        steps = json.dumps(GOOD_INCOMPLETE).replace('"', '""')
        rows = parse_output_csv(self.HEADER + f'case_1,INCOMPLETE,"{steps}"\r\n')
        self.assertEqual(validate_output_rows(rows), [])


@needs_dataset
class SampleAnswers(unittest.TestCase):
    def test_all_32_samples_satisfy_contract_and_ordering(self):
        ds = real_dataset()
        ids = [r.case_id for r in ds.sample_requests.rows]
        ev_ids = [f.file_id for f in ds.evidence_files.rows] + [a.activity_id for a in ds.activity.rows] + [a.audit_id for a in ds.audit_log.rows]
        rows = [dict(r) for r in ds.sample_output]
        self.assertEqual(len(rows), 32)
        self.assertEqual([str(v) for v in validate_output_rows(rows, ids, ev_ids)], [])

    def test_sample_status_mix(self):
        ds = real_dataset()
        from collections import Counter

        self.assertEqual(Counter(r["deletion_status"] for r in ds.sample_output), {"INCOMPLETE": 22, "COMPLETE": 10})


if __name__ == "__main__":
    unittest.main()
