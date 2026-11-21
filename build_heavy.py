import argparse
import ast
import atexit
import gzip
import importlib.machinery as machinery
import json
import os
import subprocess
import sys
import tempfile
from multiprocessing.util import Finalize
from pathlib import Path
from enum import Enum, EnumType

import heavy_profile as hp
import value_profile as vp

DEFAULT = [
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
    *("tests/test_executor.py::TestExecutor::test_" + n for n in
      ("execute_tpcds", "execute_tpch", "optimized_tpch", "agg_order")),
    *("tests/test_optimizer.py::TestOptimizer::test_" + n for n in
      ("tpcds", "scope", "annotate_funcs", "qualify_columns", "optimize_in_nulls",
       "merge_subqueries", "canonicalize", "qualify_struct_star_long_cte_chain")),
    "tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries",
    "tests/test_serde.py::TestSerde::test_recursion",
]
ENUM_CASES = {
    "test_max_nodes", "test_generator_memory", "test_execute_tpcds", "test_optimized_tpch",
    "test_execute_tpch", "test_scope", "test_tpcds",
}


class Observe:
    def __init__(self, kind, key):
        self.kind, self.key = kind, key
        self.span = None
        self.strings = set()
        self.seen = False
        self.special = 0
        self.always = True
        self.unknown = False
        self.enum_type = None
        self.last_member = None

    def enum(self, val, cls):
        self.seen = True
        if self.unknown or (self.enum_type is cls and self.last_member is val and self.enum_type is not None):
            return val
        if type(cls) is not EnumType or type(val) is not cls:
            self.unknown = True
        elif self.enum_type is None:
            eq, ne = cls.__eq__, cls.__ne__
            safe = (eq is object.__eq__ and ne is object.__ne__) or (
                eq is int.__eq__ and (ne is int.__ne__ or ne is object.__ne__)
            ) or (eq is str.__eq__ and (ne is str.__ne__ or ne is object.__ne__))
            if not safe or not any(cls.__hash__ is h for h in
                                   (Enum.__hash__, int.__hash__, str.__hash__, object.__hash__)):
                self.unknown = True
            else:
                self.enum_type = cls
        elif self.enum_type is not cls:
            self.unknown = True
        if not self.unknown:
            self.strings.add(object.__getattribute__(val, "_name_"))
            self.last_member = val
        return val

    def value(self, val):
        self.seen = True
        if self.unknown:
            return val
        t = type(val)
        if t is int and val.bit_length() <= 128:
            if self.span is None:
                self.span = [val, val]
            elif val < self.span[0]:
                self.span[0] = val
            elif val > self.span[1]:
                self.span[1] = val
        elif t is str and len(val) <= 128:
            self.strings.add(val)
            if len(self.strings) > 96:
                self.unknown = True
        elif t is bool or val is None:
            self.special |= 4 if val is None else 2 if val else 1
        else:
            self.unknown = True
        return val

    def dictionary(self, d):
        self.seen = True
        if not self.unknown:
            if type(d) is dict and len(d) <= 48:
                for k in d:
                    if type(k) is not str or len(k) > 128:
                        self.unknown = True
                        return d
                self.strings.update(d)
                self.always &= self.key in d
                if len(self.strings) > 256:
                    self.unknown = True
            else:
                self.unknown = True
        return d

    def truth(self, val):
        self.seen = True
        if self.special != 3:
            t = type(val)
            if (t is bool or val is None or t is int or t is str or t is dict or t is list or
                    t is tuple or t is set or t is frozenset or t is bytes or t is float):
                self.special |= 2 if val else 1
            else:
                self.special = 3
        return val

    def data(self):
        if self.unknown:
            return None
        if self.kind == "v":
            return [self.span, sorted(self.strings), self.special]
        if self.kind == "d":
            return [sorted(self.strings), self.always]
        if self.kind == "e":
            cls = self.enum_type
            members = {k: object.__getattribute__(v, "_name_") for k, v in cls.__members__.items()}
            raw = int.__int__ if cls.__eq__ is int.__eq__ else str.__str__ if cls.__eq__ is str.__eq__ else None
            if raw and len({raw(v) for v in cls.__members__.values()}) != len(set(members.values())):
                return None
            return [members, sorted(self.strings)]
        return self.special


class Instrument(ast.NodeTransformer):
    def __init__(self, bindings, sites):
        self.bindings, self.sites = bindings, sites

    def probe(self, node, expr, kind, key=None):
        ob = Observe(kind, key)
        self.sites[vp.location(node), kind] = ob
        name = "_operand_trace_heavy_" + str(len(self.sites))
        self.bindings[name] = {"v": ob.value, "d": ob.dictionary, "b": ob.truth, "e": ob.enum}[kind]
        args = [expr, ast.Name(key, ast.Load())] if kind == "e" else [expr]
        return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), args, []), expr)

    def visit_Compare(self, node):
        original = hp.compare(node)
        self.generic_visit(node)
        if original:
            if original[2]:
                node.comparators[0] = self.probe(node, node.comparators[0], "v")
            else:
                node.left = self.probe(node, node.left, "v")
        return node

    def visit_Call(self, node):
        eligible = hp.lookup(node)
        self.generic_visit(node)
        if eligible:
            node.func.value = self.probe(node, node.func.value, "d", node.args[0].value)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.values[0] = self.probe(node, node.values[0], "b")
        return node

    def visit_IfExp(self, node):
        self.generic_visit(node)
        node.test = self.probe(node, node.test, "b")
        return node


class EnumInstrument(Instrument):
    visit_Call = visit_BoolOp = visit_IfExp = ast.NodeTransformer.generic_visit

    def visit_Compare(self, node):
        spec = hp.enum_compare(node)
        self.generic_visit(node)
        if spec:
            node.left = self.probe(node, node.left, "e", spec[0])
        return node


class Importer:
    def __init__(self, root, folder):
        self.root, self.folder = root, folder
        self.sources, self.sites = {}, {}
        self.stamp = folder / str(os.getpid())
        self.saved = False

    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = machinery.PathFinder.find_spec(fullname, path)
        if not spec or not spec.origin or not spec.origin.endswith(".py"):
            return None
        if not self.sites:
            self.stamp.touch()
        spec.loader = self
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = Path(module.__file__).resolve()
        rel = path.relative_to(self.root).as_posix()
        text = path.read_text(encoding="utf8")
        sites = {}
        self.sites.setdefault(rel, []).append(sites)
        self.sources[rel] = vp.digest(text)
        transform = EnumInstrument if os.environ.get("OPERAND_TRACE_ENUM") == "1" else Instrument
        ast_tree = transform(vars(module), sites).visit(ast.parse(text, str(path)))
        exec(compile(ast.fix_missing_locations(ast_tree), str(path), "exec", dont_inherit=True), vars(module))

    def save(self):
        if not self.sites or self.saved:
            return
        out = []
        for path, executions in self.sites.items():
            for sites in executions:
                for (loc, kind), obs in sites.items():
                    if obs.seen:
                        out.append((path, loc, kind, obs.data()))
        self.stamp.with_suffix(".json").write_text(json.dumps([self.sources, out]), encoding="utf8")
        self.saved = True


def install():
    importer = Importer(Path(os.environ["OPERAND_TRACE_ROOT"]), Path(os.environ["OPERAND_TRACE_OUT"]))
    sys.meta_path.insert(0, importer)
    atexit.register(importer.save)
    Finalize(None, importer.save, exitpriority=-50)


def merge_files(files):
    merged = {"nodes": [], "hashes": {}, "sites": {}, "enums": {}}
    for file in files:
        with gzip.open(file, "rt") as s:
            data = json.load(s)
        for name in data["nodes"]:
            if name not in merged["nodes"]:
                merged["nodes"].append(name)
        merged["enums"].update(data.get("enums", {}))
        for path, digest in data["hashes"].items():
            if merged["hashes"].setdefault(path, digest) != digest:
                raise ValueError("inconsistent baseline")
        for path, sites in data["sites"].items():
            for loc, kinds in sites.items():
                for kind, records in kinds.items():
                    dest = merged["sites"].setdefault(path, {}).setdefault(loc, {}).setdefault(kind, {})
                    for i, rows in records.items():
                        dest.setdefault(str(merged["nodes"].index(data["nodes"][int(i)])), []).extend(rows)
    return merged


def collect(root, output, nodes):
    data = {"nodes": [], "hashes": {}, "sites": {}, "enums": {}}
    for node in nodes:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "sitecustomize.py").write_text("import build_heavy\nbuild_heavy.install()\n")
            env = dict(os.environ, OPERAND_TRACE_ROOT=str(root), OPERAND_TRACE_OUT=tmp,
                       PYTHONPATH=os.pathsep.join([tmp, str(Path(__file__).resolve().parent), str(root)]),
                       PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                       OPENBLAS_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2")
            print(node, flush=True)
            visit = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=line",
                                    "-p", "no:cacheprovider", node], env=env, cwd=root)
            markers = list(p for p in folder.iterdir() if p.name.isdigit())
            if visit.returncode or not markers or any(not p.with_suffix(".json").exists() for p in markers):
                print("Incomplete profile; retaining test", flush=True)
                continue
            index = str(len(data["nodes"]))
            data["nodes"].append(node)
            bucket = {}
            for mark in markers:
                hashes, rows = json.loads(mark.with_suffix(".json").read_bytes())
                data["hashes"].update(hashes)
                for path, loc, kind, row in rows:
                    bucket.setdefault((path, loc, kind), []).append(row)
            for (path, loc, kind), rows in bucket.items():
                if None not in rows:
                    if kind == "e":
                        for row in rows:
                            table = row[0]
                            key = vp.digest(json.dumps(table, sort_keys=True))
                            data["enums"][key] = table
                            row[0] = key
                    data["sites"].setdefault(path, {}).setdefault(loc, {}).setdefault(kind, {})[index] = rows
            save(data, output)
    return data


def save(data, output):
    for sites in data["sites"].values():
        for kinds in sites.values():
            for records in kinds.values():
                for index, rows in records.items():
                    unique = {json.dumps(row, sort_keys=True): row for row in rows}
                    records[index] = [unique[key] for key in sorted(unique)]
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    tmp = output.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(raw, mtime=0))
    tmp.replace(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("nodes", nargs="*")
    parser.add_argument("--merge", nargs="+", type=Path)
    parser.add_argument("--enums", action="store_true", help="collect symbolic enum operands")
    args = parser.parse_args()
    if args.enums:
        os.environ["OPERAND_TRACE_ENUM"] = "1"
    nodes = args.nodes or [n for n in DEFAULT if not args.enums or n.rsplit("::", 1)[-1] in ENUM_CASES]
    result = merge_files(args.merge) if args.merge else collect(args.root.resolve(), args.output.resolve(), nodes)
    save(result, args.output.resolve())
    print(len(result["nodes"]), "completed heavy tests", flush=True)
