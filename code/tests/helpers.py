"""Shared test helpers. Tests that need the real dataset skip cleanly when it is absent
(``dataset/`` is deliberately not shipped inside code.zip)."""

import sys
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

DATASET = CODE_DIR.parent / "dataset"
HAVE_DATASET = (DATASET / "evidence_files.csv").exists()

needs_dataset = unittest.skipUnless(HAVE_DATASET, "dataset/ not present")

_CACHE = {}


def real_dataset():
    from ghostaudit.dataset import load_dataset

    if "ds" not in _CACHE:
        _CACHE["ds"] = load_dataset(DATASET)
    return _CACHE["ds"]


def real_results():
    from ghostaudit.parsers import parse_all

    if "res" not in _CACHE:
        _CACHE["res"] = {r.file_id: r for r in parse_all(real_dataset())}
    return _CACHE["res"]


def crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")
