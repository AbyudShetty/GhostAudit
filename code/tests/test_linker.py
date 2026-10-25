import random
import unittest

from tests.helpers import needs_dataset, real_dataset, real_results

from ghostaudit.dataset import Activity, AuditLogEntry, EvidenceFile, Identity
from ghostaudit.evidence import FileResult, Observation
from ghostaudit.evidence_index import EvidenceIndex
from ghostaudit.identity import IdentityGraph
from ghostaudit.linker import (CATALOG, DEFAULT_POLICY, DIRECT, DOCUMENT, POLICIES, REFERENCE, TRACE, LinkPolicy, build_link_index)
from ghostaudit.schema import system_for_resource
import hashlib

AUDIT = "2026-09-15T12:00:00Z"
BEFORE, AFTER = "2026-09-10T00:00:00Z", "2026-09-17T00:00:00Z"


def h(e):
    return hashlib.sha256(e.encode()).hexdigest()


def ident(uid, name, email, phone):
    return Identity(uid, name, email, phone, h(email), "2026-05-01T00:00:00Z")


# user_1 changed e-mail in March 2026; user_2 changes phone on 2026-10-01 (after AUDIT) so the snapshot already holds the new value.
GRAPH = IdentityGraph(
    [ident("user_1", "Ann Lee", "ann.new@x.com", "+1 202 555 0103"), ident("user_2", "Bob Roy", "bob@y.org", "+44 20 0000 0000"),
     ident("user_3", "Ann Lee", "ann.other@z.net", "+49 30 1234567")],
    [AuditLogEntry("audit_1", "user_1", "2026-03-01T00:00:00Z", "email", "ann.old@x.com", "ann.new@x.com"),
     AuditLogEntry("audit_2", "user_1", "2026-03-01T00:00:00Z", "email_hash", h("ann.old@x.com"), h("ann.new@x.com")),
     AuditLogEntry("audit_3", "user_2", "2026-10-01T00:00:00Z", "phone", "+44 20 7946 0958", "+44 20 0000 0000")],
)


def obs(file_id, rid, fields=None, trace=None, fmt="json", observed_at=None, loc=None):
    return Observation(file_id, fmt, loc or f"$.{rid}", rid, fields=dict(fields or {}), trace_events=list(trace or []), observed_at=observed_at, raw="")


def fr(file_id, observations, fmt="json"):
    r = FileResult(file_id, f"{file_id}.x", fmt, None)
    r.observations = list(observations)
    return r


def manifest(**captured):
    return {fid: EvidenceFile(fid, f"{fid}.x", "t", ts) for fid, ts in captured.items()}


def catalog_row(aid, rid, details, t=BEFORE):
    return Activity(aid, t, "LINK_RESOURCE_IDENTITY", "data_catalog", "x", rid, "SUCCESS", details)


def index(results, man, acts=(), graph=GRAPH):
    return build_link_index(graph, results, man, acts)


def assoc(idx, user, policy="S2", audit=AUDIT):
    return {a.resource_id: a for a in idx.associations(user, audit, POLICIES[policy])}


class DirectLinks(unittest.TestCase):
    def test_valid_email_in_record_links(self):
        i = index([fr("e1", [obs("e1", "search_1", {"email": "ANN.NEW@x.com", "display_name": "Ann"})])], manifest(e1=BEFORE))
        a = assoc(i, "user_1")["search_1"]
        self.assertEqual((a.status, a.link_types, a.system_id), ("ASSOCIATED", (DIRECT,), "search_service"))
        l = a.links[0]
        self.assertEqual((l.identifier_kind, l.match, l.identifier_relation, l.source_id), ("email", "case_insensitive", "current", "e1"))

    def test_historical_email_and_hash_link_with_provenance(self):
        i = index([fr("e1", [obs("e1", "search_2", {"email": "ann.old@x.com"}), obs("e1", "analytics_3", {"email_hash": h("ann.old@x.com")})])], manifest(e1=BEFORE))
        a = assoc(i, "user_1")
        self.assertEqual({r: x.links[0].identifier_relation for r, x in a.items()}, {"search_2": "historical", "analytics_3": "historical"})

    def test_misleading_near_miss_email_does_not_link_and_is_recorded(self):
        i = index([fr("e1", [obs("e1", "search_9", {"email": "ann.new.jr@x.com"})])], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1"), {})
        self.assertEqual(dict(i.issues.unresolved_emails), {"ann.new.jr@x.com": 1})

    def test_misleading_name_only_never_links(self):
        i = index([fr("e1", [obs("e1", "search_9", {"display_name": "Ann Lee"}), obs("e1", "support_9", {"requester_name": "Ann Lee", "message_excerpt": "x"})])], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1"), {})
        self.assertEqual(assoc(i, "user_3"), {})

    def test_phone_and_user_ref_link(self):
        i = index([fr("e1", [obs("e1", "support_1", {"requester_phone": "+1-202-555-0103"}), obs("e1", "backup_1", {"customer_reference": "user_1"})])], manifest(e1=BEFORE))
        self.assertEqual(sorted(assoc(i, "user_1")), ["backup_1", "support_1"])

    def test_one_record_one_link_per_user_even_with_several_identifiers(self):
        i = index([fr("e1", [obs("e1", "account_1", {"email": "ann.new@x.com", "customer_reference": "user_1", "phone": "+1 202 555 0103"})])], manifest(e1=BEFORE))
        self.assertEqual(len(assoc(i, "user_1")["account_1"].links), 1)


class TraceLinks(unittest.TestCase):
    def test_valid_subject_key_on_sibling_line_links_as_trace(self):
        i = index([fr("e1", [obs("e1", "account_5", {"event": "profile_lookup_completed"}, trace=[{"line": 1, "lookup_key": "ann.new@x.com"}], fmt="log")])], manifest(e1=BEFORE))
        a = assoc(i, "user_1")["account_5"]
        self.assertEqual(a.link_types, (TRACE,))
        self.assertTrue(a.links[0].identifier_field.startswith("trace["))

    def test_misleading_unrelated_trace_has_no_events_and_no_link(self):
        i = index([fr("e1", [obs("e1", "account_6", {"event": "profile_lookup_completed"}, trace=[])], fmt="log")], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1"), {})

    def test_direct_supersedes_redundant_trace_link(self):
        i = index([fr("e1", [obs("e1", "account_7", {"lookup_key": "ann.new@x.com"}, trace=[{"line": 1, "lookup_key": "ann.new@x.com"}], fmt="log")])], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1")["account_7"].link_types, (DIRECT,))


class CatalogAndDocumentLinks(unittest.TestCase):
    def test_valid_catalog_row_links_and_is_citable_as_activity_id(self):
        i = index([], {}, [catalog_row("activity_9", "marketing_1", "Data catalog linked marketing_1 (marketing_1) to ann.old@x.com and user_1.")])
        a = assoc(i, "user_1")["marketing_1"]
        self.assertEqual((a.link_types, a.links[0].source_id, a.links[0].source_kind), ((CATALOG,), "activity_9", "activity"))

    def test_misleading_catalog_decoy_email_and_other_event_types_ignored(self):
        rows = [catalog_row("activity_1", "marketing_2", "Data catalog linked marketing_2 to ann.new.jr@x.com."),
                Activity("activity_2", BEFORE, "DELETE_RECORD", "deletion_worker", "account_service", "account_8", "SUCCESS", "Deletion worker submitted primary profile account_8; ann.new@x.com")]
        i = index([], {}, rows)
        self.assertEqual(assoc(i, "user_1"), {})

    def test_catalog_conflict_two_users_is_flagged_and_ambiguous(self):
        i = index([], {}, [catalog_row("activity_1", "export_5", "Data catalog linked export_5 to ann.new@x.com and user_2.")])
        self.assertEqual(i.issues.shared_records, [("activity_1", "activity_1", ("user_1", "user_2"))])
        self.assertEqual({a.status for a in i.associations("user_1", AUDIT, POLICIES["S2"])}, {"AMBIGUOUS"})

    def doc(self, text_fields, rids):
        return Observation("e1", "pdf", "$document", None, fields={"resource_ids": rids, **text_fields}, raw="")

    def test_valid_document_with_one_user_and_a_resource_id_links(self):
        i = index([fr("e1", [self.doc({"lines": ["Detail ann.old@x.com", "Detail +1 202 555 0103"]}, ["support_77"])], "pdf")], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1")["support_77"].link_types, (DOCUMENT,))

    def test_misleading_register_document_with_many_users_and_no_resource_ids_links_nothing(self):
        i = index([fr("e1", [self.doc({"lines": ["user_1 | user_2 | user_3"]}, [])], "pdf")], manifest(e1=BEFORE))
        self.assertEqual(i.links, [])
        self.assertEqual(i.issues.shared_records[0][2], ("user_1", "user_2", "user_3"))

    def test_misleading_document_naming_two_users_makes_the_resource_ambiguous(self):
        i = index([fr("e1", [self.doc({"lines": ["ann.new@x.com", "bob@y.org"]}, ["support_78"])], "pdf")], manifest(e1=BEFORE))
        a = assoc(i, "user_1")["support_78"]
        self.assertEqual((a.status, a.competing_users), ("AMBIGUOUS", ("user_2",)))
        self.assertEqual(a.links[0].record_users, 2)


class ReferenceLinks(unittest.TestCase):
    def test_valid_reference_from_a_directly_linked_record(self):
        i = index([fr("e1", [obs("e1", "search_1", {"email": "ann.new@x.com", "source_resource_id": "account_6"})])], manifest(e1=BEFORE))
        a = assoc(i, "user_1")
        self.assertEqual((a["account_6"].link_types, a["account_6"].links[0].via_resource), ((REFERENCE,), "search_1"))

    def test_misleading_reference_from_an_unlinked_record_creates_nothing(self):
        i = index([fr("e1", [obs("e1", "search_1", {"email": "stranger@q.com", "source_resource_id": "account_6"})])], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1"), {})

    def test_misleading_reference_from_a_trace_only_record_is_dropped(self):
        i = index([fr("e1", [obs("e1", "search_1", {"source_resource_id": "account_6"}, trace=[{"line": 1, "lookup_key": "ann.new@x.com"}], fmt="log")])], manifest(e1=BEFORE))
        self.assertNotIn("account_6", assoc(i, "user_1"))


class Ambiguity(unittest.TestCase):
    def test_resource_linked_to_two_users_is_ambiguous_for_both_never_picked(self):
        i = index([fr("e1", [obs("e1", "backup_4", {"customer_reference": "user_1"})]), fr("e2", [obs("e2", "backup_4", {"customer_email": "bob@y.org"})])],
                  manifest(e1=BEFORE, e2=BEFORE))
        a1, a2 = assoc(i, "user_1")["backup_4"], assoc(i, "user_2")["backup_4"]
        self.assertEqual((a1.status, a1.competing_users, a2.status, a2.competing_users), ("AMBIGUOUS", ("user_2",), "AMBIGUOUS", ("user_1",)))

    def test_shared_support_record_identifying_two_users_is_flagged(self):
        i = index([fr("e1", [obs("e1", "support_2", {"requester_email": "ann.new@x.com", "message": "forward to bob@y.org"})])], manifest(e1=BEFORE))
        self.assertEqual(i.issues.shared_records, [("e1", "$.support_2", ("user_1", "user_2"))])

    def test_misleading_same_user_via_two_identifiers_is_not_ambiguous(self):
        i = index([fr("e1", [obs("e1", "support_3", {"requester_email": "ann.old@x.com", "customer_reference": "user_1"})])], manifest(e1=BEFORE))
        self.assertEqual(assoc(i, "user_1")["support_3"].status, "ASSOCIATED")

    def test_identifier_claimed_by_two_users_is_recorded_not_linked(self):
        g = IdentityGraph([ident("user_1", "A", "dup@x.com", "+1"), ident("user_2", "B", "dup@x.com", "+2")], [])
        i = build_link_index(g, [fr("e1", [obs("e1", "search_1", {"email": "dup@x.com"})])], manifest(e1=BEFORE), [])
        self.assertEqual(i.links, [])
        self.assertEqual(i.issues.ambiguous_identifiers, [("e1", "$.search_1", "email:dup@x.com")])


class PostAuditPolicies(unittest.TestCase):
    """Identity association from a later file vs admissible state evidence and citations."""

    def setUp(self):
        # account_1: identified only by a file captured after the audit; search_1: same; backup_1: identified before the audit
        self.results = [
            fr("e_pre", [obs("e_pre", "backup_1", {"customer_reference": "user_1"}), obs("e_pre", "account_1", {"profile": None})]),
            fr("e_post", [obs("e_post", "account_1", {"customer_reference": "user_1"}), obs("e_post", "search_1", {"email": "ann.new@x.com"}),
                          obs("e_post", "account_2", {"event": "x"}, trace=[{"line": 1, "lookup_key": "ann.old@x.com"}], fmt="log"),
                          obs("e_post", "audit_record_1", {"subject_reference": "user_1"})]),
        ]
        self.man = manifest(e_pre=BEFORE, e_post=AFTER)
        self.idx = index(self.results, self.man)

    def test_s2_uses_only_eligible_sources(self):
        self.assertEqual(sorted(assoc(self.idx, "user_1", "S2")), ["backup_1"])

    def test_s3_adds_everything_identified_later_and_marks_it_post_audit_only(self):
        a = assoc(self.idx, "user_1", "S3")
        self.assertEqual(sorted(a), ["account_1", "account_2", "audit_record_1", "backup_1", "search_1"])
        self.assertFalse(a["backup_1"].post_audit_only)
        self.assertTrue(all(a[r].post_audit_only for r in ("account_1", "account_2", "audit_record_1", "search_1")))

    def test_s3c_rejects_post_audit_trace_links_only(self):
        a = assoc(self.idx, "user_1", "S3c")
        self.assertNotIn("account_2", a)
        self.assertIn("search_1", a)

    def test_s3k_accepts_stable_resources_and_rejects_mutable_ones_identified_only_later(self):
        a = assoc(self.idx, "user_1", "S3k")
        self.assertEqual(sorted(a), ["account_1", "account_2", "audit_record_1", "backup_1"])
        verdicts = {l.resource_id: reason for l, ok, reason in self.idx.explain("user_1", "search_1", AUDIT, POLICIES["S3k"])}
        self.assertEqual(verdicts, {"search_1": "post_audit_system_not_stable"})

    def test_default_policy_is_s3k_later_sources_identify_only_stable_resources(self):
        self.assertEqual(DEFAULT_POLICY, "S3k")
        pol = POLICIES[DEFAULT_POLICY]
        for l in self.idx.links:
            ok = self.idx.admissible(l, AUDIT, pol)[0]
            later_ok = system_for_resource(l.resource_id) in ("account_service", "security_service", "legal_service") and l.record_users == 1
            self.assertEqual(ok, self.idx.eligible(l, AUDIT) or later_ok, l)
        self.assertEqual(sorted(assoc(self.idx, "user_1", DEFAULT_POLICY)), ["account_1", "account_2", "audit_record_1", "backup_1"])
        self.assertEqual(sorted(assoc(self.idx, "user_1", "S2")), ["backup_1"])  # the strict policy stays available

    def test_association_from_later_file_grants_no_citation_or_state_rights(self):
        ev = EvidenceIndex(self.results, self.man)
        self.assertIn("account_1", assoc(self.idx, "user_1", "S3k"))
        self.assertEqual(ev.citable_ids("account_1", AUDIT), ["e_pre"])  # only the pre-audit, identifier-less sighting is citable
        self.assertEqual(ev.citable_ids("audit_record_1", AUDIT), [])  # associated, but nothing citable at audit time
        self.assertEqual(ev.citable_ids("account_1", AFTER), ["e_post", "e_pre"])  # visible once the audit time moves past the capture
        self.assertEqual([o.file_id for o in ev.observations("search_1", AUDIT)], [])

    def test_record_time_after_audit_is_not_usable_even_inside_an_early_file(self):
        res = [fr("e_pre", [obs("e_pre", "search_5", {"email": "ann.new@x.com"}, observed_at=AFTER)])]
        i = index(res, manifest(e_pre=BEFORE))
        self.assertEqual(assoc(i, "user_1", "S2"), {})
        self.assertEqual(EvidenceIndex(res, manifest(e_pre=BEFORE)).citable_ids("search_5", AUDIT), [])

    def test_boundary_capture_exactly_at_audit_is_eligible(self):
        i = index([fr("e1", [obs("e1", "search_5", {"email": "ann.new@x.com"})])], manifest(e1=AUDIT))
        self.assertIn("search_5", assoc(i, "user_1", "S2"))

    def test_s3k_strict_post_audit_requires_a_single_owner_over_all_evidence(self):
        res = [fr("e_pre", [obs("e_pre", "account_9", {"customer_reference": "user_2"})]), fr("e_post", [obs("e_post", "account_9", {"customer_reference": "user_1"})])]
        i = index(res, manifest(e_pre=BEFORE, e_post=AFTER))
        a = assoc(i, "user_1", "S3k")["account_9"]
        self.assertEqual((a.status, a.competing_users), ("AMBIGUOUS", ("user_2",)))

    def test_s3k_strict_rejects_post_audit_shared_records(self):
        res = [fr("e_post", [obs("e_post", "account_9", {"email": "ann.new@x.com", "note": "see bob@y.org"})])]
        i = index(res, manifest(e_post=AFTER))
        self.assertEqual(assoc(i, "user_1", "S3k"), {})
        self.assertEqual(self.idx.explain("user_1", "nothing", AUDIT, POLICIES["S3k"]), [])

    def test_s1_identity_gate_rejects_identifier_that_did_not_exist_yet_but_keeps_replaced_historical_ones(self):
        # user_2's new phone appears at 2026-10-01 (after the audit). A record carrying it is not attributable at audit time under S1.
        res = [fr("e1", [obs("e1", "support_6", {"requester_phone": "+44 20 0000 0000"}), obs("e1", "support_7", {"requester_phone": "+44 20 7946 0958"}),
                         obs("e1", "search_8", {"email": "ann.old@x.com"})])]
        i = index(res, manifest(e1=BEFORE))
        s1, s2 = assoc(i, "user_2", "S1"), assoc(i, "user_2", "S2")
        self.assertEqual((sorted(s1), sorted(s2)), (["support_7"], ["support_6", "support_7"]))
        self.assertIn("search_8", assoc(i, "user_1", "S1"))  # replaced in March 2026, still identifies Ann

    def test_policy_object_validation_and_switching(self):
        with self.assertRaises(ValueError):
            LinkPolicy("bad", False, "sometimes")
        custom = LinkPolicy("custom", False, "any", None, frozenset({"search_service"}), False)
        self.assertEqual(sorted(assoc_with(self.idx, "user_1", custom)), ["backup_1", "search_1"])


def assoc_with(idx, user, policy, audit=AUDIT):
    return {a.resource_id: a for a in idx.associations(user, audit, policy)}


class Determinism(unittest.TestCase):
    def test_file_and_observation_order_do_not_change_links(self):
        results = [fr("e1", [obs("e1", "search_1", {"email": "ann.new@x.com"}), obs("e1", "backup_1", {"customer_reference": "user_1"})]),
                   fr("e2", [obs("e2", "account_1", {"customer_reference": "user_1"}), obs("e2", "support_1", {"requester_email": "bob@y.org"})]),
                   fr("e3", [obs("e3", "search_1", {"email": "ann.old@x.com"})])]
        acts = [catalog_row("activity_1", "marketing_1", "linked to ann.new@x.com"), catalog_row("activity_2", "marketing_2", "linked to bob@y.org")]
        man = manifest(e1=BEFORE, e2=AFTER, e3=BEFORE)
        base = index(results, man, acts)
        rng = random.Random(3)
        for _ in range(6):
            rs = [fr(r.file_id, rng.sample(r.observations, len(r.observations))) for r in results]
            rng.shuffle(rs)
            ac = acts[:]; rng.shuffle(ac)
            other = index(rs, man, ac)
            self.assertEqual(other.links, base.links)
            for p in POLICIES.values():
                self.assertEqual(other.associations("user_1", AUDIT, p), base.associations("user_1", AUDIT, p))


@needs_dataset
class RealDataLinking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ghostaudit.identity import build_identity_graph

        cls.ds = real_dataset()
        cls.graph = build_identity_graph(cls.ds)
        cls.idx = build_link_index(cls.graph, list(real_results().values()), cls.ds.evidence_by_id(), cls.ds.activity.rows)
        cls.q = {q.case_id: q for q in cls.ds.sample_requests.rows + cls.ds.requests.rows}

    def a(self, case, policy="S3k"):
        q = self.q[case]
        return {x.resource_id: x for x in self.idx.associations(self.graph.resolve_request_email(q.email).user_id, q.audit_at, POLICIES[policy])}

    def test_no_resource_has_two_owners_and_no_identifier_is_ambiguous(self):
        by = {}
        for l in self.idx.links:
            by.setdefault(l.resource_id, set()).add(l.user_id)
        self.assertEqual([r for r, u in by.items() if len(u) > 1], [])
        self.assertEqual(self.idx.issues.ambiguous_identifiers, [])
        self.assertEqual([x[:2] for x in self.idx.issues.shared_records], [("evidence_27", "$document")])

    def test_decoy_emails_stay_unresolved(self):
        # 22 distinct e-mails in the evidence belong to nobody: 15 "<known local part>.jr@" variants plus 7 look-alikes of
        # sample-scenario users. Exact matching must leave every one of them unlinked.
        un = self.idx.issues.unresolved_emails
        self.assertEqual(len(un), 22)
        self.assertEqual(sum(1 for e in un if e.split("@")[0].endswith(".jr")), 15)
        for decoy, real in (("g.silva76.jr@gmail.com", "g.silva76@gmail.com"), ("joana.berg@outlook.com", "jonas.berg@outlook.com"),
                            ("jonas.berglund@outlook.com", "jonas.berg@outlook.com"), ("m.rios.gil@outlook.com", "mateo.rios@outlook.com"),
                            ("p.ramanathan@gmail.com", "priya.raman@gmail.com"), ("nadia.popescu@yahoo.com", "nadia.ionescu@yahoo.com")):
            self.assertIn(decoy, un)
            self.assertNotEqual(self.graph.resolve("email", decoy).status, "RESOLVED")
            self.assertEqual(self.graph.resolve("email", real).status, "RESOLVED")
        linked_users = {l.identifier_norm for l in self.idx.links if l.identifier_kind == "email"}
        self.assertFalse(linked_users & set(un))

    def test_decoy_search_document_does_not_leak_into_the_real_users_case(self):
        # evidence_57 holds search docs for jonas.berg (real), jonas.berglund and joana.berg (decoys); only search_001 is case_06's
        res = self.a("case_06", "S2")
        self.assertIn("search_001", res)
        self.assertNotIn("search_8814", res)
        self.assertNotIn("search_4203", res)

    def test_reference_link_case_06(self):
        self.assertEqual(self.a("case_06", "S2")["account_006"].link_types, (REFERENCE,))

    def test_trace_link_case_09_account_is_post_audit_only(self):
        a = self.a("case_09")["account_3960"]
        self.assertEqual((a.link_types, a.post_audit_only, a.sources), ((TRACE,), True, ("evidence_06",)))
        self.assertNotIn("account_3960", self.a("case_09", "S2"))
        self.assertEqual(EvidenceIndex(list(real_results().values()), self.ds.evidence_by_id()).citable_ids("account_3960", self.q["case_09"].audit_at), [])

    def test_case_27_account_is_identified_later_but_cited_from_the_eligible_file(self):
        ev = EvidenceIndex(list(real_results().values()), self.ds.evidence_by_id())
        self.assertEqual(self.a("case_27")["account_3866"].sources, ("evidence_02",))
        self.assertEqual(ev.citable_ids("account_3866", self.q["case_27"].audit_at), ["evidence_92"])

    def test_documented_sample_policy_metrics_regression(self):
        # Snapshot of values measured and reported in code/docs/m2_identity_linking.md; not a requirement.
        import json

        gold = {r["case_id"]: {s["resource_id"] for s in json.loads(r["steps_json"])} for r in self.ds.sample_output}
        for name, expected in {"S2": (77, 3, 34), "S3": (85, 3, 26), "S3c": (83, 3, 28), "S3k": (85, 3, 26)}.items():
            tp = fp = fn = 0
            for q in self.ds.sample_requests.rows:
                A = {r for r, x in self.a(q.case_id, name).items() if x.status == "ASSOCIATED"}
                tp += len(A & gold[q.case_id]); fp += len(A - gold[q.case_id]); fn += len(gold[q.case_id] - A)
            self.assertEqual((tp, fp, fn), expected, name)

    def test_all_eval_cases_resolve_and_each_has_associations(self):
        for q in self.ds.requests.rows:
            self.assertTrue(self.a(q.case_id), q.case_id)


class SharedReference(unittest.TestCase):
    """A record naming another resource in source_resource_id passes it to every subject the record names, never just the first."""

    def build(self, first, second):
        o = obs("e", "export_1", {"source_resource_id": "search_9", "requester_email": first, "cc_email": second})
        return index([fr("e", [o])], manifest(e=BEFORE))

    def test_both_field_orders_give_both_subjects_and_an_ambiguous_resource(self):
        for first, second in (("ann.new@x.com", "bob@y.org"), ("bob@y.org", "ann.new@x.com")):
            idx = self.build(first, second)
            self.assertEqual(sorted((l.user_id, l.link_type) for l in idx.links_for_resource("search_9")), [("user_1", REFERENCE), ("user_2", REFERENCE)])
            for user, other in (("user_1", "user_2"), ("user_2", "user_1")):
                a = assoc(idx, user)["search_9"]
                self.assertEqual((a.status, a.competing_users), ("AMBIGUOUS", (other,)), (first, user))

    def test_a_record_naming_one_subject_still_passes_the_reference_to_that_subject_only(self):
        idx = index([fr("e", [obs("e", "export_1", {"source_resource_id": "search_9", "requester_email": "ann.new@x.com"})])], manifest(e=BEFORE))
        self.assertEqual([(l.user_id, l.link_type) for l in idx.links_for_resource("search_9")], [("user_1", REFERENCE)])
        self.assertEqual((assoc(idx, "user_1")["search_9"].status, assoc(idx, "user_2")), ("ASSOCIATED", {}))

    def test_two_identifiers_of_the_same_subject_give_one_reference(self):
        o = obs("e", "export_1", {"source_resource_id": "search_9", "requester_email": "ann.new@x.com", "old_email": "ann.old@x.com"})
        idx = index([fr("e", [o])], manifest(e=BEFORE))
        self.assertEqual([(l.user_id, l.link_type) for l in idx.links_for_resource("search_9")], [("user_1", REFERENCE)])


if __name__ == "__main__":
    unittest.main()
