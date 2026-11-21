import argparse
import gzip
import importlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from pathlib import Path


def import_sites(root):
    sites = defaultdict(lambda: defaultdict(set))
    current = []
    original = SourceFileLoader.exec_module

    def execute(loader, module):
        current.append(module.__name__)
        try:
            return original(loader, module)
        finally:
            current.pop()

    package = root / "sqlglot"
    paths = {str(p): p.relative_to(root).as_posix() for p in package.rglob("*.py")}
    monitor = sys.monitoring

    def line(code, n):
        if not code.co_flags & inspect.CO_OPTIMIZED or code.co_filename not in paths:
            return monitor.DISABLE
        if current and (current[-1] == "sqlglot" or current[-1].startswith("sqlglot.")):
            sites[paths[code.co_filename]][n].add(current[-1])

    def start(code, offset):
        if code.co_name == "<lambda>":
            return line(code, code.co_firstlineno)
        return monitor.DISABLE

    SourceFileLoader.exec_module = execute
    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "import-time lines")
    monitor.register_callback(tool, monitor.events.LINE, line)
    monitor.register_callback(tool, monitor.events.PY_START, start)
    monitor.set_events(tool, monitor.events.LINE | monitor.events.PY_START)
    try:
        for rel in sorted(paths.values()):
            module = rel[:-3].replace("/", ".").removesuffix(".__init__")
            if not module.endswith(".__main__"):
                try:
                    importlib.import_module(module)
                except ImportError as exc:
                    print(f"Optional module {module}: {exc}", file=sys.stderr)
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)
        SourceFileLoader.exec_module = original
    return {p: {n: sorted(mods) for n, mods in rows.items()} for p, rows in sites.items()}


def test_footprint(test_file, result):
    import pytest

    loaded = set()
    successes = set()
    opaque = set()
    current = []
    original = SourceFileLoader.exec_module

    def execute(loader, module):
        if module.__name__ == "sqlglot" or module.__name__.startswith("sqlglot."):
            loaded.add(module.__name__)
        return original(loader, module)

    def audit(event, args):
        if current and event in {"subprocess.Popen", "os.fork", "os.posix_spawn"}:
            opaque.add(current[-1])

    class Plugin:
        def pytest_collection_finish(self, session):
            for item in session.items:
                if "ProcessPoolExecutor" in vars(item.module):
                    def local_pool(*args, mp_context=None, **kwargs):
                        return ThreadPoolExecutor(max_workers=1, **kwargs)
                    item.module.ProcessPoolExecutor = local_pool

        def pytest_runtest_logstart(self, nodeid, location):
            current.append(nodeid)

        def pytest_runtest_logreport(self, report):
            if report.when == "call" and report.passed:
                successes.add(report.nodeid)
            if report.failed or report.skipped:
                opaque.add(report.nodeid)

        def pytest_runtest_logfinish(self, nodeid, location):
            current.pop()

    SourceFileLoader.exec_module = execute
    sys.addaudithook(audit)
    status = pytest.main(["-q", "--tb=no", "-p", "no:cacheprovider", "-k",
                          "not test_max_nodes", test_file], plugins=[Plugin()])
    good = successes - opaque if status in (0, 1) else set()
    result.write_text(json.dumps([sorted(loaded), sorted(good)]), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--file")
    parser.add_argument("--site-profile", choices=("clean", "pytest"))
    parser.add_argument("--update-sites", type=Path, help="preserve the existing validated test footprints and union sites")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    root, dest = args.source.resolve(), args.output.resolve()
    runner = str(Path(__file__).resolve())
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    if args.file:
        test_footprint(args.file, dest)
        return
    if args.site_profile:
        if args.site_profile == "pytest":
            import pytest
        elif "unittest" in sys.modules or "pytest" in sys.modules:
            raise RuntimeError("clean import process already contains a test runner")
        if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
            raise RuntimeError("SQLGlot imported before profiling")
        sites = import_sites(root)
        if sys.modules["sqlglot.expressions.core"].UNITTEST != (args.site_profile == "pytest"):
            raise RuntimeError("unexpected runtime flag")
        dest.write_text(json.dumps(sites), encoding="utf-8")
        return
    data = json.loads(gzip.decompress(args.update_sites.read_bytes())) if args.update_sites else {"sites": {}, "tests": {}}
    with tempfile.TemporaryDirectory() as directory:
        for profile in ("clean", "pytest"):
            output = Path(directory) / profile
            subprocess.run([sys.executable, runner, str(root), str(output), "--site-profile", profile], check=True)
            for path, lines in json.loads(output.read_bytes()).items():
                previous = data["sites"].setdefault(path, {})
                for line, modules in lines.items():
                    previous[line] = sorted(set(previous.get(line, ())) | set(modules))

    def save():
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(".tmp")
        temp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
        temp.replace(dest)

    save()
    if args.update_sites:
        return
    with tempfile.TemporaryDirectory() as tmp:
        def run_file(test):
            out = Path(tmp) / (test.replace("/", "_") + ".json")
            proc = subprocess.run([sys.executable, "-X", "cpu_count=2", runner, str(root), str(out),
                                   "--file", test], stdout=subprocess.DEVNULL)
            return test, json.loads(out.read_bytes()) if not proc.returncode and out.exists() else [[], []]

        tests = sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for test, summary in pool.map(run_file, tests):
                data["tests"][test] = summary
                save()
                print(f"{len(data['tests'])}/{len(tests)} {test}: {len(summary[1])} tests", flush=True)


if __name__ == "__main__":
    main()
