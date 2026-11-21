import ast
import gc
import unittest
import weakref

from patches import Change
from semantics import _dump_key, _patched, generator_initialization_changed, patched_code, semantic_lines


class SemanticLinesTests(unittest.TestCase):
    def change(self, source, before, after):
        return semantic_lines("example.py", source, source.replace(before, after), set())

    def test_statement_alignment_keys(self):
        tree = ast.parse("def f(x=None):\n    return [x, 'é']\n")
        self.assertEqual(_dump_key(tree), ast.dump(tree))
        class Custom(ast.AST):
            _fields = ["value"]
        node = Custom()
        node.value = 1
        self.assertEqual(_dump_key(node), ast.dump(node))

    def test_multiline_boolean(self):
        src = "def f(a, b):\n    if (\n        a\n        and b\n    ):\n        return 1\n"
        self.assertEqual(self.change(src, "and b", "or b"), {3})

    def test_insert_else(self):
        src = "def f(a):\n    if a:\n        g()\n    return 1\n"
        self.assertEqual(self.change(src, "    return", "    else:\n        h()\n    return"), {2})

    def test_lambda_inside_declaration(self):
        src = "class A:\n    F = lambda x: (\n        x + 1\n    )\n"
        self.assertEqual(self.change(src, "x + 1", "x + 2"), {3})

    def test_metadata_and_scoping(self):
        src = 'def f(a):\n    "doc"\n    if a:\n        g()\n    return x\n'
        self.assertIsNone(self.change(src, '"doc"', '"note"'))
        self.assertIsNone(self.change(src, "g()", "x = 0"))
        self.assertIsNone(self.change(src, "g()", "yield 0"))

    def test_compiler_equivalence(self):
        self.assertEqual(self.change("def f():\n    return 3\n", "3", "(1 + 2)"), set())
        self.assertEqual(self.change("def f():\n    return 1\n", "    return", "    # note\n    return"), set())

    def test_encoding_cookie(self):
        src = "# coding: utf-8\ndef f():\n    return 'café'\n"
        self.assertIsNone(self.change(src, "utf-8", "ascii"))
        self.assertEqual(self.change(src, "utf-8", "latin-1"), {3})

    def test_invalid_compile_does_not_keep_input_after_eviction(self):
        class Source(str):
            pass

        def invalid(i):
            source = Source(f"x = {i}\nreturn x\n")
            try:
                patched_code("example.py", source)
            except SyntaxError:
                return weakref.ref(source)
            self.fail("expected a module-level return to fail compilation")

        enabled = gc.isenabled()
        gc.disable()
        try:
            refs = [invalid(i) for i in range(12)]
            _patched.cache_clear()
            self.assertTrue(all(ref() is None for ref in refs))
        finally:
            if enabled:
                gc.enable()


class GeneratorDefaultsTests(unittest.TestCase):
    path = "sqlglot/generators/example.py"
    original = "class R:\n    def render(self, x=1):\n        return x\n"
    eager = original.replace("x=1", "x=install(\n        1\n    )")

    def test_added_removed_and_modified_initializer(self):
        for before, after in ((self.original, self.eager), (self.eager, self.original),
                              (self.eager, self.eager.replace("1", "2"))):
            with self.subTest(after=after):
                self.assertTrue(generator_initialization_changed(Change(self.path, before, after)))
        self.assertTrue(generator_initialization_changed(Change(self.path, "", self.eager, structural=True)))

    def test_unchanged_initializer_and_method_body(self):
        self.assertFalse(generator_initialization_changed(Change(self.path, self.eager, self.eager.replace("return x", "return fn(x)"))))
        self.assertFalse(generator_initialization_changed(Change(self.path, self.original, self.original.replace("x=1", "x=2"))))

    def test_decorator_and_condition_evaluate_at_definition(self):
        decorated = self.original.replace("    def", "    @install\n    def")
        self.assertTrue(generator_initialization_changed(Change(self.path, self.original, decorated)))
        guarded = "if False:\n" + "\n".join("    " + line for line in self.eager.splitlines()) + "\n"
        self.assertTrue(generator_initialization_changed(Change(self.path, guarded, guarded.replace("False", "True"))))

    def test_immediate_statements_and_delayed_lambda(self):
        for statement in ("install()", "marker = setattr(T, 'key', 1)", "registry[key] = 1",
                          "if registry.setdefault('key', 1): pass"):
            after = self.original.replace("    def", "    " + statement + "\n    def")
            with self.subTest(statement=statement):
                self.assertTrue(generator_initialization_changed(Change(self.path, self.original, after)))
        after = self.original.replace("    def", "    marker = lambda: install()\n    def")
        self.assertFalse(generator_initialization_changed(Change(self.path, self.original, after)))

    def test_deferred_factory_and_explicit_initializer_write(self):
        pure = self.original.replace("    def", "    TABLE = {str: factory('x')}\n    def")
        self.assertFalse(generator_initialization_changed(Change(self.path, self.original, pure)))
        mutating = pure.replace("factory('x')", "registry.update({'x': str})")
        self.assertTrue(generator_initialization_changed(Change(self.path, pure, mutating)))

    def test_deferred_write_and_class_setter_alias(self):
        body = self.original.replace("return x", "setattr(expr.Type, 'flag', x)\n        return x")
        self.assertTrue(generator_initialization_changed(Change(self.path, self.original, body, {3}, {3, 4})))
        source = self.original.replace("    def", "    setter = setattr\n    def")
        altered = source.replace("    def", "    marker = (setter(expr.Type, 'flag', 1),)\n    def")
        self.assertTrue(generator_initialization_changed(Change(self.path, source, altered, set(), {3})))
        added = source + "\nUNRELATED = 1\n"
        self.assertFalse(generator_initialization_changed(Change(self.path, source, added, set(), {6})))

    def test_shared_registry_and_local_collection(self):
        for target, expected in (("self.dialect.FUNCTIONS", True), ("scratch", False)):
            body = self.original.replace("return x", f"{target}.update(x)\n        return x")
            self.assertEqual(generator_initialization_changed(Change(self.path, self.original, body, {3}, {3, 4})), expected)


class GeneratorInertCallTests(unittest.TestCase):
    path = "sqlglot/generators/example.py"
    original = ("from sqlglot.generator import Generator, unsupported_args\n\n"
                "_CHARS = frozenset('ab')\n\n\n"
                "class R(Generator):\n"
                "    KEYS = tuple(['x'])\n\n"
                "    def render(self, x):\n"
                "        return x\n")
    decorated = original.replace("    def render", "    @unsupported_args('options', ('mode', 'note'))\n    def render")
    write = "setattr(Generator, 'flag', 1)"

    def gate(self, after, original=None, extra=(), removed=(), added=()):
        change = Change(self.path, self.original if original is None else original, after, set(removed), set(added))
        return generator_initialization_changed(change), generator_initialization_changed(change, [change, *extra])

    def test_inert_calls_keep_the_normal_selection(self):
        for after in (self.original.replace("frozenset('ab')", "frozenset('abc')"),
                      self.original.replace("tuple(['x'])", "tuple(['x', 'y'])"),
                      self.original.replace("\n\n\nclass", "\n_MORE: frozenset = set(['c'])\nset()\n\n\nclass"),
                      self.original.replace("_CHARS =", "_LETTERS ="),
                      self.decorated,
                      self.decorated.replace("    def render", "    async def render")):
            with self.subTest(after=after):
                self.assertEqual(self.gate(after), (True, False))

    def test_shadowed_or_evaluating_calls_stay_eager(self):
        literal = self.original.replace("frozenset('ab')", "frozenset('abc')")
        cases = {
            "evaluated argument": self.original.replace("frozenset('ab')", f"frozenset({self.write} or 'ab')"),
            "set comprehension": self.original.replace("frozenset('ab')", "frozenset({c for c in 'ab'})"),
            "rebound constructor": literal.replace("_CHARS =", "frozenset = lambda *a: set()\n_CHARS ="),
            "star import": literal.replace("\n\n_CHARS", "\nfrom sqlglot.helper import *\n\n_CHARS"),
            "except as": literal.replace("_CHARS =", "try:\n    pass\nexcept Exception as frozenset:\n    pass\n_CHARS ="),
            "call in annotation": literal.replace("_CHARS =", f"_CHARS: {self.write} ="),
            "next to a write": literal.replace("_CHARS =", f"{self.write}\n_CHARS ="),
            "aliased factory": self.decorated.replace("unsupported_args\n", "unsupported_args as ua\n")
                                             .replace("@unsupported_args(", "@ua("),
            "rebound factory": self.decorated.replace("\n\n_CHARS", "\nunsupported_args = lambda *a: (lambda f: f)\n_CHARS"),
            "evaluated factory argument": self.decorated.replace("'options',", f"{self.write} or 'options',"),
            "class decorator": self.original.replace("class R", "@unsupported_args('options')\nclass R"),
            "retargeted to a shared attribute": self.original.replace("_CHARS =", "Generator.CHARS ="),
        }
        for name, after in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.gate(after), (True, True))
        foreign = self.original.replace("_CHARS = frozenset('ab')", "Generator.CHARS = frozenset('ab')")
        self.assertEqual(self.gate(foreign.replace("'ab'", "'abc'"), original=foreign), (True, True))

    def test_deferred_writes_still_decide_after_inert_effects(self):
        for target, expected in (("setattr(Generator, 'flag', x)", True), ("self.dialect.FUNCTIONS.update(x)", True),
                                 ("scratch.update(x)", False)):
            after = self.decorated.replace("        return x", f"        {target}\n        return x")
            for lines in ({}, {"added": {9, 11}}):
                with self.subTest(target=target, lines=lines):
                    self.assertEqual(self.gate(after, **lines), (True, expected))

    def test_patch_wide_conditions(self):
        literal = self.original.replace("frozenset('ab')", "frozenset('abc')")
        helper = Change("sqlglot/helper.py", "x = 1\n", "import builtins\nx = 1\n")
        factory = Change("sqlglot/generator.py", "x = 1\n", "x = 2\n")
        monkeypatch = Change("sqlglot/helper.py", "x = 1\n", "x = 1\ngenerator.unsupported_args = None\n")
        moved = Change("sqlglot/other.py", "", "", structural=True)
        self.assertEqual(self.gate(literal, extra=[helper]), (True, True))
        self.assertEqual(self.gate(literal, extra=[factory]), (True, False))
        self.assertEqual(self.gate(literal, extra=[moved]), (True, True))
        for extra in (factory, monkeypatch):
            with self.subTest(extra=extra.path):
                self.assertEqual(self.gate(self.decorated, extra=[extra]), (True, True))
        self.assertEqual(self.gate(self.decorated, extra=[helper]), (True, False))
