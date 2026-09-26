"""Local, suggestion-only review of existing transaction categories."""
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from analytics import CATEGORY_RULE_JOIN, EFFECTIVE_CATEGORY_SQL
from category_matching import conflict_sql
from llm_evaluation import (
    classify_batch, dismiss_ai_reviews, existing_category_examples,
    normalized_description, transfer_matches, validate_prediction,
)

SETTING = 'local_category_review_v1'
MODEL = 'qwen3.8:27b'
BATCH_SIZE = 5


def load(connection):
    row = connection.execute('SELECT value FROM settings WHERE key = ?', (SETTING,)).fetchone()
    return json.loads(row[0]) if row else None


def save(connection, result):
    connection.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (SETTING, json.dumps(result, separators=(',', ':'))),
    )


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def categories(connection):
    return [dict(row) for row in connection.execute(
        "SELECT name, flow_type FROM category_rules "
        "WHERE LOWER(name) != 'uncategorized' ORDER BY name COLLATE NOCASE"
    )]


def source_rows(connection):
    return [dict(row) for row in connection.execute(f'''
        SELECT t.id, t.account_id, t.transacted_at, t.amount, t.currency,
               t.description, t.merchant, t.pending, t.excluded, t.category,
               t.category_override, t.category_override_source,
               t.flow_override, t.spending_override,
               {EFFECTIVE_CATEGORY_SQL} AS current_category, {conflict_sql()} AS rule_conflict,
               a.name AS account_name, a.type AS account_type, a.subtype AS account_subtype,
               mr.id AS rule_id, mr.source AS rule_source, mr.category AS rule_category,
               mr.match_type AS rule_match_type, mr.match_value AS rule_match_value,
               mr.applies_all_accounts AS rule_all_accounts, r.flow_type AS category_treatment
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        {CATEGORY_RULE_JOIN}
        ORDER BY t.transacted_at, t.id
    ''')]


def prepare(connection):
    palette = categories(connection)
    if not palette:
        raise ValueError('Add category labels before starting a category review.')
    rows = source_rows(connection)
    posted = [row for row in rows if not row['pending']]
    # Match transfer evidence only within the same currency.
    matches = {}
    currencies = {row['currency'] for row in posted}
    for currency in currencies:
        matches.update(transfer_matches([row for row in posted if row['currency'] == currency]))
    grouped = defaultdict(list)
    for row in posted:
        if not row['current_category'] or row['current_category'].casefold() == 'uncategorized':
            continue
        key = (row['account_id'], row['amount'] < 0, row['currency'],
               normalized_description(row['description']), row['current_category'])
        grouped[key].append(row)
    groups = []
    for members in grouped.values():
        group = dict(members[0])
        group.update(
            evaluation_id=f'G{len(groups) + 1:04d}', members=members,
            occurrence_count=len(members), first_date=members[0]['transacted_at'],
            last_date=members[-1]['transacted_at'],
            all_have_transfer_match=all(row['id'] in matches for row in members),
        )
        groups.append(group)
    result = {
        'id': uuid.uuid4().hex, 'status': 'running', 'model': MODEL,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'palette_fingerprint': fingerprint(palette),
        'categories': [row['name'] for row in palette],
        'considered': len(rows), 'eligible': sum(len(group['members']) for group in groups),
        'processed': 0, 'unchanged': 0, 'uncertain': 0, 'suggestions': [],
        'pending_skipped': len(rows) - len(posted),
        'uncategorized_skipped': sum(not row['current_category'] or row['current_category'].casefold() == 'uncategorized' for row in posted),
    }
    return result, groups, existing_category_examples(connection, result['categories'])


def review_batch(result, groups, examples):
    predictions = classify_batch(
        result['model'], result['categories'], groups,
        category_examples=examples, review_existing=True,
    )
    by_id = {item['id']: item for item in predictions}
    suggestions = []
    unchanged = uncertain = processed = 0
    for group in groups:
        prediction = validate_prediction(by_id[group['evaluation_id']], group, result['categories'])
        count = len(group['members'])
        processed += count
        if not prediction['category']:
            uncertain += count
        elif prediction['category'].casefold() == group['current_category'].casefold():
            unchanged += count
        else:
            for row in group['members']:
                suggestions.append({
                    'transaction_id': row['id'], 'fingerprint': fingerprint(row),
                    'date': row['transacted_at'], 'description': row['description'],
                    'merchant': row['merchant'], 'account_name': row['account_name'],
                    'excluded': bool(row['excluded']), 'current_category': row['current_category'],
                    'proposed_category': prediction['category'],
                    'confidence': prediction['confidence'],
                    'reason': str(prediction.get('reason', ''))[:400], 'decision': 'pending',
                })
    # Add only fully validated batches, so interrupted runs retain accurate counts.
    result['suggestions'].extend(suggestions)
    result['processed'] += processed
    result['unchanged'] += unchanged
    result['uncertain'] += uncertain


def decide(connection, result, transaction_ids, action):
    current = {row['id']: row for row in source_rows(connection)}
    palette_unchanged = fingerprint(categories(connection)) == result['palette_fingerprint']
    counts = {'accepted': 0, 'kept': 0, 'stale': 0, 'needs_rule_change': 0}
    requested = set(transaction_ids)
    for item in result['suggestions']:
        if item['transaction_id'] not in requested or item['decision'] != 'pending':
            continue
        if action == 'keep':
            item['decision'] = 'kept'
        elif (not palette_unchanged or item['transaction_id'] not in current
              or fingerprint(current[item['transaction_id']]) != item['fingerprint']):
            item['decision'] = 'stale'
        elif (current[item['transaction_id']]['rule_conflict'] or
              (current[item['transaction_id']]['rule_source'] == 'user' and current[item['transaction_id']]['rule_category'] != item['proposed_category'])):
            item['decision'] = 'needs_rule_change'
        else:
            connection.execute(
                "UPDATE transactions SET category_override = ?, category_override_source = 'model' WHERE id = ?",
                (item['proposed_category'], item['transaction_id']),
            )
            dismiss_ai_reviews(connection, [item['transaction_id']])
            item['decision'] = 'accepted'
        counts[item['decision']] += 1
    result['last_decision'] = counts
    save(connection, result)
    return counts
