"""Build the pinned expression namespace and its non-point consumer witnesses."""
import ast
import builtins
import hashlib
import json
import sys
from pathlib import Path

from coverage_map import Coverage
from expression_additions import PROFILE, REGISTRIES
from map_store import MAPS, read_map


def _opaque(node, path):
    if isinstance(node, ast.Attribute):
        if path.startswith("tests/") and node.attr in REGISTRIES | {
            "__dict__", "__subclasses__", "modules", "getsizeof", "get_objects",
            "get_traced_memory", "reload", "submit", "spawn", "Popen",
        }:
            return True
        if node.attr == "ALL_FUNCTIONS":
            return True
    if not isinstance(node, ast.Call):
        return False
    call = ast.unparse(node.func)
    if path.startswith("tests/") and call in {
        "dir", "vars", "eval", "exec", "globals", "locals", "subprocess.run",
        "importlib.import_module", "getattr", "hasattr", "setattr",
    }:
        return True
    return (call in {"getattr", "hasattr", "setattr", "dir", "vars"} and node.args
            and ast.unparse(node.args[0]) in {"exp", "module", "expressions"})


def record(root):
    sys.path.insert(0, str(root))
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.helper import camel_to_snake_case
    from sqlglot.parser import Parser

    cov = Coverage()
    # Generation inspects every row. The selector can keep its on-demand table.
    data = read_map(MAPS / "declarations-lookups.json.gz")
    tests = data["tests"]
    lookup = {tuple(row[:-1]): row[-1] for row in data["records"]}
    aliases = {(a, b): (c, d) for a, b, c, d in data.get("aliases", ())}
    opaque = set()
    reserved = {name.lower() for name in (*vars(exp), *vars(builtins))} | set(exp.EXPR_CLASSES)
    functions = set(exp.FUNCTION_BY_NAME)
    parser_owners = set()
    parser_classes = {Parser} | {d.parser_class for d in Dialect.classes.values()}
    for parser in parser_classes:
        owner = next(c for c in parser.__mro__ if "FUNCTIONS" in vars(c))
        key = (f"{owner.__module__}.{owner.__qualname__}", "FUNCTIONS")
        assert (*key, "*") in lookup, key
        parser_owners.add(key)
        functions.update(parser.FUNCTIONS)
    assert all(camel_to_snake_case(name) == name[:1].upper() + "".join(
        ("_" if c.isupper() else "") + c.upper() for c in name[1:]) for name in vars(exp))

    owners = parser_owners | {(owner, attr) for owner, attr, key in lookup
                              if attr in REGISTRIES and key == "*"}
    for attr in ("FUNCTION_BY_NAME", "EXPR_CLASSES"):
        owner = aliases.get(("sqlglot.expressions", attr), ("sqlglot.expressions", attr))
        assert (*owner, "*") in lookup, owner
        owners.add(owner)
    paths = {}
    modules, type_paths = [], []
    init = ast.parse((root / "sqlglot/expressions/__init__.py").read_text(encoding="utf-8"))
    exported = {stmt.module for stmt in init.body if isinstance(stmt, ast.ImportFrom)
                and any(alias.name == "*" for alias in stmt.names)}
    point_sites = set()
    for folder in ("sqlglot", "tests"):
        for source in sorted((root / folder).rglob("*.py")):
            path = source.relative_to(root).as_posix()
            content = source.read_text(encoding="utf-8")
            tree = ast.parse(content)
            module = path[:-3].replace("/", ".")
            if module in exported:
                core_names = {a.name for s in tree.body if isinstance(s, ast.ImportFrom)
                              and s.module == "sqlglot.expressions.core" for a in s.names if not a.asname}
                if {"Expression", "Func"} <= core_names:
                    modules.append(path)
            if path.startswith("sqlglot/typing/") and not path.endswith("__init__.py"):
                type_paths.append(path)
            if path in modules or path in type_paths:
                paths[path] = hashlib.sha256(content.encode()).hexdigest()
            danger = set()
            for n in ast.walk(tree):
                # Star exports connect the expression modules. A new bare name
                # must not change how an existing module resolves a global.
                if path.startswith("sqlglot/expressions/") and isinstance(n, ast.Name) and n.id[:1].isupper():
                    reserved.add(n.id.lower())
                if isinstance(n, ast.FunctionDef) and n.name.endswith("_sql"):
                    reserved.add(n.name[:-4].lower())
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id in {"exp", "expressions"}):
                    reserved.add(n.attr.lower())
                if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sqlglot.expressions"):
                    reserved.update(a.name.lower() for a in n.names)
                if _opaque(n, path):
                    danger.add(n.lineno)
                if (isinstance(n, ast.Attribute) and n.attr == "FUNCTIONS"
                        or isinstance(n, ast.Call) and ast.unparse(n.func) in {"functions.get", "_FUNCTION_BY_NAME.get"}):
                    point_sites.update(cov.lines(path, {n.lineno}))
            opaque.update(cov.lines(path, danger))
    observed = {tests[i] for (owner, attr, key), rows in lookup.items()
                if attr in {"FUNCTIONS", "FUNCTION_BY_NAME"} and key not in {"@", "*"} for i in rows}
    opaque.update(point_sites - observed)
    return dict(modules=sorted(modules), typing=sorted(type_paths), hashes=paths,
                reserved=sorted(reserved), functions=sorted(functions),
                owners=sorted(owners), opaque=sorted(opaque))


if __name__ == "__main__":
    PROFILE.write_text(json.dumps(record(Path(sys.argv[1]).resolve()), separators=(",", ":")), encoding="utf-8")
