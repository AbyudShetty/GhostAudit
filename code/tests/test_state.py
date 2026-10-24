import unittest

from tests.factories import fr, log, manifest, obs, worker
from tests.helpers import needs_dataset, real_dataset

from ghostaudit.state import (ABSENT, ACTIVE, DEIDENTIFIED, DISABLED, IDENTIFIABLE, PRESENT, RESTORE_ALLOWED, RESTORE_BLOCKED, UNKNOWN,
                              StateReconstructor, assess, category_of, interpret)

AUDIT = "2026-09-15T12:00:00Z"
EARLY, MID, LATE = "2026-09-10T00:00:00Z", "2026-09-14T00:00:00Z", "2026-09-17T00:00:00Z"


def state_of(o, rid=None):
    r = interpret(o, rid)
    return None if r is None else r.state


class AccountShapes(unittest.TestCase):
    def test_log_lookup_rows(self):
        self.assertEqual(state_of(log("e", "account_1", "profile_lookup_completed", {"rows": "0", "http_status": "404"})), ABSENT)
        self.assertEqual(state_of(log("e", "account_1", "profile_lookup_completed", {"rows": "1", "http_status": "200"})), PRESENT)

    def test_misleading_rows_and_http_status_disagree(self):
        r = interpret(log("e", "account_1", "profile_lookup_completed", {"rows": "1", "http_status": "404"}))
        self.assertEqual((r.state, r.basis), (UNKNOWN, "log.profile_lookup_completed.conflict"))

    def test_started_lines_carry_no_state(self):
        self.assertIsNone(interpret(log("e", "account_1", "profile_lookup_started", {"lookup_key": "a@b.co"})))

    def test_json_profile_lookups(self):
        self.assertEqual(state_of(obs("e", "account_2", {"profile": None, "status_code": 404}, collection="profile_lookups")), ABSENT)
        r = interpret(obs("e", "account_2", {"profile": {"email": "a@b.co", "phone": None}, "status_code": 200}, collection="profile_lookups"))
        self.assertEqual((r.state, r.exposed), (PRESENT, ("email",)))

    def test_snapshot_row_means_present(self):
        self.assertEqual(state_of(obs("e", "account_3", {"account_status": "ACTIVE", "email": "a@b.co"}, fmt="sql", collection="customer_profiles")), PRESENT)

    def test_label_records(self):
        self.assertEqual(state_of(obs("e", "account_4", {"record_type": "USER_PROFILE", "result": "NOT_FOUND"})), ABSENT)
        self.assertEqual(state_of(obs("e", "account_4", {"category": "USER_PROFILE", "state": "FOUND"}, fmt="txt")), PRESENT)

    def test_unrecognised_shape_is_unknown_not_guessed(self):
        r = interpret(obs("e", "account_5", {"mystery": 1}, collection="odd"))
        self.assertEqual((r.state, r.basis), (UNKNOWN, "shape.unrecognised"))


class OtherCategories(unittest.TestCase):
    def test_search_hit_vs_miss_and_total(self):
        self.assertEqual(state_of(log("e", "search_1", "index_query_completed", {"result": "hit"})), PRESENT)
        self.assertEqual(state_of(log("e", "search_1", "index_query_completed", {"result": "miss"})), ABSENT)
        self.assertEqual(state_of(obs("e", "search_1", {"hits": {"total": 0, "documents": []}}, collection="index_queries")), ABSENT)
        self.assertEqual(state_of(obs("e", "search_1", {"hits": {"total": 1}}, collection="index_queries")), PRESENT)

    def test_support_message_exposure_vs_redaction(self):
        self.assertEqual(state_of(log("e", "support_1", "message_pii_scan", {"matched_value": "a@b.co"})), IDENTIFIABLE)
        self.assertEqual(state_of(obs("e", "support_2", {"requester_email": "a@b.co", "requester_name": None}, collection="tickets")), IDENTIFIABLE)
        r = interpret(obs("e", "support_3", {"customer_reference": "user_9", "requester_email": None, "requester_name": None, "requester_phone": None}, collection="tickets"))
        self.assertEqual((r.state, r.category), (DEIDENTIFIED, "SUPPORT_MESSAGE"))  # a stable customer reference alone is not exposure
        self.assertEqual(state_of(obs("e", "support_4", {"message_text": "[REDACTED]", "subject_reference": "user_9"}, collection="support_conversations")), DEIDENTIFIED)
        self.assertEqual(state_of(obs("e", "support_5", {"message_text": "please mail bob@x.org"}, collection="support_conversations")), IDENTIFIABLE)

    def test_attachment_is_a_different_category_than_message(self):
        r = interpret(log("e", "support_6", "attachment_pii_scan", {"matched_value": "+81 90 1111 2222"}))
        self.assertEqual((r.category, r.state), ("SUPPORT_ATTACHMENT", PRESENT))
        self.assertEqual(interpret(obs("e", "support_7", {"file_name": "x.pdf"}, collection="ticket_attachments")).category, "SUPPORT_ATTACHMENT")

    def test_analytics_identifiers_vs_anonymised(self):
        self.assertEqual(state_of(obs("e", "analytics_1", {"email": "a@b.co", "phone": None, "email_hash": None}, collection="profiles")), IDENTIFIABLE)
        self.assertEqual(state_of(obs("e", "analytics_2", {"email": None, "phone": None, "email_hash": None, "customer_reference": None}, collection="profiles")), DEIDENTIFIED)
        self.assertEqual(state_of(obs("e", "analytics_3", {"customer_reference": "user_4"}, collection="customer_analytics_profiles")), IDENTIFIABLE)  # still attributable
        self.assertEqual(state_of(log("e", "analytics_4", "analytics_profile_read", {"direct_identifiers": "0"})), DEIDENTIFIED)
        self.assertEqual(state_of(log("e", "analytics_5", "analytics_identity_scan", {"direct_identifiers": "1", "matched_value": "a@b.co"})), IDENTIFIABLE)

    def test_contact_lookup_is_a_marketing_profile_not_analytics(self):
        r = interpret(log("e", "analytics_9", "contact_lookup_completed", {"rows": "1", "correlation_key": "ab" * 32}))
        self.assertEqual((r.category, r.state), ("MARKETING_PROFILE", PRESENT))

    def test_data_flow_enabled_disabled_and_conflicts(self):
        self.assertEqual(state_of(obs("e", "data_flow_1", {"enabled": True}, collection="connector_configurations")), ACTIVE)
        self.assertEqual(state_of(obs("e", "data_flow_1", {"enabled": 0}, fmt="sql", collection="connector_subscriptions")), DISABLED)
        self.assertEqual(state_of(log("e", "data_flow_2", "connector_heartbeat", {"enabled": "true", "scheduler_registered": "true"})), ACTIVE)
        r = interpret(obs("e", "data_flow_3", {"enabled": True, "last_delivery_status": "stopped"}, collection="connectors"))
        self.assertEqual((r.state, r.basis), (UNKNOWN, "flow.enabled_conflicts_with_last_delivery"))

    def test_backup_restore_signals(self):
        self.assertEqual(state_of(obs("e", "backup_1", {"restore_allowed": 1}, fmt="sql", collection="backup_restore_entries")), RESTORE_ALLOWED)
        self.assertEqual(state_of(obs("e", "backup_1", {"restore_allowed": False}, collection="restore_entries")), RESTORE_BLOCKED)
        self.assertEqual(state_of(log("e", "backup_2", "restore_candidate_evaluated", {"eligible": "true", "deletion_block": "false"})), RESTORE_ALLOWED)
        self.assertEqual(state_of(log("e", "backup_2", "restore_candidate_evaluated", {"eligible": "true", "deletion_block": "true"})), RESTORE_BLOCKED)
        self.assertEqual(state_of(log("e", "backup_3", "restore_authorization_completed", {"decision": "deny"})), RESTORE_BLOCKED)

    def test_retained_records(self):
        self.assertEqual(state_of(log("e", "audit_record_1", "audit_record_read_completed", {"rows": "1", "retention_until": "2034-01-01T00:00:00Z"})), PRESENT)
        self.assertEqual(state_of(log("e", "audit_record_1", "audit_record_read_completed", {"rows": "0"})), ABSENT)
        self.assertEqual(state_of(obs("e", "legal_hold_1", {"retained_until": "2033-12-31T23:59:59Z"}, collection="retained_order_records")), PRESENT)

    def test_tombstone_with_and_without_direct_identifiers(self):
        self.assertEqual(state_of(obs("e", "tombstone_1", {"subject_reference": "user_1", "name": None, "email": None, "phone": None, "email_hash": None}, collection="deleted_customer_references")), DEIDENTIFIED)
        r = interpret(obs("e", "tombstone_2", {"subject_reference": "user_2", "name": "Kofi", "email": None, "phone": None, "email_hash": "ab" * 32}, collection="deleted_customer_references"))
        self.assertEqual((r.state, r.exposed), (IDENTIFIABLE, ("name", "email_hash")))

    def test_export_availability_is_preserved_not_decided(self):
        r = interpret(obs("e", "export_1", {"owner_email": "a@b.co", "download_available": False}, collection="customer_export_jobs"))
        self.assertEqual((r.state, dict(r.detail)["download_available"]), (PRESENT, False))

    def test_category_by_prefix(self):
        self.assertEqual([category_of(r) for r in ("account_1", "tombstone_2", "audit_record_3", "data_flow_4", "widget_5")],
                         ["USER_PROFILE", "ACCOUNT_TOMBSTONE", "SECURITY_AUDIT_LOG", "DATA_FLOW", "UNKNOWN"])


class Assessment(unittest.TestCase):
    CASES = [
        ("USER_PROFILE", PRESENT, "VIOLATION", "REPORT_REMAINING_RECORD"), ("USER_PROFILE", ABSENT, "COMPLIANT", None),
        ("SEARCH_INDEX_ENTRY", PRESENT, "VIOLATION", "REPORT_REMAINING_RECORD"),
        ("SUPPORT_MESSAGE", IDENTIFIABLE, "VIOLATION", "REPORT_EXPOSED_PII"), ("SUPPORT_MESSAGE", DEIDENTIFIED, "COMPLIANT", None),
        ("SUPPORT_ATTACHMENT", PRESENT, "VIOLATION", "REPORT_REMAINING_RECORD"),
        ("ANALYTICS_PROFILE", IDENTIFIABLE, "VIOLATION", "REPORT_EXPOSED_PII"), ("ANALYTICS_PROFILE", DEIDENTIFIED, "COMPLIANT", None),
        ("ANALYTICS_PROFILE", PRESENT, "UNKNOWN", None),
        ("ACCOUNT_TOMBSTONE", IDENTIFIABLE, "VIOLATION", "REPORT_EXPOSED_PII"), ("ACCOUNT_TOMBSTONE", DEIDENTIFIED, "COMPLIANT", None),
        ("DATA_FLOW", ACTIVE, "VIOLATION", "REPORT_ACTIVE_DATA_FLOW"), ("DATA_FLOW", DISABLED, "COMPLIANT", None),
        ("BACKUP_ARCHIVE", RESTORE_ALLOWED, "VIOLATION", "REPORT_RESTORE_RISK"), ("BACKUP_ARCHIVE", RESTORE_BLOCKED, "COMPLIANT", None),
        ("BACKUP_ARCHIVE", PRESENT, "UNKNOWN", None),
        ("SECURITY_AUDIT_LOG", PRESENT, "RETAINED_OK", None), ("SECURITY_AUDIT_LOG", ABSENT, "RETAINED_MISSING", None),
        ("LEGAL_HOLD_RECORD", PRESENT, "RETAINED_OK", None),
        ("USER_PROFILE", UNKNOWN, "UNKNOWN", None), ("WIDGET", PRESENT, "UNKNOWN", None),
    ]

    def test_matrix(self):
        for cat, st, verdict, op in self.CASES:
            with self.subTest(cat=cat, state=st):
                a = assess(cat, st)
                self.assertEqual((a.verdict, a.finding_op), (verdict, op))

    def test_deleting_a_retained_record_is_flagged_not_hidden(self):
        self.assertEqual(assess("SECURITY_AUDIT_LOG", ABSENT).verdict, "RETAINED_MISSING")

    def test_over_deletion_of_a_redact_record_is_not_called_a_violation(self):
        self.assertEqual(assess("SUPPORT_MESSAGE", ABSENT).verdict, "COMPLIANT")


class HistoricalState(unittest.TestCase):
    """The state is reconstructed as of audit_at; later evidence never decides it and is listed as ignored."""

    def setUp(self):
        self.results = [
            fr("e_early", [log("e_early", "search_1", "index_query_completed", {"result": "hit"}, observed_at=EARLY)]),
            fr("e_mid", [log("e_mid", "search_1", "index_query_completed", {"result": "miss"}, observed_at=MID)]),
            fr("e_late", [log("e_late", "search_1", "index_query_completed", {"result": "hit"}, observed_at=LATE)]),
        ]
        self.man = manifest(e_early=EARLY, e_mid=MID, e_late=LATE)
        self.rec = StateReconstructor(self.results, self.man)

    def test_latest_usable_reading_wins(self):
        s = self.rec.reconstruct("search_1", AUDIT)
        self.assertEqual((s.state, s.citable), (ABSENT, ("e_mid",)))
        self.assertEqual([e.file_id for e in s.history], ["e_early", "e_mid"])

    def test_later_evidence_is_ignored_listed_with_reason_and_never_cited(self):
        s = self.rec.reconstruct("search_1", AUDIT)
        self.assertEqual([(e.file_id, e.reason, e.eligible) for e in s.excluded], [("e_late", "file_captured_after_audit", False)])
        self.assertNotIn("e_late", s.citable)
        self.assertTrue(any("later reading" in n for n in s.notes))

    def test_moving_the_audit_time_changes_the_answer_only_through_usable_evidence(self):
        self.assertEqual(self.rec.reconstruct("search_1", "2026-09-12T00:00:00Z").state, PRESENT)
        self.assertEqual(self.rec.reconstruct("search_1", AUDIT).state, ABSENT)
        self.assertEqual(self.rec.reconstruct("search_1", "2026-09-18T00:00:00Z").state, PRESENT)

    def test_capture_exactly_at_audit_is_usable(self):
        s = StateReconstructor([fr("e", [log("e", "search_1", "index_query_completed", {"result": "hit"})])], manifest(e=AUDIT)).reconstruct("search_1", AUDIT)
        self.assertEqual((s.state, s.citable), (PRESENT, ("e",)))

    def test_record_time_after_audit_excluded_even_inside_an_early_file(self):
        res = [fr("e", [log("e", "search_1", "index_query_completed", {"result": "hit"}, observed_at=LATE)])]
        s = StateReconstructor(res, manifest(e=EARLY)).reconstruct("search_1", AUDIT)
        self.assertEqual((s.state, s.has_evidence), (UNKNOWN, False))
        self.assertEqual(s.excluded[0].reason, "record_time_after_audit")

    def test_no_usable_evidence_is_unknown_never_a_guess_from_later_files(self):
        res = [fr("e_late", [log("e_late", "account_1", "profile_lookup_completed", {"rows": "0", "http_status": "404"}, observed_at=LATE)])]
        s = StateReconstructor(res, manifest(e_late=LATE)).reconstruct("account_1", AUDIT)
        self.assertEqual((s.state, s.basis, s.citable), (UNKNOWN, "no_usable_evidence", ()))
        self.assertIn("1 later reading(s) ignored", s.notes[0])

    def test_an_uninterpretable_reading_decides_nothing_and_proves_nothing(self):
        res = [fr("e", [obs("e", "account_1", {"mystery": 1}, collection="odd", observed_at=MID)])]
        s = StateReconstructor(res, manifest(e=MID)).reconstruct("account_1", AUDIT)
        self.assertEqual((s.state, s.citable, s.assessment.verdict), (UNKNOWN, (), "UNKNOWN"))

    def test_file_capture_time_is_the_fallback_ordering_key_for_untimed_records(self):
        res = [fr("a", [obs("a", "account_1", {"profile": {"email": "x@y.co"}, "status_code": 200}, collection="profile_lookups")]),
               fr("b", [obs("b", "account_1", {"profile": None, "status_code": 404}, collection="profile_lookups")])]
        s = StateReconstructor(res, manifest(a=EARLY, b=MID)).reconstruct("account_1", AUDIT)
        self.assertEqual(s.state, ABSENT)

    def test_workflow_claims_never_change_state_but_are_surfaced(self):
        res = [fr("e", [log("e", "search_1", "index_query_completed", {"result": "hit"}, observed_at=EARLY)])]
        claims = [worker("activity_1", "search_1", MID)]
        s = StateReconstructor(res, manifest(e=EARLY), claims).reconstruct("search_1", AUDIT)
        self.assertEqual(s.state, PRESENT)  # the worker claims success, but only evidence counts
        self.assertEqual(len(s.claims_after_evidence), 1)
        self.assertIn("activity_1", s.claims_after_evidence[0])


class CopiesAndDrift(unittest.TestCase):
    def test_replica_still_holding_the_record_wins_over_an_empty_primary_at_the_same_instant(self):
        res = [fr("e", [obs("e", "account_1", {"account_status": "ACTIVE", "email": "a@b.co"}, fmt="sql", collection="customer_profiles", copy="customer_data_read_replica", observed_at=MID)])]
        s = StateReconstructor(res, manifest(e=MID)).reconstruct("account_1", AUDIT)
        self.assertEqual(s.state, PRESENT)

    def test_disagreeing_copies_keep_the_worst_case_and_say_so(self):
        res = [fr("e", [obs("e", "search_1", {"hits": {"total": 0}}, collection="index_queries", copy="primary", observed_at=MID),
                        obs("e", "search_1", {"hits": {"total": 1}}, collection="index_queries", copy="replica", observed_at=MID)])]
        s = StateReconstructor(res, manifest(e=MID)).reconstruct("search_1", AUDIT)
        self.assertEqual(s.state, PRESENT)
        self.assertTrue(any("copies disagree" in n for n in s.notes))
        self.assertEqual(len(s.deciding), 1)  # only the copy that decided is cited

    def test_a_later_clean_reading_supersedes_an_earlier_exposed_one(self):
        res = [fr("a", [obs("a", "analytics_1", {"email": "a@b.co"}, collection="profiles", observed_at=EARLY)]),
               fr("b", [obs("b", "analytics_1", {"email": None, "phone": None}, collection="profiles", observed_at=MID)])]
        s = StateReconstructor(res, manifest(a=EARLY, b=MID)).reconstruct("analytics_1", AUDIT)
        self.assertEqual((s.state, s.assessment.verdict), (DEIDENTIFIED, "COMPLIANT"))


@needs_dataset
class RealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.helpers import real_results

        cls.ds = real_dataset()
        cls.results = list(real_results().values())
        cls.rec = StateReconstructor(cls.results, cls.ds.evidence_by_id(), cls.ds.activity.rows)

    def test_every_observation_is_interpretable(self):
        unknown = [(o.file_id, o.locator) for r in self.results for o in r.observations for rid in o.resource_ids()
                   if (rd := interpret(o, rid)) is not None and rd.state == UNKNOWN]
        self.assertEqual(unknown, [])

    def test_replication_drift_case_16(self):
        s = self.rec.reconstruct("account_994", "2026-09-21T10:00:00Z")
        self.assertEqual((s.state, s.citable), (PRESENT, ("evidence_05",)))
        self.assertEqual(s.deciding[0].copy, "customer_data_read_replica")  # absent from the primary table, still in the replica

    def test_case_09_account_has_no_usable_evidence_and_the_later_log_is_listed_as_ignored(self):
        s = self.rec.reconstruct("account_3960", "2026-09-16T09:00:00Z")
        self.assertEqual((s.state, s.has_evidence), (UNKNOWN, False))
        self.assertEqual([(e.file_id, e.reason) for e in s.excluded], [("evidence_06", "file_captured_after_audit")])

    def test_case_27_account_state_is_cited_from_the_usable_file(self):
        s = self.rec.reconstruct("account_3866", "2026-09-15T12:00:00Z")
        self.assertEqual((s.state, s.citable), (ABSENT, ("evidence_92",)))
        self.assertNotIn("evidence_02", s.citable)

    def test_tombstones_case_31_32(self):
        a = self.rec.reconstruct("tombstone_440", "2026-09-15T12:00:00Z")
        b = self.rec.reconstruct("tombstone_9178", "2026-09-15T12:00:00Z")
        self.assertEqual((a.state, a.assessment.verdict, b.state, b.assessment.verdict), (DEIDENTIFIED, "COMPLIANT", IDENTIFIABLE, "VIOLATION"))


class SuppliedRules(unittest.TestCase):
    """deletion_rules.csv must say what the program applies (state.check_rules); a difference stops the run instead of being ignored."""

    # typed from dataset/deletion_rules.csv (category, required_action), independently of state.RULE_ACTIONS
    SUPPLIED = [("rule_01", "USER_PROFILE", "DELETE"), ("rule_02", "SECONDARY_IDENTIFIER", "DELETE"), ("rule_03", "SEARCH_INDEX_ENTRY", "DELETE"),
                ("rule_04", "CACHE_ENTRY", "DELETE"), ("rule_05", "SUPPORT_MESSAGE", "REDACT"), ("rule_06", "SUPPORT_ATTACHMENT", "DELETE"),
                ("rule_07", "ANALYTICS_PROFILE", "ANONYMIZE"), ("rule_08", "PRODUCT_TELEMETRY", "ANONYMIZE"), ("rule_09", "USER_EXPORT", "DELETE"),
                ("rule_10", "MARKETING_PROFILE", "DELETE"), ("rule_11", "EMAIL_DELIVERY_LOG", "REDACT"), ("rule_12", "SECURITY_AUDIT_LOG", "RETAIN"),
                ("rule_13", "LEGAL_HOLD_RECORD", "RETAIN"), ("rule_14", "BACKUP_ARCHIVE", "RETAIN"), ("rule_15", "ACCOUNT_TOMBSTONE", "REDACT")]

    @staticmethod
    def rules(rows):
        from ghostaudit.dataset import Rule
        return [Rule(i, c, "cond", a) for i, c, a in rows]

    def test_the_supplied_rules_agree_with_the_program(self):
        from ghostaudit.state import check_rules
        self.assertEqual(check_rules(self.rules(self.SUPPLIED)), [])

    @needs_dataset
    def test_the_real_file_agrees(self):
        from ghostaudit.state import check_rules
        self.assertEqual(check_rules(real_dataset().rules.rows), [])
        self.assertEqual([(r.rule_id, r.data_category, r.required_action) for r in real_dataset().rules.rows], self.SUPPLIED)

    def test_every_kind_of_difference_is_reported(self):
        from ghostaudit.state import check_rules
        changed = [r if r[1] != "ANALYTICS_PROFILE" else ("rule_07", "ANALYTICS_PROFILE", "DELETE") for r in self.SUPPLIED]
        self.assertEqual(check_rules(self.rules(changed)), ["rule_07: ANALYTICS_PROFILE requires DELETE, but this program applies ANONYMIZE"])
        self.assertEqual(check_rules(self.rules(self.SUPPLIED + [("rule_16", "LOYALTY_POINTS", "DELETE")])), ["rule_16: category LOYALTY_POINTS has no implemented treatment"])
        self.assertEqual(check_rules(self.rules([r for r in self.SUPPLIED if r[1] != "CACHE_ENTRY"])), ["no rule for CACHE_ENTRY, which this program treats as DELETE"])
        twice = check_rules(self.rules(self.SUPPLIED + [("rule_16", "CACHE_ENTRY", "RETAIN")]))
        self.assertEqual(twice, ["rule_16: CACHE_ENTRY requires RETAIN, but this program applies DELETE", "rule_16: CACHE_ENTRY is given two different actions"])

    def test_the_treatments_that_differ_from_a_rule_word_are_explicit(self):
        # rule_14 keeps archives (RETAIN) on condition that restore can be blocked; flows have no rule but must be disabled
        self.assertEqual(assess("BACKUP_ARCHIVE", RESTORE_ALLOWED).finding_op, "REPORT_RESTORE_RISK")
        self.assertEqual(assess("BACKUP_ARCHIVE", RESTORE_BLOCKED).verdict, "COMPLIANT")
        self.assertEqual(assess("DATA_FLOW", ACTIVE).finding_op, "REPORT_ACTIVE_DATA_FLOW")


if __name__ == "__main__":
    unittest.main()
