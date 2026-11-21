import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from coverage_map import Coverage
from patches import Change
import tc_replay as tc


ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
if not ROOT.is_dir():
    ROOT = Path(__file__).resolve().parents[1] / ".work" / "sqlglot"


def edit(path, old, new):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    line = source[:source.index(old)].count("\n") + 1
    return Change(path, source, source.replace(old, new), {line}, {line})


class TestTpcReplay(unittest.TestCase):
    def test_unknown_and_multifile_keep_candidates(self):
        chosen = {tc.EX + "execute_tpcds", "unrelated"}
        c = Change("sqlglot/planner.py", "", "")
        self.assertEqual(tc.refine_tc([c], chosen, None, chosen), chosen)
        self.assertEqual(tc.refine_tc([c, c], chosen, None, chosen), chosen)

    @unittest.skipUnless((ROOT / "sqlglot/parser.py").is_file(), "requires pinned source")
    def test_rejects_other_changes_on_same_line(self):
        c = edit("sqlglot/parser.py", "if not self._match(TokenType.ESCAPE):",
                 "if self._match(TokenType.STRAIGHT_JOIN):")
        self.assertIsNone(tc._supported(c, tc.profile()))
        c.structural = True
        self.assertIsNone(tc._supported(c, tc.profile()))

    @unittest.skipUnless(sys.platform == "linux" and ROOT.is_dir(), "requires Linux pinned checkout")
    def test_isolated_replay_and_breaking_backend_input(self):
        node = tc.EX + "optimized_tpch"
        chosen = {node, "unrelated"}
        cov = Coverage()
        safe = edit("sqlglot/generator.py", 'symmetric = expression.args.get("symmetric")',
                    'symmetric = expression.args.get("unused_symmetry")')
        broken = edit("sqlglot/generator.py", 'low = self.sql(expression, "low")',
                      'low = self.sql(expression, "absent_bound")')
        with patch.dict(os.environ, SQLGLOT_ROOT=str(ROOT)), patch.object(tc, "_wall_left", 20):
            self.assertEqual(tc.refine_tc([safe], chosen, cov, chosen), {"unrelated"})
            self.assertEqual(tc.refine_tc([broken], chosen, cov, chosen), chosen)
            self.assertEqual(tc.refine_tc([safe], chosen, cov, chosen), {"unrelated"})
            with patch.object(tc.subprocess, "run", side_effect=OSError):
                self.assertEqual(tc.refine_tc([safe], chosen, cov, chosen), chosen)


if __name__ == "__main__":
    unittest.main()
