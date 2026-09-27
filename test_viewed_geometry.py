#!/usr/bin/env python3
"""Regression tests for the viewed-geometry guard.

Krita-free: the plugin package is loaded with a stand-in for PyQt, so the rule
that refuses a size-changing edit on a document a canvas is showing can be
checked without a running Krita. The crash it defends against can only be
measured live (see stress_close.py and the README's crash note).

    python3 test_viewed_geometry.py
    python3 -m unittest test_viewed_geometry -v

The contract under test: a size-changing operation on a *viewed* document is
refused before anything is mutated, with a machine-readable kind and a message
that names the workaround; a view-less document -- the supported mode for
scripted work -- runs the same operations; operations that leave the size alone
(flatten, layers, paint, filters) stay available on a viewed document; and a
viewed document is handed back to its canvas instead of staying in batch mode,
which is what a hidden document wants.
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
PACKAGE = "krita_viewed_geometry_testpkg"


class _Dummy(object):
    """Stand-in for a Qt class that the module imports but never calls here."""


def _fabricate(name, module="<test>"):
    return type(name, (_Dummy,), {"__module__": module})


class _CompatModule(types.ModuleType):
    """The plugin's compat module, fabricating whatever else is imported.

    compat.py itself needs a real PyQt, which only exists inside Krita. Ops and
    imaging import ~20 names from it; the code under test here calls none of
    them.
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


class _FakeNode(object):
    """A layer node with no children -- enough for an empty layer tree."""

    def childNodes(self):
        return []


class _FakeDocument(object):
    """A document whose every call is recorded."""

    def __init__(self, name="t", views=1):
        self._name = name
        self._views = views
        self._batchmode = False
        self.calls = []

    def _record(self, name):
        self.calls.append(name)

    # identity and summary
    def name(self): return self._name
    def fileName(self): return ""
    def width(self): return 300
    def height(self): return 220
    def colorModel(self): return "RGBA"
    def colorDepth(self): return "U8"
    def colorProfile(self): return "sRGB"
    def resolution(self): return 300.0
    def xRes(self): return 300.0
    def yRes(self): return 300.0
    def modified(self): return False

    def batchmode(self): return self._batchmode
    def setBatchmode(self, value):
        self._batchmode = bool(value)
        self._record("setBatchmode({0})".format(bool(value)))

    # the operations under test
    def refreshProjection(self): self._record("refreshProjection")
    def waitForDone(self): self._record("waitForDone")
    def crop(self, x, y, width, height): self._record("crop")
    def resizeImage(self, x, y, width, height): self._record("resizeImage")
    def scaleImage(self, width, height, xres, yres, strategy):
        self._record("scaleImage")
    def rotateImage(self, radians): self._record("rotateImage")
    def flatten(self): self._record("flatten")

    # layer tree
    def activeNode(self): return None
    def rootNode(self): return _FakeNode()


class _FakeView(object):
    def __init__(self, doc):
        self._doc = doc

    def document(self):
        return self._doc


class _FakeKrita(object):
    def __init__(self, doc):
        views = [_FakeView(doc)] if doc._views else []
        self._doc = doc
        self._views = views

    def documents(self):
        return [self._doc]

    def activeDocument(self):
        return self._doc

    def windows(self):
        return [self._window()]

    def activeWindow(self):
        return self._window()

    def _window(self):
        return types.SimpleNamespace(
            views=lambda: list(self._views),
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


class _GeometryCase(unittest.TestCase):
    """A fake document, a fake Krita and the environment the guard reads."""

    def setUp(self):
        self.addCleanup(sys.modules.pop, "krita", None)
        self.addCleanup(ops._VIEWLESS.clear)
        # A stale switch in the developer's shell must not decide a test.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(ops.VIEWED_GEOMETRY_ENV, None)

    def _document(self, views):
        doc = _FakeDocument(views=views)
        _install_krita(doc)
        return doc

    def _call(self, op_name, params):
        return ops.OPS[op_name](params)


class ViewedGeometryGuardTest(_GeometryCase):
    """_refuse_on_view: who is refused, and what the caller is told."""

    def test_a_view_less_document_is_allowed(self):
        doc = self._document(views=0)
        ops._refuse_on_view(doc, "crop_image")       # must not raise

    def test_a_viewed_document_is_refused_before_anything_happens(self):
        doc = self._document(views=1)
        with self.assertRaises(ops.OpError) as caught:
            ops._refuse_on_view(doc, "crop_image")
        self.assertEqual(caught.exception.kind, "unsafe_on_view")
        message = str(caught.exception)
        self.assertIn("crop_image", message)
        self.assertIn("view=false", message)
        self.assertIn(ops.VIEWED_GEOMETRY_ENV, message)
        self.assertEqual(doc.calls, [], "the refusal must precede the edit")

    def test_the_escape_hatch_allows_the_edit(self):
        for raw in ("1", "true", "yes", "on", " TRUE "):
            with self.subTest(value=raw):
                doc = self._document(views=1)
                with mock.patch.dict(os.environ,
                                     {ops.VIEWED_GEOMETRY_ENV: raw}):
                    with mock.patch.object(ops, "_trace") as trace:
                        ops._refuse_on_view(doc, "crop_image")
                self.assertTrue(trace.called, "an overridden refusal is traced")

    def test_anything_else_keeps_the_guard(self):
        for raw in ("0", "no", "false", "", "maybe", "2"):
            with self.subTest(value=raw):
                doc = self._document(views=1)
                with mock.patch.dict(os.environ,
                                     {ops.VIEWED_GEOMETRY_ENV: raw}):
                    self.assertRaises(ops.OpError, ops._refuse_on_view,
                                      doc, "crop_image")


class ViewAttachTest(_GeometryCase):
    """_attach_view: a viewed document keeps batch mode on.

    Clearing batch mode hands the document to Krita's GUI update path, and
    measured on Krita 5.3.4 the process then dies within seconds (idle death
    after 10s with batch mode off, no death in 120s with it on). See
    tmp/baseline-2026-09-27/guard-idle-ab.log.
    """

    def test_a_viewed_document_keeps_batch_mode(self):
        doc = self._document(views=0)
        doc.setBatchmode(True)
        added, note = ops._attach_view(doc, {})
        self.assertTrue(added)
        self.assertIsNone(note)
        self.assertTrue(doc.batchmode(),
                        "a viewed document stays in batch mode on purpose")

    def test_a_view_less_document_keeps_batch_mode(self):
        doc = self._document(views=0)
        doc.setBatchmode(True)
        added, note = ops._attach_view(doc, {"view": False})
        self.assertFalse(added)
        self.assertIn("view=false", note)
        self.assertTrue(doc.batchmode())


class GeometryOpWiringTest(_GeometryCase):
    """The registered operations, not just the helper, follow that rule."""

    SIZE_CHANGING = (
        ("crop_image", {"x": 0, "y": 0, "width": 200, "height": 150}),
        ("scale_image", {"width": 150, "height": 110}),
        ("rotate_image", {"degrees": 90}),
        ("resize_canvas", {"width": 320, "height": 240}),
    )
    MUTATIONS = {"crop_image": "crop", "scale_image": "scaleImage",
                 "rotate_image": "rotateImage",
                 "resize_canvas": "resizeImage"}

    def test_every_size_changing_operation_refuses_a_viewed_document(self):
        for op_name, params in self.SIZE_CHANGING:
            with self.subTest(op=op_name):
                doc = self._document(views=1)
                with self.assertRaises(ops.OpError) as caught:
                    self._call(op_name, params)
                self.assertEqual(caught.exception.kind, "unsafe_on_view")
                self.assertNotIn(self.MUTATIONS[op_name], doc.calls)

    def test_every_size_changing_operation_runs_without_a_view(self):
        for op_name, params in self.SIZE_CHANGING:
            with self.subTest(op=op_name):
                doc = self._document(views=0)
                result = self._call(op_name, params)
                self.assertIn(self.MUTATIONS[op_name], doc.calls)
                self.assertEqual(result["after"]["views"], 0)

    def test_flatten_stays_available_on_a_viewed_document(self):
        doc = self._document(views=1)
        result = self._call("flatten_image", {})
        self.assertIn("flatten", doc.calls)
        self.assertTrue(result["flattened"])


if __name__ == "__main__":
    unittest.main()
