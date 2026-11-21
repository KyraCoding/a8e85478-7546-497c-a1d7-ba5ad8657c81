import argparse
import gzip
import hashlib
import importlib
import json
import os
import sys
import time
from functools import wraps
from pathlib import Path
from unittest import TestResult


def capture(root, output):
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "exact stress inputs")
    filenames = {str(p): p.relative_to(root).as_posix() for p in root.rglob("*.py")}
    calls = []
    lines = {}

    def on_line(code, number):
        path = filenames.get(code.co_filename)
        if path:
            lines.setdefault(path, set()).add(number)
        return monitor.DISABLE

    def on_start(code, offset):
        if code.co_name == "<lambda>":
            return on_line(code, code.co_firstlineno)
        return monitor.DISABLE

    monitor.register_callback(tool, monitor.events.LINE, on_line)
    monitor.register_callback(tool, monitor.events.PY_START, on_start)
    monitor.set_events(tool, monitor.events.LINE | monitor.events.PY_START)
    imports = lines
    mod = importlib.import_module("tests.test_parser")
    parse = mod.parse_one
    outside = {}
    phases = []
    lines = outside
    monitor.restart_events()

    from sqlglot.dialects.dialect import Dialect

    tokenize = Dialect.tokenize

    @wraps(tokenize)
    def record_tokens(*args, **kwargs):
        nonlocal lines
        prev, lines = lines, {}
        phases.append(lines)
        monitor.restart_events()
        try:
            return tokenize(*args, **kwargs)
        finally:
            lines = prev
            monitor.restart_events()

    @wraps(parse)
    def observe(sql, **kwargs):
        nonlocal lines
        previous, lines = lines, {}
        monitor.restart_events()
        case = {"sql": hashlib.sha256(sql.encode()).hexdigest(), "args": kwargs, "size": len(sql),
                "lines": lines}
        calls.append(case)
        phases.clear()
        t0 = time.monotonic()
        try:
            value = parse(sql, **kwargs)
            case["result"] = type(value).__qualname__
            return value
        except Exception as exc:
            case["exception"] = [type(exc).__qualname__, str(exc)]
            raise
        finally:
            if len(phases) != 1:
                raise RuntimeError(f"expected one tokenization, saw {len(phases)}")
            case["tokenization"] = phases[0]
            print(f"call {len(calls)}: {len(sql)} chars, {time.monotonic()-t0:.2f} seconds", flush=True)
            lines = previous
            monitor.restart_events()

    Dialect.tokenize = record_tokens
    mod.parse_one = observe
    outcome = TestResult()
    mod.TestParser("test_max_nodes").run(outcome)
    monitor.set_events(tool, 0)
    if not outcome.wasSuccessful() or not calls:
        raise RuntimeError((outcome.errors, outcome.failures))
    files = {filenames.get(getattr(m, "__file__", None)) for m in list(sys.modules.values())}
    files.discard(None)
    def index(rows):
        return {p: sorted(ns) for p, ns in sorted(rows.items())}
    for item in calls:
        item["lines"] = index(item["lines"])
        item["tokenization"] = index(item["tokenization"])
    data = {"node": "tests/test_parser.py::TestParser::test_max_nodes", "calls": calls,
            "files": sorted(files), "imports": index(imports), "outside": index(outside)}
    output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), mtime=0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    capture(args.source.resolve(), args.output.resolve())
