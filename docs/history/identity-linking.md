# M2: identity graph, content linker, and the identity-linking policy

Labels: **[spec]** = stated in `AGENTS.md` / `docs/challenge/problem_statement.md` / `README.md`; **[data]** = measured on the
dataset (reproduce with `python code/analysis/link_policy_comparison.py`); **[hypothesis]** = not confirmed;
**[decision]** = my design choice. Nothing here classifies cases, builds steps, or writes `output.csv`.

> Naming note: my M1 report defined only S1, S2 and S3. "S3c" and "S3T" were not names I had introduced.
> Here **S3c** is a policy I define (S3 with a link-type restriction on post-audit sources); **S3k** is the
> one I recommend (S3 with a resource-kind restriction); **"S3T"** (S3 + activity-timing) is *not* a linking
> policy in this milestone: timing is measured in section 5 and kept out of the linker on purpose.

## 1. What the specification actually says

* **[spec]** "Use only information available at or before `audit_at` when deciding the case. Evidence created later must not change the answer for that case." (AGENTS 6.2)
* **[spec]** "Resolve both current and historical identifiers for the subject." / "Reconstruct the true state at `audit_at`. Ignore later evidence…" (AGENTS 6.5)
* **[spec]** "Match evidence through current identifiers, historical identifiers, hashes, resource IDs, system IDs, timestamps, and other grounded links." (6.6)
* **[spec]** "Never cite organizer-only truth or an evidence item captured after the case's `audit_at`." (6.6)
* **[spec]** Findings and `COMPLETE` verification steps must cite evidence; future remediation/verification of `INCOMPLETE` cases use `""`.

Three readings of "evidence created later must not change the answer":

| Reading | Meaning | Consequence |
|---|---|---|
| **A, strict** | no fact learned from a later file may be used, including *which user owns a resource* | policy S1/S2. **[data]** misses 8 of the 111 sample gold resources (3 accounts, 5 audit records); for the 5 audit records *no* eligible file mentions them at all, yet the organizer-supplied correct answer lists them. Under A the supplied "correct" answers would contain resources no participant can derive. |
| **B, state-only** | later evidence must not change what the record's *state* was at `audit_at`, which findings are made, or what is cited; it may still tell us *whose* a stable resource is | policy S3-family. Consistent with the samples and with the spec treating identity ("resolve identifiers") and state ("reconstruct the true state") as separate duties. |
| **C, permissive** | later evidence may also establish state / be cited | contradicts the explicit citation and "ignore later evidence" rules. Rejected. |

The spec does not say which of A or B applies to *ownership*. That is the genuine ambiguity. I do not claim the
samples prove B for the hidden set; they show that the organizers' own answers are not reproducible under A.

## 2. The four questions, kept separate

| # | Question | Where it lives | Time rule |
|---|---|---|---|
| 1 | Whose is this resource? | `identity.py` + `linker.py` (`LinkPolicy`) | policy-controlled (S1…S3k) |
| 2 | What was its state at `audit_at`? | `evidence_index.py` supplies eligible observations; interpretation is M3 | eligible sources only (file captured and record time ≤ `audit_at`) |
| 3 | What may a step cite? | `EvidenceIndex.citable_ids` | eligible only; the validator also checks ids |
| 4 | Account attribution from activity timing | **not implemented** (separate step) | activity rows ≤ `audit_at` are claims, not proof |

A later file may satisfy (1) and never (2) or (3). Tested: `account_3866` (case_27) is *identified* by
`evidence_02` (captured 2 days after the audit) but can only be *cited* from `evidence_92`; `audit_record_4128`
(case_23) is associated yet has no citable evidence at all. Every association reports `post_audit_only`.

Identity time gate: `IdentityClaim.known_by(at)` ("the value already existed at `at`"), **not** "was still
held at `at`". Using "held" made 103 of 550 request e-mails unresolvable, because historical e-mails were
replaced before the audit but still identify the user in old systems; `known_by` resolves all 550.

## 3. Policies

| | identifiers | link sources | post-audit sources accepted for |
|---|---|---|---|
| S1 | only values that existed at `audit_at` | captured ≤ `audit_at` | nothing |
| S2 | all current + historical | captured ≤ `audit_at` | nothing |
| S3 | all | any | everything |
| S3c | all | any | DIRECT/CATALOG/DOCUMENT/REFERENCE links, single-user record, single owner overall |
| **S3k** | all | any | **account, security-audit and legal-hold resources only**, single-user record, single owner overall |

How each can go wrong:
* **S1/S2** omit stable resources identified only later (reading A bet). 7% of sample gold resources.
* **S3** can attach a *mutable* copy (search entry, export, backup, support record) that was created after the audit by a sync/restore and never belonged to the case, or a later scenario's resources of a user with several cases.
* **S3c** discards the weakest link type (TRACE) from post-audit files, but the type is not what makes a post-audit link risky; it loses 2 sample gold resources (case_09) for no principled gain.
* **S3k** relies on the premise that account profiles and retained records are not created or destroyed by later sync/restore activity, so a later sighting identifies them without implying anything about the case's state. **[hypothesis]**: no sample case has a legal hold; `legal_service` is included because the two eval legal holds identified only post-audit have `RETAIN_RECORD` activity 0.5 h from their case's `reported_at`.

## 4. Results on the 32 samples (111 gold resources)

Resource associations vs every `resource_id` in the sample answer **[data]**. Precision is a lower bound: a
true association the gold does not list counts as a false positive (see section 6).

| Policy | TP | FP | FN | Precision | Recall | F1 | exact set | ⊇ gold |
|---|---|---|---|---|---|---|---|---|
| S1 | 77 | 3 | 34 | 0.963 | 0.694 | 0.806 | 9/32 | 10/32 |
| S2 | 77 | 3 | 34 | 0.963 | 0.694 | 0.806 | 9/32 | 10/32 |
| S3 | 85 | 3 | 26 | 0.966 | 0.766 | 0.854 | 11/32 | 12/32 |
| S3c | 83 | 3 | 28 | 0.965 | 0.748 | 0.843 | 10/32 | 11/32 |
| **S3k** | 85 | 3 | 26 | 0.966 | 0.766 | 0.854 | 11/32 | 12/32 |

By group (TP/FP/FN): accounts S2 12/1/20, **S3k 15/1/17**; audit records S2 17/0/7, **S3k 22/0/2**; other systems 48/2/7 for every policy.
Before M2 (M1 probe, same metric): S1/S2 TP 76, S3 TP 84, exact 8/32 and 10/32. The only gain from M2 is one
`REFERENCE` link (case_06: `search_001.source_resource_id = account_006`); the rest of M2 is correctness
machinery (provenance, ambiguity, determinism), not score. **On the samples S3 and S3k are indistinguishable;
that choice rests on the spec reading and the eval-set structure, not on a score.**

## 5. The disagreements, one by one **[data]**

All 8 resources where policies differ are accounts or audit records identified only by a post-audit file:

| case | resource | link (source, captured) | S1/S2 | S3 | S3c | S3k | accept? |
|---|---|---|---|---|---|---|---|
| 09 | `account_3960`, `audit_record_4902` | TRACE, `evidence_06`, 2 d after audit | no | yes | **no** | yes | yes (reading B): grounded by `trace_id` (email on the started line, resource on the completed line); no eligible file mentions either; the account's `DELETE_RECORD` row (≤ audit) exists but is a claim |
| 23, 24, 29, 30 | `audit_record_4128/7095/3954/1707` | DIRECT user_ref, `evidence_02`, 2 d after | no | yes | yes | yes | yes: retained records with `subject_reference`; **only** reachable this way |
| 26, 27 | `account_5684`, `account_3866` | DIRECT user_ref, `evidence_02` | no | yes | yes | yes | yes, but state must come from `evidence_92` (eligible 404 lookup with no identifier) |

False positives under every policy (3): `object_19` (case_07), `tombstone_17` and `account_6523` (case_17). All are
real associations from *another* scenario of a user with several cases (`user_07` → case_61, `user_17` →
case_46); none has worker activity within 3 h of this case's `reported_at`. They are not linking errors; they
are an M3 relevance problem.

Misses under S3k (26): **ACTIVITY_ONLY 13** (accounts that appear only as `DELETE_RECORD` rows), **EVIDENCE_WITHOUT_IDENTIFIER 10**
(7 anonymized analytics profiles and 3 accounts seen as 404 lookups, with no identifier in the record),
**UNOBSERVABLE 3** (`account_004`, `audit_record_6645`, `audit_record_5569`: the id appears in no participant file or activity row).
So at most 108/111 gold resources (97.3%) are reachable by any deterministic method; 23 of the 26 misses need
activity-timing attribution. Measurement only: 16 of the 17 missed accounts lie inside a −90…+15 min window around
`reported_at` (the 17th has no activity row at all; every sample gold account that has one lies in −75…+8 min). In that
window the number of deletion-worker account deletes (all owners) is exactly 1 for 6 of them, 3 for 3 and 5 or more for 7,
so timing alone is ambiguous before elimination of accounts already owned by someone else. All 23
reachable misses have worker activity within 3 h of `reported_at`.

## 6. Eval set (518 requests; no gold) **[data]**

* 550 of 550 request e-mails resolve to exactly one user: 432 exact-current, 98 exact-historical, 15 + 5 case-insensitive. No identifier is claimed by two users; no resource is linked to two users; 0 ambiguous associations; 22 e-mails in the evidence belong to nobody (15 `…jr@` and 7 look-alikes such as `joana.berg` vs `jonas.berg`) and are correctly left unlinked.
* Of the 508 deletion requests: 246 have an account associated by content, **262 do not** (activity timing needed); 425 have an audit record, 83 do not.
* Post-audit-only associations under S3: 90 accounts (81 in deletion cases + 9 in non-deletion requests), 133 audit records, and 20 mutable resources (15 search, 1 backup, 1 export, 1 support, 2 legal holds). S3k keeps the 223 stable ones (+2 legal holds) and drops the 18 mutable ones, in 17 cases. None of the 18 has worker activity within 3 h of its case, and none has any eligible evidence, so a finding about it could not be cited.
* Scenario mixing: 3 users have 2 cases each. 9 associations in 8 cases (all screenshot cases 33–63) have worker activity > 3 h from `reported_at` and are flagged NEEDS REVIEW; 6 more catalog-linked ones are the screenshots' intended resources.
* Downside if B is wrong: S3k contributes up to 216 post-audit-only associations (81 accounts, 133 audit records, 2 legal holds; ≈15% of the 1,453 associations, in 149 cases) that a truth derived only from eligible evidence would not contain. The samples argue against that for accounts (32 of 32 sample answers contain an account step, and 17 of those 32 accounts are not reachable by content) but cannot rule it out for audit records (24 of 32 sample answers contain one; the other 8 are the hand-written early cases). This is the main open risk of choosing B.

## 7. Recommendation

**Adopt S3k as the default; keep S2 selectable** (`POLICIES["S2"]`), since flipping is a one-argument change.
1. It is the only reading under which the organizers' own sample answers are derivable.
2. It never lets later evidence into state or citations (separate `EvidenceIndex`, tested).
3. It confines post-audit association to resources whose existence does not depend on activity after the audit, and requires a single owner and a single-user record, which removes the 18 mutable-resource associations with no eligible evidence.
4. Its cost is bounded and visible: every such association carries `post_audit_only`, so M3 can decide per resource kind how to use it (e.g. verification yes, finding never).

What would change this: evidence that the grader's truth for a case is derived only from evidence ≤ `audit_at` (not
reproducible with the samples), or a sample case showing a post-audit-only *mutable* resource in the gold.

## 8. Confirmed / supported / uncertain

* **Confirmed by the specification:** resolve current *and* historical identifiers; request e-mail may match either form; never cite evidence captured after `audit_at`; state is reconstructed at `audit_at`.
* **Supported by the data:** request e-mail resolution (550/550); hash/e-mail/phone chains are consistent (0 issues); no ownership conflicts; the S3k reach figures above; every sample false positive is another scenario of the same user.
* **Uncertain:** whether ownership learned after `audit_at` is allowed in the hidden grader (reading A vs B); whether legal holds behave like audit records (no sample); how many of the 26 misses timing attribution can recover without false positives; what the 11 screenshots add beyond the catalog rows; scenario relevance for users with several cases.
