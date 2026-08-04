import importlib
import os
import re
import sys
import tempfile
import unittest


class OverviewTests(unittest.TestCase):
    PASSWORD = "a long overview test password"

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
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": self.PASSWORD,
                "confirmation": self.PASSWORD,
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

    def test_overview_defaults_to_thirty_days_without_a_month_selector(self):
        overview = self.client.get("/")

        self.assertEqual(overview.status_code, 200)
        self.assertIn(b"Past 30 days", overview.data)
        self.assertIn(b'name="lookback_days" value="30"', overview.data)
        self.assertNotIn(b'type="month"', overview.data)
        self.assertIn(b"Projected calendar month", overview.data)
        self.assertIn(b"vs prior average", overview.data)

    def test_valid_lookback_is_encrypted_and_invalid_values_are_rejected(self):
        overview = self.client.get("/")
        response = self.client.post(
            "/api/overview-lookback",
            data={
                "csrf_token": self.csrf_token(overview),
                "lookback_days": "45",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.application.db() as connection:
            stored = connection.execute(
                "SELECT value FROM settings WHERE key = 'overview_lookback_days'"
            ).fetchone()[0]
        self.assertEqual(stored, "45")
        updated = self.client.get("/")
        self.assertIn(b"Past 45 days", updated.data)
        self.assertIn(b'name="lookback_days" value="45"', updated.data)
        self.assertNotIn(
            b"overview_lookback_days",
            self.application.VAULT_PATH.read_bytes(),
        )

        for invalid in ("0", "366", "not-a-number"):
            with self.subTest(invalid=invalid):
                page = self.client.get("/")
                rejected = self.client.post(
                    "/api/overview-lookback",
                    data={
                        "csrf_token": self.csrf_token(page),
                        "lookback_days": invalid,
                    },
                )
                self.assertEqual(rejected.status_code, 302)
                self.assertIn("error=lookback", rejected.location)
                self.assertEqual(self.application.overview_lookback_days(), 45)


if __name__ == "__main__":
    unittest.main()
