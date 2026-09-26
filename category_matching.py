"""One matching definition for reports, imports, review, and conflict previews."""

def trimmed_sql(value):
    return f"TRIM(COALESCE({value}, ''), CHAR(9)||CHAR(10)||CHAR(11)||CHAR(12)||CHAR(13)||' ')"


def match_sql(rule='candidate', transaction='t'):
    value = trimmed_sql(f'{rule}.match_value')
    merchant = trimmed_sql(f'{transaction}.merchant')
    description = trimmed_sql(f'{transaction}.description')
    return f"""({rule}.account_id = {transaction}.account_id OR {rule}.applies_all_accounts = 1)
        AND {value} != '' AND (
          ({rule}.match_type = 'merchant' AND {value} = COALESCE(NULLIF({merchant}, ''), {description}) COLLATE NOCASE)
          OR ({rule}.match_type = 'description' AND {value} = COALESCE(NULLIF({description}, ''), {merchant}) COLLATE NOCASE)
          OR ({rule}.match_type = 'description_contains' AND INSTR(LOWER(COALESCE(NULLIF({description}, ''), {merchant})), LOWER({value})) > 0)
        )"""


def candidates_sql(transaction='t'):
    return f"""WITH RECURSIVE ancestry(preferred_id, fallback_id) AS (
        SELECT preferred_id, fallback_id FROM rule_fallbacks
        UNION SELECT a.preferred_id, b.fallback_id FROM ancestry a
            JOIN rule_fallbacks b ON b.preferred_id = a.fallback_id
    ), matches AS (
        SELECT candidate.* FROM merchant_rules candidate WHERE {match_sql(transaction=transaction)}
    ), eligible AS (
        SELECT m.* FROM matches m WHERE (m.source = 'user' OR NOT EXISTS (SELECT 1 FROM matches u WHERE u.source = 'user'))
    ), winners AS (
        SELECT e.* FROM eligible e WHERE NOT EXISTS (
            SELECT 1 FROM ancestry a JOIN eligible p ON p.id = a.preferred_id WHERE a.fallback_id = e.id
        )
    )"""


def winning_rule_sql(transaction='t'):
    return f"({candidates_sql(transaction)} SELECT CASE WHEN COUNT(DISTINCT category COLLATE NOCASE) = 1 THEN MIN(id) END FROM winners)"


def conflict_sql(transaction='t'):
    return f"({candidates_sql(transaction)} SELECT COUNT(DISTINCT category COLLATE NOCASE) > 1 FROM winners)"


def complete_text(merchant, description, previous=None):
    previous = dict(previous or {})
    values, sources = {}, {}
    for field, value in (('merchant', merchant), ('description', description)):
        if value and value.strip():
            values[field], sources[field] = value, 'provided'
        elif previous.get(field) and previous.get(field + '_source', 'provided') == 'provided':
            values[field], sources[field] = previous[field], 'provided'
        else:
            values[field], sources[field] = '', 'missing'
    for field, other in (('merchant', 'description'), ('description', 'merchant')):
        if not values[field] and values[other]:
            values[field], sources[field] = values[other], 'backfilled'
    return {**values, **{field + '_source': value for field, value in sources.items()}}
