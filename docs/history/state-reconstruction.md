# M3: state reconstruction, scenario relevance, account attribution, screenshots

Labels: **[spec]** = stated in `docs/challenge/problem_statement.md` / `AGENTS.md`; **[data]** = measured (commands at the end);
**[hypothesis]** = not confirmed; **[decision]** = my design choice. M3 builds no steps, decides no COMPLETE/INCOMPLETE and
writes no `output.csv`. A sample score is reported but never used as the justification for a rule (section 6 lists which rules
were informed by samples).

## 1. How it fits what already exists

```
parsers (M1) -> Observation -> linker (M2: who owns it) ----------------------------+
                      |                                                              v
                      +-> state.interpret -> Reading -> StateReconstructor(as_of) -> ResourceState   (what it was, from usable evidence)
activity rows ------> scenario.WorkflowIndex + assess_relevance ----------------> Relevance          (does it concern THIS request?)
                      attribution.AccountAttributor ------------------------------> AccountAttribution (which account, from claims)
                      reconstruction.CaseReconstructor composes all of the above, per case, with an explanation per resource
```
The four questions stay in separate modules: ownership (`identity.py`/`linker.py`), state (`state.py`), relevance
(`scenario.py`), activity attribution (`attribution.py`). `EvidenceIndex` is reused: state reconstruction only ever reads
observations that pass `observation_usable`; everything else is *kept* on the result as `excluded` with a reason.

## 2. Rules settled

### 2.1 State at `audit_at` **[spec: AGENTS 6.2/6.5/6.6]**
* A reading can decide a state or be cited only if its file was captured at/before `audit_at` **and** its own record time (if any) is too. Boundary inclusive.
* Latest usable reading wins (ordering key: record time, else file capture time). At the same instant, copies (primary vs replicas) are merged **worst case first** and the disagreement is noted. **[decision]** one surviving copy means the data still exists.
* Later evidence never changes `state`; it is listed in `ResourceState.excluded` (`file_captured_after_audit` / `record_time_after_audit`) so "why wasn't X used?" is answerable. Nothing is citable for an `UNKNOWN` state.
* Activity rows are *claims*: they never set state; newer worker rows are surfaced as `claims_after_evidence`.
* No usable evidence => `UNKNOWN` (never a guess from later files).
* Record shapes are interpreted by an explicit table (104 shapes, `state.interpret`); **0 of 3,218 readings are unrecognised** [data]. Policy verdicts (`assess`) are a separate function.

### 2.2 Scenario relevance (the M2 problem) **[decision, validated]**
Ownership is not membership. A resource is judged by three independent signals, never a time window alone:

| signal | meaning |
|---|---|
| workflow | deletion-worker rows inside `[requested_at, audit_at]` and within 4 h of `reported_at` (IN_BATCH), in the lifecycle but off batch, only outside it, none |
| freshness | usable sources (state readings + eligible link sources) during the lifecycle (FRESH), only before `requested_at` (STALE), none |
| role | retained records (audit log, legal hold) have no workflow rows by design |

Decision (first match): IN_BATCH → RELEVANT/HIGH; FRESH → RELEVANT/HIGH; retained & unambiguous → RELEVANT/MEDIUM;
**claimed by a sibling case of the same user** (its evidence/workflow lies in another request's lifecycle and none in this
one) → UNRELATED/HIGH; every worker row outside the lifecycle and nothing fresh → UNRELATED/MEDIUM; otherwise UNCERTAIN/LOW
(kept and flagged). RELEVANT is downgraded to UNCERTAIN when ownership is contested (competing owners, or a screenshot showing
the resource under someone else's identifiers).
*Why this shape:* my first version excluded anything stale. That rested on 3 sample resources, all of multi-case users,
and would also have dropped resources of single-case users with no evidence of another timeline. Exclusion now needs
**positive** evidence of another timeline; staleness alone only yields UNCERTAIN. `BATCH_HOURS` (4) is empirical: 238/247 content-linked eval
accounts are IN_BATCH and the 9 exceptions are the other-timeline accounts; changing it to 2 h or 8 h changes nothing [data].

### 2.3 Activity-based account attribution **[decision, validated; claims only]**
Candidates: deletion-worker `DELETE_RECORD` on `account_service`, inside the lifecycle and within −90…+15 min of `reported_at`
(measured offsets −75…+8 over 28 samples and 253/263 content-linked accounts), result compatible with `reported_status`
(COMPLETE→SUCCESS 133/133, NOT_STARTED→FAILED 50/50, PARTIALLY_COMPLETE→PARTIAL 69/70; a *matching key between two claims of one workflow*, not proof of state),
not owned by another user. Unique candidates are assigned and removed from every other case's pool (simultaneous forced moves only, so order cannot matter); several remaining candidates stay `AMBIGUOUS` with the full list; two cases whose only option is the same account are a flagged conflict and neither gets it. **Tested and rejected tie-breakers:** case-id order vs event order holds in 8 of 59 groups; nothing else in the data separates cases that share a `reported_at`.
Confidence: CONTENT/HIGH (identifier link + workflow row), STRONG/MEDIUM (unique by elimination), otherwise none.

### 2.4 Screenshots **[hypothesis for meaning; measured for extraction]**
Environment: `easyocr` is installed but `torch` is missing and its models are PyTorch `.pth` files; `pytesseract` is installed but there is no Tesseract binary; `onnxruntime`/`opencv` exist but there is no ONNX OCR model. **Not usable.**
The Windows built-in OCR engine (`Windows.Media.Ocr`, en-US) *is* present and needs nothing installed; `code/tools/ocr_windows.ps1` +
`build_ocr_cache.py` run it once and store text in `ghostaudit/data/screenshot_ocr.json` (image sha256 included; a stale cache is refused), so a run elsewhere needs no OCR.
OCR is noisy (`gmaiI`, `search_42B`, `user_ll`), so it is used only to **confirm a value already known** from a `LINK_RESOURCE_IDENTITY` catalog row (glyph-canonicalised on both sides; no other fuzziness) and never to discover identities.
Grounding [data]: 9 of 11 images confirmed by readable content, 1 (tombstone) tied only by the catalog's +421 s offset (low confidence), 1 (`marketing_001`) is an **ownership conflict** (the resource id is on screen but the screenshot shows `sj.park@naver.com`, not the catalog subject): no state derived, relevance downgraded.
The same grounding lets a PDF evidence a resource the catalog names only by a document alias (`export_1842 (NST-2026-0718-1842)`; `attach_catalog_aliases`); aliases that are themselves a user ref, resource id or e-mail never qualify.
Meaning rules (`state._image`): a record visible in a live console exists; the subject's identifiers visible = exposed (a stable `user_N` is allowed for support/tombstone, not for analytics); a flow is ACTIVE only with `Continuous`/`Healthy` and no `Disabled`/`Paused`/`Stopped`; a backup listing does not reveal restore access (UNKNOWN). **No sample answer uses a screenshot, so none of these rules can be validated on samples**; every screenshot-derived reading carries `ocr=True` and is flagged in the explanation.

## 3. Cross-check against the written requirements

| Requirement | Where | Status |
|---|---|---|
| State strictly as of `audit_at`; later evidence must not change it (6.2, 6.5) | `StateReconstructor`, `observation_usable` | implemented; tested incl. inclusive boundary, record time after audit, moving `as_of` |
| Never cite evidence captured after `audit_at` (6.6) | `ResourceState.citable`; real-data test over all reconstructed decisions | implemented; 0 violations |
| Activity and `reported_status` are claims (6.5, 6.6) | state never reads them; attribution uses them only as a matching key | implemented |
| Match evidence via identifiers, hashes, resource IDs, system IDs, timestamps, "other grounded links" (6.6) | M2 linker + catalog + trace + reference | M2; unchanged |
| Do not infer case membership from the manifest alone (6.6) | images are tied to resources via catalog rows plus OCR content, not via the manifest | implemented; 1 low-confidence image flagged |
| Replica drift (README) | `StateReconstructor` keeps copies, worst case wins | implemented; case_16 test |
| Tombstones keep only the stable reference (6.5) | `interpret` tombstone rule; `assess` ACCOUNT_TOMBSTONE→REDACT | implemented; cases 31/32 |
| Retained records stay available (6.5) | `assess` RETAIN: `RETAINED_MISSING` is a distinct verdict | implemented |
| "Complete" needs required retained records and no recreation path (6.5) | out of M3 (status = M4+) | not started |
| Evidence for COMPLETE verification must prove the current state (6.6) | citable ids exist only for usable, deciding readings | partial: see open question 1 |

## 4. Results

### 4.1 Resource scope on the 32 samples (111 gold resources) **[data]**
| stage | TP | FP | FN | P | R | F1 | exact set |
|---|---|---|---|---|---|---|---|
| M2 content associations (S3k) | 85 | 3 | 26 | 0.966 | 0.766 | 0.854 | 11/32 |
| + scenario relevance | 85 | 0 | 26 | 1.000 | 0.766 | 0.867 | 12/32 |
| + activity account attribution (**M3**) | 97 | 0 | 14 | **1.000** | **0.874** | **0.933** | **20/32** |

The 14 misses: 7 anonymized analytics profiles that carry no identifier at all; 2 audit records and 1 account (`account_004`) that appear in no participant file or activity row (**unobservable**); 5 accounts left unresolved on purpose: `account_410` (case_23: 3 candidates), `account_5482`/`account_7012` (cases 28/29: a symmetric pair, each is a candidate of both), `account_008` (case_08: delete result SUCCESS contradicts reported PARTIALLY_COMPLETE, so the result filter rejects its only candidate; it is listed under `rejected`). `account_006` (case_06) is reached through the M2 `REFERENCE` link.

### 4.2 State vs the answers' FINDING steps (97 resources both included and in the gold scope)
All 97 agree: 6 active flows, 11 exposed-PII, 21 remaining records, 3 restore risks, and 56 resources the answers list without a finding (22 compliant, 17 retained-OK, 17 UNKNOWN: accounts/records with no usable evidence of presence). **Diagnostic only:** "any violation among relevant resources" reproduces all 32 gold statuses (10 COMPLETE, 22 INCOMPLETE). Read this with section 6: 3 of the 97 depend on sample-informed shape rules.

### 4.3 Account attribution **[data]**
* Samples: 26 assigned and correct (14 content, 12 by elimination), 3 ambiguous with the truth among the candidates, 3 none, **0 wrong**.
* Eval population (508): 238 CONTENT, 150 STRONG, 108 AMBIGUOUS (candidate-set sizes 2:77, 3:27, 4:1, 6:3), 12 NONE (all screenshot cases whose account lies outside any workflow window).
* Hide-and-recover (independent of the sample answers; truth = a hidden content link): hiding every link 112/238 recovered, **0 wrong**; hiding every other link (the realistic ratio) 89/119 and 85/119, **0 wrong**, the rest ambiguous with the truth among the candidates.

### 4.4 Sensitivity (`analysis/m3_sensitivity.py`) **[data]**
| change | sample F1 | wrong accounts | eval STRONG/AMBIG/NONE |
|---|---|---|---|
| baseline | 0.933 | 0 | 150/108/12 |
| batch 2 h / 8 h | 0.933 | 0 | unchanged |
| window −60…+15 | 0.901 | 0 | 64/139/182 (−75 min offsets are real) |
| window −120…+30 | 0.933 | 0 | 137/121/12 |
| window −180…+60 | 0.928 | 0 | 127/131/12 |
| no result-compatibility filter | 0.901 | 0 | 39/219/12 |

No setting produced a confidently wrong account. The result filter is what makes most STRONG assignments possible (150 → 39 without it).

### 4.5 Eval-set structure (508 deletion requests; no gold)
1,593 resources RELEVANT, 9 UNRELATED (7 accounts, 1 export, 1 support item), 1 UNCERTAIN (`marketing_001`, contested ownership). Verdicts among included resources: 718 VIOLATION, 242 COMPLIANT, 307 RETAINED_OK, 327 UNKNOWN. UNKNOWN is mostly accounts (198) and retained records (124 audit records, 2 legal holds) identified only by post-audit evidence, i.e. identity without any usable state. 352 cases show at least one violation, 156 none (78 of those have an account with no usable state evidence).

## 5. Screenshot cases: what is and is not determined
| case (audit) | resource | M3 conclusion | confidence / what it prevents |
|---|---|---|---|
| 33 (07-19) | `export_1260` | UNRELATED (claimed by case_52) | high |
| 33 (07-19) | `export_1842` | PRESENT → remaining record, cited to the statement PDF `evidence_18` | the PDF names the export only through the catalog alias `NST-2026-0718-1842` (see 2.4) |
| 34 (09-27) | `account_5762` | PRESENT → remaining record | OCR-based; account page shows e-mail/phone |
| 35 (09-02) | `search_428` | PRESENT → remaining record | OCR-based |
| 39 (08-08) | `data_flow_1121` | ACTIVE → active data flow | needs `Continuous`+`Healthy` words (read) |
| 45 (09-06) | `audit_record_003` | PRESENT (retained) | OCR + `evidence_55` PDF |
| 46 (03-11) | `tombstone_17` | DEIDENTIFIED (compliant) | **low**: tied to the image only by catalog time; OCR could not read the reference |
| 50 (07-14) | `analytics_7438` | IDENTIFIABLE → exposed PII | OCR-based |
| 52 (06-19) | `export_1260` | PRESENT → remaining record | OCR-based; `export_1842` is not even a candidate (its PDF and catalog row postdate this audit) |
| 53 (05-25) | `support_1034` | IDENTIFIABLE → exposed PII | OCR-based |
| 55 (04-30) | `backup_9287` | PRESENT, **restore risk undeterminable** | the console never shows a restore flag, so REPORT_RESTORE_RISK vs compliant cannot be decided from the image |
| 61 (02-14) | `object_19` | PRESENT → remaining record | OCR matched the file name only |
| 63 (09-17) | `marketing_001` | **UNKNOWN, UNCERTAIN** | screenshot shows another person's identifiers (ownership conflict) |

What is not recovered: any screenshot field the console does not display (restore access, deletion markers), anything the OCR misreads that is not already known from the catalog, and state for resources shown only in screenshots *not* named by a catalog row (e.g. `support_9538` visible on the case_34 account page is linked only through a log and is treated as UNRELATED/MEDIUM). Which of the 12 conclusions are right cannot be checked locally.

## 6. Overfitting register
| rule | informed by | independently validated? |
|---|---|---|
| `contact_lookup_completed` = a marketing/contact profile (delete) rather than an analytics profile | case_12 answer | no (1 sample record) |
| `SUPPORT_RECORD` of unspecified type → delete | case_10 answer | no (1 sample) |
| PDF "attachment" → `SUPPORT_ATTACHMENT` (delete) | case_07 answer + document text | no |
| batch window 4 h; account window −90…+15 min | sample offsets | **yes**: 253/263 content-linked eval accounts; sensitivity table |
| result-compatibility filter | observed on samples | **yes**: 252/253 content-linked pairs |
| relevance via sibling case / outside-lifecycle workflow | 3 sample false positives, then generalised to need positive evidence | partly: 12 eval exclusions exist, none verifiable |
| screenshot meaning rules and catalog-alias grounding of PDFs | none (no sample uses a screenshot or an aliased document) | **no** |
| record-shape table, worst-case copies, usable-evidence rule | record semantics and the spec | yes: 0 unrecognised shapes; real-data tests |

## 7. Open questions
1. **Citing absence for COMPLETE cases.** 5 of the 10 sample COMPLETE cases cite evidence that never names the account (an empty `customer_profiles` snapshot for case_31, a partial profile table for 01/02/04, a 404 lookup for an unattributed account in case_28). M3 leaves those accounts UNKNOWN rather than invent a rule that does not generalise; M4 must decide how absence is proven and cited.
2. Whether hidden answers include resources identified only by post-audit evidence (M2's reading A vs B) is still unconfirmed.
3. `support_9538`-style resources visible only inside a screenshot but not in the catalog are not extracted.
4. 108 eval accounts remain ambiguous by construction; they need either a new signal or an explicit decision to emit the verification step with an uncertain account.
5. Screenshot meaning rules (section 2.4) need a human look at the 12 conclusions before they feed any step.

## Reproduce
```
cd code && python -m unittest discover -s tests -t .                    # full suite
python code/analysis/m3_evaluation.py --report local/reports/m3_sample_report.md
python code/analysis/m3_sensitivity.py
python code/analysis/account_attribution_eval.py
python code/tools/build_ocr_cache.py                                    # Windows only; regenerates the OCR cache
```
