"""When may a deletion request be COMPLETE although something is unproven? (ClassificationPolicy.unproven_status is unchanged.)

The contract (AGENTS.md 6.5) makes COMPLETE require every relevant copy treated, every required retained record available and no active
sync/import/restore path. The supplied answers label four of our PARTIAL cases COMPLETE (case_01, 02, 04, 28), and in each of them the
only thing left unproven is the subject's account (ambiguous, unattributed, or no eligible reading), sometimes with no retained record
located. That is the *attested pattern*. Anything else left unproven in a COMPLETE case must be explained by the evidence, or these tests
fail, so a new kind of unexplained gap cannot slip into the output. NOT_APPLICABLE, expired retention and set-wise account proofs are
covered as the legitimate exceptions they are."""

import unittest

from tests.helpers import needs_dataset, real_dataset
from tests.world import PRE, World, audit_line, lf, lookup

from ghostaudit.classification import COMPLETE, INCOMPLETE, NOT_APPLICABLE, classify_all
from ghostaudit.plan import build_plan

ACCOUNT_GAPS = {"ACCOUNT_AMBIGUOUS", "ACCOUNT_UNATTRIBUTED", "RETAINED_NOT_LOCATED"}


def beyond_the_attested_pattern(verdict, recon):
    """Blocking gaps and unproven requirements other than the subject's account (and 'no retained record located')."""
    out = []
    for g in verdict.gaps:
        if not g.blocking or g.code in ACCOUNT_GAPS:
            continue
        d = recon.decision(g.resource_id) if (recon and g.resource_id) else None
        if g.code == "STATE_UNKNOWN" and d is not None and d.state.category == "USER_PROFILE":
            continue
        out.append((g.code, g.resource_id))
    out += [("UNPROVEN " + r.key, r.resource_id) for r in verdict.requirements if r.status != "PROVEN" and r.key != "ACCOUNT_ABSENT"]
    return out


def identified_later_without_state(recon, resource_id):
    """True when the resource is the subject's only through files captured after audit_at (link policy S3k) and no reading of it is
    usable at audit_at: the audit-time rule makes its state unknowable, so it can be neither a finding nor a proof."""
    d = recon.decision(resource_id)
    return d is not None and d.association is not None and d.association.post_audit_only and not d.state.history


def explained(recon, resource_id):
    return resource_id is not None and (screenshot_only_and_undecided(recon, resource_id) or identified_later_without_state(recon, resource_id))


def screenshot_only_and_undecided(recon, resource_id):
    """True when every usable reading of the resource is a screenshot and none of them decides the requirement: the reading is
    tied only by catalog timing, shows someone else's identifiers, or does not show the attribute that decides compliance."""
    d = recon.decision(resource_id)
    if d is None or not d.state.history:
        return False
    return all(e.fmt == "image" for e in d.state.history) and (d.assessment.verdict == "UNKNOWN" or any(e.grounding != "catalog+ocr" for e in d.state.deciding))


class Synthetic(unittest.TestCase):
    def test_account_only_unproven_is_complete_partial(self):
        # the account is known only from activity and nothing names it at audit_at; the retained record is proven
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination), (COMPLETE, "PARTIAL"))
        self.assertEqual(beyond_the_attested_pattern(v, w.cr.reconstruct_all(w.ds.requests.rows)["case_1"]), [])
        w.close()

    def test_a_proven_or_inferred_complete_never_carries_a_blocking_gap(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination), (COMPLETE, "PROVEN"))
        self.assertEqual(([g for g in v.gaps if g.blocking], [r for r in v.requirements if r.status != "PROVEN"]), ([], []))
        w.close()

    def test_expired_retention_is_neither_a_violation_nor_a_gap(self):
        for rows, key in ((1, "RETAINED_EXPIRED"), (0, None)):  # present or deleted after its retention period ended: both fine
            w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
            w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com")
                  + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1", retention="2026-09-01T00:00:00Z", rows=rows)]).build()
            v = w.verdict()
            self.assertEqual(v.status, COMPLETE, rows)
            self.assertFalse(any(x.resource_id == "audit_record_1" for x in v.violations), rows)
            self.assertEqual([r.key for r in v.requirements if r.resource_id == "audit_record_1"], [key] if key else [], rows)
            self.assertNotIn("audit_record_1", build_plan(v, w.cr.manifest).steps_json())  # no retained-record proof is owed
            w.close()

    def test_a_set_wise_account_proof_is_a_non_blocking_gap(self):
        bare = lambda ts, rid: lf(ts, "account_service", "profile_lookup_completed", rid, trace=f"tr-{rid}", rows=0, http_status=404)  # noqa: E731
        w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
        w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
        w.log("evidence_1", PRE, [bare("2026-09-15T10:00:00Z", "account_10"), bare("2026-09-15T10:01:00Z", "account_11"),
                                  audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), audit_line("2026-09-15T10:06:00Z", "audit_record_2", "user_2")]).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination), (COMPLETE, "INFERRED"))
        self.assertTrue(all(not g.blocking for g in v.gaps if g.code == "ACCOUNT_AMBIGUOUS"))
        w.close()

    def test_the_rule_flags_an_item_left_unproven_by_structured_evidence(self):
        # a log names the subject's backup but not whether restore is eligible: COMPLETE/PARTIAL, and not explained by a screenshot
        backup = lf("2026-09-15T10:06:00Z", "backup_service", "restore_candidate_evaluated", "backup_1", trace="tr-b", customer_email="ann@x.com")
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), backup]).build()
        v = w.verdict()
        rec = w.cr.reconstruct_all(w.ds.requests.rows)["case_1"]
        self.assertEqual((v.status, v.determination), (COMPLETE, "PARTIAL"))
        self.assertIn(("STATE_UNKNOWN", "backup_1"), beyond_the_attested_pattern(v, rec))
        self.assertFalse(screenshot_only_and_undecided(rec, "backup_1"))  # so RealData would fail on such a case
        w.close()

    def test_not_applicable_carries_no_requirement_gap_or_step(self):
        w = World().case("case_1", text="Please change the email address used for this account. I want to continue using the service.")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com", rows=1)).build()
        v = w.verdict()
        self.assertEqual((v.status, v.requirements, v.gaps, v.violations), (NOT_APPLICABLE, (), (), ()))
        self.assertEqual(build_plan(v, w.cr.manifest).steps, ())
        w.close()


@needs_dataset
class RealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ghostaudit.reconstruction import CaseReconstructor

        ds = real_dataset()
        cls.cr = CaseReconstructor(ds)
        cls.answers = {r["case_id"]: r["deletion_status"] for r in ds.sample_output}
        cls.sets = {}
        for label, cases in (("samples", ds.sample_requests.rows), ("eval", ds.requests.rows)):  # each population as the CLI runs it
            v = classify_all(cls.cr, cases)
            cls.sets[label] = (v, cls.cr.reconstruct_all([c for c in cases if v[c.case_id].status != NOT_APPLICABLE]))

    def test_the_attested_pattern_is_what_the_sample_answers_label_complete(self):
        v, recs = self.sets["samples"]
        partial = sorted(c for c, x in v.items() if x.status == COMPLETE and x.determination == "PARTIAL")
        self.assertEqual(partial, ["case_01", "case_02", "case_04", "case_28"])
        for c in partial:
            self.assertEqual(self.answers[c], COMPLETE, c)
            self.assertEqual(beyond_the_attested_pattern(v[c], recs[c]), [], c)

    def test_complete_proven_or_inferred_has_nothing_unproven(self):
        for label, (v, _) in self.sets.items():
            for c, x in v.items():
                if x.status == COMPLETE and x.determination in ("PROVEN", "INFERRED"):
                    self.assertEqual(([g.code for g in x.gaps if g.blocking], [r.key for r in x.requirements if r.status != "PROVEN"]), ([], []), (label, c))

    def test_anything_beyond_the_attested_pattern_is_explained_by_the_evidence(self):
        """Fails when a COMPLETE case leaves something other than the account unproven although usable evidence exists. The only
        explanations accepted: the resource's usable evidence is one undecided screenshot, or the resource is tied to the subject only by
        files captured after audit_at and nothing about it is usable at audit_at."""
        v, recs = self.sets["eval"]
        for c, x in v.items():
            if x.status != COMPLETE:
                continue
            for code, rid in beyond_the_attested_pattern(x, recs[c]):
                self.assertTrue(explained(recs[c], rid), (c, code, rid))

    def test_the_reviewed_cases_beyond_the_attested_pattern(self):
        """Not an exception list: the rule is the test above. This pins the cases reviewed on 2026-10-25 (evaluation report, section 6),
        so that a new one is noticed and reviewed even when it satisfies the rule."""
        v, recs = self.sets["eval"]
        found = {c: sorted(r for _, r in beyond_the_attested_pattern(x, recs[c])) for c, x in v.items() if x.status == COMPLETE and beyond_the_attested_pattern(x, recs[c])}
        self.assertEqual(found, {"case_46": ["audit_record_1797", "audit_record_1797", "tombstone_17", "tombstone_17"],
                                 "case_55": ["audit_record_2857", "audit_record_2857", "backup_9287", "backup_9287"], "case_63": ["marketing_001"]})
        # the audit records are tied to the subject only by a log captured after audit_at (identity without state, S3k)

    def test_incomplete_always_rests_on_a_counted_violation_and_not_applicable_is_empty(self):
        for label, (v, _) in self.sets.items():
            for c, x in v.items():
                if x.status == INCOMPLETE:
                    self.assertTrue(x.counted_violations, (label, c))
                if x.status == NOT_APPLICABLE:
                    self.assertEqual((x.requirements, x.gaps, x.violations), ((), (), ()), (label, c))


if __name__ == "__main__":
    unittest.main()
