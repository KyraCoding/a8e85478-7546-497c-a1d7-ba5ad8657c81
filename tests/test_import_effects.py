import unittest
from unittest.mock import Mock, patch

from build_cache_effects import cache_sites
from import_effects import import_consumers


class ImportEffectsTests(unittest.TestCase):
    def test_factory_writes_global_cache(self):
        src = """cache = {}
def build(key):
    return str(key)
def get(key):
    value = cache.get(key)
    if value is None:
        value = build(key)
        cache[key] = value
    return value
"""
        self.assertEqual(cache_sites(src), [(2, 3, 5), (6, 8, 5)])
        self.assertEqual(cache_sites(src.replace("cache[key] = value", "return value")), [])

    def test_preserve_uncertain_and_cache_consumers(self):
        profile = {"x.py": {"2": ["package.feature"]}}
        tests = {"a.py": ({"package.feature"}, {"A"}), "b.py": ({"package"}, {"B"})}
        coverage = Mock()
        coverage.lines.return_value = {"B"}
        with patch("import_effects._index", return_value=(profile, tests, {"x.py": [(3, 4, 6)]})):
            self.assertEqual(import_consumers("x.py", {2}, coverage, {"A", "B", "C"}), {"A", "C"})
            self.assertEqual(import_consumers("x.py", {3}, coverage, {"A", "B", "C"}), {"B"})
            coverage.lines.assert_called_once_with("x.py", {6})
