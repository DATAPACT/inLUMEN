"""inLUMEN's pipeline design agent.

The agent edits a pipeline graph through a :class:`~inlumen.design.store.GraphStore`.
Use :func:`inlumen.design.run.design` to run it on an in-memory graph, or build
the pieces yourself: :func:`~inlumen.design.team.build_design_team` and
:func:`~inlumen.design.turn.run_design_turn`.

Needs the ``design`` extra (``pip install "inlumen-agents[design]"``).
"""
