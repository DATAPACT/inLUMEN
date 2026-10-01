"""Real multi-worker checks in an isolated schema of an explicit test database."""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from sqlalchemy import create_engine, text

from conversations import ConversationError, ConversationStore


@unittest.skipUnless(os.getenv('TEST_POSTGRES_URL'), 'requires disposable PostgreSQL')
class ConversationPostgresTests(unittest.TestCase):
    def setUp(self):
        url = os.environ['TEST_POSTGRES_URL'].replace('postgresql://', 'postgresql+psycopg://', 1)
        self.root = create_engine(url)
        self.schema = 'chat_test_' + uuid.uuid4().hex
        with self.root.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA {self.schema}'))
        location = url + ('&' if '?' in url else '?') + urlencode({'options': '-csearch_path=' + self.schema})
        self.first = create_engine(location)
        self.scope = {'user_id': str(uuid.uuid4()), 'workspace_id': str(uuid.uuid4())}
        with self.first.begin() as connection:
            connection.execute(text('CREATE TABLE app_users(id UUID PRIMARY KEY)'))
            connection.execute(text('CREATE TABLE workspaces(id UUID PRIMARY KEY)'))
            connection.execute(text('INSERT INTO app_users VALUES(:user_id)'), self.scope)
            connection.execute(text('INSERT INTO workspaces VALUES(:workspace_id)'), self.scope)
        self.stores = [ConversationStore(location), ConversationStore(location)]

    def tearDown(self):
        for store in self.stores:
            store.engine.dispose()
        self.first.dispose()
        with self.root.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {self.schema} CASCADE'))
        self.root.dispose()

    def test_cross_worker_claim_cancel_finish_replay_and_identity_cascade(self):
        def claim(store):
            try:
                store.start(self.scope, turn_id='same-turn', request_sha256='same', user_message='Create a pipeline')
                return 'claimed'
            except ConversationError as error:
                return error.code
        with ThreadPoolExecutor(2) as executor:
            self.assertCountEqual(list(executor.map(claim, self.stores)), ['claimed', 'turn_pending'])
        self.stores[1].cancel(self.scope, 'same-turn')
        self.assertTrue(self.stores[0].heartbeat(self.scope, 'same-turn'))
        response = {'assistant_message': 'Stopped'}
        self.stores[0].finish(self.scope, 'same-turn', response, 409, 'cancelled')
        self.assertEqual(len(self.stores[1].read(self.scope)['messages']), 2)
        _, replay, status = self.stores[1].start(self.scope, turn_id='same-turn', request_sha256='same', user_message='Create a pipeline')
        self.assertEqual((replay, status), (response, 409))
        with self.first.begin() as connection:
            connection.execute(text('DELETE FROM app_users WHERE id=:user_id'), self.scope)
            for table in ('chat_conversations', 'chat_messages', 'chat_turns', 'chat_active_conversations', 'chat_cancel_requests'):
                self.assertEqual(connection.execute(text(f'SELECT count(*) FROM {table}')).scalar_one(), 0)

    def test_two_workers_expire_a_crashed_turn_once_and_clear_workspace(self):
        self.stores[0].start(self.scope, turn_id='crashed-turn', request_sha256='same', user_message='Create a pipeline')
        with self.first.begin() as connection:
            connection.execute(text("UPDATE chat_turns SET heartbeat_at='2000-01-01T00:00:00+00:00'"))
        with ThreadPoolExecutor(2) as executor:
            pages = list(executor.map(lambda store: store.read(self.scope), self.stores))
        for page in pages:
            self.assertIsNone(page['conversation']['pending_turn_id'])
            self.assertEqual(len(page['messages']), 2)
        with self.stores[0].clear_guard(self.scope['workspace_id']) as connection:
            self.stores[0].clear_locked(connection, self.scope['workspace_id'])
        self.assertIsNone(self.stores[1].read(self.scope)['conversation'])
