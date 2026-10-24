"""The CLI must work from the repository root and from code/, and fail clearly (not with a traceback) on a bad path."""

import csv
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import CODE_DIR, DATASET, needs_dataset

from ghostaudit.dataset import REQUIRED_FILES, DatasetNotFound, check_dataset_dir, load_dataset
from ghostaudit.schema import parse_output_csv, validate_output_rows

REPO = CODE_DIR.parent


def run(cwd, *args):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # prove the entry points do not rely on the caller's environment
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, env=env)


class DatasetCheck(unittest.TestCase):
    def test_missing_directory_names_the_absolute_path_and_the_rule(self):
        with self.assertRaises(DatasetNotFound) as cm:
            check_dataset_dir("definitely_not_here")
        msg = str(cm.exception)
        self.assertIn(os.path.abspath("definitely_not_here"), msg)
        self.assertIn("current directory", msg)
        self.assertIsInstance(cm.exception, FileNotFoundError)  # existing callers that catch FileNotFoundError still work

    def test_incomplete_directory_lists_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "deletion_requests.csv").write_text("x", encoding="utf-8")
            with self.assertRaises(DatasetNotFound) as cm:
                load_dataset(d)
            self.assertIn("user_identities.csv", str(cm.exception))
            self.assertNotIn("missing: deletion_requests.csv", str(cm.exception))  # the one file that exists is not listed as missing
            self.assertNotIn(", deletion_requests.csv", str(cm.exception))

    def test_required_files_list_matches_what_the_loader_reads(self):
        self.assertEqual(len(REQUIRED_FILES), len(set(REQUIRED_FILES)))
        self.assertIn("sample_output.csv", REQUIRED_FILES)


class BadPaths(unittest.TestCase):
    def test_wrong_relative_dataset_from_code_gives_a_clear_error_and_a_hint(self):
        r = run(CODE_DIR, "-m", "ghostaudit", "--dataset", "dataset", "validate-samples")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("dataset directory not found", r.stderr)
        self.assertIn(str(CODE_DIR / "dataset"), r.stderr)
        self.assertIn("hint:", r.stderr)  # <repo>/dataset exists, so say so

    def test_unknown_directory_has_no_hint_and_no_traceback(self):
        r = run(REPO, str(CODE_DIR / "main.py"), "--dataset", "nowhere", "validate-samples")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("hint:", r.stderr)

    def test_incomplete_dataset_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = run(REPO, str(CODE_DIR / "main.py"), "--dataset", d, "validate-samples")
            self.assertEqual(r.returncode, 2)
            self.assertIn("missing:", r.stderr)


@needs_dataset
class FromBothDirectories(unittest.TestCase):
    def check(self, cwd, args, out):
        r = run(cwd, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = parse_output_csv(out.read_bytes().decode("utf-8"))
        self.assertEqual(len(rows), 32)
        self.assertEqual(list(csv.reader(out.read_text(encoding="utf-8").splitlines()))[0], ["case_id", "deletion_status", "steps_json"])
        self.assertEqual(validate_output_rows(rows, None, None, allow_empty_deletion_steps=True), [])
        return r

    def test_repo_root_with_main_py_default_dataset_and_a_new_output_directory(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "new" / "sub" / "pred.csv"  # parent directories do not exist yet
            self.check(REPO, [str(Path("code") / "main.py"), "predict", "--samples", "--out", str(out)], out)

    def test_repo_root_with_a_relative_dataset(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "pred.csv"
            self.check(REPO, ["code/main.py", "--dataset", "dataset", "predict", "--samples", "--out", str(out)], out)

    def test_code_directory_with_dot_dot_dataset_and_module_entry(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "pred.csv"
            self.check(CODE_DIR, ["-m", "ghostaudit", "--dataset", "../dataset", "predict", "--samples", "--out", str(out)], out)

    def test_same_predictions_whichever_way_it_is_started(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.csv", Path(d) / "b.csv"
            self.assertEqual(run(REPO, "code/main.py", "validate-samples").returncode, 0)
            self.assertEqual(run(CODE_DIR, "main.py", "validate-samples").returncode, 0)
            self.check(REPO, ["code/main.py", "predict", "--samples", "--out", str(a)], a)
            self.check(CODE_DIR, ["-m", "ghostaudit", "predict", "--samples", "--out", str(b)], b)
            self.assertEqual(a.read_bytes(), b.read_bytes())


@needs_dataset
class RootOutputGuard(unittest.TestCase):
    def test_refused_from_either_directory_and_with_a_dataset_copy_elsewhere(self):
        before = (REPO / "output.csv").exists()
        for cwd, target in ((REPO, "output.csv"), (CODE_DIR, "../output.csv"), (REPO, str(REPO / "OUTPUT.CSV" if os.name == "nt" else REPO / "output.csv"))):
            r = run(cwd, "-m" if cwd == CODE_DIR else str(CODE_DIR / "main.py"), *(["ghostaudit"] if cwd == CODE_DIR else []), "predict", "--out", target)
            self.assertEqual(r.returncode, 2, (cwd, target, r.stderr))
            self.assertIn("refusing", r.stderr)
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / "dataset"  # a dataset elsewhere must not make the real root output.csv writable
            copy.mkdir()
            for n in REQUIRED_FILES:
                shutil.copy(DATASET / n, copy / n)
            r = run(REPO, "code/main.py", "--dataset", str(copy), "predict", "--out", str(REPO / "output.csv"))
            self.assertEqual(r.returncode, 2)
            self.assertIn("refusing", r.stderr)
            sibling = Path(d) / "output.csv"  # the output.csv next to a dataset folder is protected too
            r = run(REPO, "code/main.py", "--dataset", str(copy), "predict", "--out", str(sibling))
            self.assertEqual(r.returncode, 2)
            self.assertFalse(sibling.exists())
        self.assertEqual((REPO / "output.csv").exists(), before)


if __name__ == "__main__":
    unittest.main()
