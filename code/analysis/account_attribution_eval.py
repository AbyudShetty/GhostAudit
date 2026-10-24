"""Evaluate activity-based account attribution without trusting it.

    python code/analysis/account_attribution_eval.py

Three views:
  1. samples: attribution vs the account in the supplied answer (truth known for 32 cases);
  2. hide-and-recover (pessimistic): hide the content link of EVERY content-linked eval case and ask whether
     activity alone recovers it. Truth = the hidden link (independent of the sample answers);
  3. hide-and-recover (realistic): hide every other content-linked case (about half, close to the real
     share of cases without a content link), keep the rest as elimination evidence.
A wrong assignment is the number that matters most: an ambiguous answer is allowed, a confident wrong one is not.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import link_policy_comparison as L  # noqa: E402
from ghostaudit.attribution import AMBIGUOUS, CONTENT, NONE, STRONG, AccountAttributor  # noqa: E402
from ghostaudit.linker import POLICIES  # noqa: E402

NON_DELETION = {f"case_{i}" for i in range(533, 543)}


def run(ctx: "L.Ctx"):
    cases = [(q, ctx.user(q)) for q in ctx.ds.requests.rows + ctx.ds.sample_requests.rows if q.case_id not in NON_DELETION]
    att = AccountAttributor(ctx.ds.activity.rows, ctx.idx)
    return cases, att


def summarize(label, results, truth):
    c = Counter()
    wrong = []
    cand_sizes = Counter()
    for cid, r in results.items():
        if cid not in truth:
            continue
        t = truth[cid]
        if r.account is not None:
            if r.account == t:
                c["assigned & correct"] += 1
            else:
                c["assigned & WRONG"] += 1
                wrong.append((cid, r.account, t))
        elif r.status == AMBIGUOUS:
            c["ambiguous (truth among candidates)" if any(x.resource_id == t for x in r.candidates) else "ambiguous (truth NOT among candidates)"] += 1
            cand_sizes[min(len(r.candidates), 6)] += 1
        else:
            c["none"] += 1
    n = sum(c.values())
    print(f"  {label}: n={n} " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if cand_sizes:
        print("     ambiguous candidate-set sizes:", dict(sorted(cand_sizes.items())))
    if wrong:
        print("     WRONG:", wrong[:8])
    return c


def main():
    ctx = L.Ctx(L.DEFAULT_DATASET)
    cases, att = run(ctx)
    base = att.attribute(cases)
    print("== 1. samples (truth = account in the supplied answer) ==")
    gold_acc = {cid: next(r for r in g if r.startswith("account_")) for cid, g in ctx.gold.items()}
    summarize("all sample cases", base, gold_acc)
    unlinked = {cid: t for cid, t in gold_acc.items() if base[cid].status != CONTENT}
    summarize("samples without a content-linked account", base, unlinked)
    print("   eval cases by status:", dict(Counter(r.status for cid, r in base.items() if cid not in gold_acc)))

    print("\n== 2./3. hide-and-recover on eval cases whose account is content-linked in the lifecycle window ==")
    truth = {cid: r.account for cid, r in base.items() if r.status == CONTENT and cid not in gold_acc}
    ids = sorted(truth, key=lambda c: int(c.split("_")[1]))
    summarize("pessimistic (hide all %d)" % len(ids), att.attribute(cases, hide_content_for=ids), truth)
    half = ids[::2]
    r2 = att.attribute(cases, hide_content_for=half)
    summarize("realistic (hide every other: %d)" % len(half), r2, {c: truth[c] for c in half})
    half2 = ids[1::2]
    r3 = att.attribute(cases, hide_content_for=half2)
    summarize("realistic, other half (%d)" % len(half2), r3, {c: truth[c] for c in half2})

    print("\n== 4. the real unresolved population (eval cases without a content-linked account) ==")
    res = Counter()
    sizes = Counter()
    for cid, r in base.items():
        if cid in gold_acc or r.status == CONTENT:
            continue
        res[r.status] += 1
        if r.status == AMBIGUOUS:
            sizes[min(len(r.candidates), 6)] += 1
    print("   ", dict(res), "| ambiguous candidate-set sizes:", dict(sorted(sizes.items())))


if __name__ == "__main__":
    main()
