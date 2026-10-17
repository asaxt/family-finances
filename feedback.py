"""User-submitted feedback in this installation's encrypted workspace."""
from datetime import datetime, timezone
import uuid

from flask import jsonify, render_template, request


MAX_MESSAGES = 200
MAX_TEXT = 5000


def _payload(field):
    if request.content_length and request.content_length > 65536:
        return None
    if request.is_json:
        value = request.get_json(silent=True)
    else:
        value = {key: item for key, item in request.form.items() if key != 'csrf_token'}
    if not isinstance(value, dict) or set(value) != {field} or not isinstance(value[field], str):
        return None
    return value[field].strip()


def install(app, workspace, page_context):
    @app.post('/api/feedback', endpoint='feedback_send')
    def send():
        text = _payload('text')
        if not text or len(text) > MAX_TEXT:
            return jsonify(error=f'Write a message of 1–{MAX_TEXT:,} characters.'), 400
        with workspace() as document:
            messages = document.setdefault('feedback', [])
            if len(messages) >= MAX_MESSAGES:
                return jsonify(error='The feedback inbox is full. Delete an older message before sending another.'), 409
            messages.append({'id': uuid.uuid4().hex, 'text': text,
                             'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
        return jsonify(saved=True, message='Saved to this installation’s feedback inbox.'), 201

    @app.get('/feedback', endpoint='feedback_inbox')
    def inbox():
        context = page_context('settings')
        with workspace() as document:
            context['feedback_messages'] = list(reversed(document.get('feedback', [])))
        return render_template('feedback.html', **context)

    @app.post('/api/feedback/delete', endpoint='feedback_delete')
    def delete():
        identifier = _payload('id')
        if not identifier or len(identifier) != 32:
            return jsonify(error='Choose a feedback message to delete.'), 400
        with workspace() as document:
            messages = document.get('feedback', [])
            remaining = [message for message in messages if message['id'] != identifier]
            if len(remaining) == len(messages):
                return jsonify(error='This message is no longer in the inbox.'), 404
            document['feedback'] = remaining
        return jsonify(deleted=True)
