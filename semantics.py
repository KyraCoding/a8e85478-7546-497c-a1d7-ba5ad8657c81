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


def patched_code(path, source):
    """compile(source.encode("utf-8"), path, "exec", dont_inherit=True), shared by every stage of a patch."""
    code = _patched(path, source)[1]
    if isinstance(code, BaseException):
        raise code
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
