import json
import unittest
from unittest.mock import MagicMock, patch

from llm_evaluation import MAX_MODEL_RESPONSE_BYTES, classify_batch, ollama_schema


class ModelTransportValidationTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "evaluation_id": "G0001", "transacted_at": "2001-01-02",
            "merchant": "SAMPLE CAFE", "description": "SAMPLE PURCHASE",
            "amount": 123, "account_type": "depository", "account_subtype": "checking",
            "occurrence_count": 1, "first_date": "2001-01-02", "last_date": "2001-01-02",
            "all_have_transfer_match": False,
        }
        self.prediction = {
            "id": "G0001", "category": "EXAMPLE CATEGORY", "confidence": 2,
            "reason": "EXAMPLE explanation",
        }

    def classify(self, raw):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = raw
        with patch("llm_evaluation.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = response
            result = classify_batch("example-model", ["EXAMPLE CATEGORY"], [self.row])
        return result, response

    def envelope(self, content=None, **changes):
        return json.dumps({
            "message": {"content": json.dumps({"results": [self.prediction]})
                        if content is None else content},
            "done": True, **changes,
        }).encode()

    def test_valid_response_is_read_with_a_fixed_byte_limit(self):
        result, response = self.classify(self.envelope())
        self.assertEqual(result, [self.prediction])
        response.__enter__.return_value.read.assert_called_once_with(MAX_MODEL_RESPONSE_BYTES + 1)
        self.assertEqual(ollama_schema(["EXAMPLE CATEGORY"])["properties"]["results"]
                         ["items"]["properties"]["reason"]["maxLength"], 400)

    def test_oversized_response_is_rejected_before_decoding(self):
        with self.assertRaisesRegex(ValueError, "too much"):
            self.classify(b" " * (MAX_MODEL_RESPONSE_BYTES + 1))

    def test_malformed_envelopes_and_incomplete_generation_are_rejected(self):
        for raw in (
            b"not-json", b"\xff", b"[]", b"null", b"{}",
            b'{"message":[]}', b'{"message":{"content":[]}}',
            self.envelope(done=False), self.envelope(done_reason="length"),
            self.envelope(message={"content": "{}", "tool_calls": [{"function": "EXAMPLE"}]}),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.classify(raw)

    def test_nested_task_envelope_must_contain_only_a_complete_result_list(self):
        for content in (
            "not-json", "[]", "null", "{}", '{"results":null}',
            '{"results":{}}', '{"results":[null]}',
            json.dumps({"results": [self.prediction], "debug": "EXAMPLE PRIVATE CONTENT"}),
            json.dumps({"results": [self.prediction, self.prediction]}),
            json.dumps({"results": [{**self.prediction, "id": "G9999"}]}),
            json.dumps({"results": [{**self.prediction, "account_id": "sample-other-account"}]}),
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.classify(self.envelope(content))

    def test_duplicate_json_fields_are_rejected_at_both_levels(self):
        duplicate_task = ('{"results":[{"id":"G0001","category":"EXAMPLE CATEGORY",'
                          '"category":"EXAMPLE CATEGORY","confidence":2,"reason":""}]}')
        for raw in (
            b'{"message":{"content":"{}"},"message":{"content":"{}"}}',
            self.envelope('{"results":[],"results":[]}'),
            self.envelope(duplicate_task),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.classify(raw)

    def test_transport_validation_errors_do_not_echo_response_content(self):
        marker = "EXAMPLE PRIVATE RESPONSE CONTENT"
        for raw in (self.envelope(marker), self.envelope(message=marker)):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                self.classify(raw)
            self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
