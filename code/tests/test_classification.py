import random
import unittest

from tests.helpers import needs_dataset, real_dataset
from tests.world import AT, AUD, POST, PRE, REP, REQ, World, audit_line, clean_world, lf, lookup

from ghostaudit.classification import (COMPLETE, HIGH, INCOMPLETE, LOW, MEDIUM, NOT_APPLICABLE, ClassificationPolicy, classify_all,
                                       evidence_confidence)
from ghostaudit.state import StateEvidence

STRICT = ClassificationPolicy("strict", unproven_status=INCOMPLETE)


def keys(v):
    return {(r.key, r.resource_id, r.status) for r in v.requirements}


def search_line(ts="2026-09-15T10:10:00Z", rid="search_1", email="ann@x.com", trace="tr-9"):
    return lf(ts, "search_service", "index_query_completed", rid, trace=trace, result="hit", email=email)


class NotApplicable(unittest.TestCase):
    def test_non_deletion_text_is_not_applicable_with_no_steps_material(self):
        w = World().case("case_1", text="Please change the email address used for this account. I want to continue using the service.").build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination, v.violations, v.requirements, v.gaps), (NOT_APPLICABLE, "TEXT", (), (), ()))
        self.assertEqual(v.confidence, HIGH)
        w.close()

    def test_misleading_everything_else_looks_like_a_deletion_case_and_still_not_applicable(self):
        # evidence of a remaining search entry exists, and the workflow claims COMPLETE: irrelevant, the person did not ask for deletion
        w = clean_world([search_line()])
        w.cases[0][6] = "Please send me a copy of my data. I still use the account so don't delete anything."
        w.build()
        self.assertEqual(w.verdict().status, NOT_APPLICABLE)
        w.close()

    def test_uncertain_text_never_authorises_by_default_but_policy_can_change_it(self):
        w = clean_world()
        w.cases[0][6] = "Hello, can someone call me back?"
        w.build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination, v.confidence), (NOT_APPLICABLE, "TEXT", LOW))
        self.assertIn("never authorises", v.reasons[0])
        v2 = w.verdict(policy=ClassificationPolicy("lenient", uncertain_intent_status=COMPLETE))
        self.assertEqual(v2.status, COMPLETE)  # evidence decides once the policy says to audit it
        w.close()

    def test_non_deletion_requests_do_not_take_part_in_account_attribution(self):
        # a non-deletion case in the same window must not consume the account of the real deletion case
        w = clean_world()
        w.case("case_2", "bob@x.com", text="My surname is misspelled on the account. Can you correct it without removing any of my records?")
        w.build()
        v = w.verdicts()
        self.assertEqual((v["case_1"].status, v["case_2"].status), (COMPLETE, NOT_APPLICABLE))
        w.close()


class ViolationsAndStatus(unittest.TestCase):
    def test_a_remaining_search_entry_makes_it_incomplete_and_cites_the_usable_file(self):
        w = clean_world([search_line()]).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination, v.confidence), (INCOMPLETE, "PROVEN", HIGH))
        x = v.violations[0]
        self.assertEqual((x.resource_id, x.finding_op, x.evidence_ids, x.confidence), ("search_1", "REPORT_REMAINING_RECORD", ("evidence_1",), HIGH))
        w.close()

    def test_reported_complete_does_not_make_it_complete(self):
        w = clean_world([search_line()])
        w.cases[0][3] = "COMPLETE"
        w.build()
        self.assertEqual(w.verdict().status, INCOMPLETE)
        w.close()

    def test_reported_status_is_not_what_makes_a_clean_case_complete(self):
        # the same evidence with each reported_status (consistent worker result) gives the same verdict
        for status, result in (("COMPLETE", "SUCCESS"), ("PARTIALLY_COMPLETE", "PARTIAL"), ("NOT_STARTED", "FAILED")):
            with self.subTest(status):
                w = World().case("case_1", status=status).worker("activity_1", "account_10", "2026-08-22T11:00:00Z", result)
                w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
                v = w.verdict()
                self.assertEqual((v.status, v.determination), (COMPLETE, "PROVEN"))
                w.close()

    def test_exposed_pii_and_active_flow_and_restore_risk_are_each_reported_with_their_operation(self):
        lines = [
            lf("2026-09-15T10:11:00Z", "support_platform", "support_ticket_read_completed", "support_1", trace="tr-s", requester_email="ann@x.com"),
            lf("2026-09-15T10:12:00Z", "integration_service", "connector_config_read_completed", "data_flow_1", trace="tr-f", enabled="true", last_delivery_status="delivered", customer_email="ann@x.com"),
            lf("2026-09-15T10:13:00Z", "backup_service", "restore_policy_read_started", None, trace="tr-b", lookup_key="ann@x.com"),
            lf("2026-09-15T10:13:02Z", "backup_service", "restore_policy_read_completed", "backup_1", trace="tr-b", archive_present="true", restore_allowed="true", customer_email="ann@x.com"),
        ]
        w = clean_world(lines).build()
        ops = {(x.resource_id, x.finding_op) for x in w.verdict().violations}
        self.assertEqual(ops, {("support_1", "REPORT_EXPOSED_PII"), ("data_flow_1", "REPORT_ACTIVE_DATA_FLOW"), ("backup_1", "REPORT_RESTORE_RISK")})
        w.close()

    def test_disabled_flow_and_blocked_restore_are_proofs_not_violations(self):
        lines = [
            lf("2026-09-15T10:12:00Z", "integration_service", "connector_config_read_completed", "data_flow_1", trace="tr-f", enabled="false", last_delivery_status="stopped", customer_email="ann@x.com"),
            lf("2026-09-15T10:13:02Z", "backup_service", "restore_policy_read_completed", "backup_1", trace="tr-b", archive_present="true", restore_allowed="false", customer_email="ann@x.com"),
        ]
        v = clean_world(lines).build().verdict()
        self.assertEqual((v.status, v.determination), (COMPLETE, "PROVEN"))
        self.assertTrue({("RESOURCE_TREATED", "data_flow_1", "PROVEN"), ("RESOURCE_TREATED", "backup_1", "PROVEN")} <= keys(v))
        self.assertEqual({r.expected_op for r in v.requirements}, {"VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"})

    def test_a_present_account_is_a_violation(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com", rows=1) + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        v = w.verdict()
        self.assertEqual((v.status, [(x.resource_id, x.finding_op) for x in v.violations]), (INCOMPLETE, [("account_10", "REPORT_REMAINING_RECORD")]))
        w.close()

    def test_a_violation_overrides_every_unproven_requirement(self):
        w = World().case("case_1").log("evidence_1", PRE, [search_line()]).build()  # no account, no audit record, but a remaining search entry
        v = w.verdict(policy=STRICT)
        self.assertEqual((v.status, v.determination), (INCOMPLETE, "PROVEN"))
        w.close()


class AuditTimeBoundaries(unittest.TestCase):
    def test_evidence_captured_exactly_at_audit_time_is_usable(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", AT, lookup("2026-09-15T11:59:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T11:59:30Z", "audit_record_1", "user_1"), search_line("2026-09-15T11:59:40Z")]).build()
        v = w.verdict()
        self.assertEqual((v.status, v.violations[0].evidence_ids), (INCOMPLETE, ("evidence_1",)))
        w.close()

    def test_one_second_after_audit_nothing_in_the_file_decides_or_is_cited(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", "2026-09-15T12:00:01Z", lookup("2026-09-15T11:59:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T11:59:30Z", "audit_record_1", "user_1"), search_line("2026-09-15T11:59:40Z")]).build()
        v = w.verdict()
        self.assertEqual(v.violations, ())  # the later search hit is neither a violation...
        self.assertEqual([r for r in v.requirements if r.status == "PROVEN"], [])  # ...nor is the later 404 a proof
        self.assertEqual(v.determination, "PARTIAL")
        self.assertTrue(all(not r.evidence_ids for r in v.requirements))
        w.close()

    def test_account_that_reappears_after_the_audit_does_not_change_a_proven_complete(self):
        w = clean_world().log("evidence_2", POST, lookup("2026-09-16T10:00:00Z", "account_10", "ann@x.com", rows=1, trace="tr-2")).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination), (COMPLETE, "PROVEN"))
        self.assertEqual({e for r in v.requirements for e in r.evidence_ids}, {"evidence_1"})
        w.close()

    def test_account_absent_only_in_a_later_file_is_identity_without_state(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])
        w.log("evidence_2", POST, lookup("2026-09-16T10:00:00Z", "account_10", "ann@x.com", rows=0, trace="tr-2")).build()
        v = w.verdict()
        a = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((a.resource_id, a.status, a.evidence_ids), ("account_10", "UNPROVEN", ()))
        self.assertIn("no evidence usable at audit_at", " ".join(g.message for g in v.gaps))
        self.assertEqual(v.determination, "PARTIAL")
        w.close()

    def test_record_time_after_audit_inside_an_early_file_is_not_used(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T12:30:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        v = w.verdict()
        self.assertEqual(v.determination, "PARTIAL")
        w.close()


class MissingEvidenceAndPolicy(unittest.TestCase):
    def build_partial(self):
        # account identified (activity only), no record names it, no snapshot: COMPLETE cannot be proven, nothing is violated
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        return w

    def test_unproven_is_partial_and_the_status_comes_from_the_policy(self):
        w = self.build_partial()
        d, s = w.verdict(), w.verdict(policy=STRICT)
        self.assertEqual((d.status, d.determination, d.confidence), (COMPLETE, "PARTIAL", LOW))
        self.assertEqual((s.status, s.determination), (INCOMPLETE, "PARTIAL"))
        self.assertIn("unproven_status", d.reasons[-1])
        self.assertEqual(d.violations, ())
        w.close()

    def test_the_unproven_account_is_listed_with_why_and_is_not_proof(self):
        v = self.build_partial().verdict()
        acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.status, acct.confidence, acct.evidence_ids), ("UNPROVEN", LOW, ()))
        self.assertTrue(any(g.code == "STATE_UNKNOWN" and g.blocking for g in v.gaps))

    def test_no_account_at_all_is_unattributed(self):
        w = World().case("case_1").log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        v = w.verdict()
        self.assertEqual([g.code for g in v.gaps if g.blocking], ["ACCOUNT_UNATTRIBUTED"])
        self.assertEqual(v.determination, "PARTIAL")
        w.close()

    def test_policy_values_are_validated(self):
        with self.assertRaises(ValueError):
            ClassificationPolicy(unproven_status="NOT_APPLICABLE")
        with self.assertRaises(ValueError):
            ClassificationPolicy(uncertain_intent_status="MAYBE")


class AmbiguousAccounts(unittest.TestCase):
    """Two cases, two worker deletes in both windows, no identifier on either account: which is whose cannot be known."""

    def world(self, lines_for_accounts=()):
        w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
        w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
        w.log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), audit_line("2026-09-15T10:06:00Z", "audit_record_2", "user_2")] + list(lines_for_accounts))
        return w.build()

    @staticmethod
    def bare_lookup(ts, rid, rows):
        return lf(ts, "account_service", "profile_lookup_completed", rid, trace=f"tr-{rid}", rows=rows, http_status=200 if rows else 404)

    def test_no_account_is_invented_when_the_attribution_is_ambiguous(self):
        w = self.world()
        for cid in ("case_1", "case_2"):
            v = w.verdict(cid)
            self.assertEqual(v.determination, "PARTIAL")
            self.assertFalse(any((r.resource_id or "").startswith("account_") for r in v.requirements))
            self.assertFalse(any(x.resource_id.startswith("account_") for x in v.violations))
            acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
            self.assertEqual((acct.resource_id, acct.status, acct.candidates), (None, "UNPROVEN", ("account_10", "account_11")))
            self.assertTrue(any(g.code == "ACCOUNT_AMBIGUOUS" and g.blocking for g in v.gaps))
        w.close()

    def test_when_every_candidate_is_explicitly_absent_the_status_is_proven_without_naming_one(self):
        w = self.world([self.bare_lookup("2026-09-15T10:00:00Z", "account_10", 0), self.bare_lookup("2026-09-15T10:01:00Z", "account_11", 0)])
        v = w.verdict("case_1")
        acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.status, acct.resource_id, acct.confidence, acct.candidates), ("PROVEN", None, MEDIUM, ("account_10", "account_11")))
        self.assertEqual((v.status, v.determination), (COMPLETE, "INFERRED"))
        self.assertTrue(any(g.code == "ACCOUNT_AMBIGUOUS" and not g.blocking for g in v.gaps))  # still recorded: no account step can be produced
        w.close()

    def test_when_a_candidate_is_present_the_subject_may_still_have_an_account_but_it_is_not_a_violation(self):
        w = self.world([self.bare_lookup("2026-09-15T10:00:00Z", "account_10", 0), self.bare_lookup("2026-09-15T10:01:00Z", "account_11", 1)])
        v = w.verdict("case_1")
        self.assertEqual(v.violations, ())  # account_11 may belong to the other user: no violation is claimed
        self.assertTrue(any(g.code == "ACCOUNT_AMBIGUOUS" and g.blocking and "present" in g.message for g in v.gaps))
        self.assertEqual(v.determination, "PARTIAL")
        w.close()

    def test_one_unreadable_candidate_keeps_it_unproven(self):
        w = self.world([self.bare_lookup("2026-09-15T10:00:00Z", "account_10", 0)])  # nothing about account_11
        self.assertEqual(w.verdict("case_1").determination, "PARTIAL")
        w.close()


class ConflictingOwnership(unittest.TestCase):
    def test_a_resource_claimed_by_two_users_is_neither_a_violation_nor_a_proof(self):
        lines = [search_line(rid="search_7", email="ann@x.com", trace="tr-a"), search_line("2026-09-15T10:11:00Z", rid="search_7", email="bob@x.com", trace="tr-b")]
        w = clean_world(lines).build()
        v = w.verdict()
        self.assertEqual(v.violations, ())
        self.assertTrue(any(g.code == "OWNERSHIP_CONFLICT" and g.resource_id == "search_7" for g in v.gaps))
        self.assertFalse(any(r.resource_id == "search_7" for r in v.requirements))
        self.assertEqual(v.determination, "PARTIAL")
        w.close()

    def test_misleading_same_user_via_two_identifiers_is_not_a_conflict(self):
        lines = [lf("2026-09-15T10:10:00Z", "search_service", "index_query_completed", "search_8", trace="tr-c", result="hit", email="ann@x.com", customer_reference="user_1")]
        v = clean_world(lines).build().verdict()
        self.assertEqual([x.resource_id for x in v.violations], ["search_8"])


class RetainedRecords(unittest.TestCase):
    def test_present_retained_record_is_proven_with_its_evidence(self):
        v = clean_world().build().verdict()
        r = [r for r in v.requirements if r.key == "RETAINED_PRESENT"][0]
        self.assertEqual((r.resource_id, r.status, r.expected_op, r.evidence_ids), ("audit_record_1", "PROVEN", "VERIFY_RECORD_PRESENT", ("evidence_1",)))

    def test_a_missing_required_retained_record_makes_the_deletion_incomplete(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1", rows=0)]).build()
        v = w.verdict()
        self.assertEqual(v.status, INCOMPLETE)
        self.assertEqual([(x.resource_id, x.finding_op) for x in v.violations], [("audit_record_1", None)])  # no operation exists for it
        self.assertIn("no operation", v.violations[0].caveats[0])
        w.close()

    def test_a_record_past_its_retention_period_is_not_required(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1", retention="2026-01-01T00:00:00Z", rows=0)]).build()
        v = w.verdict()
        self.assertEqual(v.violations, ())
        w.close()

    def test_a_missing_record_inside_its_retention_period_is_not_excused(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1", retention="2026-09-16T00:00:00Z", rows=0)]).build()
        self.assertEqual(w.verdict().status, INCOMPLETE)

    def test_no_retained_record_located_blocks_a_proven_complete(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com")).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination), (COMPLETE, "PARTIAL"))
        self.assertTrue(any(g.code == "RETAINED_NOT_LOCATED" for g in v.gaps))
        self.assertEqual(w.verdict(policy=STRICT).status, INCOMPLETE)
        w.close()

    def test_retained_record_identified_only_by_later_evidence_is_identity_without_state(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com"))
        w.log("evidence_2", POST, [audit_line("2026-09-16T10:00:00Z", "audit_record_1", "user_1")]).build()
        v = w.verdict()
        r = [r for r in v.requirements if r.key == "RETAINED_PRESENT"][0]
        self.assertEqual((r.resource_id, r.status, r.evidence_ids), ("audit_record_1", "UNPROVEN", ()))
        self.assertEqual(v.determination, "PARTIAL")
        w.close()


class EvidenceConfidence(unittest.TestCase):
    def ev(self, **kw):
        base = dict(file_id="e", locator="l", state="PRESENT", basis="b", effective_time=PRE, copy=None, eligible=True, reason="usable")
        base.update(kw)
        return StateEvidence(**base)

    def test_levels(self):
        self.assertEqual(evidence_confidence(self.ev(fmt="log")), HIGH)
        self.assertEqual(evidence_confidence(self.ev(fmt="sql")), HIGH)
        self.assertEqual(evidence_confidence(self.ev(fmt="pdf")), MEDIUM)
        self.assertEqual(evidence_confidence(self.ev(fmt="image", ocr=True, grounding="catalog+ocr")), MEDIUM)

    def test_screenshot_not_confirmed_by_readable_content_is_low(self):
        self.assertEqual(evidence_confidence(self.ev(fmt="image", ocr=True, grounding="catalog_time_only")), LOW)
        self.assertEqual(evidence_confidence(self.ev(fmt="image", ocr=True, grounding="conflict")), LOW)
        self.assertEqual(evidence_confidence(self.ev(fmt="image", ocr=True, grounding=None)), LOW)


class Determinism(unittest.TestCase):
    def test_case_order_and_row_order_do_not_change_any_verdict(self):
        w = clean_world([search_line()]).case("case_2", "bob@x.com").build()
        base = w.verdicts()
        rng = random.Random(4)
        for _ in range(4):
            cases = list(w.ds.requests.rows)
            rng.shuffle(cases)
            self.assertEqual(classify_all(w.cr, cases), base)
        w.close()


@needs_dataset
class RealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ghostaudit.reconstruction import CaseReconstructor

        cls.ds = real_dataset()
        cls.cr = CaseReconstructor(cls.ds)
        cls.all = cls.ds.requests.rows + cls.ds.sample_requests.rows
        cls.v = classify_all(cls.cr, cls.all)

    def test_status_counts_snapshot(self):
        from collections import Counter

        c = Counter((self.v[q.case_id].status, self.v[q.case_id].determination) for q in self.ds.requests.rows)
        self.assertEqual(dict(c), {("COMPLETE", "INFERRED"): 48, ("COMPLETE", "PARTIAL"): 45, ("COMPLETE", "PROVEN"): 62, ("INCOMPLETE", "PROVEN"): 345,
                                   ("INCOMPLETE", "SUPPORTED"): 8, ("NOT_APPLICABLE", "TEXT"): 10})

    def test_sample_status_agreement_and_what_it_rests_on(self):
        import json
        from collections import Counter

        gold = {r["case_id"]: r["deletion_status"] for r in self.ds.sample_output}
        self.assertTrue(all(self.v[c].status == s for c, s in gold.items()))
        by = Counter((gold[q.case_id], self.v[q.case_id].determination) for q in self.ds.sample_requests.rows)
        self.assertEqual(dict(by), {("COMPLETE", "PROVEN"): 3, ("COMPLETE", "INFERRED"): 3, ("COMPLETE", "PARTIAL"): 4, ("INCOMPLETE", "PROVEN"): 21, ("INCOMPLETE", "SUPPORTED"): 1})
        # every INCOMPLETE answer has a violation; no COMPLETE answer has one: the statuses are reproduced by findings, not by reported_status
        for c, s in gold.items():
            self.assertEqual(bool(self.v[c].violations), s == "INCOMPLETE", c)

    def test_every_citation_in_every_verdict_is_usable_at_its_audit_time(self):
        from ghostaudit.timeutil import is_eligible

        man = self.ds.evidence_by_id()
        audit = {q.case_id: q.audit_at for q in self.all}
        for cid, v in self.v.items():
            for e in [i for x in v.violations for i in x.evidence_ids] + [i for r in v.requirements for i in r.evidence_ids]:
                self.assertTrue(is_eligible(man[e].captured_at, audit[cid]), (cid, e))

    def test_screenshot_low_confidence_and_conflict_are_preserved_not_resolved(self):
        v46, v63 = self.v["case_46"], self.v["case_63"]
        self.assertTrue(any(g.code == "LOW_CONFIDENCE_EVIDENCE" and g.resource_id == "tombstone_17" for g in v46.gaps))
        self.assertFalse(any(r.resource_id == "tombstone_17" and r.status == "PROVEN" for r in v46.requirements))
        self.assertTrue(any(g.resource_id == "marketing_001" for g in v63.gaps))
        self.assertFalse(any(x.resource_id == "marketing_001" for x in v63.violations))
        self.assertEqual((v46.determination, v63.determination), ("PARTIAL", "PARTIAL"))

    def test_ambiguous_sample_pair_never_gets_an_account_step_material(self):
        for cid in ("case_28", "case_29"):
            v = self.v[cid]
            self.assertFalse(any(x.resource_id in ("account_5482", "account_7012") for x in v.violations))
            self.assertFalse(any(r.resource_id in ("account_5482", "account_7012") and r.status == "PROVEN" for r in v.requirements))

    def test_non_applicable_cases_are_exactly_the_ten_text_based_ones(self):
        na = sorted(q.case_id for q in self.ds.requests.rows if self.v[q.case_id].status == NOT_APPLICABLE)
        self.assertEqual(na, [f"case_{i}" for i in range(533, 543)])

    def test_violation_confidence_split(self):
        from collections import Counter

        c = Counter(x.confidence for q in self.ds.requests.rows for x in self.v[q.case_id].violations)
        self.assertEqual(dict(c), {"HIGH": 711, "MEDIUM": 8})

    def test_pdf_derived_violation_counts_by_default_and_not_when_the_policy_demands_high_only(self):
        self.assertEqual((self.v["case_07"].status, self.v["case_07"].determination, self.v["case_07"].violations[0].confidence), (INCOMPLETE, "SUPPORTED", MEDIUM))
        high_only = classify_all(self.cr, self.all, ClassificationPolicy("highonly", accept_medium_violations=False))
        self.assertEqual((high_only["case_07"].status, high_only["case_07"].determination), (COMPLETE, "PARTIAL"))
        self.assertIn("only at a confidence the policy does not count", " ".join(high_only["case_07"].reasons))

    def test_policy_variants_only_move_partial_cases(self):
        strict = classify_all(self.cr, self.all, STRICT)
        moved = [c for c in self.v if self.v[c].status != strict[c].status]
        self.assertTrue(moved)
        self.assertTrue(all(self.v[c].determination == "PARTIAL" and self.v[c].status == COMPLETE for c in moved))


class DecideGuards(unittest.TestCase):
    """The proven-COMPLETE conditions must hold on their own, not only because every unproven item happens to come with a gap."""

    def builder(self):
        from ghostaudit.classification import _Builder

        w = clean_world().build()
        recon = w.cr.reconstruct_all(w.ds.requests.rows)["case_1"]
        return w, _Builder(recon, w.cr.states, ClassificationPolicy())

    def decide(self, w, b):
        from ghostaudit.classification import _decide
        from ghostaudit.intent import classify_intent

        return _decide(w.ds.requests.rows[0], classify_intent("Please delete my account"), b, ClassificationPolicy())

    def req(self, status, conf=HIGH):
        from ghostaudit.classification import Requirement

        return Requirement("RESOURCE_TREATED", "search_1", "SEARCH_INDEX_ENTRY", "VERIFY_RECORD_ABSENT", status, conf, ("e",) if status == "PROVEN" else (), "b", "r")

    def test_control_one_proven_requirement_and_nothing_else_is_proven(self):
        w, b = self.builder()
        b.requirements.append(self.req("PROVEN"))
        v = self.decide(w, b)
        self.assertEqual((v.status, v.determination), (COMPLETE, "PROVEN"))
        w.close()

    def test_nothing_proven_and_nothing_wrong_is_partial_not_proven(self):
        w, b = self.builder()
        v = self.decide(w, b)
        self.assertEqual(v.determination, "PARTIAL")
        w.close()

    def test_an_unproven_requirement_without_any_gap_still_prevents_proven(self):
        w, b = self.builder()
        b.requirements += [self.req("PROVEN"), self.req("UNPROVEN", LOW)]
        self.assertEqual(self.decide(w, b).determination, "PARTIAL")
        w.close()

    def test_a_medium_proof_is_inferred_never_proven(self):
        w, b = self.builder()
        b.requirements += [self.req("PROVEN"), self.req("PROVEN", MEDIUM)]
        v = self.decide(w, b)
        self.assertEqual((v.status, v.determination, v.confidence), (COMPLETE, "INFERRED", MEDIUM))
        w.close()


class RelevanceUncertain(unittest.TestCase):
    """Ownership is not membership: a resource that cannot be placed in this request is neither a violation nor a proof."""

    def test_valid_fresh_evidence_makes_it_a_violation(self):
        v = clean_world([search_line("2026-09-10T10:10:00Z")]).build().verdict()
        self.assertEqual([x.resource_id for x in v.violations], ["search_1"])

    def test_misleading_only_stale_evidence_and_no_workflow_is_a_blocking_gap_not_a_violation(self):
        # the only mention is from 10 Aug, before the request was opened (20 Aug); nothing ties it to this request
        w = clean_world().log("evidence_0", "2026-08-10T12:00:00Z", [search_line("2026-08-10T10:10:00Z", trace="tr-old")]).build()
        v = w.verdict()
        self.assertEqual(v.violations, ())
        self.assertTrue(any(g.code == "RELEVANCE_UNCERTAIN" and g.blocking and g.resource_id == "search_1" for g in v.gaps))
        self.assertEqual((v.status, v.determination), (COMPLETE, "PARTIAL"))
        w.close()

    def test_a_sibling_request_of_the_same_user_claims_it_so_it_is_excluded_not_uncertain(self):
        w = clean_world().case("case_0", "ann@x.com", requested="2026-08-01T00:00:00Z", reported="2026-08-12T12:00:00Z", audit="2026-08-14T00:00:00Z")
        w.log("evidence_0", "2026-08-10T12:00:00Z", [search_line("2026-08-10T10:10:00Z", trace="tr-old")]).build()
        v = w.verdict("case_1")
        self.assertEqual(v.violations, ())
        self.assertIn(("search_1", "UNRELATED"), [(r, lab) for r, lab, _ in v.excluded])
        self.assertFalse(any(g.code == "RELEVANCE_UNCERTAIN" for g in v.gaps))
        w.close()


if __name__ == "__main__":
    unittest.main()
