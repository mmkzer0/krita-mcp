"""Brush strokes, layer transforms, selection masks and selection fills.

These operations are registered into ``ops.OPS`` by ``register()``, which
ops.py calls at import time, so ``importlib.reload(krita_mcp.ops)`` rebuilds
them together with the core set. Helpers are looked up through the ``ops``
module at call time for the same reason.

Two write paths, deliberately distinct:

* ``paint_strokes`` and ``transform_layer`` go through Krita's own stroke and
  transform machinery: brush engines, undo history, any colour space.
* ``fill`` writes RGBA/U8 pixels directly, like ``draw``: exact and
  deterministic, but not recorded in the undo history.
"""

import math

from . import imaging, raster
from .compat import (
    QByteArray,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPoint,
    QPointF,
    QPolygonF,
    QRect,
    QRectF,
)

MAX_STROKES = 200
MAX_POINTS = 5000
MAX_POLYGON_POINTS = 5000
# Native coordinates are ints; anything this far off-canvas is a mistake and
# must be refused before the first edit, not after half of them.
MAX_COORD = 1000000
# Full-canvas selection work holds several buffers of this many bytes.
MAX_SAMPLE_PIXELS = 40000000
COMBINE_MODES = ("replace", "add", "subtract", "intersect")
_TRANSFORMABLE = ("paintlayer", "grouplayer", "vectorlayer", "filelayer",
                  "clonelayer", "filllayer", "filterlayer")


def _o():
    from . import ops
    return ops


def _err(message, kind="invalid_request"):
    return _o().OpError(message, kind=kind)


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------

def _require_editable(node, what):
    if node.locked():
        raise _err("Layer {0!r} is locked; unlock it with set_layer before "
                   "{1}.".format(node.name(), what))


def _require_paint_layer(node, what):
    if node.type() != "paintlayer":
        raise _err("Layer {0!r} is a {1}; {2} needs a paint layer. Create one "
                   "with create_layer.".format(node.name(), node.type(), what))
    _require_editable(node, what)


def _rect_dict(rect):
    return {"x": rect.x(), "y": rect.y(),
            "width": rect.width(), "height": rect.height()}


def _canvas(doc):
    return QRect(0, 0, doc.width(), doc.height())


def _region_rect(doc, region, key="region"):
    o = _o()
    if not isinstance(region, dict):
        raise _err("{0} must be an object with x, y, width, height".format(key))
    rect = QRect(o._as_int(region.get("x", 0), key + ".x"),
                 o._as_int(region.get("y", 0), key + ".y"),
                 o._as_int(o._req(region, "width"), key + ".width"),
                 o._as_int(o._req(region, "height"), key + ".height"))
    try:
        return imaging.clamp_rect(rect, _canvas(doc))
    except imaging.ImagingError as exc:
        raise _err(str(exc))


def _point(item, key):
    o = _o()
    if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
        raise _err("{0} must be [x, y] or [x, y, pressure], got {1!r}"
                   .format(key, item))
    x = o._as_float(item[0], key + "[0]")
    y = o._as_float(item[1], key + "[1]")
    pressure = None
    if len(item) == 3:
        pressure = o._as_float(item[2], key + "[2]")
        if not 0.0 <= pressure <= 1.0:
            raise _err("{0}: pressure must be between 0 and 1".format(key))
    if not (math.isfinite(x) and math.isfinite(y)):
        raise _err("{0}: coordinates must be finite".format(key))
    if abs(x) > MAX_COORD or abs(y) > MAX_COORD:
        raise _err("{0}: coordinates must be within +/-{1}".format(
            key, MAX_COORD))
    return x, y, pressure


def _gray_bytes(img):
    """Tightly packed rows of a Grayscale8 QImage (Qt pads rows to 4 bytes)."""
    width, height = img.width(), img.height()
    ptr = img.constBits()
    stride = img.bytesPerLine()
    ptr.setsize(stride * height)
    raw = bytes(ptr)
    if stride == width:
        return raw
    return b"".join(raw[row * stride:row * stride + width]
                    for row in range(height))


# --------------------------------------------------------------------------
# brush strokes
# --------------------------------------------------------------------------

def _pick_view(krita, doc):
    """A view whose resource settings brush strokes will use, or None.

    Brush preset, size and colour are canvas resources: libkis only sets them
    through a View, and Krita applies the current ones to every stroke --
    including strokes on a view-less document (measured on 5.3.4). Prefer a
    view of this document, then the active one, then any.
    """
    views = [v for v in krita.views() if v.document() is not None]
    for view in views:
        if view.document() == doc:
            return view
    window = krita.activeWindow()
    active = window.activeView() if window is not None else None
    # An empty window still hands out a View wrapper, with no document and no
    # canvas resources behind it.
    if active is not None and active.document() is not None:
        return active
    return views[0] if views else None


def _find_preset(krita, name):
    presets = krita.resources("preset")
    if name in presets:
        return presets[name]
    low = name.lower()
    exact = [n for n in presets if n.lower() == low]
    if len(exact) == 1:
        return presets[exact[0]]
    partial = sorted(n for n in presets if low in n.lower())
    if len(partial) == 1:
        return presets[partial[0]]
    if not partial:
        raise _err("No brush preset matches {0!r}. Call list_capabilities "
                   "with kind=brush_presets for the names.".format(name),
                   kind="not_found")
    raise _err("{0!r} matches {1} brush presets: {2}{3}. Use the full name."
               .format(name, len(partial), ", ".join(partial[:12]),
                       ", ..." if len(partial) > 12 else ""))


def _managed_color(doc, qcolor):
    from krita import ManagedColor
    color = ManagedColor.fromQColor(qcolor)
    return color


class _BrushResources(object):
    """Apply brush settings to a view and put the user's back afterwards."""

    def __init__(self, view):
        self.view = view
        self.saved = None

    def apply(self, preset=None, size=None, color=None, opacity=None,
              erase=None):
        view = self.view
        self.saved = {
            "preset": view.currentBrushPreset(),
            "size": view.brushSize(),
            "color": view.foregroundColor(),
            "opacity": view.paintingOpacity(),
            "erase": view.eraserMode(),
        }
        # A preset carries its own size, so it goes first.
        if preset is not None:
            view.setCurrentBrushPreset(preset)
        if size is not None:
            view.setBrushSize(size)
        if color is not None:
            view.setForeGroundColor(color)
        if opacity is not None:
            view.setPaintingOpacity(opacity)
        if erase is not None:
            view.setEraserMode(erase)

    def restore(self):
        if self.saved is None:
            return
        view, saved = self.view, self.saved
        self.saved = None
        if saved["preset"] is not None:
            view.setCurrentBrushPreset(saved["preset"])
        view.setBrushSize(saved["size"])
        if saved["color"] is not None:
            view.setForeGroundColor(saved["color"])
        view.setPaintingOpacity(saved["opacity"])
        view.setEraserMode(saved["erase"])


def _smooth_path(points):
    """Catmull-Rom through every point, as cubic Beziers."""
    path = QPainterPath(QPointF(points[0][0], points[0][1]))
    if len(points) == 2:
        path.lineTo(QPointF(points[1][0], points[1][1]))
        return path
    pts = [points[0]] + list(points) + [points[-1]]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        c1 = QPointF(p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = QPointF(p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        path.cubicTo(c1, c2, QPointF(p2[0], p2[1]))
    return path


def _polyline_path(points):
    path = QPainterPath(QPointF(points[0][0], points[0][1]))
    for x, y in points[1:]:
        path.lineTo(QPointF(x, y))
    return path


def _parse_strokes(raw):
    if not isinstance(raw, (list, tuple)) or not raw:
        raise _err("`strokes` must be a non-empty array of strokes")
    if len(raw) > MAX_STROKES:
        raise _err("Too many strokes in one call ({0}); send at most {1}."
                   .format(len(raw), MAX_STROKES))
    strokes = []
    for i, stroke in enumerate(raw):
        key = "strokes[{0}]".format(i)
        if not isinstance(stroke, dict):
            raise _err("{0} must be an object with `points`".format(key))
        unknown = set(stroke) - {"points", "smooth"}
        if unknown:
            raise _err("{0} has unknown field(s) {1}; allowed: points, smooth"
                       .format(key, ", ".join(sorted(unknown))))
        points = stroke.get("points")
        if not isinstance(points, (list, tuple)) or len(points) < 2:
            raise _err("{0}.points needs at least two points".format(key))
        if len(points) > MAX_POINTS:
            raise _err("{0}.points has {1} points; the limit is {2}"
                       .format(key, len(points), MAX_POINTS))
        parsed = [_point(p, "{0}.points[{1}]".format(key, j))
                  for j, p in enumerate(points)]
        with_pressure = [p[2] is not None for p in parsed]
        if any(with_pressure) and not all(with_pressure):
            raise _err("{0}: give pressure for every point or for none"
                       .format(key))
        smooth = _o()._as_bool(stroke.get("smooth", True), key + ".smooth")
        strokes.append({"points": parsed, "pressure": all(with_pressure),
                        "smooth": smooth})
    return strokes


def _paint_one(node, stroke):
    pts = stroke["points"]
    if stroke["pressure"]:
        # libkis takes pressure only on straight segments, in whole pixels.
        for (x1, y1, p1), (x2, y2, p2) in zip(pts, pts[1:]):
            node.paintLine(QPoint(int(round(x1)), int(round(y1))),
                           QPoint(int(round(x2)), int(round(y2))), p1, p2)
        return
    xy = [(x, y) for x, y, _p in pts]
    path = _smooth_path(xy) if stroke["smooth"] else _polyline_path(xy)
    node.paintPath(path)


def op_paint_strokes(params):
    o = _o()
    krita = o._krita()
    doc = o.resolve_document(params.get("document"))
    node = o.resolve_node(doc, params.get("layer"))
    _require_paint_layer(node, "painting")
    strokes = _parse_strokes(params.get("strokes"))

    preset = size = color = opacity = erase = None
    if params.get("brush") not in (None, ""):
        preset = _find_preset(krita, str(params["brush"]))
    if params.get("size") is not None:
        size = o._as_float(params["size"], "size")
        if not 0.5 <= size <= 1000:
            raise _err("size must be between 0.5 and 1000 pixels")
    if params.get("color") is not None:
        try:
            color = _managed_color(doc, imaging.parse_color(params["color"]))
        except imaging.ImagingError as exc:
            raise _err(str(exc))
    if params.get("opacity") is not None:
        opacity = o._as_float(params["opacity"], "opacity")
        if not 0.0 <= opacity <= 1.0:
            raise _err("opacity must be between 0 and 1")
    if params.get("erase") is not None:
        erase = o._as_bool(params["erase"], "erase")

    wants_resources = any(v is not None
                          for v in (preset, size, color, opacity, erase))
    view = _pick_view(krita, doc)
    if wants_resources and view is None:
        raise _err(
            "Brush, size, colour, opacity and erase are set through a Krita "
            "view, and no document has one open. Open any document with "
            "view=true (it does not have to be this one), or omit those "
            "settings to paint with the current brush.", kind="needs_view")

    if preset is not None and view is not None \
            and view.currentBrushPreset() is None:
        raise _err("Krita has no current brush preset to restore after this "
                   "call, so a `brush` override is refused. Pick a brush in "
                   "Krita once, or omit `brush`.", kind="needs_view")

    resources = _BrushResources(view) if view is not None else None
    used = {}
    touched = _stroke_area(doc, strokes, size)
    before = (bytes(node.pixelData(touched.x(), touched.y(), touched.width(),
                                   touched.height()))
              if not touched.isEmpty() else b"")
    try:
        if resources is not None:
            resources.apply(preset, size, color, opacity, erase)
            current = view.currentBrushPreset()
            used = {"brush": current.name() if current is not None else None,
                    "size": round(view.brushSize(), 2),
                    "opacity": round(view.paintingOpacity(), 3),
                    "erase": view.eraserMode()}
        # Not Node.paintAbility(): on 5.3.4 it took Krita down twice (exit
        # 139) when called from the bridge. A brush the layer cannot take
        # (e.g. MyPaint on a non-RGBA layer) shows up as "nothing changed".
        for stroke in strokes:
            _paint_one(node, stroke)
        doc.waitForDone()
    finally:
        if resources is not None:
            resources.restore()
    o._refresh(doc)
    after = (bytes(node.pixelData(touched.x(), touched.y(), touched.width(),
                                  touched.height()))
             if not touched.isEmpty() else b"")
    result = {"painted": len(strokes), "layer": node.name(),
              "pixels_changed": before != after,
              "layer_bounds": _rect_dict(node.bounds()), "undoable": True}
    result.update(used)
    if before == after:
        result["warning"] = (
            "The strokes changed no pixels: the brush may not work on this "
            "layer's colour space, the strokes may lie outside the canvas, or "
            "an eraser met empty pixels.")
    if resources is None:
        result["note"] = ("No view is open, so the current brush was used "
                          "as it is.")
    return result


def _stroke_area(doc, strokes, size):
    """Canvas rect the strokes can reach: their points plus a brush margin."""
    xs = [p[0] for s in strokes for p in s["points"]]
    ys = [p[1] for s in strokes for p in s["points"]]
    # The current preset's size is unknown until it is applied; 1000 px is
    # the documented size ceiling, so a margin of half of it is safe.
    margin = int(size if size is not None else 1000) // 2 + 2
    rect = QRect(int(min(xs)) - margin, int(min(ys)) - margin,
                 int(max(xs) - min(xs)) + 2 * margin + 1,
                 int(max(ys) - min(ys)) + 2 * margin + 1)
    return rect.intersected(_canvas(doc))


# --------------------------------------------------------------------------
# layer transforms
# --------------------------------------------------------------------------

_STRATEGIES = ("Bicubic", "Bilinear", "NearestNeighbor", "Lanczos3", "Box")


def _scale_factors(o, scale):
    if scale is None:
        return None, None
    if isinstance(scale, (list, tuple)):
        if len(scale) != 2:
            raise _err("scale must be a number or [sx, sy]")
        sx, sy = (o._as_float(scale[0], "scale[0]"),
                  o._as_float(scale[1], "scale[1]"))
    else:
        sx = sy = o._as_float(scale, "scale")
    if not (0.01 <= sx <= 100 and 0.01 <= sy <= 100):
        raise _err("scale factors must be between 0.01 and 100")
    return sx, sy


def _translation(o, translate):
    if translate is None:
        return None, None
    if not isinstance(translate, (list, tuple)) or len(translate) != 2:
        raise _err("translate must be [dx, dy] in pixels")
    dx = o._as_int(translate[0], "translate[0]")
    dy = o._as_int(translate[1], "translate[1]")
    if abs(dx) > MAX_COORD or abs(dy) > MAX_COORD:
        raise _err("translate must be within +/-{0} pixels".format(MAX_COORD))
    return dx, dy


def op_transform_layer(params):
    o = _o()
    doc = o.resolve_document(params.get("document"))
    node = o.resolve_node(doc, params.get("layer"))
    _require_editable(node, "transforming it")
    if node.type() not in _TRANSFORMABLE:
        raise _err("Layer {0!r} is a {1}; transform_layer moves layers, not "
                   "masks.".format(node.name(), node.type()))
    if all(params.get(k) is None for k in ("translate", "scale", "rotate")):
        raise _err("Give at least one of translate, scale, rotate.")

    sx, sy = _scale_factors(o, params.get("scale"))
    dx, dy = _translation(o, params.get("translate"))
    degrees = None
    if params.get("rotate") is not None:
        degrees = o._as_float(params["rotate"], "rotate")
        if not math.isfinite(degrees):
            raise _err("rotate must be a finite number of degrees")
    strategy = str(params.get("strategy") or "Bicubic")
    if strategy not in _STRATEGIES:
        raise _err("strategy must be one of: " + ", ".join(_STRATEGIES))
    before = node.bounds()
    if (sx is not None or degrees is not None) and before.isEmpty():
        raise _err("Layer {0!r} has no pixels to scale or rotate."
                   .format(node.name()))
    steps = []
    if sx is not None:
        width = max(1, int(round(before.width() * sx)))
        height = max(1, int(round(before.height() * sy)))
        node.scaleNode(QRectF(before).center(), width, height, strategy)
        doc.waitForDone()
        steps.append("scale")
    if degrees is not None and degrees % 360 != 0:
        node.rotateNode(math.radians(degrees))
        doc.waitForDone()
        steps.append("rotate")
    if dx is not None and (dx or dy):
        position = node.position()
        node.move(position.x() + dx, position.y() + dy)
        doc.waitForDone()
        steps.append("translate")
    o._refresh(doc)
    return {"layer": node.name(), "applied": steps,
            "bounds_before": _rect_dict(before),
            "bounds_after": _rect_dict(node.bounds()), "undoable": True}


# --------------------------------------------------------------------------
# selections from shapes and pixels
# --------------------------------------------------------------------------

def _shape_mask(doc, draw):
    """Rasterise an antialiased shape into a full-canvas 8-bit mask."""
    img = QImage(doc.width(), doc.height(), QImage.Format_Grayscale8)
    img.fill(0)
    painter = QPainter()
    if not painter.begin(img):
        raise _err("Could not rasterise the selection shape.")
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QColor(0, 0, 0, 0))
        painter.setBrush(QColor(255, 255, 255))
        draw(painter)
    finally:
        painter.end()
    return _gray_bytes(img)


def _sample_pixels(doc, sample, rect):
    """RGBA/U8 bytes (BGRA order) of ``rect`` from the composite or a layer."""
    o = _o()
    if rect.width() * rect.height() > MAX_SAMPLE_PIXELS:
        raise _err("Sampling {0}x{1} pixels is over the {2} limit; pass a "
                   "smaller `region`.".format(rect.width(), rect.height(),
                                             MAX_SAMPLE_PIXELS))
    if sample in (None, "", "merged"):
        if (doc.colorModel(), doc.colorDepth()) != imaging.RGBA8:
            raise _err("Sampling needs an RGBA/U8 document; this one is "
                       "{0}/{1}.".format(doc.colorModel(), doc.colorDepth()))
        o._refresh(doc)
        data = bytes(doc.pixelData(rect.x(), rect.y(), rect.width(),
                                   rect.height()))
        source = "merged"
    else:
        node = o.resolve_node(doc, sample)
        if (node.colorModel(), node.colorDepth()) != imaging.RGBA8:
            raise _err("Sampling needs an RGBA/U8 layer; {0!r} is {1}/{2}."
                       .format(node.name(), node.colorModel(),
                               node.colorDepth()))
        data = bytes(node.projectionPixelData(rect.x(), rect.y(), rect.width(),
                                              rect.height()))
        source = node.name()
    if len(data) != rect.width() * rect.height() * 4:
        raise _err("{0!r} is not RGBA/U8, so it cannot be sampled for a "
                   "selection.".format(source))
    return data, source


def _mask_selection(mask, rect):
    from krita import Selection
    selection = Selection()
    selection.setPixelData(QByteArray(bytes(mask)), rect.x(), rect.y(),
                           rect.width(), rect.height())
    return selection


def build_mask_selection(doc, mode, params):
    """The new selection for a mask-producing mode, plus details to report."""
    o = _o()
    canvas = _canvas(doc)
    if mode in ("ellipse", "polygon") and \
            doc.width() * doc.height() > MAX_SAMPLE_PIXELS:
        raise _err("The canvas is over {0} pixels; shape selections are "
                   "rasterised at full size.".format(MAX_SAMPLE_PIXELS))
    if mode == "ellipse":
        x = o._as_float(o._arg(params, "x", 0), "x")
        y = o._as_float(o._arg(params, "y", 0), "y")
        w = o._as_float(o._req(params, "width"), "width")
        h = o._as_float(o._req(params, "height"), "height")
        if w <= 0 or h <= 0:
            raise _err("width and height must be positive")
        mask = _shape_mask(doc, lambda p: p.drawEllipse(QRectF(x, y, w, h)))
        return _mask_selection(mask, canvas), {}
    if mode == "polygon":
        raw = o._req(params, "points")
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            raise _err("points needs at least three [x, y] pairs")
        if len(raw) > MAX_POLYGON_POINTS:
            raise _err("points is limited to {0} pairs".format(
                MAX_POLYGON_POINTS))
        poly = QPolygonF()
        for j, item in enumerate(raw):
            x, y, pressure = _point(item, "points[{0}]".format(j))
            if pressure is not None:
                raise _err("polygon points are [x, y] pairs")
            poly.append(QPointF(x, y))
        mask = _shape_mask(doc, lambda p: p.drawPolygon(poly))
        return _mask_selection(mask, canvas), {}

    rect = canvas
    if params.get("region"):
        rect = _region_rect(doc, params["region"])
    sample = params.get("sample")
    if mode == "contiguous":
        seed_x = o._as_int(o._req(params, "x"), "x")
        seed_y = o._as_int(o._req(params, "y"), "y")
        if not rect.contains(seed_x, seed_y):
            raise _err("The seed point ({0}, {1}) is outside the sampled "
                       "area {2}.".format(seed_x, seed_y, _rect_dict(rect)))
        tolerance = o._as_int(o._arg(params, "tolerance", 32), "tolerance")
        connectivity = o._as_int(o._arg(params, "connectivity", 4),
                                 "connectivity")
        pixels, source = _sample_pixels(doc, sample, rect)
        try:
            mask, bbox, count = raster.contiguous_mask(
                pixels, rect.width(), rect.height(), seed_x - rect.x(),
                seed_y - rect.y(), tolerance, connectivity)
        except ValueError as exc:
            raise _err(str(exc))
        return _mask_selection(mask, rect), {"sampled": source,
                                             "selected_pixels": count}
    if mode == "color":
        try:
            qcolor = imaging.parse_color(o._req(params, "color"))
        except imaging.ImagingError as exc:
            raise _err(str(exc))
        tolerance = o._as_int(o._arg(params, "tolerance", 32), "tolerance")
        pixels, source = _sample_pixels(doc, sample, rect)
        bgra = (qcolor.blue(), qcolor.green(), qcolor.red(), qcolor.alpha())
        try:
            match = raster.color_match_mask(pixels, rect.width(),
                                            rect.height(), bgra, tolerance)
        except ValueError as exc:
            raise _err(str(exc))
        mask = match.translate(bytes([0, 255]) + bytes(254))
        return _mask_selection(mask, rect), {
            "sampled": source, "selected_pixels": len(match) - match.count(0)}
    if mode == "layer_alpha":
        node = o.resolve_node(doc, o._req(params, "layer"))
        if (node.colorModel(), node.colorDepth()) != imaging.RGBA8:
            raise _err("layer_alpha needs an RGBA/U8 layer; {0!r} is {1}/{2}."
                       .format(node.name(), node.colorModel(),
                               node.colorDepth()))
        if rect.width() * rect.height() > MAX_SAMPLE_PIXELS:
            raise _err("Pass a smaller `region`; this one is over {0} "
                       "pixels.".format(MAX_SAMPLE_PIXELS))
        data = bytes(node.projectionPixelData(rect.x(), rect.y(),
                                              rect.width(), rect.height()))
        threshold = o._as_int(o._arg(params, "threshold", 0), "threshold")
        try:
            mask = raster.alpha_mask(data, rect.width(), rect.height(),
                                     threshold)
        except ValueError as exc:
            raise _err(str(exc))
        return _mask_selection(mask, rect), {"sampled": node.name()}
    raise _err("unhandled selection mode {0!r}".format(mode))


def fill_selection_holes(doc, threshold=128):
    """Close every hole in the document's selection; returns pixels added.

    The scanlation case: a magic-wand pick of a bubble interior leaves the old
    lettering out as holes, and a glyph with a counter (O, A, ...) is too big
    for grow-then-shrink to swallow. Anything enclosed by the selection is
    taken in; what touches the selection's bounding box edge is not.
    """
    selection = doc.selection()
    if selection is None:
        raise _err("There is no selection to fill the holes of.")
    rect = QRect(selection.x(), selection.y(), selection.width(),
                 selection.height())
    if rect.isEmpty():
        raise _err("The selection is empty.")
    mask = bytes(selection.pixelData(rect.x(), rect.y(), rect.width(),
                                     rect.height()))
    try:
        filled = raster.fill_holes(mask, rect.width(), rect.height(),
                                   threshold)
    except ValueError as exc:
        raise _err(str(exc))
    added = mask.count(0) - filled.count(0)
    doc.setSelection(_mask_selection(filled, rect))
    return added


def combine_selection(doc, new, combine):
    """Merge ``new`` into the document's selection by ``combine``."""
    if combine not in COMBINE_MODES:
        raise _err("combine must be one of: " + ", ".join(COMBINE_MODES))
    current = doc.selection()
    if combine == "replace" or (combine == "add" and current is None):
        doc.setSelection(new)
        return
    if current is None:
        raise _err("There is no selection to {0} with.".format(combine))
    getattr(current, combine)(new)
    doc.setSelection(current)


# --------------------------------------------------------------------------
# filling
# --------------------------------------------------------------------------

def op_fill(params):
    """Fill (or erase) the selection, a region, or their intersection."""
    o = _o()
    doc = o.resolve_document(params.get("document"))
    node = o.resolve_node(doc, params.get("layer"))
    _require_paint_layer(node, "filling")
    imaging.require_pixel_ops(node)

    erase = o._as_bool(o._arg(params, "erase", False), "erase")
    alpha_locked = node.alphaLocked()
    if erase and alpha_locked:
        raise _err("Layer {0!r} has its alpha locked, so it cannot be erased; "
                   "unlock it with set_layer alpha_lock=false."
                   .format(node.name()))
    color = None
    if not erase:
        try:
            color = imaging.parse_color(o._req(params, "color"))
        except imaging.ImagingError as exc:
            raise _err(str(exc))
    opacity = o._as_float(o._arg(params, "opacity", 1.0), "opacity")
    if not 0.0 <= opacity <= 1.0:
        raise _err("opacity must be between 0 and 1")

    selection = doc.selection()
    use_selection = o._as_bool(o._arg(params, "use_selection", True),
                               "use_selection")
    if not use_selection:
        selection = None
    region = params.get("region")
    if selection is None and not region:
        raise _err("Nothing to fill: make a selection (set_selection) or "
                   "pass a region.")
    rect = _region_rect(doc, region) if region else _canvas(doc)
    if selection is not None:
        sel_rect = QRect(selection.x(), selection.y(), selection.width(),
                         selection.height())
        rect = rect.intersected(sel_rect)
    if rect.isEmpty():
        raise _err("The selection and region do not overlap the canvas.")

    w, h = rect.width(), rect.height()
    if selection is not None:
        mask = bytes(selection.pixelData(rect.x(), rect.y(), w, h))
        if len(mask) != w * h:
            raise _err("Krita returned an unexpected selection buffer.")
        if mask.count(0) == len(mask):
            raise _err("The selection is empty inside that area.")
    else:
        mask = b"\xff" * (w * h)
    mask_img = QImage(mask, w, h, w, QImage.Format_Grayscale8).copy()

    stamp = QImage(w, h, QImage.Format_ARGB32)
    stamp.fill(color if color is not None else QColor(0, 0, 0))
    if color is not None and color.alpha() < 255:
        opacity *= color.alpha() / 255.0
    stamp.setAlphaChannel(mask_img)

    canvas = imaging.read_node_image(node, rect).convertToFormat(
        QImage.Format_ARGB32_Premultiplied)
    painter = QPainter()
    if not painter.begin(canvas):
        raise _err("Could not start painting on the layer buffer.")
    try:
        if erase:
            painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        elif alpha_locked:
            # Keep the layer's own coverage, as Krita's alpha lock does.
            painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        painter.setOpacity(opacity)
        painter.drawImage(0, 0, stamp)
    finally:
        painter.end()
    imaging.write_node_image(node, rect,
                             canvas.convertToFormat(QImage.Format_ARGB32))
    o._refresh(doc)
    return {"layer": node.name(), "erased" if erase else "filled": True,
            "region": _rect_dict(rect),
            "used_selection": selection is not None, "undoable": False,
            "note": ("Pixels were written directly, which Krita's undo "
                     "history does not record.")}


# --------------------------------------------------------------------------
# capability listings
# --------------------------------------------------------------------------

def _filtered(names, params):
    needle = str(params.get("filter") or "").strip().lower()
    out = sorted(n for n in names if needle in n.lower())
    return out


def op_list_brush_presets(params):
    names = _filtered(_o()._krita().resources("preset").keys(), params)
    return {"count": len(names), "brush_presets": names}


def op_list_fonts(params):
    from .compat import QFontDatabase
    try:
        families = QFontDatabase.families()
    except TypeError:  # PyQt5: an instance method
        families = QFontDatabase().families()
    names = _filtered(families, params)
    return {"count": len(names), "fonts": names}


def register(op):
    op("paint_strokes", timeout=120.0, mutates=True)(op_paint_strokes)
    op("transform_layer", timeout=120.0, mutates=True)(op_transform_layer)
    op("fill", timeout=120.0, mutates=True)(op_fill)
    op("list_brush_presets", timeout=15.0)(op_list_brush_presets)
    op("list_fonts", timeout=15.0)(op_list_fonts)
