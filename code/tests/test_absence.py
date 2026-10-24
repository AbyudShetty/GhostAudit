import unittest

from tests.helpers import needs_dataset, real_dataset
from tests.world import AT, AUD, POST, PRE, World, audit_line, lf, lookup

from ghostaudit.classification import COMPLETE, INCOMPLETE, MEDIUM, ClassificationPolicy

SNAP_PRE = "2026-09-15T10:30:00Z"


def bundle(captured, subject_records, profiles=(), other_user_records=()):
    """A records-assurance snapshot bundle that declares account_service/customer_profiles and lists the given records."""
    snaps = [{"captured_at": captured, "data_store": "account_service", "collection": "customer_profiles", "records": list(profiles)}]
    if subject_records:
        snaps.append({"captured_at": captured, "data_store": "security_service", "collection": "security_audit_events",
                      "records": [{"resource_id": rid, "subject_reference": u, "event_name": "account_deletion_reviewed"} for rid, u in subject_records]})
    return {"exported_at": captured, "source": "records_assurance_service", "snapshots": snaps}


def world(doc, captured=SNAP_PRE, with_account_activity=True):
    w = World().case("case_1")
    if with_account_activity:
        w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
    return w.json("evidence_2", captured, doc).build()


class ScopedSnapshotSupport(unittest.TestCase):
    def test_snapshot_that_lists_the_subject_elsewhere_and_no_profile_supports_absence_at_medium(self):
        w = world(bundle(SNAP_PRE, [("audit_record_1", "user_1")]))
        v = w.verdict()
        acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.status, acct.resource_id, acct.confidence, acct.basis, acct.evidence_ids), ("PROVEN", "account_10", MEDIUM, "scoped_snapshot", ("evidence_2",)))
        self.assertIn("lists the subject's audit_record_1", acct.reason)
        self.assertEqual((v.status, v.determination), (COMPLETE, "INFERRED"))
        self.assertTrue(any(g.code == "ACCOUNT_STATE_NOT_EXPLICIT" and not g.blocking for g in v.gaps))  # the weaker basis stays visible
        w.close()

    def test_misleading_snapshot_that_does_not_list_the_subject_anywhere_gives_no_support(self):
        w = world(bundle(SNAP_PRE, [("audit_record_9", "user_2")]))  # lists someone else only
        v = w.verdict()
        self.assertEqual(v.determination, "PARTIAL")
        self.assertEqual([r.status for r in v.requirements if r.key == "ACCOUNT_ABSENT"], ["UNPROVEN"])
        w.close()

    def test_misleading_accounts_only_snapshot_cannot_enumerate_the_subject(self):
        doc = {"exported_at": SNAP_PRE, "snapshots": [{"captured_at": SNAP_PRE, "data_store": "account_service", "collection": "customer_profiles",
                                                        "records": [{"resource_id": "account_77", "customer_reference": "user_2", "full_name": "x", "email": "bob@x.com", "account_status": "ACTIVE"}]}]}
        self.assertEqual(world(doc).verdict().determination, "PARTIAL")

    def test_misleading_snapshot_listing_a_different_profile_of_the_same_subject_gives_no_support(self):
        # the subject has another account (account_77) listed, so "no profile for the subject" would be false; account_10 stays unproven
        other = {"resource_id": "account_77", "customer_reference": "user_1", "email": "ann@x.com", "full_name": "Ann", "account_status": "ACTIVE"}
        w = world(bundle(SNAP_PRE, [("audit_record_1", "user_1")], profiles=[other]))
        v = w.verdict()
        self.assertFalse(any(r.basis == "scoped_snapshot" for r in v.requirements))  # "no profile for the subject" would be false
        self.assertEqual((v.status, [x.resource_id for x in v.violations]), (INCOMPLETE, ["account_77"]))  # the listed profile is an explicit violation
        w.close()

    def test_snapshot_captured_after_the_audit_cannot_prove_anything(self):
        w = world(bundle(POST, [("audit_record_1", "user_1")]), captured=POST)
        v = w.verdict()
        self.assertEqual(v.determination, "PARTIAL")
        self.assertTrue(all(not r.evidence_ids for r in v.requirements))
        w.close()

    def test_snapshot_captured_exactly_at_the_audit_time_counts(self):
        w = world(bundle(AT, [("audit_record_1", "user_1")]), captured=AT)
        self.assertEqual(w.verdict().determination, "INFERRED")
        w.close()

    def test_subject_whose_profile_is_listed_is_a_violation_not_an_absence(self):
        profile = {"resource_id": "account_10", "customer_reference": "user_1", "email": "ann@x.com", "full_name": "Ann", "account_status": "ACTIVE"}
        w = world(bundle(SNAP_PRE, [("audit_record_1", "user_1")], profiles=[profile]))
        v = w.verdict()
        self.assertEqual((v.status, [(x.resource_id, x.finding_op) for x in v.violations]), (INCOMPLETE, [("account_10", "REPORT_REMAINING_RECORD")]))
        w.close()

    def test_explicit_evidence_takes_precedence_over_the_snapshot(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com"))
        w.json("evidence_2", SNAP_PRE, bundle(SNAP_PRE, [("audit_record_1", "user_1")])).build()
        acct = [r for r in w.verdict().requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.basis, acct.confidence, acct.evidence_ids), ("log.profile_lookup_completed.rows", "HIGH", ("evidence_1",)))
        w.close()

    def test_explicit_present_reading_beats_a_non_listing_snapshot(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com", rows=1))
        w.json("evidence_2", SNAP_PRE, bundle(SNAP_PRE, [("audit_record_1", "user_1")])).build()
        v = w.verdict()
        self.assertEqual((v.status, [x.resource_id for x in v.violations]), (INCOMPLETE, ["account_10"]))
        w.close()

    def test_policy_can_switch_the_tier_off(self):
        w = world(bundle(SNAP_PRE, [("audit_record_1", "user_1")]))
        v = w.verdict(policy=ClassificationPolicy("off", accept_scoped_snapshot_absence=False))
        self.assertEqual(v.determination, "PARTIAL")
        w.close()

    def test_later_bundle_showing_the_account_back_does_not_change_the_historical_answer(self):
        # the dataset's recreation trap: at audit time no profile; a later bundle lists it again
        profile = {"resource_id": "account_10", "customer_reference": "user_1", "email": "ann@x.com", "full_name": "Ann", "account_status": "ACTIVE"}
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.json("evidence_2", SNAP_PRE, bundle(SNAP_PRE, [("audit_record_1", "user_1")]))
        w.json("evidence_3", POST, bundle(POST, [("audit_record_1", "user_1")], profiles=[profile])).build()
        v = w.verdict()
        self.assertEqual((v.status, v.determination, v.violations), (COMPLETE, "INFERRED", ()))
        self.assertEqual({e for r in v.requirements for e in r.evidence_ids}, {"evidence_2"})
        w.close()

    def test_without_an_attributable_account_the_support_is_subject_level_and_names_no_account(self):
        w = world(bundle(SNAP_PRE, [("audit_record_1", "user_1")]), with_account_activity=False)
        v = w.verdict()
        acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.resource_id, acct.status, acct.basis), (None, "PROVEN", "scoped_snapshot"))
        self.assertTrue(any(g.code == "ACCOUNT_UNATTRIBUTED" and not g.blocking for g in v.gaps))
        w.close()


class AmbiguousAccountsWithSnapshot(unittest.TestCase):
    """Two cases, two candidate accounts, no identifiers on the accounts; a snapshot lists both subjects' audit records and no profile."""

    def world(self, account_lines=()):
        w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
        w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
        w.log("evidence_1", PRE, list(account_lines))
        w.json("evidence_2", SNAP_PRE, bundle(SNAP_PRE, [("audit_record_1", "user_1"), ("audit_record_2", "user_2")]))
        return w.build()

    @staticmethod
    def bare(ts, rid, rows):
        return lf(ts, "account_service", "profile_lookup_completed", rid, trace=f"tr-{rid}", rows=rows, http_status=200 if rows else 404)

    def test_valid_snapshot_supports_absence_for_the_subject_without_naming_either_account(self):
        v = self.world().verdict("case_1")
        acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.status, acct.resource_id, acct.basis, acct.confidence, acct.candidates), ("PROVEN", None, "scoped_snapshot", MEDIUM, ("account_10", "account_11")))
        self.assertEqual((v.status, v.determination), (COMPLETE, "INFERRED"))
        self.assertTrue(any(g.code == "ACCOUNT_AMBIGUOUS" and not g.blocking for g in v.gaps))

    def test_misleading_an_explicitly_present_candidate_overrides_the_snapshot(self):
        # account_11 is present; it may be this subject's account, so the snapshot may not be used to say "no account"
        v = self.world([self.bare("2026-09-15T10:00:00Z", "account_11", 1)]).verdict("case_1")
        self.assertEqual(v.determination, "PARTIAL")
        self.assertTrue(any(g.code == "ACCOUNT_AMBIGUOUS" and g.blocking and "present" in g.message for g in v.gaps))
        self.assertFalse(any(r.basis == "scoped_snapshot" for r in v.requirements))
        self.assertEqual(v.violations, ())  # and still no violation is claimed for an account that may be someone else's


@needs_dataset
class RealSnapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ghostaudit.classification import classify_all
        from ghostaudit.reconstruction import CaseReconstructor

        cls.ds = real_dataset()
        cls.cr = CaseReconstructor(cls.ds)
        cls.v = classify_all(cls.cr, cls.ds.requests.rows + cls.ds.sample_requests.rows)

    def test_sample_31_is_supported_by_its_snapshot_but_the_account_only_snapshot_of_01_02_04_is_not(self):
        r31 = [r for r in self.v["case_31"].requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((r31.basis, r31.evidence_ids, r31.confidence), ("scoped_snapshot", ("evidence_51",), MEDIUM))
        for cid in ("case_01", "case_02", "case_04"):
            self.assertEqual(self.v[cid].determination, "PARTIAL", cid)
            self.assertFalse(any(r.status == "PROVEN" and r.key == "ACCOUNT_ABSENT" for r in self.v[cid].requirements))

    def test_non_listing_in_a_partial_profile_snapshot_is_demonstrably_not_proof(self):
        # evidence_51 (empty profile collection, 12:00) vs evidence_92 (account_5334 present, 09:41, same day): the premise of the rule
        from ghostaudit.state import PRESENT

        s = self.cr.states.reconstruct("account_5334", "2026-09-15T12:00:00Z")
        self.assertEqual((s.state, s.citable), (PRESENT, ("evidence_92",)))
        self.assertTrue(any(c.name == "customer_profiles" and c.record_count == 0 for r in self.cr.results if r.file_id == "evidence_51" for c in r.containers))

    def test_trap_case_175_later_bundle_does_not_override_the_eligible_snapshot(self):
        v = self.v["case_175"]
        acct = [r for r in v.requirements if r.key == "ACCOUNT_ABSENT"][0]
        self.assertEqual((acct.basis, acct.evidence_ids, v.status, v.determination), ("scoped_snapshot", ("evidence_84",), "COMPLETE", "INFERRED"))
        self.assertNotIn("evidence_17", {e for r in v.requirements for e in r.evidence_ids})


if __name__ == "__main__":
    unittest.main()
