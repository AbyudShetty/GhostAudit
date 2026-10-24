"""code.zip builder and verifier: what goes in, what stays out, and that the verifier inspects the archive itself."""

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.helpers import CODE_DIR

from tools import package  # noqa: E402  (code/tools is importable because tests run with code/ as the top-level directory)

REPORT = ("# Evaluation report\n\n" + "status accuracy, step-level accuracy, field-level accuracy, sequence accuracy, additional evaluation cases, "
          "failures found and improvements made. " * 8)
USAGE = ("# Usage report\n\n" + "provider, model calls, input tokens, output tokens, tokens per request, cost. " * 12)
GOOD = {
    "README.md": "# readme\n",
    "code/main.py": "print('hi')\n",
    "code/ghostaudit/__init__.py": "",
    "code/ghostaudit/cli.py": "def main():\n    return 0\n",
    "code/tests/test_x.py": "import unittest\n",
    "code/evaluation/evaluation_report.md": REPORT,
    "code/evaluation/usage_report.md": USAGE,
}


def write_tree(root: Path, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))


def make_zip(path: Path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return path


class Build(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.root = self.tmp / "repo"
        write_tree(self.root, GOOD)

    def tearDown(self):
        self._t.cleanup()

    def names(self, z):
        with zipfile.ZipFile(z) as f:
            return f.namelist()

    def test_a_valid_tree_builds_verifies_and_is_deterministic(self):
        a, b = self.tmp / "a.zip", self.tmp / "b.zip"
        count, skipped = package.build(self.root, a)
        package.build(self.root, b)
        self.assertEqual((count, skipped), (len(GOOD), []))
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(package.verify(a)[0], [])
        self.assertEqual(self.names(a), sorted(GOOD))  # sorted, forward slashes, no directory entries

    def test_unwanted_files_are_left_out_with_a_reason(self):
        write_tree(self.root, {
            "code/ghostaudit/__pycache__/cli.cpython-310.pyc": b"\x00junk", "code/ghostaudit/old.pyc": b"x", "code/.venv/lib/x.py": "x=1\n", "code/venv/y.py": "y=1\n",
            "code/node_modules/z.js": "1", "code/.pytest_cache/v": "1", "code/nested.zip": b"PK\x03\x04", "code/backup.tar.gz": b"\x1f\x8b", "code/data/deletion_requests.csv": "a,b\n",
            "code/data/sample_output.csv": "a\n", "code/docs/m5_sample_report.md": "dump", "code/docs/m3_sample_report.md": "dump", "code/notes.tmp": "t", "code/.DS_Store": b"x",
            "code/.env": "SECRET=1", "code/log.txt": "log", "code/output.csv": "o",
            "dataset/deletion_requests.csv": "a\n", "output.csv": "o", "log.txt": "l", "stray.txt": "not under code/",
        })
        out = self.tmp / "o.zip"
        count, skipped = package.build(self.root, out)
        self.assertEqual(self.names(out), sorted(GOOD))
        self.assertEqual(count, len(GOOD))
        reasons = dict(skipped)
        self.assertEqual(reasons["code/nested.zip"], "nested archive")
        self.assertEqual(reasons["code/backup.tar.gz"], "nested archive")
        self.assertIn("per-case dump", reasons["code/docs/m5_sample_report.md"])
        self.assertEqual(reasons["code/data/deletion_requests.csv"], "dataset file name")
        self.assertNotIn("dataset/deletion_requests.csv", reasons)  # only README.md and code/ are ever considered
        self.assertNotIn("stray.txt", reasons)
        self.assertEqual(package.verify(out)[0], [])

    def test_refuses_to_write_the_archive_inside_code(self):
        self.assertEqual(package.main(["--root", str(self.root), "--out", str(self.root / "code" / "x.zip")]), 2)
        self.assertFalse((self.root / "code" / "x.zip").exists())

    def test_cli_builds_and_verifies(self):
        out = self.tmp / "cli.zip"
        self.assertEqual(package.main(["--root", str(self.root), "--out", str(out)]), 0)
        self.assertEqual(package.main(["--verify", str(out)]), 0)


class Verify(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def problems(self, files, **kw):
        return package.verify(make_zip(self.tmp / "t.zip", files), **kw)[0]

    def raw_name_zip(self, name):
        """zipfile rewrites backslashes in names on Windows; setting .filename afterwards keeps them, as a hostile archive would."""
        p = self.tmp / "raw.zip"
        with zipfile.ZipFile(p, "w") as z:
            for n, body in GOOD.items():
                z.writestr(n, body)
            info = zipfile.ZipInfo("placeholder.py")
            info.filename = name
            z.writestr(info, "x=1\n")
        return package.verify(p)[0]

    def has(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), (fragment, problems))

    def test_good_archive_has_no_problems(self):
        self.assertEqual(self.problems(GOOD), [])

    def test_missing_required_files_and_tests(self):
        for rel in ("README.md", "code/main.py", "code/evaluation/evaluation_report.md", "code/evaluation/usage_report.md"):
            self.has(self.problems({k: v for k, v in GOOD.items() if k != rel}), f"missing required file: {rel}")
        self.has(self.problems({k: v for k, v in GOOD.items() if k != "code/tests/test_x.py"}), "no tests")

    def test_nested_archives_are_found_by_name_and_by_content(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as inner:
            inner.writestr("a", "b")
        self.has(self.problems({**GOOD, "code/data/blob.dat": buf.getvalue()}), "nested zip archive")
        self.has(self.problems({**GOOD, "code/lib.zip": b"x"}), "nested archive")
        tbuf = io.BytesIO()
        with tarfile.open(fileobj=tbuf, mode="w") as t:
            info = tarfile.TarInfo("f")
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
        self.has(self.problems({**GOOD, "code/data/blob2.dat": tbuf.getvalue()}), "nested tar archive")
        self.has(self.problems({**GOOD, "code/data/g.dat": b"\x1f\x8b\x08\x00rest"}), "nested gzip archive")

    def test_dataset_and_cache_and_stray_files_are_rejected(self):
        self.has(self.problems({**GOOD, "dataset/deletion_requests.csv": "a"}), "unexpected top-level entry")
        self.has(self.problems({**GOOD, "code/dataset/user_identities.csv": "a"}), "dataset file name")
        self.has(self.problems({**GOOD, "code/ghostaudit/__pycache__/x.pyc": b"x"}), "cache/venv directory")
        self.has(self.problems({**GOOD, "code/.venv/bin/python": "x"}), "cache/venv directory")
        self.has(self.problems({**GOOD, "notes.txt": "x"}), "unexpected top-level entry")
        self.has(self.problems({**GOOD, "code/docs/m4_sample_report.md": "x"}), "per-case dump")

    def test_unsafe_entry_names(self):
        for name in ("code" + chr(92) + "evil.py", "../escape.py", "code/../../escape.py", "/abs.py", "C:/abs.py"):
            self.has(self.raw_name_zip(name), "unsafe entry name")

    def test_credentials_and_local_paths_are_found(self):
        bs = chr(92)
        self.has(self.problems({**GOOD, "code/cfg.py": "KEY = 'sk-" + "a1" * 15 + "'\n"}), "api key")
        self.has(self.problems({**GOOD, "code/cfg2.py": "pass" + "word = '" + "x9" * 8 + "'\n"}), "hard-coded credential")
        self.has(self.problems({**GOOD, "code/k.txt": "-----BEGIN RSA " + "PRIVATE KEY-----\n"}), "private key")
        self.has(self.problems({**GOOD, "code/p.md": "see C:" + bs + "Users" + bs + "someone" + bs + "x\n"}), "local Windows path")
        self.has(self.problems({**GOOD, "code/q.md": "see /ho" + "me/someone/work/x\n"}), "local home path")

    def test_python_that_does_not_compile_is_found(self):
        self.has(self.problems({**GOOD, "code/ghostaudit/bad.py": "def (:\n"}), "does not compile")

    def test_reports_must_be_real_and_cover_their_topics(self):
        self.has(self.problems({**GOOD, "code/evaluation/evaluation_report.md": "# short\n"}), "too short")
        self.has(self.problems({**GOOD, "code/evaluation/evaluation_report.md": "x" * 900}), "required topic missing: 'status accuracy'")
        self.has(self.problems({**GOOD, "code/evaluation/usage_report.md": "y" * 900}), "required topic missing: 'input tokens'")

    def test_oversized_file_and_bad_archives(self):
        self.has(self.problems({**GOOD, "code/big.txt": "a" * (package.MAX_FILE_BYTES + 1)}), "bytes (>")
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"this is not a zip")
        self.assertIn("not a valid zip", package.verify(bad)[0][0])
        self.assertIn("archive not found", package.verify(self.tmp / "nope.zip")[0][0])

    def test_duplicate_entry_names(self):
        p = self.tmp / "dup.zip"
        with zipfile.ZipFile(p, "w") as z:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for name, body in GOOD.items():
                    z.writestr(name, body)
                z.writestr("README.md", "again")
        self.has(package.verify(p)[0], "duplicate entry names")

    def fake_dataset(self):
        ds = self.tmp / "dataset"
        ds.mkdir()
        rows = ["user_id,name,email,phone,email_hash,updated_at", "u1,Quentin Fictional,someone@example.test,+99 1 555 0100," + "ab" * 32 + ",t", "u2,Al,short@example.test,,,t"]
        (ds / "user_identities.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        audit = ["audit_id,user_id,event_time,field_name,old_value,new_value", "a1,u1,t,email,old.address@example.test,someone@example.test"]
        (ds / "user_audit_log.csv").write_text("\n".join(audit) + "\n", encoding="utf-8")
        return ds

    def test_unexpected_dataset_identifiers_are_problems_of_every_kind(self):
        ds = self.fake_dataset()
        for body, kind in (("contact someone@example.test", "e-mail"), ("was old.address@example.test once", "e-mail"), ("Quentin Fictional wrote", "name"),
                           ("call +99 1 555 0100", "phone"), ("hash " + "AB" * 32, "e-mail hash")):
            problems, _ = package.verify(make_zip(self.tmp / "n.zip", {**GOOD, "code/docs/x.md": body}), ds)
            self.has(problems, "code/docs/x.md: contains dataset identifiers")
            self.has(problems, kind)

    def test_short_names_and_clean_files_are_not_flagged(self):
        problems, notes = package.verify(make_zip(self.tmp / "n.zip", {**GOOD, "code/docs/x.md": "Al said hello"}), self.fake_dataset())
        self.assertEqual(problems, [])
        self.assertTrue(any("none found" in n for n in notes), notes)

    def test_intentional_files_are_notes_with_their_reason_not_problems(self):
        ds = self.fake_dataset()
        files = {**GOOD, "code/tests/test_linker.py": "x = 'someone@example.test'\n"}
        problems, notes = package.verify(make_zip(self.tmp / "n.zip", files), ds)
        self.assertEqual(problems, [])
        self.assertTrue(any("test_linker.py" in n and "intentional" in n and "real dataset" in n for n in notes), notes)

    def test_without_a_dataset_no_identifier_check_runs(self):
        self.assertEqual(self.problems({**GOOD, "code/docs/x.md": "contact someone@example.test"}), [])


class RealTree(unittest.TestCase):
    """The actual repository, packaged and then inspected from the written archive."""

    @classmethod
    def setUpClass(cls):
        cls._t = tempfile.TemporaryDirectory()
        cls.zip = Path(cls._t.name) / "code.zip"
        cls.count, cls.skipped = package.build(CODE_DIR.parent, cls.zip)

    @classmethod
    def tearDownClass(cls):
        cls._t.cleanup()

    def test_the_real_archive_verifies_clean(self):
        problems, notes = package.verify(self.zip)
        self.assertEqual(problems, [], problems)

    def test_the_real_archive_contains_dataset_identifiers_only_where_intended(self):
        from tests.helpers import DATASET, HAVE_DATASET

        if not HAVE_DATASET:
            self.skipTest("needs the dataset")
        problems, notes = package.verify(self.zip, DATASET)
        self.assertEqual(problems, [], problems)
        self.assertTrue(any("screenshot_ocr.json" in n and "intentional" in n for n in notes), notes)

    def test_required_content_is_present_and_excluded_content_is_absent(self):
        with zipfile.ZipFile(self.zip) as z:
            names = set(z.namelist())
        for r in package.REQUIRED:
            self.assertIn(r, names)
        self.assertTrue({n for n in names if n.startswith("code/tests/test_")})
        self.assertIn("code/ghostaudit/data/screenshot_ocr.json", names)  # needed at run time
        self.assertIn("code/requirements.txt", names)
        for n in names:
            self.assertTrue(n == "README.md" or n.startswith("code/"), n)
            self.assertNotIn("__pycache__", n)
            self.assertFalse(n.endswith((".pyc", ".csv", ".zip", ".log")), n)
            self.assertNotIn(n.split("/")[-1], {"output.csv", "log.txt", ".env"})
        self.assertFalse(any(n.startswith("code/docs/") and n.endswith("_sample_report.md") for n in names))

    def test_the_archive_is_small_and_reproducible(self):
        self.assertLess(self.zip.stat().st_size, 2 * 1024 * 1024)
        again = self.zip.with_name("again.zip")
        package.build(CODE_DIR.parent, again)
        self.assertEqual(self.zip.read_bytes(), again.read_bytes())


if __name__ == "__main__":
    unittest.main()
