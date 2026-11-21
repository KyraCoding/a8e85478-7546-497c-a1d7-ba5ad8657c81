import hashlib
import unittest

from expression_additions import _eligible
from patches import Change


CORE = "from sqlglot.expressions.core import Expression, Func\n"
DECL = "class FreshShape(Expression, Func):\n    arg_types = {'this': True}\n"
TYPES = """EXPRESSION_METADATA = {
    **{t: {"returns": exp.DType.DOUBLE} for t in {exp.Sec}},
}
"""


class ExpressionAdditionTests(unittest.TestCase):
    def shape(self, expression, typing=None, functions=(), reserved=()):
        expr = Change("sqlglot/expressions/string.py", CORE, expression)
        types = Change("sqlglot/typing/duckdb.py", TYPES, typing)
        profile = {"modules": [expr.path], "typing": [types.path], "functions": functions,
                   "reserved": reserved, "hashes": {c.path: hashlib.sha256(c.original.encode()).hexdigest()
                                                     for c in (expr, types)}}
        return _eligible([expr, types] if typing else [expr], profile)

    def test_pure_new_class_and_optional_typed_key(self):
        expected = ({"FreshShape"}, {"FRESH_SHAPE"})
        self.assertEqual(self.shape(CORE + DECL), expected)
        self.assertEqual(self.shape(CORE + DECL, TYPES.replace("exp.Sec", "exp.FreshShape, exp.Sec")), expected)

    def test_early_binding_and_registry_collision(self):
        self.assertIsNone(self.shape(DECL + CORE))
        self.assertIsNone(self.shape(CORE + DECL, functions=["FRESH_SHAPE"]))
        self.assertIsNone(self.shape(CORE + DECL, reserved=["freshshape"]))

    def test_opaque_class_creation_and_old_typed_keys(self):
        for expr in (CORE + "@register\n" + DECL, CORE + DECL + "    extra = call()\n",
                     CORE + DECL.replace("True", "default()"), CORE + DECL + DECL):
            with self.subTest(expr=expr):
                self.assertIsNone(self.shape(expr))
        for typed in (TYPES.replace("exp.Sec", "exp.FreshShape"), TYPES.replace("exp.Sec", "exp.Sec, exp.Other"),
                      TYPES.replace("exp.Sec", "exp.Sec, exp.FreshShape").replace("DOUBLE", "INT")):
            with self.subTest(typed=typed):
                self.assertIsNone(self.shape(CORE + DECL, typed))
