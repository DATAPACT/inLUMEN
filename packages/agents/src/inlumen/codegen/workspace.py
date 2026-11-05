"""Which workspace a generation run executes for.

Authenticated deployments set this per request (the codegen service's
security middleware); library callers keep the local default.
"""
from __future__ import annotations

from contextvars import ContextVar

EXECUTION_WORKSPACE: ContextVar[str] = ContextVar("execution_workspace", default="local-workspace")
