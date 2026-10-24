"""M3 evaluation: resource scope and state reconstruction vs the supplied sample answers (+ structure on the eval set).

    python code/analysis/m3_evaluation.py                       # summary
    python code/analysis/m3_evaluation.py --report out.md       # also write the per-case markdown report
    python code/analysis/m3_evaluation.py --variant NAME        # ablations (see VARIANTS)

Gold labels are derived from the supplied sample answers only, per resource:
  * *in scope*  - the resource_id appears in the answer's steps;
  * *finding*   - a FINDING step exists for it (operation REPORT_*), which is what a violating state looks like.
Predictions are what ``CaseReconstructor`` concludes from usable evidence. Nothing here feeds back into the rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from ghostaudit import scenario  # noqa: E402
from ghostaudit.attribution import STRONG  # noqa: E402
from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.reconstruction import CaseReconstructor  # noqa: E402
from ghostaudit.scenario import RELEVANT, UNCERTAIN, UNRELATED  # noqa: E402
from ghostaudit.schema import system_for_resource  # noqa: E402

DEFAULT_DATASET = CODE.parent / "dataset"
NON_DELETION = {f"case_{i}" for i in range(533, 543)}


def group(rid: str) -> str:
    s = system_for_resource(rid)
    return "account" if s == "account_service" else "audit_record" if s == "security_service" else "other"


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def load_gold(ds):
    scope: Dict[str, Set[str]] = {}
    finding: Dict[Tuple[str, str], str] = {}
    status: Dict[str, str] = {}
    for r in ds.sample_output:
        steps = json.loads(r["steps_json"])
        scope[r["case_id"]] = {s["resource_id"] for s in steps}
        status[r["case_id"]] = r["deletion_status"]
        for s in steps:
            if s["phase"] == "FINDING":
                finding[(r["case_id"], s["resource_id"])] = s["operation"]
    return scope, finding, status


def build(ds, use_screenshots=True, batch_hours=None):
    if batch_hours is not None:
        scenario.BATCH_HOURS = batch_hours
    cr = CaseReconstructor(ds, use_screenshots=use_screenshots)
    cases = [q for q in ds.requests.rows + ds.sample_requests.rows if q.case_id not in NON_DELETION]
    return cr, cases, cr.reconstruct_all(cases)


def scope_metrics(recs, ds, gold_scope, include):
    tot = Counter(); grp = defaultdict(Counter); exact = 0; per_case = {}
    for q in ds.sample_requests.rows:
        pred = {d.resource_id for d in recs[q.case_id].decisions if include(d)}
        g = gold_scope[q.case_id]
        per_case[q.case_id] = (pred, g)
        for r in pred & g: tot["tp"] += 1; grp[group(r)]["tp"] += 1
        for r in pred - g: tot["fp"] += 1; grp[group(r)]["fp"] += 1
        for r in g - pred: tot["fn"] += 1; grp[group(r)]["fn"] += 1
        exact += pred == g
    return tot, grp, exact, per_case


def print_scope(label, m):
    tot, grp, exact, _ = m
    p, r, f = prf(tot["tp"], tot["fp"], tot["fn"])
    print(f"   {label:34s} TP={tot['tp']:3d} FP={tot['fp']:2d} FN={tot['fn']:3d}  P={p:.3f} R={r:.3f} F1={f:.3f}  exact={exact:2d}/32   " +
          " ".join(f"{k}={grp[k]['tp']}/{grp[k]['fp']}/{grp[k]['fn']}" for k in ("account", "audit_record", "other")))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--report", help="write the per-case markdown report")
    ap.add_argument("--no-screenshots", action="store_true")
    ap.add_argument("--batch-hours", type=float, default=None, help="ablation: change the workflow batch window")
    a = ap.parse_args(argv)
    ds = load_dataset(a.dataset)
    gold_scope, gold_finding, gold_status = load_gold(ds)
    cr, cases, recs = build(ds, not a.no_screenshots, a.batch_hours)

    print("== 1. Resource scope on the 32 samples (111 gold resources) ==")
    print("   (TP/FP/FN by group: account audit_record other)")
    m2 = scope_metrics(recs, ds, gold_scope, lambda d: d.origin == "content")
    print_scope("M2 content associations (S3k)", m2)
    m3a = scope_metrics(recs, ds, gold_scope, lambda d: d.origin == "content" and d.included)
    print_scope("+ scenario relevance", m3a)
    m3 = scope_metrics(recs, ds, gold_scope, lambda d: d.included)
    print_scope("+ activity account attribution (M3)", m3)
    m3s = scope_metrics(recs, ds, gold_scope, lambda d: d.included and (d.origin == "content" or d.relevance.label == RELEVANT))
    print("   relevance labels among M3 candidates:", dict(Counter(d.relevance.label for q in ds.sample_requests.rows for d in recs[q.case_id].decisions)))

    print("\n== 2. Violations vs the answers' FINDING steps (resources both predicted-in-scope and in the gold scope) ==")
    conf = Counter(); detail = []
    for q in ds.sample_requests.rows:
        for d in recs[q.case_id].decisions:
            if not d.included or d.resource_id not in gold_scope[q.case_id]:
                continue
            gold_op = gold_finding.get((q.case_id, d.resource_id))
            pred_op = d.assessment.finding_op if d.assessment.verdict == "VIOLATION" else None
            key = ("finding:" + gold_op[7:] if gold_op else "no finding", "pred:" + pred_op[7:] if pred_op else f"pred:none({d.assessment.verdict})")
            conf[key] += 1
            if (gold_op or None) != pred_op:
                detail.append((q.case_id, d.resource_id, d.state.category, d.state.state, gold_op, pred_op, d.assessment.verdict, d.state.has_evidence))
    for k, v in sorted(conf.items()): print(f"   {v:3d}  gold {k[0]:26s} -> {k[1]}")
    n_ok = sum(v for (g, p), v in conf.items() if (g == "no finding" and p.startswith("pred:none")) or g.replace("finding:", "") == p.replace("pred:", ""))
    print(f"   agreement {n_ok}/{sum(conf.values())}; disagreements:")
    for dd in detail: print("     ", dd)

    print("\n== 3. Diagnostic only (not the classifier): any violation among relevant resources vs gold status ==")
    cm = Counter()
    for q in ds.sample_requests.rows:
        viol = any(d.assessment.verdict == "VIOLATION" for d in recs[q.case_id].included)
        cm[(gold_status[q.case_id], "violation found" if viol else "no violation found")] += 1
    for k, v in sorted(cm.items()): print("   ", k, v)

    print("\n== 4. Eval set structure (508 deletion requests; no gold) ==")
    rel = Counter(); verdict = Counter(); unk = Counter(); acc = Counter(); post = Counter()
    for q in ds.requests.rows:
        if q.case_id in NON_DELETION: continue
        r = recs[q.case_id]; acc[r.account.status] += 1
        for d in r.decisions:
            rel[(d.state.category if False else d.system_id, d.relevance.label)] += 1
            if d.included:
                verdict[d.assessment.verdict] += 1
                if not d.state.has_evidence: unk[d.system_id] += 1
                if d.association and d.association.post_audit_only: post[d.system_id] += 1
    lab = Counter()
    for (s, l), v in rel.items(): lab[l] += v
    print("   relevance labels:", dict(lab), "| UNRELATED by system:", {s: v for (s, l), v in rel.items() if l == UNRELATED})
    print("   verdicts among included resources:", dict(verdict))
    print("   included resources with NO usable state evidence, by system:", dict(unk))
    print("   included but identified only by post-audit evidence, by system:", dict(post))
    print("   account status:", dict(acc))
    print("   screenshot notes:", *cr.screenshot_notes, sep="\n     ")
    if a.report:
        write_report(a.report, cr, ds, recs, gold_scope, gold_finding, gold_status)


def write_report(path, cr, ds, recs, gold_scope, gold_finding, gold_status):
    L = ["# M3 per-case reconstruction report (32 sample cases)", "",
         "Generated by `code/analysis/m3_evaluation.py`; regenerate rather than edit. For each case: what M3 included, why, the state it "
         "reconstructed from *usable* evidence, and how that compares with the supplied answer. `in gold` = the resource appears in the answer; "
         "`gold finding` = it has a FINDING step. Later (unusable) evidence is listed as ignored, never used.", ""]
    for q in ds.sample_requests.rows:
        r = recs[q.case_id]
        g = gold_scope[q.case_id]
        inc = {d.resource_id for d in r.included}
        L += [f"## {q.case_id} — {gold_status[q.case_id]} — {q.email} → {r.user_id}",
              f"audit_at {q.audit_at}; reported_status {q.reported_status}; gold resources {len(g)}, M3 included {len(inc)}, correct {len(inc & g)}, missing {len(g - inc)}, extra {len(inc - g)}; account attribution: **{r.account.status}** ({r.account.confidence})", ""]
        for d in r.decisions:
            tag = "IN" if d.included else "OUT"
            gf = gold_finding.get((q.case_id, d.resource_id))
            L.append(f"- [{tag} {d.relevance.label}] `{d.resource_id}` ({d.origin}) state **{d.state.state}** → {d.assessment.verdict}"
                     + (f" {d.assessment.finding_op}" if d.assessment.finding_op else "") + f" | in gold: {'yes' if d.resource_id in g else 'NO'}"
                     + (f", gold finding: {gf}" if gf else ""))
            for e in d.explanation: L.append(f"    - {e}")
            if d.state.excluded: L.append("    - ignored (not usable at audit_at): " + "; ".join(f"{e.file_id} {e.state} [{e.reason}]" for e in d.state.excluded))
        for rid in sorted(g - inc):
            L.append(f"- MISSING `{rid}`" + (f" (gold finding {gold_finding[(q.case_id, rid)]})" if (q.case_id, rid) in gold_finding else "")
                     + f": {'; '.join(r.notes) if group(rid) == 'account' and r.notes else 'no identifier link and not an attributable account'}")
        L.append("")
    Path(path).write_text("\n".join(L), encoding="utf-8", newline="\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
