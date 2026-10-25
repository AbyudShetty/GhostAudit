"""Controlled comparison of the COMPLETE proof-emission policies (one switch per kind of inference).

    python code/analysis/proof_policy_comparison.py

Policies: none (no inferred proof), the default (inferred identity only), and the other combinations with snapshot and OCR proofs. For each it reports, on the 32
samples, step precision / recall / F1 (our scorer, not the official grader), sequence accuracy and the exact steps it adds; on the
518 evaluation requests, which have no answers, only *how many* steps it adds, by the kinds of inference they rest on, and every
added step's ownership and state facts, with the ones that rest on weak ground flagged. Nothing here is a claim of accuracy on the 518.

Diagnostic only: readings captured after ``audit_at`` are shown (``later file shows PRESENT``) to judge how reliable a rule is. They
never decide a case, and the pipeline never uses them for state.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from ghostaudit.attribution import RESULT_FOR_STATUS, STRONG  # noqa: E402
from ghostaudit.classification import COMPLETE, NOT_APPLICABLE, classify_all  # noqa: E402
from ghostaudit.plan import PlanPolicy  # noqa: E402
from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.plan import plan_all  # noqa: E402
from ghostaudit.reconstruction import CaseReconstructor  # noqa: E402
from ghostaudit.scoring import load_cases, score  # noqa: E402
from ghostaudit.timeutil import parse_ts  # noqa: E402

DATASET = CODE.parent / "dataset"
# (label, (inferred identity, scoped snapshot, document or OCR)); built explicitly so that the defaults do not leak into the comparison
POLICIES = [("none", (False, False, False)), ("inferred identity (default)", (True, False, False)), ("scoped snapshot only", (False, True, False)),
            ("identity + snapshot", (True, True, False)), ("identity + OCR", (True, False, True)), ("all three", (True, True, True))]


def key(s):
    return (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"], s["evidence_id"])


def pre_elimination_pools(cr, pairs):
    """Accounts each case could have before other cases' accounts are eliminated (same filters as the attributor)."""
    att, out = cr.attributor, {}
    for c, u in pairs:
        want = RESULT_FOR_STATUS.get(c.reported_status)
        owners = att.owners_at(c.audit_at)  # ownership knowable at this case's audit_at
        out[c.case_id] = {rid for rid in att.events for hit in [att.row_for(c, rid)] if hit and not owners.get(rid) and hit[0].reported_result == want}
    return out


def facts(cr, case, rec, req, pools):
    """Ownership and state facts for one added step, and the flags that say it rests on weak ground."""
    rid, d = req.resource_id, rec.decision(req.resource_id)
    f, flags = {}, []
    if rid.startswith("account_"):
        f["owner"] = "activity (unique)" if rec.account.status == STRONG else "identifier" + (" (post-audit file only)" if d and d.association and d.association.post_audit_only else "")
        others = sorted(c for c, p in pools.items() if c != case.case_id and rid in p)
        if rec.account.status == STRONG and others:
            flags.append("attribution depends on eliminating " + ", ".join(others))
        hit = cr.attributor.row_for(case, rid)
        f["worker"] = hit[0].reported_result if hit else "-"
    if "scoped_snapshot" in req.inferences:
        flags.append("absence = not listed in a subject-scoped snapshot")
        if f.get("worker") in ("FAILED", "PARTIAL"):
            flags.append(f"the workflow itself claims the delete {f['worker']}")
    if "document_or_ocr" in req.inferences and d is not None:
        others = sorted({e.file_id for e in d.state.history if e.file_id not in req.evidence_ids})
        f["other usable readings"] = others or "none"
    if d is not None:
        later = [e for e in d.state.excluded if e.state == "PRESENT" and parse_ts(e.effective_time) > parse_ts(case.audit_at)]
        if later:
            flags.append(f"diagnostic: later file {later[0].file_id} shows PRESENT (not admissible)")
    return f, flags


def main():
    ds = load_dataset(DATASET)
    cr = CaseReconstructor(ds)
    ref = load_cases(DATASET / "sample_output.csv")
    sets = {}
    for label, cases in (("samples", ds.sample_requests.rows), ("eval", ds.requests.rows)):
        v = classify_all(cr, cases)
        dels = [c for c in cases if v[c.case_id].status != NOT_APPLICABLE]
        recs = cr.reconstruct_all(dels)
        sets[label] = (cases, v, recs, pre_elimination_pools(cr, [(c, recs[c.case_id].user_id) for c in dels]))
    none = PlanPolicy(emit_inferred_identity_proofs=False, emit_scoped_snapshot_proofs=False, emit_document_or_ocr_proofs=False)
    base = {label: plan_all(sets[label][1], cr.manifest, none) for label in sets}
    print(f"{'policy':18} {'TP':>4} {'FP':>3} {'FN':>3} {'precision':>9} {'recall':>7} {'F1':>7} {'sequence':>8}  sample steps added | eval steps added")
    detail = []
    for name, (ident, snap, ocr) in POLICIES:
        pol = PlanPolicy(emit_inferred_identity_proofs=ident, emit_scoped_snapshot_proofs=snap, emit_document_or_ocr_proofs=ocr)
        plans = {label: plan_all(sets[label][1], cr.manifest, pol) for label in sets}
        pred = {c: (p.status, list(p.steps)) for c, p in plans["samples"].items()}
        rep = score(pred, ref)
        tp = sum(sum((Counter(map(key, ref[c][1])) & Counter(map(key, pred[c][1]))).values()) for c in ref)
        fp = sum(len(pred[c][1]) for c in ref) - tp
        fn = sum(len(ref[c][1]) for c in ref) - tp
        added_s = []
        for c in sorted(ref, key=lambda x: int(x.split("_")[1])):
            had = set(map(key, base["samples"][c].steps))
            for s in plans["samples"][c].steps:
                if key(s) not in had:
                    added_s.append(f"{c} {s['resource_id']} ({'in' if key(s) in set(map(key, ref[c][1])) else 'NOT in'} answer)")
        kinds = Counter()
        cases, verdicts, recs, pools = sets["eval"]
        byc = {c.case_id: c for c in cases}
        for c, p in plans["eval"].items():
            assert p.status == base["eval"][c].status and set(map(key, base["eval"][c].steps)) <= set(map(key, p.steps)), (name, c)  # only additions
            had = set(map(key, base["eval"][c].steps))
            for s in p.steps:
                if key(s) in had:
                    continue
                req = next(r for r in verdicts[c].requirements if r.resource_id == s["resource_id"] and r.expected_op == s["operation"])
                kinds["+".join(req.inferences)] += 1
                if name == "all three":
                    detail.append((c, s, req, *facts(cr, byc[c], recs[c], req, pools)))
        se = rep["step_exact"]
        print(f"{name:18} {tp:4} {fp:3} {fn:3} {se['precision']:9.4f} {se['recall']:7.4f} {se['f1']:7.4f} {rep['sequence_exact']:8.4f}  "
              f"{'; '.join(added_s) or '-'} | {sum(kinds.values())} {dict(sorted(kinds.items()))}")
    print("\nEvery evaluation step the policies can add (no answers exist for these; flags mark weak ground):")
    for c, s, req, f, flags in sorted(detail, key=lambda t: ("+".join(t[2].inferences), int(t[0].split("_")[1]))):
        print(f"  [{'+'.join(req.inferences)}] {c} {s['operation']} {s['resource_id']} cites {s['evidence_id']} | "
              + ", ".join(f"{k}: {v}" for k, v in f.items()) + (" | " + "; ".join(flags) if flags else ""))
    print("\nCOMPLETE evaluation cases with no step: default", sum(1 for p in base["eval"].values() if p.status == COMPLETE and not p.steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
