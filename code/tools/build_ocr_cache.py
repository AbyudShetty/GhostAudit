"""Build ghostaudit/data/screenshot_ocr.json by running Windows OCR over every PNG in the evidence manifest.

    python code/tools/build_ocr_cache.py [--dataset dataset] [--out code/ghostaudit/data/screenshot_ocr.json]

Windows only (uses tools/ocr_windows.ps1, the OS's built-in engine; nothing is installed and nothing is sent
anywhere). The cache stores the image sha256 so ghostaudit can refuse a cache that no longer matches the image,
and contains only recognised text lines and positions. Output is deterministic for a given Windows OCR build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from ghostaudit.dataset import load_dataset  # noqa: E402

ENGINE = "Windows.Media.Ocr en-US"


def ocr(png: Path) -> list:
    script = Path(__file__).with_name("ocr_windows.ps1")
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Path", str(png)],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise SystemExit(f"OCR failed for {png}: {p.stderr.strip()[:300]}")
    lines = []
    for ln in p.stdout.splitlines():
        parts = ln.split("|", 2)
        if len(parts) == 3 and parts[0].strip().replace(",", "").isdigit():
            lines.append({"x": int(parts[0].replace(",", "")), "y": int(parts[1].replace(",", "")), "text": parts[2].strip()})
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=CODE.parent / "dataset")
    ap.add_argument("--out", type=Path, default=CODE / "ghostaudit" / "data" / "screenshot_ocr.json")
    a = ap.parse_args()
    ds = load_dataset(a.dataset)
    images = {}
    for ev in ds.evidence_files.rows:
        if ev.file_path.lower().endswith(".png"):
            path = a.dataset / ev.file_path
            images[ev.file_id] = {"path": ev.file_path, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "lines": ocr(path)}
            print(f"{ev.file_id} {ev.file_path}: {len(images[ev.file_id]['lines'])} lines")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"engine": ENGINE, "tool": "code/tools/ocr_windows.ps1", "images": images}, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                     encoding="utf-8", newline="\n")
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
