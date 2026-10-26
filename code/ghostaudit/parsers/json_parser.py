"""JSON evidence -> Observations.

Shapes handled (all discovered by inspection of dataset/evidence/records):

* snapshot bundles: ``{"exported_at", "source", "snapshots": [{"captured_at",
  "data_store", "collection", "records": [...]}]}`` (including snapshots whose
  ``records`` list is empty -- preserved as an empty :class:`ContainerInfo`);
* service resource exports: ``{"exported_at", "service_exports": {service:
  {list_name: [records]}}}``;
* flat record files: ``{"exported_at"|"captured_at", "origin"|"source",
  "collection"?, "records": [...]}``.

Every dict carrying a ``resource_id`` is one observation. Dicts nested inside a
record (``hits.documents[]`` inside a search query record, ``recent_activity``
details) stay part of that record; nested dicts that themselves carry a
``resource_id`` are *also* emitted with ``parent`` pointing at the enclosing
record's locator.

Accounting: ``input_units`` is the number of resource-bearing dicts;
``observations + failed_units == input_units``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..evidence import FMT_JSON, ContainerInfo, FileResult, Observation, ParseFailure
from ..timeutil import try_parse_ts
from .common import TIME_KEYS, first_present

_FILE_TIME_KEYS = ("exported_at", "captured_at", "recorded_at")


class _Ctx:
    __slots__ = ("system", "collection", "stated", "parent")

    def __init__(self, system=None, collection=None, stated=None, parent=None):
        self.system, self.collection, self.stated, self.parent = system, collection, stated, parent

    def but(self, **kw) -> "_Ctx":
        c = _Ctx(self.system, self.collection, self.stated, self.parent)
        for k, v in kw.items():
            setattr(c, k, v)
        return c


def parse_json_text(file_id: str, path: str, text: str, captured_at: Optional[str] = None) -> FileResult:
    res = FileResult(file_id, path, FMT_JSON, captured_at, unit_label="resource-bearing objects")
    dup_keys: List[str] = []

    def hook(pairs):
        seen: Dict[str, Any] = {}
        for k, v in pairs:
            if k in seen:
                dup_keys.append(k)
            seen[k] = v  # last wins (json default); reported below
        return seen

    try:
        doc = json.loads(text, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        res.failures.append(ParseFailure(file_id, path, f"char:{exc.pos}", f"invalid JSON: {exc.msg}", text[max(0, exc.pos - 40): exc.pos + 40]))
        return res
    if dup_keys:
        res.warnings.append(ParseFailure(file_id, path, "$", f"duplicate object keys (last value kept): {sorted(set(dup_keys))}", ""))
    if not isinstance(doc, dict):
        res.failures.append(ParseFailure(file_id, path, "$", f"top-level JSON must be an object, got {type(doc).__name__}", ""))
        return res

    for k, v in doc.items():
        if not isinstance(v, (dict, list)):
            res.meta[k] = v
    file_stated = first_present(doc, _FILE_TIME_KEYS)

    def emit(rec: Dict[str, Any], loc: str, ctx: _Ctx) -> None:
        res.input_units += 1
        rid = rec.get("resource_id")
        if not isinstance(rid, str) or not rid.strip():
            res.failures.append(ParseFailure(file_id, path, loc, f"resource_id must be a non-empty string, got {rid!r}", json.dumps(rec)[:160]))
            res.failed_units += 1
            return
        system = None
        for key in ("system", "origin_system"):
            if isinstance(rec.get(key), str) and rec[key]:
                system = rec[key]
                break
        system = system or ctx.system
        stated = first_present(rec, TIME_KEYS)
        if stated and try_parse_ts(stated) is None:
            res.warnings.append(ParseFailure(file_id, path, loc, f"unparseable timestamp {stated!r}", ""))
        res.observations.append(
            Observation(
                file_id=file_id, fmt=FMT_JSON, locator=loc, resource_id=rid, system_id=system,
                collection=ctx.collection, copy=ctx.collection, observed_at=stated, source_time=ctx.stated,
                fields=rec, parent=ctx.parent, raw=json.dumps(rec, ensure_ascii=False, sort_keys=True),
            )
        )

    def nested(node: Any, loc: str, ctx: _Ctx) -> None:
        """Look for resource-bearing dicts inside an already-emitted record."""
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "resource_id":
                    continue
                if isinstance(v, dict) and "resource_id" in v:
                    emit(v, f"{loc}.{k}", ctx)
                    nested(v, f"{loc}.{k}", ctx.but(parent=f"{loc}.{k}"))
                else:
                    nested(v, f"{loc}.{k}", ctx)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, dict) and "resource_id" in v:
                    emit(v, f"{loc}[{i}]", ctx)
                    nested(v, f"{loc}[{i}]", ctx.but(parent=f"{loc}[{i}]"))
                else:
                    nested(v, f"{loc}[{i}]", ctx)

    def records(items: Any, loc: str, ctx: _Ctx) -> None:
        if not isinstance(items, list):
            res.failures.append(ParseFailure(file_id, path, loc, f"expected a list of records, got {type(items).__name__}", ""))
            return
        for i, item in enumerate(items):
            iloc = f"{loc}[{i}]"
            if isinstance(item, dict) and "resource_id" in item:
                emit(item, iloc, ctx)
                nested(item, iloc, ctx.but(parent=iloc))
            else:
                res.failures.append(
                    ParseFailure(file_id, path, iloc, "list item without resource_id outside a record", json.dumps(item, ensure_ascii=False)[:160])
                )

    def wrapper(node: Dict[str, Any], loc: str, ctx: _Ctx) -> None:
        """A dict holding ``records``/``snapshots``/``service_exports``."""
        stated = first_present(node, ("captured_at", "exported_at")) or ctx.stated
        system = node.get("data_store") if isinstance(node.get("data_store"), str) else ctx.system
        collection = node.get("collection") if isinstance(node.get("collection"), str) else ctx.collection
        c2 = ctx.but(system=system, collection=collection, stated=stated)
        if "records" in node:
            n = len(node["records"]) if isinstance(node["records"], list) else 0
            declared = node.get("record_count")
            if isinstance(declared, int) and declared != n:
                res.warnings.append(ParseFailure(file_id, path, loc, f"record_count={declared} but records has {n} items", ""))
            res.containers.append(ContainerInfo(file_id, "json_collection", collection or "records", f"{loc}.records", n, system, stated))
            records(node["records"], f"{loc}.records", c2)
        if "snapshots" in node:
            snaps = node["snapshots"]
            if not isinstance(snaps, list):
                res.failures.append(ParseFailure(file_id, path, f"{loc}.snapshots", "snapshots must be a list", ""))
            else:
                for i, s in enumerate(snaps):
                    if isinstance(s, dict):
                        wrapper(s, f"{loc}.snapshots[{i}]", c2)
                    else:
                        res.failures.append(ParseFailure(file_id, path, f"{loc}.snapshots[{i}]", "snapshot must be an object", ""))
        if "service_exports" in node:
            se = node["service_exports"]
            if not isinstance(se, dict):
                res.failures.append(ParseFailure(file_id, path, f"{loc}.service_exports", "service_exports must be an object", ""))
            else:
                for svc, lists in se.items():
                    if not isinstance(lists, dict):
                        res.failures.append(ParseFailure(file_id, path, f"{loc}.service_exports.{svc}", "service entry must be an object", ""))
                        continue
                    for list_name, items in lists.items():
                        lloc = f"{loc}.service_exports.{svc}.{list_name}"
                        n = len(items) if isinstance(items, list) else 0
                        res.containers.append(ContainerInfo(file_id, "json_collection", f"{svc}.{list_name}", lloc, n, svc, stated))
                        records(items, lloc, c2.but(system=svc, collection=list_name))

    if not any(k in doc for k in ("records", "snapshots", "service_exports")):
        res.failures.append(ParseFailure(file_id, path, "$", "no records/snapshots/service_exports found", ""))
        return res
    wrapper(doc, "$", _Ctx(stated=file_stated))
    return res
