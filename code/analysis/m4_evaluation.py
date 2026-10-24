"""M4 evaluation: deletion status, findings and proofs vs the 32 sample answers, plus eval-set structure.

    python code/analysis/m4_evaluation.py                    # console summary
    python code/analysis/m4_evaluation.py --report out.md    # also write the per-case markdown report

Levels reported separately (a good score at one level does not establish the next):
  1. intent         NOT_APPLICABLE vs deletion (text only), incl. corroboration by CLASSIFY_REQUEST activity rows
  2. resource scope which resources are in the case (M3 metric, unchanged)
  3. findings       predicted violations vs the answers' FINDING steps (resource, operation, cited evidence)
  4. proofs         proven requirements vs the answers' verification steps of COMPLETE cases (resource, operation, cited evidence)
  5. status         COMPLETE / INCOMPLETE / NOT_APPLICABLE, with the *determination* behind each
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from ghostaudit.classification import COMPLETE, INCOMPLETE, NOT_APPLICABLE, ClassificationPolicy, classify_all  # noqa: E402
from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.intent import DELETION, NON_DELETION, classify_intent  # noqa: E402
from ghostaudit.reconstruction import CaseReconstructor  # noqa: E402
from ghostaudit.schema import case_sort_key  # noqa: E402

DATASET = CODE.parent / "dataset"


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def load_gold(ds):
    out = {}
    for r in ds.sample_output:
        steps = json.loads(r["steps_json"])
        out[r["case_id"]] = dict(status=r["deletion_status"], steps=steps,
                                 findings={(s["resource_id"], s["operation"]): s["evidence_id"] for s in steps if s["phase"] == "FINDING"},
                                 verifies={(s["resource_id"], s["operation"]): s["evidence_id"] for s in steps if s["phase"] == "VERIFICATION"},
                                 scope={s["resource_id"] for s in steps})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    ds = load_dataset(a.dataset)
    cr = CaseReconstructor(ds)
    cases = ds.requests.rows + ds.sample_requests.rows
    gold = load_gold(ds)
    verdicts = classify_all(cr, cases)
    variants = {"default (unproven->COMPLETE)": verdicts,
                "unproven->INCOMPLETE": classify_all(cr, cases, ClassificationPolicy("strict", unproven_status=INCOMPLETE)),
                "no inferred attribution": classify_all(cr, cases, ClassificationPolicy("noinf", accept_inferred_attribution=False)),
                "violations: HIGH only": classify_all(cr, cases, ClassificationPolicy("highonly", accept_medium_violations=False))}
    samples = ds.sample_requests.rows
    sample_ids = {q.case_id for q in samples}

    # ---------------------------------------------------------------- 1 intent
    print("== 1. Intent (request text only) ==")
    ic = Counter();
    for label, rows in (("samples", samples), ("eval", ds.requests.rows)):
        c = Counter(classify_intent(q.request_text).outcome for q in rows)
        print(f"   {label}: {dict(c)}")
    claim_rows = [x for x in ds.activity.rows if x.event_type == "CLASSIFY_REQUEST"]
    non_del = [q.case_id for q in ds.requests.rows if classify_intent(q.request_text).outcome == NON_DELETION]
    linked = {}
    for q in ds.requests.rows:
        u = cr.user_of(q)
        for l in cr.links.links_for_user(u):
            linked.setdefault(l.resource_id, set()).add(q.case_id)
    corro = Counter()
    for x in claim_rows:
        cs = linked.get(x.resource_id, set())
        corro[("classified non-deletion by text" if cs and all(c in non_del for c in cs) else "other/none", len(cs))] += 1
    print(f"   corroboration (claims, not used for the decision): {len(claim_rows)} CLASSIFY_REQUEST rows; the account each row names is linked to:",
          dict(corro))
    print(f"   deletion cases whose associated accounts carry a CLASSIFY_REQUEST row: {sum(1 for x in claim_rows for c in linked.get(x.resource_id, ()) if c not in non_del)}")

    # ---------------------------------------------------------------- 5 status (samples)
    print("\n== 5a. Status on the 32 samples (default policy) ==")
    conf = Counter((gold[q.case_id]["status"], verdicts[q.case_id].status, verdicts[q.case_id].determination) for q in samples)
    for k, v in sorted(conf.items()): print(f"   gold {k[0]:10s} -> pred {k[1]:10s} determination {k[2]:9s} x{v}")
    ok = sum(1 for q in samples if verdicts[q.case_id].status == gold[q.case_id]["status"])
    print(f"   exact status agreement: {ok}/32")
    print("   by policy variant (agreement on samples | eval status counts):")
    for name, vd in variants.items():
        agree = sum(1 for q in samples if vd[q.case_id].status == gold[q.case_id]["status"])
        ev = Counter(vd[q.case_id].status for q in ds.requests.rows)
        print(f"     {name:32s} {agree:2d}/32 | eval {dict(ev)}")
    partial_gold = Counter(gold[q.case_id]["status"] for q in samples if verdicts[q.case_id].determination == "PARTIAL")
    print(f"   sample cases the classifier could NOT prove (determination PARTIAL): gold status of those = {dict(partial_gold)}")
    print("   -> their agreement comes from the unproven_status policy, not from proof.")

    # ---------------------------------------------------------------- 3 findings
    print("\n== 3. Findings: predicted violations vs the answers' FINDING steps (32 samples) ==")
    tp = fp = fn = ev_exact = ev_in = 0; misses = []; extras = []
    for q in samples:
        g, v = gold[q.case_id], verdicts[q.case_id]
        pred = {(x.resource_id, x.finding_op): x.evidence_ids for x in v.violations if x.finding_op}
        for k, e in pred.items():
            if k in g["findings"]:
                tp += 1; ev_in += g["findings"][k] in e; ev_exact += (e == (g["findings"][k],))
            else:
                fp += 1; extras.append((q.case_id, k))
        for k in g["findings"]:
            if k not in pred: fn += 1; misses.append((q.case_id, k))
    p, r, f = prf(tp, fp, fn)
    print(f"   TP={tp} FP={fp} FN={fn}  P={p:.3f} R={r:.3f} F1={f:.3f} | gold-cited file among ours: {ev_in}/{tp}; exactly our only cite: {ev_exact}/{tp}")
    print("   missed findings:", misses, "| extra findings:", extras)
    print(f"   cases whose full finding set matches exactly: {sum(1 for q in samples if {k for k in gold[q.case_id]['findings']} == {(x.resource_id, x.finding_op) for x in verdicts[q.case_id].violations if x.finding_op})}/32")

    # ---------------------------------------------------------------- 4 proofs
    print("\n== 4. Proofs: proven requirements vs the answers' verification steps of the 10 COMPLETE cases ==")
    tp = miss = ev_ok = 0; rows = []
    for q in samples:
        g = gold[q.case_id]
        if g["status"] != COMPLETE: continue
        v = verdicts[q.case_id]
        proven = {(r.resource_id, r.expected_op): r for r in v.proven if r.resource_id}
        for k, e in g["verifies"].items():
            r_ = proven.get(k)
            if r_ is not None:
                tp += 1; ev_ok += e in r_.evidence_ids
            else:
                miss += 1
                why = next((x.message for x in v.gaps if x.resource_id == k[0]), None) or next((x.message for x in v.gaps if x.resource_id is None), "no requirement for it")
                rows.append((q.case_id, k[0], k[1], e, why[:95]))
    extra = sum(1 for q in samples if gold[q.case_id]["status"] == COMPLETE for r in verdicts[q.case_id].proven if r.resource_id and (r.resource_id, r.expected_op) not in gold[q.case_id]["verifies"] and r.expected_op)
    print(f"   gold verification steps: {tp + miss}; we PROVE {tp} (cited file matches in {ev_ok}); cannot prove {miss}; extra proofs not in the answer: {extra}")
    for r_ in rows: print("     cannot prove:", r_)

    # ---------------------------------------------------------------- eval structure
    print("\n== 5b. Eval set (518 requests, no gold) ==")
    for name, vd in variants.items():
        c = Counter((vd[q.case_id].status, vd[q.case_id].determination) for q in ds.requests.rows)
        print(f"   {name:32s} {dict(sorted(c.items()))}")
    base = verdicts
    partial = [q.case_id for q in ds.requests.rows if base[q.case_id].determination == "PARTIAL"]
    print(f"   PARTIAL cases (status rests on policy): {len(partial)}; blocking gap codes:",
          dict(Counter(g.code for cid in partial for g in base[cid].gaps if g.blocking)))
    print("   gap codes over all deletion cases:", dict(Counter(g.code for q in ds.requests.rows for g in base[q.case_id].gaps)))
    print("   violations by finding op and confidence:", dict(Counter((x.finding_op, x.confidence) for q in ds.requests.rows for x in base[q.case_id].violations)))
    print("   COMPLETE proofs by confidence:", dict(Counter(r.confidence for q in ds.requests.rows if base[q.case_id].status == COMPLETE for r in base[q.case_id].proven)))
    xt = Counter((q.reported_status, base[q.case_id].status) for q in ds.requests.rows if base[q.case_id].status != NOT_APPLICABLE)
    print("   reported_status x predicted status (eval; reported_status is a claim and is not an input):", dict(sorted(xt.items())))
    print("   same on samples with gold:", dict(sorted(Counter((q.reported_status, gold[q.case_id]["status"]) for q in samples).items())))

    # ---------------------------------------------------------------- dependence on reported_status
    print("\n== dependence on reported_status (only via the account matching key) ==")
    cr2 = CaseReconstructor(ds, require_result_match=False)
    v2 = classify_all(cr2, cases)
    chg = [q.case_id for q in cases if v2[q.case_id].status != base[q.case_id].status]
    chg_det = [q.case_id for q in cases if (v2[q.case_id].status, v2[q.case_id].determination) != (base[q.case_id].status, base[q.case_id].determination)]
    print(f"   with the result-compatibility key switched off: status changes in {len(chg)} cases, (status, determination) in {len(chg_det)} cases")

    # ---------------------------------------------------------------- sample-informed rules
    recs = cr.reconstruct_all([q for q in cases if classify_intent(q.request_text).outcome == DELETION])
    print("\n== sample-informed rules: how many eval cases do they touch? ==")
    rules = {"contact_lookup_completed -> delete-class": lambda d: d.state.basis == "log.contact_lookup_completed.rows",
             "support contact record (contact_query_completed) -> delete-class": lambda d: d.state.basis == "log.contact_query_completed.rows",
             "pdf attachment -> SUPPORT_ATTACHMENT": lambda d: d.state.basis == "pdf.attachment_document"}
    cs_all = {q.case_id for q in ds.requests.rows}
    for name, fn_ in rules.items():
        hit = sorted(cid for cid, rc in recs.items() for d in rc.decisions if fn_(d) and d.state.has_evidence)
        print(f"   {name:62s} samples: {[c for c in hit if c in sample_ids]}  eval: {[c for c in hit if c in cs_all]}")

    # ---------------------------------------------------------------- consequences for the required steps
    print("\n== consequence of unresolved accounts for the account step (eval, 508 deletion requests) ==")
    from ghostaudit.attribution import AMBIGUOUS, NONE as ATT_NONE
    cons = Counter()
    for q in ds.requests.rows:
        v = verdicts[q.case_id]
        if v.status == NOT_APPLICABLE:
            continue
        att = recs[q.case_id].account
        acct_req = next((r for r in v.requirements if r.key == "ACCOUNT_ABSENT"), None)
        has_id = acct_req is not None and acct_req.resource_id is not None
        cons[(att.status, v.status, "account id known" if has_id or any(x.resource_id and x.resource_id.startswith("account_") for x in v.violations) else "NO account id")] += 1
    for k, n in sorted(cons.items()): print(f"   attribution {k[0]:9s} status {k[1]:10s} {k[2]:17s} x{n}")
    noid = sum(n for (st, _s, idk), n in cons.items() if idk == "NO account id")
    print(f"   cases for which no account step can be produced without inventing an id: {noid}")
    print("   (the output schema requires non-empty system_id and resource_id; there is no placeholder or 'unknown' form)")

    # ---------------------------------------------------------------- recreation claims
    print("\n== recreation claims in activity (SYNC_RECORD by connector / RESTORE_BACKUP): informational, not evidence of state ==")
    rid_re = re.compile(r"(?:search|support|account|analytics|backup|export|data_flow)_\d+")
    claims = [x for x in ds.activity.rows if x.event_type in ("RESTORE_BACKUP",) or (x.event_type == "SYNC_RECORD" and x.source_system == "integration_service")]
    named = Counter()
    for q in ds.requests.rows:
        rc = recs.get(q.case_id)
        if rc is None: continue
        mine = {d.resource_id for d in rc.included}
        for x in claims:
            if x.event_time <= q.audit_at and (x.resource_id in mine or any(m in mine for m in rid_re.findall(x.details))):
                named[(x.event_type, q.case_id)] += 1
    print(f"   {len(claims)} such rows in total; rows that name a resource included in an eval case at/before its audit_at: {sum(named.values())} in {len({c for _, c in named})} cases")
    for (ev, cid), n in sorted(named.items())[:6]: print(f"     {ev} {cid} x{n} -> status {verdicts[cid].status}")

    if a.report:
        write_report(a.report, ds, gold, verdicts, samples)


def write_report(path, ds, gold, verdicts, samples):
    L = ["# M4 per-case classification report (32 sample cases)", "",
         "Generated by `code/analysis/m4_evaluation.py` (default policy); regenerate rather than edit. *determination* says how well the status is supported; "
         "`PARTIAL` means no violation was found but the deletion could not be proven, so the status comes from the policy.", ""]
    for q in samples:
        v, g = verdicts[q.case_id], gold[q.case_id]
        mark = "agree" if v.status == g["status"] else "**DISAGREE**"
        L += [f"## {q.case_id} — gold {g['status']} / predicted {v.status} ({mark}) — determination {v.determination}, confidence {v.confidence}",
              f"{q.email}; audit_at {q.audit_at}; reported_status {q.reported_status}; intent {v.intent.outcome} ({v.intent.reason})", ""]
        for r in v.reasons: L.append(f"- why: {r}")
        for x in v.violations:
            gf = g["findings"].get((x.resource_id, x.finding_op))
            L.append(f"- violation `{x.resource_id}` {x.finding_op} [{x.confidence}] cites {', '.join(x.evidence_ids) or '-'}; answer: " +
                     (f"same finding, cites {gf}" if gf else "**no such finding in the answer**") + (f"; caveats: {'; '.join(x.caveats)}" if x.caveats else ""))
        for k, e in g["findings"].items():
            if not any((x.resource_id, x.finding_op) == k for x in v.violations):
                L.append(f"- MISSED finding `{k[0]}` {k[1]} (answer cites {e})")
        for r in v.proven:
            L.append(f"- proven [{r.confidence}] `{r.resource_id or '(set of ' + str(len(r.candidates)) + ' candidates)'}` {r.expected_op} cites {', '.join(r.evidence_ids) or '-'}")
        for r in v.unproven:
            L.append(f"- UNPROVEN `{r.resource_id or '(account)'}` {r.expected_op}: {r.reason}")
        for x in v.gaps:
            L.append(f"- gap [{x.code}{'' if x.blocking else ', non-blocking'}] {x.message}")
        for rid, lab, why in v.excluded:
            L.append(f"- excluded `{rid}` ({lab}): {why}")
        L.append("")
    Path(path).write_text("\n".join(L), encoding="utf-8", newline="\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
