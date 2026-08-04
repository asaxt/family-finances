from vault import (
    create_encrypted_backup,
    delete_encrypted_backup,
    restore_encrypted_backup,
)


CURRENT_SCHEMA_VERSION = 0
DEFAULT_SAVINGS_GOAL = 1_000_000


class SchemaError(RuntimeError):
    pass


EXPECTED_COLUMNS = {
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


def _validate_version_zero(connection):
    tables = user_tables(connection)
    expected_tables = set(EXPECTED_COLUMNS)
    if tables != expected_tables:
        missing = sorted(expected_tables - tables)
        unexpected = sorted(tables - expected_tables)
        details = []
        if missing:
            details.append(f"missing tables: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected tables: {', '.join(unexpected)}")
        raise SchemaError(f"Schema version 0 is not recognized ({'; '.join(details)}).")

    for table, expected in EXPECTED_COLUMNS.items():
        columns = {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if columns != expected:
            raise SchemaError(f"Schema version 0 has unexpected columns in {table}.")


VALIDATORS = {0: _validate_version_zero}
MIGRATIONS = {}


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
                current_balance INTEGER,
                balance_updated_at TEXT,
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
                excluded INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE budgets (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL,
                PRIMARY KEY (month, category)
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
