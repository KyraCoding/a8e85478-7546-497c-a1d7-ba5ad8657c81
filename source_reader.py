import hashlib
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from tempfile import TemporaryDirectory

METHOD = "tests.test_typing.TestTyping.test_no_redundant_expression_metadata"
NODE = "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"


def mirror(source, target, route, replacement):
    target.mkdir()
    for item in source.iterdir():
        if item.name == "__pycache__" and len(route) == 1:
            continue
        dest = target / item.name
        if item.name != route[0]:
            dest.symlink_to(item, target_is_directory=item.is_dir())
        elif len(route) == 1:
            dest.write_bytes(replacement)
        else:
            mirror(item, dest, route[1:], replacement)


def passing(change, root):
    root = root.resolve()
    path = PurePosixPath(change.path)
    if len(path.parts) != 3 or path.parts[:2] != ("sqlglot", "typing"):
        return False
    if path.suffix != ".py" or path.name == "__init__.py" or not path.stem.isidentifier():
        return False
    if change.structural or not (root / path).is_file():
        return False
    with TemporaryDirectory() as tmp:
        tree = Path(tmp) / "project"
        patched = change.patched.encode("utf-8")
        mirror(root, tree, path.parts, patched)
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), str(tree), str(path), hashlib.sha256(patched).hexdigest()],
            env=dict(os.environ, PYTHONPATH=str(tree)), cwd=tree, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False,
        )
        return result.returncode == 0 and result.stdout == b"verified\n"


def refine_source_reader(changes, chosen, cov, root):
    if len(changes) != 1 or chosen != {NODE} or NODE in cov.missing:
        return chosen
    change = changes[0]
    if not change.path.startswith("sqlglot/typing/") or change.structural:
        return chosen
    from semantics import semantic_lines

    try:
        if semantic_lines(change.path, change.original, change.patched, change.removed) != set():
            return chosen
        if NODE in cov.fixture(change.path) and passing(change, root):
            return set()
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        pass
    return chosen


def verify_file():
    import importlib
    import unittest

    root = Path(sys.argv[1])
    path = PurePosixPath(sys.argv[2])
    source = root / path
    if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != sys.argv[3]:
        raise SystemExit(2)
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.loadTestsFromName(METHOD)

    class Outcome(unittest.TestResult):
        verified = False

        def addSuccess(self, test):
            self.verified = test.id() == METHOD
            super().addSuccess(test)

    result = suite.run(Outcome())
    mod = importlib.import_module(".".join((*path.parts[:2], path.stem)))
    if (Path(mod.__file__).resolve() != source.resolve() or not result.wasSuccessful()
            or not result.verified or result.testsRun != 1):
        raise SystemExit(1)
    print("verified")


if __name__ == "__main__":
    verify_file()
