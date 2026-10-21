"""Unit tests for the pi backend. Run: python3 test_scan.py"""

import json
import os
import tempfile
import unittest

import scan


def line(entry):
    return json.dumps(entry) + "\n"


def assistant(timestamp, model="muse-spark-1.3", response=None, **usage):
    message = {
        "role": "assistant",
        "model": model,
        "usage": dict({"input": 0, "cacheRead": 0, "cacheWrite": 0,
                       "output": 0, "totalTokens": 0, "cost": {"total": 0}},
                      **usage),
        "timestamp": timestamp,
    }
    if response:
        message["responseId"] = response
    entry = {"type": "message", "id": "e", "parentId": None,
             "timestamp": "2026-10-21T16:00:00.000Z", "message": message}
    return entry


class PiSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = scan.os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        scan.os.environ["PI_CODING_AGENT_SESSION_DIR"] = self.tmp.name
        self.dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.dir)

    def tearDown(self):
        if self.old is None:
            scan.os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        else:
            scan.os.environ["PI_CODING_AGENT_SESSION_DIR"] = self.old
        self.tmp.cleanup()

    def write(self, name, entries):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.writelines(line(entry) for entry in entries)

    def rows(self):
        totals = scan.Totals()
        scan.pi_sessions(totals)
        return totals.export("")

    def test_splits_token_kinds(self):
        self.write("a.jsonl", [
            assistant(1792563225815, response="r1", input=1000, cacheRead=2000,
                      cacheWrite=300, output=40, totalTokens=3340,
                      cost={"total": 0.5}),
        ])
        self.assertEqual(self.rows(), [["2026-10-22", "pi", "muse-spark-1.3", 1,
                                        1000, 2000, 300, 40, 0.5]])

    def test_response_id_dedupes_forks(self):
        copy = assistant(1792563225815, response="r1", input=100, output=10,
                         totalTokens=110)
        self.write("a.jsonl", [copy])
        self.write("b.jsonl", [copy])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][3], 1)

    def test_timestamp_dedupes_without_response_id(self):
        copy = assistant(1792563225815, input=100, output=10, totalTokens=110)
        self.write("a.jsonl", [copy])
        self.write("b.jsonl", [copy])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][3], 1)

    def test_skips_non_assistant_and_empty_usage(self):
        tool = assistant(1792563225815, response="r1", input=100, output=10)
        tool["message"]["role"] = "toolResult"
        self.write("a.jsonl", [
            tool,
            assistant(1792563225815, response="r2"),
            {"type": "message", "id": "x", "parentId": None,
             "timestamp": "2026-10-21T16:00:00.000Z",
             "message": {"role": "user", "content": "hi"}},
        ])
        self.assertEqual(self.rows(), [])

    def test_reads_custom_session_dir(self):
        os.makedirs(os.path.join(self.tmp.name, "nested", "more"))
        path = os.path.join(self.tmp.name, "nested", "more", "a.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(line(assistant(1792563225815, response="r1", input=7, output=3)))
        self.assertEqual(self.rows()[0][4:8], [7, 0, 0, 3])


if __name__ == "__main__":
    unittest.main()
