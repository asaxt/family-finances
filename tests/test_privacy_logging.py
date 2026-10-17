import io
import logging
import unittest
import uuid
from unittest.mock import patch

from flask import Flask, request

import privacy_logging
from tests import test_development_mirror


class PrivacyLoggingTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask('privacy_test_' + uuid.uuid4().hex)
        self.previous_werkzeug_disabled = logging.getLogger('werkzeug').disabled
        self.addCleanup(setattr, logging.getLogger('werkzeug'), 'disabled', self.previous_werkzeug_disabled)
        privacy_logging.configure(self.app)
        self.output = io.StringIO()
        self.app.logger.handlers[0].setStream(self.output)
        self.client = self.app.test_client()

    def test_request_records_keep_basics_and_omit_personal_payloads(self):
        @self.app.post('/record/<entry>')
        def record(entry):
            return request.get_data(), 202

        response = self.client.post('/record/SAMPLE_PRIVATE_PATH?search=SAMPLE_PRIVATE_QUERY',
                                    data='SAMPLE_PRIVATE_BODY', headers={'X-Example': 'SAMPLE_PRIVATE_HEADER'})
        self.assertEqual(response.status_code, 202)
        output = self.output.getvalue()
        self.assertIn('request_complete method=POST endpoint=record status=202 duration_ms=', output)
        self.assertNotIn('SAMPLE_PRIVATE', output)
        self.assertNotIn('/record/', output)
        self.client.get('/SAMPLE_UNKNOWN_PATH?query=SAMPLE_PRIVATE_QUERY')
        self.assertIn('endpoint=unmatched status=404', self.output.getvalue())
        self.assertNotIn('SAMPLE_', self.output.getvalue())

    def test_raw_messages_arguments_and_exception_details_become_generic_codes(self):
        try:
            raise ValueError('SAMPLE_PRIVATE_EXCEPTION')
        except ValueError:
            self.app.logger.exception('Could not process %s', 'SAMPLE_PRIVATE_ARGUMENT',
                                      extra={'financial_context': 'SAMPLE_PRIVATE_EXTRA'})
        self.app.logger.warning('request_complete SAMPLE_PRIVATE_FAKE_EVENT',
                                extra={'_privacy_event': 'request', '_endpoint': 'SAMPLE_PRIVATE_ENDPOINT'})
        logging.getLogger(self.app.logger.name + '.child').error('SAMPLE_PRIVATE_CHILD_LOG')
        output = self.output.getvalue()
        self.assertIn('ERROR application_error', output)
        self.assertIn('WARNING application_warning', output)
        self.assertNotIn('SAMPLE_PRIVATE', output)
        self.assertNotIn('Traceback', output)
        self.assertNotIn('ValueError', output)

    def test_unhandled_error_is_still_500_without_path_or_exception_content(self):
        @self.app.get('/failure/<entry>')
        def fail(entry):
            raise RuntimeError('SAMPLE_PRIVATE_EXCEPTION ' + entry)

        response = self.client.get('/failure/SAMPLE_PRIVATE_PATH?amount=SAMPLE_PRIVATE_QUERY')
        self.assertEqual(response.status_code, 500)
        output = self.output.getvalue()
        self.assertIn('ERROR application_error', output)
        self.assertIn('endpoint=fail status=500', output)
        self.assertNotIn('SAMPLE_PRIVATE', output)
        self.assertNotIn('Traceback', output)

    def test_configure_does_not_swallow_propagating_errors_or_duplicate_hooks(self):
        privacy_logging.configure(self.app)
        self.assertEqual(len(self.app.before_request_funcs[None]), 1)
        self.assertEqual(len(self.app.after_request_funcs[None]), 1)
        self.assertTrue(logging.getLogger('werkzeug').disabled)
        self.app.config['TESTING'] = True

        @self.app.get('/failure')
        def fail():
            raise RuntimeError('SAMPLE_PRIVATE_EXCEPTION')

        with self.assertRaises(RuntimeError):
            self.client.get('/failure')


class LocalModelErrorPrivacyTests(unittest.TestCase):
    setUp = test_development_mirror.ProductionMirrorTests.setUp
    tearDown = test_development_mirror.ProductionMirrorTests.tearDown
    complete_setup = test_development_mirror.ProductionMirrorTests.complete_setup
    complete_category_setup = test_development_mirror.ProductionMirrorTests.complete_category_setup
    csrf_token = staticmethod(test_development_mirror.ProductionMirrorTests.csrf_token)

    def test_unexpected_classification_error_reveals_no_private_payload(self):
        self.complete_setup()
        token = self.csrf_token(self.client.get('/settings'))
        output = io.StringIO()
        self.application.app.logger.handlers[0].setStream(output)
        with patch.object(self.application, 'local_ai_enabled', return_value=True), \
             patch.object(self.application, 'prepare_evaluation', side_effect=RuntimeError('SAMPLE PRIVATE MODEL PAYLOAD')):
            response = self.client.post('/api/local-ai/evaluation', json={}, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 500)
        self.assertIn('Local categorization could not finish', response.json['error'])
        self.assertNotIn(b'SAMPLE PRIVATE MODEL PAYLOAD', response.data)
        self.assertNotIn('SAMPLE PRIVATE MODEL PAYLOAD', output.getvalue())
        self.assertIn('application_error', output.getvalue())
