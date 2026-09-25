import math
import unittest

from projections import ProjectionError, project, validate
from tests import test_development_mirror


def sample_plan(**changes):
    return {"current_age": 40, "end_age": 44, "retirement_age": 42,
            "starting_assets": 1000, "annual_savings": 100, "annual_spending": 300,
            "annual_income": 0, "income_age": 42, "growth_rate": 0,
            "inflation_rate": 0, "expense_amount": 0, "expense_age": 41, **changes}


class ProjectionMathTests(unittest.TestCase):
    def test_retirement_boundary_and_end_of_year_contributions(self):
        result = project(sample_plan())
        self.assertEqual([row["assets"] for row in result["rows"]], [1000, 1100, 1200, 900, 600])
        self.assertEqual(result["retirement"]["assets"], 1200)
        self.assertEqual(result["rows"][3]["savings"], 0)
        self.assertEqual(result["rows"][3]["spending"], 300)
        result = project(sample_plan(growth_rate=10))
        self.assertEqual(result["rows"][1]["assets"], 1200)

    def test_inflation_conversion_preserves_purchasing_power_at_matching_growth(self):
        result = project(sample_plan(annual_savings=0, annual_spending=0, growth_rate=5, inflation_rate=5))
        self.assertAlmostEqual(result["final"]["assets"] / result["final"]["factor"], 1000, places=2)
        result = project(sample_plan(inflation_rate=10))
        self.assertEqual(result["rows"][2]["savings"], 110)
        self.assertEqual(result["rows"][3]["spending"], 363)

    def test_shortfall_is_not_hidden_as_negative_assets_or_carried_as_debt(self):
        result = project(sample_plan(retirement_age=40, starting_assets=100, annual_income=500, income_age=42))
        self.assertEqual(result["first_shortfall_age"], 40)
        self.assertEqual(result["total_real_shortfall"], 500)
        self.assertEqual([row["assets"] for row in result["rows"]], [100, 0, 0, 200, 400])

    def test_expense_is_inflated_once_and_income_waits_for_retirement(self):
        result = project(sample_plan(annual_income=200, income_age=40, expense_amount=100, inflation_rate=10))
        self.assertEqual(result["rows"][1]["income"], 0)
        self.assertEqual(result["rows"][2]["expense"], 110)
        self.assertEqual(result["rows"][3]["expense"], 0)
        self.assertEqual(result["rows"][3]["income"], 242)

    def test_negative_returns_and_already_retired(self):
        result = project(sample_plan(retirement_age=40, annual_spending=0, growth_rate=-10))
        self.assertEqual(result["retirement"]["assets"], 1000)
        self.assertEqual(result["final"]["assets"], 656.1)
        self.assertIsNone(result["first_shortfall_age"])

    def test_zero_balance_without_unfunded_spending_is_not_a_shortfall(self):
        result = project(sample_plan(starting_assets=0, annual_savings=0, annual_spending=0))
        self.assertIsNone(result["first_shortfall_age"])

    def test_invalid_nonfinite_and_inconsistent_inputs(self):
        for change in ({"starting_assets": math.nan}, {"growth_rate": math.inf},
                       {"annual_savings": -1}, {"annual_spending": True},
                       {"current_age": 40.5}, {"current_age": "40"},
                       {"retirement_age": 39}, {"retirement_age": 44},
                       {"expense_amount": 1, "expense_age": 44}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ProjectionError):
                validate(sample_plan(**change))
        for invalid in (None, [], {}):
            with self.assertRaises(ProjectionError):
                validate(invalid)


class ProjectionRouteTests(unittest.TestCase):
    setUp = test_development_mirror.ProductionMirrorTests.setUp
    tearDown = test_development_mirror.ProductionMirrorTests.tearDown
    complete_setup = test_development_mirror.ProductionMirrorTests.complete_setup
    complete_category_setup = test_development_mirror.ProductionMirrorTests.complete_category_setup
    csrf_token = staticmethod(test_development_mirror.ProductionMirrorTests.csrf_token)
    enable_mirror = test_development_mirror.ProductionMirrorTests.enable_mirror

    def test_seed_uses_latest_active_snapshots_and_keeps_unknown_balances_blank(self):
        self.complete_setup()
        with self.application.db() as connection:
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification) VALUES (1, 'EXAMPLE BANK', 'SAMPLE INVESTMENTS', 'pre_tax')")
        self.assertIn(b'name="starting_assets" value=""', self.client.get('/plan').data)
        with self.application.db() as connection:
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification, archived) VALUES (2, 'EXAMPLE BANK', 'SAMPLE ARCHIVED', 'taxable', 1)")
            connection.executemany("INSERT INTO savings_snapshots (manual_account_id, amount, recorded_on) VALUES (?, ?, ?)",
                                   [(1, 11111, '2040-01-01'), (1, 22222, '2040-02-01'), (2, 99999, '2040-02-01')])
        page = self.client.get('/plan')
        self.assertIn(b'name="starting_assets" value="222.22"', page.data)
        self.assertNotIn(b'SAMPLE ARCHIVED', page.data)

    def test_mirror_can_calculate_without_writing_records_or_settings(self):
        self.enable_mirror()
        before = self.application.VAULT_PATH.read_bytes()
        page = self.client.get('/plan')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'No manually recorded savings accounts', page.data)
        self.assertIn(b'name="starting_assets" value=""', page.data)
        self.assertIn(b'/static/vendor/chart.umd.min.js', page.data)
        token = self.csrf_token(page)
        payload = {"baseline": sample_plan(), "comparison": sample_plan(retirement_age=41)}
        response = self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['baseline']['final']['assets'], 600)
        self.assertEqual(response.json['comparison']['final']['assets'], 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), before)
        self.assertEqual(self.client.post('/api/projections', json=payload).status_code, 400)
        self.application.lock_data()
        self.assertEqual(self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': token}).status_code, 401)

    def test_rejects_bad_comparisons_and_oversized_requests(self):
        self.enable_mirror()
        token = self.csrf_token(self.client.get('/plan'))
        for payload in (None, {}, {"baseline": sample_plan(), "comparison": sample_plan(starting_assets=2000)},
                        {"baseline": sample_plan(), "comparison": sample_plan(annual_savings=-1)}):
            response = self.client.post('/api/projections', json=payload, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/projections', data=' ' * 10001, content_type='application/json', headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 413)
