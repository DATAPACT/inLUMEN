"""The in-memory and Neo4j graph stores behave the same (ADR 0005).

Runs every scenario of inlumen.design.store_contract on both stores and
compares what the model would see and the resulting graph. Only run against
an explicitly configured disposable Neo4j database: it deletes all its data.
"""
import asyncio
import os
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.getenv('RUN_NEO4J_INTEGRATION') == '1', 'requires disposable Neo4j')
class DesignStoreParityTests(unittest.TestCase):
    def _clear(self):
        from neo4j_api import _base_driver

        with _base_driver.session() as session:
            session.run('MATCH (n) DETACH DELETE n').consume()

    def test_every_scenario_behaves_the_same_on_both_stores(self):
        from inlumen.design.memory_store import InMemoryGraphStore
        from inlumen.design.store_contract import SCENARIOS, observe
        from pipeline_agent.neo4j_store import Neo4jGraphStore

        with patch.dict('os.environ', {'AUTH_ENABLED': 'false'}):
            for name in SCENARIOS:
                with self.subTest(scenario=name):
                    self._clear()
                    on_neo4j = asyncio.run(observe(Neo4jGraphStore(), name))
                    in_memory = asyncio.run(observe(InMemoryGraphStore(), name))
                    self.maxDiff = None
                    self.assertEqual(in_memory, on_neo4j)
        self._clear()


if __name__ == '__main__':
    unittest.main()
