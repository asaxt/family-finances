"""Local annual household projections. Calculation never writes financial data."""
import math
from copy import deepcopy
from datetime import date

from planning_taxes import STATES, TAX_YEAR, household_taxes

END_AGE = 95


class ProjectionError(ValueError):
    pass


PERSON_FIELDS = {
    'annual_income': (0, 100_000_000, 'Gross annual income'),
    'tax_advantaged_rate': (0, 100, 'Tax-advantaged saving percentage'),
    'retirement_age': (18, END_AGE - 1, 'Retirement age'),
    'current_age': (18, END_AGE - 1, 'Current age'),
    'starting_pretax': (0, 1_000_000_000, 'Starting pre-tax investments'),
    'starting_roth': (0, 1_000_000_000, 'Starting Roth investments'),
    'work_state_percent': (0, 100, 'Work-state percentage'),
}
FIELDS = {
    'withdrawal_rate': (0, 100, 'Household withdrawal percentage'),
    'starting_taxable': (0, 1_000_000_000, 'Starting taxable investments'),
    'inflation_rate': (0, 15, 'Annual inflation'),
    'growth_rate': (-30, 30, 'Annual investment growth'),
    'tax_payments_in_spending': (-100_000_000, 100_000_000, 'Tax payments already in spending'),
}
SCENARIO_FIELDS = {'annual_lifestyle_spending', 'life_events'}


def validate_numbers(values, fields):
    for key, (low, high, label) in fields.items():
        value = values[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ProjectionError(f'{label} must be a number between {low:g} and {high:g}.')
        if key.endswith('age') and value != int(value):
            raise ProjectionError(f'{label} must be a whole number.')


def validate(values):
    required = set(FIELDS) | {'people', 'filing_status', 'mfs_allocation', 'withdrawal_start'}
    if not isinstance(values, dict) or not required <= set(values) or set(values) - required - SCENARIO_FIELDS:
        raise ProjectionError('Complete the household planning inputs before calculating.')
    validate_numbers(values, FIELDS)
    if values['withdrawal_start'] not in ('first_retirement', 'both_retired'):
        raise ProjectionError('Choose when household withdrawals begin.')
    if values['filing_status'] not in ('joint', 'separate'):
        raise ProjectionError('Choose married filing jointly or separately.')
    if values['mfs_allocation'] not in ('individual', 'community_wages', 'community_all') and not (values['filing_status'] == 'joint' and values['mfs_allocation'] == ''):
        raise ProjectionError('Choose the income allocation for separate returns.')
    people = values['people']
    if not isinstance(people, list) or len(people) != 2:
        raise ProjectionError('This married-household preview needs two spouse profiles.')
    for person in people:
        if not isinstance(person, dict) or set(person) != set(PERSON_FIELDS) | {'name', 'contribution_type', 'residence_state', 'employment_state'}:
            raise ProjectionError('Complete each spouse profile.')
        validate_numbers(person, PERSON_FIELDS)
        if not isinstance(person['name'], str) or not person['name'].strip() or len(person['name']) > 60 or any(ord(c) < 32 for c in person['name']):
            raise ProjectionError('Each person needs a name or label of 1–60 characters.')
        if person['residence_state'] not in tuple(STATES) or person['employment_state'] not in tuple(STATES):
            raise ProjectionError('This preview supports Washington and Oregon.')
        if person['contribution_type'] not in ('pre_tax', 'roth'):
            raise ProjectionError('Choose pre-tax or Roth saving for each person.')
        if person['retirement_age'] < person['current_age']:
            raise ProjectionError('Use current age as retirement age if already retired.')
    if max(p['current_age'] for p in people) - min(p['current_age'] for p in people) > 25:
        raise ProjectionError('This preview supports an age difference up to 25 years.')
    if values['filing_status'] == 'separate' and values['mfs_allocation'] != 'individual' and people[0]['residence_state'] != people[1]['residence_state']:
        raise ProjectionError('Community allocation requires the same residence state for both spouses in this preview.')
    plan = deepcopy(values)
    plan.setdefault('annual_lifestyle_spending', None)
    plan.setdefault('life_events', [])
    if plan['annual_lifestyle_spending'] is not None:
        validate_numbers(plan, {'annual_lifestyle_spending': (0, 100_000_000, 'Annual lifestyle spending')})
    if not isinstance(plan['life_events'], list) or len(plan['life_events']) > 20:
        raise ProjectionError('Use up to 20 life events in a plan.')
    for event in plan['life_events']:
        if not isinstance(event, dict) or set(event) != {'name', 'start_year', 'years', 'annual_amount'}:
            raise ProjectionError('Each life event needs a name, start year, duration, and annual amount.')
        if (not isinstance(event['name'], str) or not event['name'].strip() or len(event['name']) > 80
                or any(ord(c) < 32 for c in event['name'])):
            raise ProjectionError('Each life event needs a name of 1–80 characters.')
        validate_numbers(event, {'start_year': (TAX_YEAR, 2200, 'Life event start year'),
                                'years': (1, 50, 'Life event duration'),
                                'annual_amount': (0, 100_000_000, 'Life event annual amount')})
        if event['start_year'] != int(event['start_year']) or event['years'] != int(event['years']):
            raise ProjectionError('Life event start year and duration must be whole numbers.')
    return plan


def restore_saved_plan(values):
    """Translate the earlier preview in memory; never rewrite the saved vault."""
    legacy = isinstance(values, dict) and 'withdrawal_rate' not in values
    if not legacy:
        return validate(values), False
    restored = deepcopy(values)
    people = restored.get('people')
    if not isinstance(people, list) or len(people) != 2 or any(not isinstance(p, dict) or 'withdrawal_rate' not in p for p in people):
        raise ProjectionError('Complete the household planning inputs.')
    rates = [person.pop('withdrawal_rate') for person in people]
    for rate in rates:
        validate_numbers({'withdrawal_rate': rate}, {'withdrawal_rate': FIELDS['withdrawal_rate']})
    restored.update(withdrawal_rate=rates[0] if rates[0] == rates[1] else 0,
                    withdrawal_start='first_retirement')
    restored = validate(restored)
    if rates[0] != rates[1]:
        restored['withdrawal_rate'] = ''  # User must select one shared rate.
    return restored, True


def split_withdrawal(target, retirement_available, taxable_available):
    """Equal dollar withdrawals, then use the other pool if one cannot supply half."""
    target = min(target, retirement_available + taxable_available)
    retirement = min(target / 2, retirement_available)
    taxable = min(target / 2, taxable_available)
    remainder = target - retirement - taxable
    if retirement_available - retirement >= remainder:
        retirement += remainder
    else:
        taxable += remainder
    return retirement, taxable


def project(values, annual_spending, start_year=None):
    plan = validate(values)
    if annual_spending is None:
        if plan['annual_lifestyle_spending'] is None:
            raise ProjectionError('A usable 12-month spending estimate or an explicit lifestyle assumption is needed before calculating.')
        if plan['tax_payments_in_spending'] != 0:
            raise ProjectionError('Use zero for tax payments already in spending when there is no spending history.')
        historical_spending = None
    else:
        if (not isinstance(annual_spending, (int, float)) or isinstance(annual_spending, bool)
                or not math.isfinite(annual_spending) or annual_spending < 0):
            raise ProjectionError('A usable 12-month spending estimate is needed before calculating.')
        if plan['tax_payments_in_spending'] > annual_spending:
            raise ProjectionError('Tax payments already in spending cannot exceed the spending estimate.')
        historical_spending = annual_spending - plan['tax_payments_in_spending']
    start_year = date.today().year if start_year is None else start_year
    if (isinstance(start_year, bool) or not isinstance(start_year, int)
            or not TAX_YEAR <= start_year <= 2200):
        raise ProjectionError('Projection start year must be a whole year from 2026 to 2200.')
    people = plan['people']
    growth, inflation = plan['growth_rate'] / 100, plan['inflation_rate'] / 100
    balances = [[p['starting_pretax'], p['starting_roth']] for p in people]
    taxable = plan['starting_taxable']
    spending_base = historical_spending if plan['annual_lifestyle_spending'] is None else plan['annual_lifestyle_spending']
    horizon = END_AGE - int(min(p['current_age'] for p in people))
    for event in plan['life_events']:
        if event['start_year'] < start_year or event['start_year'] + event['years'] > start_year + horizon:
            raise ProjectionError(f'Life events must fit within the projection years {start_year}–{start_year + horizon - 1}.')
    tax_keys = ('federal_income_tax', 'federal_payroll_tax', 'oregon_tax', 'washington_tax', 'interstate_credit', 'taxes')
    rows = [dict(year=start_year, elapsed=0, ages=[p['current_age'] for p in people], factor=1,
                 cash_flow_year=None, cash_flow_factor=1,
                 retirement_assets=sum(map(sum, balances)), taxable_assets=taxable,
                 assets=sum(map(sum, balances)) + taxable, income=0, tax_advantaged_savings=0,
                 requested_savings=0, withdrawal=0, retirement_withdrawal=0, taxable_withdrawal=0,
                 spending=0, taxable_cash_flow=0, shortfall=0, event_spending=0,
                 event_funded=0, event_shortfall=0, total_shortfall=0,
                 **dict.fromkeys(tax_keys, 0))]
    retirement_offsets = [p['retirement_age'] - p['current_age'] for p in people]
    withdrawal_offset = min(retirement_offsets) if plan['withdrawal_start'] == 'first_retirement' else max(retirement_offsets)
    first_shortfall_year, total_real_shortfall = None, 0
    first_event_shortfall_year = None
    event_total_real_spending = total_real_event_shortfall = 0
    for elapsed in range(horizon):
        year = start_year + elapsed
        factor = (1 + inflation) ** elapsed
        tax_factor = (1 + inflation) ** (year - TAX_YEAR)
        income = contribution = requested = 0
        opening_retirement = sum(map(sum, balances))
        retirement_available = opening_retirement * (1 + growth)
        taxable_available = taxable * (1 + growth)
        target = (opening_retirement + taxable) * plan['withdrawal_rate'] / 100 if elapsed >= withdrawal_offset else 0
        retirement_withdrawal, taxable_withdrawal = split_withdrawal(target, retirement_available, taxable_available)
        withdrawal = retirement_withdrawal + taxable_withdrawal
        tax_people = []
        for person, balance in zip(people, balances):
            age = person['current_age'] + elapsed
            retired = age >= person['retirement_age']
            wages = 0 if retired else person['annual_income'] * factor
            desired = wages * person['tax_advantaged_rate'] / 100
            saving = min(desired, 24500 * tax_factor)
            distributions = [retirement_withdrawal * amount / opening_retirement if opening_retirement else 0
                             for amount in balance]
            for i in (0, 1):
                balance[i] = max(0, balance[i] * (1 + growth) - distributions[i])
            balance[0 if person['contribution_type'] == 'pre_tax' else 1] += saving
            tax_people.append(dict(age=age, wages=wages,
                pretax_savings=saving if person['contribution_type'] == 'pre_tax' else 0,
                taxable_withdrawals=distributions[0], residence_state=person['residence_state'],
                employment_state=person['employment_state'], work_state_percent=person['work_state_percent']))
            income += wages
            contribution += saving
            requested += desired
        taxes = household_taxes(tax_people, plan['filing_status'], plan['mfs_allocation'] or 'individual', tax_factor, year, inflation)
        spending = spending_base * factor
        cash_flow = income - contribution + withdrawal - spending - taxes['taxes']
        # The elected withdrawal is the spending budget; never silently take extra.
        shortfall = max(0, -cash_flow)
        closing = taxable_available - taxable_withdrawal + max(0, cash_flow)
        # Events are explicit additional spending, funded at year end from brokerage.
        # Recurring lifestyle gaps still never trigger an extra withdrawal.
        event_spending = sum(event['annual_amount'] for event in plan['life_events']
                             if event['start_year'] <= year < event['start_year'] + event['years']) * factor
        event_funded = min(event_spending, max(0, closing))
        event_shortfall = event_spending - event_funded
        closing -= event_funded
        if shortfall > 0 and first_shortfall_year is None:
            first_shortfall_year = year
        if event_shortfall > 0 and first_event_shortfall_year is None:
            first_event_shortfall_year = year
        # Annual flows use that year's prices, not the following balance boundary.
        total_real_shortfall += shortfall / factor
        event_total_real_spending += event_spending / factor
        total_real_event_shortfall += event_shortfall / factor
        taxable = max(0, closing)
        retirement = sum(map(sum, balances))
        rows.append(dict(year=year + 1, elapsed=elapsed + 1,
            ages=[p['current_age'] + elapsed + 1 for p in people], factor=factor * (1 + inflation),
            cash_flow_year=year, cash_flow_factor=factor,
            **{key: round(value, 2) for key, value in dict(retirement_assets=retirement,
                taxable_assets=taxable, assets=retirement + taxable, income=income,
                tax_advantaged_savings=contribution, requested_savings=requested,
                withdrawal=withdrawal, retirement_withdrawal=retirement_withdrawal, taxable_withdrawal=taxable_withdrawal,
                spending=spending, taxable_cash_flow=cash_flow,
                shortfall=shortfall, event_spending=event_spending, event_funded=event_funded,
                event_shortfall=event_shortfall, total_shortfall=shortfall + event_shortfall, **taxes).items()}))
    both_retired = int(max(p['retirement_age'] - p['current_age'] for p in people))
    monthly = []
    for opening, closing in zip(rows, rows[1:]):
        available = closing['income'] + closing['withdrawal'] - closing['tax_advantaged_savings'] - closing['taxes']
        monthly.append(dict(year=opening['year'], ages=opening['ages'], factor=opening['factor'],
            retired_count=sum(age >= person['retirement_age'] for age, person in zip(opening['ages'], people)),
            **{key: value / 12 for key, value in dict(spending=closing['spending'],
                available=available, difference=available - closing['spending'],
                withdrawal=closing['withdrawal'], shortfall=closing['shortfall'],
                event_spending=closing['event_spending'], event_funded=closing['event_funded'],
                event_shortfall=closing['event_shortfall'], total_shortfall=closing['total_shortfall'],
                taxes=closing['taxes']).items()}))
    gap_years = [year for year in (first_shortfall_year, first_event_shortfall_year) if year is not None]
    return dict(rows=rows, retirement=rows[both_retired], final=rows[-1],
                monthly=monthly, first_retirement_monthly=monthly[both_retired],
                annual_spending=annual_spending, historical_lifestyle_spending=historical_spending,
                adjusted_annual_spending=spending_base,
                annual_tax_advantaged_savings=rows[1]['tax_advantaged_savings'],
                annual_taxable_savings=max(0, rows[1]['taxable_cash_flow']),
                annual_event_funding=rows[1]['event_funded'],
                annual_net_taxable_savings=round(max(0, rows[1]['taxable_cash_flow']) - rows[1]['taxable_withdrawal'] - rows[1]['event_funded'], 2),
                annual_taxes=rows[1]['taxes'], first_shortfall_year=first_shortfall_year,
                total_real_shortfall=round(total_real_shortfall, 2),
                event_total_real_spending=round(event_total_real_spending, 2),
                total_real_event_shortfall=round(total_real_event_shortfall, 2),
                first_event_shortfall_year=first_event_shortfall_year,
                first_funding_gap_year=min(gap_years) if gap_years else None,
                total_real_funding_gap=round(total_real_shortfall + total_real_event_shortfall, 2),
                event_funding_note='Life events use brokerage after yearly growth and cash flow. Retirement funds are not drawn automatically; capital-gains taxes and borrowing are not modeled.')
