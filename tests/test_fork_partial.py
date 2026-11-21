import marshal
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fork_check as fc


@unittest.skipUnless(sys.platform == "linux" and hasattr(sys, "monitoring"), "needs Linux fork worker")
class PartialWorkerTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        sys.path.insert(0, folder.name)
        self.addCleanup(sys.path.remove, folder.name)

    def module(self, name, source):
        (self.folder / (name + ".py")).write_text("import os, signal, unittest\n" + source)

    def run_job(self, cases, *, context=(), imports=(), cpu=2, wall=5):
        job = dict(id=1, tests=cases, modules=[], need_line=[], imports=imports,
                   cpu=cpu, wall=wall, context={t[0] for t in context})
        return fc._run_job(1, job, [])

    def plan(self, second, *, finalizer="", last=False):
        self.module("one", "class T(unittest.TestCase):\n    def test_ok(self): pass\n")
        self.module("two", "class T(unittest.TestCase):\n"
                    "    def test_before(self): pass\n"
                    "    def test_end(self): pass\n"
                    "    def test_check(self):\n" + second + "\n" + finalizer)
        cases = [(1, "one", "T", "test_ok"), (2, "two", "T", "test_before"),
                 (3, "two", "T", "test_check")]
        if last:
            cases.append((4, "two", "T", "test_end"))
        return cases

    def retained(self, cases, reply):
        numbers = {method: n for n, _, _, method in cases}
        return fc._retained(reply, set(numbers), numbers, cases, [])

    def test_late_subtest_and_unfinished_sibling(self):
        for last in (False, True):
            with self.subTest(unfinished=last):
                cases = self.plan("        for i in range(3):\n"
                                  "            with self.subTest(i=i): self.assertLess(i, 2)", last=last)
                reply = self.run_job(cases)
                self.assertEqual(reply["tests"][3], "fail")
                self.assertTrue(reply["fixtures_closed"])
                kept = {"test_check"}
                if last:
                    self.assertEqual(reply["tests"][4], "notrun")
                    kept.update(("test_before", "test_end"))
                self.assertEqual(self.retained(cases, reply), kept)

    def test_class_and_module_teardown_errors(self):
        for finalizer in ("    @classmethod\n    def tearDownClass(cls): raise ValueError('teardown')",
                          "def tearDownModule(): raise ValueError('teardown')"):
            with self.subTest(finalizer=finalizer):
                cases = self.plan("        self.fail('body')", finalizer=finalizer)
                reply = self.run_job(cases)
                self.assertFalse(reply["fixtures_closed"])
                self.assertEqual(len(self.retained(cases, reply)), 3)

    def test_unresolved_and_abnormal_completion(self):
        for body, expected in (("self.skipTest('unknown')", "ok"),
                               ("os.kill(os.getpid(), signal.SIGKILL)", "crash"),
                               ("while True: pass", "cpu-timeout")):
            with self.subTest(body=body):
                cases = self.plan("        " + body)
                reply = self.run_job(cases, cpu=0.15)
                self.assertEqual(reply["status"], expected)
                self.assertEqual(len(self.retained(cases, reply)), 3)

    def test_truncated_completion_frame(self):
        def short_write(level, job, fd, tmp):
            message = marshal.dumps(dict(status="ok", tests={1: "pass"}, moved=[], taint=[],
                                         fixtures_closed=True))
            os.write(fd, struct.pack("<I", len(message) + 1) + message)

        cases = self.plan("        pass")
        with patch.object(fc, "_worker", short_write):
            reply = self.run_job(cases)
        self.assertEqual(reply["status"], "crash")
        self.assertEqual(len(self.retained(cases, reply)), 3)

    def test_missing_result_or_wall_timeout_discards_earlier_success(self):
        cases = self.plan("        self.fail('late')", last=True)
        reply = self.run_job(cases)
        del reply["tests"][4]
        self.assertEqual(len(self.retained(cases, reply)), 4)
        cases = self.plan("        __import__('time').sleep(5)")
        reply = self.run_job(cases, wall=0.25)
        self.assertEqual(reply["status"], "timeout")
        self.assertEqual(len(self.retained(cases, reply)), 3)

    @unittest.skipUnless(Path(fc.ROOT, "tests/test_dialect_entry_points.py").is_file(), "needs pinned SQLGlot")
    def test_ordered_registry_writers_and_reset(self):
        self.module("observer", "from sqlglot import Dialect\nclass T(unittest.TestCase):\n"
                    "    def test_present(self): self.assertIn('mydialect', Dialect._classes)\n"
                    "    def test_absent(self): self.assertNotIn('mydialect', Dialect._classes)\n")
        numbers = dict(zip(fc._REGISTRY_TESTS, (2, 3, 5, 6, 7)))
        targets = [(4, "observer", "T", "test_present"), (8, "observer", "T", "test_absent")]
        chosen = {"observer.py::T::" + t[3]: t[0] for t in targets}
        numbers.update(chosen)
        millis = {n: 500 if n == 4 else 1 for n in numbers.values()}
        context = fc._partial_context([], set(chosen), numbers, millis, targets)
        self.assertEqual([t[0] for t in context], [2, 3, 5, 6, 7])
        imports = ["tests.test_dialect_entry_points"]
        self.assertEqual(self.run_job(targets, imports=imports)["tests"][4], "fail")
        ordered = self.run_job(sorted(targets + context), context=context, imports=imports)
        self.assertTrue(ordered["fixtures_closed"])
        self.assertEqual(set(ordered["tests"].values()), {"pass"})
        self.assertIsNone(fc._partial_context([SimpleNamespace(path="tests/conftest.py")],
                                              set(chosen), numbers, millis, targets))
        millis[4] = 1
        self.assertIsNone(fc._partial_context([], set(chosen), numbers, millis, targets))
