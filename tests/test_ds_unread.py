import sys
import unittest
from unittest.mock import Mock, patch

import ds_unread
from ds_unread import DS, GM, OH, Profile, _delta, changed_functions, conflicts
from patches import Change


def profile(records=(), globals_=(), co_names=(), co_strings=()):
    names = list(records)
    return Profile({"names": names, "stages": {"s": list(range(len(names)))}, "globals": list(globals_),
                    "co_names": list(co_names), "co_strings": list(co_strings)})


def state(**overrides):
    base = {"loaded": ["sqlglot", "sqlglot.a"], "process": "p", "classes": {"sqlglot.a.K": ["x", "y"],
            "sqlglot.a.Sub": ["z"]}, "modules": {"sqlglot.a": ["K", "Sub", "f"]},
            "mros": {"sqlglot.a.K": ["builtins.object"], "sqlglot.a.Sub": ["sqlglot.a.K", "builtins.object"]},
            "digests": {"c sqlglot.a.K x": "1"}, "keys": {"c sqlglot.a.K y": {"k1": "v1", "k2": "v2"}},
            "enums": {"sqlglot.a.E": [["A", "'A'"], ["B", "'B'"]]},
            "dunders": {"c sqlglot.a.K __eq__": "e1"}}
    base.update(overrides)
    return base


HIERARCHY = {"sqlglot.a.K": ["sqlglot.a.K", "sqlglot.a.Sub"], "sqlglot.a.Sub": ["sqlglot.a.Sub"]}


class ChangedFunctionTests(unittest.TestCase):
    def test_body_changes_are_reported_and_positions_ignored(self):
        original = "def f(x):\n    return x + 1\n\n\ndef g():\n    return 2\n"
        moved = "\n\n" + original
        self.assertEqual(changed_functions("sqlglot/a.py", original, moved), set())
        body = original.replace("x + 1", "x + 2")
        self.assertEqual(changed_functions("sqlglot/a.py", original, body), {("sqlglot/a.py", "f", 1)})

    def test_nested_and_class_level_changes(self):
        original = "class K:\n    X = 1\n    def m(self):\n        return (lambda: 1)()\n"
        attribute = original.replace("X = 1", "X = 2")
        self.assertEqual(changed_functions("sqlglot/a.py", original, attribute), {("sqlglot/a.py", "K", 1)})
        nested = original.replace("lambda: 1", "lambda: 3")
        self.assertEqual({q for _, q, _ in changed_functions("sqlglot/a.py", original, nested)},
                         {"K", "K.m", "K.m.<locals>.<lambda>"})


class DeltaTests(unittest.TestCase):
    def test_entries_names_and_enums(self):
        self.assertEqual(_delta(state(), state()), {})
        changed = _delta(state(digests={"c sqlglot.a.K x": "2"}), state())
        self.assertEqual(changed, {"c sqlglot.a.K x": {"kind": "value"}})
        keyed = _delta(state(keys={"c sqlglot.a.K y": {"k1": "v1", "k2": "other"}}), state())
        self.assertEqual(keyed, {"c sqlglot.a.K y": {"kind": "keys", "changed": ["k2"]}})
        named = _delta(state(classes={"sqlglot.a.K": ["x", "y", "w"], "sqlglot.a.Sub": [], "sqlglot.a.New": ["q"]},
                             mros={"sqlglot.a.K": ["builtins.object"], "sqlglot.a.Sub": ["sqlglot.a.K", "builtins.object"],
                                   "sqlglot.a.New": ["builtins.object"]}), state())
        self.assertEqual(named, {"c sqlglot.a.K w": {"kind": "added"}, "c sqlglot.a.Sub z": {"kind": "removed"},
                                 "c sqlglot.a.New __mro__": {"kind": "added"}})
        grown = _delta(state(enums={"sqlglot.a.E": [["A", "'A'"], ["B", "'B'"], ["C", "'C'"]]}), state())
        self.assertEqual(grown, {"c sqlglot.a.E __members__": {"kind": "added"}})
        shifted = _delta(state(enums={"sqlglot.a.E": [["A", "1"], ["B", "2"]]}), state())
        self.assertEqual(shifted, {"c sqlglot.a.E __members__": {"kind": "value"}})
        special = _delta(state(dunders={"c sqlglot.a.K __eq__": "e2"}), state())
        self.assertEqual(special, {"c sqlglot.a.K __eq__": {"kind": "special"}})
        self.assertIn("loaded", _delta(state(loaded=["sqlglot"]), state()))
        self.assertIn("process", _delta(state(process="q"), state()))

    def test_enum_replacements_are_not_additions(self):
        old = {"sqlglot.a.E": [["A", "A", "'A'"], ["B", "B", "'B'"]]}
        replaced = {"sqlglot.a.E": [["A", "A", "'A'"], ["C", "C", "'C'"], ["D", "D", "'D'"]]}
        self.assertEqual(_delta(state(enums=replaced), state(enums=old)),
                         {"c sqlglot.a.E __members__": {"kind": "value"}})
        inserted = {"sqlglot.a.E": [["A", "A", "'A'"], ["C", "C", "'C'"], ["B", "B", "'B'"]]}
        self.assertEqual(_delta(state(enums=inserted), state(enums=old)),
                         {"c sqlglot.a.E __members__": {"kind": "added"}})
        alias = {"sqlglot.a.E": [["A", "A", "'A'"], ["B", "B", "'B'"], ["BB", "B", "'B'"]]}
        self.assertEqual(_delta(state(enums=alias), state(enums=old)),
                         {"c sqlglot.a.E __members__": {"kind": "added"}})
        renamed = {"sqlglot.a.E": [["A", "A", "'A'"], ["B", "A", "'A'"]]}
        self.assertEqual(_delta(state(enums=renamed), state(enums=old)),
                         {"c sqlglot.a.E __members__": {"kind": "value"}})

    def test_shared_mutable_objects(self):
        shared = state(aliases={"c sqlglot.a.Sub z": "c sqlglot.a.K x"})
        self.assertEqual(_delta(state(aliases={}), shared), {"c sqlglot.a.Sub z": {"kind": "value"}})
        self.assertEqual(_delta(shared, shared), {})

    def test_shared_descendants_are_located(self):
        child = [1, 2]
        together = ds_unread._topology([("c a.K x", {"EQ": child}, None), ("c a.K y", {"EQ": child}, None)])
        apart = ds_unread._topology([("c a.K x", {"EQ": [1, 2]}, None), ("c a.K y", {"EQ": [1, 2]}, None)])
        self.assertEqual(together, {"c a.K y/'EQ'": "c a.K x/'EQ'"})
        self.assertEqual(apart, {})
        self.assertIn("c a.K y/'EQ'", _delta(state(aliases=apart), state(aliases=together)))
        table = {"EQ": child, "NEQ": [3]}
        only = ds_unread._topology([("c a.K x", {"NEQ": table["NEQ"]}, None), ("c a.K t", table, ["EQ"])])
        self.assertEqual(only, {})  # the keyed row is walked only under the keys the role reads
        top = ds_unread._topology([("c a.K x", child, None), ("c a.K y", child, None)])
        self.assertEqual(top, {"c a.K y": "c a.K x"})


class ConflictTests(unittest.TestCase):
    def test_read_entries_and_keys(self):
        p = profile(["a\tsqlglot.a.K\tsqlglot.a.K\tx", "k\tc sqlglot.a.K y\tk1"])
        self.assertTrue(conflicts({"c sqlglot.a.K x": {"kind": "value"}}, p, HIERARCHY))
        self.assertTrue(conflicts({"c sqlglot.a.K y": {"kind": "keys", "changed": ["k1"]}}, p, HIERARCHY))
        self.assertTrue(conflicts({"d sqlglot.g.G": {"kind": "keys", "changed": ["k"]}}, p, HIERARCHY))
        self.assertTrue(conflicts({"loaded": {"kind": "value"}}, p, HIERARCHY))

    def test_added_names_need_a_reachable_use(self):
        added = {"c sqlglot.a.K w": {"kind": "added"}}
        unused = profile(["a\tsqlglot.b.Other\tsqlglot.b.Other\tw"], co_names=["w"])
        self.assertEqual(conflicts(added, unused, HIERARCHY), [])
        in_use = ["a\tsqlglot.a.Sub\tsqlglot.a.K\tx"]
        self.assertEqual(conflicts(added, profile(in_use), HIERARCHY), [])
        self.assertTrue(conflicts(added, profile(in_use, co_names=["w"]), HIERARCHY))
        self.assertTrue(conflicts(added, profile(in_use, co_strings=["w"]), HIERARCHY))
        self.assertTrue(conflicts(added, profile(in_use + ["n\tw"]), HIERARCHY))
        self.assertTrue(conflicts(added, profile(in_use + ["r\tsqlglot.a.Sub"]), HIERARCHY))
        shadow = profile(["a\tsqlglot.a.Sub\tsqlglot.base.B\tw"])
        self.assertTrue(conflicts(added, shadow, HIERARCHY))
        dunder = {"c sqlglot.a.K __eq__": {"kind": "added"}}
        self.assertTrue(conflicts(dunder, profile(in_use), HIERARCHY))
        rebound = {"c sqlglot.a.K __eq__": {"kind": "special"}}
        self.assertTrue(conflicts(rebound, profile(in_use), HIERARCHY))
        self.assertEqual(conflicts(rebound, profile(), HIERARCHY), [])
        self.assertTrue(conflicts({"c sqlglot.a.K __mro__": {"kind": "value"}}, profile(in_use), HIERARCHY))

    def test_new_classes_are_reached_only_through_other_rows(self):
        delta = {"c sqlglot.a.New __mro__": {"kind": "added"}, "c sqlglot.a.New key": {"kind": "added"}}
        self.assertEqual(conflicts(delta, profile(co_names=["key"]), HIERARCHY), [])
        self.assertTrue(conflicts(delta, profile(co_names=["__subclasses__"]), HIERARCHY))

    def test_module_names(self):
        added = {"m sqlglot.a New": {"kind": "added"}}
        self.assertEqual(conflicts(added, profile(), HIERARCHY), [])
        self.assertTrue(conflicts(added, profile(["m\tsqlglot.a\tNew"]), HIERARCHY))
        self.assertTrue(conflicts(added, profile(globals_=[["sqlglot/a.py", "New"]]), HIERARCHY))
        self.assertTrue(conflicts(added, profile(["r\tmodule:sqlglot.a"]), HIERARCHY))
        hook = {"m sqlglot.a __getattr__": {"kind": "added"}}
        self.assertEqual(conflicts(hook, profile(), HIERARCHY), ["m sqlglot.a __getattr__: module attribute hook"])

    def test_enums(self):
        grown = {"c sqlglot.a.E __members__": {"kind": "added"}}
        self.assertEqual(conflicts(grown, profile(), HIERARCHY), [])
        self.assertTrue(conflicts(grown, profile(["e\tsqlglot.a.E"]), HIERARCHY))
        self.assertTrue(conflicts(grown, profile(["w\tc sqlglot.a.E _member_map_"]), HIERARCHY))
        self.assertTrue(conflicts({"c sqlglot.a.E __members__": {"kind": "value"}}, profile(), HIERARCHY))


class VerdictTests(unittest.TestCase):
    def test_static_refusals_do_not_start_a_child(self):
        ran = profile(["f\tsqlglot/a.py\tf\t1"])
        with patch("ds_unread.profiles", return_value=({}, {"worker": ran, "parent": profile()})), \
             patch("ds_unread._run", side_effect=AssertionError("child started")):
            body = Change("sqlglot/a.py", "def f():\n    return 1\n", "def f():\n    return 2\n", {2}, {2})
            self.assertEqual(ds_unread.verdict([body])[0], False)
            fixture = Change("tests/fixtures/optimizer/tpc-ds/x.sql", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([fixture])[0], False)
            structural = Change("sqlglot/b.py", "", "x = 1\n", set(), {1}, True)
            self.assertEqual(ds_unread.verdict([structural])[0], False)
            cov = Mock()
            cov.fixture.side_effect = lambda path: {"tests/fixtures/optimizer/a.sql": {"other"},
                                                    "tests/fixtures/optimizer/b.sql": {"other", DS}}.get(path, set())
            read = Change("tests/fixtures/optimizer/b.sql", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([read], cov=cov)[0], False)
            unknown = Change("tests/fixtures/optimizer/c.sql", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([unknown], cov=cov)[0], False)
            inert = Change("tests/fixtures/optimizer/a.sql", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([inert], cov=cov), (False, ["no sqlglot source change"]))

    def test_child_reasons_decide(self):
        quiet = {"worker": profile(), "parent": profile()}
        change = Change("sqlglot/a.py", "X = 1\n", "X = 2\n", {1}, {1})
        with patch("ds_unread.profiles", return_value=({}, quiet)), patch("ds_unread._request", return_value={}), \
             patch("ds_unread._run", return_value={"roles": {"worker": [], "parent": []}, "cpu": 1}):
            self.assertEqual(ds_unread.verdict([change]), (True, []))
            cov = Mock()
            cov.fixture.return_value = {"other"}
            fixture = Change("tests/fixtures/optimizer/a.sql", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([change, fixture], cov=cov), (True, []))
            tpc = Change("tests/fixtures/optimizer/tpc-ds/x.csv.gz", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([change, tpc], cov=cov)[0], False)
            doc = Change("docs/a.md", "a", "b", {1}, {1})
            self.assertEqual(ds_unread.verdict([change, doc], cov=cov), (True, []))
        with patch("ds_unread.profiles", return_value=({}, quiet)), patch("ds_unread._request", return_value={}), \
             patch("ds_unread._run", return_value={"roles": {"worker": ["c x: read entry differs"], "parent": []}, "cpu": 1}):
            self.assertEqual(ds_unread.verdict([change])[0], False)

    def test_import_time_effects_keep_the_tests_without_a_child(self):
        quiet = {"worker": profile(), "parent": profile()}
        original = "class Table:\n    pass\n"
        reducer = original + ("\n\ndef _reduce(t):\n    return Table, ()\n\n\n"
                              "from multiprocessing.reduction import ForkingPickler\nForkingPickler.register(Table, _reduce)\n")
        with patch("ds_unread.profiles", return_value=({}, quiet)), \
             patch("ds_unread._run", side_effect=AssertionError("child started")):
            removable, reasons = ds_unread.verdict([Change("sqlglot/executor/table.py", original, reducer, {3}, {3})])
        self.assertFalse(removable)
        self.assertIn("ForkingPickler.register", reasons[0])

    def test_new_and_changed_functions_are_watched_during_import(self):
        quiet = {"worker": profile(), "parent": profile()}
        change = Change("sqlglot/a.py", "def f():\n    return 1\n", "def f():\n    return 2\n\n\ndef g():\n    return 3\n",
                        {2}, {2, 3, 4, 5, 6})
        with patch("ds_unread.profiles", return_value=({}, quiet)), patch("ds_unread._request", return_value={}), \
             patch("ds_unread._run", return_value={"roles": {"worker": [], "parent": []}, "cpu": 1}) as run:
            self.assertEqual(ds_unread.verdict([change]), (True, []))
        self.assertEqual(run.call_args[0][0]["watch"], [("sqlglot/a.py", "f"), ("sqlglot/a.py", "g")])


class ProcessTests(unittest.TestCase):
    def test_reducer_registrations_change_the_process_digest(self):
        import copyreg
        from multiprocessing.reduction import ForkingPickler

        class Local:
            pass

        before = ds_unread._process_before()
        clean = ds_unread._process_digest(before)
        self.assertEqual(ds_unread._process_digest(before), clean)
        copyreg.pickle(Local, lambda item: (Local, ()))
        try:
            self.assertNotEqual(ds_unread._process_digest(before), clean)
        finally:
            del copyreg.dispatch_table[Local]
        self.assertEqual(ds_unread._process_digest(before), clean)
        ForkingPickler.register(Local, lambda item: (Local, ()))
        try:
            self.assertNotEqual(ds_unread._process_digest(before), clean)
        finally:
            del ForkingPickler._extra_reducers[Local]
        self.assertEqual(ds_unread._process_digest(before), clean)
        # the poolcopy control: a private copy of copyreg's table on the class, copyreg itself untouched
        original = ForkingPickler.__dict__["_copyreg_dispatch_table"]
        type.__setattr__(ForkingPickler, "_copyreg_dispatch_table", {**original, Local: lambda item: (Local, ())})
        try:
            self.assertNotEqual(ds_unread._process_digest(before), clean)
        finally:
            type.__setattr__(ForkingPickler, "_copyreg_dispatch_table", original)
        self.assertEqual(ds_unread._process_digest(before), clean)

    def test_reducers_are_compared_by_content(self):
        import functools

        def reduce(item, n):
            return n

        self.assertNotEqual(ds_unread._content(functools.partial(reduce, n=1)),
                            ds_unread._content(functools.partial(reduce, n=2)))
        self.assertEqual(ds_unread._content(functools.partial(reduce, n=1)),
                         ds_unread._content(functools.partial(reduce, n=1)))

    def test_import_watch_sees_watched_functions_start(self):
        source = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
        namespace = {}
        exec(compile(source, "/opt/sqlglot/sqlglot/zz_watch.py", "exec"), namespace)
        watch = ds_unread._ImportWatch([("sqlglot/zz_watch.py", "f")])
        try:
            watch.enable(True)
            namespace["g"]()
            self.assertEqual(watch.seen, set())
            namespace["f"]()
            watch.enable(False)
            self.assertEqual(watch.seen, {"sqlglot/zz_watch.py:f"})
        finally:
            watch.enable(False)
            sys.monitoring.register_callback(watch.tool, sys.monitoring.events.PY_START, None)
            sys.monitoring.free_tool_id(watch.tool)


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.cov = Mock()
        self.cov.missing = set()
        self.cov.nodes = {DS, GM, "other"}
        self.catalog = {DS, GM, "other"}
        self.chosen = {DS, GM, "other"}
        roles = {role: profile() for roles in ds_unread.HEAVY.values() for role in roles}
        self.profiles = patch("ds_unread.profiles", return_value=({}, roles))
        self.profiles.start()

    def tearDown(self):
        self.profiles.stop()

    def refine(self, **kwargs):
        return ds_unread.refine_unread([], self.chosen, self.cov, self.catalog, float("inf"), **kwargs)

    def test_removes_only_cleared_tests_when_the_check_pays(self):
        cleared = {DS: (True, []), GM: (False, ["gm: read entry differs"])}
        with patch("ds_unread._saving", return_value=20.0), patch("ds_unread.verdicts", return_value=cleared):
            self.assertEqual(self.refine(), {GM, "other"})
        with patch("ds_unread._saving", return_value=20.0), \
             patch("ds_unread.verdicts", return_value={DS: (False, ["x"]), GM: (False, ["y"])}):
            self.assertEqual(self.refine(), self.chosen)
        with patch("ds_unread._saving", return_value=1.0), \
             patch("ds_unread.verdicts", side_effect=AssertionError("unpaid check")):
            self.assertEqual(self.refine(), self.chosen)

    def test_errors_keep_the_tests(self):
        with patch("ds_unread._saving", return_value=20.0), patch("ds_unread.verdicts", side_effect=RuntimeError):
            self.assertEqual(self.refine(), self.chosen)
        with patch("ds_unread._saving", return_value=20.0), \
             patch("ds_unread.verdicts", side_effect=__import__("subprocess").TimeoutExpired("x", 1)):
            self.assertEqual(self.refine(), self.chosen)
        self.cov.missing = {DS, GM}
        with patch("ds_unread.verdicts", side_effect=AssertionError("unmeasured test")):
            self.assertEqual(self.refine(), self.chosen)

    def test_tests_without_a_profile_are_left_alone(self):
        with patch("ds_unread.profiles", return_value=({}, {"worker": profile(), "parent": profile()})), \
             patch("ds_unread._saving", return_value=20.0), \
             patch("ds_unread.verdicts", return_value={DS: (True, [])}) as called:
            self.assertEqual(self.refine(), {GM, "other"})
            self.assertEqual(called.call_args[0][1], [DS])


class MultiTestVerdictTests(unittest.TestCase):
    def test_roles_are_judged_per_test(self):
        roles = {role: profile() for roles in ds_unread.HEAVY.values() for role in roles}
        roles["gm"] = profile(["f\tsqlglot/a.py\tf\t1"])
        change = Change("sqlglot/a.py", "def f():\n    return 1\n", "def f():\n    return 2\n", {2}, {2})
        observed = {"roles": {"worker": [], "parent": [], "oh": ["c x: read entry differs"]}, "cpu": 1}
        with patch("ds_unread.profiles", return_value=({}, roles)), patch("ds_unread._request", return_value={}), \
             patch("ds_unread._run", return_value=observed) as run:
            result = ds_unread.verdicts([change], [DS, GM, OH])
            self.assertEqual(run.call_args[0][0]["roles"], ["oh", "parent", "worker"])
        self.assertEqual(result[DS], (True, []))
        self.assertFalse(result[GM][0])
        self.assertFalse(result[OH][0])


if __name__ == "__main__":
    unittest.main()
