#!/usr/bin/env python3
"""Offline consumer-contract tests for the pure SVG builder."""

import importlib.util
import math
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location(
    "svgshapes", Path(__file__).resolve().parent / "pykrita" / "krita_mcp" / "svgshapes.py")
svgshapes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(svgshapes)
build_svg = svgshapes.build_svg
parse_color = svgshapes.parse_color
ShapeSpecError = svgshapes.ShapeSpecError
NS = "{http://www.w3.org/2000/svg}"


class SvgShapesTests(unittest.TestCase):
    def build(self, shapes, **kwargs):
        svg, names = build_svg(shapes, 800, 600, **kwargs)
        return ET.fromstring(svg), names

    def assert_bad(self, shape, field):
        with self.assertRaisesRegex(ShapeSpecError, "shape 0:.*" + field):
            build_svg([shape], 800, 600)

    def test_root_empty_and_names(self):
        root, names = self.build([])
        self.assertEqual(root.tag, NS + "svg")
        self.assertEqual(root.attrib, {"width": "800px", "height": "600px", "viewBox": "0 0 800 600"})
        self.assertEqual(names, [])
        shapes = [{"type": "path", "d": "M0 0Z", "name": "Hello world/<&>"},
                  {"type": "path", "d": "M1 1Z"}]
        root, names = self.build(shapes, id_prefix="my shapes")
        self.assertEqual(names, ["Hello_world____", "my_shapes-1"])
        self.assertEqual([e.attrib["id"] for e in root], names)
        self.assertEqual(shapes[0]["name"], "Hello world/<&>")
        self.assert_bad({"type": "path", "d": "M0 0", "name": ""}, "name")
        self.assert_bad({"type": "path", "d": "M0 0", "name": 2}, "name")
        for first, second in [("a b", "a_b"), ("mcp-1", None)]:
            shapes = [{"type": "path", "d": "M0 0", "name": first}, {"type": "path", "d": "M0 0"}]
            if second is not None:
                shapes[1]["name"] = second
            with self.assertRaisesRegex(ShapeSpecError, "shape 1: name: duplicate"):
                build_svg(shapes, 800, 600)

    def test_text_styles_and_escaping(self):
        text = 'Hello <&>"\'\nsecond line'
        root, _ = self.build([{"type": "text", "text": text, "x": 200, "y": 80,
                               "width": 220, "align": "center", "font": "Arial Black, Arial, sans-serif",
                               "size": 28, "color": [10, 20, 30, 128], "stroke": "white", "stroke_width": 2,
                               "bold": True, "italic": True, "vertical": True, "line_height": 1.2,
                               "letter_spacing": -0.5, "rotation": -12}])
        node = root[0]
        self.assertEqual(node.tag, NS + "text")
        self.assertEqual(node.text, text)
        self.assertEqual(node.get("x"), "200")
        self.assertEqual(node.get("y"), "80")
        self.assertEqual(node.get("fill"), "#0a141e")
        self.assertEqual(node.get("fill-opacity"), "0.502")
        self.assertEqual(node.get("stroke"), "#ffffff")
        self.assertEqual(node.get("stroke-width"), "2")
        self.assertEqual(node.get("transform"), "rotate(-12 200 80)")
        style = node.get("style")
        for item in ["text-anchor: middle", "font-family: 'Arial Black', Arial, sans-serif",
                     "font-size: 28px", "inline-size: 220px", "font-weight: bold", "font-style: italic",
                     "writing-mode: vertical-rl", "line-height: 1.2", "letter-spacing: -0.5px",
                     "white-space: pre-wrap", "paint-order: stroke"]:
            self.assertIn(item, style)

    def test_text_defaults_and_alignments(self):
        for align, anchor in [("left", "start"), ("center", "middle"), ("right", "end")]:
            root, _ = self.build([{"type": "text", "text": "hello", "x": 1, "y": 2, "align": align}])
            self.assertIn("text-anchor: " + anchor, root[0].get("style"))
            self.assertIn("font-size: 24px", root[0].get("style"))
            self.assertIn("font-family: sans-serif", root[0].get("style"))
            self.assertNotIn("white-space", root[0].get("style"))
            self.assertEqual(root[0].get("fill"), "#000000")
        for field, value in [("text", 2), ("align", []), ("align", "justify"), ("size", 0),
                             ("width", 0), ("line_height", 0), ("bold", 1), ("italic", "yes"),
                             ("vertical", None), ("text", "a" * 10001), ("text", "a\x00b")]:
            shape = {"type": "text", "text": "hi", "x": 0, "y": 0, field: value}
            self.assert_bad(shape, field)
        self.build([{"type": "text", "text": "a" * 10000, "x": 0, "y": 0}])

    def test_font_quoting_and_injection_rejection(self):
        root, _ = self.build([{"type": "text", "text": "hi", "x": 0, "y": 0,
                               "font": "'Arial Black', O'Brien"}])
        self.assertIn("font-family: 'Arial Black', 'O\\27 Brien'", root[0].get("style"))
        for font in ['Arial; fill:red', 'Arial"', 'Arial<', 'Arial>', 'Arial{', 'Arial}', 'Arial\\', '', 'Arial,,serif', None]:
            self.assert_bad({"type": "text", "text": "hi", "x": 0, "y": 0, "font": font}, "font")

    def test_colors(self):
        cases = [("#aBc", ("#aabbcc", 1.0)), ("#AbCdEf", ("#abcdef", 1.0)),
                 ("#aabbcc80", ("#aabbcc", 128 / 255)), ("#aabbcc00", ("#aabbcc", 0.0)),
                 ("transparent", ("#000000", 0.0)), ("none", ("none", 1.0)),
                 ([1, 2, 3], ("#010203", 1.0)), ([255, 0, 127, 64], ("#ff007f", 64 / 255))]
        names = dict(zip("black silver gray white maroon red purple fuchsia green lime olive yellow navy blue teal aqua".split(),
                         "000000 c0c0c0 808080 ffffff 800000 ff0000 800080 ff00ff 008000 00ff00 808000 ffff00 000080 0000ff 008080 00ffff".split()))
        for name, rgb in names.items():
            cases.append((name.upper(), ("#" + rgb, 1.0)))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_color(value), expected)
        for value in [None, 123, "url(x)", "#12", "#12345", "#gggggg", [1, 2], [1, 2, 256],
                      [-1, 2, 3], [True, 2, 3], [1.0, 2, 3], [1, 2, 3, 4, 5]]:
            with self.subTest(value=value), self.assertRaises(ShapeSpecError):
                parse_color(value)

    def test_geometric_shapes_and_opacity(self):
        root, _ = self.build([
            {"type": "ellipse", "cx": 10, "cy": 20, "rx": 5, "ry": 8, "rotation": 45},
            {"type": "rect", "x": 10, "y": 20, "width": 40, "height": 60, "radius": 5,
             "rotation": 90, "fill": "none", "stroke": "#abcdef80", "stroke_width": 1.25},
            {"type": "path", "d": "M0 0 L1e2,-2.5 C1 2 3 4 5 6z", "fill": "transparent"},
            {"type": "polygon", "points": [[0, 1], [2.1256, 3], [-4, 5]]}])
        self.assertEqual([e.tag for e in root], [NS + k for k in ("ellipse", "rect", "path", "polygon")])
        self.assertEqual(root[0].get("transform"), "rotate(45 10 20)")
        self.assertEqual(root[0].get("rx"), "5")
        self.assertEqual(root[0].get("ry"), "8")
        self.assertEqual(root[0].get("fill"), "#000000")
        self.assertEqual(root[0].get("stroke"), "none")
        self.assertEqual(root[1].get("transform"), "rotate(90 30 50)")
        self.assertEqual(root[1].get("rx"), "5")
        self.assertEqual(root[1].get("ry"), "5")
        self.assertEqual(root[1].get("stroke-opacity"), "0.502")
        self.assertEqual(root[1].get("fill"), "none")
        self.assertEqual(root[2].get("fill-opacity"), "0")
        self.assertEqual(root[3].get("points"), "0,1 2.126,3 -4,5")

    def test_path_character_filter(self):
        for d in ['M0 0<script>', 'M0 0;stroke:red', 'M0 0"', 'M0 0 R1 2', '', '   ', None, 'M0\x00 1']:
            self.assert_bad({"type": "path", "d": d}, "d")

    def test_validation_and_limits(self):
        for value in [True, False, float("nan"), float("inf"), -float("inf"), "1", None, 10 ** 1000]:
            self.assert_bad({"type": "rect", "x": value, "y": 0, "width": 10, "height": 20}, "x")
        for field in ["width", "height"]:
            for value in [0, -1, True, float("nan")]:
                with self.assertRaisesRegex(ShapeSpecError, field):
                    build_svg([], **{"width": 800, "height": 600, field: value})
        for shape, field in [({"type": "ellipse", "cx": 0, "cy": 0, "rx": 0, "ry": 1}, "rx"),
                             ({"type": "rect", "x": 0, "y": 0, "width": 1, "height": 1, "radius": -1}, "radius"),
                             ({"type": "polygon", "points": [[0, 0], [1, 1]]}, "points"),
                             ({"type": "polygon", "points": [[0, 0], [1, 1], [2, True]]}, "points"),
                             ({"type": "path", "d": "M0 0", "stroke_width": -1}, "stroke_width"),
                             ({"type": "path", "d": "M0 0", "fill": "bad"}, "fill"),
                             ({"type": "rect", "y": 0, "width": 1, "height": 1}, "x"),
                             ({"type": []}, "type"), ({}, "type"), (None, "spec")]:
            self.assert_bad(shape, field)
        with self.assertRaisesRegex(ShapeSpecError, "shape 0: unknown keys.*allowed keys:.*d.*fill.*name.*stroke.*type"):
            build_svg([{"type": "path", "d": "M0 0", "surprise": 1}], 1, 1)
        self.assert_bad({"type": "path", "d": "M0 0", "rotation": 10}, "allowed keys")
        shape = {"type": "path", "d": "M0 0"}
        self.assertEqual(len(self.build([shape] * 500)[1]), 500)
        for shapes in [[shape] * 501, {}, None]:
            with self.assertRaisesRegex(ShapeSpecError, "shapes"):
                build_svg(shapes, 1, 1)

    def test_numeric_serialization(self):
        root, _ = self.build([{"type": "rect", "x": -0.00001, "y": 1.234567, "width": 1e8, "height": 1e-3}])
        self.assertEqual(root[0].get("x"), "0")
        self.assertEqual(root[0].get("y"), "1.235")
        self.assertEqual(root[0].get("width"), "100000000")
        self.assertEqual(root[0].get("height"), "0.001")

    def test_bubble_contour_geometry(self):
        # Pythagorean base points make the serialized ellipse invariant exact.
        for tail in [[600, 120], [-300, 120], [210, 500], [210, -300]]:
            shape = {"type": "bubble", "x": 10, "y": 20, "width": 400, "height": 200,
                     "tail": tail, "tail_width": 150 if tail[1] == 120 else 300}
            root, _ = self.build([shape])
            node = root[0]
            self.assertEqual(node.tag, NS + "path")
            self.assertEqual(node.get("fill"), "#ffffff")
            self.assertEqual(node.get("stroke"), "#000000")
            self.assertEqual(node.get("stroke-width"), "3")
            tokens = node.get("d").split()
            self.assertEqual([tokens[i] for i in [0, 3, 11, 14]], ["M", "A", "L", "Z"])
            self.assertEqual(tokens[4:9], ["200", "100", "0", "1", "1"])
            start = tuple(map(float, tokens[1:3]))
            end = tuple(map(float, tokens[9:11]))
            self.assertEqual(tuple(map(float, tokens[12:14])), tuple(tail))
            for px, py in (start, end):
                self.assertAlmostEqual(((px - 210) / 200) ** 2 + ((py - 120) / 100) ** 2, 1, delta=1e-6)
            a = math.atan2((start[1] - 120) / 100, (start[0] - 210) / 200)
            b = math.atan2((end[1] - 120) / 100, (end[0] - 210) / 200)
            self.assertGreater((b - a) % (2 * math.pi), math.pi)
        root, _ = self.build([{"type": "bubble", "x": 0, "y": 0, "width": 400, "height": 200}])
        self.assertEqual(root[0].get("d"), "M 0 100 A 200 100 0 1 1 400 100 A 200 100 0 1 1 0 100 Z")
        for angle in [0.3, 1.1, 2.6, -2.2]:
            tail = [5000 + 8000 * math.cos(angle), 2500 + 8000 * math.sin(angle)]
            root, _ = self.build([{"type": "bubble", "x": 0, "y": 0, "width": 10000,
                                   "height": 5000, "tail": tail}])
            tokens = root[0].get("d").split()
            for offset in [1, 9]:
                px, py = map(float, tokens[offset:offset + 2])
                self.assertAlmostEqual(((px - 5000) / 5000) ** 2 + ((py - 2500) / 2500) ** 2, 1, delta=1e-6)
            self.assertEqual(tokens[7:9], ["1", "1"])
        for field, value in [("tail", [200, 100]), ("tail", [400, 100]), ("tail", [0]),
                             ("tail", [True, 200]), ("tail_width", 0), ("width", 0)]:
            self.assert_bad({"type": "bubble", "x": 0, "y": 0, "width": 400, "height": 200, field: value}, field)


class LayerPositionTests(unittest.TestCase):
    """Positional layer-SVG edits (Krita uniquifies exported ids)."""

    SVG = ('<svg xmlns="http://www.w3.org/2000/svg"><defs/>'
           '<rect id="x"/><rect id="x1"/><rect id="x11"/></svg>')

    def test_set_root_shape_ids_restores_duplicate_names_by_position(self):
        root = svgshapes.parse_layer_svg(self.SVG)
        svgshapes.set_root_shape_ids(root, ["x", "x", "x1"])
        self.assertEqual(svgshapes.layer_shape_ids(
            svgshapes.serialize_layer_svg(root)), ["x", "x", "x1"])
        with self.assertRaisesRegex(svgshapes.LayerSvgError, "3 top-level shapes but 2"):
            svgshapes.set_root_shape_ids(root, ["x", "y"])

    def test_replace_root_shape_at_targets_the_position_not_the_id(self):
        root = svgshapes.parse_layer_svg(
            '<svg xmlns="http://www.w3.org/2000/svg"><defs/><rect id="x"/>'
            '<rect id="x"/><rect id="x1"/></svg>')
        new = ET.fromstring('<rect xmlns="http://www.w3.org/2000/svg" id="x1" width="77"/>')
        svgshapes.replace_root_shape_at(root, 2, [new])
        shapes = svgshapes.layer_shape_elements(root)
        self.assertEqual([e.get("id") for e in shapes], ["x", "x", "x1"])
        self.assertEqual([e.get("width") for e in shapes], [None, None, "77"])
        self.assertTrue(svgshapes._local_name(root[0].tag) == "defs")
        for bad in (3, -1, True):
            with self.assertRaisesRegex(svgshapes.LayerSvgError, "out of range"):
                svgshapes.replace_root_shape_at(root, bad, [new])


if __name__ == "__main__":
    unittest.main()
