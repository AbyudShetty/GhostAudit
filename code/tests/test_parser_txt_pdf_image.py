import struct
import unittest

from tests.helpers import crlf, needs_dataset, real_results

from ghostaudit.evidence import extract_identifiers, is_null, Observation
from ghostaudit.parsers.image_parser import parse_image_bytes
from ghostaudit.parsers.txt_parser import parse_txt_text

HANDOVER = """CUSTOMER PLATFORM - SHIFT HANDOVER
Snapshot: 2026-10-01T12:00:00Z

Account and downstream checks completed during the shift:

- 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.example18@example.test
- 2026-10-01T12:00:00Z | account_service | account_1810 | USER_PROFILE | NOT_FOUND | LEE.SAMPLE94@EXAMPLE.TEST
- 2026-10-01T12:00:00Z | analytics_platform | analytics_6830 | ANALYTICS_PROFILE | ANONYMIZED | no direct identifier
"""


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8


class Txt(unittest.TestCase):
    def test_entries_header_and_accounting(self):
        r = parse_txt_text("evidence_x", "h.txt", HANDOVER)
        self.assertEqual((r.input_units, len(r.observations), r.non_evidence_units, len(r.failures)), (6, 3, 3, 0))  # title, Snapshot:, prose + 3 entries
        self.assertEqual(r.meta["snapshot"], "2026-10-01T12:00:00Z")
        self.assertTrue(r.accounting_ok())

    def test_fields_preserved_verbatim_including_case(self):
        o = parse_txt_text("e", "h.txt", HANDOVER).observations[1]
        self.assertEqual((o.resource_id, o.system_id, o.collection, o.observed_at), ("account_1810", "account_service", "USER_PROFILE", "2026-10-01T12:00:00Z"))
        self.assertEqual(o.fields, {"category": "USER_PROFILE", "state": "NOT_FOUND", "identifier": "LEE.SAMPLE94@EXAMPLE.TEST"})
        self.assertEqual([i.norm for i in o.identifiers()], ["lee.sample94@example.test"])  # normalised only for joining

    def test_no_direct_identifier_text_is_kept_verbatim(self):
        o = parse_txt_text("e", "h.txt", HANDOVER).observations[2]
        self.assertEqual(o.fields["identifier"], "no direct identifier")
        self.assertEqual(o.identifiers(), [])

    def test_malformed_entries_reported(self):
        text = HANDOVER + "- 2026-10-01T12:00:00Z | account_service | account_9 | USER_PROFILE | NOT_FOUND\n- yesterday | a | b | c | d | e\n"
        r = parse_txt_text("e", "h.txt", text)
        self.assertEqual(len(r.failures), 2)
        self.assertEqual(len(r.observations), 3)
        self.assertTrue(r.accounting_ok())

    def test_crlf(self):
        a = parse_txt_text("e", "h.txt", HANDOVER)
        b = parse_txt_text("e", "h.txt", crlf(HANDOVER))
        self.assertEqual([o.fields for o in a.observations], [o.fields for o in b.observations])


class Image(unittest.TestCase):
    def test_png_header_read_without_interpretation(self):
        r = parse_image_bytes("evidence_x", "a.png", png(1800, 1200))
        self.assertEqual((r.meta["width"], r.meta["height"]), (1800, 1200))
        self.assertFalse(r.parsed)
        self.assertEqual(r.observations, [])
        self.assertIn("not interpreted", r.meta["status"])

    def test_non_png_is_failure(self):
        r = parse_image_bytes("evidence_x", "a.png", b"GIF89a" + b"\x00" * 40)
        self.assertEqual((len(r.failures), r.failed_units), (1, 1))


class Identifiers(unittest.TestCase):
    def obs(self, **fields):
        return Observation("e", "json", "$", "account_1", fields=fields)

    def test_kinds_and_normalisation(self):
        h = "f2989219bf906a4c016a25386110c34a55649a1f19aeba522e01067e28c9c153"
        o = self.obs(email="Jo.Example@Example.Test", ref="user_01", phone="+44 20 7946 0958", hash=h, note="plain words")
        got = {(i.kind, i.norm) for i in extract_identifiers(o)}
        self.assertEqual(got, {("email", "jo.example@example.test"), ("user_ref", "user_01"), ("phone", "+442079460958"), ("email_hash", h)})

    def test_integrity_seal_is_not_an_email_hash(self):
        seal = "sha256:" + "0" * 60 + "1afd"
        self.assertEqual(extract_identifiers(self.obs(integrity_seal=seal, other=seal)), [])

    def test_user_ref_not_matched_inside_longer_tokens(self):
        got = extract_identifiers(self.obs(a="superuser_12x user_7a", b="user_9"))
        self.assertEqual([(i.norm, i.field) for i in got], [("user_9", "fields.b")])

    def test_nested_values_and_deterministic_order(self):
        o = self.obs(a={"x": ["u@v.io", {"y": "user_2"}]})
        first = extract_identifiers(o)
        self.assertEqual([(i.kind, i.field) for i in first], [("email", "fields.a.x[0]"), ("user_ref", "fields.a.x[1].y")])
        self.assertEqual(first, extract_identifiers(o))

    def test_is_null(self):
        for v in (None, "", "null", "NULL", " None "):
            self.assertTrue(is_null(v))
        for v in ("x", 0, False):
            self.assertFalse(is_null(v))


@needs_dataset
class RealPdfs(unittest.TestCase):
    def test_security_retention_extract(self):
        o = real_results()["evidence_55"].observations[0]
        self.assertEqual(o.fields["resource_ids"], ["audit_record_003"])
        stamps = dict(o.fields["timestamps"])
        self.assertEqual(stamps["recorded"], "2026-08-25T04:03:00Z")
        self.assertEqual(stamps["issued"], "2026-09-05T06:30:00Z")
        self.assertEqual(o.resource_ids(), ("audit_record_003",))
        self.assertTrue(any(i.norm == "user_03" for i in o.identifiers()))

    def test_support_attachment_has_contact_details_and_stamps(self):
        o = real_results()["evidence_58"].observations[0]
        self.assertEqual(o.fields["resource_ids"], ["support_001"])
        norms = {i.norm for i in o.identifiers()}
        self.assertIn("priya.raman@gmail.com", norms)
        self.assertIn("+919876573142", norms)
        stamps = dict(o.fields["timestamps"])
        self.assertEqual(stamps["captured"], "2026-09-12T07:30:00Z")

    def test_pdfs_without_resource_ids_still_preserve_identifiers(self):
        o = real_results()["evidence_18"].observations[0]
        self.assertEqual(o.fields["resource_ids"], [])
        norms = {i.norm for i in o.identifiers()}
        self.assertTrue({"user_18", "ingrid.dahl@outlook.com"} <= norms)
        self.assertIn("NST-2026-0718-1842", o.raw)

    def test_register_pdf_lists_users(self):
        o = real_results()["evidence_27"].observations[0]
        self.assertEqual({i.norm for i in o.identifiers() if i.kind == "user_ref"}, {"user_08", "user_12", "user_17", "user_21", "user_24", "user_29"})


if __name__ == "__main__":
    unittest.main()
