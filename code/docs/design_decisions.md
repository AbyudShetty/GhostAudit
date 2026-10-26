# M1 design decisions

Status: Milestone 1 (foundation). Facts below were measured on the real dataset on 2026-10-24
unless marked **[hypothesis]** or **[decision]**. Nothing here classifies cases or writes `output.csv`.

## 1. Row ordering — **[decision]**

`problem_statement.md` and `README.md` say "sorted by `case_id`" without defining the order.
Evidence: `dataset/deletion_requests.csv` is already in natural numeric order (`case_33 … case_550`)
and the 32 sample rows are `case_01 … case_32`; plain string order would put `case_100` before `case_33`.
**Rule:** natural numeric order (`schema.case_sort_key`); unknown id shapes sort last. Deterministic.
Risk: a grader that string-sorts would see the same *set* of rows; most graders key on `case_id`, so the
exposure is low but not zero. Cheap to flip (one function).

## 2. Time rules

| Rule | Source | Implementation |
|---|---|---|
| Evidence captured after `audit_at` may not change the answer or be cited | AGENTS.md 6.2, 6.6 | `timeutil.is_eligible(file_captured_at, audit_at)` |
| Boundary is inclusive | `evidence_51` captured at exactly `case_31.audit_at` and is cited by the sample answer | `<=`; tested |
| "Captured" means the manifest `captured_at`, not line timestamps | `evidence_06` holds lines older than `case_09.audit_at` but is captured after it; the sample cites `evidence_93` instead **[one data point]** | file-level test first, then record-level (`observation_usable`) |
| A record without its own timestamp is judged by its file | nothing may be invented | parsers leave `observed_at=None`; file time is never copied into it |

All 75 evidence citations in the sample answers are eligible under this rule (tested).

## 3. Identity resolution vs. state evidence — **[decision, measured]**

Two different questions that must not be conflated:

* **Identity resolution**: which identifiers (current/historical email, phone, hash, `user_id`) belong to a user,
  and therefore which resources are *associated* with that user. A stable mapping.
* **State evidence**: what a resource looked like at `audit_at`. Must come only from eligible evidence.

`code/analysis/identity_link_probe.py` compares strategies on the samples (S1 strict; S2 identity-full +
eligible observations; S3 as S2 but resource→user *association* may come from any observation):

| | gold resources | linkable S1 | S2 | S3 |
|---|---|---|---|---|
| all systems | 111 | 76 | 76 | **84** |
| account_service | 32 | 11 | 11 | 14 |
| security_service | 24 | 17 | 17 | 22 |

* Request e-mails resolve to exactly one user under every identity strategy (32/32 samples, 518/518 eval);
  rewinding the audit log to `audit_at` changes nothing for the resolution step.
* **8 sample gold resources are reachable only through post-audit evidence** (accounts `account_3960`, `account_5684`,
  `account_3866`; audit records `audit_record_4902/4128/7095/3954/1707`), via `evidence_06` and `evidence_02`
  (captured about 2 days after the audit). For 5 of them (audit records in INCOMPLETE cases) *no* eligible file mentions
  the resource at all, yet the gold answer lists it as a future `VERIFY_RECORD_PRESENT` step with an empty citation.
* Eval: **90 account resources** (90 cases) can be associated with their user only via evidence captured after
  `audit_at` (22 if only record-level timestamps are used). My earlier estimate of "39" came from an exploratory
  script with different timestamp handling and is **not reproducible with the M1 parsers**; use 90/22.
  Of the 90, 81 users also have *some* eligible link; the DELETE_RECORD workflow result for the 90 accounts is
  38 SUCCESS / 20 PARTIAL / 23 FAILED / 9 absent. 262 of 518 cases have no account linkable by content at all
  (even under S3) and depend on activity-timing attribution or on information absent from the files.

**[decision for M2]** Treat identity/association as a stable mapping that may use all supplied metadata
(S3), but enforce eligibility for every *state* observation and every *citation*. This is the only strategy that
reaches the 8 gold resources above. **[hypothesis]** the alternative explanation — those resources are obtained
through activity-timing attribution rather than post-audit links — cannot be excluded with 32 samples; M2
must ablate S2 vs S3 against the samples and report it. Open spec question: "evidence created later must not change
the answer" could be read to forbid S3; the samples argue against that reading.

## 4. Parser contract

* One `Observation` per resource-bearing unit; values are kept as the source wrote them (`"null"` stays a string in
  logs, JSON/SQL NULL stays `None`). No state is inferred; `system_id` is only what the source labels.
* Every input unit is accounted for: `input_units = observations + non_evidence_units + failed_units`
  (JSON: `resource-bearing dicts = observations + failed`). Failures carry file, locator, reason, snippet.
  Recoverable oddities (duplicate keys, bad record timestamp, `record_count` mismatch) are **warnings**; the record is kept.
* Empty containers (e.g. a `customer_profiles` snapshot with zero records, `evidence_51`) and SQL tables without
  `resource_id` (`snapshot_scope`) are preserved (`FileResult.containers`, `meta`), because absence is evidence.
* SQL replicas keep their identity: `Observation.copy` is the table (`customer_data_primary`, `…_read_replica`,
  `…_replica_ap_south`, …) so replication drift can be computed downstream.
* Logs: 7 dialects (logfmt, logfmt with positional level, JSON-lines, bracket, pipe, RFC-5424-style syslog,
  `requestId`+nested `message`). One observation per `resource_id` line; sibling lines of the same `trace_id`
  (interleaved, anywhere in the file) are attached as `trace_events` so the `lookup_key`/`subject_ref` logged by a
  `*_started` line stays linked to the `resource_id` of the `*_completed` line.
  `record_lookup` lines report `system=` (audited system) separately from `service=` (reporter); `system_id` prefers the former.
* Encoding: files are decoded as UTF-8 (BOM tolerated), never cp1252; CRLF and LF are equivalent (the Windows
  checkout has CRLF because `core.autocrlf=true`; tests cover both). CLIs reconfigure stdout to UTF-8.
* PDFs have a text layer (pdfplumber, already installed): one document observation per file. PNGs: header only
  (size, sha256); **not interpreted**.

## 5. Independent verification of parser counts

`tests/expected_counts.py` holds per-file counts measured with raw byte/line counting (no parser code), plus the
log dialect of every line from a separate survey regex. Parser output equals them for all 94 files
(4,187 input units → 3,220 observations, 967 non-evidence units, 0 failures, 0 warnings).
Differences from the first exploratory inventory (2,954 observations) are methodological, not parser bugs:
that script merged a trace into one record (the `resource_lookup_trace_*` logs emit a `*_started` *and* a
`*_completed` line per resource, both carrying `resource_id`, so M1 counts 2 observations per lookup), excluded
PDFs, and treated `support_replica.log`'s two same-trace resource lines as one.

## 6. Scoring harness (our metrics, not the official grader)

`ghostaudit/scoring.py` / `evaluation/main.py`: status accuracy, exact-row, step P/R/F1 (with and without
`evidence_id`), positional field accuracy, exact-sequence and LCS sequence accuracy, status confusion and per-case
diffs. The official grader's weights are unknown; a local score is a regression signal only.

## 7. Screenshot dependency (deterministic findings only; no image content was machine-read)

11 PNGs ↔ 12 eval cases (33, 34, 35, 39, 45, 46, 50, 52, 53, 55, 61, 63), linked through 12 `LINK_RESOURCE_IDENTITY`
catalog rows (11 images + the `evidence_18` PDF). For 10 of the 12 cases no *eligible* text/structured evidence mentions
the screenshot's resource, so the screenshot would be the only state evidence (35, 39, 50, 33/52 `export_1260`, 53, 55,
46, 61, 63); cases 34 and 45 also have eligible text evidence (`evidence_01`, `evidence_55`). `evidence_18`
(account-history PDF) is eligible for `case_33` but captured *after* `case_52`'s audit (same e-mail) — a deliberate
time-boundary trap. Open: what the screenshots show beyond what the catalog row says (state fields such as
enabled/active/restorable, e-mails, ids) — to be determined before deciding between OCR and a vision call.
