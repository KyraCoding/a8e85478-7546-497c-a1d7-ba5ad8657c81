import ast
import functools
import gzip
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
import sys

import pytest


DEST = Path(__file__).resolve().parent / "maps" / "declarations.json.gz"
seen = set()
observations = defaultdict(list)
labels = {}
parents = {}
complete = set()
failed = set()
shared_reads = defaultdict(set)
lookup_mode = False
lookup_aliases = []


def label(*parts):
    return labels.setdefault(tuple(parts), len(labels))


def owner(cls):
    return f"{cls.__module__}.{cls.__qualname__}"


def lookup_key(key):
    if isinstance(key, Enum):
        return "member:" + key.name
    if isinstance(key, str):
        return "str:" + key
    if isinstance(key, type):
        return "member:" + key.__name__
    return None


def finalize(data, repo):
    from declarations import _declarations

    parents = data["parents"]
    cached, copies = set(), set()
    suffixes = defaultdict(set)
    for name in parents:
        parts = name.split(".")
        for start in range(len(parts)):
            suffixes[".".join(parts[start:])].add(name)

    def import_reads(node):
        if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
            cached.add(node.attr if isinstance(node, ast.Attribute) else node.id)
        for field, value in ast.iter_fields(node):
            if field == "body" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if getattr(node, "name", None) not in {"__new__", "__init_subclass__"}:
                    continue
            if isinstance(value, ast.AST):
                import_reads(value)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        import_reads(child)

    for file in (repo / "sqlglot").rglob("*.py"):
        tree = ast.parse(file.read_bytes())
        import_reads(tree)
        module = file.relative_to(repo).as_posix()[:-3].replace("/", ".").removesuffix(".__init__")
        for cls, attr, stmt in _declarations(tree, module):
            if cls not in parents:
                continue
            for expression in ast.walk(stmt.value):
                if isinstance(expression, ast.Attribute) and expression.attr == attr:
                    for src in suffixes[ast.unparse(expression.value)]:
                        if src not in parents[cls]:
                            copies.add((src, attr, cls))
    data["copies"] = sorted(copies)
    data["cached"] = sorted(cached)
    data["parents"] = dict(sorted(parents.items()))
    data["records"] = [r for r in data["records"] if len(r) < 4 or r[-2] == "*" or r[-1]]
    return gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0)


class ReadAttribute:
    def __init__(self, index, value):
        self.index = index
        self.value = value

    def __get__(self, obj, cls):
        seen.add(self.index)
        return self.value


class ReadDict(dict):
    def __init__(self, cls, name, mapping):
        super().__init__(mapping)
        self.all_values = label(owner(cls), name, "*")
        self.entries = {
            key: label(owner(cls), name, token)
            for key in mapping
            if (token := lookup_key(key)) is not None
        }

    def record(self, key):
        index = self.entries.get(key)
        if index is not None:
            seen.add(index)

    def __getitem__(self, key):
        self.record(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.record(key)
        return super().get(key, default)

    def items(self):
        seen.add(self.all_values)
        return super().items()

    def values(self):
        seen.add(self.all_values)
        return super().values()

    def copy(self):
        seen.add(self.all_values)
        return super().copy()

    def __eq__(self, other):
        seen.add(self.all_values)
        return super().__eq__(other)


class Probe:
    def trace(self, cls, name):
        self.origin = cls, name
        self.all_reads = label(cls, name, "*")
        self.exposed = label(cls, name, "@")
        self.cache = {}

    def record(self, key):
        index = self.cache.get(key)
        if index is None:
            token = lookup_key(key)
            if token is None or len(token) > 100:
                index = self.all_reads
            else:
                index = label(*self.origin, token)
                self.cache[key] = index
        seen.add(index)


class LookupDict(Probe, dict):
    def get(self, key, default=None):
        self.record(key)
        return dict.get(self, key, default)

    def __getitem__(self, key):
        self.record(key)
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self.record(key)
        return dict.__contains__(self, key)


class LookupSet(Probe, set):
    def __contains__(self, key):
        self.record(key)
        return set.__contains__(self, key)


def build_access(repo):
    from declarations import _declarations

    reads = defaultdict(lambda: defaultdict(set))
    opaque = defaultdict(lambda: defaultdict(set))
    eager = defaultdict(set)
    allowed = set()
    aliases = defaultdict(set)
    files = []
    for folder in ("sqlglot", "tests"):
        for path in sorted((repo / folder).rglob("*.py")):
            rel = path.relative_to(repo).as_posix()
            tree = ast.parse(path.read_bytes())
            module = rel[:-3].replace("/", ".").removesuffix(".__init__")
            files.append((rel, module, tree))
            if folder == "sqlglot":
                allowed.update(attr for _, attr, _ in _declarations(tree, module) if attr.isupper())

    def visit(node, parent, field, scope, runtime, stmt, assign):
        if isinstance(node, ast.stmt):
            stmt = node
        if isinstance(node, (ast.AnnAssign, ast.Assign)):
            target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
            assign = target.id if isinstance(target, ast.Name) else ""
        if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
            attr = node.attr if isinstance(node, ast.Attribute) else node.id
            if attr in allowed:
                if runtime:
                    reads[attr][rel].add(node.lineno)
                    direct = (
                        isinstance(parent, ast.Subscript) and field == "value"
                        or isinstance(parent, ast.Attribute) and parent.attr == "get"
                        or isinstance(parent, ast.Compare) and field == "comparators"
                        and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
                    )
                    if not direct:
                        opaque[attr][rel].update((node.lineno, stmt.lineno))
                elif attr != assign:
                    eager[attr].add(scope if isinstance(node, ast.Name) else "*")
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.asname and item.asname != item.name and item.name in allowed:
                    aliases[item.name].add(item.asname)
        for child_field, value in ast.iter_fields(node):
            child_runtime, child_scope = runtime, scope
            if isinstance(node, ast.ClassDef) and child_field == "body":
                child_scope = f"{scope}.{node.name}"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                child_runtime = child_field == "body" and getattr(node, "name", "") not in {"__new__", "__init_subclass__"}
            children = value if isinstance(value, list) else (value,)
            for child in children:
                if isinstance(child, ast.AST):
                    visit(child, node, child_field, child_scope, child_runtime, stmt, assign)

    for rel, module, tree in files:
        visit(tree, None, "", module, False, tree, "")
    dialect = next(tree for rel, _, tree in files if rel == "sqlglot/dialects/dialect.py")
    derived = defaultdict(set)
    for stmt in ast.walk(dialect):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "klass":
            for n in ast.walk(stmt.value):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "klass":
                    derived[n.attr].add(target.attr)
    formats = {}
    for attr in ("TIME_MAPPING", "FORMAT_MAPPING", "INVERSE_TIME_MAPPING"):
        affected = {attr}
        pending = [attr]
        while pending:
            extra = derived.get(pending.pop(), set()) - affected
            affected.update(extra)
            pending.extend(extra)
        formats[attr] = sorted(affected)
    from sqlglot import exp
    from sqlglot.tokens import TokenType
    from sqlglot.dialects.dialect import NormalizationStrategy
    symbols = {cls.__name__: sorted(cls.__members__) for cls in (TokenType, exp.DType, NormalizationStrategy)}
    symbols["exp"] = sorted(name for name in vars(exp) if not name.startswith("_"))
    def encode(sites):
        return {attr: {path: sorted(points) for path, points in rows.items()} for attr, rows in sites.items()}
    data = dict(reads=encode(reads), opaque=encode(opaque), eager={a:sorted(v) for a,v in eager.items()},
                aliases={a:sorted(v) for a,v in aliases.items()}, symbols=symbols, format_dependencies=formats)
    DEST.with_name("declarations-access.json.gz").write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))


def watch_bulk(cls, base, methods):
    def monitored(method):
        @functools.wraps(method)
        def call(self, /, *args, **kwargs):
            seen.add(self.all_reads)
            return method(self, *args, **kwargs)
        return call
    for name in methods.split():
        setattr(cls, name, monitored(getattr(base, name)))


watch_bulk(LookupDict, dict, "__iter__ __len__ __eq__ __ne__ __or__ __ror__ copy items keys values pop popitem setdefault update")
watch_bulk(LookupSet, set, "__iter__ __len__ __eq__ __ne__ __or__ __ror__ __and__ __rand__ __xor__ __rxor__ __sub__ __rsub__ __lt__ __le__ __gt__ __ge__ copy difference intersection isdisjoint issubset issuperset symmetric_difference union")


def install_lookups(classes, generator):
    modules = [m for name, m in sys.modules.copy().items() if name.startswith("sqlglot.") and m]
    aliases = {}

    def observe(value, cls, name):
        wrapper = LookupSet(value) if type(value) is set else LookupDict(value)
        wrapper.trace(cls, name)
        return wrapper

    for module in modules:
        for name, value in list(vars(module).items()):
            if name.isupper() and type(value) in (dict, set):
                aliases.setdefault(id(value), []).append((module, name, value))
    for group in aliases.values():
        source, original_name, original_value = group[0]
        wrapped = observe(original_value, source.__name__, original_name)
        for module, name, value in group:
            setattr(module, name, wrapped)
            if (module, name) != (source, original_name):
                lookup_aliases.append((module.__name__, name, source.__name__, original_name))
    for cls in classes:
        if not cls.__module__.startswith("sqlglot."):
            continue
        for name, value in list(vars(cls).items()):
            if name.isupper() and type(value) in (dict, set):
                wrapper = observe(value, owner(cls), name)
                setattr(cls, name, ReadAttribute(wrapper.exposed, wrapper))
    original = generator._build_dispatch
    def dispatch(cls):
        return observe(original(cls), owner(cls), "TRANSFORMS")
    generator._build_dispatch = dispatch
    generator._DISPATCH_CACHE.clear()


def subclasses(cls):
    result = {cls}
    for child in cls.__subclasses__():
        result.update(subclasses(child))
    return result


def watch_init(cls):
    original = cls.__init__
    indices = {c: label(owner(c)) for c in subclasses(cls)}

    @functools.wraps(original)
    def init(instance, /, *args, **kwargs):
        index = indices.get(type(instance))
        if index is not None:
            seen.add(index)
        return original(instance, *args, **kwargs)

    cls.__init__ = init


def pytest_collection_finish(session):
    from sqlglot import exp, generator
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    _ = Dialect.classes
    def local_worker(*args, mp_context=None, **kwargs):
        return ThreadPoolExecutor(max_workers=1, **kwargs)

    for item in session.items:
        if "ProcessPoolExecutor" in vars(item.module):
            item.module.ProcessPoolExecutor = local_worker
    roots = (Dialect, Parser, generator.Generator, Tokenizer, exp.Expr)
    classes = set().union(*(subclasses(root) for root in roots))
    if lookup_mode:
        install_lookups(classes, generator)
        return
    for cls in classes:
        if not cls.__module__.startswith("sqlglot."):
            continue
        base_names = {owner(base) for base in cls.__mro__ if base in classes}
        parents[owner(cls)] = sorted(set(parents.get(owner(cls), ())) | base_names)
        for name, value in list(vars(cls).items()):
            if name.isupper() and isinstance(value, (str, int, float, bool, Enum, type(None))):
                setattr(cls, name, ReadAttribute(label(owner(cls), name), value))
            elif issubclass(cls, exp.Expr) and name in {"arg_types", "required_args"}:
                setattr(cls, name, ReadAttribute(label(owner(cls), name), value))
            elif type(value) is dict and (
                name.endswith("_PARSERS")
                or name in {"TRANSFORMS", "FUNCTIONS", "TYPE_MAPPING", "KEYWORDS", "NUMERIC_ESCAPES"}
            ):
                setattr(cls, name, ReadDict(cls, name, value))

    original = generator._build_dispatch

    def dispatch(cls):
        return ReadDict(cls, "TRANSFORMS", original(cls))

    generator._build_dispatch = dispatch
    generator._DISPATCH_CACHE.clear()
    for root in roots:
        watch_init(root)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.scope == "function":
        return (yield)
    before = set(seen)
    seen.clear()
    try:
        return (yield)
    finally:
        shared_reads[request.node.nodeid].update(seen)
        seen.update(before)


def pytest_runtest_logstart(nodeid, location):
    seen.clear()


def pytest_runtest_logreport(report):
    if report.failed or report.skipped:
        failed.add(report.nodeid)
    if report.when == "call" and report.passed:
        complete.add(report.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    for index in seen:
        observations[index].append(nodeid)


def pytest_sessionfinish(session, exitstatus):
    if exitstatus not in (0, 1) or not complete:
        return
    good = sorted(complete - failed)
    for prefix, hits in shared_reads.items():
        group = [name for name in good if not prefix or name.startswith(prefix + "::")]
        for index in hits:
            observations[index].extend(group)
    test_index = {name: i for i, name in enumerate(good)}
    records = [
        [*key, sorted({test_index[name] for name in observations[idx] if name in test_index})]
        for key, idx in sorted(labels.items())
        if len(key) < 3 or key[-1] == "*" or observations[idx]
    ]
    result = {"parents": parents, "tests": good, "records": records}
    DEST.parent.mkdir(exist_ok=True)
    tmp = DEST.with_suffix(".tmp")
    if lookup_mode:
        compact = {"tests": good, "records": records, "aliases": lookup_aliases}
        tmp.write_bytes(gzip.compress(json.dumps(compact, separators=(",", ":")).encode(), mtime=0))
    else:
        tmp.write_bytes(finalize(result, Path.cwd()))
    tmp.replace(DEST)


if __name__ == "__main__":
    repo = Path(sys.argv[1]).resolve()
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if "--access" in sys.argv:
        build_access(repo)
        sys.exit(0)
    if "--lookups" in sys.argv:
        sys.argv.remove("--lookups")
        lookup_mode = True
        DEST = DEST.with_name("declarations-lookups.json.gz")
    sys.exit(pytest.main(["-q", "--tb=line", "-p", "no:cacheprovider", *(sys.argv[2:] or ["tests"])],
                         plugins=[sys.modules[__name__]]))
