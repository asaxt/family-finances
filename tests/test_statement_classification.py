import sqlite3
import unittest

from analytics import CATEGORY_RULE_JOIN, EFFECTIVE_CATEGORY_SQL
from llm_evaluation import load_ai_reviews
from schema import create_schema
from statement_classification import eligible_import_ids, match_import_transfers


class StatementClassificationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        create_schema(self.db)
        self.db.execute("INSERT INTO connections (id, owner_name, institution, access_token) VALUES (1, 'Example', 'Example Bank', '')")
        for account in ('checking', 'savings', 'card'):
            self.db.execute("INSERT INTO accounts (id, connection_id, institution, name, type, cash_flow_role, spending_enabled) VALUES (?, 1, 'Example Bank', ?, 'depository', 'cash_flow', 1)", (account, account))
        self.db.execute("INSERT OR REPLACE INTO category_rules (name, flow_type) VALUES ('Transfer', 'transfer')")

    def tearDown(self):
        self.db.close()

    def add(self, key, account, amount, category='Uncategorized', currency='USD', day='2026-01-02'):
        self.db.execute("INSERT INTO transactions (id, account_id, amount, currency, description, pending, transacted_at, category) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                        (key, account, amount, currency, key, day, category))

    def categories(self):
        return dict(self.db.execute(f'SELECT t.id, {EFFECTIVE_CATEGORY_SQL} FROM transactions t JOIN accounts a ON a.id = t.account_id {CATEGORY_RULE_JOIN}'))

    def test_import_pairs_with_existing_custom_transfer_rule_without_overwriting_it(self):
        self.add('existing', 'checking', 1000)
        self.add('new', 'savings', -1000)
        self.db.execute("INSERT INTO category_rules (name, flow_type) VALUES ('Account moves', 'transfer')")
        self.db.execute("INSERT INTO merchant_rules (account_id, match_type, match_value, category) VALUES ('checking', 'description', 'existing', 'Account moves')")
        self.assertEqual(self.categories()['new'], 'Transfer')
        self.assertEqual(match_import_transfers(self.db, ['new']), 1)
        self.assertEqual(self.categories(), {'existing': 'Account moves', 'new': 'Transfer'})
        self.assertIsNone(self.db.execute("SELECT category_override FROM transactions WHERE id = 'existing'").fetchone()[0])
        self.assertIn('new', load_ai_reviews(self.db))
        self.assertEqual(eligible_import_ids(self.db, ['new']), [])

    def test_two_uncategorized_sides_are_marked_and_known_pairs_are_not_reused(self):
        self.add('old', 'checking', 1000)
        self.add('new', 'savings', -1000)
        self.assertEqual(match_import_transfers(self.db, ['new']), 2)
        self.add('extra', 'card', -1000)
        self.assertEqual(match_import_transfers(self.db, ['extra']), 0)
        self.assertEqual(self.categories()['extra'], 'Uncategorized')

    def test_ambiguous_same_amount_pairs_are_not_inferred(self):
        self.add('one', 'checking', 1000)
        self.add('two', 'savings', -1000)
        self.add('three', 'card', -1000)
        self.assertEqual(match_import_transfers(self.db, ['one', 'two', 'three']), 0)
        self.assertEqual(set(self.categories().values()), {'Uncategorized'})

    def test_currency_exclusion_pending_same_account_and_category_choices_are_respected(self):
        scenarios = ['currency', 'excluded', 'pending', 'same-account', 'categorized', 'user-choice', 'old-date']
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.db.execute('DELETE FROM transactions')
                self.add('new', 'checking', 1000)
                self.add('other', 'checking' if scenario == 'same-account' else 'savings', -1000,
                         category='Income' if scenario == 'categorized' else 'Uncategorized',
                         currency='EUR' if scenario == 'currency' else 'USD',
                         day='2025-12-01' if scenario == 'old-date' else '2026-01-02')
                if scenario in {'excluded', 'pending'}:
                    self.db.execute(f"UPDATE transactions SET {scenario} = 1 WHERE id = 'other'")
                if scenario == 'user-choice':
                    self.db.execute("UPDATE transactions SET category_override = 'Uncategorized', category_override_source = 'user' WHERE id = 'other'")
                self.assertEqual(match_import_transfers(self.db, ['new']), 0)
                self.assertEqual(self.categories()['new'], 'Uncategorized')

    def test_existing_user_transfer_is_unchanged(self):
        self.add('existing', 'checking', 1000)
        self.db.execute("UPDATE transactions SET category_override = 'Transfer', category_override_source = 'user'")
        self.add('new', 'savings', -1000)
        self.assertEqual(match_import_transfers(self.db, ['new']), 1)
        self.assertEqual(self.db.execute("SELECT category_override_source FROM transactions WHERE id = 'existing'").fetchone()[0], 'user')

    def test_category_treatment_takes_precedence_over_transfer_label(self):
        self.db.execute("UPDATE category_rules SET flow_type = 'spending' WHERE name = 'Transfer'")
        self.add('existing', 'checking', 1000, category='Transfer')
        self.add('new', 'savings', -1000)
        self.assertEqual(match_import_transfers(self.db, ['new']), 0)
        self.assertEqual(self.categories()['new'], 'Uncategorized')
