"""The shipped OCR cache keeps only the dataset identifiers the pipeline needs, and scrubbing the rest changes nothing else."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import CODE_DIR, DATASET, HAVE_DATASET, needs_dataset, real_dataset

from ghostaudit.screenshots import CACHE_PATH, canon, load_cache, verify_cache
from tools import scrub_ocr_cache as T

VALS = {"lena.fictional@example.test": "email", "lena fictional": "name", "+99 1 555 0100": "phone", "keep.me@example.test": "email", "kept person": "name"}
KEEP = ("keep.me@example.test", "Kept Person")


def cache(*texts):
    return {"engine": "e", "tool": "t", "images": {"evidence_1": {"path": "p", "sha256": "ab" * 32, "lines": [{"text": t, "x": 10 * i, "y": 20 * i} for i, t in enumerate(texts)]}}}


def texts(d):
    return [line["text"] for line in d["images"]["evidence_1"]["lines"]]


class Scrub(unittest.TestCase):
    ORIGINAL = cache("Customer Operations", "Lena Fictional", "p email:lena.fictional@example.test", "lena.fictional@exampIe.test", "+99 1 555 0100", "keep.me@example.test", "Kept Person",
                     "support_1034 order 1842", "Al Short")

    def run_scrub(self, d=None, keep=KEEP):
        return T.scrub(d if d is not None else self.ORIGINAL, VALS, keep)

    def test_unneeded_values_are_replaced_and_needed_ones_stay_verbatim(self):
        new, log = self.run_scrub()
        t = texts(new)
        self.assertEqual(t[0], "Customer Operations")
        self.assertNotIn("Lena Fictional", t)
        self.assertTrue(t[2].startswith("p email:") and "lena.fictional" not in t[2])
        self.assertEqual((t[5], t[6]), ("keep.me@example.test", "Kept Person"))
        self.assertEqual(t[7], "support_1034 order 1842")  # ids, order numbers and other text are untouched
        self.assertEqual(sorted(k for k, _, _ in log), ["email", "name", "phone"])

    def test_length_shape_coordinates_and_checksums_are_preserved(self):
        new, _ = self.run_scrub()
        for a, b in zip(self.ORIGINAL["images"]["evidence_1"]["lines"], new["images"]["evidence_1"]["lines"]):
            self.assertEqual(len(a["text"]), len(b["text"]))
            self.assertEqual((a["x"], a["y"]), (b["x"], b["y"]))
        self.assertEqual(new["images"]["evidence_1"]["sha256"], "ab" * 32)
        self.assertIn("@", texts(new)[2])  # an e-mail stays an e-mail, so "another address on screen" still counts as one
        self.assertTrue(texts(new)[4].startswith("+") and texts(new)[4].count(" ") == 3)  # a phone keeps its separators

    def test_one_value_gets_one_placeholder_even_when_the_ocr_garbled_it(self):
        new, log = self.run_scrub()
        t = texts(new)
        self.assertEqual(t[2].split(":")[1], t[3])  # "exampIe" (capital I) is the same address as "example"
        self.assertEqual(sum(1 for k, v, _ in log if v == "lena.fictional@example.test"), 1)

    def test_idempotent_deterministic_and_does_not_touch_its_input(self):
        before = copy.deepcopy(self.ORIGINAL)
        once, _ = self.run_scrub()
        self.assertEqual(self.ORIGINAL, before)
        twice, log2 = T.scrub(once, VALS, KEEP)
        self.assertEqual(once, twice)
        self.assertEqual(log2, [])
        self.assertEqual(self.run_scrub()[0], once)

    def test_remaining_reports_exactly_what_is_left(self):
        left = T.remaining(self.ORIGINAL, VALS, KEEP)
        self.assertEqual({(k, v) for k, v, _ in left}, {("name", "lena fictional"), ("email", "lena.fictional@example.test"), ("phone", "+99 1 555 0100")})
        self.assertEqual(T.remaining(self.run_scrub()[0], VALS, KEEP), [])

    def test_short_names_are_not_treated_as_identifiers(self):
        self.assertEqual(texts(self.run_scrub()[0])[-1], "Al Short")

    def test_keep_list_is_case_insensitive(self):
        new, _ = self.run_scrub(keep=("KEEP.ME@EXAMPLE.TEST", "kept person"))
        self.assertEqual((texts(new)[5], texts(new)[6]), ("keep.me@example.test", "Kept Person"))

    def test_placeholders_never_collide_with_each_other(self):
        d = cache("one.person@example.test", "two.person@example.test", "Three Persons", "Four Persons")
        vals = {"one.person@example.test": "email", "two.person@example.test": "email", "three persons": "name", "four persons": "name"}
        new, _ = T.scrub(d, vals, ())
        t = texts(new)
        self.assertNotEqual(t[0], t[1])
        self.assertTrue(all(len(a) == len(b) for a, b in zip(texts(d), t)))


class ShippedCache(unittest.TestCase):
    def test_the_keep_list_values_are_really_in_the_cache(self):
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        allcanon = canon(" ".join(line["text"] for img in data["images"].values() for line in img["lines"]))
        for v in T.KEEP:
            self.assertIn(canon(v), allcanon, v)  # a stale keep-list entry would hide that the justification no longer holds

    def test_cache_format_is_stable(self):
        raw = CACHE_PATH.read_text(encoding="utf-8")
        self.assertEqual(json.dumps(json.loads(raw), indent=1, ensure_ascii=False), raw.rstrip("\n"))
        self.assertEqual(len(json.loads(raw)["images"]), 11)

    @needs_dataset
    def test_no_unneeded_dataset_identifier_is_left(self):
        vals = T.dataset_values(DATASET)
        self.assertGreater(len(vals), 1000)
        self.assertEqual(T.remaining(json.loads(CACHE_PATH.read_text(encoding="utf-8")), vals), [])

    @needs_dataset
    def test_rewriting_the_shipped_cache_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            copy_ = Path(d) / "c.json"
            shutil.copy(CACHE_PATH, copy_)
            r = subprocess.run([sys.executable, str(CODE_DIR / "tools" / "scrub_ocr_cache.py"), "--dataset", str(DATASET), "--cache", str(copy_), "--write"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("0 identifier(s) replaced", r.stdout)
            self.assertEqual(copy_.read_bytes(), CACHE_PATH.read_bytes())
            self.assertEqual(subprocess.run([sys.executable, str(CODE_DIR / "tools" / "scrub_ocr_cache.py"), "--dataset", str(DATASET), "--check"], capture_output=True).returncode, 0)

    @needs_dataset
    def test_every_cached_entry_still_matches_its_image_bytes(self):
        self.assertEqual(verify_cache(load_cache(), DATASET), [])

    @needs_dataset
    def test_screenshot_dependent_outcomes_are_unchanged(self):
        from ghostaudit.classification import classify_all
        from ghostaudit.reconstruction import CaseReconstructor

        ds = real_dataset()
        v = classify_all(CaseReconstructor(ds), ds.requests.rows)
        supported = sorted((c for c, x in v.items() if x.status == "INCOMPLETE" and x.determination == "SUPPORTED"), key=lambda c: int(c.split("_")[1]))
        self.assertEqual(supported, ["case_33", "case_34", "case_35", "case_39", "case_50", "case_52", "case_53", "case_61"])
        for c, status in (("case_39", "INCOMPLETE"), ("case_52", "INCOMPLETE"), ("case_53", "INCOMPLETE"), ("case_55", "COMPLETE")):
            self.assertEqual(v[c].status, status, c)


if __name__ == "__main__":
    unittest.main()
