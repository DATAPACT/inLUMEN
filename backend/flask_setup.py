"""Setup shared by the backend Flask apps: CORS headers and request diagnostics.

The diagnostics log no prompts, bodies or credentials.
"""
import json
import logging
import re
import time
import uuid
from flask import g, request

from runtime_config import add_cors_headers

LOGGER = logging.getLogger('inlumen.requests')
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())
LOGGER.propagate = False


def install_request_observability(app):
    app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

    @app.before_request
    def begin():
        supplied = request.headers.get('X-Request-ID', '')
        g.request_id = supplied if re.fullmatch(r'[A-Za-z0-9-]{1,64}', supplied) else uuid.uuid4().hex
        g.request_started = time.monotonic()

    @app.after_request
    def finish(response):
        response.headers['X-Request-ID'] = g.request_id
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        LOGGER.info(json.dumps({
            'event': 'http_request', 'request_id': g.request_id,
            'method': request.method,
            'route': str(request.url_rule) if request.url_rule else 'unmatched',
            'status': response.status_code,
            'duration_ms': round((time.monotonic() - g.request_started) * 1000, 1),
        }))
        return response


def install_cors(app):
    """Add the CORS headers to every response of the app.

    Flask runs after_request hooks in reverse order of registration, so the
    place of this call in a module decides which later hooks see the headers.
    """
    @app.after_request
    def apply_cors(response):
        return add_cors_headers(response, request.headers.get("Origin"))
