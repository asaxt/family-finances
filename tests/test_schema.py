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

    def test_new_schema_is_current_and_strictly_validated(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.assertEqual(schema.schema_version(connection), 16)
            self.assertNotIn("budgets", schema.user_tables(connection))
            schema.validate_schema(connection)
            goal = connection.execute(
                "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
            ).fetchone()[0]
            self.assertEqual(goal, "1000000")
            with self.assertRaisesRegex(schema.SchemaError, "empty database"):
                schema.create_schema(connection)
            self.remove_version_sixteen(connection)
        finally:
            connection.close()

    def test_money_in_migration_preserves_classifications_and_is_idempotent(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.remove_version_sixteen(connection)
            connection.execute("UPDATE category_rules SET flow_type = 'other_inflow' WHERE name = 'Loan Disbursements'")
            connection.execute("INSERT INTO connections (id, owner_name, institution, access_token) VALUES (1, 'Example', 'Example', 'test')")
            connection.execute("INSERT INTO accounts (id, connection_id, institution, name, type) VALUES ('test', 1, 'Example', 'Example', 'depository')")
            connection.execute("INSERT INTO transactions (id, account_id, amount, currency, description, pending, transacted_at, category, category_override, category_override_source) VALUES ('test', 'test', -100, 'USD', 'Example', 0, '2026-08-01', 'Income', 'Loan Disbursements', 'user')")
            columns = [row[1] for row in connection.execute("PRAGMA table_info(transactions)") if row[1] != "merchant"]
            before = connection.execute("SELECT " + ",".join(columns) + " FROM transactions").fetchall()
            connection.execute(
                "ALTER TABLE merchant_rules DROP COLUMN applies_all_accounts"
            )
            connection.execute("PRAGMA user_version = 12")
            connection.commit()
            self.assertTrue(schema.migrate_schema(connection))
            self.assertEqual(connection.execute("SELECT flow_type FROM category_rules WHERE name = 'Loan Disbursements'").fetchone()[0], 'earned_income')
            self.assertEqual(connection.execute("SELECT " + ",".join(columns) + " FROM transactions").fetchall(), before)
            self.assertFalse(schema.migrate_schema(connection))
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
            connection.execute("PRAGMA user_version = 17")
        database.persist()

        with self.assertRaisesRegex(schema.SchemaError, "supports up to version 16"):
            schema.prepare_encrypted_database(database, key, auth_path)
        self.assertEqual(list(self.root.glob(".migration-backup-*")), [])

    def test_version_nine_preserves_rules_and_allows_category_only_rules(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.remove_version_sixteen(connection)
            connection.execute(
                """
                INSERT INTO connections (
                    id, plaid_item_id, owner_name, institution, access_token,
                    cursor, transactions_update_status, last_synced_at
                ) VALUES (
                    1, 'item-1', 'Household', 'Example Bank', 'test-token',
                    'cursor-1', 'HISTORICAL_UPDATE_COMPLETE', '2026-08-31T12:00:00'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type,
                    cash_flow_role, spending_enabled
                ) VALUES (
                    'checking', 1, 'Example Bank', 'Checking', 'depository',
                    'cash_flow', 1
                )
                """
            )
            connection.execute("DROP TABLE merchant_rules")
            connection.execute(
                """
                CREATE TABLE merchant_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    match_type TEXT NOT NULL CHECK (
                        match_type IN ('merchant', 'description')
                    ),
                    match_value TEXT NOT NULL COLLATE NOCASE,
                    category TEXT NOT NULL,
                    flow_type TEXT NOT NULL CHECK (
                        flow_type IN (
                            'earned_income', 'other_inflow', 'spending', 'transfer'
                        )
                    ),
                    spending_override TEXT CHECK (
                        spending_override IN ('include', 'exclude')
                    ),
                    UNIQUE (account_id, match_type, match_value),
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category, flow_type
                ) VALUES ('checking', 'merchant', 'Existing', 'Transfers', 'transfer')
                """
            )
            self.drop_version_eleven_column(connection)
            connection.execute("PRAGMA user_version = 8")
            connection.commit()

            self.assertTrue(schema.migrate_schema(connection))
            existing = connection.execute(
                "SELECT category, flow_type FROM merchant_rules WHERE match_value = 'Existing'"
            ).fetchone()
            self.assertIsNone(existing)
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category, flow_type
                ) VALUES ('checking', 'merchant', 'Example', 'Groceries', NULL)
                """
            )
            category_only_flow = connection.execute(
                "SELECT flow_type FROM merchant_rules WHERE match_value = 'Example'"
            ).fetchone()[0]
            self.assertIsNone(category_only_flow)
            self.assertEqual(schema.schema_version(connection), 16)
            schema.validate_schema(connection)
        finally:
            connection.close()

    def test_version_ten_combines_transfer_labels_and_seeds_mappings(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.remove_version_sixteen(connection)
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, pending,
                    transacted_at, category, category_override
                ) VALUES (?, 'checking', ?, 'USD', ?, 0, '2026-08-01', ?, ?)
                """,
                (
                    ("out", 100, "Out", "Transfer Out", None),
                    ("in", -100, "In", "Other", "Transfer In"),
                ),
            )
            connection.execute(
                "DELETE FROM category_rules WHERE name = 'Transfer' COLLATE NOCASE"
            )
            connection.execute(
                "INSERT INTO category_rules VALUES ('Transfer Out', 'spending')"
            )
            connection.execute(
                "ALTER TABLE merchant_rules DROP COLUMN applies_all_accounts"
            )
            self.drop_version_eleven_column(connection)
            connection.execute("PRAGMA user_version = 9")
            connection.commit()

            self.assertTrue(schema.migrate_schema(connection))

            categories = connection.execute(
                "SELECT category, category_override FROM transactions ORDER BY id"
            ).fetchall()
            mappings = dict(connection.execute("SELECT name, flow_type FROM category_rules"))
            self.assertEqual(
                categories,
                [("Uncategorized", None), ("Uncategorized", None)],
            )
            self.assertEqual(mappings["Income"], "earned_income")
            self.assertEqual(mappings["Loan Disbursements"], "earned_income")
            self.assertEqual(mappings["Reimbursed Work Travel"], "earned_income")
            self.assertEqual(mappings["Transfer"], "transfer")
            self.assertNotIn("Transfer Out", mappings)
        finally:
            connection.close()

    def test_version_twelve_resets_existing_categorization(self):
        connection = sqlite3.connect(":memory:")
        try:
            schema.create_schema(connection)
            self.remove_version_sixteen(connection)
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                UPDATE connections
                SET plaid_item_id = 'item-1', cursor = 'cursor-1',
                    transactions_update_status = 'HISTORICAL_UPDATE_COMPLETE',
                    last_synced_at = '2026-08-31T12:00:00'
                WHERE id = 1
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.execute(
                """
                UPDATE accounts
                SET cash_flow_role = 'credit_card', spending_enabled = 0
                WHERE id = 'checking'
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, pending,
                    transacted_at, category, category_override
                ) VALUES (
                    'older', 'checking', 1000, 'USD', 'SAMPLE CAFE', 0,
                    '2024-01-01', 'Food And Drink', 'Groceries'
                )
                """
            )
            connection.execute(
                """
                UPDATE transactions
                SET flow_override = 'spending', spending_override = 'include',
                    excluded = 1
                WHERE id = 'older'
                """
            )
            connection.execute(
                "INSERT INTO category_rules VALUES ('Groceries', 'spending')"
            )
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category
                ) VALUES ('checking', 'description', 'SAMPLE CAFE', 'Groceries')
                """
            )
            connection.execute(
                """
                INSERT INTO manual_accounts (
                    id, institution, name, owner_name, classification,
                    goal_eligible, reminder_enabled
                ) VALUES (
                    1, 'Retirement Provider', 'IRA', 'Household', 'post_tax', 1, 1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO savings_snapshots (manual_account_id, amount, recorded_on)
                VALUES (1, 123456, '2026-08-31')
                """
            )
            connection.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (
                    ("overview_lookback_days", "45"),
                    ("local_ai_enabled_v1", "1"),
                    ("local_ai_result_v1", "saved-result"),
                ),
            )
            connection.execute(
                "ALTER TABLE merchant_rules DROP COLUMN applies_all_accounts"
            )
            self.drop_version_eleven_column(connection)
            connection.execute("PRAGMA user_version = 10")
            connection.commit()

            self.assertTrue(schema.migrate_schema(connection))

            transaction = connection.execute(
                """
                SELECT category, category_override, category_override_source,
                       flow_override, spending_override, excluded
                FROM transactions
                WHERE id = 'older'
                """
            ).fetchone()
            self.assertEqual(
                tuple(transaction),
                ("Uncategorized", None, None, None, None, 0),
            )
            plaid_connection = connection.execute(
                """
                SELECT plaid_item_id, access_token, cursor,
                       transactions_update_status, last_synced_at
                FROM connections WHERE id = 1
                """
            ).fetchone()
            self.assertEqual(
                tuple(plaid_connection),
                (
                    "item-1",
                    "test-token",
                    "cursor-1",
                    "HISTORICAL_UPDATE_COMPLETE",
                    "2026-08-31T12:00:00",
                ),
            )
            account = connection.execute(
                """
                SELECT cash_flow_role, spending_enabled
                FROM accounts WHERE id = 'checking'
                """
            ).fetchone()
            self.assertEqual(tuple(account), ("cash_flow", 1))
            savings = connection.execute(
                """
                SELECT a.name, a.classification, a.goal_eligible,
                       s.amount, s.recorded_on
                FROM manual_accounts a
                JOIN savings_snapshots s ON s.manual_account_id = a.id
                """
            ).fetchone()
            self.assertEqual(
                tuple(savings),
                ("IRA", "post_tax", 1, 123456, "2026-08-31"),
            )
            settings = dict(connection.execute("SELECT key, value FROM settings"))
            self.assertEqual(settings["savings_goal_cents"], "1000000")
            self.assertEqual(settings["overview_lookback_days"], "45")
            self.assertEqual(settings["local_ai_enabled_v1"], "1")
            self.assertNotIn("local_ai_result_v1", settings)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT flow_type FROM category_rules WHERE name = 'Groceries'"
                ).fetchone()[0],
                "spending",
            )
            self.assertEqual(schema.schema_version(connection), 16)
            schema.validate_schema(connection)
        finally:
            connection.close()

    def test_successful_migration_retains_encrypted_backup(self):
        database, key, auth_path = self.encrypted_schema_zero()
        changed = schema.prepare_encrypted_database(database, key, auth_path)
        self.assertTrue(changed)
        with database.connection() as connection:
            self.assertEqual(schema.schema_version(connection), 16)
            account_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(accounts)")
            }
            transaction_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transactions)")
            }
        self.assertIn("available_balance", account_columns)
        self.assertIn("subtype", account_columns)
        self.assertIn("flow_override", transaction_columns)

        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 1)
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
        self.assertEqual(rows["income"], (None, 0))
        self.assertEqual(rows["refund"], (None, 0))
        self.assertEqual(rows["payment"], (None, 0))
        self.assertEqual(rows["ignored"], (None, 0))

    def test_version_two_normalizes_venmo_categories_and_treatments(self):
        database, key, auth_path = self.encrypted_schema_zero()
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.remove_version_sixteen(connection)
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, flow_override
                ) VALUES (?, 'checking', ?, 'USD', ?, ?, 0, '2026-08-01', ?, ?)
                """,
                (
                    ("venmo-in", -100, "Payment", "Venmo", "Transfer In", "transfer"),
                    ("venmo-out", 200, "VENMO payment", None, "Transfer Out", "transfer"),
                    ("bank-transfer", 300, "Transfer", "Example Bank", "Transfer Out", "transfer"),
                    ("reviewed", -400, "Venmo payment", None, "Transfer In", "other_inflow"),
                ),
            )
            connection.execute(
                "INSERT INTO category_rules (name, flow_type) VALUES ('Venmo', 'earned_income')"
            )
            self.create_legacy_budget_table(connection)
            connection.execute("DROP TABLE merchant_rules")
            self.drop_version_eight_columns(connection, merchant_rules=False)
            connection.execute("ALTER TABLE accounts DROP COLUMN cash_flow_role")
            connection.execute("PRAGMA user_version = 2")
            schema._validate_version_two(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            rows = {
                row[0]: tuple(row[1:])
                for row in connection.execute(
                    "SELECT id, category_override, flow_override FROM transactions ORDER BY id"
                )
            }
            venmo_rule = connection.execute(
                "SELECT flow_type FROM category_rules WHERE name = 'Venmo' COLLATE NOCASE"
            ).fetchone()
            self.assertEqual(schema.schema_version(connection), 16)
        self.assertEqual(rows["venmo-in"], (None, None))
        self.assertEqual(rows["venmo-out"], (None, None))
        self.assertEqual(rows["bank-transfer"], (None, None))
        self.assertEqual(rows["reviewed"], (None, None))
        self.assertIsNone(venmo_rule)
        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 2)

    def test_version_three_reapplies_venmo_normalization(self):
        database, key, auth_path = self.encrypted_schema_zero()
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.remove_version_sixteen(connection)
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.execute(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES ('checking', 1, 'Example Bank', 'Checking', 'depository')
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    id, account_id, amount, currency, description, merchant,
                    pending, transacted_at, category, flow_override
                ) VALUES (
                    'venmo', 'checking', -100, 'USD', 'Payment', 'Venmo',
                    0, '2026-08-01', 'Venmo', 'earned_income'
                )
                """
            )
            connection.execute(
                "INSERT INTO category_rules (name, flow_type) VALUES ('Venmo', 'earned_income')"
            )
            self.create_legacy_budget_table(connection)
            connection.execute("DROP TABLE merchant_rules")
            self.drop_version_eight_columns(connection, merchant_rules=False)
            connection.execute("ALTER TABLE accounts DROP COLUMN cash_flow_role")
            connection.execute("PRAGMA user_version = 3")
            schema._validate_version_three(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            transaction = connection.execute(
                "SELECT category_override, flow_override FROM transactions WHERE id = 'venmo'"
            ).fetchone()
            rule = connection.execute(
                "SELECT flow_type FROM category_rules WHERE name = 'Venmo' COLLATE NOCASE"
            ).fetchone()
            self.assertEqual(schema.schema_version(connection), 16)
        self.assertEqual(tuple(transaction), (None, None))
        self.assertIsNone(rule)
        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 2)

    def test_version_four_adds_recurring_merchant_rules(self):
        database, key, auth_path = self.encrypted_schema_zero()
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.remove_version_sixteen(connection)
            self.create_legacy_budget_table(connection)
            connection.execute("DROP TABLE merchant_rules")
            self.drop_version_eight_columns(connection, merchant_rules=False)
            connection.execute("ALTER TABLE accounts DROP COLUMN cash_flow_role")
            connection.execute("PRAGMA user_version = 4")
            schema._validate_version_four(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(merchant_rules)")
            }
            self.assertEqual(schema.schema_version(connection), 16)
        self.assertEqual(columns, schema.EXPECTED_COLUMNS["merchant_rules"])
        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 2)

    def test_version_five_removes_budget_table(self):
        database, key, auth_path = self.encrypted_schema_zero()
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.remove_version_sixteen(connection)
            self.create_legacy_budget_table(connection)
            self.drop_version_eight_columns(connection)
            connection.execute("ALTER TABLE accounts DROP COLUMN cash_flow_role")
            connection.execute("PRAGMA user_version = 5")
            schema._validate_version_five(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.assertEqual(schema.schema_version(connection), 16)
            self.assertNotIn("budgets", schema.user_tables(connection))
        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 2)

    def test_version_six_assigns_default_account_roles(self):
        database, key, auth_path = self.encrypted_schema_zero()
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.remove_version_sixteen(connection)
            self.drop_version_eight_columns(connection)
            connection.execute("ALTER TABLE accounts DROP COLUMN cash_flow_role")
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.executemany(
                """
                INSERT INTO accounts (id, connection_id, institution, name, type)
                VALUES (?, 1, 'Example Bank', ?, ?)
                """,
                (
                    ("checking", "Checking", "depository"),
                    ("card", "Card", "credit"),
                    ("investment", "Investment", "investment"),
                ),
            )
            connection.execute("PRAGMA user_version = 6")
            schema._validate_version_six(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            roles = dict(
                connection.execute(
                    "SELECT id, cash_flow_role FROM accounts ORDER BY id"
                ).fetchall()
            )
            self.assertEqual(schema.schema_version(connection), 16)
        self.assertEqual(
            roles,
            {
                "card": "cash_flow",
                "checking": "cash_flow",
                "investment": "other",
            },
        )
        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 2)

    def test_version_seven_enables_spending_for_credit_cards(self):
        database, key, auth_path = self.encrypted_schema_zero()
        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            self.remove_version_sixteen(connection)
            self.drop_version_eight_columns(connection)
            connection.execute(
                """
                INSERT INTO connections (id, owner_name, institution, access_token)
                VALUES (1, 'Household', 'Example Bank', 'test-token')
                """
            )
            connection.executemany(
                """
                INSERT INTO accounts (
                    id, connection_id, institution, name, type, cash_flow_role
                ) VALUES (?, 1, 'Example Bank', ?, ?, ?)
                """,
                (
                    ("checking", "Checking", "depository", "cash_flow"),
                    ("card", "Card", "credit", "credit_card"),
                ),
            )
            connection.execute("PRAGMA user_version = 7")
            schema._validate_version_seven(connection)
        database.persist()

        schema.prepare_encrypted_database(database, key, auth_path)
        with database.connection() as connection:
            settings = {
                row[0]: tuple(row[1:])
                for row in connection.execute(
                    "SELECT id, cash_flow_role, spending_enabled FROM accounts"
                )
            }
            self.assertEqual(schema.schema_version(connection), 16)
        self.assertEqual(settings["checking"], ("cash_flow", 1))
        self.assertEqual(settings["card"], ("cash_flow", 1))
        self.assertEqual(len(list(self.root.glob(".migration-backup-*"))), 2)

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
            self.remove_version_sixteen(connection)
            connection.execute("DROP TABLE merchant_rules")
            connection.execute("DROP TABLE category_rules")
            connection.execute("ALTER TABLE transactions DROP COLUMN flow_override")
            self.drop_version_eight_columns(connection, merchant_rules=False)
            connection.execute("ALTER TABLE accounts DROP COLUMN cash_flow_role")
            connection.execute("ALTER TABLE accounts DROP COLUMN available_balance")
            connection.execute("ALTER TABLE accounts DROP COLUMN subtype")
            self.create_legacy_budget_table(connection)
            connection.execute("PRAGMA user_version = 0")
            schema._validate_version_zero(connection)
        database.persist()
        return database, key, auth_path

    @staticmethod
    def create_legacy_budget_table(connection):
        connection.execute(
            """
            CREATE TABLE budgets (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL,
                PRIMARY KEY (month, category)
            )
            """
        )

    @staticmethod
    def drop_version_eight_columns(connection, merchant_rules=True):
        SchemaTests.drop_version_eleven_column(connection)
        connection.execute("ALTER TABLE accounts DROP COLUMN spending_enabled")
        connection.execute("ALTER TABLE transactions DROP COLUMN spending_override")
        if merchant_rules:
            connection.execute(
                "ALTER TABLE merchant_rules DROP COLUMN applies_all_accounts"
            )
            connection.execute(
                "ALTER TABLE merchant_rules DROP COLUMN spending_override"
            )

    @staticmethod
    def drop_version_eleven_column(connection):
        connection.execute(
            "ALTER TABLE transactions DROP COLUMN category_override_source"
        )

    @staticmethod
    def remove_version_sixteen(connection):
        connection.execute('DROP TRIGGER complete_transaction_text_insert')
        connection.execute('DROP TRIGGER complete_transaction_text_update')
        connection.execute('DROP TABLE rule_fallbacks')
        connection.execute('ALTER TABLE transactions DROP COLUMN merchant_source')
        connection.execute('ALTER TABLE transactions DROP COLUMN description_source')
        connection.execute('ALTER TABLE merchant_rules DROP COLUMN source')
        connection.execute('PRAGMA user_version = 15')


if __name__ == "__main__":
    unittest.main()
