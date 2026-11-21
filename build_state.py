import argparse
import ast
import os
import sys
import gzip
import hashlib
from pathlib import Path

from state_diff import MAP, TIME_FIELDS, VERSION, _literal_map, _statements, _child, _json, _module, _runtime


def time_certificates(root, rows, descendants):
    import pytest
    sys.path.insert(0, str(root))
    os.chdir(root)
    from sqlglot.dialects.dialect import Dialect, _Dialect

    owners = {owner for module in rows for owner in rows[module] if
              module.startswith("sqlglot.dialects.") and owner.count(".") == 3 and
              owner not in descendants and TIME_FIELDS.intersection(rows[module][owner])}
    owners.difference_update(c.__module__ + "." + c.__qualname__ for c in _Dialect._classes.values())
    fields = TIME_FIELDS | {"__dict__"}
    init = _Dialect.__new__.__code__
    class_get, instance_get = _Dialect.__getattribute__, Dialect.__getattribute__

    def observe(cls, name, caller):
        if name in fields and caller is not init:
            owners.discard(class_get(cls, "__module__") + "." + class_get(cls, "__qualname__"))

    def get_class(cls, name):
        observe(cls, name, sys._getframe(1).f_code)
        return class_get(cls, name)

    def get_instance(obj, name):
        observe(type(obj), name, sys._getframe(1).f_code)
        return instance_get(obj, name)

    collected = []
    class Collection:
        def pytest_collection_finish(self, session):
            collected.extend(item.nodeid for item in session.items)

    _Dialect.__getattribute__ = get_class
    Dialect.__getattribute__ = get_instance
    try:
        _ = Dialect.classes
        if pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", "tests"],
                       plugins=[Collection()]) != 0:
            owners.clear()
    finally:
        del _Dialect.__getattribute__
        del Dialect.__getattribute__

    reflection = {}
    for folder in ("sqlglot", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            points = set()
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "__dict__", "__getattribute__", "getattr_static", "getmembers", "getmembers_static"
                }:
                    points.add(node.lineno)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars":
                    points.add(node.lineno)
                if isinstance(node, ast.FunctionDef) and node.name == "__new__" and path.name == "dialect.py":
                    points.update(range(node.lineno, node.end_lineno + 1))
            if points:
                reflection[path.relative_to(root).as_posix()] = sorted(points)
    return {"owners": sorted(owners), "collected": sorted(collected), "reflection": reflection}


def build(root, output, certify=False):
    root, output = root.resolve(), output.resolve()
    sources = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
               for p in sorted((root / "sqlglot").rglob("*.py")) if p.stem != "__main__"}
    rows, parents = _child(root, {_module(p) for p in sources}, [], building=True)
    repeat, _ = _child(root, {_module(p) for p in sources}, [], building=True)
    for module, owners in rows.items():
        for owner, attrs in owners.items():
            for name, digest in attrs.items():
                if repeat.get(module, {}).get(owner, {}).get(name) != digest:
                    attrs[name] = None
    ranges = {}
    for path in sources:
        tree = ast.parse((root / path).read_bytes())
        spans = [[stmt.lineno, stmt.end_lineno] for _, name, stmt in _statements(tree.body, _module(path))
                 if name.isupper() and _literal_map(stmt.value) is not None]
        if spans:
            ranges[path] = spans
    descendants = {}
    for module, owners in rows.items():
        for owner in owners:
            for base in parents.get(owner, ()):
                descendants.setdefault(base, set()).add(module)
    info = {"version": VERSION, "runtime": _runtime(), "sources": sources, "rows": rows, "ranges": ranges,
            "descendants": {k: sorted(v) for k, v in descendants.items()}}
    if certify:
        info["time"] = time_certificates(root, rows, descendants)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".tmp")
    temp.write_bytes(gzip.compress(_json(info).encode("ascii"), mtime=0))
    temp.replace(output)
    print(f"{len(rows)}/{len(sources)} modules; {sum(len(v) for v in rows.values())} owners; {output.stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path, nargs="?", default=MAP)
    parser.add_argument("--certify", action="store_true", help="profile imports and pytest collection")
    args = parser.parse_args()
    build(args.root, args.output, args.certify)
