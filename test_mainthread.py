#!/usr/bin/env python3
"""Regression tests for the GUI-thread invoker's timeout contract.

Krita-free: PyQt is replaced by a stand-in whose queued signal and singleShot
timers are delivered on demand, so "the GUI thread is busy" and "the deadline
expires mid-job" are deterministic rather than timing races.

    python3 test_mainthread.py
    python3 -m unittest test_mainthread -v

The contract under test: a job still queued when its deadline passes is
cancelled and never runs, even when the delivery was deferred by an operation
already in progress; a job that had already started is reported as an unknown
outcome rather than as a refusal; a job that finishes inside the race window
still reports its own result.
"""

import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PLUGIN = HERE / "pykrita" / "krita_mcp"
PACKAGE = "krita_mainthread_testpkg"


class _Signal(object):
    """pyqtSignal stand-in: connections are recorded, delivery is manual."""

    def __init__(self):
        self.slots = []
        self.pending = []

    def connect(self, slot, *flags):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            self.pending.append((slot, args))


class _SignalProperty(object):
    """pyqtSignal stand-in: one _Signal per owning instance."""

    def __init__(self):
        self._name = None

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        signal = instance.__dict__.get(self._name)
        if signal is None:
            signal = _Signal()
            instance.__dict__[self._name] = signal
        return signal


class _QTimer(object):
    """QTimer.singleShot stand-in: callbacks wait for drain()."""

    pending = []

    @classmethod
    def singleShot(cls, msecs, callback):
        cls.pending.append(callback)

    @classmethod
    def drain(cls):
        while cls.pending:
            cls.pending.pop(0)()


class _QObject(object):
    """QObject stand-in: accepts an optional parent, nothing else."""

    def __init__(self, parent=None):
        pass


def _pyqt_signal(*ignored):
    """pyqtSignal stand-in: one fresh descriptor per class attribute."""
    return _SignalProperty()


def _install_fake_qt():
    """Register the plugin package with a compat module the invoker imports."""
    qt = types.SimpleNamespace(QueuedConnection=0)
    compat = types.ModuleType(PACKAGE + ".compat")
    compat.__dict__.update({
        "QObject": _QObject,
        "QTimer": _QTimer,
        "QtCompat": qt,
        "Qt": qt,
        "pyqtSignal": _pyqt_signal,
    })
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN)]
    package.compat = compat
    sys.modules[PACKAGE] = package
    sys.modules[PACKAGE + ".compat"] = compat

    spec = importlib.util.spec_from_file_location(
        PACKAGE + ".mainthread", PLUGIN / "mainthread.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mainthread = _install_fake_qt()


def _wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class InvokerTimeoutTest(unittest.TestCase):

    def setUp(self):
        _QTimer.pending = []
        self.invoker = mainthread.MainThreadInvoker()

    def _deliver(self):
        """Run the queued deliveries the way Qt's event loop would."""
        signal = self.invoker._submit
        while signal.pending:
            slot, args = signal.pending.pop(0)
            slot(*args)

    def _call_in_thread(self, fn, timeout):
        """Start call() on its own thread and return (thread, outcome dict)."""
        outcome = {}

        def caller():
            try:
                outcome["value"] = self.invoker.call(fn, timeout=timeout)
            except BaseException as exc:  # asserted by the test
                outcome["error"] = exc
        thread = threading.Thread(target=caller)
        thread.start()
        return thread, outcome

    def test_delivered_job_returns_its_value(self):
        thread, outcome = self._call_in_thread(lambda: 41 + 1, timeout=5)
        self.assertTrue(_wait_for(lambda: self.invoker._submit.pending),
                        "the job was never submitted")
        self._deliver()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get("value"), 42)
        self.assertNotIn("error", outcome)

    def test_job_failure_is_reported_without_the_exception_object(self):
        def boom():
            raise ValueError("nope")
        thread, outcome = self._call_in_thread(boom, timeout=5)
        self.assertTrue(_wait_for(lambda: self.invoker._submit.pending))
        self._deliver()
        thread.join(5)
        error = outcome.get("error")
        self.assertIsInstance(error, mainthread.MainThreadError)
        self.assertEqual(error.type_name, "ValueError")
        self.assertEqual(error.message, "nope")
        self.assertIn("ValueError", error.formatted)
        self.assertFalse(self.invoker._busy)

    def test_queued_job_is_cancelled_by_its_deadline(self):
        ran = []

        def late():
            ran.append(1)
            return "too late"

        # call() from the invoker's own thread runs inline by design (see
        # MainThreadInvoker.call), so the waiting caller is a worker thread.
        thread, outcome = self._call_in_thread(late, timeout=0.05)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        error = outcome.get("error")
        self.assertIsInstance(error, mainthread.MainThreadTimeout)
        self.assertEqual(error.reason, "not_started")

        # The GUI thread gets around to the delivery now.
        self._deliver()
        _QTimer.drain()
        self.assertEqual(ran, [], "a cancelled job must not touch Krita")
        self.assertFalse(self.invoker._busy)

    def test_deferred_delivery_never_runs_after_cancellation(self):
        """The real F14 shape: delivery queued behind an operation in flight."""
        ran = []

        def late():
            ran.append(1)
            return "too late"

        self.invoker._busy = True  # a settle pump is running on the GUI thread
        thread, outcome = self._call_in_thread(late, timeout=0.3)
        self.assertTrue(_wait_for(lambda: self.invoker._submit.pending))
        self._deliver()            # delivered mid-operation: re-queued
        thread.join(5)
        self.assertEqual(outcome.get("error").reason, "not_started")

        self.invoker._busy = False  # the pump finished
        _QTimer.drain()
        self.assertEqual(ran, [], "the re-queued job must stay cancelled")

    def test_running_job_reports_an_unknown_outcome(self):
        started = threading.Event()
        release = threading.Event()

        def work():
            started.set()
            release.wait(5)
            return "finished"

        thread, outcome = self._call_in_thread(work, timeout=2)
        self.assertTrue(_wait_for(lambda: self.invoker._submit.pending))
        deliver_thread = threading.Thread(target=self._deliver)
        deliver_thread.start()
        self.assertTrue(started.wait(5), "the job never started")
        thread.join(5)
        self.assertFalse(thread.is_alive())

        error = outcome.get("error")
        self.assertIsInstance(error, mainthread.MainThreadTimeout)
        self.assertEqual(error.reason, "still_running")
        self.assertNotIn("value", outcome)

        release.set()
        deliver_thread.join(5)
        self.assertFalse(self.invoker._busy)

    def test_job_finishing_inside_the_race_window_still_returns(self):
        """The deadline can expire while the job already finished."""

        def deliver_now():
            self._deliver()

        class RacyEvent(threading.Event):
            def wait(self, timeout=None):
                deliver_now()   # the GUI thread finished just before the poll
                return False

        class RacyJob(mainthread._Job):
            def __init__(self, fn):
                super().__init__(fn)
                self.event = RacyEvent()

        with mock.patch.object(mainthread, "_Job", RacyJob):
            thread, outcome = self._call_in_thread(lambda: "raced",
                                                   timeout=0.01)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get("value"), "raced")
        self.assertNotIn("error", outcome)


if __name__ == "__main__":
    unittest.main()
