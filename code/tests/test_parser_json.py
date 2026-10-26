import json
import unittest

from tests.helpers import crlf

from ghostaudit.parsers.json_parser import parse_json_text

BUNDLE = {
    "exported_at": "2026-09-15T12:00:00Z",
    "source": "records_assurance_service",
    "snapshots": [
        {"captured_at": "2026-09-15T12:00:00Z", "data_store": "account_service", "collection": "customer_profiles", "records": []},
        {
            "captured_at": "2026-09-15T12:00:00Z", "data_store": "identity_service", "collection": "deleted_customer_references",
            "records": [
                {"resource_id": "tombstone_9178", "subject_reference": "user_542", "name": "Kofi Addo", "email": "addo.kofi@gmail.com", "account_status": "DELETED"},
                {"resource_id": "tombstone_440", "subject_reference": "user_541", "name": None, "email": None, "account_status": "DELETED",
                 "recent_activity": [{"event": "x"}]},
            ],
        },
    ],
}
EXPORT = {
    "exported_at": "2026-09-15T11:44:55Z", "region": "global", "source": "records_assurance_gateway",
    "service_exports": {
        "search_service": {"index_queries": [{
            "hits": {"documents": [{"resource_id": "search_1993", "email": "noah@outlook.com"}], "total": 1},
            "observed_at": "2026-09-15T09:14:18Z", "query": "noah@outlook.com", "resource_id": "search_1993"}]},
        "account_service": {"profile_lookups": [{"observed_at": "2026-09-15T09:31:40Z", "profile": None, "resource_id": "account_3136", "status_code": 404}]},
    },
}


def parse(doc, text=None):
    return parse_json_text("evidence_x", "x.json", text if text is not None else json.dumps(doc, ensure_ascii=False), "2026-09-15T12:00:00Z")


class SnapshotBundle(unittest.TestCase):
    def setUp(self):
        self.r = parse(BUNDLE)

    def test_records_and_accounting(self):
        self.assertEqual([o.resource_id for o in self.r.observations], ["tombstone_9178", "tombstone_440"])
        self.assertEqual((self.r.input_units, self.r.failed_units), (2, 0))
        self.assertTrue(self.r.accounting_ok())

    def test_empty_snapshot_is_preserved_as_evidence(self):
        empty = [c for c in self.r.containers if c.record_count == 0]
        self.assertEqual(len(empty), 1)
        self.assertEqual((empty[0].system_id, empty[0].name, empty[0].stated_time), ("account_service", "customer_profiles", "2026-09-15T12:00:00Z"))

    def test_system_and_collection_come_from_container_not_inferred(self):
        o = self.r.observations[0]
        self.assertEqual((o.system_id, o.collection, o.source_time), ("identity_service", "deleted_customer_references", "2026-09-15T12:00:00Z"))
        self.assertIsNone(o.observed_at)  # record has no timestamp of its own; the file time is NOT copied in

    def test_null_values_stay_none_and_nested_details_stay_in_record(self):
        o = self.r.observations[1]
        self.assertIsNone(o.fields["name"])
        self.assertEqual(o.fields["recent_activity"], [{"event": "x"}])
        self.assertEqual(len(self.r.observations), 2)  # recent_activity item is not a separate record

    def test_locators_are_stable_paths(self):
        self.assertEqual(self.r.observations[0].locator, "$.snapshots[1].records[0]")


class ServiceExport(unittest.TestCase):
    def setUp(self):
        self.r = parse(EXPORT)

    def test_nested_resource_bearing_dict_is_emitted_with_parent(self):
        got = [(o.resource_id, o.locator, o.parent) for o in self.r.observations]
        outer = "$.service_exports.search_service.index_queries[0]"
        self.assertIn(("search_1993", outer, None), got)
        self.assertIn(("search_1993", outer + ".hits.documents[0]", outer), got)
        self.assertEqual(self.r.input_units, 3)
        self.assertTrue(self.r.accounting_ok())

    def test_system_is_the_service_key(self):
        by = {o.resource_id: o for o in self.r.observations}
        self.assertEqual(by["account_3136"].system_id, "account_service")
        self.assertEqual(by["account_3136"].collection, "profile_lookups")
        self.assertEqual(by["account_3136"].observed_at, "2026-09-15T09:31:40Z")
        self.assertIsNone(by["account_3136"].fields["profile"])

    def test_identifiers_extracted_from_values(self):
        o = [o for o in self.r.observations if o.locator.endswith("index_queries[0]")][0]
        kinds = {(i.kind, i.norm) for i in o.identifiers()}
        self.assertIn(("email", "noah@outlook.com"), kinds)


class Robustness(unittest.TestCase):
    def test_list_item_without_resource_id_is_a_failure(self):
        r = parse({"exported_at": "2026-01-01T00:00:00Z", "records": [{"resource_id": "account_1"}, {"nope": 1}, "str"]})
        self.assertEqual(len(r.observations), 1)
        self.assertEqual(len(r.failures), 2)
        self.assertEqual(r.failures[0].locator, "$.records[1]")

    def test_non_string_or_empty_resource_id_is_a_failure_and_counted(self):
        r = parse({"records": [{"resource_id": 12}, {"resource_id": " "}, {"resource_id": "account_2"}]})
        self.assertEqual((len(r.observations), len(r.failures), r.failed_units, r.input_units), (1, 2, 2, 3))
        self.assertTrue(r.accounting_ok())

    def test_invalid_json(self):
        r = parse(None, '{"records": [')
        self.assertEqual(len(r.failures), 1)
        self.assertIn("invalid JSON", r.failures[0].reason)

    def test_top_level_must_be_object(self):
        self.assertEqual(len(parse(None, "[1,2]").failures), 1)

    def test_unrecognised_shape(self):
        self.assertEqual(len(parse({"foo": 1}).failures), 1)

    def test_duplicate_keys_warn(self):
        r = parse(None, '{"records": [{"resource_id": "account_1", "email": "a@b.co", "email": "c@d.co"}]}')
        self.assertEqual(len(r.observations), 1)
        self.assertTrue(any("duplicate object keys" in w.reason for w in r.warnings))

    def test_record_count_mismatch_warns(self):
        r = parse({"record_count": 5, "records": [{"resource_id": "account_1"}]})
        self.assertTrue(any("record_count=5" in w.reason for w in r.warnings))

    def test_bad_timestamp_warns_but_keeps_record(self):
        r = parse({"records": [{"resource_id": "account_1", "observed_at": "soon"}]})
        self.assertEqual(len(r.observations), 1)
        self.assertEqual(len(r.warnings), 1)

    def test_crlf_equivalent_and_bom_tolerated_by_decoder(self):
        a = parse(BUNDLE)
        b = parse(BUNDLE, crlf(json.dumps(BUNDLE, indent=2, ensure_ascii=False)))
        self.assertEqual([o.fields for o in a.observations], [o.fields for o in b.observations])
        from ghostaudit.parsers.common import decode_text

        self.assertEqual(json.loads(decode_text(b"\xef\xbb\xbf" + json.dumps({"a": "é"}, ensure_ascii=False).encode("utf-8"))), {"a": "é"})

    def test_decode_never_falls_back_to_cp1252(self):
        from ghostaudit.parsers.common import decode_text

        with self.assertRaises(UnicodeDecodeError):
            decode_text("é".encode("cp1252"))


if __name__ == "__main__":
    unittest.main()
