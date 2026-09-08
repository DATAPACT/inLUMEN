"""Request timing and correlation without bodies, credentials or query strings."""
import json
import logging
import re
import time
import uuid

LOGGER = logging.getLogger('inlumen.requests')
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())
LOGGER.propagate = False


class RequestDiagnosticsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        supplied = headers.get(b'x-request-id', b'').decode('ascii', errors='ignore')
        request_id = supplied if re.fullmatch(r'[A-Za-z0-9-]{1,64}', supplied) else uuid.uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        started, status = time.monotonic(), 500

        async def wrapped_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message['headers'] = [(key, value) for key, value in message.get('headers', [])
                                      if key.lower() not in {b'x-request-id', b'cache-control', b'x-content-type-options'}]
                message['headers'].extend([(b'x-request-id', request_id.encode()),
                                          (b'cache-control', b'no-store'),
                                          (b'x-content-type-options', b'nosniff')])
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            LOGGER.info(json.dumps({'event': 'http_request', 'request_id': request_id,
                'method': scope['method'], 'route': getattr(scope.get('route'), 'path', 'unmatched'),
                'status': status, 'duration_ms': round((time.monotonic() - started) * 1000, 1)}))
