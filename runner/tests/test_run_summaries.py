from app.run_summaries import Neo4jRunSummaryStore


class _Record:
    def __getitem__(self, key):
        return "pipeline-1" if key == "pipeline_uid" else None


class _Result:
    def single(self):
        return _Record()


class _Transaction:
    def __init__(self):
        self.query = ""
        self.parameters = {}

    def run(self, query, **parameters):
        self.query = " ".join(query.split())
        self.parameters = parameters
        return _Result()


def test_neo4j_summary_write_is_idempotent_and_updates_latest_pipeline_status():
    transaction = _Transaction()
    summary = {
        "run_id": "run-1",
        "pipeline_id": "pipeline-1",
        "active_version_uid": "main",
        "snapshot_sha256": "sha256:graph",
        "bundle_sha256": "sha256:bundle",
        "status": "succeeded",
        "engine": "dagster",
        "execution_mode": "background",
        "created_at": "2026-08-27T10:00:00Z",
        "started_at": "2026-08-27T10:00:01Z",
        "finished_at": "2026-08-27T10:00:03Z",
        "duration_ms": 2000,
        "output_count": 1,
        "error_code": None,
        "error_message": None,
        "resource_profile": "ml_cpu",
        "resource_cpu": 4,
        "resource_memory_bytes": 4 * 1024**3,
    }

    assert Neo4jRunSummaryStore._write_summary(transaction, summary) is True
    assert "MERGE (run:PIPELINE_RUN_SUMMARY {run_id: $run_id})" in transaction.query
    assert "MERGE (pipeline)-[:HAS_RUN_SUMMARY]->(run)" in transaction.query
    assert "pipeline.last_run_status = run.status" in transaction.query
    assert "MERGE (version)-[:HAS_RUN_SUMMARY]->(run)" in transaction.query
    assert transaction.parameters == summary
