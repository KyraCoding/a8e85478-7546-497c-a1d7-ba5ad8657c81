import argparse
import gzip
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


OBSERVER = '''
import os
import sys

root = os.environ["IMPORT_TRACE_ROOT"] + "/"
seen = set()
out = os.open(os.environ["IMPORT_TRACE_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
def observe(event, args):
    if event == "exec":
        path = args[0].co_filename
        if path not in seen:
            seen.add(path)
            if path.startswith(root):
                os.write(out, (path[len(root):] + "\\n").encode())
    elif event == "subprocess.Popen":
        exe, argv, _, env = args
        if (not isinstance(argv, (list, tuple)) or "-I" in argv or "-S" in argv
                or (env is not None and env.get("IMPORT_TRACE_LOG") != os.environ["IMPORT_TRACE_LOG"])):
            os.write(out, b"!opaque\\n")
sys.addaudithook(observe)
'''

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("catalog", type=Path)
    ap.add_argument("destination", type=Path)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--min-seconds", type=float, default=0.09)
    args = ap.parse_args()
    root = args.source.resolve()
    tests = json.loads(args.catalog.read_bytes())["tests"]
    nodes = [row["id"] for row in tests
             if row["seconds"] >= args.min_seconds or row["id"].endswith("::test_lazy_load")]
    nodes = [node for node in nodes if node != "tests/test_parser.py::TestParser::test_max_nodes"]
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        (temp / "sitecustomize.py").write_text(OBSERVER, encoding="utf-8")

        def measure(item):
            i, node = item
            log = temp / f"trace-{i}"
            env = dict(os.environ, IMPORT_TRACE_ROOT=str(root), IMPORT_TRACE_LOG=str(log),
                       PYTHONPATH=os.pathsep.join([directory, str(root)]), SKIP_INTEGRATION="0",
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1",
                       PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
                                  cwd=root, env=env, capture_output=True)
            paths = set(log.read_text().splitlines()) if log.exists() else set()
            if proc.returncode or b"1 passed" not in proc.stdout or "!opaque" in paths:
                return node, []
            return node, sorted(p for p in paths if p.endswith(".py"))

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for node, paths in pool.map(measure, enumerate(nodes)):
                if paths:
                    results[node] = paths
    args.destination.write_bytes(gzip.compress(json.dumps(results, separators=(",", ":"),
                                    sort_keys=True).encode(), mtime=0))
    print(f"Certified {len(results)} of {len(nodes)} isolated items")


if __name__ == "__main__":
    main()
