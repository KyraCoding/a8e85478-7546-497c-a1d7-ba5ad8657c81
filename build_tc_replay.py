import ast
import gzip
import json
import multiprocessing
import os
import sys
from pathlib import Path

from tc_replay import (EX, FILES, MAP, OP, PARSE_MAP, boundary_hashes, digest, get_fixtures,
                       observe, tree_state)


def owner_map(path):
    source = path.read_text(encoding="utf-8")
    owners = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.ClassDef)):
                    continue
                for line in range(max(node.lineno + 1, stmt.lineno), stmt.end_lineno + 1):
                    owners[str(line)] = node.name
        if isinstance(node, ast.ClassDef) and node.name in {"Generator", "Parser", "DuckDBGenerator"}:
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if (isinstance(target, ast.Name) and target.id.isupper()
                            and isinstance(stmt.value, ast.Constant) and stmt.lineno == stmt.end_lineno):
                        owners[str(stmt.lineno)] = "*"
    return source, owners


def single_statements(source):
    return {n.lineno for n in ast.walk(ast.parse(source))
            if isinstance(n, (ast.Assign, ast.Return)) and n.lineno == n.end_lineno
            and not any(isinstance(c, (ast.Lambda, ast.comprehension)) for c in ast.walk(n))}


def measure(task):
    hits = set()
    monitor = sys.monitoring
    def start(code, offset):
        path = filenames.get(code.co_filename)
        if path:
            owner = owners[path].get(str(code.co_firstlineno))
            hits.add(path + ":" + code.co_name)
            if owner and owner != "*":
                hits.add(path + ":" + owner)
        return monitor.DISABLE
    monitor.use_tool_id(4, "tc")
    monitor.register_callback(4, monitor.events.PY_START, start)
    monitor.set_events(4, monitor.events.PY_START)
    try:
        output = observe(task, fixtures)
    finally:
        monitor.set_events(4, 0)
        monitor.free_tool_id(4)
    return [task, digest(output), sorted(hits)]


def build(root, output):
    global fixtures, owners, filenames
    sys.path.insert(0, str(root))
    fixtures = get_fixtures()
    from sqlglot import TokenType
    from sqlglot.optimizer import optimize
    from sqlglot.dialects.duckdb import DuckDB
    from sqlglot import exp
    from sqlglot.serde import dump

    sources, owners = {}, {}
    filenames = {str(root / path): path for path in FILES}
    for p in sorted(FILES):
        source, owners[p] = owner_map(root / p)
        sources[p] = digest(source)
    tasks = []
    tests = {g: {} for g in ("general", "duck", "rule")}
    def add(node, groups, kind, suite, index):
        task = (kind, suite, index)
        if task not in tasks:
            tasks.append(task)
        row = tasks.index(task)
        for group in groups:
            tests[group].setdefault(node, []).append(row)
    for suite, (_, cases) in fixtures.items():
        for i, (meta, _, _) in enumerate(cases):
            for group in tests:
                add(OP + "tpc" + suite, [group], "assert", suite, i)
            if suite == "h":
                if meta.get("execute"):
                    raise ValueError("TPC-H worker now executes SQL")
                for kind in ("duck", "duck_expected"):
                    add(EX + "optimized_tpch", tests, kind, suite, i)
                add(EX + "execute_tpch", tests, "ship", suite, i)
            else:
                node = EX + "execute_tpcds"
                add(node, ("rule", "duck"), "ship", suite, i)
                if meta.get("execute"):
                    add(node, ("rule", "duck"), "duck", suite, i)
                    add(node, ("rule",), "plan", suite, i)
    with multiprocessing.get_context("fork").Pool(2, maxtasksperchild=1) as pool:
        rows = pool.map(measure, tasks, chunksize=1)
    full_owners = {p: {n: p + ":" + f if f != "*" else f for n, f in lines.items()}
                   for p, lines in owners.items()}
    data = {"version": 1, "tests": tests, "rows": rows, "sources": sources, "pins": boundary_hashes(root),
            "owners": full_owners, "tokens": sorted(TokenType.__members__)}
    with gzip.GzipFile(str(output), "wb", mtime=0) as stream:
        stream.write(json.dumps(data, separators=(",", ":")).encode())
    print(f"{len(rows)} TPC boundaries, {output.stat().st_size} bytes")


def build_parses(root, arguments=False):
    import pickle
    import unittest
    from concurrent.futures import Future
    from functools import wraps
    sys.path.insert(0, str(root))
    path = "sqlglot/parser.py"
    filename = str(root / path)
    source, owners = owner_map(root / path)
    line_sites = {n: path + "#" + str(n) for n in single_statements(source)
                  if str(n) in owners and owners[str(n)] != "*"}
    outside = set()
    active = outside
    monitor = sys.monitoring
    def start(code, offset):
        if code.co_filename != filename:
            return monitor.DISABLE
        active.add(path + ":" + code.co_name)
        owner = owners.get(str(code.co_firstlineno))
        if owner:
            active.add(path + ":" + owner)
    monitor.use_tool_id(4, "tc-parse")
    monitor.register_callback(4, monitor.events.PY_START, start)
    def line(code, n):
        if code.co_filename == filename and n in line_sites:
            active.add(line_sites[n])
            return
        return monitor.DISABLE
    monitor.register_callback(4, monitor.events.LINE, line)
    monitor.set_events(4, monitor.events.PY_START | monitor.events.LINE)
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    from tests import test_executor, test_optimizer

    if arguments:
        from build_tc_arguments import install, save_arguments
        import_hits = set(outside)
        sites, touched = install(root)
        outside.clear()
        outside.update(import_hits)
    reads = {}
    rows, indices, tests, excluded = [], {}, {}, {}
    used = set()
    def wrap(method, func):
        @wraps(func)
        def call(self, *args, **opts):
            nonlocal active
            if (active is not outside or type(self) is not Dialect
                    or vars(self) != vars(Dialect()) or any(v is not None for v in opts.values())):
                return func(self, *args, **opts)
            if method == "parse" and len(args) == 1:
                sql, into = args[0], None
            elif method == "parse_into" and len(args) == 2 and isinstance(args[0], type):
                into, sql = args[0].__name__, args[1]
                if getattr(exp, into, None) is not args[0]:
                    return func(self, *args, **opts)
            else:
                return func(self, *args, **opts)
            if type(sql) != str:
                return func(self, *args, **opts)
            descriptor = [method, sql, into, opts]
            key = json.dumps(descriptor, sort_keys=True)
            hits = set()
            active = hits
            if arguments:
                touched.clear()
            try:
                result = func(self, *args, **opts)
            except BaseException:
                outside.update(hits)
                if arguments:
                    reads[key] = None
                raise
            finally:
                active = outside
            try:
                value = digest(tree_state(result))
            except (ValueError, RecursionError, TypeError):
                outside.update(hits)
                if arguments:
                    reads[key] = None
                return result
            if key not in indices:
                indices[key] = len(rows)
                rows.append([descriptor, value, set()])
            i = indices[key]
            if rows[i][1] != value:
                outside.update(hits | rows[i][2])
                if arguments:
                    reads[key] = None
            if arguments and reads.setdefault(key, set()) is not None:
                reads[key].update(touched)
            used.add(i)
            rows[i][2].update(hits)
            return result
        return call
    parse, parse_into = Dialect.parse, Dialect.parse_into
    Dialect.parse = wrap("parse", parse)
    Dialect.parse_into = wrap("parse_into", parse_into)

    class SerialPool:
        def __init__(self, *, initializer=None, initargs=(), **kwargs):
            if initializer:
                initializer(*initargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def shutdown(self):
            pass
        def submit(self, func, *args, **kwargs):
            future = Future()
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(func(*args, **kwargs))))
            return future
    test_executor.ProcessPoolExecutor = test_optimizer.ProcessPoolExecutor = SerialPool
    imports = set(outside)
    for module, cls, prefix, names in (
        (test_executor, test_executor.TestExecutor, EX, ("execute_tpcds", "execute_tpch", "optimized_tpch")),
        (test_optimizer, test_optimizer.TestOptimizer, OP, ("tpch", "tpcds")),
    ):
        outside.clear()
        used.clear()
        cls.setUpClass()
        setup_hits, setup_ids = set(outside) | imports, set(used)
        try:
            for name in names:
                outside.clear()
                used.clear()
                if hasattr(cls, "cache"):
                    cls.cache.clear()
                case = cls("test_" + name)
                result = case.run(unittest.TestResult())
                if result.errors or result.failures or result.skipped:
                    raise RuntimeError(str(result.errors + result.failures + result.skipped)[:2000])
                tests[prefix + name] = sorted(used | setup_ids)
                excluded[prefix + name] = sorted(outside | setup_hits)
        finally:
            cls.tearDownClass()
            cls.doClassCleanups()
    monitor.set_events(4, 0)
    fixture_sql = {sql for _, cases in get_fixtures().values()
                   for _, original, expected in cases for sql in (original, expected)}
    ancillary = {i for i, (call, _, _) in enumerate(rows)
                 if call[0] != "parse" or call[1] not in fixture_sql}
    tests = {n: sorted(set(ids) | ancillary) for n, ids in tests.items()}
    outside_union = sorted(set().union(*excluded.values()))
    excluded = dict.fromkeys(tests, outside_union)
    for row in rows:
        row[2] = sorted(row[2])
    if arguments:
        Dialect.parse, Dialect.parse_into = parse, parse_into
        save_arguments(source, rows, tests, excluded, sites, reads, touched)
        return
    with gzip.GzipFile(str(PARSE_MAP), "wb", mtime=0) as stream:
        stream.write(json.dumps({"source": digest(source), "version": 1, "rows": rows,
                                 "lines": sorted(line_sites), "tests": tests, "outside": excluded},
                                separators=(",", ":")).encode())
    print(f"{len(rows)} actual parser inputs, {PARSE_MAP.stat().st_size} bytes")


if __name__ == "__main__":
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise SystemExit("build with PYTHONHASHSEED=0")
    root = Path(sys.argv[1]).resolve()
    if sys.argv[2:] == ["--parses"]:
        build_parses(root)
    elif sys.argv[2:] == ["--arguments"]:
        build_parses(root, arguments=True)
    else:
        build(root, Path(sys.argv[2]) if len(sys.argv) > 2 else MAP)
