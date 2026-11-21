"""Record which sqlglot state four heavy tests read on their baseline paths.

    python build_ds_reads.py /opt/sqlglot ds_reads.json.gz [fe_modules.json.gz]

Each role is profiled in its own process with the modules that kind of process imports:
  worker    pool worker of test_execute_tpcds (fe_modules.json.gz): for each executed query, unpickle, find_tables
            and sqlglot.executor.execute (validation, optimize, logging, plan, Python execution); the result is
            pickled back. The 42 queries without the execute flag are only unpickled.
  parent    test_execute_tpcds in a process with every collected module imported (fe_baseline session_modules):
            setUpClass Table construction and pickling, parse and pickle of all 99 queries, DuckDB transpile of
            the executed ones, and unpickling each worker result and reading its rows and columns.
  worker_h  pool worker of test_execute_tpch: the TPC-H fixtures carry no execute flag, so workers only unpickle
            the tables and the 22 parsed queries.
  parent_h  test_execute_tpch's parent: setUpClass, parse and pickle of the 22 TPC-H queries.
  oh        test_optimized_tpch: setUpClass and the DuckDB SQL of each TPC-H query and of its optimized form.
  gm        test_generator_memory: every dialect looked up and its generator constructed.
Session roles are profiled with the dialect registry as left by import and also cleared, and every role under
three hash seeds; the profiles are merged per role. Before the measured window every module and class attribute
of every loaded sqlglot module is wrapped, so each read records (receiver class, defining class, attribute);
dictionary values become dictionaries that record looked-up keys, misses included, and any whole-container use;
module objects record attribute reads. Executed code objects, their LOAD_GLOBAL names, names passed to
getattr/hasattr/setattr/delattr/dir/vars and enum iteration are recorded. Generator dispatch tables are built
before the window and tracked as dictionaries. Each profiled run must give the same outputs as an unprofiled run
with the same seed (parse pickles, DuckDB SQL, tables, optimized trees, result tables).
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

CHILD = r'''
import unittest  # the test processes import unittest before sqlglot, which turns on sqlglot's test-mode checks
import ast, builtins, csv, dis, enum, gzip, hashlib, importlib, json, os, pickle, sys, types
here, root, role, profiled, modules_file, results_dir = sys.argv[1:7]
profiled = profiled == "1"
sys.path.insert(0, here)
sys.path.insert(0, root)
os.chdir(root)
import ds_rows
import fe_replay as fr
from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA, load_sql_fixture_pairs
from sqlglot import find_tables, parse_one, transpile
import sqlglot.executor as executor_module
from sqlglot.executor import execute
from sqlglot.executor.table import Table
for name in json.loads(open(modules_file).read()):
    importlib.import_module(name)
if sys.modules["sqlglot.expressions.core"].UNITTEST is not True:
    raise SystemExit("sqlglot was imported without its test-mode checks")
kind, _, variant = role.partition(":")
if variant.endswith("cleared"):
    from sqlglot.dialects.dialect import Dialect
    Dialect._classes.clear()
tpch = variant.startswith("h")
pairs = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql" if tpch else "optimizer/tpc-ds/tpc-ds.sql"))
executed = [i for i in range(len(pairs)) if pairs[i][0].get("execute")]
ROOT = os.path.realpath(root).replace(os.sep, "/").rstrip("/") + "/"

def relative(filename):
    text = os.path.realpath(filename).replace(os.sep, "/") if os.path.isabs(filename) else filename
    return text[len(ROOT):] if text.startswith(ROOT) else None

def read_csv(directory, schema):
    raw = {}
    for table, columns in schema.items():
        with gzip.open(os.path.join(root, "tests/fixtures/optimizer", directory, f"{table}.csv.gz"), "rt", newline="") as f:
            reader = csv.reader(f, delimiter="|"); next(reader); rows = []; ctypes = []
            for row in reader:
                if not ctypes:
                    for v in row:
                        try: ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError): ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        raw[table] = (columns, rows)
    return raw

raw_ds = read_csv("tpc-ds", TPCDS_SCHEMA) if kind in ("worker", "parent", "oh") else None
raw_h = read_csv("tpc-h", TPCH_SCHEMA) if kind in ("worker", "parent", "oh") else None
blobs = {i: pickle.dumps(parse_one(pairs[i][1])) for i in range(len(pairs))} if kind == "worker" else {}
table_blob = (pickle.dumps({n: Table(columns=c, rows=r) for n, (c, r) in (raw_h if tpch else raw_ds).items()})
              if kind == "worker" else None)

# ------------------------------------------------------------------ instrumentation
names, index = [], {}
def nid(text):
    i = index.get(text)
    if i is None:
        i = index[text] = len(names); names.append(text)
    return i
stage = [None]
reads = {}
def rec(text):
    if stage[0] is not None:
        reads.setdefault(stage[0], set()).add(nid(text))

def cname(c):
    return f"{c.__module__}.{c.__qualname__}"

class Read:
    __slots__ = ("value", "owner", "name")
    def __init__(self, value, owner, name):
        self.value, self.owner, self.name = value, owner, name
    def __get__(self, instance, owner=None):
        if stage[0] is not None:
            receiver = type(instance) if instance is not None else owner
            rec("a\t" + (cname(receiver) if isinstance(receiver, type) else "?") + "\t" + self.owner + "\t" + self.name)
        value = self.value
        get = getattr(type(value), "__get__", None)
        return get(value, instance, owner) if get is not None else value

class ReadData(Read):
    __slots__ = ()
    def __set__(self, instance, value):
        type(self.value).__set__(self.value, instance, value)
    def __delete__(self, instance):
        type(self.value).__delete__(self.value, instance)

def kd(key):
    try:
        return ds_rows.digest(key, frozenset())
    except Exception:
        return "?"

class TDict(dict):
    __slots__ = ("_row",)
    def __getitem__(self, key):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.__getitem__(self, key)
    def get(self, key, default=None):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.get(self, key, default)
    def __contains__(self, key):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.__contains__(self, key)
    def setdefault(self, key, default=None):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.setdefault(self, key, default)
    def pop(self, key, *default):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.pop(self, key, *default)
    def __setitem__(self, key, value):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.__setitem__(self, key, value)
    def __delitem__(self, key):
        rec("k\t" + self._row + "\t" + kd(key)); return dict.__delitem__(self, key)
def _whole(name):
    original = getattr(dict, name)
    def method(self, *a, **k):
        rec("w\t" + self._row); return original(self, *a, **k)
    method.__name__ = name
    return method
for _name in ("__iter__", "keys", "values", "items", "__len__", "copy", "__eq__", "__ne__", "__repr__",
              "__reduce_ex__", "__reduce__", "__or__", "__ror__", "__ior__", "popitem", "clear", "update",
              "__reversed__", "__sizeof__", "__lt__", "__le__", "__gt__", "__ge__", "__format__", "__str__"):
    setattr(TDict, _name, _whole(_name))
TDict.__hash__ = None

class TModule(types.ModuleType):
    def __getattribute__(self, name):
        if stage[0] is not None and not (name.startswith("__") and name.endswith("__")):
            rec("m\t" + types.ModuleType.__getattribute__(self, "__name__") + "\t" + name)
        return types.ModuleType.__getattribute__(self, name)

SKIP = ds_rows.SKIP_CLASS
wrapped = {}
def track_dict(value, row):
    if type(value) is not dict:
        return value
    existing = wrapped.get(id(value))
    if existing is not None:
        return existing[1]
    t = TDict(value); t._row = row
    wrapped[id(value)] = (value, t)
    return t

def install():
    gen = sys.modules["sqlglot.generator"]
    by_name = {}
    for mname in ds_rows.sqlglot_modules():
        for c in ds_rows.classes_of(sys.modules[mname]):
            by_name[cname(c)] = c
    for name, table in ds_rows.dispatch_tables().items():
        gen._DISPATCH_CACHE[by_name[name]] = track_dict(table, "d " + name)
    for mname in ds_rows.sqlglot_modules():
        module = sys.modules[mname]
        for cls in ds_rows.classes_of(module):
            if issubclass(cls, enum.Enum):
                for internal in ("_member_map_", "_value2member_map_"):
                    if type(cls.__dict__.get(internal)) is dict:
                        type.__setattr__(cls, internal, track_dict(cls.__dict__[internal], f"c {cname(cls)} {internal}"))
                continue
            for attr, value in list(vars(cls).items()):
                if attr in SKIP or (attr.startswith("__") and attr.endswith("__")):
                    continue
                if isinstance(value, (types.MemberDescriptorType, types.GetSetDescriptorType)):
                    continue
                value = track_dict(value, f"c {cname(cls)} {attr}")
                data = hasattr(type(value), "__set__") or hasattr(type(value), "__delete__")
                try:
                    type.__setattr__(cls, attr, (ReadData if data else Read)(value, cname(cls), attr))
                except (TypeError, AttributeError):
                    pass
        for attr, value in list(vars(module).items()):
            if type(value) is dict and not attr.startswith("__"):
                module.__dict__[attr] = track_dict(value, f"m {mname} {attr}")
        if type(module) is types.ModuleType:
            module.__class__ = TModule
    for method in ("__iter__", "__len__", "__reversed__", "__contains__"):
        original = getattr(enum.EnumType, method)
        def hook(cls, *args, _original=original):
            if stage[0] is not None and str(cls.__module__).startswith("sqlglot"):
                rec("e\t" + cname(cls))
            return _original(cls, *args)
        setattr(enum.EnumType, method, hook)
    members = enum.EnumType.__dict__["__members__"]
    def members_hook(cls, _members=members):
        if stage[0] is not None and str(cls.__module__).startswith("sqlglot"):
            rec("e\t" + cname(cls))
        return _members.__get__(cls, type(cls))
    enum.EnumType.__members__ = property(members_hook)
    real = {n: getattr(builtins, n) for n in ("getattr", "hasattr", "setattr", "delattr", "dir", "vars")}
    def named(kind):
        f = real[kind]
        def wrapper(*args):
            if stage[0] is not None:
                if kind in ("dir", "vars"):
                    target = args[0] if args else None
                    if isinstance(target, types.ModuleType):
                        rec("r\tmodule:" + types.ModuleType.__getattribute__(target, "__name__"))
                    elif isinstance(target, type):
                        rec("r\t" + cname(target))
                    elif target is not None:
                        rec("r\t" + cname(type(target)))
                elif len(args) >= 2 and isinstance(args[1], str):
                    rec("n\t" + args[1])
            return f(*args)
        return wrapper
    for n in real:
        setattr(builtins, n, named(n))

code_seen = {}
TOOL = 3
def start(code, offset):
    if stage[0] is not None:
        path = relative(code.co_filename)
        if path is not None:
            code_seen.setdefault((path, code.co_qualname, code.co_firstlineno), code)
            rec("f\t" + path + "\t" + code.co_qualname + "\t" + str(code.co_firstlineno))
    return sys.monitoring.DISABLE

def begin(name):
    stage[0] = name
    if profiled:
        sys.monitoring.restart_events()

def h(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()

# ------------------------------------------------------------------ computation
outputs = {}
if profiled:
    install()
    sys.monitoring.use_tool_id(TOOL, "ds reads")
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, start)
    sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)

def class_setup():
    # TestExecutor.setUpClass builds both datasets; the pool receives one of them pickled
    begin("tables")
    built_h = {n: Table(columns=c, rows=r) for n, (c, r) in raw_h.items()}
    built_ds = {n: Table(columns=c, rows=r) for n, (c, r) in raw_ds.items()}
    data = pickle.dumps((TPCH_SCHEMA, built_h) if tpch else (TPCDS_SCHEMA, built_ds))
    stage[0] = None
    outputs["tables"] = h(data)

if kind == "gm":
    # test_generator_memory: every dialect's generator, constructed repeatedly
    from sqlglot.dialects.dialect import Dialect, Dialects
    begin("gm")
    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        dialect.generator()
        dialect.generator()
    stage[0] = None
    outputs["gm"] = "done"
elif kind == "oh":
    # test_optimized_tpch: DuckDB SQL for each TPC-H query and its optimized form
    class_setup()
    for i, (_, sql, optimized) in enumerate(pairs):
        begin(f"oh:{i}")
        a = transpile(sql, write="duckdb")[0]
        b = transpile(optimized, write="duckdb")[0]
        stage[0] = None
        outputs[f"oh{i}"] = h(a + "\n" + b)
elif kind == "parent":
    class_setup()
    for i, (meta, sql, _) in enumerate(pairs):
        begin(f"parse:{i}")
        blob = pickle.dumps(parse_one(sql))
        stage[0] = None
        outputs[f"blob{i}"] = h(blob)
    for i in executed:
        begin(f"duck:{i}")
        text = transpile(pairs[i][1], write="duckdb")[0]
        stage[0] = None
        outputs[f"duck{i}"] = h(text)
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        pd = None
    for i in executed:
        with open(os.path.join(results_dir, f"{i}.pickle"), "rb") as stream:
            payload = stream.read()
        begin(f"result:{i}")
        table = pickle.loads(payload)
        rows, columns = table.rows, table.columns
        if pd is not None:
            frame = pd.DataFrame(((np.nan if c is None else c for c in r) for r in rows), columns=columns)
        stage[0] = None
        outputs[f"result{i}"] = h(json.dumps([list(columns), [list(map(repr, r)) for r in rows]]))
elif kind == "worker" and tpch:
    # test_execute_tpch: the TPC-H fixtures carry no execute flag, so workers only unpickle
    begin("tables")
    _tables = pickle.loads(table_blob)
    stage[0] = None
    for i in range(len(pairs)):
        begin(f"unpickle:{i}")
        pickle.loads(blobs[i])
        stage[0] = None
    outputs["unpickled"] = len(pairs)
elif kind == "worker":
    real_optimize = executor_module.optimize
    current = [None]
    def staged_optimize(*args, **kwargs):
        stage[0] = f"opt:{current[0]}"
        if profiled:
            sys.monitoring.restart_events()
        try:
            return real_optimize(*args, **kwargs)
        finally:
            stage[0] = f"exec:{current[0]}"
            if profiled:
                sys.monitoring.restart_events()
    executor_module.__dict__["optimize"] = staged_optimize
    begin("tables")
    _tables = pickle.loads(table_blob)
    stage[0] = None
    for i in range(len(pairs)):
        if i not in executed:
            begin(f"unpickle:{i}")
            pickle.loads(blobs[i])
            stage[0] = None
    for i in executed:
        current[0] = i
        begin(f"load:{i}")
        expression = pickle.loads(blobs[i])
        tables = {t.name: _tables[t.name] for t in find_tables(expression)}
        result = execute(expression, schema=TPCDS_SCHEMA, tables=tables)
        payload = pickle.dumps(result)
        stage[0] = None
        outputs[f"exec{i}"] = h(json.dumps([list(result.columns), [list(map(repr, r)) for r in result.rows]]))
        if not profiled:
            with open(os.path.join(results_dir, f"{i}.pickle"), "wb") as stream:
                stream.write(payload)
    executor_module.__dict__["optimize"] = real_optimize

profile = {}
if profiled:
    sys.monitoring.set_events(TOOL, 0)
    glob = set()
    for (path, qualname, first), code in code_seen.items():
        for ins in dis.get_instructions(code):
            if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL", "LOAD_FROM_DICT_OR_GLOBALS"):
                glob.add((path, ins.argval))
    profile = {
        "names": names,
        "stages": {s: sorted(v) for s, v in reads.items()},
        "globals": sorted([list(x) for x in glob]),
        "co_names": sorted({n for code in code_seen.values() for n in code.co_names}),
        "co_strings": sorted({c for code in code_seen.values() for c in code.co_consts if isinstance(c, str) and len(c) < 120}),
        "modules": ds_rows.sqlglot_modules(),
    }
json.dump({"outputs": outputs, "profile": profile}, sys.stdout)
'''


SEEDS = ("0", "1", "2")


def run(root, role, profiled, modules_file, results, seed="0"):
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1", "SKIP_INTEGRATION": "0"}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, "-B", "-c", CHILD, str(HERE), str(root), role, "1" if profiled else "0",
                           str(modules_file), str(results)], capture_output=True, text=True, env=env)
    if proc.returncode:
        sys.exit(f"{role} profiled={profiled} failed:\n{proc.stderr[-4000:]}")
    return json.loads(proc.stdout)


def merge(profiles):
    names, index, stages = [], {}, {}
    for body in profiles:
        for stage, ids in body["stages"].items():
            bucket = stages.setdefault(stage, set())
            for i in ids:
                text = body["names"][i]
                if text not in index:
                    index[text] = len(names)
                    names.append(text)
                bucket.add(index[text])
    return {
        "names": names,
        "stages": {s: sorted(v) for s, v in sorted(stages.items())},
        "globals": sorted({tuple(g) for body in profiles for g in body["globals"]}),
        "co_names": sorted({n for body in profiles for n in body["co_names"]}),
        "co_strings": sorted({n for body in profiles for n in body["co_strings"]}),
        "modules": sorted({n for body in profiles for n in body["modules"]}),
    }


def main():
    root, dest = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    sys.path.insert(0, str(HERE))
    import fe_replay

    modules_path = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / "fe_modules.json.gz"
    with gzip.open(modules_path, "rt", encoding="utf-8") as stream:
        modules = json.load(stream)
    session = fe_replay._load("fe_baseline.json.gz")["session_modules"]
    import ds_rows

    record = {"version": 1, "python": sys.version, "tree": fe_replay.tree_hash(root), "roles": {},
              "digest_version": ds_rows.DIGEST_VERSION, "test_mode": True}
    worker_modules = modules.get("worker_order") or modules["worker"]
    from concurrent.futures import ThreadPoolExecutor

    # role in the child -> merged profile role; ":cleared" repeats a session role with the dialect registry cleared
    ROLES = [("worker:ds", worker_modules), ("parent:ds", session), ("parent:ds-cleared", session),
             ("worker:h", worker_modules), ("parent:h", session), ("parent:h-cleared", session),
             ("gm:", session), ("gm:cleared", session), ("oh:h", session), ("oh:h-cleared", session)]
    MERGED = [("worker", ("worker:ds",)), ("parent", ("parent:ds", "parent:ds-cleared")),
              ("worker_h", ("worker:h",)), ("parent_h", ("parent:h", "parent:h-cleared")),
              ("gm", ("gm:", "gm:cleared")), ("oh", ("oh:h", "oh:h-cleared"))]

    def one_seed(seed, tmp):
        results = Path(tmp) / f"results-{seed}"
        results.mkdir()
        found = {}
        for role, wanted in ROLES:
            mfile = Path(tmp) / f"{role.replace(':', '_')}-{seed}.json"
            mfile.write_text(json.dumps(wanted))
            plain = run(root, role, False, mfile, results, seed)
            traced = run(root, role, True, mfile, results, seed)
            if plain["outputs"] != traced["outputs"]:
                bad = sorted(k for k in plain["outputs"] if plain["outputs"][k] != traced["outputs"].get(k))
                raise SystemExit(f"{role} seed {seed}: profiled outputs differ from the plain run at {bad[:10]}")
            found[role] = (plain["outputs"], traced["profile"])
            print(role, "seed", seed, "stages", len(traced["profile"]["stages"]),
                  "records", len(traced["profile"]["names"]), flush=True)
        return seed, found

    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(2) as pool:
        runs = dict(pool.map(lambda seed: one_seed(seed, tmp), SEEDS))
    for kind, roles in MERGED:
        record["roles"][kind] = {
            "outputs": {seed: {role: runs[seed][role][0] for role in roles} for seed in SEEDS},
            **merge([runs[seed][role][1] for seed in SEEDS for role in roles]),
        }
    with gzip.open(dest, "wt", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True)


if __name__ == "__main__":
    main()
