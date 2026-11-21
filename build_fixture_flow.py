import ast
import copy
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

from coverage_map import Coverage


LOADERS = {"load_sql_fixture_pairs": 3, "load_sql_fixtures": 1}


def constant(node, bindings):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.FormattedValue):
        return constant(node.value, bindings)
    if isinstance(node, ast.JoinedStr):
        items = [constant(v, bindings) for v in node.values]
        if all(isinstance(v, str) for v in items):
            return "".join(items)
    return None


def loaded_rows(node, bindings):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in LOADERS and node.args:
        path = constant(node.args[0], bindings)
        if isinstance(path, str):
            return ("tests/fixtures/" + path, node.func.id)
    return None


def method_flow(methods, entry, attrs):
    hits = defaultdict(set)
    active = set()

    def analyze(name, bindings):
        signature = (name, tuple(sorted(bindings.items())))
        if signature in active:
            return
        active.add(signature)

        class Specialize(ast.NodeTransformer):
            def visit_IfExp(self, node):
                value = constant(node.test, bindings)
                if type(value) is bool:
                    return self.visit(node.body if value else node.orelse)
                return self.generic_visit(node)

        fn = Specialize().visit(copy.deepcopy(methods[name]))
        nodes = list(ast.walk(fn))
        parents = {child: n for n in nodes for child in ast.iter_child_nodes(n)}
        reads = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

        def symbol(n):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in ("self", "cls"):
                return attrs.get(n.attr)
            return loaded_rows(n, bindings) or constant(n, bindings)

        def direct_call(n):
            return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in ("self", "cls")
                    and n.func.attr in methods)

        for node in nodes:
            if (isinstance(node, ast.Name) and node.id in ("self", "cls")
                    and isinstance(node.ctx, ast.Load) and not isinstance(parents.get(node), ast.Attribute)):
                for ref in attrs.values():
                    hits[ref].update(range(-1, LOADERS[ref[1]]))
            if direct_call(node):
                callee = methods[node.func.attr]
                args = {p.arg: symbol(v) for p, v in zip(callee.args.args[1:], node.args)}
                args.update({k.arg: symbol(k.value) for k in node.keywords if k.arg})
                analyze(callee.name, args)
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in ("self", "cls") and node.attr in methods
                    and not direct_call(parents.get(node))):
                analyze(node.attr, {})
            ref = symbol(node)
            if not isinstance(ref, tuple):
                continue
            p = parents.get(node)
            if direct_call(p) or (isinstance(p, ast.keyword) and direct_call(parents.get(p))):
                continue
            width = LOADERS[ref[1]]
            target = None
            if isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "enumerate":
                loop = parents.get(p)
                if isinstance(loop, (ast.For, ast.comprehension)) and isinstance(loop.target, ast.Tuple):
                    target = loop.target.elts[-1]
            elif isinstance(p, (ast.For, ast.comprehension)) and p.iter is node:
                target = p.target
            elif isinstance(p, ast.Subscript) and p.value is node:
                assignment = parents.get(p)
                if isinstance(assignment, ast.Assign):
                    target = assignment.targets[0]
            used = set(range(width))
            if (isinstance(target, ast.Tuple) and len(target.elts) == width
                    and all(isinstance(x, ast.Name) for x in target.elts)):
                used = {i for i, x in enumerate(target.elts) if x.id in reads}
            hits[ref].update({-1, *used})

    for name in (entry, "setUp", "tearDown", "tearDownClass"):
        if name in methods:
            analyze(name, {})
    return hits


def build(root, cov):
    result = defaultdict(dict)
    for file in sorted((root / "tests").rglob("test_*.py")):
        rel = file.relative_to(root).as_posix()
        text = file.read_text(encoding="utf-8")
        if not any(loader in text for loader in LOADERS):
            continue
        for cls in (n for n in ast.parse(text).body if isinstance(n, ast.ClassDef)):
            methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
            attrs = {}
            setup = methods.get("setUpClass")
            for stmt in (setup.body if setup else []):
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Attribute)
                        and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name)
                        and stmt.value.func.id == "list" and len(stmt.value.args) == 1):
                    ref = loaded_rows(stmt.value.args[0], {})
                    if ref:
                        attrs[stmt.targets[0].attr] = ref
            if setup and any(isinstance(n, ast.Attribute) and n.attr in attrs and isinstance(n.ctx, ast.Load)
                             for n in ast.walk(setup)):
                continue
            for entry in (n for n in methods if n.startswith("test_")):
                node = f"{rel}::{cls.name}::{entry}"
                hits = method_flow(methods, entry, attrs)
                for ref in set(hits) | set(attrs.values()):
                    path, loader = ref
                    if node in cov.fixture(path):
                        result[path][node] = [loader, sorted(hits.get(ref, ()))]
    return result


if __name__ == "__main__":
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    data = build(root, Coverage())
    out.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
