"""Record which sqlglot modules the processes of test_execute_tpcds import.

Run inside the profiler image from the repository root, with the pinned checkout at /opt/sqlglot:
    python build_fe_modules.py /opt/sqlglot fe_modules.json.gz
The real unittest method runs once with its own process pool. A sitecustomize hook writes every process's loaded
sqlglot modules at exit in import order, so the worker set is the union over all spawned workers that executed
queries and worker_order replays their import order. The front-end replay's minimal parent role is recorded
separately in a fresh interpreter.
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = '''
import atexit, json, os, sys
def _dump():
    import multiprocessing
    role = "worker" if multiprocessing.parent_process() is not None else "parent"
    names = [n for n in list(sys.modules) if n == "sqlglot" or n.startswith("sqlglot.")]
    path = os.path.join(os.environ["FE_MODULES_OUT"], f"{role}-{os.getpid()}.json")
    with open(path, "w") as stream:
        json.dump(names, stream)
atexit.register(_dump)
'''

MINIMAL = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import fe_replay
fe_replay.child_setup({}, cpu_limit=0)
pairs, schema = fe_replay.prepare()
executed = [i for i in range(len(pairs)) if pairs[i][0].get("execute")]
fe_replay.parent_role(pairs, list(range(len(pairs))), executed, lambda name, text: None, fe_replay.Stages())
print(json.dumps(sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot."))))
'''


def main():
    root, dest = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import fe_replay

    with tempfile.TemporaryDirectory() as hook, tempfile.TemporaryDirectory() as out:
        (Path(hook) / "sitecustomize.py").write_text(HOOK)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([hook, str(root)]), "FE_MODULES_OUT": out,
               "SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        test = "tests.test_executor.TestExecutor.test_execute_tpcds"
        run = subprocess.run([sys.executable, "-m", "unittest", "-v", test], cwd=root, env=env,
                             capture_output=True, text=True)
        if run.returncode != 0 or "OK" not in run.stderr.splitlines()[-1]:
            sys.exit(f"pinned test did not pass:\n{run.stderr[-2000:]}")
        parents, workers = [], []
        for file in sorted(Path(out).glob("*.json")):
            names = json.loads(file.read_text())
            if names:  # helper processes such as the resource tracker load no sqlglot module
                (workers if file.name.startswith("worker-") else parents).append(names)
    if len(parents) != 1 or not workers:
        sys.exit(f"expected one parent and at least one worker, got {len(parents)} and {len(workers)}")
    env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root)}
    minimal = subprocess.run([sys.executable, "-B", "-c", MINIMAL, str(here)], env=env, capture_output=True,
                             text=True, check=True)
    order = []
    for names in sorted(workers, key=len, reverse=True):
        order += [n for n in names if n not in order]
    record = {
        "version": 2,
        "python": sys.version,
        "tree": fe_replay.tree_hash(root),
        "worker": sorted(set().union(*workers)),
        "worker_order": order,
        "worker_processes": len(workers),
        "parent": parents[0],
        "minimal": json.loads(minimal.stdout),
    }
    with gzip.open(dest, "wt", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True)
    print(f"worker {len(record['worker'])} modules from {len(workers)} processes; "
          f"parent {len(record['parent'])}; minimal {len(record['minimal'])}")


if __name__ == "__main__":
    main()
