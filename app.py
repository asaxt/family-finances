import os
import json
import calendar
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
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
    DEFAULT_OVERVIEW_LOOKBACK_DAYS,
    MAX_OVERVIEW_LOOKBACK_DAYS,
    category_details,
    long_term_trends,
    month_label,
    rolling_spending_summary,
    spending_summary,
    transaction_list,
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
app = Flask(__name__)
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
    vault_last_activity = time.monotonic()


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
            vault_last_activity = time.monotonic()
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("overview"))
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
                "SELECT id, access_token FROM connections ORDER BY id"
            )
        ]
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
                GROUP BY c.id
                ORDER BY c.created_at, c.id
                """
            )
        ]


def save_transaction(connection, transaction):
    category = "Other"
    if transaction.personal_finance_category:
        category = transaction.personal_finance_category.primary.replace("_", " ").title()
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
            int(
                category.startswith("Transfer")
                or category in {"Loan Payments", "Loan Disbursements"}
            ),
        ),
    )


def save_account(connection, account, connection_id, institution, checked_at):
    current = account.balances.current
    current_balance = (
        int(
            (Decimal(str(current)) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if current is not None
        else None
    )
    connection.execute(
        """
        INSERT INTO accounts (
            id, connection_id, institution, name, mask, type,
            current_balance, balance_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            connection_id = excluded.connection_id,
            institution = excluded.institution,
            name = excluded.name,
            mask = excluded.mask,
            type = excluded.type,
            current_balance = excluded.current_balance,
            balance_updated_at = excluded.balance_updated_at
        """,
        (
            account.account_id,
            connection_id,
            institution,
            account.name,
            account.mask,
            account.type.value,
            current_balance,
            checked_at,
        ),
    )


def sync_connection(item):
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
                "SELECT * FROM connections ORDER BY id"
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
                SELECT a.*, c.owner_name
                FROM accounts a
                JOIN connections c ON c.id = a.connection_id
                ORDER BY c.owner_name, a.name
                """
            )
        ]
        profiles = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, owner_name, institution,
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
        "connected": bool(profiles),
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
        context["recent"] = transaction_list(
            connection,
            account_id=context["account_id"],
            connection_id=context["connection_id"],
            date_from=context["summary"]["date_from"],
            date_to=context["summary"]["date_to"],
            limit=8,
        )[:8]
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
    return render_template("overview.html", **context)


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
    query = request.args.get("q") or None
    transaction_view = request.args.get("view") or (
        "all" if request.args.get("excluded") == "1" else "active"
    )
    if transaction_view not in {"active", "excluded", "all"}:
        transaction_view = "active"
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
        )
        context["category_options"] = [
            row["name"]
            for row in connection.execute(
                """
                SELECT DISTINCT COALESCE(category_override, category) AS name
                FROM transactions ORDER BY name
                """
            )
        ]
    context.update(
        selected_category=category,
        search_query=query or "",
        transaction_view=transaction_view,
        date_from=date_from or "",
        date_to=date_to or "",
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
    context.update(
        plaid_client_id=client_id,
        plaid_configured=bool(client_id and plaid_secret),
        plaid_product_status=plaid_product_status(),
        settings_saved=request.args.get("saved"),
        settings_error=request.args.get("error"),
    )
    return render_template("settings.html", **context)


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
    if not 1 <= lookback_days <= MAX_OVERVIEW_LOOKBACK_DAYS:
        redirect_arguments["error"] = "lookback"
        return redirect(url_for("overview", **redirect_arguments))
    save_setting("overview_lookback_days", str(lookback_days))
    return redirect(url_for("overview", **redirect_arguments))


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
                "SELECT access_token FROM connections WHERE id = ?",
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
                    "SELECT transactions_update_status FROM connections"
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


@app.post("/api/budget")
def save_budget():
    month = request.form["month"]
    category = request.form["category"].strip()
    amount = round(float(request.form["amount"] or 0) * 100)
    with db() as connection:
        if amount > 0:
            connection.execute(
                """
                INSERT INTO budgets (month, category, amount) VALUES (?, ?, ?)
                ON CONFLICT(month, category) DO UPDATE SET amount = excluded.amount
                """,
                (month, category, amount),
            )
        else:
            connection.execute(
                "DELETE FROM budgets WHERE month = ? AND category = ?",
                (month, category),
            )
    return redirect(
        url_for(
            "categories",
            month=month,
            account=request.form.get("account") or None,
            person=request.form.get("person") or None,
        )
    )


@app.post("/api/transaction/<transaction_id>")
def update_transaction(transaction_id):
    category = request.form.get("category", "").strip() or None
    excluded = int(request.form.get("excluded") == "on")
    with db() as connection:
        connection.execute(
            """
            UPDATE transactions
            SET category_override = ?, excluded = ?
            WHERE id = ?
            """,
            (category, excluded, transaction_id),
        )
    return transaction_cleanup_redirect()


@app.post("/api/transactions/bulk")
def bulk_update_transactions():
    transaction_ids = list(dict.fromkeys(request.form.getlist("transaction_ids")))
    action = request.form.get("action")
    if transaction_ids and action in {"exclude", "restore"}:
        with db() as connection:
            for start in range(0, len(transaction_ids), 500):
                batch = transaction_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(
                    f"UPDATE transactions SET excluded = ? WHERE id IN ({placeholders})",
                    [int(action == "exclude"), *batch],
                )
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
            view=transaction_view if transaction_view != "active" else None,
        )
    )


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=APP_PORT,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
