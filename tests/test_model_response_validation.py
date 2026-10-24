import copy
import sqlite3
import unittest
from unittest.mock import patch

import category_review as review
from llm_evaluation import (
    classify_evaluation_rows,
    prepare_evaluation,
    save_categorization_batch,
    validate_prediction,
)
from schema import create_schema


class PredictionValidationTests(unittest.TestCase):
    categories = ["EXAMPLE DINING", "Transfer"]
    row = {"evaluation_id": "G0001", "all_have_transfer_match": False}

    def prediction(self, **changes):
        return {
            "id": "G0001", "category": "EXAMPLE DINING", "confidence": 2,
            "reason": "EXAMPLE explanation", **changes,
        }

    def test_accepts_only_exact_task_fields_and_types(self):
        valid = self.prediction(reason="X" * 400)
        self.assertEqual(validate_prediction(valid, self.row, self.categories),
                         {**valid, "status": "categorized"})
        invalid = [None, [], list(valid.items())]
        invalid.extend({key: value for key, value in valid.items() if key != field}
                       for field in valid)
        invalid.extend(self.prediction(**changes) for changes in (
            {"id": "G9999"}, {"id": 1}, {"category": None},
            {"category": []}, {"category": "EXAMPLE UNKNOWN"},
            {"confidence": True}, {"confidence": False},
            {"confidence": 1.0}, {"confidence": 2.0},
            {"confidence": "2"}, {"confidence": None}, {"confidence": 3},
            {"reason": None}, {"reason": []}, {"reason": "X" * 401},
            {"transaction_ids": ["sample-other-account"]},
            {"account_id": "sample-account-b"}, {"description": "SAMPLE OTHER"},
            {"allow_recategorization": True}, {"status": "categorized"},
        ))
        for prediction in invalid:
            with self.subTest(prediction=prediction), self.assertRaises(ValueError):
                validate_prediction(prediction, self.row, self.categories)

    def test_abstention_does_not_bypass_structural_validation(self):
        for changes in (
            {"confidence": 0, "category": "EXAMPLE UNKNOWN"},
            {"confidence": 0, "reason": None},
            {"confidence": 0, "transaction_ids": ["sample-other-account"]},
            {"category": "", "confidence": False},
            {"category": "", "confidence": "2"},
            {"category": "", "id": "G9999"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_prediction(self.prediction(**changes), self.row, self.categories)

    def test_abstention_and_unmatched_transfer_remain_supported(self):
        for changes in ({"confidence": 0}, {"category": ""}, {"category": "Transfer"}):
            with self.subTest(changes=changes):
                result = validate_prediction(self.prediction(**changes), self.row, self.categories)
                self.assertEqual((result["status"], result["category"], result["confidence"]),
                                 ("uncategorized", "", 0))
        row = {**self.row, "all_have_transfer_match": True}
        result = validate_prediction(self.prediction(category="Transfer"), row, self.categories)
        self.assertEqual((result["status"], result["category"]), ("categorized", "Transfer"))

    def test_validation_errors_do_not_echo_model_content(self):
        marker = "EXAMPLE PRIVATE MODEL CONTENT"
        with self.assertRaises(ValueError) as caught:
            validate_prediction(self.prediction(category=marker, reason=marker),
                                self.row, self.categories)
        self.assertNotIn(marker, str(caught.exception))


class CategorizationScopeTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        create_schema(self.connection)
        self.connection.execute(
            "INSERT INTO connections (id, owner_name, institution, access_token) "
            "VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', '')")
        self.connection.executemany(
            "INSERT INTO accounts (id, connection_id, institution, name, type, subtype) "
            "VALUES (?, 1, 'EXAMPLE BANK', 'EXAMPLE ACCOUNT', 'depository', 'checking')",
            [("sample-account-a",), ("sample-account-b",)])
        self.connection.executemany(
            "INSERT INTO transactions (id, account_id, amount, currency, description, "
            "merchant, pending, transacted_at, category, category_override, category_override_source) "
            "VALUES (?, ?, 123, 'USD', ?, '', 0, '2001-01-02', 'Uncategorized', ?, ?)",
            [
                ("sample-target", "sample-account-a", "SAMPLE CAFE", None, None),
                ("sample-repeat", "sample-account-a", "SAMPLE CAFE", None, None),
                ("sample-manual", "sample-account-a", "SAMPLE CAFE", "EXAMPLE MANUAL", "user"),
                ("sample-saved-rule", "sample-account-a", "SAMPLE USER RULE", None, None),
                ("sample-other-account", "sample-account-b", "SAMPLE OTHER", None, None),
            ])
        self.connection.executemany(
            "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) "
            "VALUES (?, 'description', ?, 'EXAMPLE SAVED', ?)",
            [("sample-account-a", "SAMPLE USER RULE", "user"),
             ("sample-account-b", "SAMPLE OTHER", "model")])
        self.prepared = prepare_evaluation(
            self.connection, ["EXAMPLE DINING"],
            transaction_ids=["sample-target", "sample-repeat", "sample-manual", "sample-saved-rule"])
        self.rows = self.prepared["groups"]
        self.assertEqual(len(self.rows), 2)
        self.predictions = [
            {"id": row["evaluation_id"], "category": "EXAMPLE DINING", "confidence": 2,
             "reason": "EXAMPLE explanation"}
            for row in self.rows
        ]

    def snapshot(self):
        return {
            table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("transactions", "merchant_rules", "settings")
        }

    def classify_and_save(self, predictions):
        with patch("llm_evaluation.classify_batch", return_value=predictions):
            details = classify_evaluation_rows(self.prepared, self.rows)
        save_categorization_batch(self.connection, self.prepared, [], details)
        return details

    def test_revalidates_complete_batch_before_saving(self):
        first, second = self.predictions
        foreign = {**second, "id": "G9999"}
        for predictions in (
            [first, first, second], [first], [first, second, foreign],
            [first, foreign], [], None, {"results": self.predictions},
            [first, {**second, "confidence": True}],
            [first, {**second, "transaction_ids": ["sample-other-account"]}],
        ):
            with self.subTest(predictions=predictions):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.classify_and_save(predictions)
                self.assertEqual(self.snapshot(), before)

    def test_ordinary_batch_keeps_recategorization_disabled(self):
        prepared = prepare_evaluation(self.connection, ["EXAMPLE DINING"])
        self.assertEqual(len(prepared["groups"]), 1)
        with patch("llm_evaluation.classify_batch", return_value=[self.predictions[0]]):
            details = classify_evaluation_rows(prepared, prepared["groups"])
        self.assertIs(details[0]["allow_recategorization"], False)
        self.assertEqual(set(details[0]["transaction_ids"]), {"sample-target", "sample-repeat"})

    def test_category_review_rejects_entire_invalid_batch_without_partial_progress(self):
        self.connection.executemany(
            "INSERT INTO category_rules (name, flow_type) VALUES (?, 'spending')",
            [("EXAMPLE DINING",), ("EXAMPLE SAVED",), ("EXAMPLE MANUAL",)])
        result, groups, examples = review.prepare(self.connection)
        self.assertGreaterEqual(len(groups), 2)
        predictions = [
            {"id": group["evaluation_id"], "category": "EXAMPLE DINING",
             "confidence": 2, "reason": "EXAMPLE explanation"}
            for group in groups
        ]
        first, second, *remaining = predictions
        for invalid in (
            [first, {**second, "category": "EXAMPLE UNKNOWN"}, *remaining],
            [first, {**second, "transaction_ids": ["sample-other-account"]}, *remaining],
            [first, {**second, "account_id": "sample-account-b"}, *remaining],
            [first, {**second, "confidence": True}, *remaining],
            [first, first, *remaining], predictions[:-1],
            [first, {**second, "id": "G9999"}, *remaining],
        ):
            with self.subTest(predictions=invalid):
                before_result = copy.deepcopy(result)
                before_database = self.snapshot()
                with patch("category_review.classify_batch", return_value=invalid):
                    with self.assertRaises(ValueError):
                        review.review_batch(result, groups, examples)
                self.assertEqual(result, before_result)
                self.assertEqual(self.snapshot(), before_database)

    def test_model_fields_cannot_redirect_transactions_or_rules_to_another_account(self):
        attacks = (
            {"transaction_ids": ["sample-other-account"]},
            {"account_id": "sample-account-b", "description": "SAMPLE OTHER"},
            {"allow_recategorization": True, "status": "categorized"},
            {"transaction_ids": ["sample-other-account"], "account_id": "sample-account-b",
             "description": "SAMPLE OTHER", "allow_recategorization": True},
        )
        for extras in attacks:
            with self.subTest(extras=extras):
                before = self.snapshot()
                predictions = [dict(item) for item in self.predictions]
                predictions[0].update(extras)
                with self.assertRaises(ValueError):
                    self.classify_and_save(predictions)
                self.assertEqual(self.snapshot(), before)

    def test_valid_batch_uses_local_scope_and_preserves_manual_choices(self):
        before_other = self.connection.execute(
            "SELECT * FROM transactions WHERE id = 'sample-other-account'").fetchone()
        details = self.classify_and_save(list(reversed(self.predictions)))
        for row, item in zip(self.rows, details):
            self.assertEqual(item["id"], row["evaluation_id"])
            self.assertEqual(item["account_id"], row["account_id"])
            self.assertEqual(item["transaction_ids"], row["transaction_ids"])
            self.assertEqual(item["description"], row["description"])
            self.assertIs(item["allow_recategorization"], True)
        self.assertEqual(tuple(self.connection.execute(
            "SELECT * FROM transactions WHERE id = 'sample-other-account'").fetchone()), tuple(before_other))
        manual = self.connection.execute(
            "SELECT category_override, category_override_source FROM transactions "
            "WHERE id = 'sample-manual'").fetchone()
        self.assertEqual(tuple(manual), ("EXAMPLE MANUAL", "user"))
        rules = [tuple(row) for row in self.connection.execute(
            "SELECT account_id, match_value, category, source FROM merchant_rules ORDER BY account_id, match_value")]
        self.assertEqual(rules, [
            ("sample-account-a", "SAMPLE CAFE", "EXAMPLE DINING", "model"),
            ("sample-account-a", "SAMPLE USER RULE", "EXAMPLE SAVED", "user"),
            ("sample-account-b", "SAMPLE OTHER", "EXAMPLE SAVED", "model"),
        ])


if __name__ == "__main__":
    unittest.main()
