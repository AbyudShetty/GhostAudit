"""Compare identity-linking policies against the supplied sample answers and the eval set.

    python code/analysis/link_policy_comparison.py                      # print summary
    python code/analysis/link_policy_comparison.py --report out.md      # also write the per-case markdown report

Metrics are *resource association* metrics: found = resources associated with the case's user under a
policy; gold = every ``resource_id`` in the sample answer's steps. Gold lists scenario-relevant resources
only, so precision here is a conservative lower bound on link correctness (a true association that the gold
does not list counts as a false positive; such cases are classified in section 4).

Activity-timing numbers are *measurements* of how far timing alone could go; no timing inference feeds the
linker (kept separate on purpose).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.evidence_index import EvidenceIndex  # noqa: E402
from ghostaudit.identity import build_identity_graph  # noqa: E402
from ghostaudit.linker import DEFAULT_POLICY, POLICIES, build_link_index  # noqa: E402
from ghostaudit.parsers import parse_all  # noqa: E402
from ghostaudit.schema import case_sort_key, system_for_resource  # noqa: E402
from ghostaudit.timeutil import parse_ts  # noqa: E402

DEFAULT_DATASET = CODE.parent / "dataset"
TIMING_WINDOW_MIN = (-90, 15)  # minutes around reported_at where sample gold accounts were deleted (measured offsets -75..+8)
SCENARIO_WINDOW_H = 3.0


def group(rid: str) -> str:
    s = system_for_resource(rid)
    return "account" if s == "account_service" else "audit_record" if s == "security_service" else "other"


def prf(tp: int, fp: int, fn: int) -> Tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


class Ctx:
    def __init__(self, dataset: Path):
        self.ds = load_dataset(dataset)
        self.graph = build_identity_graph(self.ds)
        self.results = parse_all(self.ds)
        self.man = self.ds.evidence_by_id()
        self.idx = build_link_index(self.graph, self.results, self.man, self.ds.activity.rows)
        self.ev = EvidenceIndex(self.results, self.man, self.ds.activity.rows)
        self.gold = {r["case_id"]: {s["resource_id"] for s in json.loads(r["steps_json"])} for r in self.ds.sample_output}
        self.acts: Dict[str, list] = defaultdict(list)
        for a in self.ds.activity.rows:
            self.acts[a.resource_id].append(a)
        self.cases_per_user: Dict[str, List[str]] = defaultdict(list)
        for q in self.ds.requests.rows + self.ds.sample_requests.rows:
            self.cases_per_user[self.graph.resolve_request_email(q.email).user_id].append(q.case_id)

    def user(self, q) -> str:
        return self.graph.resolve_request_email(q.email).user_id

    def found(self, q, policy) -> Tuple[Set[str], Set[str]]:
        assoc = self.idx.associations(self.user(q), q.audit_at, policy)
        return {a.resource_id for a in assoc if a.status == "ASSOCIATED"}, {a.resource_id for a in assoc if a.status == "AMBIGUOUS"}

    def worker_hours_from_reported(self, rid: str, q):
        rep = parse_ts(q.reported_at)
        hs = [(parse_ts(a.event_time) - rep).total_seconds() / 3600 for a in self.acts.get(rid, ()) if a.source_system == "deletion_worker"]
        return min(hs, key=abs) if hs else None


def evaluate(ctx: Ctx, policy) -> dict:
    tot = Counter()
    grp: Dict[str, Counter] = defaultdict(Counter)
    exact = superset = 0
    ambiguous = 0
    for q in ctx.ds.sample_requests.rows:
        A, amb = ctx.found(q, policy)
        G = ctx.gold[q.case_id]
        ambiguous += len(amb)
        for r in A & G:
            tot["tp"] += 1; grp[group(r)]["tp"] += 1
        for r in A - G:
            tot["fp"] += 1; grp[group(r)]["fp"] += 1
        for r in G - A:
            tot["fn"] += 1; grp[group(r)]["fn"] += 1
        exact += A == G
        superset += A >= G
    return dict(total=tot, groups=grp, exact=exact, superset=superset, ambiguous=ambiguous)


def m1_baseline(ctx: Ctx) -> Dict[str, dict]:
    """The M1 exploratory probe's linking, for the 'before' column (same gold, same metric)."""
    import identity_link_probe as probe

    ids = probe.Identities(ctx.ds)
    rows = probe.build_link_table(ctx.ds, ctx.results)
    out = {}
    for s in ("S1", "S2", "S3"):
        tot = Counter(); exact = 0
        for q in ctx.ds.sample_requests.rows:
            uid = ctx.user(q)
            A = set(probe.linked_resources(rows, ids, uid, q.audit_at, s))
            G = ctx.gold[q.case_id]
            tot["tp"] += len(A & G); tot["fp"] += len(A - G); tot["fn"] += len(G - A); exact += A == G
        out[s] = dict(total=tot, exact=exact)
    return out


def classify_miss(ctx: Ctx, rid: str, q, policy) -> str:
    mentions = ctx.ev._by_resource.get(rid, [])
    has_act = bool(ctx.acts.get(rid))
    links = [l for l in ctx.idx.links_for_resource(rid) if l.user_id == ctx.user(q)]
    if links:
        reasons = {ctx.idx.admissible(l, q.audit_at, policy)[1] for l in links}
        return "LINKED_BUT_POLICY_REJECTED(" + ",".join(sorted(reasons)) + ")"
    if not mentions and not has_act:
        return "UNOBSERVABLE: id appears in no participant-visible file or activity row"
    if not mentions:
        return "ACTIVITY_ONLY: appears only in system_activity (needs activity-timing attribution)"
    elig = ctx.ev.citable_ids(rid, q.audit_at)
    return f"EVIDENCE_WITHOUT_IDENTIFIER: seen in {sorted({o.file_id for o in mentions})} (eligible: {elig}) but the record carries no identifier of the user"


def classify_extra(ctx: Ctx, rid: str, q, policy) -> str:
    h = ctx.worker_hours_from_reported(rid, q)
    others = [c for c in ctx.cases_per_user[ctx.user(q)] if c != q.case_id]
    if h is not None and abs(h) > SCENARIO_WINDOW_H:
        return f"OTHER_SCENARIO: deletion-worker activity {h:+.0f}h from this case's reported_at; user also has cases {others}"
    if h is None and others:
        return f"OTHER_SCENARIO(probable): no worker activity near this case; user also has cases {others}"
    return "UNEXPLAINED"


def timing_candidates(ctx: Ctx, rid: str, q) -> Tuple[bool, int]:
    """(is the resource itself inside the timing window, number of competing deletion-worker account deletes in the window)."""
    rep = parse_ts(q.reported_at)
    lo, hi = TIMING_WINDOW_MIN
    cands = [a.resource_id for a in ctx.ds.activity.rows
             if a.source_system == "deletion_worker" and a.target_system == "account_service" and a.event_type == "DELETE_RECORD"
             and lo <= (parse_ts(a.event_time) - rep).total_seconds() / 60 <= hi]
    return rid in cands, len(set(cands))


def fmt_pct(x: float) -> str:
    return f"{x:.3f}"


def print_summary(ctx: Ctx, report_path) -> None:
    ev = {n: evaluate(ctx, p) for n, p in POLICIES.items()}
    base = m1_baseline(ctx)
    print("== 1. Policies on the 32 sample cases (resource associations vs gold resources; 111 gold resources) ==")
    print("   policy  TP  FP  FN   P      R      F1     exact-set  superset  ambiguous")
    for n in ("S1", "S2", "S3", "S3c", "S3k"):
        e = ev[n]; t = e["total"]; p, r, f = prf(t["tp"], t["fp"], t["fn"])
        print(f"   {n:6s} {t['tp']:3d} {t['fp']:3d} {t['fn']:3d}  {fmt_pct(p)}  {fmt_pct(r)}  {fmt_pct(f)}  {e['exact']:2d}/32      {e['superset']:2d}/32     {e['ambiguous']}")
    print("   by group [TP/FP/FN]:")
    for n in ("S1", "S2", "S3", "S3c", "S3k"):
        g = ev[n]["groups"]
        print(f"   {n:6s} " + "  ".join(f"{k}={g[k]['tp']}/{g[k]['fp']}/{g[k]['fn']}" for k in ("account", "audit_record", "other")))
    print("\n== 1b. Before M2 (M1 exploratory probe) vs after (linker) ==")
    for n in ("S1", "S2", "S3"):
        b = base[n]["total"]; a = ev[n]["total"]
        bp = prf(b["tp"], b["fp"], b["fn"]); ap = prf(a["tp"], a["fp"], a["fn"])
        print(f"   {n}: before TP={b['tp']} FP={b['fp']} FN={b['fn']} P={bp[0]:.3f} R={bp[1]:.3f} F1={bp[2]:.3f} exact={base[n]['exact']}/32"
              f"  |  after TP={a['tp']} FP={a['fp']} FN={a['fn']} P={ap[0]:.3f} R={ap[1]:.3f} F1={ap[2]:.3f} exact={ev[n]['exact']}/32")

    print("\n== 2. Policy disagreements on samples (resource-level) ==")
    n_dis = 0
    for q in ctx.ds.sample_requests.rows:
        sets = {n: ctx.found(q, p)[0] for n, p in POLICIES.items()}
        for r in sorted(set().union(*sets.values())):
            v = "".join("Y" if r in sets[n] else "-" for n in POLICIES)
            if len(set(v)) > 1:
                n_dis += 1
                u = ctx.user(q)
                links = ctx.idx.explain(u, r, q.audit_at, POLICIES["S3"])
                d = "; ".join(f"{l.link_type}:{l.source_id}@{l.source_time[:10]} ({l.identifier_kind}/{l.identifier_relation})" for l, _, _ in links)
                print(f"   {q.case_id} audit={q.audit_at[:10]} {r:18s} [{'/'.join(POLICIES)}]={v} gold={'Y' if r in ctx.gold[q.case_id] else 'n'}  {d}")
                print(f"        eligible evidence mentioning it: {ctx.ev.citable_ids(r, q.audit_at) or 'none'}; S3k verdicts: " +
                      ", ".join(sorted({ctx.idx.admissible(l, q.audit_at, POLICIES['S3k'])[1] for l, _, _ in links})))
    print(f"   ({n_dis} resources where policies differ)")

    print("\n== 3. Misses under the recommended policy (%s) ==" % DEFAULT_POLICY)
    cls = Counter(); acc_timing = Counter()
    pol = POLICIES[DEFAULT_POLICY]
    for q in ctx.ds.sample_requests.rows:
        A, _ = ctx.found(q, pol)
        for r in sorted(ctx.gold[q.case_id] - A):
            c = classify_miss(ctx, r, q, pol)
            cls[c.split(":")[0].split("(")[0]] += 1
            if group(r) == "account":
                inwin, n = timing_candidates(ctx, r, q)
                acc_timing[("in_window", inwin, "competitors<=%d" % min(n, 5))] += 1
    print("   ", dict(cls))
    print("    accounts among misses: timing window [%d,%+d] min around reported_at ->" % TIMING_WINDOW_MIN, dict(acc_timing))

    print("\n== 4. Extras (false positives vs gold) under %s ==" % DEFAULT_POLICY)
    for q in ctx.ds.sample_requests.rows:
        A, _ = ctx.found(q, pol)
        for r in sorted(A - ctx.gold[q.case_id]):
            print(f"   {q.case_id} {r:16s} {classify_extra(ctx, r, q, pol)}")

    print("\n== 5. Eval set (518 cases): exposure to the policy choice ==")
    dif = Counter(); post_total = Counter(); cases_changed = set(); no_assoc = []; amb_total = 0
    for q in ctx.ds.requests.rows:
        s2, _ = ctx.found(q, POLICIES["S2"]); s3, _ = ctx.found(q, POLICIES["S3"]); s3k, amb = ctx.found(q, POLICIES["S3k"])
        amb_total += len(amb)
        for r in s3 - s2:
            post_total[system_for_resource(r)] += 1
        for r in s3 - s3k:
            dif[system_for_resource(r)] += 1; cases_changed.add(q.case_id)
        if not s3k:
            no_assoc.append(q.case_id)
    print("    post-audit-only associations (S3 \\ S2) by system:", dict(post_total))
    print("    removed by S3k relative to S3 (mutable resources identified only after audit):", dict(dif), "in", len(cases_changed), "cases")
    print("    cases with ZERO associations under S3k:", len(no_assoc), "-> non-deletion requests + any user with no identifiable resource;",
          "non-deletion cases among them:", sum(1 for c in no_assoc if int(c.split('_')[1]) in range(533, 543)))
    print("    ambiguous associations (resource claimed by >1 user):", amb_total)
    print("    users with several cases (scenario-mixing risk):", {u: c for u, c in ctx.cases_per_user.items() if len(c) > 1})
    iss = ctx.idx.issues
    print("    link issues: ambiguous identifiers=%d, shared records=%s, unresolved e-mails in evidence=%d (all decoy-style: %s), unread screenshots=%d"
          % (len(iss.ambiguous_identifiers), [(s, l) for s, l, _ in iss.shared_records], len(iss.unresolved_emails),
             sorted(iss.unresolved_emails)[:3], len(iss.unread_images)))
    if report_path:
        write_report(ctx, report_path)


def write_report(ctx: Ctx, path: str) -> None:
    pol = POLICIES[DEFAULT_POLICY]
    lines = ["# M2 per-case resource-association report (32 sample cases)", "",
             f"Policy: **{DEFAULT_POLICY}** — {pol.description}. Generated by `code/analysis/link_policy_comparison.py`; "
             "regenerate rather than edit. *Gold* = every resource_id in the supplied sample answer.", ""]
    for q in ctx.ds.sample_requests.rows:
        A, amb = ctx.found(q, pol)
        G = ctx.gold[q.case_id]
        assoc = {a.resource_id: a for a in ctx.idx.associations(ctx.user(q), q.audit_at, pol)}
        res = ctx.graph.resolve_request_email(q.email)
        lines += [f"## {q.case_id} — {q.email} → {res.user_id} ({res.match}, {res.relation})",
                  f"audit_at {q.audit_at}; gold {len(G)}, found {len(A)}, correct {len(A & G)}, missing {len(G - A)}, extra {len(A - G)}, ambiguous {len(amb)}"
                  + ("; **exact set**" if A == G else ""), ""]
        for r in sorted(A & G):
            a = assoc[r]
            tag = "post-audit-only" if a.post_audit_only else "eligible source"
            lines.append(f"- ok `{r}` — {'+'.join(a.link_types)} via {', '.join(a.sources)} ({tag})")
        for r in sorted(G - A):
            lines.append(f"- MISSING `{r}` — {classify_miss(ctx, r, q, pol)}")
        for r in sorted(A - G):
            lines.append(f"- EXTRA `{r}` — {classify_extra(ctx, r, q, pol)}")
        for r in sorted(amb):
            lines.append(f"- AMBIGUOUS `{r}` — competing users {assoc[r].competing_users}")
        lines.append("")
    lines += eval_section(ctx, pol)
    Path(path).write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"\nwrote {path}")


def eval_section(ctx: Ctx, pol) -> List[str]:
    """Eval-set flags. There is no gold here: these are *structural* signals, not accuracy numbers."""
    non = {f"case_{i}" for i in range(533, 543)}  # the 10 explicit non-deletion requests
    no_account, far, cat_far, post_acct, no_audit = [], [], [], [], []
    for q in ctx.ds.requests.rows:
        if q.case_id in non:
            continue
        assoc = ctx.idx.associations(ctx.user(q), q.audit_at, pol)
        systems = {a.system_id for a in assoc}
        if "account_service" not in systems:
            no_account.append(q.case_id)
        if "security_service" not in systems:
            no_audit.append(q.case_id)
        for a in assoc:
            h = ctx.worker_hours_from_reported(a.resource_id, q)
            if h is not None and abs(h) > SCENARIO_WINDOW_H:
                (cat_far if "CATALOG" in a.link_types else far).append((q.case_id, a.resource_id, h))
            if a.post_audit_only and a.system_id == "account_service":
                post_acct.append(q.case_id)
    n_amb = sum(1 for q in ctx.ds.requests.rows for a in ctx.idx.associations(ctx.user(q), q.audit_at, pol) if a.status == "AMBIGUOUS")
    out = ["# Eval set (508 deletion cases; no gold available) — structural flags", "",
           f"- ambiguous associations (resource claimed by several users): {n_amb}",
           f"- cases with **no account association** by content ({len(no_account)}): the account can only come from activity-timing attribution (not implemented in M2): " + ", ".join(no_account),
           f"- cases with **no audit-record association** ({len(no_audit)}): " + (", ".join(no_audit) or "none"),
           f"- cases whose account is identified **only by post-audit evidence** ({len(post_acct)}): " + ", ".join(post_acct), "",
           f"- associations with deletion-worker activity > {SCENARIO_WINDOW_H:g}h from this case's reported_at, **not catalog-linked** ({len(far)} in {len({c for c, _, _ in far})} cases): "
           "NEEDS REVIEW, not necessarily wrong: every case here is a screenshot case (33-63) whose scenario is defined by an image not yet read, or a user with several scenarios; "
           "deciding relevance is M3 work", ""]
    out += [f"  - {c} `{r}` ({h:+.0f}h)" for c, r, h in far]
    out += ["", f"- the same signal on **catalog-linked** resources ({len(cat_far)}): these are the screenshot scenarios' intended resources (linked by catalog rows, "
            "whose scenario evidence is the screenshot, not worker timing), so the signal is *not* evidence of another scenario here", ""]
    out += [f"  - {c} `{r}` ({h:+.0f}h)" for c, r, h in cat_far]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--report", help="write the per-case markdown report here")
    args = ap.parse_args(argv)
    print_summary(Ctx(args.dataset), args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
