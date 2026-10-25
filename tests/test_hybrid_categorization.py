import copy
import math
import unittest
from unittest.mock import Mock

from classifier_runtime import ClassifierInputTooLongError
from hybrid_categorization import HybridCategorizer, HybridConfigurationError


class HybridCategorizationTests(unittest.TestCase):
    def setUp(self):
        self.meals = 'EXAMPLE PREPARED FOOD'
        self.pantry = 'EXAMPLE HOME INGREDIENTS'
        self.move = 'EXAMPLE INTERNAL MOVE'
        self.other = 'oThEr'
        self.prepared = {
            'categories': [self.meals, self.pantry, self.move, self.other],
            'category_treatments': {
                self.meals: 'spending', self.pantry: 'spending',
                self.move: 'transfer', self.other: 'spending',
            },
            'category_examples': {self.meals: [{
                'merchant': 'SAMPLE CAFE', 'description': 'SAMPLE PREPARED MEAL REFERENCE',
                'direction': 'money_out',
            }]},
            'model': 'qwen3.8:27b', 'targeted': False,
        }
        self.rows = [{
            'evaluation_id': f'G{index:04d}', 'account_id': f'sample-account-{index}',
            'transaction_ids': [f'sample-transaction-{index}-a', f'sample-transaction-{index}-b'],
            'transacted_at': '2001-01-02', 'merchant': 'SAMPLE CAFE',
            'description': 'SAMPLE PREPARED FOOD PURCHASE', 'amount': 123,
            'currency': 'USD', 'account_type': 'depository', 'account_subtype': 'checking',
            'occurrence_count': 2, 'first_date': '2001-01-02', 'last_date': '2001-01-03',
            'all_have_transfer_match': False,
        } for index in (1, 2)]

    def score(self, *, prepared=None, top=None, probability=0.85):
        prepared = self.prepared if prepared is None else prepared
        keys = [name for name in prepared['categories'] if name.casefold() != 'other'] + ['']
        top = self.meals if top is None else top
        result = {key: (1 - probability) / (len(keys) - 1) for key in keys}
        result[top] = probability
        return result

    def prediction(self, row, category=None, confidence=1, **extra):
        return {
            'id': row['evaluation_id'], 'category': self.pantry if category is None else category,
            'confidence': confidence, 'reason': 'EXAMPLE supported purchase purpose', **extra,
        }

    def qwen(self):
        return Mock(side_effect=lambda model, categories, rows, **kwargs: [
            self.prediction(row) for row in rows
        ])

    def test_classifier_cutoff_and_margin_include_the_boundaries(self):
        cases = (
            ({self.meals: 0.6, self.pantry: 0.35, self.move: 0.03, '': 0.02}, True),
            ({self.meals: 0.5999, self.pantry: 0.2, self.move: 0.1001, '': 0.1}, False),
            ({self.meals: 0.625, self.pantry: 0.375, self.move: 0.0, '': 0.0}, True),
            ({self.meals: 0.624, self.pantry: 0.376, self.move: 0.0, '': 0.0}, False),
        )
        for scores, accepted in cases:
            with self.subTest(scores=scores):
                worker = Mock()
                worker.scores.return_value = [scores]
                qwen = self.qwen()
                details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(
                    self.prepared, self.rows[:1],
                )
                self.assertEqual(details[0]['category'], self.meals if accepted else self.pantry)
                self.assertEqual(details[0]['categorization_source'], 'classifier' if accepted else 'qwen')
                self.assertEqual(details[0]['confidence'], 1)
                self.assertEqual(qwen.call_count, 0 if accepted else 1)

    def test_review_option_routes_to_qwen_with_existing_canonical_other_and_examples(self):
        worker = Mock()
        worker.scores.return_value = [self.score(top='')]
        qwen = Mock(return_value=[self.prediction(self.rows[0], self.other)])
        details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows[:1])
        self.assertEqual(details[0]['category'], self.other)
        self.assertEqual(details[0]['status'], 'categorized')
        self.assertEqual(qwen.call_args.args[0], self.prepared['model'])
        self.assertEqual(qwen.call_args.args[1], self.prepared['categories'])
        self.assertEqual(qwen.call_args.kwargs['fallback_category'], self.other)
        self.assertEqual(qwen.call_args.kwargs['category_examples'], self.prepared['category_examples'])
        self.assertFalse(qwen.call_args.kwargs.get('review_existing', False))
        self.assertFalse(qwen.call_args.kwargs.get('require_evidence', False))

    def test_only_valid_model_abstentions_become_other(self):
        for category, confidence in (('', 0), ('', 1), ('', 2), (self.pantry, 0)):
            with self.subTest(category=category, confidence=confidence):
                worker = Mock()
                worker.scores.return_value = [self.score(top='')]
                qwen = Mock(return_value=[self.prediction(self.rows[0], category, confidence)])
                details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(
                    self.prepared, self.rows[:1],
                )
                self.assertEqual((details[0]['category'], details[0]['confidence'], details[0]['status']),
                                 (self.other, 1, 'categorized'))

    def test_invalid_worker_scores_raise_before_qwen_instead_of_becoming_other(self):
        valid = self.score()
        invalid = [None, [], [valid, valid], [None], {self.rows[0]['evaluation_id']: valid},
                   [{key: value for key, value in valid.items() if key != self.meals}],
                   [{**valid, 'EXAMPLE FOREIGN LABEL': 0.0}],
                   [{**valid, self.meals: 0.95}]]
        invalid.extend([{**valid, self.meals: value}]
                       for value in (True, '0.85', None, math.nan, math.inf, -0.1, 1.1))
        for scores in invalid:
            with self.subTest(scores=scores):
                worker = Mock()
                worker.scores.return_value = scores
                qwen = self.qwen()
                with self.assertRaises(ValueError):
                    HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows[:1])
                qwen.assert_not_called()

    def test_invalid_qwen_results_are_not_normalized_into_other(self):
        valid = self.prediction(self.rows[0])
        invalid = [None, [], [valid, valid], [None],
                   [{key: value for key, value in valid.items() if key != 'reason'}],
                   [{**valid, 'id': 'EXAMPLE FOREIGN GROUP'}],
                   [{**valid, 'category': 'EXAMPLE FOREIGN LABEL'}],
                   [{**valid, 'confidence': True}], [{**valid, 'confidence': 3}],
                   [{**valid, 'reason': 'X' * 401}]]
        invalid.extend([{**valid, field: value}] for field, value in (
            ('account_id', 'sample-foreign-account'),
            ('transaction_ids', ['sample-foreign-transaction']),
            ('allow_recategorization', True), ('suppress_ai_reviews', False),
            ('categorization_source', 'classifier'), ('status', 'categorized'),
        ))
        for predictions in invalid:
            with self.subTest(predictions=predictions):
                worker = Mock()
                worker.scores.return_value = [self.score(top='')]
                qwen = Mock(return_value=predictions)
                with self.assertRaises(ValueError):
                    HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows[:1])

    def test_all_transfer_treatment_labels_require_a_verified_pair_at_both_stages(self):
        for transfer_label in (self.move, 'Transfer in'):
            prepared = copy.deepcopy(self.prepared)
            prepared['categories'] = [self.meals, self.pantry, transfer_label, self.other]
            prepared['category_treatments'] = {
                self.meals: 'spending', self.pantry: 'spending', transfer_label: 'transfer', self.other: 'spending',
            }
            for matched in (False, True):
                with self.subTest(transfer_label=transfer_label, matched=matched):
                    rows = copy.deepcopy(self.rows[:1])
                    rows[0]['all_have_transfer_match'] = matched
                    worker = Mock()
                    worker.scores.return_value = [self.score(prepared=prepared, top=transfer_label)]
                    qwen = Mock(return_value=[self.prediction(rows[0], transfer_label, 2)])
                    details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(prepared, rows)
                    self.assertEqual(details[0]['category'], transfer_label if matched else self.other)
                    self.assertEqual(qwen.call_count, 0 if matched else 1)
                    self.assertNotIn('Transfer', prepared['categories'])

    def test_qwen_transfer_proposal_is_guarded_even_when_classifier_did_not_propose_transfer(self):
        worker = Mock()
        worker.scores.return_value = [self.score(top='')]
        qwen = Mock(return_value=[self.prediction(self.rows[0], self.move, 2)])
        details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows[:1])
        self.assertEqual(details[0]['category'], self.other)
        self.assertEqual(details[0]['confidence'], 1)

    def test_invalid_other_or_treatment_configuration_fails_before_either_model(self):
        configurations = []
        missing = copy.deepcopy(self.prepared)
        missing['categories'].remove(self.other)
        missing['category_treatments'].pop(self.other)
        configurations.append(missing)
        ambiguous = copy.deepcopy(self.prepared)
        ambiguous['categories'].append('Other')
        ambiguous['category_treatments']['Other'] = 'spending'
        configurations.append(ambiguous)
        transfer = copy.deepcopy(self.prepared)
        transfer['category_treatments'][self.other] = 'transfer'
        configurations.append(transfer)
        incomplete = copy.deepcopy(self.prepared)
        incomplete['category_treatments'].pop(self.meals)
        configurations.append(incomplete)
        for prepared in configurations:
            with self.subTest(prepared=prepared):
                worker, qwen = Mock(), self.qwen()
                with self.assertRaises(HybridConfigurationError):
                    HybridCategorizer(worker=worker, qwen_classify=qwen).classify(prepared, self.rows[:1])
                worker.scores.assert_not_called()
                qwen.assert_not_called()

    def test_known_classifier_context_limit_routes_to_qwen_but_other_worker_faults_propagate(self):
        worker = Mock()
        worker.scores.side_effect = ClassifierInputTooLongError()
        qwen = self.qwen()
        details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows)
        self.assertEqual([item['category'] for item in details], [self.pantry, self.pantry])
        self.assertEqual({row['evaluation_id'] for row in qwen.call_args.args[2]},
                         {row['evaluation_id'] for row in self.rows})
        for failure in (RuntimeError('EXAMPLE worker unavailable'), ValueError('EXAMPLE invalid worker protocol')):
            with self.subTest(failure=type(failure).__name__):
                worker = Mock()
                worker.scores.side_effect = failure
                qwen = self.qwen()
                with self.assertRaises(type(failure)) as caught:
                    HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows)
                self.assertIs(caught.exception, failure)
                qwen.assert_not_called()

    def test_qwen_failure_is_not_disguised_as_other(self):
        worker = Mock()
        worker.scores.return_value = [self.score(top='')]
        qwen = Mock(side_effect=RuntimeError('EXAMPLE local Qwen unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'EXAMPLE local Qwen unavailable'):
            HybridCategorizer(worker=worker, qwen_classify=qwen).classify(self.prepared, self.rows[:1])

    def test_results_bind_to_local_ids_and_scope_even_when_qwen_returns_reversed_rows(self):
        prepared = copy.deepcopy(self.prepared)
        prepared['targeted'] = True
        original = copy.deepcopy((prepared, self.rows))
        worker = Mock()
        worker.scores.return_value = [self.score(top=''), self.score(top='')]
        qwen = Mock(return_value=[
            self.prediction(self.rows[1], self.meals), self.prediction(self.rows[0], self.pantry),
        ])
        details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(prepared, self.rows)
        self.assertEqual([item['category'] for item in details], [self.pantry, self.meals])
        for row, item in zip(self.rows, details, strict=True):
            self.assertEqual(item['id'], row['evaluation_id'])
            self.assertEqual(item['account_id'], row['account_id'])
            self.assertEqual(item['transaction_ids'], row['transaction_ids'])
            self.assertIsNot(item['transaction_ids'], row['transaction_ids'])
            self.assertIs(item['allow_recategorization'], True)
            self.assertEqual(item['categorization_source'], 'qwen')
        self.assertEqual((prepared, self.rows), original)

    def test_dynamic_names_and_dict_examples_update_without_replacing_the_worker(self):
        worker = Mock()

        def scores(rows, definitions):
            top = next(iter(definitions))
            return [self.score(prepared={'categories': [*definitions, self.other]}, top=top) for _ in rows]

        worker.scores.side_effect = scores
        qwen = self.qwen()
        categorizer = HybridCategorizer(worker=worker, qwen_classify=qwen)
        first = categorizer.classify(self.prepared, self.rows[:1])
        changed = copy.deepcopy(self.prepared)
        renamed = 'EXAMPLE CUSTOM PURPOSE'
        changed['categories'][0] = renamed
        changed['category_treatments'][renamed] = changed['category_treatments'].pop(self.meals)
        changed['category_examples'] = {renamed: [{
            'description': 'SAMPLE NEW PURPOSE REFERENCE', 'merchant': 'SAMPLE SECOND SHOP',
            'direction': 'money_out',
        }]}
        second = categorizer.classify(changed, self.rows[:1])
        self.assertEqual(first[0]['category'], self.meals)
        self.assertEqual(second[0]['category'], renamed)
        self.assertEqual(worker.scores.call_count, 2)
        first_definitions = worker.scores.call_args_list[0].args[1]
        second_definitions = worker.scores.call_args_list[1].args[1]
        self.assertEqual(set(first_definitions), set(self.prepared['categories']) - {self.other})
        self.assertEqual(set(second_definitions), set(changed['categories']) - {self.other})
        self.assertIn('SAMPLE PREPARED MEAL REFERENCE', first_definitions[self.meals])
        self.assertIn('SAMPLE NEW PURPOSE REFERENCE', second_definitions[renamed])
        self.assertIs(categorizer.worker, worker)
        qwen.assert_not_called()

    def test_only_other_category_uses_qwen_without_starting_classifier(self):
        prepared = copy.deepcopy(self.prepared)
        prepared['categories'] = [self.other]
        prepared['category_treatments'] = {self.other: 'spending'}
        worker = Mock()
        qwen = Mock(return_value=[self.prediction(self.rows[0], self.other)])
        details = HybridCategorizer(worker=worker, qwen_classify=qwen).classify(prepared, self.rows[:1])
        self.assertEqual(details[0]['category'], self.other)
        worker.scores.assert_not_called()
        qwen.assert_called_once()

    def test_context_manager_closes_worker_on_success_and_failure(self):
        worker = Mock()
        worker.scores.return_value = [self.score()]
        with HybridCategorizer(worker=worker, qwen_classify=self.qwen()) as categorizer:
            categorizer.classify(self.prepared, self.rows[:1])
        worker.close.assert_called_once()
        failing_worker = Mock()
        failing_worker.scores.side_effect = RuntimeError('EXAMPLE worker failed')
        with self.assertRaises(RuntimeError):
            with HybridCategorizer(worker=failing_worker, qwen_classify=self.qwen()) as categorizer:
                categorizer.classify(self.prepared, self.rows[:1])
        failing_worker.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
