"""Build pixel-coordinate SVG documents without Krita or Qt dependencies.

Coordinates are rounded to at most three decimals when serialized. Shape names
are ASCII ids, with unsupported characters replaced by underscores.
"""

import copy
import math
import re
import xml.etree.ElementTree as ET


class ShapeSpecError(ValueError):
    """A shape specification cannot be represented by this builder."""


_COLORS = dict(zip(
    "black silver gray white maroon red purple fuchsia green lime olive yellow navy blue teal aqua".split(),
    "000000 c0c0c0 808080 ffffff 800000 ff0000 800080 ff00ff 008000 00ff00 808000 ffff00 000080 0000ff 008080 00ffff".split()))
_COMMON = {"type", "name", "fill", "stroke", "stroke_width"}
_KEYS = {
    "text": {"type", "name", "text", "x", "y", "width", "align", "font", "size", "color", "stroke", "stroke_width", "bold", "italic", "line_height", "letter_spacing", "rotation", "vertical"},
    "ellipse": _COMMON | {"cx", "cy", "rx", "ry", "rotation"},
    "rect": _COMMON | {"x", "y", "width", "height", "radius", "rotation"},
    "path": _COMMON | {"d"},
    "polygon": _COMMON | {"points"},
    "bubble": _COMMON | {"x", "y", "width", "height", "tail", "tail_width"},
}
_PATH = re.compile(r"[MmLlHhVvCcSsQqTtAaZz0-9+\.eE,\s-]+\Z")
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
MAX_PATH_CHARS = 100000
MAX_POLYGON_POINTS = 5000


def parse_color(value):
    """Return (CSS hex or 'none', opacity) for a supported color."""
    if isinstance(value, str):
        color = value.strip().lower()
        if color == "none":
            return "none", 1.0
        if color == "transparent":
            return "#000000", 0.0
        if color in _COLORS:
            return "#" + _COLORS[color], 1.0
        if re.fullmatch(r"#[0-9a-f]{3}", color):
            return "#" + "".join(c * 2 for c in color[1:]), 1.0
        if re.fullmatch(r"#[0-9a-f]{6}([0-9a-f]{2})?", color):
            return color[:7], int(color[7:9], 16) / 255.0 if len(color) == 9 else 1.0
    elif isinstance(value, list) and len(value) in (3, 4):
        if all(type(c) is int and 0 <= c <= 255 for c in value):
            return "#{:02x}{:02x}{:02x}".format(*value[:3]), value[3] / 255.0 if len(value) == 4 else 1.0
    raise ShapeSpecError("color: expected a color name, #rgb, #rrggbb, #rrggbbaa, or RGB(A) integer list")


def _number(value, field, minimum=None, strict=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShapeSpecError("{0}: expected a finite number".format(field))
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ShapeSpecError("{0}: expected a finite number".format(field))
    if minimum is not None and (value < minimum or (strict and value == minimum)):
        raise ShapeSpecError("{0}: must be {1} {2}".format(field, ">" if strict else ">=", minimum))
    return value


def _fmt(value):
    _number(value, "serialized geometry")
    result = format(value, ".3f").rstrip("0").rstrip(".")
    return "0" if result in ("", "-0") else result


def _string(value, field):
    if not isinstance(value, str) or _BAD_XML.search(value):
        raise ShapeSpecError("{0}: expected XML-compatible text".format(field))
    return value


def _id(value, field):
    value = _string(value, field)
    value = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not value:
        raise ShapeSpecError("{0}: name must not be empty".format(field))
    return value


def _paint(attrs, field, value):
    try:
        color, opacity = parse_color(value)
    except ShapeSpecError as exc:
        raise ShapeSpecError("{0}: {1}".format(field, exc)) from None
    attrs[field] = color
    if opacity != 1.0:
        attrs[field + "-opacity"] = _fmt(opacity)


def _font(value):
    value = _string(value, "font")
    if any(c in value for c in ';"<>{}\\'):
        raise ShapeSpecError("font: unsafe CSS character")
    families = []
    for family in value.split(","):
        family = " ".join(family.strip().split())
        if family.startswith("'") and family.endswith("'") and len(family) >= 2:
            family = family[1:-1]
        if not family:
            raise ShapeSpecError("font: expected non-empty font families")
        if " " in family or "'" in family:
            family = "'" + family.replace("'", "\\27 ") + "'"
        families.append(family)
    return ", ".join(families)


def _point(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ShapeSpecError("{0}: expected [x, y]".format(field))
    return tuple(_number(v, field) for v in value)


def _bubble(spec, num):
    x, y, width, height = (num(k, positive=k in ("width", "height")) for k in ("x", "y", "width", "height"))
    rx, ry = width / 2.0, height / 2.0
    cx, cy = x + rx, y + ry
    _number(rx, "width", 0, True)
    _number(ry, "height", 0, True)
    _number(cx, "x")
    _number(cy, "y")
    tail_width = num("tail_width", 0.25 * min(width, height), positive=True)
    if "tail" not in spec:
        return "M {0} {1} A {2} {3} 0 1 1 {4} {1} A {2} {3} 0 1 1 {0} {1} Z".format(
            _fmt(x), _fmt(cy), _fmt(rx), _fmt(ry), _fmt(x + width))
    tx, ty = _point(spec["tail"], "tail")
    dx, dy = (tx - cx) / rx, (ty - cy) / ry
    if math.hypot(dx, dy) <= 1:
        raise ShapeSpecError("tail: tip must lie outside the ellipse")
    angle = math.atan2(dy, dx)
    speed = math.hypot(rx * math.sin(angle), ry * math.cos(angle))
    # atan keeps the removed arc smaller than a semicircle even for wide tails.
    delta = math.atan(tail_width / (2.0 * speed))
    start = (cx + rx * math.cos(angle + delta), cy + ry * math.sin(angle + delta))
    end = (cx + rx * math.cos(angle - delta), cy + ry * math.sin(angle - delta))
    return "M {0} {1} A {2} {3} 0 1 1 {4} {5} L {6} {7} Z".format(
        *map(_fmt, (start[0], start[1], rx, ry, end[0], end[1], tx, ty)))


def _element(spec, name):
    kind = spec["type"]
    attrs = {"id": name}

    def num(field, default=None, positive=False, nonnegative=False):
        if field not in spec and default is None:
            raise ShapeSpecError("{0}: required field".format(field))
        return _number(spec.get(field, default), field, 0 if positive or nonnegative else None, positive)

    if kind == "text":
        text = _string(spec.get("text"), "text")
        if len(text) > 10000:
            raise ShapeSpecError("text: maximum length is 10000 characters")
        x, y = num("x"), num("y")
        attrs.update(x=_fmt(x), y=_fmt(y))
        align = spec.get("align", "left")
        if not isinstance(align, str) or align not in ("left", "center", "right"):
            raise ShapeSpecError("align: expected left, center, or right")
        style = ["text-anchor: " + {"left": "start", "center": "middle", "right": "end"}[align],
                 "font-family: " + _font(spec.get("font", "sans-serif")),
                 "font-size: " + _fmt(num("size", 24, positive=True)) + "px"]
        for field in ("bold", "italic", "vertical"):
            if field in spec and type(spec[field]) is not bool:
                raise ShapeSpecError("{0}: expected bool".format(field))
        if spec.get("bold", False):
            style.append("font-weight: bold")
        if spec.get("italic", False):
            style.append("font-style: italic")
        if spec.get("vertical", False):
            style.append("writing-mode: vertical-rl")
        if "width" in spec:
            style.append("inline-size: " + _fmt(num("width", positive=True)) + "px")
        if "line_height" in spec:
            style.append("line-height: " + _fmt(num("line_height", positive=True)))
        if "letter_spacing" in spec:
            style.append("letter-spacing: " + _fmt(num("letter_spacing")) + "px")
        if "\n" in text:
            style.append("white-space: pre-wrap")
        _paint(attrs, "fill", spec.get("color", "black"))
        if "stroke" in spec:
            _paint(attrs, "stroke", spec["stroke"])
            style.append("paint-order: stroke")
        attrs["style"] = "; ".join(style)
        centre = (x, y)
    else:
        _paint(attrs, "fill", spec.get("fill", "white" if kind == "bubble" else "black"))
        _paint(attrs, "stroke", spec.get("stroke", "black" if kind == "bubble" else "none"))
        if kind == "ellipse":
            for field in ("cx", "cy", "rx", "ry"):
                attrs[field] = _fmt(num(field, positive=field in ("rx", "ry")))
            centre = (num("cx"), num("cy"))
        elif kind == "rect":
            x, y, width, height = (num(k, positive=k in ("width", "height")) for k in ("x", "y", "width", "height"))
            attrs.update(x=_fmt(x), y=_fmt(y), width=_fmt(width), height=_fmt(height))
            if "radius" in spec:
                attrs["rx"] = attrs["ry"] = _fmt(num("radius", nonnegative=True))
            centre = (x + width / 2.0, y + height / 2.0)
        elif kind == "path":
            d = _string(spec.get("d"), "d")
            if len(d) > MAX_PATH_CHARS:
                raise ShapeSpecError("d: at most {0} characters".format(MAX_PATH_CHARS))
            if not _PATH.fullmatch(d) or not d.strip():
                raise ShapeSpecError("d: contains unsupported SVG path grammar characters")
            attrs["d"] = d
        elif kind == "polygon":
            points = spec.get("points")
            if not isinstance(points, list) or len(points) < 3:
                raise ShapeSpecError("points: expected at least three [x, y] points")
            if len(points) > MAX_POLYGON_POINTS:
                raise ShapeSpecError("points: at most {0} points".format(MAX_POLYGON_POINTS))
            attrs["points"] = " ".join("{0},{1}".format(*map(_fmt, _point(p, "points"))) for p in points)
        elif kind == "bubble":
            attrs["d"] = _bubble(spec, num)
            kind = "path"
    attrs["stroke-width"] = _fmt(num("stroke_width", 3 if spec["type"] == "bubble" else 0, nonnegative=True))
    if "rotation" in spec:
        attrs["transform"] = "rotate({0} {1} {2})".format(_fmt(num("rotation")), *map(_fmt, centre))
    element = ET.Element(kind, attrs)
    if kind == "text":
        element.text = text
    return element


def build_svg(shapes, width, height, id_prefix="mcp"):
    """Return (svg_text, names), using one top-level SVG element per spec.

    Errors identify the zero-based shape index and invalid field. Dimensions
    must be positive; at most 500 shapes and 10000 text characters are accepted.
    """
    width = _number(width, "width", 0, True)
    height = _number(height, "height", 0, True)
    prefix = _id(id_prefix, "id_prefix")
    if not isinstance(shapes, list) or len(shapes) > 500:
        raise ShapeSpecError("shapes: expected a list of at most 500 shapes")
    root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "width": _fmt(width) + "px", "height": _fmt(height) + "px", "viewBox": "0 0 {0} {1}".format(_fmt(width), _fmt(height))})
    names, seen = [], set()
    for i, spec in enumerate(shapes):
        try:
            if not isinstance(spec, dict):
                raise ShapeSpecError("spec: expected a dict")
            kind = spec.get("type")
            if not isinstance(kind, str) or kind not in _KEYS:
                raise ShapeSpecError("type: expected " + ", ".join(sorted(_KEYS)))
            if set(spec) - _KEYS[kind]:
                raise ShapeSpecError("unknown keys {0}; allowed keys: {1}".format(
                    ", ".join(sorted(map(str, set(spec) - _KEYS[kind]))), ", ".join(sorted(_KEYS[kind]))))
            name = _id(spec["name"], "name") if "name" in spec else "{0}-{1}".format(prefix, i)
            if name in seen:
                raise ShapeSpecError("name: duplicate id " + name)
            root.append(_element(spec, name))
            names.append(name)
            seen.add(name)
        except ShapeSpecError as exc:
            raise ShapeSpecError("shape {0}: {1}".format(i, exc)) from None
    return ET.tostring(root, encoding="unicode"), names


# --------------------------------------------------------------------------
# layer-level SVG (VectorLayer.toSvg()) editing
# --------------------------------------------------------------------------
#
# A layer SVG is the document Krita hands back from VectorLayer.toSvg(); its
# top-level elements are the shapes, in stacking order (first = bottom). These
# helpers edit that string without Krita, so a layer can be rebuilt without
# libkis Shape.remove() (F31).

LAYER_SVG_NAMESPACES = (
    ("", "http://www.w3.org/2000/svg"),
    ("xlink", "http://www.w3.org/1999/xlink"),
    ("krita", "http://krita.org/namespaces/svg/krita"),
    ("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"),
)
for _prefix, _uri in LAYER_SVG_NAMESPACES:
    ET.register_namespace(_prefix, _uri)

_NON_SHAPE_TAGS = frozenset(
    ("defs", "title", "desc", "metadata", "style", "script"))


class LayerSvgError(ValueError):
    """A layer SVG cannot be read or edited as requested."""


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_layer_svg(svg):
    """Parse a ``VectorLayer.toSvg()`` string and return its <svg> root."""
    if not isinstance(svg, str) or not svg.strip():
        raise LayerSvgError("layer SVG: expected a non-empty string")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise LayerSvgError("layer SVG: does not parse ({0})".format(exc)) \
            from None
    if _local_name(root.tag) != "svg":
        raise LayerSvgError("layer SVG: root must be <svg>, got <{0}>".format(
            _local_name(root.tag)))
    return root


def serialize_layer_svg(root):
    """Serialize an edited layer SVG root (prefixes preserved)."""
    return ET.tostring(root, encoding="unicode")


def empty_root_like(svg):
    """The <svg> root of ``svg`` with every shape removed (defs kept).

    An empty vector layer exports "" from toSvg(); a layer SVG from the same
    document (a scratch layer's) supplies the root size and viewBox.
    """
    root = parse_layer_svg(svg)
    for element in layer_shape_elements(root):
        root.remove(element)
    return root


def is_layer_shape(element):
    """True for a top-level element that is a shape, not <defs> and friends."""
    return _local_name(element.tag) not in _NON_SHAPE_TAGS


def layer_shape_elements(root):
    """Top-level shape elements in stacking order (first = bottom)."""
    return [element for element in root if is_layer_shape(element)]


def layer_shape_ids(svg):
    return [element.get("id") for element in
            layer_shape_elements(parse_layer_svg(svg))]


def layer_shape_count(svg):
    return len(layer_shape_elements(parse_layer_svg(svg)))


def _root_shape(root, shape_id):
    matches = [element for element in layer_shape_elements(root)
               if element.get("id") == shape_id]
    if not matches:
        raise LayerSvgError(
            "layer SVG: no top-level shape with id {0!r}".format(shape_id))
    if len(matches) > 1:
        raise LayerSvgError(
            "layer SVG: id {0!r} appears {1} times at the top level; expected "
            "one shape".format(shape_id, len(matches)))
    return matches[0]


def _copy_shape(element):
    if not isinstance(element, ET.Element) or not is_layer_shape(element):
        raise LayerSvgError(
            "layer SVG: replacement must be a top-level shape element")
    if not element.get("id"):
        raise LayerSvgError("layer SVG: replacement shape needs an id")
    return copy.deepcopy(element)


def layer_shape_element(svg, shape_id):
    """Return a copy of the top-level shape element with ``shape_id``."""
    return root_shape_element(parse_layer_svg(svg), shape_id)


def root_shape_element(root, shape_id):
    """Return a copy of a top-level shape element from a parsed root."""
    return copy.deepcopy(_root_shape(root, shape_id))


def drop_root_shapes(root, ids):
    """Remove every top-level shape whose id is in ``ids``; return ids."""
    wanted = list(dict.fromkeys(ids))
    wanted_set = set(wanted)
    matches = {}
    for element in list(root):
        if is_layer_shape(element) and element.get("id") in wanted_set:
            matches.setdefault(element.get("id"), []).append(element)
    missing = [shape_id for shape_id in wanted if shape_id not in matches]
    if missing:
        raise LayerSvgError(
            "layer SVG: no top-level shape with id {0}".format(
                ", ".join(repr(shape_id) for shape_id in missing)))
    removed = []
    for shape_id in wanted:
        for element in matches[shape_id]:
            root.remove(element)
            removed.append(shape_id)
    return removed


def drop_layer_shapes(svg, ids):
    """Return (edited SVG, removed ids), dropping top-level shapes by id."""
    root = parse_layer_svg(svg)
    removed = drop_root_shapes(root, ids)
    return serialize_layer_svg(root), removed


def drop_root_shapes_at(root, indexes):
    """Remove top-level shapes by their position; return how many were dropped.

    Krita's shape names are not always unique (two shapes can both be named
    "dup"), while its SVG export uniquifies the ids ("dup", "dup1"). Position
    is the only lossless key in that state; the position matches
    ``VectorLayer.shapes()`` order.
    """
    shapes = layer_shape_elements(root)
    for index in indexes:
        if not isinstance(index, int) or isinstance(index, bool):
            raise LayerSvgError(
                "layer SVG: shape index must be an integer, got {0!r}".format(
                    index))
    wanted = sorted(set(indexes))
    for index in wanted:
        if index < 0 or index >= len(shapes):
            raise LayerSvgError(
                "layer SVG: shape index {0} is out of range (0..{1})".format(
                    index, len(shapes) - 1))
    for index in reversed(wanted):
        root.remove(shapes[index])
    return len(wanted)


def drop_layer_shapes_at(svg, indexes):
    """Return (edited SVG, dropped count), dropping top-level shapes by index."""
    root = parse_layer_svg(svg)
    dropped = drop_root_shapes_at(root, indexes)
    return serialize_layer_svg(root), dropped


def replace_root_shape(root, shape_id, elements):
    """Replace one top-level shape with ``elements`` at its position."""
    old = _root_shape(root, shape_id)
    replacements = [_copy_shape(element) for element in elements]
    index = list(root).index(old)
    root.remove(old)
    for offset, element in enumerate(replacements):
        root.insert(index + offset, element)
    return old


def replace_layer_shape(svg, shape_id, elements):
    """Return the SVG with one top-level shape replaced in place."""
    root = parse_layer_svg(svg)
    replace_root_shape(root, shape_id, elements)
    return serialize_layer_svg(root)


def append_root_shapes(root, elements):
    """Append shapes after the last top-level element (top of the stack)."""
    copies = [_copy_shape(element) for element in elements]
    root.extend(copies)


def append_layer_shapes(svg, elements):
    """Return the SVG with ``elements`` appended at the top of the stack."""
    root = parse_layer_svg(svg)
    append_root_shapes(root, elements)
    return serialize_layer_svg(root)
