import copy
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import planning_profiles as profiles
from projections import ProjectionError
from schema import create_schema
from tests import test_projections
from tests.test_projections import sample_plan


def account(owner='EXAMPLE PERSON', classification='pre_tax', amount=12300, recorded_on='2001-01-01'):
    return dict(owner_name=owner, classification=classification, amount=amount,
                recorded_on=recorded_on, name='SAMPLE INVESTMENT')


def person(**changes):
    return dict(name='EXAMPLE PERSON', birth_date='1980-06-15', savings_owner='EXAMPLE OWNER',
                retirement_age='65', residence_state='WA', employment_state='OR', work_state_percent='75') | changes


class ProfileDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        create_schema(self.db)
        self.addCleanup(self.db.close)
        clock = patch('planning_profiles.date', wraps=date)
        clock.start().today.return_value = date(2001, 1, 20)
        self.addCleanup(clock.stop)

    def test_profiles_derive_age_and_match_exact_owner_not_shared_accounts(self):
        saved = profiles.save_person(self.db, person(), date(2001, 1, 1))
        accounts = [account(' example owner ', recorded_on='2020-06-01'), account('EXAMPLE OWNER', 'post_tax', 0, '2020-06-01'),
                    account('EXAMPLE OWNER AND OTHER', amount=90000), account('Household', amount=80000)]
        choices = profiles.choices(self.db, accounts, date(2020, 6, 14))
        option = next(item for item in choices if item['id'] == saved['id'])
        self.assertEqual(option['values']['current_age'], 39)
        self.assertEqual(option['balances']['starting_pretax']['value'], 123)
        self.assertEqual(option['balances']['starting_roth']['value'], 0)
        self.assertEqual(profiles.age('1980-06-15', date(2020, 6, 15)), 40)
        self.assertFalse(any(item['owner'] == 'Household' for item in choices))

    def test_missing_balance_is_not_zero_or_partial_total(self):
        for accounts in ([], [account(recorded_on=None)], [account(), account(recorded_on=None)], [account(amount=-1)]):
            self.assertEqual(profiles.balance(accounts)['value'], '')
        self.assertEqual(profiles.balance([account(amount=0)])['value'], 0)

    def test_balance_dates_and_amounts_must_be_valid_before_using_any_total(self):
        today = date(2001, 3, 15)
        for invalid in (account(recorded_on='not-a-date'), account(recorded_on='2001-02-30'),
                        account(recorded_on='2001-03-16'), account(amount=None), account(amount=float('nan'))):
            with self.subTest(invalid=invalid):
                self.assertEqual(profiles.balance([account(), invalid], today)['value'], '')
        self.assertEqual(profiles.balance([account(amount=0, recorded_on='2001-03-15')], today)['value'], 0)

    def test_stale_balances_leave_the_entire_inferred_pool_blank(self):
        result = profiles.balance([account(recorded_on='2001-01-01'), account(recorded_on='2001-03-01')], date(2001, 3, 15))
        self.assertEqual(result['value'], '')
        self.assertIn('Starting amount left blank', result['note'])
        self.assertIn('1 balance over 31 days old', result['note'])
        self.assertIn('2001-01-01', result['note'])
        self.assertIn('enter a manual amount', result['note'])

    def test_fresh_mixed_dates_are_explicit_and_31_day_boundary_is_inclusive(self):
        accounts = [account(recorded_on='2001-02-12'), account(recorded_on='2001-03-01')]
        result = profiles.balance(accounts, date(2001, 3, 15))
        self.assertEqual(result['value'], 246)
        self.assertIn('different dates', result['note'])
        self.assertNotIn('over 31 days old', result['note'])
        self.assertEqual(profiles.balance(accounts, date(2001, 3, 16))['value'], '')

    def test_profile_choices_use_the_same_date_for_age_and_balance_provenance(self):
        options = profiles.choices(self.db, [account(recorded_on='2001-03-16')], date(2001, 3, 15))
        self.assertEqual(options[0]['balances']['starting_pretax']['value'], '')
        self.assertIn('future-dated', options[0]['balances']['starting_pretax']['note'])

    def test_existing_manual_fields_survive_and_connected_fields_refresh(self):
        options = profiles.choices(self.db, [account(), account(classification='post_tax', amount=4500)])
        plan = sample_plan()
        original = copy.deepcopy(plan)
        refs = profiles.apply_defaults(plan, options, {'value': 999}, fresh=False)
        self.assertEqual(plan, original)
        refs['people'][0]['fields'] = ['starting_roth']
        refs['taxable_from_savings'] = True
        profiles.apply_defaults(plan, options, {'value': 999}, refs)
        self.assertEqual(plan['people'][0]['starting_roth'], 45)
        self.assertEqual(plan['people'][0]['starting_pretax'], original['people'][0]['starting_pretax'])
        self.assertEqual(plan['starting_taxable'], 999)
        options = profiles.choices(self.db, [account(), account(classification='post_tax', amount=4500)], date(2001, 3, 15))
        profiles.apply_defaults(plan, options, {'value': ''}, refs)
        self.assertEqual(plan['people'][0]['starting_roth'], '')
        self.assertEqual(plan['people'][0]['starting_pretax'], original['people'][0]['starting_pretax'])
        self.assertEqual(plan['starting_taxable'], '')

    def test_multiple_people_require_selection_and_duplicate_references_rejected(self):
        options = profiles.choices(self.db, [account(owner=f'EXAMPLE OWNER {index}') for index in range(3)])
        refs = profiles.apply_defaults(sample_plan(), options, {'value': ''}, fresh=True)
        self.assertEqual([item['choice'] for item in refs['people']], ['', ''])
        refs['people'] = [{'choice': options[0]['id'], 'fields': []}] * 2
        with self.assertRaises(ProjectionError):
            profiles.validate_references(refs, options)

    def test_profile_edits_require_current_version_and_unique_ownership(self):
        saved = profiles.save_person(self.db, person())
        for invalid in (person(name='EXAMPLE SECOND'), person(name='example person', savings_owner=''),
                        person(id=saved['id']), person(birth_date='3000-01-01', name='EXAMPLE SECOND', savings_owner=''),
                        person(name='EXAMPLE SECOND', savings_owner='', retirement_age='2')):
            with self.assertRaises(ProjectionError):
                profiles.save_person(self.db, invalid)
        updated = profiles.save_person(self.db, person(id=saved['id'], version=saved['version'], name='EXAMPLE RENAMED'))
        self.assertEqual(updated['id'], saved['id'])
        self.assertNotEqual(updated['version'], saved['version'])
        self.assertEqual(len(profiles.load(self.db, profiles.SETTING, [])), 1)


class ProfileRouteTests(unittest.TestCase):
    setUp = test_projections.ProjectionRouteTests.setUp
    tearDown = test_projections.ProjectionRouteTests.tearDown
    complete_setup = test_projections.ProjectionRouteTests.complete_setup
    complete_category_setup = test_projections.ProjectionRouteTests.complete_category_setup
    csrf_token = staticmethod(test_projections.ProjectionRouteTests.csrf_token)
    enable_mirror = test_projections.ProjectionRouteTests.enable_mirror

    def test_savings_marks_unknown_balances_without_hiding_recorded_zero(self):
        self.complete_setup()
        with self.application.db() as db:
            db.execute("INSERT INTO manual_accounts (id,institution,name,classification,goal_eligible) VALUES (1,'EXAMPLE BANK','SAMPLE GOAL ACCOUNT','taxable',1)")
        page = self.client.get('/savings')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'0 of 1 selected balances recorded', page.data)
        self.assertIn(b'Record a balance to see progress.', page.data)
        self.assertIn(b'missing balances are unknown', page.data)
        self.assertIn(b'it does not reserve money', page.data)
        with self.application.db() as db:
            db.execute("INSERT INTO savings_snapshots (manual_account_id,amount,recorded_on) VALUES (1,0,'2001-01-01')")
        page = self.client.get('/savings')
        self.assertIn(b'1 of 1 selected balances recorded', page.data)
        self.assertNotIn(b'Record a balance to see progress.', page.data)
        self.assertNotIn(b'missing balances are unknown', page.data)
        self.assertIn(b'<div class="metric-value">$0.00</div>', page.data)

    @patch('planning_profiles.date', wraps=date)
    def test_save_profile_and_plan_connections_persist_without_get_writes(self, clock):
        clock.today.return_value = date(2001, 1, 20)
        self.complete_setup()
        page = self.client.get('/people')
        self.assertEqual(page.status_code, 200)
        token = self.csrf_token(page)
        self.assertEqual(self.client.post('/api/people', data=person()).status_code, 400)
        self.assertEqual(self.client.post('/api/people', data=person() | {'csrf_token': token}).status_code, 200)
        with self.application.db() as db:
            db.execute("INSERT INTO manual_accounts (id,institution,name,owner_name,classification) VALUES (1,'EXAMPLE BANK','SAMPLE INVESTMENT','EXAMPLE OWNER','pre_tax')")
            db.executemany('INSERT INTO savings_snapshots (manual_account_id,amount,recorded_on) VALUES (1,?,?)', [(9900,'2000-01-01'),(12300,'2001-01-01')])
        page = self.client.get('/plan')
        self.assertIn(b'name="person_0_starting_pretax" value="123.0"', page.data)
        self.assertIn(b'name="person_0_residence_state"', page.data)
        with self.application.db() as db:
            saved = profiles.load(db, profiles.SETTING, [])[0]
        refs = {'people': [{'choice': saved['id'], 'fields': ['starting_pretax']}, {'choice': '', 'fields': []}], 'taxable_from_savings': False}
        response = self.client.post('/api/plan-settings', json={'plan': sample_plan(), 'references': refs}, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 200)
        before = self.application.VAULT_PATH.read_bytes()
        self.assertNotIn(b'EXAMPLE PERSON', before)
        page = self.client.get('/plan')
        self.assertIn(b'name="person_0_starting_pretax" value="123.0"', page.data)
        self.assertIn(b'name="person_0_current_age" value="40"', page.data)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
        with self.application.db() as db:
            db.execute("INSERT INTO savings_snapshots (manual_account_id,amount,recorded_on) VALUES (1,23400,'2001-01-16')")
        self.assertIn(b'name="person_0_starting_pretax" value="234.0"', self.client.get('/plan').data)
        refs['people'][1]['choice'] = saved['id']
        before = self.application.VAULT_PATH.read_bytes()
        self.assertEqual(self.client.post('/api/plan-settings', json={'plan': sample_plan(), 'references': refs}, headers={'X-CSRF-Token': token}).status_code, 400)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())

    def test_mirror_profiles_are_read_only(self):
        self.enable_mirror()
        before = self.application.VAULT_PATH.read_bytes()
        page = self.client.get('/people')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'Save person', page.data)
        self.assertEqual(self.client.post('/api/people', data=person() | {'csrf_token': self.csrf_token(page)}).status_code, 403)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
