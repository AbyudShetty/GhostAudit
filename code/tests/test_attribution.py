import random
import unittest

from tests.factories import case, fr, manifest, obs, worker
from tests.helpers import needs_dataset, real_dataset

from ghostaudit.attribution import AMBIGUOUS, CONTENT, HIGH, MEDIUM, NONE, STRONG, AccountAttributor
from ghostaudit.dataset import AuditLogEntry, Identity
from ghostaudit.identity import IdentityGraph
from ghostaudit.linker import build_link_index
import hashlib


def ident(uid, email):
    return Identity(uid, uid.title(), email, "+1 " + uid, hashlib.sha256(email.encode()).hexdigest(), "2026-01-01T00:00:00Z")


GRAPH = IdentityGraph([ident("user_1", "u1@x.com"), ident("user_2", "u2@x.com"), ident("user_3", "u3@x.com")], [])
REQ, REP, AUD = "2026-08-20T00:00:00Z", "2026-08-22T12:00:00Z", "2026-09-15T12:00:00Z"


def mk_case(cid, email, status="COMPLETE", reported=REP):
    return case(cid, email, REQ, reported, AUD, status)


def acct(aid, rid, minutes_before, result="SUCCESS", reported=REP):
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(reported, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - timedelta(minutes=minutes_before)
    return worker(aid, rid, t.strftime("%Y-%m-%dT%H:%M:%SZ"), result)


def attributor(activities, owned=(), **kw):
    """owned: (account_resource, 'user_N') content links."""
    results = [fr("e", [obs("e", rid, {"customer_reference": u}) for rid, u in owned])] if owned else []
    links = build_link_index(GRAPH, results, manifest(e="2026-09-01T00:00:00Z"), [])
    return AccountAttributor(activities, links, **kw)


C1, C2, C3 = (mk_case("case_1", "u1@x.com"), "user_1"), (mk_case("case_2", "u2@x.com"), "user_2"), (mk_case("case_3", "u3@x.com"), "user_3")


class Strong(unittest.TestCase):
    def test_content_link_plus_workflow_row_is_high_confidence(self):
        a = attributor([acct("a1", "account_10", 60)], owned=[("account_10", "user_1")])
        r = a.attribute([C1])["case_1"]
        self.assertEqual((r.status, r.confidence, r.account), (CONTENT, HIGH, "account_10"))

    def test_own_content_linked_account_is_not_rejected_by_the_result_key(self):
        # a PARTIALLY_COMPLETE workflow whose account delete succeeded: the identifier already says whose account it is
        c = (mk_case("case_1", "u1@x.com", status="PARTIALLY_COMPLETE"), "user_1")
        r = attributor([acct("a1", "account_10", 60, result="SUCCESS")], owned=[("account_10", "user_1")]).attribute([c])["case_1"]
        self.assertEqual((r.status, r.account, r.rejected), (CONTENT, "account_10", ()))

    def test_the_result_key_still_filters_unowned_candidates(self):
        c = (mk_case("case_1", "u1@x.com", status="PARTIALLY_COMPLETE"), "user_1")
        r = attributor([acct("a1", "account_10", 60, result="SUCCESS")]).attribute([c])["case_1"]
        self.assertEqual((r.status, r.account), (NONE, None))
        self.assertEqual(r.rejected, (("account_10", "result SUCCESS incompatible with reported_status PARTIALLY_COMPLETE"),))

    def test_unique_unowned_candidate_is_strong_medium(self):
        r = attributor([acct("a1", "account_10", 60)]).attribute([C1])["case_1"]
        self.assertEqual((r.status, r.confidence, r.account), (STRONG, MEDIUM, "account_10"))
        self.assertIn("unique", r.reasons[0])

    def test_two_cases_two_accounts_resolved_by_elimination_when_one_is_content_linked(self):
        acts = [acct("a1", "account_10", 60), acct("a2", "account_11", 50)]
        res = attributor(acts, owned=[("account_10", "user_1")]).attribute([C1, C2])
        self.assertEqual((res["case_1"].status, res["case_1"].account), (CONTENT, "account_10"))
        self.assertEqual((res["case_2"].status, res["case_2"].account), (STRONG, "account_11"))

    def test_chain_of_forced_moves_resolves_every_case_exactly(self):
        # windows are -90..+15 min around reported_at; events A 10:55, B 11:45, C 12:20 (all on 2026-08-22)
        #   case_1 reported 11:10 -> window 09:40-11:25 -> {A}        forced
        #   case_2 reported 11:55 -> window 10:25-12:10 -> {A, B}     B once A is taken
        #   case_3 reported 12:30 -> window 11:00-12:45 -> {B, C}     C once B is taken
        c1, c2, c3 = ((mk_case("case_1", "u1@x.com", reported="2026-08-22T11:10:00Z"), "user_1"),
                      (mk_case("case_2", "u2@x.com", reported="2026-08-22T11:55:00Z"), "user_2"),
                      (mk_case("case_3", "u3@x.com", reported="2026-08-22T12:30:00Z"), "user_3"))
        acts = [worker("a1", "account_1", "2026-08-22T10:55:00Z"), worker("a2", "account_2", "2026-08-22T11:45:00Z"), worker("a3", "account_3", "2026-08-22T12:20:00Z")]
        res = attributor(acts).attribute([c1, c2, c3])
        self.assertEqual({k: (v.status, v.account) for k, v in res.items()},
                         {"case_1": (STRONG, "account_1"), "case_2": (STRONG, "account_2"), "case_3": (STRONG, "account_3")})
        rng = random.Random(1)
        for _ in range(6):  # same answer whatever the order the cases/events are presented in
            cs, ac = [c1, c2, c3], acts[:]
            rng.shuffle(cs); rng.shuffle(ac)
            again = attributor(ac).attribute(cs)
            self.assertEqual({k: v.account for k, v in again.items()}, {"case_1": "account_1", "case_2": "account_2", "case_3": "account_3"})


class Ambiguity(unittest.TestCase):
    def test_symmetric_pair_stays_ambiguous_for_both_and_lists_the_same_candidates(self):
        acts = [acct("a1", "account_10", 60), acct("a2", "account_11", 50)]
        res = attributor(acts).attribute([C1, C2])
        for cid in ("case_1", "case_2"):
            self.assertEqual((res[cid].status, res[cid].account), (AMBIGUOUS, None))
            self.assertEqual({c.resource_id for c in res[cid].candidates}, {"account_10", "account_11"})
            self.assertIn("plausible temporal match is not an attribution", res[cid].reasons[0])

    def test_only_option_shared_by_two_cases_is_a_conflict_nobody_gets_it(self):
        res = attributor([acct("a1", "account_10", 60)]).attribute([C1, C2])
        for cid in ("case_1", "case_2"):
            self.assertEqual((res[cid].status, res[cid].account), (AMBIGUOUS, None))
            self.assertIn("conflict", res[cid].reasons[0])

    def test_no_candidate_is_none_not_a_guess(self):
        r = attributor([]).attribute([C1])["case_1"]
        self.assertEqual((r.status, r.account, r.candidates), (NONE, None, ()))

    def test_several_own_content_accounts_in_the_window_are_ambiguous(self):
        acts = [acct("a1", "account_10", 60), acct("a2", "account_11", 50)]
        r = attributor(acts, owned=[("account_10", "user_1"), ("account_11", "user_1")]).attribute([C1])["case_1"]
        self.assertEqual((r.status, r.account), (AMBIGUOUS, None))


class HardFilters(unittest.TestCase):
    def test_result_must_match_reported_status(self):
        c = (mk_case("case_1", "u1@x.com", "NOT_STARTED"), "user_1")
        r = attributor([acct("a1", "account_10", 60, "SUCCESS")]).attribute([c])["case_1"]
        self.assertEqual(r.status, NONE)
        self.assertEqual(r.rejected, (("account_10", "result SUCCESS incompatible with reported_status NOT_STARTED"),))
        ok = attributor([acct("a1", "account_10", 60, "FAILED")]).attribute([c])["case_1"]
        self.assertEqual((ok.status, ok.account), (STRONG, "account_10"))

    def test_result_filter_can_be_switched_off_for_ablation(self):
        c = (mk_case("case_1", "u1@x.com", "NOT_STARTED"), "user_1")
        r = attributor([acct("a1", "account_10", 60, "SUCCESS")], require_result_match=False).attribute([c])["case_1"]
        self.assertEqual(r.status, STRONG)

    def test_account_owned_by_another_user_is_never_a_candidate(self):
        r = attributor([acct("a1", "account_10", 60)], owned=[("account_10", "user_2")]).attribute([C1])["case_1"]
        self.assertEqual((r.status, r.rejected), (NONE, (("account_10", "owned by another user"),)))

    def test_outside_the_window_is_not_a_candidate(self):
        for minutes in (-30, 120):  # 30 min after reported_at (> +15) and 120 min before (< -90)
            r = attributor([acct("a1", "account_10", minutes)]).attribute([C1])["case_1"]
            self.assertEqual(r.status, NONE, minutes)

    def test_an_event_before_the_request_was_opened_is_not_a_candidate_even_inside_the_window(self):
        late_request = (case("case_1", "u1@x.com", "2026-08-22T11:50:00Z", REP, AUD, "COMPLETE"), "user_1")
        before = worker("a1", "account_10", "2026-08-22T11:00:00Z")  # 60 min before reported_at: in the window, but before requested_at
        self.assertEqual(attributor([before]).attribute([late_request])["case_1"].status, NONE)
        after = worker("a2", "account_11", "2026-08-22T11:55:00Z")
        self.assertEqual(attributor([after]).attribute([late_request])["case_1"].status, STRONG)

    def test_only_deletion_worker_account_deletes_count(self):
        sync = worker("a1", "account_10", "2026-08-22T11:00:00Z", op="SYNC_RECORD")
        not_account = worker("a2", "search_5", "2026-08-22T11:00:00Z")
        self.assertEqual(attributor([sync, not_account]).attribute([C1])["case_1"].status, NONE)

    def test_hide_content_for_makes_a_content_account_a_free_candidate(self):
        a = attributor([acct("a1", "account_10", 60)], owned=[("account_10", "user_1")])
        self.assertEqual(a.attribute([C1])["case_1"].status, CONTENT)
        hidden = a.attribute([C1], hide_content_for=["case_1"])["case_1"]
        self.assertEqual((hidden.status, hidden.account), (STRONG, "account_10"))

    def test_candidate_carries_audit_trail(self):
        r = attributor([acct("a1", "account_10", 60)]).attribute([C1])["case_1"]
        c = r.candidates[0]
        self.assertEqual((c.resource_id, c.activity_id, c.result, c.offset_min), ("account_10", "a1", "SUCCESS", -60.0))


class Determinism(unittest.TestCase):
    def test_order_of_cases_and_activities_does_not_change_the_outcome(self):
        acts = [acct("a1", "account_10", 60), acct("a2", "account_11", 50), acct("a3", "account_12", 40, reported="2026-08-23T12:00:00Z")]
        c3 = (mk_case("case_3", "u3@x.com", reported="2026-08-23T12:00:00Z"), "user_3")
        base = attributor(acts, owned=[("account_10", "user_1")]).attribute([C1, C2, c3])
        rng = random.Random(5)
        for _ in range(8):
            a2, cs = acts[:], [C1, C2, c3]
            rng.shuffle(a2); rng.shuffle(cs)
            self.assertEqual(attributor(a2, owned=[("account_10", "user_1")]).attribute(cs), base)


@needs_dataset
class RealDataSafety(unittest.TestCase):
    def test_no_confidently_wrong_assignment_on_samples_and_hide_and_recover(self):
        import sys, json
        from ghostaudit.reconstruction import CaseReconstructor

        cr = CaseReconstructor(real_dataset())
        non = {f"case_{i}" for i in range(533, 543)}
        cases = [q for q in cr.ds.requests.rows + cr.ds.sample_requests.rows if q.case_id not in non]
        pairs = [(q, cr.user_of(q)) for q in cases]
        base = cr.attributor.attribute(pairs)
        gold = {r["case_id"]: next(s["resource_id"] for s in json.loads(r["steps_json"]) if s["resource_id"].startswith("account_")) for r in cr.ds.sample_output}
        wrong = [(c, r.account, gold[c]) for c, r in base.items() if c in gold and r.account and r.account != gold[c]]
        self.assertEqual(wrong, [])
        self.assertEqual(sum(1 for c, r in base.items() if c in gold and r.account == gold[c]), 27)  # measured; not a requirement. S3k; 25 under S2 (case_18, case_32 ambiguous, truth among the candidates)
        truth = {c: r.account for c, r in base.items() if r.status == CONTENT and c not in gold}
        ids = sorted(truth, key=lambda c: int(c.split("_")[1]))
        hidden = ids[::2]
        res = cr.attributor.attribute(pairs, hide_content_for=hidden)
        self.assertEqual([c for c in hidden if res[c].account and res[c].account != truth[c]], [])
        self.assertGreater(sum(1 for c in hidden if res[c].account == truth[c]), len(hidden) * 0.6)

    def test_symmetric_sample_pair_28_29_is_reported_ambiguous(self):
        from ghostaudit.reconstruction import CaseReconstructor

        cr = CaseReconstructor(real_dataset())
        non = {f"case_{i}" for i in range(533, 543)}
        pairs = [(q, cr.user_of(q)) for q in cr.ds.requests.rows + cr.ds.sample_requests.rows if q.case_id not in non]
        base = cr.attributor.attribute(pairs)
        for cid in ("case_28", "case_29"):
            self.assertEqual((base[cid].status, base[cid].account), (AMBIGUOUS, None))
            self.assertEqual({c.resource_id for c in base[cid].candidates}, {"account_5482", "account_7012"})


class RetriedDeleteRows(unittest.TestCase):
    """An account can have several deletion-worker rows (a retry). All are kept; for a case, the last row inside its window speaks for
    the account, and the order of system_activity.csv never changes the result."""

    def both_orders(self, rows, cases, **kw):
        a = attributor(rows, **kw).attribute(cases)
        b = attributor(rows[::-1], **kw).attribute(cases)
        self.assertEqual({k: (v.status, v.account, v.candidates, v.rejected) for k, v in a.items()},
                         {k: (v.status, v.account, v.candidates, v.rejected) for k, v in b.items()})
        return a

    def test_a_later_retry_outside_the_window_does_not_hide_the_attempt_inside_it(self):
        c = (mk_case("case_1", "u1@x.com", status="NOT_STARTED"), "user_1")
        rows = [acct("a1", "account_10", 60, "FAILED"), worker("a2", "account_10", "2026-08-25T09:00:00Z", "SUCCESS")]
        r = self.both_orders(rows, [c])["case_1"]
        self.assertEqual((r.status, r.account, r.candidates[0].activity_id), (STRONG, "account_10", "a1"))

    def test_with_two_attempts_in_the_window_the_last_one_counts(self):
        rows = [acct("a1", "account_10", 60, "FAILED"), acct("a2", "account_10", 10, "SUCCESS")]
        done = self.both_orders(rows, [(mk_case("case_1", "u1@x.com", status="COMPLETE"), "user_1")])["case_1"]
        self.assertEqual((done.status, done.account, done.candidates[0].activity_id), (STRONG, "account_10", "a2"))
        not_started = self.both_orders(rows, [(mk_case("case_1", "u1@x.com", status="NOT_STARTED"), "user_1")])["case_1"]
        self.assertEqual((not_started.status, not_started.rejected), (NONE, (("account_10", "result SUCCESS incompatible with reported_status NOT_STARTED"),)))

    def test_an_own_account_with_a_retry_is_content_and_names_the_last_row(self):
        rows = [acct("a1", "account_10", 60, "FAILED"), acct("a2", "account_10", 10, "SUCCESS")]
        r = self.both_orders(rows, [C1], owned=[("account_10", "user_1")])["case_1"]
        self.assertEqual((r.status, r.account), (CONTENT, "account_10"))
        self.assertIn("a2", r.reasons[0])

    def test_shuffled_activity_never_changes_any_attribution(self):
        rows = [acct("a1", "account_10", 60, "FAILED"), acct("a2", "account_10", 10, "SUCCESS"), acct("a3", "account_11", 50, "SUCCESS"),
                acct("a4", "account_12", 40, "FAILED"), worker("a5", "account_11", "2026-08-25T09:00:00Z", "FAILED")]
        cases = [C1, C2, (mk_case("case_3", "u3@x.com", status="NOT_STARTED"), "user_3")]
        want = {k: (v.status, v.account, v.candidates) for k, v in attributor(rows).attribute(cases).items()}
        rng = random.Random(7)
        for _ in range(8):
            shuffled = rows[:]
            rng.shuffle(shuffled)
            self.assertEqual({k: (v.status, v.account, v.candidates) for k, v in attributor(shuffled).attribute(cases).items()}, want)


if __name__ == "__main__":
    unittest.main()
