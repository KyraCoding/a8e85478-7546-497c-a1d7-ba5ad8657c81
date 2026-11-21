import ast
import gzip
import json
import os
import re
from collections import defaultdict
from functools import cache
from pathlib import Path


@cache
def _index():
    path = Path(__file__).resolve().parent / "maps" / "declarations.json.gz"
    with gzip.open(path, "rb") as stream:
        data = json.load(stream)
    descendants = defaultdict(set)
    for child, bases in data["parents"].items():
        for base in bases:
            descendants[base].add(child)
    copies = defaultdict(set)
    for src, attr, dst in data.get("copies", ()):
        copies[src, attr].add(dst)
    records = {tuple(row[:-1]): row[-1] for row in data["records"]}
    return descendants, records, data["tests"], set(data["cached"]), copies


def _declarations(node, owner):
    for stmt in node.body:
        if isinstance(stmt, ast.ClassDef):
            yield from _declarations(stmt, f"{owner}.{stmt.name}")
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            target = stmt.target if isinstance(stmt, ast.AnnAssign) else stmt.targets[0]
            if isinstance(target, ast.Name) and stmt.value is not None:
                yield owner, target.id, stmt


def _key(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "str:" + node.value
    if isinstance(node, ast.Attribute):
        return "member:" + node.attr
    return None


@cache
def _lookup_index():
    folder = Path(__file__).resolve().parent / "maps"
    with gzip.open(folder / "declarations-lookups.json.gz", "rb") as f:
        data = json.load(f)
    with gzip.open(folder / "declarations-access.json.gz", "rb") as f:
        access = json.load(f)
    aliases = {(a, b): (c, d) for a, b, c, d in data.get("aliases", ())}
    return data["tests"], {tuple(r[:-1]): r[-1] for r in data["records"]}, aliases, access


def _safe_reference(node, known, symbols):
    if isinstance(node, (ast.Constant, ast.Lambda)):
        return isinstance(node, ast.Constant) or not (node.args.defaults or any(node.args.kw_defaults))
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = ast.unparse(node)
        if name in known:
            return True
        owner, _, member = name.rpartition(".")
        if owner.split(".")[0] not in known:
            return False
        if owner == "exp.DType":
            owner = "DType"
        return member in symbols.get(owner, ())
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_safe_reference(e, known, symbols) for e in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)
    return False


def _entry_edits(a, b, symbols):
    if isinstance(a, ast.BinOp) and isinstance(b, ast.BinOp) and isinstance(a.op, (ast.BitOr, ast.Sub)):
        if type(a.op) != type(b.op) or ast.dump(a.left) != ast.dump(b.left):
            return None
        a, b = a.right, b.right
    if not isinstance(a, (ast.Dict, ast.Set)) or type(a) != type(b):
        return None
    known = set()
    pending = [a]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Lambda, ast.SetComp, ast.ListComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, (ast.Name, ast.Attribute)):
            known.add(ast.unparse(node))
        pending.extend(ast.iter_child_nodes(node))
    def entries(node):
        items = zip(node.keys, node.values) if isinstance(node, ast.Dict) else ((k, None) for k in node.elts)
        result = {}
        for k, v in items:
            if k is None or isinstance(k, ast.Starred):
                key = "spread:" + ast.dump(v if k is None else k)
            else:
                key = _key(k)
                if key is None or not _safe_reference(k, known, symbols):
                    return None
            if key in result:
                return None
            result[key] = v
        return result
    x, y = entries(a), entries(b)
    if x is None or y is None:
        return None
    shared = x.keys() & y.keys()
    if [k for k in x if k in shared] != [k for k in y if k in shared]:
        return None
    changes = x.keys() ^ y.keys()
    shape = bool(changes)
    for key in shared:
        if x[key] is not None and ast.dump(x[key]) != ast.dump(y[key]):
            changes.add(key)
    for key in changes:
        if key.startswith("spread:"):
            return None
        v = y.get(key)
        if v is not None and not _safe_reference(v, known, symbols):
            return None
    return changes, shape


def _observed(rows, cov, relevant=None):
    found = set()
    for filename, lines in rows.items():
        found.update(cov.lines(filename, lines))
        if relevant is not None and relevant <= found:
            break
    return found


def _lookup_candidates(cls, attr, impacted, keys, shape, module, cov):
    tests, records, aliases, access = _lookup_index()
    origins = [aliases.get((owner, attr), (owner, attr)) for owner in impacted]
    if not any((*origin, "*") in records for origin in origins):
        return None
    eager = set(access["eager"].get(attr, ()))
    point_dispatch = attr in {"FUNCTIONS", "TRANSFORMS", "TYPE_MAPPING"}
    word_keyword = attr == "KEYWORDS" and (
        not shape or all(re.fullmatch("str:[A-Z][A-Z0-9]*", key) for key in keys)
    )
    derived = ()
    if cls != module and attr in {"TIME_MAPPING", "FORMAT_MAPPING", "INVERSE_TIME_MAPPING"}:
        derived = access.get("format_dependencies", {}).get(attr, ())
    if not point_dispatch and not word_keyword and not derived and ("*" in eager or eager & impacted):
        return None
    if cls == module and (attr in access["aliases"] or eager or attr in _index()[3]):
        return None
    exposed, hit = set(), set()
    for origin in origins:
        exposed.update(records.get((*origin, "@"), ()))
        for key in ("*", *keys):
            hit.update(records.get((*origin, key), ()))
    if derived:
        hit.update(exposed)
        for owner in impacted:
            for name in derived:
                hit.update(records.get((owner, name, "@"), ()))
    result = {tests[i] for i in hit}
    missing = set(cov.nodes) - set(tests)
    reads = access["reads"].get(attr, {})
    if not reads:
        return None
    result.update(missing & _observed(reads, cov, missing))
    for name in derived:
        result.update(missing & _observed(access["reads"].get(name, {}), cov, missing))
    if not point_dispatch and not word_keyword:
        opaque = _observed(access["opaque"].get(attr, {}), cov)
        result.update(opaque if cls == module else opaque & {tests[i] for i in exposed})
    return result


def _value_keys(before, after):
    if not isinstance(before, ast.Dict) or not isinstance(after, ast.Dict):
        return None
    if len(before.keys) != len(after.keys):
        return None
    changed = []
    for k, v, k2, v2 in zip(before.keys, before.values, after.keys, after.values):
        if (ast.dump(k) if k else None) != (ast.dump(k2) if k2 else None):
            return None
        if ast.dump(v) != ast.dump(v2):
            token = _key(k)
            if token is None:
                return None
            changed.append(token)
    return changed


def _literal_edits(before, after):
    if type(before) is not type(after):
        if isinstance(before, (ast.Constant, ast.UnaryOp)) and isinstance(after, (ast.Constant, ast.UnaryOp)):
            try:
                a, b = ast.literal_eval(before), ast.literal_eval(after)
                return type(a) is type(b) and isinstance(a, (int, float))
            except ValueError:
                pass
        return False
    if isinstance(before, ast.Constant):
        return type(before.value) is type(after.value)
    if isinstance(before, list):
        return len(before) == len(after) and all(_literal_edits(x, y) for x, y in zip(before, after))
    if isinstance(before, ast.AST):
        return all(_literal_edits(a, getattr(after, field)) for field, a in ast.iter_fields(before))
    return before == after


@cache
def _module_references(root, attr):
    refs = defaultdict(set)
    word = re.compile(rb"\b" + re.escape(attr.encode()) + rb"\b")
    for folder in ("sqlglot", "tests"):
        for path in (root / folder).rglob("*.py"):
            source = path.read_bytes()
            if not word.search(source):
                continue
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(source, relative)
            todo = [(tree, False, tree)]
            while todo:
                node, runtime, stmt = todo.pop()
                if isinstance(node, ast.stmt):
                    stmt = node
                if isinstance(node, ast.alias) and node.name == attr and node.asname:
                    return None
                symbol = (
                    isinstance(node, ast.Name) and node.id == attr and isinstance(node.ctx, ast.Load)
                    or isinstance(node, ast.Attribute) and node.attr == attr and isinstance(node.ctx, ast.Load)
                    or isinstance(node, ast.Constant) and node.value == attr
                )
                if symbol:
                    if not runtime:
                        return None
                    refs[relative].update((node.lineno, stmt.lineno))
                for field, value in ast.iter_fields(node):
                    active = runtime
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        active = field == "body"
                    if isinstance(value, list):
                        todo.extend((item, active, stmt) for item in value if isinstance(item, ast.AST))
                    elif isinstance(value, ast.AST):
                        todo.append((value, active, stmt))
    return refs or None


def declaration_candidates(path, original, patched, old_lines, cov):
    if not path.startswith("sqlglot/"):
        return None
    old, new = ast.parse(original), ast.parse(patched)
    module = path[:-3].replace("/", ".").removesuffix(".__init__")
    left = list(_declarations(old, module))
    right = list(_declarations(new, module))
    if len(left) != len(right):
        return None
    changes = []
    for (cls, attr, a), (cls2, attr2, b) in zip(left, right):
        if (cls, attr) != (cls2, attr2):
            return None
        if old_lines and not any(a.lineno <= line <= a.end_lineno for line in old_lines):
            continue
        before, after = a.value, b.value
        if ast.dump(before) == ast.dump(after):
            continue
        keys = _value_keys(before, after)
        extended = None
        container = before.right if isinstance(before, ast.BinOp) else before
        if not _literal_edits(before, after) or (isinstance(container, (ast.Dict, ast.Set)) and not keys):
            symbols = _lookup_index()[3]["symbols"]
            extended = _entry_edits(before, after, symbols)
            if extended is None and not (
                isinstance(before, ast.Attribute) and isinstance(after, ast.Attribute)
                and ast.dump(before.value) == ast.dump(after.value)
                and _safe_reference(after, {ast.unparse(before).split(".")[0]}, symbols)
                or isinstance(before, ast.Constant) and isinstance(after, ast.Constant)
            ):
                return None
        if attr in {"TIME_MAPPING", "FORMAT_MAPPING", "INVERSE_TIME_MAPPING"}:
            if not isinstance(after, ast.Dict) or any(
                not isinstance(k, ast.Constant) or type(k.value) is not str
                or not isinstance(v, ast.Constant) or type(v.value) is not str
                for k, v in zip(after.keys, after.values) if k is not None
            ):
                return None
        changes.append((cls, attr, keys, extended))
        a.value = b.value = ast.Constant(None)
    if not changes or ast.dump(old) != ast.dump(new):
        return None

    descendants, records, tests, cached, copies = _index()
    names = set(cov.nodes).difference(tests)
    hits = set()
    for cls, attr, keys, extended in changes:
        if cls == module and not extended:
            if not attr.isupper():
                return None
            if keys:
                result = _lookup_candidates(cls, attr, {cls}, keys, False, module, cov)
                if result is not None:
                    names.update(result)
                    continue
            root = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
            refs = _module_references(root, attr)
            if not refs:
                return None
            for filename, lines in refs.items():
                names.update(cov.lines(filename, lines))
            continue
        if cls != module and cls not in descendants:
            return None
        impacted = set(descendants.get(cls, {cls}))
        pending = list(impacted)
        while pending:
            for target in copies.get((pending.pop(), attr), ()):
                additions = descendants[target] - impacted
                impacted.update(additions)
                pending.extend(additions)
        if extended and attr != "arg_types":
            changed_keys, shape = extended
            if not shape and (cls, attr, "*") in records:
                keys = changed_keys
            else:
                result = _lookup_candidates(cls, attr, impacted, changed_keys, shape, module, cov)
                if result is None:
                    return None
                names.update(result)
                continue
        if keys and (cls, attr, "*") in records:
            for child in impacted:
                for key in ["*", *keys]:
                    hits.update(records.get((child, attr, key), ()))
        elif attr == "arg_types" and (cls, attr) in records:
            for child in impacted:
                hits.update(records.get((child,), ()))
                for name in ("arg_types", "required_args"):
                    hits.update(records.get((child, name), ()))
        elif (cls, attr) in records and attr not in cached:
            hits.update(records[cls, attr])
        elif keys:
            result = _lookup_candidates(cls, attr, impacted, keys, False, module, cov)
            if result is None:
                return None
            names.update(result)
        else:
            return None
    return names | {tests[i] for i in hits} | cov.missing
