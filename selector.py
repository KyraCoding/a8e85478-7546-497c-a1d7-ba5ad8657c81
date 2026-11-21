import json
import os
import sys
import time
from pathlib import Path

from coverage_map import Coverage
from declarations import declaration_candidates
from default_profile import refine_defaults
from ds_verify import refine_ds
from fixture_flow import refine_fixture
from heavy_profile import refine_heavy
from identity_profile import refine_identity
from import_effects import import_consumers, module_fallback
from patches import read_changes
from registry_context import registry_context_required
from replay import refine_replay
from schedule import SpareCapacity, distribute
from semantics import semantic_lines
from state_diff import refine_state
from tc_replay import refine_tc
from value_profile import refine
from source_reader import refine_source_reader


def candidates(change, cov, catalog, *, fixtures_only=False):
    path = change.path
    if change.structural:
        return catalog
    readers = cov.fixture(path)
    if not path.endswith(".py"):
        if readers:
            if fixtures_only:
                readers = refine_fixture(change, readers, cov)
            return readers | cov.missing
        if path.startswith(("docs/", ".github/")) or path.endswith(".md"):
            return set()
        return catalog

    lines = semantic_lines(path, change.original, change.patched, change.removed)
    if lines == set():
        return readers
    if path.startswith("tests/") and not Path(path).name.startswith("test_"):
        return catalog
    if lines is None:
        try:
            result = declaration_candidates(path, change.original, change.patched, change.removed, cov)
        except SyntaxError:
            result = None
        if result is None:
            return module_fallback(path, cov, catalog)
    else:
        result = cov.lines(path, lines)
        if not cov.present(path) and not path.startswith("sqlglot/"):
            return catalog
        result = refine(path, change.original, change.patched, change.removed, result)
        result = refine_identity(path, change.original, change.patched, change.removed, result)
        result = refine_heavy(change, lines, result)
        result.update(import_consumers(path, lines, cov, catalog))
    return result | readers | cov.missing


def main():
    data = Path(os.environ.get("INPUT_DIR", "/data"))
    source = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    numbers = {test["id"]: test["number"] for test in tests}
    millis = {test["number"]: round(1000 * test["seconds"]) for test in tests}
    catalog = set(numbers)
    cov = Coverage()
    unseen = catalog.difference(cov.nodes)
    patches = sorted((data / "commits").glob("*.patch"))
    catalog_bytes = sum(len(str(number)) + 1 for number in millis)
    max_output = sum(len(json.dumps(p.stem)) + catalog_bytes + 50 for p in patches)
    padding = SpareCapacity(millis) if max_output <= 2_000_000 else None
    verify_until = time.monotonic() + 85
    for patch in patches:
        try:
            changes = read_changes(patch.read_text(encoding="utf-8"), source)
            if registry_context_required(changes):
                chosen = set(catalog)
            else:
                chosen = set()
                fixtures_only = all(c.path.startswith("tests/fixtures/") and not c.structural for c in changes)
                for change in changes:
                    chosen.update(candidates(change, cov, catalog, fixtures_only=fixtures_only))
                if chosen and time.monotonic() < verify_until:
                    frontend_allowed = not chosen.issuperset(catalog)
                    chosen = refine_defaults(changes, chosen, cov, catalog)
                    chosen = refine_state(changes, chosen, cov, catalog)
                    chosen = refine_replay(changes, chosen, cov, catalog)
                    chosen = refine_tc(changes, chosen, cov, catalog)
                    chosen = refine_ds(changes, chosen, cov, catalog, verify_until)
                    if frontend_allowed and not chosen.issuperset(catalog):
                        from fe_replay import refine_frontend
                        chosen = refine_frontend(changes, chosen, cov, catalog, verify_until,
                                                 padding.full_cost if padding is not None else None)
                    chosen = refine_source_reader(changes, chosen, cov, source)
            chosen.update(unseen)
        except (SyntaxError, ValueError, IndexError, KeyError, OSError, RecursionError) as exc:
            print(f"{patch.name}: using full suite ({type(exc).__name__})", file=sys.stderr)
            chosen = catalog
        workers = distribute((numbers[node] for node in chosen & catalog), millis)
        if padding is not None:
            workers = padding.fill(workers)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
