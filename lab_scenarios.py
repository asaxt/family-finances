from datetime import date, datetime, timedelta, timezone


LAB_SCENARIOS = {
    "checking_debit": {
        "name": "Checking only · debit purchases",
        "description": "Paychecks and everyday debit-card purchases in one checking account.",
        "accounts": ("checking",),
        "groups": ("checking_income", "checking_debit"),
    },
    "checking_direct": {
        "name": "Checking only · direct payments",
        "description": "Paychecks plus rent, utilities, and other ACH-style payments.",
        "accounts": ("checking",),
        "groups": ("checking_income", "checking_direct"),
    },
    "checking_mixed": {
        "name": "Checking only · mixed spending",
        "description": "Debit purchases and direct payments from the same checking account.",
        "accounts": ("checking",),
        "groups": ("checking_income", "checking_debit", "checking_direct"),
    },
    "savings_direct": {
        "name": "Savings only · direct payments",
        "description": "Deposits and unusual direct withdrawals from a savings account.",
        "accounts": ("savings",),
        "groups": ("savings_income", "savings_direct"),
    },
    "checking_savings": {
        "name": "Checking + savings",
        "description": "Checking activity, savings transfers, and a direct savings withdrawal.",
        "accounts": ("checking", "savings"),
        "groups": (
            "checking_income",
            "checking_debit",
            "checking_direct",
            "bank_transfers",
            "savings_direct",
        ),
    },
    "checking_credit": {
        "name": "Checking + credit card · card-first",
        "description": "Direct checking payments, card purchases, and offsetting card payments.",
        "accounts": ("checking", "card"),
        "groups": (
            "checking_income",
            "checking_direct",
            "card_purchases",
            "card_payment_checking",
        ),
    },
    "checking_credit_mixed": {
        "name": "Checking + credit card · mixed purchasing",
        "description": "Card purchases plus occasional debit and direct checking purchases.",
        "accounts": ("checking", "card"),
        "groups": (
            "checking_income",
            "checking_debit",
            "checking_direct",
            "card_purchases",
            "card_payment_checking",
        ),
    },
    "savings_credit": {
        "name": "Savings + credit card",
        "description": "Card purchases with the card payment drawn directly from savings.",
        "accounts": ("savings", "card"),
        "groups": (
            "savings_income",
            "savings_direct",
            "card_purchases",
            "card_payment_savings",
        ),
    },
    "checking_savings_credit": {
        "name": "Checking + savings + credit card",
        "description": "A full mix of bank transfers, debit activity, direct payments, and card spending.",
        "accounts": ("checking", "savings", "card"),
        "groups": (
            "checking_income",
            "checking_debit",
            "checking_direct",
            "bank_transfers",
            "savings_direct",
            "card_purchases",
            "card_payment_checking",
        ),
    },
    "credit_only": {
        "name": "Credit card only",
        "description": "Card purchases, a genuine refund, and payment credits without the funding bank.",
        "accounts": ("card",),
        "groups": ("card_purchases", "card_payment_unlinked"),
    },
}


CONNECTIONS = {
    "bank": (101, "synthetic-bank", "Lab Household", "Synthetic Community Bank"),
    "card": (202, "synthetic-card", "Lab Household", "Synthetic Card Company"),
}


ACCOUNTS = {
    "checking": {
        "connection": "bank",
        "id": "lab-checking",
        "institution": "Synthetic Community Bank",
        "name": "Everyday Checking",
        "mask": "1101",
        "type": "depository",
        "subtype": "checking",
        "current_balance": 482_500,
        "available_balance": 472_500,
        "cash_flow_role": "cash_flow",
        "spending_enabled": 0,
    },
    "savings": {
        "connection": "bank",
        "id": "lab-savings",
        "institution": "Synthetic Community Bank",
        "name": "Household Savings",
        "mask": "2202",
        "type": "depository",
        "subtype": "savings",
        "current_balance": 1_275_000,
        "available_balance": 1_275_000,
        "cash_flow_role": "cash_flow",
        "spending_enabled": 0,
    },
    "card": {
        "connection": "card",
        "id": "lab-card",
        "institution": "Synthetic Card Company",
        "name": "Everyday Rewards Card",
        "mask": "3303",
        "type": "credit",
        "subtype": "credit card",
        "current_balance": 196_400,
        "available_balance": 803_600,
        "cash_flow_role": "credit_card",
        "spending_enabled": 1,
    },
}


def _transaction(
    transaction_id,
    account_id,
    amount,
    description,
    merchant,
    days_ago,
    category,
    today,
):
    return (
        transaction_id,
        account_id,
        amount,
        "USD",
        description,
        merchant,
        0,
        (today - timedelta(days=days_ago)).isoformat(),
        category,
        0,
    )


def _transaction_groups(today):
    tx = lambda *values: _transaction(*values, today)
    return {
        "checking_income": [
            tx("lab-paycheck-current", "lab-checking", -320_000, "PAYROLL DIRECT DEP", "Example Employer", 3, "Income"),
            tx("lab-paycheck-prior", "lab-checking", -320_000, "PAYROLL DIRECT DEP", "Example Employer", 33, "Income"),
            tx("lab-venmo-in", "lab-checking", -4_200, "VENMO CASHOUT", "Venmo", 6, "Venmo"),
        ],
        "checking_debit": [
            tx("lab-debit-grocery", "lab-checking", 6_450, "DEBIT PURCHASE FRESH MARKET", "Fresh Market", 1, "Food And Drink"),
            tx("lab-debit-coffee", "lab-checking", 875, "DEBIT PURCHASE CORNER CAFE", "Corner Cafe", 4, "Food And Drink"),
            tx("lab-debit-grocery-prior", "lab-checking", 5_980, "DEBIT PURCHASE FRESH MARKET", "Fresh Market", 31, "Food And Drink"),
        ],
        "checking_direct": [
            tx("lab-checking-rent", "lab-checking", 145_000, "ACH RENT PAYMENT", "Example Property", 2, "Housing"),
            tx("lab-checking-utility", "lab-checking", 9_250, "AUTOPAY ELECTRIC BILL", "Example Electric", 5, "Utilities"),
            tx("lab-checking-insurance", "lab-checking", 12_800, "ACH INSURANCE PREMIUM", "Example Insurance", 7, "Insurance"),
            tx("lab-checking-rent-prior", "lab-checking", 145_000, "ACH RENT PAYMENT", "Example Property", 32, "Housing"),
        ],
        "savings_income": [
            tx("lab-savings-deposit", "lab-savings", -75_000, "EXTERNAL DEPOSIT", None, 3, "Other"),
            tx("lab-savings-interest", "lab-savings", -425, "MONTHLY INTEREST", "Synthetic Community Bank", 5, "Other"),
        ],
        "savings_direct": [
            tx("lab-savings-repair", "lab-savings", 38_500, "ACH HOME REPAIR", "Example Home Repair", 6, "Home Improvement"),
        ],
        "bank_transfers": [
            tx("lab-transfer-checking-out", "lab-checking", 50_000, "TRANSFER TO SAVINGS", None, 4, "Transfer Out"),
            tx("lab-transfer-savings-in", "lab-savings", -50_000, "TRANSFER FROM CHECKING", None, 4, "Transfer In"),
        ],
        "card_purchases": [
            tx("lab-card-grocery", "lab-card", 7_250, "FRESH MARKET", "Fresh Market", 1, "Food And Drink"),
            tx("lab-card-travel", "lab-card", 28_400, "EXAMPLE AIRLINES", "Example Airlines", 3, "Travel"),
            tx("lab-card-streaming", "lab-card", 1_899, "STREAMING SERVICE", "Example Streaming", 5, "Entertainment"),
            tx("lab-card-grocery-prior", "lab-card", 6_890, "FRESH MARKET", "Fresh Market", 31, "Food And Drink"),
            tx("lab-card-refund", "lab-card", -3_200, "RETURN EXAMPLE SHOP", "Example Shop", 2, "Shopping"),
        ],
        "card_payment_checking": [
            tx("lab-card-payment-bank", "lab-checking", 86_000, "PAYMENT TO EVERYDAY REWARDS", "Synthetic Card Company", 1, "Loan Payments"),
            tx("lab-card-payment-card", "lab-card", -86_000, "PAYMENT RECEIVED - THANK YOU", "Synthetic Community Bank", 1, "Loan Payments"),
            tx("lab-card-payment-bank-ambiguous", "lab-checking", 31_500, "AUTOPAY TO CARD", "Synthetic Card Company", 6, "Loan Payments"),
            tx("lab-card-payment-card-ambiguous", "lab-card", -31_500, "AUTOMATIC PAYMENT - THANK YOU", None, 6, "Other"),
        ],
        "card_payment_savings": [
            tx("lab-card-payment-savings", "lab-savings", 86_000, "PAYMENT TO EVERYDAY REWARDS", "Synthetic Card Company", 1, "Loan Payments"),
            tx("lab-card-payment-card", "lab-card", -86_000, "PAYMENT RECEIVED - THANK YOU", "Synthetic Community Bank", 1, "Loan Payments"),
            tx("lab-card-payment-savings-ambiguous", "lab-savings", 31_500, "AUTOPAY TO CARD", "Synthetic Card Company", 6, "Loan Payments"),
            tx("lab-card-payment-card-ambiguous", "lab-card", -31_500, "AUTOMATIC PAYMENT - THANK YOU", None, 6, "Other"),
        ],
        "card_payment_unlinked": [
            tx("lab-card-payment-card", "lab-card", -86_000, "PAYMENT RECEIVED - THANK YOU", "External Bank", 1, "Loan Payments"),
            tx("lab-card-payment-card-ambiguous", "lab-card", -31_500, "AUTOMATIC PAYMENT - THANK YOU", None, 6, "Other"),
        ],
    }


def load_lab_scenario(connection, scenario_key, *, today=None):
    scenario = LAB_SCENARIOS.get(scenario_key)
    if scenario is None:
        raise ValueError("Unknown lab scenario.")

    today = today or date.today()
    account_keys = scenario["accounts"]
    connection_keys = dict.fromkeys(
        ACCOUNTS[account_key]["connection"] for account_key in account_keys
    )

    connection.execute("DELETE FROM savings_snapshots")
    connection.execute("DELETE FROM manual_accounts")
    connection.execute("DELETE FROM merchant_rules")
    connection.execute("DELETE FROM category_rules")
    connection.execute("DELETE FROM transactions")
    connection.execute("DELETE FROM accounts")
    connection.execute("DELETE FROM connections")
    connection.execute(
        "DELETE FROM settings WHERE key IN ('plaid_client_id', 'plaid_secret', 'plaid_product_audit', 'lab_scenario')"
    )

    checked_at = datetime.now(timezone.utc).isoformat()
    for connection_key in connection_keys:
        connection_id, item_id, owner_name, institution = CONNECTIONS[connection_key]
        connection.execute(
            """
            INSERT INTO connections (
                id, plaid_item_id, owner_name, institution, access_token,
                last_synced_at
            ) VALUES (?, ?, ?, ?, 'synthetic-disabled-token', ?)
            """,
            (connection_id, item_id, owner_name, institution, checked_at),
        )

    for account_key in account_keys:
        account = ACCOUNTS[account_key]
        connection.execute(
            """
            INSERT INTO accounts (
                id, connection_id, institution, name, mask, type, subtype,
                current_balance, available_balance, balance_updated_at,
                cash_flow_role, spending_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account["id"],
                CONNECTIONS[account["connection"]][0],
                account["institution"],
                account["name"],
                account["mask"],
                account["type"],
                account["subtype"],
                account["current_balance"],
                account["available_balance"],
                checked_at,
                account["cash_flow_role"],
                account["spending_enabled"],
            ),
        )

    groups = _transaction_groups(today)
    transactions = [
        transaction
        for group_name in scenario["groups"]
        for transaction in groups[group_name]
    ]
    connection.executemany(
        """
        INSERT INTO transactions (
            id, account_id, amount, currency, description, merchant, pending,
            transacted_at, category, excluded
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        transactions,
    )
    connection.execute(
        "INSERT INTO settings (key, value) VALUES ('lab_scenario', ?)",
        (scenario_key,),
    )
    return {
        "scenario": scenario_key,
        "accounts": len(account_keys),
        "transactions": len(transactions),
    }
