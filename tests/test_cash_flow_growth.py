import json
import re
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from analytics import cash_flow_growth, cash_flow_summary, month_range
from schema import create_schema
from tests import test_budgeting
from tests.test_budgeting import seed, transaction


def fictional_history(first='2001-01', last='2003-12'):
    rows=[]
    for target in month_range(first,last):
        for kind,amount in [('earned_income',-20000 if target>='2003-01' else -10000),
                            ('spending',6000 if target>='2003-01' else 8000)]:
            rows.append({'date':target+'-03','amount':amount,'currency':'USD','flow_type':kind})
    return rows


class CashFlowGrowthTests(unittest.TestCase):
    def test_default_rolling_year_comparison_at_twelve_completed_endpoints(self):
        result=cash_flow_growth(fictional_history(),today=date(2004,1,19))
        self.assertEqual((result['window'],result['comparison']),(12,12))
        self.assertEqual([row['month'] for row in result['points']],month_range('2003-01','2003-12'))
        first,last=result['points'][0],result['points'][-1]
        self.assertEqual((first['income'],first['spending']),(8.33,-2.08))
        self.assertEqual((last['income'],last['spending']),(100,-25))
        self.assertEqual((last['current']['first'],last['prior']['first']),('2003-01','2002-01'))
        self.assertEqual(last['current']['income'],240000)

    def test_independent_six_month_total_and_three_year_comparison(self):
        rows=fictional_history('1999-01','2003-12')
        result=cash_flow_growth(rows,6,36,date(2004,1,1))
        point=result['points'][-1]
        self.assertEqual((point['current']['first'],point['current']['last']),('2003-07','2003-12'))
        self.assertEqual((point['prior']['first'],point['prior']['last']),('2000-07','2000-12'))
        self.assertEqual((point['income'],point['current']['income'],point['prior']['income']),(100,120000,60000))
        overlapping=cash_flow_growth(rows,12,1,date(2004,1,1))['points'][-1]
        self.assertEqual(overlapping['income'],4.35)

    def test_missing_months_and_unusable_classification_leave_gaps(self):
        for issue in ('missing','unknown','currency'):
            rows=fictional_history()
            if issue=='missing':
                rows=[row for row in rows if row['date'][:7]!='2002-07']
            else:
                rows.append({'date':'2002-07-14','amount':377,'currency':'EUR' if issue=='currency' else 'USD',
                             'flow_type':None if issue=='unknown' else 'spending'})
            with self.subTest(issue=issue):
                last=cash_flow_growth(rows,today=date(2004,1,1))['points'][-1]
                self.assertIsNone(last['income'])
                self.assertIsNone(last['spending'])
                self.assertIsNone(last['prior']['income'])
                key={'missing':'missing','unknown':'unclassified','currency':'foreign'}[issue]
                self.assertEqual(last['prior'][key],1)

    def test_zero_negative_refunds_and_partial_month_handling(self):
        rows=fictional_history('2003-01','2003-12')
        for row in rows:
            if row['date'].startswith('2003-11') and row['flow_type']=='earned_income':
                row['amount']=0
        rows.extend([{'date':'2003-12-17','amount':-3000,'currency':'USD','flow_type':'spending'},
                     {'date':'2004-01-01','amount':-999999,'currency':'USD','flow_type':'earned_income'}])
        last=cash_flow_growth(rows,1,1,date(2004,1,19))['points'][-1]
        self.assertIsNone(last['income'])
        self.assertEqual(last['prior']['income'],0)
        self.assertEqual(last['spending'],-50)
        self.assertEqual(last['current']['income'],20000)
        rows.append({'date':'2003-11-18','amount':-9000,'currency':'USD','flow_type':'spending'})
        self.assertIsNone(cash_flow_growth(rows,1,1,date(2004,1,19))['points'][-1]['spending'])
        rows=[row for row in rows if not (row['date'].startswith('2003-12') and row['flow_type']=='earned_income')]
        self.assertEqual(cash_flow_growth(rows,1,2,date(2004,1,19))['points'][-1]['income'],-100)

    def test_validation_and_empty_history(self):
        self.assertFalse(cash_flow_growth([],today=date(2004,1,1))['available'])
        for window,comparison in [(0,12),(12,61),(1.5,12),('12',12),(True,12)]:
            with self.subTest(window=window,comparison=comparison),self.assertRaises(ValueError):
                cash_flow_growth([],window,comparison)

    def test_database_scope_treatments_and_exclusions(self):
        connection=sqlite3.connect(':memory:')
        connection.row_factory=sqlite3.Row
        self.addCleanup(connection.close)
        create_schema(connection)
        seed(connection)
        connection.execute("UPDATE accounts SET cash_flow_role='cash_flow' WHERE id='sample-card'")
        for target in ('2003-11','2003-12'):
            transaction(connection,'sample-income-'+target,target,-13000,'Income','sample-bank')
            transaction(connection,'sample-expense-'+target,target,3700)
            transaction(connection,'sample-transfer-'+target,target,777,'Transfer','sample-bank')
            transaction(connection,'sample-excluded-'+target,target,-33000,'Income','sample-bank',excluded=1)
            transaction(connection,'sample-pending-'+target,target,1337,pending=1)
            transaction(connection,'sample-ignored-'+target,target,-91000,'Income','sample-ignore')
        connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category,source) VALUES('sample-card','description','SAMPLE TRANSACTION','Transfer','user')")
        connection.execute("UPDATE transactions SET category_override='SAMPLE ALPHA',category_override_source='user' WHERE id LIKE 'sample-expense-%'")
        result=cash_flow_summary(connection,today=date(2004,1,1),growth_window=1,growth_comparison=1)['growth']['points'][-1]
        self.assertEqual((result['current']['income'],result['current']['spending']),(13000,3700))
        scoped=cash_flow_summary(connection,account_id='sample-card',today=date(2004,1,1),growth_window=1,growth_comparison=1)['growth']['points'][-1]
        self.assertEqual(scoped['current']['income'],0)
        self.assertEqual(scoped['spending'],0)
        empty=cash_flow_summary(connection,connection_id=999,today=date(2004,1,1),growth_window=1)['growth']
        self.assertFalse(empty['available'])


class CashFlowGrowthRouteTests(unittest.TestCase):
    setUp=test_budgeting.BudgetRouteTests.setUp
    tearDown=test_budgeting.BudgetRouteTests.tearDown
    ready=test_budgeting.BudgetRouteTests.ready
    csrf_token=staticmethod(test_budgeting.BudgetRouteTests.csrf_token)

    def chart(self,page):
        self.assertEqual(page.status_code,200)
        return json.loads(re.search(rb'<script id="cash-flow-growth-data" type="application/json">(.*?)</script>',page.data).group(1))

    def test_defaults_custom_controls_and_scope_survive_session_navigation(self):
        self.ready()
        result=self.chart(self.client.get('/cash-flow'))
        self.assertEqual((result['window'],result['comparison']),(12,12))
        page=self.client.get('/cash-flow?growth_window=6&growth_comparison=36&account=sample-bank&person=1')
        result=self.chart(page)
        self.assertEqual((result['window'],result['comparison']),(6,36))
        self.assertIn(b'name="account" value="sample-bank"',page.data)
        self.assertIn(b'name="person" value="1"',page.data)
        self.assertEqual(self.chart(self.client.get('/cash-flow'))['comparison'],36)
        for query in ('growth_window=0','growth_comparison=61','growth_window=bad','growth_window=1.5'):
            self.assertEqual(self.client.get('/cash-flow?'+query).status_code,400)

    def test_mirror_controls_are_read_only_and_locked_data_stays_protected(self):
        self.ready()
        self.application.MIRROR_METADATA.write_text('{}')
        with patch.object(self.application,'READ_ONLY_MIRROR',True):
            before=self.application.VAULT_PATH.read_bytes()
            self.chart(self.client.get('/cash-flow?growth_window=3&growth_comparison=24'))
            self.assertEqual(self.application.VAULT_PATH.read_bytes(),before)
        self.application.lock_data()
        self.assertEqual(self.client.get('/cash-flow').status_code,302)


if __name__=='__main__':
    unittest.main()
