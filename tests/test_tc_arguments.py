import gzip
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tc_replay as tc
import build_tc_arguments as builder
from build_tc_arguments import install
from patches import Change


class TestArgumentReads(unittest.TestCase):
    def test_union_requires_matching_real_and_independent_observations(self):
        source = "parser"
        calls = [["parse", str(i), None, {}] for i in range(4)]
        rows = [[call, tc.digest("ok"), []] for call in calls]
        actual = {json.dumps(call, sort_keys=True): {"a"} for call in calls[:3]}
        actual[json.dumps(calls[2], sort_keys=True)] = None
        touched = set()

        def replay(call):
            touched.add("b")
            return "different" if call == calls[1] else "ok"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parse.gz"
            arg = Path(directory) / "arguments.gz"
            baseline = {"source": tc.digest(source), "rows": rows, "tests": {}, "outside": {}}
            path.write_bytes(gzip.compress(json.dumps(baseline).encode()))
            with (patch.object(builder, "PARSE_MAP", path), patch.object(builder, "ARG_MAP", arg),
                  patch.object(builder, "replay_parse", side_effect=replay)):
                builder.save_arguments(source, rows, {}, {}, ["a", "b"], actual, touched)
            data = json.loads(gzip.decompress(arg.read_bytes()))
            self.assertIs(data["complete"], True)
            self.assertEqual(data["reads"], [["a", "b"], None, None, None])

    def test_forwarding_aliases_and_instance_override(self):
        source = ("class Parser:\n"
                  "    def run(self):\n"
                  "        return self.relay(flag=False)\n"
                  "    def relay(self, flag):\n"
                  "        return self.consume(flag=flag)\n"
                  "    def consume(self, flag):\n"
                  "        if self.enabled:\n"
                  "            return flag\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sqlglot/parser.py"
            path.parent.mkdir()
            path.write_text(source, encoding="utf-8")
            parser = types.ModuleType("sqlglot.parser")
            exec(compile(source, str(path), "exec"), vars(parser))
            module = types.ModuleType("sqlglot")
            module.parser = parser
            dialect = types.ModuleType("sqlglot.dialects.dialect")
            dialect.Dialect = lambda: SimpleNamespace(parser=parser.Parser)
            alias = parser.Parser.run
            with patch.dict(sys.modules, {"sqlglot": module, "sqlglot.parser": parser, dialect.__name__: dialect}):
                sites, touched = install(root)
            instance = parser.Parser()
            instance.enabled = False
            self.assertIsNone(alias(instance))
            self.assertEqual(touched, set())
            instance.enabled = True
            self.assertIs(alias(instance), False)
            self.assertEqual(touched, set(sites))
            touched.clear()
            instance.consume = lambda flag: flag
            self.assertIs(alias(instance), False)
            self.assertEqual(touched, set(sites))

    def test_map_binding_and_boolean_span(self):
        source = "é = call(a=False, b=False)\n"
        offset = source.encode().rindex(b"False")
        site = f"1:{offset}:{offset + 5}"
        pars = {"source": tc.digest(source), "rows": [1, 2, 3]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parse.gz"
            arg = Path(directory) / "arguments.gz"
            path.write_bytes(gzip.compress(json.dumps(pars).encode()))
            data = {"version": 1, "complete": True, "profile": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "source": pars["source"], "sites": [site], "reads": [[], [site], None]}
            tc.parse_profile.cache_clear()
            self.addCleanup(tc.parse_profile.cache_clear)
            self.addCleanup(tc.argument_profile.cache_clear)
            with patch.object(tc, "PARSE_MAP", path), patch.object(tc, "ARG_MAP", arg):
                def put(**changes):
                    tc.argument_profile.cache_clear()
                    arg.write_bytes(gzip.compress(json.dumps({**data, **changes}).encode()))
                def unread(before, after):
                    return tc.unread_rows(Change("parser.py", source, source.replace(before, after)), 1)
                put()
                self.assertEqual(unread("b=False", "b=True"), {0})
                self.assertEqual(unread("a=False", "a=True"), set())
                self.assertEqual(unread("b=False", "b=None"), set())
                for changes in ({"complete": False}, {"profile": "stale"}, {"reads": [[]]},
                                {"source": "stale"}, {"reads": [[], [], ["unknown"]]}):
                    put(**changes)
                    self.assertEqual(unread("b=False", "b=True"), set())

    def test_partial_and_complete_node_discharge(self):
        path = "sqlglot/parser.py"
        source = "value = self.select(flag=False)\n"
        change = Change(path, source, source.replace("False", "True"), {1}, {1})
        node = tc.EX + "execute_tpcds"
        other = tc.OP + "tpch"
        outside = tc.OP + "tpcds"
        chosen = {node, other, outside}
        site = path + ":select"
        key = path + "#1"
        baseline = {"pins": {}, "sources": {path: tc.digest(source)}, "tests": {"general": {}}}
        pars = {"source": tc.digest(source), "version": 1, "lines": [1],
                "rows": [[["parse", str(i), None, {}], "", [key]] for i in range(2)],
                "tests": {node: [0], other: [0, 1], outside: [0]}, "outside": {node: [], other: [], outside: [key]}}
        with (patch.object(tc, "_supported", return_value=site), patch.object(tc, "profile", return_value=baseline),
              patch.object(tc, "boundary_hashes", return_value={}), patch.object(tc, "parse_profile", return_value=pars),
              patch.object(tc, "unread_rows", return_value={0}), patch("import_effects.import_consumers", return_value=set()),
              patch.object(tc.subprocess, "run", side_effect=OSError) as child, patch.object(tc, "_wall_left", 20)):
            cov = SimpleNamespace(missing=set())
            self.assertEqual(tc.refine_tc([change], chosen, cov, chosen), {other, outside})
            payload = json.loads(child.call_args.kwargs["input"])
            self.assertEqual(payload["nodes"], [other])
            self.assertEqual(payload["tasks"], [[["parse_call", pars["rows"][1][0]], "", [other]]])
            child.reset_mock()
            self.assertEqual(tc.refine_tc([change], {node}, cov, chosen), set())
            child.assert_not_called()
