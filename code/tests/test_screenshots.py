import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import needs_dataset, real_dataset

from ghostaudit.dataset import Activity, Dataset, EvidenceFile, Identity
from ghostaudit.evidence import FileResult, Observation
from ghostaudit.identity import IdentityGraph
from ghostaudit.linker import POLICIES, build_link_index
from ghostaudit.screenshots import (CATALOG_OFFSET_SECONDS, OcrImage, canon, catalog_for_image, foreign_emails, ground, load_cache,
                                    status_words, verify_cache, visible_identifiers)
from ghostaudit.state import ACTIVE, DEIDENTIFIED, IDENTIFIABLE, PRESENT, UNKNOWN, assess, interpret
import ghostaudit.screenshots as S

EMAIL = "gabriel.silva@gmail.com"
GRAPH = IdentityGraph([Identity("user_13", "Gabriel Silva", EMAIL, "+55 11 91234 5678", hashlib.sha256(EMAIL.encode()).hexdigest(), "2026-01-01T00:00:00Z"),
                       Identity("user_14", "Aditi Jain", "aditi@x.com", "+91 1", hashlib.sha256(b"aditi@x.com").hexdigest(), "2026-01-01T00:00:00Z")], [])
CAPTURED = "2026-09-01T06:00:00Z"
ROW_TIME = "2026-09-01T06:07:01Z"


def row(rid, details, t=ROW_TIME, aid="activity_1"):
    return Activity(aid, t, "LINK_RESOURCE_IDENTITY", "data_catalog", "x", rid, "SUCCESS", details)


def image(*lines):
    return OcrImage("evidence_x", "evidence/images/x.png", "0" * 64, tuple(lines))


class Canonicalisation(unittest.TestCase):
    def test_glyph_confusions_collapse_on_both_sides(self):
        self.assertEqual(canon("g.silva76@gmaiI.com"), canon("g.silva76@gmail.com"))
        self.assertEqual(canon("user_ll • account _5762"), canon("user_11 account_5762"))
        self.assertEqual(canon("search_42B"), canon("search_428"))

    def test_different_values_stay_different(self):
        self.assertNotEqual(canon("g.silva76@gmail.com"), canon("g.silva76.jr@gmail.com"))
        self.assertNotEqual(canon("account_5762"), canon("account_5763"))


class Grounding(unittest.TestCase):
    def test_known_resource_id_in_ocr_grounds_with_content(self):
        g = ground(row("search_428", "Data catalog linked search_428 (search_428) to " + EMAIL + " and user_13."), image("Raw document · search_42B", "email g.silva76@gmaiI.com"), GRAPH, "user_13")
        self.assertEqual(g.level, "catalog+ocr")
        self.assertIn("resource_id", g.matched)

    def test_misleading_nothing_readable_falls_back_to_time_only_low_confidence(self):
        g = ground(row("tombstone_17", "Data catalog linked tombstone_17 (user_17) to x@y.co and user_17."), image("Retained reference", "Customer key"), GRAPH, None)
        self.assertEqual((g.level, g.matched), ("catalog_time_only", ()))

    def test_alias_in_parentheses_is_a_known_value(self):
        g = ground(row("object_19", "Attachment catalog confirmed object_19 (delivery_confirmation_19.pdf) belongs only to a@b.co and user_13; no other subject is linked."),
                   image("delivery_confirmation_ 19. pdf"), GRAPH, "user_13")
        self.assertEqual(g.matched, ("alias",))

    def test_catalog_rows_attach_to_images_by_the_systematic_offset_only(self):
        ev = EvidenceFile("evidence_x", "evidence/images/x.png", "o", CAPTURED)
        good, off = row("search_1", "x", ROW_TIME), row("search_2", "x", "2026-09-01T06:09:00Z", "activity_2")
        self.assertEqual(CATALOG_OFFSET_SECONDS, 421)
        self.assertEqual([a.activity_id for a in catalog_for_image(ev, [good, off])], ["activity_1"])


class Visibility(unittest.TestCase):
    def test_subject_identifiers_found_by_kind_only(self):
        v = visible_identifiers(image("Gabriel Silva", "g.silva76@gmaiI.com", "user_13"), GRAPH, "user_13")
        self.assertEqual(set(v), {"name", "user_ref"})  # the OCR'd e-mail is a different address (g.silva76) so it is NOT the subject's
        self.assertEqual(set(visible_identifiers(image(EMAIL.replace("l", "I")), GRAPH, "user_13")), {"email"})

    def test_other_peoples_emails_are_reported_but_never_become_subject_identifiers(self):
        img = image("aditi@x.com", EMAIL)
        self.assertEqual(foreign_emails(img, GRAPH, "user_13"), ("aditi@x.com",))
        self.assertEqual(visible_identifiers(img, GRAPH, "user_13"), ("email",))

    def test_status_words_are_whole_words(self):
        self.assertEqual(status_words(image("Mode Continuous", "Status Healthy", "Inactive node")), ("Continuous", "Healthy"))


class Interpretation(unittest.TestCase):
    def reading(self, rid, **fields):
        o = Observation("evidence_x", "image", "ocr", rid, fields={"visible_identifiers": [], "status_words": [], "ocr": True, **fields})
        return interpret(o)

    def test_data_flow_active_only_with_status_words(self):
        self.assertEqual(self.reading("data_flow_1", status_words=["Continuous", "Healthy"]).state, ACTIVE)
        self.assertEqual(self.reading("data_flow_1", status_words=[]).state, UNKNOWN)
        self.assertEqual(self.reading("data_flow_1", status_words=["Continuous", "Paused"]).state, UNKNOWN)

    def test_analytics_attributable_by_any_identifier_including_user_ref(self):
        self.assertEqual(self.reading("analytics_1", visible_identifiers=["user_ref"]).state, IDENTIFIABLE)
        self.assertEqual(self.reading("analytics_1").state, PRESENT)

    def test_support_and_tombstone_ignore_the_allowed_stable_reference(self):
        self.assertEqual(self.reading("support_1", visible_identifiers=["user_ref"]).state, PRESENT)
        self.assertEqual(self.reading("support_1", visible_identifiers=["email", "user_ref"]).state, IDENTIFIABLE)
        self.assertEqual(self.reading("tombstone_1", visible_identifiers=["user_ref"], status_words=["Retained reference"]).state, DEIDENTIFIED)
        self.assertEqual(self.reading("tombstone_1", visible_identifiers=["name"], status_words=["Retained reference"]).state, IDENTIFIABLE)

    def test_backup_listing_does_not_reveal_restore_access(self):
        r = self.reading("backup_1", visible_identifiers=["email"])
        self.assertEqual((r.state, assess(r.category, r.state).verdict), (PRESENT, "UNKNOWN"))

    def test_ownership_conflict_yields_no_state(self):
        r = self.reading("marketing_1", conflict=True, foreign_email_count=1)
        self.assertEqual((r.state, r.basis), (UNKNOWN, "screenshot.conflicting_subject"))

    def test_every_screenshot_reading_is_flagged_as_ocr(self):
        for rid in ("search_1", "data_flow_1", "support_1", "export_1"):
            self.assertTrue(dict(self.reading(rid, status_words=["Healthy"]).detail).get("ocr"))


class CacheIntegrity(unittest.TestCase):
    def test_stale_cache_entry_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "evidence/images").mkdir(parents=True)
            png = root / "evidence/images/x.png"
            png.write_bytes(b"original bytes")
            cache = {"evidence_x": OcrImage("evidence_x", "evidence/images/x.png", hashlib.sha256(b"original bytes").hexdigest(), ("text",))}
            self.assertEqual(verify_cache(cache, root), [])
            png.write_bytes(b"edited bytes")
            self.assertIn("stale cache", verify_cache(cache, root)[0])
            png.unlink()
            self.assertIn("missing", verify_cache(cache, root)[0])

    def test_missing_cache_file_is_empty_not_an_error(self):
        self.assertEqual(load_cache(Path("does/not/exist.json")), {})


class NoLeakIntoLinking(unittest.TestCase):
    """A screenshot lists other people too; those must never become links for the resource being audited."""

    def test_other_people_on_screen_do_not_create_ownership(self):
        ev = EvidenceFile("evidence_x", "evidence/images/x.png", "o", CAPTURED)
        cache = {"evidence_x": OcrImage("evidence_x", "evidence/images/x.png", "0" * 64,
                                        ("data_flow_1121", "Healthy Continuous", EMAIL, "aditi@x.com", "Gabriel Silva"))}
        ds = _FakeDataset([ev], [row("data_flow_1121", f"Data catalog linked data_flow_1121 (data_flow_1121) to {EMAIL} and user_13.")])
        orig = S.verify_cache
        S.verify_cache = lambda c, r: []  # bytes are not the point here
        try:
            results, notes = S.screenshot_observations(ds, GRAPH, cache)
        finally:
            S.verify_cache = orig
        obs = results[0].observations[0]
        self.assertEqual(obs.fields["foreign_email_count"], 1)
        self.assertNotIn("aditi@x.com", json.dumps(obs.fields))
        links = build_link_index(GRAPH, results, {"evidence_x": ev}, ds.activity.rows)
        self.assertEqual({l.user_id for l in links.links_for_resource("data_flow_1121")}, {"user_13"})


class CatalogAliasForDocuments(unittest.TestCase):
    def doc(self, text):
        o = Observation("evidence_18", "pdf", "$document", None, fields={"resource_ids": [], "pages": [[text]]}, raw=text)
        r = FileResult("evidence_18", "evidence/documents/s.pdf", "pdf", CAPTURED)
        r.observations = [o]
        return r, o

    def test_document_number_in_the_text_grounds_the_resource_the_catalog_names_by_that_number(self):
        r, o = self.doc("NORTHSTAR SHOP - CUSTOMER STATEMENT Statement NST-2026-0718-1842 ingrid")
        ds = _FakeDataset([], [row("export_1842", "Data catalog linked export_1842 (NST-2026-0718-1842) to ingrid.dahl@outlook.com and user_18.")])
        notes = S.attach_catalog_aliases(ds, [r])
        self.assertEqual(o.fields["resource_ids"], ["export_1842"])
        self.assertEqual(o.fields["catalog_alias_matches"][0]["catalog_activity"], "activity_1")
        self.assertEqual(len(notes), 1)

    def test_misleading_user_reference_in_parentheses_never_attaches_a_tombstone_to_a_register(self):
        r, o = self.doc("register: user_08 user_12 user_17 user_21")
        ds = _FakeDataset([], [row("tombstone_17", "Data catalog linked tombstone_17 (user_17) to x@y.co and user_17.")])
        self.assertEqual(S.attach_catalog_aliases(ds, [r]), [])
        self.assertEqual(o.fields["resource_ids"], [])

    def test_misleading_alias_that_is_absent_from_the_text_attaches_nothing(self):
        r, o = self.doc("an unrelated invoice NST-2026-0101-0001")
        ds = _FakeDataset([], [row("export_1842", "Data catalog linked export_1842 (NST-2026-0718-1842) to x@y.co and user_13.")])
        self.assertEqual(S.attach_catalog_aliases(ds, [r]), [])

    def test_non_pdf_results_are_untouched(self):
        r, o = self.doc("NST-2026-0718-1842")
        r.fmt = "json"
        self.assertEqual(S.attach_catalog_aliases(_FakeDataset([], [row("export_1842", "linked export_1842 (NST-2026-0718-1842) to x@y.co")]), [r]), [])


class _Table:
    def __init__(self, rows):
        self.rows = rows


class _FakeDataset:
    root = Path(".")

    def __init__(self, evidence, activities):
        self.evidence_files, self.activity = _Table(evidence), _Table(activities)

    def evidence_by_id(self):
        return {e.file_id: e for e in self.evidence_files.rows}


@needs_dataset
class RealScreenshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = real_dataset()
        cls.graph = IdentityGraph(cls.ds.identities.rows, cls.ds.audit_log.rows)
        cls.cache = load_cache()
        cls.results, cls.notes = S.screenshot_observations(cls.ds, cls.graph, cls.cache)

    def test_cache_matches_the_images_and_covers_all_eleven(self):
        self.assertEqual(len(self.cache), 11)
        self.assertEqual(verify_cache(self.cache, self.ds.root), [])

    def test_every_image_has_exactly_one_catalog_row(self):
        for r in self.results:
            ev = self.ds.evidence_by_id()[r.file_id]
            self.assertEqual(len(catalog_for_image(ev, self.ds.activity.rows)), 1, r.file_id)

    def test_measured_grounding_levels(self):
        from collections import Counter

        lv = Counter(o.fields["grounding"] for r in self.results for o in r.observations)
        self.assertEqual(dict(lv), {"catalog+ocr": 9, "catalog_time_only": 1, "conflict": 1})

    def test_marketing_screenshot_is_an_ownership_conflict(self):
        o = next(o for r in self.results for o in r.observations if o.resource_id == "marketing_001")
        self.assertTrue(o.fields["conflict"])
        self.assertEqual(interpret(o).state, UNKNOWN)
        self.assertTrue(any("marketing_001" in n and "conflict" in n for n in self.notes))

    def test_tombstone_screenshot_is_low_confidence(self):
        o = next(o for r in self.results for o in r.observations if o.resource_id == "tombstone_17")
        self.assertEqual(o.fields["grounding"], "catalog_time_only")
        self.assertTrue(any("tombstone_17" in n and "low confidence" in n for n in self.notes))

    def test_statement_pdf_evidences_the_export_for_case_33_but_not_for_case_52(self):
        from ghostaudit.reconstruction import CaseReconstructor

        cr = CaseReconstructor(self.ds)
        recs = cr.reconstruct_all([q for q in self.ds.requests.rows if q.case_id in ("case_33", "case_52")])
        d = recs["case_33"].decision("export_1842")
        self.assertEqual((d.state.state, d.state.citable, d.assessment.finding_op), (PRESENT, ("evidence_18",), "REPORT_REMAINING_RECORD"))
        self.assertIsNone(recs["case_52"].decision("export_1842"))  # the PDF was captured after case_52's audit: not citable, not even a candidate

    def test_no_screenshot_observation_links_a_resource_to_a_second_user(self):
        results = [r for r in self.results]
        idx = build_link_index(self.graph, results, self.ds.evidence_by_id(), self.ds.activity.rows)
        for r in results:
            for o in r.observations:
                self.assertEqual(len({l.user_id for l in idx.links_for_resource(o.resource_id)}), 1, o.resource_id)


if __name__ == "__main__":
    unittest.main()
