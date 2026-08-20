"""Compatibility facade for the dedicated :mod:`pipeline_agent` package.

New code should import ``pipeline_agent.team``. This module remains so existing
API integrations do not need to change in lockstep with the refactor.
"""

from pipeline_agent.contract import normalize_agent_implementation
from pipeline_agent.team import build_pipeline_editing_team
from pipeline_agent.tools import _agent_query_returned_no_rows

__all__ = [
    "_agent_query_returned_no_rows",
    "build_pipeline_editing_team",
    "normalize_agent_implementation",
]
