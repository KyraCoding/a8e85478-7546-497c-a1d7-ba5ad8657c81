import ast
import unittest
from enum import Enum
from types import SimpleNamespace
from unittest.mock import patch

import heavy_profile as hp
from build_heavy import EnumInstrument, Instrument


class HeavyProfileTests(unittest.TestCase):
    def apply(self, expr, revised, values):
        original = f"def run(x):\n    return {expr}\n"
        patched = f"def run(x):\n    return {revised}\n"
        bindings, sites = {}, {}
        tree = Instrument(bindings, sites).visit(ast.parse(original))
        exec(compile(ast.fix_missing_locations(tree), "demo.py", "exec"), bindings)
        for value in values:
            bindings["run"](value)
        rows = {}
        for (key, kind), obs in sites.items():
            if obs.seen and obs.data() is not None:
                rows.setdefault(key, {})[kind] = {"0": [obs.data()]}
        data = {"nodes": ["observed"], "hashes": {"demo.py": hp.vp.digest(original)},
                "sites": {"demo.py": rows}}
        change = SimpleNamespace(path="demo.py", original=original, patched=patched, removed={2}, added={2})
        with patch.object(hp, "load", return_value=data):
            return hp.refine_heavy(change, {2}, {"observed", "unknown"})

    def test_literal_sets_and_thresholds(self):
        self.assertEqual(self.apply("x in ('a','b')", "x in ('a','c')", ["a","z"]), {"unknown"})
        self.assertIn("observed", self.apply("x in ('a','b')", "x in ('a','c')", ["b"]))
        self.assertEqual(self.apply("x > 3", "x > 4", [12, 90]), {"unknown"})
        self.assertIn("observed", self.apply("x > 3", "x > 4", [2, 5]))
        self.assertIn("observed", self.apply("x == 'a'", "x < 'b'", [4]))

    def test_dictionary_defaults_and_keys(self):
        self.assertEqual(self.apply("x.get('old')", "x.get('new')", [{"third": 1}]), {"unknown"})
        self.assertIn("observed", self.apply("x.get('old')", "x.get('new')", [{"new": 7}]))
        self.assertEqual(self.apply("x.get('k',1)", "x.get('k',2)", [{"k": 0}]), {"unknown"})
        self.assertIn("observed", self.apply("x.get('k',1)", "x.get('k',2)", [{}]))

    def test_skipped_operands(self):
        self.assertEqual(self.apply("x and x[0]", "x and x[1]", [[]]), {"unknown"})
        self.assertEqual(self.apply("x and x[0]", "x", [[]]), {"unknown"})
        self.assertIn("observed", self.apply("x and x[0]", "x and x[1]", [["a","b"]]))
        self.assertEqual(self.apply("2 if x else 3", "99 if x else 3", [False]), {"unknown"})
        self.assertIn("observed", self.apply("x and 2", "x and (y := 2)", [False]))
        self.assertEqual(self.apply("x == 'rare'", "x == 'rare' and log(x)", ["common"]), {"unknown"})
        self.assertIn("observed", self.apply("x == 'rare'", "x == 'rare' and log(x)", ["rare"]))
        self.assertEqual(self.apply("x.get('rare')", "x.get('rare') and log(x)", [{}]), {"unknown"})

    def test_overloads_are_not_replayed(self):
        class Custom(dict):
            def get(self, key, default=None):
                return key
        self.assertIn("observed", self.apply("x.get('old')", "x.get('new')", [Custom()]))
        class Compare(str):
            def __eq__(self, other):
                return False
        self.assertIn("observed", self.apply("x == 'a'", "x == 'b'", [Compare('a')]))

    def test_enum_members_aliases_and_missing_attributes(self):
        class Flavor(Enum):
            RED = 1
            BLUE = 2
            ALSO_BLUE = 2
            GREEN = 3

        original = "def run(x):\n    return x == Flavor.RED\n"
        symbols, sites = {"Flavor": Flavor}, {}
        tree = EnumInstrument(symbols, sites).visit(ast.parse(original))
        exec(compile(ast.fix_missing_locations(tree), "demo.py", "exec"), symbols)
        self.assertFalse(symbols["run"](Flavor.BLUE))
        [(key, obs)] = sites.items()
        members, names = obs.data()
        data = {"nodes": ["test"], "hashes": {"demo.py": hp.vp.digest(original)},
                "sites": {"demo.py": {key[0]: {"e": {"0": [["table", names]]}}}},
                "enums": {"table": members}}
        with patch.object(hp, "load", return_value=data):
            for name, expected in [("GREEN", set()), ("ALSO_BLUE", {"test"}), ("MISSING", {"test"})]:
                change = SimpleNamespace(path="demo.py", original=original, patched=original.replace("RED", name),
                                         removed={2}, added={2})
                self.assertEqual(hp.refine_heavy(change, {2}, {"test"}), expected)
