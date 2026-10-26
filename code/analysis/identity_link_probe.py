"""EXPLORATORY probe (not part of the audit pipeline): identity resolution vs audit-time evidence.

Question: some resources can only be tied to a user by evidence captured *after*
the case's ``audit_at``. How much does the choice of strategy matter, and what do
the 32 supplied sample answers say about the right choice?

Two different things are separated here:

* identity resolution - which identifiers (current/historical email, phone, hash,
  user_id) belong to a user. This is a *stable mapping*, not system state.
* state evidence - what a record looked like at ``audit_at``. This must come only
  from evidence captured at or before ``audit_at`` (spec section 6.2 / 6.6).

Strategies compared (a resource is "linked" when an observation carrying an
identifier of the subject mentions it):

  S1 strict        identifiers as they were known at audit_at (audit log rewound),
                   observations restricted to evidence usable at audit_at,
                   catalog rows restricted to event_time <= audit_at
  S2 identity-full all current+historical identifiers (no time rewinding),
                   observations restricted to evidence usable at audit_at
  S3 link-relaxed  as S2, but the *association* resource->user may come from any
                   observation (even captured after audit_at); the post-cutoff
                   evidence is used for the link only, never for state/citation

Usage:  python code/analysis/identity_link_probe.py [--dataset dataset] [--list-eval]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from ghostaudit.dataset import load_dataset  # noqa: E402
from ghostaudit.evidence import EMAIL_RE, USER_RE, extract_identifiers  # noqa: E402
from ghostaudit.parsers import parse_all  # noqa: E402
from ghostaudit.schema import case_sort_key, system_for_resource  # noqa: E402
from ghostaudit.timeutil import is_eligible, observation_usable, parse_ts  # noqa: E402

DATASET_DEFAULT = Path(__file__).resolve().parents[2] / "dataset"


def norm(kind: str, v: str) -> str:
    return "+" + "".join(c for c in v if c.isdigit()) if kind == "phone" else v.lower()


class Identities:
    def __init__(self, ds):
        self.cur = {i.user_id: i for i in ds.identities.rows}
        self.events: Dict[str, List] = defaultdict(list)
        for a in ds.audit_log.rows:
            self.events[a.user_id].append(a)
        for v in self.events.values():
            v.sort(key=lambda a: parse_ts(a.event_time))

    def identifiers(self, uid: str, at: str = None) -> Set[Tuple[str, str]]:
        """Identifiers of ``uid``. With ``at``: the set known at that time (audit log rewound past ``at``)."""
        i = self.cur[uid]
        now = {"email": i.email, "phone": i.phone, "email_hash": i.email_hash}
        hist: Set[Tuple[str, str]] = set()
        for a in self.events.get(uid, []):
            if at is not None and parse_ts(a.event_time) > parse_ts(at):
                if now.get(a.field_name) == a.new_value:  # undo a change that had not happened yet
                    now[a.field_name] = a.old_value
                continue
            hist.add((a.field_name, norm(a.field_name, a.old_value)))
        if at is not None:
            for a in self.events.get(uid, []):
                if parse_ts(a.event_time) > parse_ts(at):
                    hist.discard((a.field_name, norm(a.field_name, a.new_value)))
        out = {(k, norm(k, v)) for k, v in now.items()} | hist | {("user_ref", i.user_id.lower())}
        return out

    def users_for(self, kind: str, value: str, at: str = None) -> Set[str]:
        key = (kind, norm(kind, value))
        return {u for u in self.cur if key in self.identifiers(u, at)}


def build_link_table(ds, results):
    """resource_id -> list of (user_candidates_by_identifier, file_id, file_captured, obs_time, source)"""
    ev = ds.evidence_by_id()
    rows = []
    for r in results:
        cap = ev[r.file_id].captured_at
        for o in r.observations:
            rids = [x for x in o.resource_ids() if system_for_resource(x)]
            idents = [(i.kind, i.norm) for i in extract_identifiers(o) if i.kind in ("email", "email_hash", "phone", "user_ref")]
            if rids and idents:
                rows.append((rids, idents, o.file_id, cap, o.observed_at, "evidence"))
    for a in ds.activity.rows:
        if a.event_type == "LINK_RESOURCE_IDENTITY":
            idents = [("email", m.group(0).lower()) for m in EMAIL_RE.finditer(a.details)] + [("user_ref", m.group(0).lower()) for m in USER_RE.finditer(a.details)]
            rows.append(([a.resource_id], idents, a.activity_id, a.event_time, a.event_time, "catalog"))
    return rows


def linked_resources(rows, ids: Identities, uid: str, audit_at: str, strategy: str) -> Dict[str, List[str]]:
    at = audit_at if strategy == "S1" else None
    mine = ids.identifiers(uid, at)
    out: Dict[str, List[str]] = defaultdict(list)
    for rids, idents, src, cap, obs_t, kind in rows:
        if not any(i in mine for i in idents):
            continue
        if strategy in ("S1", "S2") and not observation_usable(cap, obs_t, audit_at):
            continue
        for rid in rids:
            out[rid].append(src)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DATASET_DEFAULT)
    ap.add_argument("--list-eval", action="store_true", help="list every eval case whose account link needs post-audit evidence")
    args = ap.parse_args(argv)
    ds = load_dataset(args.dataset)
    ids = Identities(ds)
    rows = build_link_table(ds, parse_all(ds))
    ev = ds.evidence_by_id()

    # --- (1) does the request email resolve under each identity strategy?
    print("== (1) request-email resolution ==")
    for label, rs in (("samples", ds.sample_requests.rows), ("eval", ds.requests.rows)):
        c = Counter()
        for q in rs:
            full = ids.users_for("email", q.email)
            at = ids.users_for("email", q.email, q.audit_at)
            c["resolves (identity-full)"] += len(full) == 1
            c["resolves (as of audit_at)"] += len(at) == 1
            if len(full) == 1 and len(at) != 1:
                c["email not yet assigned at audit_at (change after audit)"] += 1
        print(f"  {label:8s} n={len(rs)} {dict(c)}")

    # --- (2) sample gold resources found under each strategy
    print("\n== (2) sample gold resources linkable by content, per strategy ==")
    gold: Dict[str, Set[str]] = {}
    for r in ds.sample_output:
        gold[r["case_id"]] = {s["resource_id"] for s in json.loads(r["steps_json"])}
    tot = Counter(); per_sys = defaultdict(Counter); only_s3 = []
    for q in ds.sample_requests.rows:
        users = ids.users_for("email", q.email)
        for uid in users:
            found = {s: set(linked_resources(rows, ids, uid, q.audit_at, s)) for s in ("S1", "S2", "S3")}
            for rid in sorted(gold[q.case_id]):
                sysid = system_for_resource(rid) or "?"
                tot["gold resources"] += 1
                per_sys[sysid]["gold"] += 1
                for s in ("S1", "S2", "S3"):
                    if rid in found[s]:
                        tot[s] += 1
                        per_sys[sysid][s] += 1
                if rid in found["S3"] and rid not in found["S2"]:
                    only_s3.append((q.case_id, rid))
    print("  totals:", dict(tot))
    for sysid, c in sorted(per_sys.items()):
        print(f"  {sysid:20s} {dict(c)}")
    print("  linkable only with post-audit evidence (S3 \\ S2):", only_s3 or "none")

    # --- (3) the eval accounts whose only content link is post-audit
    print("\n== (3) eval cases: account resources linkable only via post-audit evidence ==")
    acts_by_res = defaultdict(list)
    for a in ds.activity.rows:
        acts_by_res[a.resource_id].append(a)
    affected = []
    for q in ds.requests.rows:
        for uid in ids.users_for("email", q.email):
            s2 = linked_resources(rows, ids, uid, q.audit_at, "S2")
            s3 = linked_resources(rows, ids, uid, q.audit_at, "S3")
            for rid in sorted(set(s3) - set(s2)):
                if rid.startswith("account_"):
                    srcs = s3[rid]
                    delta = min((parse_ts(ev[x].captured_at) - parse_ts(q.audit_at)).total_seconds() / 3600 for x in srcs if x in ev) if any(x in ev for x in srcs) else None
                    res = sorted({a.reported_result for a in acts_by_res[rid] if a.event_type == "DELETE_RECORD"})
                    affected.append((q.case_id, rid, srcs[:2], delta, res, bool(s2)))
    print(f"  accounts affected: {len(affected)} across {len({a[0] for a in affected})} cases")
    print("  reported DELETE_RECORD results for these accounts:", dict(Counter(tuple(a[4]) for a in affected)))
    print("  cases where another (eligible) link exists for the same user:", sum(1 for a in affected if a[5]))
    ds_hours = sorted(round(a[3], 1) for a in affected if a[3] is not None)
    if ds_hours:
        print(f"  link evidence captured {ds_hours[0]}h .. {ds_hours[-1]}h after audit_at (median {ds_hours[len(ds_hours)//2]}h)")
    print("  link sources:", dict(Counter(s for a in affected for s in a[2] if s.startswith("evidence_"))))
    if args.list_eval:
        for a in sorted(affected, key=lambda x: case_sort_key(x[0])):
            print("   ", a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
