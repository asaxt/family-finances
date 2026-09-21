"""Import-scoped classification with account-aware, one-to-one transfers."""
from datetime import date
from collections import defaultdict

from llm_evaluation import apply_categorized_suggestions


def account_rows(connection, transaction_ids=None):
    if transaction_ids == []:
        return []
    scope = ' AND t.id IN (' + ','.join('?' for _ in transaction_ids) + ')' if transaction_ids is not None else ''
    return [dict(row) for row in connection.execute("""
        SELECT t.*, a.cash_flow_role, a.spending_enabled,
               COALESCE(t.category_override, mr.category, t.category) AS effective_category,
               r.flow_type, mr.category AS rule_category
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        LEFT JOIN merchant_rules mr ON mr.id = (
          SELECT candidate.id FROM merchant_rules candidate
          WHERE (candidate.account_id = t.account_id
                 OR candidate.applies_all_accounts = 1) AND (
            (candidate.match_type = 'description' AND candidate.match_value = TRIM(t.description) COLLATE NOCASE)
            OR (candidate.match_type = 'description_contains'
                AND INSTR(LOWER(TRIM(t.description)), LOWER(candidate.match_value)) > 0)
          )
          ORDER BY CASE candidate.match_type WHEN 'description' THEN 0 ELSE 1 END,
                   LENGTH(candidate.match_value) DESC,
                   (candidate.account_id = t.account_id) DESC, candidate.id
          LIMIT 1
        )
        LEFT JOIN category_rules r ON r.name = COALESCE(t.category_override, mr.category, t.category) COLLATE NOCASE
        WHERE t.pending = 0 AND t.excluded = 0
    """ + scope, transaction_ids or [])]


def needs_category(row):
    return (row['effective_category'].casefold() == 'uncategorized'
            and not row['rule_category'] and row['category_override_source'] != 'user')


def match_import_transfers(connection, imported_ids):
    imported_ids = set(imported_ids)
    rows = [row for row in account_rows(connection)
            if row['amount'] and row['cash_flow_role'] == 'cash_flow' and row['spending_enabled']
            and (needs_category(row) or row['flow_type'] == 'transfer'
                 or (row['flow_type'] is None and row['effective_category'].casefold() == 'transfer'))]
    available = {row['id']: row for row in rows}
    by_amount = defaultdict(list)
    for row in rows:
        by_amount[(row['currency'], row['amount'])].append(row)
    pairs = []

    def opposite(left, right):
        return (left['account_id'] != right['account_id'] and left['currency'] == right['currency']
                and left['amount'] == -right['amount']
                and abs((date.fromisoformat(left['transacted_at']) - date.fromisoformat(right['transacted_at'])).days) <= 5)

    # Reserve already-classified pairs before considering newly imported rows.
    for known_only in (True, False):
        candidates = {key: [candidate['id'] for candidate in by_amount[(row['currency'], -row['amount'])]
                           if candidate['id'] in available and opposite(row, candidate)
                           and (not known_only or not needs_category(candidate))]
                      for key, row in available.items() if not known_only or not needs_category(row)}
        for left, matches in candidates.items():
            if len(matches) != 1:
                continue
            right = matches[0]
            if candidates.get(right) != [left] or left not in available or right not in available:
                continue
            pairs.append((available.pop(left), available.pop(right)))
    transfer_label = connection.execute(
        "SELECT name FROM category_rules WHERE flow_type = 'transfer' "
        "ORDER BY (name = 'Transfer' COLLATE NOCASE) DESC, name LIMIT 1"
    ).fetchone()
    if not transfer_label:
        return 0
    details = []
    for left, right in pairs:
        if left['id'] not in imported_ids and right['id'] not in imported_ids:
            continue
        for row in (left, right):
            if needs_category(row):
                details.append({'status': 'categorized', 'category': transfer_label[0], 'confidence': 1,
                    'reason': 'Equal and opposite transaction in another included account, in the same currency within five days. Check this transfer match.',
                    'transaction_ids': [row['id']]})
    return apply_categorized_suggestions(connection, {'details': details})


def eligible_import_ids(connection, imported_ids):
    wanted = set(imported_ids)
    return [row['id'] for row in account_rows(connection, list(wanted)) if needs_category(row)]
