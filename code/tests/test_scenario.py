import unittest

from tests.factories import case, fr, log, manifest, obs, worker
from tests.helpers import needs_dataset

from ghostaudit.linker import Association
from ghostaudit.scenario import (FRESH, IN_BATCH, NO_SOURCE, NO_WORKFLOW, OFF_BATCH, OUTSIDE, RELEVANT, STALE, UNCERTAIN, UNRELATED,
                                 WorkflowIndex, assess_relevance, freshness)
from ghostaudit.state import StateReconstructor

# a request opened on 08-20, reported on 08-22 12:00, audited on 09-15 12:00
CASE = case("case_1", "a@b.co", "2026-08-20T00:00:00Z", "2026-08-22T12:00:00Z", "2026-09-15T12:00:00Z")
BEFORE, DURING, AFTER = "2026-06-01T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-20T00:00:00Z"
IN_BATCH_T, OFF_T, OUT_T = "2026-08-22T11:00:00Z", "2026-09-05T00:00:00Z", "2026-07-01T00:00:00Z"


def assoc(rid, status="ASSOCIATED", competing=()):
    return Association(rid, None, "user_1", status, (), ("DIRECT",), False, competing)


def state_for(rid, file_time, observed=None, kind="search"):
    o = {"search": log("e", rid, "index_query_completed", {"result": "hit"}, observed_at=observed),
         "audit": log("e", rid, "audit_record_read_completed", {"rows": "1"}, observed_at=observed)}[kind]
    return StateReconstructor([fr("e", [o])], manifest(e=file_time)).reconstruct(rid, "2026-09-15T12:00:00Z")


def rel(rid, rows=(), state=None, link_times=(), siblings=(), association=None, **kw):
    return assess_relevance(CASE, rid, WorkflowIndex(rows), state, association or assoc(rid), link_times, siblings=siblings, **kw)


class WorkflowClassification(unittest.TestCase):
    def kind(self, t):
        return WorkflowIndex([worker("activity_1", "search_1", t)]).classify("search_1", CASE)[0]

    def test_in_batch_off_batch_outside_none(self):
        self.assertEqual(self.kind(IN_BATCH_T), IN_BATCH)
        self.assertEqual(self.kind(OFF_T), OFF_BATCH)
        self.assertEqual(self.kind(OUT_T), OUTSIDE)
        self.assertEqual(self.kind("2026-09-16T00:00:00Z"), OUTSIDE)  # after the audit
        self.assertEqual(WorkflowIndex([]).classify("search_1", CASE)[0], NO_WORKFLOW)

    def test_lifecycle_boundaries_are_inclusive(self):
        self.assertEqual(self.kind("2026-08-20T00:00:00Z"), OFF_BATCH)
        self.assertEqual(self.kind("2026-09-15T12:00:00Z"), OFF_BATCH)

    def test_batch_window_is_a_parameter(self):
        idx = WorkflowIndex([worker("activity_1", "search_1", "2026-08-22T07:00:00Z")])  # 5h before reported_at
        self.assertEqual(idx.classify("search_1", CASE, 4.0)[0], OFF_BATCH)
        self.assertEqual(idx.classify("search_1", CASE, 6.0)[0], IN_BATCH)

    def test_non_worker_and_non_deletion_rows_are_not_workflow(self):
        sync = worker("activity_2", "search_1", IN_BATCH_T, op="SYNC_RECORD")
        other = worker("activity_3", "search_1", IN_BATCH_T)
        object.__setattr__(other, "source_system", "profile_service")
        self.assertEqual(WorkflowIndex([sync, other]).classify("search_1", CASE)[0], NO_WORKFLOW)


class FreshnessSignal(unittest.TestCase):
    def test_fresh_stale_none(self):
        self.assertEqual(freshness(CASE, state_for("search_1", DURING), None), FRESH)
        self.assertEqual(freshness(CASE, state_for("search_1", BEFORE), None), STALE)
        self.assertEqual(freshness(CASE, state_for("search_1", AFTER), None), NO_SOURCE)  # unusable evidence is no source at all

    def test_eligible_link_time_counts_as_a_source(self):
        self.assertEqual(freshness(CASE, None, None, [DURING]), FRESH)
        self.assertEqual(freshness(CASE, None, None, [BEFORE]), STALE)
        self.assertEqual(freshness(CASE, None, None, [AFTER]), NO_SOURCE)


class DecisionTable(unittest.TestCase):
    def test_workflow_in_batch_is_relevant_high(self):
        r = rel("search_1", [worker("activity_1", "search_1", IN_BATCH_T)])
        self.assertEqual((r.label, r.confidence, r.workflow), (RELEVANT, "HIGH", IN_BATCH))
        self.assertEqual(r.workflow_rows, ("activity_1",))

    def test_fresh_evidence_is_relevant_even_if_background_workflow_rows_are_elsewhere(self):
        r = rel("search_1", [worker("activity_1", "search_1", OUT_T)], state_for("search_1", DURING))
        self.assertEqual((r.label, r.workflow, r.freshness), (RELEVANT, OUTSIDE, FRESH))
        self.assertTrue(any("background activity" in x for x in r.reasons))

    def test_retained_record_is_relevant_by_role_without_any_workflow_row(self):
        r = rel("audit_record_1", state=state_for("audit_record_1", AFTER, kind="audit"))  # only post-audit evidence
        self.assertEqual((r.label, r.role, r.confidence), (RELEVANT, "RETAINED", "MEDIUM"))

    def test_misleading_retained_record_with_competing_owners_is_not_relevant(self):
        r = rel("audit_record_1", state=state_for("audit_record_1", AFTER, kind="audit"), association=assoc("audit_record_1", "AMBIGUOUS", ("user_2",)))
        self.assertEqual(r.label, UNCERTAIN)

    def test_claimed_by_a_sibling_case_is_unrelated_high(self):
        sib = case("case_2", "a@b.co", "2026-05-25T00:00:00Z", "2026-05-27T00:00:00Z", "2026-06-02T00:00:00Z")
        r = rel("search_1", state=state_for("search_1", BEFORE, observed="2026-05-30T00:00:00Z"), siblings=[CASE, sib])
        self.assertEqual((r.label, r.confidence, r.claimed_by), (UNRELATED, "HIGH", ("case_2",)))

    def test_misleading_stale_alone_without_a_competing_request_is_only_uncertain(self):
        r = rel("search_1", state=state_for("search_1", BEFORE), siblings=[CASE])
        self.assertEqual((r.label, r.confidence, r.freshness), (UNCERTAIN, "LOW", STALE))
        self.assertTrue(r.included)  # uncertain resources are kept, flagged, never silently dropped

    def test_sibling_that_does_not_cover_the_evidence_time_does_not_claim(self):
        sib = case("case_2", "a@b.co", "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z", "2026-03-05T00:00:00Z")
        r = rel("search_1", state=state_for("search_1", BEFORE, observed="2026-05-30T00:00:00Z"), siblings=[CASE, sib])
        self.assertEqual((r.label, r.claimed_by), (UNCERTAIN, ()))

    def test_workflow_only_elsewhere_and_nothing_fresh_is_unrelated_medium(self):
        r = rel("account_1", [worker("activity_1", "account_1", OUT_T)])
        self.assertEqual((r.label, r.confidence, r.workflow), (UNRELATED, "MEDIUM", OUTSIDE))
        self.assertFalse(r.included)

    def test_off_batch_row_alone_is_uncertain_not_relevant(self):
        r = rel("account_1", [worker("activity_1", "account_1", OFF_T)])
        self.assertEqual((r.label, r.workflow), (UNCERTAIN, OFF_BATCH))

    def test_nothing_at_all_is_uncertain(self):
        self.assertEqual(rel("search_1").label, UNCERTAIN)

    def test_in_batch_beats_staleness_and_notes_it_is_not_overruled_by_a_sibling(self):
        sib = case("case_2", "a@b.co", "2026-05-25T00:00:00Z", "2026-05-27T00:00:00Z", "2026-06-02T00:00:00Z")
        r = rel("search_1", [worker("activity_1", "search_1", IN_BATCH_T)], state_for("search_1", BEFORE, observed="2026-05-30T00:00:00Z"), siblings=[CASE, sib])
        self.assertEqual(r.label, RELEVANT)

    def test_ambiguous_ownership_downgrades_a_workflow_match(self):
        r = rel("search_1", [worker("activity_1", "search_1", IN_BATCH_T)], association=assoc("search_1", "AMBIGUOUS", ("user_2",)))
        self.assertEqual((r.label, r.confidence), (UNCERTAIN, "LOW"))
        self.assertTrue(any("competing owners" in x for x in r.reasons))

    def test_contested_screenshot_ownership_downgrades_an_otherwise_relevant_resource(self):
        shot = obs("e", "marketing_1", {"conflict": True, "visible_identifiers": [], "status_words": [], "foreign_email_count": 1, "ocr": True}, fmt="image")
        st = StateReconstructor([fr("e", [shot], "image")], manifest(e=DURING)).reconstruct("marketing_1", "2026-09-15T12:00:00Z")
        self.assertEqual(st.history[0].basis, "screenshot.conflicting_subject")
        r = rel("marketing_1", state=st)  # fresh evidence would normally make it RELEVANT
        self.assertEqual((r.label, r.confidence), (UNCERTAIN, "LOW"))
        self.assertTrue(any("conflicting ownership" in x for x in r.reasons))

    def test_every_decision_explains_itself(self):
        for r in (rel("search_1"), rel("account_1", [worker("activity_1", "account_1", OUT_T)]), rel("search_2", [worker("activity_2", "search_2", IN_BATCH_T)])):
            self.assertTrue(r.reasons and all(isinstance(x, str) and x for x in r.reasons))


@needs_dataset
class RealParameterRegularities(unittest.TestCase):
    """The empirical windows are validated on resources that are NOT in the sample answers."""

    def test_content_linked_resources_sit_in_the_batch(self):
        from tests.helpers import real_dataset
        from ghostaudit.reconstruction import CaseReconstructor

        cr = CaseReconstructor(real_dataset())
        cases = [q for q in cr.ds.requests.rows if q.case_id not in {f"case_{i}" for i in range(533, 543)}]
        n = inb = 0
        for q in cases:
            u = cr.user_of(q)
            for a in cr.links.associations(u, q.audit_at, __import__("ghostaudit.linker", fromlist=["POLICIES"]).POLICIES["S3k"]):
                if a.system_id == "account_service":
                    n += 1
                    inb += cr.workflow.classify(a.resource_id, q)[0] == IN_BATCH
        self.assertGreater(n, 200)  # 247 content-linked accounts across the 508 eval deletion requests
        self.assertGreaterEqual(inb / n, 0.95)  # measured 238/247; the rest are other-timeline accounts


if __name__ == "__main__":
    unittest.main()
