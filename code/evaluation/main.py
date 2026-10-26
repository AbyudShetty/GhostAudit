"""Sample-answer scoring harness.

    python code/evaluation/main.py --pred <predictions.csv> [--ref dataset/sample_output.csv]
    python code/evaluation/main.py --self-check          # score the reference against itself + validate it

Compares a predictions CSV with a reference CSV (default: the 32 supplied sample
answers) using ghostaudit.scoring. See that module for metric definitions and the
limits of what a local score means. Never writes to dataset/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.scoring import format_report, load_cases, score  # noqa: E402
from ghostaudit.schema import parse_output_csv, validate_output_rows  # noqa: E402


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    repo = CODE_DIR.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pred", type=Path, help="predictions CSV (case_id,deletion_status,steps_json)")
    ap.add_argument("--ref", type=Path, default=repo / "dataset" / "sample_output.csv")
    ap.add_argument("--dataset", type=Path, default=repo / "dataset")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--details", type=int, default=10, help="max mismatched cases to list")
    ap.add_argument("--self-check", action="store_true", help="validate the reference file and score it against itself")
    args = ap.parse_args(argv)
    if not args.pred and not args.self_check:
        ap.error("provide --pred or --self-check")
    pred_path = args.pred or args.ref
    ref = load_cases(args.ref)
    pred = load_cases(pred_path)
    if args.self_check:
        ds = load_dataset(args.dataset)
        ev_ids = [f.file_id for f in ds.evidence_files.rows] + [a.activity_id for a in ds.activity.rows] + [a.audit_id for a in ds.audit_log.rows]
        rows = parse_output_csv(args.ref.read_text(encoding="utf-8-sig"))
        viol = validate_output_rows(rows, None, ev_ids)
        print(f"validate {args.ref.name}: {len(rows)} rows, {len(viol)} violations")
        for v in viol[:20]:
            print("  ", v)
    rep = score(pred, ref)
    print(json.dumps(rep, indent=2, ensure_ascii=False) if args.json else format_report(rep, args.details))
    return 0


if __name__ == "__main__":
    sys.exit(main())
