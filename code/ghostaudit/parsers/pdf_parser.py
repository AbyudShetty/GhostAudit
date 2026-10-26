"""PDF business documents -> Observations (text layer only).

The PDFs are ReportLab documents with an extractable text layer (verified for
all four files), so no OCR is involved. ``pdfplumber`` is used because it is
already installed; if it is missing the parser reports that instead of failing
silently.

A PDF has no record structure, so one *document-level* Observation is produced
per file (``resource_id`` is ``None``). It preserves:

* ``pages``  - list of per-page line lists;
* ``resource_ids`` - resource ids literally mentioned;
* ``timestamps`` - ``(label, iso)`` pairs for explicit stamps such as
  ``captured 12 Sep 2026 07 30 UTC``;
* ``label_values`` - ``("Record"|"Detail", text)`` rows exactly as printed.

No identity or state is inferred here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..evidence import FMT_PDF, FileResult, Observation, ParseFailure, mentioned_resource_ids
from ..timeutil import format_ts

_STAMP = re.compile(
    r"\b(?P<label>[A-Za-z]+) (?P<d>\d{1,2}) (?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?P<y>\d{4})"
    r"(?: (?P<h>\d{2}) (?P<mi>\d{2}) UTC)?"
)
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_ROW = re.compile(r"^(Record|Detail) (.+)$")


def _stamps(lines: List[str]) -> List[List[str]]:
    out: List[List[str]] = []
    for ln in lines:
        for m in _STAMP.finditer(ln):
            if m["h"] is None:
                continue  # date-only mentions (order dates, delivery dates) are not capture stamps
            dt = datetime(int(m["y"]), _MONTHS[m["mon"]], int(m["d"]), int(m["h"]), int(m["mi"]), tzinfo=timezone.utc)
            out.append([m["label"].lower(), format_ts(dt)])
    return out


def pdf_pages_text(path: str) -> List[str]:
    import pdfplumber  # noqa: PLC0415 - optional dependency, reported if absent

    with pdfplumber.open(path) as pdf:
        return [(pg.extract_text() or "") for pg in pdf.pages]


def parse_pdf_file(file_id: str, path: str, fs_path: str, captured_at: Optional[str] = None) -> FileResult:
    res = FileResult(file_id, path, FMT_PDF, captured_at, unit_label="documents")
    try:
        pages = pdf_pages_text(fs_path)
    except ImportError as exc:
        res.failures.append(ParseFailure(file_id, path, "$", f"pdfplumber unavailable: {exc}", ""))
        res.input_units = res.failed_units = 1
        return res
    except Exception as exc:  # noqa: BLE001 - any pdf error must be reported, not raised
        res.failures.append(ParseFailure(file_id, path, "$", f"cannot read PDF: {type(exc).__name__}: {exc}", ""))
        res.input_units = res.failed_units = 1
        return res
    res.input_units = 1
    page_lines: List[List[str]] = []
    for i, txt in enumerate(pages, start=1):
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        if not lines:
            res.warnings.append(ParseFailure(file_id, path, f"page:{i}", "page has no extractable text layer", ""))
        page_lines.append(lines)
    flat = [ln for pl in page_lines for ln in pl]
    fields: Dict[str, Any] = {
        "pages": page_lines,
        "resource_ids": mentioned_resource_ids("\n".join(flat)),
        "timestamps": _stamps(flat),
        "label_values": [[m.group(1), m.group(2)] for ln in flat if (m := _ROW.match(ln))],
        "title": flat[0] if flat else "",
    }
    res.observations.append(
        Observation(
            file_id=file_id, fmt=FMT_PDF, locator="$document", resource_id=None, collection="pdf_document",
            fields=fields, raw="\n".join(flat),
        )
    )
    # pages are folded into the single document observation (not one observation per page)
    res.meta["pages"] = len(pages)
    return res
