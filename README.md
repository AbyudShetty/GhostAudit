# GhostAudit

**Evidence-backed data deletion audits.**

GhostAudit checks whether a deletion request was actually fulfilled across databases, replicas, logs, support records, exports, analytics, search indexes and backups. It reconstructs historical identities and resource state at an audit cutoff, then returns cited findings, an ordered remediation plan and verification checks.

Built for **HackerRank Orchestrate 2026 — October edition**, for the challenge **Ghost Data Still Exists**. See [challenge attribution and original materials](docs/challenge/README.md).

The engine is deterministic: it makes no language-model calls, requires no API keys and makes no network requests during an audit. It produces a plan; it does not perform deletions against live systems.

## What it audits

- Whether the request authorizes deletion rather than access, correction, export or marketing opt-out.
- Current and historical identifiers, including email addresses, phone numbers, hashes and stable user references.
- Evidence of remaining records, exposed identifiers, replication drift and active processes that can recreate data.
- Category-specific deletion, redaction, anonymization, retention and account-tombstone requirements.
- Safe remediation order: stop data flows and block restores before changing dependent records.

Inputs include CSV tables, JSON, text, logs, SQLite-compatible SQL snapshots, PDFs and screenshots. Screenshot text comes from a shipped, checksum-matched OCR cache.

## Quick start

Use **Python 3.10+**. The original environment was Windows 11 with Python 3.10.0; other platforms have not been independently validated. `pdfplumber` is the only direct third-party runtime dependency.

Create a virtual environment:

```sh
python -m venv .venv
```

Activate it in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or in macOS/Linux shells:

```sh
source .venv/bin/activate
```

Install the dependency and run from the repository root:

```sh
python -m pip install -r code/requirements.txt
python code/main.py
```

Place the organizer-supplied `dataset/` directory next to `code/` first. It is an external input, excluded from the public source tree and submission archive. The run reads the supplied evidence and writes **`output.csv` at the repository root**, with one row for each of the 518 evaluation requests. Use `--dataset PATH` before the subcommand to read data elsewhere:

```sh
python code/main.py --dataset path/to/dataset submit
```

No environment variables or credentials are needed.

## Output

```csv
case_id,deletion_status,steps_json
```

`deletion_status` is `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation and verification. Non-deletion requests return `NOT_APPLICABLE` and `[]`.

Each step includes `step_no`, `phase`, `system_id`, `resource_id`, `operation`, `state` and `evidence_id`. Findings and complete-case verification cite supplied evidence. Proposed remediation and future verification for incomplete cases use an empty citation. Output is validated before it is written atomically.

## Validate and evaluate

```sh
python code/main.py validate-samples
python evaluation/custom_cases.py
python code/main.py predict --samples --out local/samples.csv
python evaluation/main.py --pred local/samples.csv
```

Run the full test suite:

```sh
cd code
python -m unittest discover -s tests -t .
```

Tests requiring the supplied dataset skip when it is absent. Return to the repository root before running the remaining examples.

The final candidate's local evaluation against the **32 public sample answers** reports:

| Metric | Result |
| --- | --- |
| Deletion-status accuracy | 32/32 (100%) |
| Step precision / recall / F1 | 1.0000 / 0.9067 / 0.9511 |
| Exact ordered step sequences | 17/32 (53.12%) |
| Extra steps absent from sample answers | 0 |
| Synthetic evaluation results | 7/7 across six scenarios |
| Final-run model calls / tokens / API cost | 0 / 0 / USD 0.00 |

These are local sample metrics, **not an official HackerRank score**. Reference answers for the 518 evaluation requests are unavailable. The [evaluation report](evaluation/evaluation_report.md) explains all metrics and missing steps; the [usage report](evaluation/usage_report.md) documents final-run usage.

## Audit policy and limitations

The final candidate uses S3k identity linking, with inferred-identity proofs enabled and snapshot and document/OCR proofs disabled. S3k permits later evidence to establish some ownership links while excluding it from historical state and citations. This conflicts with a strict reading of the challenge's temporal rule and is documented openly.

Eighteen sample verification steps are withheld. The classifier can also return `COMPLETE` with partial proof when no evidence contradicts completeness; three evaluation cases consequently have empty step lists. Review these limits before adapting the engine to another dataset or using its output for real deletion decisions.

See [architecture and policy](docs/architecture.md) for the pipeline, proof safeguards, OCR cache and ordering decisions.

## Repository layout

```text
ghostaudit/
├── README.md
├── AGENTS.md                 Coding-agent and logging contract
├── CLAUDE.md                 Points Claude Code to AGENTS.md
├── code/
│   ├── main.py               Terminal entry point
│   ├── requirements.txt      Pinned runtime dependency
│   ├── ghostaudit/           Audit engine, parsers and OCR asset
│   ├── tests/                Unit, integration and regression tests
│   ├── tools/                Packaging and offline OCR utilities
│   └── analysis/             Evaluation and policy-comparison scripts
├── evaluation/               Reports, local scorer and custom cases
├── docs/
│   ├── architecture.md
│   ├── development.md
│   ├── publishing.md
│   ├── design_decisions.md
│   ├── challenge/            Attribution and supplied instructions
│   └── history/              Earlier design milestones
├── dataset/                  External challenge input; ignored
└── local/                    Scratch work and generated reports; ignored
```

## Build the challenge archive

```sh
python code/tools/package.py
python code/tools/package.py --verify code.zip --dataset dataset
```

The archive includes runnable code, documentation and the required reports at `evaluation/evaluation_report.md` and `evaluation/usage_report.md`. Data, predictions, transcripts, virtual environments, caches and nested archives are excluded. `output.csv` and the transcript are separate challenge artifacts.

More commands and reproduction notes are in the [development guide](docs/development.md). The [publishing guide](docs/publishing.md) gives the suggested repository name, tracked-file cleanup and commands to push it yourself.
