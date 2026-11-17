#!/usr/bin/env python3
"""Offline tests for update_vector_shapes planning and layer-SVG editing.

The Krita-facing half (box fit, rebuild, translate) needs a running Krita and
is exercised by tmp/probe-vector-update-2026-11-18.py. Everything here is
Krita-free: vector_ops is loaded with a fake ``krita_mcp.compat`` and
``krita_mcp.ops`` so the pure planning functions run with plain dicts.
"""

import importlib.util
import math
import sys
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parent
PKG_DIR = ROOT / "pykrita" / "krita_mcp"

# One real VectorLayer.toSvg() sample captured from Krita 5.3.4 (300 dpi,
# 1200x900 px document, three shapes). Keep it verbatim: prefixes, <defs>,
# element order and text/tail whitespace are what the helpers must preserve.
KRITA_LAYER_SVG = """<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 20010904//EN" "http://www.w3.org/TR/2001/REC-SVG-20010904/DTD/svg10.dtd">
<!-- Created using Krita: https://krita.org -->
<svg xmlns="http://www.w3.org/2000/svg" 
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:krita="http://krita.org/namespaces/svg/krita"
    xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
    width="288pt"
    height="216pt"
    viewBox="0 0 288 216">
<defs/>
<path id="t23-bubble" transform="matrix(0.24 0 0 0.24 23.9998689832871 24.0001768434892)" fill="#ffffff" stroke="#000000" stroke-width="3" stroke-linecap="butt" stroke-linejoin="miter" stroke-miterlimit="2" d="M174.962 151.115C148.387 160.244 117.631 162.43 88.766 157.243C59.9013 152.055 34.9608 139.86 18.8684 123.064C2.77602 106.267 -3.33478 86.0538 1.74241 66.4132C6.8196 46.7727 22.7271 29.0888 46.3246 16.8526C69.922 4.61646 99.5471 -1.31 129.351 0.243236C159.155 1.79647 187.039 10.72 207.498 25.2521C227.957 39.7843 239.549 58.9013 239.986 78.8293C240.424 98.7572 229.676 118.092 209.864 133.017L260.001 199.999Z" sodipodi:nodetypes="ccccccccccc"/><text id="t23-plain" krita:textVersion="3" transform="matrix(0.24 0 0 0.24 0 0)" fill="#000000" stroke-opacity="0" stroke="#000000" stroke-width="0" stroke-linecap="square" stroke-linejoin="bevel" x="520" y="220" style="font-size: 28;">Plain &lt;&amp;&gt; text</text><rect id="t23-rect" transform="matrix(0.24 0 0 0.24 168 124.8)" fill="#ff0000" stroke-opacity="0" stroke="#000000" stroke-width="0" stroke-linecap="square" stroke-linejoin="bevel" width="120" height="80"/>
</svg>
"""

# A replacement element as Krita would emit it from a scratch layer.
NEW_TEXT_SVG = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:krita="http://krita.org/namespaces/svg/krita" width="288pt" height="216pt" viewBox="0 0 288 216"><text id="t23-plain" krita:textVersion="3" transform="matrix(0.24 0 0 0.24 0 0)" fill="#000000" x="520" y="240" style="font-size: 20;">A much longer replacement</text></svg>"""

TWO_SHAPES_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="288pt" height="216pt" viewBox="0 0 288 216"><rect id="new-a" x="1" y="2" width="30" height="40"/><rect id="new-b" x="5" y="6" width="70" height="80"/></svg>"""


class _QPointF(object):
    def __init__(self, x=0.0, y=0.0):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


def _load_modules():
    """Load svgshapes and vector_ops without importing Krita's Qt stack."""
    package = types.ModuleType("krita_mcp")
    package.__path__ = [str(PKG_DIR)]
    sys.modules["krita_mcp"] = package

    compat = types.ModuleType("krita_mcp.compat")
    compat.QPointF = _QPointF
    sys.modules["krita_mcp.compat"] = compat
    package.compat = compat

    ops = types.ModuleType("krita_mcp.ops")

    class OpError(Exception):
        def __init__(self, message, kind="invalid_request"):
            super(OpError, self).__init__(message)
            self.kind = kind

    def _as_float(value, key):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise OpError("{0} must be a number, got {1!r}".format(key, value))

    def _as_bool(value, key):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "1"):
                return True
            if low in ("false", "no", "0"):
                return False
        raise OpError("{0} must be true or false, got {1!r}".format(key, value))

    def _as_int(value, key):
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            raise OpError("{0} must be a number, got {1!r}".format(key, value))

    def _log(_text):
        pass

    ops.OpError = OpError
    ops._as_float = _as_float
    ops._as_bool = _as_bool
    ops._as_int = _as_int
    ops._log = _log

    def _node_uuid(node):
        raw = node.uniqueId()
        return str(raw).strip("{}") or None

    def _insert_node(parent, child, ui_index):
        siblings = list(parent.childNodes())
        count = len(siblings)
        ui_index = max(0, min(count, ui_index))
        below_pos = count - ui_index - 1
        above = siblings[below_pos] if below_pos >= 0 else None
        parent.addChildNode(child, above)

    ops._node_uuid = _node_uuid
    ops._insert_node = _insert_node
    ops._parent_of = lambda doc, node: node.parentNode()
    ops._refresh = lambda doc: None
    ops.resolve_document = lambda ref: ops.document
    ops.resolve_node = lambda doc, ref: [
        n for n in doc.rootNode().childNodes() if n.name() == ref][0]
    sys.modules["krita_mcp.ops"] = ops
    package.ops = ops

    svg_spec = importlib.util.spec_from_file_location(
        "krita_mcp.svgshapes", PKG_DIR / "svgshapes.py")
    svgshapes = importlib.util.module_from_spec(svg_spec)
    sys.modules["krita_mcp.svgshapes"] = svgshapes
    package.svgshapes = svgshapes
    svg_spec.loader.exec_module(svgshapes)

    ops_spec = importlib.util.spec_from_file_location(
        "krita_mcp.vector_ops", PKG_DIR / "vector_ops.py")
    vector_ops = importlib.util.module_from_spec(ops_spec)
    sys.modules["krita_mcp.vector_ops"] = vector_ops
    package.vector_ops = vector_ops
    ops_spec.loader.exec_module(vector_ops)
    return svgshapes, vector_ops, OpError


svgshapes, vector_ops, OpError = _load_modules()
LayerSvgError = svgshapes.LayerSvgError


def element_by_id(root, shape_id):
    return [element for element in root if element.get("id") == shape_id][0]


class LayerSvgTests(unittest.TestCase):
    def parse(self, text):
        return svgshapes.parse_layer_svg(text)

    def test_shape_ids_count_and_defs(self):
        self.assertEqual(svgshapes.layer_shape_ids(KRITA_LAYER_SVG),
                         ["t23-bubble", "t23-plain", "t23-rect"])
        self.assertEqual(svgshapes.layer_shape_count(KRITA_LAYER_SVG), 3)
        root = self.parse(KRITA_LAYER_SVG)
        self.assertEqual([element.get("id")
                          for element in svgshapes.layer_shape_elements(root)],
                         ["t23-bubble", "t23-plain", "t23-rect"])
        self.assertTrue(any(element.tag.endswith("defs") for element in root))

    def test_round_trip_keeps_names_attributes_text_and_order(self):
        before = self.parse(KRITA_LAYER_SVG)
        after = self.parse(svgshapes.serialize_layer_svg(before))
        self.assertEqual(after.get("width"), "288pt")
        self.assertEqual(after.get("height"), "216pt")
        self.assertEqual(after.get("viewBox"), "0 0 288 216")
        old_shapes = svgshapes.layer_shape_elements(before)
        new_shapes = svgshapes.layer_shape_elements(after)
        self.assertEqual([e.get("id") for e in new_shapes],
                         [e.get("id") for e in old_shapes])
        for old, new in zip(old_shapes, new_shapes):
            self.assertEqual(old.attrib, new.attrib)
            self.assertEqual(old.text, new.text)
            self.assertEqual(old.tail, new.tail)
        self.assertEqual(new_shapes[1].text, "Plain <&> text")
        sodipodi = "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}"
        self.assertEqual(new_shapes[0].get(sodipodi + "nodetypes"),
                         "ccccccccccc")

    def test_drop_by_id_keeps_defs_and_other_shapes(self):
        edited, removed = svgshapes.drop_layer_shapes(
            KRITA_LAYER_SVG, ["t23-plain"])
        self.assertEqual(removed, ["t23-plain"])
        self.assertEqual(svgshapes.layer_shape_ids(edited),
                         ["t23-bubble", "t23-rect"])
        root = self.parse(edited)
        self.assertTrue(any(element.tag.endswith("defs") for element in root))
        rect = element_by_id(root, "t23-rect")
        self.assertEqual(rect.get("fill"), "#ff0000")
        self.assertEqual(rect.get("width"), "120")
        bubble = element_by_id(root, "t23-bubble")
        self.assertTrue(bubble.get("d").startswith("M174.962 151.115"))
        self.assertNotIn("Plain", edited)
        # prefixes used by the remaining attributes survive serialization
        self.assertIn("sodipodi:nodetypes", edited)
        self.assertIn("xmlns:sodipodi=", edited)

    def test_drop_refuses_missing_id(self):
        with self.assertRaisesRegex(LayerSvgError, "no top-level shape.*nope"):
            svgshapes.drop_layer_shapes(KRITA_LAYER_SVG, ["t23-plain", "nope"])

    def test_drop_by_index_keeps_order_and_separates_equal_ids(self):
        # Krita exports two shapes named "dup" as ids "dup" and "dup1", but a
        # hand-built layer can contain equal ids; position is unambiguous.
        svg = ('<svg xmlns="http://www.w3.org/2000/svg">'
               '<rect id="dup"/><rect id="dup"/><rect id="last"/></svg>')
        edited, dropped = svgshapes.drop_layer_shapes_at(svg, [0])
        self.assertEqual(dropped, 1)
        self.assertEqual(svgshapes.layer_shape_ids(edited), ["dup", "last"])
        edited, dropped = svgshapes.drop_layer_shapes_at(svg, [0, 2])
        self.assertEqual(dropped, 2)
        self.assertEqual(svgshapes.layer_shape_ids(edited), ["dup"])
        edited, dropped = svgshapes.drop_layer_shapes_at(
            KRITA_LAYER_SVG, [1])
        self.assertEqual(dropped, 1)
        self.assertEqual(svgshapes.layer_shape_ids(edited),
                         ["t23-bubble", "t23-rect"])
        with self.assertRaisesRegex(LayerSvgError, "out of range"):
            svgshapes.drop_layer_shapes_at(KRITA_LAYER_SVG, [3])
        with self.assertRaisesRegex(LayerSvgError, "must be an integer"):
            svgshapes.drop_layer_shapes_at(KRITA_LAYER_SVG, [True])

    def test_replace_in_place_keeps_order(self):
        replacement = svgshapes.layer_shape_element(NEW_TEXT_SVG, "t23-plain")
        edited = svgshapes.replace_layer_shape(
            KRITA_LAYER_SVG, "t23-plain", [replacement])
        self.assertEqual(svgshapes.layer_shape_ids(edited),
                         ["t23-bubble", "t23-plain", "t23-rect"])
        root = self.parse(edited)
        text = element_by_id(root, "t23-plain")
        self.assertEqual(text.text, "A much longer replacement")
        self.assertEqual(text.get("style"), "font-size: 20;")
        krita = "{http://krita.org/namespaces/svg/krita}"
        self.assertEqual(text.get(krita + "textVersion"), "3")
        self.assertIn("krita:textVersion", edited)
        self.assertTrue(any(element.tag.endswith("defs") for element in root))
        self.assertEqual(element_by_id(root, "t23-rect").get("fill"),
                         "#ff0000")

    def test_replace_with_several_elements_at_same_position(self):
        replacements = [svgshapes.layer_shape_element(TWO_SHAPES_SVG, "new-a"),
                        svgshapes.layer_shape_element(TWO_SHAPES_SVG, "new-b")]
        edited = svgshapes.replace_layer_shape(
            KRITA_LAYER_SVG, "t23-plain", replacements)
        self.assertEqual(svgshapes.layer_shape_ids(edited),
                         ["t23-bubble", "new-a", "new-b", "t23-rect"])

    def test_replace_refuses_missing_and_ambiguous_ids(self):
        replacement = svgshapes.layer_shape_element(NEW_TEXT_SVG, "t23-plain")
        with self.assertRaisesRegex(LayerSvgError, "no top-level shape.*nope"):
            svgshapes.replace_layer_shape(KRITA_LAYER_SVG, "nope", [replacement])
        duplicate = KRITA_LAYER_SVG.replace(
            '<rect id="t23-rect"', '<rect id="t23-plain"')
        with self.assertRaisesRegex(LayerSvgError, "appears 2 times"):
            svgshapes.replace_layer_shape(duplicate, "t23-plain", [replacement])

    def test_append_puts_shapes_on_top_and_keeps_defs(self):
        first = svgshapes.layer_shape_element(NEW_TEXT_SVG, "t23-plain")
        second = svgshapes.layer_shape_element(TWO_SHAPES_SVG, "new-a")
        edited = svgshapes.append_layer_shapes(
            KRITA_LAYER_SVG, [first, second])
        self.assertEqual(svgshapes.layer_shape_ids(edited),
                         ["t23-bubble", "t23-plain", "t23-rect",
                          "t23-plain", "new-a"])
        self.assertTrue(any(element.tag.endswith("defs")
                            for element in self.parse(edited)))

    def test_empty_layer_takes_root_size_from_a_sibling_svg(self):
        # toSvg() of an empty vector layer is "", so add on a new layer
        # borrows the scratch layer's root (same document size and viewBox).
        root = svgshapes.empty_root_like(KRITA_LAYER_SVG)
        template = self.parse(KRITA_LAYER_SVG)
        self.assertEqual(svgshapes.layer_shape_elements(root), [])
        self.assertEqual(root.get("viewBox"), template.get("viewBox"))
        self.assertEqual(root.get("width"), template.get("width"))
        svgshapes.append_root_shapes(
            root, [svgshapes.layer_shape_element(NEW_TEXT_SVG, "t23-plain")])
        self.assertEqual(svgshapes.layer_shape_ids(
            svgshapes.serialize_layer_svg(root)), ["t23-plain"])
        with self.assertRaises(LayerSvgError):
            svgshapes.parse_layer_svg("")

    def test_replace_requires_a_shape_element_with_an_id(self):
        idless = svgshapes.parse_layer_svg(
            '<svg xmlns="http://www.w3.org/2000/svg"><rect x="1" y="2" '
            'width="3" height="4"/></svg>')[0]
        with self.assertRaisesRegex(LayerSvgError, "needs an id"):
            svgshapes.replace_layer_shape(
                KRITA_LAYER_SVG, "t23-plain", [idless])
        with self.assertRaisesRegex(LayerSvgError, "top-level shape element"):
            svgshapes.replace_layer_shape(
                KRITA_LAYER_SVG, "t23-plain",
                [svgshapes.parse_layer_svg(KRITA_LAYER_SVG)[0]])


class _FakeShape(object):
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeLayer(object):
    def __init__(self, names):
        self._shapes = [_FakeShape(name) for name in names]

    def name(self):
        return "Shapes"

    def shapes(self):
        return list(self._shapes)


class UpdatePlanningTests(unittest.TestCase):
    def refused(self, updates, pattern, kind="invalid_request"):
        with self.assertRaises(OpError) as caught:
            vector_ops._split_updates(updates)
        self.assertRegex(str(caught.exception), pattern)
        self.assertEqual(caught.exception.kind, kind)
        return caught.exception

    def test_valid_shape_and_translate_plan(self):
        plan = vector_ops._split_updates([
            {"name": "bubble", "translate": [30, -10]},
            {"name": "boxed", "shape": {
                "type": "text", "text": "Hi", "name": "boxed",
                "box": {"x": 10, "y": 20, "width": 100, "height": 60},
                "size": 30, "fit": True}},
            {"name": "rect", "shape": {
                "type": "rect", "x": 1, "y": 2, "width": 3, "height": 4}},
        ])
        self.assertEqual([item["kind"] for item in plan],
                         ["translate", "shape", "shape"])
        self.assertEqual(plan[0]["delta"], (30.0, -10.0))
        self.assertIsNone(plan[0]["spec"])
        self.assertEqual(plan[1]["layout"]["box"], [10.0, 20.0, 100.0, 60.0])
        self.assertNotIn("box", plan[1]["spec"])
        self.assertNotIn("name", plan[2]["spec"])
        self.assertEqual(plan[2]["spec"]["type"], "rect")
        self.assertIsNone(plan[2]["layout"])

    def test_both_shape_and_translate_refused(self):
        self.refused([{
            "name": "a",
            "shape": {"type": "rect", "x": 0, "y": 0, "width": 1,
                      "height": 1},
            "translate": [1, 2]}], "exactly one.*both")

    def test_neither_shape_nor_translate_refused(self):
        self.refused([{"name": "a"}], "exactly one.*neither")
        self.refused([{"name": "a", "shape": None, "translate": None}],
                     "exactly one.*neither")

    def test_duplicate_name_in_updates_refused(self):
        self.refused([{"name": "a", "translate": [0, 0]},
                      {"name": "a", "translate": [1, 1]}],
                     "already appears in updates\\[0\\]")

    def test_shape_name_must_match_update_name(self):
        self.refused([{"name": "a", "shape": {
            "type": "rect", "x": 0, "y": 0, "width": 1, "height": 1,
            "name": "b"}}], "shape.name is 'b'.*update name is 'a'")
        self.refused([{"name": "a", "shape": {
            "type": "rect", "x": 0, "y": 0, "width": 1, "height": 1,
            "name": None}}], "shape.name is None")

    def test_more_than_200_updates_refused(self):
        self.refused([{"name": "s{0}".format(i), "translate": [0, 0]}
                      for i in range(201)], "At most 200 updates")

    def test_update_list_must_be_non_empty(self):
        self.refused([], "non-empty array")
        self.refused(None, "non-empty array")

    def test_non_numeric_or_malformed_translate_refused(self):
        self.refused([{"name": "a", "translate": ["left", 2]}],
                     "translate\\[0\\] must be a number")
        self.refused([{"name": "a", "translate": [1]}],
                     "translate must be \\[dx, dy\\]")
        self.refused([{"name": "a", "translate": [1, 2, 3]}],
                     "translate must be \\[dx, dy\\]")
        self.refused([{"name": "a", "translate": [True, 2]}],
                     "translate\\[0\\] must be a number")

    def test_unknown_update_key_refused(self):
        self.refused([{"name": "a", "transalte": [1, 2]}],
                     "unknown key\\(s\\) transalte")

    def test_box_refusal_names_the_update_field(self):
        exc = self.refused([{"name": "a", "shape": {
            "type": "text", "text": "Hi", "x": 5,
            "box": {"x": 10, "y": 20, "width": 100, "height": 60}}}],
            "updates\\[0\\]\\.shape: with a box")
        self.assertIn("updates[0].shape", str(exc))

    def test_add_shape_errors_keep_the_shapes_prefix(self):
        # The shared _split_box helper must not change add_vector_shapes
        # messages when called without an explicit key.
        with self.assertRaises(OpError) as caught:
            vector_ops._split_box({"type": "rect", "x": 0, "y": 0,
                                   "width": 1, "height": 1, "box": {}}, 3)
        self.assertIn("shapes[3]", str(caught.exception))

    def test_spec_check_labels_the_failing_update(self):
        class _Doc(object):
            def width(self):
                return 1200

            def height(self):
                return 900

        good = {"type": "rect", "x": 0, "y": 0, "width": 10, "height": 10,
                "name": "a"}
        with self.assertRaises(OpError) as caught:
            vector_ops._check_specs(
                _Doc(), [(0, good), (1, {"type": "bogus"})], [], ["a", "b"],
                labels=lambda i: "updates[{0}].shape".format(i))
        self.assertRegex(str(caught.exception),
                         "updates\\[1\\]\\.shape: type: expected")
        with self.assertRaises(OpError) as caught:
            vector_ops._check_one_spec(_Doc(), {"type": "bogus"},
                                       "updates[3].shape")
        self.assertRegex(str(caught.exception),
                         "updates\\[3\\]\\.shape: type: expected")


class LayerNameTests(unittest.TestCase):
    def test_unique_names_are_mapped_to_shapes(self):
        layer = _FakeLayer(["a", "b"])
        found = vector_ops._layer_shapes_by_name(layer, ["b", "a"], "Shapes")
        self.assertEqual([shape.name() for shape in found.values()],
                         ["b", "a"])

    def test_missing_name_is_not_found_and_changes_nothing(self):
        with self.assertRaises(OpError) as caught:
            vector_ops._layer_shapes_by_name(
                _FakeLayer(["a"]), ["a", "nope"], "Shapes")
        self.assertEqual(caught.exception.kind, "not_found")
        self.assertRegex(str(caught.exception),
                         "No shape named 'nope'.*nothing was updated")

    def test_duplicate_name_on_layer_is_ambiguous(self):
        with self.assertRaises(OpError) as caught:
            vector_ops._layer_shapes_by_name(
                _FakeLayer(["a", "a"]), ["a"], "Shapes")
        self.assertEqual(caught.exception.kind, "ambiguous")
        self.assertRegex(str(caught.exception),
                         "more than once.*remove_vector_shapes")


class _FakeNode(object):
    """Just enough of libkis Node for the layer-swap logic."""

    _serial = [0]

    def __init__(self, name, kind="paintlayer"):
        _FakeNode._serial[0] += 1
        self._name = name
        self._kind = kind
        self._id = "node-{0}".format(_FakeNode._serial[0])
        self.parent = None
        self.children = []
        # "ok" | "noop" (returns False, stays) | "raise" (stays) |
        # "raise_after" (detaches, then raises)
        self.remove_mode = "ok"
        # "ok" | "false" (returns False, nothing attached) |
        # "lie" (returns True, nothing attached)
        self.attach_mode = "ok"

    def uniqueId(self):
        return self._id

    def name(self):
        return self._name

    def type(self):
        return self._kind

    def childNodes(self):
        return list(self.children)

    def parentNode(self):
        return self.parent

    def addChildNode(self, child, above):
        if self.attach_mode == "false":
            return False
        if self.attach_mode == "lie":
            return True
        index = 0 if above is None else self.children.index(above) + 1
        self.children.insert(index, child)
        child.parent = self
        return True

    def _detach(self):
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)
        self.parent = None

    def remove(self):
        if self.remove_mode == "noop":
            return False
        if self.remove_mode == "raise":
            raise RuntimeError("remove exploded")
        self._detach()
        if self.remove_mode == "raise_after":
            raise RuntimeError("remove exploded after detaching")
        return True


class _FakeRect(object):
    def __init__(self, element):
        self._e = element

    def x(self):
        return float(self._e.get("x", 0))

    def y(self):
        return float(self._e.get("y", 0))

    def width(self):
        return float(self._e.get("width", 0))

    def height(self):
        return float(self._e.get("height", 0))


class _VecShape(object):
    def __init__(self, element, layer):
        self.element = element
        self.layer = layer

    def name(self):
        return self.element.get("id")

    def setName(self, name):
        if not self.layer.sticky_names:
            self.element.set("id", name)

    def type(self):
        return "KoPathShape"

    def visible(self):
        return True

    def boundingBox(self):
        return _FakeRect(self.element)


_LAYER_PROPS = {"opacity": 255, "visible": True, "blendingMode": "normal",
                "inheritAlpha": False, "colorLabel": 0, "collapsed": False,
                "locked": False}


class _FakeVector(_FakeNode):
    """A vector layer: addShapesFromSvg keeps ids as names, toSvg uniquifies
    them the way Krita 5.3.4 does (measured: x, x1, x -> x, x1, x2)."""

    def __init__(self, name):
        _FakeNode.__init__(self, name, "vectorlayer")
        self.props = dict(_LAYER_PROPS)
        self.elements = []
        self.rename_on_import = {}
        self.sticky_names = False  # setName() is ignored

    def __getattr__(self, attr):
        if attr in _LAYER_PROPS:
            return lambda: self.props[attr]
        if attr.startswith("set") and attr[3:4].isupper():
            key = attr[3].lower() + attr[4:]
            if key in _LAYER_PROPS:
                def setter(value, key=key):
                    self.props[key] = value
                return setter
        raise AttributeError(attr)

    def layerStyleToAsl(self):
        return ""

    def shapes(self):
        return [_VecShape(e, self) for e in self.elements]

    def addShapesFromSvg(self, svg):
        root = svgshapes.parse_layer_svg(svg)
        made = []
        seen = set()
        for element in svgshapes.layer_shape_elements(root):
            element = svgshapes.ET.fromstring(
                svgshapes.ET.tostring(element, encoding="unicode"))
            old = element.get("id")
            new = self.rename_on_import.get(old, old)
            if new in seen:  # Krita renames a repeated id inside one import
                new = new + "_1681b89e"
            seen.add(new)
            element.set("id", new)
            self.elements.append(element)
            made.append(_VecShape(element, self))
        return made

    def toSvg(self):
        if not self.elements:
            return ""
        root = svgshapes.ET.fromstring(
            '<svg xmlns="http://www.w3.org/2000/svg" width="288pt" '
            'height="216pt" viewBox="0 0 288 216"/>')
        used = set()
        for element in self.elements:
            copy = svgshapes.ET.fromstring(
                svgshapes.ET.tostring(element, encoding="unicode"))
            base = copy.get("id")
            ident, n = base, 0
            while ident in used:
                n += 1
                ident = "{0}{1}".format(base, n)
            used.add(ident)
            copy.set("id", ident)
            root.append(copy)
        return svgshapes.ET.tostring(root, encoding="unicode")


class _FakeDoc(object):
    def __init__(self):
        self.root = _FakeNode("root", "grouplayer")
        self.active = None
        self.set_active_fails = False
        self.created = []

    def width(self):
        return 1200

    def height(self):
        return 900

    def xRes(self):
        return 72.0

    def yRes(self):
        return 72.0

    def resolution(self):
        return 72.0

    def rootNode(self):
        return self.root

    def createVectorLayer(self, name):
        layer = _FakeVector(name)
        self.created.append(layer)
        return layer

    def activeNode(self):
        return self.active

    def setActiveNode(self, node):
        if self.set_active_fails:
            raise RuntimeError("no view")
        self.active = node

    def add(self, node):
        self.root.addChildNode(node, self.root.children[-1]
                               if self.root.children else None)
        return node

    def vector(self, name, shapes):
        """A vector layer on top of the stack; ``shapes`` = [(id, x, width)].

        One import per shape, like a user adding them over time (a single
        import would rename repeated ids)."""
        layer = _FakeVector(name)
        for i, x, w in shapes:
            layer.addShapesFromSvg(
                '<svg xmlns="http://www.w3.org/2000/svg"><rect id="{0}" '
                'x="{1}" y="0" width="{2}" height="5"/></svg>'.format(i, x, w))
        return self.add(layer)


def _stack(shapes=(("a", 0, 10), ("b", 20, 10))):
    """Bottom / Letters (vector) / Top inside a fake document."""
    doc = _FakeDoc()
    bottom = doc.add(_FakeNode("Bottom"))
    layer = doc.vector("Letters", list(shapes))
    top = doc.add(_FakeNode("Top"))
    sys.modules["krita_mcp.ops"].document = doc
    return doc, bottom, layer, top


def _names(node):
    return [c.name() for c in node.children]


class RebuildLayerTests(unittest.TestCase):
    """F1/F10: the swap is all-or-nothing."""

    def rebuild(self, doc, layer):
        names, svg = vector_ops._layer_snapshot(layer)
        return vector_ops._rebuild_layer(doc, layer, svg, names)

    def assert_untouched(self, doc, bottom, layer, top):
        self.assertEqual(doc.root.children, [bottom, layer, top])
        self.assertEqual([s.name() for s in layer.shapes()], ["a", "b"])

    def test_swap_keeps_position_names_and_selection(self):
        doc, bottom, layer, top = _stack()
        layer.setOpacity(128)
        doc.active = layer
        new = self.rebuild(doc, layer)
        self.assertIsNot(new, layer)
        self.assertEqual(doc.root.children, [bottom, new, top])
        self.assertEqual([s.name() for s in new.shapes()], ["a", "b"])
        self.assertEqual(new.opacity(), 128)
        self.assertIs(doc.active, new)

    def test_attach_returning_false_keeps_the_original(self):
        doc, bottom, layer, top = _stack()
        doc.root.attach_mode = "false"
        with self.assertRaisesRegex(OpError, "did not attach"):
            self.rebuild(doc, layer)
        self.assert_untouched(doc, bottom, layer, top)

    def test_attach_reporting_true_without_attaching_keeps_the_original(self):
        doc, bottom, layer, top = _stack()
        doc.root.attach_mode = "lie"
        with self.assertRaisesRegex(OpError, "did not attach"):
            self.rebuild(doc, layer)
        self.assert_untouched(doc, bottom, layer, top)

    def test_duplicate_names_are_restored_after_the_import_renames_them(self):
        doc, bottom, layer, top = _stack(
            (("x", 0, 10), ("x", 20, 20), ("x1", 40, 30)))
        new = self.rebuild(doc, layer)
        self.assertEqual(doc.root.children, [bottom, new, top])
        self.assertEqual([s.name() for s in new.shapes()], ["x", "x", "x1"])

    def test_wrong_names_after_import_keep_the_original(self):
        doc, bottom, layer, top = _stack()
        real = doc.createVectorLayer

        def renaming(name):
            made = real(name)
            made.rename_on_import = {"b": "b1"}
            made.sticky_names = True
            return made
        doc.createVectorLayer = renaming
        with self.assertRaisesRegex(OpError, "names .*instead of"):
            self.rebuild(doc, layer)
        self.assert_untouched(doc, bottom, layer, top)
        self.assertEqual(doc.root.children.count(doc.created[0]), 0)

    def test_original_refusing_removal_discards_the_copy(self):
        doc, bottom, layer, top = _stack()
        layer.remove_mode = "noop"
        with self.assertRaisesRegex(OpError, "could not remove the original"):
            self.rebuild(doc, layer)
        self.assert_untouched(doc, bottom, layer, top)

    def test_original_removal_raising_while_still_attached(self):
        doc, bottom, layer, top = _stack()
        layer.remove_mode = "raise"
        with self.assertRaisesRegex(OpError, "could not remove the original"):
            self.rebuild(doc, layer)
        self.assert_untouched(doc, bottom, layer, top)

    def test_original_removal_raising_after_detaching_is_success(self):
        doc, bottom, layer, top = _stack()
        layer.remove_mode = "raise_after"
        new = self.rebuild(doc, layer)
        self.assertEqual(doc.root.children, [bottom, new, top])

    def test_uncollectable_copy_is_reported_as_two_layers(self):
        doc, bottom, layer, top = _stack()
        real = doc.createVectorLayer

        def stubborn(name):
            made = real(name)
            made.rename_on_import = {"b": "b1"}
            made.sticky_names = True
            made.remove_mode = "noop"
            return made
        doc.createVectorLayer = stubborn
        with self.assertRaisesRegex(OpError, "two layers") as caught:
            self.rebuild(doc, layer)
        self.assertEqual(caught.exception.kind, "internal")
        self.assertIn(layer, doc.root.children)

    def test_selection_failure_does_not_undo_an_applied_edit(self):
        doc, bottom, layer, top = _stack()
        doc.active = layer
        doc.set_active_fails = True
        new = self.rebuild(doc, layer)
        self.assertEqual(doc.root.children, [bottom, new, top])

    def test_rebuild_refuses_a_layer_with_masks(self):
        doc, bottom, layer, top = _stack()
        mask = _FakeNode("Soft mask", "transparencymask")
        layer.addChildNode(mask, None)
        names, svg = vector_ops._layer_snapshot(layer)
        with self.assertRaisesRegex(OpError, "Soft mask.*transparencymask"):
            vector_ops._rebuild_layer(doc, layer, svg, names)
        self.assertEqual(doc.root.children, [bottom, layer, top])
        self.assertEqual(layer.children, [mask])
        self.assertEqual(doc.created, [])

    def test_scratch_removal_failure_is_reported(self):
        self.assertFalse(vector_ops._remove_layer(_stuck("noop"), "scratch"))
        self.assertFalse(vector_ops._remove_layer(_stuck("raise"), "scratch"))
        self.assertTrue(vector_ops._remove_layer(_stuck("ok"), "scratch"))


def _stuck(mode):
    node = _FakeNode("mcp-scratch", "vectorlayer")
    node.remove_mode = mode
    return node


class PositionalUpdateTests(unittest.TestCase):
    """F11: shapes are addressed by position, never by exported SVG id."""

    def widths(self, layer):
        return [s.element.get("width") for s in layer.shapes()]

    def test_export_uniquifies_but_names_are_restored_by_position(self):
        doc, _bottom, layer, _top = _stack(
            (("x", 0, 10), ("x", 20, 20), ("x1", 40, 30)))
        self.assertEqual(svgshapes.layer_shape_ids(layer.toSvg()),
                         ["x", "x1", "x11"])
        names, svg = vector_ops._layer_snapshot(layer)
        self.assertEqual(names, ["x", "x", "x1"])
        self.assertEqual(svgshapes.layer_shape_ids(svg), ["x", "x", "x1"])

    def test_update_of_the_third_shape_touches_the_third_shape(self):
        doc, bottom, layer, top = _stack(
            (("x", 0, 10), ("x", 20, 20), ("x1", 40, 30)))
        result = vector_ops.op_update_vector_shapes({
            "layer": "Letters",
            "updates": [{"name": "x1", "shape": {
                "type": "rect", "x": 40, "y": 0, "width": 77,
                "height": 5}}]})
        new = doc.root.children[1]
        self.assertIsNot(new, layer)
        self.assertEqual(doc.root.children, [bottom, new, top])
        self.assertEqual([s.name() for s in new.shapes()], ["x", "x", "x1"])
        self.assertEqual(self.widths(new), ["10", "20", "77"])
        self.assertTrue(result["layer_rebuilt"])
        self.assertEqual([u["name"] for u in result["updated"]], ["x1"])
        self.assertEqual([c for c in doc.root.children
                          if c.name() == "mcp-scratch"], [])

    def test_duplicate_name_is_still_ambiguous(self):
        doc, bottom, layer, top = _stack(
            (("x", 0, 10), ("x", 20, 20), ("x1", 40, 30)))
        with self.assertRaises(OpError) as caught:
            vector_ops.op_update_vector_shapes({
                "layer": "Letters",
                "updates": [{"name": "x", "shape": {
                    "type": "rect", "x": 0, "y": 0, "width": 9,
                    "height": 5}}]})
        self.assertEqual(caught.exception.kind, "ambiguous")
        self.assertEqual(doc.root.children, [bottom, layer, top])

    def test_remove_by_position_keeps_the_remaining_names(self):
        doc, bottom, layer, top = _stack(
            (("x", 0, 10), ("x", 20, 20), ("x1", 40, 30)))
        result = vector_ops.op_remove_vector_shapes({
            "layer": "Letters", "names": ["x1"]})
        new = doc.root.children[1]
        self.assertEqual(result["removed"], 1)
        self.assertEqual([s.name() for s in new.shapes()], ["x", "x"])
        self.assertEqual(self.widths(new), ["10", "20"])

    def test_krita_export_with_a_different_shape_count_is_refused(self):
        doc, bottom, layer, top = _stack()
        one_shape = svgshapes.ET.tostring(
            svgshapes.ET.fromstring(layer.toSvg())[0], encoding="unicode")
        layer.toSvg = lambda: (
            '<svg xmlns="http://www.w3.org/2000/svg">{0}</svg>'.format(
                one_shape))
        with self.assertRaisesRegex(OpError, "exports 1 top-level shapes"):
            vector_ops._layer_snapshot(layer)
        with self.assertRaisesRegex(OpError, "exports 1 top-level shapes"):
            vector_ops.op_remove_vector_shapes({
                "layer": "Letters", "names": ["a"]})
        self.assertEqual(doc.root.children, [bottom, layer, top])


class AddRollbackTests(unittest.TestCase):
    """F7: roll back by rebuilding only when the user's layer was touched."""

    def setUp(self):
        self.rebuilds = []
        self.real_rebuild = vector_ops._rebuild_layer
        self.real_place = vector_ops._place_in_box

        def spy(doc, layer, svg, names):
            self.rebuilds.append(list(names))
            return self.real_rebuild(doc, layer, svg, names)

        def failing_place(*_args, **_kwargs):
            raise OpError("trial exploded")
        vector_ops._rebuild_layer = spy
        vector_ops._place_in_box = failing_place

    def tearDown(self):
        vector_ops._rebuild_layer = self.real_rebuild
        vector_ops._place_in_box = self.real_place

    BOXED = {"type": "text", "text": "Hi", "name": "hello",
             "box": {"x": 0, "y": 0, "width": 100, "height": 50}}
    PLAIN = {"type": "rect", "x": 0, "y": 0, "width": 9, "height": 5,
             "name": "plain"}

    def test_untouched_layer_is_not_rebuilt_on_a_trial_failure(self):
        doc, bottom, layer, top = _stack()
        with self.assertRaisesRegex(OpError, "trial exploded"):
            vector_ops.op_add_vector_shapes({
                "layer": "Letters", "shapes": [self.BOXED]})
        self.assertEqual(self.rebuilds, [])
        self.assertEqual(doc.root.children, [bottom, layer, top])
        self.assertEqual([s.name() for s in layer.shapes()], ["a", "b"])
        self.assertEqual([n for n in _names(doc.root)
                          if n == "mcp-scratch"], [])

    def test_touched_layer_is_rebuilt_from_its_snapshot(self):
        doc, bottom, layer, top = _stack()
        with self.assertRaisesRegex(OpError, "trial exploded"):
            vector_ops.op_add_vector_shapes({
                "layer": "Letters", "shapes": [self.PLAIN, self.BOXED]})
        self.assertEqual(self.rebuilds, [["a", "b"]])
        current = doc.root.children[1]
        self.assertEqual(_names(doc.root), ["Bottom", "Letters", "Top"])
        self.assertEqual([s.name() for s in current.shapes()], ["a", "b"])

    def test_masked_layer_refuses_a_boxed_add_before_any_change(self):
        doc, bottom, layer, top = _stack()
        layer.addChildNode(_FakeNode("Soft mask", "transparencymask"), None)
        with self.assertRaisesRegex(OpError, "Soft mask"):
            vector_ops.op_add_vector_shapes({
                "layer": "Letters", "shapes": [self.PLAIN, self.BOXED]})
        self.assertEqual(doc.created, [])
        self.assertEqual([s.name() for s in layer.shapes()], ["a", "b"])


class MaskAndLockTests(unittest.TestCase):
    """F9/F15: refuse before anything changes."""

    def test_update_with_replacement_refused_on_a_masked_layer(self):
        doc, bottom, layer, top = _stack()
        mask = _FakeNode("Soft mask", "transparencymask")
        layer.addChildNode(mask, None)
        with self.assertRaisesRegex(OpError, "Soft mask.*transparencymask"):
            vector_ops.op_update_vector_shapes({
                "layer": "Letters",
                "updates": [{"name": "a", "shape": {
                    "type": "rect", "x": 0, "y": 0, "width": 9,
                    "height": 5}}]})
        self.assertEqual(doc.root.children, [bottom, layer, top])
        self.assertEqual(layer.children, [mask])
        self.assertEqual(doc.created, [])

    def test_remove_refused_on_a_masked_layer(self):
        doc, bottom, layer, top = _stack()
        layer.addChildNode(_FakeNode("Soft mask", "transparencymask"), None)
        with self.assertRaisesRegex(OpError, "Soft mask"):
            vector_ops.op_remove_vector_shapes({
                "layer": "Letters", "names": ["a"]})
        self.assertEqual(doc.root.children, [bottom, layer, top])
        self.assertEqual([s.name() for s in layer.shapes()], ["a", "b"])

    def test_remove_refused_on_a_locked_layer(self):
        doc, bottom, layer, top = _stack()
        layer.setLocked(True)
        with self.assertRaisesRegex(OpError, "locked"):
            vector_ops.op_remove_vector_shapes({
                "layer": "Letters", "names": ["a"]})
        self.assertEqual(doc.root.children, [bottom, layer, top])
        self.assertEqual(doc.created, [])


class BoxFinitenessTests(unittest.TestCase):
    """F8: NaN/inf must be refused while still pure validation."""

    BOX = {"x": 10, "y": 20, "width": 100, "height": 60}

    def spec(self, **box):
        return {"type": "text", "text": "Hi",
                "box": dict(self.BOX, **box)}

    def refused(self, spec, field):
        with self.assertRaisesRegex(OpError, field + ".*finite"):
            vector_ops._split_box(spec, 0)

    def test_non_finite_box_fields_refused(self):
        for field in ("x", "y", "width", "height"):
            for bad in (float("nan"), float("inf"), float("-inf"), 1e999,
                        10 ** 400):
                with self.subTest(field=field, bad=bad):
                    self.refused(self.spec(**{field: bad}), field)

    def test_non_finite_size_and_min_size_refused(self):
        for bad in (float("nan"), float("inf")):
            self.refused(dict(self.spec(), size=bad), "size")
            self.refused(dict(self.spec(), min_size=bad), "min_size")

    def test_finite_box_still_parses(self):
        _spec, layout = vector_ops._split_box(self.spec(), 0)
        self.assertTrue(all(math.isfinite(v) for v in layout["box"]))


if __name__ == "__main__":
    unittest.main()
