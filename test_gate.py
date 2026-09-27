#!/usr/bin/env python3
"""Regression tests for the arbitrary-Python gate.

Pure, deterministic and Krita-free: they import the plugin's policy module
directly plus the MCP server, so they run anywhere Python does.

    python3 test_gate.py
    python3 -m unittest test_gate -v

The gate has two halves and both are covered here at the level that can be
checked without a running Krita:

  * plugin side: pykrita/krita_mcp/gate.py, the policy the bridge consults
  * server side: mcp_server.py, which advertises and refuses the gated tool

What these tests cannot see is the wiring inside the plugin, because
pykrita/krita_mcp/ops.py imports PyQt and libkis and therefore only loads
inside Krita. The live suite (test_mcp.py) exercises that half over HTTP: it
proves the script-action guard is actually consulted, and that a default
install hides and refuses run_python end to end.
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _load(name, path):
    """Load a file by path under a private name.

    The gate, the MCP server and the live harness are separate files rather
    than a package. Importing them as top-level names would append this
    directory and the plugin directory to sys.path and register generic names
    like `gate` and `mcp_server` for every later test in the same process.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("krita_gate_under_test", HERE / "pykrita" / "krita_mcp" / "gate.py")
mcp_server = _load("krita_gate_mcp_server", HERE / "mcp_server.py")
test_mcp = _load("krita_gate_harness", HERE / "test_mcp.py")


class GatePolicyTest(unittest.TestCase):
    """decision(): which switch wins, and what the default is."""

    def test_default_is_disabled(self):
        self.assertEqual(gate.decision(gate.UNSET, None), (False, "default"))

    def test_environment_enables_when_kritarc_has_no_opinion(self):
        self.assertEqual(gate.decision(gate.UNSET, "1"), (True, "env"))

    def test_setting_enables(self):
        for raw in ("true", "TRUE", "yes", "on", "1"):
            with self.subTest(raw=raw):
                self.assertEqual(gate.decision(raw, None), (True, "setting"))

    def test_setting_false_disables(self):
        self.assertEqual(gate.decision("false", None), (False, "setting"))

    def test_explicit_deny_beats_an_enabling_environment(self):
        # A stale KRITA_MCP_ALLOW_PYTHON=1 must not override an explicit
        # allow_python=false in kritarc.
        self.assertEqual(gate.decision("false", "1"), (False, "setting"))

    def test_environment_can_force_the_gate_off(self):
        self.assertEqual(gate.decision("true", "0"), (False, "env"))

    def test_setting_wins_when_both_switches_allow(self):
        self.assertEqual(gate.decision("true", "1"), (True, "setting"))

    def test_absent_setting_survives_a_krita_round_trip(self):
        # Krita hands the default back as a fresh string object, so "absent"
        # has to be recognised by value. An identity comparison here reads a
        # missing key as an explicit deny and closes the gate on every launch.
        round_tripped = "".join(["\x00", "unset"])
        self.assertIsNot(round_tripped, gate.UNSET)
        self.assertEqual(gate.decision(round_tripped, "1"), (True, "env"))
        self.assertEqual(gate.decision(round_tripped, None), (False, "default"))

    def test_unrecognised_values_read_as_off(self):
        for raw in ("", "maybe", "2", "no"):
            with self.subTest(raw=raw):
                self.assertEqual(gate.decision(raw, None)[0], False)


class GateDecisionSourceTest(unittest.TestCase):
    """The policy must also survive the real environment lookup."""

    def setUp(self):
        self.gate = gate.Gate(read_setting=lambda: gate.UNSET)

    def test_refresh_follows_the_environment(self):
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            self.assertTrue(self.gate.refresh())
            self.assertEqual(self.gate.source, "env")
        with mock.patch.dict(os.environ):
            os.environ.pop(gate.ENV_VAR, None)
            self.assertFalse(self.gate.refresh())
            self.assertEqual(self.gate.source, "default")

    def test_refresh_failure_leaves_the_gate_closed(self):
        def explode():
            raise RuntimeError("settings unreadable")

        self.gate = gate.Gate(read_setting=explode)
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            with self.assertRaises(RuntimeError):
                self.gate.refresh()
            self.assertFalse(self.gate.enabled,
                             "an unreadable setting must leave the gate closed")
            self.assertEqual(self.gate.source, "unavailable")


class ScriptActionGuardTest(unittest.TestCase):
    """Krita's script-running actions, refused while the gate is closed."""

    def test_known_runners_are_flagged(self):
        for name in ("execute_script_1", "execute_script_10", "ten_scripts",
                     "python_scripter"):
            with self.subTest(action=name):
                self.assertTrue(gate.runs_code(name))

    def test_ordinary_actions_are_not_flagged(self):
        for name in ("edit_undo", "edit_redo", "deselect", "select_all",
                     "invert_selection", "file_close"):
            with self.subTest(action=name):
                self.assertFalse(gate.runs_code(name))


class GateThreadingTest(unittest.TestCase):
    """The /health path must not reach Krita from a worker thread."""

    def setUp(self):
        self.reads = []
        self.gate = gate.Gate(read_setting=self._read)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(gate.ENV_VAR, None)

    def _read(self):
        self.reads.append(1)
        return gate.UNSET

    def _run_on_worker(self, fn):
        out = []
        thread = threading.Thread(target=lambda: out.append(fn()),
                                  name="health-worker")
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "worker thread did not finish")
        return out

    def test_refresh_is_refused_off_the_owning_thread(self):
        self.gate.refresh()  # warms the cache on the owner thread
        out = self._run_on_worker(self.gate.refresh)
        self.assertEqual(out, [], "refresh() must raise on a foreign thread")
        self.assertFalse(self.gate.enabled)

    def test_advertised_hides_gated_operations_while_disabled(self):
        self.gate.refresh()
        self.assertEqual(self.gate.advertised(("status", "run_python")),
                         ["status"])

    def test_advertised_lists_everything_while_enabled(self):
        # The environment is read on every call, so the assertion belongs
        # inside the patch: the setting half is what the cache holds.
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            self.gate.refresh()
            names = self.gate.advertised(("status", "run_python"))
        self.assertEqual(names, ["status", "run_python"])

    def test_advertised_never_touches_the_settings_reader(self):
        self.gate.refresh()
        self.assertEqual(len(self.reads), 1)
        out = self._run_on_worker(
            lambda: self.gate.advertised(("status", "run_python")))
        self.assertEqual(out, [["status"]])
        self.assertEqual(len(self.reads), 1,
                         "the worker-thread path must read only cached state")


class ServerGateTest(unittest.TestCase):
    """mcp_server.py: advertise, and refuse before touching the bridge."""

    def setUp(self):
        calls = []

        class StubBridge(object):
            def call(self, op, params=None):
                calls.append((op, params))
                return {"stub": op}

        self.calls = calls
        self._saved = mcp_server.BRIDGE
        mcp_server.BRIDGE = StubBridge()

    def tearDown(self):
        mcp_server.BRIDGE = self._saved

    def _tool_names(self, allow_exec):
        return {t["name"] for t in mcp_server.public_tools(allow_exec)}

    def test_gated_tool_is_not_advertised_by_default(self):
        names = self._tool_names(False)
        self.assertIn("status", names)
        self.assertNotIn("run_python", names)

    def test_gated_tool_is_advertised_with_exec_enabled(self):
        self.assertIn("run_python", self._tool_names(True))

    def test_tools_list_follows_the_server_flag(self):
        def names(allow_exec):
            reply = mcp_server.Server(allow_exec=allow_exec).handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            return {t["name"] for t in reply["result"]["tools"]}

        self.assertNotIn("run_python", names(False))
        self.assertIn("run_python", names(True))

    def test_gated_call_is_refused_before_the_bridge(self):
        with self.assertRaises(mcp_server.BridgeError) as caught:
            mcp_server.call_tool("run_python", {"code": "1 + 1"},
                                 allow_exec=False)
        self.assertEqual(caught.exception.kind, "exec_disabled")
        self.assertEqual(self.calls, [],
                         "a refused call must not reach the bridge")

    def test_enabled_call_reaches_the_bridge(self):
        content = mcp_server.call_tool("run_python", {"code": "1 + 1"},
                                       allow_exec=True)
        self.assertEqual(self.calls, [("run_python", {"code": "1 + 1"})])
        self.assertIn("stub", content[0]["text"])

    def test_ungated_tools_still_pass_through(self):
        mcp_server.call_tool("status", {}, allow_exec=False)
        self.assertEqual(self.calls, [("status", {})])


class HarnessPredicateTest(unittest.TestCase):
    """test_mcp.py skips only when Krita's own gate refused, not the server."""

    def test_plugin_refusal_skips(self):
        self.assertTrue(test_mcp.is_plugin_gated(
            "disabled: run_python is disabled. It executes arbitrary Python "
            "inside Krita, so it is opt-in in two places:"))

    def test_server_refusal_does_not_skip(self):
        self.assertFalse(test_mcp.is_plugin_gated(
            "exec_disabled: run_python is not enabled on this MCP server: it "
            "runs arbitrary Python inside Krita."))

    def test_other_failures_do_not_skip(self):
        self.assertFalse(test_mcp.is_plugin_gated(
            "Could not reach the Krita MCP bridge."))


class GateResilienceTest(unittest.TestCase):
    """S1: a gate that cannot refresh must close, not break the bridge.

    The Gate refuses to refresh off the thread that owns Krita, which is what
    keeps libkis off the /health worker threads. That refusal used to escape
    into ops.dispatch(), where every operation turned into HTTP 500.
    """

    def _policy(self, instance):
        policy = getattr(instance, "refresh_or_closed", None)
        self.assertIsNotNone(policy, "Gate.refresh_or_closed is missing")
        return policy

    def test_foreign_thread_reports_closed_instead_of_raising(self):
        instance = gate.Gate(read_setting=lambda: gate.UNSET)
        policy = self._policy(instance)
        instance.refresh()  # owned by this thread
        logs, outcome = [], []

        def worker():
            outcome.append(policy(log=logs.append))

        thread = threading.Thread(target=worker, name="foreign-thread")
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [False],
                         "the adapter must report the gate closed")
        self.assertTrue(logs, "the refusal must be logged, not silently eaten")

    def test_owner_thread_still_reports_the_live_value(self):
        instance = gate.Gate(read_setting=lambda: gate.UNSET)
        policy = self._policy(instance)
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            self.assertTrue(policy())
            self.assertTrue(instance.enabled)


class GateFreshnessTest(unittest.TestCase):
    """S2: the environment override must apply before any refresh.

    A module reload builds a fresh Gate, and /health reads it from a worker
    thread before the next operation has refreshed it.
    """

    def test_environment_applies_before_any_refresh(self):
        instance = gate.Gate(read_setting=lambda: gate.UNSET)  # never refreshed
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            self.assertTrue(instance.enabled)
            self.assertIn("run_python",
                          instance.advertised(("status", "run_python")))

    def test_a_refused_refresh_never_advertises_the_gated_operation(self):
        instance = gate.Gate(read_setting=lambda: gate.UNSET)
        outcome = []
        thread = threading.Thread(
            target=lambda: outcome.append(instance.refresh_or_closed()))
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(outcome, [False], "the foreign thread must be refused")
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            self.assertFalse(instance.enabled,
                             "a gate that cannot refresh reports closed")
            self.assertEqual(instance.source, "unavailable")
            self.assertNotIn("run_python",
                             instance.advertised(("status", "run_python")))

    def test_cached_setting_decides_when_the_environment_is_silent(self):
        instance = gate.Gate(read_setting=lambda: "true")
        with mock.patch.dict(os.environ):
            os.environ.pop(gate.ENV_VAR, None)
            instance.refresh()
            self.assertIn("run_python",
                          instance.advertised(("status", "run_python")))

    def test_environment_deny_applies_before_any_refresh(self):
        instance = gate.Gate(read_setting=lambda: "true")
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "0"}):
            self.assertFalse(instance.enabled)


class ReaderFailureTest(unittest.TestCase):
    """F16: a failed settings read closes the gate, whatever the environment.

    ops._read_allow_python used to answer UNSET when libkis raised, which means
    "never configured" -- and an exported KRITA_MCP_ALLOW_PYTHON=1 then opened a
    gate whose kritarc setting may have said false. The adapter must report the
    gate unavailable instead: closed, hide run_python, and leave the rest of the
    bridge alone.
    """

    @staticmethod
    def _unreadable():
        def explode():
            raise RuntimeError("settings unreadable")

        return gate.Gate(read_setting=explode)

    def test_an_unreadable_setting_never_opens_the_gate(self):
        instance = self._unreadable()
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            self.assertFalse(instance.refresh_or_closed(),
                             "a failed read must not be read as 'allowed'")
            self.assertFalse(instance.enabled)
            self.assertEqual(instance.source, "unavailable")

    def test_an_unreadable_setting_hides_the_gated_operation(self):
        instance = self._unreadable()
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "1"}):
            instance.refresh_or_closed()
            self.assertEqual(instance.advertised(("status", "run_python")),
                             ["status"])

    def test_the_refusal_is_logged_once_with_its_cause(self):
        instance = self._unreadable()
        logs = []
        instance.refresh_or_closed(log=logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn("settings unreadable", logs[0])

    def test_a_later_readable_setting_opens_the_gate_again(self):
        reads = []

        def reader():
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("first read fails")
            return "true"

        instance = gate.Gate(read_setting=reader)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(gate.ENV_VAR, None)
        self.assertFalse(instance.refresh_or_closed())
        self.assertTrue(instance.refresh_or_closed(), "the next read decides")
        self.assertEqual(instance.source, "setting")


class GateSourceTest(unittest.TestCase):
    """S3: the operator must see why the gate is open, not just that it is."""

    def test_summary_names_the_source(self):
        summary = getattr(gate, "summary", None)
        self.assertIsNotNone(summary, "gate.summary is missing")
        # decision() only pairs enabled with a configured switch, so these are
        # the states the status dialog can actually show.
        self.assertEqual(summary(True, "env"), "enabled (environment)")
        self.assertEqual(summary(True, "setting"), "enabled (kritarc)")
        self.assertEqual(summary(False, "setting"), "disabled")
        self.assertEqual(summary(False, "env"), "disabled")
        self.assertEqual(summary(False, "default"), "disabled")
        self.assertEqual(summary(False, "unavailable"),
                         "disabled (could not be read from this thread)")

    def test_source_follows_the_deciding_switch(self):
        instance = gate.Gate(read_setting=lambda: "true")
        with mock.patch.dict(os.environ, {gate.ENV_VAR: "0"}):
            instance.refresh()
            self.assertFalse(instance.enabled)
            self.assertEqual(instance.source, "env")


class ImportHygieneTest(unittest.TestCase):
    """S5: importing this module must not disturb the interpreter.

    The module loads the plugin's policy file, the MCP server and the live
    harness. Doing that by inserting directories into sys.path and importing
    bare names leaks both into every other test in the same process.
    """

    def test_import_leaves_sys_path_and_module_names_alone(self):
        probe = (
            "import importlib.util, json, sys\n"
            "before = list(sys.path)\n"
            "names = ('gate', 'mcp_server', 'test_mcp')\n"
            "spec = importlib.util.spec_from_file_location('tg_probe', {path!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(json.dumps({{'added': [p for p in sys.path if p not in before],\n"
            "                   'bare': [n for n in names if n in sys.modules]}}))\n"
        ).format(path=str(HERE / "test_gate.py"))
        result = subprocess.run([sys.executable, "-c", probe],
                                capture_output=True, text=True, cwd=str(HERE),
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["added"], [],
                         "the module must not grow sys.path")
        self.assertEqual(payload["bare"], [],
                         "the module must not register bare module names")


if __name__ == "__main__":
    unittest.main(verbosity=2)
