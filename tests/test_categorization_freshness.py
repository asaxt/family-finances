import copy
from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from analytics import CATEGORY_RULE_JOIN, RAW_CATEGORY_SQL
from llm_evaluation import (
    StaleCategorizationError,
    categorization_fingerprint,
    classify_and_save_batch,
    classify_evaluation_rows,
    evaluation_result,
    prepare_evaluation,
    save_categorization_batch,
)
from schema import create_schema


class CategorizationFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        self.connection.execute(
            "INSERT INTO connections (id, owner_name, institution, access_token) "
            "VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', 'EXAMPLE UNUSED PLACEHOLDER')"
        )
        self.connection.executemany(
            "INSERT INTO accounts (id, connection_id, institution, name, type, subtype) "
            "VALUES (?, 1, 'EXAMPLE BANK', ?, 'depository', 'checking')",
            [("sample-account-a", "EXAMPLE ACCOUNT A"), ("sample-account-b", "EXAMPLE ACCOUNT B")],
        )
        self.connection.executemany(
            "INSERT INTO category_rules (name, flow_type) VALUES (?, 'spending')",
            [("EXAMPLE DINING",), ("EXAMPLE GROCERIES",)],
        )
        for identifier, description, category in (
            ("sample-target-a", "EXAMPLE TARGET A", "Uncategorized"),
            ("sample-target-b", "EXAMPLE TARGET B", "Uncategorized"),
            ("sample-reference", "EXAMPLE REFERENCE CAFE", "EXAMPLE DINING"),
        ):
            self.connection.execute(
                "INSERT INTO transactions (id, account_id, amount, currency, description, merchant, "
                "pending, transacted_at, category) VALUES (?, 'sample-account-a', 12300, 'USD', "
                "?, ?, 0, '2001-01-02', ?)",
                (identifier, description, description, category),
            )
        self.connection.commit()
        self.context_depth = 0

    def tearDown(self):
        self.connection.close()

    @contextmanager
    def connection_factory(self):
        self.context_depth += 1
        try:
            with self.connection:
                yield self.connection
        finally:
            self.context_depth -= 1

    def prepared(self, identifiers=("sample-target-a",), *, all_categories=False):
        result = prepare_evaluation(
            self.connection, ["EXAMPLE DINING", "EXAMPLE GROCERIES", "Other", "Transfer"],
            model="EXAMPLE LOCAL MODEL", transaction_ids=list(identifiers),
        )
        result["categories_from_database"] = all_categories
        return result

    def change(self, sql, values=()):
        self.assertEqual(self.context_depth, 0, "Model work must not hold a database context.")
        self.assertFalse(self.connection.in_transaction, "Model work must not hold a SQLite transaction.")
        self.connection.execute(sql, values)
        self.connection.commit()

    def classify(self, prepared, rows, category="EXAMPLE DINING"):
        predictions = [{"id": row["evaluation_id"], "category": category,
                        "confidence": 2, "reason": "EXAMPLE supported purchase purpose."} for row in rows]
        with patch("llm_evaluation.classify_batch", return_value=predictions):
            return classify_evaluation_rows(prepared, rows)

    def effective_category(self, identifier):
        row = self.connection.execute(
            f"SELECT {RAW_CATEGORY_SQL} FROM transactions t {CATEGORY_RULE_JOIN} WHERE t.id = ?",
            (identifier,),
        ).fetchone()
        return row[0] if row else None

    def finances(self):
        return {table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table}")]
                for table in ("transactions", "merchant_rules", "category_rules", "settings")}

    def assert_no_model_assignments(self):
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_override_source = 'model'"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM merchant_rules WHERE source = 'model'"
        ).fetchone()[0], 0)

    def test_model_runs_outside_database_context_and_unrelated_rows_are_not_assigned(self):
        prepared = self.prepared()
        calls = []

        def classifier(current, rows):
            self.assertEqual(self.context_depth, 0)
            self.assertFalse(self.connection.in_transaction)
            calls.append(copy.deepcopy(rows))
            return self.classify(current, rows)

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(applied, 1)
        self.assertEqual(details[0]["transaction_ids"], ["sample-target-a"])
        self.assertEqual(self.effective_category("sample-target-a"), "EXAMPLE DINING")
        self.assertEqual(self.effective_category("sample-target-b"), "Uncategorized")

    def test_changed_description_and_amount_are_reclassified_before_saving(self):
        prepared = self.prepared()
        calls = []

        def classifier(current, rows):
            calls.append(copy.deepcopy(rows))
            if len(calls) == 1:
                self.change("UPDATE transactions SET description = 'EXAMPLE FRESH PURCHASE', amount = 45600 "
                            "WHERE id = 'sample-target-a'")
            else:
                self.assert_no_model_assignments()
            return self.classify(current, rows)

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0]["description"], "EXAMPLE TARGET A")
        self.assertEqual(calls[1][0]["description"], "EXAMPLE FRESH PURCHASE")
        self.assertEqual(calls[1][0]["amount"], 45600)
        self.assertEqual(details[0]["description"], "EXAMPLE FRESH PURCHASE")
        self.assertEqual(applied, 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM merchant_rules WHERE match_value = 'EXAMPLE TARGET A'"
        ).fetchone()[0], 0)

    def test_database_palette_and_reference_examples_are_refreshed_on_retry(self):
        prepared = self.prepared(all_categories=True)
        calls = []

        def classifier(current, rows):
            calls.append(copy.deepcopy(current))
            if len(calls) == 1:
                self.change("INSERT INTO category_rules (name, flow_type) VALUES ('EXAMPLE NEW CATEGORY', 'spending')")
                self.change("UPDATE transactions SET description = 'EXAMPLE FRESH REFERENCE', "
                            "category = 'EXAMPLE GROCERIES' WHERE id = 'sample-reference'")
            return self.classify(current, rows, "EXAMPLE GROCERIES")

        classify_and_save_batch(self.connection_factory, prepared, [], prepared["groups"], classify=classifier)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("EXAMPLE NEW CATEGORY", calls[0]["categories"])
        self.assertIn("EXAMPLE NEW CATEGORY", calls[1]["categories"])
        descriptions = [item["description"] for item in calls[1]["category_examples"]["EXAMPLE GROCERIES"]]
        self.assertIn("EXAMPLE FRESH REFERENCE", descriptions)
        self.assertNotIn("EXAMPLE REFERENCE CAFE", [
            item["description"] for items in calls[1]["category_examples"].values() for item in items
        ])

    def test_explicit_palette_intersects_current_categories_after_category_deletion(self):
        prepared = self.prepared()
        calls = []

        def classifier(current, rows):
            calls.append(copy.deepcopy(current["categories"]))
            if len(calls) == 1:
                self.change("DELETE FROM category_rules WHERE name = 'EXAMPLE DINING'")
                self.change("INSERT INTO category_rules (name, flow_type) VALUES ('EXAMPLE NEW CATEGORY', 'spending')")
                return self.classify(current, rows, "EXAMPLE DINING")
            return self.classify(current, rows, "EXAMPLE GROCERIES")

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
        )
        self.assertEqual(len(calls), 2)
        self.assertNotIn("EXAMPLE DINING", calls[1])
        self.assertNotIn("EXAMPLE NEW CATEGORY", calls[1])
        self.assertEqual(details[0]["category"], "EXAMPLE GROCERIES")
        self.assertEqual(applied, 1)

    def test_new_manual_override_user_rule_pending_deleted_and_excluded_rows_are_skipped(self):
        changes = (
            ("manual", "UPDATE transactions SET category_override = 'EXAMPLE GROCERIES', category_override_source = 'user' WHERE id = 'sample-target-a'"),
            ("rule", "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) VALUES ('sample-account-a', 'description', 'EXAMPLE TARGET A', 'EXAMPLE GROCERIES', 'user')"),
            ("pending", "UPDATE transactions SET pending = 1 WHERE id = 'sample-target-a'"),
            ("deleted", "DELETE FROM transactions WHERE id = 'sample-target-a'"),
            ("excluded", "UPDATE transactions SET excluded = 1 WHERE id = 'sample-target-a'"),
        )
        for name, sql in changes:
            with self.subTest(change=name):
                self.connection.close()
                self.setUp()
                prepared = self.prepared()
                calls = []

                def classifier(current, rows):
                    calls.append(1)
                    self.change(sql)
                    return self.classify(current, rows)

                details, applied = classify_and_save_batch(
                    self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(applied, 0)
                self.assertTrue(details)
                self.assertTrue(all(item["status"] == "skipped" for item in details))
                self.assert_no_model_assignments()
                if name in {"manual", "rule"}:
                    self.assertEqual(self.effective_category("sample-target-a"), "EXAMPLE GROCERIES")

    def test_persistent_source_changes_stop_after_three_attempts_without_stale_writes(self):
        prepared = self.prepared()
        calls = []

        def classifier(current, rows):
            calls.append(1)
            self.change("UPDATE transactions SET amount = amount + 100 WHERE id = 'sample-target-a'")
            return self.classify(current, rows)

        with self.assertRaises(StaleCategorizationError):
            classify_and_save_batch(self.connection_factory, prepared, [], prepared["groups"], classify=classifier)
        self.assertEqual(len(calls), 3)
        self.assert_no_model_assignments()

    def test_invalid_model_output_is_not_retried_or_saved(self):
        prepared = self.prepared()
        calls = []

        def classifier(current, rows):
            calls.append(1)
            self.change("UPDATE transactions SET amount = amount + 100 WHERE id = 'sample-target-a'")
            malformed = [{"id": rows[0]["evaluation_id"], "category": "EXAMPLE DINING", "confidence": True,
                          "reason": "EXAMPLE invalid confidence."}]
            with patch("llm_evaluation.classify_batch", return_value=malformed):
                return classify_evaluation_rows(current, rows)

        with self.assertRaises(ValueError):
            classify_and_save_batch(self.connection_factory, prepared, [], prepared["groups"], classify=classifier)
        self.assertEqual(len(calls), 1)
        self.assert_no_model_assignments()

    def test_direct_save_requires_current_fingerprint_before_any_writes(self):
        prepared = self.prepared()
        details = self.classify(prepared, prepared["groups"])
        missing = {key: value for key, value in prepared.items() if key != "source_fingerprint"}
        before = self.finances()
        with self.assertRaises(StaleCategorizationError):
            save_categorization_batch(self.connection, missing, [], copy.deepcopy(details))
        self.assertEqual(self.finances(), before)
        self.change("UPDATE transactions SET amount = 45600 WHERE id = 'sample-target-a'")
        before = self.finances()
        with self.assertRaises(StaleCategorizationError):
            save_categorization_batch(self.connection, prepared, [], copy.deepcopy(details))
        self.assertEqual(self.finances(), before)

    def test_fingerprint_tracks_semantic_fields_but_ignores_settings_and_balances(self):
        before = categorization_fingerprint(self.connection)
        self.connection.execute("UPDATE accounts SET current_balance = 45600, available_balance = 12300")
        self.connection.execute("INSERT INTO settings (key, value) VALUES ('EXAMPLE VIEW', 'EXAMPLE PREFERENCE')")
        self.connection.commit()
        self.assertEqual(categorization_fingerprint(self.connection), before)
        for sql in (
            "UPDATE transactions SET amount = amount + 100 WHERE id = 'sample-target-a'",
            "UPDATE transactions SET description = 'EXAMPLE CHANGED' WHERE id = 'sample-target-a'",
            "UPDATE transactions SET currency = 'EUR' WHERE id = 'sample-target-a'",
            "UPDATE accounts SET type = 'credit', subtype = 'credit card' WHERE id = 'sample-account-a'",
            "UPDATE category_rules SET flow_type = 'other_inflow' WHERE name = 'EXAMPLE DINING'",
            "DELETE FROM category_rules WHERE name = 'EXAMPLE GROCERIES'",
            "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) VALUES ('sample-account-a', 'description', 'EXAMPLE RULE', 'EXAMPLE GROCERIES', 'user')",
        ):
            with self.subTest(sql=sql):
                self.connection.execute(sql)
                self.assertNotEqual(categorization_fingerprint(self.connection), before)
                self.connection.rollback()
                self.assertEqual(categorization_fingerprint(self.connection), before)

    def test_fingerprint_tracks_rule_updates_deletion_and_fallback_edges(self):
        self.connection.executemany(
            "INSERT INTO merchant_rules (account_id, match_type, match_value, category, source) "
            "VALUES ('sample-account-a', 'description', ?, 'EXAMPLE DINING', 'user')",
            [("EXAMPLE RULE A",), ("EXAMPLE RULE B",)],
        )
        self.connection.commit()
        identifiers = [row[0] for row in self.connection.execute("SELECT id FROM merchant_rules ORDER BY id")]
        before = categorization_fingerprint(self.connection)
        for sql, values in (
            ("UPDATE merchant_rules SET category = 'EXAMPLE GROCERIES' WHERE id = ?", (identifiers[0],)),
            ("DELETE FROM merchant_rules WHERE id = ?", (identifiers[0],)),
            ("INSERT INTO rule_fallbacks (preferred_id, fallback_id) VALUES (?, ?)", tuple(identifiers)),
        ):
            with self.subTest(sql=sql):
                self.connection.execute(sql, values)
                self.assertNotEqual(categorization_fingerprint(self.connection), before)
                self.connection.rollback()
                self.assertEqual(categorization_fingerprint(self.connection), before)

    def test_retry_of_later_batch_never_reruns_completed_batch_or_makes_its_stale_rule(self):
        self.connection.execute(
            "UPDATE transactions SET description = 'EXAMPLE SHARED PURCHASE', "
            "amount = CASE id WHEN 'sample-target-a' THEN 12300 ELSE -12300 END "
            "WHERE id IN ('sample-target-a', 'sample-target-b')"
        )
        self.connection.commit()
        prepared = self.prepared(("sample-target-a", "sample-target-b"))
        self.assertEqual(len(prepared["groups"]), 2)
        first, second = prepared["groups"]
        first_calls, second_calls = [], []

        def first_classifier(current, rows):
            first_calls.append([identifier for row in rows for identifier in row["transaction_ids"]])
            return self.classify(current, rows)

        completed, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], [first], classify=first_classifier,
        )
        self.assertEqual(applied, 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)

        def second_classifier(current, rows):
            second_calls.append([identifier for row in rows for identifier in row["transaction_ids"]])
            if len(second_calls) == 1:
                self.change("UPDATE transactions SET description = 'EXAMPLE AUTHORITATIVE EDIT', "
                            "category_override = 'EXAMPLE GROCERIES', category_override_source = 'user' "
                            "WHERE id = 'sample-target-a'")
            return self.classify(current, rows)

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, completed, [second], classify=second_classifier,
        )
        self.assertEqual(first_calls, [["sample-target-a"]])
        self.assertEqual(second_calls, [["sample-target-b"], ["sample-target-b"]])
        self.assertEqual(applied, 1)
        self.assertEqual(details[0]["transaction_ids"], ["sample-target-b"])
        self.assertEqual(self.effective_category("sample-target-a"), "EXAMPLE GROCERIES")
        self.assertEqual(self.effective_category("sample-target-b"), "EXAMPLE DINING")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)

    def test_manual_edit_of_one_group_member_preserves_other_members_and_blocks_group_rule(self):
        self.connection.execute(
            "UPDATE transactions SET description = 'EXAMPLE GROUP PURCHASE' "
            "WHERE id IN ('sample-target-a', 'sample-target-b')"
        )
        self.connection.commit()
        prepared = self.prepared(("sample-target-a", "sample-target-b"))
        self.assertEqual(len(prepared["groups"]), 1)
        calls = []

        def classifier(current, rows):
            calls.append([identifier for row in rows for identifier in row["transaction_ids"]])
            if len(calls) == 1:
                self.change("UPDATE transactions SET category_override = 'EXAMPLE GROCERIES', "
                            "category_override_source = 'user' WHERE id = 'sample-target-b'")
            return self.classify(current, rows)

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
        )
        self.assertEqual(calls, [["sample-target-a", "sample-target-b"], ["sample-target-a"]])
        self.assertEqual(applied, 1)
        self.assertEqual(self.effective_category("sample-target-a"), "EXAMPLE DINING")
        self.assertEqual(self.effective_category("sample-target-b"), "EXAMPLE GROCERIES")
        skipped = [item for item in details if item["status"] == "skipped"]
        self.assertEqual(skipped[0]["transaction_ids"], ["sample-target-b"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0], 0)

    def test_cross_currency_or_excluded_counterpart_never_proves_transfer(self):
        for currency, excluded in (("EUR", 0), ("USD", 1)):
            with self.subTest(currency=currency, excluded=excluded):
                self.connection.close()
                self.setUp()
                self.connection.execute("UPDATE transactions SET amount = 45600 WHERE id = 'sample-target-a'")
                self.connection.execute(
                    "UPDATE transactions SET account_id = 'sample-account-b', amount = -45600, "
                    "currency = ?, excluded = ? WHERE id = 'sample-target-b'", (currency, excluded),
                )
                self.connection.commit()
                prepared = self.prepared()

                def classifier(current, rows):
                    self.assertFalse(rows[0]["all_have_transfer_match"])
                    return self.classify(current, rows, "Transfer")

                details, applied = classify_and_save_batch(
                    self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
                )
                self.assertEqual(applied, 0)
                self.assertEqual(details[0]["category"], "")
                self.assert_no_model_assignments()

    def test_one_payment_with_two_possible_credits_cannot_be_assigned_transfer(self):
        self.connection.execute("UPDATE transactions SET amount = 45600 WHERE id = 'sample-target-a'")
        self.connection.execute(
            "UPDATE transactions SET account_id = 'sample-account-b', amount = -45600 "
            "WHERE id IN ('sample-target-b', 'sample-reference')"
        )
        self.connection.commit()
        prepared = self.prepared()

        def classifier(current, rows):
            self.assertEqual([row['transaction_ids'] for row in rows], [['sample-target-a']])
            self.assertFalse(rows[0]['all_have_transfer_match'])
            return self.classify(current, rows, 'Transfer')

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared['groups'], classify=classifier,
        )
        self.assertEqual(applied, 0)
        self.assertEqual(details[0]['category'], '')
        self.assert_no_model_assignments()

    def test_two_payments_with_one_possible_credit_cannot_be_assigned_transfer(self):
        self.connection.execute(
            "UPDATE transactions SET amount = 45600 WHERE id IN ('sample-target-a', 'sample-reference')"
        )
        self.connection.execute(
            "UPDATE transactions SET account_id = 'sample-account-b', amount = -45600 "
            "WHERE id = 'sample-target-b'"
        )
        self.connection.commit()
        prepared = self.prepared()

        def classifier(current, rows):
            self.assertEqual([row['transaction_ids'] for row in rows], [['sample-target-a']])
            self.assertFalse(rows[0]['all_have_transfer_match'])
            return self.classify(current, rows, 'Transfer')

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared['groups'], classify=classifier,
        )
        self.assertEqual(applied, 0)
        self.assertEqual(details[0]['category'], '')
        self.assert_no_model_assignments()

    def test_mutually_unique_transfer_pair_preserves_unselected_counterpart(self):
        self.connection.execute("UPDATE transactions SET amount = 45600 WHERE id = 'sample-target-a'")
        self.connection.execute(
            "UPDATE transactions SET account_id = 'sample-account-b', amount = -45600 "
            "WHERE id = 'sample-target-b'"
        )
        self.connection.commit()
        prepared = self.prepared()

        def classifier(current, rows):
            self.assertEqual([row['transaction_ids'] for row in rows], [['sample-target-a']])
            self.assertTrue(rows[0]['all_have_transfer_match'])
            return self.classify(current, rows, 'Transfer')

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared['groups'], classify=classifier,
        )
        self.assertEqual(applied, 1)
        self.assertEqual(details[0]['category'], 'Transfer')
        self.assertEqual(self.effective_category('sample-target-a'), 'Transfer')
        for identifier in ('sample-target-b', 'sample-reference'):
            self.assertIsNone(self.connection.execute(
                'SELECT category_override FROM transactions WHERE id = ?', (identifier,),
            ).fetchone()[0])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)

    def test_import_policy_can_disable_transfer_inference_even_when_counterpart_exists(self):
        self.connection.execute("UPDATE transactions SET amount = 45600 WHERE id = 'sample-target-a'")
        self.connection.execute(
            "UPDATE transactions SET account_id = 'sample-account-b', amount = -45600 "
            "WHERE id = 'sample-target-b'"
        )
        self.connection.commit()
        prepared = self.prepared()
        prepared["disable_transfer_inference"] = True

        def classifier(current, rows):
            self.assertFalse(rows[0]["all_have_transfer_match"])
            return self.classify(current, rows, "Transfer")

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
        )
        self.assertEqual(applied, 0)
        self.assertEqual(details[0]["category"], "")
        self.assert_no_model_assignments()

    def test_transfer_cache_is_invalidated_by_changes_to_an_unselected_counterpart(self):
        changes = (
            "UPDATE transactions SET currency = 'EUR' WHERE id = 'sample-target-b'",
            "UPDATE transactions SET excluded = 1 WHERE id = 'sample-target-b'",
        )
        for sql in changes:
            with self.subTest(sql=sql):
                self.connection.close()
                self.setUp()
                self.connection.execute("UPDATE transactions SET amount = 45600 WHERE id = 'sample-target-a'")
                self.connection.execute(
                    "UPDATE transactions SET account_id = 'sample-account-b', amount = -45600 "
                    "WHERE id = 'sample-target-b'"
                )
                self.connection.commit()
                prepared = self.prepared()
                evidence = []

                def classifier(current, rows):
                    self.assertEqual([identifier for row in rows for identifier in row["transaction_ids"]],
                                     ["sample-target-a"])
                    evidence.append(rows[0]["all_have_transfer_match"])
                    if len(evidence) == 1:
                        self.change(sql)
                    else:
                        self.assert_no_model_assignments()
                    return self.classify(current, rows, "Transfer")

                details, applied = classify_and_save_batch(
                    self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
                )
                self.assertEqual(evidence, [True, False])
                self.assertEqual(applied, 0)
                self.assertEqual(details[0]["category"], "")
                self.assertEqual(details[0]["status"], "uncategorized")
                self.assertEqual(self.effective_category("sample-target-b"), "Uncategorized")
                self.assert_no_model_assignments()

    def test_description_split_saves_each_fresh_group_once_and_preserves_original_report_count(self):
        self.connection.execute(
            "UPDATE transactions SET description = 'EXAMPLE ORIGINAL GROUP' "
            "WHERE id IN ('sample-target-a', 'sample-target-b')"
        )
        self.connection.commit()
        prepared = self.prepared(("sample-target-a", "sample-target-b"))
        self.assertEqual(len(prepared["groups"]), 1)
        calls = []

        def classifier(current, rows):
            calls.append([(row["description"], list(row["transaction_ids"])) for row in rows])
            if len(calls) == 1:
                self.change("UPDATE transactions SET description = 'EXAMPLE SPLIT GROUP' "
                            "WHERE id = 'sample-target-b'")
            else:
                self.assert_no_model_assignments()
            return self.classify(current, rows)

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared["groups"], classify=classifier,
        )
        self.assertEqual(calls, [
            [("EXAMPLE ORIGINAL GROUP", ["sample-target-a", "sample-target-b"])],
            [("EXAMPLE ORIGINAL GROUP", ["sample-target-a"]),
             ("EXAMPLE SPLIT GROUP", ["sample-target-b"])],
        ])
        self.assertEqual(applied, 2)
        self.assertEqual(len(details), 2)
        self.assertEqual([identifier for item in details for identifier in item["applied_transaction_ids"]],
                         ["sample-target-a", "sample-target-b"])
        self.assertEqual({row[0] for row in self.connection.execute(
            "SELECT match_value FROM merchant_rules WHERE source = 'model'"
        )}, {"EXAMPLE ORIGINAL GROUP", "EXAMPLE SPLIT GROUP"})
        self.assertEqual(self.effective_category("sample-target-a"), "EXAMPLE DINING")
        self.assertEqual(self.effective_category("sample-target-b"), "EXAMPLE DINING")
        result = evaluation_result(prepared, details, 0.0, applied_transaction_count=applied)
        self.assertEqual(result["sample_transaction_count"], 1)
        self.assertEqual(result["processed_group_count"], 1)
        self.assertEqual(result["remaining_group_count"], 0)
        self.assertEqual(result["applied_transaction_count"], 2)
        self.assertEqual(result["coverage"]["considered"], 2)
        self.assertEqual(result["coverage"]["eligible"], 2)
        self.assertEqual(sum(result["coverage"][key] for key in (
            "confident", "tentative", "uncategorized", "not_processed", "skipped"
        )), 2)
