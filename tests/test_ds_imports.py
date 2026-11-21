import unittest
from unittest.mock import patch

from ds_imports import import_effects, new_or_changed_functions

TABLE = "sqlglot/executor/table.py"
BASE = (
    "from __future__ import annotations\n\nimport typing as t\n\nfrom sqlglot.helper import dict_depth\n\n\n"
    "class Table:\n    def __init__(self, columns=None, rows=None, column_range=None):\n"
    "        self.columns = tuple(columns or ())\n        self.rows = rows or []\n\n\n"
    "class Tokenizer:\n    KEYWORDS = {'A': 1, 'B': 2}\n    KEYWORDS.pop('B')\n"
)
REDUCE = "\n\ndef _reduce(table):\n    return Table, (table.rows, table.columns)\n"


def effects(extra, original=BASE, sources=None):
    return import_effects(TABLE, original, original + extra, sources)


class RegistrationTests(unittest.TestCase):
    def test_frozen_pool_reducer_control(self):
        reasons = effects(REDUCE + "\n\nfrom multiprocessing.reduction import ForkingPickler\n"
                                   "ForkingPickler.register(Table, _reduce)\n")
        self.assertIn("calls multiprocessing.reduction.ForkingPickler.register", reasons)
        self.assertIn("imports multiprocessing.reduction.ForkingPickler", reasons)

    def test_other_spellings_of_a_registration(self):
        spellings = {
            "from multiprocessing import reduction as r\nr.ForkingPickler.register(Table, _reduce)\n":
                "calls multiprocessing.reduction.ForkingPickler.register",
            "import copyreg\n_ = copyreg.pickle(Table, _reduce)\n": "calls copyreg.pickle",
            "import copyreg\ncopyreg.dispatch_table[Table] = _reduce\n": "assigns into copyreg.dispatch_table",
            "import copyreg\nregister = copyreg.pickle\nregister(Table, _reduce)\n": "calls copyreg.pickle",
            "setattr(Table, '__reduce__', _reduce)\n": "calls builtin setattr",
            "_ = __import__('copyreg').pickle(Table, _reduce)\n": "calls builtin __import__",
            "import sys\nsys.modules['copyreg'].pickle(Table, _reduce)\n": "calls sys.modules[].pickle",
            "def _register():\n    import copyreg\n    copyreg.pickle(Table, _reduce)\n\n\n_register()\n":
                "calls new or changed function _register at import",
            "class R:\n    import copyreg\n    copyreg.pickle(Table, _reduce)\n": "calls copyreg.pickle",
            "import os\nos.environ['X'] = '1'\n": "assigns into os.environ",
            "import logging\nlogging.getLogger('sqlglot').addHandler(None)\n":
                "calls logging.getLogger().addHandler",
        }
        for text, expected in spellings.items():
            with self.subTest(text=text):
                self.assertIn(expected, effects(REDUCE + "\n\n" + text))

    def test_frozen_poolcopy_control(self):
        text = REDUCE + (
            "\n\nimport sys as _pool_modules_source\n\n"
            "if \"multiprocessing.reduction\" in _pool_modules_source.modules:\n"
            "    type.__setattr__(\n"
            "        _pool_modules_source.modules[\"multiprocessing.reduction\"].ForkingPickler,\n"
            "        \"_copyreg_dispatch_table\",\n"
            "        {\n"
            "            **_pool_modules_source.modules[\"multiprocessing.reduction\"].ForkingPickler._copyreg_dispatch_table,\n"
            "            Table: _reduce,\n"
            "        },\n"
            "    )\n")
        reasons = effects(text)
        self.assertIn("calls builtin type.__setattr__", reasons)
        self.assertIn("references sys.modules", reasons)

    def test_outside_objects_handed_to_harmless_calls(self):
        spellings = {
            "import functools, sys\nfunctools.partial(sys.modules['copyreg'].pickle, Table)(_reduce)\n":
                "references sys.modules[].pickle",
            "_ = list(map(setattr, [Table], ['__reduce__'], [_reduce]))\n": "references setattr",
            "object.__setattr__(Table, '__reduce__', _reduce)\n": "calls builtin object.__setattr__",
            "dict.__setitem__(Table.__dict__, 'x', 1)\n": "calls builtin dict.__setitem__",
            "import sys\n\n\n@sys.modules['atexit'].register\ndef _bye():\n    pass\n": "calls sys.modules[].register",
            "X = Table.__init__.__globals__\n": "uses __globals__",
            "import sys\nREDUCTION = sys.modules.get('multiprocessing.reduction')\n": "calls sys.modules.get",
            "def _deco(f):\n    return f\n\n\n@_deco\ndef _g():\n    pass\n":
                "calls new or changed function _deco at import",
        }
        for text, expected in spellings.items():
            with self.subTest(text=text):
                self.assertIn(expected, effects(REDUCE + "\n\n" + text))

    def test_names_reached_through_another_sqlglot_module(self):
        helper = "import copyreg\n\n\ndef dict_depth(d):\n    return 1\n"
        reasons = effects(REDUCE + "\n\nfrom sqlglot import helper\nhelper.copyreg.pickle(Table, _reduce)\n",
                          sources={"sqlglot/helper.py": helper, TABLE: ""})
        self.assertIn("calls copyreg.pickle", reasons)

    def test_removed_effects_are_reported(self):
        original = BASE + "\n\nimport copyreg\ncopyreg.pickle(Table, lambda t: (Table, ()))\n"
        reasons = import_effects(TABLE, original, BASE + "\n\nimport copyreg\n")
        self.assertIn("removed: calls copyreg.pickle", reasons)


class ModeledEditTests(unittest.TestCase):
    def test_definitions_and_sqlglot_state(self):
        self.assertEqual(effects(REDUCE), [])
        self.assertEqual(effects("\n\nTable.__reduce__ = _reduce\n" + REDUCE), [])  # compared as a special method
        self.assertEqual(import_effects(TABLE, BASE, BASE.replace("KEYWORDS.pop('B')", "KEYWORDS.pop('A')")), [])
        self.assertEqual(import_effects(TABLE, BASE, BASE.replace("'B': 2}", "'B': 2, 'C': 3}")), [])
        self.assertEqual(effects("\n\nPATTERN = __import__ and None\n"), ["references __import__"])

    def test_pure_library_calls_and_imports(self):
        allowed = [
            "import re\nPATTERN = re.compile('x')\nMATCHED = PATTERN.match('x')\n",
            "import logging\nlogger = logging.getLogger('sqlglot')\n",
            "from functools import lru_cache\n\n\n@lru_cache(maxsize=None)\ndef cached(x):\n    return x\n",
            "if t.TYPE_CHECKING:\n    from multiprocessing.reduction import ForkingPickler\n",
            "WIDTHS = sorted({len(k) for k in ('a', 'bb')})\nJOINED = ', '.join(['a', 'b']).upper()\n",
            "DEPTH = dict_depth({'a': {'b': 1}})\n",
            "import logging\nlog = logging.getLogger(__name__)\nLEVEL = logging.DEBUG\n",
            "import sys\nNEW = sys.version_info >= (3, 12)\n",
            "ALIAS = dict[str, int]\nNAMES = {k: v for k, v in {'a': 1}.items() if v}\n",
            "try:\n    import re\nexcept ImportError:\n    re = None\n",
            "@property\ndef _width(self):\n    return 1\n",
            "if len(Tokenizer.KEYWORDS) > 1:\n    Tokenizer.KEYWORDS.pop('A')\n",
            "class Sub(Table):\n    NAME = __qualname__\n",
        ]
        for text in allowed:
            with self.subTest(text=text):
                self.assertEqual(effects("\n\n" + text), [])

    def test_star_imported_names(self):
        star = BASE.replace("import typing as t\n", "import typing as t\nfrom sqlglot.helper import *\n")
        self.assertEqual(import_effects(TABLE, star, star + "\n\nDEPTH = dict_depth({})\nCOPY = seq_get([1], 0)\n"), [])
        outside = BASE.replace("import typing as t\n", "import typing as t\nfrom copyreg import *\n")
        self.assertIn("calls copyreg.pickle", import_effects(TABLE, outside, outside + "\n\npickle(Table, None)\n"))

    def test_body_only_edits_skip_the_syntax_tree(self):
        body = BASE.replace("tuple(columns or ())", "tuple(columns or [])")
        with patch("ds_imports._tree_of", side_effect=AssertionError("tree built")):
            self.assertEqual(import_effects(TABLE, BASE, body), [])
        default = BASE.replace("columns=None, rows", "columns=(), rows")
        with patch("ds_imports._tree_of", side_effect=AssertionError("tree built")):
            self.assertRaises(AssertionError, import_effects, TABLE, BASE, default)
        self.assertEqual(import_effects(TABLE, BASE, default), [])

    def test_new_functions_called_at_import_are_refused(self):
        text = "\n\ndef _builder(kind):\n    def inner(args):\n        return kind(args)\n    return inner\n\n\n" \
               "BUILT = {'x': _builder(list)}\n"
        self.assertEqual(effects(text), ["calls new or changed function _builder at import"])


class ChangedFunctionTests(unittest.TestCase):
    def test_new_and_changed_functions_positions_ignored(self):
        self.assertEqual(new_or_changed_functions(TABLE, BASE, "\n\n" + BASE), set())
        self.assertEqual(new_or_changed_functions(TABLE, BASE, BASE + REDUCE), {"_reduce"})
        changed = BASE.replace("tuple(columns or ())", "tuple(columns or [])")
        self.assertEqual(new_or_changed_functions(TABLE, BASE, changed), {"Table.__init__"})
        lam = BASE + "\nF = lambda: 1\n"
        self.assertEqual(new_or_changed_functions(TABLE, lam, lam.replace("lambda: 1", "lambda: 2")), {"<lambda>"})


if __name__ == "__main__":
    unittest.main()
