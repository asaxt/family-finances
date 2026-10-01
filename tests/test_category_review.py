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
        review.apply_rules(self.connection, result, [])
        result['status'] = 'completed'
        review.save(self.connection, result)
        return result

    def test_scan_saves_rules_automatically_and_preserves_individual_choices(self):
        before = finances(self.connection)
        result = self.scan()
        self.assertEqual((result['considered'], result['eligible'], result['processed']), (4, 2, 2))
        self.assertEqual((result['pending_skipped'], result['uncategorized_skipped']), (1, 1))
        self.assertEqual(result['rules_saved'], 1)
        self.assertEqual({item['decision'] for item in result['suggestions']}, {'rule_saved'})
        self.assertEqual(review.load(self.connection), result)
        self.assertEqual(finances(self.connection)['transactions'], before['transactions'])
        self.assertEqual(self.connection.execute('SELECT source, category FROM merchant_rules').fetchone()[:], ('model', 'EXAMPLE SPECIFIC'))
        review.apply_rules(self.connection, result, [])
        self.assertEqual(result['rules_saved'], 1)

    def test_manual_edit_or_label_change_during_scan_prevents_rule(self):
        for mutation in ("UPDATE transactions SET excluded = 0 WHERE id = 'sample-1'",
                         "DELETE FROM category_rules WHERE name = 'EXAMPLE SPECIFIC'"):
            with self.subTest(mutation=mutation):
                result, groups, examples = review.prepare(self.connection)
                with patch('category_review.classify_batch', side_effect=prediction):
                    review.review_batch(result, groups, examples)
                self.connection.execute(mutation)
                review.apply_rules(self.connection, result, [])
                self.assertEqual({item['decision'] for item in result['suggestions']}, {'stale'})
                self.assertEqual(result['rules_saved'], 0)

    def test_effective_rule_categories_and_distinct_current_labels(self):
        self.connection.execute("INSERT INTO merchant_rules (account_id, match_type, match_value, category) "
                                "VALUES ('sample-account', 'description', 'SAMPLE CAFE', 'EXAMPLE SPECIFIC')")
        result, groups, examples = review.prepare(self.connection)
        self.assertEqual({group['current_category'] for group in groups}, {'EXAMPLE BROAD', 'EXAMPLE SPECIFIC'})
        self.assertEqual(result['eligible'], 3)
        self.assertIn('EXAMPLE SPECIFIC', examples)

    def test_user_rules_are_retained(self):
        self.connection.execute("INSERT INTO merchant_rules (account_id, match_type, match_value, category) VALUES ('sample-account', 'description', 'SAMPLE CAFE', 'EXAMPLE BROAD')")
        result = self.scan()
        self.assertEqual({item['decision'] for item in result['suggestions']}, {'user_rule'})
        self.assertEqual(result['rules_saved'], 0)

    def test_matching_groups_wait_and_disagreeing_categories_do_not_create_rule(self):
        self.connection.execute("UPDATE transactions SET category='EXAMPLE SPECIFIC' WHERE id='sample-2'")
        result, groups, examples = review.prepare(self.connection)
        with patch('category_review.classify_batch', side_effect=prediction):
            review.review_batch(result, groups[:1], examples)
        review.apply_rules(self.connection, result, groups[1:])
        self.assertEqual(result['rules_saved'], 0)
        with patch('category_review.classify_batch', return_value=[{
            'id': groups[1]['evaluation_id'], 'category': 'EXAMPLE BROAD', 'confidence': 2,
        }]):
            review.review_batch(result, groups[1:], examples)
        review.apply_rules(self.connection, result, [])
        self.assertEqual(result['rules_saved'], 0)
        self.assertEqual({item['decision'] for item in result['suggestions']}, {'unsupported'})

    def test_unchanged_category_becomes_rule_and_uncertain_has_no_rule(self):
        for category, confidence, field, rule_count in [('EXAMPLE BROAD', 2, 'unchanged', 1), ('', 0, 'uncertain', 0)]:
            self.connection.execute('DELETE FROM merchant_rules')
            result, groups, examples = review.prepare(self.connection)
            with patch('category_review.classify_batch', return_value=[{
                'id': groups[0]['evaluation_id'], 'category': category, 'confidence': confidence, 'reason': ''
            }]):
                review.review_batch(result, groups, examples)
            review.apply_rules(self.connection, result, [])
            self.assertEqual(result[field], 2)
            self.assertEqual(result['rules_saved'], rule_count)

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
        self.assertIn('reusable rules automatically', payload['messages'][0]['content'])
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

    def test_start_saves_rules_without_acceptance_and_can_run_again(self):
        token = self.ready()
        self.assertIn(b'id="start-category-review"', self.client.get('/local-ai/category-review').data)
        self.assertEqual(self.start(token).status_code, 200)
        page = self.client.get('/local-ai/category-review')
        self.assertIn(b'EXAMPLE SPECIFIC', page.data)
        self.assertIn(b'Rule saved', page.data)
        self.assertNotIn(b'>Accept', page.data)
        self.assertNotIn(b'apply_mode', page.data)
        with self.application.db() as connection:
            self.assertEqual(review.load(connection)['rules_saved'], 1)
            self.assertEqual(connection.execute('SELECT source FROM merchant_rules').fetchone()[0], 'model')
        self.assertEqual(self.start(token).status_code, 200)

    def test_old_pending_report_does_not_block_new_run_or_apply_on_read(self):
        token = self.ready()
        with self.application.db() as connection:
            result, groups, examples = review.prepare(connection)
            with patch('category_review.classify_batch', side_effect=prediction):
                review.review_batch(result, groups, examples)
            result.pop('automatic_rules')
            result['status'] = 'completed'
            review.save(connection, result)
        page = self.client.get('/local-ai/category-review')
        self.assertIn(b'previous suggestion-only workflow', page.data)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)
        self.assertEqual(self.start(token).status_code, 200)

    def test_interruption_keeps_categories_unchanged_and_releases_lock(self):
        token = self.ready()
        with self.application.db() as connection:
            before = finances(connection)
        self.start(token, model=TimeoutError())
        with self.application.db() as connection:
            self.assertEqual(finances(connection), before)
            self.assertEqual(review.load(connection)['status'], 'interrupted')
        self.assertFalse(self.application.OLLAMA_EVALUATION_LOCK.locked())

    def test_partial_scan_keeps_completed_rules(self):
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
            self.assertEqual(finances(connection)['transactions'], before['transactions'])
            self.assertEqual(result['rules_saved'], 5)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 5)

    def test_concurrent_scan_is_rejected_without_replacing_results(self):
        token = self.ready()
        self.application.OLLAMA_EVALUATION_LOCK.acquire()
        try:
            self.assertEqual(self.start(token).status_code, 409)
            with self.application.db() as connection:
                self.assertIsNone(review.load(connection))
        finally:
            self.application.OLLAMA_EVALUATION_LOCK.release()

    def test_failed_batch_rolls_back_rules_and_report_together(self):
        token = self.ready()
        apply = review.apply_rules
        def fail_after_rule(*args):
            apply(*args)
            raise RuntimeError('Fictional save failure')
        with patch('category_review.apply_rules', side_effect=fail_after_rule):
            self.start(token)
        with self.application.db() as connection:
            result = review.load(connection)
            self.assertEqual(result['status'], 'interrupted')
            self.assertEqual(result['rules_saved'], 0)
            self.assertEqual(result['processed'], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0], 0)

    def test_local_ai_disabled_and_csrf_required(self):
        token = self.ready()
        self.assertEqual(self.client.post('/api/local-ai/category-review/start').status_code, 400)
        self.application.save_setting(self.application.LOCAL_AI_SETTING, '0')
        self.assertEqual(self.start(token).status_code, 403)
        self.assertIn(b'Turn on Local AI in Settings', self.client.get('/local-ai/category-review').data)
