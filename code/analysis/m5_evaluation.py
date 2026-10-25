"""M5 evaluation: steps vs the 32 sample answers, per phase, plus validity/withholding statistics on all 518 requests.

    python code/analysis/m5_evaluation.py [--report out.md] [--emit-inferred] [--predictions PATH]

Reported separately (a good number at one level says nothing about the next):
  status | findings (resource, operation, cited file) | remediation (resource, operation) | verification (resource, operation, evidence)
For each phase: matched, MISSING (the answer has it, we do not emit it, with the reason) and UNSUPPORTED (we emit it, the answer lacks it).
``--predictions PATH`` writes the 518-row prediction CSV to PATH for inspection; it refuses to write to the repository root.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from ghostaudit.classification import COMPLETE, INCOMPLETE, NOT_APPLICABLE, classify_all  # noqa: E402
from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.plan import PlanPolicy, plan_all, rows_for, rows_to_csv  # noqa: E402
from ghostaudit.reconstruction import CaseReconstructor  # noqa: E402
from ghostaudit.schema import validate_output_rows  # noqa: E402
from ghostaudit.scoring import format_report, score  # noqa: E402
from ghostaudit.timeutil import is_eligible  # noqa: E402

DATASET = CODE.parent / "dataset"
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")


def key(step, with_evidence):
    return (step["resource_id"], step["operation"]) + ((step["evidence_id"],) if with_evidence else ())


def gold_cases(ds):
    out = {}
    for r in ds.sample_output:
        out[r["case_id"]] = (r["deletion_status"], json.loads(r["steps_json"]))
    return out


def missing_reason(cid, step, plan, verdict, recon_account):
    rid, op = step["resource_id"], step["operation"]
    w = [x for x in plan.withheld if x.resource_id == rid and x.operation == op]
    if w:
        return "withheld: " + w[0].reason
    if rid.startswith("account_"):
        gaps = [g.code for g in verdict.gaps if g.code.startswith("ACCOUNT")]
        return "account not identified (" + (gaps[0] if gaps else recon_account) + ")"
    return "resource not linked to the subject by any identifier (no participant-visible link)"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--report")
    ap.add_argument("--emit-inferred", action="store_true", help="ablation: also emit COMPLETE proofs that rest on inferred identity/scoped snapshot/OCR")
    ap.add_argument("--predictions", help="write the 518-row prediction CSV here (never the repository root)")
    a = ap.parse_args(argv)
    ds = load_dataset(a.dataset)
    cr = CaseReconstructor(ds)
    cases = ds.requests.rows + ds.sample_requests.rows
    # each population on its own, exactly as `code/main.py` (518 requests) and `predict --samples` (32) run them: account attribution
    # eliminates candidates across the requests it is given, so classifying both together would describe neither output
    verdicts = {**classify_all(cr, ds.requests.rows), **classify_all(cr, ds.sample_requests.rows)}
    pol = PlanPolicy.all_inferred() if a.emit_inferred else PlanPolicy()
    plans = plan_all(verdicts, cr.manifest, pol)
    gold = gold_cases(ds)
    samples = [q.case_id for q in ds.sample_requests.rows]
    recon = cr.reconstruct_all([q for q in cases if verdicts[q.case_id].status != NOT_APPLICABLE])

    print(f"plan policy: {pol}")
    print("\n== status (samples) ==")
    print(f"   exact agreement {sum(1 for c in samples if plans[c].status == gold[c][0])}/32; determination of the agreeing cases: "
          f"{dict(Counter(plans[c].determination for c in samples))}")

    tot = {}
    detail = defaultdict(list)
    for ph in PHASES:
        m = miss = uns = ev_ok = ev_n = 0
        for c in samples:
            gs = [s for s in gold[c][1] if s["phase"] == ph]
            ps = [s for s in plans[c].steps if s["phase"] == ph]
            gk, pk = {key(s, False): s for s in gs}, {key(s, False): s for s in ps}
            for k, s in gk.items():
                if k in pk:
                    m += 1
                    if ph == "FINDING" or gold[c][0] == COMPLETE:
                        ev_n += 1
                        ev_ok += pk[k]["evidence_id"] == s["evidence_id"]
                else:
                    miss += 1
                    detail[(ph, "missing")].append((c, k, missing_reason(c, s, plans[c], verdicts[c], recon[c].account.status if c in recon else "n/a")))
            for k, s in pk.items():
                if k not in gk:
                    uns += 1
                    detail[(ph, "unsupported")].append((c, k, s["evidence_id"]))
        tot[ph] = (m, miss, uns, ev_ok, ev_n)
    print("\n== per-phase agreement on the 32 samples ==")
    print("   phase          matched  missing  unsupported  evidence-id equal (where cited)")
    for ph in PHASES:
        m, miss, uns, ok, n = tot[ph]
        print(f"   {ph:13s} {m:7d} {miss:8d} {uns:12d}   {ok}/{n}" if n else f"   {ph:13s} {m:7d} {miss:8d} {uns:12d}   (empty evidence by design)")
    for (ph, kind), items in sorted(detail.items()):
        by = Counter(r for _, _, r in items) if kind == "missing" else None
        print(f"\n   {ph} {kind} ({len(items)}):")
        if by:
            for r, n in by.most_common(): print(f"      {n:3d}  {r[:150]}")
        for it in items[:40 if kind == "unsupported" else 0]: print("      ", it)

    print("\n== the scorer (our metrics, not the official grader) ==")
    from ghostaudit.scoring import Case
    pred = {c: (plans[c].status, list(plans[c].steps)) for c in samples}
    ref = {c: (gold[c][0], gold[c][1]) for c in samples}
    rep = score(pred, ref)
    print(format_report(rep, 0))

    print("\n== 518 eval rows ==")
    rows = rows_for(plans, [q.case_id for q in ds.requests.rows])
    ev_ids = [f.file_id for f in ds.evidence_files.rows] + [x.activity_id for x in ds.activity.rows] + [x.audit_id for x in ds.audit_log.rows]
    viol = validate_output_rows(rows, [q.case_id for q in ds.requests.rows], ev_ids, allow_empty_deletion_steps=True)
    print(f"   schema/ordering/evidence-convention violations: {len(viol)} {[str(v) for v in viol[:5]]}")
    man = cr.manifest
    audit = {q.case_id: q.audit_at for q in cases}
    bad_cite = [(c, s["evidence_id"]) for c, p in plans.items() for s in p.steps if s["evidence_id"] and s["evidence_id"] in man and not is_eligible(man[s["evidence_id"]].captured_at, audit[c])]
    print(f"   citations captured after the case's audit_at: {len(bad_cite)}")
    st = Counter((plans[q.case_id].status, plans[q.case_id].determination) for q in ds.requests.rows)
    print("   status/determination:", dict(sorted(st.items())))
    empty = [q.case_id for q in ds.requests.rows if plans[q.case_id].status == COMPLETE and not plans[q.case_id].steps]
    print(f"   COMPLETE cases with no step (nothing directly provable): {len(empty)} {empty}")
    n_steps = Counter((plans[q.case_id].status, s["phase"]) for q in ds.requests.rows for s in plans[q.case_id].steps)
    print("   steps emitted:", dict(sorted(n_steps.items())))
    wr = Counter(w.reason.split(":")[0][:90] for q in ds.requests.rows for w in plans[q.case_id].withheld)
    print("   steps withheld, by reason:", dict(wr.most_common()))
    cw = Counter((plans[q.case_id].status, w.phase) for q in ds.requests.rows for w in plans[q.case_id].withheld)
    print("   withheld by (status, phase):", dict(sorted(cw.items())))
    comp_acct = Counter(("account step emitted" if any(s["resource_id"].startswith("account_") for s in plans[q.case_id].steps) else "no account step") for q in ds.requests.rows if plans[q.case_id].status == COMPLETE)
    inc_acct = Counter(("account step emitted" if any(s["resource_id"].startswith("account_") for s in plans[q.case_id].steps) else "no account step") for q in ds.requests.rows if plans[q.case_id].status == INCOMPLETE)
    print("   account step, COMPLETE cases:", dict(comp_acct), "| INCOMPLETE cases:", dict(inc_acct))
    if a.predictions:
        out = Path(a.predictions).resolve()
        if out.parent == CODE.parent:
            raise SystemExit("refusing to write predictions into the repository root (output.csv is a later, deliberate step)")
        out.write_text(rows_to_csv(rows), encoding="utf-8", newline="")
        print("   wrote", out)
    if a.report:
        write_report(a.report, ds, gold, plans, verdicts, samples, detail)


def write_report(path, ds, gold, plans, verdicts, samples, detail):
    L = ["# M5 per-case step report (32 samples)", "", "Generated by `code/analysis/m5_evaluation.py`; regenerate rather than edit. Status, findings, remediation and verification are compared separately.", ""]
    for c in samples:
        g_status, g_steps = gold[c]
        p = plans[c]
        L += [f"## {c} — gold {g_status} / ours {p.status} ({p.determination}) — emitted {len(p.steps)} step(s), gold {len(g_steps)}"]
        for ph in PHASES:
            gk = {key(s, False): s for s in g_steps if s["phase"] == ph}
            pk = {key(s, False): s for s in p.steps if s["phase"] == ph}
            for k in sorted(set(gk) | set(pk)):
                tag = "match" if k in gk and k in pk else ("MISSING" if k in gk else "UNSUPPORTED")
                ev = (pk[k]["evidence_id"] if k in pk else gk[k]["evidence_id"]) or "-"
                same = "" if tag != "match" or pk[k]["evidence_id"] == gk[k]["evidence_id"] else f" (evidence differs: ours {pk[k]['evidence_id'] or '-'}, answer {gk[k]['evidence_id'] or '-'})"
                L.append(f"- {ph[:4]} {tag} `{k[0]}` {k[1]} [{ev}]{same}")
        for w in p.withheld:
            L.append(f"- withheld {w.phase[:4]} `{w.resource_id or '(account)'}` {w.operation}: {w.reason}")
        L.append("")
    Path(path).write_text("\n".join(L), encoding="utf-8", newline="\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
