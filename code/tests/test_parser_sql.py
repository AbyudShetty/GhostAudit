import unittest

from tests.helpers import crlf

from ghostaudit.parsers.sql_parser import parse_sql_text

TOPOLOGY = """-- Ghost Data Still Exists: point-in-time database snapshot
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-21T10:00:00Z
-- Database topology: read_replica

BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (
    resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL,
    record_json TEXT NOT NULL CHECK (json_valid(record_json)), updated_at TEXT NOT NULL
);
CREATE TABLE customer_data_read_replica (
    resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL,
    record_json TEXT NOT NULL CHECK (json_valid(record_json)), updated_at TEXT NOT NULL
);
INSERT INTO "customer_data_primary" VALUES('search_1','search_service','customer_search_documents','{"resource_id":"search_1","document":{"display_name":"Arthur Lefèvre","email":"a.lefevre74@yahoo.com"}}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_read_replica" VALUES('account_994','account_service','customer_profiles','{"resource_id":"account_994","email":"diya.k4436@outlook.com"}','2026-09-21T10:00:00Z');
INSERT INTO "customer_data_read_replica" VALUES('search_1','search_service','customer_search_documents','{"resource_id":"search_1","document":{"display_name":"Arthur Lefèvre","email":"a.lefevre74@yahoo.com"}}','2026-09-21T10:00:00Z');
COMMIT;
"""

INVENTORY = """PRAGMA foreign_keys=ON;
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
CREATE TABLE connector_subscriptions (resource_id TEXT PRIMARY KEY, customer_reference TEXT, customer_email TEXT, connector_name TEXT NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)), last_delivery_status TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES ('account_service', '2026-09-16T08:37:39Z');
INSERT INTO snapshot_scope VALUES ('search_service', '2026-09-16T08:37:39Z');
INSERT INTO connector_subscriptions VALUES ('data_flow_2406', NULL, 'ishaan.iyer@outlook.com', 'customer_events_connector', '1', 'delivered', '2026-09-16T07:04:46Z');
COMMIT;
-- snapshot_completed_at=2026-09-16T08:37:39Z
"""


class SqlTopology(unittest.TestCase):
    def setUp(self):
        self.r = parse_sql_text("evidence_x", "x.sql", TOPOLOGY, "2026-09-21T10:00:00Z")

    def test_counts_and_accounting(self):
        self.assertEqual(self.r.input_units, 3)
        self.assertEqual(len(self.r.observations), 3)
        self.assertEqual(self.r.failures, [])
        self.assertTrue(self.r.accounting_ok())

    def test_copy_preserves_primary_vs_replica(self):
        copies = sorted((o.resource_id, o.copy) for o in self.r.observations)
        self.assertEqual(copies, [("account_994", "customer_data_read_replica"), ("search_1", "customer_data_primary"),
                                  ("search_1", "customer_data_read_replica")])

    def test_header_metadata_and_containers(self):
        self.assertEqual(self.r.meta["database_topology"], "read_replica")
        self.assertEqual(self.r.meta["captured_at"], "2026-09-21T10:00:00Z")
        self.assertEqual({c.name: c.record_count for c in self.r.containers}, {"customer_data_primary": 1, "customer_data_read_replica": 2})

    def test_record_json_is_decoded_and_utf8_preserved(self):
        o = [o for o in self.r.observations if o.resource_id == "search_1"][0]
        self.assertEqual(o.fields["record"]["document"]["display_name"], "Arthur Lefèvre")
        self.assertEqual(o.system_id, "search_service")
        self.assertEqual(o.collection, "customer_search_documents")
        self.assertEqual(o.observed_at, "2026-09-21T10:00:00Z")

    def test_crlf_and_lf_give_identical_results(self):
        a = parse_sql_text("e", "x.sql", TOPOLOGY)
        b = parse_sql_text("e", "x.sql", crlf(TOPOLOGY))
        self.assertEqual([(o.resource_id, o.copy, o.fields) for o in a.observations], [(o.resource_id, o.copy, o.fields) for o in b.observations])

    def test_deterministic(self):
        a = parse_sql_text("e", "x.sql", TOPOLOGY)
        b = parse_sql_text("e", "x.sql", TOPOLOGY)
        self.assertEqual([o.raw for o in a.observations], [o.raw for o in b.observations])


class SqlInventory(unittest.TestCase):
    def test_snapshot_scope_preserved_not_dropped(self):
        r = parse_sql_text("e", "inv.sql", INVENTORY)
        self.assertEqual(r.input_units, 3)
        self.assertEqual(len(r.observations), 1)
        self.assertEqual(r.non_evidence_units, 2)
        self.assertEqual(r.meta["snapshot_completed_at"], "2026-09-16T08:37:39Z")
        self.assertEqual([x["origin_system"] for x in r.meta["tables_without_resource_id"]["snapshot_scope"]], ["account_service", "search_service"])
        self.assertTrue(r.accounting_ok())

    def test_raw_columns_not_coerced(self):
        o = parse_sql_text("e", "inv.sql", INVENTORY).observations[0]
        self.assertEqual(o.fields["enabled"], 1)
        self.assertIsNone(o.fields["customer_reference"])
        self.assertEqual(o.collection, "connector_subscriptions")


class SqlBadInput(unittest.TestCase):
    def test_failed_insert_is_reported_with_line_and_counted(self):
        bad = TOPOLOGY.replace("COMMIT;", "INSERT INTO \"customer_data_primary\" VALUES('search_9','search_service');\nCOMMIT;")
        r = parse_sql_text("e", "x.sql", bad)
        self.assertEqual(len(r.failures), 1)
        self.assertIn("sqlite error", r.failures[0].reason)
        self.assertRegex(r.failures[0].locator, r"^stmt@line:\d+$")
        self.assertEqual(r.failed_units, 1)
        self.assertTrue(r.accounting_ok())

    def test_duplicate_primary_key_is_reported_not_dropped_silently(self):
        dup = TOPOLOGY.replace("COMMIT;", TOPOLOGY.split("\n")[14] + "\nCOMMIT;")
        r = parse_sql_text("e", "x.sql", dup)
        self.assertEqual(len(r.failures), 1)
        self.assertIn("UNIQUE", r.failures[0].reason)

    def test_invalid_json_rejected_by_check_constraint_is_reported(self):
        bad = TOPOLOGY.replace("COMMIT;", "INSERT INTO \"customer_data_primary\" VALUES('search_9','search_service','c','{not json','2026-09-21T10:00:00Z');\nCOMMIT;")
        r = parse_sql_text("e", "x.sql", bad)
        self.assertEqual(len(r.failures), 1)
        self.assertEqual(r.failed_units, 1)

    def test_garbled_trailing_statement_reported(self):
        r = parse_sql_text("e", "x.sql", TOPOLOGY + "INSERT INTO customer_data_primary VALUES('x'\n")
        self.assertTrue(any("sqlite error" in f.reason for f in r.failures))

    def test_unparseable_timestamp_is_a_warning_and_record_kept(self):
        t = TOPOLOGY.replace("'2026-09-21T10:00:00Z');\nINSERT INTO \"customer_data_read_replica\" VALUES('account_994'", "'yesterday');\nINSERT INTO \"customer_data_read_replica\" VALUES('account_994'", 1)
        r = parse_sql_text("e", "x.sql", t)
        self.assertEqual(len(r.warnings), 1)
        self.assertEqual(len(r.observations), 3)

    def test_empty_file(self):
        r = parse_sql_text("e", "x.sql", "")
        self.assertEqual((r.input_units, len(r.observations), len(r.failures)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
