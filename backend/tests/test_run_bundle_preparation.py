"""Concurrent run submissions bound executable-snapshot preparation."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

def test_run_bundle_preparation_is_bounded_and_releases_after_failure():
    import analytics_api

    lock = threading.Lock()
    active = 0
    peak = 0
    def prepare(graph):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.005)
            if graph.get("fail"):
                raise ValueError("invalid input")
            return {"files": []}
        finally:
            with lock:
                active -= 1

    with patch.object(analytics_api, "_prepare_dagster_execution_bundle", side_effect=prepare):
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(analytics_api.prepare_dagster_execution_bundle, [{}] * 20))
        assert len(results) == 20 and peak == 1
        with pytest.raises(ValueError, match="invalid input"):
            analytics_api.prepare_dagster_execution_bundle({"fail": True})
        assert analytics_api.prepare_dagster_execution_bundle({}) == {"files": []}
