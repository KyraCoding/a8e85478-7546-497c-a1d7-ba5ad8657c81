import gzip
import importlib
import inspect
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path


def observe(root, module):
    import unittest

    os.chdir(root)
    sys.path.insert(0, str(root))
    importlib.import_module("sqlglot")
    depth = 0
    original = SourceFileLoader.exec_module

    def exec_module(loader, target):
        nonlocal depth
        inside = target.__name__ == "sqlglot" or target.__name__.startswith("sqlglot.")
        depth += inside
        try:
            return original(loader, target)
        finally:
            depth -= inside

    SourceFileLoader.exec_module = exec_module
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    prefix = str(root / "sqlglot") + os.sep
    seen = set()

    def on_line(code, number):
        if not code.co_flags & inspect.CO_OPTIMIZED or not code.co_filename.startswith(prefix):
            return monitor.DISABLE
        if depth:
            return None
        seen.add((Path(code.co_filename).relative_to(root).as_posix(), number))
        return monitor.DISABLE

    def on_start(code, offset):
        if code.co_name == "<lambda>":
            return on_line(code, code.co_firstlineno)
        return monitor.DISABLE

    monitor.use_tool_id(tool, "collection")
    monitor.register_callback(tool, monitor.events.LINE, on_line)
    monitor.register_callback(tool, monitor.events.PY_START, on_start)
    monitor.set_events(tool, monitor.events.LINE | monitor.events.PY_START)
    try:
        importlib.import_module(module)
    finally:
        monitor.set_events(tool, 0)
        SourceFileLoader.exec_module = original
    return sorted(seen)


def build(root, output):
    from coverage_map import Coverage

    cov = Coverage()
    files = sorted({node.partition("::")[0] for node in [*cov.nodes, *cov.missing]})
    lines = {}
    for file in files:
        module = file[:-3].replace("/", ".")
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--observe", str(root), module],
            capture_output=True, check=True, timeout=300,
        )
        for path, number in json.loads(result.stdout.splitlines()[-1]):
            lines.setdefault(path, {}).setdefault(str(number), []).append(file)
    payload = json.dumps({"schema": 1, "files": files, "lines": lines}, sort_keys=True, separators=(",", ":"))
    output.write_bytes(gzip.compress(payload.encode(), mtime=0))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--observe"]:
        print(json.dumps(observe(Path(sys.argv[2]).resolve(), sys.argv[3])))
    else:
        build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))
