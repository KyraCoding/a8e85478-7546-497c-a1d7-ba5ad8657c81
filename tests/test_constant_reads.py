"""Immutable class-constant reads in generator callbacks (Flint-52, fresh x23).

Needs the pinned checkout at SQLGLOT_ROOT (default /opt/sqlglot).  Each case builds a real diff against it, as the
selector would read it, and asks generator_initialization_changed about the Postgres generator file.
"""
import difflib
import os
import unittest
from pathlib import Path
from unittest import mock

import semantics
from patches import read_changes

ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
POSTGRES = "sqlglot/generators/postgres.py"
REDSHIFT = "sqlglot/generators/redshift.py"

POSTGRES_EDITS = [
    ('    VARIANCE_NAME = "VAR_SAMP"\n',
     '    VARIANCE_NAME = "VAR_SAMP"\n    DATE_PART_WITH_EXPRESSION = "DATE_PART"\n'),
    ("    def bytestring_sql(self, expression: exp.ByteString) -> str:\n",
     "    def extract_sql(self, expression: exp.Extract) -> str:\n"
     "        if not expression.args.get(\"date_part\"):\n"
     "            return super().extract_sql(expression)\n"
     "\n"
     "        part = expression.this\n"
     "        name = self.DATE_PART_WITH_EXPRESSION\n"
     "        if isinstance(part, exp.Var):\n"
     "            name = \"DATE_PART\"\n"
     "            part = exp.Literal.string(part.name.lower())\n"
     "        return self.func(name, part, expression.expression)\n"
     "\n"
     "    def bytestring_sql(self, expression: exp.ByteString) -> str:\n"),
]
REDSHIFT_ANCHOR = "class RedshiftGenerator(PostgresGenerator):\n"


def diff(path, edits):
    old = (ROOT / path).read_text(encoding="utf-8")
    new = old
    for anchor, text in edits:
        assert new.count(anchor) == 1, (path, anchor)
        new = new.replace(anchor, text)
    body = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n{body}"


def decide(redshift_edits=None, extra_edits=None):
    text = diff(POSTGRES, POSTGRES_EDITS + (extra_edits or []))
    if redshift_edits is not None:
        text += diff(REDSHIFT, redshift_edits)
    changes = read_changes(text, ROOT)
    (change,) = [c for c in changes if c.path == POSTGRES]
    return semantics.generator_initialization_changed(change, changes), semantics.generator_initialization_changed(change)


def override(line, after=""):
    return [(REDSHIFT_ANCHOR, REDSHIFT_ANCHOR + line)] + ([(after[0], after[1])] if after else [])


@unittest.skipUnless((ROOT / POSTGRES).is_file(), "needs the pinned SQLGlot checkout")
class ConstantReadTest(unittest.TestCase):
    def test_x23_narrows_only_with_the_patch_context(self):
        proven, legacy = decide(override('    DATE_PART_WITH_EXPRESSION = "PGDATE_PART"\n'))
        self.assertFalse(proven)
        self.assertTrue(legacy)

    def test_postgres_alone_narrows(self):
        # Materialize, RisingWave and Redshift inherit the Postgres literal by MRO.
        self.assertFalse(decide()[0])

    def test_false_friends_fall_back(self):
        cases = {
            "property override": override('    DATE_PART_WITH_EXPRESSION = property(lambda self: "PGDATE_PART")\n'),
            "list override": override('    DATE_PART_WITH_EXPRESSION = ["PGDATE_PART"]\n'),
            "method override": override("    def DATE_PART_WITH_EXPRESSION(self):\n        return 'PGDATE_PART'\n\n"),
            "reassigned after the class": override(
                '    DATE_PART_WITH_EXPRESSION = "PGDATE_PART"\n',
                ("    LOCKING_READS_SUPPORTED = False\n",
                 "    LOCKING_READS_SUPPORTED = False\n    DATE_PART_WITH_EXPRESSION = []\n")),
            "setattr": override(
                "    def reset(self):\n        setattr(self, 'DATE_PART_WITH_EXPRESSION', [])\n\n"),
            "lookup hook": override("    def __getattr__(self, name):\n        return []\n\n"),
            "instance store": override(
                "    def reset(self):\n        self.DATE_PART_WITH_EXPRESSION = []\n\n"),
            "mutable subclass": [(REDSHIFT_ANCHOR,
                                  "class OtherGenerator(PostgresGenerator):\n"
                                  "    DATE_PART_WITH_EXPRESSION = []\n\n\n" + REDSHIFT_ANCHOR)],
        }
        for name, edits in cases.items():
            with self.subTest(name):
                self.assertTrue(decide(edits)[0], name)

    def test_false_friend_needs_the_fallback(self):
        # With the proof forced to succeed, the list override would be narrowed: the fallback is what keeps it.
        edits = override('    DATE_PART_WITH_EXPRESSION = ["PGDATE_PART"]\n')
        self.assertTrue(decide(edits)[0])
        with mock.patch.object(semantics._ConstantRead, "_prove", lambda self, receiver, attr: None):
            self.assertFalse(decide(edits)[0])


if __name__ == "__main__":
    unittest.main()
