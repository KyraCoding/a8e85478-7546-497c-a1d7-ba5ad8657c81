import unittest
from unittest.mock import Mock, patch

from import_effects import module_fallback


class SelectionTests(unittest.TestCase):
    def test_fallback_preserves_runtime_and_unmeasured_consumers(self):
        cov = Mock()
        cov.missing = {"unmeasured"}
        cov.file_execution.return_value = {"used-in-suite"}
        cov.fixture.return_value = {"raw-source-reader"}
        catalog = {"uses-module", "cold-negative", "used-in-suite", "raw-source-reader", "unmeasured"}
        footprint = {"tests/a.py": ({"sqlglot.a"}, catalog)}
        cold = {"cold-negative": {"sqlglot.b"}}
        with patch("import_effects._index", return_value=({}, footprint, {})), \
             patch("import_effects._isolated_items", return_value=cold):
            self.assertEqual(module_fallback("sqlglot/a.py", cov, catalog), catalog - {"cold-negative"})
