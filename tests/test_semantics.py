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
