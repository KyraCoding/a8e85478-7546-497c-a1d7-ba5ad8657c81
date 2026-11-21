import ast
import json
import sys
from pathlib import Path


def cache_sites(source):
    tree = ast.parse(source)
    globals_ = set()
    factories = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Dict):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            globals_.update(t.id for t in targets if isinstance(t, ast.Name))
    if not globals_:
        return []

    result = set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for read, guard in zip(fn.body, fn.body[1:]):
            if not (isinstance(read, ast.Assign) and len(read.targets) == 1
                    and isinstance(read.targets[0], ast.Name) and isinstance(read.value, ast.Call)
                    and isinstance(guard, ast.If)):
                continue
            lookup = read.value.func
            if not (isinstance(lookup, ast.Attribute) and lookup.attr == "get"
                    and isinstance(lookup.value, ast.Name) and lookup.value.id in globals_):
                continue
            local = read.targets[0].id
            tested = guard.test
            if not (isinstance(tested, ast.Compare) and len(tested.ops) == 1
                    and isinstance(tested.ops[0], ast.Is) and isinstance(tested.left, ast.Name)
                    and tested.left.id == local and isinstance(tested.comparators[0], ast.Constant)
                    and tested.comparators[0].value is None):
                continue
            stores = [target for stmt in guard.body if isinstance(stmt, ast.Assign)
                      and isinstance(stmt.value, ast.Name) and stmt.value.id == local
                      for target in stmt.targets if isinstance(target, ast.Subscript)
                      and isinstance(target.value, ast.Name) and target.value.id == lookup.value.id]
            if not stores:
                continue
            result.add((guard.lineno, guard.end_lineno, read.lineno))
            for stmt in guard.body:
                if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                        and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id in factories
                        and any(isinstance(t, ast.Name) and t.id == local for t in stmt.targets)):
                    factory = factories[stmt.value.func.id]
                    result.add((factory.lineno, factory.end_lineno, read.lineno))
    return sorted(result)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    entries = {p.relative_to(root).as_posix(): areas for p in (root / "sqlglot").rglob("*.py")
               if (areas := cache_sites(p.read_bytes()))}
    Path(sys.argv[2]).write_text(json.dumps(entries, sort_keys=True) + "\n", encoding="utf-8")
