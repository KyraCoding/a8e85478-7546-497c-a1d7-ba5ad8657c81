import argparse
import ast
import gzip
import importlib.machinery as machinery
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True

import value_profile as vp


def relation(left, right):
    lt, rt = type(left), type(right)
    li, ri = lt is int or lt is bool, rt is int or rt is bool
    if li and ri:
        shift = 0
    elif ((li or lt is float) and (ri or rt is float)) or (
        lt is rt and (lt is str or lt is bytes)
    ):
        shift = 3
    else:
        return vp.UNKNOWN
    if left < right:
        return 1 << shift
    if left == right:
        return 2 << shift
    if left > right:
        return 4 << shift
    return vp.UNORDERED


def singleton_bit(value):
    if value is None:
        return 1
    if value is True:
        return 2
    if value is False:
        return 4
    t = type(value)
    if t is int or t is float or t is complex:
        return 8 if value == 0 else 16 if value == 1 else 32
    if any(t is safe for safe in (str, bytes, list, dict, tuple, set, frozenset, range)):
        return 32
    return 1 << vp.OPAQUE


class Recorder:
    def __init__(self):
        self.ambient = {}
        self.current = self.ambient
        self.tests = {}
        self.sites = []
        self.site_ids = {}
        self.hashes = {}

    def register(self, path, node):
        key = path, vp.location(node)
        if key not in self.site_ids:
            self.site_ids[key] = len(self.sites)
            self.sites.append(key)
        return self.site_ids[key]

    def pair(self, site, left, right):
        obs = self.current
        obs[site] = obs.get(site, 0) | relation(left, right)
        return left, right

    def single(self, site, value):
        obs = self.current
        obs[site] = obs.get(site, 0) | singleton_bit(value)
        return value


class Instrument(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.rec = recorder
        self.path = path

    def visit_Compare(self, node):
        kind = vp.layout(node)
        self.generic_visit(node)
        if kind is None:
            return node
        site = ast.Constant(self.rec.register(self.path, node))
        if kind in ("left", "right"):
            operand = node.left if kind == "right" else node.comparators[0]
            call = ast.Call(ast.Name("_operand_trace_single", ast.Load()), [site, operand], [])
            if kind == "right":
                node.left = ast.copy_location(call, operand)
            else:
                node.comparators[0] = ast.copy_location(call, operand)
            return node
        pair = ast.Call(ast.Name("_operand_trace_pair", ast.Load()), [site, node.left, node.comparators[0]], [])
        fn = ast.Name("_operand_trace_" + type(node.ops[0]).__name__, ast.Load())
        return ast.copy_location(ast.Call(fn, [ast.Starred(pair, ast.Load())], []), node)


class BaselineImports:
    def __init__(self, root, recorder):
        self.root, self.rec = root, recorder

    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = machinery.PathFinder.find_spec(fullname, path)
        if not spec:
            return None
        source = Path(spec.origin).resolve()
        if not source.is_relative_to(self.root / "sqlglot") or source.suffix != ".py":
            raise RuntimeError(f"expected Python baseline source for {fullname}")
        spec.loader = self
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        source = Path(module.__file__)
        path = source.relative_to(self.root).as_posix()
        text = source.read_text(encoding="utf-8")
        self.rec.hashes[path] = vp.digest(text)
        tree = Instrument(self.rec, path).visit(ast.parse(text, str(source)))
        code = compile(ast.fix_missing_locations(tree), str(source), "exec", dont_inherit=True)
        module.__dict__.update({"_operand_trace_" + k.__name__: v for k, v in vp.OPS.items()})
        module._operand_trace_single, module._operand_trace_pair = self.rec.single, self.rec.pair
        previous = self.rec.current
        self.rec.current = self.rec.ambient
        try:
            exec(code, module.__dict__)
        finally:
            self.rec.current = previous


def collect(root, recorder, tests):
    import pytest

    completed = set()
    failed = set()
    deadline = time.monotonic() + 180

    class Plugin:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_call(self, item):
            recorder.current = recorder.tests.setdefault(item.nodeid, {})
            try:
                return (yield)
            finally:
                recorder.current = recorder.ambient

        def pytest_runtest_logreport(self, report):
            if not report.passed:
                failed.add(report.nodeid)
            if report.when == "teardown" and report.passed:
                completed.add(report.nodeid)

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            if time.monotonic() >= deadline:
                pytest.exit("baseline operand collection time limit", returncode=pytest.ExitCode.INTERRUPTED)

    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    sys.meta_path.insert(0, BaselineImports(root, recorder))
    args = ["-q", "-s", "--tb=line", "--rootdir=.", "-p", "no:cacheprovider",
            "--deselect=tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
            "--deselect=tests/test_parser.py::TestParser::test_max_nodes", *tests]
    status = pytest.main(args, plugins=[Plugin()])
    if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED) and not (
        status == pytest.ExitCode.INTERRUPTED and time.monotonic() >= deadline
    ):
        raise SystemExit(status)
    return sorted(completed.intersection(recorder.tests) - failed)


def save(output, rec, nodes):
    rows = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for index, node in enumerate(nodes):
        for site, mask in rec.tests[node].items():
            path, key = rec.sites[site]
            rows[path][key][mask | rec.ambient.get(site, 0)].append(index)
    data = dict(version=1, hashes=rec.hashes, nodes=nodes, sites=rows)
    encoded = gzip.compress(json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
    output.parent.mkdir(exist_ok=True, parents=True)
    tmp = output.with_name(f"{output.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(encoded)
        tmp.replace(output)
    finally:
        tmp.unlink(missing_ok=True)
    return sum(map(len, rows.values()))


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=project / ".work/sqlglot")
    parser.add_argument("output", nargs="?", type=Path, default=vp.MAP)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    excluded = {"test_optimizer.py", "test_executor.py", "test_sqlglotc.py", "test_docs.py",
                "test_integration_loader.py", "test_typing.py", "test_dialect_imports.py",
                "test_dialect_entry_points.py"}
    tests = [p.relative_to(root).as_posix() for p in sorted((root / "tests").glob("test_*.py"))
             if p.name not in excluded]
    tests.append("tests/dialects")
    rec = Recorder()
    start = time.monotonic()
    nodes = collect(root, rec, tests)
    if not nodes:
        raise SystemExit("no completed baseline tests")
    sites = save(output, rec, nodes)
    print(f"{len(nodes)} completed tests, {sites} sites, {output.stat().st_size} bytes, "
          f"{time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    main()
