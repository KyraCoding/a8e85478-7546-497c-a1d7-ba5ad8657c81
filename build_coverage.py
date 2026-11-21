import argparse
import gzip
import inspect
import json
import os
import pickle
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from importlib.machinery import SourceFileLoader
from multiprocessing.util import Finalize
from pathlib import Path

import pytest


class Recorder:
    def __init__(self, root):
        self.root = root
        self.mask = 0
        self.importing = 0
        self.lines = defaultdict(dict)
        self.files = {}
        self.paths = {
            os.path.normcase(str(p)): p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not set(p.parts).intersection({".git", "__pycache__", ".pytest_cache"})
        }
        self.monitor = sys.monitoring
        self.tool = self.monitor.COVERAGE_ID
        self.monitor.use_tool_id(self.tool, "baseline item coverage")
        self.monitor.register_callback(self.tool, self.monitor.events.LINE, self.on_line)
        self.monitor.register_callback(self.tool, self.monitor.events.PY_START, self.on_start)
        sys.addaudithook(self.on_audit)
        original = SourceFileLoader.exec_module

        def importing(loader, module):
            self.importing += 1
            try:
                return original(loader, module)
            finally:
                self.importing -= 1

        SourceFileLoader.exec_module = importing

    @cache
    def source(self, filename):
        if not os.path.isabs(filename):
            return None
        return self.paths.get(os.path.normcase(filename))

    def on_line(self, code, lineno):
        if not code.co_flags & inspect.CO_OPTIMIZED:
            return self.monitor.DISABLE
        path = self.source(code.co_filename)
        if path:
            if self.importing:
                return None
            lines = self.lines[path]
            lines[lineno] = lines.get(lineno, 0) | self.mask
        return self.monitor.DISABLE

    def on_start(self, code, offset):
        if code.co_name == "<lambda>":
            return self.on_line(code, code.co_firstlineno)
        return self.monitor.DISABLE

    def on_audit(self, event, args):
        if event == "open" and self.mask and not self.importing:
            filename, _, flags = args
            if isinstance(filename, str) and not flags & os.O_WRONLY:
                path = self.paths.get(os.path.normcase(os.path.abspath(filename)))
                if path:
                    self.files[path] = self.files.get(path, 0) | self.mask

    def activate(self, mask):
        if mask != self.mask:
            self.mask = mask
            if mask:
                self.monitor.restart_events()
            events = self.monitor.events.LINE | self.monitor.events.PY_START
            self.monitor.set_events(self.tool, events if mask else 0)


worker = None


def record_job(root, directory, mask, fn, args, kwargs):
    global worker
    if worker is None:
        worker = Recorder(root)

        def flush():
            worker.activate(0)
            with (directory / f"{os.getpid()}.pickle").open("wb") as out:
                pickle.dump((dict(worker.lines), worker.files), out)

        Finalize(None, flush, exitpriority=1)
    worker.activate(mask)
    return fn(*args, **kwargs)


class Plugin:
    def __init__(self, recorder, directory):
        self.recorder = recorder
        self.masks = {}
        self.failures = set()
        self.nodes = []
        submit = ProcessPoolExecutor.submit

        def submit_job(pool, fn, /, *args, **kwargs):
            return submit(pool, record_job, recorder.root, directory, recorder.mask, fn, args, kwargs)

        ProcessPoolExecutor.submit = submit_job

    def pytest_collection_finish(self, session):
        self.nodes = sorted(item.nodeid for item in session.items)
        self.masks[""] = (1 << len(self.nodes)) - 1
        for i, node in enumerate(self.nodes):
            while node:
                self.masks[node] = self.masks.get(node, 0) | (1 << i)
                node = node.rpartition("::")[0]

    def pytest_runtest_logstart(self, nodeid, location):
        self.recorder.activate(self.masks[nodeid])

    def pytest_runtest_logfinish(self, nodeid, location):
        self.recorder.activate(0)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous = self.recorder.mask
        if fixturedef.scope != "function":
            self.recorder.activate(self.masks.get(request.node.nodeid, previous))
        try:
            return (yield)
        finally:
            self.recorder.activate(previous)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failures.add(report.nodeid)


def write_map(path, rec, plugin, directory):
    for part in directory.glob("*.pickle"):
        with part.open("rb") as stream:
            lines, files = pickle.load(stream)
        for filename, mapping in lines.items():
            dest = rec.lines[filename]
            for n, mask in mapping.items():
                dest[n] = dest.get(n, 0) | mask
        for filename, mask in files.items():
            rec.files[filename] = rec.files.get(filename, 0) | mask

    masks = {0: 0}

    def encode(mask):
        return masks.setdefault(mask, len(masks))

    files = {}
    for filename, mapping in sorted(rec.lines.items()):
        indices = [0] * (max(mapping) + 1)
        for lineno, mask in sorted(mapping.items()):
            indices[lineno] = encode(mask)
        files[filename] = indices
    reads = {p: encode(mask) for p, mask in sorted(rec.files.items())}
    payload = dict(nodes=plugin.nodes, missing=sorted(plugin.failures), lines=files,
                   reads=reads, masks=[format(m, "x") for m in masks])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("tests", nargs="*", default=["tests"])
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with tempfile.TemporaryDirectory() as directory:
        rec = Recorder(root)
        plugin = Plugin(rec, Path(directory))
        status = pytest.main(["-q", "--tb=line", "-p", "no:cacheprovider", *args.tests], plugins=[plugin])
        rec.activate(0)
        if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED) or not plugin.nodes:
            raise SystemExit(status or 1)
        write_map(output, rec, plugin, Path(directory))
        print(f"{len(plugin.nodes)} tests, {len(plugin.failures)} incomplete, {output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
