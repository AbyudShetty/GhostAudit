# M4: deletion status (COMPLETE / INCOMPLETE / NOT_APPLICABLE) and the evidence behind it

Labels: **[spec]** stated in `docs/challenge/problem_statement.md` / `AGENTS.md` / `README.md`; **[data]** measured (commands at the end);
**[hypothesis]** unconfirmed; **[decision]** my choice. M4 classifies and explains; it builds **no steps** and writes **no `output.csv`**.

## 1. What the specification actually requires **[spec]**

| Status | Requirement (wording) |
|---|---|
| `NOT_APPLICABLE` | "Use `NOT_APPLICABLE` with `steps_json=[]` when the request does not authorize deletion." Access, correction, export, marketing opt-out or "another non-deletion action" qualify "even if other fields resemble a deletion case" (AGENTS 6.3, 6.5) |
| `COMPLETE` | "only when every relevant copy has the required treatment, every required retained record remains available, and no active sync, import, or restore path can recreate prohibited data" (6.5). Its steps are "evidence-backed verification steps proving the state at `audit_at`"; "a verification step for a `COMPLETE` case must cite the evidence that proves the current compliant state" (6.3, 6.6) |
| `INCOMPLETE` | "report every relevant policy violation and recreation risk supported by participant-visible evidence" (6.5) |
| Claims | "Treat activity logs, workflow statuses, and system messages as claims unless the available evidence establishes the resulting state" (6.6) |

Two consequences drive the design. (1) COMPLETE is a conjunction of three conditions plus *cited proof*; "no violation found" is not
one of them. (2) The spec defines COMPLETE only; it does not say what to output when no violation is found but COMPLETE cannot be
proven. That gap is the single genuine policy choice in M4 (section 6).

## 2. Layering

```
intent.py        request text -> DELETION | NON_DELETION | UNCERTAIN                       (text only)
reconstruction   (M3) ownership, relevance, account attribution, state at audit_at           (unchanged)
absence.py       subject-scoped snapshot support for "no account profile"                    (weaker, separate tier)
classification   violations + requirements + gaps -> status, determination, confidence, reasons
```
`classification.py` consumes `CaseReconstruction`; it collects nothing and builds no steps. Each requirement/violation only carries the
*label* of the operation M5 will need (`expected_op` / `finding_op`, from `schema.VERIFY_FOR_ACTION` and `state.assess`).
`reported_status` / `reported_result` are never inputs to the status; they are used upstream only as a *matching key* between two
claims of one workflow when choosing among candidate accounts. **[data]** Switching that key off changes the status of **0** of 550 cases
(and the determination of 15).

## 3. Rules

### 3.1 Intent (`NOT_APPLICABLE`)
Ordered: explicit denial → scope limiter (e.g. "marketing lists only") → deletion cue (6 languages, plus "is gone / was deleted" because
people asking us to *verify or finish* a deletion often use no verb: samples 21, 25, 26, 27) → non-deletion topic → `UNCERTAIN`.
A denial plus an explicit imperative request is contradictory and is `UNCERTAIN`. `UNCERTAIN` never authorises deletion (default
`NOT_APPLICABLE`, switchable). **[data]** 32/32 samples are deletion; eval: 508 deletion, 10 non-deletion, 0 uncertain. The 10 intake
`CLASSIFY_REQUEST` rows (claims) independently name accounts linked to exactly those 10 cases and to no deletion case. The sample set
contains no non-deletion request, so the non-deletion lexicon is validated only by the 10 eval texts (a closed set) and synthetic cases.
*Bug found by running on all 550 requests:* accent-stripping decomposed Hangul and made 12 Korean deletion requests `UNCERTAIN`; fixed and tested.

### 3.2 What counts as proof of absence (the central evidence question)
| Tier | Evidence | Counts as | Confidence |
|---|---|---|---|
| explicit, resource-scoped | a record that *names* the resource: `rows=0`/404, `NOT_FOUND`, `profile: null`, `hits.total=0` | proof | HIGH (MEDIUM if the account id rests on activity attribution) |
| subject-scoped snapshot | one eligible snapshot that declares an account-profile collection, **lists ≥1 other record of this subject**, and lists no profile for them | support, labelled `scoped_snapshot` | MEDIUM, determination `INFERRED` |
| non-listing in a table | resource simply not in a table/collection | **nothing** | - |

*Why non-listing is not proof* **[data]**: 9 accounts are shown present by other evidence within 24 h of a profile snapshot; **7 are missing
from that snapshot** (e.g. `account_5334`, present 09:41, absent from `evidence_51`'s empty profile collection at 12:00 the same day). These
snapshots are partial. `state.interpret` therefore only yields `ABSENT` from a record naming the resource.
*Why the narrower subject-scoped tier is still admitted* **[data + hypothesis]**: 179 (snapshot, subject) pairs satisfy it; no independent
PRESENT reading precedes any of them. Only 5 pairs could even be contradicted, all by a reading **captured after** the snapshot; two of those
are an explicit ABSENT followed by PRESENT two days later (an account that came back after the audit), the exact trap the audit-time rule
exists for. The test has low power, so the tier is MEDIUM, reported as `INFERRED`, and switchable
(`accept_scoped_snapshot_absence`). It never overrides an explicit reading, and an accounts-only table (`evidence_54`, cases 01/02/04) cannot
satisfy it because it cannot enumerate the subject.
*Consequence for the samples:* 4 of the 10 COMPLETE answers cite non-listing evidence; M4 supports one of them (`case_31`, `evidence_51`) at MEDIUM
and cannot support three (01, 02, 04: `account_004` is named by no participant file or activity row at all).

### 3.3 Violations and confidence
A violation is a RELEVANT, unambiguously owned resource whose state at `audit_at` (usable evidence only) breaks its rule: `REPORT_REMAINING_RECORD`,
`REPORT_EXPOSED_PII`, `REPORT_ACTIVE_DATA_FLOW`, `REPORT_RESTORE_RISK`. Confidence is that of the best deciding evidence: **HIGH** log/JSON/SQL/TXT; **MEDIUM** PDF text,
screenshot OCR confirmed by readable content, activity-attributed account; **LOW** screenshot tied only by the catalog time offset, or
a conflicting screenshot. LOW is never a violation or a proof; it becomes a blocking gap. A missing required retained record (inside its retention period) is a
violation with `finding_op = None` because the output vocabulary has no operation for it.

### 3.4 Determination
`INCOMPLETE`: `PROVEN` (≥1 HIGH violation) or `SUPPORTED` (only MEDIUM). `COMPLETE`: `PROVEN` (every requirement HIGH), `INFERRED` (all proven, some MEDIUM), or `PARTIAL`
(no violation but a requirement unproven: the status then comes from `ClassificationPolicy.unproven_status`). `NOT_APPLICABLE`: `TEXT`.
Requirements for a proven COMPLETE: the account absent; each included resource's required treatment (`VERIFY_RECORD_ABSENT/REDACTED/ANONYMIZED`, flow disabled, restore blocked); each linked
retained record present; ≥1 retained record located. A requirement is proven only by usable evidence whose `citable` ids are what the verdict lists.

## 4. The cases you asked about

**Ambiguous accounts (108 eval cases).** The output schema (`schema.validate_steps`, and the statement) requires a non-empty `system_id` and `resource_id` on every step and has no
placeholder, "unknown" or alternative form. So an unresolved account has exactly two representations: omit the step, or name a candidate. Naming one is a guess (for a 2-candidate set, one of two claims is wrong); I do not do it.
**[data]** After M4, **119** of the 508 eval deletion cases have no account id that can be named (108 ambiguous + 11 unattributed). In 73 of the 108 ambiguous ones the status is already decided by another violation;
35 are COMPLETE. Two safe tools exist: if *every* candidate has an explicit absence record the requirement is proven set-wise (no id named, gap kept, non-blocking), and if a subject-scoped
snapshot covers the subject the same applies. If a candidate is explicitly **present**, that is *not* reported as a violation (it may be the other user's) but blocks a proven COMPLETE.
Consequence for the required steps: those cases will lack one `VERIFY_RECORD_ABSENT` account step. Most defensible handling: omit it and record why (M5 should surface the gap, not hide it).
Alternative (emit all candidates) trades precision for recall and invents claims; not recommended without your decision.

**Screenshot conclusions.** `tombstone_17` (case_46): tied to its image only by the catalog time offset (OCR could not read the reference): LOW, so its compliant-looking state is a blocking gap and **not** proof, and the case is `PARTIAL`.
`marketing_001` (case_63): the screenshot shows another person's identifiers; no state is derived, relevance is UNCERTAIN and the case is `PARTIAL`. Neither is resolved. The other 10 screenshot conclusions are MEDIUM. **[data]** The 8 `SUPPORTED` eval cases
are exactly the screenshot/PDF cases (33, 34, 35, 39, 50, 52, 53, 61); for 7 of them the status is `COMPLETE/PARTIAL` if no OCR is used, i.e. it rests entirely on unvalidated "a visible record exists / visible identifiers are exposed" meaning rules.

**Sample-informed rules vs the written rules.** Re-grounded: `contact_lookup_completed` (a contact record keyed by a *historical* e-mail hash, `email_opt_in`) and the support legacy contact index (`contact_query_completed`) fall under `rule_02 SECONDARY_IDENTIFIER Always DELETE` / `rule_10 MARKETING_PROFILE Always DELETE`;
every written candidate for those shapes is DELETE-class, so the finding (`REPORT_REMAINING_RECORD`) does not depend on the sample, only the choice between a delete-class category and `ANALYTICS_PROFILE→ANONYMIZE` does (the shape has no analytics fields; limitation recorded). The PDF attachment is
checked against rule_06's condition ("belongs only to the deletion subject"): it names one customer, and an attachment that names several subjects is a competing-owner resource, which becomes an `OWNERSHIP_CONFLICT` gap instead of a violation (a dedicated rule_06 branch was removed as unreachable). Support messages are treated as `rule_05 REDACT` because every support conversation has an agent counterpart (every sample answer redacts, including the "account access" one). **[data]** None of the three
shape rules touches any eval case (0 of 508), so they cannot move the submission; they remain only as sample-informed rules with a documented limitation.

## 5. Results (default policy) **[data]**

| Level | Result |
|---|---|
| Intent | samples 32 deletion; eval 508 deletion / 10 non-deletion / 0 uncertain |
| Resource scope (M3, unchanged) | P 1.000 R 0.874 F1 0.933 |
| **Findings** | TP 41 FP 0 FN 0 (P/R/F1 1.000); cited file equals the answer's in 41/41; full finding set exact in 32/32 |
| **Proofs** (34 verification steps of the 10 COMPLETE answers) | **23 provable**, and the file cited by the answer is one of ours in 23/23; **11 not provable**: `account_001`, `account_002`, `account_004` (no usable record names them), `account_5482` (ambiguous with `account_7012`), and 7 anonymized analytics profiles that carry no identifier at all. 0 extra proofs the answers lack |
| **Status** | 32/32 exact; 28 of 32 with `unproven→INCOMPLETE` |

What the 32/32 rests on: 3 COMPLETE `PROVEN`, 3 `INFERRED`, **4 `PARTIAL` (01, 02, 04, 28)**, 21 INCOMPLETE `PROVEN`, 1 `SUPPORTED` (case_07, PDF). So 4 of 10 COMPLETE agreements come from the policy default, not from proof.
Every INCOMPLETE answer has a violation and no COMPLETE answer has one, so within the samples the statuses are reproduced by findings, not by `reported_status` (which is wrong in 11 of 17 reported-COMPLETE samples).
No status, finding or proof disagreement exists on the samples; the only disagreements are the 11 proofs we decline to make (listed in `m4_sample_report.md`). Eval (518): 345 INCOMPLETE PROVEN, 8 SUPPORTED, 62 COMPLETE PROVEN, 48 INFERRED, **45 PARTIAL**, 10 NOT_APPLICABLE.

## 6. Decisions, trade-offs and what needs your input

1. **Status of a `PARTIAL` case (45 eval cases, 9%; 4 samples).** Options: (a) `COMPLETE` (default now): INCOMPLETE would have to report findings that do not exist, and violations are identifier-exposed by construction (an exposed record carries an identifier, so a linkable violation is findable), so "none found" is informative;
   (b) `INCOMPLETE`: the strict contrapositive of "COMPLETE only when ..."; (c) leave it `PARTIAL` internally and decide per hidden-grader policy. **Recommendation: (a)**, with `determination=PARTIAL` carried to M5 so that those cases omit the unprovable step and claim nothing they cannot cite. The samples favour (a) (4/4), but that is 4 cases from the same author-written families. **Please confirm.**
2. Accept MEDIUM violations (PDF/OCR) as sufficient for INCOMPLETE (default yes; the alternative flips 8 eval cases and `case_07`).
3. Accept a subject-scoped snapshot as MEDIUM support for the account (default yes). **[data]** Switching it off turns 31 eval cases (and sample `case_31`) from `INFERRED` into `PARTIAL`; no status changes under (a).
4. Accept activity-attributed accounts (STRONG, MEDIUM) as proof when an explicit negative names them (default yes).
5. `RETAINED_NOT_LOCATED` blocks a proven COMPLETE (strict reading of "every required retained record remains available"). **[data]** It is never the sole blocker (0 eval cases), so this choice moves nothing today. Diagnostic: 82 of the 83 eval cases with no locatable audit record are INCOMPLETE by findings.

## Reproduce
```
cd code && python -m unittest discover -s tests -t .
python code/analysis/m4_evaluation.py --report local/reports/m4_sample_report.md
```
