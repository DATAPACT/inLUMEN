"""Database-backed worker ownership and atomic admission (also used by codegen)."""
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import text


class WorkerLeases:
    def __init__(self, engine, kind: str, ttl: int = 60):
        self.engine, self.kind, self.ttl = engine, kind, ttl
        self.owner = uuid.uuid4().hex
        self.lock = threading.RLock()
        with engine.begin() as connection:
            connection.execute(text("""CREATE TABLE IF NOT EXISTS worker_leases (
                name TEXT PRIMARY KEY, kind TEXT NOT NULL, workspace_id TEXT NOT NULL,
                owner TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL
            )"""))

    @contextmanager
    def transaction(self):
        with self.lock, self.engine.begin() as connection:
            if self.engine.dialect.name == 'postgresql':
                connection.execute(text("SELECT pg_advisory_xact_lock(1749926001)"))
            else:
                connection.execute(text("BEGIN IMMEDIATE"))
            yield connection

    def claim(self, run_id: str, workspace_id: str, limit: int = 100000, global_limit: int = 100000) -> bool:
        now = time.time()
        name = f'{self.kind}:{workspace_id}:{run_id}'
        with self.transaction() as connection:
            existing = connection.execute(text('SELECT owner, expires_at FROM worker_leases WHERE name=:name'), {'name': name}).first()
            if existing and existing[1] > now:
                return existing[0] == self.owner
            active = connection.execute(text('SELECT workspace_id FROM worker_leases WHERE kind=:kind AND expires_at>:now'), {'kind': self.kind, 'now': now}).fetchall()
            if len(active) >= global_limit or sum(row[0] == workspace_id for row in active) >= limit:
                return False
            connection.execute(text('''INSERT INTO worker_leases (name,kind,workspace_id,owner,expires_at)
                VALUES (:name,:kind,:workspace,:owner,:expiry)
                ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at'''),
                {'name': name, 'kind': self.kind, 'workspace': workspace_id, 'owner': self.owner, 'expiry': now + self.ttl})
            return True

    def renew(self, run_id: str, workspace_id: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(text('UPDATE worker_leases SET expires_at=:expiry WHERE name=:name AND owner=:owner AND expires_at>:now'),
                {'expiry': time.time() + self.ttl, 'now': time.time(), 'name': f'{self.kind}:{workspace_id}:{run_id}', 'owner': self.owner})
            return result.rowcount == 1

    def release(self, run_id: str, workspace_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text('DELETE FROM worker_leases WHERE name=:name AND owner=:owner'),
                {'name': f'{self.kind}:{workspace_id}:{run_id}', 'owner': self.owner})

    def active(self, run_id: str, workspace_id: str) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(text('SELECT 1 FROM worker_leases WHERE name=:name AND expires_at>:now'),
                {'name': f'{self.kind}:{workspace_id}:{run_id}', 'now': time.time()}).first() is not None


@contextmanager
def durable_write(engine):
    """Serialize deletion tombstones and callbacks across database clients."""
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(1749926002)'))
        else:
            connection.execute(text('BEGIN IMMEDIATE'))
        yield connection
