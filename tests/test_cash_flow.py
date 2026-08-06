import sqlite3
import unittest
from datetime import date

from analytics import cash_flow_summary
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
        self.add("paycheck", "checking", -500_000, "Income", "2026-08-01")
        self.add("purchase", "card", 150_000, "Travel", "2026-08-02")
        self.add("checking-to-savings", "checking", 100_000, "Transfer Out", "2026-08-03")
        self.add("savings-from-checking", "savings", -100_000, "Transfer In", "2026-08-03")
        self.add("card-payment-out", "checking", 150_000, "Transfer Out", "2026-08-04")
        self.add("card-payment-in", "card", -150_000, "Loan Payments", "2026-08-04")
        self.add("unclear-deposit", "checking", -20_000, "Other", "2026-08-05")
        self.add("venmo-out", "checking", 5_000, "Transfer Out", "2026-08-06")
        self.add("venmo-in", "checking", -7_000, "Transfer In", "2026-08-07")
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


if __name__ == "__main__":
    unittest.main()
