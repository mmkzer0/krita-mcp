"""Layer styles for lettering: an outline (stroke) and a drop shadow.

Krita's libkis can only exchange a layer style as Photoshop-ASL XML
(``Node.layerStyleToAsl`` / ``setLayerStyleFromAsl``). The first half of this
module is a pure builder/parser for the two effects this tool models; the
second half is the operations. The result is an ordinary Krita layer style, so
it stays editable in Layer > Layer Style.

Facts measured on Krita 5.3.4 that shape the code:

* Krita stores every number as a whole number: 33.5 is read back as 33, a
  colour channel 20.5 as 20. Sizes, opacities (as percent) and angles are
  therefore rounded here, so the value returned is the value Krita holds.
* The reader is lenient and substitutes defaults for anything unspecified or
  malformed (a stroke defaults to the Multiply blend mode and a shadow to
  "use global light"). The builder writes every field explicitly.
* ``setLayerStyleFromAsl("")`` is refused and a refused call changes nothing;
  there is no way to unset a style. "Clear" installs an empty style with its
  master switch off, which renders and reads back as no style.
"""

import xml.etree.ElementTree as ET

MAX_STROKE_SIZE = 250
MAX_SHADOW_SIZE = 250
MAX_SHADOW_DISTANCE = 30000

_POSITIONS = {"outside": "OutF", "inside": "InsF", "center": "CtrF"}
_POSITION_NAMES = {v: k for k, v in _POSITIONS.items()}

_STROKE_KEYS = ("size", "color", "position", "opacity")
_SHADOW_KEYS = ("color", "opacity", "angle", "distance", "size", "spread")

# ASL effect descriptor keys this tool does not model, by friendly name.
_OTHER_EFFECTS = (
    ("IrSh", "inner_shadow"), ("OrGl", "outer_glow"), ("IrGl", "inner_glow"),
    ("ebbl", "bevel_emboss"), ("ChFX", "satin"), ("SoFi", "color_overlay"),
    ("GrFl", "gradient_overlay"), ("patternFill", "pattern_overlay"),
)
_MASK_TYPES = ("transparencymask", "filtermask", "transformmask",
               "selectionmask", "colorizemask")


class StyleError(ValueError):
    """A request or an ASL document this module refuses."""


# --------------------------------------------------------------------------
# validation (pure)
# --------------------------------------------------------------------------

def _number(value, field, lo, hi, unit):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StyleError("{0} must be a number, got {1!r}.".format(field, value))
    if value != value or value in (float("inf"), float("-inf")) \
            or not lo <= value <= hi:
        raise StyleError("{0} must be between {1} and {2}{3}, got {4!r}."
                         .format(field, lo, hi, unit, value))
    return value


def _whole(value):
    """Round half up; Krita keeps whole numbers only."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def _parse_hex(text, field):
    digits = text[1:]
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    if len(digits) not in (6, 8) or any(
            c not in "0123456789abcdefABCDEF" for c in digits):
        raise StyleError("{0}: {1!r} is not a valid #rgb, #rrggbb or "
                         "#rrggbbaa colour.".format(field, text))
    parts = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
    return parts


def parse_rgb(value, field):
    """A colour as (r, g, b) ints. Styles carry no alpha; use `opacity`."""
    if isinstance(value, (list, tuple)):
        if len(value) not in (3, 4):
            raise StyleError("{0} needs 3 or 4 numbers [r,g,b(,a)], got {1}."
                             .format(field, len(value)))
        parts = [_whole(_number(c, "{0}[{1}]".format(field, i), 0, 255, ""))
                 for i, c in enumerate(value)]
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("#"):
            parts = _parse_hex(text, field)
        else:
            parts = _named_color(text, field)
    else:
        raise StyleError("{0} must be a colour string or [r,g,b] array, got "
                         "{1}.".format(field, type(value).__name__))
    if len(parts) == 4 and parts[3] != 255:
        raise StyleError("{0} has alpha {1}, but a layer style colour is "
                         "opaque; drop the alpha and set `opacity` instead."
                         .format(field, parts[3]))
    return tuple(parts[:3])


def _named_color(text, field):
    from .imaging import ImagingError, parse_color
    try:
        color = parse_color(text)
    except ImagingError as exc:
        raise StyleError("{0}: {1}".format(field, exc))
    return [color.red(), color.green(), color.blue(), color.alpha()]


def _spec(spec, name, allowed):
    if not isinstance(spec, dict):
        raise StyleError("{0} must be an object, got {1}.".format(
            name, type(spec).__name__))
    unknown = sorted(set(spec) - set(allowed))
    if unknown:
        raise StyleError("{0} has unknown field(s) {1}; allowed: {2}.".format(
            name, ", ".join(unknown), ", ".join(allowed)))
    return spec


def _opacity(spec, name, default):
    value = spec.get("opacity", default)
    return _whole(_number(value, name + ".opacity", 0, 1, "") * 100)


def normalize_stroke(spec):
    """Validate a stroke request; returns whole-number canonical fields."""
    spec = _spec(spec, "stroke", _STROKE_KEYS)
    for key in ("size", "color"):
        if spec.get(key) is None:
            raise StyleError("stroke.{0} is required.".format(key))
    position = spec.get("position", "outside")
    if position not in _POSITIONS:
        raise StyleError("stroke.position must be one of {0}, got {1!r}."
                         .format(", ".join(_POSITIONS), position))
    return {
        "size": _whole(_number(spec["size"], "stroke.size", 1,
                               MAX_STROKE_SIZE, " px")),
        "color": parse_rgb(spec["color"], "stroke.color"),
        "position": position,
        "opacity": _opacity(spec, "stroke", 1),
    }


def normalize_shadow(spec):
    """Validate a drop-shadow request, filling the documented defaults."""
    spec = _spec(spec, "drop_shadow", _SHADOW_KEYS)

    def num(key, default, lo, hi, unit):
        value = spec.get(key)
        return _whole(_number(default if value is None else value,
                              "drop_shadow." + key, lo, hi, unit))

    color = spec.get("color")
    return {
        "color": parse_rgb("#000000" if color is None else color,
                           "drop_shadow.color"),
        "opacity": _opacity(spec, "drop_shadow", 0.75),
        "angle": num("angle", 120, -180, 180, " degrees"),
        "distance": num("distance", 5, 0, MAX_SHADOW_DISTANCE, " px"),
        "size": num("size", 5, 0, MAX_SHADOW_SIZE, " px"),
        "spread": num("spread", 0, 0, 100, " %"),
    }


def validate_request(stroke, drop_shadow, clear):
    """(stroke, shadow, clear) normalised, or StyleError. Touches nothing."""
    if clear is not True and clear is not False:
        raise StyleError("clear must be true or false, got {0!r}.".format(clear))
    if clear:
        if stroke is not None or drop_shadow is not None:
            raise StyleError("`clear` removes the style and cannot be "
                             "combined with `stroke` or `drop_shadow`; send "
                             "`clear` alone, or drop it to set a style.")
        return None, None, True
    if stroke is None and drop_shadow is None:
        raise StyleError("Give at least one of `stroke`, `drop_shadow` or "
                         "`clear`: true.")
    return (None if stroke is None else normalize_stroke(stroke),
            None if drop_shadow is None else normalize_shadow(drop_shadow),
            False)


# --------------------------------------------------------------------------
# ASL XML (pure)
# --------------------------------------------------------------------------

def _boolean(key, on):
    return '<node type="Boolean" key="{0}" value="{1}"/>'.format(key, int(on))


def _unit(key, unit, value):
    return '<node type="UnitFloat" key="{0}" value="{1}" unit="{2}"/>'.format(
        key, value, unit)


def _enum(key, type_id, value):
    return '<node type="Enum" key="{0}" typeId="{1}" value="{2}"/>'.format(
        key, type_id, value)


def _rgb(rgb):
    r, g, b = rgb
    return ('<node type="Descriptor" key="Clr " name="" classId="RGBC">'
            '<node type="Double" key="Rd  " value="{0}"/>'
            '<node type="Double" key="Grn " value="{1}"/>'
            '<node type="Double" key="Bl  " value="{2}"/></node>'
            .format(r, g, b))


def _descriptor(key, parts):
    return ('<node type="Descriptor" key="{0}" name="" classId="{0}">{1}</node>'
            .format(key, "".join(parts)))


def _shadow_asl(s):
    return _descriptor("DrSh", [
        _boolean("enab", True),
        _enum("Md  ", "BlnM", "Mltp"),
        _rgb(s["color"]),
        _unit("Opct", "#Prc", s["opacity"]),
        _boolean("uglg", False),
        _unit("lagl", "#Ang", s["angle"]),
        _unit("Dstn", "#Pxl", s["distance"]),
        # Krita labels the spread (choke) percentage with the pixel unit.
        _unit("Ckmt", "#Pxl", s["spread"]),
        _unit("blur", "#Pxl", s["size"]),
        _unit("Nose", "#Prc", 0),
        _boolean("AntA", False),
        _boolean("layerConceals", True),
    ])


def _stroke_asl(s):
    return _descriptor("FrFX", [
        _boolean("enab", True),
        _enum("Styl", "FStl", _POSITIONS[s["position"]]),
        _enum("PntT", "FrFl", "SClr"),
        _enum("Md  ", "BlnM", "Nrml"),
        _unit("Opct", "#Prc", s["opacity"]),
        _unit("Sz  ", "#Pxl", s["size"]),
        _rgb(s["color"]),
    ])


def _style_document(master_on, effects):
    return ('<asl><node type="Descriptor" name="" classId="null">'
            + _unit("Scl ", "#Prc", 100)
            + _boolean("masterFXSwitch", master_on)
            + "".join(effects) + "</node></asl>")


def assemble_style_asl(stroke, shadow):
    """ASL XML for already-normalised effects; neither means an empty style
    with its master switch off."""
    effects = []
    if shadow is not None:
        effects.append(_shadow_asl(shadow))
    if stroke is not None:
        effects.append(_stroke_asl(stroke))
    return _style_document(bool(effects), effects)


def build_style_asl(stroke=None, drop_shadow=None):
    """Validate raw request dicts and return the ASL XML for them."""
    return assemble_style_asl(
        None if stroke is None else normalize_stroke(stroke),
        None if drop_shadow is None else normalize_shadow(drop_shadow))


def _children(desc):
    return {c.get("key"): c for c in desc.findall("node") if c.get("key")}


def _value(node, default=None):
    if node is None:
        return default
    return node.get("value", default)


def _flag(desc, key, default=False):
    node = _children(desc).get(key)
    return default if node is None else _value(node) in ("1", "true")


def _num(desc, key, default=0):
    try:
        number = float(_value(_children(desc).get(key), default))
    except ValueError:
        return default
    return int(number) if number == int(number) else number


def _enum_value(desc, key):
    return _value(_children(desc).get(key))


def _hex(desc):
    clr = _children(desc).get("Clr ")
    channels = _children(clr) if clr is not None else {}
    parts = []
    for key in ("Rd  ", "Grn ", "Bl  "):
        try:
            number = float(_value(channels.get(key), 0))
        except ValueError:
            number = 0
        parts.append(max(0, min(255, int(round(number)))))
    return "#{0:02x}{1:02x}{2:02x}".format(*parts)


def _percent(desc, key, default):
    return round(_num(desc, key, default) / 100.0, 2)


def _read_stroke(desc):
    out = {"size": _num(desc, "Sz  "), "color": _hex(desc),
           "position": _POSITION_NAMES.get(_enum_value(desc, "Styl"),
                                           "outside"),
           "opacity": _percent(desc, "Opct", 100)}
    fill = _enum_value(desc, "PntT")
    if fill not in (None, "SClr"):
        out["fill"] = {"GrFl": "gradient", "Ptrn": "pattern"}.get(fill, fill)
        del out["color"]
    if _enum_value(desc, "Md  ") not in (None, "Nrml"):
        out["blend_mode"] = _enum_value(desc, "Md  ")
    return out


def _read_shadow(desc):
    out = {"color": _hex(desc), "opacity": _percent(desc, "Opct", 75),
           "angle": _num(desc, "lagl", 120), "distance": _num(desc, "Dstn", 5),
           "size": _num(desc, "blur", 5), "spread": _num(desc, "Ckmt", 0)}
    if _flag(desc, "uglg"):
        # The angle then comes from the document's global light.
        out["use_global_light"] = True
    if _enum_value(desc, "Md  ") not in (None, "Mltp"):
        out["blend_mode"] = _enum_value(desc, "Md  ")
    return out


def _style_descriptor(root):
    for desc in root.iter("node"):
        if desc.get("type") == "Descriptor" and \
                "masterFXSwitch" in _children(desc):
            return desc
    return None


def parse_style_asl(asl):
    """{has_style, stroke?, drop_shadow?, other_effects} from Krita's ASL.

    ``has_style`` is true only when the style is switched on and at least one
    effect is enabled; the empty style `clear` leaves behind reads as none.
    """
    result = {"has_style": False, "other_effects": []}
    if not asl or not asl.strip():
        return result
    try:
        root = ET.fromstring(asl)
    except ET.ParseError as exc:
        raise StyleError("Krita returned layer style XML that cannot be "
                         "parsed: {0}".format(exc))
    style = _style_descriptor(root)
    if style is None or not _flag(style, "masterFXSwitch"):
        return result
    effects = _children(style)
    enabled = {key for key, desc in effects.items()
               if desc.get("type") == "Descriptor" and _flag(desc, "enab")}
    if "FrFX" in enabled:
        result["stroke"] = _read_stroke(effects["FrFX"])
    if "DrSh" in enabled:
        result["drop_shadow"] = _read_shadow(effects["DrSh"])
    result["other_effects"] = [name for key, name in _OTHER_EFFECTS
                               if key in enabled]
    result["has_style"] = bool(enabled)
    return result


def expected_style(stroke, shadow):
    """What parse_style_asl should report for normalised requests."""
    out = {}
    if stroke is not None:
        out["stroke"] = {"size": stroke["size"],
                         "color": "#{0:02x}{1:02x}{2:02x}".format(
                             *stroke["color"]),
                         "position": stroke["position"],
                         "opacity": round(stroke["opacity"] / 100.0, 2)}
    if shadow is not None:
        out["drop_shadow"] = {
            "color": "#{0:02x}{1:02x}{2:02x}".format(*shadow["color"]),
            "opacity": round(shadow["opacity"] / 100.0, 2),
            "angle": shadow["angle"], "distance": shadow["distance"],
            "size": shadow["size"], "spread": shadow["spread"]}
    return out


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

def _o():
    from . import ops
    return ops


def _err(message, kind="invalid_request"):
    return _o().OpError(message, kind=kind)


def _styled_layer(doc, params):
    o = _o()
    ref = params.get("layer")
    if ref is None or ref == "":
        raise _err("missing required parameter 'layer'; name the layer to "
                   "style.")
    node = o.resolve_node(doc, ref)
    if node.type() in _MASK_TYPES:
        raise _err("{0!r} is a {1}; layer styles belong on paint, vector, "
                   "group and other layers, not masks.".format(
                       node.name(), node.type()))
    return node


def _read_back(node):
    try:
        return parse_style_asl(node.layerStyleToAsl())
    except StyleError as exc:
        raise _err(str(exc), kind="internal")


def op_set_layer_style(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    clear = params.get("clear", False)
    clear = False if clear is None else o._as_bool(clear, "clear")
    try:
        stroke, shadow, clear = validate_request(
            params.get("stroke"), params.get("drop_shadow"), clear)
    except StyleError as exc:
        raise _err(str(exc))
    node = _styled_layer(doc, params)
    if node.locked():
        raise _err("Layer {0!r} is locked; unlock it with set_layer before "
                   "styling it.".format(node.name()))

    before = _read_back(node)
    result = {"layer": node.name()}
    if clear and not node.layerStyleToAsl():
        result["style"] = {}
        return result
    if not node.setLayerStyleFromAsl(assemble_style_asl(stroke, shadow)):
        raise _err("Krita rejected the layer style for {0!r}; the layer was "
                   "not changed.".format(node.name()), kind="internal")
    o._refresh(doc)

    now = _read_back(node)
    result["style"] = {k: now[k] for k in ("stroke", "drop_shadow") if k in now}
    if result["style"] != expected_style(stroke, shadow):
        result["warnings"] = [
            "Krita holds a different style than requested; call "
            "get_layer_style and compare."]
    if before["other_effects"]:
        result["discarded_effects"] = before["other_effects"]
    return result


def op_get_layer_style(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    node = _styled_layer(doc, params)
    result = {"layer": node.name()}
    result.update(_read_back(node))
    return result


def register(op):
    op("set_layer_style", timeout=30.0, mutates=True)(op_set_layer_style)
    op("get_layer_style", timeout=20.0)(op_get_layer_style)
