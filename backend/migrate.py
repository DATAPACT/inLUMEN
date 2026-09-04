"""Idempotent PostgreSQL schema bootstrap for a single-VM deployment."""

from __future__ import annotations

import os

from chatbot_config_store import _engine as chatbot_config_engine
from chat_state import _engine as chat_state_engine
from codegen_runs import CodegenRunStore
from llm_credential_store import _engine as llm_credential_engine
from node_secrets import _engine as node_secret_engine
from workspace_store import ensure_schema


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    ensure_schema()
    CodegenRunStore(database_url)
    for factory in (chatbot_config_engine, chat_state_engine, node_secret_engine, llm_credential_engine):
        engine = factory()
        engine.dispose()
    print("inLUMEN database schema is ready", flush=True)


if __name__ == "__main__":
    main()
