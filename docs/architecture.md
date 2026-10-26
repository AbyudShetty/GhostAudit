# Architecture and audit policy

GhostAudit is a deterministic deletion-audit engine. It reads the supplied evidence, produces an audit and remediation plan, and does not execute that plan against external systems. The runtime uses Python's standard library and `pdfplumber`; it makes no model calls or network requests.

## Evidence pipeline

1. **Parse evidence.** `code/ghostaudit/parsers/` turns JSON, text records, seven log dialects, SQLite-compatible SQL exports, PDFs and screenshot text into typed observations. SQL replicas retain their table identity so replication drift remains visible.
2. **Resolve identity.** `identity.py` reconstructs current and historical emails, phones, hashes and stable user references. Conflicting ownership is treated as ambiguous.
3. **Link resources.** `linker.py` and `evidence_index.py` connect records through their contents and grounded identifiers. The evidence manifest supplies paths and capture times, not case membership.
4. **Reconstruct state.** `reconstruction.py`, `state.py`, `scenario.py` and `attribution.py` establish resource state at each request's `audit_at`, scenario relevance and the account targeted by deletion activity. A workflow's reported success is not proof of deletion.
5. **Apply policy.** `intent.py`, `classification.py` and `absence.py` distinguish deletion requests from other requests and apply category-specific deletion, redaction, anonymization and retention rules.
6. **Plan and validate.** `plan.py` emits findings, ordered remediation and verification. `schema.py` checks the output before an atomic write. Flows and unsafe restores are stopped before dependent records change.

## Screenshot extraction

The shipped cache, `code/ghostaudit/data/screenshot_ocr.json`, contains text extracted from the 11 challenge screenshots with Windows.Media.Ocr during development. Entries are matched by image checksum; no OCR service runs during prediction. Identifiers unused by the pipeline were replaced with synthetic values, with identical predictions across all 550 sample and evaluation cases.

`code/tools/build_ocr_cache.py` rebuilds the original cache on Windows. `code/tools/scrub_ocr_cache.py` checks or replaces unused identifiers. The cache is an intentional runtime asset and must remain in version control.

## Current policy and its limits

The final candidate uses **S3k**, with inferred-identity proofs enabled and scoped-snapshot and document/OCR proofs disabled.

- S3k permits later files to establish ownership of accounts, security-audit records and legal holds. Later files never establish historical state and are never cited. This differs from a strict reading of the challenge's time cutoff; the decision and measured effect are disclosed in the [evaluation report](../evaluation/evaluation_report.md). `CaseReconstructor(link_policy="S2")` selects the stricter alternative in Python.
- An inferred account-absence proof needs eligible lookup evidence and attribution supported at `audit_at`. The later-ownership guard withholds proofs when a later file was needed to rule out a rival account.
- Findings cite participant-visible evidence available by `audit_at`. Remediation and future verification for incomplete cases have an empty `evidence_id`. Complete cases emit only supported verification steps.
- The classifier defaults to `COMPLETE` when nothing contradicts completeness, even if proof is partial. It records `determination=PARTIAL` internally; three evaluation cases consequently have no emitted steps. This is a classification default, not proof that deletion succeeded.
- The policy file's category/action pairs are checked against the implemented treatments at every run. Unexpected changes stop the run with exit code 2. Conditions expressed as prose remain implemented in Python rather than interpreted dynamically.
- Output rows use numeric case order, matching the supplied request table. The contract does not settle numeric versus lexicographic order. `CASE_ORDER` in `code/ghostaudit/schema.py` controls the choice; the [design decisions](design_decisions.md) explain it.

The [evaluation report](../evaluation/evaluation_report.md) documents missing verification steps, alternatives and failure analysis. [Historical design notes](history/README.md) preserve earlier measurements and superseded decisions.
