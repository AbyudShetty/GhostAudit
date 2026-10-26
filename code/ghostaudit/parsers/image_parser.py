"""PNG screenshots: header validation only.

Screenshots are *not* interpreted in M1 (no OCR / vision calls). The parser only
checks the PNG signature and reads width/height from the IHDR chunk with the
standard library, so the inventory can prove each file is a readable image and
report exactly which files still need interpretation.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Optional

from ..evidence import FMT_IMAGE, FileResult, ParseFailure

_SIG = b"\x89PNG\r\n\x1a\n"


def parse_image_bytes(file_id: str, path: str, data: bytes, captured_at: Optional[str] = None) -> FileResult:
    res = FileResult(file_id, path, FMT_IMAGE, captured_at, parsed=False, unit_label="images", input_units=1)
    if data[:8] != _SIG or data[12:16] != b"IHDR" or len(data) < 24:
        res.failures.append(ParseFailure(file_id, path, "header", "not a valid PNG (signature/IHDR mismatch)", ""))
        res.failed_units = 1
        return res
    width, height = struct.unpack(">II", data[16:24])
    res.meta.update(width=width, height=height, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                    status="not interpreted (deferred: screenshot content needs OCR/vision)")
    res.non_evidence_units = 1
    return res
