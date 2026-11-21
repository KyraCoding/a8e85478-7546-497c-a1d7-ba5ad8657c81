import os
import unittest
from pathlib import Path
from unittest.mock import patch

from coverage_map import Coverage
from fixture_flow import refine_fixture
from patches import Change


class FixtureFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        roots = [Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot")), Path(".work/sqlglot")]
        cls.root = next((r.resolve() for r in roots if (r / "tests/helpers.py").exists()), None)
        if cls.root is None:
            raise unittest.SkipTest("pinned SQLGlot checkout unavailable")
        cls.cov = Coverage()

    def selected(self, path, before, after):
        change = Change(path, before, after)
        readers = self.cov.fixture(path)
        with patch.dict(os.environ, {"SQLGLOT_ROOT": str(self.root)}):
            return refine_fixture(change, readers, self.cov)

    def test_expected_field_and_malformed_setup(self):
        path = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
        old = "SELECT x; SELECT y;"
        selected = self.selected(path, old, old.replace("SELECT y", "SELECT z"))
        self.assertEqual({n.split("::")[-1] for n in selected}, {"test_tpch", "test_optimized_tpch"})
        self.assertEqual(self.selected(path, old, "# invalid\n" + old), self.cov.fixture(path))

    def test_comments_and_metadata(self):
        path = "tests/fixtures/pretty.sql"
        old = "# title: foo\nSELECT x; SELECT y;"
        self.assertEqual(self.selected(path, old, "-- hello\n" + old), set())
        self.assertEqual(self.selected(path, old, old.replace("title: foo", "title: bar")), set())
        self.assertEqual(self.selected(path, old, old.replace("SELECT x", "SELECT y")), self.cov.fixture(path))
