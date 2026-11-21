import ast
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coverage_map import Coverage
from declarations import _pure_removed, _renderer_migration, _transform_table_edits, declaration_candidates

PINNED = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
SQLITE = "sqlglot/generators/sqlite.py"
POSTGRES = "sqlglot/generators/postgres.py"
SUPPORT = ("sqlglot/generator.py", "sqlglot/transforms.py", SQLITE, POSTGRES, "sqlglot/generators/redshift.py",
           "sqlglot/generators/materialize.py", "sqlglot/generators/risingwave.py")

RAND_ENTRY = "        exp.Rand: lambda self, e: self.func(\"RANDOM\", e.this),\n"
RAND_METHOD = (
    "    def rand_sql(self, expression: exp.Rand) -> str:\n"
    "        return self.func(\"RANDOM\", expression.this)\n\n"
)
INSERT_ANCHOR = "    def bitwiseandagg_sql(self, expression: exp.BitwiseAndAgg) -> str:\n"

# Genuine upstream bodies, verbatim from the c25, x08 and x21 projections.
C25_RAND = (
    "    def rand_sql(self, expression: exp.Rand) -> str:\n"
    "        random = self.func(\"RANDOM\", expression.this)\n"
    "        if any(expression.args.values()):\n"
    "            return random\n"
    "\n"
    "        # Keep 53 bits so even the largest draw is exactly representable and strictly below 1 after division.\n"
    "        return f\"(CAST({random} & 9007199254740991 AS REAL) / 9007199254740992)\"\n\n"
)
X08_FILTER = "            if k != exp.CommentColumnConstraint\n"
X08_ENTRY = "        exp.JSONObjectAgg: rename_func(\"JSON_OBJECT_AGG\"),\n"
X08_ANCHOR = "    def schemacommentproperty_sql(self, expression: exp.SchemaCommentProperty) -> str:\n"
X08_METHOD = (
    "    def jsonobjectagg_sql(self, expression: exp.JSONObjectAgg) -> str:\n"
    "        args = expression.expressions\n"
    "        pair = args[0] if len(args) == 1 else None\n"
    "        # Only the SQL/JSON spelling, JSON_OBJECTAGG, accepts these clauses.\n"
    "        if (\n"
    "            isinstance(pair, exp.FormatJson)\n"
    "            or expression.args.get(\"null_handling\")\n"
    "            or expression.args.get(\"unique_keys\") is not None\n"
    "            or expression.args.get(\"return_type\")\n"
    "            or expression.args.get(\"encoding\")\n"
    "        ):\n"
    "            return self._jsonobject_sql(expression)\n"
    "\n"
    "        if isinstance(pair, exp.JSONKeyValue):\n"
    "            args = [pair.this, pair.expression]\n"
    "        return self.func(\"JSON_OBJECT_AGG\", *args)\n\n"
)
X21_ENTRY = "        exp.CurrentTimestamp: lambda *_: \"CURRENT_TIMESTAMP\",\n"
X21_ANCHOR = "    def interval_sql(self, expression: exp.Interval) -> str:\n"
X21_TIME = (
    "    def currenttime_sql(self, expression: exp.CurrentTime) -> str:\n"
    "        precision = expression.this\n"
    "        return self.func(\"CURRENT_TIME\", precision) if precision else \"CURRENT_TIME\"\n\n"
)
X21_TIMESTAMP = (
    "    def currenttimestamp_sql(self, expression: exp.CurrentTimestamp) -> str:\n"
    "        # Other dialects can use this argument for a time zone instead of a precision.\n"
    "        precision = expression.this\n"
    "        if precision and precision.is_int:\n"
    "            return self.func(\"CURRENT_TIMESTAMP\", precision)\n"
    "        return \"CURRENT_TIMESTAMP\"\n\n"
)
# Fircrest's timestamp-deferred control (Linux: test 436 passes, then parser test 1527 fails).
DEFERRED_WRITE = ("        __import__(\"sqlglot.parser\", fromlist=(\"Parser\",)).Parser.FUNCTIONS"
                  ".setdefault(\"FOO\", exp.Abs.from_arg_list)\n")


@unittest.skipUnless((PINNED / SQLITE).is_file(), "requires the pinned SQLGlot checkout")
class RendererMigrationTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for rel in SUPPORT:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PINNED / rel, self.root / rel)
        (self.root / "tests").mkdir()
        self.cov = Coverage()

    def source(self, rel):
        return (PINNED / rel).read_text(encoding="utf-8")

    def check(self, rel, patched):
        return _renderer_migration(rel, ast.parse(self.source(rel)), ast.parse(patched), self.root, self.cov)

    def sqlite_rand(self, method=RAND_METHOD, entry_replacement=""):
        text = self.source(SQLITE)
        self.assertIn(RAND_ENTRY, text)
        self.assertIn(INSERT_ANCHOR, text)
        return text.replace(RAND_ENTRY, entry_replacement).replace(INSERT_ANCHOR, method + INSERT_ANCHOR)

    def test_table_entry_to_method_selects_dispatch_readers(self):
        result = self.check(SQLITE, self.sqlite_rand())
        self.assertIn("tests/dialects/test_sqlite.py::TestSQLite::test_random", result)
        self.assertIn("tests/dialects/test_dialect.py::TestDialect::test_random", result)
        self.assertLess(len(result), 20)

    def test_postgres_inherited_and_filtered_entry(self):
        text = self.source(POSTGRES)
        patched = text.replace("if k != exp.CommentColumnConstraint\n",
                               "if k not in (exp.CommentColumnConstraint, exp.JSONObjectAgg)\n")
        patched = patched.replace("        exp.JSONObjectAgg: rename_func(\"JSON_OBJECT_AGG\"),\n", "")
        patched = patched.replace(
            "    def schemacommentproperty_sql(",
            "    def jsonobjectagg_sql(self, expression: exp.JSONObjectAgg) -> str:\n"
            "        return self.func(\"JSON_OBJECT_AGG\", *expression.expressions)\n\n"
            "    def schemacommentproperty_sql(")
        self.assertNotEqual(patched, text)
        result = self.check(POSTGRES, patched)
        self.assertIn("tests/dialects/test_postgres.py::TestPostgres::test_postgres", result)
        self.assertLess(len(result), 20)

    def test_through_the_normal_entry(self):
        patched = self.sqlite_rand()
        original = self.source(SQLITE)
        removed = {n + 1 for n, line in enumerate(original.split("\n")) if line + "\n" == RAND_ENTRY}
        result = declaration_candidates(SQLITE, original, patched, removed, self.cov, single_change=True)
        self.assertIsNotNone(result)
        self.assertIn("tests/dialects/test_sqlite.py::TestSQLite::test_random", result)
        self.assertIsNone(declaration_candidates(SQLITE, original, patched, removed, self.cov, single_change=False))

    def test_unsafe_shapes_decline(self):
        controls = {
            "default with a side effect at class creation": RAND_METHOD.replace(
                "expression: exp.Rand)", "expression: exp.Rand, _probe=print(\"x\"))"),
            "decorator": "    @staticmethod\n" + RAND_METHOD,
            "private name": RAND_METHOD.replace("def rand_sql", "def _rand_sql"),
            "unknown expression key": RAND_METHOD.replace("def rand_sql", "def notanexpression_sql"),
            "connector key": RAND_METHOD.replace("def rand_sql", "def and_sql"),
            "extra parameter": RAND_METHOD.replace("expression: exp.Rand)", "expression: exp.Rand, extra)"),
        }
        for label, method in controls.items():
            with self.subTest(label):
                self.assertIsNone(self.check(SQLITE, self.sqlite_rand(method=method)))

    def test_table_edits_other_than_removal_decline(self):
        changed = RAND_ENTRY.replace("RANDOM", "RAND")
        added = RAND_ENTRY + "        exp.Left: lambda self, e: \"LEFT\",\n"
        for label, entry in {"changed value": changed, "added entry": added}.items():
            with self.subTest(label):
                self.assertIsNone(self.check(SQLITE, self.sqlite_rand(entry_replacement=entry)))
        with self.subTest("removed value evaluated by an unknown call"):
            old = ast.parse("{exp.Rand: make_renderer('RANDOM'), exp.Left: f}", mode="eval").body
            new = ast.parse("{exp.Left: f}", mode="eval").body
            self.assertIsNone(_transform_table_edits(old, new))
        with self.subTest("removed rename_func and lambda values are inert"):
            old = ast.parse("{exp.Rand: rename_func('RANDOM'), exp.Left: lambda self, e: 'L', exp.Right: f}",
                            mode="eval").body
            new = ast.parse("{exp.Right: f}", mode="eval").body
            self.assertEqual(_transform_table_edits(old, new), {"Rand", "Left"})

    def test_other_class_reading_the_table_declines(self):
        reader = self.root / "sqlglot" / "generators" / "copycat.py"
        reader.write_text("from sqlglot.generators.sqlite import SQLiteGenerator\n"
                          "class CopyCat:\n    TRANSFORMS = {**SQLiteGenerator.TRANSFORMS}\n")
        self.assertIsNone(self.check(SQLITE, self.sqlite_rand()))

    def test_direct_caller_in_an_unmapped_file_declines(self):
        caller = self.root / "tests" / "test_direct.py"
        caller.write_text("def render(generator, e):\n    return generator.rand_sql(e)\n")
        self.assertIsNone(self.check(SQLITE, self.sqlite_rand()))

    def test_audited_transform_must_match_its_digest(self):
        transforms = self.root / "sqlglot" / "transforms.py"
        text = transforms.read_text(encoding="utf-8")
        marker = "def eliminate_semi_and_anti_joins(expression: exp.Expr) -> exp.Expr:\n"
        self.assertIn(marker, text)
        transforms.write_text(text.replace(marker, marker + "    expression = expression.this\n"), encoding="utf-8")
        self.assertIsNone(self.check(SQLITE, self.sqlite_rand()))

    def test_migrated_class_returned_by_a_transform_declines(self):
        text = self.source(POSTGRES)
        patched = text.replace(
            "    def schemacommentproperty_sql(",
            "    def withingroup_sql(self, expression: exp.WithinGroup) -> str:\n"
            "        return \"\"\n\n"
            "    def schemacommentproperty_sql(")
        self.assertIsNone(self.check(POSTGRES, patched))

    # The genuine upstream bodies (c25, x08, x21) and the controls built on them.

    def postgres_clock(self, timestamp=X21_TIMESTAMP, time=X21_TIME):
        text = self.source(POSTGRES)
        self.assertIn(X21_ENTRY, text)
        self.assertIn(X21_ANCHOR, text)
        return text.replace(X21_ENTRY, "").replace(X21_ANCHOR, time + timestamp + X21_ANCHOR)

    def postgres_aggregate(self, method=X08_METHOD):
        text = self.source(POSTGRES)
        for old in (X08_FILTER, X08_ENTRY, X08_ANCHOR):
            self.assertIn(old, text)
        return (text.replace(X08_FILTER, X08_FILTER.replace("if k != exp.CommentColumnConstraint",
                                                            "if k not in (exp.CommentColumnConstraint, exp.JSONObjectAgg)"))
                .replace(X08_ENTRY, "").replace(X08_ANCHOR, method + X08_ANCHOR))

    def test_genuine_upstream_bodies_are_read_only(self):
        cases = {
            "c25 SQLite Rand": (SQLITE, self.sqlite_rand(method=C25_RAND),
                                "tests/dialects/test_sqlite.py::TestSQLite::test_random"),
            "x08 Postgres JSONObjectAgg": (POSTGRES, self.postgres_aggregate(),
                                           "tests/dialects/test_postgres.py::TestPostgres::test_postgres"),
            "x21 Postgres clock": (POSTGRES, self.postgres_clock(),
                                   "tests/dialects/test_postgres.py::TestPostgres::test_postgres"),
        }
        for label, (rel, patched, reader) in cases.items():
            with self.subTest(label):
                result = self.check(rel, patched)
                self.assertIsNotNone(result)
                self.assertIn(reader, result)

    def test_deferred_global_write_in_the_new_body_declines(self):
        # Fircrest's timestamp-deferred control, verbatim: rendering a Postgres timestamp registers FOO in the
        # shared Parser.FUNCTIONS, and parser test 1527 fails later in the suite.
        timestamp = X21_TIMESTAMP.replace("        return \"CURRENT_TIMESTAMP\"\n", DEFERRED_WRITE
                                          + "        return \"CURRENT_TIMESTAMP\"\n")
        self.assertIn("setdefault", timestamp)
        self.assertIsNone(self.check(POSTGRES, self.postgres_clock(timestamp=timestamp)))

    def test_class_definition_default_declines(self):
        # Fircrest's timestamp-default control: the same write happens once, when the class body is executed.
        timestamp = X21_TIMESTAMP.replace(
            "expression: exp.CurrentTimestamp)",
            "expression: exp.CurrentTimestamp, _h=__import__(\"sqlglot.parser\", fromlist=(\"Parser\",))"
            ".Parser.FUNCTIONS.setdefault(\"FOO\", exp.Abs.from_arg_list))")
        self.assertIsNone(self.check(POSTGRES, self.postgres_clock(timestamp=timestamp)))

    def test_material_writes_through_other_routes_decline(self):
        head = "        precision = expression.this\n"
        routes = {
            "mutator on a shared class table": "        exp.Abs.arg_types.update({\"probe\": False})\n",
            "mutator through the dialect": "        precision = self.dialect.parser_class.FUNCTIONS.pop(\"FOO\", None)\n",
            "mutator hidden in an expression": "        precision = self.TRANSFORMS.setdefault(exp.Abs, None) or precision\n",
            "mutator on the expression's args": "        precision = expression.args.setdefault(\"this\", precision)\n",
            "setattr": "        precision = setattr(exp, \"FOO\", precision)\n",
            "attribute store": "        type(self).TIMESTAMP = precision\n",
            "item store": "        exp.Abs.arg_types[\"probe\"] = False\n",
            "delete": "        del precision\n",
            "global": "        global FOO\n",
            "import": "        import sqlglot.parser\n",
            "instance helper outside the pinned set": "        self.unsupported(\"probe\")\n",
            "generator alias": "        generator = self\n        precision = generator.func(\"X\", precision)\n",
            "builtin rebound": "        len = self.func\n",
            "comprehension": "        precision = [arg for arg in expression.expressions] or precision\n",
            "walrus": "        if (probe := expression.this):\n            precision = probe\n",
            "loop": "        for arg in expression.expressions:\n            precision = arg\n",
            "call inside an f-string": "        precision = f\"{self.func('X')}\" or precision\n",
            "dunder read": "        precision = expression.__class__ or precision\n",
            "keyword to a helper": "        precision = self.func(\"X\", precision, suffix=\")\") or precision\n",
            "lambda": "        precision = (lambda: precision)() or precision\n",
        }
        for label, line in routes.items():
            with self.subTest(label):
                timestamp = X21_TIMESTAMP.replace(head, head + line)
                self.assertNotEqual(timestamp, X21_TIMESTAMP)
                self.assertIsNone(self.check(POSTGRES, self.postgres_clock(timestamp=timestamp)))

    def test_implicit_expr_behaviour_declines(self):
        # Reads that run Expr code: `==` hashes both trees and caches `_hash` on their nodes, formatting renders
        # through a dialect, indexing builds a Bracket, and a non-literal helper name is not the pinned shape.
        head = "        precision = expression.this\n"
        routes = {
            "expression equality": "        if precision == expression.this:\n            return \"X\"\n",
            "f-string of an expression": "        return f\"CURRENT_TIMESTAMP({precision})\"\n",
            "index into an expression": "        precision = expression.this[0]\n",
            "unpack a non-list": "        return self.func(\"CURRENT_TIMESTAMP\", *precision)\n",
            "computed function name": "        return self.func(precision, precision)\n",
        }
        for label, line in routes.items():
            with self.subTest(label):
                timestamp = X21_TIMESTAMP.replace(head, head + line)
                self.assertIsNone(self.check(POSTGRES, self.postgres_clock(timestamp=timestamp)))

    def test_removed_and_inherited_renderers_must_be_read_only(self):
        with self.subTest("removed lambda with a mutator"):
            old = ast.parse("{exp.Rand: lambda self, e: e.args.setdefault('x', 1), exp.Left: f}", mode="eval").body
            new = ast.parse("{exp.Left: f}", mode="eval").body
            scope = {"shadowed": frozenset(), "exp_classes": frozenset({"Rand", "Left"}), "rename_func": True,
                     "generator": True}
            self.assertEqual(_transform_table_edits(old, new), {"Rand"})
            self.assertIsNone(_transform_table_edits(old, new, pure=lambda v: _pure_removed(v, scope)))
        with self.subTest("removed read-only lambdas and rename_func"):
            old = ast.parse("{exp.Rand: lambda self, e: self.func('RANDOM', e.this), exp.Left: lambda *_: 'L',"
                            " exp.Right: rename_func('R'), exp.Abs: f}", mode="eval").body
            new = ast.parse("{exp.Abs: f}", mode="eval").body
            self.assertEqual(_transform_table_edits(old, new, pure=lambda v: _pure_removed(v, scope)),
                             {"Rand", "Left", "Right"})
        with self.subTest("excluded base renderer with a mutator"):
            base = self.root / "sqlglot" / "generator.py"
            text = base.read_text(encoding="utf-8")
            entry = "        exp.JSONObjectAgg: lambda self, e: self._jsonobject_sql(e),\n"
            self.assertIn(entry, text)
            base.write_text(text.replace(entry, "        exp.JSONObjectAgg: lambda self, e: e.args.pop('x', None)"
                                                " or self._jsonobject_sql(e),\n"), encoding="utf-8")
            self.assertIsNone(self.check(POSTGRES, self.postgres_aggregate()))

    def test_type_parameters_and_annotations(self):
        cases = {
            "class type parameter": (SQLITE, self.sqlite_rand(method=C25_RAND).replace(
                "class SQLiteGenerator(generator.Generator):", "class SQLiteGenerator[T](generator.Generator):")),
            "method type parameter": (SQLITE, self.sqlite_rand(method=C25_RAND.replace("def rand_sql(",
                                                                                     "def rand_sql[T]("))),
            "union parameter annotation": (SQLITE, self.sqlite_rand(method=C25_RAND.replace(
                "expression: exp.Rand)", "expression: exp.Rand | None)"))),
            "called parameter annotation": (SQLITE, self.sqlite_rand(method=C25_RAND.replace(
                "expression: exp.Rand)", "expression: print(\"probe\"))"))),
            "unknown expression annotation": (SQLITE, self.sqlite_rand(method=C25_RAND.replace(
                "expression: exp.Rand)", "expression: exp.NotAnExpression)"))),
            "annotated generator": (SQLITE, self.sqlite_rand(method=C25_RAND.replace(
                "(self, expression", "(self: \"SQLiteGenerator\", expression"))),
            "non-str return": (SQLITE, self.sqlite_rand(method=C25_RAND.replace(") -> str:", ") -> int:"))),
        }
        for label, (rel, patched) in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.check(rel, patched))
        with self.subTest("string annotations of the same shape are accepted"):
            quoted = C25_RAND.replace("expression: exp.Rand) -> str:", "expression: \"exp.Rand\") -> \"str\":")
            self.assertIsNotNone(self.check(SQLITE, self.sqlite_rand(method=quoted)))


if __name__ == "__main__":
    unittest.main()
