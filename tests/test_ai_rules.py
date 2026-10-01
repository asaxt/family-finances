import sqlite3
import unittest

from analytics import transaction_list
from llm_evaluation import save_categorization_batch
from schema import create_schema
from tests.test_category_review import seed


class CompletedAIRuleTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        seed(self.connection)
        self.connection.execute("UPDATE transactions SET pending=0, excluded=0, category='Uncategorized', category_override=NULL, category_override_source=NULL")

    def tearDown(self):
        self.connection.close()

    def detail(self, identifier='sample-1', category='EXAMPLE SPECIFIC'):
        return {'account_id': 'sample-account', 'description': 'SAMPLE CAFE',
                'transaction_ids': [identifier], 'status': 'categorized',
                'category': category, 'confidence': 2}

    def test_completed_batch_saves_rule_and_future_matches_without_manual_override(self):
        item = self.detail()
        prepared = {'groups': [item]}
        self.connection.execute("UPDATE transactions SET category_override='EXAMPLE BROAD', category_override_source='user' WHERE id='sample-2'")
        save_categorization_batch(self.connection, prepared, [], [item])
        rows = {row['id']: row for row in transaction_list(self.connection)}
        self.assertIsNone(rows['sample-1']['category_override'])
        self.assertEqual(rows['sample-1']['effective_category'], 'EXAMPLE SPECIFIC')
        self.assertEqual(rows['sample-2']['effective_category'], 'EXAMPLE BROAD')
        self.assertEqual(rows['sample-4']['effective_category'], 'EXAMPLE SPECIFIC')
        self.assertEqual(self.connection.execute('SELECT source FROM merchant_rules').fetchone()[0], 'model')

    def test_mixed_direction_groups_wait_and_disagreeing_guesses_never_become_rule(self):
        first, second = self.detail(), self.detail('sample-2', 'EXAMPLE BROAD')
        prepared = {'groups': [first, second]}
        save_categorization_batch(self.connection, prepared, [], [first])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
        save_categorization_batch(self.connection, prepared, [first], [second])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT category_override_source FROM transactions WHERE id='sample-1'").fetchone()[0], 'model')

    def test_transfer_evidence_and_unresolved_groups_do_not_create_rules(self):
        for category in ('Transfer', ''):
            with self.subTest(category=category):
                item = self.detail(category=category)
                if not category:
                    item['status'] = 'uncategorized'
                save_categorization_batch(self.connection, {'groups': [item]}, [], [item])
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)

    def test_uncertain_later_group_does_not_generalize_earlier_prediction(self):
        first = self.detail()
        second = {**self.detail('sample-2', ''), 'status': 'uncategorized'}
        prepared = {'groups': [first, second]}
        save_categorization_batch(self.connection, prepared, [], [first])
        save_categorization_batch(self.connection, prepared, [first], [second])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
        self.assertIsNone(self.connection.execute("SELECT category_override FROM transactions WHERE id='sample-2'").fetchone()[0])

    def test_legacy_individual_choice_is_not_relabelled_or_generalized(self):
        self.connection.execute("UPDATE transactions SET category_override='EXAMPLE BROAD', category_override_source=NULL WHERE id='sample-1'")
        item = {**self.detail(), 'allow_recategorization': True}
        self.assertEqual(save_categorization_batch(self.connection, {'groups': [item]}, [], [item]), 0)
        self.assertEqual(self.connection.execute("SELECT category_override FROM transactions WHERE id='sample-1'").fetchone()[0], 'EXAMPLE BROAD')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
