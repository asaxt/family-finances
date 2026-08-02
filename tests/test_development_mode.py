import importlib
import os
import re
import sys
import tempfile
import unittest


class DevelopmentModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment_names = (
            "FAMILY_FINANCES_DATA_DIR",
            "SONDER_DATA_DIR",
            "FAMILY_FINANCES_MODE",
            "FAMILY_FINANCES_PORT",
            "FAMILY_FINANCES_DISABLE_PLAID",
            "PLAID_CLIENT_ID",
            "PLAID_SECRET",
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

    def test_development_mode_is_visible_and_blocks_plaid(self):
        health = self.client.get("/health").get_json()
        self.assertEqual(health["mode"], "development")
        self.assertFalse(health["plaid_enabled"])

        setup_page = self.client.get("/setup")
        self.assertIn(b"Development", setup_page.data)
        password = "a long development password"
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": password,
                "confirmation": password,
            },
        )
        self.assertEqual(response.status_code, 302)

        overview = self.client.get("/")
        token = self.csrf_token(overview)
        self.assertIn(b"Development", overview.data)
        self.assertIn(b"Plaid is disabled in this environment", overview.data)
        self.assertNotIn(b'id="sync-button"', overview.data)
        self.assertNotIn(b'id="connect-button"', overview.data)

        settings = self.client.get("/settings")
        self.assertIn(
            b"Plaid connections and synchronization are disabled",
            settings.data,
        )

        for path, keyword in (
            ("/api/link-token", {}),
            ("/api/exchange-token", {}),
            ("/api/sync", None),
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


if __name__ == "__main__":
    unittest.main()
