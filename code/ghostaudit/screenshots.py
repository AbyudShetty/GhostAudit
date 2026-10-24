"""Screenshot evidence from cached OCR text. Grounded, conservative, and clearly labelled.

What is and is not claimed
--------------------------
* Text comes from the Windows built-in OCR engine, captured once by ``code/tools/build_ocr_cache.py`` into
  ``ghostaudit/data/screenshot_ocr.json`` (so runs elsewhere need no OCR). The cache holds the image sha256
  and is **refused** if it no longer matches the image bytes.
* OCR is noisy (``gmaiI.com``, ``search_42B``, ``user_ll``). It is therefore used only to *confirm* things
  already known, never to discover identities: an image is tied to a resource through a
  ``LINK_RESOURCE_IDENTITY`` catalog row, and OCR text then confirms or fails to confirm it.
* Matching compares **glyph-canonicalised** strings (``l I | ! -> 1``, ``O -> 0``, ``S -> 5``, ``B -> 8``,
  spaces and bullets removed) of one *specific known value* against the OCR text. No other fuzziness.
* Grounding levels: ``catalog+ocr`` (a known id, alias, user ref, e-mail or name from the catalog row
  / identity is visible in the image) is usable; ``catalog_time_only`` (only the systematic +421 s offset
  between a screenshot's capture time and its catalog row ties them) is kept but marked low confidence;
  no catalog row means no observation.
* The *meaning* of what is visible (``state.py:_image``) is a hypothesis: no sample answer uses a screenshot,
  so these rules cannot be validated on samples. Every screenshot-derived state is flagged ``ocr=True``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .dataset import Activity, Dataset, EvidenceFile
from .evidence import EMAIL_RE, USER_RE, FileResult, Observation
from .identity import IdentityGraph
from .schema import RESOURCE_ID_RE, system_for_resource
from .timeutil import parse_ts

CACHE_PATH = Path(__file__).resolve().parent / "data" / "screenshot_ocr.json"
CATALOG_OFFSET_SECONDS = 421  # every catalog row is stamped 7m01s after the artifact it describes (measured: 12 of 12)
STATUS_WORDS = ("Continuous", "Healthy", "Disabled", "Paused", "Stopped", "Ready", "Live", "Retained reference", "Archive", "Active", "Delivered", "Returned")
_GLYPH = str.maketrans({"l": "1", "I": "1", "|": "1", "!": "1", "O": "0", "S": "5", "B": "8", "Z": "2"})
_PAREN = re.compile(r"\(([^()]+)\)")


def canon(text: str) -> str:
    """Glyph-canonical form used on BOTH the OCR text and the known value being looked for."""
    return re.sub(r"[\s·•]+", "", text.translate(_GLYPH)).lower()


@dataclass(frozen=True)
class OcrImage:
    file_id: str
    path: str
    sha256: str
    lines: Tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def load_cache(path: Path = CACHE_PATH) -> Dict[str, OcrImage]:
    if not Path(path).exists():
        return {}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return {fid: OcrImage(fid, v["path"], v["sha256"], tuple(l["text"] for l in v["lines"])) for fid, v in doc["images"].items()}


def verify_cache(cache: Mapping[str, OcrImage], dataset_root: Path) -> List[str]:
    """Problems that make a cached OCR entry untrustworthy (empty list = all entries match their image bytes)."""
    bad = []
    for fid, img in cache.items():
        p = Path(dataset_root) / img.path
        if not p.exists():
            bad.append(f"{fid}: image file missing")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != img.sha256:
            bad.append(f"{fid}: image bytes differ from the OCR'd image (stale cache)")
    return bad


def catalog_for_image(ev: EvidenceFile, activities: Sequence[Activity]) -> List[Activity]:
    """Catalog rows describing this artifact (stamped exactly ``CATALOG_OFFSET_SECONDS`` after its capture)."""
    t = parse_ts(ev.captured_at)
    return [a for a in activities if a.event_type == "LINK_RESOURCE_IDENTITY" and (parse_ts(a.event_time) - t).total_seconds() == CATALOG_OFFSET_SECONDS]


@dataclass(frozen=True)
class Grounding:
    level: str  # "catalog+ocr" | "catalog_time_only"
    matched: Tuple[str, ...]  # which known values were found in the OCR text


def _known_values(row: Activity, graph: IdentityGraph, user_id: Optional[str]) -> Dict[str, str]:
    vals: Dict[str, str] = {"resource_id": row.resource_id}
    for m in _PAREN.finditer(row.details):  # e.g. "object_19 (delivery_confirmation_19.pdf)" -> alias
        a = m.group(1).strip()
        if a and a != row.resource_id and not a.startswith("NST-"):
            vals["alias"] = a
    for m in USER_RE.finditer(row.details):
        vals["user_ref"] = m.group(0)
    for m in EMAIL_RE.finditer(row.details):
        vals["email"] = m.group(0)
    return vals


def ground(row: Activity, img: OcrImage, graph: IdentityGraph, user_id: Optional[str]) -> Grounding:
    text = canon(img.text)
    found = tuple(sorted(k for k, v in _known_values(row, graph, user_id).items() if canon(v) in text))
    return Grounding("catalog+ocr" if found else "catalog_time_only", found)


def visible_identifiers(img: OcrImage, graph: IdentityGraph, user_id: Optional[str]) -> Tuple[str, ...]:
    """Which of the *subject's own* identifiers are visible: kinds only (email / phone / name / user_ref)."""
    if not user_id:
        return ()
    text = canon(img.text)
    out = []
    claims = graph.claims_of(user_id)
    for kind in ("email", "phone", "user_ref"):
        if any(canon(c.raw) in text for c in claims if c.kind == kind):
            out.append(kind)
    if canon(graph.name_of(user_id)) in text:
        out.append("name")
    return tuple(out)


def foreign_emails(img: OcrImage, graph: IdentityGraph, user_id: Optional[str]) -> Tuple[str, ...]:
    """E-mail-looking strings in the OCR text that are not any of the subject's own addresses (list views legitimately show some)."""
    own = {c.norm for c in graph.claims_of(user_id) if c.kind == "email"} if user_id else set()
    own_canon = {canon(c.raw) for c in graph.claims_of(user_id) if c.kind == "email"} if user_id else set()
    found = []
    for m in EMAIL_RE.finditer(img.text):
        if m.group(0).lower() not in own and canon(m.group(0)) not in own_canon:
            found.append(m.group(0))
    return tuple(sorted(set(found)))


def status_words(img: OcrImage) -> Tuple[str, ...]:
    words = set()
    for ln in img.lines:
        for w in STATUS_WORDS:
            if re.search(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", ln):
                words.add(w)
    return tuple(sorted(words))


def screenshot_observations(ds: Dataset, graph: IdentityGraph, cache: Mapping[str, OcrImage]) -> Tuple[List[FileResult], List[str]]:
    """Return one FileResult per cached image with grounded observations, plus human-readable notes on what was skipped."""
    notes: List[str] = []
    out: List[FileResult] = []
    ev_by_id = ds.evidence_by_id()
    problems = verify_cache(cache, ds.root)
    notes += [f"CACHE REFUSED: {p}" for p in problems]
    refused = {p.split(":")[0] for p in problems}
    for fid in sorted(cache):
        if fid in refused or fid not in ev_by_id:
            continue
        ev, img = ev_by_id[fid], cache[fid]
        fr = FileResult(fid, ev.file_path, "image", ev.captured_at, parsed=True, unit_label="images", input_units=1)
        rows = catalog_for_image(ev, ds.activity.rows)
        if not rows:
            notes.append(f"{fid}: no catalog row describes this image; nothing derived")
            fr.non_evidence_units = 1
        for row in rows:
            users = sorted({r.user_ids[0] for i in USER_RE.finditer(row.details) for r in [graph.resolve("user_ref", i.group(0))] if r.status == "RESOLVED"} |
                           {r.user_ids[0] for i in EMAIL_RE.finditer(row.details) for r in [graph.resolve("email", i.group(0))] if r.status == "RESOLVED"})
            uid = users[0] if len(users) == 1 else None
            g = ground(row, img, graph, uid)
            visible = visible_identifiers(img, graph, uid)
            foreign = foreign_emails(img, graph, uid)
            # NOTE: other people's e-mails are deliberately NOT stored in ``fields`` (identifier scanning would link them); only their count is
            # the id/alias is on screen but nothing of the subject is, while someone else's e-mail is: ownership conflict
            conflict = bool(g.matched) and not visible and bool(foreign)
            if not system_for_resource(row.resource_id):
                notes.append(f"{fid}: catalog resource {row.resource_id} has an unknown prefix; skipped")
                continue
            fr.observations.append(Observation(
                file_id=fid, fmt="image", locator=f"ocr:{row.activity_id}", resource_id=row.resource_id, system_id=system_for_resource(row.resource_id),
                collection="screenshot", observed_at=None, source_time=ev.captured_at,
                fields={"visible_identifiers": list(visible), "status_words": list(status_words(img)), "foreign_email_count": len(foreign),
                        "conflict": conflict, "grounding": "conflict" if conflict else g.level, "matched_values": list(g.matched),
                        "catalog_activity": row.activity_id, "user_id": uid, "ocr": True},
                raw=img.text))
            if conflict:
                notes.append(f"{fid}: {row.resource_id} is on screen but none of the catalog subject's identifiers are; it shows {', '.join(foreign)}: ownership conflict, no state derived")
            elif g.level == "catalog_time_only":
                notes.append(f"{fid}: {row.resource_id} tied to the image only by the catalog time offset (no known value readable in OCR): low confidence")
        fr.input_units = max(1, len(rows))
        out.append(fr)
    return out, notes


def attach_catalog_aliases(ds: Dataset, results: List[FileResult]) -> List[str]:
    """Let a PDF evidence a resource that the catalog names only by a document alias.

    ``LINK_RESOURCE_IDENTITY`` rows can read ``export_1842 (NST-2026-0718-1842)``; the PDF statement carries the number but never
    the id. If the alias occurs in a document's text it becomes a *mention* of that resource (``fields['resource_ids']`` gains it,
    ``fields['catalog_alias_matches']`` says why). Only aliases that are not themselves an identifier (user ref, resource id,
    e-mail) qualify, so ``(user_17)`` can never attach a tombstone to a document that merely lists ``user_17``. Mutates the
    observations in place; returns notes.
    """
    notes: List[str] = []
    aliases: List[Tuple[str, str, str]] = []
    for a in ds.activity.rows:
        if a.event_type != "LINK_RESOURCE_IDENTITY" or not system_for_resource(a.resource_id):
            continue
        for m in _PAREN.finditer(a.details):
            alias = m.group(1).strip()
            if alias and alias != a.resource_id and not (USER_RE.fullmatch(alias) or RESOURCE_ID_RE.fullmatch(alias) or EMAIL_RE.fullmatch(alias)):
                aliases.append((alias, a.resource_id, a.activity_id))
    for r in results:
        if r.fmt != "pdf":
            continue
        for o in r.observations:
            text = canon(o.raw)
            for alias, rid, aid in aliases:
                if canon(alias) in text:
                    ids = list(o.fields.get("resource_ids") or [])
                    if rid not in ids:
                        ids.append(rid)
                        o.fields["resource_ids"] = ids
                    o.fields.setdefault("catalog_alias_matches", []).append({"alias": alias, "resource_id": rid, "catalog_activity": aid})
                    notes.append(f"{r.file_id}: document alias {alias!r} grounds {rid} via {aid}")
    return notes
