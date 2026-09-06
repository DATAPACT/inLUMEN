"""Durable execution receipts: duplicate submissions never replay side effects."""
import base64
import hashlib
import json
import time
from sqlalchemy import text


class ExecutionStore:
    def __init__(self, engine):
        self.engine = engine
        with engine.begin() as connection:
            connection.execute(text('''CREATE TABLE IF NOT EXISTS deployment_executions (
                execution_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, deadline DOUBLE PRECISION NOT NULL,
                result_json TEXT
            )'''))

    def start(self, execution_id: str, payload: dict, timeout: int) -> bool:
        safe = {key: value for key, value in payload.items() if key != 'runtime_secrets'}
        fingerprint = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
        with self.engine.begin() as connection:
            inserted = connection.execute(text('''INSERT INTO deployment_executions
                (execution_id, fingerprint, status, deadline) VALUES (:id,:fingerprint,'running',:deadline)
                ON CONFLICT(execution_id) DO NOTHING'''),
                {'id': execution_id, 'fingerprint': fingerprint, 'deadline': time.time() + timeout + 120})
            row = connection.execute(text('SELECT fingerprint FROM deployment_executions WHERE execution_id=:id'), {'id': execution_id}).first()
            if row[0] != fingerprint:
                raise ValueError('Execution ID already belongs to a different immutable bundle.')
            return inserted.rowcount == 1

    def finish(self, execution_id: str, result: dict, secrets: dict) -> dict:
        # Receipts are durable. Provider/connector credentials must not enter them.
        def redact(value):
            if isinstance(value, str):
                for secret in secrets.values():
                    if secret: value = value.replace(secret, '[REDACTED]')
                return value
            if isinstance(value, list): return [redact(item) for item in value]
            if isinstance(value, dict):
                cleaned = {key: redact(item) for key, item in value.items()}
                if isinstance(value.get('content'), str) and 'sha256' in value:
                    encoding = value.get('content_encoding', 'utf-8')
                    if encoding == 'base64':
                        content = base64.b64decode(value['content'], validate=True)
                        for secret in secrets.values():
                            if secret: content = content.replace(secret.encode(), b'[REDACTED]')
                        cleaned['content'] = base64.b64encode(content).decode('ascii')
                    else:
                        content = cleaned['content'].encode('utf-8')
                    cleaned['size_bytes'] = len(content)
                    cleaned['sha256'] = 'sha256:' + hashlib.sha256(content).hexdigest()
                return cleaned
            return value
        cleaned = redact(result)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE deployment_executions SET status='completed', result_json=:result WHERE execution_id=:id"),
                {'id': execution_id, 'result': json.dumps(cleaned)})
        return cleaned

    def get(self, execution_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(text('SELECT status,deadline,result_json FROM deployment_executions WHERE execution_id=:id'), {'id': execution_id}).first()
        if row is None: return None
        if row[0] == 'running' and row[1] < time.time():
            return {'status': 'interrupted', 'result': None}
        return {'status': row[0], 'result': json.loads(row[2]) if row[2] else None}
