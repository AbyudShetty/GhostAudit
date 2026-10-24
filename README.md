# Ghost Data Still Exists - deletion-audit agent

HackerRank Orchestrate, October 2026. For every request in `dataset/deletion_requests.csv` this program decides whether the person actually asked for deletion, reconstructs what data still exists at the case's `audit_at` time, and writes `output.csv` (`case_id,deletion_status,steps_json`) with the findings, a safe ordered remediation plan, and the checks that must pass.

The full challenge contract is in `problem_statement.md` (and `AGENTS.md` for the logging rules). This file explains how to set up, run, test and package the solution. It replaces the organizers' starter README; that version is still in version control (`git show 11576c9:README.md`).

* It is **deterministic and uses no language model**: no API keys, no network, no prompts. See `code/evaluation/usage_report.md`.
* **No official score is claimed.** The hidden answers are not available; every accuracy figure in `code/evaluation/evaluation_report.md` comes from our own local scorer against the 32 supplied samples.

## 1. Requirements and setup

* Python 3.10 or newer. Developed and tested with Python 3.10.0 on Windows 11 only: the run-time code is standard-library Python with no OS-specific calls, but it has not been run on other systems.
* One third-party package, `pdfplumber`, for reading PDF evidence. Everything else is the standard library.

```text
python -m venv .venv                       # optional; keep the venv outside code/
.venv\Scripts\activate                     # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r code/requirements.txt
```

The dataset is **not** part of `code.zip`. Put the supplied `dataset/` folder next to `code/` (so the tree is `README.md`, `code/`, `dataset/`), or pass its location with `--dataset PATH`. The program reads `deletion_requests.csv`, `user_identities.csv`, `user_audit_log.csv`, `system_activity.csv`, `deletion_rules.csv`, `evidence_files.csv` and everything under `evidence/`. The sample files (`sample_deletion_requests.csv`, `sample_output.csv`) are used only by the local scorer and tests, never to make predictions.

## 2. Run

From the repository root (the folder that contains `code/` and `dataset/`):

```text
python code/main.py
```

This processes all 518 requests and writes `output.csv` in the same root folder (about 3 seconds). It prints the row count, the status counts and how many COMPLETE cases had no directly provable step. The file is written only after it passes the output-contract validator, and never half-written.

Other commands (all work from the repository root or from `code/`; a relative `--dataset` or `--out` path is resolved against the **current directory**, so from `code/` the dataset is `../dataset`):

| Command | What it does |
|---|---|
| `python code/main.py submit [--out PATH]` | same as the bare command; `--out` chooses another file |
| `python code/main.py predict --out PATH` | the same 518 predictions, written to PATH. Refuses to write the root `output.csv` |
| `python code/main.py predict --samples --out PATH` | predictions for the 32 sample requests |
| `python code/main.py validate-samples` | validate the supplied sample answers against the output contract |
| `python code/main.py inventory [--json]` | parse every evidence file and report counts and failures |
| `python code/main.py --dataset PATH ...` | use a dataset in another location (read-only) |
| `python code/main.py ... --emit-proofs KIND` | also emit COMPLETE proofs that rest on one kind of inference: `inferred-identity`, `scoped-snapshot` or `document-or-ocr` (repeatable; all off by default, see section 6) |
| `python code/main.py ... --emit-inferred-proofs` | shorthand for all three `--emit-proofs` kinds |

A wrong dataset path, or a `deletion_rules.csv` that differs from the treatments the program applies, stops with a clear message and exit code 2 (nothing is written).

## 3. Output contract (summary of `problem_statement.md`)

`output.csv` has the header `case_id,deletion_status,steps_json` and exactly one row per request (518 data rows).

* `deletion_status` is `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`. A request that is not a deletion request (access, correction, export, marketing opt-out) is `NOT_APPLICABLE` with `steps_json` = `[]`.
* `steps_json` is an ordered JSON array. Each step has exactly `step_no`, `phase`, `system_id`, `resource_id`, `operation`, `state`, `evidence_id`; numbering starts at 1 and is consecutive. Phases run FINDING, then REMEDIATION, then VERIFICATION; within a phase steps follow the operation order of the problem statement, then `system_id`, then `resource_id`.
* Findings, and the verification steps of a COMPLETE case, cite a `file_id`, `activity_id` or `audit_id` captured at or before the case's `audit_at`. Remediation steps and the future verification steps of an INCOMPLETE case use an empty `evidence_id`.
* **Row order is a judgement, not a fact.** The statement only says "sorted by `case_id`". Rows are ordered by case number (`case_33` before `case_100`), the order of `dataset/deletion_requests.csv` and of every other organizer table whose ids mix widths; a plain text sort would put `case_100` first and differs in all 518 positions. The materials cannot settle which was meant (evidence for both readings: `code/docs/design_decisions.md`, section 1). To switch, set `CASE_ORDER = "lexicographic"` in `code/ghostaudit/schema.py`; `tests/test_row_order.py` shows the rows themselves do not change.

## 4. Test and evaluate

```text
cd code
python -m unittest discover -s tests -t .
```

Tests that need the real dataset skip themselves when `dataset/` is not next to `code/`; run them with the dataset in place for the full suite.

```text
python code/analysis/m5_evaluation.py                      # status/finding/remediation/verification vs the 32 samples; validity of all 518 rows
python code/evaluation/custom_cases.py                     # six extra synthetic evaluation cases
python code/main.py predict --samples --out samples.csv
python code/evaluation/main.py --pred samples.csv          # local scorer: case (status), row, step, field and sequence accuracy
```

Results, the 19 sample verification steps we deliberately do not emit, and the 3 COMPLETE evaluation cases with no directly provable step are described in `code/evaluation/evaluation_report.md`.

## 5. How it works

Each stage is a separate module in `code/ghostaudit/`, so a claim can be traced to the evidence that supports it. Design notes are in `code/docs/`.

1. **Parse evidence** (`parsers/`): logs (seven dialects), JSON, text records, SQL snapshots (executed with SQLite), PDFs and screenshots become typed observations. Screenshot text comes from a shipped OCR cache (`ghostaudit/data/screenshot_ocr.json`), so no OCR runs at run time. Raw `.json` data is not visible to automated code review, so the cache is described here and in `usage_report.md`: it holds the Windows-OCR text of the 11 supplied screenshots, keyed by image checksum. Personal-looking values in it that the pipeline never uses were replaced with same-shape synthetic ones (`code/tools/scrub_ocr_cache.py`; a full-pipeline comparison on all 550 cases showed identical results). `python code/tools/build_ocr_cache.py` (Windows) rebuilds the verbatim cache.
2. **Resolve identity** (`identity.py`): current and historical e-mails, phones and hashes; conflicting claims become AMBIGUOUS, never a guess.
3. **Link content to the subject** (`linker.py`, `evidence_index.py`): a record belongs to the subject only through a grounded identifier. Evidence captured after `audit_at` never decides a case.
4. **Reconstruct state** (`state.py`, `scenario.py`, `attribution.py`): the state of each resource at `audit_at`, whether it is relevant to this case's scenario, and which account the deletion worker acted on.
5. **Classify** (`intent.py`, `classification.py`, `absence.py`): is deletion requested; apply `deletion_rules.csv`; COMPLETE / INCOMPLETE / NOT_APPLICABLE, with a separate `determination` (PROVEN / SUPPORTED / INFERRED / PARTIAL) saying how strongly it is supported.
6. **Build the steps** (`plan.py`): findings (cited), remediation ordered so data flows and restores are stopped before records change, and verification. In a COMPLETE case only steps backed by direct, citable evidence are emitted. Every step is checked by `schema.py` (numbering, phase/operation/state, canonical order, evidence conventions) before anything is written.

## 6. Decisions you should know about

* **A step is emitted only if the evidence supports it.** Findings cite a `file_id`/`activity_id`/`audit_id` captured at or before `audit_at`. COMPLETE verification steps cite the evidence of the compliant state. Remediation and INCOMPLETE verification steps carry an empty `evidence_id`.
* **Inferred identity is not proof.** A COMPLETE proof can rest on three kinds of inference: an account known only from deletion-worker activity, absence read from a subject-scoped snapshot, or state read from document/OCR text. Each can support a COMPLETE status, but none produces a verification step by default. Each kind has its own switch (`--emit-proofs`). A proof that rests on two kinds, such as snapshot absence of an activity-attributed account, needs both switches, so a direct state reading never stands in for an uncertain owner. Unresolved accounts get no step; they are never guessed. The comparison of these policies is in section 6 of the evaluation report (`code/analysis/proof_policy_comparison.py`).
* **Uncertain evidence cannot establish a violation.** Screenshots tied to a case only by capture time, or conflicting with other evidence, are ignored; screenshot and PDF readings that are grounded in the catalog are usable (MEDIUM confidence).
* **A case with nothing directly provable is still COMPLETE if nothing contradicts completeness**, with `determination=PARTIAL` and possibly an empty `steps_json` (three evaluation cases). This is a labelling policy (`ClassificationPolicy.unproven_status`), not proof of completion.
* Switches and their measured effects are listed in section 6 of the evaluation report.
* **Treatments come from `deletion_rules.csv`, checked at every run.** The rules' conditions are prose, so each rule is implemented in `code/ghostaudit/state.py` (`RULE_ACTIONS`). Before anything is predicted, the program compares the file's category and action pairs with what it applies. If they differ it stops with exit code 2 rather than silently ignore a changed policy. Two treatments are not spelled out in the file: data flows must be disabled (AGENTS.md 6.5), and a backup archive, which `rule_14` retains when restore access can be blocked, is checked for a blocked restore.
* No organizer-only file and no sample answer is read by the prediction code.

## 7. Package `code.zip`

```text
python code/tools/package.py                  # builds <repo>/code.zip, then re-opens and verifies the written archive
python code/tools/package.py --verify code.zip --dataset dataset
```

The archive contains `README.md` and `code/` (sources, tests, tools, analysis scripts, design notes, requirements and the two reports in `code/evaluation/`), the same layout as the starter's `zip -r code.zip README.md code ...` command. It excludes the dataset, virtual environments, caches, nested archives, `output.csv`, `log.txt` and the generated per-case sample reports. The verifier checks the archive itself: required files, forward-slash names, no nested archives or dataset file names, no credentials or local paths, every Python file compiles, the two reports contain their required topics, and (with `--dataset`) that dataset names, e-mails, phones or hashes appear only in the few files where they are intentional.

Prefer the script to the starter's PowerShell command: on Windows PowerShell 5.1, `Compress-Archive -Path README.md, code` writes backslash path separators (tested here: 85 of 86 entries), which extract incorrectly on Linux and macOS, and it also packs `__pycache__` and the per-case reports.

## 8. Submission checklist

* `output.csv` at the repository root: produced by `python code/main.py`; exact header, 518 rows, valid `steps_json` arrays with consecutive numbering, citations as described in section 3.
* `code.zip`: produced and verified by `python code/tools/package.py`; contains the runnable code, `README.md`, `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`.
* `chat_transcript`: the root `log.txt`, in the format required by `AGENTS.md`. It is not part of `code.zip`.
* Agent trace: follow `agent_trace_instructions.md` (a separate `agent-trace.zip`, reviewed for secrets first).
* No API keys, credentials or real personal data anywhere; the program never reads environment variables.

## 9. Repository layout

```text
README.md
code/
  main.py                 entry point
  requirements.txt
  ghostaudit/             the solution (pipeline stages above)
  tests/                  unit, integration and CLI tests
  evaluation/             evaluation_report.md, usage_report.md, custom_cases.py, main.py (local scorer)
  analysis/               scripts that regenerate the measurements quoted in the docs
  docs/                   design notes
  tools/                  package.py; scrub_ocr_cache.py; build_ocr_cache.py + ocr_windows.ps1 (one-off OCR cache builder, Windows only)
```
