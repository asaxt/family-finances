import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class FakePlaidClient:
    def __init__(self, item):
        self.item = item
        self.item_get_requests = []
        self.link_token_requests = []

    def item_get(self, plaid_request):
        self.item_get_requests.append(plaid_request)
        return SimpleNamespace(item=self.item)

    def link_token_create(self, plaid_request):
        self.link_token_requests.append(plaid_request)
        return SimpleNamespace(link_token="fake-link-token")


class PlaidProductTests(unittest.TestCase):
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

        setup_page = self.client.get("/setup")
        password = "a long plaid product test password"
        self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": password,
                "confirmation": password,
            },
        )
        with self.application.db() as connection:
            connection.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (
                    ("plaid_client_id", "fake-client-id"),
                    ("plaid_secret", "fake-production-secret"),
                ),
            )
            connection.execute(
                """
                INSERT INTO connections (
                    plaid_item_id, owner_name, institution, access_token
                ) VALUES (?, ?, ?, ?)
                """,
                ("fake-item", "Household", "Test Bank", "fake-access-token"),
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

    def post_product_check(self, item):
        settings_page = self.client.get("/settings")
        fake_client = FakePlaidClient(item)
        with patch.object(self.application, "plaid_client", return_value=fake_client):
            response = self.client.post(
                "/api/plaid-products",
                data={"csrf_token": self.csrf_token(settings_page)},
            )
        return response, fake_client

    def test_transactions_only_status_ignores_merely_available_products(self):
        item = SimpleNamespace(
            products=["transactions"],
            billed_products=["transactions"],
            consented_products=["transactions"],
            available_products=["transfer"],
        )
        response, fake_client = self.post_product_check(item)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            fake_client.item_get_requests[0].access_token,
            "fake-access-token",
        )
        saved = json.loads(self.application.setting("plaid_product_audit"))
        self.assertNotIn("available_products", saved["connections"][0])
        self.assertNotIn("fake-access-token", json.dumps(saved))

        page = self.client.get(response.headers["Location"])
        self.assertIn(b"Plaid product access matches Transactions only", page.data)
        self.assertNotIn(b"Transfer", page.data)

    def test_unexpected_transfer_is_flagged_and_stored_encrypted(self):
        item = SimpleNamespace(
            products=["transactions"],
            billed_products=["transactions"],
            consented_products=["transactions", "transfer"],
        )
        response, _ = self.post_product_check(item)

        page = self.client.get(response.headers["Location"])
        self.assertIn(b"Unexpected: Transfer", page.data)
        self.assertIn(b"Unexpected Plaid product access needs review", page.data)
        encrypted = self.application.VAULT_PATH.read_bytes()
        self.assertNotIn(b"transfer", encrypted.lower())
        self.assertNotIn(b"fake-access-token", encrypted)

    def test_new_connections_request_transactions_only(self):
        settings_page = self.client.get("/settings")
        fake_client = FakePlaidClient(None)
        with patch.object(self.application, "plaid_client", return_value=fake_client):
            response = self.client.post(
                "/api/link-token",
                json={},
                headers={"X-CSRF-Token": self.csrf_token(settings_page)},
            )

        self.assertEqual(response.status_code, 200)
        requested = self.application.normalize_plaid_products(
            fake_client.link_token_requests[0].products
        )
        self.assertEqual(requested, ["transactions"])


if __name__ == "__main__":
    unittest.main()
