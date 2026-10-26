"""Dataset + parser inventory: counts, failures and accounting, per file and in total."""

from __future__ import annotations

import json
from collections import Counter, OrderedDict
from typing import Any, Dict, List

from .dataset import Dataset, evidence_disk_check, sorted_case_ids
from .evidence import FileResult
from .schema import case_sort_key


def build_inventory(ds: Dataset, results: List[FileResult]) -> Dict[str, Any]:
    missing_on_disk, unlisted = evidence_disk_check(ds)
    files: List[Dict[str, Any]] = []
    for r in results:
        files.append(
            OrderedDict(
                file_id=r.file_id, path=r.path, fmt=r.fmt, captured_at=r.captured_at, unit_label=r.unit_label,
                input_units=r.input_units, observations=len(r.observations), non_evidence_units=r.non_evidence_units,
                failed_units=r.failed_units, failures=len(r.failures), warnings=len(r.warnings),
                containers=len(r.containers), empty_containers=sum(1 for c in r.containers if c.record_count == 0),
                accounting_ok=r.accounting_ok() if r.fmt != "pdf" else True,
                dialects=r.meta.get("dialects"), interpreted=r.parsed,
            )
        )
    by_fmt: Dict[str, Counter] = {}
    for f in files:
        c = by_fmt.setdefault(f["fmt"], Counter())
        c["files"] += 1
        for k in ("input_units", "observations", "non_evidence_units", "failed_units", "failures", "warnings"):
            c[k] += f[k]
    requests = ds.requests.rows
    return OrderedDict(
        tables=OrderedDict((t.name, OrderedDict(rows=len(t.rows), issues=len(t.issues))) for t in ds.tables()),
        sample_output_rows=len(ds.sample_output),
        request_case_ids=OrderedDict(count=len(requests), first=sorted_case_ids(ds.requests)[0], last=sorted_case_ids(ds.requests)[-1],
                                     file_order_is_natural=[r.case_id for r in requests] == sorted((r.case_id for r in requests), key=case_sort_key)),
        evidence_manifest=OrderedDict(files=len(ds.evidence_files.rows), missing_on_disk=missing_on_disk, unlisted_on_disk=unlisted),
        files=files,
        by_format=OrderedDict((k, dict(v)) for k, v in sorted(by_fmt.items())),
        totals=OrderedDict(
            files=len(files), input_units=sum(f["input_units"] for f in files), observations=sum(f["observations"] for f in files),
            non_evidence_units=sum(f["non_evidence_units"] for f in files), failures=sum(f["failures"] for f in files),
            warnings=sum(f["warnings"] for f in files), images_not_interpreted=sum(1 for f in files if not f["interpreted"]),
        ),
        failures=[str(x) for r in results for x in r.failures],
        warnings=[str(x) for r in results for x in r.warnings],
        dataset_issues=[str(i) for i in ds.issues()],
    )


def format_inventory(inv: Dict[str, Any]) -> str:
    out: List[str] = []
    w = out.append
    w("== Dataset tables ==")
    for name, t in inv["tables"].items():
        w(f"  {name:32s} rows={t['rows']:5d} issues={t['issues']}")
    w(f"  {'sample_output.csv':32s} rows={inv['sample_output_rows']:5d}")
    rc = inv["request_case_ids"]
    w(f"  request ids {rc['first']}..{rc['last']} count={rc['count']} file already in natural order: {rc['file_order_is_natural']}")
    em = inv["evidence_manifest"]
    w(f"  manifest files={em['files']} missing_on_disk={len(em['missing_on_disk'])} unlisted_on_disk={len(em['unlisted_on_disk'])}")
    w("")
    w("== Evidence files ==")
    w(f"  {'file_id':12s} {'fmt':6s} {'unit':22s} {'input':>6s} {'obs':>5s} {'non-ev':>6s} {'fail':>5s} {'warn':>5s}  ok  path")
    for f in inv["files"]:
        ok = "yes" if f["accounting_ok"] and f["failures"] == 0 else "NO "
        w(f"  {f['file_id']:12s} {f['fmt']:6s} {f['unit_label']:22s} {f['input_units']:6d} {f['observations']:5d} "
          f"{f['non_evidence_units']:6d} {f['failures']:5d} {f['warnings']:5d}  {ok} {f['path']}")
    w("")
    w("== By format ==")
    for k, v in inv["by_format"].items():
        w(f"  {k:6s} files={v['files']:3d} input={v['input_units']:6d} observations={v['observations']:5d} "
          f"non-evidence={v['non_evidence_units']:5d} failures={v['failures']} warnings={v['warnings']}")
    t = inv["totals"]
    w("")
    w(f"== Totals == files={t['files']} input_units={t['input_units']} observations={t['observations']} "
      f"non_evidence={t['non_evidence_units']} failures={t['failures']} warnings={t['warnings']} images_not_interpreted={t['images_not_interpreted']}")
    for label in ("dataset_issues", "failures", "warnings"):
        if inv[label]:
            w(f"== {label} ({len(inv[label])}) ==")
            for x in inv[label][:50]:
                w("  " + x)
    return "\n".join(out)


def inventory_json(inv: Dict[str, Any]) -> str:
    return json.dumps(inv, indent=2, ensure_ascii=False, sort_keys=False)
