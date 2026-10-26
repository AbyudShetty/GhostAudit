"""Build and verify ``code.zip`` (stdlib only, deterministic, forward-slash entry names on every platform).

    python code/tools/package.py                       # build <repo>/code.zip, then verify the written archive
    python code/tools/package.py --out some/dir/x.zip  # build elsewhere
    python code/tools/package.py --verify code.zip     # verify an existing archive only
    python code/tools/package.py --verify code.zip --dataset dataset   # also fail on dataset identifiers in unexpected files

Layout: ``README.md``, ``code/``, ``docs/`` and ``evaluation/`` at the archive root, with the required reports at
``evaluation/``. Nothing from ``dataset/`` or ``local/`` is included. Verification reads the archive back, not the working tree.
Exit code 0 = built/verified, 1 = problems found (listed), 2 = bad usage.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]

REQUIRED = ("README.md", "code/main.py", "evaluation/evaluation_report.md", "evaluation/usage_report.md", "code/ghostaudit/__init__.py", "code/ghostaudit/cli.py")
SOURCE_DIRS = ("code", "docs", "evaluation")
EXCLUDED_DIRS = {"__pycache__", ".venv", "venv", "env", "node_modules", ".git", ".pytest_cache", ".mypy_cache", ".idea", ".vscode", ".ruff_cache"}
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db", "output.csv", "log.txt"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".part", ".log"}
ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".tbz2", ".jar", ".whl"}
# Generated per-case reports quote sample records and sample answers one-for-one; they stay in the repository, not in the archive.
EXCLUDED_FILES = {
    "code/docs/m2_sample_link_report.md": "per-case dump of sample requests",
    "code/docs/m3_sample_report.md": "per-case dump of sample requests",
    "code/docs/m4_sample_report.md": "per-case dump of sample requests",
    "code/docs/m5_sample_report.md": "per-case dump of sample requests and sample answers",
    "code/evaluation/m5_case_report.md": "generated per-case report",
}
DATASET_FILENAMES = {"deletion_requests.csv", "sample_deletion_requests.csv", "sample_output.csv", "user_identities.csv", "user_audit_log.csv",
                     "system_activity.csv", "deletion_rules.csv", "evidence_files.csv", "output.csv"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
FIXED_TIME = (2026, 10, 1, 0, 0, 0)  # constant, so rebuilding unchanged sources gives a byte-identical archive

SECRET_PATTERNS = [
    ("api key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hard-coded credential", re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
]
LOCAL_PATH_PATTERNS = [
    ("local Windows path", re.compile(r"[A-Za-z]:\\(?:Users|ABYUD)\\", re.I)),
    ("local home path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
]
REPORT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "evaluation/evaluation_report.md": ("status accuracy", "step-level", "field-level", "sequence", "additional evaluation case", "failures found", "improvements"),
    "evaluation/usage_report.md": ("provider", "model calls", "input tokens", "output tokens", "per request", "cost"),
}
MAGIC = (("zip", b"PK\x03\x04"), ("zip", b"PK\x05\x06"), ("gzip", b"\x1f\x8b"), ("7z", b"7z\xbc\xaf\x27\x1c"), ("rar", b"Rar!"), ("bzip2", b"BZh"), ("xz", b"\xfd7zXZ\x00"))


def is_excluded(rel: PurePosixPath) -> Optional[str]:
    """Why a repository-relative path is kept out of the archive (None = include)."""
    parts = rel.parts
    if parts and parts[0] == "local":
        return "local working files"
    if parts and parts[0] == "dataset":
        return "dataset"
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return "cache/venv directory"
    name = rel.name
    if name == ".env" or name.startswith(".env.") or rel.suffix.lower() in {".pem", ".key"}:
        return "credentials/local configuration"
    if name in EXCLUDED_NAMES:
        return "excluded file name"
    if rel.suffix.lower() in EXCLUDED_SUFFIXES:
        return "cache/temporary file"
    if rel.suffix.lower() in ARCHIVE_SUFFIXES or name.lower().endswith((".tar.gz", ".tar.bz2")):
        return "nested archive"
    if str(rel) in EXCLUDED_FILES:
        return EXCLUDED_FILES[str(rel)]
    if name in DATASET_FILENAMES:
        return "dataset file name"
    return None


def collect(root: Path) -> Tuple[List[Tuple[str, Path]], List[Tuple[str, str]]]:
    """(arcname, source) pairs to include, and (path, reason) pairs that were skipped, both sorted."""
    include, skipped = [], []
    candidates = [root / "README.md"]
    for directory in SOURCE_DIRS:
        candidates.extend(sorted(p for p in (root / directory).rglob("*") if p.is_file()))
    for p in candidates:
        if not p.is_file():
            continue
        rel = PurePosixPath(p.relative_to(root).as_posix())
        why = is_excluded(rel)
        (skipped if why else include).append((str(rel), why) if why else (str(rel), p))
    return sorted(include, key=lambda t: t[0]), sorted(skipped)


def build(root: Path, out: Path) -> Tuple[int, List[Tuple[str, str]]]:
    include, skipped = collect(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc, src in include:
            info = zipfile.ZipInfo(arc, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, Path(src).read_bytes())
    tmp.replace(out)
    return len(include), skipped


def _magic(data: bytes) -> Optional[str]:
    for label, sig in MAGIC:
        if data.startswith(sig):
            return label
    return "tar" if data[257:262] == b"ustar" else None


def verify(path: Path, dataset: Optional[Path] = None) -> Tuple[List[str], List[str]]:
    """Inspect the archive itself. Returns (problems, notes); problems must be empty for a valid submission."""
    problems: List[str] = []
    notes: List[str] = []
    if not path.is_file():
        return [f"archive not found: {path}"], notes
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        problems.append(f"archive is {size} bytes (> {MAX_ARCHIVE_BYTES})")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        return [f"not a valid zip: {e}"], notes
    with z:
        bad = z.testzip()
        if bad:
            problems.append(f"corrupt member: {bad}")
        infos = [i for i in z.infolist() if not i.is_dir()]
        names = [i.orig_filename for i in infos]
        if len(set(names)) != len(names):
            problems.append("duplicate entry names")
        for n in names:
            if "\\" in n or n.startswith("/") or ".." in PurePosixPath(n).parts or re.match(r"^[A-Za-z]:", n):
                problems.append(f"unsafe entry name: {n!r}")
            elif n != "README.md" and PurePosixPath(n).parts[0] not in SOURCE_DIRS:
                problems.append(f"unexpected top-level entry (allowed: README.md, code/, docs/, evaluation/): {n}")
            why = is_excluded(PurePosixPath(n))
            if why:
                problems.append(f"{n}: must not be in the archive ({why})")
        for r in REQUIRED:
            if r not in names:
                problems.append(f"missing required file: {r}")
        if not any(n.startswith("code/tests/test_") for n in names):
            problems.append("no tests in the archive")
        ids = {}
        for info in infos:
            data = z.read(info)
            if info.file_size > MAX_FILE_BYTES:
                problems.append(f"{info.orig_filename}: {info.file_size} bytes (> {MAX_FILE_BYTES})")
            kind = _magic(data)
            if kind:
                problems.append(f"{info.orig_filename}: contains a nested {kind} archive")
            if b"\x00" in data[:2048]:
                continue  # binary but not an archive: nothing to scan as text
            text = data.decode("utf-8", errors="replace")
            for label, rx in SECRET_PATTERNS:
                m = rx.search(text)
                if m:
                    problems.append(f"{info.orig_filename}: looks like a {label}")
            for label, rx in LOCAL_PATH_PATTERNS:
                m = rx.search(text)
                if m:
                    problems.append(f"{info.orig_filename}: contains a {label} ({m.group(0)!r})")
            if info.orig_filename.endswith(".py"):
                try:
                    compile(text, info.orig_filename, "exec")
                except SyntaxError as e:
                    problems.append(f"{info.orig_filename}: does not compile ({e.msg}, line {e.lineno})")
            if info.orig_filename in REPORT_KEYWORDS:
                low = text.lower()
                if len(text.strip()) < 500:
                    problems.append(f"{info.orig_filename}: too short to be a real report")
                for kw in REPORT_KEYWORDS[info.orig_filename]:
                    if kw not in low:
                        problems.append(f"{info.orig_filename}: required topic missing: {kw!r}")
            if dataset is not None:
                ids[info.orig_filename] = text
        if dataset is not None:
            n2, p2 = _dataset_identifier_report(ids, dataset)
            notes.extend(n2)
            problems.extend(p2)
        notes.insert(0, f"{len(names)} files, {size} bytes")
    return problems, notes


# Shipped files that may legitimately contain values taken from the dataset, and why. Anything else that does is reported as a problem.
INTENTIONAL_DATASET_IDENTIFIERS = {
    "code/ghostaudit/data/screenshot_ocr.json": "runtime data: OCR text of the supplied screenshots; only values the pipeline matches stay verbatim (tools/scrub_ocr_cache.py --check)",
    "code/tools/scrub_ocr_cache.py": "tool: its KEEP list names the few cache values the pipeline needs verbatim",
    "code/tests/test_real_dataset.py": "test only: asserts facts extracted from the real evidence; skips without the dataset",
    "code/tests/test_linker.py": "test only: near-miss decoy addresses that exist in the real dataset; skips without the dataset",
    "code/tests/test_identity.py": "test only: name collisions in the real dataset; skips without the dataset",
    "code/tests/test_parser_txt_pdf_image.py": "test only: contact details expected in the real PDFs; skips without the dataset",
}
MIN_NAME_LENGTH = 7  # shorter names are too generic to mean anything


def dataset_identifiers(dataset: Path) -> Dict[str, str]:
    """lower-cased value -> kind, for every e-mail, name, phone number and e-mail hash the dataset assigns to a user (current or historical)."""
    import csv

    vals: Dict[str, str] = {}
    f = dataset / "user_identities.csv"
    if f.is_file():
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                for key, kind in (("email", "e-mail"), ("phone", "phone"), ("email_hash", "e-mail hash")):
                    if r.get(key):
                        vals[r[key].lower()] = kind
                if len(r.get("name") or "") >= MIN_NAME_LENGTH:
                    vals[r["name"].lower()] = "name"
    g = dataset / "user_audit_log.csv"
    if g.is_file():
        with open(g, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                for v in (r.get("old_value") or "", r.get("new_value") or ""):
                    if "@" in v:
                        vals[v.lower()] = "e-mail"
                    elif r.get("field_name") == "phone" and v:
                        vals[v.lower()] = "phone"
                    elif r.get("field_name") == "name" and len(v) >= MIN_NAME_LENGTH:
                        vals[v.lower()] = "name"
    return vals


def _dataset_identifier_report(texts: Dict[str, str], dataset: Path) -> Tuple[List[str], List[str]]:
    """(notes, problems): which shipped files contain dataset identifiers; intentional ones are notes, unexpected ones are problems."""
    from collections import Counter

    vals = dataset_identifiers(dataset)
    if not vals:
        return [], []
    notes, problems = [], []
    for name, text in sorted(texts.items()):
        low = text.lower()
        kinds = Counter(k for v, k in vals.items() if v in low)
        if not kinds:
            continue
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        if name in INTENTIONAL_DATASET_IDENTIFIERS:
            notes.append(f"dataset identifiers in {name}: {summary} (intentional: {INTENTIONAL_DATASET_IDENTIFIERS[name]})")
        else:
            problems.append(f"{name}: contains dataset identifiers ({summary}); replace them with synthetic values or justify the file in INTENTIONAL_DATASET_IDENTIFIERS")
    if not notes and not problems:
        notes.append("dataset identifiers: none found in shipped files")
    return notes, problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build and verify code.zip")
    ap.add_argument("--root", type=Path, default=REPO, help="repository root (default: the one containing this file)")
    ap.add_argument("--out", type=Path, default=None, help="archive to write (default: <root>/code.zip)")
    ap.add_argument("--verify", type=Path, default=None, help="only verify this archive")
    ap.add_argument("--dataset", type=Path, default=None, help="optional: also check that no shipped file contains dataset e-mails, names, phones or hashes beyond the intentional ones")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    target = args.verify
    if target is None:
        out = (args.out or args.root / "code.zip").resolve()
        if args.root.resolve() in out.parents and out.relative_to(args.root.resolve()).parts[0] in SOURCE_DIRS:
            print("refusing to write the archive inside code/, docs/ or evaluation/ (it would be packaged into itself)", file=sys.stderr)
            return 2
        count, skipped = build(args.root, out)
        from collections import Counter

        by_reason = Counter(why for _, why in skipped)
        print(f"built {out} with {count} files; skipped {len(skipped)} ({', '.join(f'{n} {w}' for w, n in sorted(by_reason.items()))})")
        for p, why in skipped:
            if why != "cache/venv directory":
                print(f"  - {p}  ({why})")
        target = out
    problems, notes = verify(target, args.dataset)
    for n in notes:
        print("note:", n)
    for p in problems:
        print("PROBLEM:", p)
    print(f"{'OK' if not problems else 'FAILED'}: {len(problems)} problem(s) in {target}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
