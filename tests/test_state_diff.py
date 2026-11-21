import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from patches import Change
import state_diff as state


BASE = """class ClockMeta(type):
    def __new__(meta, name, bases, attrs):
        table = attrs['TIME_MAPPING']
        attrs['INVERSE_TIME_MAPPING'] = {v: k for k, v in table.items()}
        if not table['hour']:
            raise ValueError('empty format')
        return super().__new__(meta, name, bases, attrs)

class Clock(metaclass=ClockMeta):
    TIME_MAPPING = {'hour': 'H'}
"""
MOD = "sqlglot.clock"
OWNER = MOD + ".Clock"
PATH = "sqlglot/clock.py"


class StateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "sqlglot").mkdir()
        (self.root / "sqlglot/__init__.py").write_text("")
        (self.root / PATH).write_text(BASE)
        rows, _ = state._child(self.root, [MOD], [])
        self.nodes = {"format", "unrelated", "raw", "unknown"}
        self.cov = Mock(nodes=list(self.nodes - {"unknown"}), missing=set())
        self.cov.fixture.return_value = set()
        self.cov.lines.side_effect = lambda path, ns: {"raw"} if path == "reflect.py" else set()
        self.info = {"version": state.VERSION, "runtime": state._runtime(),
                     "sources": {PATH: hashlib.sha256(BASE.encode()).hexdigest()},
                     "rows": rows, "descendants": {},
                     "time": {"owners": [OWNER], "collected": sorted(self.nodes),
                              "reflection": {"reflect.py": [9]}}}
        self.enterContext(patch.dict(os.environ, SQLGLOT_ROOT=str(self.root)))
        self.enterContext(patch("state_diff._map", return_value=self.info))
        reads = {"TIME_MAPPING": {}, "INVERSE_TIME_MAPPING": {"reader.py": [2]}}
        self.records = {(OWNER, "TIME_MAPPING", "@"): [], (OWNER, "INVERSE_TIME_MAPPING", "@"): [0]}
        self.records.update({(OWNER, attr, "*"): [] for attr in reads})
        self.enterContext(patch("declarations._lookup_index", return_value=(
            ["format", "unrelated", "raw"], self.records, {}, {"reads": reads})))
        self.enterContext(patch("import_effects._index", return_value=({}, {}, {})))

    def edit(self, rhs):
        return Change(PATH, BASE, BASE.replace("{'hour': 'H'}", rhs), {10})

    def select(self, change):
        return state.refine_state_diagnostics([change], self.nodes, self.cov, self.nodes)

    def oracle(self, source):
        path = self.root / PATH
        path.write_text(source)
        try:
            proc = subprocess.run([sys.executable, "-B", "-S", "-c",
                                   "from sqlglot.clock import Clock; "
                                   "assert Clock.INVERSE_TIME_MAPPING['H'] == 'hour'"],
                                  cwd=self.root, capture_output=True)
            return proc.returncode
        finally:
            path.write_text(BASE)

    def test_real_derived_failure_and_source_proven_equivalence(self):
        broken = self.edit("{'hour': 'X'}")
        self.assertEqual(self.oracle(BASE), 0)
        self.assertNotEqual(self.oracle(broken.patched), 0)
        self.assertEqual(self.select(broken)[0], {"format", "raw", "unknown"})
        harmless = self.edit("{'hour': 'unused', 'hour': 'H'}")
        self.assertEqual(self.oracle(harmless.patched), 0)
        self.cov.fixture.return_value = {"raw"}
        self.assertEqual(self.select(harmless)[0], {"raw", "unknown"})

    def test_import_errors_missing_support_and_budget_keep_candidates(self):
        self.assertEqual(self.select(self.edit("{'hour': ''}"))[0], self.nodes)
        change = self.edit("{'hour': 'X'}")
        del self.records[OWNER, "INVERSE_TIME_MAPPING", "*"]
        self.assertEqual(self.select(change)[0], self.nodes)
        with patch("state_diff._child", side_effect=subprocess.TimeoutExpired("child", 3)):
            self.assertEqual(self.select(change)[0], self.nodes)
        with patch("state_diff._child", side_effect=AssertionError("should skip")):
            self.assertEqual(state.refine_state([change], {"format"}, self.cov, self.nodes), {"format"})
            change.structural = True
            self.assertEqual(self.select(change)[0], self.nodes)

    def test_combined_overlay_and_external_effect_guard(self):
        a = Change("sqlglot/a.py", "LABEL = 'A'", "LABEL = 'B'")
        b = Change("sqlglot/b.py", "from .a import LABEL\nTABLE = {LABEL: 1}",
                   "from .a import LABEL\nTABLE = {LABEL: 2}")
        for c in (a, b):
            (self.root / c.path).write_text(c.original)
        rows, _ = state._child(self.root, ["sqlglot.b"], [a, b])
        self.assertEqual(rows["sqlglot.b"]["sqlglot.b"]["TABLE"], state.Fingerprints().binding({"B": 2}))
        blocked = self.root / "forbidden"
        b.patched += f"\nopen({str(blocked)!r}, 'w').close()"
        with self.assertRaises(subprocess.CalledProcessError):
            state._child(self.root, ["sqlglot.b"], [a, b])
        self.assertFalse(blocked.exists())
        self.assertEqual((self.root / a.path).read_text(), a.original)

    def test_fingerprints_include_bytecode_closures_defaults_and_order(self):
        def make(x):
            return lambda y=1: x + y

        def alternate(x):
            return lambda y=1: x - y

        fp = lambda v: state.Fingerprints().binding(v)
        self.assertEqual(fp(make("a")), fp(make("a")))
        self.assertNotEqual(fp(make("a")), fp(make("b")))
        self.assertNotEqual(fp(make(1)), fp(alternate(1)))
        f = make(1)
        before = fp(f)
        f.__defaults__ = (2,)
        self.assertNotEqual(before, fp(f))
        self.assertNotEqual(fp({"a": 1, "b": 2}), fp({"b": 2, "a": 1}))
        self.assertEqual(fp({"a", "b"}), fp({"b", "a"}))
        self.assertNotEqual(fp(True), fp(1))
        self.assertIsNone(fp(object()))

    def test_unsupported_and_mixed_patches_never_launch_python(self):
        change = self.edit("{'hour': str(123)}")
        with patch("state_diff._child", side_effect=AssertionError("should skip")):
            self.assertEqual(self.select(change)[0], self.nodes)
            result = state.refine_state([self.edit("{'hour': 'X'}"), change],
                                        self.nodes, self.cov, self.nodes)
            self.assertEqual(result, self.nodes)
