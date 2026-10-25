"""Local classifier routing; the application retains all save authority."""
import json
import os
import platform
from pathlib import Path

from classifier_routing import hybrid_predictions, preview_predictions
from classifier_runtime import MODEL_REPO, MODEL_REVISION, validate_scores
from classifier_worker_client import ClassifierInputTooLongError, ClassifierWorker
from llm_evaluation import PREDICTION_FIELDS, classify_batch, model_transactions, validated_predictions


class HybridConfigurationError(ValueError):
    pass


class HybridCategorizer:
    def __init__(self, worker=None, qwen_classify=None):
        self.worker = worker
        self.qwen_classify = qwen_classify or classify_batch

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.worker is not None:
            self.worker.close()

    def _worker(self):
        if self.worker is None:
            root = Path(__file__).parent
            try:
                manifest = json.loads((root / '.classifier-cache/manifest.json').read_text())
                entry = manifest[MODEL_REPO]
                if entry['revision'] != MODEL_REVISION:
                    raise ValueError()
                default_device = 'mps' if platform.system() == 'Darwin' and platform.machine() == 'arm64' else 'cpu'
                device = os.environ.get('FAMILY_FINANCES_CLASSIFIER_DEVICE', default_device)
                self.worker = ClassifierWorker(entry['path'], device=device)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                raise HybridConfigurationError(
                    'The local classifier runtime or cached model is unavailable.'
                ) from None
        return self.worker

    def classify(self, prepared, rows):
        categories = prepared['categories']
        treatments = prepared.get('category_treatments', {})
        others = [name for name in categories if name.casefold() == 'other']
        if len(others) != 1:
            raise HybridConfigurationError('Add an Other category before using mixed categorization.')
        other = others[0]
        if (set(treatments) != set(categories)
                or any(flow not in {'spending', 'earned_income', 'other_inflow', 'transfer'}
                       for flow in treatments.values()) or treatments[other] == 'transfer'):
            raise HybridConfigurationError('Check the category treatments before using mixed categorization.')
        if not rows:
            return []

        def guard(predictions, batch):
            checked = validated_predictions(predictions, batch, categories)
            result = []
            for row in batch:
                item = checked[row['evaluation_id']]
                if treatments.get(item['category']) == 'transfer' and not row.get('all_have_transfer_match'):
                    item = {**item, 'category': '', 'confidence': 0,
                            'reason': 'A transfer requires a verified household counterpart.'}
                result.append({key: item[key] for key in PREDICTION_FIELDS})
            return result

        candidates = [name for name in categories if name != other]
        flow_words = {'spending': 'Money out for this purpose, including refunds.',
                      'earned_income': 'Money in from earned income.',
                      'other_inflow': 'Other money in, not earned income.',
                      'transfer': 'Movement between household accounts with a verified counterpart.'}
        examples = prepared.get('category_examples', {})
        definitions = {}
        for name in candidates:
            definition = f'{name}. {flow_words[treatments[name]]}'
            # A short local example helps custom labels without expanding the token budget much.
            samples = [value.get('description', '') if isinstance(value, dict) else value
                       for value in examples.get(name, [])]
            sample = next((value for value in samples if isinstance(value, str) and value), '')
            if sample:
                definition += f' Example: {sample[:80]}'
            definitions[name] = definition
        try:
            scores = self._worker().scores(rows, definitions) if candidates else None
            if candidates:
                validate_scores(scores, definitions, len(rows))
            first = preview_predictions(rows, candidates, scores) if candidates else None
        except ClassifierInputTooLongError:
            # A known context limit is an explicit handoff; faults never become Other.
            first = None
        if first is None:
            first = [{'id': row['evaluation_id'], 'category': '', 'confidence': 0,
                      'reason': 'The classifier requires a Qwen handoff.'} for row in rows]
        rankings = {item['id']: {key: item[key] for key in ('top_category', 'score', 'margin') if key in item}
                    for item in first}
        first = [{**item, **rankings[item['id']]}
                 for item in guard([{key: item[key] for key in PREDICTION_FIELDS} for item in first], rows)]

        def qwen(_model, palette, batch, **kwargs):
            return guard(self.qwen_classify(prepared['model'], palette, batch, **kwargs), batch)

        mixed = hybrid_predictions(rows, categories, first, qwen,
                                   category_examples=examples, fallback_category=other)['predictions']
        checked = validated_predictions([{key: item[key] for key in PREDICTION_FIELDS} for item in mixed],
                                        rows, categories)
        details = []
        for row, item in zip(rows, mixed, strict=True):
            prediction = checked[row['evaluation_id']]
            details.append({**model_transactions([row])[0], **prediction,
                            'account_id': row['account_id'],
                            'transaction_ids': list(row['transaction_ids']),
                            'allow_recategorization': prepared['targeted'],
                            'categorization_source': item['source']})
        return details
