import tempfile
import unittest
from pathlib import Path

from patches import read_changes
from schedule import distribute


class InputAndSchedulingTests(unittest.TestCase):
    def test_old_line_numbers_survive_an_earlier_insertion(self):
        diff = ("--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+# intro\n"
                "@@ -3 +4 @@\n-third\n+replaced\n")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.py").write_text("first\nsecond\nthird\n", encoding="utf-8")
            item, = read_changes(diff, root)
        self.assertEqual(item.removed, {3})
        self.assertEqual(item.added, {1, 4})
        self.assertEqual(item.patched, "# intro\nfirst\nsecond\nreplaced\n")

    def test_workers_are_worth_the_startup_cost(self):
        self.assertEqual(distribute([], {}), [])
        self.assertEqual(distribute([0, 1, 2], {0: 10, 1: 20, 2: 30}), [[2, 1, 0]])
        self.assertEqual(distribute([0, 1, 2], {0: 10000, 1: 9000, 2: 1000}), [[0], [1, 2]])
