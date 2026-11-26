"""Regression checks for incorrect mock traces that used to pass validation."""
import json
import unittest
from copy import deepcopy

import yaml

from verify import Contract, HERE, check_scenario


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = Contract(yaml.safe_load((HERE / "openapi.yaml").read_text(encoding="utf-8")))
        cases = json.loads((HERE / "examples.json").read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cases}

    def setUp(self):
        self.docs = deepcopy(self.cases["c-docs"])
        self.tender = deepcopy(self.cases["c-tender"])

    def assert_invalid(self, case, message):
        with self.assertRaisesRegex(ValueError, message):
            check_scenario(self.contract, case)

    def test_independent_readers_observe_one_history(self):
        reads = [step for step in self.tender["steps"] if step["operationId"] == "readEvents"]
        first_reader = next(step for step in reads if step["role"] == "customer")
        second_reader = next(step for step in reads if step["role"] == "manager")
        self.assertEqual(first_reader["params"]["after"], 0)
        self.assertEqual(second_reader["params"]["after"], 0)
        unique_seqs = {event["seq"] for step in reads for event in step["response"]["events"]}
        self.assertEqual(check_scenario(self.contract, self.tender), len(unique_seqs))
        second_reader["response"]["events"][0]["data"]["text"] = "Другая история"
        self.assert_invalid(self.tender, "Existing journal event changed")

    def test_plain_message_cannot_confirm_quote(self):
        step = next(s for s in self.docs["steps"] if s.get("key") == "confirm3")
        step["body"] = {"type": "message", "text": "Пока отложим покупку."}
        self.assert_invalid(self.docs, "Confirmation requires buyer consent")

    def test_document_must_match_requested_kind(self):
        for step in self.docs["steps"]:
            if step.get("body", {}).get("type") == "request_document" and step.get("response", {}).get("actionId") == "d7":
                step["body"]["kind"] = "quote_pdf"
        self.assert_invalid(self.docs, "Document does not match its issuance command")

    def test_question_does_not_close_a_handoff(self):
        for step in self.tender["steps"]:
            if step.get("body", {}).get("type") == "close":
                step["body"]["type"] = "reply"
        self.assert_invalid(self.tender, "Closing a handoff requires an explicit decision")

    def test_pending_duplicate_returns_original_receipt(self):
        accepted = [s for s in self.docs["steps"] if s.get("body", {}).get("kind") == "demo_invoice"
                    and s["status"] == 202]
        pending_duplicate = next(s for s in accepted if s["key"] != accepted[0]["key"])
        pending_duplicate["response"]["actionId"] = "second-issuance"
        self.assert_invalid(self.docs, "Pending issuance must reuse its original receipt")

    def test_published_document_is_returned_unchanged(self):
        step = next(s for s in self.docs["steps"] if s["operationId"] == "submitAction" and s["status"] == 200)
        step["response"]["documentId"] = "different-file"
        step["response"]["url"] = "/api/v1/documents/different-file"
        self.assert_invalid(self.docs, "Repeated issuance must return the original document")

    def test_original_key_keeps_202_after_publication(self):
        replays = [s for s in self.docs["steps"] if s.get("key") == "bill"]
        existing = next(s for s in self.docs["steps"] if s["operationId"] == "submitAction" and s["status"] == 200)
        replays[-1].update(status=200, response=existing["response"])
        self.assert_invalid(self.docs, "Retry changed the original status or response")

    def test_fresh_key_cannot_reissue_archived_quote(self):
        quote_pdf = next(event["data"]["document"] for step in self.docs["steps"]
                         for event in step.get("response", {}).get("events", [])
                         if event["data"].get("document", {}).get("kind") == "quote_pdf")
        stale = next(step for step in self.docs["steps"] if step.get("key") == "stale")
        stale["body"]["kind"] = "quote_pdf"
        stale.update(status=200, response=quote_pdf)
        self.assert_invalid(self.docs, "A new issuance key requires the current quote revision")


if __name__ == "__main__":
    unittest.main()
