import importlib
import os
import re
import sys
import tempfile
import unittest
from datetime import date

from lab_scenarios import ACCOUNTS


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
        password = "a synthetic lab password"
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

    def load_scenario(self, scenario):
        page = self.client.get("/lab")
        return self.client.post(
            "/api/lab-scenario",
            data={
                "csrf_token": self.csrf_token(page),
                "scenario": scenario,
                "confirm_reset": "yes",
            },
        )

    def test_lab_mode_is_isolated_and_blocks_plaid(self):
        health = self.client.get("/health").get_json()
        self.assertEqual(health["mode"], "lab")
        self.assertFalse(health["plaid_enabled"])
        self.assertEqual(
            self.application.app.config["SESSION_COOKIE_NAME"],
            "family_finances_lab",
        )
        lab_page = self.client.get("/lab")
        self.assertIn(b"Synthetic lab", lab_page.data)
        self.assertIn(b"Checking + credit card", lab_page.data)
        self.assertNotIn(b'id="sync-button"', lab_page.data)
        self.assertNotIn(b'id="connect-button"', lab_page.data)

    def test_every_profile_loads_only_its_synthetic_accounts(self):
        for scenario_key, scenario in self.application.LAB_SCENARIOS.items():
            with self.subTest(scenario=scenario_key):
                self.assertEqual(self.load_scenario(scenario_key).status_code, 302)
                with self.application.db() as connection:
                    account_ids = {
                        row[0]
                        for row in connection.execute("SELECT id FROM accounts")
                    }
                    expected_ids = {
                        ACCOUNTS[key]["id"]
                        for key in scenario["accounts"]
                    }
                    self.assertEqual(account_ids, expected_ids)
                    self.assertFalse(
                        connection.execute("PRAGMA foreign_key_check").fetchall()
                    )
                    self.assertGreater(
                        connection.execute(
                            "SELECT COUNT(*) FROM transactions"
                        ).fetchone()[0],
                        0,
                    )

    def test_card_purchases_and_payments_have_separate_reporting_roles(self):
        self.load_scenario("checking_credit")
        with self.application.db() as connection:
            all_transactions = {
                row["id"]: row
                for row in self.application.transaction_list(
                    connection, reporting_scope="all"
                )
            }
            spending_ids = {
                row["id"]
                for row in self.application.transaction_list(
                    connection,
                    reporting_scope="spending",
                    spending_only=True,
                )
            }
            cash_flow_ids = {
                row["id"]
                for row in self.application.transaction_list(
                    connection, reporting_scope="cash_flow"
                )
            }
            cash_flow = self.application.cash_flow_summary(
                connection, lookback_days=30, today=date.today()
            )

        self.assertIn("lab-card-grocery", spending_ids)
        self.assertNotIn("lab-card-grocery", cash_flow_ids)
        self.assertIn("lab-card-payment-bank", cash_flow_ids)
        self.assertNotIn("lab-card-payment-bank", spending_ids)
        self.assertEqual(
            all_transactions["lab-card-payment-bank"]["flow_type"], "spending"
        )
        self.assertEqual(
            all_transactions["lab-card-payment-card"]["flow_type"], "transfer"
        )
        self.assertFalse(
            all_transactions["lab-card-payment-card"]["spending_included"]
        )
        self.assertFalse(
            all_transactions["lab-card-payment-card-ambiguous"][
                "spending_included"
            ]
        )
        self.assertGreater(cash_flow["spending"], 0)

    def test_loading_a_new_profile_discards_only_prior_lab_records(self):
        self.load_scenario("checking_credit")
        self.load_scenario("checking_debit")
        with self.application.db() as connection:
            account_ids = {
                row[0] for row in connection.execute("SELECT id FROM accounts")
            }
            card_transactions = connection.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_id = 'lab-card'"
            ).fetchone()[0]
        self.assertEqual(account_ids, {"lab-checking"})
        self.assertEqual(card_transactions, 0)


if __name__ == "__main__":
    unittest.main()
