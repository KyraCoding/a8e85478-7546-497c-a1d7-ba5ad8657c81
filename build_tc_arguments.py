import __future__
import ast
import gzip
import hashlib
import inspect
import json
from collections import defaultdict
from types import FunctionType, MethodType

from tc_replay import ARG_MAP, PARSE_MAP, digest, replay_parse


def method_name(call):
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"):
        return call.func.attr


def install(root):
    import sqlglot.parser as module
    from sqlglot.dialects.dialect import Dialect

    path = root / "sqlglot/parser.py"
    cls = next(node for node in ast.parse(path.read_bytes()).body
               if isinstance(node, ast.ClassDef) and node.name == "Parser")
    concrete = type(Dialect().parser())
    functions = {}
    methods = {}
    params = set()
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.decorator_list or fn.name.startswith("__"):
            continue
        method = vars(module.Parser).get(fn.name)
        if (type(method) is not FunctionType or method.__closure__ or method.__code__.co_firstlineno != fn.lineno
                or inspect.getattr_static(concrete, fn.name, None) is not method):
            continue
        body = [node for stmt in fn.body for node in ast.walk(stmt)]
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                                 ast.Yield, ast.YieldFrom, ast.GeneratorExp))
               or isinstance(node, ast.Name) and node.id in {"super", "locals", "vars", "eval", "exec", "inspect", "sys"}
               for node in body):
            continue
        functions[fn.name] = fn, body
        methods[fn.name] = method
        stored = {node.id for node in body if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)}
        params.update((fn.name, arg.arg) for arg in [*fn.args.args[1:], *fn.args.kwonlyargs]
                      if arg.arg not in stored)

    roots = {}
    forwards = defaultdict(set)
    calls = defaultdict(list)
    uses = {}
    forwarded_calls = {}
    for name, (fn, body) in functions.items():
        parents = {child: node for node in body for child in ast.iter_child_nodes(node)}
        for node in body:
            if isinstance(node, ast.Call):
                callee = method_name(node)
                for kw in node.keywords:
                    value = kw.value
                    if (callee, kw.arg) in params and isinstance(value, ast.Constant) and type(value.value) is bool:
                        site = f"{value.lineno}:{value.col_offset}:{value.end_col_offset}"
                        roots[site] = callee, kw.arg
                        calls[node].append(site)
            if not isinstance(node, ast.Name) or (name, node.id) not in params:
                continue
            parent = parents.get(node)
            call = parents.get(parent)
            dest = (method_name(call), parent.arg) if isinstance(parent, ast.keyword) else None
            if dest in params:
                forwards[name, node.id].add(dest)
                forwarded_calls.setdefault(call, set()).add((name, node.id))
            else:
                uses[node] = name, node.id

    watchers = defaultdict(set)
    for site, param in roots.items():
        pending = [param]
        while pending:
            current = pending.pop()
            if site not in watchers[current]:
                watchers[current].add(site)
                pending.extend(forwards[current])
    for call, params_used in forwarded_calls.items():
        if any(param in watchers for param in params_used):
            calls.setdefault(call, [])

    active = []
    touched = set()

    def invoke(name, sites, func, /, *args, **kwargs):
        if type(func) is not MethodType or type(func.__self__) is not concrete or func.__func__ is not methods[name]:
            touched.update(sites)
            for group in active:
                touched.update(group)
        if sites:
            active.append(sites)
            try:
                return func(*args, **kwargs)
            finally:
                active.pop()
        return func(*args, **kwargs)

    def read(param, value):
        for group in active:
            touched.update(watchers[param].intersection(group))
        return value

    class Instrument(ast.NodeTransformer):
        def visit_Call(self, node):
            name = method_name(node)
            self.generic_visit(node)
            if node in calls:
                node.args[:0] = [ast.Constant(name), ast.Constant(tuple(calls[node])), node.func]
                node.func = ast.Name("_tc_arg_call", ast.Load())
            return node

        def visit_Name(self, node):
            param = uses.get(node)
            if param in watchers:
                call = ast.Call(ast.Name("_tc_arg_read", ast.Load()), [ast.Constant(param), node], [])
                return ast.copy_location(call, node)
            return node

    namespace = vars(module)
    if "_tc_arg_call" in namespace or "_tc_arg_read" in namespace:
        raise ValueError("parser instrumentation already installed")
    namespace.update(_tc_arg_call=invoke, _tc_arg_read=read)
    for name, (fn, body) in functions.items():
        if not any(node in calls or uses.get(node) in watchers for node in body):
            continue
        fn.args.defaults = [ast.Constant(None) for default in fn.args.defaults]
        fn.args.kw_defaults = [ast.Constant(None) if default is not None else None for default in fn.args.kw_defaults]
        tree = ast.fix_missing_locations(ast.Module([Instrument().visit(fn)], []))
        compiled = {}
        exec(compile(tree, str(path), "exec", flags=__future__.annotations.compiler_flag), namespace, compiled)
        original = methods[name].__code__
        methods[name].__code__ = compiled[name].__code__.replace(co_qualname=original.co_qualname)
    return sorted(roots), touched


def save_arguments(source, rows, tests, outside, sites, reads, touched):
    contents = PARSE_MAP.read_bytes()
    baseline = json.loads(gzip.decompress(contents))
    if (digest(source) != baseline["source"] or tests != baseline["tests"] or outside != baseline["outside"]
            or len(rows) != len(baseline["rows"])):
        raise ValueError("parse observations changed")
    records = []
    for (call, expected, hits), (base_call, base_expected, base_hits) in zip(rows, baseline["rows"]):
        if call != base_call or expected != base_expected:
            raise ValueError("parse descriptor changed")
        touched.clear()
        key = json.dumps(call, sort_keys=True)
        try:
            valid = digest(replay_parse(call)) == expected
        except Exception:
            valid = False
        actual = reads.get(key)
        records.append(sorted(touched | actual) if valid and actual is not None and hits == base_hits else None)
    data = {"version": 1, "complete": True, "profile": hashlib.sha256(contents).hexdigest(), "source": baseline["source"],
            "sites": sites, "reads": records}
    ARG_MAP.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(sites)} argument sites, {len(records) - records.count(None)} verified inputs, {ARG_MAP.stat().st_size} bytes")
