"""Command line entry points.

Run it from the repository root or from ``code/``; ``--dataset`` defaults to the repository's ``dataset/`` folder wherever you are.
A relative ``--dataset`` or ``--out`` path is resolved against the *current directory*, so from ``code/`` the dataset is ``../dataset``.

    python code/main.py inventory [--json]                         (from the repository root)
    python code/main.py validate-samples
    python code/main.py predict --samples --out some/dir/pred.csv
    python -m ghostaudit predict --samples --out pred.csv           (from code/)

Nothing here writes to ``dataset/`` or produces the repository-root ``output.csv``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .dataset import DatasetNotFound, load_dataset
from .inventory import build_inventory, format_inventory, inventory_json
from .parsers import parse_all
from .schema import validate_output_rows


def _utf8_stdout() -> None:
    # Windows consoles default to cp1252; evidence/request text is multilingual.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_dataset_dir() -> Path:
    return repo_root() / "dataset"


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="ghostaudit")
    ap.add_argument("--dataset", type=Path, default=default_dataset_dir(), help="path to the dataset/ directory (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_inv = sub.add_parser("inventory", help="parse every evidence file and report counts/failures")
    p_inv.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub.add_parser("validate-samples", help="validate sample_output.csv against the output contract")
    p_pred = sub.add_parser("predict", help="classify every request in deletion_requests.csv and write a prediction CSV (never the repository-root output.csv yet)")
    p_pred.add_argument("--out", type=Path, required=True, help="where to write the CSV; the repository-root output.csv is refused until the packaging milestone")
    p_pred.add_argument("--samples", action="store_true", help="predict the 32 sample requests instead of the 518 evaluation requests")
    p_pred.add_argument("--emit-inferred-proofs", action="store_true", help="also emit COMPLETE proofs that rest on inferred identity / scoped snapshot / OCR text")
    args = ap.parse_args(argv)

    try:
        ds = load_dataset(args.dataset)
    except DatasetNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        hint = repo_root() / args.dataset
        if not args.dataset.is_absolute() and hint.is_dir() and hint.resolve() != args.dataset.resolve():
            print(f"hint: {hint} exists; use --dataset {hint}, run from the repository root, or omit --dataset to use the default", file=sys.stderr)
        return 2
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
    if args.cmd == "predict":
        return _predict(ds, args)
    return 2


def _predict(ds, args) -> int:
    from .classification import classify_all
    from .plan import PlanPolicy, plan_all, rows_for, rows_to_csv
    from .reconstruction import CaseReconstructor

    out = args.out.resolve()
    protected = {(repo_root() / "output.csv").resolve(), (args.dataset.resolve().parent / "output.csv").resolve()}
    if out in protected:
        print("refusing to write the repository-root output.csv: producing the submission file is a later, deliberate step", file=sys.stderr)
        return 2
    cases = ds.sample_requests.rows if args.samples else ds.requests.rows
    cr = CaseReconstructor(ds)
    verdicts = classify_all(cr, cases)
    plans = plan_all(verdicts, cr.manifest, PlanPolicy(emit_inferred_proofs=args.emit_inferred_proofs))
    ids = [c.case_id for c in cases]
    rows = rows_for(plans, ids)
    ev = [f.file_id for f in ds.evidence_files.rows] + [a.activity_id for a in ds.activity.rows] + [a.audit_id for a in ds.audit_log.rows]
    viol = validate_output_rows(rows, ids, ev, allow_empty_deletion_steps=True)
    for v in viol:
        print(v, file=sys.stderr)
    if viol:
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rows_to_csv(rows), encoding="utf-8", newline="")
    from collections import Counter

    print(f"wrote {len(rows)} rows to {out}; status counts {dict(Counter(plans[i].status for i in ids))}; "
          f"COMPLETE with no provable step: {sum(1 for i in ids if plans[i].status == 'COMPLETE' and not plans[i].steps)}")
    return 0
