#!/usr/bin/env python3
"""Regression tests for the pre-close event-loop drain.

Krita-free: the plugin package is loaded with a stand-in for PyQt, and a fake
document records every call the close path makes. The crash the drain defends
against -- the layer docker's node model running a queued update for a document
that ``Document.close()`` has already torn down -- can only be measured live
(see test_mcp.py and probe_close_age.py).

    python3 test_close_settle.py
    python3 -m unittest test_close_settle -v

The contract under test:

* ``close_document`` drains the GUI event loop *before* it tears the document
  down, while the document is still whole and before the document is put into
  batch mode, and keeps the existing pump *after* the close;
* ``KRITA_MCP_CLOSE_SETTLE_S`` sets that pre-close budget and ``0`` switches
  the drain off, which is what makes the live A/B measurable;
* a refused close (unsaved changes without ``discard_changes``) neither tears
  anything down nor pays for the drain.
"""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PLUGIN = HERE / "pykrita" / "krita_mcp"
PACKAGE = "krita_close_settle_testpkg"


class _Dummy(object):
    """Stand-in for a Qt class that the module imports but never calls here."""


def _fabricate(name, module="<test>"):
    return type(name, (_Dummy,), {"__module__": module})


class _CompatModule(types.ModuleType):
    """The plugin's compat module, fabricating whatever else is imported.

    compat.py itself needs a real PyQt, which only exists inside Krita. Ops
    imports ~20 names from it; the code under test here calls none of them.
    """

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _fabricate(name, self.__name__)
        setattr(self, name, value)
        return value


class _AutoNamespace(object):
    """Stand-in for QtCompat, which ops.py reads at module level."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _fabricate(name, "QtCompat")
        setattr(self, name, value)
        return value


def _install_plugin_package():
    """Load gate.py, imaging.py and ops.py as a private package."""
    compat = _CompatModule(PACKAGE + ".compat")
    compat.__dict__.update({
        "QtCompat": _AutoNamespace(),
        "Qt": _AutoNamespace(),
    })
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN)]
    package.compat = compat
    sys.modules[PACKAGE] = package
    sys.modules[PACKAGE + ".compat"] = compat

    for name in ("gate", "imaging", "ops"):
        spec = importlib.util.spec_from_file_location(
            PACKAGE + "." + name, PLUGIN / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        setattr(package, name, module)
    return package.ops


ops = _install_plugin_package()


class _FakeDocument(object):
    """A document that records the close sequence and reports what is asked."""

    def __init__(self, name="close me", modified=False, file_name=""):
        self._name = name
        self._modified = modified
        self._file_name = file_name
        self._batchmode = False
        self.order = []

    def _record(self, entry):
        self.order.append(entry)

    # identity and summary, as _doc_summary needs them
    def name(self): return self._name
    def fileName(self): return self._file_name
    def width(self): return 300
    def height(self): return 220
    def colorModel(self): return "RGBA"
    def colorDepth(self): return "U8"
    def colorProfile(self): return "sRGB"
    def resolution(self): return 300.0

    def modified(self): return self._modified
    def setModified(self, value):
        self._modified = bool(value)
        self._record("setModified({0})".format(bool(value)))

    def batchmode(self): return self._batchmode
    def setBatchmode(self, value):
        self._batchmode = bool(value)
        self._record("setBatchmode({0})".format(bool(value)))

    def setAutosave(self, value):
        self._record("setAutosave({0})".format(bool(value)))

    def waitForDone(self):
        self._record("waitForDone")

    def save(self):
        self._record("save")
        return True

    def close(self):
        self._record("close")


class _FakeView(object):
    def __init__(self, doc):
        self._doc = doc

    def document(self):
        return self._doc


class _FakeKrita(object):
    def __init__(self, doc):
        self._doc = doc

    def documents(self): return [self._doc]
    def activeDocument(self): return self._doc
    def activeWindow(self): return self._window()
    def windows(self): return [self._window()]

    def _window(self):
        return types.SimpleNamespace(views=lambda: [_FakeView(self._doc)],
                                     addView=lambda doc: None)

    def readSetting(self, group, key, default):
        return default


def _install_krita(doc):
    """Register a ``krita`` module whose Krita.instance() serves ``doc``."""
    module = types.ModuleType("krita")
    module.Krita = type("Krita", (), {
        "instance": staticmethod(lambda: _FakeKrita(doc)),
    })
    sys.modules["krita"] = module
    return module


class _CloseCase(unittest.TestCase):
    """A fake document, a fake Krita and a recorder in place of the pump."""

    def setUp(self):
        self.addCleanup(sys.modules.pop, "krita", None)
        self.addCleanup(ops._VIEWLESS.clear)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(ops.CLOSE_SETTLE_ENV, None)
        os.environ.pop("KRITA_MCP_TRACE", None)
        self.order = []
        self.drains = []

    def _document(self, modified=False, file_name=""):
        doc = _FakeDocument(modified=modified, file_name=file_name)
        _install_krita(doc)

        def record_drain(budget):
            self.drains.append(budget)
            doc._record("drain(batchmode={0})".format(doc.batchmode()))

        patcher = mock.patch.object(ops, "_drain_event_loop", record_drain)
        patcher.start()
        self.addCleanup(patcher.stop)
        return doc

    def _close(self, params=None):
        return ops.OPS["close_document"](params or {"document": "close me",
                                                    "discard_changes": True})


class DrainOrder(_CloseCase):
    """Where the drain sits in the close sequence."""

    def _drain_positions(self, doc):
        return [index for index, entry in enumerate(doc.order)
                if entry.startswith("drain")]

    def test_drain_runs_before_the_document_is_torn_down(self):
        doc = self._document()
        self._close()
        positions = self._drain_positions(doc)
        self.assertTrue(positions)
        self.assertLess(positions[0], doc.order.index("close"))

    def test_drain_runs_before_the_document_enters_batch_mode(self):
        # Batch mode suppresses Krita's own view updates, so the drain has to
        # happen while the document is still in its normal state.
        doc = self._document()
        self._close()
        positions = self._drain_positions(doc)
        self.assertEqual(doc.order[positions[0]], "drain(batchmode=False)")
        self.assertLess(positions[0], doc.order.index("setBatchmode(True)"))

    def test_the_post_close_pump_still_runs(self):
        doc = self._document()
        self._close()
        positions = self._drain_positions(doc)
        self.assertEqual(len(positions), 2)
        self.assertGreater(positions[1], doc.order.index("close"))

    def test_the_pre_close_drain_uses_the_configured_budget(self):
        os.environ[ops.CLOSE_SETTLE_ENV] = "1.25"
        self._document()
        self._close()
        self.assertEqual(self.drains[0], 1.25)
        self.assertEqual(self.drains[1], ops.CLOSE_PUMP_SECONDS)


class SaveBeforeClose(_CloseCase):
    """The save path must not leave the document in batch mode for the drain."""

    def test_a_saved_document_is_drained_in_its_normal_state(self):
        doc = self._document(modified=True, file_name="/tmp/save-me.kra")
        self._close({"document": "close me", "save": True})
        positions = [index for index, entry in enumerate(doc.order)
                     if entry.startswith("drain")]
        self.assertTrue(positions)
        self.assertEqual(doc.order[positions[0]], "drain(batchmode=False)")
        self.assertLess(doc.order.index("save"), positions[0])
        self.assertIn("close", doc.order)


class SettleBudget(unittest.TestCase):
    """The switch that makes the live A/B measurable."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(ops.CLOSE_SETTLE_ENV, None)

    def test_default_budget_spans_the_node_model_compressor(self):
        # KisNodeModel processes its update queue from a 100 ms coalescing
        # timer, so a shorter drain could return before the queue is handled.
        self.assertGreaterEqual(ops.CLOSE_SETTLE_SECONDS + 1e-9,
                                3 * ops.NODE_MODEL_TICK_SECONDS)

    def test_unset_environment_uses_the_default(self):
        self.assertEqual(ops._close_settle_budget(), ops.CLOSE_SETTLE_SECONDS)

    def test_setting_the_budget(self):
        os.environ[ops.CLOSE_SETTLE_ENV] = "0.75"
        self.assertEqual(ops._close_settle_budget(), 0.75)

    def test_zero_turns_the_drain_off(self):
        os.environ[ops.CLOSE_SETTLE_ENV] = "0"
        self.assertEqual(ops._close_settle_budget(), 0.0)

    def test_the_cap_is_accepted(self):
        os.environ[ops.CLOSE_SETTLE_ENV] = str(ops.CLOSE_SETTLE_MAX_SECONDS)
        self.assertEqual(ops._close_settle_budget(),
                         ops.CLOSE_SETTLE_MAX_SECONDS)

    def test_unusable_values_fall_back_to_the_default(self):
        # A typo must neither disable the drain nor hang the GUI thread.
        for raw in ("soon", "", "nan", "inf", "-inf", "-3", "1e9"):
            os.environ[ops.CLOSE_SETTLE_ENV] = raw
            self.assertEqual(ops._close_settle_budget(),
                             ops.CLOSE_SETTLE_SECONDS, raw)


class DrainDisabled(_CloseCase):
    """The A/B switch must not change anything else about the close."""

    def test_zero_keeps_the_pre_close_drain_off(self):
        os.environ[ops.CLOSE_SETTLE_ENV] = "0"
        doc = self._document()
        self._close()
        positions = [index for index, entry in enumerate(doc.order)
                     if entry.startswith("drain")]
        self.assertEqual(len(positions), 1)
        self.assertGreater(positions[0], doc.order.index("close"))


class RefusedClose(_CloseCase):
    """A refusal must stay free of side effects."""

    def test_unsaved_changes_are_refused_without_draining(self):
        doc = self._document(modified=True)
        with self.assertRaises(ops.OpError) as caught:
            self._close({"document": "close me"})
        self.assertEqual(caught.exception.kind, "unsaved_changes")
        self.assertEqual(doc.order, [])
        self.assertEqual(self.drains, [])

    def test_discard_changes_still_closes_a_modified_document(self):
        doc = self._document(modified=True)
        self._close({"document": "close me", "discard_changes": True})
        self.assertIn("close", doc.order)


if __name__ == "__main__":
    unittest.main(verbosity=2)
