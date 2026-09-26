"""Atomic, encrypted rule-change previews and explicit fallback relationships."""
import hashlib
import json

from category_matching import match_sql


class RuleConflict(Exception):
    pass


class PreviewRequired(Exception):
    def __init__(self, preview):
        self.preview = preview


def rules(connection):
    return {row['id']: dict(row) for row in connection.execute('SELECT * FROM merchant_rules ORDER BY id')}


def snapshot(connection):
    values = []
    for table in ('merchant_rules', 'rule_fallbacks', 'category_rules', 'transactions'):
        values.append([tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1')])
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def categories(connection):
    from analytics import EFFECTIVE_CATEGORY_SQL, CATEGORY_RULE_JOIN
    return {row['id']: dict(row) for row in connection.execute(f'''
        SELECT t.id, t.description, t.merchant, t.transacted_at, t.category_override_source,
               {EFFECTIVE_CATEGORY_SQL} AS category
        FROM transactions t {CATEGORY_RULE_JOIN}
    ''')}


def overlaps(left, right, connection):
    if not (left['applies_all_accounts'] or right['applies_all_accounts'] or left['account_id'] == right['account_id']):
        return False
    # Exact comparisons follow the database's ASCII-insensitive matching.
    lower = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
    a, b = left['match_value'].strip().translate(lower), right['match_value'].strip().translate(lower)
    same_field = (left['match_type'] == 'merchant') == (right['match_type'] == 'merchant')
    if same_field:
        if left['match_type'] != 'description_contains' and right['match_type'] != 'description_contains':
            return a == b
        if a in b or b in a:
            return True
    return bool(connection.execute(f'''
        SELECT 1 FROM transactions t JOIN merchant_rules l ON l.id = ? JOIN merchant_rules r ON r.id = ?
        WHERE {match_sql('l')} AND {match_sql('r')} LIMIT 1
    ''', (left['id'], right['id'])).fetchone())


def review_changes(connection, before, after):
    changed = [rule for key, rule in after.items() if before.get(key) != rule]
    # Do not keep stale fallback relationships when a rule's meaning changes.
    for rule in changed:
        connection.execute('DELETE FROM rule_fallbacks WHERE preferred_id = ? OR fallback_id = ?', (rule['id'], rule['id']))
    pairs = []
    changed_ids = {row['id'] for row in changed}
    for new in changed:
        for old in after.values():
            if old['id'] != new['id'] and old['id'] not in changed_ids and overlaps(new, old, connection):
                pairs.append((new['id'], old['id']))
    # Two conflicting proposals in one bulk edit cannot be ordered by accident.
    for i, left in enumerate(changed):
        for right in changed[i + 1:]:
            if left['category'].casefold() != right['category'].casefold() and overlaps(left, right, connection):
                raise RuleConflict('Bulk changes contain conflicting proposed categories. Edit these rules separately.')
    return changed, pairs


def resolve(connection, pairs, choice):
    if choice == 'replace':
        connection.executemany('DELETE FROM merchant_rules WHERE id = ?', [(old,) for old in sorted({old for _, old in pairs})])
    elif choice == 'fallback':
        connection.executemany('INSERT OR IGNORE INTO rule_fallbacks (preferred_id, fallback_id) VALUES (?, ?)', pairs)
        cycle = connection.execute('''WITH RECURSIVE paths(a,b) AS (
            SELECT preferred_id, fallback_id FROM rule_fallbacks
            UNION SELECT p.a, f.fallback_id FROM paths p JOIN rule_fallbacks f ON p.b = f.preferred_id
        ) SELECT 1 FROM paths WHERE a=b LIMIT 1''').fetchone()
        if cycle:
            raise RuleConflict('This fallback choice would create a circular rule relationship.')
    elif pairs:
        raise RuleConflict('Choose replacement or fallback for the overlapping rules.')


def impact(before, after):
    changed = [dict(id=key, description=row['description'], date=row['transacted_at'],
                    before=row['category'], after=after[key]['category'])
               for key, row in before.items() if key in after and row['category'] != after[key]['category']]
    return {'count': len(changed), 'examples': changed[:40]}
