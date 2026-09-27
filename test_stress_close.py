#!/usr/bin/env python3
"""Unit tests for the soak harness's evidence handling.

Krita-free: the parts of stress_close.py that turn a trace log and a command
line into the run's evidence are exercised directly, because the harness is
the gate for the crash work -- a wrong "no crash" verdict is worse than a
crash.

    python3 test_stress_close.py
    python3 -m unittest test_stress_close -v
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, path):
    """Load a file by path under a private name (see test_gate.py)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# stress_close.py imports `mcp_server` by name, so the private copy has to be
# aliased for the duration of the import. Nothing else in the suite looks it
# up afterwards.
_mcp_server = _load("krita_stress_mcp_server", HERE / "mcp_server.py")
sys.modules["mcp_server"] = _mcp_server
try:
    stress_close = _load("krita_stress_close", HERE / "stress_close.py")
finally:
    sys.modules.pop("mcp_server", None)


class TraceEvidenceTest(unittest.TestCase):
    """trace_pending_op: a start without its end names the operation."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="krita-trace-")
        self.trace = os.path.join(self.state_dir, "krita_mcp_trace.log")
        self._previous = os.environ.get("KRITA_MCP_STATE_DIR")
        os.environ["KRITA_MCP_STATE_DIR"] = self.state_dir
        self.addCleanup(self._restore_state_dir)

    def _restore_state_dir(self):
        if self._previous is None:
            os.environ.pop("KRITA_MCP_STATE_DIR", None)
        else:
            os.environ["KRITA_MCP_STATE_DIR"] = self._previous

    def _write(self, text):
        with open(self.trace, "a", encoding="utf-8") as handle:
            handle.write(text)

    def test_no_trace_file_is_not_a_pending_op(self):
        self.assertIsNone(stress_close.trace_pending_op())

    def test_balanced_entries_leave_nothing_pending(self):
        self._write("1.0 --> draw {\"x\": 1}\n"
                    "1.1 <-- draw ok\n")
        self.assertIsNone(stress_close.trace_pending_op())

    def test_start_without_end_names_the_operation(self):
        self._write("1.0 --> draw {}\n"
                    "1.1 <-- draw ok\n"
                    "2.0 --> close_document {}\n")
        self.assertEqual(stress_close.trace_pending_op(), "close_document")

    def test_marker_inside_the_params_is_still_a_start(self):
        self._write("2.0 --> run_python {\"code\": \"print('--> not a marker')\"}\n")
        self.assertEqual(stress_close.trace_pending_op(), "run_python")

    def test_offset_ignores_an_earlier_run(self):
        self._write("1.0 --> close_document {}\n")   # an earlier crash
        offset = stress_close._trace_offset()
        # This run wrote nothing: the offset reports no pending operation,
        # while the unscoped scan still sees the earlier crash.
        self.assertIsNone(stress_close.trace_pending_op(offset))
        self.assertEqual(stress_close.trace_pending_op(), "close_document")

    def test_crash_report_names(self):
        self.assertTrue(stress_close._is_crash_report("krita-2026-09-27-1.ips"))
        self.assertTrue(stress_close._is_crash_report("krita-1.crash"))
        self.assertFalse(stress_close._is_crash_report("krita-1.txt"))
        self.assertFalse(stress_close._is_crash_report("kate-1.ips"))


class CommandLineTest(unittest.TestCase):
    """_arguments/_parse_groups: the harness refuses a run it cannot describe."""

    def test_defaults(self):
        self.assertEqual(stress_close._arguments([]),
                         (stress_close.DEFAULT_STRATEGY,
                          stress_close.DEFAULT_ITERATIONS))

    def test_explicit_strategy_and_count(self):
        self.assertEqual(stress_close._arguments(["op", "30"]), ("op", 30))
        self.assertEqual(stress_close._arguments(["deferred", "2"]),
                         ("deferred", 2))

    def test_unknown_strategy_is_refused(self):
        with self.assertRaises(ValueError):
            stress_close._arguments(["nope"])

    def test_bad_iteration_count_is_refused(self):
        for raw in ("x", "0", "-3"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    stress_close._arguments(["op", raw])

    def test_groups_default_exclude_python(self):
        previous = os.environ.pop("STRESS_GROUPS", None)
        try:
            self.assertNotIn("python", stress_close._parse_groups())
        finally:
            if previous is not None:
                os.environ["STRESS_GROUPS"] = previous

    def test_unknown_group_is_refused(self):
        previous = os.environ.get("STRESS_GROUPS")
        os.environ["STRESS_GROUPS"] = "reads,nope"
        try:
            with self.assertRaises(ValueError):
                stress_close._parse_groups()
        finally:
            if previous is None:
                os.environ.pop("STRESS_GROUPS", None)
            else:
                os.environ["STRESS_GROUPS"] = previous


class WorkloadGroupTest(unittest.TestCase):
    """_build: the geometry group is what a close-only run leaves out.

    A run without geometry still has to reach close with a fully edited
    document, so the assertions cover both directions: the four geometry
    operations disappear, and the editing/saving work stays.
    """

    def setUp(self):
        self.ops = []
        self._real_call = stress_close.call
        self._totals = dict(stress_close.TOTALS)
        stress_close.call = self._record
        self.addCleanup(self._restore)

    def _record(self, op, params=None, timeout=None):
        self.ops.append(op)
        # _roundtrip resolves the reopened document from what open_document
        # returned, so the stand-in has to look like a document summary.
        return {"name": stress_close.NAME, "file_name": "/tmp/stress.png"}

    def _restore(self):
        stress_close.call = self._real_call
        stress_close.TOTALS.update(self._totals)

    def _build(self, groups):
        stress_close._build(tempfile.mkdtemp(prefix="krita-groups-"), 1,
                            groups, True)

    def test_default_groups_run_the_geometry_sequence(self):
        self._build(stress_close.DEFAULT_GROUPS)
        for op in ("crop_image", "scale_image", "rotate_image",
                   "flatten_image"):
            with self.subTest(op=op):
                self.assertIn(op, self.ops)

    def test_a_run_without_geometry_still_edits_saves_and_reopens(self):
        groups = tuple(name for name in stress_close.DEFAULT_GROUPS
                       if name != "geometry")
        self._build(groups)
        for op in ("crop_image", "scale_image", "rotate_image",
                   "flatten_image"):
            with self.subTest(op=op):
                self.assertNotIn(op, self.ops)
        for op in ("create_document", "draw", "apply_filter",
                   "export_document", "save_document", "open_document",
                   "set_layer"):
            with self.subTest(op=op):
                self.assertIn(op, self.ops)

    def test_geometry_is_the_only_difference(self):
        with_geometry = (stress_close.DEFAULT_GROUPS,)
        self._build(with_geometry[0])
        full = list(self.ops)
        self.ops = []
        self._build(tuple(name for name in stress_close.DEFAULT_GROUPS
                          if name != "geometry"))
        self.assertEqual(
            [op for op in full if op not in ("crop_image", "scale_image",
                                             "rotate_image", "flatten_image")],
            self.ops)


if __name__ == "__main__":
    unittest.main()
