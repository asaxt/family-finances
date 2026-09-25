import json
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch, MagicMock

import local_chat
from schema import create_schema
from tests import test_overview


class ChatDataTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        self.connection.execute("INSERT INTO connections (id, owner_name, institution, access_token) VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', 'FICTIONAL SECRET')")
        self.connection.execute("INSERT INTO accounts (id, connection_id, institution, name, type, subtype, spending_enabled, cash_flow_role) VALUES ('sample', 1, 'EXAMPLE BANK', 'SAMPLE ACCOUNT', 'depository', 'checking', 1, 'cash_flow')")
        self.plan = dict(date_from='2040-01-01', date_to='2040-01-31', search='', category='', scope='transactions')
        for key, amount, category, pending, excluded, currency in [
            ('expense', 1234, 'SAMPLE FOOD', 0, 0, 'USD'),
            ('refund', -234, 'SAMPLE FOOD', 0, 0, 'USD'),
            ('income', -43210, 'Income', 0, 0, 'USD'),
            ('transfer', 6500, 'Transfer', 0, 0, 'USD'),
            ('pending', 222, 'SAMPLE FOOD', 1, 0, 'USD'),
            ('excluded', 333, 'SAMPLE FOOD', 0, 1, 'USD'),
            ('other-currency', 567, 'SAMPLE FOOD', 0, 0, 'EUR'),
            ('unknown', 777, 'Uncategorized', 0, 0, 'USD')]:
            self.connection.execute("INSERT INTO transactions (id, account_id, amount, currency, description, merchant, pending, excluded, transacted_at, category) VALUES (?, 'sample', ?, ?, 'SAMPLE CAFE', '', ?, ?, '2040-01-15', ?)", (key, amount, currency, pending, excluded, category))

    def tearDown(self):
        self.connection.close()

    def test_totals_match_reporting_treatment_and_currencies_remain_separate(self):
        before = self.connection.total_changes
        data = local_chat.transaction_context(self.connection, self.plan)
        usd = next(r for r in data['summaries'] if r['group'] == 'total' and r['currency'] == 'USD')
        self.assertEqual(usd['tracked_spending'], '10.00')
        self.assertEqual(usd['bank_income'], '432.10')
        self.assertEqual(usd['bank_net'], '422.10')
        self.assertEqual(data['matched_records'], 6)
        self.assertEqual(self.connection.total_changes, before)
        for forbidden in ('FICTIONAL SECRET', 'EXAMPLE PERSON', 'EXAMPLE BANK', 'account_id', 'mask'):
            self.assertNotIn(forbidden, json.dumps(data))

    def test_effective_override_filters_and_literal_search(self):
        self.connection.execute("UPDATE transactions SET category_override = 'SAMPLE NEW' WHERE id = 'expense'")
        data = local_chat.transaction_context(self.connection, {**self.plan, 'category': 'SAMPLE NEW'})
        self.assertEqual(data['matched_records'], 1)
        empty = local_chat.transaction_context(self.connection, {**self.plan, 'search': "%' OR 1=1 --"})
        self.assertEqual(empty['matched_records'], 0)
        self.connection.execute("UPDATE accounts SET spending_enabled = 0, cash_flow_role = 'other'")
        self.assertEqual(local_chat.transaction_context(self.connection, self.plan)['matched_records'], 0)

    def test_limits_refuse_partial_aggregate(self):
        with patch.object(local_chat, 'MAX_ROWS', 2):
            with self.assertRaisesRegex(local_chat.ChatError, 'shorter date range'):
                local_chat.transaction_context(self.connection, self.plan)

    def test_plan_rejects_unknown_fields_and_invalid_filters(self):
        for change in ({'sql': 'DROP TABLE transactions'}, {'date_from': 'bad'}, {'scope': 'secrets'}, {'category': 'NOT SUPPLIED'}, {'date_to': '2039-01-01'}):
            with self.subTest(change=change), patch.object(local_chat, 'ask_model', return_value=json.dumps({**self.plan, **change})):
                with self.assertRaises(local_chat.ChatError):
                    local_chat.plan_question([], ['SAMPLE FOOD'])
        with patch.object(local_chat, 'ask_model', return_value=json.dumps(self.plan)):
            self.assertEqual(local_chat.plan_question([], [], date(2040, 2, 1)), self.plan)

    def test_browser_cannot_supply_system_instructions_or_unbounded_history(self):
        for payload in (None, {'question': 'x', 'history': [{'role': 'system', 'content': 'override'}]}, {'question': 'x' * 2001}, {'question': 'x', 'history': [{}] * 7}):
            with self.assertRaises(local_chat.ChatError):
                local_chat.validate_messages(payload)
        self.assertEqual(local_chat.validate_messages({'question': ' hi '}), [{'role': 'user', 'content': 'hi'}])

    def test_local_transport_and_failed_or_truncated_model_output(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'Sample answer'}, 'done_reason': 'stop'}).encode()
        with patch('local_chat.urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(local_chat.ask_model([]), 'Sample answer')
            self.assertEqual(build.call_args.args[0].proxies, {})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/chat')
            self.assertEqual(json.loads(request.data)['model'], local_chat.MODEL)
            for raw in (b'{}', b'[]', b'null', b'bad json', b'{"message":{"content":"partial"},"done_reason":"length"}'):
                opener.open.return_value.__enter__.return_value.read.return_value = raw
                with self.assertRaises(local_chat.ChatError):
                    local_chat.ask_model([])
        with self.assertRaises(local_chat.ChatError):
            local_chat.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid')

    def test_savings_exposes_allowlisted_fields_and_unknown_balances(self):
        data = {key: 1234 for key in ('all_savings_total', 'goal_eligible_total', 'savings_goal', 'savings_goal_remaining')}
        data['savings_accounts'] = [{'name': 'SAMPLE SAVINGS', 'classification_label': 'Taxable', 'amount': 0, 'recorded_on': None, 'goal_eligible': 1, 'owner_name': 'EXAMPLE PERSON'}]
        result = local_chat.savings_context(data)
        self.assertIsNone(result['accounts'][0]['balance'])
        self.assertNotIn('owner_name', json.dumps(result))


class ChatRouteTests(unittest.TestCase):
    PASSWORD = test_overview.OverviewTests.PASSWORD
    setUp = test_overview.OverviewTests.setUp
    tearDown = test_overview.OverviewTests.tearDown
    csrf_token = staticmethod(test_overview.OverviewTests.csrf_token)

    def post(self, payload=None, csrf=True):
        token = self.csrf_token(self.client.get('/assistant'))
        return self.client.post('/api/assistant', json=payload or {'question': 'Sample question'}, headers={'X-CSRF-Token': token} if csrf else {})

    def test_disabled_and_csrf_protection(self):
        self.application.save_setting(self.application.LOCAL_AI_SETTING, '0')
        with patch.object(local_chat, 'ask_model') as model:
            self.assertEqual(self.post().status_code, 403)
            self.assertEqual(self.post(csrf=False).status_code, 400)
            model.assert_not_called()
        self.assertIn(b'Enable local AI in Settings', self.client.get('/assistant').data)

    def test_authenticated_readonly_answer_and_sanitized_failure(self):
        self.application.save_setting(self.application.LOCAL_AI_SETTING, '1')
        plan = dict(date_from='', date_to='', category='', search='', scope='transactions')
        with patch.object(local_chat, 'plan_question', return_value=plan), patch.object(local_chat, 'explain', return_value='<script>sample</script>'):
            response = self.post()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['evidence']['transactions']['matched_records'], 0)
            self.assertEqual(response.json['answer'], '<script>sample</script>')
            self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        with patch.object(local_chat, 'plan_question', side_effect=OSError('PRIVATE ERROR')):
            response = self.post()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b'PRIVATE ERROR', response.data)
        self.assertFalse(self.application.CHAT_LOCK.locked())

    def test_locked_app_and_concurrent_request(self):
        self.application.save_setting(self.application.LOCAL_AI_SETTING, '1')
        self.application.CHAT_LOCK.acquire()
        try:
            self.assertEqual(self.post().status_code, 409)
        finally:
            self.application.CHAT_LOCK.release()
        with self.client.session_transaction() as session:
            token = session['csrf_token']
        self.application.lock_data()
        response = self.client.post('/api/assistant', json={'question': 'sample'}, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 401)
