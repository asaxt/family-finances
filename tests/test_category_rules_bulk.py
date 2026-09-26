import unittest
from urllib.parse import parse_qs, urlparse

from tests import test_app_setup


class CategoryRulesBulkTests(unittest.TestCase):
    setUp = test_app_setup.AppSetupTests.setUp
    tearDown = test_app_setup.AppSetupTests.tearDown
    csrf_token = staticmethod(test_app_setup.AppSetupTests.csrf_token)

    def ready(self):
        page = self.client.get('/setup')
        self.client.post('/setup', data={'csrf_token': self.csrf_token(page),
                         'password': 'fictional rules test password', 'confirmation': 'fictional rules test password'})
        with self.application.db() as connection:
            connection.execute("INSERT INTO connections (id, owner_name, institution, access_token) "
                               "VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', 'fake-token')")
            connection.execute("INSERT INTO accounts (id, connection_id, institution, name, type) "
                               "VALUES ('sample-account', 1, 'EXAMPLE BANK', 'EXAMPLE ACCOUNT', 'depository')")
            connection.executemany("INSERT INTO category_rules (name, flow_type) VALUES (?, 'spending')",
                                   [('EXAMPLE BROAD',), ('EXAMPLE SPECIFIC',), ('EXAMPLE MANUAL',)])
            for identifier, text in ((1, 'SAMPLE CAFE ALPHA'), (2, 'SAMPLE CAFE BETA'), (3, 'SAMPLE OTHER')):
                connection.execute(
                    "INSERT INTO merchant_rules (id, account_id, match_type, match_value, category) "
                    "VALUES (?, 'sample-account', 'description', ?, 'EXAMPLE BROAD')", (identifier, text))
                connection.execute(
                    "INSERT INTO transactions (id, account_id, amount, currency, description, merchant, pending, transacted_at, category, category_override, category_override_source) "
                    "VALUES (?, 'sample-account', 123, 'USD', ?, '', 0, '2001-01-02', 'Uncategorized', ?, ?)",
                    (f'sample-{identifier}', text, 'EXAMPLE MANUAL' if identifier == 1 else None, 'user' if identifier == 1 else None))
        page = self.client.get('/category-rules')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Review details for selected rules', page.data)
        return {'csrf_token': self.csrf_token(page), 'rule_ids': ['1', '2'], 'action': 'apply'}

    def rows(self, table):
        with self.application.db() as connection:
            return [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1')]

    def test_bulk_category_scope_and_treatment_preserve_transaction_overrides(self):
        data = self.ready()
        before = self.rows('transactions')
        data.update(category_change='EXAMPLE SPECIFIC', scope_change='all',
                    edit_category_treatment='on', category_flow_type='earned_income', return_q='SAMPLE',
                    return_category='EXAMPLE BROAD', return_scope='account', return_account='sample-account',
                    return_match_type='exact', return_sort='matches_desc')
        response = self.client.post('/api/category-rules/bulk', data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlparse(response.location).query), {
            'q': ['SAMPLE'], 'category': ['EXAMPLE BROAD'], 'scope': ['account'],
            'rule_account': ['sample-account'], 'match_type': ['exact'], 'sort': ['matches_desc']})
        rules = self.rows('merchant_rules')
        self.assertEqual([(row['category'], row['applies_all_accounts']) for row in rules],
                         [('EXAMPLE SPECIFIC', 1), ('EXAMPLE SPECIFIC', 1), ('EXAMPLE BROAD', 0)])
        self.assertEqual(self.rows('transactions'), before)
        with self.application.db() as connection:
            self.assertEqual(connection.execute("SELECT flow_type FROM category_rules WHERE name = 'EXAMPLE SPECIFIC'").fetchone()[0], 'earned_income')
            effective = {row['id']: row['effective_category'] for row in self.application.transaction_list(connection)}
            self.assertEqual(effective['sample-1'], 'EXAMPLE MANUAL')
            self.assertEqual(effective['sample-2'], 'EXAMPLE SPECIFIC')

    def test_shared_phrase_combines_selected_rules_only_with_explicit_choice(self):
        data = self.ready()
        before = self.rows('merchant_rules')
        data.update(category_change='__new__', new_category='EXAMPLE GROUP', category_flow_type='spending',
                    replace_match_text='on', match_value='SAMPLE CAFE', match_type_change='description_contains')
        failed = self.client.post('/api/category-rules/bulk', data=data, headers={'Accept': 'application/json'})
        self.assertEqual(failed.status_code, 400)
        self.assertIn('Combine identical', failed.json['error'])
        self.assertEqual(self.rows('merchant_rules'), before)
        self.assertNotIn('EXAMPLE GROUP', [row['name'] for row in self.rows('category_rules')])
        data['combine_duplicates'] = 'on'
        self.assertEqual(self.client.post('/api/category-rules/bulk', data=data).status_code, 302)
        rules = self.rows('merchant_rules')
        self.assertEqual(len(rules), 2)
        self.assertEqual((rules[0]['id'], rules[0]['match_type'], rules[0]['match_value'], rules[0]['category']),
                         (1, 'description_contains', 'SAMPLE CAFE', 'EXAMPLE GROUP'))
        self.assertEqual(rules[1], before[2])

    def test_unselected_conflicts_and_invalid_text_rollback_entire_update(self):
        data = self.ready()
        before = self.rows('merchant_rules')
        data.update(rule_ids=['1'], replace_match_text='on', match_value='SAMPLE CAFE BETA',
                    category_change='__new__', new_category='EXAMPLE GROUP', category_flow_type='earned_income',
                    scope_change='all', combine_duplicates='on')
        response = self.client.post('/api/category-rules/bulk', data=data, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('unselected', response.json['error'])
        self.assertEqual(self.rows('merchant_rules'), before)
        self.assertNotIn('EXAMPLE GROUP', [row['name'] for row in self.rows('category_rules')])
        for text in (' ', 'X' * 256):
            data['match_value'] = text
            self.assertEqual(self.client.post('/api/category-rules/bulk', data=data, headers={'Accept': 'application/json'}).status_code, 400)
            self.assertEqual(self.rows('merchant_rules'), before)

    def test_combining_different_categories_is_blocked(self):
        data = self.ready()
        with self.application.db() as connection:
            connection.execute("UPDATE merchant_rules SET category = 'EXAMPLE SPECIFIC' WHERE id = 2")
        before = self.rows('merchant_rules')
        data.update(replace_match_text='on', match_value='SAMPLE CAFE',
                    match_type_change='description_contains', combine_duplicates='on')
        response = self.client.post('/api/category-rules/bulk', data=data, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('different categories', response.json['error'])
        self.assertEqual(self.rows('merchant_rules'), before)

    def test_no_change_preserves_legacy_fields_and_delete_leaves_transactions(self):
        data = self.ready()
        with self.application.db() as connection:
            connection.execute("UPDATE merchant_rules SET flow_type = 'transfer', spending_override = 'exclude' WHERE id = 1")
        before = self.rows('merchant_rules')
        transactions = self.rows('transactions')
        self.client.post('/api/category-rules/bulk', data=data)
        self.assertEqual(self.rows('merchant_rules'), before)
        data.update(action='delete', match_value='', replace_match_text='on', category_change='__new__')
        self.client.post('/api/category-rules/bulk', data=data)
        self.assertEqual(self.rows('merchant_rules'), [before[2]])
        self.assertEqual(self.rows('transactions'), transactions)

    def test_style_change_keeps_each_text_and_missing_selection_is_rejected(self):
        data = self.ready()
        data.update(match_type_change='description_contains')
        self.client.post('/api/category-rules/bulk', data=data)
        rules = self.rows('merchant_rules')
        self.assertEqual([row['match_value'] for row in rules[:2]], ['SAMPLE CAFE ALPHA', 'SAMPLE CAFE BETA'])
        self.assertTrue(all(row['match_type'] == 'description_contains' for row in rules[:2]))
        for ids in (['1', '9999'], ['invalid']):
            data.update(rule_ids=ids, scope_change='all')
            self.assertEqual(self.client.post('/api/category-rules/bulk', data=data, headers={'Accept': 'application/json'}).status_code, 400)
            self.assertEqual(self.rows('merchant_rules'), rules)
        page = self.client.get('/category-rules?q=ALPHA')
        self.assertEqual(page.data.count(b'class="rule-select"'), 1)
