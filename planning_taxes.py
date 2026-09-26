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
    State taxes are deliberately absent until a supported state policy is supplied.
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
