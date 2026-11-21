import argparse
import hashlib
import json
from pathlib import Path

from registry_context import dialect_provider, lookup_digest, lookup_rows


def collect(root):
    records = {}
    for file in sorted((root / "sqlglot").rglob("*.py")):
        source = file.read_text(encoding="utf-8")
        path = file.relative_to(root).as_posix()
        if "Dialect" not in source and not dialect_provider(path):
            continue
        rows = lookup_rows(source, path)
        if not dialect_provider(path) and not any(text.startswith(("Call(", "Attribute(", "Subscript(")) for _, text, _, _ in rows):
            continue
        lines = sorted({line for _, _, start, end in rows for line in range(start, end + 1)})
        records[path] = [
            hashlib.sha256(source.encode()).hexdigest(), lookup_digest(rows), lines
        ]
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    records = collect(args.root)
    args.output.write_bytes((json.dumps(records, separators=(",", ":")) + "\n").encode())
    print(len(records), sum(len(entry[2]) for entry in records.values()))
