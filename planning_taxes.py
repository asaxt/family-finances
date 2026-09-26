"""Local 2026 planning estimates; ordinary W-2 income and retirement distributions.

Sources and deliberate exclusions are documented in docs/planning-taxes.md.
Future indexed thresholds follow the plan's inflation assumption, not a forecast
of future tax law. Statutory nonindexed thresholds remain fixed.
"""
import math

TAX_YEAR = 2026
FEDERAL_RATES = (.10, .12, .22, .24, .32, .35, .37)
FEDERAL_BRACKETS = {
    'single': (12400, 50400, 105700, 201775, 256225, 640600),
    'joint': (24800, 100800, 211400, 403550, 512450, 768700),
    'separate': (12400, 50400, 105700, 201775, 256225, 384350),
    'head': (17700, 67450, 105700, 201750, 256200, 640600),
}
STANDARD_DEDUCTION = {'single': 16100, 'joint': 32200, 'separate': 16100, 'head': 24150}
FILING_STATUSES = {'single': 'Single', 'joint': 'Married filing jointly',
                   'separate': 'Married filing separately', 'head': 'Head of household'}
SOCIAL_SECURITY_CAP = 184500


def federal_return(people, filing_status, factor=1, year=TAX_YEAR):
    """Estimate one return, preserving each person's wage and saving treatment.

    Call separately for separate returns; never pool unrelated household members.
    Savings inputs must already be eligible contributions, within plan limits.
    State taxes are calculated separately by household_taxes.
    """
    if filing_status not in FILING_STATUSES:
        raise ValueError("Choose a supported federal filing status.")
    count = 2 if filing_status == 'joint' else 1
    if not isinstance(people, list) or len(people) != count:
        raise ValueError("A joint return needs two people; other returns need one.")
    if (isinstance(factor, bool) or not isinstance(factor, (int, float))
            or not math.isfinite(factor) or factor <= 0):
        raise ValueError("The inflation factor must be positive and finite.")
    if isinstance(year, bool) or not isinstance(year, int) or year < TAX_YEAR:
        raise ValueError("The tax estimate starts in 2026.")
    fields = {'age', 'wages', 'pretax_savings', 'roth_savings', 'taxable_withdrawals'}
    for person in people:
        if not isinstance(person, dict) or set(person) != fields:
            raise ValueError("Complete each person's federal tax inputs.")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value < 0 for value in person.values()):
            raise ValueError("Federal tax inputs must be nonnegative finite numbers.")
        if person['age'] != int(person['age']) or not 18 <= person['age'] <= 120:
            raise ValueError("Each person's age must be a whole number from 18 to 120.")
        if person['pretax_savings'] + person['roth_savings'] > person['wages']:
            raise ValueError("Employee retirement savings cannot exceed wages.")
    wages = [person['wages'] for person in people]
    income = sum(person['wages'] - person['pretax_savings']
                 + person['taxable_withdrawals'] for person in people)
    income_tax = federal_income_tax(income, filing_status,
                                    [person['age'] for person in people], factor, year)
    payroll_tax = federal_payroll_tax(wages, filing_status, factor)
    return {'ordinary_income': round(income, 2), 'federal_income_tax': round(income_tax, 2),
            'federal_payroll_tax': round(payroll_tax, 2),
            'federal_total': round(income_tax + payroll_tax, 2)}


def progressive_tax(taxable, thresholds, rates, factor=1):
    tax, floor = 0, 0
    for ceiling, rate in zip((*thresholds, float('inf')), rates):
        ceiling *= factor
        tax += max(0, min(taxable, ceiling) - floor) * rate
        floor = ceiling
    return tax


def federal_income_tax(income, filing_status, ages, factor=1, year=TAX_YEAR):
    """Standard deduction and age deductions, without credits or itemization."""
    deduction = STANDARD_DEDUCTION[filing_status] * factor
    senior_count = sum(age >= 65 for age in ages)
    deduction += senior_count * (1650 if filing_status in {'joint', 'separate'} else 2050) * factor
    if year <= 2028 and filing_status != 'separate':
        threshold = 150000 if filing_status == 'joint' else 75000
        deduction += senior_count * max(0, 6000 - .06 * max(0, income - threshold))
    taxable = max(0, income - deduction)
    return progressive_tax(taxable, FEDERAL_BRACKETS[filing_status], FEDERAL_RATES, factor)


def federal_payroll_tax(wages, filing_status, factor=1):
    """Employee FICA; the Social Security cap applies to each worker separately."""
    tax = sum(min(wage, SOCIAL_SECURITY_CAP * factor) * .062 + wage * .0145 for wage in wages)
    threshold = 250000 if filing_status == 'joint' else 125000 if filing_status == 'separate' else 200000
    tax += max(0, sum(wages) - threshold) * .009
    return tax


STATES = {'WA': 'Washington', 'OR': 'Oregon'}


def oregon_tax(income, oregon_income, status, federal_tax, ages, factor=1):
    """Ordinary-income estimate using OR-40-N deduction proration."""
    if income <= 0 or oregon_income <= 0:
        return 0
    joint = status == 'joint'
    ratio = min(1, oregon_income / income)
    standard = (5820 if joint else 2910) * factor + 1000 * sum(age >= 65 for age in ages)
    # The federal subtraction limit is halved for separate returns.
    phase_start, phase_step = (250000, 10000) if joint else (125000, 5000)
    phase = max(0, 1 - .2 * max(0, 1 + math.floor((income - phase_start) / phase_step)))
    federal_subtraction = min(federal_tax, (8750 if joint else 4375) * factor * phase)
    taxable = max(0, oregon_income - (standard + federal_subtraction) * ratio)
    thresholds = (9100 * factor, 22800 * factor, 250000) if joint else (4550 * factor, 11400 * factor, 125000)
    # Keep the two indexed bands below the fixed top-band threshold in long scenarios.
    thresholds = tuple(min(value, thresholds[-1]) for value in thresholds)
    tax = progressive_tax(taxable, thresholds, (.0475, .0675, .0875, .099))
    credit = 263 * factor * len(ages) * ratio if income <= (200000 if joint else 100000) else 0
    return max(0, tax - credit)


def washington_tax(income, washington_income, status, year, inflation):
    """Enacted ordinary-income tax from 2028; capital gains are not modeled."""
    if year < 2028 or income <= 0 or washington_income <= 0:
        return 0
    # Biennial indexing begins with 2029 income, collected in 2030.
    periods = max(0, (year - 2027) // 2)
    deduction = 1000000 * (1 + inflation) ** (2 * periods)
    if status == 'separate':
        deduction /= 2  # Explicit equal allocation of the shared married deduction.
    deduction *= min(1, washington_income / income)
    return max(0, washington_income - deduction) * .099


def household_taxes(people, status, allocation, factor=1, year=TAX_YEAR, inflation=0):
    """Two married adults; federal + OR/WA ordinary-income planning estimate.

    Each person supplies wages, pretax_savings, taxable_withdrawals, age, states,
    and work_state_percent. Community allocation affects income tax, never FICA.
    Residence and work locations are assumed constant for the full year.
    """
    if status not in {'joint', 'separate'} or len(people) != 2:
        raise ValueError('Choose joint or separate filing for two spouses.')
    if allocation not in {'individual', 'community_wages', 'community_all'}:
        raise ValueError('Choose how income is allocated to separate returns.')
    incomes, sources, overlaps = [], [], []
    for person in people:
        residence, work = person['residence_state'], person['employment_state']
        if residence not in STATES or work not in STATES:
            raise ValueError('This estimate supports Washington and Oregon only.')
        net_wages = person['wages'] - person['pretax_savings']
        retirement = person['taxable_withdrawals']
        fraction = person['work_state_percent'] / 100
        worked = {state: net_wages * ((fraction if work == state else 0)
                  + (1 - fraction if residence == state else 0)) for state in STATES}
        income = net_wages + retirement
        incomes.append([net_wages, retirement])
        sources.append({state: income if residence == state else worked[state] for state in STATES})
        # Income taxed by both states, tagged by the state granting a resident credit.
        overlaps.append({state: worked['OR' if state == 'WA' else 'WA'] if residence == state else 0
                         for state in STATES})
    if status == 'separate' and allocation != 'individual':
        for index in (0, 1) if allocation == 'community_all' else (0,):
            average = sum(row[index] for row in incomes) / 2
            for row in incomes:
                row[index] = average
        # Community allocation is supported where both spouses share domicile.
        if people[0]['residence_state'] != people[1]['residence_state']:
            raise ValueError('Community allocation requires the same residence state for both spouses in this preview.')
        for state in STATES:
            for collection in (sources, overlaps):
                average = sum(row[state] for row in collection) / 2
                if allocation == 'community_wages' and collection is sources and state == people[0]['residence_state']:
                    for i, row in enumerate(collection):
                        row[state] = incomes[i][0] + incomes[i][1]
                else:
                    for row in collection:
                        row[state] = average
    totals = dict(federal_income_tax=0, federal_payroll_tax=0, oregon_tax=0,
                  washington_tax=0, interstate_credit=0)
    groups = [(0, 1)] if status == 'joint' else [(0,), (1,)]
    for group in groups:
        income = sum(sum(incomes[i]) for i in group)
        ages = [people[i]['age'] for i in group]
        federal = federal_income_tax(income, status, ages, factor, year)
        payroll = federal_payroll_tax([people[i]['wages'] for i in group], status, factor)
        sourced = {state: sum(sources[i][state] for i in group) for state in STATES}
        oregon = oregon_tax(income, sourced['OR'], status, federal, ages, factor)
        washington = washington_tax(income, sourced['WA'], status, year, inflation)
        state_taxes = {'OR': oregon, 'WA': washington}
        credits = {}
        for state, other in (('OR', 'WA'), ('WA', 'OR')):
            overlap = sum(overlaps[i][state] for i in group)
            credits[state] = min(state_taxes[state] * overlap / sourced[state] if sourced[state] else 0,
                                 state_taxes[other] * overlap / sourced[other] if sourced[other] else 0)
        totals['federal_income_tax'] += federal
        totals['federal_payroll_tax'] += payroll
        totals['oregon_tax'] += oregon - credits['OR']
        totals['washington_tax'] += washington - credits['WA']
        totals['interstate_credit'] += sum(credits.values())
    totals['taxes'] = sum(totals[key] for key in ('federal_income_tax', 'federal_payroll_tax', 'oregon_tax', 'washington_tax'))
    return totals
