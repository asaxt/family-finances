import unittest
from datetime import date
from types import SimpleNamespace

from analytics import transaction_list
from tests import test_app_setup


class TransactionDescriptionTests(unittest.TestCase):
    setUp = test_app_setup.AppSetupTests.setUp
    tearDown = test_app_setup.AppSetupTests.tearDown
    csrf_token = staticmethod(test_app_setup.AppSetupTests.csrf_token)
    post_with_rule_review = test_app_setup.AppSetupTests.post_with_rule_review
    prepare_bulk_review = test_app_setup.AppSetupTests.prepare_bulk_review

    def ready(self):
        self.token = self.prepare_bulk_review()['csrf_token']
        with self.application.db() as connection:
            connection.execute("INSERT INTO merchant_rules (account_id, match_type, match_value, category) VALUES ('sample-a', 'description_contains', 'SAMPLE CAFE', 'EXAMPLE CATEGORY')")

    def save(self, value, identifier='sample-1', **extra):
        return self.client.post(f'/api/transaction/{identifier}/description', data={
            'csrf_token': self.token, 'custom_description': value,
            'return_view': 'all', 'return_purpose': 'all', **extra,
        })

    def test_save_changes_only_description_and_clear_restores_original(self):
        self.ready()
        with self.application.db() as connection:
            before = dict(connection.execute("SELECT * FROM transactions WHERE id = 'sample-1'").fetchone())
            rules = [tuple(row) for row in connection.execute('SELECT * FROM merchant_rules')]
            categories = [tuple(row) for row in connection.execute('SELECT * FROM category_rules')]
        response = self.save('  EXAMPLE PERSONAL LABEL  ', category_choice='Income', remember_match='on', excluded='on')
        self.assertEqual(response.status_code, 302)
        with self.application.db() as connection:
            after = dict(connection.execute("SELECT * FROM transactions WHERE id = 'sample-1'").fetchone())
            self.assertEqual(after, {**before, 'custom_description': 'EXAMPLE PERSONAL LABEL'})
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM merchant_rules')], rules)
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM category_rules')], categories)
            self.assertIsNone(connection.execute("SELECT custom_description FROM transactions WHERE id = 'sample-2'").fetchone()[0])
        page = self.client.get(response.location)
        self.assertIn(b'<div class="transaction-name">EXAMPLE PERSONAL LABEL</div>', page.data)
        self.assertIn(b'Original description: SAMPLE CAFE ALPHA', page.data)
        self.assertIn(b'Edit description', page.data)
        self.assertEqual(self.save('   ').status_code, 302)
        with self.application.db() as connection:
            self.assertEqual(dict(connection.execute("SELECT * FROM transactions WHERE id = 'sample-1'").fetchone()), before)

    def test_search_original_and_custom_text_and_sort_displayed_names(self):
        self.ready()
        self.save('ZZZ EXAMPLE LABEL')
        with self.application.db() as connection:
            for query in ('zzz example', 'cafe alpha'):
                rows = transaction_list(connection, query=query, include_excluded=True)
                self.assertEqual([row['id'] for row in rows], ['sample-1'])
                self.assertEqual(rows[0]['effective_category'], 'EXAMPLE CATEGORY')
            rows = transaction_list(connection, include_excluded=True, sort='merchant_asc')
            self.assertEqual(rows[-1]['id'], 'sample-1')
            rows = transaction_list(connection, include_excluded=True, sort='merchant_desc')
            self.assertEqual(rows[0]['id'], 'sample-1')
        response = self.save('EXAMPLE LABEL', account='sample-a', return_q='sample', return_sort='merchant_asc')
        self.assertIn('account=sample-a', response.location)
        self.assertIn('q=sample', response.location)
        self.assertIn('sort=merchant_asc', response.location)

    def test_validation_authentication_and_html_escaping(self):
        self.ready()
        self.assertEqual(self.save('X' * 255).status_code, 302)
        for value in ('X' * 256, 'EXAMPLE\nLABEL', 'EXAMPLE\x00LABEL'):
            self.assertEqual(self.save(value).status_code, 400)
        self.assertEqual(self.save('EXAMPLE', identifier='not-present').status_code, 404)
        self.assertEqual(self.client.post('/api/transaction/sample-1/description', data={'csrf_token': self.token}).status_code, 400)
        self.assertEqual(self.client.post('/api/transaction/sample-1/description', data={'custom_description': 'EXAMPLE'}).status_code, 400)
        with self.application.db() as connection:
            self.assertEqual(connection.execute("SELECT custom_description FROM transactions WHERE id = 'sample-1'").fetchone()[0], 'X' * 255)
        self.save('<script>EXAMPLE</script>')
        page = self.client.get('/transactions?view=all&purpose=all')
        self.assertNotIn(b'<script>EXAMPLE</script>', page.data)
        self.assertIn(b'&lt;script&gt;EXAMPLE&lt;/script&gt;', page.data)
        with self.client.session_transaction() as session:
            session.pop('authenticated', None)
        self.assertEqual(self.save('EXAMPLE').status_code, 401)

    def test_description_survives_encrypted_reopen_and_bank_refresh(self):
        self.ready()
        self.save('EXAMPLE SAVED LABEL')
        self.application.lock_data()
        self.application.unlock_data('fictional bulk test password')
        with self.application.db() as connection:
            self.application.save_transaction(connection, SimpleNamespace(
                transaction_id='sample-1', account_id='sample-a', amount=1.23,
                iso_currency_code='USD', name='SAMPLE UPDATED BANK TEXT',
                merchant_name='SAMPLE MERCHANT', pending=False, date=date(2001, 1, 2),
            ))
            row = connection.execute("SELECT custom_description, description FROM transactions WHERE id = 'sample-1'").fetchone()
            self.assertEqual(tuple(row), ('EXAMPLE SAVED LABEL', 'SAMPLE UPDATED BANK TEXT'))

    def test_posted_replacement_keeps_pending_description(self):
        self.ready()
        self.save('EXAMPLE PENDING LABEL')
        with self.application.db() as connection:
            connection.execute("UPDATE transactions SET pending = 1 WHERE id = 'sample-1'")
            self.application.save_transaction(connection, SimpleNamespace(
                transaction_id='sample-posted', pending_transaction_id='sample-1',
                account_id='sample-a', amount=1.23, iso_currency_code='USD',
                name='SAMPLE POSTED TEXT', merchant_name='SAMPLE MERCHANT',
                pending=False, date=date(2001, 1, 2),
            ))
            self.assertEqual(connection.execute("SELECT custom_description FROM transactions WHERE id = 'sample-posted'").fetchone()[0], 'EXAMPLE PENDING LABEL')


if __name__ == '__main__':
    unittest.main()
