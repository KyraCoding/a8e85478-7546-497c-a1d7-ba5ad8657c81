import argparse
import ast
import gzip
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

import build_values as bv
import value_profile as vp

PRIMITIVE, ENUM, OBJECT, UNKNOWN = 0, 4, 8, 12
MRO = type.__dict__["__mro__"].__get__
NAMESPACE = type.__dict__["__dict__"].__get__


def slot(bases, name):
    for base in bases:
        namespace = NAMESPACE(base)
        if name in namespace:
            return namespace[name]


def primitive(t):
    return (t is int or t is bool or t is str or t is bytes or t is float
            or t is complex or t is type(None))


def nan(value, t):
    return ((t is float and math.isnan(value)) or
            (t is complex and (math.isnan(value.real) or math.isnan(value.imag))))


def relation(a, b):
    ta, tb = type(a), type(b)
    same = int(a is b) << 1
    if primitive(ta) and primitive(tb):
        if nan(a, ta) or nan(b, tb):
            return UNKNOWN
        return PRIMITIVE + same + (a == b)
    if a is None or b is None or ta is bool or tb is bool:
        other = tb if a is None or ta is bool else ta
        if (other is list or other is dict or other is set or other is frozenset
                or other is tuple or other is range):
            return PRIMITIVE
        if a is None or b is None:
            bases = MRO(other)
            if slot(bases, "__eq__") is object.__eq__ and slot(bases, "__ne__") is object.__ne__:
                return OBJECT
        return UNKNOWN
    if ta is tb:
        bases = MRO(ta)
        if any(base is Enum for base in bases):
            eq, ne = slot(bases, "__eq__"), slot(bases, "__ne__")
            if eq is object.__eq__ and ne is object.__ne__:
                return ENUM + same + (a is b)
            for base in (int, str):
                if (eq is base.__eq__ and (ne is base.__ne__ or ne is object.__ne__)
                        and any(parent is base for parent in bases)):
                    return ENUM + same + eq(a, b)
    return UNKNOWN


def supported(node):
    return (len(node.ops) == 1 and type(node.ops[0]) in vp.IDENTITY + vp.EQUALITY
            and any(vp.singleton(n) or isinstance(n, ast.Attribute)
                    for n in (node.left, node.comparators[0])))


class Instrument(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.rec, self.path = recorder, path

    def visit_Compare(self, node):
        watch = supported(node)
        self.generic_visit(node)
        if not watch:
            return node
        site = self.rec.register(self.path, node)
        pair = ast.Call(ast.Name("_operand_trace_identity_pair", ast.Load()),
                        [ast.Constant(site), node.left, node.comparators[0]], [])
        op = ast.Name("_operand_trace_identity_" + type(node.ops[0]).__name__, ast.Load())
        return ast.copy_location(ast.Call(op, [ast.Starred(pair, ast.Load())], []), node)


class Recorder(bv.Recorder):
    def __init__(self):
        super().__init__()
        self.ambient = self.current = set()

    def pair(self, site, left, right):
        self.current.add((site << 4) + relation(left, right))
        return left, right


class Imports(bv.BaselineImports):
    def exec_module(self, module):
        source = Path(module.__file__)
        path = source.relative_to(self.root).as_posix()
        text = source.read_text(encoding="utf-8")
        self.rec.hashes[path] = vp.digest(text)
        tree = Instrument(self.rec, path).visit(ast.parse(text, str(source)))
        code = compile(ast.fix_missing_locations(tree), str(source), "exec", dont_inherit=True)
        module._operand_trace_identity_pair = self.rec.pair
        for op in vp.IDENTITY + vp.EQUALITY:
            module.__dict__["_operand_trace_identity_" + op.__name__] = vp.OPS[op]
        previous = self.rec.current
        self.rec.current = self.rec.ambient
        try:
            exec(code, module.__dict__)
        finally:
            self.rec.current = previous


def collect(root, tests):
    import pytest

    rec = Recorder()
    passed, bad = set(), set()

    class Plugin:
        def pytest_collection_finish(self, session):
            def local_worker(*args, mp_context=None, **kwargs):
                return ThreadPoolExecutor(max_workers=1, **kwargs)
            for item in session.items:
                if "ProcessPoolExecutor" in vars(item.module):
                    item.module.ProcessPoolExecutor = local_worker

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_call(self, item):
            rec.current = rec.tests.setdefault(item.nodeid, set())
            try:
                return (yield)
            finally:
                rec.current = rec.ambient

        def pytest_runtest_logreport(self, report):
            if not report.passed:
                bad.add(report.nodeid)
            elif report.when == "teardown":
                passed.add(report.nodeid)

    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Imports(root, rec))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    status = pytest.main(["-q", "--tb=line", "-p", "no:cacheprovider",
                          "-k", "not test_max_nodes and not test_generator_memory and not test_lazy_load",
                          *(tests or ["tests"])], plugins=[Plugin()])
    if status not in (0, 1):
        raise SystemExit(status)
    nodes = sorted(passed.intersection(rec.tests) - bad)
    return rec, nodes, status


def masks(events):
    result = {}
    for event in events:
        site, bit = divmod(event, 16)
        result[site] = result.get(site, 0) | (1 << bit)
    return result


def pack(rec, nodes):
    ambient = masks(rec.ambient)
    rows = {}
    for i, name in enumerate(nodes):
        for site, mask in masks(rec.tests[name]).items():
            path, key = rec.sites[site]
            groups = rows.setdefault(path, {}).setdefault(key, {})
            groups.setdefault(str(mask | ambient.get(site, 0)), []).append(i)
    return dict(version=1, nodes=nodes, hashes=rec.hashes, sites=rows)


def merge(files):
    result = dict(version=1, nodes=[], hashes={}, sites={})
    for file in files:
        with gzip.open(file, "rt") as stream:
            data = json.load(stream)
        offset = len(result["nodes"])
        if set(result["nodes"]).intersection(data["nodes"]):
            raise ValueError("overlapping test shards")
        result["nodes"].extend(data["nodes"])
        for path, digest in data["hashes"].items():
            if result["hashes"].setdefault(path, digest) != digest:
                raise ValueError("incompatible source in shards")
        for path, sites in data["sites"].items():
            rows = result["sites"].setdefault(path, {})
            for site, groups in sites.items():
                target = rows.setdefault(site, {})
                for mask, ids in groups.items():
                    target.setdefault(mask, []).extend(offset + i for i in ids)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("tests", nargs="*")
    parser.add_argument("--merge", nargs="+", metavar="SHARD")
    args = parser.parse_args()
    output = args.output.resolve()
    status = 0
    if args.merge:
        data = merge(args.merge)
    else:
        rec, nodes, status = collect(args.source.resolve(), args.tests)
        data = pack(rec, nodes)
    tmp = output.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    tmp.replace(output)
    print(f"{len(data['nodes'])} completed baseline tests, {output.stat().st_size} bytes", flush=True)
    sys.exit(status)
