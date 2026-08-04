import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from vault import EncryptedDatabase, VaultError, unlock_key


class PasswordChangeTests(unittest.TestCase):
    OLD_PASSWORD = "a long original password"
    NEW_PASSWORD = "a different secure passphrase"

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
        self.setup_app()

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

    def setup_app(self):
        setup_page = self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "csrf_token": self.csrf_token(setup_page),
                "password": self.OLD_PASSWORD,
                "confirmation": self.OLD_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.application.db() as connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('password_test', 'preserved')"
            )

    def change_password(self, **overrides):
        values = {
            "current_password": self.OLD_PASSWORD,
            "new_password": self.NEW_PASSWORD,
            "confirmation": self.NEW_PASSWORD,
        }
        values.update(overrides)
        settings_page = self.client.get("/settings")
        return self.client.post(
            "/api/password",
            data={"csrf_token": self.csrf_token(settings_page), **values},
        )

    def login(self, client, password):
        login_page = client.get("/login")
        return client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(login_page),
                "password": password,
            },
        )

    def test_success_rotates_encryption_and_invalidates_old_sessions(self):
        original_vault = self.application.VAULT_PATH.read_bytes()
        original_auth = json.loads(self.application.AUTH_PATH.read_text())
        original_secret_key = self.application.app.secret_key

        other_client = self.application.app.test_client()
        self.assertEqual(self.login(other_client, self.OLD_PASSWORD).status_code, 302)

        response = self.change_password()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login?changed=1"))
        self.assertFalse(self.application.vault.unlocked)
        self.assertNotEqual(self.application.VAULT_PATH.read_bytes(), original_vault)
        self.assertNotEqual(self.application.app.secret_key, original_secret_key)
        self.assertEqual(
            list(self.application.DATA_ROOT.glob(".password-change-backup-*")),
            [],
        )

        current_auth = self.application.AUTH_PATH.read_bytes()
        self.assertNotIn(self.OLD_PASSWORD.encode(), current_auth)
        self.assertNotIn(self.NEW_PASSWORD.encode(), current_auth)

        login_page = self.client.get("/login?changed=1")
        self.assertIn(b"Password changed", login_page.data)
        old_login = self.login(self.client, self.OLD_PASSWORD)
        self.assertIn(b"That password is not correct", old_login.data)
        self.assertEqual(self.login(self.client, self.NEW_PASSWORD).status_code, 302)

        with self.application.db() as connection:
            value = connection.execute(
                "SELECT value FROM settings WHERE key = 'password_test'"
            ).fetchone()[0]
        self.assertEqual(value, "preserved")
        self.assertEqual(other_client.get("/").status_code, 302)

        old_data_key = unlock_key(self.OLD_PASSWORD, original_auth["vault_key"])
        old_database = EncryptedDatabase(self.application.VAULT_PATH)
        with self.assertRaises(VaultError):
            old_database.unlock(old_data_key)

    def test_invalid_entries_do_not_change_encrypted_files(self):
        original_vault = self.application.VAULT_PATH.read_bytes()
        original_auth = self.application.AUTH_PATH.read_bytes()
        invalid_entries = (
            ({"current_password": "incorrect password"}, "current_password"),
            ({"new_password": "too short", "confirmation": "too short"}, "password_length"),
            ({"confirmation": "does not match"}, "password_match"),
            (
                {
                    "new_password": self.OLD_PASSWORD,
                    "confirmation": self.OLD_PASSWORD,
                },
                "password_same",
            ),
        )

        for values, error_code in invalid_entries:
            with self.subTest(error_code=error_code):
                response = self.change_password(**values)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith(f"error={error_code}"))
                self.assertEqual(self.application.VAULT_PATH.read_bytes(), original_vault)
                self.assertEqual(self.application.AUTH_PATH.read_bytes(), original_auth)

    def test_backup_cleanup_failure_reports_success_and_retains_recovery_copy(self):
        with patch.object(
            self.application,
            "delete_encrypted_backup",
            side_effect=OSError("simulated backup cleanup failure"),
        ):
            response = self.change_password()

        self.assertEqual(response.status_code, 302)
        self.assertIn("changed=1", response.location)
        self.assertIn("backup=1", response.location)
        self.assertFalse(self.application.vault.unlocked)
        backups = list(
            self.application.DATA_ROOT.glob(".password-change-backup-*")
        )
        self.assertEqual(len(backups), 1)

        login_page = self.client.get(response.location)
        self.assertIn(b"encrypted recovery copy could not be removed", login_page.data)
        self.assertEqual(self.login(self.client, self.NEW_PASSWORD).status_code, 302)

    def test_verification_failure_restores_original_pair_and_keeps_backup(self):
        original_vault = self.application.VAULT_PATH.read_bytes()
        original_auth = self.application.AUTH_PATH.read_bytes()
        rewritten_pair = []
        unlock_data = self.application.unlock_data

        def fail_new_password_verification(password):
            if password == self.NEW_PASSWORD:
                rewritten_pair.append(
                    (
                        self.application.VAULT_PATH.read_bytes(),
                        self.application.AUTH_PATH.read_bytes(),
                    )
                )
                raise VaultError("simulated verification failure")
            return unlock_data(password)

        with patch.object(
            self.application,
            "unlock_data",
            side_effect=fail_new_password_verification,
        ):
            response = self.change_password()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("error=password_change"))
        self.assertEqual(len(rewritten_pair), 1)
        self.assertNotEqual(rewritten_pair[0][0], original_vault)
        self.assertNotEqual(rewritten_pair[0][1], original_auth)
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), original_vault)
        self.assertEqual(self.application.AUTH_PATH.read_bytes(), original_auth)
        self.assertTrue(self.application.vault.unlocked)
        self.assertEqual(self.client.get("/settings").status_code, 200)

        backups = list(
            self.application.DATA_ROOT.glob(".password-change-backup-*")
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(os.stat(backups[0]).st_mode & 0o777, 0o700)
        self.assertEqual(
            (backups[0] / self.application.VAULT_PATH.name).read_bytes(),
            original_vault,
        )
        self.assertEqual(
            (backups[0] / self.application.AUTH_PATH.name).read_bytes(),
            original_auth,
        )
        self.assertEqual(
            os.stat(backups[0] / self.application.VAULT_PATH.name).st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            os.stat(backups[0] / self.application.AUTH_PATH.name).st_mode & 0o777,
            0o600,
        )
        self.assertNotIn(
            b"preserved",
            (backups[0] / self.application.VAULT_PATH.name).read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
