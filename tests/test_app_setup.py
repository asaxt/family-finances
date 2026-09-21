import json
import importlib
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

from schema import schema_version


class AppSetupTests(unittest.TestCase):
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
        os.environ["FAMILY_FINANCES_DATA_DIR"] = self.temporary.name
        sys.modules.pop("app", None)
        self.application = importlib.import_module("app")
        self.application.app.config["TESTING"] = True
        self.client = self.application.app.test_client()
        self.assertEqual(self.application.app.config["SESSION_COOKIE_NAME"], "session")

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

    def test_new_installation_creates_current_schema_and_encrypted_settings(self):
        password = "a long setup password"
        setup_page = self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": password,
                "confirmation": password,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.application.VAULT_PATH.exists())
        self.assertTrue(self.application.AUTH_PATH.exists())

        for page in (
            "/",
            "/trends",
            "/categories",
            "/category-rules",
            "/transactions",
            "/cash-flow",
            "/savings",
            "/settings",
        ):
            self.assertEqual(self.client.get(page).status_code, 200, page)

        savings_page = self.client.get("/savings")
        token = self.csrf_token(savings_page)
        with self.application.db() as connection:
            self.assertEqual(schema_version(connection), 15)
            initial_goal = connection.execute(
                "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO connections (
                    plaid_item_id, owner_name, institution, access_token
                ) VALUES ('item', 'Household', 'Example Bank', 'fake-token')
                """
            )
            self.application.save_account(
                connection,
                SimpleNamespace(
                    account_id="checking",
                    name="Checking",
                    mask="1234",
                    type=SimpleNamespace(value="depository"),
                    subtype=SimpleNamespace(value="checking"),
                    balances=SimpleNamespace(current=1250.50, available=1200.25),
                ),
                1,
                "Example Bank",
                "2026-08-04T12:00:00",
            )
            checking = connection.execute(
                """
                SELECT subtype, current_balance, available_balance,
                       cash_flow_role, spending_enabled
                FROM accounts WHERE id = 'checking'
                """
            ).fetchone()
            self.application.save_transaction(
                connection,
                SimpleNamespace(
                    transaction_id="peer-payment",
                    account_id="checking",
                    amount=-25.00,
                    iso_currency_code="USD",
                    name="Payment",
                    merchant_name="Venmo",
                    pending=False,
                    date=date.today(),
                    personal_finance_category=SimpleNamespace(primary="TRANSFER_IN"),
                ),
            )
            peer_payment_category = connection.execute(
                "SELECT category FROM transactions WHERE id = 'peer-payment'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, excluded
                ) VALUES (
                    'deposit', 'checking', -50000, 'USD', 'Deposit', NULL,
                    0, ?, 'Other', 0
                )
                """,
                (date.today().isoformat(),),
            )
        self.assertEqual(initial_goal, "1000000")
        self.assertEqual(
            tuple(checking),
            ("checking", 125050, 120025, "cash_flow", 1),
        )
        self.assertEqual(peer_payment_category, "Uncategorized")
        overview_with_cash = self.client.get("/")
        self.assertIn(b"Current cash balances", overview_with_cash.data)
        cash_flow_page = self.client.get("/cash-flow")
        self.assertIn(b"Household \xc2\xb7 Example Bank", cash_flow_page.data)

        transactions_page = self.client.get("/transactions?purpose=all")
        self.assertIn(b"<summary>Cash flow", transactions_page.data)
        self.assertNotIn(b"<th>Spending</th>", transactions_page.data)
        self.assertIn(b'data-flow-type="spending"', transactions_page.data)
        with self.application.db() as connection:
            connection.execute("INSERT INTO category_rules (name, flow_type) VALUES ('Example receipts', 'earned_income')")
        defaults_page = self.client.get("/transactions?purpose=all")
        defaults = json.loads(re.search(
            rb'const categoryFlowDefaults = (.*);', defaults_page.data
        ).group(1))
        self.assertEqual(defaults['Example receipts'], 'earned_income')
        self.assertEqual(defaults['Transfer'], 'transfer')
        self.assertIn(b'applies to all transactions in this category', defaults_page.data)
        self.assertNotIn(b'Other money in', defaults_page.data)
        css_response = self.client.get("/static/app.css")
        try:
            self.assertIn(
                b"[hidden] { display: none !important; }",
                css_response.data,
            )
        finally:
            css_response.close()
        self.assertEqual(
            self.client.post(
                "/api/transaction/deposit",
                data={
                    "csrf_token": self.csrf_token(transactions_page),
                    "category_choice": "__new__",
                    "new_category": "  Payback  ",
                    "category_flow_type": "earned_income",
                },
            ).status_code,
            302,
        )
        with self.application.db() as connection:
            transaction = connection.execute(
                """
                SELECT category_override, category_override_source,
                       flow_override, excluded
                FROM transactions WHERE id = 'deposit'
                """
            ).fetchone()
            rule = connection.execute(
                "SELECT flow_type FROM category_rules WHERE name = 'payback'"
            ).fetchone()[0]
        self.assertEqual(tuple(transaction), ("Payback", "user", None, 0))
        self.assertEqual(rule, "earned_income")
        self.assertIn(b"Money in", self.client.get("/cash-flow").data)

        transactions_page = self.client.get("/transactions?purpose=all")
        self.client.post(
            "/api/transaction/deposit",
            data={
                "csrf_token": self.csrf_token(transactions_page),
                "category_choice": "__new__",
                "new_category": "payback",
                "category_flow_type": "earned_income",
            },
        )
        with self.application.db() as connection:
            category = connection.execute(
                "SELECT category_override FROM transactions WHERE id = 'deposit'"
            ).fetchone()[0]
            matching_rules = connection.execute(
                "SELECT COUNT(*) FROM category_rules WHERE name = 'payback' COLLATE NOCASE"
            ).fetchone()[0]
        self.assertEqual(category, "Payback")
        self.assertEqual(matching_rules, 1)

        transactions_page = self.client.get("/transactions?purpose=all")
        self.assertEqual(
            self.client.post(
                "/api/transactions/bulk",
                data={
                    "csrf_token": self.csrf_token(transactions_page),
                    "transaction_ids": ["deposit"],
                    "action": "apply",
                    "category_choice": "__no_change__",
                    "inclusion": "exclude",
                },
            ).status_code,
            302,
        )
        with self.application.db() as connection:
            transaction = connection.execute(
                "SELECT category_override, flow_override, excluded FROM transactions WHERE id = 'deposit'"
            ).fetchone()
        self.assertEqual(tuple(transaction), ("Payback", None, 1))

        self.assertEqual(
            self.client.post(
                "/api/app-name",
                data={"csrf_token": token, "app_name": "My Money"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                "/api/savings-goal",
                data={"csrf_token": token, "goal": "2500000"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                "/api/plaid-settings",
                data={
                    "csrf_token": token,
                    "client_id": "test-client-id",
                    "secret": "test-production-secret",
                },
            ).status_code,
            302,
        )

        with self.application.db() as connection:
            credentials = dict(
                connection.execute(
                    """
                    SELECT key, value FROM settings
                    WHERE key IN ('plaid_client_id', 'plaid_secret')
                    """
                ).fetchall()
            )
            goal = connection.execute(
                "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
            ).fetchone()[0]
        self.assertEqual(credentials["plaid_client_id"], "test-client-id")
        self.assertEqual(credentials["plaid_secret"], "test-production-secret")
        self.assertEqual(goal, "250000000")
        self.assertEqual(self.application.display_name(), "My Money")

        settings_page = self.client.get("/settings")
        self.assertIn(b"Accounts included in reporting", settings_page.data)
        self.assertIn(b"Include in reporting", settings_page.data)
        saved_roles = self.client.post(
            "/api/account-roles",
            data={
                "csrf_token": self.csrf_token(settings_page),
                "account_id": "checking",
                "reporting_purpose": "include",
            },
        )
        self.assertEqual(saved_roles.status_code, 302)
        self.assertIn("saved=account_roles", saved_roles.location)
        with self.application.db() as connection:
            role = connection.execute(
                """
                SELECT cash_flow_role, spending_enabled
                FROM accounts WHERE id = 'checking'
                """
            ).fetchone()
        self.assertEqual(tuple(role), ("cash_flow", 1))

        settings_page = self.client.get("/settings")
        ignored_role = self.client.post(
            "/api/account-roles",
            data={
                "csrf_token": self.csrf_token(settings_page),
                "account_id": "checking",
                "reporting_purpose": "ignore",
            },
        )
        self.assertEqual(ignored_role.status_code, 302)
        with self.application.db() as connection:
            role = connection.execute(
                """
                SELECT cash_flow_role, spending_enabled
                FROM accounts WHERE id = 'checking'
                """
            ).fetchone()
        self.assertEqual(tuple(role), ("other", 0))

        settings_page = self.client.get("/settings")
        rejected_roles = self.client.post(
            "/api/account-roles",
            data={
                "csrf_token": self.csrf_token(settings_page),
                "account_id": "checking",
                "reporting_purpose": "not-a-purpose",
            },
        )
        self.assertEqual(rejected_roles.status_code, 302)
        self.assertIn("error=account_roles", rejected_roles.location)

        encrypted = self.application.VAULT_PATH.read_bytes()
        self.assertNotIn(b"test-production-secret", encrypted)
        self.assertNotIn(b"test-client-id", encrypted)

        self.assertEqual(
            self.client.post(
                "/logout",
                data={"csrf_token": token},
            ).status_code,
            302,
        )
        login_page = self.client.get("/login")
        login_token = self.csrf_token(login_page)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)
        rejected_login = self.client.post(
            "/login",
            data={
                "csrf_token": login_token,
                "password": "deliberately incorrect password",
            },
        )
        self.assertEqual(rejected_login.status_code, 200)
        self.assertIn(b"That password is not correct", rejected_login.data)

    def test_local_ai_can_be_enabled_in_the_normal_app_and_defaults_off(self):
        password = "a long setup password"
        setup_page = self.client.get("/setup")
        self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": password,
                "confirmation": password,
            },
        )

        settings = self.client.get("/settings")
        self.assertIn(b"Local AI assistance", settings.data)
        self.assertNotIn(b'name="enabled" checked', settings.data)
        token = self.csrf_token(settings)
        blocked = self.client.post(
            "/api/local-ai/evaluation",
            json={},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(blocked.status_code, 403)

        response = self.client.post(
            "/api/local-ai",
            data={"csrf_token": token, "enabled": "on"},
        )
        self.assertEqual(response.location, "/settings?saved=local_ai")
        self.assertIn(b'name="enabled" checked', self.client.get("/settings").data)
        transactions = self.client.get("/transactions")
        self.assertIn(b"Set up categories", transactions.data)
        self.assertNotIn(b'id="run-local-categorization"', transactions.data)
        needs_categories = self.client.post(
            "/api/local-ai/evaluation",
            json={},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(needs_categories.status_code, 409)

    def test_recurring_transaction_rule_updates_existing_and_future_matches(self):
        setup_page = self.client.get("/setup")
        self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": "a recurring rule test password",
                "confirmation": "a recurring rule test password",
            },
        )
        with self.application.db() as connection:
            connection.execute(
                """
                INSERT INTO connections (
                    id, owner_name, institution, access_token
                ) VALUES (1, 'Household', 'Example Bank', 'fake-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type,
                    cash_flow_role, spending_enabled
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking', 'depository',
                    'cash_flow', 1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type,
                    cash_flow_role, spending_enabled
                ) VALUES (
                    'savings', 1, 'Example Bank', 'Savings', 'depository',
                    'cash_flow', 1
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, excluded
                ) VALUES (?, 'checking', 5000, 'USD', 'Payment detail 100',
                          'Recurring Payment', 0, '2026-08-05', 'Loan Payments', 0)
                """,
                (("reviewed",), ("existing-match",)),
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, excluded
                ) VALUES (
                    'other-account-match', 'savings', 5000, 'USD',
                    'Payment detail 300', 'Recurring Payment', 0,
                    '2026-08-05', 'Loan Payments', 0
                )
                """
            )

        page = self.client.get("/transactions?purpose=all")
        self.client.post(
            "/api/transaction/reviewed",
            data={
                "csrf_token": self.csrf_token(page),
                "category_choice": "__new__",
                "new_category": "Transfer",
                "category_flow_type": "transfer",
                "return_purpose": "all",
                "remember_match": "on",
                "apply_all_accounts": "on",
                "match_value": "Payment detail",
            },
        )
        with self.application.db() as connection:
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, excluded
                ) VALUES (
                    'future-match', 'checking', 6000, 'USD', 'Payment detail 200',
                    'Recurring Payment', 0, '2026-08-06', 'Loan Payments', 0
                )
                """
            )
            rows = {
                row["id"]: row
                for row in self.application.transaction_list(connection)
            }
            rule_count = connection.execute(
                "SELECT COUNT(*) FROM merchant_rules"
            ).fetchone()[0]
            saved_rule = connection.execute(
                """
                SELECT match_type, match_value, applies_all_accounts
                FROM merchant_rules
                """
            ).fetchone()
        self.assertEqual(rule_count, 1)
        self.assertEqual(saved_rule["match_type"], "description_contains")
        self.assertEqual(saved_rule["match_value"], "Payment detail")
        self.assertEqual(saved_rule["applies_all_accounts"], 1)
        for transaction_id in (
            "existing-match", "future-match", "other-account-match"
        ):
            self.assertEqual(rows[transaction_id]["effective_category"], "Transfer")
            self.assertEqual(rows[transaction_id]["flow_type"], "transfer")
            self.assertFalse(rows[transaction_id]["spending_included"])
        self.assertIn(
            b'data-recurring-rule="1"',
            self.client.get("/transactions?purpose=all").data,
        )
        self.assertIn(
            b'data-rule-all-accounts="1"',
            self.client.get("/transactions?purpose=all").data,
        )
        rules_page = self.client.get("/category-rules")
        self.assertIn(b"Payment detail", rules_page.data)
        self.assertIn(b"All accounts", rules_page.data)
        self.assertIn(b"4 transactions", rules_page.data)
        self.assertNotIn(
            b"Recurring Payment",
            self.application.VAULT_PATH.read_bytes(),
        )

        with self.application.db() as connection:
            connection.execute(
                """
                UPDATE transactions SET flow_override = 'spending'
                WHERE id = 'future-match'
                """
            )
            overridden = {
                row["id"]: row
                for row in self.application.transaction_list(connection)
            }["future-match"]
        self.assertEqual(overridden["flow_type"], "transfer")

        page = self.client.get("/transactions?purpose=all")
        self.client.post(
            "/api/transaction/reviewed",
            data={
                "csrf_token": self.csrf_token(page),
                "category_choice": "Transfer",
                "category_flow_type": "transfer",
                "return_purpose": "all",
            },
        )
        with self.application.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0],
                0,
            )
            reverted = {
                row["id"]: row
                for row in self.application.transaction_list(connection)
            }["existing-match"]
        self.assertEqual(reverted["effective_category"], "Loan Payments")
        self.assertEqual(reverted["flow_type"], "spending")
        self.assertTrue(reverted["spending_included"])

        page = self.client.get("/transactions?purpose=all")
        self.client.post(
            "/api/transaction/reviewed",
            data={
                "csrf_token": self.csrf_token(page),
                "category_choice": "Transfer",
                "category_flow_type": "transfer",
                "return_purpose": "all",
                "remember_match": "on",
                "match_value": "Payment detail",
            },
        )
        with self.application.db() as connection:
            local_rule = connection.execute(
                "SELECT applies_all_accounts FROM merchant_rules"
            ).fetchone()
            other_account = {
                row["id"]: row
                for row in self.application.transaction_list(connection)
            }["other-account-match"]
        self.assertEqual(local_rule["applies_all_accounts"], 0)
        self.assertEqual(other_account["effective_category"], "Loan Payments")

        with self.application.db() as connection:
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category
                ) VALUES ('savings', 'description', 'Example deposit', 'Income')
                """
            )
            rule_ids = dict(
                connection.execute("SELECT match_value, id FROM merchant_rules")
            )
        filtered = self.client.get("/category-rules?q=Payment&scope=account")
        self.assertIn(b"Payment detail", filtered.data)
        self.assertNotIn(b"Example deposit", filtered.data)
        self.assertEqual(filtered.data.count(b'class="rule-select"'), 1)
        category_filtered = self.client.get("/category-rules?category=Income")
        self.assertIn(b"Example deposit", category_filtered.data)
        self.assertNotIn(b"Payment detail", category_filtered.data)

        rules_page = self.client.get("/category-rules")
        self.client.post(
            "/api/category-rules/bulk",
            data={
                "csrf_token": self.csrf_token(rules_page),
                "rule_ids": list(rule_ids.values()),
                "scope_change": "all",
                "category_change": "Transfer",
            },
        )
        with self.application.db() as connection:
            updated = connection.execute(
                """
                SELECT category, applies_all_accounts FROM merchant_rules
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in updated],
            [("Transfer", 1), ("Transfer", 1)],
        )

        rule_id = rule_ids["Payment detail"]
        rules_page = self.client.get("/category-rules")
        self.client.post(
            f"/api/category-rule/{rule_id}",
            data={
                "csrf_token": self.csrf_token(rules_page),
                "match_value": "Payment detail",
                "category": "Transfer",
                "apply_all_accounts": "on",
            },
        )
        with self.application.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT applies_all_accounts FROM merchant_rules WHERE id = ?",
                    (rule_id,),
                ).fetchone()[0],
                1,
            )
        rules_page = self.client.get("/category-rules")
        self.client.post(
            f"/api/category-rule/{rule_id}/delete",
            data={"csrf_token": self.csrf_token(rules_page)},
        )
        rules_page = self.client.get("/category-rules")
        self.client.post(
            f"/api/category-rule/{rule_ids['Example deposit']}/delete",
            data={"csrf_token": self.csrf_token(rules_page)},
        )
        with self.application.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
