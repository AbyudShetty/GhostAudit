"""Local scoring harness: compare a predictions CSV against a reference CSV.

IMPORTANT LIMITATION: the official grader and its weights are not published.
These are *our* metrics, defined below, for regression testing against the 32
supplied sample answers (``dataset/sample_output.csv``). A high local score does
not guarantee a high hidden-test score.

Metrics
-------
status_accuracy      fraction of reference cases whose ``deletion_status`` matches.
row_exact            fraction whose status AND complete step list (all 7 fields,
                     order significant) match.
step precision/recall/F1
                     micro-averaged over multisets of ``(phase, system_id,
                     resource_id, operation, state, evidence_id)``;
                     ``identity`` variant drops ``evidence_id``.
field_accuracy       positional: for every reference step ``i`` the predicted step
                     ``i`` is compared field by field; missing steps count wrong.
sequence_exact       fraction of cases whose ordered list of
                     ``(phase, system_id, resource_id, operation, state)`` is equal
                     (evidence ignored); ``sequence_exact_with_evidence`` keeps it.
sequence_lcs         mean over cases of LCS(pred, ref) / max(len) on the same tuples.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schema import STEP_FIELDS, case_sort_key

Case = Tuple[str, List[Dict[str, Any]]]  # (deletion_status, steps)


def load_cases(path: Path) -> Dict[str, Case]:
    """Load an output-format CSV into ``{case_id: (status, steps)}``. UTF-8, newline-safe."""
    out: Dict[str, Case] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                steps = json.loads(row["steps_json"])
            except (json.JSONDecodeError, TypeError, KeyError):
                steps = None
            out[row["case_id"]] = (row["deletion_status"], steps if isinstance(steps, list) else [])
    return out


def _full(s: Dict[str, Any]) -> Tuple:
    return tuple(s.get(f) for f in ("phase", "system_id", "resource_id", "operation", "state", "evidence_id"))


def _ident(s: Dict[str, Any]) -> Tuple:
    return tuple(s.get(f) for f in ("phase", "system_id", "resource_id", "operation", "state"))


def _lcs(a: Sequence, b: Sequence) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, start=1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def _prf(tp: int, fp: int, fn: int) -> Dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": p, "recall": r, "f1": f}


def score(pred: Dict[str, Case], ref: Dict[str, Case]) -> Dict[str, Any]:
    ids = sorted(ref, key=case_sort_key)
    missing = [i for i in ids if i not in pred]
    extra = sorted((i for i in pred if i not in ref), key=case_sort_key)
    n = len(ids)
    status_ok = row_exact = seq_ok = seq_ev_ok = 0
    tp_full = fp_full = fn_full = tp_id = fp_id = fn_id = 0
    field_hits: Counter = Counter()
    field_total = 0
    lcs_sum = 0.0
    confusion: Counter = Counter()
    details: List[Dict[str, Any]] = []
    for cid in ids:
        rs, rsteps = ref[cid]
        ps, psteps = pred.get(cid, ("<MISSING>", []))
        confusion[(rs, ps)] += 1
        status_ok += rs == ps
        row_exact += rs == ps and rsteps == psteps
        rf, pf = [_full(s) for s in rsteps], [_full(s) for s in psteps]
        ri, pi = [_ident(s) for s in rsteps], [_ident(s) for s in psteps]
        seq_ok += ri == pi
        seq_ev_ok += rf == pf
        cf, cp = Counter(rf), Counter(pf)
        tp = sum((cf & cp).values())
        tp_full += tp; fp_full += sum(cp.values()) - tp; fn_full += sum(cf.values()) - tp
        ci, cq = Counter(ri), Counter(pi)
        tpi = sum((ci & cq).values())
        tp_id += tpi; fp_id += sum(cq.values()) - tpi; fn_id += sum(ci.values()) - tpi
        for i, rstep in enumerate(rsteps):
            field_total += 1
            pstep = psteps[i] if i < len(psteps) else {}
            for f in STEP_FIELDS:
                field_hits[f] += pstep.get(f) == rstep.get(f)
        longest = max(len(ri), len(pi))
        lcs_sum += (_lcs(ri, pi) / longest) if longest else 1.0
        if not (rs == ps and rsteps == psteps):
            only_ref = sorted((cf - cp).elements())
            only_pred = sorted((cp - cf).elements())
            details.append(OrderedDict(case_id=cid, ref_status=rs, pred_status=ps, missing_steps=[list(x) for x in only_ref],
                                       unexpected_steps=[list(x) for x in only_pred], same_multiset_wrong_order=(not only_ref and not only_pred)))
    d = n or 1
    return OrderedDict(
        reference_cases=n, predicted_cases=len(pred), missing_cases=missing, extra_cases=extra,
        status_accuracy=status_ok / d, row_exact=row_exact / d,
        step_exact=_prf(tp_full, fp_full, fn_full), step_identity=_prf(tp_id, fp_id, fn_id),
        field_accuracy=OrderedDict((f, field_hits[f] / field_total if field_total else 1.0) for f in STEP_FIELDS),
        field_accuracy_all=sum(field_hits.values()) / (field_total * len(STEP_FIELDS)) if field_total else 1.0,
        sequence_exact=seq_ok / d, sequence_exact_with_evidence=seq_ev_ok / d, sequence_lcs=lcs_sum / d,
        status_confusion=[[a, b, c] for (a, b), c in sorted(confusion.items())],
        mismatched_cases=details,
    )


def format_report(rep: Dict[str, Any], max_details: int = 10) -> str:
    o: List[str] = []
    w = o.append
    w(f"reference cases={rep['reference_cases']} predicted={rep['predicted_cases']} "
      f"missing={len(rep['missing_cases'])} extra={len(rep['extra_cases'])}")
    w(f"status_accuracy        {rep['status_accuracy']:.4f}")
    w(f"row_exact              {rep['row_exact']:.4f}")
    se, si = rep["step_exact"], rep["step_identity"]
    w(f"step (with evidence)   P={se['precision']:.4f} R={se['recall']:.4f} F1={se['f1']:.4f}")
    w(f"step (identity only)   P={si['precision']:.4f} R={si['recall']:.4f} F1={si['f1']:.4f}")
    w("field accuracy (positional): " + " ".join(f"{k}={v:.4f}" for k, v in rep["field_accuracy"].items()) + f" | all={rep['field_accuracy_all']:.4f}")
    w(f"sequence_exact         {rep['sequence_exact']:.4f}  (with evidence {rep['sequence_exact_with_evidence']:.4f})  lcs={rep['sequence_lcs']:.4f}")
    w("status confusion (ref, pred, n): " + "; ".join(f"{a}->{b}:{c}" for a, b, c in rep["status_confusion"]))
    for d in rep["mismatched_cases"][:max_details]:
        w(f"  mismatch {d['case_id']}: status {d['ref_status']}->{d['pred_status']} missing={len(d['missing_steps'])} unexpected={len(d['unexpected_steps'])}"
          + (" (order only)" if d["same_multiset_wrong_order"] else ""))
    if len(rep["mismatched_cases"]) > max_details:
        w(f"  ... {len(rep['mismatched_cases']) - max_details} more mismatched cases")
    return "\n".join(o)
