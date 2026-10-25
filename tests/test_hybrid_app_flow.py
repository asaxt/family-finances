"""Private mixed-engine app flows over temporary, invented encrypted fixtures."""
import copy
from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from analytics import transaction_list
from hybrid_categorization import HybridCategorizer, HybridConfigurationError
from llm_evaluation import classify_evaluation_rows as original_classify_rows
from tests import test_development_mode as support


class HybridAppFlowTests(unittest.TestCase):
    tearDown = support.DevelopmentModeTests.tearDown
    csrf_token = staticmethod(support.DevelopmentModeTests.csrf_token)
    complete_setup = support.DevelopmentModeTests.complete_setup
    complete_category_setup = support.DevelopmentModeTests.complete_category_setup
    set_local_ai = support.DevelopmentModeTests.set_local_ai

    def setUp(self):
        self.engine_environment = patch.dict(os.environ, {"FAMILY_FINANCES_CATEGORIZATION_ENGINE": "hybrid"})
        self.engine_environment.start()
        self.addCleanup(self.engine_environment.stop)
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
                "INSERT INTO accounts (id, connection_id, institution, name, type, subtype) "
                "VALUES ('sample-account', 1, 'EXAMPLE BANK', 'EXAMPLE ACCOUNT', 'depository', 'checking')"
            )
            connection.executemany(
                "INSERT OR IGNORE INTO category_rules (name, flow_type) VALUES (?, 'spending')",
                [("EXAMPLE DINING",), ("EXAMPLE GROCERIES",), ("EXAMPLE USER",), ("Other",)],
            )
            self.insert_transaction(connection, "sample-target", "SAMPLE ORIGINAL PURCHASE")
        self.token = self.csrf_token(self.client.get("/transactions"))

    @staticmethod
    def insert_transaction(connection, identifier, description):
        connection.execute(
            "INSERT INTO transactions (id, account_id, amount, currency, description, merchant, "
            "pending, transacted_at, category) VALUES (?, 'sample-account', 123, 'USD', ?, "
            "'SAMPLE SHOP', 0, '2001-01-02', 'Uncategorized')", (identifier, description),
        )

    def classified(self, prepared, rows, category="EXAMPLE DINING"):
        predictions = [{"id": row["evaluation_id"], "category": category, "confidence": 1,
                        "reason": "EXAMPLE supported purchase purpose."} for row in rows]
        with patch("llm_evaluation.classify_batch", return_value=predictions):
            return original_classify_rows(prepared, rows)

    def session(self, events, classify):
        @contextmanager
        def factory(engine):
            events.append(("opened", engine))
            try:
                yield SimpleNamespace(classify=classify)
            finally:
                events.append(("closed", engine))
        return factory

    def ordinary_request(self):
        return self.client.post("/api/local-ai/evaluation", json={},
                                headers={"X-CSRF-Token": self.token})

    def financial_state(self):
        with self.application.db() as connection:
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    for table in ("transactions", "merchant_rules", "category_rules")}

    def test_engine_choice_defaults_to_qwen_and_rejects_unknown_names(self):
        with patch.dict(os.environ):
            os.environ.pop("FAMILY_FINANCES_CATEGORIZATION_ENGINE", None)
            self.assertEqual(self.application.selected_categorization_engine(), "qwen")
            os.environ["FAMILY_FINANCES_CATEGORIZATION_ENGINE"] = "hybrid"
            self.assertEqual(self.application.selected_categorization_engine(), "hybrid")
            os.environ["FAMILY_FINANCES_CATEGORIZATION_ENGINE"] = "EXAMPLE INVALID ENGINE"
            with self.assertRaises(ValueError):
                self.application.selected_categorization_engine()

    def test_ordinary_endpoint_uses_one_hybrid_session_and_batches_of_thirty_two(self):
        with self.application.db() as connection:
            for index in range(32):
                self.insert_transaction(connection, f"sample-extra-{index:02d}", f"SAMPLE PURCHASE {index:02d}")
        calls, events = [], []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            self.assertIn("Other", prepared["categories"])
            return self.classified(prepared, rows)

        with patch.object(self.application, "categorization_session", side_effect=self.session(events, classify)) as factory:
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        factory.assert_called_once_with("hybrid")
        self.assertEqual([len(rows) for rows in calls], [32, 1])
        self.assertEqual(events, [("opened", "hybrid"), ("closed", "hybrid")])
        with self.application.db() as connection:
            self.assertTrue(all(row["effective_category"] == "EXAMPLE DINING" for row in transaction_list(connection)))
            self.assertEqual(self.application.load_ai_reviews(connection), {})
            result = self.application.load_ollama_result(connection)
        self.assertEqual(result["applied_transaction_count"], 33)

    def test_hybrid_other_assignments_do_not_create_recurring_rules_or_review_entries(self):
        with self.application.db() as connection:
            self.insert_transaction(connection, "sample-repeat", "SAMPLE ORIGINAL PURCHASE")
        events = []

        def classify(prepared, rows):
            return self.classified(prepared, rows, "Other")

        with patch.object(self.application, "categorization_session", side_effect=self.session(events, classify)):
            response = self.ordinary_request()
        self.assertTrue(response.get_json()["ok"])
        with self.application.db() as connection:
            rows = transaction_list(connection)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["effective_category"] == "Other" for row in rows))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)
            self.assertEqual(self.application.load_ai_reviews(connection), {})

    def test_hybrid_policy_blocks_other_even_when_initial_palette_is_empty(self):
        prepared = {"categories": []}
        self.application.categorization_policy(prepared, "hybrid")
        self.assertIn("other", {name.casefold() for name in prepared["non_recurring_categories"]})

    def test_other_added_after_initial_preparation_still_cannot_become_a_recurring_rule(self):
        with self.application.db() as connection:
            connection.execute("DELETE FROM category_rules WHERE name = 'Other'")

        @contextmanager
        def session_with_new_other(engine):
            # Session initialization happens after the initial palette/policy was
            # prepared, but before the freshness helper rebuilds model inputs.
            with self.application.db() as connection:
                connection.execute("INSERT INTO category_rules (name, flow_type) VALUES ('Other', 'spending')")

            def classify(prepared, rows):
                self.assertIn("Other", prepared["categories"])
                return self.classified(prepared, rows, "Other")

            yield SimpleNamespace(classify=classify)

        with patch.object(self.application, "categorization_session", side_effect=session_with_new_other):
            response = self.ordinary_request()
        self.assertTrue(response.get_json()["ok"])
        with self.application.db() as connection:
            self.assertEqual(transaction_list(connection)[0]["effective_category"], "Other")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)

    def test_settings_identifies_selected_mixed_mode_without_relaxing_mirror_restrictions(self):
        metadata = SimpleNamespace(read_text=lambda: '{"copied_at":"2001-01-02T00:00:00Z"}')
        with patch.object(self.application, "READ_ONLY_MIRROR", True), \
             patch.object(self.application, "MIRROR_METADATA", metadata):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"a fast local classifier handles clear cases", response.data)
        self.assertIn(b"This preview cannot save changes to the production snapshot.", response.data)
        with patch.dict(os.environ, {"FAMILY_FINANCES_CATEGORIZATION_ENGINE": "qwen"}):
            legacy = self.client.get("/settings")
        self.assertNotIn(b"a fast local classifier handles clear cases", legacy.data)

    def test_real_hybrid_adapter_reloads_changed_category_treatment_before_saving(self):
        worker, qwen = Mock(), Mock()
        definitions_seen = []

        def scores(rows, definitions):
            definitions_seen.append(copy.deepcopy(definitions))
            self.assertFalse(rows[0]["all_have_transfer_match"])
            if len(definitions_seen) == 1:
                with self.application.db() as connection:
                    connection.execute("UPDATE category_rules SET flow_type = 'transfer' "
                                       "WHERE name = 'EXAMPLE DINING'")
            else:
                with self.application.db() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)
                    self.assertIsNone(connection.execute(
                        "SELECT category_override FROM transactions WHERE id = 'sample-target'"
                    ).fetchone()[0])
            labels = [*definitions, ""]
            score = {label: 0.1 / (len(labels) - 1) for label in labels}
            score["EXAMPLE DINING"] = 0.9
            return [dict(score) for _ in rows]

        def qwen_prediction(model, categories, rows, **kwargs):
            return [{"id": row["evaluation_id"], "category": "EXAMPLE DINING", "confidence": 2,
                     "reason": "EXAMPLE proposed label."} for row in rows]

        worker.scores.side_effect = scores
        qwen.side_effect = qwen_prediction
        engine = HybridCategorizer(worker=worker, qwen_classify=qwen)
        with patch.object(self.application, "HybridCategorizer", return_value=engine) as constructor:
            response = self.ordinary_request()
        self.assertTrue(response.get_json()["ok"])
        constructor.assert_called_once_with()
        self.assertEqual(worker.scores.call_count, 2)
        qwen.assert_called_once()
        worker.close.assert_called_once()
        self.assertIn("Money out", definitions_seen[0]["EXAMPLE DINING"])
        self.assertIn("Movement between household accounts", definitions_seen[1]["EXAMPLE DINING"])
        with self.application.db() as connection:
            self.assertEqual(transaction_list(connection)[0]["effective_category"], "Other")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)
            result = self.application.load_ollama_result(connection)
        self.assertEqual(result["details"][0]["categorization_source"], "qwen")

    def test_freshness_retry_reuses_session_and_only_saves_refreshed_model_input(self):
        calls, events = [], []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            if len(calls) == 1:
                with self.application.db() as connection:
                    connection.execute("UPDATE transactions SET description = 'SAMPLE CHANGED PURCHASE' "
                                       "WHERE id = 'sample-target'")
            return self.classified(prepared, rows, "EXAMPLE DINING" if len(calls) == 1 else "EXAMPLE GROCERIES")

        with patch.object(self.application, "categorization_session", side_effect=self.session(events, classify)) as factory:
            response = self.ordinary_request()
        self.assertTrue(response.get_json()["ok"])
        factory.assert_called_once_with("hybrid")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0]["description"], "SAMPLE CHANGED PURCHASE")
        self.assertEqual(events, [("opened", "hybrid"), ("closed", "hybrid")])
        with self.application.db() as connection:
            self.assertEqual(transaction_list(connection)[0]["effective_category"], "EXAMPLE GROCERIES")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM merchant_rules WHERE category = 'EXAMPLE DINING'").fetchone()[0], 0)

    def test_user_rule_created_during_hybrid_work_is_preserved_without_another_model_call(self):
        calls, events = [], []

        def classify(prepared, rows):
            calls.append(1)
            with self.application.db() as connection:
                connection.execute(
                    "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) "
                    "VALUES ('sample-account', 'description', 'SAMPLE ORIGINAL PURCHASE', 'EXAMPLE USER', 'user')"
                )
            return self.classified(prepared, rows)

        with patch.object(self.application, "categorization_session", side_effect=self.session(events, classify)):
            response = self.ordinary_request()
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[-1], ("closed", "hybrid"))
        with self.application.db() as connection:
            row = transaction_list(connection)[0]
            self.assertEqual(row["effective_category"], "EXAMPLE USER")
            self.assertIsNone(row["category_override"])
            self.assertEqual([tuple(row) for row in connection.execute("SELECT category, source FROM merchant_rules")],
                             [("EXAMPLE USER", "user")])

    def test_statement_hybrid_session_keeps_import_scope_and_transfer_policy(self):
        with self.application.db() as connection:
            self.insert_transaction(connection, "sample-outside", "SAMPLE OUTSIDE PURCHASE")
            self.application.statements.write_setting(connection, self.application.statements.HISTORY_KEY,
                                                      [{"id": "sample-import", "transaction_ids": ["sample-target"]}])
        events, calls = [], []

        def classify(prepared, rows):
            calls.append(copy.deepcopy(rows))
            self.assertFalse(prepared["targeted"])
            self.assertTrue(prepared["disable_transfer_inference"])
            self.assertTrue(all(not row["all_have_transfer_match"] for row in rows))
            self.assertEqual({identifier for row in rows for identifier in row["transaction_ids"]}, {"sample-target"})
            return self.classified(prepared, rows)

        with patch.object(self.application, "categorization_session", side_effect=self.session(events, classify)) as factory:
            response = self.client.post("/statement-import/history/sample-import/classify",
                                        data={"csrf_token": self.token})
        self.assertEqual(response.status_code, 302)
        factory.assert_called_once_with("hybrid")
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[-1], ("closed", "hybrid"))
        with self.application.db() as connection:
            rows = {row["id"]: row for row in transaction_list(connection)}
            self.assertEqual(rows["sample-target"]["effective_category"], "EXAMPLE DINING")
            self.assertEqual(rows["sample-outside"]["effective_category"], "Uncategorized")

    def test_invalid_engine_configuration_fails_before_financial_writes(self):
        before = self.financial_state()
        with patch.dict(os.environ, {"FAMILY_FINANCES_CATEGORIZATION_ENGINE": "EXAMPLE INVALID ENGINE"}), \
             patch.object(self.application, "categorization_session") as factory:
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 400)
        factory.assert_not_called()
        self.assertEqual(self.financial_state(), before)

    def test_unavailable_hybrid_configuration_returns_generic_error_without_fallback_or_financial_writes(self):
        before = self.financial_state()
        marker = "EXAMPLE PRIVATE STARTUP DETAIL"

        @contextmanager
        def unavailable(engine):
            raise HybridConfigurationError(marker)
            yield  # Make startup failure occur when entering the session.

        with patch.object(self.application, "categorization_session", side_effect=unavailable), \
             patch.object(self.application, "classify_evaluation_rows") as ordinary:
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(marker.encode(), response.data)
        ordinary.assert_not_called()
        self.assertEqual(self.financial_state(), before)
        self.assertFalse(self.application.OLLAMA_EVALUATION_LOCK.locked())

    def test_timeout_closes_hybrid_session_preserves_data_and_allows_a_later_run(self):
        before = self.financial_state()
        events = []
        timed_out = Mock(side_effect=TimeoutError("EXAMPLE worker timeout"))
        with patch.object(self.application, "categorization_session", side_effect=self.session(events, timed_out)):
            response = self.ordinary_request()
        self.assertIn(response.status_code, (200, 503))
        self.assertFalse(response.get_json().get("ok", False))
        self.assertEqual(events, [("opened", "hybrid"), ("closed", "hybrid")])
        self.assertEqual(self.financial_state(), before)
        self.assertFalse(self.application.OLLAMA_EVALUATION_LOCK.locked())
        with patch.object(self.application, "categorization_session", side_effect=self.session([], self.classified)):
            retried = self.ordinary_request()
        self.assertTrue(retried.get_json()["ok"])

    def test_read_only_mirror_cannot_start_a_hybrid_write_session(self):
        before = self.financial_state()
        with patch.object(self.application, "READ_ONLY_MIRROR", True), \
             patch.object(self.application, "categorization_session") as factory:
            response = self.ordinary_request()
        self.assertEqual(response.status_code, 403)
        factory.assert_not_called()
        self.assertEqual(self.financial_state(), before)

    def test_existing_category_review_keeps_its_qwen_review_policy(self):
        with self.application.db() as connection:
            connection.execute("UPDATE transactions SET category = 'EXAMPLE DINING' WHERE id = 'sample-target'")

        def review(model, categories, rows, **kwargs):
            return [{"id": row["evaluation_id"], "category": row["current_category"], "confidence": 2,
                     "reason": "EXAMPLE current label remains supported."} for row in rows]

        with patch.object(self.application, "categorization_session") as factory, \
             patch.object(self.application.category_review, "classify_batch", side_effect=review) as qwen, \
             patch.object(self.application.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
            response = self.client.post("/api/local-ai/category-review/start", headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 200)
        factory.assert_not_called()
        self.assertTrue(qwen.call_args.kwargs["review_existing"])

    def test_assistant_keeps_its_existing_qwen_path(self):
        with patch.object(self.application, "categorization_session") as factory, \
             patch.object(self.application.local_chat, "plan_question", return_value={"scope": "transactions"}) as plan, \
             patch.object(self.application.local_chat, "transaction_context", return_value={"rows": []}), \
             patch.object(self.application.local_chat, "explain", return_value="EXAMPLE ANSWER") as explain:
            response = self.client.post("/api/assistant", json={"question": "EXAMPLE QUESTION"},
                                        headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 200)
        factory.assert_not_called()
        plan.assert_called_once()
        explain.assert_called_once()
        self.assertEqual(response.get_json()["model"], self.application.local_chat.MODEL)

    def test_saved_result_page_distinguishes_hybrid_sources_and_preserves_legacy_display(self):
        with self.application.db() as connection:
            self.insert_transaction(connection, "sample-second", "SAMPLE SECOND PURCHASE")

        def classify(prepared, rows):
            details = self.classified(prepared, rows)
            for item, source in zip(details, ("classifier", "qwen"), strict=True):
                item["categorization_source"] = source
            return details

        with patch.object(self.application, "categorization_session", side_effect=self.session([], classify)):
            response = self.ordinary_request()
        self.assertTrue(response.get_json()["ok"])
        report_url = response.get_json()["report_url"]
        report = self.client.get(report_url)
        self.assertEqual(report.status_code, 200)
        self.assertIn(b"Fast classifier + Qwen", report.data)
        self.assertIn(b"Saved groups: 1 Classifier", report.data)
        self.assertIn(b"1 Qwen", report.data)
        self.assertIn(b'<div class="transaction-sub">Classifier</div>', report.data)
        self.assertIn(b'<div class="transaction-sub">Qwen</div>', report.data)
        self.assertNotIn(b"Review AI guesses", report.data)
        self.assertNotIn(b"reviewing them is optional", report.data)
        with self.application.db() as connection:
            legacy = self.application.load_ollama_result(connection)
            legacy.pop("categorization_engine")
            legacy["model"] = "EXAMPLE QWEN LEGACY MODEL"
            self.application.save_ollama_result(connection, legacy)
        old_report = self.client.get(report_url)
        self.assertIn(b"EXAMPLE QWEN LEGACY MODEL", old_report.data)
        self.assertIn(b"Review AI guesses", old_report.data)
        self.assertIn(b"reviewing them is optional", old_report.data)
        self.assertNotIn(b"Saved groups:", old_report.data)

    def test_actual_session_factory_closes_private_context_on_failure_and_leaves_qwen_lightweight(self):
        events = []

        @contextmanager
        def private_context():
            events.append("opened")
            try:
                yield SimpleNamespace(classify=self.classified)
            finally:
                events.append("closed")

        with patch.object(self.application, "HybridCategorizer", side_effect=private_context) as constructor:
            with self.assertRaises(TimeoutError):
                with self.application.categorization_session("hybrid") as categorizer:
                    self.assertTrue(callable(categorizer.classify))
                    raise TimeoutError("EXAMPLE private context failure")
            constructor.assert_called_once_with()
        self.assertEqual(events, ["opened", "closed"])
        with patch.object(self.application, "HybridCategorizer") as constructor:
            with self.application.categorization_session("qwen") as categorizer:
                self.assertIs(categorizer.classify, self.application.classify_evaluation_rows)
            constructor.assert_not_called()
