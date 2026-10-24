"""Sensitivity / ablation of the M3 rules, to expose overfitting to the 32 samples.

    python code/analysis/m3_sensitivity.py

Two empirical parameters were set from the samples (batch window 4h, account window -90..+15 min); the result-compatibility
filter and the relevance rules were designed from record semantics and then checked. Each row changes one thing and reports
sample scope P/R/F1, the number of confidently WRONG account assignments (samples and hide-and-recover), and eval-set shifts.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import m3_evaluation as M  # noqa: E402
from ghostaudit.attribution import AMBIGUOUS, CONTENT, NONE, STRONG, AccountAttributor  # noqa: E402
from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.reconstruction import CaseReconstructor  # noqa: E402

CONFIGS = [
    ("baseline (4h, -90..+15, result match)", dict()),
    ("batch 2h", dict(batch_hours=2.0)),
    ("batch 8h", dict(batch_hours=8.0)),
    ("window -60..+15", dict(window_min=(-60.0, 15.0))),
    ("window -120..+30", dict(window_min=(-120.0, 30.0))),
    ("window -180..+60", dict(window_min=(-180.0, 60.0))),
    ("no result-compatibility filter", dict(require_result_match=False)),
]


def hide_recover(cr, cases, base):
    pairs = [(c, cr.user_of(c)) for c in cases]
    truth = {cid: r.account.account for cid, r in base.items() if r.account.status == CONTENT}
    ids = sorted(truth, key=lambda c: int(c.split("_")[1]))
    out = Counter()
    for part in (ids[::2], ids[1::2]):
        res = cr.attributor.attribute(pairs, hide_content_for=part)
        for cid in part:
            r = res[cid]
            out["correct" if r.account == truth[cid] else ("WRONG" if r.account else "undecided")] += 1
    return out


def main():
    ds = load_dataset(M.DEFAULT_DATASET)
    gold_scope, _, _ = M.load_gold(ds)
    print(f"{'config':40s} {'sample P':>8s} {'R':>6s} {'F1':>6s} {'exact':>6s} | samples acct wrong | hide&recover (correct/WRONG/undecided) | eval: STRONG AMBIG NONE UNRELATED")
    for name, kw in CONFIGS:
        cr = CaseReconstructor(ds, **kw)
        cases = [q for q in ds.requests.rows + ds.sample_requests.rows if q.case_id not in M.NON_DELETION]
        recs = cr.reconstruct_all(cases)
        tot, grp, exact, _ = M.scope_metrics(recs, ds, gold_scope, lambda d: d.included)
        p, r, f = M.prf(tot["tp"], tot["fp"], tot["fn"])
        gold_acc = {cid: next(x for x in g if x.startswith("account_")) for cid, g in gold_scope.items()}
        wrong = sum(1 for cid, t in gold_acc.items() if recs[cid].account.account and recs[cid].account.account != t)
        hr = hide_recover(cr, cases, recs)
        ev = Counter(recs[q.case_id].account.status for q in ds.requests.rows if q.case_id not in M.NON_DELETION)
        unrel = sum(1 for q in ds.requests.rows if q.case_id not in M.NON_DELETION for d in recs[q.case_id].decisions if not d.included)
        print(f"{name:40s} {p:8.3f} {r:6.3f} {f:6.3f} {exact:4d}/32 | {wrong:6d}            | {hr['correct']:4d}/{hr['WRONG']:3d}/{hr['undecided']:4d}                    | {ev[STRONG]:5d} {ev[AMBIGUOUS]:5d} {ev[NONE]:4d} {unrel:5d}")


if __name__ == "__main__":
    main()
