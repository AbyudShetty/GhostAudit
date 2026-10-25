# Evaluation report

Ghost Data Still Exists - deletion-audit agent. Figures were produced by the commands in section 9 on 2026-10-25 (Python 3.10.0). Each population is run on its own, exactly as the CLI runs it: the 32 samples by `predict --samples`, the 518 requests by `python code/main.py`.

## 0. How to read this report

* **No official score is claimed.** The hidden canonical answers are not available. Every accuracy figure below is computed by our own local scorer (`ghostaudit/scoring.py`) against the 32 supplied sample answers, and it may weigh things differently from the official grader.
* **Samples and evaluation requests are reported separately.** The 32 sample cases have supplied answers, so they can be scored. The 518 evaluation requests have no answers available to us, so for them we report only structure, validity and how often we deliberately withheld a step. Nothing about the 518 is an accuracy figure.
* **The sample answers were used to evaluate, not to predict.** The prediction pipeline never reads `sample_output.csv` (a test pins this: a dataset without that file still produces predictions). Three early rules were informed by sample cases (contact lookup, support contact record, PDF attachment); we measured that they change no evaluation request, see section 6.
* **Status, findings and verification are different claims and are scored separately** (sections 1-3), because a correct status says nothing about whether each step is justified.

## 1. Case-level status accuracy (32 samples)

| | COMPLETE (answer) | INCOMPLETE (answer) |
|---|---|---|
| **COMPLETE (ours)** | 10 | 0 |
| **INCOMPLETE (ours)** | 0 | 22 |

**Status accuracy: 32/32 = 1.0000** (no NOT_APPLICABLE among the samples; the 10 non-deletion requests in the evaluation set are covered by intent tests and custom case A).

How strongly each sample status is supported (our `determination` field, which is never part of `output.csv`):

| Status | Determination | Cases | Meaning |
|---|---|---|---|
| INCOMPLETE | PROVEN | 21 | at least one violation from structured evidence (logs, JSON, SQL, text records) |
| INCOMPLETE | SUPPORTED | 1 | the only violation comes from a PDF (`case_07`) |
| COMPLETE | PROVEN | 3 | every requirement shown by direct evidence |
| COMPLETE | INFERRED | 3 | complete, but some proof rests on a subject-scoped snapshot or an identity inferred from activity |
| COMPLETE | PARTIAL | 4 | `case_01`, `case_02`, `case_04`, `case_28`: nothing contradicts completeness, but not every requirement could be shown |

The 4 PARTIAL cases agree with the answers only because our policy labels an unproven case COMPLETE (`ClassificationPolicy.unproven_status`). That is a labelling default, **not proof of completion**, and it is why the field exists.

## 2. Step-level accuracy (32 samples)

Each phase is compared on (resource, operation); findings and COMPLETE verification are also compared on the cited evidence id.

| Phase | Answer steps | Matched | Missing | Unsupported | Cited id equal |
|---|---|---|---|---|---|
| FINDING | 41 | 41 | 0 | 0 | 41/41 |
| REMEDIATION | 41 | 41 | 0 | 0 | not cited by design |
| VERIFICATION | 111 | 93 | 18 | 0 | 21/21 (steps cited in COMPLETE answers) |
| **All** | **193** | **175** | **18** | **0** | |

Overall step precision 1.0000, recall 0.9067, F1 0.9511. **Unsupported steps: 0.** Every step we emit is in the answer; our only loss is steps we chose not to emit.

### The 18 missing sample verification steps

Two policies shape these figures (section 6). **Identity:** link policy S3k lets a file captured after `audit_at` say who owns an account or a retained record, never what state it was in, and it is never cited. Under the stricter S2 (identity only from files captured by `audit_at`) 9 more answer steps would be missing (`case_09`, `case_18`, `case_23`, `case_24`, `case_26`, `case_27`, `case_29`, `case_30`, `case_32`); no status would change. **Inferred-identity proofs:** an account known only from the deletion worker's activity is verified absent when an eligible lookup shows it gone (`case_21`), unless the attribution itself needed a later file (`case_25`).

| # | Reason we did not emit them | Why this is deliberate |
|---|---|---|
| 9 | The resource is not linked to the subject by any identifier visible in the dataset: 7 anonymized analytics profiles that carry no identifier, and 2 retained audit records (`case_18`, `case_20`) | A step must name a resource we can tie to the subject; guessing would be fabricating. The 2 audit records appear in no participant file at all. An anonymized profile is tied to its case only by the deletion worker's `ANONYMIZE_RECORD` row in the case's batch, and 433 of the 508 evaluation deletion cases share their `reported_at` with another case: that tie would be unique for only 17 of the 129 such rows (re-checked 2026-10-25) |
| 5 | The account could not be named: 3 ambiguous (`case_23`, `28`, `29`; several workers could be the subject's) and 2 unattributed (`case_04`, `08`) | An unresolved account is never guessed. No step is produced. `account_004` is named by no participant file or activity row. `case_08`'s only worker row reports SUCCESS for a PARTIALLY_COMPLETE request; relaxing that result key would recover it but changes no evaluation case (all 252 content-linked evaluation pairs agree with the key), so it would only fit the samples |
| 2 | Proof withheld: `case_25`'s account is unique only because a file captured after `audit_at` rules out a rival account (`later_ownership`); `case_31` rests on a subject-scoped snapshot (switched off) and on the same later ownership | An inferred identity may use only evidence available at `audit_at`; snapshot absence is not observed absence |
| 2 | No evidence names the account at `audit_at` (`case_01`, `case_02`) | The answers cite `evidence_54`, an accounts table that simply does not list these accounts. Non-listing is not accepted as proof of absence (section 8, "Absence"), and the identity of both accounts is inferred from activity, so even with that rule they would be withheld by default |

No switch recovers `case_25` or `case_31`: a proof that rests on later ownership is never emitted.

## 3. Field-level accuracy (32 samples)

Compared position by position over each answer's steps (local scorer). Because a missing step shifts the later steps, the identity fields (`system_id`, `resource_id`, `operation`, `state`) are lower than the field-by-field quality of the steps we did emit.

| Field | Accuracy |
|---|---|
| step_no | 0.9067 |
| phase | 0.9067 |
| system_id | 0.7824 |
| resource_id | 0.7824 |
| operation | 0.8031 |
| state | 0.8031 |
| evidence_id | 0.9067 |
| all fields | 0.8416 |

## 4. Sequence accuracy (32 samples)

* **Metric names.** *Case* accuracy is the status accuracy of section 1 (1.0000). *Row* accuracy is the share of rows whose status and every step (including evidence ids) equal the answer: **17/32 = 0.5312**. *Sequence* accuracy is the share of rows whose ordered list of steps equals the answer's, ignoring evidence ids: **17/32 = 0.5312** as well, because every row whose steps match also cites the same evidence. *Field* accuracy is section 3.
* Exact sequence (every step, in order, per row): **17/32 = 0.5312**.
* Mean per-row longest-common-subsequence ratio (common subsequence length divided by the longer sequence): **0.8132** (`ghostaudit/scoring.py`).
* The 15 inexact rows are all *ordered subsets* of the answer: in each, our steps appear in the answer's order and the only difference is missing steps (answers hold 193 steps, we emit 175 = 193 - 18). No row differs by an extra step or a wrong order. The 15 rows are `case_01`, `02`, `03`, `04`, `08`, `18`, `20`, `21`, `23`, `25`, `26`, `27`, `28`, `29`, `31`.
* Three of them are COMPLETE sample cases for which we emit **no step at all** (`case_01`, `case_02`, `case_04`, where the answers hold 1, 1 and 2 steps) because nothing could be directly proven. If the official grader requires at least one step for a COMPLETE case, these would be penalised; we preferred an empty list to an unsupported step.

## 5. The 518 evaluation requests (no answers available)

Structure and validity only; **none of these is an accuracy figure**.

| Check | Result |
|---|---|
| Rows / header / ordering | 518 data rows, header `case_id,deletion_status,steps_json`, natural numeric case order, one row per request |
| Schema, step numbering, phase/operation/state consistency, canonical order, evidence conventions | 0 violations |
| Citations captured after the case's `audit_at` | 0 |
| Status counts | 353 INCOMPLETE, 155 COMPLETE, 10 NOT_APPLICABLE |
| Determination | INCOMPLETE: 345 PROVEN, 8 SUPPORTED. COMPLETE: 62 PROVEN, 48 INFERRED, 45 PARTIAL |
| Steps | INCOMPLETE: 719 findings, 719 remediation, 1,216 verification (2,654 in total). COMPLETE: 332 verification. NOT_APPLICABLE: none |
| Verification steps withheld | 165 (84 in COMPLETE cases, 81 in INCOMPLETE cases), each recorded with its reason |
| Account step emitted | in 77 of 155 COMPLETE and 273 of 353 INCOMPLETE cases |
| Full run | about 3 seconds, deterministic (two runs, and the same run started from the repository root or from `code/`, are byte-identical) |

Points to be aware of:

* **Three COMPLETE evaluation cases have no directly provable step: `case_45`, `case_55`, `case_63`.** They are COMPLETE because nothing contradicts completeness and the request is a deletion request, but no requirement could be shown with direct evidence, so their `steps_json` is `[]`. This is the same trade-off as section 4. Their rows are listed by the CLI after every run (`COMPLETE with no provable step: 3`).
* 120 deletion requests have no nameable account (108 ambiguous, 12 unattributed); they get no account step.
* **Identity policy (S3k).** A file captured after `audit_at` may say who owns an account or retained record; state and citations come only from evidence captured by `audit_at`. The strict alternative (S2) would remove 160 steps from 135 rows (124 future retained-record checks, 31 account checks or proofs, 5 account checks whose rivals only a later file excluded) and change no status. This reading conflicts with the literal wording of AGENTS.md 6.2; see section 6.
* **Inferred-identity proofs** add 14 COMPLETE verification steps: each cites an eligible lookup that shows the account gone, for an account tied to the subject by the deletion worker's unique row in the request window. One more (`case_351`) is withheld because its attribution needs a later file to rule out a rival (`later_ownership`).
* 8 INCOMPLETE cases are SUPPORTED rather than PROVEN: their violations come from screenshots (read with a cached OCR pass) or a PDF. Screenshots whose link to the subject rests only on the capture-time offset, or that conflict with other evidence, are never used as a violation or a proof (e.g. `tombstone_17`, `marketing_001`).
* Evaluation requests whose request text asks for access, correction, export or marketing opt-out are NOT_APPLICABLE with `[]` (10 cases, `case_533`-`case_542`).

## 6. Where we are uncertain or made a policy choice

| Topic | Choice | What changes if you disagree |
|---|---|---|
| Unproven COMPLETE | Labelled COMPLETE, `determination=PARTIAL` (`unproven_status`). Allowed when the only unproven item is the subject's account, the pattern of the 4 sample cases the answers label COMPLETE. Anything else must rest on an undecided screenshot or on a record tied to the subject only by later files with nothing usable at `audit_at`, which `tests/test_verdict_invariants.py` enforces | `unproven_status="INCOMPLETE"` would turn 45 COMPLETE evaluation cases into INCOMPLETE with no finding; on the samples it would break the 4 PARTIAL agreements. See "Cases beyond the sample pattern" below |
| Identity from later files | Link policy S3k: a file captured after `audit_at` may identify the owner of an account, security-audit record or legal hold (single-subject records, one owner); it never sets state and is never cited. **This conflicts with the literal wording of AGENTS.md 6.2** ("Evidence created later must not change the answer"); we follow AGENTS.md 6.1, which calls the sample answers correct: 9 of them need this, and S3k matches all 9 | S2 (`link_policy="S2"`): 8 fewer sample answer steps (recall 0.9067 -> 0.8653, exact rows 17 -> 13), 152 fewer evaluation steps, no status change |
| Inferred proofs | One switch per kind (`--emit-proofs`); a proof needs every kind it rests on. **Inferred identity: on** (eligible 404 + unique worker row at `audit_at`). Scoped snapshot and document/OCR: off. A proof whose attribution needs a later file (`later_ownership`) is never emitted | See the table below |
| MEDIUM-confidence violations (cached OCR grounded by the catalog, PDF text) | Enabled; LOW-confidence readings never count | `accept_medium_violations=False` would leave the 8 SUPPORTED cases without a violation |
| Subject-scoped snapshot as absence evidence | Accepted as a weaker tier, never as a step | Off: 31 evaluation cases move INFERRED -> PARTIAL; no status changes |
| A retained record that is missing | INCOMPLETE with a future `VERIFY_RECORD_PRESENT` check (empty `evidence_id`), the check that must pass afterwards. There is no finding and no recovery step, because the vocabulary has no operation for either | none available; no supplied case has one |
| Verification in INCOMPLETE cases | Remediated items, the identified account and retained records; not resources that were already compliant and untouched | no sample answer contains such a check |
| Which file to cite when several prove one thing | The most recently captured eligible file, ties to the lower id | matches the answers on 41/41 findings and 21/21 cited verification steps in the samples |
| Row order of `output.csv` | Numeric (`case_33` before `case_100`), the order of `deletion_requests.csv` and of every other organizer table whose ids mix widths. The statement only says "sorted by `case_id`" and no supplied material settles whether text order was meant | a text sort puts `case_100` first and differs in all 518 positions; set `CASE_ORDER = "lexicographic"` in `ghostaudit/schema.py` (rows are identical, only their order changes). Evidence for both readings: `code/docs/design_decisions.md` |
| OCR cache | Pre-computed once with the Windows OCR engine and shipped (`ghostaudit/data/screenshot_ocr.json`), keyed by image checksum, so no OCR is needed to run. Names, e-mails and one phone number in it that no result depends on were replaced with same-shape synthetic values; 4 values stay verbatim because scrubbing any one changes a result (`case_39`, `case_52`, `case_53`, `case_55`) | without the cache screenshot evidence is skipped and the 8 SUPPORTED cases would change. The scrub is reproducible and checked: statuses, determinations, violations, proofs, gaps and steps of all 550 cases are identical before and after |

### Inferred-proof policies compared (`python code/analysis/proof_policy_comparison.py`)

Sample figures are our scorer against the 32 supplied answers. For the 518 evaluation requests no answers exist, so only the number of added steps is given; it is not an accuracy figure. Every row uses link policy S3k. No policy changes a status or removes a step.

| Policy | Sample step precision / recall / F1 | Sample steps added (all in the answers) | Evaluation steps added |
|---|---|---|---|
| none | 1.0000 / 0.9016 / 0.9482 | - | 0 |
| **inferred identity (default)** | **1.0000 / 0.9067 / 0.9511** | `case_21` `account_6440` | 14 |
| scoped snapshot only | 1.0000 / 0.9016 / 0.9482 | none (`case_31` also rests on later ownership) | 14 |
| identity + OCR | 1.0000 / 0.9067 / 0.9511 | as the default (no sample answer cites a screenshot) | 15 (+1, `case_45`) |
| identity + snapshot | 1.0000 / 0.9067 / 0.9511 | as the default | 35 (14 + 14 + 7 that need both) |
| all three | 1.0000 / 0.9067 / 0.9511 | as the default | 36 |

What each kind rests on, from the script's per-step listing:
* **Inferred identity (14 eval steps, on by default):** the state is direct. An eligible lookup names the account and returns no profile (404), read 8 to 22 days after the account's deletion-worker row. The owner is inferred: no identifier can remain on a deleted profile, so the link is the unique deletion-worker row compatible with the case. In 3 of the 14 (`case_190`, `case_274`, `case_427`) another case could also have taken the account; each is still the only candidate in its own case's list, and the rival case is settled by an identifier captured before the audit. Activity attribution made 0 wrong assignments in the hide-and-recover checks (112, 89 and 85 correct; `code/analysis/account_attribution_eval.py`) and is right for all 12 activity-attributed sample accounts. A proof whose attribution needs a file captured after `audit_at` to rule out a rival (`later_ownership`: sample `case_25`, eval `case_351`) is never emitted, so every emitted inferred proof rests only on evidence available at `audit_at`.
* **Scoped snapshot (off; 21 eval steps if on):** absence is not observed. It is read from a bundle that lists the subject's other records but no profile. These bundles are partial: 3 of them leave out accounts that other files show present within 24 hours, although none of those accounts' owners is covered by that bundle. For the 14 whose account has an identifier link, the link comes only from a file captured after `audit_at`. In 13 of the 21 the workflow itself reports the account delete as FAILED or PARTIAL.
* **Document or OCR (1):** `audit_record_003` in `case_45` is tied to the subject by the catalog row, the subject reference visible in the screenshot, and a compliance-journal PDF captured 30 minutes earlier naming the same record. The state rule for screenshots ("a record shown in the console exists") is not validated by any sample.

### Cases beyond the sample pattern (`tests/test_verdict_invariants.py`)

Three COMPLETE/PARTIAL evaluation cases leave something other than the account unproven. For each such resource, either the only usable evidence is one screenshot that does not decide its requirement, or the resource is tied to the subject only by a file captured after `audit_at` and nothing about it is usable at `audit_at`. None of them shows a violation, so none can be a finding. They are a policy question, not a defect:
* `case_46` and `case_55` also carry a security-audit record (`audit_record_1797`, `audit_record_2857`) that only a log captured after `audit_at` ties to the subject; its state at `audit_at` is unknowable, so its presence cannot be shown.
* `case_46`, `tombstone_17` (redaction): the screenshot shows "Retained reference" and no identifiers, which would be compliant. It is tied to the record only by the catalog's capture-time offset, though, so it cannot prove anything.
* `case_55`, `backup_9287` (restore must be blocked): the screenshot lists the backup but not whether it can be restored. A log captured after `audit_at` says restore is allowed; it cannot be used. This is the one case where a contract condition ("no restore path can recreate prohibited data") stays unestablished.
* `case_63`, `marketing_001`: the screenshot shows the record under someone else's identifiers, so it is not established that the record belongs to the subject. No requirement can be applied.

## 7. Additional evaluation cases (created for this project)

Six synthetic scenarios with fictional identifiers, run through the real pipeline (`code/evaluation/custom_cases.py`, also asserted by `tests/test_custom_cases.py`). The expected output of each was written by hand from the specification before running it, including the full ordered step list for case C. **All 7 case results (case E has two requests) agree with their expectation.**

| Case | Scenario | Expected outcome |
|---|---|---|
| A | Subject asks what data is held (an access request) | NOT_APPLICABLE, `[]` |
| B | Clean deletion; account explicitly absent; required audit record present | COMPLETE with exactly two cited verification steps |
| C | Five copies remain: a search record, a support ticket, an analytics profile, an active sync flow, a restorable backup | INCOMPLETE; 5 cited findings; remediation ordered disable flow, block restore, delete, redact, anonymize; 7 uncited checks; 17 steps in canonical order |
| D | A later log (captured after `audit_at`) shows the account back | COMPLETE, answer unchanged and never cites the later file |
| E | Two subjects with two indistinguishable worker events; subject 1 also has a residual search record | no account step for either subject; subject 1 INCOMPLETE with its record found and remediated; subject 2 COMPLETE with only the audit record verified |
| F | An account named only by a worker event and a 404 captured before `audit_at` | COMPLETE; the account is verified absent citing the 404 (inferred identity, all evidence available at `audit_at`), next to the directly proven audit record |

## 8. Failures found and improvements made

| Where | Failure found | Improvement |
|---|---|---|
| Evidence parsing | An early independent count check reported 79 false mismatches; the checking helper, not the parser, was wrong | The check was redone independently, found 0 mismatches, and the counts were pinned in tests |
| Identity | Requiring an identifier to be "current at `audit_at`" left 103 request e-mails unresolved | Identity uses "known by `audit_at`" so historical addresses resolve; conflicting claims become AMBIGUOUS instead of a guess |
| Screenshots | E-mail addresses of other people visible in a screenshot were scanned as the subject's identifiers, giving a data flow a second owner | Foreign addresses are counted, not stored as identifiers |
| State | An over-aggressive "stale evidence" exclusion discarded valid readings | Exclusion now needs positive evidence of a different timeline |
| Account attribution | A faulty "forced move" rule attributed accounts wrongly | Rewritten so only simultaneous forced moves attribute; ambiguity is preserved |
| Intent | Accent stripping destroyed Hangul, leaving 12 Korean deletion requests UNCERTAIN | Accents are stripped for Latin letters only; 508 requests are DELETION, 10 NON_DELETION, 0 UNCERTAIN |
| Absence | Treating "not listed in a profile table" as proof of absence | Measured: 7 of 9 present accounts were missing from the nearest snapshot. Replaced with tiers (explicit record, subject-scoped snapshot, nothing) and never used as a step |
| Steps | Mutation checks on `plan.py` left 4 surviving mutants (unproven requirements, de-duplication, NOT_APPLICABLE emission, counted vs all violations) | Tests with hand-built verdicts added; all 17 mutants are now caught |
| CLI | A relative `--dataset` path failed with a raw traceback; `python -m ghostaudit` was unusable from the repository root; the root `output.csv` guard depended on the dataset location | Clear errors with a hint, `code/main.py` entry point, a guard on the real root file, parent directories created, tests from both directories |
| Dataset loading | Predictions needlessly required `sample_output.csv` | Optional; predictions never use it |
| Policy | `deletion_rules.csv` was loaded but never consulted: treatments came from a table in `state.py`. It agreed with the file, but a changed rule would have been ignored silently, and the README said the file drove every treatment | The table (`RULE_ACTIONS`) is compared with the file before every run. Any difference stops the run with exit code 2 and writes nothing, and the two treatments not written in the file (flows, backup restore) are documented. Predictions are byte-identical |
| Account attribution | The result key (COMPLETE/SUCCESS, PARTIALLY_COMPLETE/PARTIAL, NOT_STARTED/FAILED) was also applied to the subject's *own* content-linked account. In sample `case_06` (a partial workflow whose account delete succeeded) that rejected the right account and left a spurious "unattributed" gap | The key now only chooses among unowned accounts. Samples: 27 accounts attributed correctly instead of 26, still 0 wrong. The 518 predictions and the 32 sample predictions are byte-identical, because all 252 content-linked eval pairs agree with the key |
| Tests | One test asserted that the root `output.csv` does not exist, so the suite failed once the submission file had been generated | The CLI tests now check that the file's bytes are unchanged (or that it is still absent) |
| Audit time | An audit (2026-10-25) showed that a file captured after `audit_at` could turn a withheld proof into a "direct" one, flip a status (synthetic) and resolve another case's ambiguous account. We implemented the strict policy (S2) and measured it: no status change on any of the 550 requests, but 9 sample answer steps and 160 evaluation steps lost, all of them tied to the subject only by later files | Kept the reading the sample answers support (S3k, section 6), with per-case attribution, "not used" diagnostics, S2 available as `link_policy="S2"`, and its guarantees tested (`tests/test_audit_time_identity.py`). Under S3k, too, a later file never sets state, is never cited, and never makes an inferred-identity proof possible (`later_ownership`) |
| Latent defects (from an independent review; none occurs in the supplied data, so no prediction changed) | (1) Only the last deletion-worker row per account was kept, so a retry could hide the attempt inside the request window, and the result depended on file order. (2) A record naming two subjects passed a referenced resource to whichever subject's identifier came first. (3) A required retained record shown missing produced no step at all | (1) Every row is kept; for each case the last row inside its window counts, and rows are ordered by time and activity id. (2) The reference goes to every subject the record names, so the resource has competing owners and is AMBIGUOUS. (3) The missing record gets its future `VERIFY_RECORD_PRESENT` check. Tests cover both row orders, a shuffled activity file, both field orders, and the schema validity of the new check |
| Figures | The evaluation script and some tests classified samples and evaluation requests together, so attribution could eliminate candidates across the two populations. Once identity became time-bound, that gave 3 more steps than the submission run | They now run each population on its own, as the CLI does. Every figure here matches `python code/main.py` and `predict --samples` |
| Packaging | The archive verifier could not see backslash entry names (Python converts them when reading on Windows), and the starter's PowerShell zip command writes exactly such names | The verifier reads the original names; packaging uses `code/tools/package.py`, which writes portable names |
| Packaging | The shipped OCR cache and several test fixtures carried dataset names, e-mails and one phone number the code does not need | An experiment on all 550 cases found the pipeline needs only 4 of 13 values in the cache; the rest were replaced (identical results), inline test fixtures now use synthetic values, and the verifier fails on dataset identifiers in any file not explicitly justified. A first experiment wrongly concluded that 3 values were enough because its placeholders had no `@`; the realistic-placeholder rerun caught it (`case_39`) |

## 9. Reproducing every number

From the repository root (the `dataset/` folder next to `code/`):

```text
python code/analysis/m5_evaluation.py                          # sections 1-5 (samples scored; 518 rows validated)
python code/analysis/proof_policy_comparison.py                # section 6, inferred-proof policies compared
python code/evaluation/custom_cases.py                         # section 7
python code/evaluation/main.py --pred <predictions.csv>        # local scorer on any predictions file
python code/main.py predict --samples --out <file>             # sample predictions
python code/main.py predict --out <file>                       # the 518 predictions without touching output.csv
cd code && python -m unittest discover -s tests -t .           # full test suite
```
