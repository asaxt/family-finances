import sqlite3
import unittest
from datetime import date

from analytics import (
    cash_flow_summary,
    category_details,
    daily_trends,
    long_term_trends,
    rolling_spending_summary,
    spending_summary,
    transaction_list,
)
from schema import create_schema


class CashFlowAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        self.connection.executemany(
            """
            INSERT INTO connections (id, owner_name, institution, access_token)
            VALUES (?, 'Household', ?, ?)
            """,
            ((1, "Card Bank", "card-token"), (2, "Cash Bank", "cash-token")),
        )
        self.connection.executemany(
            """
            INSERT INTO accounts (
                id, connection_id, institution, name, mask, type, subtype
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ("card", 1, "Card Bank", "Credit Card", "1111", "credit", "credit card"),
                ("checking", 2, "Cash Bank", "Checking", "2222", "depository", "checking"),
                ("savings", 2, "Cash Bank", "Savings", "3333", "depository", "savings"),
            ),
        )

    def tearDown(self):
        self.connection.close()

    def add(self, transaction_id, account_id, amount, category, transacted_at, *, excluded=0, override=None):
        self.connection.execute(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded, flow_override
            ) VALUES (?, ?, ?, 'USD', ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                account_id,
                amount,
                transaction_id,
                transaction_id,
                transacted_at,
                category,
                excluded,
                override,
            ),
        )

    def test_income_spending_and_internal_transfers_remain_separate(self):
        self.connection.execute(
            "INSERT INTO category_rules (name, flow_type) VALUES ('Venmo', 'earned_income')"
        )
        self.add("paycheck", "checking", -500_000, "Income", "2026-08-01")
        self.add("purchase", "card", 150_000, "Travel", "2026-08-02")
        self.add("checking-to-savings", "checking", 100_000, "Transfer Out", "2026-08-03")
        self.add("savings-from-checking", "savings", -100_000, "Transfer In", "2026-08-03")
        self.add("card-payment-out", "checking", 150_000, "Transfer Out", "2026-08-04")
        self.add("card-payment-in", "card", -150_000, "Loan Payments", "2026-08-04")
        self.add("unclear-deposit", "checking", -20_000, "Other", "2026-08-05")
        self.add("venmo-out", "checking", 5_000, "Transfer Out", "2026-08-06")
        self.add("venmo-in", "checking", -7_000, "Venmo", "2026-08-07")
        self.add("loan-payment", "checking", 8_000, "Loan Payments", "2026-08-08")
        self.add("excluded-inflow", "checking", -99_000, "Income", "2026-08-09", excluded=1)
        self.add("excluded-transfer", "checking", 99_000, "Transfer Out", "2026-08-10", excluded=1)
        self.add("older-income", "checking", -400_000, "Income", "2025-06-01")

        summary = cash_flow_summary(
            self.connection,
            lookback_days=30,
            today=date(2026, 8, 15),
        )

        self.assertEqual(summary["income"], 500_000)
        self.assertEqual(summary["spending"], 163_000)
        self.assertEqual(summary["other_inflows"], 27_000)
        self.assertEqual(summary["transfers_in"], 250_000)
        self.assertEqual(summary["transfers_out"], 250_000)
        self.assertEqual(summary["net"], 364_000)
        self.assertEqual(summary["savings_rate"], 72.8)
        self.assertGreaterEqual(len(summary["months"]), 2)

        treatments = {
            row["id"]: row["flow_type"]
            for row in transaction_list(self.connection, include_excluded=True)
        }
        self.assertEqual(treatments["venmo-in"], "other_inflow")
        self.assertEqual(treatments["venmo-out"], "spending")
        self.assertEqual(treatments["checking-to-savings"], "transfer")
        self.assertEqual(treatments["paycheck"], "earned_income")

        self.connection.execute(
            "UPDATE transactions SET flow_override = 'earned_income' WHERE id = 'unclear-deposit'"
        )
        reviewed = cash_flow_summary(
            self.connection,
            lookback_days=30,
            today=date(2026, 8, 15),
        )
        self.assertEqual(reviewed["income"], 520_000)
        self.assertEqual(reviewed["other_inflows"], 7_000)

    def test_category_rule_applies_until_transaction_override_wins(self):
        self.connection.execute(
            "INSERT INTO category_rules (name, flow_type) VALUES ('Payback', 'other_inflow')"
        )
        self.add("payback", "checking", -12_000, "Payback", "2026-08-05")
        initial = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        self.assertEqual(initial["other_inflows"], 12_000)

        self.connection.execute(
            "UPDATE transactions SET flow_override = 'transfer' WHERE id = 'payback'"
        )
        overridden = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        self.assertEqual(overridden["other_inflows"], 0)
        self.assertEqual(overridden["transfers_in"], 12_000)

    def test_spending_pages_share_cash_flow_classification(self):
        self.add("earlier-purchase", "card", 1_000, "Dining", "2026-07-01")
        self.add("purchase", "card", 2_500, "Dining", "2026-08-04")
        self.add("transfer", "checking", 500_000, "Transfer Out", "2026-08-05")
        self.add(
            "reviewed-transfer",
            "checking",
            600_000,
            "General",
            "2026-08-06",
            override="transfer",
        )
        self.add("income", "checking", -100_000, "Income", "2026-08-07")
        self.add(
            "excluded-purchase",
            "card",
            70_000,
            "Travel",
            "2026-08-08",
            excluded=1,
        )

        monthly = spending_summary(self.connection, "2026-08")
        rolling = rolling_spending_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        cash_flow = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        trends = long_term_trends(self.connection)
        details = category_details(self.connection, "2026-08")

        self.assertEqual(monthly["total"], 2_500)
        self.assertEqual(rolling["total"], 2_500)
        self.assertEqual(cash_flow["spending"], 2_500)
        august_daily_total = sum(
            row["amount"]
            for row in daily_trends(self.connection)
            if row["date"].startswith("2026-08")
        )
        self.assertEqual(august_daily_total, 2_500)
        self.assertEqual(trends["months"][-1]["amount"], 2_500)
        self.assertEqual([row["name"] for row in monthly["categories"]], ["Dining"])
        self.assertEqual([row["name"] for row in details], ["Dining"])

    def test_account_scoped_merchant_rules_apply_until_transaction_override(self):
        self.connection.execute(
            """
            INSERT INTO merchant_rules (
                account_id, match_type, match_value, category, flow_type
            ) VALUES ('checking', 'merchant', 'Recurring Payment', 'Transfer Out', 'transfer')
            """
        )
        self.connection.execute(
            """
            INSERT INTO merchant_rules (
                account_id, match_type, match_value, category, flow_type
            ) VALUES ('checking', 'description', 'Fallback Payment', 'Transfer Out', 'transfer')
            """
        )
        self.connection.execute(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded
            ) VALUES (
                'matched', 'checking', 90000, 'USD', 'Payment detail',
                'Recurring Payment', 0, '2026-08-05', 'Loan Payments', 0
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded
            ) VALUES
                ('description-match', 'checking', 80000, 'USD',
                 'Fallback Payment', NULL, 0, '2026-08-06', 'Loan Payments', 0),
                ('different-account', 'card', 1200, 'USD', 'Payment detail',
                 'Recurring Payment', 0, '2026-08-07', 'General', 0)
            """
        )

        transactions = {
            row["id"]: row for row in transaction_list(self.connection)
        }
        matched = transactions["matched"]
        self.assertEqual(matched["effective_category"], "Transfer Out")
        self.assertEqual(matched["flow_type"], "transfer")
        self.assertIsNotNone(matched["merchant_rule_id"])
        self.assertEqual(transactions["description-match"]["flow_type"], "transfer")
        self.assertEqual(transactions["different-account"]["flow_type"], "spending")
        self.assertEqual(
            rolling_spending_summary(
                self.connection, lookback_days=30, today=date(2026, 8, 15)
            )["total"],
            1_200,
        )

        self.connection.execute(
            """
            UPDATE transactions
            SET category_override = 'Housing', flow_override = 'spending'
            WHERE id = 'matched'
            """
        )
        overridden = {
            row["id"]: row for row in transaction_list(self.connection)
        }["matched"]
        self.assertEqual(overridden["effective_category"], "Housing")
        self.assertEqual(overridden["flow_type"], "spending")


if __name__ == "__main__":
    unittest.main()
