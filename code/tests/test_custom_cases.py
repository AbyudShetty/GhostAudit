import sys
import unittest

from tests.helpers import CODE_DIR

sys.path.insert(0, str(CODE_DIR / "evaluation"))
import custom_cases  # noqa: E402


class CustomCases(unittest.TestCase):
    def test_every_hand_written_expectation_holds(self):
        results = custom_cases.run_all()
        self.assertEqual(len(results), 7)
        self.assertGreaterEqual(len({r[0] for r in results}), 3)  # the specification asks for at least three extra cases
        failed = [r for r in results if not r[2]]
        self.assertEqual(failed, [], "\n".join(f"{k}/{c}: {d}" for k, c, _, d in failed))

    def test_a_wrong_expectation_is_reported_as_a_failure(self):
        case = custom_cases.CASES[1]  # B: clean deletion
        wrong = case._replace(expected={"case_1": custom_cases.Expect("INCOMPLETE", ())})
        (cid, ok, detail), = custom_cases.run_case(wrong)
        self.assertFalse(ok)
        self.assertIn("expected INCOMPLETE", detail)
        wrong2 = case._replace(expected={"case_1": custom_cases.Expect("COMPLETE", custom_cases.CLEAN_PROOFS[:1])})
        self.assertFalse(custom_cases.run_case(wrong2)[0][1])

    def test_cli_exit_code(self):
        self.assertEqual(custom_cases.main(), 0)


if __name__ == "__main__":
    unittest.main()
