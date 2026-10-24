# Evaluation report

Ghost Data Still Exists - deletion-audit agent. Figures were produced by the commands in section 9 on 2026-10-24 (Python 3.10.0).

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
| VERIFICATION | 111 | 92 | 19 | 0 | 20/20 (steps cited in COMPLETE answers) |
| **All** | **193** | **174** | **19** | **0** | |

Overall step precision 1.0000, recall 0.9016, F1 0.9482. **Unsupported steps: 0.** Every step we emit is in the answer; our only loss is steps we chose not to emit.

### The 19 missing sample verification steps

| # | Reason we did not emit them | Why this is deliberate |
|---|---|---|
| 9 | The resource is not linked to the subject by any identifier visible in the dataset: 7 anonymized analytics profiles that carry no identifier, and 2 retained audit records (`case_18`, `case_20`) | A step must name a resource we can tie to the subject; guessing would be fabricating |
| 5 | The account could not be named: 3 ambiguous (`case_23`, `28`, `29`; several workers could be the subject's) and 2 unattributed (`case_04`, `08`) | An unresolved account is never guessed. No step is produced |
| 3 | Proof withheld by default: 2 rest on an identity inferred from activity (`case_21`, `25`), 1 on a subject-scoped snapshot (`case_31`) | An inferred identity must not become proof that a resource is absent |
| 2 | No evidence usable at `audit_at` (`case_01`, `case_02`) | Evidence captured later cannot decide the case or be cited |

With `--emit-inferred-proofs` the 3 withheld proofs are emitted and match (verification matched 95, missing 16, all 23 cited ids equal). We do not use it for the submission, for the reason in the table. The choice is a switch, not a hidden assumption.

## 3. Field-level accuracy (32 samples)

Compared position by position over each answer's steps (local scorer). Because a missing step shifts the later steps, the identity fields (`system_id`, `resource_id`, `operation`, `state`) are lower than the field-by-field quality of the steps we did emit.

| Field | Accuracy |
|---|---|
| step_no | 0.9016 |
| phase | 0.9016 |
| system_id | 0.7720 |
| resource_id | 0.7720 |
| operation | 0.7927 |
| state | 0.7927 |
| evidence_id | 0.9016 |
| all fields | 0.8335 |

## 4. Sequence accuracy (32 samples)

* **Metric names.** *Case* accuracy is the status accuracy of section 1 (1.0000). *Row* accuracy is the share of rows whose status and every step (including evidence ids) equal the answer: **17/32 = 0.5312**. *Sequence* accuracy is the share of rows whose ordered list of steps equals the answer's, ignoring evidence ids: **17/32 = 0.5312** as well, because every row whose steps match also cites the same evidence. *Field* accuracy is section 3.
* Exact sequence (every step, in order, per row): **17/32 = 0.5312**.
* Mean per-row longest-common-subsequence ratio (common subsequence length divided by the longer sequence): **0.8028** (`ghostaudit/scoring.py`).
* The 15 inexact rows are all *ordered subsets* of the answer: in each, our steps appear in the answer's order and the only difference is missing steps (answers hold 193 steps, we emit 174 = 193 - 19). No row differs by an extra step or a wrong order. The 15 rows are `case_01`, `02`, `03`, `04`, `08`, `18`, `20`, `21`, `23`, `25`, `26`, `27`, `28`, `29`, `31`.
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
| Steps | INCOMPLETE: 719 findings, 719 remediation, 1,216 verification (2,654 in total). COMPLETE: 318 verification. NOT_APPLICABLE: none |
| Verification steps withheld | 179 (98 in COMPLETE cases, 81 in INCOMPLETE cases), each recorded with its reason |
| Account step emitted | in 63 of 155 COMPLETE and 273 of 353 INCOMPLETE cases |
| Full run | about 3 seconds, deterministic (two runs, and the same run started from the repository root or from `code/`, are byte-identical) |

Points to be aware of:

* **Three COMPLETE evaluation cases have no directly provable step: `case_45`, `case_55`, `case_63`.** They are COMPLETE because nothing contradicts completeness and the request is a deletion request, but no requirement could be shown with direct evidence, so their `steps_json` is `[]`. This is the same trade-off as section 4. Their rows are listed by the CLI after every run (`COMPLETE with no provable step: 3`).
* 119 deletion requests have no nameable account (108 ambiguous, 11 unattributed); they get no account step.
* 8 INCOMPLETE cases are SUPPORTED rather than PROVEN: their violations come from screenshots (read with a cached OCR pass) or a PDF. Screenshots whose link to the subject rests only on the capture-time offset, or that conflict with other evidence, are never used as a violation or a proof (e.g. `tombstone_17`, `marketing_001`).
* Evaluation requests whose request text asks for access, correction, export or marketing opt-out are NOT_APPLICABLE with `[]` (10 cases, `case_533`-`case_542`).

## 6. Where we are uncertain or made a policy choice

| Topic | Choice | What changes if you disagree |
|---|---|---|
| Unproven COMPLETE | Labelled COMPLETE, `determination=PARTIAL` (`unproven_status`) | `unproven_status="INCOMPLETE"` would turn 45 COMPLETE evaluation cases into INCOMPLETE; on the samples it would break the 4 PARTIAL agreements |
| Inferred proofs | Withheld from COMPLETE steps | `--emit-inferred-proofs`: +37 COMPLETE steps (318 -> 355), empty COMPLETE cases 3 -> 2, sample verification matched 92 -> 95 |
| MEDIUM-confidence violations (cached OCR grounded by the catalog, PDF text) | Enabled; LOW-confidence readings never count | `accept_medium_violations=False` would leave the 8 SUPPORTED cases without a violation |
| Subject-scoped snapshot as absence evidence | Accepted as a weaker tier, never as a step | Off: 31 evaluation cases move INFERRED -> PARTIAL; no status changes |
| A retained record that is missing | INCOMPLETE, but no finding, because the vocabulary has no operation for it | none available |
| Verification in INCOMPLETE cases | Remediated items, the identified account and retained records; not resources that were already compliant and untouched | no sample answer contains such a check |
| Which file to cite when several prove one thing | The most recently captured eligible file, ties to the lower id | matches the answers on 41/41 findings and 20/20 cited verification steps in the samples |
| Row order of `output.csv` | Numeric (`case_33` before `case_100`), the order of `deletion_requests.csv` and of every other organizer table whose ids mix widths. The statement only says "sorted by `case_id`" and no supplied material settles whether text order was meant | a text sort puts `case_100` first and differs in all 518 positions; set `CASE_ORDER = "lexicographic"` in `ghostaudit/schema.py` (rows are identical, only their order changes). Evidence for both readings: `code/docs/design_decisions.md` |
| OCR cache | Pre-computed once with the Windows OCR engine and shipped (`ghostaudit/data/screenshot_ocr.json`), keyed by image checksum, so no OCR is needed to run. Names, e-mails and one phone number in it that no result depends on were replaced with same-shape synthetic values; 4 values stay verbatim because scrubbing any one changes a result (`case_39`, `case_52`, `case_53`, `case_55`) | without the cache screenshot evidence is skipped and the 8 SUPPORTED cases would change. The scrub is reproducible and checked: statuses, determinations, violations, proofs, gaps and steps of all 550 cases are identical before and after |

## 7. Additional evaluation cases (created for this project)

Six synthetic scenarios with fictional identifiers, run through the real pipeline (`code/evaluation/custom_cases.py`, also asserted by `tests/test_custom_cases.py`). The expected output of each was written by hand from the specification before running it, including the full ordered step list for case C. **All 7 case results (case E has two requests) agree with their expectation.**

| Case | Scenario | Expected outcome |
|---|---|---|
| A | Subject asks what data is held (an access request) | NOT_APPLICABLE, `[]` |
| B | Clean deletion; account explicitly absent; required audit record present | COMPLETE with exactly two cited verification steps |
| C | Five copies remain: a search record, a support ticket, an analytics profile, an active sync flow, a restorable backup | INCOMPLETE; 5 cited findings; remediation ordered disable flow, block restore, delete, redact, anonymize; 7 uncited checks; 17 steps in canonical order |
| D | A later log (captured after `audit_at`) shows the account back | COMPLETE, answer unchanged and never cites the later file |
| E | Two subjects with two indistinguishable worker events; subject 1 also has a residual search record | no account step for either subject; subject 1 INCOMPLETE with its record found and remediated; subject 2 COMPLETE with only the audit record verified |
| F | An account named only by a worker event and a 404 | COMPLETE; the account is not claimed absent; only the directly proven audit record is verified |

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
| Packaging | The archive verifier could not see backslash entry names (Python converts them when reading on Windows), and the starter's PowerShell zip command writes exactly such names | The verifier reads the original names; packaging uses `code/tools/package.py`, which writes portable names |
| Packaging | The shipped OCR cache and several test fixtures carried dataset names, e-mails and one phone number the code does not need | An experiment on all 550 cases found the pipeline needs only 4 of 13 values in the cache; the rest were replaced (identical results), inline test fixtures now use synthetic values, and the verifier fails on dataset identifiers in any file not explicitly justified. A first experiment wrongly concluded that 3 values were enough because its placeholders had no `@`; the realistic-placeholder rerun caught it (`case_39`) |

## 9. Reproducing every number

From the repository root (the `dataset/` folder next to `code/`):

```text
python code/analysis/m5_evaluation.py                          # sections 1-5 (samples scored; 518 rows validated)
python code/evaluation/custom_cases.py                         # section 7
python code/evaluation/main.py --pred <predictions.csv>        # local scorer on any predictions file
python code/main.py predict --samples --out <file>             # sample predictions
python code/main.py predict --out <file>                       # the 518 predictions without touching output.csv
cd code && python -m unittest discover -s tests -t .           # full test suite
```
