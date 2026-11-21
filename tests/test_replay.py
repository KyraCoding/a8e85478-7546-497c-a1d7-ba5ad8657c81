import unittest
from unittest.mock import patch

from patches import Change
from replay import _plan_max, _profile, refine_replay


class StressReplayTests(unittest.TestCase):
    def test_profile_contains_actual_large_exceptions(self):
        data = _profile()
        large = [c for c in data["calls"] if c["size"] > 100_000]
        self.assertEqual(len(large), 2)
        self.assertEqual(large[0]["sql"], large[1]["sql"])
        self.assertEqual([c["args"]["max_nodes"] for c in large], [1_000_000, 50_000])
        self.assertTrue(all(c["exception"][0] == "ParseError" for c in large))

    def test_unchanged_cold_import_and_suffix_replay(self):
        data = {"files": ["sqlglot/a.py"], "imports": {}, "outside": {}, "node": "stress",
                "calls": [{"size": 200_000, "lines": {"sqlglot/a.py": [3]}, "tokenization": {}},
                          {"size": 10, "lines": {"sqlglot/a.py": [2]}, "tokenization": {}}]}
        a = Change("sqlglot/a.py", "def f(x):\n    a = x + 1\n    return a\n",
                   "def f(x):\n    a = x + 2\n    return a\n", {2})
        other = Change("sqlglot/b.py", "x = 1", "x = 2", {1})
        self.assertEqual(_plan_max([other], data), set())
        self.assertEqual(_plan_max([a], data), {1})
        with patch("replay._profile", return_value=data), patch("replay._verify", return_value=False):
            self.assertEqual(refine_replay([a], {"stress"}), {"stress"})
        data["calls"].reverse()
        self.assertIsNone(_plan_max([a], data))
