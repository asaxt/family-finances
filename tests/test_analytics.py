import sqlite3
import unittest
from datetime import date

from analytics import rolling_spending_summary
from schema import create_schema


class RollingOverviewAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        self.connection.execute(
            """
            INSERT INTO connections (id, owner_name, institution, access_token)
            VALUES (1, 'Household', 'Example', 'test-token')
            """
        )
        self.connection.execute(
            """
            INSERT INTO accounts (
                id, connection_id, institution, name, mask, type
            ) VALUES ('card-1', 1, 'Example', 'Everyday Card', '1234', 'credit')
            """
        )

    def tearDown(self):
        self.connection.close()

    def add_transaction(
        self,
        transaction_id,
        transacted_at,
        amount,
        category,
        merchant,
        *,
        pending=0,
        excluded=0,
    ):
        self.connection.execute(
            """
            INSERT INTO transactions (
                id, account_id, amount, currency, description, merchant,
                pending, transacted_at, category, excluded
            ) VALUES (?, 'card-1', ?, 'USD', ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                amount,
                merchant,
                merchant,
                pending,
                transacted_at,
                category,
                excluded,
            ),
        )

    def test_rolling_and_calendar_metrics_use_their_intended_windows(self):
        transactions = (
            ("june", "2026-06-17", 1000, "Dining", "Cafe"),
            ("july-prior", "2026-07-16", 3000, "Groceries", "Market"),
            ("july-current", "2026-07-17", 1000, "Dining", "Cafe"),
            ("august-one", "2026-08-01", 2000, "Groceries", "Market"),
            ("august-two", "2026-08-10", 3000, "Groceries", "Market"),
        )
        for transaction in transactions:
            self.add_transaction(*transaction)
        self.add_transaction(
            "pending",
            "2026-08-12",
            50000,
            "Ignored",
            "Pending",
            pending=1,
        )
        self.add_transaction(
            "excluded",
            "2026-08-13",
            50000,
            "Ignored",
            "Excluded",
            excluded=1,
        )

        summary = rolling_spending_summary(
            self.connection,
            lookback_days=30,
            today=date(2026, 8, 15),
        )

        self.assertEqual(summary["date_from"], "2026-07-17")
        self.assertEqual(summary["date_to"], "2026-08-15")
        self.assertEqual(summary["prior_date_from"], "2026-06-17")
        self.assertEqual(summary["prior_date_to"], "2026-07-16")
        self.assertEqual(summary["total"], 6000)
        self.assertEqual(summary["daily_average"], 200)
        self.assertEqual(summary["prior_total"], 4000)
        self.assertEqual(summary["change"], 50)
        self.assertEqual(summary["current_month_total"], 5000)
        self.assertEqual(summary["projected_total"], 10333)
        self.assertEqual(summary["categories"][0]["name"], "Groceries")
        self.assertEqual(summary["categories"][0]["amount"], 5000)
        self.assertEqual(summary["categories"][0]["transaction_count"], 2)
        self.assertEqual(summary["merchants"][0]["name"], "Market")
        self.assertEqual(summary["card_totals"][0]["amount"], 6000)
        self.assertEqual(summary["largest"]["amount"], 3000)

if __name__ == "__main__":
    unittest.main()
