import os
import io
import base64
import hashlib
import json
import re
import calendar
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import Flask, Request, Response, abort, jsonify, redirect, render_template, request, session, url_for
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.country_code import CountryCode
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from werkzeug.security import check_password_hash, generate_password_hash

import statement_import as statements
from statement_classification import eligible_import_ids, match_import_transfers

from schema import SchemaError, prepare_encrypted_database
from vault import (
    EncryptedDatabase,
    VaultError,
    create_encrypted_backup,
    create_key_record,
    delete_encrypted_backup,
    restore_encrypted_backup,
    unlock_key,
)

from analytics import (
    CATEGORY_RULE_JOIN,
    EFFECTIVE_CATEGORY_SQL,
    DEFAULT_OVERVIEW_LOOKBACK_DAYS,
    MAX_OVERVIEW_LOOKBACK_DAYS,
    category_details,
    cash_flow_summary,
    long_term_trends,
    month_label,
    rolling_spending_summary,
    spending_summary,
    transaction_list,
)
from llm_evaluation import (
    AI_REVIEW_SETTING,
    apply_categorized_suggestions,
    classify_evaluation_rows,
    create_recurring_category_rules,
    evaluation_batches,
    evaluation_result,
    load_ai_reviews,
    save_ai_reviews,
    dismiss_ai_reviews,
    prepare_evaluation,
)


ROOT = Path(__file__).parent


def environment_flag(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def local_port():
    try:
        port = int(os.environ.get("FAMILY_FINANCES_PORT", "4242"))
    except ValueError as error:
        raise RuntimeError("FAMILY_FINANCES_PORT must be a number.") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("FAMILY_FINANCES_PORT must be between 1 and 65535.")
    return port


APP_MODE = os.environ.get("FAMILY_FINANCES_MODE", "stable").strip().lower()
DEVELOPMENT_MODE = APP_MODE == "development"
PLAID_DISABLED = DEVELOPMENT_MODE or environment_flag("FAMILY_FINANCES_DISABLE_PLAID")
APP_PORT = local_port()
DATA_ROOT = Path(
    os.environ.get("FAMILY_FINANCES_DATA_DIR")
    or ROOT
)
VAULT_PATH = DATA_ROOT / "family-finances.vault"
AUTH_PATH = DATA_ROOT / ".auth.json"
DEFAULT_APP_NAME = "Family Finances"
VAULT_IDLE_SECONDS = 12 * 60 * 60
SAVINGS_CLASSIFICATIONS = {
    "pre_tax": "Pre-tax",
    "post_tax": "Post-tax",
    "taxable": "Taxable",
}
EXPECTED_PLAID_PRODUCTS = {"transactions"}
DEVELOPMENT_CLASSIFICATION_MODE = "category_mapping_v1"
DEVELOPMENT_RESET_MARKER = "development_blank_slate_v3"
LOCAL_AI_SETTING = "local_ai_enabled_v1"
OLLAMA_RESULT_SETTING = "local_ai_result_v1"
OLLAMA_EVALUATION_LOCK = threading.Lock()
CATEGORY_SETUP_SETTING = "category_setup_completed_v1"
CATEGORY_SUGGESTIONS = (
    "Grocery",
    "Eating Out",
    "Travel",
    "Pets",
    "Clothes",
    "Gifts",
    "Charity",
    "Skiing",
    "Sailing",
    "Healthcare",
    "Rent/Mortgage/Utilities",
    "Transportation",
)
FLOW_TYPES = {
    "earned_income": "Money in",
    "spending": "Money out",
    "transfer": "Transfer",
}
ACCOUNT_PURPOSES = {
    "include": "Include in reporting",
    "ignore": "Ignore",
}
class MemoryUploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        # Bank statements must not spill into Werkzeug's plaintext temp files.
        return io.BytesIO()


app = Flask(__name__)
app.request_class = MemoryUploadRequest
app.config["MAX_CONTENT_LENGTH"] = statements.MAX_UPLOAD_BYTES + 65536
# Review forms can contain up to 2,500 editable transaction rows.
app.config["MAX_FORM_MEMORY_SIZE"] = 4 * 1024 * 1024
LOGIN_ATTEMPTS = {}
vault = EncryptedDatabase(VAULT_PATH)
vault_last_activity = 0.0


def load_auth_config():
    try:
        return json.loads(AUTH_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_auth_config(config):
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUTH_PATH.with_name(f".{AUTH_PATH.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w") as handle:
            os.chmod(temporary, 0o600)
            json.dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, AUTH_PATH)
        os.chmod(AUTH_PATH, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def display_name():
    value = str(load_auth_config().get("app_name", DEFAULT_APP_NAME)).strip()
    return value[:40] or DEFAULT_APP_NAME


auth_config = load_auth_config()
app.config.update(
    SECRET_KEY=auth_config.get("secret_key") or secrets.token_hex(32),
    SESSION_COOKIE_NAME="family_finances_development" if DEVELOPMENT_MODE else "session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def local_request():
    return request.remote_addr in {"127.0.0.1", "::1"}


def local_setup_request():
    return local_request() and request.host.split(":", 1)[0] in {"127.0.0.1", "localhost"}


def safe_next_url(value):
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.context_processor
def template_branding():
    return {
        "app_name": display_name(),
        "development_mode": DEVELOPMENT_MODE,
        "plaid_disabled": PLAID_DISABLED,
    }


def unlock_data(password):
    global vault_last_activity
    config = load_auth_config()
    key_record = config.get("vault_key")
    if not key_record:
        raise VaultError("The encrypted data key is missing.")
    if not vault.exists:
        raise VaultError("The encrypted data file is missing.")
    data_key = unlock_key(password, key_record)
    vault.unlock(data_key)
    prepare_encrypted_database(vault, data_key, AUTH_PATH)
    prepare_development_blank_slate()
    reconcile_saved_model_rules()
    vault_last_activity = time.monotonic()


def prepare_development_blank_slate():
    if not DEVELOPMENT_MODE:
        return False
    with vault.connection() as connection:
        marker = connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            (DEVELOPMENT_RESET_MARKER,),
        ).fetchone()
        if marker and marker[0] == "1":
            return False
        category_names = {
            row[0]
            for row in connection.execute(
                """
                SELECT category FROM transactions
                UNION SELECT category_override FROM transactions
                UNION SELECT category FROM merchant_rules
                UNION SELECT name FROM category_rules
                """
            )
            if row[0]
        }
        normalized_categories = set()
        for name in category_names:
            if name.casefold() in {"venmo", "uncategorized"}:
                continue
            normalized_categories.add(
                "Transfer"
                if name.casefold() in {"transfer in", "transfer out"}
                else name
            )
        connection.execute(
            """
            UPDATE transactions
            SET category = 'Uncategorized', category_override = NULL,
                category_override_source = NULL,
                flow_override = NULL, spending_override = NULL, excluded = 0
            """
        )
        connection.execute("DELETE FROM merchant_rules")
        connection.execute("DELETE FROM category_rules")
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
            "DELETE FROM settings WHERE key IN (?, ?)",
            (OLLAMA_RESULT_SETTING, AI_REVIEW_SETTING),
        )
        connection.executemany(
            "INSERT INTO category_rules (name, flow_type) VALUES (?, ?)",
            (
                (name, default_category_flow_type(name))
                for name in sorted(normalized_categories, key=str.casefold)
            ),
        )
        connection.executemany(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                ("classification_mode", DEVELOPMENT_CLASSIFICATION_MODE),
                (DEVELOPMENT_RESET_MARKER, "1"),
            ),
        )
    return True


def lock_data():
    global vault_last_activity
    vault.lock()
    vault_last_activity = 0.0


def rotate_password(current_password, new_password):
    config = load_auth_config()
    key_record = config.get("vault_key")
    if not key_record:
        raise VaultError("The encrypted data key is missing.")

    unlock_key(current_password, key_record)
    new_key_record, new_data_key = create_key_record(new_password)
    new_config = dict(config)
    new_config.update(
        password_hash=generate_password_hash(new_password),
        secret_key=secrets.token_hex(32),
        vault_key=new_key_record,
    )

    backup_dir = create_encrypted_backup(
        VAULT_PATH,
        AUTH_PATH,
        prefix=".password-change-backup-",
    )
    try:
        vault.rotate_key(new_data_key)
        save_auth_config(new_config)
        vault.lock()
        unlock_data(new_password)
    except Exception:
        vault.lock()
        restore_encrypted_backup(backup_dir, VAULT_PATH, AUTH_PATH)
        unlock_data(current_password)
        raise
    else:
        try:
            delete_encrypted_backup(backup_dir)
        except OSError as error:
            app.logger.error(
                "Password changed, but its encrypted recovery copy remains: %s",
                error,
            )
            backup_retained = True
        else:
            backup_retained = False
        return new_config["secret_key"], backup_retained


@app.before_request
def require_login():
    global vault_last_activity
    if request.method == "POST":
        submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        expected = session.get("csrf_token")
        if not expected or not submitted or not secrets.compare_digest(expected, submitted):
            abort(400, "The form expired. Reload the page and try again.")
    if request.endpoint in {"static", "favicon", "health", "login", "setup"}:
        return None
    if not load_auth_config().get("password_hash"):
        if local_setup_request():
            return redirect(url_for("setup"))
        return "Password setup must be completed on the host Mac.", 403
    if vault.unlocked and time.monotonic() - vault_last_activity > VAULT_IDLE_SECONDS:
        lock_data()
        session.clear()
    if session.get("authenticated") and vault.unlocked:
        vault_last_activity = time.monotonic()
        return None
    session.clear()
    if request.path.startswith("/api/"):
        return jsonify(error="Authentication required."), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.after_request
def protect_responses(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        mode="development" if DEVELOPMENT_MODE else "stable",
        plaid_enabled=not PLAID_DISABLED,
    )


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.route("/setup", methods=["GET", "POST"])
def setup():
    global vault_last_activity
    if load_auth_config().get("password_hash"):
        return redirect(url_for("login"))
    if not local_setup_request():
        return "Password setup must be completed on the host Mac.", 403

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        if len(password) < 12:
            error = "Use at least 12 characters. A memorable passphrase works well."
        elif password != confirmation:
            error = "The passwords do not match."
        elif vault.exists:
            error = "Encrypted data already exists. Use the existing password to unlock it."
        else:
            key_record, data_key = create_key_record(password)
            config = {
                "password_hash": generate_password_hash(password),
                "secret_key": secrets.token_hex(32),
                "vault_key": key_record,
                "app_name": display_name(),
            }
            vault.create(data_key)
            save_auth_config(config)
            app.secret_key = config["secret_key"]
            vault.unlock(data_key)
            prepare_encrypted_database(vault, data_key, AUTH_PATH)
            prepare_development_blank_slate()
            vault_last_activity = time.monotonic()
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("category_setup"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    config = load_auth_config()
    if not config.get("password_hash"):
        return redirect(url_for("setup")) if local_setup_request() else ("Password setup is incomplete.", 403)

    client = request.remote_addr or "unknown"
    now = time.monotonic()
    attempts = [attempt for attempt in LOGIN_ATTEMPTS.get(client, []) if now - attempt < 900]
    LOGIN_ATTEMPTS[client] = attempts
    error = None
    next_url = safe_next_url(request.values.get("next"))

    if request.method == "POST":
        if len(attempts) >= 5:
            error = "Too many attempts. Try again in 15 minutes."
        elif check_password_hash(config["password_hash"], request.form.get("password", "")):
            try:
                unlock_data(request.form.get("password", ""))
            except (VaultError, SchemaError, OSError, sqlite3.DatabaseError) as vault_error:
                app.logger.error("Encrypted data could not be opened: %s", vault_error)
                error = "Your encrypted data could not be opened. No data was changed."
            else:
                LOGIN_ATTEMPTS.pop(client, None)
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                return redirect(next_url)
        else:
            attempts.append(now)
            LOGIN_ATTEMPTS[client] = attempts
            error = "That password is not correct."
    return render_template(
        "login.html",
        error=error,
        next_url=next_url,
        password_changed=request.args.get("changed") == "1",
        password_backup_retained=request.args.get("backup") == "1",
    )


@app.post("/logout")
def logout():
    lock_data()
    session.clear()
    return redirect(url_for("login"))


@app.template_filter("money")
def money(value):
    return f"${(value or 0) / 100:,.2f}"


def plaid_client():
    if PLAID_DISABLED:
        raise RuntimeError("Plaid is disabled in this local environment.")
    client_id, plaid_secret = plaid_credentials()
    configuration = Configuration(
        host="https://production.plaid.com",
        api_key={
            "clientId": client_id,
            "secret": plaid_secret,
        },
    )
    return plaid_api.PlaidApi(ApiClient(configuration))


def db():
    return vault.connection()


def setting(key):
    with db() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def save_setting(key, value):
    with db() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def local_ai_enabled(connection=None):
    if connection is None:
        with db() as saved_connection:
            return local_ai_enabled(saved_connection)
    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (LOCAL_AI_SETTING,)
    ).fetchone()
    return bool(row and row["value"] == "1")


def load_ollama_result(connection):
    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (OLLAMA_RESULT_SETTING,)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def save_ollama_result(connection, result):
    if not connection.execute(
        "SELECT 1 FROM settings WHERE key = ?", (AI_REVIEW_SETTING,)
    ).fetchone():
        save_ai_reviews(connection, load_ai_reviews(connection))
    connection.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (OLLAMA_RESULT_SETTING, json.dumps(result, separators=(",", ":"))),
    )


def reconcile_saved_model_rules():
    with vault.connection() as connection:
        if not local_ai_enabled(connection):
            return 0
        result = load_ollama_result(connection)
        if not result:
            return 0
        rule_count = create_recurring_category_rules(connection, result)
        if rule_count:
            save_ollama_result(connection, result)
        return rule_count


def overview_lookback_days():
    try:
        value = int(setting("overview_lookback_days"))
    except (TypeError, ValueError):
        return DEFAULT_OVERVIEW_LOOKBACK_DAYS
    return (
        value
        if 1 <= value <= MAX_OVERVIEW_LOOKBACK_DAYS
        else DEFAULT_OVERVIEW_LOOKBACK_DAYS
    )


def plaid_credentials():
    with db() as connection:
        values = dict(
            connection.execute(
                "SELECT key, value FROM settings WHERE key IN ('plaid_client_id', 'plaid_secret')"
            ).fetchall()
        )
    return values.get("plaid_client_id", ""), values.get("plaid_secret", "")


def normalize_plaid_products(values):
    return sorted(
        {
            str(getattr(value, "value", value)).strip().lower()
            for value in (values or [])
            if str(getattr(value, "value", value)).strip()
        }
    )


def plaid_product_status():
    try:
        saved = json.loads(setting("plaid_product_audit") or "{}")
    except (TypeError, json.JSONDecodeError):
        saved = {}
    saved_connections = {
        record.get("connection_id"): record
        for record in saved.get("connections", [])
        if isinstance(record, dict)
    }
    connections = []
    for connection in connection_rows():
        record = saved_connections.get(connection["id"], {})
        product_fields = {
            field: normalize_plaid_products(record.get(field))
            for field in ("products", "billed_products", "consented_products")
        }
        unexpected = sorted(
            set().union(*map(set, product_fields.values())) - EXPECTED_PLAID_PRODUCTS
        )
        connections.append(
            {
                **connection,
                **product_fields,
                "unavailable": bool(record.get("unavailable")),
                "unexpected_products": unexpected,
            }
        )
    return {
        "checked_at": saved.get("checked_at"),
        "connections": connections,
        "has_warning": any(
            item["unavailable"] or item["unexpected_products"]
            for item in connections
        ),
    }


def audit_plaid_products():
    with db() as connection:
        items = [
            dict(row)
            for row in connection.execute(
                "SELECT id, access_token FROM connections WHERE access_token != '' ORDER BY id"
            )
        ]
    if not items:
        return []
    client = plaid_client()
    results = []
    for item in items:
        try:
            plaid_item = client.item_get(
                ItemGetRequest(access_token=item["access_token"])
            ).item
        except Exception:
            app.logger.warning(
                "Plaid product status could not be checked for connection %s.",
                item["id"],
            )
            results.append({"connection_id": item["id"], "unavailable": True})
            continue
        results.append(
            {
                "connection_id": item["id"],
                "products": normalize_plaid_products(plaid_item.products),
                "billed_products": normalize_plaid_products(
                    getattr(plaid_item, "billed_products", [])
                ),
                "consented_products": normalize_plaid_products(
                    getattr(plaid_item, "consented_products", [])
                ),
            }
        )
    save_setting(
        "plaid_product_audit",
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "connections": results,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    return results


def month_after(value):
    year = value.year + (value.month == 12)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def stale_savings_accounts():
    with db() as connection:
        accounts = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.id, a.institution, a.name,
                       MAX(s.recorded_on) AS recorded_on
                FROM manual_accounts a
                LEFT JOIN savings_snapshots s ON s.manual_account_id = a.id
                WHERE a.archived = 0 AND a.reminder_enabled = 1
                GROUP BY a.id
                ORDER BY a.institution, a.name
                """
            )
        ]
    today = date.today()
    return [
        account
        for account in accounts
        if not account["recorded_on"]
        or today > month_after(date.fromisoformat(account["recorded_on"]))
    ]


def savings_data():
    with db() as connection:
        account_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM manual_accounts
                ORDER BY archived, institution, name, id
                """
            )
        ]
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT s.id, s.manual_account_id, s.amount, s.recorded_on,
                       a.institution, a.name, a.owner_name,
                       a.classification, a.goal_eligible, a.archived
                FROM savings_snapshots s
                JOIN manual_accounts a ON a.id = s.manual_account_id
                ORDER BY s.recorded_on, s.id
                """
            )
        ]
        goal_row = connection.execute(
            "SELECT value FROM settings WHERE key = 'savings_goal_cents'"
        ).fetchone()

    definitions = {account["id"]: account for account in account_rows}
    active_definitions = {
        account["id"]: account for account in account_rows if not account["archived"]
    }
    latest = {}
    for row in rows:
        latest[row["manual_account_id"]] = row

    accounts = []
    totals = {key: 0 for key in SAVINGS_CLASSIFICATIONS}
    goal_total = 0
    for definition in active_definitions.values():
        snapshot = latest.get(definition["id"])
        amount = snapshot["amount"] if snapshot else 0
        totals[definition["classification"]] += amount
        if definition["goal_eligible"]:
            goal_total += amount
        accounts.append(
            {
                **definition,
                "amount": amount,
                "recorded_on": snapshot["recorded_on"] if snapshot else None,
                "classification_label": SAVINGS_CLASSIFICATIONS[definition["classification"]],
            }
        )

    timeline = []
    state = {}
    current_date = None

    def add_timeline_point(recorded_on):
        classification_totals = {
            classification: sum(
                state.get(account["id"], 0)
                for account in active_definitions.values()
                if account["classification"] == classification
            )
            for classification in SAVINGS_CLASSIFICATIONS
        }
        timeline.append(
            {
                "date": recorded_on,
                **classification_totals,
                "total": sum(classification_totals.values()),
            }
        )

    for row in rows:
        if row["manual_account_id"] not in active_definitions:
            continue
        if current_date and row["recorded_on"] != current_date:
            add_timeline_point(current_date)
        current_date = row["recorded_on"]
        state[row["manual_account_id"]] = row["amount"]
    if current_date:
        add_timeline_point(current_date)

    savings_goal = int(goal_row["value"]) if goal_row else DEFAULT_SAVINGS_GOAL
    all_savings_total = sum(totals.values())
    return {
        "savings_accounts": accounts,
        "archived_savings_accounts": [
            {
                **account,
                "classification_label": SAVINGS_CLASSIFICATIONS[account["classification"]],
            }
            for account in account_rows
            if account["archived"]
        ],
        "savings_history": [
            {
                **row,
                "classification_label": SAVINGS_CLASSIFICATIONS[row["classification"]],
            }
            for row in reversed(rows)
        ],
        "savings_timeline": timeline,
        "classification_totals": totals,
        "classification_labels": SAVINGS_CLASSIFICATIONS,
        "all_savings_total": all_savings_total,
        "goal_eligible_total": goal_total,
        "savings_goal": savings_goal,
        "savings_goal_remaining": max(savings_goal - goal_total, 0),
        "savings_goal_percent": goal_total / savings_goal * 100 if savings_goal else 0,
    }


def connection_rows():
    with db() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT c.id, c.owner_name, c.institution,
                       c.transactions_update_status, c.last_synced_at,
                       COUNT(a.id) AS account_count
                FROM connections c
                LEFT JOIN accounts a ON a.connection_id = c.id
                WHERE c.access_token != ''
                GROUP BY c.id
                ORDER BY c.created_at, c.id
                """
            )
        ]


def save_transaction(connection, transaction):
    if not connection.execute("SELECT 1 FROM transactions WHERE id = ?", (transaction.transaction_id,)).fetchone():
        imported_id = statements.matching_import(
            connection, transaction.account_id, transaction.date.isoformat(),
            round(transaction.amount * 100), transaction.iso_currency_code or "USD", transaction.name,
        ) if not transaction.pending else None
        if imported_id:
            connection.execute("UPDATE transactions SET id = ? WHERE id = ?", (transaction.transaction_id, imported_id))
            reviews = load_ai_reviews(connection)
            if imported_id in reviews:
                reviews[transaction.transaction_id] = reviews.pop(imported_id)
                save_ai_reviews(connection, reviews)
    category = "Uncategorized"
    connection.execute(
        """
        INSERT INTO transactions (
            id, account_id, amount, currency, description, merchant,
            pending, transacted_at, category, excluded
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            amount = excluded.amount,
            description = excluded.description,
            merchant = excluded.merchant,
            pending = excluded.pending,
            transacted_at = excluded.transacted_at,
            category = excluded.category
        """,
        (
            transaction.transaction_id,
            transaction.account_id,
            round(transaction.amount * 100),
            transaction.iso_currency_code or "USD",
            transaction.name,
            transaction.merchant_name,
            int(transaction.pending),
            transaction.date.isoformat(),
            category,
            0,
        ),
    )


def save_account(connection, account, connection_id, institution, checked_at):
    current = account.balances.current
    available = getattr(account.balances, "available", None)
    current_balance = (
        int(
            (Decimal(str(current)) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if current is not None
        else None
    )
    available_balance = (
        int(
            (Decimal(str(available)) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if available is not None
        else None
    )
    subtype = getattr(account, "subtype", None)
    subtype = getattr(subtype, "value", subtype)
    account_type = account.type.value
    included_by_default = account_type in {"depository", "credit"}
    cash_flow_role = "cash_flow" if included_by_default else "other"
    spending_enabled = int(included_by_default)
    connection.execute(
        """
        INSERT INTO accounts (
            id, connection_id, institution, name, mask, type,
            subtype, current_balance, available_balance, balance_updated_at,
            cash_flow_role, spending_enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            connection_id = excluded.connection_id,
            institution = excluded.institution,
            name = excluded.name,
            mask = excluded.mask,
            type = excluded.type,
            subtype = excluded.subtype,
            current_balance = excluded.current_balance,
            available_balance = excluded.available_balance,
            balance_updated_at = excluded.balance_updated_at
        """,
        (
            account.account_id,
            connection_id,
            institution,
            account.name,
            account.mask,
            account_type,
            subtype,
            current_balance,
            available_balance,
            checked_at,
            cash_flow_role,
            spending_enabled,
        ),
    )


def sync_connection(item):
    if not item["access_token"]:
        return 0
    client = plaid_client()
    cursor = item["cursor"]
    count = 0
    update_status = None
    added = []
    modified = []
    removed = []
    while True:
        response = client.transactions_sync(
            TransactionsSyncRequest(
                access_token=item["access_token"],
                cursor=cursor or "",
                count=500,
            )
        )
        added.extend(response.added)
        modified.extend(response.modified)
        removed.extend(response.removed)
        cursor = response.next_cursor
        update_status = response.transactions_update_status.value
        if not response.has_more:
            break

    synced_at = datetime.now().isoformat(timespec="seconds")
    balance_accounts = []
    try:
        balance_accounts = client.accounts_get(
            AccountsGetRequest(access_token=item["access_token"])
        ).accounts
    except Exception as error:
        app.logger.warning(
            "Cached balances could not be refreshed for connection %s: %s",
            item["id"],
            error,
        )
    with db() as connection:
        for transaction in added + modified:
            save_transaction(connection, transaction)
            count += 1
        for removed_transaction in removed:
            connection.execute(
                """
                DELETE FROM transactions
                WHERE id = ?
                  AND account_id IN (
                      SELECT id FROM accounts WHERE connection_id = ?
                  )
                """,
                (removed_transaction.transaction_id, item["id"]),
            )
        for account in balance_accounts:
            save_account(
                connection,
                account,
                item["id"],
                item["institution"],
                synced_at,
            )
        connection.execute(
            """
            UPDATE connections
            SET cursor = ?, transactions_update_status = ?, last_synced_at = ?
            WHERE id = ?
            """,
            (cursor, update_status, synced_at, item["id"]),
        )
    return count


def sync_all_connections():
    with db() as connection:
        items = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM connections WHERE access_token != '' ORDER BY id"
            )
        ]
    imported = 0
    errors = []
    for item in items:
        try:
            imported += sync_connection(item)
        except Exception as error:
            errors.append(
                {"connection_id": item["id"], "owner_name": item["owner_name"], "error": str(error)}
            )
    return imported, errors


def page_context(active):
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        month = datetime.now().strftime("%Y-%m")
    account_id = request.args.get("account") or None
    connection_id = request.args.get("person") or None
    if connection_id and not connection_id.isdigit():
        connection_id = None
    connection_id = int(connection_id) if connection_id else None
    with db() as connection:
        accounts = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.*, c.owner_name, (c.access_token = '') AS unlinked
                FROM accounts a
                JOIN connections c ON c.id = a.connection_id
                ORDER BY c.owner_name, a.name
                """
            )
        ]
        for account in accounts:
            account["reporting_purpose"] = (
                "include"
                if account["cash_flow_role"] == "cash_flow"
                and account["spending_enabled"]
                else "ignore"
            )
        profiles = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, owner_name, institution, (access_token = '') AS unlinked,
                       transactions_update_status, last_synced_at
                FROM connections ORDER BY created_at, id
                """
            )
        ]
    last_synced_values = [
        profile["last_synced_at"]
        for profile in profiles
        if profile["last_synced_at"]
    ]
    return {
        "active": active,
        "accounts": accounts,
        "profiles": profiles,
        "month": month,
        "month_label": month_label(month),
        "account_id": account_id,
        "connection_id": connection_id,
        "connected": any(not profile["unlinked"] for profile in profiles),
        "last_synced_at": max(last_synced_values) if last_synced_values else None,
        "history_loading": any(
            profile["transactions_update_status"]
            in {"NOT_READY", "INITIAL_UPDATE_COMPLETE"}
            for profile in profiles
        ),
        "stale_savings_accounts": stale_savings_accounts(),
    }


@app.get("/")
def overview():
    context = page_context("overview")
    context["flow_types"] = FLOW_TYPES
    context["lookback_days"] = overview_lookback_days()
    context["max_lookback_days"] = MAX_OVERVIEW_LOOKBACK_DAYS
    context["overview_error"] = request.args.get("error")
    with db() as connection:
        context["summary"] = rolling_spending_summary(
            connection,
            context["lookback_days"],
            context["account_id"],
            context["connection_id"],
        )
        context["cash_flow"] = cash_flow_summary(
            connection,
            context["lookback_days"],
            context["account_id"],
            context["connection_id"],
        )
        context["recent"] = transaction_list(
            connection,
            account_id=context["account_id"],
            connection_id=context["connection_id"],
            date_from=context["summary"]["date_from"],
            date_to=context["summary"]["date_to"],
            limit=8,
            reporting_scope="spending",
            spending_only=True,
        )[:8]
    context["savings"] = (
        savings_data()
        if not context["account_id"] and not context["connection_id"]
        else None
    )
    context["credit_balances"] = [
        account
        for account in context["accounts"]
        if account["type"] == "credit"
        and account["current_balance"] is not None
        and (not context["account_id"] or account["id"] == context["account_id"])
        and (
            not context["connection_id"]
            or account["connection_id"] == context["connection_id"]
        )
    ]
    context["credit_balance_total"] = sum(
        account["current_balance"] for account in context["credit_balances"]
    )
    balance_dates = [
        account["balance_updated_at"]
        for account in context["credit_balances"]
        if account["balance_updated_at"]
    ]
    context["credit_balance_updated_at"] = max(balance_dates) if balance_dates else None
    context["cash_balances"] = [
        account
        for account in context["accounts"]
        if account["type"] == "depository"
        and account["current_balance"] is not None
        and (not context["account_id"] or account["id"] == context["account_id"])
        and (
            not context["connection_id"]
            or account["connection_id"] == context["connection_id"]
        )
    ]
    context["cash_balance_total"] = sum(
        account["current_balance"] for account in context["cash_balances"]
    )
    cash_balance_dates = [
        account["balance_updated_at"]
        for account in context["cash_balances"]
        if account["balance_updated_at"]
    ]
    context["cash_balance_updated_at"] = (
        max(cash_balance_dates) if cash_balance_dates else None
    )
    return render_template("overview.html", **context)


@app.get("/cash-flow")
def cash_flow():
    context = page_context("cash_flow")
    context["flow_types"] = FLOW_TYPES
    context["cash_flow_accounts"] = [
        account
        for account in context["accounts"]
        if account["cash_flow_role"] == "cash_flow"
    ]
    context["lookback_days"] = overview_lookback_days()
    context["max_lookback_days"] = MAX_OVERVIEW_LOOKBACK_DAYS
    context["cash_flow_error"] = request.args.get("error")
    with db() as connection:
        context["cash_flow"] = cash_flow_summary(
            connection,
            context["lookback_days"],
            context["account_id"],
            context["connection_id"],
        )
    return render_template("cash_flow.html", **context)


@app.get("/trends")
def trends():
    context = page_context("trends")
    with db() as connection:
        context["long_term"] = long_term_trends(
            connection,
            context["account_id"],
            context["connection_id"],
        )
    return render_template("trends.html", **context)


@app.get("/categories")
def categories():
    context = page_context("categories")
    with db() as connection:
        context["summary"] = spending_summary(
            connection,
            context["month"],
            context["account_id"],
            context["connection_id"],
        )
        context["category_details"] = category_details(
            connection,
            context["month"],
            context["account_id"],
            context["connection_id"],
        )
    return render_template("categories.html", **context)


@app.get("/transactions")
def transactions():
    context = page_context("transactions")
    category = request.args.get("category") or None
    ai_review = request.args.get("ai_review") == "1"
    excluded_categories = list(
        dict.fromkeys(
            value.strip()
            for value in request.args.getlist("exclude_category")
            if value.strip()
        )
    )
    query = request.args.get("q") or None
    sort = request.args.get("sort") or "date_desc"
    if sort not in {
        "date_desc",
        "date_asc",
        "amount_desc",
        "amount_asc",
        "merchant_asc",
        "merchant_desc",
        "category_asc",
        "account_asc",
    }:
        sort = "date_desc"
    transaction_view = request.args.get("view") or (
        "all" if request.args.get("excluded") == "1" else "active"
    )
    if transaction_view not in {"active", "excluded", "all"}:
        transaction_view = "active"
    default_scope = "all" if DEVELOPMENT_MODE else "spending"
    reporting_scope = request.args.get("purpose") or default_scope
    if reporting_scope not in {"spending", "cash_flow", "all"}:
        reporting_scope = default_scope
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None

    for value_name, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                if value_name == "date_from":
                    date_from = None
                else:
                    date_to = None
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    drilldown_month = request.args.get("month")
    if drilldown_month and not date_from and not date_to:
        try:
            selected = datetime.strptime(drilldown_month, "%Y-%m")
            date_from = f"{drilldown_month}-01"
            date_to = (
                f"{drilldown_month}-"
                f"{calendar.monthrange(selected.year, selected.month)[1]:02d}"
            )
        except ValueError:
            pass

    with db() as connection:
        context["transactions"] = transaction_list(
            connection=connection,
            account_id=context["account_id"],
            connection_id=context["connection_id"],
            category=category,
            query=query,
            include_excluded=transaction_view == "all",
            excluded_only=transaction_view == "excluded",
            date_from=date_from,
            date_to=date_to,
            reporting_scope=reporting_scope,
            excluded_categories=excluded_categories,
            sort=sort,
        )
        overlaps = statements.possible_overlaps(connection)
        reviews = load_ai_reviews(connection)
        for transaction in context["transactions"]:
            transaction["statement_overlap"] = transaction["id"] in overlaps
            review = reviews.get(transaction["id"])
            transaction["ai_review"] = bool(
                review and transaction["category_override_source"] != "user"
                and review["category"].casefold() == transaction["effective_category"].casefold()
            )
            transaction["ai_reason"] = review.get("reason", "") if transaction["ai_review"] else ""
        if ai_review:
            context["transactions"] = [
                transaction for transaction in context["transactions"] if transaction["ai_review"]
            ]
        context["category_options"] = [
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM (
                    SELECT COALESCE(category_override, category) AS name
                    FROM transactions
                    UNION
                    SELECT name FROM category_rules
                    UNION
                    SELECT category AS name FROM merchant_rules
                ) ORDER BY name COLLATE NOCASE
                """
            )
        ]
        context["category_flow_defaults"] = {
            name: default_category_flow_type(name)
            for name in context["category_options"]
            if name
        }
        context["category_flow_defaults"].update(
            dict(connection.execute("SELECT name, flow_type FROM category_rules"))
        )
        context["local_ai_enabled"] = local_ai_enabled(connection)
        ollama_result = (
            load_ollama_result(connection) if context["local_ai_enabled"] else None
        )
        context["has_ollama_result"] = bool(ollama_result)
        context["ollama_result_status"] = (
            ollama_result.get("status") if ollama_result else None
        )
        context["category_setup_complete"] = category_setup_is_complete(connection)
    context["flow_types"] = FLOW_TYPES
    context.update(
        ai_review=ai_review,
        selected_category=category,
        search_query=query or "",
        transaction_view=transaction_view,
        reporting_scope=reporting_scope,
        date_from=date_from or "",
        date_to=date_to or "",
        excluded_categories=excluded_categories,
        transaction_sort=sort,
        table_filters_active=bool(
            ai_review
            or date_from
            or date_to
            or query
            or category
            or excluded_categories
            or transaction_view != "active"
            or reporting_scope != default_scope
            or sort != "date_desc"
            or context["account_id"]
            or context["connection_id"]
        ),
    )
    return render_template("transactions.html", **context)


@app.get("/savings")
def savings():
    context = page_context("savings")
    context.update(savings_data())
    context["today"] = date.today().isoformat()
    context["saved_count"] = request.args.get("saved")
    context["savings_error"] = request.args.get("error")
    return render_template("savings.html", **context)


@app.get("/settings")
def settings_page():
    context = page_context("settings")
    client_id, plaid_secret = plaid_credentials()
    with db() as connection:
        category_count = connection.execute(
            "SELECT COUNT(*) FROM category_rules"
        ).fetchone()[0]
        category_setup_complete = category_setup_is_complete(connection)
        local_ai = local_ai_enabled(connection)
    context.update(
        plaid_client_id=client_id,
        plaid_configured=bool(client_id and plaid_secret),
        plaid_product_status=plaid_product_status(),
        account_purposes=ACCOUNT_PURPOSES,
        settings_saved=request.args.get("saved"),
        settings_error=request.args.get("error"),
        category_count=category_count,
        category_setup_complete=category_setup_complete,
        local_ai_enabled=local_ai,
    )
    return render_template("settings.html", **context)


@app.post("/api/local-ai")
def update_local_ai():
    enabled = request.form.get("enabled") == "on"
    save_setting(LOCAL_AI_SETTING, "1" if enabled else "0")
    return redirect(url_for("settings_page", saved="local_ai"))


@app.post("/api/account-roles")
def update_account_roles():
    account_ids = request.form.getlist("account_id")
    purposes = request.form.getlist("reporting_purpose")
    if len(account_ids) != len(purposes) or any(
        purpose not in ACCOUNT_PURPOSES for purpose in purposes
    ):
        return redirect(url_for("settings_page", error="account_roles"))

    with db() as connection:
        saved_accounts = {
            row["id"]: row
            for row in connection.execute("SELECT id, type FROM accounts")
        }
        if (
            len(account_ids) != len(set(account_ids))
            or set(account_ids) != set(saved_accounts)
        ):
            return redirect(url_for("settings_page", error="account_roles"))
        connection.executemany(
            """
            UPDATE accounts
            SET cash_flow_role = ?, spending_enabled = ?
            WHERE id = ?
            """,
            (
                (
                    "cash_flow" if purpose == "include" else "other",
                    int(purpose == "include"),
                    account_id,
                )
                for account_id, purpose in zip(account_ids, purposes)
            ),
        )
    return redirect(url_for("settings_page", saved="account_roles"))


def currency_to_cents(raw_amount):
    amount = Decimal(raw_amount.replace(",", "").replace("$", ""))
    if (
        not amount.is_finite()
        or amount < 0
        or amount > Decimal("9999999999")
    ):
        raise InvalidOperation
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@app.post("/api/savings")
def save_savings():
    recorded_on = request.form.get("recorded_on", "")
    try:
        recorded_date = date.fromisoformat(recorded_on)
    except ValueError:
        return redirect(url_for("savings", error="date"))
    if recorded_date > date.today():
        return redirect(url_for("savings", error="future"))

    entries = []
    try:
        with db() as connection:
            accounts = [
                dict(row)
                for row in connection.execute(
                    "SELECT id FROM manual_accounts WHERE archived = 0 ORDER BY id"
                )
            ]
        for account in accounts:
            raw_amount = request.form.get(f"account_{account['id']}", "").strip()
            if not raw_amount:
                continue
            entries.append(
                (
                    account["id"],
                    currency_to_cents(raw_amount),
                    recorded_on,
                )
            )
    except InvalidOperation:
        return redirect(url_for("savings", error="amount"))

    if not entries:
        return redirect(url_for("savings", error="empty"))

    with db() as connection:
        connection.executemany(
            """
            INSERT INTO savings_snapshots (manual_account_id, amount, recorded_on)
            VALUES (?, ?, ?)
            ON CONFLICT(manual_account_id, recorded_on)
            DO UPDATE SET amount = excluded.amount
            """,
            entries,
        )
    return redirect(url_for("savings", saved=len(entries)))


@app.post("/api/savings-goal")
def update_savings_goal():
    try:
        goal = currency_to_cents(request.form.get("goal", "").strip())
        if goal <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return redirect(url_for("savings", error="goal"))
    save_setting("savings_goal_cents", str(goal))
    return redirect(url_for("savings", saved="goal"))


@app.post("/api/overview-lookback")
def update_overview_lookback():
    try:
        lookback_days = int(request.form.get("lookback_days", ""))
    except ValueError:
        lookback_days = 0
    redirect_arguments = {
        "account": request.form.get("account") or None,
        "person": request.form.get("person") or None,
    }
    destination = "cash_flow" if request.form.get("view") == "cash_flow" else "overview"
    if not 1 <= lookback_days <= MAX_OVERVIEW_LOOKBACK_DAYS:
        redirect_arguments["error"] = "lookback"
        return redirect(url_for(destination, **redirect_arguments))
    save_setting("overview_lookback_days", str(lookback_days))
    return redirect(url_for(destination, **redirect_arguments))


def manual_account_values():
    institution = request.form.get("institution", "").strip()[:80]
    name = request.form.get("name", "").strip()[:80]
    owner_name = request.form.get("owner_name", "").strip()[:40] or "Household"
    classification = request.form.get("classification", "")
    if not institution or not name or classification not in SAVINGS_CLASSIFICATIONS:
        return None
    return (
        institution,
        name,
        owner_name,
        classification,
        int(request.form.get("goal_eligible") == "on"),
        int(request.form.get("reminder_enabled") == "on"),
    )


@app.post("/api/manual-accounts")
def add_manual_account():
    values = manual_account_values()
    if not values:
        return redirect(url_for("savings", error="account"))
    with db() as connection:
        connection.execute(
            """
            INSERT INTO manual_accounts (
                institution, name, owner_name, classification,
                goal_eligible, reminder_enabled
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return redirect(url_for("savings", saved="account"))


@app.post("/api/manual-accounts/<int:account_id>")
def update_manual_account(account_id):
    values = manual_account_values()
    if not values:
        return redirect(url_for("savings", error="account"))
    with db() as connection:
        connection.execute(
            """
            UPDATE manual_accounts
            SET institution = ?, name = ?, owner_name = ?, classification = ?,
                goal_eligible = ?, reminder_enabled = ?, archived = ?
            WHERE id = ?
            """,
            (*values, int(request.form.get("archived") == "on"), account_id),
        )
    return redirect(url_for("savings", saved="account"))


@app.post("/api/plaid-settings")
def update_plaid_settings():
    if PLAID_DISABLED:
        return jsonify(error="Plaid is disabled in this local environment."), 403
    client_id = request.form.get("client_id", "").strip()
    new_secret = request.form.get("secret", "").strip()
    _, current_secret = plaid_credentials()
    if not client_id or not (new_secret or current_secret):
        return redirect(url_for("settings_page", error="plaid"))
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('plaid_client_id', ?)",
            (client_id,),
        )
        if new_secret:
            connection.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('plaid_secret', ?)",
                (new_secret,),
            )
    return redirect(url_for("settings_page", saved="plaid"))


@app.post("/api/plaid-products")
def update_plaid_product_status():
    if PLAID_DISABLED:
        return jsonify(error="Plaid is disabled in this local environment."), 403
    client_id, plaid_secret = plaid_credentials()
    if not client_id or not plaid_secret:
        return redirect(url_for("settings_page", error="plaid"))
    if not connection_rows():
        return redirect(url_for("settings_page", error="plaid_products_connections"))
    results = audit_plaid_products()
    error = "plaid_products_check" if all(
        result.get("unavailable") for result in results
    ) else None
    return redirect(
        url_for(
            "settings_page",
            **({"error": error} if error else {"saved": "plaid_products"}),
        )
    )


@app.post("/api/app-name")
def update_app_name():
    name = request.form.get("app_name", "").strip()[:40]
    if not name:
        return redirect(url_for("settings_page", error="app_name"))
    config = load_auth_config()
    config["app_name"] = name
    save_auth_config(config)
    return redirect(url_for("settings_page", saved="app_name"))


@app.post("/api/password")
def update_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirmation = request.form.get("confirmation", "")
    config = load_auth_config()

    if not check_password_hash(config.get("password_hash", ""), current_password):
        return redirect(url_for("settings_page", error="current_password"))
    if len(new_password) < 12:
        return redirect(url_for("settings_page", error="password_length"))
    if new_password != confirmation:
        return redirect(url_for("settings_page", error="password_match"))
    if new_password == current_password:
        return redirect(url_for("settings_page", error="password_same"))

    try:
        new_secret_key, backup_retained = rotate_password(
            current_password,
            new_password,
        )
    except (VaultError, SchemaError, OSError, sqlite3.DatabaseError) as error:
        app.logger.error("Password change failed: %s", error)
        return redirect(url_for("settings_page", error="password_change"))

    lock_data()
    app.secret_key = new_secret_key
    LOGIN_ATTEMPTS.clear()
    session.clear()
    login_arguments = {"changed": "1"}
    if backup_retained:
        login_arguments["backup"] = "1"
    return redirect(url_for("login", **login_arguments))


@app.post("/api/link-token")
def create_link_token():
    if PLAID_DISABLED:
        return jsonify(error="Plaid is disabled in this local environment."), 403
    client_id, plaid_secret = plaid_credentials()
    if not client_id or not plaid_secret:
        return jsonify(
            error="Add your Plaid Client ID and Production secret in Settings."
        ), 400

    payload = request.get_json(silent=True) or {}
    connection_id = payload.get("connection_id")
    request_data = {
        "client_name": display_name()[:30],
        "country_codes": [CountryCode("US")],
        "language": "en",
        "user": LinkTokenCreateRequestUser(client_user_id="local-user"),
    }
    if connection_id:
        with db() as connection:
            item = connection.execute(
                "SELECT access_token FROM connections WHERE id = ? AND access_token != ''",
                (connection_id,),
            ).fetchone()
        if not item:
            return jsonify(error="That connection no longer exists."), 404
        request_data["access_token"] = item["access_token"]
    else:
        request_data["products"] = [Products("transactions")]
        request_data["transactions"] = LinkTokenTransactions(days_requested=730)
    response = plaid_client().link_token_create(LinkTokenCreateRequest(**request_data))
    return jsonify(link_token=response.link_token)


@app.post("/api/exchange-token")
def exchange_token():
    if PLAID_DISABLED:
        return jsonify(error="Plaid is disabled in this local environment."), 403
    payload = request.get_json(force=True)
    public_token = payload["public_token"]
    owner_name = (payload.get("owner_name") or "Household member").strip()[:40]
    client = plaid_client()
    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    checked_at = datetime.now().isoformat(timespec="seconds")
    accounts = client.accounts_get(
        AccountsGetRequest(access_token=exchange.access_token)
    ).accounts
    institution = payload.get("institution_name", "Financial institution")
    with db() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO connections (
                    plaid_item_id, owner_name, institution, access_token
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    exchange.item_id,
                    owner_name,
                    institution,
                    exchange.access_token,
                ),
            )
        except sqlite3.IntegrityError:
            return jsonify(error="This bank connection is already connected."), 409
        connection_id = cursor.lastrowid
        for account in accounts:
            save_account(
                connection,
                account,
                connection_id,
                institution,
                checked_at,
            )
    return jsonify(ok=True, connection_id=connection_id)


@app.post("/api/sync")
def sync():
    if PLAID_DISABLED:
        return jsonify(error="Plaid is disabled in this local environment."), 403
    try:
        imported, errors = sync_all_connections()
        with db() as connection:
            coverage = connection.execute(
                "SELECT MIN(transacted_at) AS earliest, MAX(transacted_at) AS latest FROM transactions"
            ).fetchone()
            statuses = [
                row["transactions_update_status"]
                for row in connection.execute(
                    "SELECT transactions_update_status FROM connections WHERE access_token != ''"
                )
            ]
        history_loading = any(
            status in {"NOT_READY", "INITIAL_UPDATE_COMPLETE"}
            for status in statuses
        )
        return jsonify(
            ok=not errors,
            imported=imported,
            history_status=(
                "INITIAL_UPDATE_COMPLETE"
                if history_loading
                else "HISTORICAL_UPDATE_COMPLETE"
            ),
            earliest=coverage["earliest"],
            latest=coverage["latest"],
            errors=errors,
        )
    except Exception as error:
        return jsonify(error=str(error)), 409


@app.post("/api/link-exit")
def link_exit():
    payload = request.get_json(force=True)
    diagnostic = {
        "error_code": payload.get("error_code"),
        "error_type": payload.get("error_type"),
        "request_id": payload.get("request_id"),
        "exit_status": payload.get("exit_status"),
        "institution": payload.get("institution"),
    }
    save_setting("last_link_error", json.dumps(diagnostic))
    app.logger.warning("Plaid Link exit: %s", diagnostic)
    return jsonify(ok=True)


def normalized_category(value):
    return " ".join(value.split())[:80] or None


def default_category_flow_type(name):
    normalized = name.casefold()
    if normalized == "income":
        return "earned_income"
    if normalized in {"loan disbursements", "reimbursed work travel"}:
        return "earned_income"
    if normalized == "transfer":
        return "transfer"
    return "spending"


def category_setup_is_complete(connection):
    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?",
        (CATEGORY_SETUP_SETTING,),
    ).fetchone()
    return bool(row and row["value"] == "1")


def category_setup_rows(connection):
    complete = category_setup_is_complete(connection)
    rows = [
        {
            "original_name": row["name"],
            "name": row["name"],
            "flow_type": row["flow_type"],
            "required": row["name"].casefold() == "transfer",
        }
        for row in connection.execute(
            "SELECT name, flow_type FROM category_rules ORDER BY name COLLATE NOCASE"
        )
    ]
    if not complete:
        existing = {row["name"].casefold() for row in rows}
        rows.extend(
            {
                "original_name": "",
                "name": name,
                "flow_type": "spending",
                "required": False,
            }
            for name in CATEGORY_SUGGESTIONS
            if name.casefold() not in existing
        )
    return rows, complete


def save_category_setup(connection, rows):
    existing = {
        row["name"].casefold(): row["name"]
        for row in connection.execute("SELECT name FROM category_rules")
    }
    submitted = {
        row["original_name"].casefold(): row["name"]
        for row in rows
        if row["original_name"]
        and row["original_name"].casefold() in existing
    }
    reviews = load_ai_reviews(connection)
    for transaction_id, review in list(reviews.items()):
        key = review["category"].casefold()
        if key in existing:
            if key in submitted:
                review["category"] = submitted[key]
            else:
                del reviews[transaction_id]
    save_ai_reviews(connection, reviews)
    staged = []
    for key, original_name in existing.items():
        replacement = submitted.get(key)
        if replacement is None:
            connection.execute(
                "UPDATE transactions SET category_override = NULL, "
                "category_override_source = NULL "
                "WHERE category_override = ? COLLATE NOCASE",
                (original_name,),
            )
            connection.execute(
                "DELETE FROM merchant_rules WHERE category = ? COLLATE NOCASE",
                (original_name,),
            )
        elif replacement != original_name:
            temporary_name = f"__category_setup_{secrets.token_hex(8)}"
            connection.execute(
                "UPDATE transactions SET category_override = ? "
                "WHERE category_override = ? COLLATE NOCASE",
                (temporary_name, original_name),
            )
            connection.execute(
                "UPDATE merchant_rules SET category = ? "
                "WHERE category = ? COLLATE NOCASE",
                (temporary_name, original_name),
            )
            staged.append((temporary_name, replacement))

    connection.execute("DELETE FROM category_rules")
    connection.executemany(
        "INSERT INTO category_rules (name, flow_type) VALUES (?, ?)",
        ((row["name"], row["flow_type"]) for row in rows),
    )
    for temporary_name, replacement in staged:
        connection.execute(
            "UPDATE transactions SET category_override = ? "
            "WHERE category_override = ?",
            (replacement, temporary_name),
        )
        connection.execute(
            "UPDATE merchant_rules SET category = ? WHERE category = ?",
            (replacement, temporary_name),
        )
    connection.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, '1')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (CATEGORY_SETUP_SETTING,),
    )
    connection.execute(
        "DELETE FROM settings WHERE key = ?", (OLLAMA_RESULT_SETTING,)
    )


@app.route("/category-setup", methods=["GET", "POST"])
def category_setup():
    next_url = safe_next_url(request.values.get("next"))
    error = None
    with db() as connection:
        rows, complete = category_setup_rows(connection)
        if request.method == "POST":
            if OLLAMA_EVALUATION_LOCK.locked():
                error = "Wait for local categorization to finish before changing labels."
            else:
                names = request.form.getlist("category_name")
                flow_types = request.form.getlist("flow_type")
                original_names = request.form.getlist("original_name")
                submitted_rows = []
                if not (
                    len(names) == len(flow_types) == len(original_names)
                ):
                    error = "The category list could not be saved. Reload and try again."
                else:
                    for name, flow_type, original_name in zip(
                        names, flow_types, original_names
                    ):
                        name = normalized_category(name)
                        if not name:
                            continue
                        if flow_type not in FLOW_TYPES:
                            error = "Choose a cash-flow treatment for every category."
                            break
                        submitted_rows.append(
                            {
                                "original_name": normalized_category(original_name) or "",
                                "name": name,
                                "flow_type": flow_type,
                                "required": name.casefold() == "transfer",
                            }
                        )
                if not error:
                    names_by_key = {
                        row["name"].casefold() for row in submitted_rows
                    }
                    if len(names_by_key) != len(submitted_rows):
                        error = "Each category needs a unique name."
                    transfer = next(
                        (
                            row
                            for row in submitted_rows
                            if row["name"].casefold() == "transfer"
                        ),
                        None,
                    )
                    if transfer:
                        transfer["name"] = "Transfer"
                        transfer["flow_type"] = "transfer"
                        transfer["required"] = True
                    else:
                        original_transfer = next(
                            (
                                row["original_name"]
                                for row in rows
                                if row["name"].casefold() == "transfer"
                            ),
                            "",
                        )
                        submitted_rows.append(
                            {
                                "original_name": original_transfer,
                                "name": "Transfer",
                                "flow_type": "transfer",
                                "required": True,
                            }
                        )
                    save_category_setup(connection, submitted_rows)
                    return redirect(next_url)
                rows = submitted_rows or rows

    context = page_context("settings")
    context.update(
        category_rows=rows,
        category_setup_complete=complete,
        flow_types=FLOW_TYPES,
        next_url=next_url,
        category_error=error,
    )
    return render_template("category_setup.html", **context)


def canonical_category(connection, name):
    return connection.execute(
        """
        SELECT name FROM (
            SELECT COALESCE(category_override, category) AS name FROM transactions
            UNION SELECT name FROM category_rules
            UNION SELECT category AS name FROM merchant_rules
        ) WHERE name = ? COLLATE NOCASE LIMIT 1
        """,
        (name,),
    ).fetchone()


def selected_category(connection, choice, new_name=None, new_flow_type=None):
    if choice == "":
        return True, None
    if choice == "__new__":
        name = normalized_category(new_name or "")
        if not name or new_flow_type not in FLOW_TYPES:
            return False, None
        existing = canonical_category(connection, name)
        if existing:
            name = existing[0]
        connection.execute(
            """
            INSERT INTO category_rules (name, flow_type) VALUES (?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name, new_flow_type),
        )
        return True, name
    name = normalized_category(choice)
    if not name:
        return False, None
    row = canonical_category(connection, name)
    return (True, row[0]) if row else (False, None)


def recurring_match(transaction):
    return "description", transaction["description"].strip()


@app.post("/api/transaction/<transaction_id>")
def update_transaction(transaction_id):
    category_flow_type = request.form.get("category_flow_type", "")
    edit_treatment = request.form.get("edit_category_treatment") == "on"
    new_category = request.form.get("category_choice") == "__new__"
    if (edit_treatment or new_category) and category_flow_type not in FLOW_TYPES:
        return transaction_cleanup_redirect()
    excluded = int(request.form.get("excluded") == "on")
    with db() as connection:
        transaction = connection.execute(
            """
            SELECT account_id, merchant, description,
                   COALESCE(category_override, category) AS category
            FROM transactions WHERE id = ?
            """,
            (transaction_id,),
        ).fetchone()
        if not transaction:
            return transaction_cleanup_redirect()
        valid, category = selected_category(
            connection,
            request.form.get("category_choice", ""),
            request.form.get("new_category"),
            category_flow_type,
        )
        if not valid:
            return transaction_cleanup_redirect()
        if category and edit_treatment:
            connection.execute(
                """
                INSERT INTO category_rules (name, flow_type) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET flow_type = excluded.flow_type
                """,
                (category, category_flow_type),
            )
        match_type, match_value = recurring_match(transaction)
        existing_rule = connection.execute(
            """
            SELECT id, category FROM merchant_rules
            WHERE account_id = ? AND match_type = ?
              AND match_value = ? COLLATE NOCASE
            """,
            (transaction["account_id"], match_type, match_value),
        ).fetchone()
        if request.form.get("remember_match") == "on":
            recurring_category = category or (
                existing_rule["category"] if existing_rule else transaction["category"]
            )
            connection.execute(
                """
                INSERT INTO merchant_rules (
                    account_id, match_type, match_value, category
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, match_type, match_value) DO UPDATE SET
                    category = excluded.category,
                    flow_type = NULL,
                    spending_override = NULL
                """,
                (
                    transaction["account_id"],
                    match_type,
                    match_value,
                    recurring_category,
                ),
            )
        elif existing_rule:
            connection.execute(
                "DELETE FROM merchant_rules WHERE id = ?",
                (existing_rule["id"],),
            )
        connection.execute(
            """
            UPDATE transactions
            SET category_override = ?, category_override_source = ?,
                flow_override = ?, spending_override = ?,
                excluded = ?
            WHERE id = ?
            """,
            (
                category,
                "user" if category else None,
                None,
                None,
                excluded,
                transaction_id,
            ),
        )
        dismiss_ai_reviews(connection, [transaction_id])
    return transaction_cleanup_redirect()


@app.post("/api/transaction/<transaction_id>/confirm-ai")
def confirm_ai_review(transaction_id):
    with db() as connection:
        review = load_ai_reviews(connection).get(transaction_id)
        transaction = connection.execute(
            f"SELECT {EFFECTIVE_CATEGORY_SQL} AS category, t.category_override_source "
            f"FROM transactions t {CATEGORY_RULE_JOIN} WHERE t.id = ?",
            (transaction_id,),
        ).fetchone()
        if (review and transaction
                and transaction["category_override_source"] != "user"
                and transaction["category"].casefold() == review["category"].casefold()):
            connection.execute(
                "UPDATE transactions SET category_override = ?, category_override_source = 'user' "
                "WHERE id = ?",
                (transaction["category"], transaction_id),
            )
        dismiss_ai_reviews(connection, [transaction_id])
    return transaction_cleanup_redirect()


@app.post("/api/transactions/bulk")
def bulk_update_transactions():
    transaction_ids = list(dict.fromkeys(request.form.getlist("transaction_ids")))
    action = request.form.get("action")
    if transaction_ids and action in {"exclude", "restore", "apply"}:
        with db() as connection:
            category = "__no_change__"
            if action == "apply":
                inclusion = request.form.get("inclusion", "__no_change__")
                if inclusion not in {"__no_change__", "include", "exclude"}:
                    return transaction_cleanup_redirect()
                choice = request.form.get("category_choice", "__no_change__")
                if choice != "__no_change__":
                    valid, category = selected_category(
                        connection,
                        choice,
                        request.form.get("new_category"),
                        request.form.get("new_category_flow_type"),
                    )
                    if not valid:
                        return transaction_cleanup_redirect()
            for start in range(0, len(transaction_ids), 500):
                batch = transaction_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                if action in {"exclude", "restore"}:
                    connection.execute(
                        f"UPDATE transactions SET excluded = ? WHERE id IN ({placeholders})",
                        [int(action == "exclude"), *batch],
                    )
                    continue
                assignments = []
                values = []
                if category != "__no_change__":
                    assignments.append("category_override = ?")
                    values.append(category)
                    assignments.append("category_override_source = ?")
                    values.append("user" if category else None)
                    assignments.extend(
                        ["flow_override = NULL", "spending_override = NULL"]
                    )
                if inclusion != "__no_change__":
                    assignments.append("excluded = ?")
                    values.append(int(inclusion == "exclude"))
                if assignments:
                    connection.execute(
                        f"UPDATE transactions SET {', '.join(assignments)} "
                        f"WHERE id IN ({placeholders})",
                        [*values, *batch],
                    )
            if category != "__no_change__":
                dismiss_ai_reviews(connection, transaction_ids)
    return transaction_cleanup_redirect()


def transaction_cleanup_redirect():
    transaction_view = request.form.get("return_view")
    return redirect(
        url_for(
            "transactions",
            account=request.form.get("account") or None,
            person=request.form.get("person") or None,
            date_from=request.form.get("date_from") or None,
            date_to=request.form.get("date_to") or None,
            q=request.form.get("return_q") or None,
            category=request.form.get("return_category") or None,
            exclude_category=request.form.getlist("return_excluded_category"),
            purpose=request.form.get("return_purpose") or None,
            view=transaction_view if transaction_view != "active" else None,
            sort=request.form.get("return_sort") or None,
            ai_review="1" if request.form.get("return_ai_review") == "1" else None,
        )
    )


@app.post("/api/local-ai/evaluation")
def evaluate_with_ollama():
    if not local_ai_enabled():
        return jsonify(error="Local AI assistance is turned off."), 403
    payload = request.get_json(silent=True) or {}
    categories = payload.get("categories") or []
    model = str(payload.get("model") or "qwen3.8:27b").strip()
    transaction_ids = payload.get("transaction_ids") or []
    if not isinstance(transaction_ids, list) or any(
        not isinstance(transaction_id, str) for transaction_id in transaction_ids
    ):
        return jsonify(error="Selected transactions were not valid."), 400
    transaction_ids = list(
        dict.fromkeys(
            transaction_id.strip()
            for transaction_id in transaction_ids
            if transaction_id.strip()
        )
    )
    if not OLLAMA_EVALUATION_LOCK.acquire(blocking=False):
        return jsonify(
            ok=True,
            running=True,
            report_url=url_for("ollama_evaluation_report"),
        )
    try:
        try:
            with db() as connection:
                if not category_setup_is_complete(connection):
                    return jsonify(
                        error="Set up category labels before running local categorization.",
                        setup_url=url_for(
                            "category_setup", next=url_for("transactions")
                        ),
                    ), 409
                if not categories:
                    categories = [
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT name FROM category_rules
                            ORDER BY 1 COLLATE NOCASE
                            """
                        )
                    ]
                prepared = prepare_evaluation(
                    connection,
                    categories,
                    model,
                    transaction_ids=transaction_ids,
                )
        except ValueError as error:
            return jsonify(error=str(error)), 400

        started = time.monotonic()
        details = []
        applied = 0
        result = evaluation_result(
            prepared, details, 0, status="running"
        )
        with db() as connection:
            save_ollama_result(connection, result)

        try:
            for rows in evaluation_batches(prepared):
                batch_details = classify_evaluation_rows(prepared, rows)
                next_details = [*details, *batch_details]
                with db() as connection:
                    batch_result = {"details": batch_details}
                    batch_applied = apply_categorized_suggestions(connection, batch_result)
                    progress = evaluation_result(
                        prepared, next_details, time.monotonic() - started,
                        status="running", applied_transaction_count=applied + batch_applied,
                    )
                    save_ollama_result(connection, progress)
                details = next_details
                applied += batch_applied

            result = evaluation_result(
                prepared,
                details,
                time.monotonic() - started,
                status="completed",
                applied_transaction_count=applied,
            )
            with db() as connection:
                create_recurring_category_rules(connection, result)
                applied += sum(item.get("rule_applied_transaction_count", 0) for item in details)
                result = evaluation_result(
                    prepared, details, time.monotonic() - started,
                    status="completed", applied_transaction_count=applied,
                )
                save_ollama_result(connection, result)
        except Exception:
            app.logger.exception("Local model categorization stopped early")
            message = (
                "The local model stopped before finishing. Completed groups "
                "were saved; run local categorization again to continue."
            )
            result = evaluation_result(
                prepared,
                details,
                time.monotonic() - started,
                status="interrupted",
                applied_transaction_count=applied,
                error=message,
            )
            with db() as connection:
                save_ollama_result(connection, result)
            return jsonify(
                ok=False,
                error=message,
                report_url=url_for("ollama_evaluation_report"),
            )
        return jsonify(ok=True, report_url=url_for("ollama_evaluation_report"))
    except Exception as error:
        app.logger.exception("Local model evaluation failed")
        return jsonify(error=str(error)), 500
    finally:
        OLLAMA_EVALUATION_LOCK.release()


def statement_accounts(connection):
    return [dict(row) for row in connection.execute(
        "SELECT a.id, a.name, a.mask, a.type, c.owner_name, (c.access_token = '') AS unlinked FROM accounts a "
        "JOIN connections c ON c.id = a.connection_id WHERE a.type IN ('depository', 'credit') "
        "ORDER BY c.owner_name, a.name"
    )]


@app.route('/history-accounts/new', methods=['GET', 'POST'])
def add_history_account():
    error = None
    if request.method == 'POST':
        values = {key: request.form.get(key, '').strip()
                  for key in ('owner_name', 'institution', 'name', 'type', 'mask')}
        if any(not values[key] or len(values[key]) > limit
               for key, limit in (('owner_name', 40), ('institution', 100), ('name', 100))):
            error = 'Enter an owner, institution, and account name within the shown limits.'
        elif values['type'] not in {'depository', 'credit'}:
            error = 'Choose a bank account or credit card.'
        elif values['mask'] and not re.fullmatch(r'[0-9]{4}', values['mask']):
            error = 'Enter only the last four digits, or leave them blank.'
        else:
            with db() as connection:
                # An empty token identifies a local-only group, never a Plaid connection.
                group = connection.execute(
                    "SELECT id FROM connections WHERE access_token = '' "
                    "AND owner_name = ? COLLATE NOCASE AND institution = ? COLLATE NOCASE",
                    (values['owner_name'], values['institution']),
                ).fetchone()
                if group:
                    group_id = group['id']
                else:
                    group_id = connection.execute(
                        "INSERT INTO connections (owner_name, institution, access_token) VALUES (?, ?, '')",
                        (values['owner_name'], values['institution']),
                    ).lastrowid
                existing = connection.execute(
                    "SELECT id FROM accounts WHERE connection_id = ? AND name = ? COLLATE NOCASE "
                    "AND type = ? AND COALESCE(mask, '') = ?",
                    (group_id, values['name'], values['type'], values['mask']),
                ).fetchone()
                account_id = existing['id'] if existing else 'manual:' + secrets.token_hex(16)
                if not existing:
                    connection.execute(
                        "INSERT INTO accounts (id, connection_id, institution, name, mask, type, "
                        "cash_flow_role, spending_enabled) VALUES (?, ?, ?, ?, ?, ?, 'cash_flow', 1)",
                        (account_id, group_id, values['institution'], values['name'], values['mask'], values['type']),
                    )
            draft_token = request.args.get('draft', '')
            if re.fullmatch(r'[0-9a-f]{32}', draft_token):
                return redirect(url_for('review_statement', token=draft_token))
            return redirect(url_for('statement_import', account=account_id, account_added='1'))
    return render_template('history_account.html', error=error, **page_context('transactions'))


def statement_context(connection):
    drafts = []
    for row in connection.execute("SELECT key, value FROM settings WHERE key LIKE ?", (statements.DRAFT_PREFIX + '%',)):
        draft = json.loads(row['value'])
        drafts.append({'id': row['key'][len(statements.DRAFT_PREFIX):],
                       'account_name': draft['account_name'], 'filename': draft.get('filename', ''),
                       'start': draft['start'], 'end': draft['end']})
    return dict(accounts=statement_accounts(connection), drafts=drafts,
                history=statements.read_setting(connection, statements.HISTORY_KEY, []),
                local_ai_enabled=local_ai_enabled(connection), max_batch_files=statements.MAX_BATCH_FILES,
                max_open_drafts=statements.MAX_OPEN_DRAFTS)


def refresh_statement_rows(connection, draft):
    draft['start'], draft['end'] = statements.date_range(draft['rows'])
    accounts = {account['id']: account for account in statement_accounts(connection)}
    if 'account_groups' not in draft:
        draft['account_groups'] = statements.group_accounts(draft['rows'], list(accounts.values()), draft.get('account_id', ''))
    groups = {group['id']: group for group in draft['account_groups']}
    valid_by_account = {}
    for index, row in enumerate(draft['rows']):
        row['duplicate'] = ''
        row['account_id'] = row.get('account_override') or groups.get(row.get('account_group'), {}).get('account_id', '')
        row['account_name'] = accounts.get(row['account_id'], {}).get('name', '')
        try:
            if row['account_id'] not in accounts:
                raise statements.StatementError('Choose the account for this transaction before importing.')
            valid = statements.validate_row(row)
            valid_by_account.setdefault(row['account_id'], []).append((index, valid))
            row['error'] = ''
        except statements.StatementError as error:
            row['error'] = str(error)
    for account_id, entries in valid_by_account.items():
        duplicates = statements.find_duplicates(connection, account_id, [row for _, row in entries])
        for (index, _), duplicate in zip(entries, duplicates):
            draft['rows'][index]['duplicate'] = duplicate
    imported_ids = {transaction_id for entry in statements.read_setting(connection, statements.HISTORY_KEY, []) for transaction_id in entry['transaction_ids']}
    for row in draft['rows']:
        if row['id'] in imported_ids or connection.execute("SELECT 1 FROM transactions WHERE id = ?", (row['id'],)).fetchone():
            row['duplicate'] = 'This statement row was already imported'
            row['already_imported'] = True
        else:
            row['already_imported'] = False


@app.errorhandler(413)
def oversized_upload(error):
    return 'The upload is too large. Choose a statement no larger than 20 MB and try again.', 413


@app.route('/statement-import', methods=['GET', 'POST'])
def statement_import():
    global vault_last_activity
    error = None
    error_code = 'read_failed'
    wants_json = request.accept_mimetypes.best == 'application/json'
    if request.method == 'GET' and wants_json:
        return jsonify(csrf_token=csrf_token())
    if request.method == 'POST':
        with db() as connection:
            accounts = {row['id']: row for row in statement_accounts(connection)}
            enabled = local_ai_enabled(connection)
            draft_count = connection.execute("SELECT COUNT(*) FROM settings WHERE key LIKE ?", (statements.DRAFT_PREFIX + '%',)).fetchone()[0]
        if not enabled:
            error = 'Enable local AI in Settings before reading a statement.'
            error_code = 'disabled'
        elif len(request.files.getlist('statement')) > 1:
            error = 'Enable JavaScript to read multiple statements in sequence, or choose one file.'
        elif not OLLAMA_EVALUATION_LOCK.acquire(blocking=False):
            error = 'The local model is already busy. Wait for the current run to finish.'
            error_code = 'busy'
        else:
            try:
                account = accounts.get(request.form.get('account_id'))
                if request.form.get('account_id') and not account:
                    raise statements.StatementError('Choose an available account or let the reader identify accounts.')
                if request.form.get('usd') != 'on':
                    raise statements.StatementError('This first version supports US-dollar statements only. Confirm the statement currency.')
                upload = request.files.get('statement')
                if not upload:
                    raise statements.StatementError('Choose a PDF, PNG, or JPEG statement.')
                data = upload.read(statements.MAX_UPLOAD_BYTES + 1)
                digest = hashlib.sha256(data).hexdigest()
                with db() as connection:
                    existing = next((dict(row) for row in connection.execute(
                        "SELECT key, value FROM settings WHERE key LIKE ?", (statements.DRAFT_PREFIX + '%',))
                        if json.loads(row['value']).get('digest') == digest), None)
                if existing:
                    review_url = url_for('review_statement', token=existing['key'][len(statements.DRAFT_PREFIX):])
                    return jsonify(review_url=review_url, reused=True) if wants_json else redirect(review_url)
                if draft_count >= statements.MAX_OPEN_DRAFTS:
                    error_code = 'capacity'
                    raise statements.StatementError('You have 30 open drafts. Review or discard some, then continue this batch.')
                pages = statements.document_pages(data)
                del data
                draft = {'account_id': account['id'] if account else '', 'account_name': 'Statement accounts',
                         'filename': (upload.filename or 'Statement').replace('\\', '/').rsplit('/', 1)[-1][:120],
                         'start': '', 'end': '', 'digest': digest,
                         'created_at': datetime.now(timezone.utc).isoformat(), 'rows': [], 'warnings': [],
                         'pages': [{'number': page['number'], 'image': page['image']} for page in pages]}
                if request.form.get('read_images') == 'on':
                    for page in pages:
                        page['text'] = ''
                date_context = statements.date_context(pages)
                for page in pages:
                    result = statements.extract_page(page, account['type'] if account else 'unknown', date_context)
                    for key in ('statement_start', 'statement_end'):
                        if result.get(key):
                            date_context[key] = result[key]
                    draft['warnings'].extend(f"Page {page['number']}: {warning}" for warning in result['warnings'])
                    for index, row in enumerate(result['transactions']):
                        draft['rows'].append({**row, 'page': page['number'],
                            'id': statements.import_identity('', digest, page['number'], index)})
                    if result['transactions']:
                        last_row = result['transactions'][-1]
                        date_context['previous_page_last_account'] = {
                            key: last_row.get(key, '') for key in ('account_label', 'account_last4', 'account_type')}
                    if len(draft['rows']) > statements.MAX_ROWS:
                        raise statements.StatementError('This statement exceeds the limit of 2,500 transaction rows. No transactions were added.')
                draft['account_groups'] = statements.group_accounts(draft['rows'], list(accounts.values()), draft['account_id'])
                token = secrets.token_hex(16)
                with db() as connection:
                    refresh_statement_rows(connection, draft)
                    for row in draft['rows']:
                        try:
                            statements.validate_row(row)
                            row['selected'] = not row['duplicate']
                        except statements.StatementError:
                            row['selected'] = False
                    statements.write_setting(connection, statements.DRAFT_PREFIX + token, draft)
                review_url = url_for('review_statement', token=token)
                vault_last_activity = time.monotonic()
                return jsonify(review_url=review_url, reused=False) if wants_json else redirect(review_url)
            except statements.StatementError as failure:
                error = str(failure)
            except Exception:
                error = 'The statement could not be read. No transactions were added. Try a clearer document or check that Ollama is running.'
            finally:
                OLLAMA_EVALUATION_LOCK.release()
    if request.method == 'POST' and wants_json:
        return jsonify(error=error, code=error_code), 409 if error_code in {'busy', 'capacity'} else 400
    context = page_context('transactions')
    with db() as connection:
        context.update(statement_context(connection))
    return render_template('statement_import.html', error=error, **context)


def classify_statement_import(import_key):
    """Save each classification batch separately; model failures never undo an import."""
    with db() as connection:
        history = statements.read_setting(connection, statements.HISTORY_KEY, [])
        entries = [entry for entry in history if entry['id'] == import_key or entry['id'].startswith(import_key + ':')]
        transaction_ids = [transaction_id for entry in entries for transaction_id in entry['transaction_ids']]
        if not transaction_ids:
            return
        match_import_transfers(connection, transaction_ids)
        enabled = local_ai_enabled(connection)
        categories = [row[0] for row in connection.execute('SELECT name FROM category_rules ORDER BY name')]
    status = 'complete'
    acquired = enabled and OLLAMA_EVALUATION_LOCK.acquire(blocking=False)
    if not enabled:
        status = 'disabled'
    elif not acquired:
        status = 'busy'
    else:
        try:
            with db() as connection:
                eligible = eligible_import_ids(connection, transaction_ids)
                prepared = prepare_evaluation(connection, categories, statements.MODEL, transaction_ids=eligible) if eligible else None
            if prepared:
                # Import classification must never recategorize user choices or saved rules.
                prepared['targeted'] = False
                for group in prepared['groups']:
                    # Verified pairs were handled above. Unmatched amounts are not enough to infer a transfer.
                    group['all_have_transfer_match'] = False
                for rows in evaluation_batches(prepared):
                    details = classify_evaluation_rows(prepared, rows)
                    with db() as connection:
                        still_eligible = set(eligible_import_ids(connection, transaction_ids))
                        for item in details:
                            item['allow_recategorization'] = False
                            item['transaction_ids'] = [value for value in item['transaction_ids'] if value in still_eligible]
                        apply_categorized_suggestions(connection, {'details': details})
        except Exception:
            status = 'interrupted'
        finally:
            OLLAMA_EVALUATION_LOCK.release()
    with db() as connection:
        remaining = set(eligible_import_ids(connection, transaction_ids))
        history = statements.read_setting(connection, statements.HISTORY_KEY, [])
        for entry in history:
            if entry['id'] == import_key or entry['id'].startswith(import_key + ':'):
                count = sum(value in remaining for value in entry['transaction_ids'])
                entry['classification'] = {'status': status if count else 'complete', 'remaining': count}
        statements.write_setting(connection, statements.HISTORY_KEY, history)


@app.post('/statement-import/history/<token>/classify')
def retry_statement_classification(token):
    classify_statement_import(token)
    return redirect(url_for('statement_import'))


@app.route('/statement-import/<token>', methods=['GET', 'POST'])
def review_statement(token):
    error = None
    imported_count = 0
    key = statements.DRAFT_PREFIX + token
    with db() as connection:
        draft = statements.read_setting(connection, key)
        if not draft:
            return redirect(url_for('statement_import'))
        refresh_statement_rows(connection, draft)
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'discard':
                connection.execute('DELETE FROM settings WHERE key = ?', (key,))
                return redirect(url_for('statement_import'))
            for group in draft['account_groups']:
                group['account_id'] = request.form.get('group_account_' + group['id'], group['account_id'])
            selected = set(request.form.getlist('include'))
            for index, row in enumerate(draft['rows']):
                for field in ['date', 'description', 'amount', 'direction']:
                    row[field] = request.form.get(f'{field}_{index}', row[field])[:300]
                row['account_override'] = request.form.get(f'account_override_{index}', row.get('account_override', ''))
                row['selected'] = str(index) in selected
            refresh_statement_rows(connection, draft)
            if action == 'add_row' and len(draft['rows']) < statements.MAX_ROWS:
                draft['rows'].append({'id': 'statement:' + secrets.token_hex(32), 'page': draft['pages'][0]['number'],
                    'date': '', 'description': '', 'amount': '', 'direction': '', 'evidence': 'Manually added during review',
                    'account_group': '', 'account_override': '', 'account_id': '',
                    'selected': True, 'duplicate': '', 'error': 'Complete this row before importing.', 'already_imported': False})
            elif action == 'confirm':
                chosen = [row for row in draft['rows'] if row['selected']]
                if not chosen:
                    error = 'Select at least one transaction to add.'
                elif request.form.get('reviewed') != 'on':
                    error = 'Confirm that you checked the account, dates, directions, and amounts against the statement.'
                elif any(row['already_imported'] for row in chosen):
                    error = 'Some selected statement rows were already imported. Deselect them to avoid adding them twice.'
                elif any(row['error'] for row in chosen):
                    error = 'Correct the highlighted selected rows before adding transactions.'
                elif any(row['duplicate'] for row in chosen) and request.form.get('allow_duplicates') != 'on':
                    error = 'Deselect possible duplicates, or explicitly confirm they are separate transactions.'
                else:
                    for row in chosen:
                        validated = statements.validate_row(row)
                        connection.execute(
                            "INSERT INTO transactions (id, account_id, amount, currency, description, pending, transacted_at, category) "
                            "VALUES (?, ?, ?, 'USD', ?, 0, ?, 'Uncategorized')",
                            (row['id'], row['account_id'], validated['amount'], validated['description'], validated['date']),
                        )
                    history = statements.read_setting(connection, statements.HISTORY_KEY, [])
                    accounts = {account['id']: account for account in statement_accounts(connection)}
                    for account_id in dict.fromkeys(row['account_id'] for row in chosen):
                        account_rows = [row for row in chosen if row['account_id'] == account_id]
                        start, end = statements.date_range(account_rows)
                        account = accounts[account_id]
                        history.insert(0, {'id': token + ':' + account_id, 'account_id': account_id,
                            'account_name': account['owner_name'] + ' · ' + account['name'],
                            'start': start, 'end': end, 'count': len(account_rows),
                            'transaction_ids': [row['id'] for row in account_rows],
                            'created_at': datetime.now(timezone.utc).isoformat()})
                    statements.write_setting(connection, statements.HISTORY_KEY, history)
                    connection.execute('DELETE FROM settings WHERE key = ?', (key,))
                    imported_count = len(chosen)
            if not imported_count:
                statements.write_setting(connection, key, draft)
        refresh_statement_rows(connection, draft)
    if imported_count:
        try:
            classify_statement_import(token)
        except Exception:
            # The confirmed rows are already persisted; allow a safe retry from import history.
            pass
        return redirect(url_for('statement_import', imported=imported_count))
    return render_template('statement_review.html', draft=draft, token=token, error=error, **page_context('transactions'))


@app.get('/statement-import/<token>/page/<int:number>')
def statement_page(token, number):
    with db() as connection:
        draft = statements.read_setting(connection, statements.DRAFT_PREFIX + token)
    if not draft:
        abort(404)
    page = next((page for page in draft['pages'] if page['number'] == number), None)
    if not page:
        abort(404)
    return Response(base64.b64decode(page['image']), mimetype='image/jpeg')


@app.post('/statement-import/history/<token>/exclude')
def exclude_statement_import(token):
    with db() as connection:
        history = statements.read_setting(connection, statements.HISTORY_KEY, [])
        entry = next((entry for entry in history if entry['id'] == token), None)
        if entry:
            connection.executemany('UPDATE transactions SET excluded = 1 WHERE id = ?',
                                   [(transaction_id,) for transaction_id in entry['transaction_ids']])
            entry['excluded'] = True
            statements.write_setting(connection, statements.HISTORY_KEY, history)
    return redirect(url_for('statement_import'))


@app.get("/local-ai/evaluation")
def ollama_evaluation_report():
    if not local_ai_enabled():
        abort(404)
    with db() as connection:
        result = load_ollama_result(connection)
        if (
            result
            and result.get("status") == "running"
            and not OLLAMA_EVALUATION_LOCK.locked()
        ):
            result["status"] = "interrupted"
            result["error"] = (
                "The app restarted before this run finished. Completed groups "
                "were saved; run local categorization again to continue."
            )
            save_ollama_result(connection, result)
    if not result:
        return redirect(url_for("transactions"))
    context = page_context("transactions")
    context.update(result=result, flow_types=FLOW_TYPES)
    return render_template("llm_evaluation.html", **context)


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=APP_PORT,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
