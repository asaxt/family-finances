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

    def test_mirror_readable_but_saving_is_blocked(self):
        self.ready()
        with patch.object(self.application,'READ_ONLY_MIRROR',True):
            before=self.application.VAULT_PATH.read_bytes()
            self.assertEqual(self.post().status_code,403)
            # Middleware blocks writes even with otherwise valid data and CSRF.
            self.assertEqual(self.application.VAULT_PATH.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
