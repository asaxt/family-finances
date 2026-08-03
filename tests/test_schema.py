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

    def test_new_schema_is_version_zero_and_strictly_validated(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.assertEqual(schema.schema_version(connection), 0)
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
            connection.execute("PRAGMA user_version = 1")
        database.persist()

        with self.assertRaisesRegex(schema.SchemaError, "supports up to version 0"):
            schema.prepare_encrypted_database(database, key, auth_path)
        self.assertEqual(list(self.root.glob(".migration-backup-*")), [])

    def test_successful_migration_deletes_encrypted_backup(self):
        database, key, auth_path = self.encrypted_schema_zero()

        def migrate_to_one(connection):
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('migration_test', 'complete')"
            )

        def validate_one(connection):
            schema._validate_version_zero(connection)
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'migration_test'"
            ).fetchone()
            if not row or row[0] != "complete":
                raise schema.SchemaError("Migration test marker is missing.")

        with self.future_schema(migrate_to_one, validate_one):
            changed = schema.prepare_encrypted_database(database, key, auth_path)
            self.assertTrue(changed)
            with database.connection() as connection:
                self.assertEqual(schema.schema_version(connection), 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM settings WHERE key = 'migration_test'"
                    ).fetchone()[0],
                    "complete",
                )

        self.assertEqual(list(self.root.glob(".migration-backup-*")), [])
        self.assertNotIn(b"migration_test", database.path.read_bytes())

    def test_failed_migration_restores_original_and_keeps_backup(self):
        database, key, auth_path = self.encrypted_schema_zero()
        original_vault = database.path.read_bytes()
        original_auth = auth_path.read_bytes()

        def fail_migration(connection):
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('migration_test', 'partial')"
            )
            raise RuntimeError("simulated migration failure")

        with self.future_schema(fail_migration, schema._validate_version_zero):
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
        return database, key, auth_path

    @staticmethod
    def future_schema(migration, validator):
        return _FutureSchema(migration, validator)


class _FutureSchema:
    def __init__(self, migration, validator):
        self.patches = (
            patch.object(schema, "CURRENT_SCHEMA_VERSION", 1),
            patch.dict(schema.MIGRATIONS, {0: migration}, clear=True),
            patch.dict(
                schema.VALIDATORS,
                {0: schema._validate_version_zero, 1: validator},
                clear=True,
            ),
        )

    def __enter__(self):
        for active_patch in self.patches:
            active_patch.start()
        return self

    def __exit__(self, *error):
        for active_patch in reversed(self.patches):
            active_patch.stop()


if __name__ == "__main__":
    unittest.main()
