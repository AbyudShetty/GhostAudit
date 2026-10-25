"""Files captured after audit_at, under the two link policies.

* S2 (strict reading of AGENTS.md 6.2: "Evidence created later must not change the answer"): adding a later file that names a resource
  with the subject's identifier leaves the answer (status, determination, steps, citations) exactly as without it, and the file stays
  visible as a diagnostic note. These tests run with link_policy="S2".
* S3k (the default; reads 6.2 together with 6.1, see linker.DEFAULT_POLICY): a later file may say *who owns* an account or retained
  record, but it never sets state, is never cited, and never makes an activity-inferred proof possible (LATER_OWNERSHIP).

The expected outcomes follow from those rules, not from the implementation."""

import unittest

from tests.world import POST, PRE, World, audit_line, lf, lookup

from ghostaudit.attribution import AMBIGUOUS, CONTENT, STRONG
from ghostaudit.classification import classify_all
from ghostaudit.plan import build_plan


def bare(ts, rid, rows):
    """An account lookup that names only the resource id: no identifier of anyone."""
    return lf(ts, "account_service", "profile_lookup_completed", rid, trace=f"tr-{rid}", rows=rows, http_status=200 if rows else 404)


S2 = "S2"


def answer(w, cid="case_1"):
    v = classify_all(w.cr, w.ds.requests.rows)[cid]
    p = build_plan(v, w.cr.manifest)
    return v.status, v.determination, p.steps_json()


def attribution(w, cid="case_1"):
    a = w.cr.reconstruct_all(w.ds.requests.rows)[cid].account
    return a.status, a.account, tuple(c.resource_id for c in a.candidates)


def later_file(w, rid, email, rows=0):
    return w.log("evidence_9", POST, lookup("2026-09-16T10:00:00Z", rid, email, rows=rows, trace="tr-later"))


class UnderS2LaterIdentityEvidenceChangesNothing(unittest.TestCase):
    def check(self, make, add_later, cid="case_1"):
        base = make().build(link_policy=S2)
        with_later = add_later(make()).build(link_policy=S2)
        try:
            self.assertEqual(attribution(with_later, cid), attribution(base, cid))  # attribution cannot use it either
            self.assertEqual(answer(with_later, cid), answer(base, cid))
            notes = with_later.cr.reconstruct_all(with_later.ds.requests.rows)
            self.assertTrue(any("via evidence_9" in n for r in notes.values() for n in r.notes), "the later file must stay visible as a diagnostic")
            return base, with_later
        finally:
            base.close()
            with_later.close()

    def test_a_later_file_does_not_create_a_proof(self):
        # reproduction A: a pre-audit 404 names account_10 (no identifier); only a later file ties account_10 to the subject
        make = lambda: World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").log(  # noqa: E731
            "evidence_1", PRE, [bare("2026-09-15T10:00:00Z", "account_10", 0), audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])
        base, _ = self.check(make, lambda w: later_file(w, "account_10", "ann@x.com"))
        status, det, steps = answer(make().build(link_policy=S2))
        self.assertEqual((status, det), ("COMPLETE", "INFERRED"))
        self.assertIn('"resource_id":"account_10","operation":"VERIFY_RECORD_ABSENT","state":"ABSENT","evidence_id":"evidence_1"', steps)  # the inferred-identity proof, from pre-audit evidence only

    def test_a_later_file_does_not_change_the_status(self):
        # reproduction B: a pre-audit read shows account_10 with a profile but no identifier; no worker row ties it to the request
        make = lambda: World().case("case_1").log(  # noqa: E731
            "evidence_1", PRE, [bare("2026-09-15T10:00:00Z", "account_10", 1), audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])
        self.check(make, lambda w: later_file(w, "account_10", "ann@x.com", rows=1))
        w = make().build(link_policy=S2)
        self.assertEqual(answer(w)[0], "COMPLETE")  # what was knowable at audit_at does not show the subject's profile
        w.close()

    def test_a_later_file_for_one_subject_does_not_resolve_another_cases_account(self):
        # reproduction C: two subjects, two indistinguishable worker rows; a later file says account_11 is bob's
        def make():
            w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
            w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
            return w.log("evidence_1", PRE, [bare("2026-09-15T10:00:00Z", "account_10", 0), bare("2026-09-15T10:01:00Z", "account_11", 0),
                                             audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), audit_line("2026-09-15T10:06:00Z", "audit_record_2", "user_2")])
        for cid in ("case_1", "case_2"):
            self.check(make, lambda w: later_file(w, "account_11", "bob@x.com"), cid)
        w = later_file(make(), "account_11", "bob@x.com").build(link_policy=S2)
        att = w.cr.reconstruct_all(w.ds.requests.rows)
        self.assertEqual((att["case_1"].account.status, att["case_2"].account.status), (AMBIGUOUS, AMBIGUOUS))
        w.close()

    def test_a_proof_supported_by_pre_audit_identity_is_kept(self):
        # the subject's e-mail already names account_10 in a pre-audit lookup: a later file that says the same changes nothing
        make = lambda: World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").log(  # noqa: E731
            "evidence_1", PRE, lookup("2026-09-15T10:00:00Z", "account_10", "ann@x.com") + [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])
        w = make().build(link_policy=S2)
        self.assertEqual(answer(w)[:2], ("COMPLETE", "PROVEN"))
        self.assertIn('"resource_id":"account_10","operation":"VERIFY_RECORD_ABSENT","state":"ABSENT","evidence_id":"evidence_1"', answer(w)[2])
        self.assertEqual(w.cr.reconstruct_all(w.ds.requests.rows)["case_1"].account.status, CONTENT)
        w.close()
        later = later_file(make(), "account_10", "ann@x.com").build(link_policy=S2)
        self.assertEqual(answer(later), answer(make().build(link_policy=S2)))
        later.close()

    def test_a_later_file_never_becomes_a_citation(self):
        w = later_file(World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").log(
            "evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")]), "account_10", "ann@x.com").build(link_policy=S2)
        self.assertNotIn("evidence_9", answer(w)[2])
        w.close()


class UnderS3kLaterFilesIdentifyButNeverProve(unittest.TestCase):
    """The default policy: a later file may name the owner of an account; state and citations stay at audit_at."""

    def make_404(self):
        return World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z").log(
            "evidence_1", PRE, [bare("2026-09-15T10:00:00Z", "account_10", 0), audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])

    def test_the_later_file_identifies_the_account_but_the_proof_cites_the_pre_audit_404(self):
        w = later_file(self.make_404(), "account_10", "ann@x.com").build()
        self.assertEqual(attribution(w)[:2], (CONTENT, "account_10"))
        status, det, steps = answer(w)
        self.assertIn('"resource_id":"account_10","operation":"VERIFY_RECORD_ABSENT","state":"ABSENT","evidence_id":"evidence_1"', steps)
        self.assertNotIn("evidence_9", steps)  # the later file is never cited
        w.close()

    def test_a_later_file_never_sets_state(self):
        # the later file shows the account *present*; at audit_at only the 404 exists, so the account is absent and the case COMPLETE
        w = later_file(self.make_404(), "account_10", "ann@x.com", rows=1).build()
        self.assertEqual(answer(w)[0], "COMPLETE")
        d = w.cr.reconstruct_all(w.ds.requests.rows)["case_1"].decision("account_10")
        self.assertEqual((d.state.state, d.state.citable), ("ABSENT", ("evidence_1",)))
        self.assertEqual([(e.file_id, e.reason) for e in d.state.excluded], [("evidence_9", "file_captured_after_audit")])
        w.close()

    def test_an_inferred_proof_never_rests_on_a_rival_ruled_out_by_a_later_file(self):
        # two subjects, two indistinguishable worker rows; only a later file says account_11 is bob's
        w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
        w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
        w.log("evidence_1", PRE, [bare("2026-09-15T10:00:00Z", "account_10", 0), bare("2026-09-15T10:01:00Z", "account_11", 0),
                                  audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), audit_line("2026-09-15T10:06:00Z", "audit_record_2", "user_2")])
        w = later_file(w, "account_11", "bob@x.com").build()
        rec = w.cr.reconstruct_all(w.ds.requests.rows)["case_1"]
        self.assertEqual((rec.account.status, rec.account.account, rec.account.later_ownership), (STRONG, "account_10", True))
        self.assertNotIn("account_10", answer(w)[2])  # attribution may use it (S3k); a proof may not
        p = build_plan(classify_all(w.cr, w.ds.requests.rows)["case_1"], w.cr.manifest)
        self.assertTrue(any(x.resource_id == "account_10" and "later_ownership" in x.reason for x in p.withheld))
        w.close()


if __name__ == "__main__":
    unittest.main()
