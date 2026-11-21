import sys
import types
import unittest
from unittest.mock import patch

from fe_replay import _admissible, _writes
from patches import Change

HEAD = "LIMIT = 1\n\n\ndef f(self, x):\n"


def _edit(old, new, path="sqlglot/first.py"):
    return Change(path, HEAD + "    " + old + "\n    return x\n", HEAD + "    " + new + "\n    return x\n", {5}, {5})


def _gate(changes, consumer=None, during_import=False):
    stub = types.ModuleType("import_effects")
    stub.import_consumers = lambda *_: during_import
    with patch.dict(sys.modules, {"import_effects": stub}):
        return _admissible(changes, None, set(), consumer or {})


class FrontendGateTests(unittest.TestCase):
    def test_local_body_edits_write_nothing(self):
        for old, new in (
            ("y = x + 1", "y = x + 2"),
            ("y = x", "y = [x, LIMIT]"),
            ("self.args['x'] = x", "self.args['x'] = None"),
            ("x = x or 0", "x = x and 0"),
        ):
            with self.subTest(new=new):
                self.assertFalse(_writes(_edit(old, new)))

    def test_shared_writes(self):
        for new in (
            "LIMIT = x",
            "self.TRANSFORMS[x] = None",
            "Generator.TRANSFORMS.update(x)",
            "SETTINGS.add(x)",
            "cls.cache = x",
            "type(self).seen = x",
            "setattr(self, 'name', x)",
            "global LIMIT",
        ):
            with self.subTest(new=new):
                self.assertTrue(_writes(_edit("y = x", new)))
                self.assertEqual(_gate([_edit("y = x", new)]), "edit writes shared state")

    def test_module_level_line_is_not_a_body_line(self):
        self.assertFalse(_writes(Change("sqlglot/first.py", "LIMIT = 1\n", "LIMIT = 2\n", {1}, {1})))

    def test_several_edits_in_several_files(self):
        first = Change(
            "sqlglot/first.py",
            HEAD + "    y = x + 1\n    return y\n",
            HEAD + "    y = x + 2\n    y = y * 3\n    return y\n",
            {5}, {5, 6},
        )
        second = Change("sqlglot/second.py", "def g(a):\n    return a < 1\n", "def g(a):\n    return a <= 2\n", {2}, {2})
        sources, changed = _gate([first, second])
        self.assertEqual(sources, {first.path: first.patched, second.path: second.patched})
        self.assertEqual(changed, {first.path: {5}, second.path: {2}})

    def test_refusals(self):
        body = _edit("y = x + 1", "y = x + 2")
        self.assertEqual(_gate([body], consumer={body.path: [5]}), "changed line runs on the execution side")
        self.assertEqual(_gate([body], during_import=True), "changed line runs during import")
        level = Change("sqlglot/first.py", HEAD + "    return x\n", HEAD.replace("1", "2") + "    return x\n", {1}, {1})
        self.assertEqual(_gate([level]), "definition-time edit")
        self.assertEqual(_gate([body, body]), "repeated file")
        self.assertEqual(_gate([Change("tests/helper.py", "a = 1\n", "a = 2\n", {1}, {1})]), "python edit outside sqlglot")


if __name__ == "__main__":
    unittest.main()
