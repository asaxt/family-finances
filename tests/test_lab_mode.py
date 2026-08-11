import importlib
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plaid.exceptions import ApiException


class FakeSandboxClient:
    def __init__(self):
        self.public_token_request = None
        self.sync_calls = 0
        self.accounts = [
            SimpleNamespace(
                account_id="sandbox-checking",
                name="Lab Checking",
                mask="0000",
                type=SimpleNamespace(value="depository"),
                subtype=SimpleNamespace(value="checking"),
                balances=SimpleNamespace(current=5000.0, available=4800.0),
            ),
            SimpleNamespace(
                account_id="sandbox-card",
                name="Lab Credit Card",
                mask="3333",
                type=SimpleNamespace(value="credit"),
                subtype=SimpleNamespace(value="credit card"),
                balances=SimpleNamespace(current=750.0, available=4250.0),
            ),
        ]

    def sandbox_public_token_create(self, request):
        self.public_token_request = request
        return SimpleNamespace(public_token="public-sandbox-test")

    def item_public_token_exchange(self, request):
        return SimpleNamespace(
            access_token="access-sandbox-test",
            item_id="item-sandbox-test",
        )

    def accounts_get(self, request):
        return SimpleNamespace(accounts=self.accounts)

    def transactions_sync(self, request):
        self.sync_calls += 1
        ready = self.sync_calls > 1
        return SimpleNamespace(
            added=(
                [
                    SimpleNamespace(
                        transaction_id="sandbox-grocery",
                        account_id="sandbox-checking",
                        amount=42.5,
                        iso_currency_code="USD",
                        name="Plaid Sandbox Grocery",
                        merchant_name="Sandbox Grocery",
                        pending=False,
                        date=date.today(),
                        personal_finance_category=SimpleNamespace(
                            primary="FOOD_AND_DRINK"
                        ),
                    )
                ]
                if ready
                else []
            ),
            modified=[],
            removed=[],
            next_cursor=f"sandbox-cursor-{self.sync_calls}",
            transactions_update_status=SimpleNamespace(
                value="HISTORICAL_UPDATE_COMPLETE" if ready else "NOT_READY"
            ),
            has_more=False,
        )


class LabModeTests(unittest.TestCase):
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
                "FAMILY_FINANCES_MODE": "lab",
                "FAMILY_FINANCES_PORT": "4244",
            }
        )
        sys.modules.pop("app", None)
        self.application = importlib.import_module("app")
        self.application.app.config["TESTING"] = True
        self.client = self.application.app.test_client()

        setup_page = self.client.get("/setup")
        password = "a Plaid Sandbox lab password"
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": password,
                "confirmation": password,
            },
        )
        self.assertEqual(response.status_code, 302)

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

    def test_lab_is_isolated_and_forced_to_plaid_sandbox(self):
        health = self.client.get("/health").get_json()
        self.assertEqual(health["mode"], "lab")
        self.assertTrue(health["plaid_enabled"])
        self.assertEqual(health["plaid_environment"], "sandbox")
        self.assertEqual(self.application.PLAID_HOST, "https://sandbox.plaid.com")
        self.assertEqual(
            self.application.plaid_client().api_client.configuration.host,
            "https://sandbox.plaid.com",
        )
        self.assertEqual(
            self.application.app.config["SESSION_COOKIE_NAME"],
            "family_finances_lab",
        )

        lab_page = self.client.get("/lab")
        self.assertIn(b"Family Finances Lab", lab_page.data)
        self.assertIn(b"Plaid sandbox", lab_page.data)
        self.assertIn(b"sandbox.plaid.com", lab_page.data)
        self.assertIn(b"Checking + credit card", lab_page.data)
        self.assertIn(b"First Gingham Credit Union", lab_page.data)
        self.assertIn(b"Full household", lab_page.data)
        self.assertIn(b"Card purchases, payment, and refund", lab_page.data)
        self.assertIn(b'name="institution"', lab_page.data)
        self.assertIn(b'name="account_profile"', lab_page.data)
        self.assertIn(b'name="transaction_profile"', lab_page.data)
        self.assertIn(b'id="sync-button"', lab_page.data)
        self.assertNotIn(b'id="connect-button"', lab_page.data)
        self.assertNotIn(b"cdn.plaid.com", lab_page.data)
        self.assertIn(b'action="/api/lab-connect"', lab_page.data)
        self.assertNotIn(b"Synthetic lab", lab_page.data)

        token = self.csrf_token(lab_page)
        self.assertEqual(
            self.client.post(
                "/api/link-token",
                json={},
                headers={"X-CSRF-Token": token},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/api/exchange-token",
                json={},
                headers={"X-CSRF-Token": token},
            ).status_code,
            404,
        )

        settings = self.client.get("/settings")
        self.assertIn(b"Plaid Sandbox account", settings.data)
        self.assertIn(b"Sandbox secret", settings.data)
        self.assertNotIn(b"<span>Production secret</span>", settings.data)

    def test_profile_creation_bypasses_link_and_imports_plaid_accounts(self):
        with self.application.db() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (
                    ("plaid_client_id", "sandbox-client"),
                    ("plaid_secret", "sandbox-secret"),
                ),
            )
        fake_client = FakeSandboxClient()
        page = self.client.get("/lab")
        with (
            patch.object(self.application, "plaid_client", return_value=fake_client),
            patch.object(self.application.time, "sleep"),
        ):
            response = self.client.post(
                "/api/lab-connect",
                data={
                    "csrf_token": self.csrf_token(page),
                    "institution": "tartan",
                    "account_profile": "checking_credit",
                    "transaction_profile": "card_activity",
                    "owner_name": "Test household",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("created=checking_credit", response.location)
        self.assertIn("imported=1", response.location)
        self.assertEqual(fake_client.sync_calls, 2)

        request_data = fake_client.public_token_request.to_dict()
        self.assertEqual(request_data["institution_id"], "ins_109511")
        self.assertEqual(request_data["initial_products"], ["transactions"])
        self.assertEqual(
            request_data["options"]["override_username"], "user_custom"
        )
        custom_user = self.application.json.loads(
            request_data["options"]["override_password"]
        )
        self.assertEqual(
            [account["type"] for account in custom_user["override_accounts"]],
            ["depository", "credit"],
        )
        self.assertIn("transactions", custom_user["override_accounts"][0])
        self.assertIn("transactions", custom_user["override_accounts"][1])

        with self.application.db() as connection:
            saved_connection = connection.execute(
                """
                SELECT plaid_item_id, owner_name, institution, access_token, cursor
                FROM connections
                """
            ).fetchone()
            saved_accounts = connection.execute(
                """
                SELECT type, cash_flow_role, spending_enabled
                FROM accounts ORDER BY type
                """
            ).fetchall()
        self.assertEqual(
            tuple(saved_connection),
            (
                "item-sandbox-test",
                "Test household",
                "Tartan Bank",
                "access-sandbox-test",
                "sandbox-cursor-2",
            ),
        )
        self.assertEqual(
            [tuple(account) for account in saved_accounts],
            [("credit", "credit_card", 1), ("depository", "cash_flow", 0)],
        )
        with self.application.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
                1,
            )

    def test_plaid_failure_shows_safe_error_code_and_request_id(self):
        with self.application.db() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (
                    ("plaid_client_id", "sandbox-client"),
                    ("plaid_secret", "sandbox-secret"),
                ),
            )
        plaid_error = ApiException(status=400)
        plaid_error.body = self.application.json.dumps(
            {
                "error_type": "INVALID_INPUT",
                "error_code": "INVALID_FIELD",
                "error_message": "The custom user profile is invalid.",
                "request_id": "safe-request-id",
            }
        )
        fake_client = Mock()
        fake_client.sandbox_public_token_create.side_effect = plaid_error
        page = self.client.get("/lab")
        with patch.object(self.application, "plaid_client", return_value=fake_client):
            response = self.client.post(
                "/api/lab-connect",
                data={
                    "csrf_token": self.csrf_token(page),
                    "institution": "platypus",
                    "account_profile": "checking",
                    "transaction_profile": "plaid_generated",
                    "owner_name": "Test household",
                },
                follow_redirects=True,
            )
        self.assertIn(b"INVALID_FIELD", response.data)
        self.assertIn(b"The custom user profile is invalid.", response.data)
        self.assertIn(b"safe-request-id", response.data)
        self.assertNotIn(b"sandbox-secret", response.data)

    def test_custom_transaction_profiles_cover_payment_transfer_refund_and_pending(self):
        accounts = self.application.build_sandbox_accounts(
            "checking_savings_credit",
            "comprehensive",
            today=date(2026, 8, 9),
        )
        checking, savings, credit = accounts
        allowed_fields = {
            "date_transacted",
            "date_posted",
            "amount",
            "description",
            "currency",
        }
        for account in accounts:
            self.assertNotIn("role", account)
            for transaction in account["transactions"]:
                self.assertEqual(set(transaction), allowed_fields)

        checking_by_description = {
            transaction["description"]: transaction
            for transaction in checking["transactions"]
        }
        savings_by_description = {
            transaction["description"]: transaction
            for transaction in savings["transactions"]
        }
        credit_by_description = {
            transaction["description"]: transaction
            for transaction in credit["transactions"]
        }
        self.assertEqual(checking_by_description["CREDIT CARD AUTOPAY"]["amount"], 850.0)
        self.assertEqual(
            credit_by_description["AUTOMATIC PAYMENT - THANK YOU"]["amount"],
            -850.0,
        )
        self.assertEqual(
            checking_by_description["ONLINE TRANSFER TO SAVINGS"]["amount"],
            600.0,
        )
        self.assertEqual(
            savings_by_description["ONLINE TRANSFER FROM CHECKING"]["amount"],
            -600.0,
        )
        self.assertEqual(
            credit_by_description["FRESH FOODS MARKET REFUND"]["amount"],
            -34.2,
        )
        self.assertEqual(
            checking_by_description["PENDING DEBIT PURCHASE"]["date_posted"],
            "2026-08-10",
        )

        for account_profile in self.application.SANDBOX_ACCOUNT_PROFILES:
            for transaction_profile in self.application.SANDBOX_TRANSACTION_PROFILES:
                generated = self.application.build_sandbox_accounts(
                    account_profile,
                    transaction_profile,
                    today=date(2026, 8, 9),
                )
                self.assertLessEqual(len(generated), 10)
                self.assertTrue(generated)
        plaid_generated = self.application.build_sandbox_accounts(
            "checking", "plaid_generated", today=date(2026, 8, 9)
        )
        self.assertNotIn("transactions", plaid_generated[0])

    def test_invalid_sandbox_dimension_is_rejected_before_plaid(self):
        page = self.client.get("/lab")
        response = self.client.post(
            "/api/lab-connect",
            data={
                "csrf_token": self.csrf_token(page),
                "institution": "not-a-bank",
                "account_profile": "checking",
                "transaction_profile": "cash_flow",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("error=profile", response.location)
        self.assertEqual(
            self.client.get(
                "/lab?created=invalid&transactions=invalid&institution=invalid"
            ).status_code,
            200,
        )

    def test_reset_clears_only_lab_data_and_keeps_sandbox_credentials(self):
        with self.application.db() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (
                    ("plaid_client_id", "sandbox-client"),
                    ("plaid_secret", "sandbox-secret"),
                    ("plaid_product_audit", "{}"),
                    ("lab_scenario", "legacy-synthetic-profile"),
                ),
            )
            connection.execute(
                """
                INSERT INTO connections (
                    id, plaid_item_id, owner_name, institution, access_token
                ) VALUES (1, 'sandbox-item', 'Lab user', 'Sandbox bank', 'sandbox-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type
                ) VALUES ('sandbox-checking', 1, 'Sandbox bank', 'Checking', 'depository')
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, pending,
                    transacted_at, category, excluded
                ) VALUES (
                    'sandbox-transaction', 'sandbox-checking', 1000, 'USD',
                    'Test purchase', 0, '2026-08-09', 'Shopping', 0
                )
                """
            )
            connection.execute(
                "INSERT INTO category_rules (name, flow_type) VALUES ('Shopping', 'spending')"
            )
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category, flow_type
                ) VALUES (
                    'sandbox-checking', 'description', 'Test purchase',
                    'Shopping', 'spending'
                )
                """
            )
            cursor = connection.execute(
                """
                INSERT INTO manual_accounts (
                    institution, name, owner_name, classification
                ) VALUES ('Sandbox bank', 'Savings', 'Lab user', 'taxable')
                """
            )
            connection.execute(
                """
                INSERT INTO savings_snapshots (
                    manual_account_id, amount, recorded_on
                ) VALUES (?, 5000, '2026-08-09')
                """,
                (cursor.lastrowid,),
            )

        page = self.client.get("/lab")
        token = self.csrf_token(page)
        rejected = self.client.post(
            "/api/lab-reset",
            data={"csrf_token": token},
        )
        self.assertIn("error=confirmation", rejected.location)

        page = self.client.get("/lab")
        reset = self.client.post(
            "/api/lab-reset",
            data={"csrf_token": self.csrf_token(page), "confirm_reset": "yes"},
        )
        self.assertEqual(reset.status_code, 302)
        self.assertIn("reset=1", reset.location)

        with self.application.db() as connection:
            for table in (
                "connections",
                "accounts",
                "transactions",
                "category_rules",
                "merchant_rules",
                "manual_accounts",
                "savings_snapshots",
            ):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                    table,
                )
            settings = dict(
                connection.execute(
                    "SELECT key, value FROM settings"
                ).fetchall()
            )
        self.assertEqual(settings["plaid_client_id"], "sandbox-client")
        self.assertEqual(settings["plaid_secret"], "sandbox-secret")
        self.assertIn("savings_goal_cents", settings)
        self.assertNotIn("plaid_product_audit", settings)
        self.assertNotIn("lab_scenario", settings)


if __name__ == "__main__":
    unittest.main()
