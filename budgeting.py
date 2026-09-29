"""Monthly allowances and cumulative utilization, stored in the encrypted vault."""
import calendar
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from analytics import CATEGORY_RULE_JOIN, EFFECTIVE_CATEGORY_SQL, EFFECTIVE_CASH_FLOW_SQL, month_range, shift_month, scope_filter, DISPLAY_NAME_SQL

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


def replacement_review(state, target, plan, revision, first):
    """Describe every effective interval replaced by an ongoing plan."""
    starts = sorted({target} | {key for key in state['plans'] if key > target})
    intervals = []
    def values(item):
        if item is None:
            return {}
        return {line['category']: (line['kind'],line['value']) for line in item['lines']}
    proposed = values(plan)
    for index, start in enumerate(starts):
        source, old = plan_for(state,start)
        previous = values(old)
        changes = [{'category':name, 'before':previous.get(name), 'after':proposed.get(name)}
                   for name in sorted(previous.keys() | proposed.keys(),key=str.casefold)
                   if previous.get(name)!=proposed.get(name)]
        def income(item):
            return None if item is None else ('recorded', None) if item['income_mode']=='recorded' else ('amount',item['income'])
        if income(old)!=income(plan):
            changes.insert(0,{'category':'Income base','before':income(old),'after':income(plan)})
        intervals.append({'first':start,'last':shift_month(starts[index+1],-1) if index+1<len(starts) else None,
                          'source':source,'changes':changes})
    token = hashlib.sha256(json.dumps([revision,target,first,plan],sort_keys=True).encode()).hexdigest()
    return {'intervals':intervals,'confirmation':token,
            'conflict':any(row['source'] and row['changes'] for row in intervals),
            'replaced_starts':[key for key in sorted(state['plans']) if key>=target]}


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


def activity(connection, first, last, today, account_id=None, connection_id=None):
    scope_sql, scope_params = scope_filter(account_id, connection_id)
    rows = connection.execute(f'''
        SELECT substr(t.transacted_at,1,7) AS month, COALESCE(r.name, {EFFECTIVE_CATEGORY_SQL}) AS category,
               {EFFECTIVE_CASH_FLOW_SQL} AS treatment, t.currency, a.spending_enabled,
               a.cash_flow_role, COUNT(*) AS count, SUM(t.amount) AS amount,
               SUM(CASE WHEN t.amount<0 THEN -t.amount ELSE 0 END) AS inflows
        FROM transactions t JOIN accounts a ON a.id=t.account_id {CATEGORY_RULE_JOIN}
        WHERE t.pending=0 AND t.excluded=0 AND t.transacted_at >= ? AND t.transacted_at < ?
              AND t.transacted_at <= ? AND (a.spending_enabled=1 OR a.cash_flow_role='cash_flow')
        {scope_sql}
        GROUP BY 1, 2, 3, 4, 5, 6
    ''', (first+'-01', shift_month(last,1)+'-01', today.isoformat(), *scope_params)).fetchall()
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


def report(connection, state, first, last, today=None, account_id=None, connection_id=None, recorded=None):
    today = today or date.today()
    months = period(first, last)
    if recorded is None:
        recorded = activity(connection,first,last,today,account_id,connection_id)
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


def quarter_key(value):
    value = month(value)
    return f"{value[:4]}-Q{(int(value[5:])-1)//3+1}"


def quarter_bounds(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-Q[1-4]',value):
        raise BudgetError('Choose a valid calendar quarter.')
    first = month(f"{value[:4]}-{(int(value[-1])-1)*3+1:02d}")
    return first, shift_month(first,2)


def end_date(value):
    return f"{value}-{calendar.monthrange(*map(int,value.split('-')))[1]:02d}"


def group_periods(points, granularity):
    """Sum monthly values, applying each month's saved allowance before grouping."""
    groups = {}
    for point in points:
        key = 'rolling' if granularity=='rolling' else point['month'][:4] if granularity=='year' else quarter_key(point['month']) if granularity=='quarter' else point['month']
        groups.setdefault(key,[]).append(point)
    result = []
    for key, sample in groups.items():
        row = {name:sum(point.get(name,0) for point in sample) for name in
               ('actual','allowance','deposited','records','unknown','foreign')}
        row.update(key=key,month=sample[-1]['month'],label=key.replace('-',' '),
                   date_from=sample[0]['month']+'-01', date_to=end_date(sample[-1]['month']),
                   future=all(point['future'] for point in sample),
                   partial=any(point['partial'] or point['future'] for point in sample),
                   incomplete=len(sample)<(12 if granularity in {'year','rolling'} else 3 if granularity=='quarter' else 1) or any(not point['records'] and not point['future'] for point in sample),
                   plan=any(point.get('plan') for point in sample), rows=[])
        categories = {}
        for point in sample:
            for item in point['rows']:
                total=categories.setdefault(item['category'],{'category':item['category'],'actual':0,'allowance':0})
                total['actual']+=item['actual']
                total['allowance']+=item['allowance']
        for item in categories.values():
            item.update(remaining=item['allowance']-item['actual'],percent=utilization(item['actual'],item['allowance']))
        row['rows']=list(categories.values())
        row['remaining']=row['allowance']-row['actual']
        row['brokerage_plan']=sum(point['brokerage_plan'] or 0 for point in sample) if row['plan'] else None
        covered=[point for point in sample if not point['future']]
        row['brokerage_actual']=(row['deposited']-row['actual'] if covered and all(point['brokerage_actual'] is not None for point in covered) else None)
        result.append(row)
    return result


def moving_average(values, window):
    result=[]
    for index in range(len(values)):
        sample=values[max(0,index-window+1):index+1]
        result.append(round(sum(sample)/window) if len(sample)==window and all(value is not None for value in sample) else None)
    return result


def spending_history(recorded, granularity, today):
    """Full recorded spending history; deliberately independent of budget settings."""
    if not recorded:
        return {'points':[],'categories':[],'first':None,'last':None,'windows':[], 'omitted':0}
    monthly=[]
    omitted=0
    for target in month_range(min(recorded),max(recorded)):
        amounts={}
        records=0
        for row in recorded.get(target,[]):
            if row['currency']!='USD' or row['treatment'] is None:
                omitted+=row['count']
                continue
            records+=row['count']
            if row['spending_enabled'] and row['treatment']=='spending':
                amounts[row['category']]=amounts.get(row['category'],0)+row['amount']
        monthly.append({'month':target,'actual':sum(amounts.values()),'records':records,
                        'future':False,'partial':target==today.strftime('%Y-%m'),
                        'brokerage_actual':None,'brokerage_plan':None,
                        'rows':[{'category':name,'actual':value,'allowance':0} for name,value in amounts.items()]})
    points=group_periods(monthly,granularity)
    if granularity=='quarter':
        for point in points:
            first,last=quarter_bounds(point['key'])
            point['date_from'],point['date_to']=first+'-01',end_date(last)
    windows=[4,8] if granularity=='quarter' else [3,6,12,24]
    totals={}
    for index,point in enumerate(points):
        for row in point['rows']:
            totals[row['category']]=totals.get(row['category'],0)+row['actual']
        for window in windows:
            sample=points[max(0,index-window+1):index+1]
            point[f'ma_{window}']=(round(sum(item['actual'] for item in sample)/window)
                                  if len(sample)==window and all(item['records'] and not item['incomplete'] for item in sample) else None)
    categories=[]
    for name,total in sorted(totals.items(),key=lambda pair:pair[1],reverse=True):
        values=[next((row['actual'] for row in point['rows'] if row['category']==name),0) if point['records'] else None for point in points]
        categories.append({'name':name,'total':total,'values':values,
                           'ma_3':moving_average(values,3),'ma_12':moving_average(values,12)})
    return {'points':points,'categories':categories,'first':min(recorded)+'-01',
            'last':min(end_date(max(recorded)),today.isoformat()),'windows':windows,'omitted':omitted}


def dashboard(connection, state, first, last, account_id=None, connection_id=None, today=None, granularity="month", snapshot_first=None):
    today = today or date.today()
    household = not account_id and not connection_id
    # A scoped spending view must never compare a subset with a household allowance.
    visible_state = state if household else {'plans': {}}
    if granularity not in {'month','quarter','year','rolling'}:
        raise BudgetError('Choose monthly, calendar-quarter, or calendar-year reporting.')
    period(first,last)
    snapshot_first = snapshot_first or last
    recorded = activity(connection,'1900-01','2200-12',today,account_id,connection_id)
    data = report(connection, visible_state, first, last, today, account_id, connection_id, recorded)
    selected = report(connection, visible_state, snapshot_first, last, today, account_id, connection_id, recorded)
    current = group_periods(selected['history'],granularity)[-1]
    data['current'] = current
    data['spending'] = spending_history(recorded,'month' if granularity in {'year','rolling'} else granularity,today)
    def observed(row):
        return bool(row['records']) and not row['future'] and not row['unknown'] and not row['foreign'] and not row.get('incomplete')
    points=data['spending']['points']
    previous_key=quarter_key(shift_month(last,-12)) if granularity=='quarter' else shift_month(last,-12)
    previous=next((row for row in points if row['key']==previous_key),None)
    if granularity in {'year','rolling'} and snapshot_first>'1900-12':
        prior_year = report(connection,visible_state,shift_month(snapshot_first,-12),shift_month(last,-12),today,account_id,connection_id,recorded)
        previous = group_periods(prior_year['history'],granularity)[-1]
    data['yoy']=((current['actual']-previous['actual'])/abs(previous['actual'])*100
                 if previous and previous['actual'] and observed(previous) and observed(current) and not current['partial'] else None)
    if granularity=='month':
        heat_history = report(connection,visible_state,max('1900-01',shift_month(last,-11)),last,today,account_id,connection_id,recorded)
        heat_months = group_periods(heat_history['history'],'month')
    else:
        heat_months = group_periods(selected['history'],'month') if granularity in {'year','rolling'} else group_periods(data['history'],granularity)[-12:]
    data['heat_months'] = [row['label'] for row in heat_months]
    data['heat_budget'] = household and any(row['plan'] for row in heat_months)
    current_rows = {row['category']:row for row in current['rows']}
    accumulated = {row['category'] for row in data['totals']}
    heat_categories = {row['category'] for point in heat_months for row in point['rows']}
    for name in (current_rows.keys() | heat_categories)-accumulated:
        data['totals'].append({'category':name,'actual':0,'allowance':0,'remaining':0,'percent':None})
    for total in data['totals']:
        name = total['category']
        total['current'] = current_rows.get(name, {'actual':0,'allowance':0,'unbudgeted':True})
        total['heat'] = []
        cells = [next((row for row in point['rows'] if row['category']==name), None) for point in heat_months]
        peak = max([abs(row['actual']) for row in cells if row] or [0])
        for point, row in zip(heat_months,cells):
            actual, allowance = (row['actual'],row['allowance']) if row else (0,0)
            ratio = utilization(actual,allowance)
            if not observed(point):
                level, label = 'missing', 'Future month' if point['future'] else 'Incomplete or unavailable records'
            elif not data['heat_budget']:
                level = f'intensity-{min(4, max(1, (abs(actual)*4+peak-1)//peak))}' if actual and peak else 'empty'
                label = 'Net refund' if actual<0 else 'Recorded spending'
            elif not point['plan'] or not allowance:
                level, label = ('unbudgeted' if actual else 'empty'), 'No allowance'
            elif ratio > 100:
                level, label = 'over', f'{ratio:g}% used'
            elif ratio >= 80:
                level, label = 'near', f'{ratio:g}% used'
            else:
                level, label = 'low', f'{ratio:g}% used'
            total['heat'].append({'month':point['label'],'date_from':point['date_from'],'date_to':point['date_to'],'actual':actual,'allowance':allowance,
                                  'level':level,'label':label,'percent':ratio})
    data['totals'].sort(key=lambda row:row['actual'],reverse=True)
    # Five recent transactions per category, using precisely the report's scope.
    scope_sql, params = scope_filter(account_id,connection_id)
    rows = connection.execute(f"""
        WITH ranked AS (
          SELECT COALESCE(r.name, {EFFECTIVE_CATEGORY_SQL}) AS category,
                 {DISPLAY_NAME_SQL} AS name, t.transacted_at, t.amount,
                 ROW_NUMBER() OVER (PARTITION BY COALESCE(r.name, {EFFECTIVE_CATEGORY_SQL})
                                    ORDER BY t.transacted_at DESC, ABS(t.amount) DESC, t.id) AS position
          FROM transactions t JOIN accounts a ON a.id=t.account_id {CATEGORY_RULE_JOIN}
          WHERE t.pending=0 AND t.excluded=0 AND t.currency='USD' AND a.spending_enabled=1
                AND ({EFFECTIVE_CASH_FLOW_SQL})='spending' AND t.amount!=0
                AND t.transacted_at>=? AND t.transacted_at<=? AND t.transacted_at<=? {scope_sql}
        ) SELECT category,name,transacted_at,amount FROM ranked WHERE position<=5
        ORDER BY category,position
    """, (snapshot_first+'-01',end_date(last),today.isoformat(),*params)).fetchall()
    data['details'] = {}
    for row in rows:
        data['details'].setdefault(row['category'],[]).append(dict(row))
    return data
