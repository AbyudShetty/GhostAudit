import unittest

from tests.helpers import needs_dataset, real_dataset

from ghostaudit.intent import DELETION, NON_DELETION, UNCERTAIN, classify_intent, normalise


def outcome(text):
    return classify_intent(text).outcome


class DeletionRequests(unittest.TestCase):
    CASES = {
        "en imperative": "Please delete my account and personal data.",
        "en erase": "Please erase the account linked to this email address.",
        "en close": "I want to close this account and have its associated personal data deleted.",
        "en remove": "I am asking you to remove my account and the personal information linked to it.",
        "es": "Solicito la eliminación permanente de la cuenta asociada a este correo.",
        "pt": "Solicito a exclusão permanente da conta associada a este e-mail.",
        "fr": "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
        "ja": "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
        "ko": "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
        "verify-only wording (no deletion verb)": "I found an old report saying my account still existed. Please confirm that the profile is gone.",
        "audit of a past deletion": "Please audit the deletion as it stood when my request was reviewed.",
        "follow-up on a deletion": "Intenté eliminar mi cuenta ayer, pero la página terminó mostrando un error.",
        "keeps things but still deletes the account": "I no longer need this account. Please remove my name and contact details, but keeping an internal deleted-account reference is fine.",
        "retention allowance is not a denial": "I want to close this account permanently. You may retain backups if required, but they must not be able to restore my personal profile.",
        "'do not treat that record as mine' is not a denial": "Can you verify that my account was deleted? A search result with a similar email belongs to someone else, so please do not treat that record as mine.",
    }

    def test_each_is_a_deletion_request(self):
        for name, text in self.CASES.items():
            with self.subTest(name):
                r = classify_intent(text)
                self.assertEqual(r.outcome, DELETION, (r.reason, r.cues))
                self.assertTrue(r.cues)

    def test_korean_is_not_broken_by_accent_stripping(self):
        self.assertIn("삭제", normalise("영구 삭제를 요청합니다"))
        self.assertEqual(normalise("Suppression définitive"), "suppression definitive")


class NonDeletionRequests(unittest.TestCase):
    CASES = {
        "correction": "The mobile number on my profile is out of date. Please replace it with the new one and leave the account active.",
        "marketing opt-out": "Please stop the promotional emails. I still need the account and my order history, so I am not asking for deletion.",
        "export": "I'd like to download my account information in a portable format. Please keep the account as it is.",
        "login help": "I can't access my profile and need help resetting the login. Do not close the account.",
        "typo": "My surname is misspelled on the account. Can you correct it without removing any of my records?",
        "marketing-only removal": "Please remove me from marketing lists only. I still want receipts and service messages for this account.",
        "access": "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
        "invoices": "I need a copy of last year's invoices for my records. Please leave my profile and purchase history in place.",
        "email change": "Please change the email address used for this account. I want to continue using the service.",
        "copy": "Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything.",
        "denial without any other cue": "Do not delete anything, I just want to check my order.",
        "limiter without denial": "Unsubscribe me from the promotional emails only.",
    }

    def test_each_is_not_a_deletion_request(self):
        for name, text in self.CASES.items():
            with self.subTest(name):
                r = classify_intent(text)
                self.assertEqual(r.outcome, NON_DELETION, (r.reason, r.cues))


class Ambiguity(unittest.TestCase):
    def test_denial_plus_explicit_request_is_uncertain_not_resolved(self):
        r = classify_intent("Please delete my account. Do not delete my invoices though.")
        self.assertEqual(r.outcome, UNCERTAIN)
        self.assertIn("contradictory", r.reason)

    def test_no_cue_and_no_topic_is_uncertain(self):
        self.assertEqual(outcome("Hello, could you call me back about my parcel?"), UNCERTAIN)
        self.assertEqual(outcome(""), UNCERTAIN)

    def test_uncertain_never_authorises(self):
        self.assertFalse(classify_intent("hello").authorises_deletion)
        self.assertTrue(classify_intent("Please delete my account").authorises_deletion)

    def test_only_removal_of_a_non_account_item_is_still_a_cue_so_it_needs_review_downstream(self):
        # documented limitation: a bare verb with a non-account object is treated as a deletion cue (the lexicon is closed, not semantic)
        self.assertEqual(outcome("Please remove the duplicate item from my order."), DELETION)


@needs_dataset
class RealRequests(unittest.TestCase):
    def test_samples_and_eval_split(self):
        from collections import Counter

        ds = real_dataset()
        self.assertEqual(dict(Counter(outcome(q.request_text) for q in ds.sample_requests.rows)), {DELETION: 32})
        self.assertEqual(dict(Counter(outcome(q.request_text) for q in ds.requests.rows)), {DELETION: 508, NON_DELETION: 10})

    def test_non_deletion_cases_are_corroborated_by_the_intake_claims_but_not_decided_by_them(self):
        ds = real_dataset()
        claims = [a for a in ds.activity.rows if a.event_type == "CLASSIFY_REQUEST"]
        self.assertEqual(len(claims), 10)
        self.assertTrue(all("non-deletion" in a.details for a in claims))


if __name__ == "__main__":
    unittest.main()
