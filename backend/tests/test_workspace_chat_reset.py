import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_state


class WorkspaceChatResetTests(unittest.TestCase):
    def test_bulk_clear_is_scoped_and_retains_other_state(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            selected = directory / 'chat-sessions' / 'workspace-a'
            other = directory / 'chat-sessions' / 'workspace-b'
            selected.mkdir(parents=True)
            other.mkdir(parents=True)
            for session in ['first', 'second']:
                (selected / f'{session}.json').write_text('{}')
            (other / 'first.json').write_text('{}')
            (directory / 'runtime.json').write_text('{}')
            with patch.object(chat_state, 'STATE_DIR', directory), patch.object(chat_state, '_database_url', return_value=''), patch.object(chat_state, 'current_workspace_id', return_value='workspace-a'):
                self.assertEqual(chat_state.clear_workspace_chat_states(), 2)
            self.assertEqual(list(selected.glob('*.json')), [])
            self.assertTrue((other / 'first.json').exists())
            self.assertTrue((directory / 'runtime.json').exists())
