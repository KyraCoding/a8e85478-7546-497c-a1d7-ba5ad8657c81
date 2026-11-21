import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inert_files import inert_change, safe_headers  # noqa: E402
from patches import Change  # noqa: E402


class FakeCoverage:
    def __init__(self, readers=None):
        self.readers = readers or {}

    def fixture(self, path):
        return set(self.readers.get(path, ()))


def modified(path, original="a\n", patched="b\n"):
    return Change(path, original, patched, {1}, {1}, False)


def added(path, text="x\n"):
    return Change(path, "", text, set(), {1}, True)


def deleted(path, text="x\n"):
    return Change(path, text, "", {1}, set(), True)


ARTICLE_PATCH = (
    "diff --git a/posts/release-notes.md b/posts/release-notes.md\n"
    "new file mode 100644\n--- /dev/null\n+++ b/posts/release-notes.md\n@@ -0,0 +1 @@\n+Probe note.\n"
)
LICENSE_PATCH = (
    "diff --git a/LICENSE b/LICENSE\n--- a/LICENSE\n+++ b/LICENSE\n@@ -19,3 +19,4 @@\n"
    " LIABILITY\n OUT OF\n SOFTWARE.\n+(Probe line.)\n"
)


class TestInertFiles(unittest.TestCase):
    def test_admitted_mechanisms(self):
        cov = FakeCoverage()
        self.assertTrue(inert_change(added("posts/release-notes.md"), cov))
        self.assertTrue(inert_change(modified("posts/ast_primer.md"), cov))
        self.assertTrue(inert_change(modified("LICENSE"), cov))
        self.assertTrue(safe_headers(ARTICLE_PATCH))
        self.assertTrue(safe_headers(LICENSE_PATCH))
        self.assertTrue(safe_headers(LICENSE_PATCH.replace("--- a/LICENSE", "index 0123abc..4567def 100644\n--- a/LICENSE")))

    def test_mixed_source_change(self):
        cov = FakeCoverage()
        license_edit = modified("LICENSE")
        source_edit = modified("sqlglot/parser.py", "x = 1\n", "x = 2\n")
        self.assertTrue(inert_change(license_edit, cov))
        self.assertFalse(inert_change(source_edit, cov))  # judged by the ordinary selector

    def test_unrecognized_root_extension(self):
        cov = FakeCoverage()
        for change in (added("notes.xyz"), modified("data.cfg"), added("tool.ini"), modified("Makefile")):
            self.assertFalse(inert_change(change, cov), change.path)

    def test_doc_false_friends(self):
        cov = FakeCoverage()
        for change in (added("posts/onboarding_images/notes.md"), added("posts/example.py"),
                       added("posts/notes.txt"), added("docs/guide.md"), added("README.md"),
                       deleted("posts/ast_primer.md"), deleted("LICENSE"), added("LICENSE"),
                       modified("posts/.hidden.md"), modified("LICENSE.md")):
            self.assertFalse(inert_change(change, cov), change.path)

    def test_recorded_reader_and_binary_keep_fallback(self):
        self.assertFalse(inert_change(modified("LICENSE"), FakeCoverage({"LICENSE": {"tests/t.py::T::t"}})))
        self.assertFalse(inert_change(modified("LICENSE", patched="a\0b\n"), FakeCoverage()))

    def test_unsafe_headers(self):
        for extra in ("new file mode 120000", "new file mode 100755", "deleted file mode 100644",
                      "old mode 100644\nnew mode 100755", "similarity index 100%\nrename from a\nrename to b",
                      "copy from a\ncopy to b", "Binary files a/x and b/x differ", "index 0123abc..4567def 100755"):
            text = "diff --git a/LICENSE b/LICENSE\n" + extra + "\n--- a/LICENSE\n+++ b/LICENSE\n@@ -1 +1 @@\n-a\n+b\n"
            self.assertFalse(safe_headers(text), extra)
        self.assertFalse(safe_headers("diff --git a/LICENSE b/OTHER\n--- a/LICENSE\n+++ b/OTHER\n@@ -1 +1 @@\n-a\n+b\n"))


if __name__ == "__main__":
    unittest.main()
