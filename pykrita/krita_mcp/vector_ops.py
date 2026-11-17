"""Editable vector shapes and text on vector layers.

Shapes arrive as pixel-coordinate specs, are turned into SVG by
``svgshapes.build_svg`` and inserted with ``VectorLayer.addShapesFromSvg``, so
everything stays editable with Krita's own text and shape tools afterwards.

Units: Krita keeps vector shapes in points (1/72 inch). An SVG whose root is
``width="{W}pt" viewBox="0 0 W H"`` maps user units 1:1 to document points
(measured on 5.3.4: a 1200 px document at 300 dpi is 288 pt), and
``boundingBox()`` is converted back with the document's resolution, so every
coordinate this module reports is in canvas pixels.

Layer edits never call ``Shape.remove()``. libkis resolves it through
``Krita.activeDocument()`` instead of the shape's own document, so it crashes
when no document is active (only view-less documents open) and otherwise
removes through the active document's view and undo (F31). Instead a layer edit
copies the layer's own ``toSvg()``, drops/replaces/splices elements in that
string (document order is stacking order) and replays it into a fresh vector
layer with ``_rebuild_layer``, which verifies the attach and the removal of
the original so a failure never leaves zero or two copies. A rebuild gives
the layer a new uuid, which the results report as ``layer_rebuilt``, and
destroys child nodes (masks), so a layer that has any is refused. Box-fit
trials run on a scratch vector layer that is discarded with
``Node.remove()``; ``Node.remove()`` does not have the F31 problem.
"""

import math

from . import svgshapes
from .compat import QPointF

MAX_FIT_STEPS = 8
MAX_UPDATES = 200
_ALIGN_OFFSET = {"left": 0.0, "center": 0.5, "right": 1.0}
_VALIGN = ("top", "middle", "bottom")
_BOX_KEYS = ("box", "valign", "fit", "min_size")
_UPDATE_KEYS = ("name", "shape", "translate")
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


def _is_child(parent, node):
    uuid = _o()._node_uuid(node)
    for child in parent.childNodes():
        if uuid:
            if _o()._node_uuid(child) == uuid:
                return True
        elif child == node:
            return True
    return False


def _new_vector_layer(doc, name):
    layer = doc.createVectorLayer(name)
    if layer is None:
        raise _err("Krita could not create a vector layer.")
    root = doc.rootNode()
    _o()._insert_node(root, layer, 0)
    if not _is_child(root, layer):
        raise _err("Krita did not attach the new vector layer {0!r} to the "
                   "document.".format(name), kind="internal")
    return layer


def _new_scratch_layer(doc):
    """A throwaway vector layer for box-fit trials; deleted by the caller."""
    return _new_vector_layer(doc, "mcp-scratch")


def _remove_layer(layer, purpose):
    """Node.remove() a layer this module created (scratch, failed rebuild).

    Returns True when Krita reports it removed; a False return or an
    exception is logged and returns False so the caller can decide whether
    the leftover is user-visible. Replacing a user's layer is
    ``_rebuild_layer``'s job, never this function's.
    """
    name = layer.name()
    try:
        removed = layer.remove()
    except Exception as exc:  # keep the operation's own error visible
        _o()._log("could not remove {0} layer {1!r}: {2}".format(
            purpose, name, exc))
        return False
    if removed is False:
        _o()._log("Krita refused to remove {0} layer {1!r}".format(
            purpose, name))
        return False
    return True


def _refuse_children(layer, action):
    """A vector-layer rebuild destroys masks; refuse before anything changes."""
    children = list(layer.childNodes())
    if children:
        raise _err(
            "Layer {0!r} has child node(s) {1}; {2} rebuilds the vector layer "
            "and would destroy them. Nothing was changed. Delete or move the "
            "child nodes first (delete_layer / move_layer), then retry and "
            "re-create them.".format(
                layer.name(),
                ", ".join("{0!r} ({1})".format(c.name(), c.type())
                          for c in children),
                action))


def _finite(value, key):
    try:
        number = _o()._as_float(value, key)
    except OverflowError:
        raise _err("{0} must be a finite number".format(key))
    if not math.isfinite(number):
        raise _err("{0} must be a finite number".format(key))
    return number


def _layer_snapshot(layer):
    """(shape names, layer SVG) in ``layer.shapes()`` order.

    Shapes are addressed by position, never by SVG id: Krita's export
    uniquifies duplicate names ("x", "x1", "x2" for shapes named "x", "x1",
    "x") while the import keeps ids verbatim, so the exported ids are written
    back as the true names. The export must hold exactly one top-level
    element per shape or the layer is refused.
    """
    names = [shape.name() for shape in layer.shapes()]
    svg = layer.toSvg()
    if not svg.strip():
        if names:
            raise _err("Krita reports {0} shapes on layer {1!r} but exports "
                       "an empty SVG; nothing was changed. This is a Krita "
                       "inconsistency -- reopen the document and retry."
                       .format(len(names), layer.name()))
        return names, ""
    try:
        root = svgshapes.parse_layer_svg(svg)
        count = len(svgshapes.layer_shape_elements(root))
        if count != len(names):
            raise _err("Krita reports {0} shapes on layer {1!r} but exports "
                       "{2} top-level shapes in its SVG; nothing was changed. "
                       "This is a Krita inconsistency -- reopen the document "
                       "and retry.".format(len(names), layer.name(), count))
        svgshapes.set_root_shape_ids(root, names)
    except svgshapes.LayerSvgError as exc:
        raise _err("Could not read layer {0!r}'s SVG: {1}; nothing was "
                   "changed.".format(layer.name(), exc))
    return names, svgshapes.serialize_layer_svg(root)


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
        raise _err("Krita created {0} shapes from {1} specs; the call was "
                   "rolled back.".format(len(shapes), len(names)))
    return shapes, names


def _unique_prefix(layer):
    existing = [s.name() for s in layer.shapes()]
    k = len(existing)
    while any(n.startswith("shape{0}-".format(k)) for n in existing):
        k += 1
    return "shape{0}".format(k)


def _split_box(spec, index, key=None):
    """Separate the layout keys this module owns from the builder's spec."""
    o = _o()
    layout = {k: spec[k] for k in _BOX_KEYS if k in spec}
    if not layout:
        return spec, None
    key = key or "shapes[{0}]".format(index)
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
        rect.append(_finite(box[field], "{0}.box.{1}".format(key, field)))
    if rect[2] <= 0 or rect[3] <= 0:
        raise _err("{0}.box width and height must be positive".format(key))
    valign = layout.get("valign", "middle")
    if valign not in _VALIGN:
        raise _err("{0}.valign must be one of: {1}".format(
            key, ", ".join(_VALIGN)))
    size = _finite(spec.get("size", 24), key + ".size")
    min_size = _finite(layout.get("min_size", min(8.0, size)),
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


def _trial_name(prefix, number, reserved):
    name = "{0}-{1}".format(prefix, number)
    while name in reserved:
        name = "_" + name
    return name


def _place_in_box(doc, layer, spec, layout, name, units, prefix, reserved):
    """Lay one text shape out on ``layer``; returns (shape, size, warning).

    Trials stay on ``layer`` (a scratch layer) instead of being removed:
    Shape.remove() is unusable (F31). The final shape carries ``name`` and is
    the one the caller extracts from the scratch layer's SVG.

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
    counter = [0]

    def attempt(size, shape_name):
        trial = dict(base, name=shape_name, size=size, y=y + size)
        shapes, _names = _add(doc, layer, [trial], prefix)
        return shapes[0], units.rect(shapes[0].boundingBox())

    def trial(size):
        counter[0] += 1
        return attempt(size, _trial_name(prefix, counter[0], reserved))

    size = layout["size"]
    _trial_shape, bounds = trial(size)
    best_size = size
    warning = None
    if layout["fit"] and not _fits(bounds, layout["box"]):
        lo, hi = layout["min_size"], size
        _trial_shape, bounds = trial(lo)
        best_size = lo
        if not _fits(bounds, layout["box"]):
            warning = ("Text {0!r} overflows its box even at min_size {1}px."
                       .format(name, lo))
        else:
            for _ in range(MAX_FIT_STEPS):
                if hi - lo < 0.5:
                    break
                mid = round((lo + hi) / 2.0, 2)
                _trial_shape, trial_bounds = trial(mid)
                if _fits(trial_bounds, layout["box"]):
                    best_size, lo = mid, mid
                else:
                    hi = mid
    elif not _fits(bounds, layout["box"]):
        warning = "Text {0!r} overflows its box (fit is off).".format(name)

    shape, bounds = attempt(best_size, name)
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
    return shape, best_size, warning


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


def _check_specs(doc, plain, boxed, names, labels=None):
    """Validate builder specs before anything is created.

    ``labels`` (an index -> error prefix function) is for update_vector_shapes:
    each spec is checked on its own so its refusal names that update. Add keeps
    its historical combined check, and therefore its historical messages.
    """
    try:
        if labels is None:
            svgshapes.build_svg([s for _i, s in plain], doc.width(),
                                doc.height())
            for i, spec, _layout in boxed:
                svgshapes.build_svg(
                    [dict(spec, name=names[i], x=0, y=0, width=1)],
                    doc.width(), doc.height())
            return
        for i, spec in plain:
            _check_one_spec(doc, spec, labels(i))
        for i, spec, _layout in boxed:
            _check_one_spec(
                doc, dict(spec, name=names[i], x=0, y=0, width=1), labels(i))
    except svgshapes.ShapeSpecError as exc:
        raise _err(str(exc))


def _check_one_spec(doc, spec, key):
    try:
        svgshapes.build_svg([spec], doc.width(), doc.height())
    except svgshapes.ShapeSpecError as exc:
        message = str(exc)
        prefix = "shape 0: "
        if message.startswith(prefix):
            message = message[len(prefix):]
        raise _err("{0}: {1}".format(key, message))


def _ui_index(parent, node):
    """Top-first docker position of ``node`` among ``parent``'s children."""
    uuid = _o()._node_uuid(node)
    for index, child in enumerate(reversed(list(parent.childNodes()))):
        if uuid and _o()._node_uuid(child) == uuid:
            return index
        if not uuid and child.name() == node.name():
            return index
    return 0


def _is_active(doc, layer):
    try:
        return doc.activeNode() == layer
    except Exception:
        return False


def _discard_new_layer(parent, new_layer, name):
    """Take back a rebuilt copy; raise if it cannot be removed."""
    _remove_layer(new_layer, "incomplete rebuild")
    try:
        left = _is_child(parent, new_layer)
    except Exception:
        left = True
    if left:
        raise _err("Krita could not discard the incomplete rebuilt copy of "
                   "layer {0!r}; the document now holds two layers named "
                   "{0!r}. Delete the incomplete one (the one without all "
                   "the shapes) with delete_layer.".format(name),
                   kind="internal")


def _rebuild_layer(doc, layer, svg, names):
    """Replace ``layer`` by a fresh vector layer holding ``svg``; return it.

    ``names`` are the shape names the new layer must hold, in order.
    Shape.remove() is unusable (F31), so a layer edit is replayed as SVG.
    Names, types, stacking order (document order in the SVG) and bounds are
    reproduced; the layer's uuid changes. All or nothing: the fresh layer must
    be attached and hold exactly ``names`` before the original is removed, and
    the original must be gone afterwards. Any failure before that point
    removes the fresh layer and leaves the original untouched.
    """
    _refuse_children(layer, "a rebuild")
    try:
        count = (svgshapes.layer_shape_count(svg) if svg.strip() else 0)
    except svgshapes.LayerSvgError as exc:
        raise _err("Could not rebuild layer {0!r}: {1}; nothing was changed."
                   .format(layer.name(), exc))
    if count != len(names):
        raise _err("Could not rebuild layer {0!r}: the SVG holds {1} shapes "
                   "but {2} were expected; nothing was changed.".format(
                       layer.name(), count, len(names)))
    parent = _o()._parent_of(doc, layer)
    ui_index = _ui_index(parent, layer)
    new_layer = doc.createVectorLayer(layer.name())
    if new_layer is None:
        raise _err("Krita could not create a vector layer to rebuild {0!r}."
                   .format(layer.name()))
    try:
        _o()._insert_node(parent, new_layer, ui_index)
        if not _is_child(parent, new_layer):
            raise _err("Krita did not attach the rebuilt copy of layer {0!r}; "
                       "the layer was left unchanged. Retry the call."
                       .format(layer.name()), kind="internal")
        shapes = list(new_layer.addShapesFromSvg(svg)) if names else []
        got = [shape.name() for shape in new_layer.shapes()]
        if got != names and len(got) == len(names):
            # One import renames repeated ids (measured: "p", "p" -> "p",
            # "p_40e972bd"), but a layer may legitimately hold two shapes
            # with the same name; put the names back by position.
            for shape, wanted in zip(new_layer.shapes(), names):
                if shape.name() != wanted:
                    shape.setName(wanted)
            got = [shape.name() for shape in new_layer.shapes()]
        if len(shapes) != len(names) or got != names:
            raise _err("Krita rebuilt {0} of {1} shapes of layer {2!r} from "
                       "its own SVG (names {3} instead of {4}); the layer was "
                       "left unchanged. This is a Krita bug; retry the call."
                       .format(len(shapes), len(names), layer.name(), got,
                               names))
        new_layer.setOpacity(layer.opacity())
        new_layer.setVisible(layer.visible())
        new_layer.setBlendingMode(layer.blendingMode())
        new_layer.setInheritAlpha(layer.inheritAlpha())
        new_layer.setColorLabel(layer.colorLabel())
        new_layer.setCollapsed(layer.collapsed())
        # A lettering layer typically carries its outline as a layer style
        # (set_layer_style); losing it on every text edit would be silent.
        style = layer.layerStyleToAsl()
        if style and not new_layer.setLayerStyleFromAsl(style):
            raise _err("Krita refused to copy the layer style of {0!r}; the "
                       "layer was left unchanged.".format(layer.name()))
        new_layer.setLocked(layer.locked())
        was_active = _is_active(doc, layer)
        # Last step that can fail: nothing below may raise, or the cleanup
        # would take the only remaining copy away.
        try:
            layer.remove()
        except Exception as exc:
            _o()._log("removing the original of rebuilt layer {0!r} "
                      "raised: {1}".format(layer.name(), exc))
        if _is_child(parent, layer):
            raise _err("Krita could not remove the original layer {0!r}; the "
                       "rebuilt copy was discarded and the layer was left "
                       "unchanged.".format(layer.name()), kind="internal")
    except Exception:
        try:
            original_present = _is_child(parent, layer)
        except Exception:
            original_present = True
        if original_present:
            _discard_new_layer(parent, new_layer, layer.name())
        raise
    if was_active:
        try:
            doc.setActiveNode(new_layer)
        except Exception as exc:  # the edit is applied; selection is cosmetic
            _o()._log("could not re-select rebuilt layer {0!r}: {1}".format(
                layer.name(), exc))
    return new_layer


def _translate_delta(value, key):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _err("{0} must be [dx, dy] in canvas pixels".format(key))
    delta = []
    for axis, number in enumerate(value):
        if isinstance(number, bool):
            raise _err("{0}[{1}] must be a number, got {2!r}".format(
                key, axis, number))
        try:
            number = _o()._as_float(number, "{0}[{1}]".format(key, axis))
        except OverflowError:
            raise _err("{0}[{1}] must be finite".format(key, axis))
        if not math.isfinite(number):
            raise _err("{0}[{1}] must be finite".format(key, axis))
        delta.append(number)
    return tuple(delta)


def _split_updates(updates):
    """Validate ``updates`` and split each replacement's box layout.

    Pure except for the argument coercers reached through ``_o()``: every
    refusal happens here, before the layer is read or changed.
    """
    if not isinstance(updates, list) or not updates:
        raise _err("`updates` must be a non-empty array of updates")
    if len(updates) > MAX_UPDATES:
        raise _err("At most {0} updates per call; got {1}. Split the call."
                   .format(MAX_UPDATES, len(updates)))
    split = []
    seen = {}
    for i, update in enumerate(updates):
        key = "updates[{0}]".format(i)
        if not isinstance(update, dict):
            raise _err("{0} must be an object with name and shape or translate"
                       .format(key))
        unknown = sorted(str(k) for k in set(update) - set(_UPDATE_KEYS))
        if unknown:
            raise _err("{0}: unknown key(s) {1}; allowed keys: name, shape, "
                       "translate".format(key, ", ".join(unknown)))
        name = update.get("name")
        if not isinstance(name, str) or not name:
            raise _err("{0}.name must be a non-empty string".format(key))
        if name in seen:
            raise _err("{0}.name {1!r} already appears in updates[{2}]; a "
                       "shape may be updated once per call.".format(
                           key, name, seen[name]))
        seen[name] = i
        has_shape = update.get("shape") is not None
        has_translate = update.get("translate") is not None
        if has_shape == has_translate:
            raise _err("{0}: give exactly one of `shape` (replace the shape) "
                       "or `translate` ([dx, dy], move it); {1} were given."
                       .format(key, "both" if has_shape else "neither"))
        if has_translate:
            split.append({"name": name, "kind": "translate", "spec": None,
                          "layout": None,
                          "delta": _translate_delta(update["translate"],
                                                    key + ".translate")})
            continue
        spec = update["shape"]
        if not isinstance(spec, dict):
            raise _err("{0}.shape must be an object".format(key))
        if "name" in spec and spec["name"] != name:
            raise _err("{0}.shape.name is {1!r} but the update name is {2!r}; "
                       "omit shape.name or make them equal.".format(
                           key, spec["name"], name))
        clean, layout = _split_box(spec, i, key=key + ".shape")
        split.append({"name": name, "kind": "shape", "spec": clean,
                      "layout": layout, "delta": None})
    return split


def _layer_shapes_by_name(layer, names, layer_name):
    """Map each wanted name to its single shape, or refuse before any edit."""
    by_name = {}
    for shape in layer.shapes():
        by_name.setdefault(shape.name(), []).append(shape)
    missing = [n for n in names if n not in by_name]
    if missing:
        raise _err("No shape named {0} on {1!r}; nothing was updated. Call "
                   "list_vector_shapes for the names.".format(
                       ", ".join(repr(n) for n in missing), layer_name),
                   kind="not_found")
    duplicated = [n for n in names if len(by_name[n]) > 1]
    if duplicated:
        detail = ", ".join("{0!r} ({1} copies)".format(n, len(by_name[n]))
                           for n in duplicated)
        raise _err("Shape name(s) {0} appear more than once on {1!r}; "
                   "update_vector_shapes needs exactly one shape per name. "
                   "Remove the extra copies with remove_vector_shapes, then "
                   "retry.".format(detail, layer_name), kind="ambiguous")
    return {n: by_name[n][0] for n in names}


def _translate_shape(shape, delta, units):
    # Measured on 5.3.4: position()/setPosition() are in document points and
    # translate a rotated text exactly (the linear part of the transformation
    # is unchanged), so no SVG edit or rebuild is needed to move a shape.
    position = shape.position()
    shape.setPosition(QPointF(position.x() + delta[0] / units.sx,
                              position.y() + delta[1] / units.sy))


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
    if boxed and layer is not None:
        _refuse_children(layer, "add_vector_shapes with a box")

    created_layer = layer is None
    if created_layer:
        layer = _new_vector_layer(doc, layer_name)
    before_names, before_svg = (None, None) if created_layer \
        else _layer_snapshot(layer)
    units = _Units(doc)
    warnings = _font_warnings([s for s, _layout in split])
    results = [None] * len(split)
    actual = list(names)
    fitted = {}
    scratch = None
    rebuilt = False
    mutated = False  # the user's layer was touched; rollback must rebuild it
    try:
        if plain:
            mutated = True
            shapes, made = _add(doc, layer, [s for _i, s in plain], prefix)
            for (i, _spec), shape, shape_name in zip(plain, shapes, made):
                actual[i] = shape_name
                results[i] = _shape_entry(shape, units)
        if boxed:
            scratch = _new_scratch_layer(doc)
            reserved = set(names)
            for i, spec, layout in boxed:
                shape, size, warning = _place_in_box(
                    doc, scratch, spec, layout, names[i], units,
                    "fit{0}".format(i), reserved)
                fitted[i] = size
                actual[i] = shape.name()
                if warning:
                    warnings.append(warning)
            scratch_svg = scratch.toSvg()
            try:
                scratch_root = svgshapes.parse_layer_svg(scratch_svg)
                names_now, layer_svg = _layer_snapshot(layer)
                # An empty layer (new, or emptied) exports "".
                root = (svgshapes.parse_layer_svg(layer_svg)
                        if layer_svg.strip()
                        else svgshapes.empty_root_like(scratch_svg))
                for i, _spec, _layout in boxed:
                    svgshapes.append_root_shapes(
                        root, [svgshapes.root_shape_element(scratch_root,
                                                            actual[i])])
            except svgshapes.LayerSvgError as exc:
                raise _err("Could not edit layer {0!r}'s SVG: {1}; the call "
                           "was rolled back.".format(layer.name(), exc))
            layer = _rebuild_layer(
                doc, layer, svgshapes.serialize_layer_svg(root),
                names_now + [actual[i] for i, _spec, _layout in boxed])
            rebuilt = True
            mutated = True
            by_name = {s.name(): s for s in layer.shapes()}
            for i in range(len(split)):
                shape = by_name.get(actual[i])
                if shape is None:
                    raise _err("Krita's rebuilt layer {0!r} lost shape {1!r}; "
                               "the call was rolled back.".format(
                                   layer.name(), actual[i]))
                entry = _shape_entry(shape, units)
                if i in fitted:
                    entry["font_size"] = fitted[i]
                results[i] = entry
    except Exception:
        # All or nothing: a half-lettered page is worse than a refusal.
        if created_layer:
            _remove_layer(layer, "rolled-back new")
        elif mutated and before_svg is not None:
            try:
                _rebuild_layer(doc, layer, before_svg, before_names)
            except Exception as exc:
                o._log("rollback of add_vector_shapes failed: {0}".format(exc))
        raise
    finally:
        scratch_gone = scratch is None or _remove_layer(scratch, "scratch")
    if not scratch_gone:
        raise _err("The shapes were added, but Krita could not remove the "
                   "temporary layer 'mcp-scratch'; delete it with "
                   "delete_layer.", kind="internal")
    o._refresh(doc)
    result = {"layer": layer.name(), "created_layer": created_layer,
              "layer_rebuilt": rebuilt, "shapes": results}
    if warnings:
        result["warnings"] = warnings
    return result


def op_update_vector_shapes(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    layer_ref = params.get("layer")
    if layer_ref in (None, ""):
        raise _err("`layer` is required; pass the vector layer that holds the "
                   "shapes.")
    layer = _vector_layer(doc, layer_ref)
    if layer.locked():
        raise _err("Layer {0!r} is locked; unlock it with set_layer first."
                   .format(layer.name()))
    plan = _split_updates(params.get("updates"))
    names = [item["name"] for item in plan]
    # One pass over the layer: missing and ambiguous names are both refusals,
    # and both happen before anything is created.
    _layer_shapes_by_name(layer, names, layer.name())
    plain = [(i, dict(item["spec"], name=item["name"]))
             for i, item in enumerate(plan)
             if item["kind"] == "shape" and item["layout"] is None]
    boxed = [(i, item["spec"], item["layout"])
             for i, item in enumerate(plan)
             if item["kind"] == "shape" and item["layout"] is not None]
    _check_specs(doc, plain, boxed, names,
                 labels=lambda i: "updates[{0}].shape".format(i))

    shape_indexes = [i for i, item in enumerate(plan)
                     if item["kind"] == "shape"]
    before_names = before_svg = positions = None
    if shape_indexes:
        # Replacing a shape rebuilds the layer; a pure translate does not.
        _refuse_children(layer, "update_vector_shapes with a replacement")
        before_names, before_svg = _layer_snapshot(layer)
        # Names are unique for every targeted shape (checked above), and the
        # position in layer.shapes() is the key the SVG edit uses.
        positions = {name: index for index, name in enumerate(before_names)}
    units = _Units(doc)
    warnings = _font_warnings([item["spec"] for item in plan
                               if item["kind"] == "shape"])
    results = [None] * len(plan)
    actual = list(names)
    fitted = {}
    scratch = None
    rebuilt = False
    translated = []
    try:
        if shape_indexes:
            scratch = _new_scratch_layer(doc)
            reserved = set(names)
            for i in shape_indexes:
                item = plan[i]
                if item["layout"] is None:
                    shapes, made = _add(
                        doc, scratch, [dict(item["spec"], name=item["name"])],
                        "upd{0}".format(i))
                    actual[i] = made[0]
                else:
                    shape, size, warning = _place_in_box(
                        doc, scratch, item["spec"], item["layout"],
                        item["name"], units, "upd{0}".format(i), reserved)
                    actual[i] = shape.name()
                    fitted[i] = size
                    if warning:
                        warnings.append(warning)
                if actual[i] != item["name"]:
                    raise _err(
                        "Shape {0!r} cannot keep its name through an SVG "
                        "rebuild: the builder would create {1!r}. Rename the "
                        "shape in Krita first.".format(item["name"],
                                                       actual[i]))
            scratch_svg = scratch.toSvg()
            try:
                scratch_root = svgshapes.parse_layer_svg(scratch_svg)
                root = svgshapes.parse_layer_svg(before_svg)
                for i in shape_indexes:
                    svgshapes.replace_root_shape_at(
                        root, positions[names[i]],
                        [svgshapes.root_shape_element(scratch_root,
                                                      actual[i])])
            except svgshapes.LayerSvgError as exc:
                raise _err("Could not edit layer {0!r}'s SVG: {1}; the call "
                           "was rolled back.".format(layer.name(), exc))
            layer = _rebuild_layer(doc, layer,
                                   svgshapes.serialize_layer_svg(root),
                                   before_names)
            rebuilt = True
        current = {s.name(): s for s in layer.shapes()}
        for i, item in enumerate(plan):
            if item["kind"] != "translate":
                continue
            shape = current.get(item["name"])
            if shape is None:
                raise _err("Shape {0!r} disappeared before it could be moved; "
                           "the call was rolled back.".format(item["name"]))
            translated.append((shape, shape.position()))
            _translate_shape(shape, item["delta"], units)
        for i, item in enumerate(plan):
            shape = current.get(item["name"])
            if shape is None:
                raise _err("Shape {0!r} is missing after the update; the call "
                           "was rolled back.".format(item["name"]))
            entry = {"name": item["name"],
                     "bounds": units.rect(shape.boundingBox()),
                     "replaced": item["kind"] == "shape"}
            if i in fitted:
                entry["fitted_size"] = fitted[i]
            results[i] = entry
        o._refresh(doc)
    except Exception:
        if rebuilt:
            try:
                _rebuild_layer(doc, layer, before_svg, before_names)
            except Exception as exc:
                o._log("rollback of update_vector_shapes failed: {0}".format(
                    exc))
        else:
            for shape, position in reversed(translated):
                try:
                    shape.setPosition(position)
                except Exception as exc:
                    o._log("could not restore a translated shape: {0}".format(
                        exc))
        raise
    finally:
        scratch_gone = scratch is None or _remove_layer(scratch, "scratch")
    if not scratch_gone:
        raise _err("The shapes were updated, but Krita could not remove the "
                   "temporary layer 'mcp-scratch'; delete it with "
                   "delete_layer.", kind="internal")
    result = {"layer": layer.name(), "layer_rebuilt": rebuilt,
              "updated": results}
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
    if layer.locked():
        raise _err("Layer {0!r} is locked; unlock it with set_layer first."
                   .format(layer.name()))
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
    wanted = set(names)
    _refuse_children(layer, "remove_vector_shapes")
    shape_names, svg = _layer_snapshot(layer)
    indexes = [i for i, name in enumerate(shape_names) if name in wanted]
    try:
        svg, removed = svgshapes.drop_layer_shapes_at(svg, indexes)
    except svgshapes.LayerSvgError as exc:
        raise _err("Could not edit layer {0!r}'s SVG: {1}; nothing was "
                   "removed.".format(layer.name(), exc))
    kept = set(indexes)
    layer = _rebuild_layer(
        doc, layer, svg,
        [name for i, name in enumerate(shape_names) if i not in kept])
    o._refresh(doc)
    return {"layer": layer.name(), "removed": removed,
            "remaining": len(layer.shapes()), "layer_rebuilt": True}


def register(op):
    op("add_vector_shapes", timeout=120.0, mutates=True)(op_add_vector_shapes)
    op("list_vector_shapes", timeout=20.0)(op_list_vector_shapes)
    op("remove_vector_shapes", timeout=60.0, mutates=True)(
        op_remove_vector_shapes)
    op("update_vector_shapes", timeout=120.0, mutates=True)(
        op_update_vector_shapes)
