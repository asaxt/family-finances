from vault import (
    create_encrypted_backup,
    delete_encrypted_backup,
    restore_encrypted_backup,
)


CURRENT_SCHEMA_VERSION = 14
DEFAULT_SAVINGS_GOAL = 1_000_000


class SchemaError(RuntimeError):
    pass


VERSION_ZERO_COLUMNS = {
    "settings": {"key", "value"},
    "connections": {
        "id",
        "plaid_item_id",
        "owner_name",
        "institution",
        "access_token",
        "cursor",
        "transactions_update_status",
        "last_synced_at",
        "created_at",
    },
    "accounts": {
        "id",
        "connection_id",
        "institution",
        "name",
        "mask",
        "type",
        "current_balance",
        "balance_updated_at",
    },
    "transactions": {
        "id",
        "account_id",
        "amount",
        "currency",
        "description",
        "merchant",
        "pending",
        "transacted_at",
        "category",
        "category_override",
        "excluded",
    },
    "budgets": {"month", "category", "amount"},
    "manual_accounts": {
        "id",
        "legacy_key",
        "institution",
        "name",
        "owner_name",
        "classification",
        "goal_eligible",
        "reminder_enabled",
        "archived",
        "created_at",
    },
    "savings_snapshots": {
        "id",
        "manual_account_id",
        "amount",
        "recorded_on",
        "created_at",
    },
}
VERSION_ONE_COLUMNS = {
    **VERSION_ZERO_COLUMNS,
    "accounts": VERSION_ZERO_COLUMNS["accounts"] | {"subtype", "available_balance"},
    "transactions": VERSION_ZERO_COLUMNS["transactions"] | {"cash_flow_override"},
}
VERSION_TWO_COLUMNS = {
    **VERSION_ZERO_COLUMNS,
    "accounts": VERSION_ZERO_COLUMNS["accounts"] | {"subtype", "available_balance"},
    "transactions": VERSION_ZERO_COLUMNS["transactions"] | {"flow_override"},
    "category_rules": {"name", "flow_type"},
}
VERSION_FIVE_COLUMNS = {
    **VERSION_TWO_COLUMNS,
    "merchant_rules": {
        "id",
        "account_id",
        "match_type",
        "match_value",
        "category",
        "flow_type",
    },
}
VERSION_SIX_COLUMNS = {
    name: columns
    for name, columns in VERSION_FIVE_COLUMNS.items()
    if name != "budgets"
}
VERSION_SEVEN_COLUMNS = {
    **VERSION_SIX_COLUMNS,
    "accounts": VERSION_SIX_COLUMNS["accounts"] | {"cash_flow_role"},
}
VERSION_TEN_COLUMNS = {
    **VERSION_SEVEN_COLUMNS,
    "accounts": VERSION_SEVEN_COLUMNS["accounts"] | {"spending_enabled"},
    "transactions": VERSION_SEVEN_COLUMNS["transactions"] | {"spending_override"},
    "merchant_rules": VERSION_SEVEN_COLUMNS["merchant_rules"] | {"spending_override"},
}
EXPECTED_COLUMNS = {
    **VERSION_TEN_COLUMNS,
    "transactions": VERSION_TEN_COLUMNS["transactions"]
    | {"category_override_source"},
}


def schema_version(connection):
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def user_tables(connection):
    return {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }


def database_is_empty(connection):
    return not user_tables(connection)


def _validate_columns(connection, expected_columns, version):
    tables = user_tables(connection)
    expected_tables = set(expected_columns)
    if tables != expected_tables:
        missing = sorted(expected_tables - tables)
        unexpected = sorted(tables - expected_tables)
        details = []
        if missing:
            details.append(f"missing tables: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected tables: {', '.join(unexpected)}")
        raise SchemaError(f"Schema version {version} is not recognized ({'; '.join(details)}).")

    for table, expected in expected_columns.items():
        columns = {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if columns != expected:
            raise SchemaError(f"Schema version {version} has unexpected columns in {table}.")


def _validate_version_zero(connection):
    _validate_columns(connection, VERSION_ZERO_COLUMNS, 0)


def _validate_version_one(connection):
    _validate_columns(connection, VERSION_ONE_COLUMNS, 1)


def _validate_version_two(connection):
    _validate_columns(connection, VERSION_TWO_COLUMNS, 2)


def _validate_version_three(connection):
    _validate_columns(connection, VERSION_TWO_COLUMNS, 3)


def _validate_version_four(connection):
    _validate_columns(connection, VERSION_TWO_COLUMNS, 4)


def _validate_version_five(connection):
    _validate_columns(connection, VERSION_FIVE_COLUMNS, 5)


def _validate_version_six(connection):
    _validate_columns(connection, VERSION_SIX_COLUMNS, 6)


def _validate_version_seven(connection):
    _validate_columns(connection, VERSION_SEVEN_COLUMNS, 7)


def _validate_version_eight(connection):
    _validate_columns(connection, VERSION_TEN_COLUMNS, 8)


def _validate_version_nine(connection):
    _validate_columns(connection, VERSION_TEN_COLUMNS, 9)


def _validate_version_ten(connection):
    _validate_columns(connection, VERSION_TEN_COLUMNS, 10)


def _validate_version_eleven(connection):
    _validate_columns(connection, EXPECTED_COLUMNS, 11)


def _validate_version_twelve(connection):
    _validate_columns(connection, EXPECTED_COLUMNS, 12)


def _migrate_zero_to_one(connection):
    connection.execute("ALTER TABLE accounts ADD COLUMN subtype TEXT")
    connection.execute("ALTER TABLE accounts ADD COLUMN available_balance INTEGER")
    connection.execute(
        """
        ALTER TABLE transactions ADD COLUMN cash_flow_override TEXT
        CHECK (cash_flow_override IN ('income', 'transfer', 'refund', 'ignore'))
        """
    )


def _migrate_one_to_two(connection):
    statements = (
        """
        CREATE TABLE transactions_v2 (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            description TEXT NOT NULL,
            merchant TEXT,
            pending INTEGER NOT NULL,
            transacted_at TEXT NOT NULL,
            category TEXT NOT NULL,
            category_override TEXT,
            flow_override TEXT CHECK (
                flow_override IN (
                    'earned_income', 'other_inflow', 'spending', 'transfer'
                )
            ),
            excluded INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        INSERT INTO transactions_v2 (
            id, account_id, amount, currency, description, merchant, pending,
            transacted_at, category, category_override, flow_override, excluded
        )
        SELECT
            t.id, t.account_id, t.amount, t.currency, t.description, t.merchant,
            t.pending, t.transacted_at, t.category, t.category_override,
            CASE
                WHEN t.cash_flow_override = 'income' THEN 'earned_income'
                WHEN t.cash_flow_override = 'refund' THEN 'other_inflow'
                WHEN t.cash_flow_override = 'transfer' THEN 'transfer'
                WHEN LOWER(COALESCE(t.category_override, t.category)) LIKE 'transfer%'
                    THEN 'transfer'
                WHEN a.type = 'credit'
                     AND LOWER(COALESCE(t.category_override, t.category)) = 'loan payments'
                    THEN 'transfer'
                ELSE NULL
            END,
            CASE
                WHEN t.cash_flow_override = 'ignore' THEN 1
                WHEN t.excluded = 1 AND (
                    LOWER(COALESCE(t.category_override, t.category)) LIKE 'transfer%'
                    OR LOWER(COALESCE(t.category_override, t.category)) = 'loan payments'
                    OR LOWER(COALESCE(t.category_override, t.category)) = 'loan disbursements'
                ) THEN 0
                ELSE t.excluded
            END
        FROM transactions t
        LEFT JOIN accounts a ON a.id = t.account_id
        """,
        "DROP TABLE transactions",
        "ALTER TABLE transactions_v2 RENAME TO transactions",
        """
        CREATE TABLE category_rules (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            flow_type TEXT NOT NULL CHECK (
                flow_type IN (
                    'earned_income', 'other_inflow', 'spending', 'transfer'
                )
            )
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _normalize_venmo_transactions(connection):
    connection.execute(
        """
        UPDATE transactions
        SET category_override = 'Venmo', flow_override = NULL
        WHERE (
              LOWER(COALESCE(merchant, '')) LIKE '%venmo%'
              OR LOWER(description) LIKE '%venmo%'
          )
        """
    )
    connection.execute(
        "DELETE FROM category_rules WHERE name = 'Venmo' COLLATE NOCASE"
    )


def _migrate_two_to_three(connection):
    _normalize_venmo_transactions(connection)


def _migrate_three_to_four(connection):
    _normalize_venmo_transactions(connection)


def _migrate_four_to_five(connection):
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
            UNIQUE (account_id, match_type, match_value),
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """
    )


def _migrate_five_to_six(connection):
    connection.execute("DROP TABLE budgets")


def _migrate_six_to_seven(connection):
    connection.execute(
        """
        ALTER TABLE accounts ADD COLUMN cash_flow_role TEXT NOT NULL
        DEFAULT 'other'
        CHECK (cash_flow_role IN ('cash_flow', 'credit_card', 'other'))
        """
    )
    connection.execute(
        """
        UPDATE accounts
        SET cash_flow_role = CASE
            WHEN type = 'depository' THEN 'cash_flow'
            WHEN type = 'credit' THEN 'credit_card'
            ELSE 'other'
        END
        """
    )


def _migrate_seven_to_eight(connection):
    connection.execute(
        """
        ALTER TABLE accounts ADD COLUMN spending_enabled INTEGER NOT NULL
        DEFAULT 0 CHECK (spending_enabled IN (0, 1))
        """
    )
    connection.execute(
        "UPDATE accounts SET spending_enabled = 1 WHERE cash_flow_role = 'credit_card'"
    )
    connection.execute(
        """
        ALTER TABLE transactions ADD COLUMN spending_override TEXT
        CHECK (spending_override IN ('include', 'exclude'))
        """
    )
    connection.execute(
        """
        ALTER TABLE merchant_rules ADD COLUMN spending_override TEXT
        CHECK (spending_override IN ('include', 'exclude'))
        """
    )


def _migrate_eight_to_nine(connection):
    connection.execute(
        """
        CREATE TABLE merchant_rules_v9 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            match_type TEXT NOT NULL CHECK (
                match_type IN ('merchant', 'description')
            ),
            match_value TEXT NOT NULL COLLATE NOCASE,
            category TEXT NOT NULL,
            flow_type TEXT CHECK (
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
        INSERT INTO merchant_rules_v9 (
            id, account_id, match_type, match_value, category, flow_type,
            spending_override
        )
        SELECT id, account_id, match_type, match_value, category, flow_type,
               spending_override
        FROM merchant_rules
        """
    )
    connection.execute("DROP TABLE merchant_rules")
    connection.execute("ALTER TABLE merchant_rules_v9 RENAME TO merchant_rules")


def _migrate_nine_to_ten(connection):
    connection.execute(
        """
        UPDATE transactions SET category = 'Transfer'
        WHERE category COLLATE NOCASE IN ('Transfer In', 'Transfer Out')
        """
    )
    connection.execute(
        """
        UPDATE transactions SET category_override = 'Transfer'
        WHERE category_override COLLATE NOCASE IN ('Transfer In', 'Transfer Out')
        """
    )
    connection.execute(
        """
        UPDATE merchant_rules SET category = 'Transfer'
        WHERE category COLLATE NOCASE IN ('Transfer In', 'Transfer Out')
        """
    )
    connection.execute(
        """
        DELETE FROM category_rules
        WHERE name COLLATE NOCASE IN ('Transfer In', 'Transfer Out')
        """
    )
    connection.execute(
        """
        INSERT INTO category_rules (name, flow_type) VALUES ('Transfer', 'transfer')
        ON CONFLICT(name) DO UPDATE SET flow_type = excluded.flow_type
        """
    )
    connection.executemany(
        """
        INSERT INTO category_rules (name, flow_type) VALUES (?, ?)
        ON CONFLICT(name) DO NOTHING
        """,
        (
            ("Income", "earned_income"),
            ("Loan Disbursements", "other_inflow"),
            ("Reimbursed Work Travel", "other_inflow"),
        ),
    )


def _migrate_ten_to_eleven(connection):
    connection.execute(
        """
        ALTER TABLE transactions ADD COLUMN category_override_source TEXT
        CHECK (category_override_source IN ('user', 'model'))
        """
    )
    connection.execute(
        """
        UPDATE transactions SET category_override_source = 'model'
        WHERE category_override IS NOT NULL
        """
    )


def _migrate_eleven_to_twelve(connection):
    connection.execute(
        """
        UPDATE transactions
        SET category = 'Uncategorized',
            category_override = NULL,
            category_override_source = NULL,
            flow_override = NULL,
            spending_override = NULL,
            excluded = 0
        """
    )
    connection.execute("DELETE FROM merchant_rules")
    connection.execute(
        """
        UPDATE accounts
        SET cash_flow_role = CASE
                WHEN type IN ('depository', 'credit') THEN 'cash_flow'
                ELSE 'other'
            END,
            spending_enabled = CASE
                WHEN type IN ('depository', 'credit') THEN 1
                ELSE 0
            END
        """
    )
    connection.execute(
        """
        DELETE FROM settings
        WHERE key IN ('development_ollama_result_v1', 'local_ai_result_v1')
        """
    )


def _migrate_twelve_to_thirteen(connection):
    # Keep the existing storage key for compatibility; both treatments are Money in.
    connection.execute(
        "UPDATE category_rules SET flow_type = 'earned_income' "
        "WHERE flow_type = 'other_inflow'"
    )


def _migrate_thirteen_to_fourteen(connection):
    statements = (
        """
        CREATE TABLE merchant_rules_v14 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            match_type TEXT NOT NULL CHECK (
                match_type IN ('merchant', 'description', 'description_contains')
            ),
            match_value TEXT NOT NULL COLLATE NOCASE,
            category TEXT NOT NULL,
            flow_type TEXT CHECK (
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
        """,
        """
        INSERT INTO merchant_rules_v14 (
            id, account_id, match_type, match_value, category, flow_type,
            spending_override
        )
        SELECT id, account_id, match_type, match_value, category, flow_type,
               spending_override
        FROM merchant_rules
        """,
        "DROP TABLE merchant_rules",
        "ALTER TABLE merchant_rules_v14 RENAME TO merchant_rules",
    )
    for statement in statements:
        connection.execute(statement)


VALIDATORS = {
    0: _validate_version_zero,
    1: _validate_version_one,
    2: _validate_version_two,
    3: _validate_version_three,
    4: _validate_version_four,
    5: _validate_version_five,
    6: _validate_version_six,
    7: _validate_version_seven,
    8: _validate_version_eight,
    9: _validate_version_nine,
    10: _validate_version_ten,
    11: _validate_version_eleven,
    12: _validate_version_twelve,
    13: _validate_version_twelve,
    14: _validate_version_twelve,
}
MIGRATIONS = {
    0: _migrate_zero_to_one,
    1: _migrate_one_to_two,
    2: _migrate_two_to_three,
    3: _migrate_three_to_four,
    4: _migrate_four_to_five,
    5: _migrate_five_to_six,
    6: _migrate_six_to_seven,
    7: _migrate_seven_to_eight,
    8: _migrate_eight_to_nine,
    9: _migrate_nine_to_ten,
    10: _migrate_ten_to_eleven,
    11: _migrate_eleven_to_twelve,
    12: _migrate_twelve_to_thirteen,
    13: _migrate_thirteen_to_fourteen,
}


def validate_schema(connection, version=None):
    version = schema_version(connection) if version is None else version
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaError(
            f"This database uses schema version {version}, but this app supports "
            f"up to version {CURRENT_SCHEMA_VERSION}."
        )
    validator = VALIDATORS.get(version)
    if validator is None:
        raise SchemaError(f"Schema version {version} is not supported.")

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SchemaError(f"Database integrity check failed: {integrity}")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise SchemaError("Database foreign-key validation failed.")
    validator(connection)


def create_schema(connection):
    if not database_is_empty(connection):
        raise SchemaError("A new schema can only be created in an empty database.")
    try:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;

            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plaid_item_id TEXT UNIQUE,
                owner_name TEXT NOT NULL,
                institution TEXT NOT NULL,
                access_token TEXT NOT NULL,
                cursor TEXT NOT NULL DEFAULT '',
                transactions_update_status TEXT,
                last_synced_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY,
                connection_id INTEGER,
                institution TEXT NOT NULL,
                name TEXT NOT NULL,
                mask TEXT,
                type TEXT NOT NULL,
                subtype TEXT,
                current_balance INTEGER,
                available_balance INTEGER,
                balance_updated_at TEXT,
                cash_flow_role TEXT NOT NULL DEFAULT 'other' CHECK (
                    cash_flow_role IN ('cash_flow', 'credit_card', 'other')
                ),
                spending_enabled INTEGER NOT NULL DEFAULT 0 CHECK (
                    spending_enabled IN (0, 1)
                ),
                FOREIGN KEY (connection_id) REFERENCES connections(id)
            );
            CREATE TABLE transactions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                description TEXT NOT NULL,
                merchant TEXT,
                pending INTEGER NOT NULL,
                transacted_at TEXT NOT NULL,
                category TEXT NOT NULL,
                category_override TEXT,
                category_override_source TEXT CHECK (
                    category_override_source IN ('user', 'model')
                ),
                flow_override TEXT CHECK (
                    flow_override IN (
                        'earned_income', 'other_inflow', 'spending', 'transfer'
                    )
                ),
                spending_override TEXT CHECK (
                    spending_override IN ('include', 'exclude')
                ),
                excluded INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE category_rules (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                flow_type TEXT NOT NULL CHECK (
                    flow_type IN (
                        'earned_income', 'other_inflow', 'spending', 'transfer'
                    )
                )
            );
            CREATE TABLE merchant_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                match_type TEXT NOT NULL CHECK (
                    match_type IN (
                        'merchant', 'description', 'description_contains'
                    )
                ),
                match_value TEXT NOT NULL COLLATE NOCASE,
                category TEXT NOT NULL,
                flow_type TEXT CHECK (
                    flow_type IN (
                        'earned_income', 'other_inflow', 'spending', 'transfer'
                    )
                ),
                spending_override TEXT CHECK (
                    spending_override IN ('include', 'exclude')
                ),
                UNIQUE (account_id, match_type, match_value),
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE TABLE manual_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                legacy_key TEXT UNIQUE,
                institution TEXT NOT NULL,
                name TEXT NOT NULL,
                owner_name TEXT NOT NULL DEFAULT 'Household',
                classification TEXT NOT NULL CHECK (
                    classification IN ('pre_tax', 'post_tax', 'taxable')
                ),
                goal_eligible INTEGER NOT NULL DEFAULT 0,
                reminder_enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE savings_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manual_account_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                recorded_on TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (manual_account_id, recorded_on),
                FOREIGN KEY (manual_account_id) REFERENCES manual_accounts(id)
            );
            INSERT INTO settings (key, value)
            VALUES ('savings_goal_cents', '{DEFAULT_SAVINGS_GOAL}');
            INSERT INTO category_rules (name, flow_type) VALUES
                ('Income', 'earned_income'),
                ('Loan Disbursements', 'earned_income'),
                ('Reimbursed Work Travel', 'earned_income'),
                ('Transfer', 'transfer');
            PRAGMA user_version = {CURRENT_SCHEMA_VERSION};

            COMMIT;
            """
        )
    except Exception:
        connection.rollback()
        raise
    validate_schema(connection)


def migration_required(connection):
    version = schema_version(connection)
    validate_schema(connection, version)
    return version < CURRENT_SCHEMA_VERSION


def migrate_schema(connection):
    version = schema_version(connection)
    validate_schema(connection, version)
    if version == CURRENT_SCHEMA_VERSION:
        return False

    try:
        connection.execute("BEGIN IMMEDIATE")
        while version < CURRENT_SCHEMA_VERSION:
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise SchemaError(
                    f"No migration exists from schema version {version} to {version + 1}."
                )
            migration(connection)
            version += 1
            connection.execute(f"PRAGMA user_version = {version}")
            validate_schema(connection, version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    validate_schema(connection)
    return True


def prepare_encrypted_database(database, data_key, auth_path):
    with database.connection() as connection:
        if database_is_empty(connection):
            create_schema(connection)
            needs_migration = False
            created = True
        else:
            needs_migration = migration_required(connection)
            created = False

    if created:
        database.persist()
        return False
    if not needs_migration:
        return False

    backup_dir = create_encrypted_backup(
        database.path,
        auth_path,
        prefix=".migration-backup-",
    )
    try:
        with database.connection() as connection:
            migrate_schema(connection)
        database.persist()
        database.lock()
        database.unlock(data_key)
        with database.connection() as connection:
            validate_schema(connection)
    except Exception:
        database.lock()
        restore_encrypted_backup(backup_dir, database.path, auth_path)
        database.unlock(data_key)
        raise
    else:
        delete_encrypted_backup(backup_dir)
        return True
