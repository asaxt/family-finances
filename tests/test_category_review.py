import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import category_review as review
from llm_evaluation import classify_batch
from schema import create_schema
from tests import test_app_setup


def seed(connection):
    connection.execute("INSERT INTO connections (id, owner_name, institution, access_token) "
                       "VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', 'fake-token')")
    connection.execute("INSERT INTO accounts (id, connection_id, institution, name, type) "
                       "VALUES ('sample-account', 1, 'EXAMPLE BANK', 'EXAMPLE ACCOUNT', 'depository')")
    connection.executemany("INSERT INTO category_rules (name, flow_type) VALUES (?, 'spending')",
                           [('EXAMPLE BROAD',), ('EXAMPLE SPECIFIC',)])
    for identifier, pending, category, override in (
        ('sample-1', 0, 'EXAMPLE BROAD', 'EXAMPLE BROAD'),
        ('sample-2', 0, 'EXAMPLE BROAD', None),
        ('sample-3', 1, 'EXAMPLE BROAD', None),
        ('sample-4', 0, 'Uncategorized', None),
    ):
        connection.execute(
            "INSERT INTO transactions (id, account_id, amount, currency, description, merchant, "
            "pending, transacted_at, category, category_override, category_override_source, excluded) "
            "VALUES (?, 'sample-account', 123, 'USD', 'SAMPLE CAFE', 'SAMPLE CAFE', ?, "
            "'2001-01-02', ?, ?, ?, 1)",
            (identifier, pending, category, override, 'user' if override else None),
        )


def prediction(model, categories, groups, **kwargs):
    return [{'id': group['evaluation_id'], 'category': 'EXAMPLE SPECIFIC', 'confidence': 2,
             'reason': 'The supplied label describes the purchase purpose more precisely.'}
            for group in groups]


def finances(connection):
    return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table}')]
            for table in ('transactions', 'category_rules', 'merchant_rules', 'accounts')}


class CategoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        seed(self.connection)

    def tearDown(self):
        self.connection.close()

    def scan(self):
        result, groups, examples = review.prepare(self.connection)
        with patch('category_review.classify_batch', side_effect=prediction):
            review.review_batch(result, groups, examples)
        result['status'] = 'completed'
        review.save(self.connection, result)
        return result

    def test_scan_only_saves_suggestions_and_includes_manual_categories(self):
        before = finances(self.connection)
        result = self.scan()
        self.assertEqual(finances(self.connection), before)
        self.assertEqual((result['considered'], result['eligible'], result['processed']), (4, 2, 2))
        self.assertEqual((result['pending_skipped'], result['uncategorized_skipped']), (1, 1))
        self.assertEqual({item['transaction_id'] for item in result['suggestions']}, {'sample-1', 'sample-2'})
        self.assertEqual(review.load(self.connection), result)

    def test_accept_changes_only_selected_transaction_and_is_idempotent(self):
        result = self.scan()
        before = finances(self.connection)
        self.assertEqual(review.decide(self.connection, result, ['sample-1'], 'accept')['accepted'], 1)
        rows = {row['id']: row for row in self.connection.execute('SELECT * FROM transactions')}
        self.assertEqual(rows['sample-1']['category_override'], 'EXAMPLE SPECIFIC')
        self.assertEqual(rows['sample-1']['category_override_source'], 'user')
        self.assertEqual(rows['sample-1']['excluded'], 1)
        self.assertIsNone(rows['sample-2']['category_override'])
        for table in ('category_rules', 'merchant_rules', 'accounts'):
            self.assertEqual(finances(self.connection)[table], before[table])
        self.assertEqual(review.decide(self.connection, result, ['sample-1'], 'accept')['accepted'], 0)
        self.assertEqual(review.decide(self.connection, result, ['sample-2'], 'keep')['kept'], 1)
        self.assertIsNone(self.connection.execute("SELECT category_override FROM transactions WHERE id = 'sample-2'").fetchone()[0])

    def test_manual_edit_and_label_changes_invalidate_suggestions(self):
        result = self.scan()
        self.connection.execute("UPDATE transactions SET excluded = 0 WHERE id = 'sample-1'")
        self.assertEqual(review.decide(self.connection, result, ['sample-1'], 'accept')['stale'], 1)
        self.connection.execute("DELETE FROM category_rules WHERE name = 'EXAMPLE SPECIFIC'")
        self.assertEqual(review.decide(self.connection, result, ['sample-2'], 'accept')['stale'], 1)
        self.assertEqual(self.connection.execute("SELECT category_override FROM transactions WHERE id = 'sample-1'").fetchone()[0], 'EXAMPLE BROAD')

    def test_effective_rule_categories_and_distinct_current_labels(self):
        self.connection.execute("INSERT INTO merchant_rules (account_id, match_type, match_value, category) "
                                "VALUES ('sample-account', 'description', 'SAMPLE CAFE', 'EXAMPLE SPECIFIC')")
        result, groups, examples = review.prepare(self.connection)
        self.assertEqual({group['current_category'] for group in groups}, {'EXAMPLE BROAD', 'EXAMPLE SPECIFIC'})
        self.assertEqual(result['eligible'], 3)
        self.assertIn('EXAMPLE SPECIFIC', examples)

    def test_matching_rule_change_invalidates_suggestion(self):
        result = self.scan()
        self.connection.execute("INSERT INTO merchant_rules (account_id, match_type, match_value, category) "
                                "VALUES ('sample-account', 'description', 'SAMPLE CAFE', 'EXAMPLE SPECIFIC')")
        self.assertEqual(review.decide(self.connection, result, ['sample-2'], 'accept')['stale'], 1)

    def test_unchanged_and_uncertain_are_not_proposed(self):
        result, groups, examples = review.prepare(self.connection)
        for category, confidence, field in [('EXAMPLE BROAD', 2, 'unchanged'), ('', 0, 'uncertain')]:
            with patch('category_review.classify_batch', return_value=[{
                'id': groups[0]['evaluation_id'], 'category': category, 'confidence': confidence, 'reason': ''
            }]):
                review.review_batch(result, groups, examples)
            self.assertEqual(result[field], 2)
            self.assertEqual(result['suggestions'], [])

    def test_unknown_model_category_cannot_be_saved(self):
        result, groups, examples = review.prepare(self.connection)
        with patch('category_review.classify_batch', return_value=[{
            'id': groups[0]['evaluation_id'], 'category': 'INVENTED LABEL', 'confidence': 2
        }]):
            with self.assertRaises(ValueError):
                review.review_batch(result, groups, examples)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['suggestions'], [])

    def test_review_prompt_uses_current_label_and_local_transport(self):
        result, groups, examples = review.prepare(self.connection)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'message': {'content': json.dumps({'results': prediction(None, None, groups)})}
        }).encode()
        with patch('llm_evaluation.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value = response
            classify_batch('example-model', result['categories'], groups, review_existing=True)
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/chat')
        payload = json.loads(request.data)
        self.assertIn('no category will be applied automatically', payload['messages'][0]['content'])
        self.assertNotIn('broad coverage is more useful', payload['messages'][0]['content'])
        self.assertEqual(json.loads(payload['messages'][1]['content'])['transactions'][0]['current_category'], 'EXAMPLE BROAD')
        self.assertEqual(factory.call_args.args[0].proxies, {})


class CategoryReviewRouteTests(unittest.TestCase):
    setUp = test_app_setup.AppSetupTests.setUp
    tearDown = test_app_setup.AppSetupTests.tearDown
    csrf_token = staticmethod(test_app_setup.AppSetupTests.csrf_token)

    def ready(self):
        page = self.client.get('/setup')
        self.client.post('/setup', data={'csrf_token': self.csrf_token(page),
                         'password': 'fictional review test password', 'confirmation': 'fictional review test password'})
        with self.application.db() as connection:
            seed(connection)
            connection.execute('INSERT INTO settings (key, value) VALUES (?, ?)',
                               (self.application.LOCAL_AI_SETTING, '1'))
        page = self.client.get('/local-ai/category-review')
        self.assertEqual(page.status_code, 200)
        with self.client.session_transaction() as session:
            return session['csrf_token']

    def start(self, token, model=prediction):
        def immediate_thread(*, target, args, **kwargs):
            thread = MagicMock()
            thread.start.side_effect = lambda: target(*args)
            return thread
        with patch.object(self.application.threading, 'Thread', side_effect=immediate_thread), \
             patch('category_review.classify_batch', side_effect=model):
            return self.client.post('/api/local-ai/category-review/start', headers={'X-CSRF-Token': token})

    def test_start_report_and_explicit_decisions(self):
        token = self.ready()
        with self.application.db() as connection:
            before = finances(connection)
        self.assertEqual(self.start(token).status_code, 200)
        page = self.client.get('/local-ai/category-review')
        self.assertIn(b'EXAMPLE SPECIFIC', page.data)
        self.assertIn(b'Current category', page.data)
        self.assertIn(b'Keep current', page.data)
        with self.application.db() as connection:
            self.assertEqual(finances(connection), before)
            result = review.load(connection)
        self.assertEqual(self.start(token).status_code, 409)
        self.assertEqual(self.client.post('/api/local-ai/category-review/decide', data={
            'csrf_token': token, 'review_id': 'old-review', 'action': 'accept',
            'transaction_ids': ['sample-1'],
        }).status_code, 409)
        self.assertEqual(self.client.post('/api/local-ai/category-review/decide', data={
            'csrf_token': token, 'review_id': result['id'], 'action': 'accept',
            'transaction_ids': ['sample-1'],
        }).status_code, 302)
        with self.application.db() as connection:
            self.assertEqual(connection.execute("SELECT category_override FROM transactions WHERE id = 'sample-1'").fetchone()[0], 'EXAMPLE SPECIFIC')
        self.assertEqual(self.client.post('/api/local-ai/category-review/decide', data={
            'csrf_token': token, 'review_id': result['id'], 'action': 'keep',
            'transaction_ids': ['sample-2'],
        }).status_code, 302)

    def test_interruption_keeps_categories_unchanged_and_releases_lock(self):
        token = self.ready()
        with self.application.db() as connection:
            before = finances(connection)
        self.start(token, model=TimeoutError())
        with self.application.db() as connection:
            self.assertEqual(finances(connection), before)
            self.assertEqual(review.load(connection)['status'], 'interrupted')
        self.assertFalse(self.application.OLLAMA_EVALUATION_LOCK.locked())

    def test_partial_scan_saves_completed_suggestions_without_mutation(self):
        token = self.ready()
        with self.application.db() as connection:
            for index in range(6):
                connection.execute(
                    "INSERT INTO transactions (id, account_id, amount, currency, description, pending, transacted_at, category) "
                    "VALUES (?, 'sample-account', 123, 'USD', ?, 0, '2001-01-02', 'EXAMPLE BROAD')",
                    (f'sample-extra-{index}', f'SAMPLE DESCRIPTION {index}'))
            before = finances(connection)
        calls = 0
        def stop_second_batch(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise TimeoutError()
            return prediction(*args, **kwargs)
        self.start(token, model=stop_second_batch)
        with self.application.db() as connection:
            result = review.load(connection)
            self.assertEqual(result['status'], 'interrupted')
            self.assertGreater(result['processed'], 0)
            self.assertLess(result['processed'], result['eligible'])
            self.assertEqual(len(result['suggestions']), result['processed'])
            self.assertEqual(finances(connection), before)

    def test_concurrent_scan_is_rejected_without_replacing_results(self):
        token = self.ready()
        self.application.OLLAMA_EVALUATION_LOCK.acquire()
        try:
            self.assertEqual(self.start(token).status_code, 409)
            with self.application.db() as connection:
                self.assertIsNone(review.load(connection))
        finally:
            self.application.OLLAMA_EVALUATION_LOCK.release()

    def test_local_ai_disabled_and_csrf_required(self):
        token = self.ready()
        self.assertEqual(self.client.post('/api/local-ai/category-review/start').status_code, 400)
        self.application.save_setting(self.application.LOCAL_AI_SETTING, '0')
        self.assertEqual(self.start(token).status_code, 403)
        self.assertIn(b'Turn on Local AI in Settings', self.client.get('/local-ai/category-review').data)
