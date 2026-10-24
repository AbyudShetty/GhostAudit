import random
import unittest

from tests.helpers import needs_dataset, real_dataset

from ghostaudit.dataset import AuditLogEntry, Identity
from ghostaudit.identity import IdentityGraph, build_identity_graph, normalise
import hashlib


def h(email):
    return hashlib.sha256(email.encode()).hexdigest()


def ident(uid, name, email, phone, updated="2026-05-01T00:00:00Z"):
    return Identity(uid, name, email, phone, h(email), updated)


def change(aid, uid, t, field, old, new):
    return AuditLogEntry(aid, uid, t, field, old, new)


# user_1 changed e-mail twice (a -> b -> c), phone once; user_2 never changed; user_3 shares user_1's *name*.
IDENTITIES = [
    ident("user_1", "Ann Lee", "ann.c@x.com", "+1 (202) 555-0100"),
    ident("user_2", "Bob Roy", "bob@y.org", "+44 20 7946 0958"),
    ident("user_3", "Ann Lee", "ann.other@z.net", "+49 30 1234567"),
]
LOG = [
    change("audit_1", "user_1", "2025-01-10T10:00:00Z", "email", "ann.a@x.com", "ann.b@x.com"),
    change("audit_2", "user_1", "2025-01-10T10:00:00Z", "email_hash", h("ann.a@x.com"), h("ann.b@x.com")),
    change("audit_3", "user_1", "2026-03-01T08:00:00Z", "email", "ann.b@x.com", "ann.c@x.com"),
    change("audit_4", "user_1", "2026-03-01T08:00:00Z", "email_hash", h("ann.b@x.com"), h("ann.c@x.com")),
    change("audit_5", "user_1", "2025-06-01T00:00:00Z", "phone", "+1 202 555 0000", "+1 (202) 555-0100"),
]


def graph(identities=IDENTITIES, log=LOG):
    return IdentityGraph(identities, log)


class EmailResolution(unittest.TestCase):
    def setUp(self):
        self.g = graph()

    def test_exact_current(self):
        r = self.g.resolve_request_email("ann.c@x.com")
        self.assertEqual((r.status, r.user_ids, r.match, r.relation), ("RESOLVED", ("user_1",), "exact", "current"))
        self.assertEqual(r.claims[0].source, "user_audit_log.csv:audit_3")

    def test_case_insensitive_current(self):
        r = self.g.resolve_request_email("ANN.C@X.COM")
        self.assertEqual((r.status, r.user_id, r.match, r.relation), ("RESOLVED", "user_1", "case_insensitive", "current"))

    def test_historical_exact_and_case_insensitive(self):
        for q, match in (("ann.a@x.com", "exact"), ("Ann.B@X.com", "case_insensitive")):
            with self.subTest(q=q):
                r = self.g.resolve_request_email(q)
                self.assertEqual((r.status, r.user_id, r.relation, r.match), ("RESOLVED", "user_1", "historical", match))

    def test_unchanged_user_resolves_from_snapshot(self):
        r = self.g.resolve_request_email("bob@y.org")
        self.assertEqual((r.user_id, r.claims[0].source), ("user_2", "user_identities.csv:user_2"))

    def test_near_miss_decoys_never_resolve(self):
        for q in ("ann.c.jr@x.com", "ann.c@x.co", "ann.cc@x.com", "ann.c@x.com.evil.io", " ", "bob"):
            with self.subTest(q=q):
                self.assertEqual(self.g.resolve_request_email(q).status, "UNRESOLVED")

    def test_surrounding_whitespace_is_normalised_not_fuzzy(self):
        r = self.g.resolve_request_email("  ann.c@x.com ")
        self.assertEqual((r.status, r.user_id), ("RESOLVED", "user_1"))
        self.assertNotEqual(r.match, "exact")

    def test_names_are_not_identifiers(self):
        self.assertEqual(self.g.resolve("name", "Ann Lee").status, "UNRESOLVED")
        self.assertEqual(self.g.name_collisions(), {"Ann Lee": ["user_1", "user_3"]})
        self.assertEqual(self.g.resolve_request_email("ann.other@z.net").user_id, "user_3")


class OtherIdentifiers(unittest.TestCase):
    def setUp(self):
        self.g = graph()

    def test_historical_and_current_hash(self):
        self.assertEqual(self.g.resolve("email_hash", h("ann.a@x.com")).relation, "historical")
        r = self.g.resolve("email_hash", h("ann.c@x.com").upper())
        self.assertEqual((r.user_id, r.relation, r.match), ("user_1", "current", "case_insensitive"))
        self.assertEqual(self.g.resolve("email_hash", h("unrelated@q.com")).status, "UNRESOLVED")

    def test_phone_formatting_is_normalised_historical_included(self):
        r = self.g.resolve("phone", "+1 202-555-0100")
        self.assertEqual((r.user_id, r.relation, r.match), ("user_1", "current", "normalized"))
        self.assertEqual(self.g.resolve("phone", "+12025550000").relation, "historical")
        self.assertEqual(self.g.resolve("phone", "+1 202 555 9999").status, "UNRESOLVED")

    def test_user_reference(self):
        self.assertEqual(self.g.resolve("user_ref", "USER_2").user_id, "user_2")
        self.assertEqual(self.g.resolve("user_ref", "user_22").status, "UNRESOLVED")

    def test_multiple_identifiers_of_one_user_all_resolve_to_it(self):
        users = {self.g.resolve(k, v).user_id for k, v in (
            ("email", "ann.a@x.com"), ("email", "ann.c@x.com"), ("email_hash", h("ann.b@x.com")), ("phone", "+12025550100"), ("user_ref", "user_1"))}
        self.assertEqual(users, {"user_1"})

    def test_unsupported_kind(self):
        self.assertEqual(self.g.resolve("passport", "x").status, "UNRESOLVED")

    def test_find_users_by_any_groups_and_flags_ambiguity(self):
        out = self.g.find_users_by_any([("email", "ann.a@x.com"), ("phone", "+49 30 1234567"), ("email", "nobody@q.com")])
        self.assertEqual(sorted(out), ["user_1", "user_3"])


class TimeGate(unittest.TestCase):
    def setUp(self):
        self.g = graph()

    def test_value_that_did_not_exist_yet_is_unresolved_with_reason(self):
        r = self.g.resolve("email", "ann.c@x.com", known_by="2026-02-01T00:00:00Z")
        self.assertEqual(r.status, "UNRESOLVED")
        self.assertIn("after", r.reason)

    def test_replaced_historical_value_still_identifies_the_user(self):
        # ann.a was replaced in Jan 2025 but is still how old systems know her in Sep 2026
        r = self.g.resolve("email", "ann.a@x.com", known_by="2026-09-15T00:00:00Z")
        self.assertEqual((r.status, r.user_id, r.relation), ("RESOLVED", "user_1", "historical"))

    def test_boundary_inclusive(self):
        self.assertEqual(self.g.resolve("email", "ann.c@x.com", known_by="2026-03-01T08:00:00Z").status, "RESOLVED")
        self.assertEqual(self.g.resolve("email", "ann.c@x.com", known_by="2026-03-01T07:59:59Z").status, "UNRESOLVED")

    def test_validity_windows_and_held_at(self):
        a = self.g.claims_for("email", "ann.b@x.com")[0]
        self.assertEqual((a.valid_from, a.valid_until, a.relation), ("2025-01-10T10:00:00Z", "2026-03-01T08:00:00Z", "historical"))
        self.assertTrue(a.active_at("2025-12-01T00:00:00Z"))
        self.assertFalse(a.active_at("2026-03-01T08:00:00Z"))  # replaced exactly then
        first = self.g.claims_for("email", "ann.a@x.com")[0]
        self.assertIsNone(first.valid_from)  # unknown start is never invented
        self.assertEqual(first.valid_until, "2025-01-10T10:00:00Z")

    def test_claims_of_known_by(self):
        emails = [c.raw for c in self.g.claims_of("user_1", "2025-02-01T00:00:00Z") if c.kind == "email"]
        self.assertEqual(emails, ["ann.a@x.com", "ann.b@x.com"])


class ConflictsAndConsistency(unittest.TestCase):
    def codes(self, g):
        return sorted(i.code for i in g.issues)

    def test_clean_graph_has_no_issues(self):
        self.assertEqual(graph().issues, [])

    def test_duplicate_identifier_across_users_is_ambiguous_not_silently_resolved(self):
        dup = IDENTITIES + [ident("user_4", "Zed", "ann.c@x.com", "+1 000")]
        g = graph(dup, LOG)
        r = g.resolve_request_email("ann.c@x.com")
        self.assertEqual((r.status, r.user_ids), ("AMBIGUOUS", ("user_1", "user_4")))
        self.assertIsNone(r.user_id)
        self.assertIn("cross_user_claim", self.codes(g))

    def test_historical_value_reused_as_someone_elses_current_is_flagged(self):
        g = graph(IDENTITIES + [ident("user_4", "Zed", "ann.a@x.com", "+1 000")], LOG)
        self.assertEqual(g.resolve_request_email("ann.a@x.com").status, "AMBIGUOUS")

    def test_chain_break_and_snapshot_mismatch(self):
        bad = [change("a1", "user_1", "2025-01-01T00:00:00Z", "email", "x@x.com", "y@x.com"),
               change("a2", "user_1", "2025-02-01T00:00:00Z", "email", "NOT-y@x.com", "z@x.com")]
        codes = self.codes(graph(IDENTITIES, bad))
        self.assertIn("chain_break", codes)
        self.assertIn("snapshot_mismatch", codes)

    def test_hash_mismatch_unknown_user_unknown_field(self):
        broken = [Identity("user_1", "Ann", "ann.c@x.com", "+1", "00" * 32, "2026-01-01T00:00:00Z")]
        log = [change("a1", "user_9", "2025-01-01T00:00:00Z", "email", "a@a.com", "b@a.com"),
               change("a2", "user_1", "2025-01-01T00:00:00Z", "nickname", "a", "b")]
        codes = self.codes(graph(broken, log))
        self.assertEqual(codes, ["hash_mismatch", "unknown_field", "unknown_user"])


class Determinism(unittest.TestCase):
    def test_input_order_does_not_change_claims_or_resolutions(self):
        base = graph()
        rng = random.Random(7)
        for _ in range(5):
            ids, log = IDENTITIES[:], LOG[:]
            rng.shuffle(ids); rng.shuffle(log)
            g = graph(ids, log)
            for kind, v in (("email", "ann.a@x.com"), ("email", "ANN.C@X.COM"), ("phone", "+12025550000"), ("email_hash", h("ann.b@x.com"))):
                self.assertEqual(g.resolve(kind, v), base.resolve(kind, v))
            self.assertEqual(g.claims_of("user_1"), base.claims_of("user_1"))
            self.assertEqual(g.users(), base.users())
            self.assertEqual([str(i) for i in g.issues], [str(i) for i in base.issues])

    def test_normalise(self):
        self.assertEqual(normalise("phone", "+1 (202) 555-0100"), "+12025550100")
        self.assertEqual(normalise("email", " A@B.Co "), "a@b.co")


@needs_dataset
class RealIdentityData(unittest.TestCase):
    def test_graph_is_consistent(self):
        g = build_identity_graph(real_dataset())
        self.assertEqual([str(i) for i in g.issues], [])
        self.assertEqual(len(g.users()), 850)
        self.assertEqual(set(g.name_collisions()), {"Gabriel Costa", "Mateo Ríos", "Thandiwe Ndlovu"})

    def test_every_request_email_resolves_uniquely_with_expected_forms(self):
        from collections import Counter

        ds = real_dataset(); g = build_identity_graph(ds)
        forms = Counter()
        for q in ds.requests.rows + ds.sample_requests.rows:
            r = g.resolve_request_email(q.email, known_by=q.audit_at)
            self.assertEqual(r.status, "RESOLVED", q.case_id)
            forms[(r.match, r.relation)] += 1
        self.assertEqual(dict(forms), {("exact", "current"): 432, ("exact", "historical"): 98,
                                       ("case_insensitive", "current"): 15, ("case_insensitive", "historical"): 5})

    def test_many_eval_requests_use_a_historical_email(self):
        ds = real_dataset(); g = build_identity_graph(ds)
        hist = [q.case_id for q in ds.requests.rows if g.resolve_request_email(q.email).relation == "historical"]
        self.assertGreater(len(hist), 90)  # historical resolution is not an edge case: ~1 in 5 eval requests

    def test_real_hash_chain_resolves_to_same_user_as_email(self):
        ds = real_dataset(); g = build_identity_graph(ds)
        n = 0
        for a in ds.audit_log.rows:
            if a.field_name == "email":
                u = g.resolve("email", a.old_value).user_id
                self.assertEqual(g.resolve("email_hash", h(a.old_value)).user_id, u)
                n += 1
        self.assertEqual(n, 107)


if __name__ == "__main__":
    unittest.main()
