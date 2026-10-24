import json
import random
import unittest

from tests.helpers import needs_dataset, real_dataset
from tests.world import AT, AUD, POST, PRE, REP, REQ, World, audit_line, clean_world, lf, lookup

from ghostaudit.classification import COMPLETE, INCOMPLETE, NOT_APPLICABLE, CaseVerdict, ClassificationPolicy, Requirement, Violation, classify_all
from ghostaudit.dataset import EvidenceFile
from ghostaudit.plan import PlanPolicy, build_plan, choose_citation, plan_all, rows_for, rows_to_csv
from ghostaudit.schema import OPERATION_STATE, parse_output_csv, validate_output_rows, validate_steps

SNAP_PRE = "2026-09-15T10:30:00Z"


def plan_for(w, cid="case_1", cpol=ClassificationPolicy(), ppol=PlanPolicy()):
    v = classify_all(w.cr, w.ds.requests.rows, cpol)
    return build_plan(v[cid], w.cr.manifest, ppol)


def ops(plan, phase=None):
    return [(s["phase"][:3], s["operation"], s["resource_id"], s["evidence_id"]) for s in plan.steps if phase is None or s["phase"] == phase]


def violations(plan, ev_ids=None):
    return validate_steps(plan.case_id, plan.status, list(plan.steps), ev_ids, allow_empty_deletion_steps=True)


def search_line(ts="2026-09-15T10:10:00Z", rid="search_1", email="ann@x.com", trace="tr-9"):
    return lf(ts, "search_service", "index_query_completed", rid, trace=trace, result="hit", email=email)


FULL = [
    search_line(),
    lf("2026-09-15T10:11:00Z", "support_platform", "support_ticket_read_completed", "support_1", trace="tr-s", requester_email="ann@x.com"),
    lf("2026-09-15T10:12:00Z", "integration_service", "connector_config_read_completed", "data_flow_1", trace="tr-f", enabled="true", last_delivery_status="delivered", customer_email="ann@x.com"),
    lf("2026-09-15T10:13:02Z", "backup_service", "restore_policy_read_completed", "backup_1", trace="tr-b", archive_present="true", restore_allowed="true", customer_email="ann@x.com"),
    lf("2026-09-15T10:14:00Z", "analytics_platform", "analytics_profile_read_completed", "analytics_1", trace="tr-a", email="ann@x.com"),
]


class NotApplicable(unittest.TestCase):
    def test_empty_steps_and_row(self):
        w = World().case("case_1", text="Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.").build()
        p = plan_for(w)
        self.assertEqual((p.status, p.steps, p.steps_json()), (NOT_APPLICABLE, (), "[]"))
        self.assertEqual(rows_for({"case_1": p}, ["case_1"]), [{"case_id": "case_1", "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"}])
        w.close()


class IncompleteCase(unittest.TestCase):
    def setUp(self):
        self.w = clean_world(FULL).build()
        self.p = plan_for(self.w)

    def tearDown(self):
        self.w.close()

    def test_findings_are_cited_remediation_and_verification_are_not(self):
        self.assertEqual(self.p.status, INCOMPLETE)
        for s in self.p.steps:
            self.assertEqual(bool(s["evidence_id"]), s["phase"] == "FINDING", s)
        self.assertTrue(all(s["evidence_id"] == "evidence_1" for s in self.p.steps if s["phase"] == "FINDING"))

    def test_each_violation_gets_the_right_finding_remediation_and_verification(self):
        by = {r: [] for r in ("search_1", "support_1", "data_flow_1", "backup_1", "analytics_1")}
        for s in self.p.steps:
            if s["resource_id"] in by:
                by[s["resource_id"]].append(s["operation"])
        self.assertEqual(by["search_1"], ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])
        self.assertEqual(by["support_1"], ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"])
        self.assertEqual(by["analytics_1"], ["REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"])
        self.assertEqual(by["data_flow_1"], ["REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"])
        self.assertEqual(by["backup_1"], ["REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"])

    def test_flows_and_restores_are_stopped_before_records_are_changed(self):
        rem = [s["operation"] for s in self.p.steps if s["phase"] == "REMEDIATION"]
        self.assertEqual(rem, ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"])

    def test_verification_covers_remediated_items_the_account_and_the_retained_record(self):
        ver = {(s["resource_id"], s["operation"]) for s in self.p.steps if s["phase"] == "VERIFICATION"}
        self.assertIn(("account_10", "VERIFY_RECORD_ABSENT"), ver)  # already absent, but a check that must still pass afterwards
        self.assertIn(("audit_record_1", "VERIFY_RECORD_PRESENT"), ver)
        self.assertEqual(len(ver), 7)

    def test_steps_are_numbered_ordered_and_state_matches_operation(self):
        self.assertEqual([s["step_no"] for s in self.p.steps], list(range(1, len(self.p.steps) + 1)))
        phases = [s["phase"] for s in self.p.steps]
        self.assertEqual(phases, sorted(phases, key=("FINDING", "REMEDIATION", "VERIFICATION").index))
        self.assertTrue(all(s["state"] == OPERATION_STATE[s["operation"]] for s in self.p.steps))
        self.assertEqual(violations(self.p, ["evidence_1"]), [])

    def test_already_compliant_untouched_resources_are_not_added_as_checks(self):
        lines = FULL + [lf("2026-09-15T10:15:00Z", "integration_service", "connector_config_read_completed", "data_flow_2", trace="tr-g", enabled="false", last_delivery_status="stopped", customer_email="ann@x.com")]
        w = clean_world(lines).build()
        p = plan_for(w)
        self.assertNotIn("data_flow_2", {s["resource_id"] for s in p.steps})
        w.close()

    def test_account_that_is_present_is_found_remediated_and_verified(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com", rows=1) + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]).build()
        p = plan_for(w)
        self.assertEqual([o[1:3] for o in ops(p) if o[2] == "account_10"], [("REPORT_REMAINING_RECORD", "account_10"), ("DELETE_RECORD", "account_10"), ("VERIFY_RECORD_ABSENT", "account_10")])
        w.close()

    def test_missing_retained_record_has_no_finding_because_no_operation_exists(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1", rows=0)]).build()
        p = plan_for(w)
        self.assertEqual(p.status, INCOMPLETE)
        self.assertFalse(any(s["phase"] == "FINDING" for s in p.steps))
        self.assertTrue(any("no operation for a missing retained record" in x.reason for x in p.withheld))
        w.close()


class CompleteCase(unittest.TestCase):
    def test_direct_proofs_become_cited_verification_steps_only(self):
        w = clean_world().build()
        p = plan_for(w)
        self.assertEqual((p.status, p.determination), (COMPLETE, "PROVEN"))
        self.assertEqual(ops(p), [("VER", "VERIFY_RECORD_ABSENT", "account_10", "evidence_1"), ("VER", "VERIFY_RECORD_PRESENT", "audit_record_1", "evidence_1")])
        self.assertEqual(violations(p, ["evidence_1"]), [])
        w.close()

    def test_every_complete_step_cites_evidence_usable_at_audit_time(self):
        w = clean_world().log("evidence_2", POST, lookup("2026-09-16T10:00:00Z", "account_10", "ann@x.com", rows=1, trace="tr-2")).build()
        p = plan_for(w)
        self.assertEqual({s["evidence_id"] for s in p.steps}, {"evidence_1"})
        w.close()

    def test_flow_disabled_and_restore_blocked_proofs_are_emitted(self):
        lines = [lf("2026-09-15T10:12:00Z", "integration_service", "connector_config_read_completed", "data_flow_1", trace="tr-f", enabled="false", last_delivery_status="stopped", customer_email="ann@x.com"),
                 lf("2026-09-15T10:13:02Z", "backup_service", "restore_policy_read_completed", "backup_1", trace="tr-b", archive_present="true", restore_allowed="false", customer_email="ann@x.com")]
        p = plan_for(clean_world(lines).build())
        self.assertEqual({s["operation"] for s in p.steps}, {"VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"})

    def test_nothing_provable_gives_complete_with_no_steps_and_says_so(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").log("evidence_1", PRE, [lf("2026-09-15T10:00:00Z", "x", "noop")]).build()
        p = plan_for(w)
        self.assertEqual((p.status, p.determination, p.steps, p.steps_json()), (COMPLETE, "PARTIAL", (), "[]"))
        self.assertTrue(p.withheld)
        v = validate_steps("case_1", p.status, [], allow_empty_deletion_steps=False)
        self.assertEqual([x.code for x in v], ["empty_steps"])  # flagged unless explicitly allowed
        self.assertEqual(violations(p), [])
        w.close()


class InferredEvidenceIsNotDirectProof(unittest.TestCase):
    @staticmethod
    def bare(ts, rid, rows):
        return lf(ts, "account_service", "profile_lookup_completed", rid, trace=f"tr-{rid}", rows=rows, http_status=200 if rows else 404)

    def strong_world(self):
        # the account is named only by activity; an explicit 404 names the account but no identifier ties it to the subject
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, [self.bare("2026-09-15T10:00:00Z", "account_10", 0), audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])
        return w.build()

    def test_activity_attributed_account_is_not_proof_of_absence_by_default(self):
        w = self.strong_world()
        v = classify_all(w.cr, w.ds.requests.rows)["case_1"]
        self.assertEqual((v.status, v.determination), (COMPLETE, "INFERRED"))  # M4 still reports it as supported
        p = build_plan(v, w.cr.manifest)
        self.assertEqual([o[2] for o in ops(p)], ["audit_record_1"])
        self.assertTrue(any(x.resource_id == "account_10" and "inferred_identity" in x.reason for x in p.withheld))
        w.close()

    def test_the_switch_emits_it_with_the_same_citation(self):
        w = self.strong_world()
        p = plan_for(w, ppol=PlanPolicy(emit_inferred_proofs=True))
        self.assertIn(("VER", "VERIFY_RECORD_ABSENT", "account_10", "evidence_1"), ops(p))
        w.close()

    def test_scoped_snapshot_support_is_withheld_but_a_direct_retained_proof_beside_it_is_kept(self):
        doc = {"exported_at": SNAP_PRE, "snapshots": [
            {"captured_at": SNAP_PRE, "data_store": "account_service", "collection": "customer_profiles", "records": []},
            {"captured_at": SNAP_PRE, "data_store": "security_service", "collection": "security_audit_events",
             "records": [{"resource_id": "audit_record_1", "subject_reference": "user_1", "event_name": "account_deletion_reviewed"}]}]}
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").json("evidence_2", SNAP_PRE, doc).build()
        p = plan_for(w)
        self.assertEqual([(o[1], o[2]) for o in ops(p)], [("VERIFY_RECORD_PRESENT", "audit_record_1")])
        self.assertTrue(any(x.resource_id == "account_10" and "scoped_snapshot" in x.reason and x.would_cite == ("evidence_2",) for x in p.withheld))
        w.close()

    def test_future_check_for_an_inferred_account_in_an_incomplete_case_is_a_check_not_a_proof(self):
        w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
        w.log("evidence_1", PRE, [self.bare("2026-09-15T10:00:00Z", "account_10", 0), audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), search_line()]).build()
        p = plan_for(w)
        self.assertIn(("VER", "VERIFY_RECORD_ABSENT", "account_10", ""), ops(p))
        p2 = plan_for(w, ppol=PlanPolicy(include_inferred_account_checks=False))
        self.assertNotIn("account_10", {s["resource_id"] for s in p2.steps})
        self.assertTrue(any(x.resource_id == "account_10" for x in p2.withheld))
        w.close()


class UnresolvedAccounts(unittest.TestCase):
    def world(self, extra=()):
        w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
        w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
        w.log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), audit_line("2026-09-15T10:06:00Z", "audit_record_2", "user_2")] + list(extra))
        return w.build()

    def test_no_account_step_in_a_complete_case(self):
        w = self.world()
        p = plan_for(w)
        self.assertFalse(any(s["resource_id"].startswith("account_") for s in p.steps))
        self.assertTrue(any(x.resource_id is None and "ACCOUNT_AMBIGUOUS" in x.reason for x in p.withheld))
        w.close()

    def test_no_account_step_in_an_incomplete_case_either(self):
        w = self.world([search_line()])
        p = plan_for(w)
        self.assertEqual(p.status, INCOMPLETE)
        self.assertFalse(any(s["resource_id"] in ("account_10", "account_11") for s in p.steps))
        w.close()

    def test_set_wise_proof_names_no_resource_and_yields_no_step(self):
        bare = lambda ts, rid: lf(ts, "account_service", "profile_lookup_completed", rid, trace=f"tr-{rid}", rows=0, http_status=404)  # noqa: E731
        w = self.world([bare("2026-09-15T10:00:00Z", "account_10"), bare("2026-09-15T10:01:00Z", "account_11")])
        v = classify_all(w.cr, w.ds.requests.rows)["case_1"]
        self.assertEqual((v.status, v.determination), (COMPLETE, "INFERRED"))
        p = build_plan(v, w.cr.manifest)
        self.assertFalse(any(s["resource_id"].startswith("account_") for s in p.steps))
        self.assertTrue(any("set_wise" in x.reason for x in p.withheld))
        w.close()


class HandBuiltVerdicts(unittest.TestCase):
    """The step builder must not trust the verdict's shape: it re-applies its own rules."""

    MAN = {"evidence_1": EvidenceFile("evidence_1", "a", "o", "2026-09-15T10:30:00Z")}

    @staticmethod
    def verdict(status, violations=(), requirements=(), counted=None, gaps=()):
        from ghostaudit.intent import IntentResult

        return CaseVerdict("case_1", status, "PROVEN", "HIGH", IntentResult("DELETION", "t", ()), tuple(violations), tuple(requirements), tuple(gaps), (), (), "default",
                           tuple(violations if counted is None else counted))

    @staticmethod
    def viol(rid, op="REPORT_REMAINING_RECORD", act="DELETE", ev=("evidence_1",)):
        return Violation(rid, None, "SEARCH_INDEX", op, act, ev, "HIGH", "log")

    @staticmethod
    def req(rid, status="PROVEN", ev=("evidence_1",), key="RESOURCE_TREATED", op="VERIFY_RECORD_ABSENT", kind="direct"):
        return Requirement(key, rid, "SEARCH_INDEX", op, status, "HIGH" if status == "PROVEN" else "LOW", ev, "log", "r", kind=kind)

    def test_an_unproven_requirement_is_never_emitted_even_if_it_carries_evidence(self):
        p = build_plan(self.verdict(COMPLETE, requirements=[self.req("search_1", status="UNPROVEN")]), self.MAN)
        self.assertEqual(p.steps, ())
        self.assertTrue(any(w.resource_id == "search_1" and "unproven" in w.reason for w in p.withheld))

    def test_not_applicable_emits_nothing_whatever_the_verdict_holds(self):
        v = self.verdict(NOT_APPLICABLE, violations=[self.viol("search_1")], requirements=[self.req("search_1")])
        self.assertEqual(build_plan(v, self.MAN).steps, ())

    def test_duplicate_violations_collapse_to_one_step_each_with_consecutive_numbers(self):
        v = self.verdict(INCOMPLETE, violations=[self.viol("search_1"), self.viol("search_1")])
        p = build_plan(v, self.MAN)
        self.assertEqual([(s["step_no"], s["operation"]) for s in p.steps], [(1, "REPORT_REMAINING_RECORD"), (2, "DELETE_RECORD"), (3, "VERIFY_RECORD_ABSENT")])

    def test_only_counted_violations_become_findings(self):
        v = self.verdict(INCOMPLETE, violations=[self.viol("search_1"), self.viol("search_2")], counted=[self.viol("search_1")])
        p = build_plan(v, self.MAN)
        self.assertEqual({s["resource_id"] for s in p.steps}, {"search_1"})

    def test_a_violation_without_usable_evidence_is_not_reported(self):
        p = build_plan(self.verdict(INCOMPLETE, violations=[self.viol("search_1", ev=())]), self.MAN)
        self.assertEqual(p.steps, ())
        self.assertTrue(any("no usable evidence" in w.reason for w in p.withheld))


class CitationChoice(unittest.TestCase):
    MAN = {"evidence_5": EvidenceFile("evidence_5", "a", "o", "2026-09-10T00:00:00Z"), "evidence_6": EvidenceFile("evidence_6", "b", "o", "2026-09-12T00:00:00Z"),
           "evidence_7": EvidenceFile("evidence_7", "c", "o", "2026-09-12T00:00:00Z")}

    def test_latest_capture_wins_ties_prefer_the_lower_id(self):
        self.assertEqual(choose_citation(["evidence_5", "evidence_6", "evidence_7"], self.MAN), "evidence_6")
        self.assertEqual(choose_citation(["evidence_5"], self.MAN), "evidence_5")

    def test_order_independent_and_empty(self):
        ids = ["evidence_7", "evidence_5", "evidence_6"]
        for _ in range(5):
            random.shuffle(ids)
            self.assertEqual(choose_citation(ids, self.MAN), "evidence_6")
        self.assertEqual(choose_citation([], self.MAN), "")


class RowsAndCsv(unittest.TestCase):
    def test_rows_sorted_naturally_and_csv_roundtrips_through_the_validator(self):
        w = clean_world(FULL).case("case_10", text="Please change the email address used for this account. I want to continue using the service.").build()
        plans = plan_all(classify_all(w.cr, w.ds.requests.rows), w.cr.manifest)
        rows = rows_for(plans, ["case_10", "case_1"])
        self.assertEqual([r["case_id"] for r in rows], ["case_1", "case_10"])
        text = rows_to_csv(rows)
        self.assertTrue(text.startswith("case_id,deletion_status,steps_json\r\n"))
        back = parse_output_csv(text)
        self.assertEqual(back, rows)
        self.assertEqual(validate_output_rows(back, ["case_1", "case_10"], ["evidence_1"], allow_empty_deletion_steps=True), [])
        step = json.loads(back[0]["steps_json"])[0]
        self.assertEqual(list(step), ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"])
        w.close()

    def test_serialisation_is_compact_utf8_and_stable(self):
        w = clean_world().build()
        p = plan_for(w)
        self.assertEqual(p.steps_json(), plan_for(w).steps_json())
        self.assertNotIn(" ", p.steps_json().replace('"', "").replace("_", "")[:0] + "")  # no pretty-printing
        self.assertNotIn(", ", p.steps_json())
        w.close()

    def test_row_and_plan_order_does_not_depend_on_input_order(self):
        w = clean_world(FULL).case("case_2", "bob@x.com").build()
        v = classify_all(w.cr, w.ds.requests.rows)
        base = {c: p.steps_json() for c, p in plan_all(v, w.cr.manifest).items()}
        rng = random.Random(2)
        for _ in range(4):
            items = list(v.items())
            rng.shuffle(items)
            self.assertEqual({c: p.steps_json() for c, p in plan_all(dict(items), w.cr.manifest).items()}, base)
        w.close()


@needs_dataset
class RealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ghostaudit.reconstruction import CaseReconstructor
        from ghostaudit.scoring import score

        cls.ds = real_dataset()
        cls.cr = CaseReconstructor(cls.ds)
        cls.cases = cls.ds.requests.rows + cls.ds.sample_requests.rows
        cls.v = classify_all(cls.cr, cls.cases)
        cls.plans = plan_all(cls.v, cls.cr.manifest)
        cls.gold = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in cls.ds.sample_output}
        cls.score = score

    def test_all_518_rows_are_schema_valid_ordered_and_cite_known_ids(self):
        ev = [f.file_id for f in self.ds.evidence_files.rows] + [a.activity_id for a in self.ds.activity.rows] + [a.audit_id for a in self.ds.audit_log.rows]
        ids = [q.case_id for q in self.ds.requests.rows]
        rows = rows_for(self.plans, ids)
        self.assertEqual(len(rows), 518)
        self.assertEqual(validate_output_rows(rows, ids, ev, allow_empty_deletion_steps=True), [])

    def test_no_citation_postdates_its_audit_time(self):
        from ghostaudit.timeutil import is_eligible

        man = self.ds.evidence_by_id()
        audit = {q.case_id: q.audit_at for q in self.cases}
        for c, p in self.plans.items():
            for s in p.steps:
                if s["evidence_id"]:
                    self.assertTrue(is_eligible(man[s["evidence_id"]].captured_at, audit[c]), (c, s))

    def test_incomplete_has_future_steps_uncited_and_complete_has_only_cited_verification(self):
        for c, p in self.plans.items():
            if p.status == COMPLETE:
                self.assertTrue(all(s["phase"] == "VERIFICATION" and s["evidence_id"] for s in p.steps), c)
            if p.status == INCOMPLETE:
                self.assertTrue(all(bool(s["evidence_id"]) == (s["phase"] == "FINDING") for s in p.steps), c)
            if p.status == NOT_APPLICABLE:
                self.assertEqual(p.steps, (), c)

    def test_sample_status_findings_remediation_and_verification_agreement(self):
        samples = [q.case_id for q in self.ds.sample_requests.rows]
        self.assertTrue(all(self.plans[c].status == self.gold[c][0] for c in samples))
        tallies = {}
        for ph in ("FINDING", "REMEDIATION", "VERIFICATION"):
            m = miss = uns = 0
            for c in samples:
                g = {(s["resource_id"], s["operation"]) for s in self.gold[c][1] if s["phase"] == ph}
                p = {(s["resource_id"], s["operation"]) for s in self.plans[c].steps if s["phase"] == ph}
                m += len(g & p); miss += len(g - p); uns += len(p - g)
            tallies[ph] = (m, miss, uns)
        self.assertEqual(tallies, {"FINDING": (41, 0, 0), "REMEDIATION": (41, 0, 0), "VERIFICATION": (92, 19, 0)})

    def test_a_finding_always_carries_the_answers_cited_file_on_samples(self):
        for c in (q.case_id for q in self.ds.sample_requests.rows):
            g = {(s["resource_id"], s["operation"]): s["evidence_id"] for s in self.gold[c][1] if s["phase"] == "FINDING"}
            p = {(s["resource_id"], s["operation"]): s["evidence_id"] for s in self.plans[c].steps if s["phase"] == "FINDING"}
            self.assertEqual(g, p, c)

    def test_no_step_is_unsupported_on_samples_and_no_unresolved_account_is_named(self):
        samples = [q.case_id for q in self.ds.sample_requests.rows]
        for c in samples:
            g = {(s["resource_id"], s["operation"]) for s in self.gold[c][1]}
            self.assertLessEqual({(s["resource_id"], s["operation"]) for s in self.plans[c].steps}, g, c)
        for c in ("case_23", "case_28", "case_29", "case_04", "case_08"):
            self.assertFalse(any(s["resource_id"].startswith("account_") for s in self.plans[c].steps), c)

    def test_inferred_proofs_are_withheld_on_samples_21_25_31(self):
        for c, rid in (("case_21", "account_6440"), ("case_25", "account_3136"), ("case_31", "account_4857")):
            self.assertNotIn(rid, {s["resource_id"] for s in self.plans[c].steps}, c)
            self.assertTrue(any(w.resource_id == rid and w.would_cite for w in self.plans[c].withheld), c)

    def test_complete_cases_with_nothing_provable(self):
        empty = sorted(c for c, p in self.plans.items() if p.status == COMPLETE and not p.steps)
        self.assertEqual(empty, ["case_01", "case_02", "case_04", "case_45", "case_55", "case_63"])

    def test_uncertain_screenshot_evidence_never_becomes_a_finding_or_a_proof(self):
        self.assertFalse(any(s["resource_id"] == "tombstone_17" for s in self.plans["case_46"].steps))
        self.assertFalse(any(s["resource_id"] == "marketing_001" for s in self.plans["case_63"].steps))
        self.assertTrue(all(s["evidence_id"] != "evidence_42" for p in self.plans.values() for s in p.steps))
        self.assertTrue(all(s["evidence_id"] != "evidence_60" for p in self.plans.values() for s in p.steps))

    def test_emit_inferred_switch_only_adds_cited_steps_and_never_names_unresolved_accounts(self):
        more = plan_all(self.v, self.cr.manifest, PlanPolicy(emit_inferred_proofs=True))
        added = 0
        for c, p in more.items():
            base = {(s["resource_id"], s["operation"]) for s in self.plans[c].steps}
            for s in p.steps:
                if (s["resource_id"], s["operation"]) not in base:
                    added += 1
                    self.assertTrue(s["evidence_id"], (c, s))
        self.assertGreater(added, 0)
        for c in ("case_23", "case_28", "case_29"):
            self.assertFalse(any(s["resource_id"] in ("account_410", "account_5482", "account_7012") for s in more[c].steps))

    def test_findings_follow_the_policys_counted_violations_only(self):
        # case_07's only violation is PDF-derived (MEDIUM): reported by default, never reported when the policy demands HIGH evidence
        default = self.plans["case_07"]
        self.assertEqual([s["resource_id"] for s in default.steps if s["phase"] == "FINDING"], ["support_001"])
        strict = plan_all(classify_all(self.cr, self.ds.sample_requests.rows, ClassificationPolicy("highonly", accept_medium_violations=False)), self.cr.manifest)["case_07"]
        self.assertEqual((strict.status, [s for s in strict.steps if s["phase"] == "FINDING"]), (COMPLETE, []))
        self.assertNotIn("support_001", {s["resource_id"] for s in strict.steps})

    def test_predict_cli_writes_a_valid_file_and_refuses_the_repository_root(self):
        import subprocess, sys, tempfile
        from pathlib import Path
        from tests.helpers import CODE_DIR, DATASET

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "pred.csv"
            r = subprocess.run([sys.executable, "-m", "ghostaudit", "--dataset", str(DATASET), "predict", "--samples", "--out", str(out)], cwd=CODE_DIR, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = parse_output_csv(out.read_bytes().decode("utf-8"))
            self.assertEqual([x["case_id"] for x in rows], [f"case_{i:02d}" for i in range(1, 33)])
            self.assertEqual(validate_output_rows(rows, None, None, allow_empty_deletion_steps=True), [])
        root = subprocess.run([sys.executable, "-m", "ghostaudit", "--dataset", str(DATASET), "predict", "--out", str(DATASET.parent / "output.csv")], cwd=CODE_DIR, capture_output=True, text=True)
        self.assertEqual(root.returncode, 2)
        self.assertIn("refusing", root.stderr)
        self.assertFalse((DATASET.parent / "output.csv").exists())

    def test_two_full_runs_are_identical(self):
        from ghostaudit.reconstruction import CaseReconstructor

        again = plan_all(classify_all(CaseReconstructor(self.ds), self.cases), self.cr.manifest)
        self.assertEqual({c: p.steps_json() for c, p in again.items()}, {c: p.steps_json() for c, p in self.plans.items()})


if __name__ == "__main__":
    unittest.main()
