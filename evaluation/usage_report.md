# Usage report (tokens, model calls and cost)

## Summary

GhostAudit is **deterministic and makes no model calls**. The full-dataset run that produces `output.csv` uses no language model, no embedding model, no OCR service and no network access, so its token usage and cost are zero.

| | Value |
|---|---|
| Requests processed (final full-dataset run) | 518 (`dataset/deletion_requests.csv`) |
| Model providers | none |
| Model names | none |
| Model calls | 0 |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request | 0 |
| Estimated total cost | USD 0.00 |
| Estimated cost per request | USD 0.00 |
| Wall-clock time of the run | about 3 seconds on the development machine (Windows 11, Python 3.10.0), including loading and parsing all evidence |

### Per-model and overall totals

The run uses a single processing path and no models, so the per-model table has no rows; the overall total is the line above.

| Provider | Model | Calls | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|
| (none) | (none) | 0 | 0 | 0 | 0.00 |
| **Overall** | | **0** | **0** | **0** | **0.00** |

## The run this report describes

```text
python code/main.py                 # reads dataset/, writes output.csv at the repository root
```

`code/main.py predict --out <file>` runs the same code path and writes the same bytes to a file you choose; `output.csv` is produced only by `python code/main.py` (or `submit`). The figures above describe that code path. They were measured by running it on all 518 requests with the output directed to a temporary file, and they do not depend on any setting, key or service.

## Why the figures are zero (and how that is enforced)

* The runtime package (`code/ghostaudit/` and `code/main.py`) imports only the Python standard library and `pdfplumber` (local PDF text extraction).
* `tests/test_runtime_purity.py` fails if the runtime package imports any network library (`requests`, `urllib`, `http`, `socket`, ...), any model-provider SDK (`openai`, `anthropic`, ...), or `subprocess`, if it calls `os.system`/`os.popen`/dynamic imports, or if it reads environment variables (so no API key can be consumed).
* No prompts are used at run time. There are no prompt templates in the package because no model is called.

## Things that used a model or a tool outside the run

These are not part of the final run and are listed so the report is complete.

* **Screenshot text.** Application screenshots are read through a cache of text produced **once, during development**, with the operating system's built-in OCR engine (Windows.Media.Ocr, English), by `code/tools/build_ocr_cache.py`. It is a local OS component, not a model API: no tokens, no cost, no network. The cache (`code/ghostaudit/data/screenshot_ocr.json`) is keyed by image checksum and shipped, so the final run needs no OCR at all. Names, e-mails and one phone number in it that no result depends on were replaced with same-shape synthetic values by `code/tools/scrub_ocr_cache.py`; the results on all 550 cases are identical before and after. (As a raw `.json` file it is not visible to automated code review; this paragraph and the README describe it.)
* **AI coding assistant.** The code was developed with Claude Code (model `claude-sonnet-5-5`). That usage built and tested the software; it is not consumed by the software. This repository does not record the development sessions' token counts, so **no figure is given and none is estimated**.

## If a model is added later

Every figure above would need to be re-measured per provider and model (calls, input tokens, output tokens, tokens per request, cost at the provider's published rates). Nothing in this report should be carried over to such a version.
