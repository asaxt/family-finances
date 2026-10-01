import json
import re
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from analytics import transaction_list
from tests import test_app_setup
from tests.test_category_review import seed


class CashFlowLinkTests(unittest.TestCase):
    setUp = test_app_setup.AppSetupTests.setUp
    tearDown = test_app_setup.AppSetupTests.tearDown
    csrf_token = staticmethod(test_app_setup.AppSetupTests.csrf_token)

    def ready(self):
        page = self.client.get('/setup')
        self.client.post('/setup', data={'csrf_token': self.csrf_token(page),
            'password': 'fictional link test password', 'confirmation': 'fictional link test password'})
        self.client.get('/transactions')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']
        with self.application.db() as connection:
            seed(connection)
            connection.execute('DELETE FROM transactions')
            connection.execute("UPDATE accounts SET cash_flow_role='cash_flow', spending_enabled=1")
            connection.execute("INSERT INTO accounts (id,connection_id,name,institution,type,cash_flow_role,spending_enabled) VALUES ('sample-ignored',1,'EXAMPLE IGNORED ACCOUNT','EXAMPLE BANK','depository','other',0)")
            for identifier, amount, category, pending, excluded, account, currency in [
                ('income', -101, 'Income', 0, 0, 'sample-account', 'USD'),
                ('positive-income', 102, 'Income', 0, 0, 'sample-account', 'USD'),
                ('expense', 103, 'EXAMPLE BROAD', 0, 0, 'sample-account', 'USD'),
                ('refund', -7, 'EXAMPLE BROAD', 0, 0, 'sample-account', 'USD'),
                ('transfer-in', -105, 'Transfer', 0, 0, 'sample-account', 'USD'),
                ('transfer-out', 106, 'Transfer', 0, 0, 'sample-account', 'USD'),
                ('pending', 107, 'EXAMPLE BROAD', 1, 0, 'sample-account', 'USD'),
                ('excluded', 108, 'EXAMPLE BROAD', 0, 1, 'sample-account', 'USD'),
                ('ignored', 109, 'EXAMPLE BROAD', 0, 0, 'sample-ignored', 'USD'),
                ('foreign', 110, 'EXAMPLE BROAD', 0, 0, 'sample-account', 'EUR'),
                ('unknown', 111, 'Uncategorized', 0, 0, 'sample-account', 'USD'),
            ]:
                connection.execute("INSERT INTO transactions (id,account_id,amount,currency,description,merchant,pending,excluded,transacted_at,category) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ('sample-' + identifier, account, amount, currency, 'EXAMPLE ' + identifier.upper(), 'SAMPLE MERCHANT', pending, excluded, '2004-02-12', category))
            connection.execute("INSERT INTO transactions (id,account_id,amount,currency,description,pending,transacted_at,category) VALUES ('sample-future','sample-account',113,'USD','EXAMPLE FUTURE',0,'2004-03-20','EXAMPLE BROAD')")

    def test_metric_filters_match_cash_flow_contributors(self):
        self.ready()
        expected = {'income': {'income'}, 'spending': {'expense', 'refund', 'foreign'},
                    'net': {'income', 'expense', 'refund', 'foreign'},
                    'transfers_in': {'transfer-in'}, 'transfers_out': {'transfer-out'}}
        with self.application.db() as connection:
            for metric, identifiers in expected.items():
                rows = transaction_list(connection, cash_flow_metric=metric, date_from='2004-02-01', date_to='2004-02-29', include_excluded=True)
                self.assertEqual({row['id'] for row in rows}, {'sample-' + value for value in identifiers})
            rows = transaction_list(connection, cash_flow_metric='spending', date_from='2004-02-01', date_to='2004-02-29', currency='USD')
            self.assertEqual(sum(row['amount'] for row in rows), 96)
            self.assertEqual(transaction_list(connection, cash_flow_metric='net', connection_id=999), [])
            self.assertEqual(transaction_list(connection, cash_flow_metric='net', account_id='sample-ignored'), [])

    def test_chart_links_preserve_scope_and_exact_periods(self):
        self.ready()
        with patch('analytics.date', wraps=date) as clock:
            clock.today.return_value = date(2004, 3, 7)
            page = self.client.get('/cash-flow?growth_window=6&growth_comparison=12&person=1&account=sample-account')
        self.assertEqual(page.status_code, 200)
        growth = json.loads(re.search(rb'<script id="cash-flow-growth-data" type="application/json">(.*?)</script>', page.data).group(1))
        last = growth['points'][-1]
        for period, first, last_date in [('current','2003-09-01','2004-02-29'), ('prior','2002-09-01','2003-02-28')]:
            query = parse_qs(urlsplit(last[period]['links']['income']).query)
            self.assertEqual(query, {'date_from':[first], 'date_to':[last_date], 'person':['1'], 'account':['sample-account'],
                                    'purpose':['cash_flow'], 'cash_flow_metric':['income'], 'currency':['USD']})
        months = json.loads(re.search(rb'const cashFlowMonths = (.*?);', page.data).group(1))
        february = next(point for point in months if point['month'] == '2004-02')
        query = parse_qs(urlsplit(february['links']['spending']).query)
        self.assertEqual(query['date_to'], ['2004-02-29'])
        self.assertNotIn('currency', query)
        transactions = self.client.get(february['links']['spending'])
        self.assertIn(b'EXAMPLE EXPENSE', transactions.data)
        self.assertIn(b'EXAMPLE REFUND', transactions.data)
        self.assertEqual(set(re.findall(rb'data-id="([^"]+)"', transactions.data)),
                         {b'sample-expense', b'sample-refund', b'sample-foreign'})
        self.assertIn(b'Posted transactions in included accounts', transactions.data)
        self.assertIn(b'name="return_cash_flow_metric" value="spending"', transactions.data)
        self.assertIn(b'value="spending" selected', transactions.data)
        self.assertIn(b'Monthly details', page.data)

    def test_drilldown_filters_survive_transaction_edit(self):
        self.ready()
        response = self.client.post('/api/transaction/sample-expense', data={
            'csrf_token': self.token, 'category_edited': '', 'custom_description': 'EXAMPLE EDITED',
            'return_cash_flow_metric': 'spending', 'return_currency': 'USD', 'return_purpose': 'cash_flow',
            'date_from': '2004-02-01', 'date_to': '2004-02-29', 'account': 'sample-account', 'person': '1',
        })
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response.location).query)
        self.assertEqual(query['cash_flow_metric'], ['spending'])
        self.assertEqual(query['currency'], ['USD'])
        self.assertEqual(query['date_to'], ['2004-02-29'])
        self.assertEqual(query['account'], ['sample-account'])
        self.assertEqual(self.client.get('/transactions?cash_flow_metric=unknown').status_code, 400)
        self.assertEqual(self.client.get('/transactions?currency=unknown').status_code, 400)
