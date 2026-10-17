"""Local operational records that never include request or exception content."""
import logging
from time import perf_counter

from flask import g, request


_REQUEST_EVENT = object()
_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}
_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_REQUEST_FIELDS = {'_privacy_event', '_method', '_endpoint', '_status', '_duration_ms'}


class _SafeLogFilter(logging.Filter):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def filter(self, record):
        allowed_request = getattr(record, '_privacy_event', None) is _REQUEST_EVENT
        if allowed_request:
            method = getattr(record, '_method', None)
            endpoint = getattr(record, '_endpoint', None)
            status = getattr(record, '_status', None)
            duration = getattr(record, '_duration_ms', None)
            record._method = method if method in _METHODS else 'OTHER'
            record._endpoint = endpoint if endpoint in self.app.view_functions else 'unmatched'
            record._status = status if type(status) is int and 100 <= status <= 599 else 0
            record._duration_ms = duration if type(duration) is int and 0 <= duration <= 86400000 else 0
            record.msg = (f'request_complete method={record._method} endpoint={record._endpoint} '
                          f'status={record._status} duration_ms={record._duration_ms}')
        else:
            record.msg = ('application_error' if record.levelno >= logging.ERROR else
                          'application_warning' if record.levelno >= logging.WARNING else 'application_event')
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        for key in set(record.__dict__) - _RECORD_FIELDS - (_REQUEST_FIELDS if allowed_request else set()):
            del record.__dict__[key]
        return True


def configure(app):
    """Install one local handler and privacy-safe request timing; leave errors intact."""
    if app.extensions.get('privacy_logging'):
        return
    app.extensions['privacy_logging'] = True
    logger = app.logger
    for previous in tuple(logger.filters):
        if isinstance(previous, _SafeLogFilter):
            logger.removeFilter(previous)
    safety_filter = _SafeLogFilter(app)
    logger.addFilter(safety_filter)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    handler.addFilter(safety_filter)
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Werkzeug's ordinary access log includes URL paths and query strings.
    logging.getLogger('werkzeug').disabled = True

    @app.before_request
    def start_private_request_record():
        g._privacy_started = perf_counter()

    @app.after_request
    def finish_private_request_record(response):
        elapsed = max(0, round((perf_counter() - getattr(g, '_privacy_started', perf_counter())) * 1000))
        logger.info('', extra={'_privacy_event': _REQUEST_EVENT, '_method': request.method,
                              '_endpoint': request.endpoint, '_status': response.status_code,
                              '_duration_ms': elapsed})
        return response
