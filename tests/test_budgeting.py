import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import budgeting
from schema import create_schema
from tests import test_app_setup


def seed(connection):
    connection.execute("INSERT INTO connections(id,owner_name,institution,access_token) VALUES(1,'EXAMPLE PERSON','EXAMPLE BANK','fictional')")
    for identifier,kind,role,spending in [('sample-bank','depository','cash_flow',1),('sample-card','credit','other',1),('sample-ignore','depository','other',0)]:
        connection.execute('INSERT INTO accounts(id,connection_id,institution,name,type,cash_flow_role,spending_enabled) VALUES(?,1,\'EXAMPLE BANK\',\'EXAMPLE ACCOUNT\',?,?,?)',(identifier,kind,role,spending))
    connection.executemany("INSERT INTO category_rules(name,flow_type) VALUES(?,'spending')",[('SAMPLE ALPHA',),('SAMPLE BETA',)])


def transaction(connection,identifier,month,amount,category='SAMPLE ALPHA',account='sample-card',pending=0,excluded=0,currency='USD'):
    connection.execute('INSERT INTO transactions(id,account_id,amount,currency,description,pending,excluded,transacted_at,category) VALUES(?,?,?,?,\'SAMPLE TRANSACTION\',?,?,?,?)',(identifier,account,amount,currency,pending,excluded,month+'-03',category))


def fixed(value='11.75'):
    return budgeting.validate_plan({'income_mode':'recorded','lines':[{'category':'SAMPLE ALPHA','kind':'amount','value':value}]},{'SAMPLE ALPHA'})


class BudgetMathTests(unittest.TestCase):
    def setUp(self):
        self.connection=sqlite3.connect(':memory:')
        self.connection.row_factory=sqlite3.Row
        create_schema(self.connection)
        seed(self.connection)

    def tearDown(self):
        self.connection.close()

    def report(self,state,first='2001-12',last='2002-02',today=date(2002,3,1)):
        return budgeting.report(self.connection,state,first,last,today)

    def test_accumulation_crosses_january_and_refunds_reduce_actuals(self):
        state={'plans':{'2001-12':fixed()}}
        transaction(self.connection,'sample-1','2001-12',241)
        transaction(self.connection,'sample-2','2002-01',113)
        transaction(self.connection,'sample-refund','2002-02',-71)
        result=self.report(state)
        total=result['totals'][0]
        self.assertEqual((total['allowance'],total['actual'],total['remaining']),(3525,283,3242))
        self.assertEqual(total['percent'],8.0)
        self.assertEqual(result['current']['rows'][0]['actual'],-71)

    def test_percentages_share_recorded_income_base_and_round_to_cents(self):
        plan=budgeting.validate_plan({'lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'25.00'},{'category':'SAMPLE BETA','kind':'percent','value':'12.50'}]}, {'SAMPLE ALPHA','SAMPLE BETA'})
        transaction(self.connection,'sample-income','2002-02',-12346,'Income','sample-bank')
        result=self.report({'plans':{'2002-02':plan}},'2002-02','2002-02')['current']
        self.assertEqual([row['allowance'] for row in result['rows']],[3087,1543])
        self.assertEqual(result['income_base'],12346)
        self.assertEqual(result['brokerage_plan'],7716)
        self.assertEqual(result['brokerage_actual'],12346)

    def test_new_month_plan_preserves_prior_allowances_and_negative_shortfalls(self):
        state={'plans':{'2001-12':fixed(),'2002-02':fixed('18.93')}}
        transaction(self.connection,'sample-large','2002-02',2931)
        result=self.report(state)
        self.assertEqual([row['allowance'] for row in result['history']],[1175,1175,1893])
        self.assertEqual(result['current']['remaining'],-1038)
        self.assertEqual(result['current']['brokerage_actual'],-2931)
        self.assertEqual(result['totals'][0]['allowance'],4243)

    def test_scope_excludes_pending_ignored_transfers_and_combines_card_spending(self):
        transaction(self.connection,'sample-income','2002-02',-9876,'Income','sample-bank')
        transaction(self.connection,'sample-card','2002-02',347)
        transaction(self.connection,'sample-bank','2002-02',211,account='sample-bank')
        transaction(self.connection,'sample-pending','2002-02',733,pending=1)
        transaction(self.connection,'sample-excluded','2002-02',855,excluded=1)
        transaction(self.connection,'sample-ignored','2002-02',965,account='sample-ignore')
        transaction(self.connection,'sample-transfer','2002-02',347,'Transfer','sample-bank')
        result=self.report({'plans':{'2002-02':fixed()}},'2002-02','2002-02')['current']
        self.assertEqual((result['deposited'],result['actual'],result['brokerage_actual']),(9876,558,9318))

    def test_effective_rules_and_manual_choices_match_canonical_budget_labels(self):
        transaction(self.connection,'sample-ruled','2002-02',347)
        transaction(self.connection,'sample-manual','2002-02',211)
        self.connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category,source) VALUES ('sample-card','description','SAMPLE TRANSACTION','sample beta','user')")
        self.connection.execute("UPDATE transactions SET category_override='sample alpha',category_override_source='user' WHERE id='sample-manual'")
        current=self.report({'plans':{'2002-02':fixed()}},'2002-02','2002-02')['current']
        self.assertEqual([(row['category'],row['actual']) for row in current['rows']],[('SAMPLE ALPHA',211),('SAMPLE BETA',347)])
        self.assertFalse(current['rows'][0]['unbudgeted'])

    def test_unbudgeted_categories_are_never_hidden(self):
        transaction(self.connection,'sample-new','2002-02',513,'SAMPLE BETA')
        result=self.report({'plans':{'2002-02':fixed()}},'2002-02','2002-02')
        unbudgeted=next(row for row in result['current']['rows'] if row['category']=='SAMPLE BETA')
        self.assertTrue(unbudgeted['unbudgeted'])
        self.assertEqual(unbudgeted['remaining'],-513)
        self.assertIsNone(unbudgeted['percent'])

    def test_missing_months_foreign_currency_unknowns_and_future_are_explicit(self):
        transaction(self.connection,'sample-foreign','2002-02',123,currency='EUR')
        transaction(self.connection,'sample-unknown','2002-02',731,'Uncategorized')
        result=self.report({'plans':{'2002-01':fixed()}})
        self.assertEqual(result['missing_plans'],1)
        self.assertEqual(result['empty_months'],2)
        self.assertEqual((result['foreign'],result['unknown']),(1,1))
        self.assertIsNone(result['current']['brokerage_actual'])
        future=self.report({'plans':{'2002-01':fixed()}},'2002-01','2002-04',date(2002,2,10))
        self.assertTrue(future['current']['future'])
        self.assertEqual(future['totals'][0]['allowance'],2350)

    def test_planned_income_is_optional_and_does_not_replace_actual_income(self):
        plan=budgeting.validate_plan({'income_mode':'planned','income':'183.17','lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'20'}]}, {'SAMPLE ALPHA'})
        transaction(self.connection,'sample-income','2002-02',-9127,'Income','sample-bank')
        current=self.report({'plans':{'2002-02':plan}},'2002-02','2002-02')['current']
        self.assertEqual(current['allowance'],3663)
        self.assertEqual(current['deposited'],9127)
        self.assertEqual(current['brokerage_plan'],14654)
        self.assertEqual(current['brokerage_actual'],9127)

    def test_validation_rejects_nonfinite_invalid_precision_duplicates_and_ranges(self):
        for value in ('NaN','Infinity','-1','1.234','1000000001'):
            with self.subTest(value=value), self.assertRaises(budgeting.BudgetError):
                fixed(value)
        for value in ('2002-13','2002-2','0000-01',None):
            with self.subTest(value=value), self.assertRaises(budgeting.BudgetError):
                budgeting.month(value)
        with self.assertRaises(budgeting.BudgetError):
            self.report({'plans':{}},'2002-02','2002-01')
        with self.assertRaises(budgeting.BudgetError):
            self.report({'plans':{}},'1900-01','2200-01')
        line={'category':'SAMPLE ALPHA','kind':'percent','value':'25'}
        with self.assertRaises(budgeting.BudgetError):
            budgeting.validate_plan({'lines':[line,line]},{'SAMPLE ALPHA'})

    def test_percent_and_dollar_allowances_continue_until_next_saved_change(self):
        plan=budgeting.validate_plan({'lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'12.50'},{'category':'SAMPLE BETA','kind':'amount','value':'11.75'}]}, {'SAMPLE ALPHA','SAMPLE BETA'})
        state={'plans':{'2001-12':plan,'2002-02':fixed('18.93')}}
        for target,income in [('2001-12',18317),('2002-01',20131)]:
            transaction(self.connection,'sample-income-'+target,target,-income,'Income','sample-bank')
        result=self.report(state)
        self.assertEqual([row['allowance'] for row in result['history']],[3465,3691,1893])
        self.assertEqual(len(state['plans']),2)

    def test_dashboard_heatmap_scope_recent_details_and_missing_months(self):
        state={'plans':{'2001-12':fixed()}}
        transaction(self.connection,'sample-dec','2001-12',413)
        transaction(self.connection,'sample-jan','2002-01',1331)
        for index in range(7):
            transaction(self.connection,f'sample-feb-{index}','2002-02',231+index)
        transaction(self.connection,'sample-bank','2002-02',811,account='sample-bank')
        transaction(self.connection,'sample-foreign','2002-02',913,currency='EUR')
        result=budgeting.dashboard(self.connection,state,'2001-12','2002-02',today=date(2002,3,1))
        self.assertEqual([cell['level'] for cell in result['totals'][0]['heat']],['low','over','missing'])
        self.assertTrue(result['spending']['omitted'])
        self.assertEqual(len(result['details']['SAMPLE ALPHA']),5)
        self.assertNotIn(913,[row['amount'] for row in result['details']['SAMPLE ALPHA']])
        scoped=budgeting.dashboard(self.connection,state,'2001-12','2002-02',account_id='sample-bank',today=date(2002,3,1))
        self.assertEqual(scoped['current']['actual'],811)
        self.assertFalse(scoped['current']['plan'])
        self.assertEqual(len(scoped['details']['SAMPLE ALPHA']),1)
        self.assertEqual(scoped['totals'][0]['allowance'],0)
        self.assertFalse(scoped['heat_budget'])
        self.assertEqual(scoped['totals'][0]['heat'][-1]['level'],'intensity-4')

    def test_full_history_charts_ignore_budget_dates_amounts_and_keep_refunds(self):
        transaction(self.connection,'sample-old','1985-04',733)
        transaction(self.connection,'sample-recent','2002-02',617)
        transaction(self.connection,'sample-credit','2002-02',-131)
        transaction(self.connection,'sample-nonusd','2002-02',857,currency='EUR')
        transaction(self.connection,'sample-excluded','2002-02',911,excluded=1)
        first=budgeting.dashboard(self.connection,{'plans':{}},'2002-02','2002-02',today=date(2002,3,1))
        second=budgeting.dashboard(self.connection,{'plans':{'2001-12':fixed('97.31')}},'2001-12','2002-02',today=date(2002,3,1))
        self.assertEqual(first['spending'],second['spending'])
        series=first['spending']['categories'][0]
        self.assertEqual(series['total'],1219)
        self.assertEqual(series['values'][0],733)
        self.assertEqual(series['values'][-1],486)
        self.assertIsNone(series['values'][1])

    def test_calendar_quarters_sum_monthly_changes_and_match_drilldown_dates(self):
        for target,amount in [('2001-12',211),('2002-01',317),('2002-02',419),('2002-03',523),('2002-04',631)]:
            transaction(self.connection,'sample-'+target,target,amount)
        state={'plans':{'2002-01':fixed(),'2002-02':fixed('18.93')}}
        result=budgeting.dashboard(self.connection,state,'2002-01','2002-03',today=date(2002,5,1),granularity='quarter',snapshot_first='2002-01')
        self.assertEqual(result['current']['actual'],1259)
        self.assertEqual(result['current']['allowance'],4961)
        points=result['spending']['points']
        self.assertEqual([point['key'] for point in points],['2001-Q4','2002-Q1','2002-Q2'])
        self.assertEqual((points[1]['date_from'],points[1]['date_to']),('2002-01-01','2002-03-31'))
        self.assertEqual(points[1]['actual'],1259)
        self.assertEqual(result['heat_months'],['2002 Q1'])
        self.assertEqual(budgeting.quarter_bounds('2004-Q1'),('2004-01','2004-03'))
        self.assertEqual(budgeting.end_date('2004-02'),'2004-02-29')
        with self.assertRaises(budgeting.BudgetError):
            budgeting.quarter_bounds('2002-Q5')

    def test_rename_keeps_historical_allowances_without_changing_transactions(self):
        state={'plans':{'2001-12':fixed()}}
        budgeting.save(self.connection,state)
        budgeting.renamed_categories(self.connection,{'sample alpha':'SAMPLE RENAMED'})
        self.assertEqual(budgeting.load(self.connection)['plans']['2001-12']['lines'][0]['category'],'SAMPLE RENAMED')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],0)


class BudgetRouteTests(unittest.TestCase):
    setUp=test_app_setup.AppSetupTests.setUp
    tearDown=test_app_setup.AppSetupTests.tearDown
    csrf_token=staticmethod(test_app_setup.AppSetupTests.csrf_token)

    def ready(self):
        page=self.client.get('/setup')
        self.client.post('/setup',data={'csrf_token':self.csrf_token(page),'password':'fictional budget password','confirmation':'fictional budget password'})
        with self.application.db() as connection:
            seed(connection)
            transaction(connection,'sample-income','2002-02',-18317,'Income','sample-bank')
            transaction(connection,'sample-spending','2002-02',341)
        page=self.client.get('/budget?month=2002-02')
        self.assertEqual(page.status_code,200)
        self.token=self.csrf_token(page)
        with self.application.db() as connection:
            state=budgeting.load(connection)
            self.payload={'month':'2002-02','since':'2002-02','version':budgeting.version(connection,state),'income_mode':'recorded','lines':[{'category':'SAMPLE ALPHA','kind':'amount','value':'11.75'}]}
        return page

    def post(self,payload=None):
        return self.client.post('/api/budget/plan',json=payload or self.payload,headers={'X-CSRF-Token':self.token})

    def test_save_is_encrypted_and_roundtrip_preserves_original_transactions(self):
        self.ready()
        with self.application.db() as connection:
            before=[tuple(row) for row in connection.execute('SELECT * FROM transactions')]
        response=self.post()
        self.assertEqual(response.status_code,200)
        page=self.client.get(response.json['url'])
        self.assertEqual(page.status_code,200)
        self.assertIn(b'Cumulative category usage',page.data)
        self.assertNotIn(b'Payroll deductions',page.data)
        self.assertIn(b'date_to=2002-02-28',page.data)
        with self.application.db() as connection:
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM transactions')],before)
            self.assertEqual(budgeting.load(connection)['start'],'2002-02')
        self.assertNotIn(b'SAMPLE ALPHA',self.application.VAULT_PATH.read_bytes())
        self.application.lock_data()
        self.application.unlock_data('fictional budget password')
        with self.application.db() as connection:
            self.assertEqual(budgeting.load(connection)['plans']['2002-02']['lines'][0]['value'],1175)

    def test_csrf_stale_category_change_and_invalid_payload_are_rejected(self):
        self.ready()
        self.assertEqual(self.client.post('/api/budget/plan',json=self.payload).status_code,400)
        self.assertEqual(self.post({**self.payload,'since':'1900-01'}).status_code,400)
        for malformed in ({'income_mode':[]}, {'lines':[{'category':{},'kind':'amount','value':'1'}]}, {'lines':[{'category':'SAMPLE ALPHA','kind':[],'value':'1'}]}):
            self.assertEqual(self.post({**self.payload,**malformed}).status_code,400)
        invalid={**self.payload,'lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'101'}]}
        self.assertEqual(self.post(invalid).status_code,400)
        with self.application.db() as connection:
            self.assertFalse(budgeting.load(connection)['plans'])
            connection.execute("UPDATE category_rules SET name='SAMPLE RENAMED' WHERE name='SAMPLE ALPHA'")
        self.assertEqual(self.post().status_code,409)
        self.assertEqual(self.client.get('/budget?month=2002-02&since=2002-03').status_code,400)

    def test_no_reset_after_january_and_concurrent_save_is_not_lost(self):
        self.ready()
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.post().status_code,409)
        page=self.client.get('/budget?month=2003-01')
        self.assertIn(b'2002-02 through 2003-01',page.data)

    def test_consolidated_routes_defaults_and_scoped_views(self):
        self.ready()
        for route in ('/budget','/categories','/trends'):
            page=self.client.get(route+'?month=2002-02&since=2002-01')
            self.assertEqual(page.status_code,200)
            self.assertIn(b'<dialog id="budget-editor"',page.data)
            self.assertIn(b'<option value="percent" selected>',page.data)
            self.assertIn(b'id="category-patterns"',page.data)
            self.assertIn(b'id="spending-details"',page.data)
            self.assertNotIn(b'> Trends</a>',page.data)
        self.assertEqual(self.post().status_code,200)
        page=self.client.get('/categories?month=2002-02&account=sample-card')
        self.assertIn(b'household allowances hidden',page.data)
        self.assertIn(b'account=sample-card',page.data)
        self.assertNotIn(b'id="open-budget-editor"',page.data)
        self.assertIn(b'Earnings trends',self.client.get('/trends?view=earnings').data)

    def test_rolling_default_and_explicit_quarter_and_monthly_views(self):
        self.ready()
        default=self.client.get('/budget')
        self.assertIn(b'<option value="rolling" selected>',default.data)
        quarter=self.client.get('/budget?quarter=2002-Q1')
        self.assertEqual(quarter.status_code,200)
        self.assertIn(b'2002 Q1',quarter.data)
        self.assertIn(b'date_from=2002-01-01',quarter.data)
        self.assertIn(b'date_to=2002-03-31',quarter.data)
        self.assertIn(b'id="category-trend-select"',quarter.data)
        self.assertNotIn(b'budget-spark',quarter.data)
        self.assertIn(b'<option value="month" selected>',self.client.get('/budget?month=2002-02').data)
        self.assertEqual(self.client.get('/budget?quarter=2002-Q5').status_code,400)
        response=self.post({**self.payload,'granularity':'quarter'})
        self.assertEqual(response.status_code,200)
        self.assertIn('quarter=2002-Q1',response.json['url'])

    def test_backdated_replacement_reviews_all_intervals_and_recalculates_quarter(self):
        self.ready()
        with self.application.db() as connection:
            state={'start':'2002-03','plans':{'2001-12':fixed('13.17'),'2002-03':fixed('27.19'),'2002-05':fixed('31.23')}}
            budgeting.save(connection,state)
            revision=budgeting.version(connection,state)
        payload={**self.payload,'month':'2002-01','since':'2002-01','version':revision,'granularity':'quarter'}
        page=self.client.get('/budget?quarter=2002-Q1&since=2002-01')
        self.assertIn(b'Saved plan timeline',page.data)
        with self.application.db() as connection:
            self.assertEqual(budgeting.load(connection),state)
        preview=self.post({**payload,'preview':True})
        self.assertEqual(preview.status_code,200)
        review=preview.json['review']
        self.assertTrue(review['conflict'])
        self.assertEqual([row['first'] for row in review['intervals']],['2002-01','2002-03','2002-05'])
        self.assertEqual(review['intervals'][0]['last'],'2002-02')
        self.assertIsNone(review['intervals'][-1]['last'])
        self.assertEqual(self.post(payload).status_code,409)
        with self.application.db() as connection:
            self.assertEqual(budgeting.load(connection),state)
        confirmed={**payload,'confirmation':review['confirmation']}
        self.assertEqual(self.post(confirmed).status_code,200)
        with self.application.db() as connection:
            saved=budgeting.load(connection)
            self.assertEqual(set(saved['plans']),{'2001-12','2002-01'})
            self.assertEqual(saved['plans']['2001-12'],state['plans']['2001-12'])
            self.assertEqual(budgeting.plan_for(saved,'2002-08')[1]['lines'][0]['value'],1175)
            report=budgeting.dashboard(connection,saved,'2002-01','2002-03',today=date(2002,4,1),granularity='quarter',snapshot_first='2002-01')
            self.assertEqual(report['current']['allowance'],3525)
            self.assertEqual(report['totals'][0]['allowance'],3525)

    def test_confirmation_is_bound_to_draft_and_current_version(self):
        self.ready()
        self.post()
        with self.application.db() as connection:
            revision=budgeting.version(connection,budgeting.load(connection))
        payload={**self.payload,'version':revision,'lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'13.27'}]}
        review=self.post({**payload,'preview':True}).json['review']
        changed={**payload,'lines':[],'confirmation':review['confirmation']}
        self.assertEqual(self.post(changed).status_code,409)
        with self.application.db() as connection:
            state=budgeting.load(connection)
            state['plans']['2002-04']=fixed('19.31')
            budgeting.save(connection,state)
        self.assertEqual(self.post({**payload,'confirmation':review['confirmation']}).status_code,409)

    def test_backfill_before_first_plan_continues_percentages_and_preserves_actuals(self):
        self.ready()
        with self.application.db() as connection:
            plan=budgeting.validate_plan({'lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'13.27'}]}, {'SAMPLE ALPHA'})
            state={'plans':{'2002-03':plan}}
            budgeting.save(connection,state)
            before=[tuple(row) for row in connection.execute('SELECT * FROM transactions')]
            payload={**self.payload,'month':'2002-01','since':'2002-01','version':budgeting.version(connection,state),'lines':[{'category':'SAMPLE ALPHA','kind':'percent','value':'13.27'}]}
        review=self.post({**payload,'preview':True}).json['review']
        self.assertIsNone(review['intervals'][0]['source'])
        self.assertEqual(review['intervals'][1]['changes'],[])
        self.assertEqual(self.post({**payload,'confirmation':review['confirmation']}).status_code,200)
        with self.application.db() as connection:
            saved=budgeting.load(connection)
            self.assertEqual(set(saved['plans']),{'2002-01'})
            self.assertEqual(budgeting.plan_for(saved,'2002-02')[1],plan)
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM transactions')],before)

    def test_calendar_year_summary_and_twelve_month_heatmap(self):
        self.ready()
        with self.application.db() as connection:
            state={'plans':{'2002-01':fixed('11.75'),'2002-03':fixed('19.31')}}
            budgeting.save(connection,state)
            result=budgeting.dashboard(connection,state,'2002-02','2002-12',today=date(2003,1,1),granularity='year',snapshot_first='2002-01')
            self.assertEqual(result['current']['allowance'],21660)
            self.assertEqual(len(result['heat_months']),12)
            self.assertEqual(result['heat_months'][0],'2002 01')
            self.assertEqual(result['heat_months'][-1],'2002 12')
            self.assertEqual(result['totals'][0]['heat'][1]['date_to'],'2002-02-28')
            self.assertEqual(result['spending']['points'][0]['key'],'2002-02')
            revision=budgeting.version(connection,state)
        page=self.client.get('/budget?year=2002').data
        self.assertIn(b'2002-01 through 2002-12',page)
        self.assertIn(b'Annual allowance',page)
        self.assertIn(b'Monthly budget use',page)
        self.assertIn(b'Click a cell for transactions',page)
        self.assertIn(b'>Jan</span>',page)
        self.assertIn(b'>Dec</span>',page)
        self.assertEqual(self.client.get('/budget?year=bad').status_code,400)
        payload={**self.payload,'version':revision,'granularity':'year'}
        review=self.post({**payload,'preview':True}).json['review']
        response=self.post({**payload,'confirmation':review['confirmation']})
        self.assertIn('year=2002',response.json['url'])

    def test_rolling_window_crosses_year_boundary_and_keeps_monthly_cells(self):
        self.ready()
        with self.application.db() as connection:
            state={'plans':{'2001-01':fixed('11.75')}}
            budgeting.save(connection,state)
            result=budgeting.dashboard(connection,state,'2001-03','2002-02',today=date(2002,3,1),granularity='rolling',snapshot_first='2001-03')
            self.assertEqual(result['current']['allowance'],14100)
            self.assertEqual(result['heat_months'][0],'2001 03')
            self.assertEqual(result['heat_months'][-1],'2002 02')
            self.assertEqual(len(result['heat_months']),12)
            self.assertEqual(result['totals'][0]['allowance'],14100)
        page=self.client.get('/budget?granularity=rolling&month=2002-02')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'2001-03 through 2002-02',page.data)
        self.assertIn(b'12-month allowance',page.data)
        self.assertIn(b'Ending month',page.data)
        self.assertIn(b'Rolling 12-month accumulation',page.data)

    def test_mirror_readable_but_saving_is_blocked(self):
        self.ready()
        with patch.object(self.application,'READ_ONLY_MIRROR',True):
            before=self.application.VAULT_PATH.read_bytes()
            self.assertEqual(self.post().status_code,403)
            # Middleware blocks writes even with otherwise valid data and CSRF.
            self.assertEqual(self.application.VAULT_PATH.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
