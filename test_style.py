#!/usr/bin/env python3
"""Offline contract tests for layer styles (no Krita, no Qt).

Loads ``pykrita/krita_mcp/style_ops.py`` by path and exercises the pure ASL
builder, the request validation and the ASL parser. The ASL fixtures are
layerStyleToAsl() output captured from Krita 5.3.4.

    python3 test_style.py
"""

import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


SPEC = importlib.util.spec_from_file_location(
    "style_ops",
    Path(__file__).resolve().parent / "pykrita" / "krita_mcp" / "style_ops.py")
style = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(style)

# Stroke 6px white outside + shadow (colour 10,20,30, 50 %, -45 deg, 12 px,
# size 3, spread 20), as read back by Krita.
BOTH = """<asl>
 <node type="Descriptor" name="" classId="null">
  <node type="UnitFloat" key="Scl " value="100" unit="#Prc"/>
  <node type="Boolean" key="masterFXSwitch" value="1"/>
  <node type="Descriptor" key="DrSh" name="" classId="DrSh">
   <node type="Boolean" key="enab" value="1"/>
   <node typeId="BlnM" type="Enum" key="Md  " value="Mltp"/>
   <node type="Descriptor" key="Clr " name="" classId="RGBC">
    <node type="Double" key="Rd  " value="10.0000003539026"/>
    <node type="Double" key="Grn " value="20.0000007078052"/>
    <node type="Double" key="Bl  " value="30.0000001117587"/>
   </node>
   <node type="UnitFloat" key="Opct" value="50" unit="#Prc"/>
   <node type="Boolean" key="uglg" value="0"/>
   <node type="UnitFloat" key="lagl" value="-45" unit="#Ang"/>
   <node type="UnitFloat" key="Dstn" value="12" unit="#Pxl"/>
   <node type="UnitFloat" key="Ckmt" value="20" unit="#Pxl"/>
   <node type="UnitFloat" key="blur" value="3" unit="#Pxl"/>
   <node type="UnitFloat" key="Nose" value="0" unit="#Prc"/>
   <node type="Boolean" key="AntA" value="0"/>
   <node type="Descriptor" key="TrnS" name="" classId="ShpC">
    <node type="Text" key="Nm  " value="Linear"/>
    <node type="List" key="Crv ">
     <node type="Descriptor" name="" classId="CrPt">
      <node type="Double" key="Hrzn" value="0"/>
      <node type="Double" key="Vrtc" value="0"/>
     </node>
     <node type="Descriptor" name="" classId="CrPt">
      <node type="Double" key="Hrzn" value="255"/>
      <node type="Double" key="Vrtc" value="255"/>
     </node>
    </node>
   </node>
   <node type="Boolean" key="layerConceals" value="1"/>
  </node>
  <node type="Descriptor" key="FrFX" name="" classId="FrFX">
   <node type="Boolean" key="enab" value="1"/>
   <node typeId="FStl" type="Enum" key="Styl" value="OutF"/>
   <node typeId="FrFl" type="Enum" key="PntT" value="SClr"/>
   <node typeId="BlnM" type="Enum" key="Md  " value="Nrml"/>
   <node type="UnitFloat" key="Opct" value="100" unit="#Prc"/>
   <node type="UnitFloat" key="Sz  " value="6" unit="#Pxl"/>
   <node type="Descriptor" key="Clr " name="" classId="RGBC">
    <node type="Double" key="Rd  " value="255"/>
    <node type="Double" key="Grn " value="255"/>
    <node type="Double" key="Bl  " value="255"/>
   </node>
  </node>
 </node>
 <node type="Descriptor" name="" classId="Styl">
  <node type="Descriptor" key="documentMode" name="" classId="documentMode"/>
 </node>
</asl>
"""

# Drop shadow (Krita defaults, global light on), outer glow and colour overlay
# enabled; inner shadow and stroke disabled (Krita omits those). Curves cut.
OTHER = """<asl>
 <node type="Descriptor" name="" classId="null">
  <node type="UnitFloat" key="Scl " value="100" unit="#Prc" />
  <node type="Boolean" key="masterFXSwitch" value="1" />
  <node type="Descriptor" key="DrSh" name="" classId="DrSh">
   <node type="Boolean" key="enab" value="1" />
   <node typeId="BlnM" type="Enum" key="Md  " value="Mltp" />
   <node type="Descriptor" key="Clr " name="" classId="RGBC">
    <node type="Double" key="Rd  " value="0" />
    <node type="Double" key="Grn " value="0" />
    <node type="Double" key="Bl  " value="0" />
   </node>
   <node type="UnitFloat" key="Opct" value="75" unit="#Prc" />
   <node type="Boolean" key="uglg" value="1" />
   <node type="UnitFloat" key="lagl" value="120" unit="#Ang" />
   <node type="UnitFloat" key="Dstn" value="21" unit="#Pxl" />
   <node type="UnitFloat" key="Ckmt" value="0" unit="#Pxl" />
   <node type="UnitFloat" key="blur" value="21" unit="#Pxl" />
   <node type="UnitFloat" key="Nose" value="0" unit="#Prc" />
   <node type="Boolean" key="AntA" value="0" />
   <node type="Boolean" key="layerConceals" value="0" />
  </node>
  <node type="Descriptor" key="OrGl" name="" classId="OrGl">
   <node type="Boolean" key="enab" value="1" />
   <node typeId="BlnM" type="Enum" key="Md  " value="linearDodge" />
   <node type="Descriptor" key="Clr " name="" classId="RGBC">
    <node type="Double" key="Rd  " value="255" />
    <node type="Double" key="Grn " value="255" />
    <node type="Double" key="Bl  " value="255" />
   </node>
   <node type="UnitFloat" key="Opct" value="75" unit="#Prc" />
   <node typeId="BETE" type="Enum" key="GlwT" value="SfBL" />
   <node type="UnitFloat" key="Ckmt" value="0" unit="#Pxl" />
   <node type="UnitFloat" key="blur" value="21" unit="#Pxl" />
   <node type="UnitFloat" key="Nose" value="0" unit="#Prc" />
   <node type="UnitFloat" key="ShdN" value="0" unit="#Prc" />
   <node type="Boolean" key="AntA" value="0" />
   <node type="UnitFloat" key="Inpr" value="100" unit="#Prc" />
  </node>
  <node type="Descriptor" key="SoFi" name="" classId="SoFi">
   <node type="Boolean" key="enab" value="1" />
   <node typeId="BlnM" type="Enum" key="Md  " value="Mltp" />
   <node type="UnitFloat" key="Opct" value="75" unit="#Prc" />
   <node type="Descriptor" key="Clr " name="" classId="RGBC">
    <node type="Double" key="Rd  " value="255" />
    <node type="Double" key="Grn " value="255" />
    <node type="Double" key="Bl  " value="255" />
   </node>
  </node>
 </node>
 <node type="Descriptor" name="" classId="Styl">
  <node type="Descriptor" key="documentMode" name="" classId="documentMode" />
 </node>
</asl>
"""

# A style switched off with no effects: what `clear` leaves behind.
OFF = """<asl>
 <node type="Descriptor" name="" classId="null">
  <node type="UnitFloat" key="Scl " value="100" unit="#Prc"/>
  <node type="Boolean" key="masterFXSwitch" value="0"/>
 </node>
 <node type="Descriptor" name="" classId="Styl">
  <node type="Descriptor" key="documentMode" name="" classId="documentMode"/>
 </node>
</asl>
"""


def effect(asl, key):
    root = ET.fromstring(asl)
    found = [n for n in root.iter("node") if n.get("key") == key
             and n.get("type") == "Descriptor"]
    return found[0] if found else None


def field(desc, key):
    for child in desc.findall("node"):
        if child.get("key") == key:
            return child
    raise AssertionError("no {0} in {1}".format(key, desc.get("key")))


def rgb(desc):
    clr = field(desc, "Clr ")
    return tuple(float(field(clr, k).get("value"))
                 for k in ("Rd  ", "Grn ", "Bl  "))


class BuilderTests(unittest.TestCase):
    def test_stroke_structure_and_numbers(self):
        asl = style.build_style_asl(
            stroke={"size": 6, "color": "#ff8000", "position": "inside",
                    "opacity": 0.8})
        root = ET.fromstring(asl)
        top = root.find("node")
        self.assertEqual(field(top, "masterFXSwitch").get("value"), "1")
        self.assertEqual(field(top, "Scl ").get("value"), "100")
        self.assertIsNone(effect(asl, "DrSh"))
        stroke = effect(asl, "FrFX")
        self.assertEqual(field(stroke, "enab").get("value"), "1")
        self.assertEqual(field(stroke, "Styl").get("value"), "InsF")
        self.assertEqual(field(stroke, "Styl").get("typeId"), "FStl")
        self.assertEqual(field(stroke, "PntT").get("value"), "SClr")
        # Krita's reader defaults the blend mode to Multiply: say Normal.
        self.assertEqual(field(stroke, "Md  ").get("value"), "Nrml")
        self.assertEqual(field(stroke, "Opct").get("value"), "80")
        self.assertEqual(field(stroke, "Opct").get("unit"), "#Prc")
        self.assertEqual(field(stroke, "Sz  ").get("value"), "6")
        self.assertEqual(field(stroke, "Sz  ").get("unit"), "#Pxl")
        self.assertEqual(rgb(stroke), (255.0, 128.0, 0.0))

    def test_stroke_defaults_are_outside_and_opaque(self):
        stroke = effect(style.build_style_asl(
            stroke={"size": 3, "color": "#000"}), "FrFX")
        self.assertEqual(field(stroke, "Styl").get("value"), "OutF")
        self.assertEqual(field(stroke, "Opct").get("value"), "100")
        self.assertEqual(rgb(stroke), (0.0, 0.0, 0.0))

    def test_positions(self):
        for name, code in (("outside", "OutF"), ("inside", "InsF"),
                           ("center", "CtrF")):
            stroke = effect(style.build_style_asl(
                stroke={"size": 1, "color": "#fff", "position": name}), "FrFX")
            self.assertEqual(field(stroke, "Styl").get("value"), code)

    def test_shadow_defaults(self):
        shadow = effect(style.build_style_asl(drop_shadow={}), "DrSh")
        self.assertIsNone(effect(style.build_style_asl(drop_shadow={}),
                                 "FrFX"))
        want = {"Md  ": "Mltp", "Opct": "75", "lagl": "120", "Dstn": "5",
                "blur": "5", "Ckmt": "0"}
        for key, value in want.items():
            self.assertEqual(field(shadow, key).get("value"), value, key)
        self.assertEqual(rgb(shadow), (0.0, 0.0, 0.0))
        # Krita's reader defaults to the document's global light: switch off.
        self.assertEqual(field(shadow, "uglg").get("value"), "0")
        self.assertEqual(field(shadow, "lagl").get("unit"), "#Ang")
        self.assertEqual(field(shadow, "Dstn").get("unit"), "#Pxl")

    def test_shadow_explicit_values(self):
        shadow = effect(style.build_style_asl(drop_shadow={
            "color": [10, 20, 30], "opacity": 0.5, "angle": -45,
            "distance": 12, "size": 3, "spread": 20}), "DrSh")
        for key, value in {"Opct": "50", "lagl": "-45", "Dstn": "12",
                           "blur": "3", "Ckmt": "20"}.items():
            self.assertEqual(field(shadow, key).get("value"), value, key)
        self.assertEqual(rgb(shadow), (10.0, 20.0, 30.0))

    def test_both_effects_and_neither(self):
        asl = style.build_style_asl(stroke={"size": 2, "color": "#fff"},
                                    drop_shadow={})
        self.assertIsNotNone(effect(asl, "FrFX"))
        self.assertIsNotNone(effect(asl, "DrSh"))
        empty = style.build_style_asl()
        self.assertEqual(field(ET.fromstring(empty).find("node"),
                               "masterFXSwitch").get("value"), "0")

    def test_numbers_are_rounded_to_whole_numbers(self):
        # Krita truncates fractions; the builder rounds so the readback matches.
        shadow = effect(style.build_style_asl(drop_shadow={
            "distance": 12.5, "size": 7.25, "opacity": 0.335}), "DrSh")
        self.assertEqual(field(shadow, "Dstn").get("value"), "13")
        self.assertEqual(field(shadow, "blur").get("value"), "7")
        self.assertEqual(field(shadow, "Opct").get("value"), "34")
        negative = effect(style.build_style_asl(drop_shadow={"angle": -44.5}),
                          "DrSh")
        self.assertEqual(field(negative, "lagl").get("value"), "-45")

    def test_bounds_inclusive(self):
        style.build_style_asl(stroke={"size": 1, "color": "#fff"})
        style.build_style_asl(stroke={"size": 250, "color": "#fff",
                                      "opacity": 0})
        style.build_style_asl(drop_shadow={
            "angle": -180, "distance": 0, "size": 0, "spread": 0,
            "opacity": 1})
        style.build_style_asl(drop_shadow={
            "angle": 180, "distance": 30000, "size": 250, "spread": 100})

    def test_bounds_refused(self):
        bad_strokes = [{"size": 0}, {"size": 251}, {"size": -1},
                       {"size": 6, "opacity": 1.01}, {"size": 6, "opacity": -0.1}]
        for spec in bad_strokes:
            with self.assertRaises(style.StyleError, msg=spec):
                style.build_style_asl(stroke=dict(spec, color="#fff"))
        bad_shadows = [{"angle": 181}, {"angle": -181}, {"distance": -1},
                       {"distance": 30001}, {"size": 251}, {"size": -1},
                       {"spread": 101}, {"spread": -1}, {"opacity": 2}]
        for spec in bad_shadows:
            with self.assertRaises(style.StyleError, msg=spec):
                style.build_style_asl(drop_shadow=spec)

    def test_refusals_name_the_field(self):
        cases = [
            ({"stroke": {"color": "#fff"}}, "stroke.size"),
            ({"stroke": {"size": 3}}, "stroke.color"),
            ({"stroke": {"size": 300, "color": "#fff"}}, "stroke.size"),
            ({"stroke": {"size": 3, "color": "#fff", "position": "above"}},
             "stroke.position"),
            ({"stroke": {"size": "3", "color": "#fff"}}, "stroke.size"),
            ({"stroke": {"size": True, "color": "#fff"}}, "stroke.size"),
            ({"stroke": {"size": 3, "color": "#fff", "width": 2}}, "width"),
            ({"stroke": [3]}, "stroke"),
            ({"drop_shadow": {"blur": 3}}, "blur"),
            ({"drop_shadow": {"angle": float("nan")}}, "drop_shadow.angle"),
            ({"drop_shadow": {"distance": "far"}}, "drop_shadow.distance"),
            ({"drop_shadow": "yes"}, "drop_shadow"),
        ]
        for kwargs, needle in cases:
            with self.assertRaises(style.StyleError, msg=kwargs) as ctx:
                style.build_style_asl(**kwargs)
            self.assertIn(needle, str(ctx.exception), kwargs)


class ColourTests(unittest.TestCase):
    def test_hex_forms(self):
        parse = style.parse_rgb
        self.assertEqual(parse("#ff8000", "c"), (255, 128, 0))
        self.assertEqual(parse("#FF8000", "c"), (255, 128, 0))
        self.assertEqual(parse("#f80", "c"), (255, 136, 0))
        self.assertEqual(parse("  #102030  ", "c"), (16, 32, 48))
        self.assertEqual(parse("#102030ff", "c"), (16, 32, 48))

    def test_array_forms(self):
        parse = style.parse_rgb
        self.assertEqual(parse([1, 2, 3], "c"), (1, 2, 3))
        self.assertEqual(parse([1, 2, 3, 255], "c"), (1, 2, 3))
        self.assertEqual(parse((255, 0, 0), "c"), (255, 0, 0))
        self.assertEqual(parse([0.4, 127.6, 255], "c"), (0, 128, 255))

    def test_refusals(self):
        parse = style.parse_rgb
        for value in ("#12345", "#gg0000", "#", [1, 2], [1, 2, 3, 4, 5],
                      [256, 0, 0], [-1, 0, 0], ["a", 0, 0], [True, 0, 0],
                      None, 5, {"r": 1}):
            with self.assertRaises(style.StyleError, msg=repr(value)):
                parse(value, "c")

    def test_alpha_is_refused_and_points_at_opacity(self):
        for value in ("#10203080", [1, 2, 3, 128]):
            with self.assertRaises(style.StyleError) as ctx:
                style.parse_rgb(value, "stroke.color")
            self.assertIn("opacity", str(ctx.exception))
            self.assertIn("stroke.color", str(ctx.exception))


class RequestTests(unittest.TestCase):
    def test_needs_something(self):
        with self.assertRaises(style.StyleError) as ctx:
            style.validate_request(None, None, False)
        self.assertIn("at least one", str(ctx.exception))

    def test_clear_alone(self):
        self.assertEqual(style.validate_request(None, None, True),
                         (None, None, True))

    def test_clear_is_exclusive(self):
        for stroke, shadow in (({"size": 3, "color": "#fff"}, None),
                               (None, {}), ({"size": 3, "color": "#fff"}, {})):
            with self.assertRaises(style.StyleError) as ctx:
                style.validate_request(stroke, shadow, True)
            self.assertIn("clear", str(ctx.exception))

    def test_clear_must_be_boolean(self):
        for value in ("yes", 1, None):
            with self.assertRaises(style.StyleError):
                style.validate_request(None, None, value)

    def test_normalised_request(self):
        stroke, shadow, clear = style.validate_request(
            {"size": 4.4, "color": "#fff"}, {"opacity": 0.5}, False)
        self.assertFalse(clear)
        self.assertEqual(stroke, {"size": 4, "color": (255, 255, 255),
                                  "position": "outside", "opacity": 100})
        self.assertEqual(shadow, {"color": (0, 0, 0), "opacity": 50,
                                  "angle": 120, "distance": 5, "size": 5,
                                  "spread": 0})

    def test_empty_shadow_object_means_defaults(self):
        _, shadow, _ = style.validate_request(None, {}, False)
        self.assertEqual(shadow["opacity"], 75)

    def test_validation_happens_before_anything_else(self):
        with self.assertRaises(style.StyleError):
            style.validate_request({"size": 3, "color": "#fff"},
                                   {"angle": 999}, False)


class ParserTests(unittest.TestCase):
    def test_krita_readback_of_stroke_and_shadow(self):
        got = style.parse_style_asl(BOTH)
        self.assertEqual(got, {
            "has_style": True, "other_effects": [],
            "stroke": {"size": 6, "color": "#ffffff", "position": "outside",
                       "opacity": 1.0},
            "drop_shadow": {"color": "#0a141e", "opacity": 0.5, "angle": -45,
                            "distance": 12, "size": 3, "spread": 20}})

    def test_other_effects_are_named_and_disabled_ones_skipped(self):
        got = style.parse_style_asl(OTHER)
        self.assertTrue(got["has_style"])
        self.assertEqual(got["other_effects"], ["outer_glow", "color_overlay"])
        self.assertNotIn("stroke", got)
        self.assertEqual(got["drop_shadow"]["distance"], 21)
        self.assertEqual(got["drop_shadow"]["angle"], 120)
        self.assertTrue(got["drop_shadow"]["use_global_light"])

    def test_switched_off_and_absent_styles_have_no_style(self):
        for asl in (OFF, "", "   ", None):
            self.assertEqual(style.parse_style_asl(asl),
                             {"has_style": False, "other_effects": []}, asl)

    def test_master_switch_off_hides_enabled_effects(self):
        off = BOTH.replace('key="masterFXSwitch" value="1"',
                           'key="masterFXSwitch" value="0"')
        self.assertFalse(style.parse_style_asl(off)["has_style"])
        self.assertNotIn("stroke", style.parse_style_asl(off))

    def test_non_solid_stroke_is_reported_as_such(self):
        gradient = BOTH.replace('key="PntT" value="SClr"',
                                'key="PntT" value="GrFl"')
        stroke = style.parse_style_asl(gradient)["stroke"]
        self.assertEqual(stroke["fill"], "gradient")
        self.assertNotIn("color", stroke)

    def test_non_default_blend_mode_is_reported(self):
        multiplied = BOTH.replace('typeId="BlnM" type="Enum" key="Md  " '
                                  'value="Nrml"',
                                  'typeId="BlnM" type="Enum" key="Md  " '
                                  'value="Mltp"')
        self.assertEqual(style.parse_style_asl(multiplied)["stroke"]
                         ["blend_mode"], "Mltp")

    def test_colour_noise_rounds(self):
        noisy = BOTH.replace('key="Grn " value="255"',
                             'key="Grn " value="254.9999993"')
        self.assertEqual(style.parse_style_asl(noisy)["stroke"]["color"],
                         "#ffffff")

    def test_unparseable_xml_is_refused(self):
        with self.assertRaises(style.StyleError):
            style.parse_style_asl("<asl><node")

    def test_xml_without_a_style_descriptor(self):
        self.assertFalse(style.parse_style_asl("<asl/>")["has_style"])


class RoundTripTests(unittest.TestCase):
    def check(self, stroke, shadow):
        s, d, _ = style.validate_request(stroke, shadow, False)
        got = style.parse_style_asl(style.assemble_style_asl(s, d))
        want = style.expected_style(s, d)
        self.assertEqual({k: got[k] for k in ("stroke", "drop_shadow")
                          if k in got}, want)
        self.assertTrue(got["has_style"])
        self.assertEqual(got["other_effects"], [])

    def test_stroke_only(self):
        self.check({"size": 6, "color": "#ffffff"}, None)

    def test_shadow_only(self):
        self.check(None, {})

    def test_everything(self):
        self.check({"size": 11, "color": [1, 2, 3], "position": "center",
                    "opacity": 0.4},
                   {"color": "#445566", "opacity": 0.9, "angle": -170,
                    "distance": 300, "size": 40, "spread": 55})

    def test_empty_style_reads_as_none(self):
        got = style.parse_style_asl(style.assemble_style_asl(None, None))
        self.assertEqual(got, {"has_style": False, "other_effects": []})


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    if result.wasSuccessful():
        print("OK")
    raise SystemExit(0 if result.wasSuccessful() else 1)
