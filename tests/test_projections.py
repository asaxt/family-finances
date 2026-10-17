import math
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from analytics import planning_spending_trend
from projections import END_AGE, ProjectionError, project, validate, restore_saved_plan, split_withdrawal
from schema import create_schema
from tests import test_development_mirror


def sample_person(**changes):
    return dict(name='EXAMPLE PERSON', current_age=40, retirement_age=42,
                starting_pretax=1000, starting_roth=0, annual_income=1000,
                tax_advantaged_rate=10, contribution_type='pre_tax',
                residence_state='WA', employment_state='WA', work_state_percent=100) | changes


def sample_plan(**changes):
    person_fields = {'current_age', 'retirement_age', 'annual_income', 'tax_advantaged_rate',
                     'starting_pretax', 'starting_roth'}
    person = sample_person(**{key: value for key, value in changes.items() if key in person_fields})
    return dict(people=[person, sample_person(name='EXAMPLE SPOUSE', starting_pretax=0, annual_income=0)],
                starting_taxable=0, growth_rate=0, inflation_rate=0, tax_payments_in_spending=0,
                withdrawal_rate=10, withdrawal_start='first_retirement',
                filing_status='joint', mfs_allocation='community_wages') | {key: value for key, value in changes.items() if key not in person_fields}


def sample_event(**changes):
    return dict(name='EXAMPLE EVENT', start_year=2054, years=1, annual_amount=137) | changes


def seed_trend(connection):
    connection.execute("INSERT INTO connections (id, owner_name, institution, access_token) VALUES (1, 'EXAMPLE PERSON', 'EXAMPLE BANK', '')")
    connection.execute("INSERT INTO accounts (id, connection_id, institution, name, type, spending_enabled, cash_flow_role) VALUES ('sample', 1, 'EXAMPLE BANK', 'SAMPLE ACCOUNT', 'depository', 1, 'cash_flow')")
    for month in range(1, 13):
        connection.execute("INSERT INTO transactions (id, account_id, amount, currency, description, merchant, pending, excluded, transacted_at, category) VALUES (?, 'sample', 1000, 'USD', 'SAMPLE CAFE', '', 0, 0, ?, 'SAMPLE FOOD')",
                           (f'sample-{month}', f'2040-{month:02d}-15'))


class ProjectionMathTests(unittest.TestCase):
    def test_monthly_retirement_uses_first_retired_year_and_its_inflation(self):
        result = project(sample_plan(inflation_rate=10), 600, start_year=2026)
        monthly = result['first_retirement_monthly']
        self.assertEqual(monthly['year'], 2028)
        self.assertEqual(monthly['ages'], [42, 42])
        self.assertEqual(monthly['retired_count'], 2)
        self.assertAlmostEqual(monthly['available'], 167.94 / 12)
        self.assertAlmostEqual(monthly['spending'], 60.5)
        self.assertAlmostEqual(monthly['spending'] / monthly['factor'], 50)
        self.assertAlmostEqual(monthly['available'] + monthly['shortfall'], monthly['spending'], places=2)
        self.assertEqual(len(result['monthly']), len(result['rows']) - 1)

    def test_monthly_budget_includes_brokerage_without_extra_draws(self):
        plan = sample_plan(retirement_age=40, starting_pretax=0, starting_taxable=1000)
        plan['people'][1]['retirement_age'] = 40
        result = project(plan, 600, start_year=2026)
        first, second, third = result['monthly'][:3]
        self.assertEqual(result['first_retirement_monthly']['year'], 2026)
        self.assertAlmostEqual(first['available'], 100 / 12)
        self.assertAlmostEqual(first['withdrawal'], 100 / 12)
        self.assertAlmostEqual(first['shortfall'], 500 / 12)
        self.assertAlmostEqual(second['available'], 90 / 12)
        self.assertAlmostEqual(second['shortfall'], 510 / 12)
        self.assertAlmostEqual(third['shortfall'], 519 / 12)
        self.assertEqual(result['rows'][1]['taxable_assets'], 900)

    def test_monthly_available_deducts_taxes_and_uses_adjusted_spending(self):
        result = project(sample_plan(annual_income=100000, tax_payments_in_spending=120), 600, start_year=2026)
        monthly = result['monthly'][0]
        yearly = result['rows'][1]
        self.assertEqual(monthly['spending'], 40)
        self.assertAlmostEqual(monthly['available'], (yearly['income'] - yearly['tax_advantaged_savings'] - yearly['taxes']) / 12)
        self.assertAlmostEqual(monthly['difference'], yearly['taxable_cash_flow'] / 12)
        self.assertEqual(monthly['withdrawal'], 0)

    def test_income_allocates_after_taxes_and_retirement_stops_contributions(self):
        result = project(sample_plan(), 600, start_year=2026)
        self.assertEqual(result['annual_tax_advantaged_savings'], 100)
        self.assertEqual(result['annual_taxable_savings'], 223.5)
        self.assertEqual([row['assets'] for row in result['rows'][:4]], [1000, 1323.5, 1647, 1482.3])
        self.assertEqual(result['retirement']['retirement_assets'], 1200)
        retired = result['rows'][3]
        self.assertEqual(retired['income'], 0)
        self.assertEqual(retired['tax_advantaged_savings'], 0)
        self.assertEqual(retired['withdrawal'], 164.7)
        self.assertEqual(result['final']['ages'], [END_AGE, END_AGE])

    def test_each_person_retires_independently(self):
        plan = sample_plan()
        plan['people'][1] = sample_person(current_age=38, retirement_age=41, annual_income=2000, starting_pretax=0)
        result = project(plan, 0, start_year=2026)
        self.assertEqual([row['income'] for row in result['rows'][1:5]], [3000, 3000, 2000, 0])
        self.assertEqual(result['retirement']['elapsed'], 3)
        self.assertEqual(result['final']['ages'], [97, 95])

    def test_withdrawals_recalculate_on_combined_remaining_balance(self):
        plan = sample_plan(retirement_age=40, starting_pretax=500, starting_taxable=500)
        result = project(plan, 100, start_year=2026)
        self.assertEqual(result['rows'][1]['withdrawal'], 100)
        self.assertEqual(result['rows'][2]['withdrawal'], 90)
        self.assertEqual(result['rows'][2]['retirement_withdrawal'], 45)
        self.assertEqual(result['rows'][2]['taxable_withdrawal'], 45)
        self.assertEqual(result['rows'][2]['retirement_assets'], 405)
        self.assertEqual(result['rows'][2]['taxable_assets'], 405)

    def test_equal_split_and_empty_pool_fallback_are_capped(self):
        self.assertEqual(split_withdrawal(100, 900, 100), (50, 50))
        self.assertEqual(split_withdrawal(100, 10, 900), (10, 90))
        self.assertEqual(split_withdrawal(100, 900, 10), (90, 10))
        self.assertEqual(split_withdrawal(1000, 30, 40), (30, 40))
        self.assertEqual(split_withdrawal(100, 0, 0), (0, 0))

    def test_household_timing_and_retirement_ownership(self):
        plan = sample_plan(starting_pretax=300, starting_taxable=400, retirement_age=40)
        plan['people'][1].update(starting_pretax=300, annual_income=1000)
        first = project(plan, 1000, start_year=2026)['rows'][1]
        self.assertEqual(first['retirement_withdrawal'], 50)
        self.assertEqual(first['taxable_withdrawal'], 50)
        self.assertEqual(first['income'], 1000)
        plan['withdrawal_start'] = 'both_retired'
        result = project(plan, 1000, start_year=2026)
        self.assertEqual(result['rows'][1]['withdrawal'], 0)
        self.assertEqual(result['rows'][2]['withdrawal'], 0)
        self.assertGreater(result['rows'][3]['withdrawal'], 0)

    def test_only_pretax_portion_of_equal_household_draw_is_taxable(self):
        from planning_taxes import household_taxes
        plan = sample_plan(retirement_age=40, starting_pretax=300, starting_taxable=400)
        plan['people'][1].update(starting_roth=300, starting_pretax=0)
        with patch('projections.household_taxes', wraps=household_taxes) as taxes:
            result = project(plan, 100, start_year=2026)
        first_people = taxes.call_args_list[0].args[0]
        self.assertEqual([p['taxable_withdrawals'] for p in first_people], [25, 0])
        first = result['rows'][1]
        self.assertEqual(first['withdrawal'], 100)
        self.assertEqual(first['retirement_assets'], 550)
        self.assertEqual(first['taxable_assets'], 350)
        self.assertEqual(first['shortfall'], 0)

    def test_no_automatic_top_up_even_if_assets_remain(self):
        result = project(sample_plan(retirement_age=40, withdrawal_rate=0, starting_taxable=1000), 600, start_year=2026)
        self.assertEqual(result['rows'][1]['taxable_assets'], 1000)
        self.assertEqual(result['rows'][1]['retirement_assets'], 1000)
        self.assertEqual(result['rows'][1]['withdrawal'], 0)
        self.assertEqual(result['rows'][1]['shortfall'], 600)

    def test_legacy_saved_profiles_are_preserved_without_mutation(self):
        old = sample_plan()
        old.pop('withdrawal_start')
        rate = old.pop('withdrawal_rate')
        for person in old['people']:
            person['withdrawal_rate'] = rate
        restored, changed = restore_saved_plan(old)
        self.assertTrue(changed)
        self.assertEqual(restored['withdrawal_rate'], rate)
        self.assertNotIn('withdrawal_rate', restored['people'][0])
        self.assertEqual(old['people'][0]['withdrawal_rate'], rate)
        old['people'][1]['withdrawal_rate'] = 7
        restored, changed = restore_saved_plan(old)
        self.assertEqual(restored['withdrawal_rate'], '')
        self.assertEqual(restored['people'][0]['annual_income'], old['people'][0]['annual_income'])
        with self.assertRaises(ProjectionError):
            validate(restored)

    def test_both_balances_grow_and_cash_conserves(self):
        result = project(sample_plan(growth_rate=10, starting_pretax=500, starting_taxable=500), 600, start_year=2026)
        row = result['rows'][1]
        self.assertEqual(row['retirement_assets'], 650)
        self.assertEqual(row['taxable_assets'], 773.5)
        self.assertEqual(row['assets'], 1000 * 1.1 + 1000 - 600 - row['taxes'])

    def test_contribution_limit_redirects_excess_to_brokerage(self):
        result = project(sample_plan(annual_income=100000, tax_advantaged_rate=80), 0, start_year=2026)
        row = result['rows'][1]
        self.assertEqual(row['requested_savings'], 80000)
        self.assertEqual(row['tax_advantaged_savings'], 24500)
        self.assertEqual(row['taxable_cash_flow'], round(100000 - 24500 - row['taxes'], 2))

    def test_spending_adjustment_avoids_double_counting(self):
        result = project(sample_plan(tax_payments_in_spending=100), 600, start_year=2026)
        self.assertEqual(result['adjusted_annual_spending'], 500)
        self.assertEqual(result['annual_taxable_savings'], 323.5)
        refund = project(sample_plan(tax_payments_in_spending=-100), 600, start_year=2026)
        self.assertEqual(refund['adjusted_annual_spending'], 700)
        with self.assertRaises(ProjectionError):
            project(sample_plan(tax_payments_in_spending=601), 600)

    def test_roth_and_pretax_withdrawals_have_different_tax_treatment(self):
        pretax = project(sample_plan(retirement_age=40, starting_pretax=1000000), 0, start_year=2026)
        roth = project(sample_plan(retirement_age=40, starting_pretax=0, starting_roth=1000000), 0, start_year=2026)
        self.assertGreater(pretax['annual_taxes'], 0)
        self.assertEqual(roth['annual_taxes'], 0)
        self.assertEqual(roth['rows'][1]['withdrawal'], pretax['rows'][1]['withdrawal'])

    def test_inflation_and_funding_gaps(self):
        result = project(sample_plan(inflation_rate=10), 600, start_year=2026)
        row = result['rows'][2]
        self.assertEqual(row['income'], 1100)
        self.assertEqual(row['tax_advantaged_savings'], 110)
        self.assertEqual(row['spending'], 660)
        result = project(sample_plan(retirement_age=40, withdrawal_rate=0), 100, start_year=2026)
        self.assertEqual(result['first_shortfall_year'], 2026)
        self.assertEqual(result['rows'][1]['retirement_assets'], 1000)
        self.assertEqual(result['rows'][1]['shortfall'], 100)

    def test_lifestyle_override_preserves_historical_spending_and_avoids_second_tax_adjustment(self):
        plan = sample_plan(tax_payments_in_spending=73, annual_lifestyle_spending=311)
        result = project(plan, 600, start_year=2054)
        self.assertEqual(result['annual_spending'], 600)
        self.assertEqual(result['historical_lifestyle_spending'], 527)
        self.assertEqual(result['adjusted_annual_spending'], 311)
        self.assertEqual(result['rows'][1]['spending'], 311)
        self.assertEqual(result['monthly'][0]['spending'], 311 / 12)
        self.assertNotIn('life_events', plan)
        self.assertEqual(project(sample_plan(annual_lifestyle_spending=0), 600, start_year=2054)['rows'][1]['spending'], 0)
        with self.assertRaises(ProjectionError):
            project(sample_plan(annual_lifestyle_spending=311, tax_payments_in_spending=601), 600, start_year=2054)

    def test_missing_history_needs_explicit_lifestyle_and_stays_unknown(self):
        result = project(sample_plan(annual_lifestyle_spending=311), None, start_year=2054)
        self.assertIsNone(result['annual_spending'])
        self.assertIsNone(result['historical_lifestyle_spending'])
        self.assertEqual(result['adjusted_annual_spending'], 311)
        self.assertEqual(result['rows'][1]['spending'], 311)
        self.assertEqual(project(sample_plan(annual_lifestyle_spending=0), None, start_year=2054)['rows'][1]['spending'], 0)
        for adjustment in (-1, 1):
            with self.subTest(adjustment=adjustment), self.assertRaises(ProjectionError):
                project(sample_plan(annual_lifestyle_spending=311, tax_payments_in_spending=adjustment), None, start_year=2054)
        with self.assertRaises(ProjectionError):
            project(sample_plan(), None, start_year=2054)
        observed_zero = project(sample_plan(), 0, start_year=2054)
        self.assertEqual(observed_zero['annual_spending'], 0)
        self.assertEqual(observed_zero['historical_lifestyle_spending'], 0)

    def test_optional_scenario_fields_keep_existing_plan_behavior(self):
        old = sample_plan()
        normalized = validate(old)
        self.assertIsNone(normalized['annual_lifestyle_spending'])
        self.assertEqual(normalized['life_events'], [])
        self.assertNotIn('annual_lifestyle_spending', old)
        self.assertEqual(project(old, 600, start_year=2054), project(normalized, 600, start_year=2054))

    def test_life_event_uses_year_end_brokerage_including_growth(self):
        plan = sample_plan(annual_income=0, starting_taxable=310, growth_rate=11,
                           life_events=[sample_event()])
        before = project(plan | {'life_events': []}, 0, start_year=2054)
        result = project(plan, 0, start_year=2054)
        row = result['rows'][1]
        self.assertEqual(row['event_spending'], 137)
        self.assertEqual(row['event_funded'], 137)
        self.assertEqual(row['event_shortfall'], 0)
        self.assertEqual(row['taxable_assets'], 207.1)
        self.assertEqual(row['retirement_assets'], before['rows'][1]['retirement_assets'])
        self.assertAlmostEqual(row['assets'], before['rows'][1]['assets'] - 137)
        self.assertEqual(result['rows'][2]['event_spending'], 0)
        self.assertEqual(result['event_total_real_spending'], 137)
        self.assertEqual(result['annual_net_taxable_savings'], -137)

    def test_life_event_uses_surplus_once_without_changing_recurring_available_income(self):
        plan = sample_plan(life_events=[sample_event()])
        before = project(plan | {'life_events': []}, 600, start_year=2054)
        result = project(plan, 600, start_year=2054)
        row = result['rows'][1]
        self.assertEqual(row['taxable_cash_flow'], 223.5)
        self.assertEqual(row['taxable_assets'], 86.5)
        self.assertEqual(row['event_funded'], 137)
        self.assertEqual(row['withdrawal'], 0)
        self.assertEqual(result['annual_taxable_savings'], 223.5)
        self.assertEqual(result['annual_net_taxable_savings'], 86.5)
        self.assertEqual(result['monthly'][0]['available'], before['monthly'][0]['available'])
        self.assertEqual(result['monthly'][0]['difference'], before['monthly'][0]['difference'])
        self.assertEqual(result['monthly'][0]['event_funded'], 137 / 12)
        self.assertAlmostEqual(row['assets'], before['rows'][1]['assets'] - 137)

    def test_life_event_gaps_do_not_take_retirement_funds_or_hide_lifestyle_gaps(self):
        plan = sample_plan(retirement_age=40, starting_pretax=211, starting_taxable=13,
                           withdrawal_rate=0, life_events=[sample_event(annual_amount=37)])
        result = project(plan, 19, start_year=2054)
        row = result['rows'][1]
        self.assertEqual(row['retirement_assets'], 211)
        self.assertEqual(row['taxable_assets'], 0)
        self.assertEqual(row['withdrawal'], 0)
        self.assertEqual(row['shortfall'], 19)
        self.assertEqual(row['event_funded'], 13)
        self.assertEqual(row['event_shortfall'], 24)
        self.assertEqual(row['total_shortfall'], 43)
        self.assertEqual(result['first_shortfall_year'], 2054)
        self.assertEqual(result['first_event_shortfall_year'], 2054)
        self.assertEqual(result['first_funding_gap_year'], 2054)
        self.assertEqual(result['total_real_event_shortfall'], 24)
        self.assertEqual(result['total_real_funding_gap'], result['total_real_shortfall'] + 24)
        self.assertEqual(result['monthly'][0]['total_shortfall'], 43 / 12)

    def test_event_cost_does_not_change_the_elected_withdrawal_or_count_twice(self):
        plan = sample_plan(retirement_age=40, starting_pretax=1000, starting_taxable=1000,
                           life_events=[sample_event()])
        result = project(plan, 200, start_year=2054)
        row = result['rows'][1]
        self.assertEqual(row['withdrawal'], 200)
        self.assertEqual(row['retirement_withdrawal'], 100)
        self.assertEqual(row['taxable_withdrawal'], 100)
        self.assertEqual(row['event_funded'], 137)
        self.assertEqual(row['taxable_assets'], 763)
        self.assertEqual(row['retirement_assets'], 900)
        self.assertEqual(row['assets'], 2000 - row['spending'] - row['taxes'] - row['event_funded'])
        self.assertEqual(result['annual_net_taxable_savings'], row['taxable_assets'] - 1000)

    def test_multi_year_events_use_inclusive_start_and_exclusive_end_at_year_prices(self):
        plan = sample_plan(annual_income=0, starting_pretax=0, starting_taxable=1000,
                           withdrawal_rate=0, inflation_rate=10,
                           life_events=[sample_event(start_year=2055, years=2)])
        result = project(plan, 0, start_year=2054)
        self.assertEqual([row['event_spending'] for row in result['rows'][1:5]], [0, 150.7, 165.77, 0])
        self.assertEqual(result['rows'][3]['taxable_assets'], 683.53)
        self.assertEqual(result['event_total_real_spending'], 274)
        self.assertEqual(result['total_real_event_shortfall'], 0)
        self.assertIsNone(result['first_funding_gap_year'])
        # A life event's yearly amount is a one-time cost in its year, even though
        # the monthly view shows an annual average alongside the lifestyle budget.
        self.assertAlmostEqual(result['monthly'][1]['event_spending'] / result['monthly'][1]['factor'], 137 / 12)

    def test_overlapping_events_add_costs_and_keep_unmet_costs_separate(self):
        plan = sample_plan(annual_income=0, starting_taxable=151, withdrawal_rate=0,
                           life_events=[sample_event(), sample_event(name='EXAMPLE SECOND EVENT', annual_amount=31)])
        result = project(plan, 0, start_year=2054)
        self.assertEqual(result['rows'][1]['event_spending'], 168)
        self.assertEqual(result['rows'][1]['event_funded'], 151)
        self.assertEqual(result['rows'][1]['event_shortfall'], 17)
        self.assertEqual(result['total_real_shortfall'], 0)
        self.assertEqual(result['total_real_funding_gap'], 17)
        self.assertEqual(result['first_funding_gap_year'], 2054)
        self.assertIsNone(result['first_shortfall_year'])

    def test_flow_years_and_real_gaps_use_the_year_spent_not_next_balance_boundary(self):
        plan = sample_plan(annual_income=0, retirement_age=40, withdrawal_rate=0, inflation_rate=10,
                           life_events=[sample_event(start_year=2055, years=2)])
        result = project(plan, 137, start_year=2054)
        self.assertIsNone(result['rows'][0]['cash_flow_year'])
        for offset, row in enumerate(result['rows'][1:]):
            self.assertEqual(row['cash_flow_year'], 2054 + offset)
            self.assertEqual(row['year'], 2055 + offset)
            self.assertAlmostEqual(row['factor'], row['cash_flow_factor'] * 1.1)
            self.assertAlmostEqual(row['spending'] / row['cash_flow_factor'], 137, places=2)
        self.assertEqual(result['total_real_shortfall'], 137 * (END_AGE - 40))
        self.assertEqual(result['total_real_event_shortfall'], 274)

    def test_life_event_and_override_validation_rejects_unsupported_or_ambiguous_values(self):
        for override in (-1, True, math.nan, math.inf, '137', 100_000_001):
            with self.subTest(override=override), self.assertRaises(ProjectionError):
                validate(sample_plan(annual_lifestyle_spending=override))
        for event in (None, {}, sample_event(name=''), sample_event(name='A' * 81),
                      sample_event(name='EXAMPLE\nEVENT'), sample_event(start_year=2054.5),
                      sample_event(start_year=2025), sample_event(start_year=2201),
                      sample_event(start_year=True), sample_event(years=0), sample_event(years=51),
                      sample_event(years=2.5), sample_event(annual_amount=-1),
                      sample_event(annual_amount=math.inf), sample_event(annual_amount=True),
                      sample_event(extra='ignored')):
            with self.subTest(event=event), self.assertRaises(ProjectionError):
                validate(sample_plan(life_events=[event]))
        for events in (None, {}, [sample_event()] * 21):
            with self.subTest(events=events), self.assertRaises(ProjectionError):
                validate(sample_plan(life_events=events))
        for event in (sample_event(start_year=2053), sample_event(start_year=2109),
                      sample_event(start_year=2108, years=2)):
            with self.subTest(event=event), self.assertRaisesRegex(ProjectionError, 'projection years'):
                project(sample_plan(life_events=[event]), 600, start_year=2054)
        # The last modeled year is included, but no spending silently disappears
        # beyond the final asset snapshot.
        result = project(sample_plan(life_events=[sample_event(start_year=2108)]), 600, start_year=2054)
        self.assertEqual(result['rows'][-1]['event_spending'], 137)
        for year in (0, True, 2025, 2201, 2054.5, '2054'):
            with self.subTest(year=year), self.assertRaises(ProjectionError):
                project(sample_plan(), 600, start_year=year)

    def test_withdrawals_cannot_exceed_funds_after_loss(self):
        result = project(sample_plan(retirement_age=40, withdrawal_rate=100, growth_rate=-30), 0, start_year=2026)
        self.assertEqual(result['rows'][1]['withdrawal'], 700)
        self.assertEqual(result['rows'][1]['retirement_assets'], 0)
        self.assertEqual(result['rows'][1]['taxable_assets'], 700)

    def test_invalid_nonfinite_and_inconsistent_inputs(self):
        for change in ({'starting_pretax': math.nan}, {'growth_rate': math.inf}, {'annual_income': -1},
                       {'tax_advantaged_rate': True}, {'withdrawal_rate': 101}, {'current_age': 40.5},
                       {'current_age': '40'}, {'retirement_age': 39}, {'current_age': END_AGE},
                       {'annual_spending': 1}, {'end_age': 100}, {'people': []}, {'filing_status': 'single'}):
            with self.subTest(change=change), self.assertRaises(ProjectionError):
                validate(sample_plan(**change))
        plan = sample_plan()
        plan['people'][0]['residence_state'] = 'XX'
        with self.assertRaises(ProjectionError):
            validate(plan)
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
        self.assertIn(b'name="starting_taxable" value=""', self.client.get('/plan').data)
        with self.application.db() as connection:
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification) VALUES (2, 'EXAMPLE BANK', 'SAMPLE BROKERAGE', 'taxable')")
            connection.execute("INSERT INTO manual_accounts (id, institution, name, classification, archived) VALUES (3, 'EXAMPLE BANK', 'SAMPLE ARCHIVED', 'taxable', 1)")
            connection.executemany('INSERT INTO savings_snapshots (manual_account_id, amount, recorded_on) VALUES (?, ?, ?)',
                                   [(1, 11111, '2040-01-01'), (1, 20000, '2040-02-01'), (2, 10000, '2040-02-01'), (3, 99999, '2040-02-01')])
            seed_trend(connection)
        with patch('planning_profiles.date', wraps=date) as clock:
            clock.today.return_value = date(2040, 2, 15)
            page = self.client.get('/plan')
        self.assertIn(b'name="starting_taxable" value="100.0"', page.data)
        self.assertNotIn(b'SAMPLE ARCHIVED', page.data)
        self.assertEqual(page.data.count(b'name="withdrawal_rate"'), 1)
        self.assertNotIn(b'person_0_withdrawal_rate', page.data)
        self.assertIn(b'Shared planning inputs', page.data)
        with patch.object(self.application, 'planning_spending_trend', side_effect=lambda c: planning_spending_trend(c, date(2041, 1, 20))):
            response = self.client.post('/api/projections', json={'baseline': sample_plan(), 'comparison': sample_plan()}, headers={'X-CSRF-Token': self.csrf_token(page)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['comparison']['annual_spending'], 120)

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
        self.assertEqual(response.json['baseline']['annual_taxable_savings'], 223.5)
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

    def test_loading_legacy_plan_preserves_profiles_without_rewriting_vault(self):
        import json
        self.complete_setup()
        legacy = sample_plan()
        legacy.pop('withdrawal_start')
        legacy.pop('withdrawal_rate')
        for index, person in enumerate(legacy['people']):
            person['withdrawal_rate'] = 3 + index
        self.application.save_setting('household_plan', json.dumps(legacy))
        before = self.application.VAULT_PATH.read_bytes()
        page = self.client.get('/plan')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'EXAMPLE PERSON', page.data)
        self.assertIn(b'name="withdrawal_rate" value=""', page.data)
        self.assertIn(b'Your saved profiles have been retained', page.data)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())

    def test_saved_household_uses_vault_and_mirror_rejects_writes(self):
        self.complete_setup()
        page = self.client.get('/plan')
        payload = sample_plan()
        token = self.csrf_token(page)
        self.assertEqual(self.client.post('/api/plan-settings', json=payload).status_code, 400)
        response = self.client.post('/api/plan-settings', json=payload, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'EXAMPLE PERSON', self.client.get('/plan').data)
        self.assertNotIn(b'EXAMPLE PERSON', self.application.VAULT_PATH.read_bytes())
        with self.client.session_transaction() as session:
            self.assertNotIn('household_plan', session)
        self.application.MIRROR_METADATA.write_text('{"copied_at":"2040-01-01T00:00:00Z"}')
        mirror = patch.object(self.application, 'READ_ONLY_MIRROR', True)
        mirror.start()
        self.addCleanup(mirror.stop)
        self.application.lock_data()
        self.application.unlock_data(self.password)
        page = self.client.get('/plan')
        before = self.application.VAULT_PATH.read_bytes()
        response = self.client.post('/api/plan-settings', json=payload, headers={'X-CSRF-Token': self.csrf_token(page)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.application.VAULT_PATH.read_bytes(), before)
        self.assertNotIn(b'id="plan-save"', page.data)
