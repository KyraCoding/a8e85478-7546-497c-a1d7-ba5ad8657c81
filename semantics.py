import ast
import difflib
from functools import lru_cache
from types import CodeType


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


@lru_cache(maxsize=6)
def _baseline(path, source):
    tree = ast.parse(source.encode("utf-8"), path)
    code = compile(tree, path, "exec", dont_inherit=True)
    lines = set()

    def collect(block):
        lines.update(n for start, end, n in block.co_lines() if end > start and n)
        for value in block.co_consts:
            if isinstance(value, CodeType):
                collect(value)

    collect(code)
    return tree, _without_positions(code), lines


@lru_cache(maxsize=1)
def patched_tree(path, source):
    return ast.parse(source.encode("utf-8"), path)


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


@lru_cache(maxsize=1)
def _changed_lines(path, original, patched):
    try:
        old, compiled_old, executable = _baseline(path, original)
        new = patched_tree(path, patched)
        compiled_new = compile(new, path, "exec", dont_inherit=True)
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
                None, [ast.dump(x) for x in av], [ast.dump(x) for x in bv], autojunk=False
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
