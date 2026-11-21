import gzip
import json
from functools import cache
from pathlib import Path


@cache
def _index():
    with gzip.open(Path(__file__).parent / "maps" / "import_effects.json.gz", "rt", encoding="utf-8") as f:
        data = json.load(f)
    tests = {path: (set(modules), set(nodes)) for path, (modules, nodes) in data["tests"].items()}
    cache_file = Path(__file__).parent / "maps" / "cache_effects.json"
    return data["sites"], tests, json.loads(cache_file.read_bytes())


def import_consumers(path, lines, cov, catalog):
    sites, tests, caches = _index()
    readers = {read for start, end, read in caches.get(path, ())
               if any(start <= n <= end for n in lines)}
    cached = cov.lines(path, readers) if readers else set()
    module_lines = sites.get(path, {})
    affected = {module for n in set(lines) | readers for module in module_lines.get(str(n), ())}
    if not affected:
        return cached
    return cached | _module_users(frozenset(affected), catalog)


def _module(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


@cache
def _isolated_items():
    directory = Path(__file__).parent / "maps"
    with gzip.open(directory / "footprints.json.gz", "rt", encoding="utf-8") as f:
        paths = json.load(f)
    with gzip.open(directory / "replay.json.gz", "rt", encoding="utf-8") as f:
        stress = json.load(f)
    paths[stress["node"]] = stress["files"]
    return {name: frozenset(map(_module, files)) for name, files in paths.items()}


def _module_users(affected, catalog):
    _, tests, _ = _index()
    result = set(catalog)
    for modules, nodes in tests.values():
        if affected.isdisjoint(modules):
            result.difference_update(nodes)
    for node, modules in _isolated_items().items():
        if affected.isdisjoint(modules):
            result.discard(node)
        elif node in catalog:
            result.add(node)
    return result


def module_fallback(path, cov, catalog):
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        return set(catalog)
    consumers = _module_users(frozenset({_module(path)}), catalog)
    return consumers | cov.file_execution(path) | cov.fixture(path) | cov.missing
