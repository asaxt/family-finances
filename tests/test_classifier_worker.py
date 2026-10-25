import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import classifier_runtime as runtime
import classifier_worker as worker
import classifier_worker_client as client


FAKE_CHILD = r'''
import json,sys,time
from pathlib import Path
mode=Path(sys.argv[sys.argv.index('--model')+1]).name
seen=set()
for number,line in enumerate(sys.stdin.buffer):
    packet=json.loads(line)
    nonce=packet['nonce']
    if set(packet)!={'nonce','texts','definitions'} or nonce in seen:
        raise RuntimeError('EXAMPLE invalid private request')
    seen.add(nonce)
    if mode=='hang':time.sleep(30)
    if mode=='oversize':
        sys.stdout.buffer.write(b'x'*131073);sys.stdout.buffer.flush();time.sleep(30)
    if mode=='stderr':
        sys.stderr.write('EXAMPLE PRIVATE DIAGNOSTIC'*100000);sys.stderr.flush()
    if mode=='privacy':
        assert 'PRIVATE_ACCOUNT_ID' not in line.decode()
        assert 'PRIVATE_TRANSACTION_ID' not in line.decode()
        assert 'PRIVATE_EVALUATION_ID' not in line.decode()
        assert 'Direction: money_in' in packet['texts'][0]
    if mode=='known_once' and number==0:
        print(json.dumps({'nonce':nonce,'error':'input_too_long'}),flush=True);continue
    keys=[*packet['definitions'],'']
    scores=[dict.fromkeys(keys,1/len(keys)) for _ in packet['texts']]
    result={'nonce':nonce,'scores':scores}
    if mode=='nonce':result['nonce']='f'*32
    if mode=='extra':result['debug']='EXAMPLE PRIVATE RESPONSE'
    if mode=='count':result['scores']=[]
    if mode=='keys':result['scores'][0]['EXAMPLE EXTRA']=0
    if mode=='nan':result['scores'][0][keys[0]]=float('nan')
    if mode=='bool':result['scores'][0][keys[0]]=True
    if mode=='range':result['scores'][0][keys[0]]=2
    if mode=='sum':result['scores'][0]=dict.fromkeys(keys,0)
    if mode=='error':result={'nonce':nonce,'error':'EXAMPLE PRIVATE ERROR'}
    if mode=='invalid_json':print('EXAMPLE PRIVATE RESPONSE',flush=True)
    elif mode=='duplicate':print('{"nonce":"'+nonce+'","nonce":"'+nonce+'","scores":[]}',flush=True)
    else:print(json.dumps(result),flush=True)
'''


class ClassifierWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / 'fake_worker.py'
        self.script.write_text(FAKE_CHILD)
        self.patch = patch.object(client, 'WORKER_SCRIPT', self.script)
        self.patch.start()
        self.definitions = {'EXAMPLE CATEGORY': 'A fictional purchase purpose.'}
        self.rows = [{'description': 'SAMPLE PURCHASE', 'merchant': 'SAMPLE CAFE',
                      'amount': -123, 'account_type': 'depository', 'account_subtype': 'checking',
                      'all_have_transfer_match': False, 'account_id': 'PRIVATE_ACCOUNT_ID',
                      'transaction_ids': ['PRIVATE_TRANSACTION_ID'],
                      'evaluation_id': 'PRIVATE_EVALUATION_ID'}]

    def tearDown(self):
        self.patch.stop()
        self.temporary.cleanup()

    def make(self, mode='valid', timeout=2):
        model = self.root / mode
        model.mkdir(exist_ok=True)
        return client.ClassifierWorker(model, device='cpu', python_path=sys.executable, timeout=timeout)

    def test_only_derived_text_and_categories_cross_the_pipe(self):
        with self.make('privacy') as classifier:
            result = classifier.scores(self.rows, self.definitions)
        self.assertEqual(result, [{'EXAMPLE CATEGORY': 0.5, '': 0.5}])
        self.assertTrue(classifier._closed)

    def test_worker_is_reused_for_new_definitions_and_fresh_nonces(self):
        with self.make() as classifier:
            classifier.scores(self.rows, self.definitions)
            process = classifier._process
            result = classifier.scores(self.rows, {'EXAMPLE SECOND': 'Another fictional purpose.'})
            self.assertIs(classifier._process, process)
            self.assertEqual(set(result[0]), {'EXAMPLE SECOND', ''})
        self.assertIsNotNone(process.returncode)

    def test_more_than_32_rows_are_chunked_and_all_results_return(self):
        with self.make() as classifier:
            self.assertEqual(len(classifier.scores(self.rows * 65, self.definitions)), 65)

    def test_stderr_is_discarded_and_child_environment_does_not_inherit_secrets(self):
        real_popen = subprocess.Popen
        with patch.dict(os.environ, {'EXAMPLE_API_SECRET': 'EXAMPLE PRIVATE VALUE'}):
            with patch.object(client.subprocess, 'Popen', wraps=real_popen) as factory:
                with self.make('stderr') as classifier:
                    classifier.scores(self.rows, self.definitions)
        self.assertIs(factory.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertFalse(factory.call_args.kwargs['shell'])
        self.assertNotIn('EXAMPLE_API_SECRET', factory.call_args.kwargs['env'])
        self.assertIsInstance(factory.call_args.args[0], list)
        self.assertIn('-I', factory.call_args.args[0])

    def test_protocol_errors_are_generic_and_kill_and_reap_child(self):
        real_popen = subprocess.Popen
        for mode in ('nonce', 'extra', 'count', 'keys', 'nan', 'bool', 'range', 'sum',
                     'error', 'invalid_json', 'duplicate', 'oversize'):
            with self.subTest(mode=mode):
                classifier = self.make(mode)
                processes = []
                def spawn(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process
                with patch.object(client.subprocess, 'Popen', side_effect=spawn):
                    with self.assertRaises(client.ClassifierWorkerError) as caught:
                        classifier.scores(self.rows, self.definitions)
                self.assertNotIn('EXAMPLE PRIVATE', str(caught.exception))
                self.assertTrue(classifier._closed)
                self.assertIsNone(classifier._process)
                self.assertIsNotNone(processes[0].returncode)
                self.assertTrue(processes[0].stdin.closed)
                self.assertTrue(processes[0].stdout.closed)
                classifier.close()

    def test_timeout_kills_and_reaps_child(self):
        classifier = self.make('hang', timeout=0.15)
        real_popen = subprocess.Popen
        processes = []
        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(client.subprocess, 'Popen', side_effect=spawn) as factory:
            with self.assertRaises(client.ClassifierWorkerError):
                classifier.scores(self.rows, self.definitions)
        self.assertTrue(classifier._closed)
        self.assertIsNone(classifier._process)
        self.assertEqual(factory.call_count, 1)
        self.assertIsNotNone(processes[0].returncode)

    def test_known_input_length_error_is_distinct_and_worker_can_continue(self):
        with self.make('known_once') as classifier:
            with self.assertRaises(client.ClassifierInputTooLongError) as caught:
                classifier.scores(self.rows, self.definitions)
            self.assertEqual(caught.exception.code, 'input_too_long')
            process = classifier._process
            self.assertIsNone(process.poll())
            self.assertEqual(len(classifier.scores(self.rows, self.definitions)), 1)
            self.assertIs(classifier._process, process)

    def test_invalid_categories_or_oversized_input_never_start_a_child(self):
        classifier = self.make()
        invalid = ({}, {'': 'EXAMPLE'}, {'EXAMPLE': 123}, {'A': '', 'a': ''},
                   {f'EXAMPLE {i}': '' for i in range(129)})
        for definitions in invalid:
            with self.subTest(size=len(definitions)), self.assertRaises(client.ClassifierWorkerError):
                classifier.scores(self.rows, definitions)
        with self.assertRaises(client.ClassifierWorkerError):
            classifier.scores([{**self.rows[0], 'description': 'SAMPLE' * 50000}], self.definitions)
        self.assertIsNone(classifier._process)
        self.assertEqual(classifier.scores([], self.definitions), [])
        classifier.close()

    def test_close_is_idempotent_and_prevents_reuse(self):
        classifier = self.make()
        classifier.scores(self.rows, self.definitions)
        process = classifier._process
        classifier.close()
        classifier.close()
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(client.ClassifierWorkerError):
            classifier.scores(self.rows, self.definitions)


class PureWorkerTests(unittest.TestCase):
    def packet(self, nonce='a', definitions=None, texts=None):
        return (json.dumps({'nonce': nonce * 32, 'texts': texts or ['SAMPLE PURCHASE'],
                            'definitions': definitions or {'EXAMPLE CATEGORY': 'Fictional purpose.'}}) + '\n').encode()

    def test_worker_loads_model_once_and_updates_definitions(self):
        instances = []

        class FakeClassifier:
            def __init__(self, path, definitions, device):
                self.definitions = definitions
                instances.append(self)

            def set_definitions(self, definitions):
                self.definitions = definitions

            def scores_texts(self, texts):
                keys = [*self.definitions, '']
                return [dict.fromkeys(keys, 1 / len(keys)) for _ in texts]

        source = io.BytesIO(self.packet() + self.packet('b', {'EXAMPLE SECOND': 'Fictional purpose.'}))
        output = io.BytesIO()
        self.assertEqual(worker.serve(source, output, 'EXAMPLE MODEL', 'cpu', FakeClassifier), 0)
        self.assertEqual(len(instances), 1)
        packets = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(set(packets[1]['scores'][0]), {'EXAMPLE SECOND', ''})

    def test_worker_rejects_malformed_and_oversized_requests_without_echo(self):
        marker = b'EXAMPLE PRIVATE CONTENT'
        for raw in (marker + b'\n', b'[]\n', self.packet()[:-1],
                    b'x' * (runtime.MAX_REQUEST_BYTES + 1),
                    self.packet(texts=['SAMPLE'] * 33),
                    self.packet().replace(b'"texts":', b'"debug": "EXAMPLE PRIVATE", "texts":')):
            with self.subTest(length=len(raw)):
                output = io.BytesIO()
                factory = unittest.mock.Mock()
                self.assertEqual(worker.serve(io.BytesIO(raw), output, 'EXAMPLE', 'cpu', factory), 1)
                factory.assert_not_called()
                self.assertNotIn(marker, output.getvalue())
                self.assertEqual(json.loads(output.getvalue())['error'], 'invalid_request')

    def test_runtime_exposes_known_length_error_before_model_forward(self):
        classifier = object.__new__(runtime.GLiClassClassifier)
        classifier.torch = SimpleNamespace(inference_mode=contextlib.nullcontext)
        classifier.pipe = SimpleNamespace(prepare_inputs=lambda *args, **kwargs: {
            'input_ids': SimpleNamespace(shape=(1, 2049))})
        classifier.labels = ['EXAMPLE CATEGORY', runtime.REVIEW_LABEL]
        with self.assertRaises(runtime.ClassifierInputTooLongError):
            classifier.scores_texts(['SAMPLE PURCHASE'])

    def test_direction_derivation_and_imports_do_not_need_application_libraries(self):
        self.assertIn('Direction: money_in', runtime.input_text({'description': 'SAMPLE', 'amount': -123}))
        self.assertIn('Direction: money_out', runtime.input_text({'description': 'SAMPLE', 'amount': 123}))
        code = ("import sys; sys.path.insert(0, " + repr(str(Path(runtime.__file__).parent)) + "); "
                "import classifier_worker, classifier_worker_client; "
                "assert not {'app','llm_evaluation','analytics','vault','torch','flask'} & set(sys.modules)")
        subprocess.run([sys.executable, '-I', '-c', code], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
