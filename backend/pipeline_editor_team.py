"""Compatibility facade for the dedicated :mod:`pipeline_agent` package.

New code should import ``pipeline_agent.team``. This module remains so existing
API integrations do not need to change in lockstep with the refactor.
"""

from inlumen.design.contract import normalize_agent_implementation
from inlumen.design.store import returned_no_rows as _agent_query_returned_no_rows
from pipeline_agent.team import build_pipeline_editing_team

__all__ = [
    "_agent_query_returned_no_rows",
    "build_pipeline_editing_team",
    "normalize_agent_implementation",
]
