import ast
import difflib
import marshal
import os
from functools import lru_cache
from importlib.util import MAGIC_NUMBER, cache_from_source
from types import CodeType

try:
    import _ast_native
except ImportError:
    _ast_native = None


def _dump_key(node):
    if _ast_native is not None:
        try:
            return _ast_native.dump(node, ast.AST, "_fields")
        except (AttributeError, TypeError, ValueError, RecursionError):
            pass
    return ast.dump(node)


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_DEFINITION_SCOPES = (*_DEFINITIONS, ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                      ast.AsyncWith, ast.Try, ast.TryStar, ast.ExceptHandler, ast.Match, ast.match_case)


def _without_positions(code):
    return code.replace(
        co_firstlineno=1,
        co_linetable=b"",
        co_consts=tuple(
            _without_positions(value) if isinstance(value, CodeType) else value
            for value in code.co_consts
        ),
    )


def _pinned_code(path, data):
    """Bytecode the image compiled from exactly these source bytes, or None.

    compileall used the same flags as compile(..., "exec", dont_inherit=True); a timestamp .pyc whose
    recorded mtime and size match the unchanged file, holding these exact bytes, is that compilation.
    """
    try:
        file = os.path.join(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"), *path.split("/"))
        with open(file, "rb") as stream:
            if stream.read() != data:
                return None
            stat = os.fstat(stream.fileno())
        with open(cache_from_source(file), "rb") as stream:
            pyc = stream.read()
        if (pyc[:4] != MAGIC_NUMBER or pyc[4:8] != b"\0\0\0\0"
                or int.from_bytes(pyc[8:12], "little") != int(stat.st_mtime) & 0xFFFFFFFF
                or int.from_bytes(pyc[12:16], "little") != stat.st_size & 0xFFFFFFFF):
            return None
        code = marshal.loads(pyc[16:])
    except (OSError, ValueError, EOFError, TypeError, NotImplementedError):
        return None
    return code if type(code) is CodeType else None


@lru_cache(maxsize=6)
def _baseline(path, source):
    data = source.encode("utf-8")
    tree = ast.parse(data, path)
    code = _pinned_code(path, data)
    if code is None:
        code = compile(tree, path, "exec", dont_inherit=True)
    lines = set()

    def collect(block):
        lines.update(n for start, end, n in block.co_lines() if end > start and n)
        for value in block.co_consts:
            if isinstance(value, CodeType):
                collect(value)

    collect(code)
    return tree, _without_positions(code), lines


@lru_cache(maxsize=4)
def _patched(path, source):
    tree = ast.parse(source.encode("utf-8"), path)
    # Compile before any caller can see the tree, so later edits to it cannot leak into the code.
    try:
        code = compile(tree, path, "exec", dont_inherit=True)
    except Exception as exc:
        # Drop the traceback: its frame holds the tree, and this process runs without cyclic collection.
        code = exc.with_traceback(None)
    return tree, code


def patched_tree(path, source):
    return _patched(path, source)[0]


_MUTATING_CALLS = frozenset({
    "setattr", "delattr", "exec", "eval", "__setattr__", "__delattr__", "__setitem__",
    "__delitem__", "setdefault", "update", "add", "append", "extend", "insert", "remove",
    "clear", "discard", "pop", "popitem", "register", "unregister", "reverse", "sort",
})

_REFLECTIVE_WRITES = {"setattr", "delattr", "__setattr__", "__delattr__", "__setitem__",
                      "__delitem__", "exec", "eval"}


def _shared_receiver(node, names):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute) and (node.attr.isupper() or node.attr in {"__dict__", "arg_types"}):
            return True
        node = node.value
    return isinstance(node, ast.Name) and node.id in names


def _may_write_shared_state(scope, shared, writers):
    nodes = list(ast.walk(scope))
    aliases = set()
    for node in nodes:
        if isinstance(node, ast.Global):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in writers:
                return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _shared_receiver(node.value, shared):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            aliases.update(t.id for t in targets if isinstance(t, ast.Name))
    roots = shared | aliases
    for node in nodes:
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if _shared_receiver(node.value, roots):
                return True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATING_CALLS:
                if _shared_receiver(node.func.value, roots):
                    return True
            if any(isinstance(arg, ast.Name) and arg.id in aliases for arg in node.args):
                return True
    return False


def _changed_shared_scopes(tree, lines):
    shared = {alias.asname or alias.name.split(".")[0]
              for stmt in tree.body if isinstance(stmt, (ast.Import, ast.ImportFrom)) for alias in stmt.names}
    shared.update(stmt.name for stmt in tree.body if isinstance(stmt, ast.ClassDef))

    def visit(scope, owner, writers):
        writers = set(writers)
        for stmt in scope.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.Name, ast.Attribute)):
                name = stmt.value.id if isinstance(stmt.value, ast.Name) else stmt.value.attr
                if name in writers:
                    writers.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        for stmt in scope.body:
            first = min((d.lineno for d in getattr(stmt, "decorator_list", ())), default=stmt.lineno)
            if lines and not any(first <= n <= stmt.end_lineno for n in lines):
                continue
            if isinstance(stmt, ast.ClassDef):
                yield from visit(stmt, (*owner, stmt.name), writers)
            elif _may_write_shared_state(stmt, shared, writers):
                yield owner, _dump_key(stmt)

    return tuple(visit(tree, (), _REFLECTIVE_WRITES))


def _executed_effects(node):
    if isinstance(node, ast.Lambda):
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                yield from _executed_effects(default)
        return
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _MUTATING_CALLS:
            yield _dump_key(node)
            return
    if isinstance(node, (ast.AugAssign, ast.Delete)):
        yield _dump_key(node)
        return
    if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Store):
        yield _dump_key(node)
    for child in ast.iter_child_nodes(node):
        yield from _executed_effects(child)


def _statement_targets(statement):
    """What an expression statement binds or writes, so that retargeting an unchanged call still differs."""
    if isinstance(statement, ast.Assign):
        return tuple(_dump_key(target) for target in statement.targets)
    if isinstance(statement, ast.AnnAssign):
        return (_dump_key(statement.target),)
    return ()


def _definition_effects(node, owner=(), skip=None):
    if isinstance(node, _DEFINITIONS):
        owner = (*owner, node.name)
        for position, decorator in enumerate(node.decorator_list):
            if skip is None or not skip(node, decorator):
                yield owner, "decorator", position, _dump_key(decorator)
        if isinstance(node, ast.ClassDef):
            expressions = (*node.bases, *(keyword.value for keyword in node.keywords))
        else:
            args = node.args
            parameters = (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
            expressions = (*args.defaults, *args.kw_defaults, node.returns,
                           *(arg.annotation for arg in parameters if arg is not None))
        for position, value in enumerate(expressions):
            if value is not None and any(isinstance(n, ast.Call) for n in ast.walk(value)):
                yield owner, "call", position, _dump_key(value)
        if not isinstance(node, ast.ClassDef):
            return
    elif not isinstance(node, ast.Module):
        # A surrounding condition can turn an unchanged initializer on or off.
        owner = (*owner, _dump_key(node))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.expr, ast.withitem, ast.comprehension)):
                if isinstance(child, ast.Call):
                    yield owner, "control", _dump_key(child)
                else:
                    for effect in _executed_effects(child):
                        yield owner, "control", effect
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFINITION_SCOPES):
            yield from _definition_effects(child, owner, skip)
        elif isinstance(child, ast.stmt):
            if isinstance(child, (ast.Expr, ast.Assign, ast.AnnAssign)) and isinstance(child.value, ast.Call):
                if skip is None or not skip(node, child):
                    yield owner, "expression", _statement_targets(child), _dump_key(child.value)
            else:
                for effect in _executed_effects(child):
                    yield owner, "statement", effect


# Definition-time calls that only build values: a builtin constructor over literals bound to a plain name, and the
# base generator's unsupported_args(<literals>) applied to a function (a dict plus a functools.wraps closure).
_INERT_CONSTRUCTORS = frozenset({"frozenset", "set", "tuple"})
_INERT_DECORATORS = frozenset({"unsupported_args"})


def _literal(node):
    if isinstance(node, ast.Constant):
        return True
    return isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(_literal(element) for element in node.elts)


def _rebound(tree):
    """Inert callee names the module binds itself; only `from sqlglot.generator import unsupported_args` is allowed."""
    names = _INERT_CONSTRUCTORS | _INERT_DECORATORS
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found = {node.id}
        elif isinstance(node, (*_DEFINITIONS, ast.ExceptHandler, ast.MatchAs, ast.MatchStar,
                               ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
            found = {node.name}
        elif isinstance(node, ast.arg):
            found = {node.arg}
        elif isinstance(node, ast.MatchMapping):
            found = {node.rest}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found = set()
            for alias in node.names:
                if alias.name == "*":
                    found |= names
                elif not (isinstance(node, ast.ImportFrom) and node.module == "sqlglot.generator"
                          and alias.name == "unsupported_args" and alias.asname is None):
                    found.add(alias.asname or alias.name.split(".")[0])
        else:
            continue
        bound |= found & names
    return bound


def _inert_call(node, bound, callees):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in callees
            and node.func.id not in bound and all(_literal(argument) for argument in node.args)
            and all(keyword.arg is not None and _literal(keyword.value) for keyword in node.keywords))


def _naming(text, token, application=None):
    return sorted(line.strip() for line in text.split("\n")
                  if token in line and not (application and line.strip().startswith(application)))


def _inert_kinds(changes):
    """Which inert calls the whole patch leaves trustworthy: none if it moves files, constants unless it edits a line
    naming builtins, decorators unless it edits sqlglot/generator.py or a non-decorator line naming unsupported_args."""
    if not changes or any(change.structural for change in changes):
        return frozenset()
    kinds = {"constant", "decorator"}
    if any(_naming(change.original, "builtins") != _naming(change.patched, "builtins") for change in changes):
        kinds.discard("constant")
    if any(change.path == "sqlglot/generator.py"
           or _naming(change.original, "unsupported_args", "@unsupported_args(")
           != _naming(change.patched, "unsupported_args", "@unsupported_args(") for change in changes):
        kinds.discard("decorator")
    return frozenset(kinds)


def _inert_skip(tree, kinds):
    bound = _rebound(tree)

    def skip(parent, node):
        if isinstance(node, ast.stmt):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            annotation = getattr(node, "annotation", None)
            return ("constant" in kinds and _inert_call(node.value, bound, _INERT_CONSTRUCTORS)
                    and all(isinstance(target, ast.Name) for target in targets)
                    and (annotation is None or not any(isinstance(n, ast.Call) for n in ast.walk(annotation))))
        # Decorators on classes stay effects: only a function decorated with the base factory is exempt.
        return ("decorator" in kinds and isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _inert_call(node, bound, _INERT_DECORATORS))

    return skip


def generator_initialization_changed(change, changes=None):
    """True when a generator module's definition-time effects differ, or a changed scope may write shared state.
    Given the whole patch (`changes`), inert calls (see _inert_skip and _inert_kinds) are left out of the effect
    comparison on both sides; the deferred and reflective write guard below still decides every such patch."""
    if not change.path.startswith("sqlglot/generators/") or not change.path.endswith(".py"):
        return False
    original = _baseline(change.path, change.original)[0]
    patched = patched_tree(change.path, change.patched)
    if tuple(_definition_effects(original)) != tuple(_definition_effects(patched)):
        kinds = _inert_kinds(changes)
        if not kinds or (tuple(_definition_effects(original, skip=_inert_skip(original, kinds)))
                         != tuple(_definition_effects(patched, skip=_inert_skip(patched, kinds)))):
            return True
    if change.removed or change.added:
        old = _changed_shared_scopes(original, change.removed) if change.removed else ()
        new = _changed_shared_scopes(patched, change.added) if change.added else ()
        return old != new
    return _changed_shared_scopes(original, ()) != _changed_shared_scopes(patched, ())


def patched_code(path, source):
    """compile(source.encode("utf-8"), path, "exec", dont_inherit=True), shared by every stage of a patch."""
    code = _patched(path, source)[1]
    if isinstance(code, BaseException):
        # Raising the cached instance would attach this frame to it again.
        raise type(code)(*code.args)
    return code


def _entry(node):
    while isinstance(node, (ast.IfExp, ast.BoolOp, ast.Compare)):
        if isinstance(node, ast.IfExp):
            node = node.test
        elif isinstance(node, ast.BoolOp):
            node = node.values[0]
        else:
            node = node.left
    return node


def _scope_changed(old, new):
    if (
        old.co_flags != new.co_flags
        or old.co_cellvars != new.co_cellvars
        or old.co_freevars != new.co_freevars
        or (set(old.co_varnames) ^ set(new.co_varnames)) & (set(old.co_names) | set(new.co_names))
    ):
        return True
    children = [v for v in old.co_consts if isinstance(v, CodeType)]
    others = [v for v in new.co_consts if isinstance(v, CodeType)]
    if [child.co_qualname for child in children] != [child.co_qualname for child in others]:
        return True
    return any(_scope_changed(a, b) for a, b in zip(children, others))


def semantic_lines(path: str, original: str, patched: str, old_lines: set[int]) -> set[int] | None:
    lines = _changed_lines(path, original, patched)
    return None if lines is None else lines.copy()


@lru_cache(maxsize=16)
def _changed_lines(path, original, patched):
    try:
        old, compiled_old, executable = _baseline(path, original)
        new = patched_tree(path, patched)
        compiled_new = patched_code(path, patched)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if compiled_old == _without_positions(compiled_new):
        return set()
    if _scope_changed(compiled_old, compiled_new):
        return None

    lines = set()
    ambiguous = False

    def record(node, ancestors, runtime):
        nonlocal ambiguous
        if not runtime:
            ambiguous = True
            return
        for target in (node, *reversed(ancestors)):
            if isinstance(target, (*_DEFINITIONS, ast.Module, ast.Lambda)):
                break
            if not hasattr(target, "lineno"):
                continue
            if isinstance(target, (ast.If, ast.While, ast.IfExp)):
                target = target.test
            if isinstance(target, (ast.BoolOp, ast.Compare)):
                target = _entry(target)
            reached = executable.intersection(range(target.lineno, target.end_lineno + 1))
            if reached:
                lines.update(reached)
                return
        ambiguous = True

    def compare(a, b, ancestors, runtime):
        if type(a) is not type(b):
            if isinstance(a, _DEFINITIONS) or isinstance(b, _DEFINITIONS):
                record(a, ancestors, False)
            else:
                record(a, ancestors, runtime)
            return
        if not isinstance(a, ast.AST):
            if a != b:
                record(a, ancestors, runtime)
            return
        if _ast_native is not None and isinstance(a, ast.stmt):
            try:
                if _ast_native.equal(a, b, ast.AST, "_fields"):
                    return
            except (AttributeError, TypeError, ValueError, RecursionError):
                pass
        parents = ancestors + (a,)
        for field, av in ast.iter_fields(a):
            bv = getattr(b, field)
            body_runtime = runtime
            if isinstance(a, (*_DEFINITIONS, ast.Lambda)):
                body_runtime = field == "body" and not isinstance(a, ast.ClassDef)
            if not isinstance(av, list):
                compare(av, bv, parents, body_runtime)
                continue
            if len(av) == len(bv):
                for index, (left, right) in enumerate(zip(av, bv)):
                    doc = (
                        index == 0 and field == "body" and isinstance(a, _DEFINITIONS)
                        and any(isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)
                                and isinstance(x.value.value, str) for x in (left, right))
                    )
                    compare(left, right, parents, body_runtime and not doc)
                continue
            if any(not isinstance(x, ast.stmt) for x in (*av, *bv)):
                record(a, ancestors, body_runtime)
                continue
            align = difflib.SequenceMatcher(
                None, [_dump_key(x) for x in av], [_dump_key(x) for x in bv], autojunk=False
            )
            for tag, i, j, p, q in align.get_opcodes():
                if tag == "equal":
                    continue
                edits = av[i:j] + bv[p:q]
                if (
                    any(isinstance(x, _DEFINITIONS) for x in edits)
                    or (isinstance(a, _DEFINITIONS) and field == "body" and i == 0
                        and any(isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)
                                and isinstance(x.value.value, str) for x in edits))
                ):
                    record(a, ancestors, False)
                elif i < j:
                    for x in av[i:j]:
                        record(x, parents, body_runtime)
                elif av:
                    record(av[min(i, len(av) - 1)], parents, body_runtime)
                else:
                    record(a, ancestors, body_runtime)

    compare(old, new, (), False)
    return None if ambiguous else lines
