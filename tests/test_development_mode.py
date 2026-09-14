import importlib
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch


class DevelopmentModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment_names = (
            "FAMILY_FINANCES_DATA_DIR",
            "FAMILY_FINANCES_MODE",
            "FAMILY_FINANCES_PORT",
            "FAMILY_FINANCES_DISABLE_PLAID",
        )
        self.previous_environment = {
            name: os.environ.get(name) for name in self.environment_names
        }
        for name in self.environment_names:
            os.environ.pop(name, None)
        os.environ.update(
            {
                "FAMILY_FINANCES_DATA_DIR": self.temporary.name,
                "FAMILY_FINANCES_MODE": "development",
                "FAMILY_FINANCES_PORT": "4243",
            }
        )
        sys.modules.pop("app", None)
        self.application = importlib.import_module("app")
        self.application.app.config["TESTING"] = True
        self.client = self.application.app.test_client()
        self.password = "a long development password"
        self.assertEqual(
            self.application.app.config["SESSION_COOKIE_NAME"],
            "family_finances_development",
        )

    def tearDown(self):
        if self.application.vault.unlocked:
            self.application.lock_data()
        sys.modules.pop("app", None)
        for name, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    @staticmethod
    def csrf_token(response):
        return re.search(
            rb'name="csrf_token" value="([^"]+)"', response.data
        ).group(1).decode()

    def complete_setup(self):
        setup_page = self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": self.password,
                "confirmation": self.password,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.complete_category_setup()

    def complete_category_setup(self):
        category_page = self.client.get("/category-setup")
        response = self.client.post(
            "/category-setup",
            data={
                "csrf_token": self.csrf_token(category_page),
                "next": "/",
                "original_name": [
                    "Income",
                    "Loan Disbursements",
                    "Reimbursed Work Travel",
                    "Transfer",
                ],
                "category_name": [
                    "Income",
                    "Loan Disbursements",
                    "Reimbursed Work Travel",
                    "Transfer",
                ],
                "flow_type": [
                    "earned_income",
                    "earned_income",
                    "earned_income",
                    "transfer",
                ],
            },
        )
        self.assertEqual(response.status_code, 302)

    def set_local_ai(self, enabled):
        settings = self.client.get("/settings")
        data = {"csrf_token": self.csrf_token(settings)}
        if enabled:
            data["enabled"] = "on"
        response = self.client.post("/api/local-ai", data=data)
        self.assertEqual(response.location, "/settings?saved=local_ai")

    def enable_local_ai(self):
        self.set_local_ai(True)

    def test_development_mode_is_visible_and_blocks_plaid(self):
        health = self.client.get("/health").get_json()
        self.assertEqual(health["mode"], "development")
        self.assertFalse(health["plaid_enabled"])

        setup_page = self.client.get("/setup")
        self.assertIn(b"Development", setup_page.data)
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": self.password,
                "confirmation": self.password,
            },
        )
        self.assertEqual(response.status_code, 302)

        overview = self.client.get("/")
        token = self.csrf_token(overview)
        self.assertIn(b"Development", overview.data)
        self.assertIn(b"Plaid is disabled in this environment", overview.data)
        self.assertNotIn(b'id="sync-button"', overview.data)
        self.assertNotIn(b'id="connect-button"', overview.data)
        transactions = self.client.get("/transactions")
        self.assertIn(b"Set up categories", transactions.data)
        self.complete_category_setup()
        self.assertNotIn(
            b'id="run-local-categorization"', self.client.get("/transactions").data
        )

        settings = self.client.get("/settings")
        self.assertIn(
            b"Plaid connections and synchronization are disabled",
            settings.data,
        )

        for path, keyword in (
            ("/api/link-token", {}),
            ("/api/exchange-token", {}),
            ("/api/sync", None),
            ("/api/plaid-products", None),
        ):
            response = self.client.post(
                path,
                json=keyword,
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(response.status_code, 403, path)

        response = self.client.post(
            "/api/plaid-settings",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 403)
        with self.assertRaisesRegex(RuntimeError, "Plaid is disabled"):
            self.application.plaid_client()

    def test_local_ai_defaults_off_and_controls_ui_and_routes(self):
        self.complete_setup()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, pending,
                    transacted_at, category
                ) VALUES (
                    'manual', 'checking', -1000, 'USD', 'PAYCHECK', 0,
                    '2026-08-20', 'Uncategorized'
                )
                """
            )
        transactions = self.client.get("/transactions")
        self.client.post(
            "/api/transaction/manual",
            data={
                "csrf_token": self.csrf_token(transactions),
                "category_choice": "Income",
                "category_flow_type": "earned_income",
            },
        )
        settings = self.client.get("/settings")
        self.assertIn(b"Local AI assistance", settings.data)
        self.assertNotIn(b'name="enabled" checked', settings.data)
        self.assertNotIn(
            b'id="run-local-categorization"', self.client.get("/transactions").data
        )
        token = self.csrf_token(settings)
        blocked = self.client.post(
            "/api/local-ai/evaluation",
            json={},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()["error"], "Local AI assistance is turned off.")
        self.assertEqual(self.client.get("/local-ai/evaluation").status_code, 404)

        self.set_local_ai(True)
        settings = self.client.get("/settings")
        self.assertIn(b'name="enabled" checked', settings.data)
        self.assertIn(
            b'id="run-local-categorization"', self.client.get("/transactions").data
        )

        self.set_local_ai(False)
        self.assertNotIn(
            b'id="run-local-categorization"', self.client.get("/transactions").data
        )
        with self.application.vault.connection() as connection:
            self.assertFalse(self.application.local_ai_enabled(connection))
            manual = connection.execute(
                """
                SELECT category_override, category_override_source
                FROM transactions WHERE id = 'manual'
                """
            ).fetchone()
        self.assertEqual(tuple(manual), ("Income", "user"))

    def test_first_time_category_setup_uses_editable_starter_labels(self):
        setup_page = self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": self.password,
                "confirmation": self.password,
            },
        )
        self.assertEqual(response.location, "/category-setup")
        self.enable_local_ai()

        category_page = self.client.get("/category-setup")
        self.assertIn(b"Grocery", category_page.data)
        self.assertIn(b"Eating Out", category_page.data)
        self.assertIn(b"Rent/Mortgage/Utilities", category_page.data)
        token = self.csrf_token(category_page)
        blocked = self.client.post(
            "/api/local-ai/evaluation",
            json={"categories": ["Grocery"]},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["setup_url"], "/category-setup?next=/transactions")

        response = self.client.post(
            "/category-setup",
            data={
                "csrf_token": token,
                "next": "/transactions",
                "original_name": [
                    "Income",
                    "Loan Disbursements",
                    "Reimbursed Work Travel",
                    "Transfer",
                    "",
                    "",
                ],
                "category_name": [
                    "Income",
                    "Loan Disbursements",
                    "Reimbursed Work Travel",
                    "Transfer",
                    "Groceries",
                    "Eating Out",
                ],
                "flow_type": [
                    "earned_income",
                    "earned_income",
                    "earned_income",
                    "transfer",
                    "spending",
                    "spending",
                ],
            },
        )
        self.assertEqual(response.location, "/transactions")
        with self.application.vault.connection() as connection:
            mappings = dict(
                connection.execute(
                    "SELECT name, flow_type FROM category_rules"
                ).fetchall()
            )
            self.assertTrue(self.application.category_setup_is_complete(connection))
        self.assertEqual(mappings["Groceries"], "spending")
        self.assertNotIn("Grocery", mappings)
        self.assertNotIn("Skiing", mappings)
        self.assertIn(
            b'id="run-local-categorization"', self.client.get("/transactions").data
        )
        self.assertIn(b"Manage category labels", self.client.get("/settings").data)

    def test_category_rename_updates_existing_transactions_and_rules(self):
        self.complete_setup()
        with self.application.vault.connection() as connection:
            connection.execute(
                "INSERT INTO category_rules VALUES ('Grocery', 'spending')"
            )
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, pending,
                    transacted_at, category, category_override
                ) VALUES (
                    'market', 'checking', 2500, 'USD', 'LOCAL MARKET', 0,
                    '2026-08-20', 'Uncategorized', 'Grocery'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category
                ) VALUES ('checking', 'description', 'LOCAL MARKET', 'Grocery')
                """
            )
            rows = list(
                connection.execute(
                    "SELECT name, flow_type FROM category_rules ORDER BY name"
                )
            )

        page = self.client.get("/category-setup")
        response = self.client.post(
            "/category-setup",
            data={
                "csrf_token": self.csrf_token(page),
                "next": "/settings",
                "original_name": [row["name"] for row in rows],
                "category_name": [
                    "Groceries" if row["name"] == "Grocery" else row["name"]
                    for row in rows
                ],
                "flow_type": [row["flow_type"] for row in rows],
            },
        )
        self.assertEqual(response.location, "/settings")
        with self.application.vault.connection() as connection:
            transaction_category = connection.execute(
                "SELECT category_override FROM transactions WHERE id = 'market'"
            ).fetchone()[0]
            recurring_category = connection.execute(
                "SELECT category FROM merchant_rules WHERE match_value = 'LOCAL MARKET'"
            ).fetchone()[0]
            mapping = connection.execute(
                "SELECT flow_type FROM category_rules WHERE name = 'Groceries'"
            ).fetchone()[0]
        self.assertEqual(transaction_category, "Groceries")
        self.assertEqual(recurring_category, "Groceries")
        self.assertEqual(mapping, "spending")

    def test_table_header_filters_exclude_categories_sort_and_survive_bulk_edit(self):
        self.complete_setup()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.executemany(
                "INSERT INTO category_rules VALUES (?, 'spending')",
                (("Grocery",), ("Travel",)),
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, category_override
                ) VALUES (?, 'checking', ?, 'USD', ?, ?, 0, '2026-08-20',
                          'Uncategorized', ?)
                """,
                (
                    ("large", 9000, "LARGE PURCHASE", "Large Purchase", "Grocery"),
                    ("medium", 5000, "TRIP", "Trip", "Travel"),
                    ("small", 1000, "SMALL PURCHASE", "Small Purchase", "Grocery"),
                ),
            )

        page = self.client.get(
            "/transactions?exclude_category=Travel&sort=amount_desc"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Hide selected categories", page.data)
        self.assertNotIn(b"transaction-filterbar", page.data)
        self.assertNotIn(b">Trip<", page.data)
        self.assertLess(page.data.index(b">Large Purchase<"), page.data.index(b">Small Purchase<"))

        response = self.client.post(
            "/api/transactions/bulk",
            data={
                "csrf_token": self.csrf_token(page),
                "transaction_ids": ["large"],
                "action": "apply",
                "category_choice": "__no_change__",
                "inclusion": "__no_change__",
                "return_excluded_category": ["Travel"],
                "return_sort": "amount_desc",
                "return_view": "active",
                "return_purpose": "all",
            },
        )
        self.assertIn("exclude_category=Travel", response.location)
        self.assertIn("sort=amount_desc", response.location)

    def test_refreshed_data_becomes_a_blank_slate_once(self):
        self.complete_setup()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type,
                    cash_flow_role, spending_enabled
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking', 'depository',
                    'credit_card', 0
                )
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, category_override,
                    flow_override, spending_override, excluded
                ) VALUES (
                    'venmo', 'checking', 2500, 'USD', 'Venmo payment', 'Venmo',
                    0, '2026-08-15', 'Transfer Out', 'Dining',
                    'spending', 'include', 1
                )
                """
            )
            connection.execute(
                "INSERT INTO category_rules VALUES ('Dining', 'spending')"
            )
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category, flow_type,
                    spending_override
                ) VALUES (
                    'checking', 'merchant', 'Venmo', 'Venmo', 'transfer', 'exclude'
                )
                """
            )
            connection.execute(
                "DELETE FROM settings WHERE key IN (?, ?)",
                (
                    self.application.DEVELOPMENT_RESET_MARKER,
                    "classification_mode",
                ),
            )

        self.assertTrue(self.application.prepare_development_blank_slate())
        with self.application.vault.connection() as connection:
            account = connection.execute(
                "SELECT cash_flow_role, spending_enabled FROM accounts"
            ).fetchone()
            transaction = connection.execute(
                """
                SELECT category, category_override, flow_override,
                       spending_override, excluded
                FROM transactions
                """
            ).fetchone()
            rule_counts = (
                connection.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0],
            )
            settings = dict(
                connection.execute(
                    """
                    SELECT key, value FROM settings
                    WHERE key IN ('classification_mode', ?)
                    """,
                    (self.application.DEVELOPMENT_RESET_MARKER,),
                ).fetchall()
            )
            rows = self.application.transaction_list(
                connection, include_excluded=True
            )

        self.assertEqual(tuple(account), ("cash_flow", 1))
        self.assertEqual(tuple(transaction), ("Uncategorized", None, None, None, 0))
        self.assertEqual(rule_counts, (5, 0))
        self.assertEqual(settings["classification_mode"], "category_mapping_v1")
        self.assertEqual(settings[self.application.DEVELOPMENT_RESET_MARKER], "1")
        self.assertEqual(rows[0]["effective_category"], "Uncategorized")
        self.assertIsNone(rows[0]["flow_type"])
        self.assertFalse(rows[0]["spending_included"])

        with self.application.vault.connection() as connection:
            connection.execute(
                "UPDATE transactions SET category_override = 'Groceries' WHERE id = 'venmo'"
            )
            connection.execute(
                "UPDATE accounts SET cash_flow_role = 'cash_flow', spending_enabled = 1"
            )
            connection.execute(
                "INSERT INTO category_rules VALUES ('Groceries', 'spending')"
            )
        self.application.lock_data()
        self.application.unlock_data(self.password)
        with self.application.vault.connection() as connection:
            override = connection.execute(
                "SELECT category_override FROM transactions WHERE id = 'venmo'"
            ).fetchone()[0]
            reviewed = self.application.transaction_list(connection)[0]
        self.assertEqual(override, "Groceries")
        self.assertEqual(reviewed["flow_type"], "spending")
        self.assertTrue(reviewed["spending_included"])
        self.assertFalse(self.application.prepare_development_blank_slate())

    def test_new_development_imports_have_no_automatic_designations(self):
        self.complete_setup()
        account = SimpleNamespace(
            account_id="new-checking",
            name="New Checking",
            mask="1234",
            type=SimpleNamespace(value="depository"),
            subtype=SimpleNamespace(value="checking"),
            balances=SimpleNamespace(current=123.45, available=120.00),
        )
        transaction = SimpleNamespace(
            transaction_id="new-venmo",
            account_id="new-checking",
            amount=25.00,
            iso_currency_code="USD",
            name="Venmo payment",
            merchant_name="Venmo",
            pending=False,
            date=date(2026, 8, 20),
            personal_finance_category=SimpleNamespace(primary="TRANSFER_OUT"),
        )
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            self.application.save_account(
                connection, account, 1, "Example Bank", "2026-08-20T12:00:00"
            )
            self.application.save_transaction(connection, transaction)
            saved_account = connection.execute(
                """
                SELECT cash_flow_role, spending_enabled
                FROM accounts WHERE id = 'new-checking'
                """
            ).fetchone()
            saved_transaction = connection.execute(
                """
                SELECT category, category_override, flow_override, spending_override
                FROM transactions WHERE id = 'new-venmo'
                """
            ).fetchone()
            row = self.application.transaction_list(connection)[0]

        self.assertEqual(tuple(saved_account), ("cash_flow", 1))
        self.assertEqual(
            tuple(saved_transaction), ("Uncategorized", None, None, None)
        )
        self.assertIsNone(row["flow_type"])
        self.assertFalse(row["spending_included"])

    def test_local_model_results_persist_and_apply(self):
        self.complete_setup()
        self.enable_local_ai()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type, subtype,
                    cash_flow_role, spending_enabled
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking', 'depository',
                    'checking', 'cash_flow', 1
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category
                ) VALUES (?, 'checking', ?, 'USD', ?, ?, 0, ?, 'Uncategorized')
                """,
                (
                    ('cafe-1', 2500, 'RECURRING CAFE', 'Recurring Cafe', '2026-07-10'),
                    ('cafe-2', 2500, 'RECURRING CAFE', 'Recurring Cafe', '2026-08-10'),
                    ('interest', -125, 'INTEREST PAID', None, '2026-08-31'),
                ),
            )

        result = {
            "model": "qwen3.8:27b",
            "date_from": "2026-05-01",
            "date_to": "2026-08-31",
            "months": ["2026-07", "2026-08"],
            "source_transaction_count": 3,
            "sample_transaction_count": 2,
            "category_count": 2,
            "categories": ["Food And Drink", "Income"],
            "elapsed_seconds": 1.2,
            "category_counts": {"Food And Drink": 1},
            "review_count": 1,
            "details": [
                {
                    "id": "G0001",
                    "date": "2026-07-10",
                    "merchant": "Recurring Cafe",
                    "description": "RECURRING CAFE",
                    "direction": "money_out",
                    "amount_usd": 25.0,
                    "account_type": "depository",
                    "account_subtype": "checking",
                    "occurrence_count": 2,
                    "first_date": "2026-07-10",
                    "last_date": "2026-08-10",
                    "all_occurrences_have_matching_household_transaction": False,
                    "account_id": "checking",
                    "transaction_ids": ["cafe-1", "cafe-2"],
                    "status": "categorized",
                    "category": "Food And Drink",
                    "confidence": 2,
                    "reason": "Restaurant purchase.",
                },
                {
                    "id": "G0002",
                    "date": "2026-08-31",
                    "merchant": None,
                    "description": "INTEREST PAID",
                    "direction": "money_in",
                    "amount_usd": 1.25,
                    "account_type": "depository",
                    "account_subtype": "checking",
                    "occurrence_count": 1,
                    "first_date": "2026-08-31",
                    "last_date": "2026-08-31",
                    "all_occurrences_have_matching_household_transaction": False,
                    "account_id": "checking",
                    "transaction_ids": ["interest"],
                    "status": "uncategorized",
                    "category": "",
                    "confidence": 0,
                    "reason": "No existing category fits interest income.",
                },
            ],
        }
        transactions_page = self.client.get("/transactions")
        token = self.csrf_token(transactions_page)
        detail_by_id = {item["id"]: item for item in result["details"]}

        def classify(prepared, rows):
            return [dict(detail_by_id[row["evaluation_id"]]) for row in rows]

        with patch.object(
            self.application, "classify_evaluation_rows", side_effect=classify
        ):
            response = self.client.post(
                "/api/local-ai/evaluation",
                json={"categories": ["Food And Drink", "Income"]},
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(response.status_code, 200)

        with self.application.vault.connection() as connection:
            categories = dict(
                connection.execute(
                    "SELECT id, category_override FROM transactions ORDER BY id"
                ).fetchall()
            )
            rule = connection.execute(
                """
                SELECT category FROM merchant_rules
                WHERE account_id = 'checking' AND match_type = 'description'
                  AND match_value = 'RECURRING CAFE'
                """
            ).fetchone()
            effective = {
                row["id"]: row["effective_category"]
                for row in self.application.transaction_list(connection)
            }
            saved_result = self.application.load_ollama_result(connection)
        self.assertIsNone(categories["cafe-1"])
        self.assertIsNone(categories["cafe-2"])
        self.assertIsNone(categories["interest"])
        self.assertEqual(effective["cafe-1"], "Food And Drink")
        self.assertEqual(effective["cafe-2"], "Food And Drink")
        self.assertEqual(rule["category"], "Food And Drink")
        self.assertEqual(saved_result["applied_transaction_count"], 2)
        self.assertEqual(saved_result["status"], "completed")

        self.application.lock_data()
        self.application.unlock_data(self.password)
        with self.application.vault.connection() as connection:
            persisted = dict(
                connection.execute(
                    "SELECT id, category_override FROM transactions ORDER BY id"
                ).fetchall()
            )
            self.assertEqual(
                self.application.load_ollama_result(connection)["status"],
                "completed",
            )
            persisted_effective = {
                row["id"]: row["effective_category"]
                for row in self.application.transaction_list(connection)
            }
        self.assertIsNone(persisted["cafe-1"])
        self.assertIsNone(persisted["cafe-2"])
        self.assertEqual(persisted_effective["cafe-1"], "Food And Drink")
        self.assertEqual(persisted_effective["cafe-2"], "Food And Drink")

        self.client.get("/")
        report = self.client.get("/local-ai/evaluation")
        self.assertIn(b"Food And Drink", report.data)
        self.assertIn(b"Confidence 2", report.data)
        self.assertIn(b"Confidence 0", report.data)
        self.assertIn(b"Uncategorized", report.data)
        self.assertNotIn(b"New category suggested", report.data)
        self.assertNotIn(b"Create and apply", report.data)
        self.assertIn(b"View last results", self.client.get("/transactions").data)
        self.assertEqual(
            self.client.post(
                "/api/local-ai/category/G0002",
                data={"csrf_token": token},
            ).status_code,
            404,
        )

    def test_selected_rows_are_the_only_transactions_sent_to_local_model(self):
        self.complete_setup()
        self.enable_local_ai()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO category_rules (name, flow_type)
                VALUES ('Food And Drink', 'spending')
                """
            )
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type, subtype
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking',
                    'depository', 'checking'
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, category_override
                ) VALUES (?, 'checking', 1200, 'USD', ?, ?, 0, ?,
                          'Uncategorized', ?)
                """,
                [
                    ('selected', 'SELECTED CAFE', 'Selected Cafe', '2026-09-01', 'Income'),
                    ('not-selected', 'OTHER CAFE', 'Other Cafe', '2026-09-01', None),
                ],
            )

        page = self.client.get("/transactions")
        self.assertIn(b"Categorize all uncategorized", page.data)
        self.assertIn(b"Categorize ${count} selected", page.data)
        token = self.csrf_token(page)

        def classify(prepared, rows):
            self.assertEqual(
                [transaction_id for row in rows for transaction_id in row["transaction_ids"]],
                ["selected"],
            )
            return [{
                "id": rows[0]["evaluation_id"],
                "account_id": rows[0]["account_id"],
                "description": rows[0]["description"],
                "transaction_ids": rows[0]["transaction_ids"],
                "allow_recategorization": True,
                "status": "categorized",
                "category": "Food And Drink",
                "confidence": 1,
                "reason": "Cafe purchase.",
            }]

        with patch.object(
            self.application, "classify_evaluation_rows", side_effect=classify
        ):
            response = self.client.post(
                "/api/local-ai/evaluation",
                json={"transaction_ids": ["selected"]},
                headers={"X-CSRF-Token": token},
            )

        self.assertEqual(response.status_code, 200)
        with self.application.vault.connection() as connection:
            categories = dict(
                connection.execute(
                    "SELECT id, category_override FROM transactions ORDER BY id"
                ).fetchall()
            )
            effective = {
                row["id"]: row["effective_category"]
                for row in self.application.transaction_list(connection)
            }
            result = self.application.load_ollama_result(connection)
        self.assertIsNone(categories["selected"])
        self.assertIsNone(categories["not-selected"])
        self.assertEqual(effective["selected"], "Food And Drink")
        self.assertEqual(effective["not-selected"], "Uncategorized")
        self.assertTrue(result["targeted"])
        self.assertEqual(result["source_transaction_count"], 1)

    def test_unlock_reconciles_saved_model_result_into_historical_rule(self):
        self.complete_setup()
        self.enable_local_ai()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type, subtype
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking',
                    'depository', 'checking'
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, category_override,
                    category_override_source
                ) VALUES (?, 'checking', 1200, 'USD', 'SAMPLE CAFE', 'Sample Cafe',
                          0, ?, 'Uncategorized', ?, 'model')
                """,
                [
                    ('recent', '2026-08-01', 'Eating Out'),
                    ('older', '2024-08-01', 'Groceries'),
                ],
            )
            self.application.save_ollama_result(
                connection,
                {
                    "status": "completed",
                    "details": [{
                        "account_id": "checking",
                        "description": "SAMPLE CAFE",
                        "status": "categorized",
                        "category": "Eating Out",
                    }],
                },
            )

        self.application.lock_data()
        self.application.unlock_data(self.password)

        with self.application.vault.connection() as connection:
            effective = {
                row["id"]: row["effective_category"]
                for row in self.application.transaction_list(connection)
            }
            sources = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT id, category_override_source FROM transactions"
                )
            }
        self.assertEqual(effective["recent"], "Eating Out")
        self.assertEqual(effective["older"], "Eating Out")
        self.assertEqual(sources, {"recent": None, "older": None})

    def test_completed_batches_survive_a_later_model_timeout(self):
        self.complete_setup()
        self.enable_local_ai()
        with self.application.vault.connection() as connection:
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type, subtype
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking',
                    'depository', 'checking'
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category
                ) VALUES (?, 'checking', 1000, 'USD', ?, ?, 0, ?, 'Uncategorized')
                """,
                [
                    (
                        f"transaction-{index}",
                        f"DESCRIPTION {index}",
                        f"Merchant {index}",
                        f"2026-08-{index + 1:02d}",
                    )
                    for index in range(6)
                ],
            )

        calls = 0

        def classify(prepared, rows):
            nonlocal calls
            calls += 1
            if calls == 1:
                database_available = []

                def read_during_inference():
                    with self.application.vault.connection() as connection:
                        database_available.append(
                            connection.execute(
                                "SELECT COUNT(*) FROM transactions"
                            ).fetchone()[0]
                        )

                probe = threading.Thread(target=read_during_inference)
                probe.start()
                probe.join(1)
                self.assertFalse(probe.is_alive())
                self.assertEqual(database_available, [6])
            if calls == 2:
                raise TimeoutError("timed out")
            return [
                {
                    "id": row["evaluation_id"],
                    "account_id": row["account_id"],
                    "description": row["description"],
                    "transaction_ids": row["transaction_ids"],
                    "status": "categorized",
                    "category": "Food And Drink",
                    "confidence": 2,
                    "reason": "Purchase.",
                }
                for row in rows
            ]

        token = self.csrf_token(self.client.get("/transactions"))
        with patch.object(
            self.application, "classify_evaluation_rows", side_effect=classify
        ):
            response = self.client.post(
                "/api/local-ai/evaluation",
                json={"categories": ["Food And Drink"]},
                headers={"X-CSRF-Token": token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["ok"])
        with self.application.vault.connection() as connection:
            overrides = dict(
                connection.execute(
                    "SELECT id, category_override FROM transactions ORDER BY id"
                ).fetchall()
            )
            saved_result = self.application.load_ollama_result(connection)
        self.assertEqual(
            sum(value == "Food And Drink" for value in overrides.values()), 5
        )
        self.assertEqual(overrides["transaction-5"], None)
        self.assertEqual(saved_result["status"], "interrupted")
        self.assertEqual(saved_result["processed_group_count"], 5)
        self.assertEqual(saved_result["remaining_group_count"], 1)

        self.application.lock_data()
        self.application.unlock_data(self.password)
        with self.application.vault.connection() as connection:
            persisted = dict(
                connection.execute(
                    "SELECT id, category_override FROM transactions ORDER BY id"
                ).fetchall()
            )
            persisted_result = self.application.load_ollama_result(connection)
            effective = self.application.transaction_list(connection)
        self.assertEqual(
            sum(value == "Food And Drink" for value in persisted.values()), 0
        )
        self.assertEqual(persisted_result["status"], "interrupted")
        self.assertEqual(
            sum(row["effective_category"] == "Food And Drink" for row in effective),
            5,
        )


if __name__ == "__main__":
    unittest.main()
