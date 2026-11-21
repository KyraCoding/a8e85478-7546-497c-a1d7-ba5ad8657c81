import hashlib
import unittest
from unittest.mock import Mock, patch

from default_profile import refine_defaults
from patches import Change
from source_reader import NODE, refine_source_reader


class ProfileTests(unittest.TestCase):
    def test_default_keeps_observers_and_refuses_other_edits(self):
        path = "sqlglot/util.py"
        original = "def choose(start=0):\n    return start\n"
        offset = original.index("0")
        table = {
            "schema": 1,
            "hashes": {path: hashlib.sha256(original.encode()).hexdigest()},
            "sites": {path: [["choose", "start", 1, offset, offset + 1, 2, 2]]},
        }
        cov = Mock()
        cov.nodes = ["body", "source", "import", "missing", "extra"]
        cov.missing = {"missing"}
        cov.lines.return_value = {"body"}
        cov.fixture.return_value = {"source"}
        chosen = set(cov.nodes) | {"unknown"}
        with patch("default_profile._sites", return_value=table), patch(
            "default_profile.import_consumers", return_value={"import"}
        ):
            def select(new):
                change = Change(path, original, new, {1}, {1})
                return refine_defaults([change], chosen, cov, chosen)
            self.assertEqual(select(original.replace("=0", "=1")), chosen - {"extra"})
            for text in ("=\ufeff1", "=True", "=(1)", "=1.0", "=1+0"):
                self.assertEqual(select(original.replace("=0", text)), chosen)

    def test_source_reader_requires_complete_singleton(self):
        from pathlib import Path
        cov = Mock()
        cov.missing = set()
        cov.fixture.return_value = {NODE}
        root = Path(".")
        change = Change("sqlglot/typing/x.py", "x=1\n", "# note\nx=1\n")
        with patch("source_reader.passing", return_value=True) as verify:
            self.assertEqual(refine_source_reader([change], {NODE}, cov, root), set())
            self.assertEqual(refine_source_reader([change, change], {NODE}, cov, root), {NODE})
            self.assertEqual(refine_source_reader([change], {NODE, "other"}, cov, root), {NODE, "other"})
            verify.assert_called_once()
        with patch("source_reader.passing", side_effect=OSError):
            self.assertEqual(refine_source_reader([change], {NODE}, cov, root), {NODE})
