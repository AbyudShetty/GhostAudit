"""Six extra evaluation cases written for this project (the dataset has no labels for them).

    python evaluation/custom_cases.py          # prints one line per case and exits non-zero if any case differs

Each case builds a tiny synthetic dataset (fictional ids, real files, the real pipeline) and compares the complete output
(status and every step in order) with an expectation written by hand from the specification, not copied from program output.
They target the places where the specification is easy to get wrong: non-deletion requests, evidence captured after
``audit_at``, unresolved accounts, and identities that were inferred rather than proven.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Tuple

CODE_DIR = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE_DIR))

from ghostaudit.classification import classify_all  # noqa: E402
from ghostaudit.plan import build_plan  # noqa: E402
from tests.world import POST, PRE, World, audit_line, clean_world, lf, lookup  # noqa: E402

Step = Tuple[str, str, str, str]  # (phase, operation, resource_id, evidence_id)


class Expect(NamedTuple):
    status: str
    steps: Tuple[Step, ...]


class Case(NamedTuple):
    key: str
    title: str
    why: str
    build: Callable[[], World]
    expected: Dict[str, Expect]


def search_line(email="ann@x.com"):
    return lf("2026-09-15T10:10:00Z", "search_service", "index_query_completed", "search_1", trace="tr-9", result="hit", email=email)


RESIDUALS = [
    search_line(),
    lf("2026-09-15T10:11:00Z", "support_platform", "support_ticket_read_completed", "support_1", trace="tr-s", requester_email="ann@x.com"),
    lf("2026-09-15T10:12:00Z", "integration_service", "connector_config_read_completed", "data_flow_1", trace="tr-f", enabled="true", last_delivery_status="delivered", customer_email="ann@x.com"),
    lf("2026-09-15T10:13:02Z", "backup_service", "restore_policy_read_completed", "backup_1", trace="tr-b", archive_present="true", restore_allowed="true", customer_email="ann@x.com"),
    lf("2026-09-15T10:14:00Z", "analytics_platform", "analytics_profile_read_completed", "analytics_1", trace="tr-a", email="ann@x.com"),
]

E = "evidence_1"
CLEAN_PROOFS = (("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_10", E), ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1", E))


def _access_request() -> World:
    return World().case("case_1", text="Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.")


def _late_evidence() -> World:
    return clean_world().log("evidence_2", POST, lookup("2026-09-16T10:00:00Z", "account_10", "ann@x.com", rows=1, trace="tr-2"))


def _two_candidate_accounts() -> World:
    w = World().case("case_1", "ann@x.com").case("case_2", "bob@x.com")
    w.worker("activity_1", "account_10", "2026-08-22T11:00:00Z").worker("activity_2", "account_11", "2026-08-22T11:20:00Z")
    return w.log("evidence_1", PRE, [audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1"), audit_line("2026-09-15T10:06:00Z", "audit_record_2", "user_2"), search_line()])


def _account_named_only_by_activity() -> World:
    w = World().case("case_1").worker("activity_1", "account_10", "2026-08-22T11:00:00Z")
    return w.log("evidence_1", PRE, [lf("2026-09-15T10:00:00Z", "account_service", "profile_lookup_completed", "account_10", trace="tr-1", rows=0, http_status=404),
                                     audit_line("2026-09-15T10:05:00Z", "audit_record_1", "user_1")])


CASES: List[Case] = [
    Case("A", "Access request, not a deletion request",
         "A request for access is NOT_APPLICABLE and carries no steps, whatever the evidence looks like.",
         _access_request, {"case_1": Expect("NOT_APPLICABLE", ())}),
    Case("B", "Clean deletion, retained audit record present",
         "COMPLETE needs only cited verification steps: the account is absent and the required audit record remains.",
         clean_world, {"case_1": Expect("COMPLETE", CLEAN_PROOFS)}),
    Case("C", "Five residual copies and risks",
         "INCOMPLETE: every violation is found with its evidence, flows and restores are stopped before records change, "
         "and every remediated item and the retained record get a post-remediation check with an empty evidence_id.",
         lambda: clean_world(RESIDUALS), {"case_1": Expect("INCOMPLETE", (
             ("FINDING", "REPORT_REMAINING_RECORD", "search_1", E), ("FINDING", "REPORT_EXPOSED_PII", "analytics_1", E), ("FINDING", "REPORT_EXPOSED_PII", "support_1", E),
             ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "data_flow_1", E), ("FINDING", "REPORT_RESTORE_RISK", "backup_1", E),
             ("REMEDIATION", "DISABLE_DATA_FLOW", "data_flow_1", ""), ("REMEDIATION", "BLOCK_RESTORE", "backup_1", ""), ("REMEDIATION", "DELETE_RECORD", "search_1", ""),
             ("REMEDIATION", "REDACT_RECORD", "support_1", ""), ("REMEDIATION", "ANONYMIZE_RECORD", "analytics_1", ""),
             ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_10", ""), ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_1", ""),
             ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1", ""), ("VERIFICATION", "VERIFY_RECORD_REDACTED", "support_1", ""),
             ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "analytics_1", ""), ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "data_flow_1", ""),
             ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_1", "")))}),
    Case("D", "Evidence captured after audit_at is ignored",
         "A later log shows the account back again; the answer at audit_at must not change or cite it.",
         _late_evidence, {"case_1": Expect("COMPLETE", CLEAN_PROOFS)}),
    Case("E", "Two possible accounts, one residual record",
         "The account cannot be told apart from the other subject's, so no account step is produced for either subject; the rest is unaffected.",
         _two_candidate_accounts, {
             "case_1": Expect("INCOMPLETE", (("FINDING", "REPORT_REMAINING_RECORD", "search_1", E), ("REMEDIATION", "DELETE_RECORD", "search_1", ""),
                                             ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_1", ""), ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1", ""))),
             "case_2": Expect("COMPLETE", (("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_2", E),))}),
    Case("F", "Account identified only from activity",
         "A 404 captured before audit_at names account_10, and the only deletion-worker row in the request's window ties it to the subject "
         "(no rival account, nothing captured later). Both facts are available at audit_at, so the account is verified absent, citing the "
         "404, next to the directly proven retained record.",
         _account_named_only_by_activity, {"case_1": Expect("COMPLETE", (("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_10", E),
                                                                           ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1", E)))}),
]


def run_case(case: Case) -> List[Tuple[str, bool, str]]:
    w = case.build()
    if not hasattr(w, "ds"):
        w = w.build()
    try:
        verdicts = classify_all(w.cr, w.ds.requests.rows)
        out = []
        for cid, exp in sorted(case.expected.items()):
            plan = build_plan(verdicts[cid], w.cr.manifest)
            got = Expect(plan.status, tuple((s["phase"], s["operation"], s["resource_id"], s["evidence_id"]) for s in plan.steps))
            numbered = [s["step_no"] for s in plan.steps] == list(range(1, len(plan.steps) + 1))
            ok = got == exp and numbered
            detail = "" if ok else f"expected {exp.status} {list(exp.steps)}\n      got      {got.status} {list(got.steps)}"
            out.append((cid, ok, detail))
        return out
    finally:
        w.close()


def run_all() -> List[Tuple[str, str, bool, str]]:
    return [(c.key, cid, ok, d) for c in CASES for cid, ok, d in run_case(c)]


def main() -> int:
    results = run_all()
    titles = {c.key: c.title for c in CASES}
    for key, cid, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  custom case {key} ({cid}): {titles[key]}")
        if detail:
            print("      " + detail)
    passed = sum(1 for r in results if r[2])
    print(f"{passed}/{len(results)} custom evaluation case results agree with their hand-written expectation")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
