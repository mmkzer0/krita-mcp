"""Editable vector shapes and text on vector layers.

Shapes arrive as pixel-coordinate specs, are turned into SVG by
``svgshapes.build_svg`` and handed to ``VectorLayer.addShapesFromSvg``, so
everything stays editable with Krita's own text and shape tools afterwards.

Units: Krita keeps vector shapes in points (1/72 inch). An SVG whose root is
``width="{W}px" viewBox="0 0 {W} {H}"`` maps user units 1:1 to document pixels
(measured on 5.3.4: a 100-unit rect at 300 dpi comes back as 24 pt), and
``boundingBox()`` is converted back with the document's resolution, so every
coordinate this module reports is in canvas pixels.
"""

from . import svgshapes
from .compat import QPointF

MAX_FIT_STEPS = 8
_ALIGN_OFFSET = {"left": 0.0, "center": 0.5, "right": 1.0}
_VALIGN = ("top", "middle", "bottom")
_BOX_KEYS = ("box", "valign", "fit", "min_size")
_GENERIC_FONTS = {"serif", "sans-serif", "monospace", "cursive", "fantasy",
                  "system-ui"}


def _o():
    from . import ops
    return ops


def _err(message, kind="invalid_request"):
    return _o().OpError(message, kind=kind)


class _Units(object):
    """Points <-> canvas pixels for one document."""

    def __init__(self, doc):
        self.sx = (doc.xRes() or doc.resolution() or 72.0) / 72.0
        self.sy = (doc.yRes() or doc.resolution() or 72.0) / 72.0

    def rect(self, rect):
        return {"x": round(rect.x() * self.sx, 1),
                "y": round(rect.y() * self.sy, 1),
                "width": round(rect.width() * self.sx, 1),
                "height": round(rect.height() * self.sy, 1)}


def _vector_layer(doc, ref):
    node = _o().resolve_node(doc, ref)
    if node.type() != "vectorlayer":
        raise _err("Layer {0!r} is a {1}; shapes live on a vector layer. Omit "
                   "`layer` to create one.".format(node.name(), node.type()))
    return node


def _new_vector_layer(doc, name):
    layer = doc.createVectorLayer(name)
    if layer is None:
        raise _err("Krita could not create a vector layer.")
    _o()._insert_node(doc.rootNode(), layer, 0)
    return layer


def _shape_entry(shape, units):
    return {"name": shape.name(), "type": shape.type(),
            "bounds": units.rect(shape.boundingBox()),
            "visible": shape.visible()}


def _installed_families():
    from .compat import QFontDatabase
    try:
        families = QFontDatabase.families()
    except TypeError:  # PyQt5: an instance method
        families = QFontDatabase().families()
    return {f.lower() for f in families}


def _font_warnings(specs):
    wanted = set()
    for spec in specs:
        if spec.get("type") == "text" and isinstance(spec.get("font"), str):
            first = spec["font"].split(",")[0].strip().strip("'\"")
            if first and first.lower() not in _GENERIC_FONTS:
                wanted.add(first)
    if not wanted:
        return []
    installed = _installed_families()
    return ["Font {0!r} is not installed; Krita substitutes another family. "
            "See list_capabilities kind=fonts.".format(f)
            for f in sorted(wanted) if f.lower() not in installed]


def _add(doc, layer, specs, prefix):
    try:
        svg, names = svgshapes.build_svg(specs, doc.width(), doc.height(),
                                         id_prefix=prefix)
    except svgshapes.ShapeSpecError as exc:
        raise _err(str(exc))
    shapes = list(layer.addShapesFromSvg(svg))
    if len(shapes) != len(names):
        for shape in shapes:
            shape.remove()
        raise _err("Krita created {0} shapes from {1} specs; nothing was "
                   "kept.".format(len(shapes), len(names)))
    return shapes, names


def _unique_prefix(layer):
    existing = [s.name() for s in layer.shapes()]
    k = len(existing)
    while any(n.startswith("shape{0}-".format(k)) for n in existing):
        k += 1
    return "shape{0}".format(k)


def _split_box(spec, index):
    """Separate the layout keys this module owns from the builder's spec."""
    o = _o()
    layout = {k: spec[k] for k in _BOX_KEYS if k in spec}
    if not layout:
        return spec, None
    key = "shapes[{0}]".format(index)
    if spec.get("type") != "text":
        raise _err("{0}: box, valign, fit and min_size apply to text only"
                   .format(key))
    if "box" not in layout:
        raise _err("{0}: valign, fit and min_size need a box".format(key))
    if spec.get("vertical"):
        raise _err("{0}: box layout supports horizontal text only".format(key))
    box = layout["box"]
    if not isinstance(box, dict):
        raise _err("{0}.box must be {{x, y, width, height}}".format(key))
    rect = []
    for field in ("x", "y", "width", "height"):
        if field not in box:
            raise _err("{0}.box needs {1}".format(key, field))
        rect.append(o._as_float(box[field], "{0}.box.{1}".format(key, field)))
    if rect[2] <= 0 or rect[3] <= 0:
        raise _err("{0}.box width and height must be positive".format(key))
    valign = layout.get("valign", "middle")
    if valign not in _VALIGN:
        raise _err("{0}.valign must be one of: {1}".format(
            key, ", ".join(_VALIGN)))
    size = o._as_float(spec.get("size", 24), key + ".size")
    min_size = o._as_float(layout.get("min_size", min(8.0, size)),
                           key + ".min_size")
    if not 0 < min_size <= size:
        raise _err("{0}.min_size must be positive and at most size".format(key))
    for field in ("x", "y", "width"):
        if field in spec:
            raise _err("{0}: with a box, x, y and width come from the box"
                       .format(key))
    clean = {k: v for k, v in spec.items() if k not in _BOX_KEYS}
    return clean, {"box": rect, "valign": valign, "size": size,
                   "min_size": min_size,
                   "fit": o._as_bool(layout.get("fit", True), key + ".fit")}


def _fits(bounds, box):
    return (bounds["width"] <= box[2] + 0.5 and
            bounds["height"] <= box[3] + 0.5)


def _place_in_box(doc, layer, spec, layout, name, units):
    """Add one text shape laid out in its box; returns (shape, size, warning).

    Wrapping comes from Krita's own layout (``inline-size`` = box width), so
    the only way to know the height is to lay the text out and measure it:
    with ``fit`` the font size is binary-searched between min_size and size
    for the largest one whose block fits, then the block is moved to the
    vertical alignment.
    """
    x, y, w, h = layout["box"]
    align = spec.get("align", "left")
    base = dict(spec, name=name, width=w,
                x=x + w * _ALIGN_OFFSET.get(align, 0.0))

    def attempt(size):
        trial = dict(base, size=size, y=y + size)
        shapes, _names = _add(doc, layer, [trial], name)
        shape = shapes[0]
        return shape, units.rect(shape.boundingBox())

    size = layout["size"]
    shape, bounds = attempt(size)
    warning = None
    if layout["fit"] and not _fits(bounds, layout["box"]):
        shape.remove()
        lo, hi = layout["min_size"], size
        shape, bounds = attempt(lo)
        size = lo
        if not _fits(bounds, layout["box"]):
            warning = ("Text {0!r} overflows its box even at min_size {1}px."
                       .format(name, lo))
        else:
            for _ in range(MAX_FIT_STEPS):
                if hi - lo < 0.5:
                    break
                mid = round((lo + hi) / 2.0, 2)
                trial, trial_bounds = attempt(mid)
                if _fits(trial_bounds, layout["box"]):
                    shape.remove()
                    shape, bounds, size, lo = trial, trial_bounds, mid, mid
                else:
                    trial.remove()
                    hi = mid
    elif not _fits(bounds, layout["box"]):
        warning = "Text {0!r} overflows its box (fit is off).".format(name)

    if layout["valign"] == "top":
        top = y
    elif layout["valign"] == "bottom":
        top = y + h - bounds["height"]
    else:
        top = y + (h - bounds["height"]) / 2.0
    dy = top - bounds["y"]
    if abs(dy) >= 0.05:
        position = shape.position()
        shape.setPosition(QPointF(position.x(), position.y() + dy / units.sy))
    return shape, size, warning


def _split_specs(specs):
    if not isinstance(specs, list) or not specs:
        raise _err("`shapes` must be a non-empty array of shape specs")
    if len(specs) > 500:
        raise _err("At most 500 shapes per call.")
    split = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise _err("shapes[{0}] must be an object".format(i))
        split.append(_split_box(spec, i))
    return split


def _target_layer(doc, params):
    """(existing vector layer or None, the name the shapes will land on)."""
    if params.get("layer") in (None, ""):
        return None, str(params.get("new_layer_name") or "Vector")
    layer = _vector_layer(doc, params["layer"])
    if layer.locked():
        raise _err("Layer {0!r} is locked; unlock it with set_layer first."
                   .format(layer.name()))
    return layer, layer.name()


def _plan_names(layer, layer_name, split):
    prefix = _unique_prefix(layer) if layer is not None else "shape0"
    existing = {s.name() for s in layer.shapes()} if layer is not None \
        else set()
    names = []
    for i, (spec, _layout) in enumerate(split):
        name = spec.get("name") or "{0}-{1}".format(prefix, i)
        if name in existing or name in names:
            raise _err("shapes[{0}]: a shape named {1!r} already exists on "
                       "{2!r}".format(i, name, layer_name))
        names.append(name)
    return prefix, names


def _check_specs(doc, plain, boxed, names):
    try:
        svgshapes.build_svg([s for _i, s in plain], doc.width(), doc.height())
        for i, spec, _layout in boxed:
            svgshapes.build_svg([dict(spec, name=names[i], x=0, y=0, width=1)],
                                doc.width(), doc.height())
    except svgshapes.ShapeSpecError as exc:
        raise _err(str(exc))


def _apply_shapes(doc, layer, plain, boxed, names, prefix, results, warnings):
    """Add every shape; returns the added shapes. Raises with none added."""
    units = _Units(doc)
    added = []
    try:
        if plain:
            shapes, _names = _add(doc, layer, [s for _i, s in plain], prefix)
            added.extend(shapes)
            for (i, _spec), shape in zip(plain, shapes):
                results[i] = _shape_entry(shape, units)
        for i, spec, layout in boxed:
            shape, size, warning = _place_in_box(doc, layer, spec, layout,
                                                 names[i], units)
            added.append(shape)
            results[i] = dict(_shape_entry(shape, units), font_size=size)
            if warning:
                warnings.append(warning)
    except Exception:
        for shape in added:
            shape.remove()
        raise
    return added


def op_add_vector_shapes(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    split = _split_specs(params.get("shapes"))
    layer, layer_name = _target_layer(doc, params)

    # Everything that can be refused is refused before the document changes.
    prefix, names = _plan_names(layer, layer_name, split)
    plain = [(i, dict(spec, name=names[i]))
             for i, (spec, layout) in enumerate(split) if layout is None]
    boxed = [(i, spec, layout) for i, (spec, layout) in enumerate(split)
             if layout is not None]
    _check_specs(doc, plain, boxed, names)

    created_layer = layer is None
    if created_layer:
        layer = _new_vector_layer(doc, layer_name)
    results = [None] * len(split)
    warnings = _font_warnings([s for s, _l in split])
    try:
        _apply_shapes(doc, layer, plain, boxed, names, prefix, results,
                      warnings)
    except Exception:
        # All or nothing: a half-lettered page is worse than a refusal.
        if created_layer:
            layer.remove()
        raise
    o._refresh(doc)
    result = {"layer": layer.name(), "created_layer": created_layer,
              "shapes": results}
    if warnings:
        result["warnings"] = warnings
    return result


def op_list_vector_shapes(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    layer = _vector_layer(doc, params.get("layer"))
    units = _Units(doc)
    include_svg = o._as_bool(params.get("include_svg", False), "include_svg")
    shapes = []
    for shape in layer.shapes():
        entry = _shape_entry(shape, units)
        if include_svg:
            entry["svg"] = shape.toSvg()
        shapes.append(entry)
    return {"layer": layer.name(), "count": len(shapes), "shapes": shapes}


def op_remove_vector_shapes(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    layer = _vector_layer(doc, params.get("layer"))
    names = params.get("names")
    if not isinstance(names, list) or not names or \
            not all(isinstance(n, str) for n in names):
        raise _err("`names` must be a non-empty array of shape names")
    by_name = {}
    for shape in layer.shapes():
        by_name.setdefault(shape.name(), []).append(shape)
    missing = [n for n in names if n not in by_name]
    if missing:
        raise _err("No shape named {0} on {1!r}; nothing was removed. Call "
                   "list_vector_shapes for the names.".format(
                       ", ".join(repr(n) for n in missing), layer.name()),
                   kind="not_found")
    removed = 0
    for name in dict.fromkeys(names):
        for shape in by_name[name]:
            shape.remove()
            removed += 1
    o._refresh(doc)
    return {"layer": layer.name(), "removed": removed,
            "remaining": len(layer.shapes())}


def register(op):
    op("add_vector_shapes", timeout=120.0, mutates=True)(op_add_vector_shapes)
    op("list_vector_shapes", timeout=20.0)(op_list_vector_shapes)
    op("remove_vector_shapes", timeout=60.0, mutates=True)(
        op_remove_vector_shapes)
