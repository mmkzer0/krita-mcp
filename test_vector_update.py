#!/usr/bin/env python3
"""Offline tests for update_vector_shapes planning and layer-SVG editing.

The Krita-facing half (box fit, rebuild, translate) needs a running Krita and
is exercised by tmp/probe-vector-update-2026-11-18.py. Everything here is
Krita-free: vector_ops is loaded with a fake ``krita_mcp.compat`` and
``krita_mcp.ops`` so the pure planning functions run with plain dicts.
"""

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
