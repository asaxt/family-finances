import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from development_mirror import prepare_snapshot
from vault import EncryptedDatabase, VaultError, unlock_key
from tests import test_development_mode


class ProductionMirrorTests(unittest.TestCase):
    setUp = test_development_mode.DevelopmentModeTests.setUp
    tearDown = test_development_mode.DevelopmentModeTests.tearDown
    csrf_token = staticmethod(test_development_mode.DevelopmentModeTests.csrf_token)
    complete_setup = test_development_mode.DevelopmentModeTests.complete_setup
    complete_category_setup = test_development_mode.DevelopmentModeTests.complete_category_setup

    def enable_mirror(self):
        self.complete_setup()
        with self.application.db() as connection:
            connection.execute("INSERT INTO category_rules (name, flow_type) VALUES ('SAMPLE PRODUCTION CATEGORY', 'spending')")
            connection.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '0')", (self.application.LOCAL_AI_SETTING,))
        self.application.MIRROR_METADATA.write_text(json.dumps({'copied_at': '2040-01-15T00:00:00+00:00'}))
        self.mode = patch.object(self.application, 'READ_ONLY_MIRROR', True)
        self.mode.start()
        self.addCleanup(self.mode.stop)
        self.application.lock_data()
        with patch.object(self.application, 'create_recurring_category_rules') as reconcile:
            self.application.unlock_data(self.password)
            reconcile.assert_not_called()

    def test_snapshot_is_separate_and_production_bytes_remain_unchanged(self):
        self.complete_setup()
        source = Path(self.temporary.name)
        before = {name: (source / name).read_bytes() for name in ('family-finances.vault', '.auth.json')}
        with tempfile.TemporaryDirectory() as destination:
            first = prepare_snapshot(source, destination)
            second = prepare_snapshot(source, destination)
            self.assertNotEqual(first, second)
            for name, content in before.items():
                self.assertEqual((first / name).read_bytes(), content)
                self.assertEqual((source / name).read_bytes(), content)
                self.assertEqual((first / name).stat().st_mode & 0o777, 0o600)
                self.assertFalse(os.path.samefile(first / name, source / name))
            config = json.loads(before['.auth.json'])
            key = unlock_key(self.password, config['vault_key'])
            mirror = EncryptedDatabase(first / 'family-finances.vault')
            mirror.unlock(key)
            try:
                # Schema translation happens only on the copy, before freezing it.
                with mirror.connection() as connection:
                    connection.execute("INSERT INTO settings (key, value) VALUES ('sample_translation', '1')")
                mirror.make_read_only()
                with self.assertRaises(sqlite3.OperationalError):
                    with mirror.connection() as connection:
                        connection.execute('DELETE FROM category_rules')
                with self.assertRaises(VaultError):
                    mirror.persist()
                for name, content in before.items():
                    self.assertEqual((source / name).read_bytes(), content)
            finally:
                mirror.lock()
        for destination in (source, source / 'child', source.parent):
            with self.assertRaises(ValueError):
                prepare_snapshot(source, destination)

    def test_relaunch_snapshot_reflects_latest_production_categories(self):
        self.complete_setup()
        with tempfile.TemporaryDirectory() as destination:
            first = prepare_snapshot(self.temporary.name, destination)
            with self.application.db() as connection:
                connection.execute("INSERT INTO category_rules (name, flow_type) VALUES ('SAMPLE NEW PRODUCTION CATEGORY', 'spending')")
            second = prepare_snapshot(self.temporary.name, destination)
            self.assertNotEqual((first / 'family-finances.vault').read_bytes(), (second / 'family-finances.vault').read_bytes())
            auth = json.loads((second / '.auth.json').read_bytes())
            mirror = EncryptedDatabase(second / 'family-finances.vault')
            mirror.unlock(unlock_key(self.password, auth['vault_key']))
            try:
                with mirror.connection() as connection:
                    self.assertIsNotNone(connection.execute("SELECT name FROM category_rules WHERE name = 'SAMPLE NEW PRODUCTION CATEGORY'").fetchone())
            finally:
                mirror.lock()

    def test_all_data_writes_blocked_but_session_preferences_still_work(self):
        self.enable_mirror()
        before = self.application.VAULT_PATH.read_bytes()
        page = self.client.get('/settings')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Read-only production snapshot', page.data)
        token = self.csrf_token(page)
        for url in ('/api/local-ai/category-review/start', '/api/local-ai/category-review/decide', '/api/local-ai/evaluation', '/api/category-rules/bulk', '/api/transaction/sample', '/api/transactions/bulk', '/category-setup', '/statement-import', '/api/account-roles', '/api/savings', '/api/password', '/api/app-name'):
            response = self.client.post(url, data={'csrf_token': token})
            self.assertEqual(response.status_code, 403, url)
            self.assertIn('read-only', response.json['error'])
        response = self.client.post('/api/local-ai', data={'csrf_token': token, 'enabled': 'on'})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertTrue(session['mirror_local_ai_enabled'])
        response = self.client.post('/api/overview-lookback', data={'csrf_token': token, 'lookback_days': '90'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), before)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT value FROM settings WHERE key = ?', (self.application.LOCAL_AI_SETTING,)).fetchone()[0], '0')
        with self.assertRaises(sqlite3.OperationalError):
            self.application.save_setting('sample_write', 'blocked')

    def test_readonly_pages_and_assistant_remain_available(self):
        self.enable_mirror()
        before = self.application.VAULT_PATH.read_bytes()
        for url in ('/', '/transactions', '/categories', '/category-rules', '/category-setup', '/cash-flow', '/trends', '/savings', '/settings', '/assistant'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        transactions = self.client.get('/transactions')
        self.assertNotIn(b'id="run-local-categorization"', transactions.data)
        token = self.csrf_token(transactions)
        self.client.post('/api/local-ai', data={'csrf_token': token, 'enabled': 'on'})
        import local_chat
        plan = dict(date_from='', date_to='', category='', search='', scope='transactions')
        with patch.object(local_chat, 'plan_question', return_value=plan), patch.object(local_chat, 'explain', return_value='Sample explanation'):
            response = self.client.post('/api/assistant', json={'question': 'Sample question'}, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
        self.assertTrue(self.client.get('/health').json['read_only_mirror'])
