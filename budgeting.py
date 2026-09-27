"""Monthly allowances and cumulative utilization, stored in the encrypted vault."""
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from analytics import CATEGORY_RULE_JOIN, EFFECTIVE_CATEGORY_SQL, EFFECTIVE_CASH_FLOW_SQL, month_range, shift_month

SETTING = 'monthly_budget_v1'


class BudgetError(ValueError):
    pass


def month(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value):
        raise BudgetError('Choose a valid month.')
    if not 1900 <= int(value[:4]) <= 2200:
        raise BudgetError('Choose a month between 1900 and 2200.')
    return value


def cents(value, *, percent=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > (100 if percent else 1_000_000_000):
            raise ValueError
        if number != number.quantize(Decimal('.01')):
            raise ValueError
        return int(number * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise BudgetError('Enter nonnegative amounts with up to two decimal places; percentages must be between 0 and 100.') from None


def load(connection):
    row = connection.execute('SELECT value FROM settings WHERE key=?', (SETTING,)).fetchone()
    return json.loads(row[0]) if row else {'plans': {}}


def save(connection, state):
    connection.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (SETTING, json.dumps(state, separators=(',', ':'))))


def palette(connection):
    return [dict(row) for row in connection.execute('SELECT name,flow_type FROM category_rules ORDER BY name COLLATE NOCASE')]


def version(connection, state):
    return hashlib.sha256(json.dumps([state, palette(connection)], sort_keys=True).encode()).hexdigest()


def plan_for(state, target):
    keys = [key for key in state['plans'] if key <= target]
    key = max(keys) if keys else None
    return key, state['plans'].get(key) if key else None


def validate_plan(payload, allowed):
    if not isinstance(payload, dict) or not isinstance(payload.get('lines'), list) or len(payload['lines']) > 500:
        raise BudgetError('Send a valid budget with at most 500 category lines.')
    mode = payload.get('income_mode', 'recorded')
    if not isinstance(mode, str) or mode not in {'recorded', 'planned'}:
        raise BudgetError('Choose recorded income or a planned monthly income.')
    result = {'income_mode': mode, 'income': cents(payload.get('income')) if mode == 'planned' else None}
    seen = set()
    result['lines'] = []
    for row in payload['lines']:
        if (not isinstance(row, dict) or not isinstance(row.get('category'), str)
                or row['category'] not in allowed or not isinstance(row.get('kind'), str)
                or row['kind'] not in {'amount', 'percent'}):
            raise BudgetError('A category changed or a line is invalid. Reload the budget and try again.')
        if row['category'].casefold() in seen:
            raise BudgetError('Each category can appear only once in a budget.')
        seen.add(row['category'].casefold())
        kind = row['kind']
        result['lines'].append({'category': row['category'], 'kind': kind,
                                'value': cents(row.get('value'), percent=kind == 'percent')})
    return result


def allocations(plan, recorded_income=0):
    remaining = max(0, recorded_income if plan['income_mode'] == 'recorded' else plan['income'])
    result = {}
    for row in plan['lines']:
        result[row['category']] = (row['value'] if row['kind'] == 'amount' else
            int((Decimal(remaining) * row['value'] / 10000).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
    return result


def renamed_categories(connection, mapping):
    state = load(connection)
    changed = False
    for plan in state['plans'].values():
        for line in plan['lines']:
            replacement = mapping.get(line['category'].casefold())
            if replacement and replacement != line['category']:
                line['category'] = replacement
                changed = True
    if changed:
        save(connection, state)


def activity(connection, first, last, today):
    rows = connection.execute(f'''
        SELECT substr(t.transacted_at,1,7) AS month, COALESCE(r.name, {EFFECTIVE_CATEGORY_SQL}) AS category,
               {EFFECTIVE_CASH_FLOW_SQL} AS treatment, t.currency, a.spending_enabled,
               a.cash_flow_role, COUNT(*) AS count, SUM(t.amount) AS amount,
               SUM(CASE WHEN t.amount<0 THEN -t.amount ELSE 0 END) AS inflows
        FROM transactions t JOIN accounts a ON a.id=t.account_id {CATEGORY_RULE_JOIN}
        WHERE t.pending=0 AND t.excluded=0 AND t.transacted_at >= ? AND t.transacted_at < ?
              AND t.transacted_at <= ? AND (a.spending_enabled=1 OR a.cash_flow_role='cash_flow')
        GROUP BY 1, 2, 3, 4, 5, 6
    ''', (first+'-01', shift_month(last,1)+'-01', today.isoformat())).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row['month'], []).append(dict(row))
    return grouped


def utilization(actual, allowance):
    return round(actual / allowance * 100, 1) if allowance > 0 else None


def period(first, last):
    months = month_range(month(first), month(last))
    if not months or len(months) > 120:
        raise BudgetError('Choose an accumulation period of one to 120 months, ending in the selected month.')
    return months


def report(connection, state, first, last, today=None):
    today = today or date.today()
    months = period(first, last)
    recorded = activity(connection,first,last,today)
    history, totals = [], {}
    for target in months:
        effective, plan = plan_for(state,target)
        spent = {}
        unknown = foreign = records = 0
        deposited = 0
        for row in recorded.get(target,[]):
            if row['currency'] != 'USD':
                foreign += row['count']
                continue
            records += row['count']
            if row['treatment'] == 'earned_income' and row['cash_flow_role'] == 'cash_flow':
                deposited += row['inflows']
            if row['treatment'] is None:
                unknown += row['count']
            if not row['spending_enabled']:
                continue
            if row['treatment'] == 'spending':
                spent[row['category']] = spent.get(row['category'],0) + row['amount']

        allowed = allocations(plan,deposited) if plan else {}
        income_base = (deposited if plan['income_mode']=='recorded' else plan['income']) if plan else None
        future = target > today.strftime('%Y-%m')
        complete = not future and not unknown and not foreign and records > 0
        rows=[]
        for category in sorted(set(allowed)|set(spent),key=str.casefold):
            allowance, amount = allowed.get(category,0), spent.get(category,0)
            rows.append({'category':category,'allowance':allowance,'actual':amount,'remaining':allowance-amount,
                         'percent':utilization(amount,allowance),'unbudgeted':category not in allowed})
            if not future:
                total=totals.setdefault(category,{'category':category,'allowance':0,'actual':0})
                total['allowance'] += allowance
                total['actual'] += amount
        planned_spending, actual_spending = sum(allowed.values()),sum(spent.values())
        history.append({'month':target,'plan':plan,'effective':effective,'rows':rows,
                        'allowance':planned_spending,'actual':actual_spending,'remaining':planned_spending-actual_spending,
                        'income_base':income_base, 'deposited':deposited,
                        'brokerage_plan':income_base-planned_spending if plan else None,
                        'brokerage_actual':deposited-actual_spending if complete else None,
                        'unknown':unknown,'foreign':foreign,'records':records,'future':future,
                        'partial':target==today.strftime('%Y-%m')})
    for row in totals.values():
        row['remaining']=row['allowance']-row['actual']
        row['percent']=utilization(row['actual'],row['allowance'])
    current=history[-1]
    return {'current':current,'history':history,'totals':sorted(totals.values(),key=lambda row:row['category'].casefold()),
            'first':first,'last':last,'missing_plans':sum(row['plan'] is None for row in history),
            'empty_months':sum(row['records']==0 and not row['future'] for row in history),
            'unknown':sum(row['unknown'] for row in history),'foreign':sum(row['foreign'] for row in history)}
