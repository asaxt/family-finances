import unittest

import feedback
from tests import test_development_mirror


class FeedbackTests(unittest.TestCase):
    setUp = test_development_mirror.ProductionMirrorTests.setUp
    tearDown = test_development_mirror.ProductionMirrorTests.tearDown
    complete_setup = test_development_mirror.ProductionMirrorTests.complete_setup
    complete_category_setup = test_development_mirror.ProductionMirrorTests.complete_category_setup
    csrf_token = staticmethod(test_development_mirror.ProductionMirrorTests.csrf_token)
    enable_mirror = test_development_mirror.ProductionMirrorTests.enable_mirror

    def token(self):
        page = self.client.get('/feedback')
        self.assertEqual(page.status_code, 200)
        return self.csrf_token(page)

    def send(self, text, token):
        return self.client.post('/api/feedback', json={'text': text}, headers={'X-CSRF-Token': token})

    def test_feedback_stores_only_entered_text_and_server_identity_encrypted(self):
        self.complete_setup()
        token = self.token()
        text = '<script>SAMPLE FEEDBACK MESSAGE</script>\nA fictional suggestion.'
        response = self.client.post('/api/feedback?page=SAMPLE_NOT_ATTACHED', json={'text': text},
                                    headers={'X-CSRF-Token': token, 'X-Sample-Metadata': 'SAMPLE_NOT_ATTACHED'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['message'], 'Saved to this installation’s feedback inbox.')
        with self.application.private_workspace() as document:
            message = document['feedback'][0]
            self.assertEqual(set(message), {'id', 'text', 'created_at'})
            self.assertEqual(message['text'], text)
            self.assertEqual(len(message['id']), 32)
            self.assertTrue(message['created_at'].endswith('+00:00'))
        self.assertNotIn(b'SAMPLE FEEDBACK MESSAGE', self.application.VAULT_PATH.read_bytes())
        page = self.client.get('/feedback')
        self.assertIn(b'&lt;script&gt;SAMPLE FEEDBACK MESSAGE&lt;/script&gt;', page.data)
        self.assertNotIn(b'<script>SAMPLE FEEDBACK MESSAGE', page.data)
        self.assertNotIn(b'SAMPLE_NOT_ATTACHED', page.data)

    def test_invalid_or_full_inbox_submissions_do_not_change_saved_messages(self):
        self.complete_setup()
        token = self.token()
        for payload in ({'text': ''}, {'text': ' '}, {'text': 'x' * (feedback.MAX_TEXT + 1)},
                        {'text': ['SAMPLE MESSAGE']}, {'text': 'SAMPLE MESSAGE', 'page': 'SAMPLE PAGE'}, []):
            before = self.application.VAULT_PATH.read_bytes()
            response = self.client.post('/api/feedback', json=payload, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
        with self.application.private_workspace() as document:
            document['feedback'] = [{'id': f'{index:032x}', 'text': 'SAMPLE SAVED MESSAGE', 'created_at': '2001-01-01T00:00:00+00:00'}
                                    for index in range(feedback.MAX_MESSAGES)]
        before = self.application.VAULT_PATH.read_bytes()
        self.assertEqual(self.send('SAMPLE EXTRA MESSAGE', token).status_code, 409)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())

    def test_delete_requires_specific_message_and_csrf(self):
        self.complete_setup()
        token = self.token()
        self.send('SAMPLE MESSAGE TO REMOVE', token)
        self.send('SAMPLE MESSAGE TO KEEP', token)
        with self.application.private_workspace() as document:
            identifier = document['feedback'][0]['id']
        before = self.application.VAULT_PATH.read_bytes()
        self.assertEqual(self.client.post('/api/feedback/delete', json={'id': identifier}).status_code, 400)
        self.assertEqual(self.client.post('/api/feedback/delete', json={'id': 'f' * 32}, headers={'X-CSRF-Token': token}).status_code, 404)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
        response = self.client.post('/api/feedback/delete', data={'id': identifier, 'csrf_token': token})
        self.assertEqual(response.status_code, 200)
        page = self.client.get('/feedback')
        self.assertNotIn(b'SAMPLE MESSAGE TO REMOVE', page.data)
        self.assertIn(b'SAMPLE MESSAGE TO KEEP', page.data)

    def test_feedback_requires_an_unlocked_session(self):
        self.complete_setup()
        token = self.token()
        self.application.lock_data()
        self.assertEqual(self.send('SAMPLE LOCKED MESSAGE', token).status_code, 401)
        self.assertEqual(self.client.get('/feedback').status_code, 302)

    def test_development_feedback_persists_separately_from_readonly_snapshot(self):
        self.enable_mirror()
        token = self.token()
        before = self.application.VAULT_PATH.read_bytes()
        self.assertEqual(self.send('SAMPLE DEVELOPMENT MESSAGE', token).status_code, 201)
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
        companion = self.application.DATA_ROOT / '.development-workspace.vault'
        self.assertTrue(companion.exists())
        self.assertNotIn(b'SAMPLE DEVELOPMENT MESSAGE', companion.read_bytes())
        self.application.lock_data()
        self.application.unlock_data(self.password)
        with self.application.private_workspace() as document:
            self.assertEqual(document['feedback'][0]['text'], 'SAMPLE DEVELOPMENT MESSAGE')
        self.assertEqual(before, self.application.VAULT_PATH.read_bytes())
