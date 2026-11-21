import ast
import gc
import unittest
import weakref

from semantics import _dump_key, _patched, patched_code, semantic_lines


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
