import math
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from analytics import planning_spending_trend
from projections import END_AGE, ProjectionError, project, validate
from schema import create_schema
from tests import test_development_mirror


def sample_plan(**changes):
    return {"current_age": 40, "retirement_age": 42, "starting_assets": 1000,
            "annual_income": 1000, "tax_advantaged_rate": 10, "withdrawal_rate": 10,
            "growth_rate": 0, "inflation_rate": 0, **changes}


def seed_trend(connection):
    connection.execute("INSERT INTO connections (id, owner_name, institution, access_token) VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', '')")
    connection.execute("INSERT INTO accounts (id, connection_id, institution, name, type, spending_enabled, cash_flow_role) VALUES ('sample', 1, 'EXAMPLE BANK', 'SAMPLE ACCOUNT', 'depository', 1, 'cash_flow')")
    for month in range(1, 13):
        connection.execute("INSERT INTO transactions (id, account_id, amount, currency, description, merchant, pending, excluded, transacted_at, category) VALUES (?, 'sample', 1000, 'USD', 'SAMPLE CAFE', '', 0, 0, ?, 'SAMPLE FOOD')",
                           (f'sample-{month}', f'2040-{month:02d}-15'))


class ProjectionMathTests(unittest.TestCase):
    def test_income_allocates_once_and_retirement_stops_contributions(self):
        result = project(sample_plan(), 600)
        self.assertEqual(result['annual_tax_advantaged_savings'], 100)
        self.assertEqual(result['annual_taxable_savings'], 300)
        self.assertEqual([row['assets'] for row in result['rows'][:4]], [1000, 1400, 1800, 1200])
        self.assertEqual(result['retirement']['retirement_assets'], 1200)
        self.assertEqual(result['retirement']['taxable_assets'], 600)
        retired = result['rows'][3]
        self.assertEqual(retired['income'], 0)
        self.assertEqual(retired['tax_advantaged_savings'], 0)
        self.assertEqual(retired['withdrawal'], 120)
        self.assertEqual(retired['taxable_assets'], 120)
        self.assertEqual(result['final']['age'], END_AGE)

    def test_withdrawal_recalculates_on_remaining_retirement_not_total_assets(self):
        result = project(sample_plan(retirement_age=40), 0, retirement_share=.5)
        self.assertEqual(result['rows'][1]['withdrawal'], 50)
        self.assertEqual(result['rows'][2]['withdrawal'], 45)
        self.assertEqual(result['rows'][2]['retirement_assets'], 405)
        self.assertEqual(result['rows'][2]['taxable_assets'], 595)
        self.assertEqual(result['rows'][2]['assets'], 1000)

    def test_surplus_brokerage_and_retirement_balances_both_grow(self):
        result = project(sample_plan(growth_rate=10), 600, retirement_share=.5)
        self.assertEqual(result['rows'][1]['retirement_assets'], 650)
        self.assertEqual(result['rows'][1]['taxable_assets'], 850)
        self.assertEqual(result['rows'][1]['assets'], 1500)

    def test_inflation_increases_income_contributions_and_spending(self):
        result = project(sample_plan(inflation_rate=10), 600)
        row = result['rows'][2]
        self.assertEqual(row['income'], 1100)
        self.assertEqual(row['tax_advantaged_savings'], 110)
        self.assertEqual(row['spending'], 660)
        self.assertEqual(row['taxable_cash_flow'], 330)
        result = project(sample_plan(annual_income=0, withdrawal_rate=0, growth_rate=5, inflation_rate=5), 0)
        self.assertAlmostEqual(result['final']['assets'] / result['final']['factor'], 1000, places=2)

    def test_negative_surplus_draws_taxable_and_reports_uncovered_amount(self):
        result = project(sample_plan(annual_income=100, starting_assets=100), 200, retirement_share=.5)
        self.assertEqual(result['annual_taxable_savings'], -110)
        self.assertEqual(result['rows'][1]['taxable_assets'], 0)
        self.assertEqual(result['rows'][1]['retirement_assets'], 60)
        self.assertEqual(result['rows'][1]['shortfall'], 60)
        self.assertEqual(result['first_shortfall_age'], 40)

    def test_gap_can_exist_while_retirement_money_remains(self):
        result = project(sample_plan(retirement_age=40, withdrawal_rate=0), 100)
        self.assertEqual(result['rows'][1]['retirement_assets'], 1000)
        self.assertEqual(result['rows'][1]['shortfall'], 100)
        self.assertEqual(result['rows'][2]['shortfall'], 100)
        self.assertEqual(result['rows'][2]['taxable_assets'], 0)

    def test_withdrawals_cannot_exceed_funds_after_market_loss(self):
        result = project(sample_plan(retirement_age=40, withdrawal_rate=100, growth_rate=-30), 0)
        self.assertEqual(result['rows'][1]['withdrawal'], 700)
        self.assertEqual(result['rows'][1]['retirement_assets'], 0)
        self.assertEqual(result['rows'][1]['taxable_assets'], 700)
        self.assertIsNone(result['first_shortfall_age'])

    def test_invalid_nonfinite_and_inconsistent_inputs(self):
        for change in ({'starting_assets': math.nan}, {'growth_rate': math.inf}, {'annual_income': -1},
                       {'tax_advantaged_rate': True}, {'withdrawal_rate': 101}, {'current_age': 40.5},
                       {'current_age': '40'}, {'retirement_age': 39}, {'current_age': END_AGE},
                       {'annual_spending': 1}, {'end_age': 100}):
            with self.subTest(change=change), self.assertRaises(ProjectionError):
                validate(sample_plan(**change))
        for invalid in (None, [], {}):
            with self.assertRaises(ProjectionError):
                validate(invalid)
        for spending in (None, math.nan, math.inf, -1):
            with self.assertRaises(ProjectionError):
                project(sample_plan(), spending)


class PlanningTrendTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        seed_trend(self.connection)

    def tearDown(self):
        self.connection.close()

    def trend(self):
        return planning_spending_trend(self.connection, today=date(2041, 1, 20))

    def test_last_twelve_complete_months_use_trends_treatment(self):
        for key, amount, category, pending, excluded, when in [
            ('refund', -100, 'SAMPLE FOOD', 0, 0, '2040-05-01'),
            ('transfer', 9000, 'Transfer', 0, 0, '2040-05-01'),
            ('income', -9000, 'Income', 0, 0, '2040-05-01'),
            ('pending', 9000, 'SAMPLE FOOD', 1, 0, '2040-05-01'),
            ('excluded', 9000, 'SAMPLE FOOD', 0, 1, '2040-05-01'),
            ('current-month', 9000, 'SAMPLE FOOD', 0, 0, '2041-01-01'),
            ('old', 9000, 'SAMPLE FOOD', 0, 0, '2039-12-31')]:
            self.connection.execute("INSERT INTO transactions (id, account_id, amount, currency, description, merchant, pending, excluded, transacted_at, category) VALUES (?, 'sample', ?, 'USD', 'SAMPLE RECORD', '', ?, ?, ?, ?)",
                                    (key, amount, pending, excluded, when, category))
        result = self.trend()
        self.assertEqual(result['date_from'], '2040-01-01')
        self.assertEqual(result['date_to'], '2040-12-31')
        self.assertEqual(result['annual_spending'], 11900)
        self.assertEqual(result['observed_months'], 12)
        self.connection.execute("UPDATE transactions SET category_override = 'Transfer' WHERE id = 'sample-1'")
        self.assertEqual(self.trend()['annual_spending'], 10900)
        self.connection.execute('UPDATE accounts SET spending_enabled = 0')
        self.assertIsNone(self.trend()['annual_spending'])

    def test_missing_month_is_unknown_but_observed_zero_spending_is_zero(self):
        self.connection.execute("UPDATE transactions SET category = 'Income', amount = -1000 WHERE id = 'sample-1'")
        self.assertEqual(self.trend()['annual_spending'], 11000)
        self.connection.execute("DELETE FROM transactions WHERE id = 'sample-1'")
        result = self.trend()
        self.assertIsNone(result['annual_spending'])
        self.assertIsNone(result['months'][0]['spending'])
        self.assertEqual(result['observed_months'], 11)

    def test_mixed_currency_and_unknown_classification_are_visible(self):
        self.connection.execute("UPDATE transactions SET currency = 'EUR' WHERE id = 'sample-1'")
        self.assertIsNone(self.trend()['annual_spending'])
        self.assertEqual(self.trend()['other_currency'], 1)
        self.connection.execute("UPDATE transactions SET currency = 'USD', category = 'Uncategorized' WHERE id = 'sample-1'")
        self.assertEqual(self.trend()['unclassified'], 1)


class ProjectionRouteTests(unittest.TestCase):
    setUp = test_development_mirror.ProductionMirrorTests.setUp
    tearDown = test_development_mirror.ProductionMirrorTests.tearDown
    complete_setup = test_development_mirror.ProductionMirrorTests.complete_setup
    complete_category_setup = test_development_mirror.ProductionMirrorTests.complete_category_setup
    csrf_token = staticmethod(test_development_mirror.ProductionMirrorTests.csrf_token)
    enable_mirror = test_development_mirror.ProductionMirrorTests.enable_mirror

    def test_seed_uses_latest_active_snapshots_and_the_recorded_tax_split(self):
        self.complete_setup()
        with self.application.db() as connection:
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification) VALUES (1, 'EXAMPLE BANK', 'SAMPLE RETIREMENT', 'pre_tax')")
        self.assertIn(b'name="starting_assets" value=""', self.client.get('/plan').data)
        with self.application.db() as connection:
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification) VALUES (2, 'EXAMPLE BANK', 'SAMPLE BROKERAGE', 'taxable')")
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification, archived) VALUES (3, 'EXAMPLE BANK', 'SAMPLE ARCHIVED', 'taxable', 1)")
            connection.executemany('INSERT INTO savings_snapshots (manual_account_id, amount, recorded_on) VALUES (?, ?, ?)',
                                   [(1, 11111, '2040-01-01'), (1, 20000, '2040-02-01'), (2, 10000, '2040-02-01'), (3, 99999, '2040-02-01')])
            seed_trend(connection)
        page = self.client.get('/plan')
        self.assertIn(b'name="starting_assets" value="300.00"', page.data)
        self.assertNotIn(b'SAMPLE ARCHIVED', page.data)
        self.assertAlmostEqual(self.application.retirement_investment_share(self.application.savings_data()), 2 / 3)
        with patch.object(self.application, 'planning_spending_trend', side_effect=lambda c: planning_spending_trend(c, date(2041, 1, 20))):
            response = self.client.post('/api/projections', json={'baseline': sample_plan(), 'comparison': sample_plan()}, headers={'X-CSRF-Token': self.csrf_token(page)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['comparison']['annual_spending'], 120)
        self.assertAlmostEqual(response.json['retirement_share'], 2 / 3)

    def test_mirror_can_calculate_without_writing_records_or_settings(self):
        self.enable_mirror()
        before = self.application.VAULT_PATH.read_bytes()
        page = self.client.get('/plan')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'No manually recorded savings accounts', page.data)
        self.assertIn(b'/static/vendor/chart.umd.min.js', page.data)
        token = self.csrf_token(page)
        payload = {'baseline': sample_plan(), 'comparison': sample_plan(retirement_age=41)}
        trend = {'annual_spending': 60000, 'date_from': '2040-01-01', 'date_to': '2040-12-31'}
        with patch.object(self.application, 'planning_spending_trend', return_value=trend):
            response = self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['baseline']['annual_taxable_savings'], 300)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), before)
        self.assertEqual(self.client.post('/api/projections', json=payload).status_code, 400)
        self.application.lock_data()
        self.assertEqual(self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': token}).status_code, 401)

    def test_missing_history_and_client_spending_override_are_rejected(self):
        self.enable_mirror()
        token = self.csrf_token(self.client.get('/plan'))
        for payload in (None, {}, {'baseline': sample_plan(), 'comparison': sample_plan()},
                        {'baseline': sample_plan(), 'comparison': sample_plan(annual_spending=1)},
                        {'baseline': sample_plan(), 'comparison': sample_plan(current_age=41)}):
            response = self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/projections', data=' ' * 10001, content_type='application/json', headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 413)
