"""Parsers: evidence file -> :class:`~ghostaudit.evidence.FileResult`.

``parse_evidence_file`` dispatches on file extension. Each ``parse_*_text``
function also accepts plain text so tests can exercise synthetic fixtures
without touching ``dataset/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List

from ..dataset import Dataset, EvidenceFile
from ..evidence import FMT_IMAGE, FMT_JSON, FMT_LOG, FMT_PDF, FMT_SQL, FMT_TXT, FileResult, ParseFailure
from .common import decode_text
from .image_parser import parse_image_bytes
from .json_parser import parse_json_text
from .log_parser import parse_log_text
from .pdf_parser import parse_pdf_file
from .sql_parser import parse_sql_text
from .txt_parser import parse_txt_text

EXTENSION_FORMAT: Dict[str, str] = {
    ".sql": FMT_SQL, ".json": FMT_JSON, ".log": FMT_LOG, ".txt": FMT_TXT, ".pdf": FMT_PDF, ".png": FMT_IMAGE,
}
_TEXT_PARSERS: Dict[str, Callable[..., FileResult]] = {
    FMT_SQL: parse_sql_text, FMT_JSON: parse_json_text, FMT_LOG: parse_log_text, FMT_TXT: parse_txt_text,
}


def parse_evidence_file(dataset_root: Path, ev: EvidenceFile) -> FileResult:
    """Parse one manifest entry. Never raises for bad content: problems become failures."""
    fs_path = Path(dataset_root) / ev.file_path
    fmt = EXTENSION_FORMAT.get(fs_path.suffix.lower())
    if fmt is None:
        res = FileResult(ev.file_id, ev.file_path, "unknown", ev.captured_at, parsed=False, input_units=1, failed_units=1)
        res.failures.append(ParseFailure(ev.file_id, ev.file_path, "$", f"unsupported extension {fs_path.suffix!r}", ""))
        return res
    try:
        data = fs_path.read_bytes()
    except OSError as exc:
        res = FileResult(ev.file_id, ev.file_path, fmt, ev.captured_at, input_units=1, failed_units=1)
        res.failures.append(ParseFailure(ev.file_id, ev.file_path, "$", f"cannot read file: {exc}", ""))
        return res
    if fmt == FMT_IMAGE:
        return parse_image_bytes(ev.file_id, ev.file_path, data, ev.captured_at)
    if fmt == FMT_PDF:
        return parse_pdf_file(ev.file_id, ev.file_path, str(fs_path), ev.captured_at)
    try:
        text = decode_text(data)
    except UnicodeDecodeError as exc:
        res = FileResult(ev.file_id, ev.file_path, fmt, ev.captured_at, input_units=1, failed_units=1)
        res.failures.append(ParseFailure(ev.file_id, ev.file_path, f"byte:{exc.start}", f"not valid UTF-8: {exc.reason}", ""))
        return res
    return _TEXT_PARSERS[fmt](ev.file_id, ev.file_path, text, ev.captured_at)


def parse_all(ds: Dataset) -> List[FileResult]:
    """Parse every manifest file, in manifest order (deterministic)."""
    return [parse_evidence_file(ds.root, ev) for ev in ds.evidence_files.rows]
