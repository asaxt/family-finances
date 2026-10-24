"""Freshness checks through real endpoints, using only temporary encrypted fixtures."""
import copy
import unittest
from unittest.mock import patch

from analytics import transaction_list
from llm_evaluation import classify_evaluation_rows as original_classify_rows
from tests import test_development_mode as support


class CategorizationFreshnessAppTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting or rediscovering the unrelated suite.
    tearDown = support.DevelopmentModeTests.tearDown
    csrf_token = staticmethod(support.DevelopmentModeTests.csrf_token)
    complete_setup = support.DevelopmentModeTests.complete_setup
    complete_category_setup = support.DevelopmentModeTests.complete_category_setup
    set_local_ai = support.DevelopmentModeTests.set_local_ai

    def setUp(self):
        support.DevelopmentModeTests.setUp(self)
        self.import_classifier.stop()
        self.complete_setup()
        self.set_local_ai(True)
        with self.application.db() as connection:
            connection.execute(
                "INSERT INTO connections (id, owner_name, institution, access_token) "
                "VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', '')"
            )
            connection.execute(
                "INSERT INTO accounts (id, connection_id, institution, name, type, subtype, "
                "cash_flow_role, spending_enabled) VALUES "
                "('sample-account', 1, 'EXAMPLE BANK', 'EXAMPLE ACCOUNT', "
                "'depository', 'checking', 'cash_flow', 1)"
            )
            connection.executemany(
                "INSERT INTO category_rules (name, flow_type) VALUES (?, 'spending')",
                [("EXAMPLE STALE",), ("EXAMPLE FRESH",), ("EXAMPLE USER",)],
            )
            connection.execute(
                "INSERT INTO transactions (id, account_id, amount, currency, description, "
                "merchant, pending, transacted_at, category) VALUES "
                "('sample-target', 'sample-account', 123, 'USD', 'SAMPLE ORIGINAL PURCHASE', "
                "'SAMPLE SHOP', 0, '2001-01-02', 'Uncategorized')"
            )
        self.token = self.csrf_token(self.client.get("/transactions"))

    def classified(self, prepared, rows, category):
        predictions = [{
            "id": row["evaluation_id"], "category": category,
            "confidence": 2, "reason": "EXAMPLE purchase purpose",
        } for row in rows]
        with patch("llm_evaluation.classify_batch", return_value=predictions):
            return original_classify_rows(prepared, rows)

    def ordinary_request(self):
        return self.client.post(
            "/api/local-ai/evaluation", json={}, headers={"X-CSRF-Token": self.token},
        )

    def seed_import(self):
        with self.application.db() as connection:
            connection.execute(
                "INSERT INTO transactions (id, account_id, amount, currency, description, "
                "merchant, pending, transacted_at, category) VALUES "
                "('sample-outside', 'sample-account', 456, 'USD', 'SAMPLE OUTSIDE PURCHASE', "
                "'SAMPLE OTHER SHOP', 0, '2001-01-03', 'Uncategorized')"
            )
            self.application.statements.write_setting(
                connection, self.application.statements.HISTORY_KEY,
                [{"id": "sample-import", "transaction_ids": ["sample-target"]}],
            )

    def statement_request(self):
        return self.client.post(
            "/statement-import/history/sample-import/classify", data={"csrf_token": self.token},
        )

    def assert_fresh_category_persisted(self):
        with self.application.db() as connection:
            rows = {row["id"]: row for row in transaction_list(connection)}
            self.assertEqual(rows["sample-target"]["effective_category"], "EXAMPLE FRESH")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM merchant_rules WHERE category='EXAMPLE STALE'"
            ).fetchone()[0], 0)
        self.assertNotIn(b"SAMPLE CHANGED PURCHASE", self.application.vault.path.read_bytes())
        self.application.lock_data()
        self.application.unlock_data(self.password)
        with self.application.db() as connection:
            rows = {row["id"]: row for row in transaction_list(connection)}
            self.assertEqual(rows["sample-target"]["effective_category"], "EXAMPLE FRESH")

    def test_ordinary_endpoint_retries_changed_transaction_and_saves_only_fresh_prediction(self):
        calls = []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            if len(calls) == 1:
                with self.application.db() as connection:
                    connection.execute(
                        "UPDATE transactions SET description='SAMPLE CHANGED PURCHASE' "
                        "WHERE id='sample-target'"
                    )
            return self.classified(prepared, rows, "EXAMPLE STALE" if len(calls) == 1 else "EXAMPLE FRESH")

        with patch.object(self.application, "classify_evaluation_rows", side_effect=classify):
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0]["description"], "SAMPLE ORIGINAL PURCHASE")
        self.assertEqual(calls[1][0]["description"], "SAMPLE CHANGED PURCHASE")
        with self.application.db() as connection:
            result = self.application.load_ollama_result(connection)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["category"] for item in result["details"]], ["EXAMPLE FRESH"])
        self.assert_fresh_category_persisted()

    def test_ordinary_endpoint_preserves_a_user_rule_added_during_inference(self):
        calls = []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            with self.application.db() as connection:
                connection.execute(
                    "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) "
                    "VALUES ('sample-account', 'description', 'SAMPLE ORIGINAL PURCHASE', "
                    "'EXAMPLE USER', 'user')"
                )
            return self.classified(prepared, rows, "EXAMPLE STALE")

        with patch.object(self.application, "classify_evaluation_rows", side_effect=classify):
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(len(calls), 1)
        with self.application.db() as connection:
            row = transaction_list(connection)[0]
            self.assertEqual(row["effective_category"], "EXAMPLE USER")
            self.assertIsNone(row["category_override"])
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT category, source FROM merchant_rules"
            )], [("EXAMPLE USER", "user")])

    def test_statement_endpoint_retries_fresh_rows_without_broadening_import_scope(self):
        self.seed_import()
        calls = []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            self.assertFalse(prepared["targeted"])
            self.assertTrue(all(not row["all_have_transfer_match"] for row in rows))
            self.assertEqual({value for row in rows for value in row["transaction_ids"]}, {"sample-target"})
            if len(calls) == 1:
                with self.application.db() as connection:
                    connection.execute(
                        "UPDATE transactions SET description='SAMPLE CHANGED PURCHASE' "
                        "WHERE id='sample-target'"
                    )
            return self.classified(prepared, rows, "EXAMPLE STALE" if len(calls) == 1 else "EXAMPLE FRESH")

        with patch.object(self.application, "classify_evaluation_rows", side_effect=classify):
            response = self.statement_request()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0]["description"], "SAMPLE CHANGED PURCHASE")
        with self.application.db() as connection:
            rows = {row["id"]: row for row in transaction_list(connection)}
            self.assertEqual(rows["sample-outside"]["effective_category"], "Uncategorized")
            history = self.application.statements.read_setting(
                connection, self.application.statements.HISTORY_KEY, [],
            )
        self.assertEqual(history[0]["classification"], {"status": "complete", "remaining": 0})
        self.assert_fresh_category_persisted()

    def test_statement_endpoint_does_not_retry_unrelated_rows_when_user_rule_removes_last_eligible_id(self):
        self.seed_import()
        calls = []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            with self.application.db() as connection:
                connection.execute(
                    "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) "
                    "VALUES ('sample-account', 'description', 'SAMPLE ORIGINAL PURCHASE', "
                    "'EXAMPLE USER', 'user')"
                )
            return self.classified(prepared, rows, "EXAMPLE STALE")

        with patch.object(self.application, "classify_evaluation_rows", side_effect=classify):
            response = self.statement_request()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(calls), 1)
        with self.application.db() as connection:
            rows = {row["id"]: row for row in transaction_list(connection)}
            self.assertEqual(rows["sample-target"]["effective_category"], "EXAMPLE USER")
            self.assertIsNone(rows["sample-target"]["category_override"])
            self.assertEqual(rows["sample-outside"]["effective_category"], "Uncategorized")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM merchant_rules WHERE source='model'"
            ).fetchone()[0], 0)

    def test_ordinary_endpoint_stops_after_bounded_freshness_retries_without_stale_writes(self):
        calls = []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            with self.application.db() as connection:
                connection.execute(
                    "UPDATE transactions SET description=? WHERE id='sample-target'",
                    (f"SAMPLE CHANGED PURCHASE {len(calls)}",),
                )
            return self.classified(prepared, rows, "EXAMPLE STALE")

        with patch.object(self.application, "classify_evaluation_rows", side_effect=classify):
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(len(calls), 3)
        with self.application.db() as connection:
            row = transaction_list(connection)[0]
            self.assertEqual(row["effective_category"], "Uncategorized")
            self.assertIsNone(row["category_override"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)
            result = self.application.load_ollama_result(connection)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["details"], [])

    def test_progress_failure_rolls_back_later_rule_and_restores_earlier_report_details(self):
        with self.application.db() as connection:
            connection.execute(
                "INSERT INTO transactions (id, account_id, amount, currency, description, "
                "merchant, pending, transacted_at, category) VALUES "
                "('sample-opposite', 'sample-account', -456, 'USD', 'SAMPLE ORIGINAL PURCHASE', "
                "'SAMPLE SHOP', 0, '2001-01-03', 'Uncategorized')"
            )
        saved_reports = []
        save_result = self.application.save_ollama_result

        def save_progress(connection, result):
            if result["status"] == "running" and result["processed_group_count"] == 2:
                # The rule and both row updates exist only in this pending transaction.
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 1)
                self.assertTrue(any("rule_match_count" in item for item in result["details"]))
                raise RuntimeError("EXAMPLE progress write failed")
            saved_reports.append(copy.deepcopy(result))
            return save_result(connection, result)

        def classify(prepared, rows):
            return self.classified(prepared, rows, "EXAMPLE FRESH")

        with patch.object(self.application, "classify_evaluation_rows", side_effect=classify), \
             patch.object(self.application, "evaluation_batches",
                          side_effect=lambda prepared: ([group] for group in prepared["groups"])), \
             patch.object(self.application, "save_ollama_result", side_effect=save_progress):
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["ok"])
        committed = next(result for result in saved_reports
                         if result["status"] == "running" and result["processed_group_count"] == 1)
        with self.application.db() as connection:
            interrupted = self.application.load_ollama_result(connection)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["details"], committed["details"])
        self.assertEqual(interrupted["applied_transaction_count"], 1)
        self.assertNotIn("rule_match_count", interrupted["details"][0])
        self.application.lock_data()
        self.application.unlock_data(self.password)
        with self.application.db() as connection:
            rows = {row["id"]: row for row in transaction_list(connection)}
            self.assertEqual(rows["sample-target"]["effective_category"], "EXAMPLE FRESH")
            self.assertEqual(rows["sample-target"]["category_override_source"], "model")
            self.assertEqual(rows["sample-opposite"]["effective_category"], "Uncategorized")
            self.assertIsNone(rows["sample-opposite"]["category_override"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
