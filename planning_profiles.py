"""Reusable people and editable Plan defaults, stored in the encrypted vault."""
from datetime import date
import json
import uuid

from projections import ProjectionError

SETTING = 'people_profiles_v1'
REFERENCES = 'household_plan_references_v1'
PROFILE_FIELDS = ('name', 'current_age', 'retirement_age', 'residence_state', 'employment_state', 'work_state_percent')
BALANCE_FIELDS = ('starting_pretax', 'starting_roth')
STATES = ('WA', 'OR')


def load(connection, key, default):
    row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def store(connection, key, value):
    connection.execute('INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, separators=(',', ':'))))


def normalized(value):
    return value.strip().casefold()


def named_owner(value):
    return normalized(value) not in ('', 'household', 'joint', 'shared', 'unknown')


def age(birth_date, today):
    if not birth_date:
        return ''
    born = date.fromisoformat(birth_date)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def save_person(connection, form, today=None):
    today = today or date.today()
    people = load(connection, SETTING, [])
    identifier = form.get('id', '')
    existing = next((person for person in people if person['id'] == identifier), None)
    if identifier and not existing:
        raise ProjectionError('This profile is no longer available. Reload People.')
    if (existing or {}).get('version', '') != form.get('version', ''):
        raise ProjectionError('This profile changed in another window. Reload People before saving.')
    person = {key: form.get(key, '').strip() for key in
              ('name', 'birth_date', 'residence_state', 'employment_state', 'savings_owner')}
    if not person['name'] or len(person['name']) > 60 or any(ord(c) < 32 for c in person['name']):
        raise ProjectionError('Enter a name of 1–60 characters.')
    if len(person['savings_owner']) > 80 or any(ord(c) < 32 for c in person['savings_owner']):
        raise ProjectionError('Choose a valid Savings owner.')
    try:
        if person['birth_date'] and not 0 <= age(person['birth_date'], today) <= 120:
            raise ValueError()
        for key, maximum in (('retirement_age', 94), ('work_state_percent', 100)):
            value = form.get(key, '').strip()
            person[key] = int(value) if value else ''
            if value and not (18 if key == 'retirement_age' else 0) <= person[key] <= maximum:
                raise ValueError()
    except (ValueError, TypeError):
        raise ProjectionError('Check the birth date, retirement age, and work-state percentage.')
    if any(person[key] not in ('', *STATES) for key in ('residence_state', 'employment_state')):
        raise ProjectionError('Choose a supported state or leave it blank.')
    for other in people:
        if other['id'] == identifier:
            continue
        if normalized(other['name']) == normalized(person['name']):
            raise ProjectionError('A profile already uses this name.')
        if person['savings_owner'] and normalized(other['savings_owner']) == normalized(person['savings_owner']):
            raise ProjectionError('That Savings owner is already linked to another profile.')
    person.update(id=identifier or uuid.uuid4().hex, version=uuid.uuid4().hex)
    store(connection, SETTING, [person if entry['id'] == identifier else entry for entry in people] if existing else [*people, person])
    return person


def balance(accounts):
    if not accounts:
        return {'value': '', 'note': 'No matching Savings accounts. Enter an amount.'}
    if any(not account['recorded_on'] for account in accounts):
        return {'value': '', 'note': 'Some matching accounts have no balance. Enter a total or update Savings.'}
    if any(account['amount'] < 0 for account in accounts):
        return {'value': '', 'note': 'A matching balance is negative. Review Savings or enter a total.'}
    return {'value': sum(account['amount'] for account in accounts) / 100,
            'note': 'Savings: ' + '; '.join(f"{account['name']} ({account['recorded_on']})" for account in accounts)}


def choices(connection, accounts, today=None):
    today = today or date.today()
    profiles = load(connection, SETTING, [])
    owners = {}
    for account in accounts:
        if named_owner(account['owner_name']):
            owners.setdefault(normalized(account['owner_name']), account['owner_name'].strip())
    for row in connection.execute('SELECT owner_name FROM connections ORDER BY id'):
        if named_owner(row[0]):
            owners.setdefault(normalized(row[0]), row[0].strip())
    result = []
    for person in profiles:
        values = {key: person.get(key, '') for key in PROFILE_FIELDS if key != 'current_age'}
        values['current_age'] = age(person['birth_date'], today)
        if values['current_age'] != '' and values['retirement_age'] != '':
            values['retirement_age'] = max(values['current_age'], values['retirement_age'])
        result.append({'id': person['id'], 'name': person['name'], 'saved': True,
                       'owner': person['savings_owner'], 'values': values})
        owners.pop(normalized(person['savings_owner']), None)
        owners.pop(normalized(person['name']), None)
    for key, name in sorted(owners.items()):
        result.append({'id': 'owner:' + key, 'name': name, 'saved': False, 'owner': name, 'values': {'name': name}})
    for item in result:
        owned = [account for account in accounts if named_owner(item['owner']) and normalized(account['owner_name']) == normalized(item['owner'])]
        item['balances'] = {field: balance([account for account in owned if account['classification'] == classification])
                            for field, classification in [('starting_pretax', 'pre_tax'), ('starting_roth', 'post_tax')]}
    return result


def validate_references(value, options):
    if not isinstance(value, dict) or set(value) != {'people', 'taxable_from_savings'} or type(value['taxable_from_savings']) is not bool:
        raise ProjectionError('Reload Plan to refresh its source selections.')
    if not isinstance(value['people'], list) or len(value['people']) != 2:
        raise ProjectionError('Choose sources for both people.')
    identifiers = {item['id'] for item in options}
    selected = []
    for item in value['people']:
        if (not isinstance(item, dict) or set(item) != {'choice', 'fields'} or not isinstance(item['choice'], str)
                or item['choice'] not in identifiers | {''} or not isinstance(item['fields'], list)
                or any(field not in (*PROFILE_FIELDS, *BALANCE_FIELDS) for field in item['fields'])):
            raise ProjectionError('Reload Plan to refresh its people and Savings sources.')
        if item['choice']:
            selected.append(item['choice'])
    if len(selected) != len(set(selected)):
        raise ProjectionError('Choose a different person for each spouse to avoid counting balances twice.')
    return value


def apply_defaults(plan, options, taxable, references=None, fresh=False):
    by_id = {item['id']: item for item in options}
    if references is None:
        references = {'people': [], 'taxable_from_savings': fresh and taxable['value'] != ''}
        for index, person in enumerate(plan['people']):
            option = next((item for item in options if normalized(item['name']) == normalized(person['name'])), None)
            if fresh:
                option = options[index] if len(options) <= 2 and index < len(options) else None
            references['people'].append({'choice': option['id'] if option else '',
                                         'fields': list(PROFILE_FIELDS + BALANCE_FIELDS) if fresh else []})
    for person, reference in zip(plan['people'], references['people']):
        option = by_id.get(reference['choice'])
        if option:
            values = {**option['values'], **{key: item['value'] for key, item in option['balances'].items()}}
            for field in reference['fields']:
                if field in values:
                    person[field] = values[field]
    if references['taxable_from_savings']:
        plan['starting_taxable'] = taxable['value']
    return references
