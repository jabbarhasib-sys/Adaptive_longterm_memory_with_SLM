"""
Unit tests for Teammate A — Flat Memory Strategy (strategies/flat_team.py).
Tests:
1. Registration & auto-discovery as 'team_flat'.
2. Candidate admission, deduplication, conflict detection & superseding.
3. Undivided pool budget constraint enforcement.
4. Relevance-based retrieval.
5. Complete reset isolation between queries.
"""

import unittest
from memory_interface import get_strategy, list_registered_strategies
from strategies.flat_team import TeamFlatMemoryStrategy


class TestTeamFlatMemory(unittest.TestCase):
    def setUp(self):
        self.strategy = TeamFlatMemoryStrategy(total_budget=500)

    def test_auto_discovery_and_registration(self):
        registered = list_registered_strategies()
        self.assertIn("team_flat", registered)
        instance = get_strategy("team_flat", budget=300)
        self.assertEqual(instance.name, "team_flat")
        self.assertEqual(instance.total_budget, 300)

    def test_intake_and_retrieval(self):
        session1 = [
            {"role": "user", "content": "I live in Berlin.", "timestamp": 100.0},
            {"role": "user", "content": "I work at OpenAI.", "timestamp": 110.0},
        ]
        self.strategy.process_session(session1, session_id="s1", current_timestamp=120.0)

        memories = self.strategy.get_persisted_memories()
        self.assertGreaterEqual(len(memories), 2)

        retrieved = self.strategy.retrieve("Where do I live?", top_k=2)
        self.assertGreater(len(retrieved), 0)
        contents = [getattr(item[0], "content", str(item[0])) for item in retrieved]
        self.assertTrue(any("Berlin" in c for c in contents))

    def test_conflict_superseding(self):
        session1 = [
            {"role": "user", "content": "I work at OpenAI.", "timestamp": 100.0},
        ]
        session2 = [
            {"role": "user", "content": "I work at Google now.", "timestamp": 200.0},
        ]
        self.strategy.process_session(session1, session_id="s1", current_timestamp=100.0)
        self.strategy.process_session(session2, session_id="s2", current_timestamp=200.0)

        # Active persisted memories should only keep current fact
        persisted = self.strategy.get_persisted_memories()
        contents = [m.content for m in persisted]
        self.assertTrue(any("Google" in c for c in contents))
        self.assertFalse(any("OpenAI" in c for c in contents))

        # Superseded memory is in archive
        archive_contents = [m.content for m in self.strategy.archive_store]
        self.assertTrue(any("OpenAI" in c for c in archive_contents))

    def test_budget_enforcement(self):
        # Strict low budget (e.g. 20 tokens)
        tight_strategy = TeamFlatMemoryStrategy(total_budget=20)
        session = [
            {"role": "user", "content": "I like reading computer science textbooks.", "timestamp": 100.0},
            {"role": "user", "content": "I prefer dark mode in VSCode.", "timestamp": 110.0},
            {"role": "user", "content": "I drink water every morning at 7am.", "timestamp": 120.0},
        ]
        tight_strategy.process_session(session, session_id="s1", current_timestamp=130.0)
        usage = tight_strategy.get_storage_usage()
        self.assertLessEqual(usage, 20)

    def test_reset_isolation(self):
        session = [{"role": "user", "content": "Secret password is XYZ.", "timestamp": 100.0}]
        self.strategy.process_session(session, session_id="s1", current_timestamp=100.0)
        self.assertGreater(len(self.strategy.get_persisted_memories()), 0)

        self.strategy.reset()
        self.assertEqual(len(self.strategy.get_persisted_memories()), 0)
        self.assertEqual(len(self.strategy.archive_store), 0)
        self.assertEqual(self.strategy.get_storage_usage(), 0)


if __name__ == "__main__":
    unittest.main()
