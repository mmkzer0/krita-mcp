#!/usr/bin/env python3
"""Offline contract tests for the checkpoint store (no Krita, no Qt).

Loads ``pykrita/krita_mcp/history_ops.py`` by path and exercises
``CheckpointStore`` with fake sizes.

    python3 test_history.py
"""

import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "history_ops",
    Path(__file__).resolve().parent / "pykrita" / "krita_mcp" / "history_ops.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)

MB = 1024 * 1024


def make(limit=100, per_document=20):
    return history.CheckpointStore(limit, per_document=per_document,
                                   clock=iter(range(1000, 2000)).__next__)


def put(store, doc, size, label=""):
    return store.add(doc, label, size, 1, {"doc": doc})


class StoreTests(unittest.TestCase):
    def test_ids_are_monotonic_and_never_reused(self):
        store = make()
        first, _ = put(store, "a", 1)
        second, _ = put(store, "b", 1)
        self.assertEqual((first.id, second.id), ("cp1", "cp2"))
        store.drop("a")
        third, _ = put(store, "a", 1)
        self.assertEqual(third.id, "cp3")
        self.assertEqual([c.id for c in store.list("a")], ["cp3"])

    def test_created_comes_from_the_clock(self):
        checkpoint, _ = put(make(), "a", 1)
        self.assertEqual(checkpoint.created, 1000)

    def test_budget_evicts_oldest_across_documents_and_reports_them(self):
        store = make(limit=100)
        put(store, "a", 40)
        put(store, "b", 40)
        put(store, "a", 15)
        self.assertEqual(store.used_bytes, 95)
        checkpoint, evicted = put(store, "c", 50)
        self.assertEqual(evicted, ["cp1", "cp2"])
        self.assertEqual(store.used_bytes, 65)
        self.assertIsNone(store.get("cp1"))
        self.assertIsNone(store.get("cp2"))
        self.assertIsNotNone(store.get("cp3"))
        self.assertEqual(checkpoint.id, "cp4")

    def test_exact_fit_evicts_nothing(self):
        store = make(limit=100)
        put(store, "a", 60)
        _, evicted = put(store, "a", 40)
        self.assertEqual(evicted, [])
        self.assertEqual(store.used_bytes, 100)

    def test_checkpoint_as_big_as_the_budget_evicts_everything_else(self):
        store = make(limit=100)
        put(store, "a", 10)
        put(store, "b", 10)
        _, evicted = put(store, "c", 100)
        self.assertEqual(evicted, ["cp1", "cp2"])
        self.assertEqual(len(store), 1)

    def test_too_large_is_refused_and_stores_nothing(self):
        store = make(limit=100)
        put(store, "a", 60)
        with self.assertRaises(history.StoreError) as caught:
            put(store, "a", 101)
        self.assertEqual(caught.exception.kind, "too_large")
        self.assertEqual(store.used_bytes, 60)
        self.assertEqual([c.id for c in store.list("a")], ["cp1"])
        # the refused one burned no id and evicted nobody
        checkpoint, evicted = put(store, "a", 10)
        self.assertEqual((checkpoint.id, evicted), ("cp2", []))

    def test_check_fits_is_the_same_refusal_before_any_capture(self):
        store = make(limit=100)
        store.check_fits(100)
        with self.assertRaises(history.StoreError) as caught:
            store.check_fits(101)
        self.assertEqual(caught.exception.kind, "too_large")

    def test_limit_can_change_between_operations(self):
        store = make(limit=100)
        put(store, "a", 60)
        store.limit_bytes = 50
        with self.assertRaises(history.StoreError):
            put(store, "a", 51)
        _, evicted = put(store, "a", 50)
        self.assertEqual(evicted, ["cp1"])

    def test_per_document_cap_evicts_that_documents_oldest(self):
        store = make(limit=10000, per_document=3)
        put(store, "a", 1)          # cp1
        put(store, "b", 1)          # cp2
        put(store, "a", 1)          # cp3
        put(store, "a", 1)          # cp4
        _, evicted = put(store, "a", 1)
        self.assertEqual(evicted, ["cp1"])
        self.assertEqual([c.id for c in store.list("a")], ["cp3", "cp4", "cp5"])
        self.assertEqual([c.id for c in store.list("b")], ["cp2"])

    def test_default_cap_is_twenty(self):
        store = make(limit=10000)
        for _ in range(20):
            put(store, "a", 1)
        _, evicted = put(store, "a", 1)
        self.assertEqual(evicted, ["cp1"])
        self.assertEqual(len(store.list("a")), 20)

    def test_cap_and_budget_evictions_are_both_reported_in_order(self):
        store = make(limit=100, per_document=2)
        put(store, "a", 30)         # cp1
        put(store, "b", 30)         # cp2
        put(store, "a", 30)         # cp3
        _, evicted = put(store, "a", 50)
        self.assertEqual(evicted, ["cp1", "cp2"])
        self.assertEqual(store.used_bytes, 80)

    def test_drop_by_ids(self):
        store = make()
        for _ in range(3):
            put(store, "a", 10)
        self.assertEqual(store.drop("a", ["cp3", "cp1", "cp3"]), ["cp3", "cp1"])
        self.assertEqual(store.used_bytes, 10)
        self.assertEqual([c.id for c in store.list("a")], ["cp2"])

    def test_drop_all_of_one_document_only(self):
        store = make()
        put(store, "a", 10)
        put(store, "b", 5)
        put(store, "a", 10)
        self.assertEqual(store.drop("a"), ["cp1", "cp3"])
        self.assertEqual(store.used_bytes, 5)
        self.assertEqual(store.drop("a"), [])
        self.assertEqual([c.id for c in store.list("b")], ["cp2"])

    def test_drop_is_all_or_nothing(self):
        store = make()
        put(store, "a", 10)
        put(store, "b", 10)
        for ids in (["cp1", "cp9"], ["cp1", "cp2"]):
            with self.assertRaises(history.StoreError) as caught:
                store.drop("a", ids)
            self.assertEqual(caught.exception.kind, "not_found")
        self.assertEqual(len(store), 2)
        self.assertEqual(store.used_bytes, 20)

    def test_forget_closed_drops_only_documents_that_are_gone(self):
        store = make()
        put(store, "a", 10)
        put(store, "b", 5)
        put(store, "a", 10)
        put(store, "c", 1)
        self.assertEqual(store.forget_closed(["b", "c"]), ["cp1", "cp3"])
        self.assertEqual(store.used_bytes, 6)
        self.assertIsNone(store.get("cp1"))
        self.assertEqual(store.forget_closed(["b", "c"]), [])
        self.assertEqual(store.forget_closed([]), ["cp2", "cp4"])
        self.assertEqual((len(store), store.used_bytes), (0, 0))

    def test_zero_size_checkpoints_are_allowed(self):
        store = make(limit=100)
        _, evicted = put(store, "a", 0)
        self.assertEqual(evicted, [])


class HelperTests(unittest.TestCase):
    def test_limit_parsing(self):
        self.assertEqual(history.parse_limit_mb(None), 1024 * MB)
        self.assertEqual(history.parse_limit_mb(""), 1024 * MB)
        self.assertEqual(history.parse_limit_mb("2"), 2 * MB)
        self.assertEqual(history.parse_limit_mb("0.5"), MB // 2)
        for bad in ("zero", "0", "-3", "nan", "inf x"):
            with self.assertRaises(history.StoreError) as caught:
                history.parse_limit_mb(bad)
            self.assertIn(history.CHECKPOINT_MB_ENV, str(caught.exception))

    def test_rect_union_ignores_empties(self):
        self.assertIsNone(history.rect_union(None, (0, 0, 0, 5)))
        self.assertEqual(history.rect_union((1, 2, 3, 4), None), (1, 2, 3, 4))
        self.assertEqual(history.rect_union(None, (1, 2, 3, 4)), (1, 2, 3, 4))
        self.assertEqual(history.rect_union((0, 0, 10, 10), (20, -5, 5, 5)),
                         (0, -5, 25, 15))

    def test_rect_contains(self):
        outer = (0, 0, 10, 10)
        self.assertTrue(history.rect_contains(outer, (2, 2, 3, 3)))
        self.assertTrue(history.rect_contains(outer, outer))
        self.assertFalse(history.rect_contains(outer, (5, 5, 6, 3)))
        self.assertFalse(history.rect_contains(outer, (-1, 0, 3, 3)))
        self.assertTrue(history.rect_contains(None, None))
        self.assertTrue(history.rect_contains(None, (0, 0, 0, 0)))
        self.assertFalse(history.rect_contains(None, (0, 0, 1, 1)))


if __name__ == "__main__":
    unittest.main()
