"""Serialize graph transactions per workspace and reject stale browser writes."""
from flask import g, has_request_context, request


class GraphRevisionConflict(ValueError):
    pass


def lock_revision(tx, workspace_id: str) -> tuple[int, bool]:
    # The unique workspace constraint makes the MERGE a single lock resource.
    row = tx.run("""
        MERGE (r:WORKSPACE_REVISION {workspace_id: $workspace_id})
        ON CREATE SET r.revision = 0
        SET r.lock = coalesce(r.lock, 0) + 1
        RETURN r.revision AS revision
    """, workspace_id=workspace_id).single()
    revision = int(row["revision"])
    mutation = has_request_context() and request.method not in {"GET", "HEAD", "OPTIONS"}
    expected = request.headers.get("If-Match") if has_request_context() else None
    if mutation and expected is not None and expected != f'"{revision}"':
        g.graph_revision_conflict = revision
        raise GraphRevisionConflict("The graph changed in another session. Reload before applying this edit.")
    return revision, mutation


def finish_revision(tx, workspace_id: str, revision: int, mutation: bool) -> None:
    if mutation:
        revision += 1
        tx.run("MATCH (r:WORKSPACE_REVISION {workspace_id:$workspace_id}) SET r.revision=$revision",
               workspace_id=workspace_id, revision=revision).consume()
    if has_request_context():
        g.graph_revision = revision
