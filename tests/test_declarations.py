import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from coverage_map import Coverage
from declarations import _declaration_trees, _entry_edits, _private_transform_helper, declaration_candidates


class DeclarationEditsTest(unittest.TestCase):
    def edits(self, old, new):
        def expr(text):
            return ast.parse(text, mode="eval").body
        return _entry_edits(expr(old), expr(new), {"TokenType": ("A", "B")})

    def test_renamed_dispatch_with_spread(self):
        self.assertEqual(
            self.edits('{**Base.F, "A": make}', '{**Base.F, "B": make}'),
            ({"str:A", "str:B"}, True),
        )

    def test_set_membership_with_inherited_items(self):
        self.assertEqual(self.edits("Base.S | {TokenType.A}", "Base.S | {TokenType.B}"),
                         ({"member:A", "member:B"}, True))

    def test_changed_enum_value_retains_key(self):
        self.assertEqual(self.edits('{"K": TokenType.A}', '{"K": TokenType.B}'), ({"str:K"}, False))

    def test_reject_unresolved_or_eager_expressions(self):
        for old, new in [
            ('{"K": TokenType.A}', '{"K": TokenType.UNKNOWN}'),
            ('{"K": 1}', '{"K": 1, "A": make(2)}'),
            ('{"K": lambda args: args}', '{"K": lambda args: args, "A": args}'),
            ('{"K": 1, "B": 2}', '{"B": 2, "K": 1}'),
            ('{"K": 1}', '{"K": 1, "K": 2}'),
        ]:
            with self.subTest(new=new):
                self.assertIsNone(self.edits(old, new))


class PrivateTransformHelperTest(unittest.TestCase):
    path = "sqlglot/generators/postgres.py"
    before = ("from __future__ import annotations\n"
              "class PostgresGenerator:\n    TRANSFORMS = {exp.Select: previous}\n")
    fn = 'def _test_renderer(self, expression):\n    return "rendered"\n'

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "tests").mkdir()
        module = self.root / self.path
        module.parent.mkdir(parents=True)
        module.write_text(self.before)

    def revised(self, function=None):
        revised = self.before.replace("class Postgres", (function or self.fn) + "class Postgres")
        return revised.replace("previous}", "previous, exp.Left: _test_renderer}")

    def check(self, source):
        return _private_transform_helper(self.path, ast.parse(self.before), ast.parse(source), self.root, Coverage())

    def test_private_handler_reaches_the_profile(self):
        result = self.check(self.revised())
        self.assertIn("tests/dialects/test_bigquery.py::TestBigQuery::test_bigquery", result)
        self.assertLess(len(result), 100)

    def test_other_observable_changes_fall_back(self):
        controls = [
            self.revised(self.fn.replace("self, expression", "self, expression=warm()")),
            self.revised("@decorate\n" + self.fn),
            self.revised(self.fn.replace('return "rendered"', "global latch\n    latch = True")),
            self.revised(self.fn.replace('return "rendered"', "return self.__dict__")),
            self.revised().replace("previous,", "other,"),
            self.revised().replace("exp.Left:", "exp.UnknownRendererType:"),
            self.revised().replace("exp.Left:", "exp.EXPR_CLASSES:"),
            self.revised().replace("exp.Left: _test_renderer", "exp.Left: _test_renderer(self, x)"),
            self.revised().replace("_test_renderer", "public_renderer"),
            self.revised().replace("annotations", "generator_stop"),
            self.revised() + "changed = True\n",
            self.revised().replace(self.fn, "") + self.fn,
        ]
        for source in controls:
            with self.subTest(source=source):
                self.assertIsNone(self.check(source))

    def test_existing_external_reference_falls_back(self):
        (self.root / "tests" / "test_external.py").write_text("assert not hasattr(pg, '_test_renderer')\n")
        self.assertIsNone(self.check(self.revised()))

    def test_projection_preserves_cached_trees_on_success_and_fallback(self):
        for source in (self.revised(), self.revised() + "changed = True\n"):
            with self.subTest(source=source), patch.dict("os.environ", SQLGLOT_ROOT=str(self.root)):
                before, after = _declaration_trees(self.path, self.before, source)
                snapshots = ast.dump(before), ast.dump(after)
                declaration_candidates(self.path, self.before, source, set(), Coverage(), single_change=True)
                self.assertEqual((ast.dump(before), ast.dump(after)), snapshots)
