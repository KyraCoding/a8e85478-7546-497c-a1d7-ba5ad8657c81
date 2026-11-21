import ast
import unittest

from declarations import _entry_edits


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
