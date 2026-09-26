"""Rule decisions use explicit user choices across sync, reports, and AI review."""
import sqlite3
import unittest
from datetime import date
from types import SimpleNamespace

from analytics import transaction_list
from category_matching import complete_text
from llm_evaluation import apply_categorized_suggestions
import rule_review
import schema
from tests import test_app_setup
from tests.test_category_review import seed
from tests import test_category_review


class RuleMatchingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        schema.create_schema(self.connection)
        seed(self.connection)
        self.connection.execute("UPDATE transactions SET excluded=0, category_override=NULL, category_override_source=NULL")

    def tearDown(self):
        self.connection.close()

    def rule(self, kind, text, category='EXAMPLE SPECIFIC', source='user'):
        return self.connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category,source) VALUES ('sample-account',?,?,?,?)", (kind,text,category,source)).lastrowid

    def row(self):
        return next(row for row in transaction_list(self.connection) if row['id']=='sample-1')

    def test_exact_merchant_and_description_match_full_text(self):
        for kind in ('merchant', 'description'):
            with self.subTest(kind=kind):
                self.connection.execute('DELETE FROM merchant_rules')
                self.rule(kind, 'sample cafe')
                self.assertEqual(self.row()['effective_category'], 'EXAMPLE SPECIFIC')
                self.connection.execute("UPDATE transactions SET merchant='SAMPLE CAFE EXTRA', description='SAMPLE CAFE EXTRA'")
                self.assertEqual(self.row()['effective_category'], 'EXAMPLE BROAD')
                self.connection.execute("UPDATE transactions SET merchant='SAMPLE CAFE', description='SAMPLE CAFE'")

    def test_no_implicit_exact_over_contains_and_fallback_restores_on_delete(self):
        exact=self.rule('description','SAMPLE CAFE','EXAMPLE BROAD')
        broad=self.rule('description_contains','SAMPLE','EXAMPLE SPECIFIC')
        self.assertEqual(self.row()['rule_conflict'],1)
        self.assertIsNone(self.row()['merchant_rule_id'])
        self.assertEqual(self.row()['effective_category'],'EXAMPLE BROAD')
        rule_review.resolve(self.connection,[(broad,exact)],'fallback')
        self.assertEqual(self.row()['rule_conflict'],0)
        self.assertEqual(self.row()['effective_category'],'EXAMPLE SPECIFIC')
        self.connection.execute('DELETE FROM merchant_rules WHERE id=?',(broad,))
        self.assertEqual(self.row()['effective_category'],'EXAMPLE BROAD')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM rule_fallbacks').fetchone()[0],0)

    def test_user_rule_supersedes_ai_and_individual_choice_supersedes_rule(self):
        self.rule('description', 'SAMPLE CAFE', 'EXAMPLE BROAD', 'model')
        self.connection.execute("UPDATE transactions SET category_override='EXAMPLE BROAD', category_override_source='model'")
        self.rule('merchant','SAMPLE CAFE')
        self.assertEqual(self.row()['effective_category'],'EXAMPLE SPECIFIC')
        self.connection.execute("UPDATE transactions SET category_override_source='user'")
        self.assertEqual(self.row()['effective_category'],'EXAMPLE BROAD')
        apply_categorized_suggestions(self.connection, {'details':[{'status':'categorized','category':'EXAMPLE SPECIFIC','transaction_ids':['sample-1'],'allow_recategorization':True}]})
        self.assertEqual(self.row()['category_override_source'],'user')
        self.assertEqual(self.row()['effective_category'],'EXAMPLE BROAD')

    def test_cycle_rejected_and_cross_field_future_conflict_detected(self):
        a=self.rule('description_contains','CAFE')
        b=self.rule('merchant','EXAMPLE FUTURE','EXAMPLE BROAD')
        self.assertFalse(rule_review.overlaps(rule_review.rules(self.connection)[a],rule_review.rules(self.connection)[b],self.connection))
        self.connection.execute("UPDATE transactions SET merchant='EXAMPLE FUTURE'")
        self.assertEqual(self.row()['rule_conflict'],1)
        rule_review.resolve(self.connection,[(a,b)],'fallback')
        with self.assertRaises(rule_review.RuleConflict):
            rule_review.resolve(self.connection,[(b,a)],'fallback')

    def test_text_completion_and_genuine_sync_replacements(self):
        first=complete_text('SAMPLE MERCHANT',' \t')
        self.assertEqual(first['description'],'SAMPLE MERCHANT')
        self.assertEqual(first['description_source'],'backfilled')
        second=complete_text(None,'SAMPLE DETAILED DESCRIPTION',first)
        self.assertEqual(second['merchant'],'SAMPLE MERCHANT')
        self.assertEqual(second['description_source'],'provided')
        self.assertEqual(complete_text(None,None,second),second)
        self.assertEqual(complete_text(None,'SAMPLE DETAIL')['merchant_source'],'backfilled')
        self.assertEqual(complete_text(' ',None)['merchant_source'],'missing')
        self.connection.execute("UPDATE transactions SET merchant=? WHERE id='sample-1'", (" \t\r\n",))
        self.assertEqual(self.row()['merchant'],'SAMPLE CAFE')
        self.assertEqual(self.row()['merchant_source'],'backfilled')

    def test_migration_preserves_manual_choices_and_old_ambiguous_category(self):
        from tests.test_schema import SchemaTests
        SchemaTests.remove_version_sixteen(self.connection)
        self.connection.execute("UPDATE transactions SET merchant=NULL,category_override='EXAMPLE SPECIFIC',category_override_source='user' WHERE id='sample-2'")
        self.connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category) VALUES ('sample-account','description','SAMPLE CAFE','EXAMPLE BROAD')")
        self.connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category) VALUES ('sample-account','merchant','SAMPLE CAFE','EXAMPLE SPECIFIC')")
        self.connection.commit()
        self.assertTrue(schema.migrate_schema(self.connection))
        self.assertEqual(self.row()['effective_category'],'EXAMPLE BROAD')
        self.assertEqual(self.row()['rule_conflict'],1)
        manual=self.connection.execute("SELECT * FROM transactions WHERE id='sample-2'").fetchone()
        self.assertEqual(manual['category_override_source'],'user')
        self.assertEqual(manual['merchant'],'SAMPLE CAFE')
        self.assertEqual(manual['merchant_source'],'backfilled')
        self.assertFalse(schema.migrate_schema(self.connection))


class RulePreviewTests(unittest.TestCase):
    setUp=test_app_setup.AppSetupTests.setUp
    tearDown=test_app_setup.AppSetupTests.tearDown
    csrf_token=staticmethod(test_app_setup.AppSetupTests.csrf_token)

    def ready(self):
        page=self.client.get('/setup')
        self.client.post('/setup',data={'csrf_token':self.csrf_token(page),'password':'fictional preview password','confirmation':'fictional preview password'})
        with self.application.db() as connection:
            seed(connection)
            connection.execute("UPDATE transactions SET excluded=0,category_override=NULL,category_override_source=NULL")
            connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category) VALUES ('sample-account','description','SAMPLE CAFE','EXAMPLE BROAD')")
        self.token=self.csrf_token(self.client.get('/transactions?purpose=all'))

    def propose(self):
        response=self.client.post('/api/transaction/sample-1',data={'csrf_token':self.token,'category_choice':'EXAMPLE SPECIFIC','remember_match':'on','match_value':'SAMPLE','rule_match_type':'description_contains','apply_all_accounts':'on'})
        self.assertEqual(response.status_code,303)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0],1)
        page=self.client.get(response.location)
        self.assertIn(b'Overlapping rules found',page.data)
        self.assertIn(b'Keep old rules as fallback',page.data)
        self.assertNotIn(b'SAMPLE CAFE',self.application.VAULT_PATH.read_bytes())
        return response.location

    def test_preview_cancel_fallback_and_replay(self):
        self.ready()
        location=self.propose()
        self.client.post(location,data={'csrf_token':self.token,'choice':'cancel'})
        self.assertEqual(self.client.get(location).status_code,410)
        location=self.propose()
        self.assertEqual(self.client.post(location,data={'csrf_token':self.token,'choice':'fallback'}).status_code,302)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0],2)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM rule_fallbacks').fetchone()[0],1)
            row=next(row for row in transaction_list(connection) if row['id']=='sample-1')
            self.assertEqual(row['effective_category'],'EXAMPLE SPECIFIC')
            self.assertIsNone(row['category_override_source'])
        self.assertEqual(self.client.post(location,data={'csrf_token':self.token,'choice':'replace'}).status_code,410)

    def test_inclusion_edit_does_not_promote_or_delete_ai_rule(self):
        self.ready()
        with self.application.db() as connection:
            connection.execute("UPDATE merchant_rules SET source='model'")
        response=self.client.post('/api/transaction/sample-1',data={
            'csrf_token':self.token,'category_choice':'EXAMPLE BROAD','excluded':'on'})
        self.assertEqual(response.status_code,302)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('SELECT source FROM merchant_rules').fetchone()[0],'model')
            row=connection.execute("SELECT * FROM transactions WHERE id='sample-1'").fetchone()
            self.assertEqual(row['excluded'],1)
            self.assertIsNone(row['category_override_source'])

    def test_individual_edit_leaves_saved_rules_unchanged(self):
        self.ready()
        with self.application.db() as connection:
            before=rule_review.rules(connection)
        response=self.client.post('/api/transaction/sample-1',data={
            'csrf_token':self.token,'category_choice':'EXAMPLE SPECIFIC','individual_only':'on'})
        self.assertEqual(response.status_code,302)
        with self.application.db() as connection:
            self.assertEqual(rule_review.rules(connection),before)
            row=next(row for row in transaction_list(connection) if row['id']=='sample-1')
            self.assertEqual(row['effective_category'],'EXAMPLE SPECIFIC')
            self.assertEqual(row['category_override_source'],'user')

    def test_stale_and_missing_csrf_preview_cannot_apply(self):
        self.ready()
        location=self.propose()
        self.assertEqual(self.client.post(location,data={'choice':'replace'}).status_code,400)
        with self.application.db() as connection:
            connection.execute("UPDATE transactions SET description='SAMPLE CHANGED' WHERE id='sample-2'")
        self.assertEqual(self.client.post(location,data={'csrf_token':self.token,'choice':'replace'}).status_code,409)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0],1)

    def test_prefer_existing_merchant_and_sync_honor_manual_choice(self):
        self.ready()
        with self.application.db() as connection:
            connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category) VALUES ('sample-account','merchant','SAMPLE CAFE','EXAMPLE SPECIFIC')")
            rule_id=connection.execute('SELECT MAX(id) FROM merchant_rules').fetchone()[0]
        response=self.client.post(f'/api/category-rule/{rule_id}/resolve',data={'csrf_token':self.token})
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.client.post(response.location,data={'csrf_token':self.token,'choice':'replace'}).status_code,302)
        with self.application.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0],1)
            self.application.save_transaction(connection,SimpleNamespace(transaction_id='sample-new',account_id='sample-account',amount=1.23,iso_currency_code='USD',merchant_name='SAMPLE CAFE',name=None,pending=False,date=date(2001,1,3)))
            row=next(row for row in transaction_list(connection) if row['id']=='sample-new')
            self.assertEqual(row['effective_category'],'EXAMPLE SPECIFIC')
            self.assertEqual(row['description_source'],'backfilled')
            connection.execute("UPDATE transactions SET category_override='EXAMPLE BROAD',category_override_source='user' WHERE id='sample-new'")
            self.application.save_transaction(connection,SimpleNamespace(transaction_id='sample-new',account_id='sample-account',amount=1.23,iso_currency_code='USD',merchant_name='SAMPLE CAFE',name='SAMPLE DETAIL',pending=False,date=date(2001,1,3)))
            row=next(row for row in transaction_list(connection) if row['id']=='sample-new')
            self.assertEqual(row['effective_category'],'EXAMPLE BROAD')
            self.assertEqual(row['description'],'SAMPLE DETAIL')
            self.assertEqual(row['description_source'],'provided')
        self.assertIn(b'Category rule text:',self.client.get('/transactions?purpose=all').data)


class ReplaceableAIReviewTests(unittest.TestCase):
    setUp = test_category_review.CategoryReviewTests.setUp
    tearDown = test_category_review.CategoryReviewTests.tearDown
    scan = test_category_review.CategoryReviewTests.scan
    def test_user_rule_blocks_acceptance_and_later_rule_supersedes_accepted_ai(self):
        result=self.scan()
        import category_review
        self.assertEqual(category_review.decide(self.connection,result,['sample-2'],'accept')['accepted'],1)
        self.connection.execute("INSERT INTO merchant_rules(account_id,match_type,match_value,category) VALUES ('sample-account','description','SAMPLE CAFE','EXAMPLE BROAD')")
        rows={row['id']:row for row in transaction_list(self.connection,include_excluded=True)}
        self.assertEqual(rows['sample-2']['effective_category'],'EXAMPLE BROAD')
        result=self.scan()
        self.assertEqual(category_review.decide(self.connection,result,['sample-2'],'accept')['needs_rule_change'],1)
