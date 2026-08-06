import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import schema
from vault import EncryptedDatabase, create_key_record


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_schema_is_version_two_and_strictly_validated(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.assertEqual(schema.schema_version(connection), 2)
            schema.validate_schema(connection)
            goal = connection.execute(
                "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
            ).fetchone()[0]
            self.assertEqual(goal, "1000000")
            with self.assertRaisesRegex(schema.SchemaError, "empty database"):
                schema.create_schema(connection)
        finally:
            connection.close()

    def test_unrecognized_version_zero_is_rejected(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE settings (key TEXT, value TEXT)")
            with self.assertRaisesRegex(schema.SchemaError, "not recognized"):
                schema.validate_schema(connection)
        finally:
            connection.close()

    def test_newer_schema_is_rejected_without_a_backup(self):
        database, key, auth_path = self.encrypted_schema_zero()
        with database.connection() as connection:
            connection.execute("PRAGMA user_version = 3")
        database.persist()

        with self.assertRaisesRegex(schema.SchemaError, "supports up to version 2"):
            schema.prepare_encrypted_database(database, key, auth_path)
        self.assertEqual(list(self.root.glob(".migration-backup-*")), [])

    def test_successful_migration_deletes_encrypted_backup(self):
        database, key, auth_path = self.encrypted_schema_zero()
        changed = schema.prepare_encrypted_database(database, key, auth_path)
        self.assertTrue(changed)
        with database.connection() as connection:
            self.assertEqual(schema.schema_version(connection), 2)
            account_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(accounts)")
            }
            transaction_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transactions)")
            }
        self.assertIn("available_balance", account_columns)
        self.assertIn("subtype", account_columns)
        self.assertIn("flow_override", transaction_columns)

        self.assertEqual(list(self.root.glob(".migration-backup-*")), [])
        self.assertNotIn(b"flow_override", database.path.read_bytes())

    def test_version_one_classifications_migrate_to_explicit_treatments(self):
        database, key, auth_path = self.encrypted_schema_zero()
        with database.connection() as connection:
            schema._migrate_zero_to_one(connection)
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('card', 1, 'Example Bank', 'Card', 'credit')
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, pending,
                    transacted_at, category, cash_flow_override, excluded
                ) VALUES (?, 'card', ?, 'USD', ?, 0, '2026-08-01', ?, ?, ?)
                """,
                (
                    ("income", -100, "Income", "Income", "income", 0),
                    ("refund", -200, "Refund", "Other", "refund", 0),
                    ("payment", -300, "Payment", "Loan Payments", None, 1),
                    ("ignored", 400, "Ignored", "Other", "ignore", 0),
                ),
            )
            connection.execute("PRAGMA user_version = 1")
            schema._validate_version_one(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            rows = {
                row[0]: tuple(row[1:])
                for row in connection.execute(
                    "SELECT id, flow_override, excluded FROM transactions"
                )
            }
        self.assertEqual(rows["income"], ("earned_income", 0))
        self.assertEqual(rows["refund"], ("other_inflow", 0))
        self.assertEqual(rows["payment"], ("transfer", 0))
        self.assertEqual(rows["ignored"], (None, 1))

    def test_failed_migration_restores_original_and_keeps_backup(self):
        database, key, auth_path = self.encrypted_schema_zero()
        original_vault = database.path.read_bytes()
        original_auth = auth_path.read_bytes()

        def fail_migration(connection):
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('migration_test', 'partial')"
            )
            raise RuntimeError("simulated migration failure")

        with patch.dict(schema.MIGRATIONS, {0: fail_migration}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "simulated migration failure"):
                schema.prepare_encrypted_database(database, key, auth_path)

        self.assertEqual(database.path.read_bytes(), original_vault)
        self.assertEqual(auth_path.read_bytes(), original_auth)
        backups = list(self.root.glob(".migration-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(os.stat(backups[0]).st_mode & 0o777, 0o700)
        backup_vault = backups[0] / database.path.name
        backup_auth = backups[0] / auth_path.name
        self.assertEqual(os.stat(backup_vault).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(backup_auth).st_mode & 0o777, 0o600)
        self.assertNotIn(b"migration_test", backup_vault.read_bytes())
        with database.connection() as connection:
            self.assertEqual(schema.schema_version(connection), 0)
            self.assertIsNone(
                connection.execute(
                    "SELECT value FROM settings WHERE key = 'migration_test'"
                ).fetchone()
            )

    def encrypted_schema_zero(self):
        auth_path = self.root / ".auth.json"
        auth_path.write_text(json.dumps({"test": True}))
        os.chmod(auth_path, 0o600)
        _, key = create_key_record("a schema test password")
        database = EncryptedDatabase(self.root / "family-finances.vault")
        database.create(key)
        database.unlock(key)
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            connection.execute("DROP TABLE category_rules")
            connection.execute("ALTER TABLE transactions DROP COLUMN flow_override")
            connection.execute("ALTER TABLE accounts DROP COLUMN available_balance")
            connection.execute("ALTER TABLE accounts DROP COLUMN subtype")
            connection.execute("PRAGMA user_version = 0")
            schema._validate_version_zero(connection)
        database.persist()
        return database, key, auth_path


if __name__ == "__main__":
    unittest.main()
