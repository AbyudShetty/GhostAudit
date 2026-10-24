import unittest

from tests.helpers import crlf

from ghostaudit.parsers.common import parse_kv
from ghostaudit.parsers.log_parser import LineError, parse_log_line, parse_log_text

# One interleaved-trace fixture per dialect. Each has: a *_started line carrying the subject key,
# an unrelated interleaved line, and a *_completed line carrying the resource_id.
LOGFMT = """2026-09-15T11:51:09Z level=DEBUG service=account_service instance=account-6 trace_id=tr-1 event=profile_lookup_started lookup_key="person3@example.test" attempt=1
2026-09-15T11:51:10Z level=INFO service=search_service instance=search-1 trace_id=tr-2 resource_id=search_9 event=index_query_completed result=hit
2026-09-15T11:51:19Z level=INFO service=account_service instance=account-6 trace_id=tr-1 resource_id=account_4092 event=profile_lookup_completed rows=0 http_status=404
"""
POSLEVEL = """2026-03-15T07:00:00Z INFO service=downstream_delivery_service event=record_lookup seq=1 system="account_service" resource_id="account_022" record_type="USER_PROFILE" result="NOT_FOUND" subject_reference="person7@example.test"
2026-03-15T07:00:00Z WARN service=downstream_delivery_service event=record_lookup seq=2 system="backup_service" resource_id="backup_5237" record_type="BACKUP_ARCHIVE" result="ACTIVE" subject_reference=""
"""
JSONL = """2026-09-10T08:56:17Z {"severity":"DEBUG","service":"account_service","trace_id":"tr-5","event":"profile_lookup_started","lookup_key":"person1@example.test"}
2026-09-10T08:56:20Z {"severity":"DEBUG","service":"account_service","trace_id":"tr-5","resource_id":"account_6633","event":"profile_lookup_completed","rows":"1","http_status":"200"}
"""
BRACKET = """2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-6074163] profile_lookup_started: instance="account-5" lookup_key="person2@example.test" attempt="1"
2026-07-28T05:57:13Z [INFO] [account_service] [tr-6074163] profile_lookup_completed: instance="account-5" resource_id="account_9549" rows="0" http_status="404"
2026-07-28T05:51:16Z [DEBUG] [log_router] [collector-9] export_started: instance="collector-9" sources="platform_services"
"""
PIPE = """2026-07-03T07:51:49Z | DEBUG | analytics_platform | tr-4862866 | analytics_profile_scan_started | instance="analytics-5" lookup_key="analytics_257" attempt="1"
2026-07-03T07:51:51Z | WARN | analytics_platform | tr-4862866 | analytics_identity_scan | resource_id="analytics_4527" matched_value="person4@example.test" direct_identifiers="1"
2026-07-03T07:51:30Z | DEBUG | log_router | collector-5 | export_started | instance="collector-5" sources="platform_services"
"""
SYSLOG = """2026-08-22T10:51:11Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG instance="account-2" event="profile_lookup_started" lookup_key="person5@example.test"
2026-08-22T10:51:17Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG instance="account-2" resource_id="account_1785" event="profile_lookup_completed" rows="1" http_status="200"
"""
REQUESTID = (
    '2026-06-08T09:53:40Z requestId=tr-7654854 level=DEBUG function=account_service message="instance=\\"account-4\\" event=\\"profile_lookup_started\\" lookup_key=\\"person6@example.test\\" attempt=\\"1\\""\n'
    '2026-06-08T09:53:48Z requestId=tr-7654854 level=INFO function=account_service message="instance=\\"account-4\\" resource_id=\\"account_4226\\" event=\\"profile_lookup_completed\\" rows=\\"0\\" http_status=\\"404\\""\n'
    '2026-06-08T06:52:07Z requestId=collector-1 level=DEBUG function=log_router message="instance=\\"collector-1\\" event=\\"export_started\\""\n'
)

DIALECTS = {
    # dialect: (text, non-blank lines, observations, first observation's resource, subject-linked resource, subject key)
    "logfmt": (LOGFMT, 3, 2, "search_9", "account_4092", "person3@example.test"),
    "logfmt-poslevel": (POSLEVEL, 2, 2, "account_022", None, None),
    "jsonl": (JSONL, 2, 1, "account_6633", "account_6633", "person1@example.test"),
    "bracket": (BRACKET, 3, 1, "account_9549", "account_9549", "person2@example.test"),
    "pipe": (PIPE, 3, 1, "analytics_4527", "analytics_4527", "person4@example.test"),
    "syslog": (SYSLOG, 2, 1, "account_1785", "account_1785", "person5@example.test"),
    "requestid": (REQUESTID, 3, 1, "account_4226", "account_4226", "person6@example.test"),
}


class EveryDialect(unittest.TestCase):
    def test_dialect_detected_counted_and_accounted(self):
        for dialect, (text, n_lines, n_obs, first_rid, _srid, _subject) in DIALECTS.items():
            with self.subTest(dialect=dialect):
                r = parse_log_text("evidence_x", "x.log", text)
                self.assertEqual(r.failures, [])
                self.assertEqual(r.meta["dialects"], {dialect: n_lines})
                self.assertEqual((r.input_units, len(r.observations)), (n_lines, n_obs))
                self.assertEqual(r.observations[0].resource_id, first_rid)
                self.assertTrue(r.accounting_ok())

    def test_subject_key_from_started_line_is_linked_through_trace(self):
        for dialect, (text, _n, _o, _first, rid, subject) in DIALECTS.items():
            if subject is None:
                continue
            with self.subTest(dialect=dialect):
                r = parse_log_text("evidence_x", "x.log", text)
                obs = [o for o in r.observations if o.resource_id == rid][0]
                self.assertTrue(obs.trace_events, "completed line must carry its sibling trace lines")
                emails = {i.norm for i in obs.identifiers() if i.kind == "email"}
                self.assertIn(subject.lower(), emails)

    def test_interleaved_unrelated_trace_is_not_attached(self):
        r = parse_log_text("e", "x.log", LOGFMT)
        search_obs = [o for o in r.observations if o.resource_id == "search_9"][0]
        self.assertEqual(search_obs.trace_events, [])
        acct = [o for o in r.observations if o.resource_id == "account_4092"][0]
        self.assertEqual([e["line"] for e in acct.trace_events], [1])

    def test_record_lookup_system_is_the_audited_system_not_the_reporter(self):
        o = parse_log_text("e", "x.log", POSLEVEL).observations[0]
        self.assertEqual(o.system_id, "account_service")
        self.assertEqual(o.fields["service"], "downstream_delivery_service")
        self.assertEqual(o.observed_at, "2026-03-15T07:00:00Z")

    def test_requestid_message_unescaped(self):
        o = parse_log_text("e", "x.log", REQUESTID).observations[0]
        self.assertEqual(o.fields["http_status"], "404")
        self.assertEqual(o.fields["event"], "profile_lookup_completed")
        self.assertEqual(o.fields["service"], "account_service")

    def test_values_are_not_coerced(self):
        o = parse_log_text("e", "x.log", LOGFMT).observations[1]
        self.assertEqual(o.fields["rows"], "0")  # stays a string; interpretation is downstream


class Robustness(unittest.TestCase):
    def test_crlf_equals_lf(self):
        for dialect, (text, *_rest) in DIALECTS.items():
            with self.subTest(dialect=dialect):
                a = parse_log_text("e", "x.log", text)
                b = parse_log_text("e", "x.log", crlf(text))
                self.assertEqual([(o.locator, o.fields) for o in a.observations], [(o.locator, o.fields) for o in b.observations])
                self.assertEqual(a.input_units, b.input_units)
                CR = chr(13)
                self.assertTrue(all(CR not in o.raw and all(CR not in str(v) for v in o.fields.values()) for o in b.observations))
                self.assertEqual([o.raw for o in a.observations], [o.raw for o in b.observations])

    def test_blank_lines_ignored_and_not_counted(self):
        r = parse_log_text("e", "x.log", "\n\n" + LOGFMT + "\n\n")
        self.assertEqual(r.input_units, 3)
        self.assertEqual(r.observations[0].locator, "line:4")  # 1-based physical line number

    def test_failures_are_reported_never_dropped(self):
        text = LOGFMT + "this is not a log line\n2026-13-45T99:99:99Z level=INFO resource_id=x\n2026-09-15T11:51:19Z level=INFO resource_id=\"unterminated event=1\n"
        r = parse_log_text("e", "x.log", text)
        self.assertEqual(len(r.failures), 3)
        self.assertEqual(r.failed_units, 3)
        self.assertEqual(len(r.observations), 2)
        self.assertTrue(r.accounting_ok())
        reasons = " | ".join(f.reason for f in r.failures)
        self.assertIn("timestamp", reasons)

    def test_unknown_dialect(self):
        with self.assertRaises(LineError):
            parse_log_line(1, "2026-09-15T11:51:19Z totally different shape")

    def test_malformed_dialect_lines(self):
        for bad in ["2026-09-15T11:51:19Z [INFO] [svc] missing-trace-bracket", "2026-09-15T11:51:19Z | INFO | only-two",
                    "2026-09-15T11:51:19Z <15>1 host svc", "2026-09-15T11:51:19Z {not json}", "2026-09-15T11:51:19Z [1,2]",
                    "2026-09-15T11:51:19Z requestId=tr-1 level=INFO"]:
            with self.subTest(bad=bad), self.assertRaises(LineError):
                parse_log_line(1, bad)

    def test_duplicate_keys_warn_and_keep_first(self):
        r = parse_log_text("e", "x.log", "2026-09-15T11:51:19Z level=INFO service=s resource_id=account_1 resource_id=account_2\n")
        self.assertEqual(r.observations[0].resource_id, "account_1")
        self.assertEqual(len(r.warnings), 1)
        self.assertNotIn("__dup__", r.observations[0].fields)

    def test_null_resource_id_is_a_warning_and_not_evidence(self):
        r = parse_log_text("e", "x.log", "2026-09-15T11:51:19Z level=INFO service=s resource_id=null event=x\n")
        self.assertEqual((len(r.observations), r.non_evidence_units, len(r.warnings)), (0, 1, 1))
        self.assertTrue(r.accounting_ok())

    def test_utf8_values_survive(self):
        r = parse_log_text("e", "x.log", '2026-09-15T11:51:19Z level=INFO service=s resource_id=support_1 note="Müller 田中"\n')
        self.assertEqual(r.observations[0].fields["note"], "Müller 田中")

    def test_trace_events_do_not_mutate_between_runs(self):
        a = parse_log_text("e", "x.log", LOGFMT)
        b = parse_log_text("e", "x.log", LOGFMT)
        self.assertEqual([o.trace_events for o in a.observations], [o.trace_events for o in b.observations])


class KeyValueTokenizer(unittest.TestCase):
    def test_quoted_values_with_spaces_and_escapes(self):
        kv, residue = parse_kv('a=1 b="x y" c="q\\"uote" d=null e=')
        self.assertEqual(kv, {"a": "1", "b": "x y", "c": 'q"uote', "d": "null", "e": ""})
        self.assertEqual(residue, "")

    def test_residue_is_reported(self):
        kv, residue = parse_kv('a=1 junk b=2')
        self.assertEqual(kv, {"a": "1"})
        self.assertEqual(residue, "junk b=2")


if __name__ == "__main__":
    unittest.main()
