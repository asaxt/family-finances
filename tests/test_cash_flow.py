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
                id, connection_id, institution, name, mask, type, subtype,
                cash_flow_role, spending_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "card", 1, "Card Bank", "Credit Card", "1111", "credit",
                    "credit card", "cash_flow", 1,
                ),
                (
                    "checking", 2, "Cash Bank", "Checking", "2222",
                    "depository", "checking", "cash_flow", 1,
                ),
                (
                    "savings", 2, "Cash Bank", "Savings", "3333",
                    "depository", "savings", "cash_flow", 1,
                ),
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
        self.add("checking-to-savings", "checking", 100_000, "Transfer", "2026-08-03")
        self.add("savings-from-checking", "savings", -100_000, "Transfer", "2026-08-03")
        self.add("card-payment-out", "checking", 150_000, "Transfer", "2026-08-04")
        self.add("card-payment-in", "card", -150_000, "Transfer", "2026-08-04")
        self.add("loan-payment", "checking", 8_000, "Loan Payments", "2026-08-08")
        self.add("purchase-refund", "checking", -2_000, "Shopping", "2026-08-08")
        self.add("excluded-inflow", "checking", -99_000, "Income", "2026-08-09", excluded=1)
        self.add("excluded-transfer", "checking", 99_000, "Transfer", "2026-08-10", excluded=1)
        self.add("older-income", "checking", -400_000, "Income", "2025-06-01")

        summary = cash_flow_summary(
            self.connection,
            lookback_days=30,
            today=date(2026, 8, 15),
        )

        self.assertEqual(summary["income"], 500_000)
        self.assertEqual(summary["spending"], 156_000)
        self.assertEqual(summary["other_inflows"], 0)
        self.assertEqual(summary["transfers_in"], 250_000)
        self.assertEqual(summary["transfers_out"], 250_000)
        self.assertEqual(summary["net"], 344_000)
        self.assertEqual(summary["savings_rate"], 68.8)
        self.assertGreaterEqual(len(summary["months"]), 2)

        treatments = {
            row["id"]: row["flow_type"]
            for row in transaction_list(self.connection, include_excluded=True)
        }
        self.assertEqual(treatments["checking-to-savings"], "transfer")
        self.assertEqual(treatments["paycheck"], "earned_income")

    def test_category_mapping_ignores_old_transaction_flow_override(self):
        self.connection.execute(
            "INSERT INTO category_rules (name, flow_type) VALUES ('Payback', 'other_inflow')"
        )
        self.add("payback", "checking", -12_000, "Payback", "2026-08-05")
        initial = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        self.assertEqual(initial["income"], 12_000)

        self.connection.execute(
            "UPDATE transactions SET flow_override = 'transfer' WHERE id = 'payback'"
        )
        overridden = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        self.assertEqual(overridden["income"], 12_000)
        self.assertEqual(overridden["transfers_in"], 0)

    def test_spending_pages_share_cash_flow_classification(self):
        self.add("earlier-purchase", "card", 1_000, "Dining", "2026-07-01")
        self.add("purchase", "card", 2_500, "Dining", "2026-08-04")
        self.add("transfer", "checking", 500_000, "Transfer", "2026-08-05")
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

        spending_ids = {
            row["id"]
            for row in transaction_list(
                self.connection, reporting_scope="spending", spending_only=True
            )
        }
        cash_flow_ids = {
            row["id"]
            for row in transaction_list(
                self.connection, reporting_scope="cash_flow"
            )
        }
        self.assertIn("purchase", spending_ids)
        self.assertNotIn("income", spending_ids)
        self.assertIn("income", cash_flow_ids)
        self.assertIn("purchase", cash_flow_ids)

    def test_included_account_contributes_to_cash_flow_and_spending(self):
        self.add("debit-purchase", "checking", 3_000, "Dining", "2026-08-04")

        cash_flow = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        spending = spending_summary(self.connection, "2026-08")
        self.assertEqual(cash_flow["spending"], 3_000)
        self.assertEqual(spending["total"], 3_000)

        self.connection.execute(
            """
            UPDATE accounts
            SET cash_flow_role = 'other', spending_enabled = 0
            WHERE id = 'checking'
            """
        )
        spending = spending_summary(self.connection, "2026-08")
        cash_flow = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        self.assertEqual(spending["total"], 0)
        self.assertEqual(cash_flow["spending"], 0)

    def test_equal_opposite_uncategorized_transactions_are_transfers(self):
        self.add("payment-out", "checking", 75_000, "Uncategorized", "2026-08-04")
        self.add("payment-in", "card", -75_000, "Uncategorized", "2026-08-06")
        self.add("unmatched", "card", 2_500, "Uncategorized", "2026-08-07")

        transactions = {
            row["id"]: row for row in transaction_list(self.connection)
        }
        summary = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )

        self.assertEqual(transactions["payment-out"]["effective_category"], "Transfer")
        self.assertEqual(transactions["payment-in"]["effective_category"], "Transfer")
        self.assertEqual(transactions["payment-out"]["flow_type"], "transfer")
        self.assertEqual(transactions["payment-in"]["flow_type"], "transfer")
        self.assertEqual(transactions["unmatched"]["effective_category"], "Uncategorized")
        self.assertIsNone(transactions["unmatched"]["flow_type"])
        self.assertEqual(summary["transfers_in"], 75_000)
        self.assertEqual(summary["transfers_out"], 75_000)
        self.assertEqual(summary["spending"], 0)

    def test_account_scoped_exact_description_rules_apply_to_recurring_transactions(self):
        self.connection.execute(
            """
            INSERT INTO merchant_rules (
                account_id, match_type, match_value, category, flow_type
            ) VALUES ('checking', 'description', 'Payment detail', 'Transfer', NULL)
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
                 'Payment detail', NULL, 0, '2026-08-06', 'Loan Payments', 0),
                ('different-account', 'card', 1200, 'USD', 'Payment detail',
                 'Recurring Payment', 0, '2026-08-07', 'General', 0)
            """
        )

        transactions = {
            row["id"]: row for row in transaction_list(self.connection)
        }
        matched = transactions["matched"]
        self.assertEqual(matched["effective_category"], "Transfer")
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

    def test_account_scoped_description_contains_rule_handles_variable_details(self):
        self.connection.execute(
            """
            INSERT INTO merchant_rules (
                account_id, match_type, match_value, category
            ) VALUES (
                'checking', 'description_contains',
                'EXAMPLE BROKERAGE TRANSFER', 'Transfer'
            )
            """
        )
        self.connection.executemany(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded
            ) VALUES (?, 'checking', 10000, 'USD', ?, NULL, 0, ?, 'Uncategorized', 0)
            """,
            (
                (
                    "brokerage-one",
                    "EXAMPLE BROKERAGE TRANSFER ~ REFERENCE 1001",
                    "2025-01-05",
                ),
                (
                    "brokerage-two",
                    "EXAMPLE BROKERAGE TRANSFER ~ REFERENCE 2002",
                    "2025-02-06",
                ),
            ),
        )

        rows = {row["id"]: row for row in transaction_list(self.connection)}
        self.assertEqual(rows["brokerage-one"]["effective_category"], "Transfer")
        self.assertEqual(rows["brokerage-two"]["effective_category"], "Transfer")
        self.assertEqual(
            rows["brokerage-one"]["merchant_rule_match_value"],
            "EXAMPLE BROKERAGE TRANSFER",
        )

    def test_earnings_trends_group_each_description_as_its_own_series(self):
        self.connection.executemany(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded
            ) VALUES (?, 'checking', ?, 'USD', ?, NULL, 0, ?, ?, 0)
            """,
            (
                ("pay-jan", -123400, "EXAMPLE EMPLOYER PAYROLL", "2025-01-15", "Income"),
                ("pay-feb", -125600, "EXAMPLE EMPLOYER PAYROLL", "2025-02-15", "Income"),
                ("bonus", -11100, "EXAMPLE EMPLOYER BONUS", "2025-02-20", "Income"),
                ("purchase", 900, "SAMPLE CAFE", "2025-02-21", "Dining"),
            ),
        )

        trends = long_term_trends(self.connection, kind="earnings")

        self.assertEqual([row["amount"] for row in trends["months"]], [123400, 136700])
        series = {row["name"]: row["values"] for row in trends["category_series"]}
        self.assertEqual(series["EXAMPLE EMPLOYER PAYROLL"], [123400, 125600])
        self.assertEqual(series["EXAMPLE EMPLOYER BONUS"], [0, 11100])
        self.assertNotIn("SAMPLE CAFE", series)

    def test_earnings_trends_use_saved_short_description_as_series_name(self):
        self.connection.execute(
            """
            INSERT INTO merchant_rules (
                account_id, match_type, match_value, category
            ) VALUES (
                'checking', 'description_contains',
                'EXAMPLE EMPLOYER REIMBURSEMENT', 'Income'
            )
            """
        )
        self.connection.executemany(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded
            ) VALUES (?, 'checking', ?, 'USD', ?, NULL, 0, ?, 'Uncategorized', 0)
            """,
            (
                (
                    "reimbursement-one",
                    -1200,
                    "EXAMPLE EMPLOYER REIMBURSEMENT ~ PERIOD 01",
                    "2025-01-20",
                ),
                (
                    "reimbursement-two",
                    -1500,
                    "EXAMPLE EMPLOYER REIMBURSEMENT ~ PERIOD 02",
                    "2025-02-20",
                ),
            ),
        )

        trends = long_term_trends(self.connection, kind="earnings")

        self.assertEqual(len(trends["category_series"]), 1)
        self.assertEqual(
            trends["category_series"][0]["name"],
            "EXAMPLE EMPLOYER REIMBURSEMENT",
        )
        self.assertEqual(trends["category_series"][0]["values"], [1200, 1500])

    def test_refunds_passively_reduce_money_out_and_spending(self):
        self.add("card-purchase", "card", 10_000, "Shopping", "2026-08-03")
        self.add("card-refund", "card", -3_000, "Shopping", "2026-08-04")
        self.add("bank-purchase", "checking", 5_000, "Shopping", "2026-08-03")
        self.add("bank-refund", "checking", -1_000, "Shopping", "2026-08-04")

        spending = spending_summary(self.connection, "2026-08")
        cash_flow = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        spending_rows = transaction_list(
            self.connection, reporting_scope="spending", spending_only=True
        )

        self.assertEqual(spending["total"], 11_000)
        self.assertEqual(cash_flow["spending"], 11_000)
        self.assertEqual(
            {row["id"] for row in spending_rows},
            {"card-purchase", "card-refund", "bank-purchase", "bank-refund"},
        )

    def test_default_category_cash_flow_mappings(self):
        self.add("income", "checking", -100_000, "Income", "2026-08-01")
        self.add(
            "loan", "checking", -50_000, "Loan Disbursements", "2026-08-02"
        )
        self.add(
            "reimbursement",
            "checking",
            -20_000,
            "Reimbursed Work Travel",
            "2026-08-03",
        )
        self.add("ordinary", "checking", 4_000, "Other", "2026-08-04")

        summary = cash_flow_summary(
            self.connection, lookback_days=30, today=date(2026, 8, 15)
        )
        self.assertEqual(summary["income"], 170_000)
        self.assertEqual(summary["other_inflows"], 0)
        self.assertEqual(summary["spending"], 4_000)


if __name__ == "__main__":
    unittest.main()
