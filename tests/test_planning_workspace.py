import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_projections
from tests.test_projections import sample_plan
from vault import VaultError


REFS = {'people': [{'choice': '', 'fields': []}, {'choice': '', 'fields': []}], 'taxable_from_savings': False}
TREND = {'annual_spending': 65400, 'date_from': '2038-01-01', 'date_to': '2038-12-31'}


class WorkspaceTests(unittest.TestCase):
    setUp = test_projections.ProjectionRouteTests.setUp
    tearDown = test_projections.ProjectionRouteTests.tearDown
    complete_setup = test_projections.ProjectionRouteTests.complete_setup
    complete_category_setup = test_projections.ProjectionRouteTests.complete_category_setup
    csrf_token = staticmethod(test_projections.ProjectionRouteTests.csrf_token)
    enable_mirror = test_projections.ProjectionRouteTests.enable_mirror

    def prepare(self, mirror=False):
        self.enable_mirror() if mirror else self.complete_setup()
        self.token = self.csrf_token(self.client.get('/plan'))

    def save(self, **changes):
        library = self.client.get('/api/plans').json
        payload = dict(action='save', name='EXAMPLE PLAN', plan=sample_plan(), references=REFS,
                       version=library.get('version', ''), id=None, source_id=None) | changes
        with patch.object(self.application, 'planning_spending_trend', return_value=TREND):
            return self.client.post('/api/plans', json=payload, headers={'X-CSRF-Token': self.token})

    def test_mirror_library_is_encrypted_persistent_and_never_mutates_snapshot(self):
        self.prepare(mirror=True)
        original = self.application.VAULT_PATH.read_bytes()
        saved = self.save()
        self.assertEqual(saved.status_code, 200)
        item = saved.json['plans'][0]
        companion = self.application.DATA_ROOT / '.development-workspace.vault'
        self.assertTrue(companion.exists())
        self.assertNotIn(b'EXAMPLE PLAN', companion.read_bytes())
        self.assertEqual(companion.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), original)
        self.application.lock_data()
        self.application.unlock_data(self.password)
        self.assertEqual(self.client.get('/api/plans').json['plans'][0]['id'], item['id'])
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), original)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
        with self.client.session_transaction() as session:
            self.assertNotIn('EXAMPLE PLAN', json.dumps(dict(session)))

    def test_saved_source_is_frozen_and_live_source_can_refresh(self):
        self.prepare()
        saved = self.save().json['plans'][0]
        payload = {'baseline': saved['plan'], 'comparison': saved['plan'], 'source_id': saved['id']}
        with patch.object(self.application, 'planning_spending_trend', return_value=TREND | {'annual_spending': 91200}):
            result = self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': self.token})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['comparison']['annual_spending'], 654)
            self.assertEqual(result.json['source'], saved['source'])
            result = self.client.post('/api/projections', json=payload | {'source_id': None}, headers={'X-CSRF-Token': self.token})
            self.assertEqual(result.json['comparison']['annual_spending'], 912)

    def test_stale_save_duplicate_names_delete_and_csrf(self):
        self.prepare()
        saved = self.save().json
        item = saved['plans'][0]
        self.assertEqual(self.save(version='old', id=item['id']).status_code, 409)
        self.assertEqual(self.save().status_code, 400)
        self.assertEqual(self.client.post('/api/plans', json={}).status_code, 400)
        changed = self.save(id=item['id'], plan=sample_plan(retirement_age=43)).json
        self.assertEqual(len(changed['plans']), 1)
        delete = dict(action='delete', id=item['id'], version=changed['version'])
        response = self.client.post('/api/plans', json=delete, headers={'X-CSRF-Token': self.token})
        self.assertEqual(response.json['plans'], [])
        self.assertNotIn('active_id', response.json)

    def test_bad_events_cannot_be_saved_and_missing_history_stays_unknown(self):
        self.prepare()
        event = {'name': 'EXAMPLE PROJECT', 'start_year': 2190, 'years': 1, 'annual_amount': 37}
        self.assertEqual(self.save(plan=sample_plan(life_events=[event])).status_code, 400)
        plan = sample_plan(annual_lifestyle_spending=321)
        with patch.object(self.application, 'planning_spending_trend', return_value=TREND | {'annual_spending': None}):
            response = self.client.post('/api/projections', json={'baseline': plan, 'comparison': plan}, headers={'X-CSRF-Token': self.token})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['comparison']['annual_spending'])
        self.assertEqual(response.json['comparison']['adjusted_annual_spending'], 321)

    def test_empty_or_tampered_companion_is_not_silently_overwritten(self):
        self.prepare(mirror=True)
        self.save()
        path = self.application.DATA_ROOT / '.development-workspace.vault'
        for damaged in (b'', b'broken encrypted example'):
            path.write_bytes(damaged)
            self.assertEqual(self.client.get('/api/plans').status_code, 409)
            self.assertEqual(self.save().status_code, 409)
            self.assertEqual(path.read_bytes(), damaged)

    def test_trend_cache_reuses_unchanged_data_and_clears_on_change_and_lock(self):
        self.prepare()
        self.application.PLANNING_TREND_CACHE = None
        with patch.object(self.application, 'planning_spending_trend', return_value=TREND) as compute:
            with self.application.db() as connection:
                self.application.cached_planning_trend(connection)
                self.application.cached_planning_trend(connection)
                self.assertEqual(compute.call_count, 1)
                connection.execute("INSERT INTO settings (key, value) VALUES ('EXAMPLE CACHE CHANGE', '1')")
                self.application.cached_planning_trend(connection)
                self.assertEqual(compute.call_count, 2)
        self.application.lock_data()
        self.assertIsNone(self.application.PLANNING_TREND_CACHE)

    def test_fast_savings_path_matches_latest_balances_without_history(self):
        self.prepare()
        with self.application.db() as connection:
            connection.execute("INSERT INTO manual_accounts (id,institution,name,owner_name,classification) VALUES (1,'EXAMPLE BANK','SAMPLE INVESTMENT','EXAMPLE PERSON','taxable')")
            connection.executemany('INSERT INTO savings_snapshots (manual_account_id,amount,recorded_on) VALUES (1,?,?)', [(123,'2000-01-01'),(456,'2001-01-01')])
        full = self.application.savings_data()
        fast = self.application.savings_data(include_history=False)
        for key in ('savings_accounts', 'classification_totals', 'goal_eligible_total', 'savings_goal_percent'):
            self.assertEqual(full[key], fast[key])
        self.assertEqual(fast['savings_timeline'], [])
        self.assertEqual(len(fast['savings_history']), 1)
