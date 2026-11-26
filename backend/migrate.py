"""Idempotent PostgreSQL schema bootstrap for a single-VM deployment."""

from __future__ import annotations

import os

from llm.chatbot_config_store import _engine as chatbot_config_engine
from chat.chat_state import _engine as chat_state_engine
from runs.codegen_runs import CodegenRunStore
from llm.llm_credential_store import _engine as llm_credential_engine
from llm.application_llm_store import _engine as application_llm_engine
from auth.node_secrets import _engine as node_secret_engine
from workspaces.workspace_store import ensure_schema
from chat.conversations import conversation_store


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    ensure_schema()
    conversation_store()
    CodegenRunStore(database_url)
    for factory in (chatbot_config_engine, chat_state_engine, node_secret_engine, llm_credential_engine, application_llm_engine):
        engine = factory()
        engine.dispose()
    print("inLUMEN database schema is ready", flush=True)


if __name__ == "__main__":
    main()
