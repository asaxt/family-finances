import importlib
import os
import re
import sys
import tempfile
import unittest

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

    def test_new_installation_creates_schema_zero_and_encrypted_settings(self):
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
            "/transactions",
            "/savings",
            "/settings",
        ):
            self.assertEqual(self.client.get(page).status_code, 200, page)

        savings_page = self.client.get("/savings")
        token = self.csrf_token(savings_page)
        with self.application.db() as connection:
            self.assertEqual(schema_version(connection), 0)
            initial_goal = connection.execute(
                "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
            ).fetchone()[0]
        self.assertEqual(initial_goal, "1000000")

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


if __name__ == "__main__":
    unittest.main()
