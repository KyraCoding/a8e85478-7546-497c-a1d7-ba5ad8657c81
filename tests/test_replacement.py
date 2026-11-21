import unittest

from fe_fence import single_replacement
from patches import Change


class ReplacementTests(unittest.TestCase):
    def test_single_operand(self):
        for old, new in (
            ("a is b", "a == b"),
            ("a is not b", "a != b"),
            ("a and b", "a or b"),
            ("f(x, 0)", "f(x, 1)"),
            ("f(x, False)", "f(x, True)"),
        ):
            with self.subTest(old=old, new=new):
                change = Change("module.py", old, new, {1}, {1})
                self.assertTrue(single_replacement(change))

    def test_extra_operations(self):
        for new in ("f(x, g())", "f(x, 1#rest)", "f(y, 1)", "f(x, 1) or g()"):
            with self.subTest(new=new):
                change = Change("module.py", "f(x, 0)", new, {1}, {1})
                self.assertFalse(single_replacement(change))
