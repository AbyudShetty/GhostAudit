"""Command line entry points.

Run it from the repository root or from ``code/``; ``--dataset`` defaults to the repository's ``dataset/`` folder wherever you are.
A relative ``--dataset`` or ``--out`` path is resolved against the *current directory*, so from ``code/`` the dataset is ``../dataset``.

    python code/main.py                                            produce <root>/output.csv (same as ``submit``)
    python code/main.py submit [--out PATH]                        the 518 evaluation requests -> output.csv
    python code/main.py predict --out PATH [--samples]             same predictions, but never into <root>/output.csv
    python code/main.py inventory [--json] | validate-samples
    python -m ghostaudit predict --samples --out pred.csv           (from code/)

``<root>`` is the directory that contains ``code/`` (the repository, or the folder a code.zip was extracted into).
Only ``submit`` writes ``<root>/output.csv``; it never writes into ``dataset/``.
"""

from __future__ import annotations

import argparse
import json
import os
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
    sub = ap.add_subparsers(dest="cmd", required=False)
    ap.set_defaults(cmd="submit", out=None, samples=False, emit_inferred_proofs=False, emit=[])  # a bare invocation produces the submission file
    p_inv = sub.add_parser("inventory", help="parse every evidence file and report counts/failures")
    p_inv.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub.add_parser("validate-samples", help="validate sample_output.csv against the output contract")
    p_sub = sub.add_parser("submit", help="classify every request in deletion_requests.csv and write <root>/output.csv (or --out)")
    p_sub.add_argument("--out", type=Path, default=None, help="default: output.csv in the directory that contains code/")
    p_pred = sub.add_parser("predict", help="classify every request in deletion_requests.csv and write a prediction CSV anywhere except the repository-root output.csv")
    p_pred.add_argument("--out", type=Path, required=True, help="where to write the CSV; the repository-root output.csv is refused (use submit for it)")
    p_pred.add_argument("--samples", action="store_true", help="predict the 32 sample requests instead of the 518 evaluation requests")
    for p in (p_sub, p_pred):
        p.add_argument("--emit-proofs", dest="emit", action="append", choices=EMIT_CHOICES, default=[],
                       help="also emit COMPLETE proofs that rest on this kind of inference (repeatable; inferred-identity is on by default, the others off). A proof that rests on two kinds needs both")
        p.add_argument("--emit-inferred-proofs", action="store_true", help="shorthand for every --emit-proofs kind")
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
        sample_file = args.dataset / "sample_output.csv"
        if not sample_file.is_file():
            print(f"error: {sample_file} not found; validate-samples needs the supplied sample answers", file=sys.stderr)
            return 2
        rows = parse_output_csv(sample_file.read_text(encoding="utf-8-sig"))
        viol = validate_output_rows(rows, ids, ev_ids)
        for v in viol:
            print(v)
        print(f"{len(rows)} sample rows, {len(viol)} violations")
        return 0 if not viol else 1
    from .state import RulesMismatch

    try:
        if args.cmd == "predict":
            return _predict(ds, args)
        if args.cmd == "submit":
            return _submit(ds, args)
    except RulesMismatch as e:  # nothing has been written yet
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2


EMIT_CHOICES = ("inferred-identity", "scoped-snapshot", "document-or-ocr")


def plan_policy(emit, emit_all: bool = False):
    """The PlanPolicy defaults plus the --emit-proofs choices (or every kind with --emit-inferred-proofs). The defaults live in PlanPolicy only."""
    from .plan import PlanPolicy

    on, d = (set(EMIT_CHOICES) if emit_all else set(emit)), PlanPolicy()
    return PlanPolicy(emit_inferred_identity_proofs=d.emit_inferred_identity_proofs or "inferred-identity" in on,
                      emit_scoped_snapshot_proofs=d.emit_scoped_snapshot_proofs or "scoped-snapshot" in on,
                      emit_document_or_ocr_proofs=d.emit_document_or_ocr_proofs or "document-or-ocr" in on)


def _pipeline(ds, cases, policy):
    """Classify and plan every case; returns (rows, plans, ids, violations). Pure: writes nothing."""
    from .classification import classify_all
    from .plan import plan_all, rows_for
    from .reconstruction import CaseReconstructor

    cr = CaseReconstructor(ds)
    plans = plan_all(classify_all(cr, cases), cr.manifest, policy)
    ids = [c.case_id for c in cases]
    rows = rows_for(plans, ids)
    ev = [f.file_id for f in ds.evidence_files.rows] + [a.activity_id for a in ds.activity.rows] + [a.audit_id for a in ds.audit_log.rows]
    return rows, plans, ids, validate_output_rows(rows, ids, ev, allow_empty_deletion_steps=True)


def _write(out: Path, rows, plans, ids) -> None:
    from collections import Counter

    from .plan import rows_to_csv

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(rows_to_csv(rows), encoding="utf-8", newline="")
    os.replace(tmp, out)  # a failed run never leaves a truncated output file behind
    print(f"wrote {len(rows)} rows to {out}; status counts {dict(Counter(plans[i].status for i in ids))}; "
          f"COMPLETE with no provable step: {sum(1 for i in ids if plans[i].status == 'COMPLETE' and not plans[i].steps)}")


def _predict(ds, args) -> int:
    out = args.out.resolve()
    protected = {(repo_root() / "output.csv").resolve(), (args.dataset.resolve().parent / "output.csv").resolve()}
    if out in protected:
        print("refusing to write the root output.csv with 'predict': use 'submit' for the submission file", file=sys.stderr)
        return 2
    cases = ds.sample_requests.rows if args.samples else ds.requests.rows
    rows, plans, ids, viol = _pipeline(ds, cases, plan_policy(args.emit, args.emit_inferred_proofs))
    for v in viol:
        print(v, file=sys.stderr)
    if viol:
        return 1
    _write(out, rows, plans, ids)
    return 0


def _submit(ds, args) -> int:
    out = (args.out if args.out is not None else repo_root() / "output.csv").resolve()
    if out.parent == args.dataset.resolve() or args.dataset.resolve() in out.parents:
        print("refusing to write inside the dataset directory", file=sys.stderr)
        return 2
    rows, plans, ids, viol = _pipeline(ds, ds.requests.rows, plan_policy(args.emit, args.emit_inferred_proofs))
    for v in viol:
        print(v, file=sys.stderr)
    if viol:
        return 1
    _write(out, rows, plans, ids)
    return 0
