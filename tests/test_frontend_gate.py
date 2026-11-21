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
    def test_changed_rows_inside_shared_mutations(self):
        for body, old, new in (
            ("del CACHE['a']", "'a'", "'b'"),
            ("cache['k'] = 1", "1", "2"),
            ("CACHE.update(\n        flag=1,\n    )", "flag=1", "flag=2"),
            ("global flag\n    flag = 1", "= 1", "= 2"),
            ("cache['k'] = (\n        1\n    )", "        1", "        2"),
            ("del cache[\n        'a'\n    ]", "'a'", "'b'"),
            ("alias = cache\n    alias.append(1)", "append(1)", "append(2)"),
            ("alias = cache\n    alias += [1]", "[1]", "[2]"),
            ("builtins.setattr(cache, 'a', 1)", "'a'", "'b'"),
            ("flag = 0\n    def inner():\n        nonlocal flag\n        flag = 1", "= 1", "= 2"),
        ):
            with self.subTest(body=body):
                source = "cache = CACHE = {}\nflag = 0\n\ndef f():\n    " + body + "\n"
                row = source[:source.index(old)].count("\n") + 1
                change = Change("sqlglot/first.py", source, source.replace(old, new, 1), {row}, {row})
                self.assertEqual(_gate([change]), "edit writes shared state")

    def test_sibling_shared_mutation_does_not_disqualify_local_edit(self):
        source = "cache = {}\ndef f(x):\n    cache.update(x)\n    return x + 1\n"
        change = Change("sqlglot/first.py", source, source.replace("+ 1", "+ 2"), {4}, {4})
        self.assertEqual(_gate([change])[1], {change.path: {4}})

    def test_removed_shared_mutation_still_disqualifies_edit(self):
        change = _edit("del cache[x]", "return None")
        self.assertEqual(_gate([change]), "edit writes shared state")

    def test_rebound_self_is_an_ambiguous_receiver(self):
        for binding in ("self = cache", "self.args = cache"):
            with self.subTest(binding=binding):
                source = HEAD + f"    {binding}\n    self.args[x] = 1\n"
                change = Change("sqlglot/first.py", source, source.replace("] = 1", "] = 2"), {6}, {6})
                self.assertEqual(_gate([change]), "edit writes shared state")

    def test_local_body_edits_write_nothing(self):
        for old, new in (
            ("y = x + 1", "y = x + 2"),
            ("y = x", "y = [x, LIMIT]"),
            ("self.args['x'] = x", "self.args['x'] = None"),
            ("self.args.update(k=1)", "self.args.update(k=2)"),
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
