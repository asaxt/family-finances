import importlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash


class AppMigrationTests(unittest.TestCase):
    def test_legacy_database_and_credentials_move_into_vault(self):
        password = "a long migration password"
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary)
            legacy_database = data_root / "plaid_budget.db"
            connection = sqlite3.connect(legacy_database)
            try:
                connection.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.commit()
            finally:
                connection.close()
            (data_root / ".auth.json").write_text(
                json.dumps(
                    {
                        "password_hash": generate_password_hash(password),
                        "secret_key": "test-session-key",
                    }
                )
            )

            previous = {
                name: os.environ.get(name)
                for name in (
                    "FAMILY_FINANCES_DATA_DIR",
                    "SONDER_DATA_DIR",
                    "FAMILY_FINANCES_MODE",
                    "FAMILY_FINANCES_PORT",
                    "FAMILY_FINANCES_DISABLE_PLAID",
                    "PLAID_CLIENT_ID",
                    "PLAID_SECRET",
                )
            }
            os.environ.update(
                {
                    "FAMILY_FINANCES_DATA_DIR": temporary,
                    "PLAID_CLIENT_ID": "test-client-id",
                    "PLAID_SECRET": "test-production-secret",
                }
            )
            os.environ.pop("FAMILY_FINANCES_MODE", None)
            os.environ.pop("FAMILY_FINANCES_PORT", None)
            os.environ.pop("FAMILY_FINANCES_DISABLE_PLAID", None)
            sys.modules.pop("app", None)
            application = importlib.import_module("app")
            try:
                client = application.app.test_client()
                login_page = client.get("/login")
                token = re.search(
                    rb'name="csrf_token" value="([^"]+)"', login_page.data
                ).group(1).decode()
                response = client.post(
                    "/login",
                    data={"password": password, "csrf_token": token},
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(application.VAULT_PATH.exists())
                self.assertTrue(legacy_database.exists())
                for page in ("/", "/trends", "/categories", "/transactions", "/savings", "/settings"):
                    self.assertEqual(client.get(page).status_code, 200, page)

                savings_page = client.get("/savings")
                authenticated_token = re.search(
                    rb'name="csrf_token" value="([^"]+)"', savings_page.data
                ).group(1).decode()
                with application.db() as connection:
                    initial_goal = connection.execute(
                        "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
                    ).fetchone()[0]
                self.assertEqual(initial_goal, "1000000")
                self.assertEqual(
                    client.post(
                        "/api/app-name",
                        data={
                            "csrf_token": authenticated_token,
                            "app_name": "My Money",
                        },
                    ).status_code,
                    302,
                )
                self.assertEqual(
                    client.post(
                        "/api/savings-goal",
                        data={"csrf_token": authenticated_token, "goal": "2500000"},
                    ).status_code,
                    302,
                )
                self.assertEqual(
                    client.post(
                        "/api/manual-accounts",
                        data={
                            "csrf_token": authenticated_token,
                            "institution": "Example Bank",
                            "name": "Brokerage",
                            "owner_name": "Household",
                            "classification": "post_tax",
                            "goal_eligible": "on",
                            "reminder_enabled": "on",
                        },
                    ).status_code,
                    302,
                )

                with application.db() as connection:
                    snapshot_count = connection.execute(
                        "SELECT COUNT(*) FROM savings_snapshots"
                    ).fetchone()[0]
                    credentials = dict(
                        connection.execute(
                            "SELECT key, value FROM settings WHERE key IN ('plaid_client_id', 'plaid_secret')"
                        ).fetchall()
                    )
                    goal = connection.execute(
                        "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
                    ).fetchone()[0]
                    added_account = connection.execute(
                        "SELECT classification, goal_eligible FROM manual_accounts WHERE institution = 'Example Bank'"
                    ).fetchone()
                self.assertEqual(snapshot_count, 0)
                self.assertEqual(credentials["plaid_client_id"], "test-client-id")
                self.assertEqual(credentials["plaid_secret"], "test-production-secret")
                self.assertEqual(goal, "250000000")
                self.assertEqual(tuple(added_account), ("post_tax", 1))
                self.assertEqual(application.display_name(), "My Money")

                encrypted = application.VAULT_PATH.read_bytes()
                self.assertNotIn(b"test-production-secret", encrypted)
                self.assertEqual(client.post("/api/sync").status_code, 400)
            finally:
                application.lock_data()
                sys.modules.pop("app", None)
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
