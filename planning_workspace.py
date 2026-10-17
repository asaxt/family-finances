"""Small, versioned plan library. Storage is supplied by the encrypted app vault."""
from copy import deepcopy
from datetime import datetime, timezone
import secrets

from projections import ProjectionError, validate

SETTING = 'planning_workspace_v1'
MAX_PLANS = 30


def empty():
    return {'version': '', 'plans': []}


def find(state, identifier):
    item = next((item for item in state['plans'] if item['id'] == identifier), None)
    if item is None:
        raise ProjectionError('That saved plan is no longer available. Reload the plan library.')
    return item


def change(state, payload, source, references):
    if not isinstance(payload, dict) or payload.get('action') not in {'save', 'delete'}:
        raise ProjectionError('Choose a plan to save or delete.')
    if payload.get('version') != state['version']:
        raise ProjectionError('The plan library changed in another tab. Reload it before saving.')
    state = deepcopy(state)
    identifier = payload.get('id')
    existing = find(state, identifier) if identifier else None
    if payload['action'] == 'delete':
        if not existing:
            raise ProjectionError('Choose a saved plan to delete.')
        state['plans'].remove(existing)
    else:
        name = payload.get('name')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60 or any(ord(c) < 32 for c in name):
            raise ProjectionError('Give the plan a name of 1–60 characters.')
        if any(item['name'].casefold() == name.strip().casefold() and item['id'] != identifier for item in state['plans']):
            raise ProjectionError('A plan already has that name. Choose another name or update that plan.')
        plan = validate(payload.get('plan'))
        if not existing and len(state['plans']) >= MAX_PLANS:
            raise ProjectionError('The library holds up to 30 plans. Delete an unused plan first.')
        item = dict(id=identifier or secrets.token_hex(12), name=name.strip(), plan=plan,
                    references=deepcopy(references), source=deepcopy(source),
                    updated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        if existing:
            state['plans'][state['plans'].index(existing)] = item
        else:
            state['plans'].append(item)
        state['active_id'] = item['id']
    if state.get('active_id') == identifier and payload['action'] == 'delete':
        state.pop('active_id', None)
    state['version'] = secrets.token_hex(12)
    return state
