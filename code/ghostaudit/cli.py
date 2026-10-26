"""Command line entry points for the M1 foundation.

    cd code
    python -m ghostaudit inventory [--dataset ../dataset] [--json]
    python -m ghostaudit validate-samples [--dataset ../dataset]

Nothing here writes to ``dataset/`` or produces ``output.csv``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .dataset import load_dataset
from .inventory import build_inventory, format_inventory, inventory_json
from .parsers import parse_all
from .schema import validate_output_rows


def _utf8_stdout() -> None:
    # Windows consoles default to cp1252; evidence/request text is multilingual.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def default_dataset_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "dataset"


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="ghostaudit")
    ap.add_argument("--dataset", type=Path, default=default_dataset_dir(), help="path to the dataset/ directory (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_inv = sub.add_parser("inventory", help="parse every evidence file and report counts/failures")
    p_inv.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub.add_parser("validate-samples", help="validate sample_output.csv against the output contract")
    args = ap.parse_args(argv)

    ds = load_dataset(args.dataset)
    if args.cmd == "inventory":
        inv = build_inventory(ds, parse_all(ds))
        print(inventory_json(inv) if args.json else format_inventory(inv))
        return 0 if not inv["failures"] and not inv["dataset_issues"] else 1
    if args.cmd == "validate-samples":
        ids = [r.case_id for r in ds.sample_requests.rows]
        ev_ids = [f.file_id for f in ds.evidence_files.rows] + [a.activity_id for a in ds.activity.rows] + [a.audit_id for a in ds.audit_log.rows]
        from .schema import parse_output_csv
        rows = parse_output_csv((args.dataset / "sample_output.csv").read_text(encoding="utf-8-sig"))
        viol = validate_output_rows(rows, ids, ev_ids)
        for v in viol:
            print(v)
        print(f"{len(rows)} sample rows, {len(viol)} violations")
        return 0 if not viol else 1
    return 2
