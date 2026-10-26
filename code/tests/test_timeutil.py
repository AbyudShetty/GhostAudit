import unittest

from tests.helpers import DATASET, needs_dataset, real_dataset  # noqa: F401  (sets sys.path)

from ghostaudit.timeutil import format_ts, is_eligible, observation_usable, parse_ts, try_parse_ts


class TimestampParsing(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(format_ts(parse_ts("2026-09-15T12:00:00Z")), "2026-09-15T12:00:00Z")

    def test_strict_rejects_other_formats(self):
        for bad in ["2026-09-15", "2026-09-15 12:00:00", "15/09/2026", "", "2026-13-01T00:00:00Z", "2026-09-15T12:00:00+02:00"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_ts(bad)
                self.assertIsNone(try_parse_ts(bad))

    def test_non_string(self):
        self.assertIsNone(try_parse_ts(None))
        with self.assertRaises(ValueError):
            parse_ts(None)  # type: ignore[arg-type]


class AuditBoundary(unittest.TestCase):
    AUDIT = "2026-09-15T12:00:00Z"

    def test_boundary_inclusive(self):
        self.assertTrue(is_eligible("2026-09-15T12:00:00Z", self.AUDIT))

    def test_one_second_after_excluded(self):
        self.assertFalse(is_eligible("2026-09-15T12:00:01Z", self.AUDIT))

    def test_before_included(self):
        self.assertTrue(is_eligible("2026-09-15T11:59:59Z", self.AUDIT))

    def test_observation_needs_both_file_and_record_time(self):
        self.assertTrue(observation_usable("2026-09-15T11:00:00Z", "2026-09-15T10:00:00Z", self.AUDIT))
        self.assertFalse(observation_usable("2026-09-15T13:00:00Z", "2026-09-15T10:00:00Z", self.AUDIT))  # file too late
        self.assertFalse(observation_usable("2026-09-15T11:00:00Z", "2026-09-15T12:00:01Z", self.AUDIT))  # record too late
        self.assertTrue(observation_usable("2026-09-15T11:00:00Z", None, self.AUDIT))  # no record time: file decides

    def test_unparseable_record_time_is_not_invented(self):
        self.assertTrue(observation_usable("2026-09-15T11:00:00Z", "garbage", self.AUDIT))


@needs_dataset
class SampleCitationsRespectBoundary(unittest.TestCase):
    """Every evidence file cited by a supplied sample answer is eligible at that case's audit_at,
    and evidence_51 sits exactly on case_31's boundary (so inclusive is required)."""

    def test_all_sample_citations_eligible(self):
        import json

        ds = real_dataset()
        ev = ds.evidence_by_id()
        audit = {r.case_id: r.audit_at for r in ds.sample_requests.rows}
        cited = 0
        for row in ds.sample_output:
            for s in json.loads(row["steps_json"]):
                eid = s["evidence_id"]
                if eid.startswith("evidence_"):
                    cited += 1
                    self.assertTrue(is_eligible(ev[eid].captured_at, audit[row["case_id"]]), (row["case_id"], eid))
        self.assertEqual(cited, 75)

    def test_evidence_51_is_exactly_on_case_31_boundary(self):
        ds = real_dataset()
        audit = {r.case_id: r.audit_at for r in ds.sample_requests.rows}
        self.assertEqual(ds.evidence_by_id()["evidence_51"].captured_at, audit["case_31"])

    def test_evidence_06_is_after_case_09_audit(self):
        # evidence_06 holds log lines dated before case_09's audit_at but was *captured* after it;
        # the sample answer cites evidence_93 instead. File-level capture time is the rule.
        ds = real_dataset()
        audit = {r.case_id: r.audit_at for r in ds.sample_requests.rows}
        self.assertFalse(is_eligible(ds.evidence_by_id()["evidence_06"].captured_at, audit["case_09"]))


if __name__ == "__main__":
    unittest.main()
