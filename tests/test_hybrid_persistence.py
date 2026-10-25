from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from llm_evaluation import (
    categorization_fingerprint,
    classify_and_save_batch,
    classify_evaluation_rows,
    load_ai_reviews,
    prepare_evaluation,
    apply_categorized_suggestions,
    save_ai_reviews,
    save_categorization_batch,
)
from schema import create_schema


class HybridPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        create_schema(self.connection)
        self.connection.execute(
            "INSERT INTO connections (id, owner_name, institution, access_token) "
            "VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', '')"
        )
        self.connection.execute(
            "INSERT INTO accounts (id, connection_id, institution, name, type, subtype) "
            "VALUES ('sample-account', 1, 'EXAMPLE BANK', 'EXAMPLE ACCOUNT', 'depository', 'checking')"
        )
        self.connection.executemany(
            "INSERT INTO category_rules (name, flow_type) VALUES (?, 'spending')",
            [('EXAMPLE DINING',), ('EXAMPLE MANUAL',)],
        )
        self.connection.executemany(
            "INSERT INTO transactions (id, account_id, amount, currency, description, merchant, "
            "pending, transacted_at, category, category_override, category_override_source) "
            "VALUES (?, 'sample-account', 123, 'USD', ?, 'SAMPLE SHOP', "
            "0, '2001-01-02', 'Uncategorized', ?, ?)",
            [
                ('sample-target', 'SAMPLE SHARED PURPOSE', None, None),
                ('sample-repeat', 'SAMPLE SHARED PURPOSE', None, None),
                ('sample-manual', 'SAMPLE SHARED PURPOSE', 'EXAMPLE MANUAL', 'user'),
                ('sample-unrelated', 'SAMPLE UNRELATED PURPOSE', None, None),
            ],
        )
        self.old_reviews = {
            identifier: {'category': 'EXAMPLE MANUAL', 'reason': 'EXAMPLE earlier proposal'}
            for identifier in ('sample-target', 'sample-repeat', 'sample-manual', 'sample-unrelated')
        }
        save_ai_reviews(self.connection, self.old_reviews)
        self.connection.commit()

    @contextmanager
    def connection_factory(self):
        with self.connection:
            yield self.connection

    def detail(self, category='EXAMPLE DINING', identifiers=('sample-target',), **metadata):
        return {
            'account_id': 'sample-account', 'description': 'SAMPLE SHARED PURPOSE',
            'transaction_ids': list(identifiers), 'status': 'categorized',
            'category': category, 'confidence': 1, 'reason': 'EXAMPLE tentative category',
            **metadata,
        }

    def prepared(self, details, **policy):
        return {'groups': details, 'source_fingerprint': categorization_fingerprint(self.connection), **policy}

    def test_suppression_clears_only_actual_applied_ids_without_changing_confidence(self):
        item = self.detail(identifiers=('sample-target', 'sample-manual'))
        result = {'details': [item], 'suppress_ai_reviews': True}
        applied = apply_categorized_suggestions(self.connection, result)
        self.assertEqual(applied, 1)
        self.assertEqual(item['confidence'], 1)
        self.assertEqual(item['applied_transaction_ids'], ['sample-target'])
        self.assertEqual(load_ai_reviews(self.connection), {
            key: value for key, value in self.old_reviews.items() if key != 'sample-target'
        })
        manual = self.connection.execute(
            "SELECT category_override, category_override_source FROM transactions WHERE id='sample-manual'"
        ).fetchone()
        self.assertEqual(tuple(manual), ('EXAMPLE MANUAL', 'user'))

    def test_legacy_engine_still_queues_tentative_direct_assignments(self):
        item = self.detail()
        apply_categorized_suggestions(self.connection, {'details': [item]})
        reviews = load_ai_reviews(self.connection)
        self.assertEqual(reviews['sample-target'], {
            'category': 'EXAMPLE DINING', 'reason': 'EXAMPLE tentative category',
        })
        for identifier in ('sample-repeat', 'sample-manual', 'sample-unrelated'):
            self.assertEqual(reviews[identifier], self.old_reviews[identifier])

    def test_mixed_other_stays_an_individual_assignment_without_a_recurring_rule(self):
        item = self.detail('Other', suppress_ai_reviews=False, non_recurring_categories=[])
        prepared = self.prepared([item], suppress_ai_reviews=True, non_recurring_categories=['other'])
        self.assertEqual(save_categorization_batch(self.connection, prepared, [], [item]), 1)
        target = self.connection.execute(
            "SELECT category_override, category_override_source FROM transactions WHERE id='sample-target'"
        ).fetchone()
        self.assertEqual(tuple(target), ('Other', 'model'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
        self.assertIsNone(self.connection.execute(
            "SELECT category_override FROM transactions WHERE id='sample-repeat'"
        ).fetchone()[0])
        self.assertEqual(load_ai_reviews(self.connection), {
            key: value for key, value in self.old_reviews.items() if key != 'sample-target'
        })

    def test_suppression_also_clears_rule_covered_ids_and_preserves_manual_and_unrelated_entries(self):
        item = self.detail()
        prepared = self.prepared([item], suppress_ai_reviews=True, non_recurring_categories=['Other'])
        save_categorization_batch(self.connection, prepared, [], [item])
        self.assertEqual([tuple(row) for row in self.connection.execute(
            'SELECT category, source FROM merchant_rules'
        )], [('EXAMPLE DINING', 'model')])
        self.assertEqual(load_ai_reviews(self.connection), {
            key: self.old_reviews[key] for key in ('sample-manual', 'sample-unrelated')
        })
        self.assertEqual(item['confidence'], 1)
        self.assertEqual(self.connection.execute(
            "SELECT category_override FROM transactions WHERE id='sample-manual'"
        ).fetchone()[0], 'EXAMPLE MANUAL')

    def test_prediction_metadata_cannot_enable_suppression_or_block_rules_for_legacy_engine(self):
        item = self.detail(suppress_ai_reviews=True, non_recurring_categories=['EXAMPLE DINING'])
        save_categorization_batch(self.connection, self.prepared([item]), [], [item])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 1)
        reviews = load_ai_reviews(self.connection)
        self.assertEqual(reviews['sample-target']['category'], 'EXAMPLE DINING')
        self.assertEqual(reviews['sample-repeat']['category'], 'EXAMPLE DINING')
        self.assertEqual(reviews['sample-unrelated'], self.old_reviews['sample-unrelated'])

    def test_freshness_retry_preserves_trusted_no_queue_and_non_recurring_policies(self):
        prepared = prepare_evaluation(
            self.connection, ['EXAMPLE DINING', 'Other'], transaction_ids=['sample-target'],
        )
        prepared.update(suppress_ai_reviews=True, non_recurring_categories=['Other'])
        calls = []

        def classifier(current, rows):
            self.assertTrue(current['suppress_ai_reviews'])
            self.assertEqual(current['non_recurring_categories'], ['Other'])
            calls.append(rows[0]['amount'])
            if len(calls) == 1:
                self.connection.execute("UPDATE transactions SET amount=456 WHERE id='sample-target'")
                self.connection.commit()
            predictions = [{
                'id': row['evaluation_id'], 'category': 'Other', 'confidence': 1,
                'reason': 'EXAMPLE insufficient purchase purpose',
            } for row in rows]
            with patch('llm_evaluation.classify_batch', return_value=predictions):
                return classify_evaluation_rows(current, rows)

        details, applied = classify_and_save_batch(
            self.connection_factory, prepared, [], prepared['groups'], classify=classifier,
        )
        self.assertEqual(calls, [123, 456])
        self.assertEqual(applied, 1)
        self.assertEqual(details[0]['confidence'], 1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
        self.assertEqual(load_ai_reviews(self.connection), {
            key: value for key, value in self.old_reviews.items() if key != 'sample-target'
        })


if __name__ == '__main__':
    unittest.main()
