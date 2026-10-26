"""Tests against the real dataset. Expected values come from tests/expected_counts.py, which was
measured with raw byte/line counting independent of the parsers (see that file's docstring)."""

import hashlib
import json
import os
import subprocess
import sys
import unittest

from tests.expected_counts import EXPECTED_COUNTS, IMAGE_SIZES, LOG_DIALECTS, PDF_PAGES, TOTAL_INPUT_UNITS, TOTAL_OBSERVATIONS
from tests.helpers import CODE_DIR, DATASET, needs_dataset, real_dataset, real_results

from ghostaudit.dataset import evidence_disk_check
from ghostaudit.inventory import build_inventory
from ghostaudit.parsers import parse_all
from ghostaudit.schema import RESOURCE_PREFIX_SYSTEM, resource_prefix, system_for_resource


@needs_dataset
class DatasetTables(unittest.TestCase):
    def test_row_counts_match_documented_inventory(self):
        ds = real_dataset()
        self.assertEqual(
            [len(t.rows) for t in ds.tables()],
            [518, 32, 850, 306, 4011, 15, 94],
        )
        self.assertEqual(len(ds.sample_output), 32)

    def test_no_structural_issues(self):
        self.assertEqual([str(i) for i in real_dataset().issues()], [])

    def test_manifest_matches_disk(self):
        self.assertEqual(evidence_disk_check(real_dataset()), ([], []))

    def test_request_case_ids_are_unique_and_already_naturally_ordered(self):
        from ghostaudit.schema import case_sort_key

        ids = [r.case_id for r in real_dataset().requests.rows]
        self.assertEqual(len(set(ids)), 518)
        self.assertEqual(ids, sorted(ids, key=case_sort_key))
        self.assertNotEqual(ids, sorted(ids))  # string order would differ -> the ordering decision matters

    def test_multilingual_request_text_decoded_as_utf8(self):
        texts = " ".join(r.request_text for r in real_dataset().requests.rows)
        for needle in ("削除", "삭제", "suppression définitive", "exclusão permanente", "eliminación permanente"):
            self.assertIn(needle, texts)
        self.assertNotIn("�", texts)

    def test_every_activity_prefix_matches_schema_system_mapping(self):
        # Only deletion-worker / sync events name the real owning system as target_system.
        mism = []
        for a in real_dataset().activity.rows:
            if a.event_type in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS"):
                if a.target_system != system_for_resource(a.resource_id):
                    mism.append((a.activity_id, a.target_system, a.resource_id))
        self.assertEqual(mism, [])

    def test_email_hash_is_sha256_of_email(self):
        for ident in real_dataset().identities.rows:
            self.assertEqual(hashlib.sha256(ident.email.encode()).hexdigest(), ident.email_hash, ident.user_id)


@needs_dataset
class ParsersOnRealEvidence(unittest.TestCase):
    def test_per_file_counts_equal_independent_measurement(self):
        res = real_results()
        self.assertEqual(set(res), set(EXPECTED_COUNTS))
        wrong = {fid: ((r.input_units, len(r.observations)), EXPECTED_COUNTS[fid]) for fid, r in res.items()
                 if (r.input_units, len(r.observations)) != EXPECTED_COUNTS[fid]}
        self.assertEqual(wrong, {})

    def test_totals(self):
        res = real_results().values()
        self.assertEqual(sum(r.input_units for r in res), TOTAL_INPUT_UNITS)
        self.assertEqual(sum(len(r.observations) for r in res), TOTAL_OBSERVATIONS)

    def test_zero_failures_and_every_unit_accounted_for(self):
        for fid, r in real_results().items():
            with self.subTest(file=fid):
                self.assertEqual([str(f) for f in r.failures], [])
                self.assertEqual([str(w) for w in r.warnings], [])
                if r.fmt != "pdf":
                    self.assertTrue(r.accounting_ok(), (r.input_units, len(r.observations), r.non_evidence_units, r.failed_units))

    def test_log_dialects_equal_independent_survey(self):
        res = real_results()
        for fid, expected in LOG_DIALECTS.items():
            with self.subTest(file=fid):
                self.assertEqual(res[fid].meta["dialects"], expected)
        self.assertEqual({fid for fid, r in res.items() if r.fmt == "log"}, set(LOG_DIALECTS))

    def test_pdf_pages_and_image_sizes(self):
        res = real_results()
        for fid, pages in PDF_PAGES.items():
            self.assertEqual(res[fid].meta["pages"], pages, fid)
        for fid, size in IMAGE_SIZES.items():
            self.assertEqual((res[fid].meta["width"], res[fid].meta["height"]), size, fid)
            self.assertFalse(res[fid].parsed)

    def test_every_observation_is_traceable_and_well_formed(self):
        for fid, r in real_results().items():
            for o in r.observations:
                self.assertEqual(o.file_id, fid)
                self.assertTrue(o.locator)
                if o.fmt != "pdf":
                    self.assertTrue(o.resource_id and resource_prefix(o.resource_id) in RESOURCE_PREFIX_SYSTEM, (fid, o.locator, o.resource_id))

    def test_deterministic_across_runs(self):
        def signature(results):
            return hashlib.sha256(json.dumps(
                [(o.file_id, o.locator, o.resource_id, o.system_id, o.collection, o.copy, o.observed_at, o.raw, o.fields, o.trace_events)
                 for r in results for o in r.observations], sort_keys=True, default=str).encode()).hexdigest()

        self.assertEqual(signature(list(real_results().values())), signature(parse_all(real_dataset())))

    def test_empty_snapshot_containers_found(self):
        # e.g. evidence_51 has an account_service customer_profiles snapshot with zero records
        empties = [(c.file_id, c.system_id, c.name) for r in real_results().values() for c in r.containers if c.record_count == 0]
        self.assertIn(("evidence_51", "account_service", "customer_profiles"), empties)

    def test_replica_copies_distinguished_in_sql(self):
        copies = {o.copy for o in real_results()["evidence_05"].observations}
        self.assertEqual(copies, {"customer_data_primary", "customer_data_read_replica"})
        copies = {o.copy for o in real_results()["evidence_29"].observations}
        self.assertEqual(copies, {"customer_data_primary", "customer_data_replica_ap_south", "customer_data_replica_eu_west"})

    def test_known_sample_facts_are_extractable(self):
        res = real_results()
        # case_16: account_994 is still present in the read replica but absent from the primary (replication drift)
        rows = [o for o in res["evidence_05"].observations if o.resource_id == "account_994"]
        self.assertEqual([o.copy for o in rows], ["customer_data_read_replica"])
        self.assertEqual(rows[0].fields["record"]["email"], "diya.k4436@outlook.com")
        # case_09: restore risk for backup_4978 visible in the inventory snapshot
        b = [o for o in res["evidence_93"].observations if o.resource_id == "backup_4978"][0]
        self.assertEqual((b.fields["restore_allowed"], b.fields["customer_email"]), (1, "k.appiah88@gmail.com"))
        # case_12: the contact record is keyed by an *old* email hash -> a hash identifier on a trace-linked line
        o = [o for o in res["evidence_01"].observations if o.resource_id == "analytics_9914"][0]
        self.assertIn("44a65154bfd8e408852578fbd76813d094d1944f255ac1769ee81ba90492f10e", {i.norm for i in o.identifiers() if i.kind == "email_hash"})
        # case_31/32: tombstones (evidence_51)
        t = {o.resource_id: o for o in res["evidence_51"].observations if o.resource_id.startswith("tombstone")}
        self.assertIsNone(t["tombstone_440"].fields["email"])
        self.assertEqual(t["tombstone_9178"].fields["email"], "addo.kofi@gmail.com")

    def test_trace_linking_on_real_log(self):
        # case_18: in evidence_94 the support ticket's requester email is on the resource line itself,
        # while for backups the lookup_key (user ref) is on the *_started line of the same trace.
        o = [o for o in real_results()["evidence_94"].observations if o.resource_id == "backup_7809"][0]
        self.assertTrue(any(i.kind == "user_ref" and i.norm == "user_49" for i in o.identifiers()))


@needs_dataset
class InventoryAndCli(unittest.TestCase):
    def test_inventory_totals(self):
        inv = build_inventory(real_dataset(), list(real_results().values()))
        self.assertEqual(inv["totals"]["files"], 94)
        self.assertEqual(inv["totals"]["failures"], 0)
        self.assertEqual(inv["totals"]["images_not_interpreted"], 11)
        self.assertEqual(set(inv["by_format"]), {"image", "json", "log", "pdf", "sql", "txt"})
        self.assertEqual({k: v["files"] for k, v in inv["by_format"].items()}, {"image": 11, "json": 28, "log": 26, "pdf": 4, "sql": 24, "txt": 1})

    def test_cli_runs_under_cp1252_console_encoding(self):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        p = subprocess.run([sys.executable, "-m", "ghostaudit", "--dataset", str(DATASET), "inventory"], cwd=CODE_DIR, env=env,
                           capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"== Totals ==", p.stdout)

    def test_dataset_files_unchanged_by_running_the_pipeline(self):
        def digest():
            h = hashlib.sha256()
            for p in sorted(DATASET.rglob("*")):
                if p.is_file():
                    h.update(p.relative_to(DATASET).as_posix().encode())
                    h.update(p.read_bytes())
            return h.hexdigest()

        before = digest()
        parse_all(real_dataset())
        self.assertEqual(before, digest())


if __name__ == "__main__":
    unittest.main()
