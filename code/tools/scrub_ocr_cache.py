"""Replace dataset identifiers in the shipped OCR cache that the pipeline does not need, with same-shape synthetic values.

    python code/tools/scrub_ocr_cache.py --dataset dataset --check     # exit 1 if an unneeded dataset identifier is still in the cache
    python code/tools/scrub_ocr_cache.py --dataset dataset --write     # rewrite the cache in place (idempotent)

Why: ``ghostaudit/data/screenshot_ocr.json`` is runtime data (the text of the 11 supplied screenshots, so no OCR is needed to run),
but most of the personal-looking values in it (names, e-mail addresses, one phone number of fictional people) are never used. An
experiment (scrub each value, rerun all 550 cases, compare statuses, determinations, violations, proofs, gaps and steps) showed that
only the ``KEEP`` values below change any result; they are matched against the case's catalog, so they must stay verbatim.

What it does not touch: image checksums, coordinates, resource ids, ticket and order numbers, and all other text. Replacements keep
the original length and shape (an e-mail stays an e-mail, a name stays a name) and are deterministic: the same original value always
gets the same placeholder, whichever OCR variant of it appears (``gmaiI`` and ``gmail`` are one value).

The original, verbatim cache can be rebuilt with ``code/tools/build_ocr_cache.py`` (Windows OCR) or restored from version control.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from ghostaudit.screenshots import canon  # noqa: E402  (the same glyph-tolerant form the pipeline matches with)

CACHE = CODE / "ghostaudit" / "data" / "screenshot_ocr.json"
# Verbatim because the pipeline matches them against the catalog; scrubbing any one of them changes a result (case_39, case_52, case_53, case_55).
# Everything else is replaceable. Re-check with a full-pipeline comparison whenever this list or the pipeline changes.
KEEP = ("Kofi Agyeman", "areed1433@gmail.com", "ingrid.dahl@outlook.com", "jain.aditi@gmail.com")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
MIN_NAME = 7  # shorter names are too generic to identify anyone and are left alone


def dataset_values(dataset: Path) -> Dict[str, str]:
    """value -> kind, for every e-mail, name and phone number the dataset assigns to a user (current or historical)."""
    vals: Dict[str, str] = {}
    with open(dataset / "user_identities.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            vals[r["email"].lower()] = "email"
            if len(r["name"]) >= MIN_NAME:
                vals[r["name"]] = "name"
            if r["phone"]:
                vals[r["phone"]] = "phone"
    with open(dataset / "user_audit_log.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            for v in (r["old_value"], r["new_value"]):
                if "@" in v:
                    vals[v.lower()] = "email"
                elif r["field_name"] == "phone" and v:
                    vals[v] = "phone"
                elif r["field_name"] == "name" and len(v) >= MIN_NAME:
                    vals[v] = "name"
    return vals


def _fit(template: str, length: int, pad: str = "x") -> str:
    return template[:length] if len(template) >= length else template + pad * (length - len(template))


def placeholder(kind: str, n: int, length: int) -> str:
    if kind == "email":
        local, domain = f"anon{n}", "@example.test"
        room = length - len(domain)
        return (_fit(local, room) if room >= 1 else _fit(f"a{n}@x.test", length)) + (domain if room >= 1 else "")
    if kind == "name":
        return _fit("Sample Person", length, pad="x")
    return re.sub(r"\d", "0", _fit("+00 00 0000 0000", length, pad="0"))  # phone: keep separators, zero the digits


def scrub(data: dict, vals: Dict[str, str], keep: Sequence[str] = KEEP) -> Tuple[dict, List[Tuple[str, str, str]]]:
    """Return (scrubbed copy, [(kind, original, placeholder)]). Never modifies ``data``."""
    out = json.loads(json.dumps(data))
    kept = {k.lower() for k in keep}
    todo = {v: k for v, k in vals.items() if v.lower() not in kept}
    cv = {v: canon(v) for v in todo}
    mapping: Dict[str, str] = {}
    log: List[Tuple[str, str, str]] = []
    numbering = {v: i for i, v in enumerate(sorted(todo), start=1)}

    def repl(v: str, original: str) -> str:
        if v not in mapping:
            mapping[v] = placeholder(todo[v], numbering[v], len(original))
            log.append((todo[v], v, mapping[v]))
        p = mapping[v]
        return p if len(p) == len(original) else placeholder(todo[v], numbering[v], len(original))

    by_length = sorted(todo, key=len, reverse=True)  # longer values first, so a value that contains another is replaced whole
    for _, img in sorted(out["images"].items()):
        for line in img["lines"]:
            text = line["text"]
            low, ct = text.lower(), canon(text)
            for v in (v for v in by_length if v.lower() in low or (cv[v] and cv[v] in ct)):  # cheap pre-filter; the regexes run only on real candidates
                if todo[v] == "email":
                    text = EMAIL.sub(lambda m, v=v: repl(v, m.group(0)) if cv[v] and cv[v] in canon(m.group(0)) else m.group(0), text)
                else:
                    text = re.sub(re.escape(v), lambda m, v=v: repl(v, m.group(0)), text, flags=re.I)
            line["text"] = text
    return out, log


def remaining(data: dict, vals: Dict[str, str], keep: Sequence[str] = KEEP) -> List[Tuple[str, str, str]]:
    """(kind, value, "image:line") for every dataset identifier still visible in the cache that is not in ``keep``."""
    kept = {k.lower() for k in keep}
    todo = sorted((v, k, canon(v)) for v, k in vals.items() if v.lower() not in kept)  # canonical forms computed once, not per line
    found = []
    for fid, img in sorted(data["images"].items()):
        for i, line in enumerate(img["lines"]):
            low, ct = line["text"].lower(), canon(line["text"])
            for v, k, cv in todo:
                if v.lower() in low or (cv and cv in ct):
                    found.append((k, v, f"{fid}:{i}"))
    return found


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=CACHE)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    vals = dataset_values(a.dataset)
    data = json.loads(a.cache.read_text(encoding="utf-8"))
    if a.check:
        left = remaining(data, vals)
        for k, v, where in left:
            print(f"unneeded dataset {k} still in cache: {v!r} at {where}")
        print(f"{'OK' if not left else 'FAILED'}: {len(left)} unneeded dataset identifier occurrence(s); kept verbatim by design: {', '.join(KEEP)}")
        return 0 if not left else 1
    new, log = scrub(data, vals)
    trailing = "\n" if a.cache.read_bytes().endswith(b"\n") else ""  # keep the file's existing format so a diff shows only the replaced values
    a.cache.write_text(json.dumps(new, ensure_ascii=False, indent=1) + trailing, encoding="utf-8", newline="\n")
    for k, v, p in log:
        print(f"replaced {k}: {v!r} -> {p!r}")
    print(f"{len(log)} identifier(s) replaced in {a.cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
