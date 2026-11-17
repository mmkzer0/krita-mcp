"""File-based exchange of canvas regions with tools outside Krita.

An agent exports a region to a PNG, another program (an inpainting model, an
image editor, a human) patches the file, and ``import_region`` pastes it back
at the same place. Pixels travel as files, never as base64 through the model
that ``get_image``/``draw`` force.

Both operations write pixels directly, which Krita's undo history does not
record: take a ``checkpoint`` first when the edit must be reversible.

The helpers at the top are plain Python -- no Krita, no Qt -- so
test_exchange.py can exercise every refusal and the rect arithmetic without a
running Krita.
"""

import hashlib
import operator
import os
import re

# A full page at 2500x3600 is 9 megapixels; 64 caps the buffers on both sides
# of a single inpainting pass while leaving room for generous context.
MAX_PIXELS = 64 * 1000 * 1000
MAX_PAD = 1024
IMPORT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MODES = ("over", "replace")

_NONZERO = re.compile(b"[^\x00]+")


class ExchangeError(ValueError):
    """A caller-fixable problem, raised before a single pixel is written."""


def _o():
    from . import ops
    return ops


def _imaging():
    from . import imaging
    return imaging


def _paint():
    from . import paint_ops
    return paint_ops


def _err(message, kind="invalid_request"):
    return _o().OpError(message, kind=kind)


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def _as_int(value, key):
    if isinstance(value, bool):
        raise ExchangeError(
            "{0} must be a number, got {1!r}".format(key, value))
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise ExchangeError(
            "{0} must be a number, got {1!r}".format(key, value))


def _require(params, key, hint):
    value = params.get(key)
    if value is None or value == "":
        raise ExchangeError("{0} is required: {1}".format(key, hint))
    return value


def parse_region(value, key="region"):
    """(x, y, width, height) from the shared region object."""
    if not isinstance(value, dict):
        raise ExchangeError(
            "{0} must be an object with x, y, width, height".format(key))
    for side in ("width", "height"):
        if value.get(side) is None:
            raise ExchangeError("{0}.{1} is required".format(key, side))
    x = _as_int(value.get("x", 0), key + ".x")
    y = _as_int(value.get("y", 0), key + ".y")
    width = _as_int(value["width"], key + ".width")
    height = _as_int(value["height"], key + ".height")
    if width < 1 or height < 1:
        raise ExchangeError(
            "{0}.width and {0}.height must be at least 1 pixel, got {1}x{2}"
            .format(key, width, height))
    return x, y, width, height


def parse_pad(value):
    if value is None:
        return 0
    pad = _as_int(value, "pad")
    if not 0 <= pad <= MAX_PAD:
        raise ExchangeError(
            "pad must be between 0 and {0} pixels, got {1}"
            .format(MAX_PAD, pad))
    return pad


def parse_bool(value, key, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    raise ExchangeError(
        "{0} must be true or false, got {1!r}".format(key, value))


def parse_mode(value):
    if value is None:
        return "over"
    if not isinstance(value, str):
        raise ExchangeError(
            "mode must be \"over\" or \"replace\", got {0!r}".format(value))
    mode = value.strip().lower()
    if mode not in MODES:
        raise ExchangeError(
            "mode must be \"over\" or \"replace\", got {0!r}".format(value))
    return mode


def parse_expect_size(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ExchangeError(
            "expect_size must be [width, height], got {0!r}".format(value))
    width = _as_int(value[0], "expect_size[0]")
    height = _as_int(value[1], "expect_size[1]")
    if width < 1 or height < 1:
        raise ExchangeError(
            "expect_size must be two positive pixel counts, got {0}x{1}"
            .format(width, height))
    return width, height


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def _absolute(path, key="path"):
    if not isinstance(path, str) or not path.strip():
        raise ExchangeError("{0} must be a non-empty file path".format(key))
    expanded = os.path.expanduser(path.strip())
    if not os.path.isabs(expanded):
        raise ExchangeError(
            "{0} must be an absolute path, got {1!r}; pass one under a real "
            "directory such as /tmp or ~.".format(key, path))
    return os.path.normpath(expanded)


def check_export_path(path, overwrite=False):
    """The absolute PNG path to write, after every refusal that can be made."""
    target = _absolute(path)
    if os.path.splitext(target)[1].lower() != ".png":
        raise ExchangeError(
            "path must end in .png, got {0!r}; export_region writes a PNG for "
            "the external tool.".format(os.path.basename(target)))
    if os.path.isdir(target):
        raise ExchangeError(
            "path {0!r} is a directory; pass a file name.".format(target))
    if os.path.exists(target) and not overwrite:
        raise ExchangeError(
            "path {0!r} already exists; pass overwrite=true to replace it or "
            "choose another name.".format(target))
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise ExchangeError(
            "The directory {0!r} does not exist. Create it first; "
            "export_region does not create directories.".format(parent))
    return target


def check_import_path(path):
    """The absolute path of an image file that is there to paste."""
    target = _absolute(path)
    if os.path.splitext(target)[1].lower() not in IMPORT_SUFFIXES:
        raise ExchangeError(
            "path must be a .png, .jpg, .jpeg or .webp image, got {0!r}."
            .format(os.path.basename(target)))
    if not os.path.isfile(target):
        raise ExchangeError(
            "No file at {0!r}. Export the region first with export_region, or "
            "fix the path.".format(target))
    return target


# --------------------------------------------------------------------------
# rect arithmetic
# --------------------------------------------------------------------------

def expand_rect(rect, pad, canvas):
    """Pad a rect on every side and clip it to the canvas.

    A rect that ends up with no area left is refused: there is nothing to
    export, and silently writing a 1x1 file would hide the mistake.
    """
    x, y, width, height = rect
    canvas_x, canvas_y, canvas_w, canvas_h = canvas
    if (min(x + width, canvas_x + canvas_w) <= max(x, canvas_x)
            or min(y + height, canvas_y + canvas_h) <= max(y, canvas_y)):
        # Padding must not resurrect a region that is not on the page at all.
        raise ExchangeError(
            "Region {0},{1} {2}x{3} lies outside the {4}x{5} canvas."
            .format(x, y, width, height, canvas_w, canvas_h))
    left = max(x - pad, canvas_x)
    top = max(y - pad, canvas_y)
    right = min(x + width + pad, canvas_x + canvas_w)
    bottom = min(y + height + pad, canvas_y + canvas_h)
    if right <= left or bottom <= top:
        raise ExchangeError(
            "Region {0},{1} {2}x{3} with pad {4} lies outside the {5}x{6} "
            "canvas.".format(x, y, width, height, pad, canvas_w, canvas_h))
    return left, top, right - left, bottom - top


def check_pixel_budget(rect, what="Exporting", fix="export a smaller "
                       "region or less pad"):
    """Refuse a buffer that would dwarf the page it came from."""
    pixels = rect[2] * rect[3]
    if pixels > MAX_PIXELS:
        raise ExchangeError(
            "{0} {1}x{2} = {3} pixels exceeds the {4} pixel limit "
            "(64 megapixels); {5}."
            .format(what, rect[2], rect[3], pixels, MAX_PIXELS, fix))
    return pixels


def place_rect(x, y, width, height, canvas):
    """Where an image at (x, y) lands, clipped to the canvas.

    Returns ((x, y, width, height), clipped). Only an image that misses the
    canvas entirely is refused.
    """
    canvas_x, canvas_y, canvas_w, canvas_h = canvas
    left = max(x, canvas_x)
    top = max(y, canvas_y)
    right = min(x + width, canvas_x + canvas_w)
    bottom = min(y + height, canvas_y + canvas_h)
    if right <= left or bottom <= top:
        raise ExchangeError(
            "An image of {0}x{1} at {2},{3} lies outside the {4}x{5} canvas; "
            "place it inside.".format(width, height, x, y, canvas_w, canvas_h))
    placed = (left, top, right - left, bottom - top)
    return placed, placed != (x, y, width, height)


# --------------------------------------------------------------------------
# pixel buffers
# --------------------------------------------------------------------------

def count_changed_pixels(before, after):
    """How many ARGB32 pixels differ between two tight buffers."""
    if len(before) != len(after) or len(before) % 4:
        raise ExchangeError("Cannot compare {0} and {1} bytes as ARGB32."
                            .format(len(before), len(after)))
    if not before:
        return 0
    return sum(map(operator.ne, memoryview(before).cast("I"),
                   memoryview(after).cast("I")))


def merge_by_mask(before, after, coverage):
    """Take ``after`` only where ``coverage`` is non-zero.

    Qt composition rounds every pixel it touches through premultiplied alpha,
    and that round trip is lossy for low alpha (a 1/255 pixel does not survive
    it). Pixels the caller did not select, or that the pasted image leaves
    fully transparent, must keep their exact bytes, so only the covered runs
    are copied over.
    """
    if len(before) != len(after) or len(after) != len(coverage) * 4:
        raise ExchangeError(
            "Cannot merge {0}/{1} bytes with a {2} byte mask."
            .format(len(before), len(after), len(coverage)))
    out = bytearray(before)
    for match in _NONZERO.finditer(coverage):
        start, end = match.span()
        out[start * 4:end * 4] = after[start * 4:end * 4]
    return bytes(out)


def _tight_rows(img, width, height, bytes_per_pixel):
    """QImage rows without Qt's scanline padding."""
    stride = img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(stride * height)
    data = bytes(ptr)
    row_bytes = width * bytes_per_pixel
    if stride == row_bytes:
        return data
    return b"".join(data[offset:offset + row_bytes]
                    for offset in range(0, stride * height, stride))


def _image_bytes(img):
    return _tight_rows(img, img.width(), img.height(), 4)


def _alpha_bytes(img):
    """The alpha channel of a 32-bit QImage, one byte per pixel.

    Krita's RGBA8 is BGRA in memory, and Qt's ARGB32 (premultiplied or not)
    keeps alpha in the fourth byte, so this is a slice, not a conversion --
    which also keeps Format_Alpha8 out of the PyQt5/PyQt6 name split.
    """
    return _image_bytes(img)[3::4]


def _coverage_image(coverage, width, height):
    """An ARGB32 image carrying ``coverage`` (one byte per pixel) as alpha."""
    from .compat import QImage
    if len(coverage) != width * height:
        raise ExchangeError(
            "Krita returned {0} selection bytes for {1}x{2} pixels."
            .format(len(coverage), width, height))
    raw = bytearray(4 * width * height)
    raw[3::4] = coverage
    return QImage(bytes(raw), width, height, width * 4,
                  QImage.Format_ARGB32).copy()


def _compose(before, stamp, mode, alpha_locked, coverage):
    """Composite ``stamp`` onto ``before`` (both ARGB32) with Qt.

    Returns (ARGB32 image, touched). ``touched`` is the one-byte-per-pixel
    mask of the pixels this may legitimately change: the selection coverage
    for "replace" (a transparent stamp still erases), the effective stamp
    alpha for "over" (a transparent stamp leaves the layer alone).
    """
    from .compat import QImage, QPainter

    width, height = before.width(), before.height()
    canvas = before.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    source = stamp.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    mask = None
    if coverage is not None:
        mask = _coverage_image(coverage, width, height)
        painter = QPainter()
        if not painter.begin(source):
            raise ExchangeError(
                "Could not start painting on the image buffer.")
        try:
            painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, mask)
        finally:
            painter.end()

    touched = None
    painter = QPainter()
    if not painter.begin(canvas):
        raise ExchangeError("Could not start painting on the layer buffer.")
    try:
        if mode == "replace":
            touched = coverage
            # Weighted replace: clear the covered part, then add the stamp.
            painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            painter.drawImage(0, 0, mask)
            painter.setCompositionMode(QPainter.CompositionMode_Plus)
            painter.drawImage(0, 0, source)
        else:
            touched = _alpha_bytes(source)
            if alpha_locked:
                # Keep the layer's own coverage, as Krita's alpha lock does.
                painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
            else:
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.drawImage(0, 0, source)
    finally:
        painter.end()
    return canvas.convertToFormat(QImage.Format_ARGB32), touched


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

def _reported(fn):
    """Report a refusal as the bridge's OpError instead of a crash."""
    def handler(params):
        try:
            return fn(params)
        except ExchangeError as exc:
            raise _err(str(exc))
    handler.__doc__ = fn.__doc__
    handler.__name__ = fn.__name__
    return handler


@_reported
def op_export_region(params):
    """Write a canvas region to a PNG file for an outside tool to patch.

    Reads only: the document keeps its pixels and its undo history. Omit
    `layer` for the merged image (the page as it renders); pass one for that
    layer's own pixels, which must be RGBA/U8. `pad` adds context on every
    side, clamped to the canvas, and the returned `region` is the rect that
    was actually exported. The merged image is the page as it renders right
    now, so take a `checkpoint` first when exporting mid-edit.
    """
    from .compat import QImage, QRect

    o = _o()
    imaging = _imaging()
    region = parse_region(_require(params, "region",
                                   "{x, y, width, height} in canvas pixels"))
    pad = parse_pad(params.get("pad"))
    overwrite = parse_bool(params.get("overwrite", False), "overwrite")
    target = check_export_path(
        _require(params, "path", "an absolute file path ending in .png"),
        overwrite)

    doc = o.resolve_document(params.get("document"))
    rect = expand_rect(region, pad, (0, 0, doc.width(), doc.height()))
    check_pixel_budget(rect)

    layer_ref = params.get("layer")
    node = None if layer_ref in (None, "", "merged") else o.resolve_node(
        doc, layer_ref)
    o._refresh(doc)
    if node is None:
        source = "merged"
        image = doc.projection(rect[0], rect[1], rect[2], rect[3])
    else:
        source = node.name()
        try:
            image = imaging.read_node_image(node, QRect(*rect))
        except imaging.ImagingError as exc:
            raise _err(str(exc))
    if image is None or image.isNull():
        raise _err("Krita returned an empty image for {0},{1} {2}x{3}."
                   .format(rect[0], rect[1], rect[2], rect[3]))

    image = image.convertToFormat(QImage.Format_ARGB32)
    data = imaging.qimage_to_png_bytes(image)
    try:
        with open(target, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise _err("Could not write {0!r}: {1}".format(target, exc))

    return {
        "path": target,
        "region": {"x": rect[0], "y": rect[1],
                   "width": rect[2], "height": rect[3]},
        "width": image.width(),
        "height": image.height(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source": source,
    }


@_reported
def op_import_region(params):
    """Paste a PNG/JPEG/WebP file into a layer at a canvas position.

    `x`,`y` are the canvas pixels of the image's top-left -- typically the
    `region.x`/`region.y` export_region returned. `mode` "over" (default)
    alpha-composites the image onto the layer; "replace" writes its pixels as
    they are, transparency included, and is refused on an alpha-locked layer
    because it would overwrite their transparency. `selection_only` limits the
    change to the current selection, weighted by its coverage. Pixels outside
    the canvas are clipped and reported, not refused.

    Writes pixels directly, which Krita's undo history does not record: take a
    `checkpoint` first.
    """
    from .compat import QImage, QImageReader, QRect

    o = _o()
    imaging = _imaging()
    doc = o.resolve_document(params.get("document"))
    layer_ref = _require(params, "layer", "the paint layer to paste into")
    node = o.resolve_node(doc, layer_ref)
    _paint()._require_paint_layer(node, "the paste")
    try:
        imaging.require_pixel_ops(node)
    except imaging.ImagingError as exc:
        raise _err(str(exc))

    mode = parse_mode(params.get("mode"))
    selection_only = parse_bool(params.get("selection_only", False),
                                "selection_only")
    expect = parse_expect_size(params.get("expect_size"))
    target = check_import_path(
        _require(params, "path", "an absolute image path"))
    x = o._as_int(_require(
        params, "x", "the canvas x of the image's top-left corner"), "x")
    y = o._as_int(_require(
        params, "y", "the canvas y of the image's top-left corner"), "y")

    alpha_locked = node.alphaLocked()
    if mode == "replace" and alpha_locked:
        raise _err(
            "Layer {0!r} has its alpha locked, so replace would overwrite its "
            "transparency; use mode over, or unlock it with set_layer "
            "alpha_lock=false.".format(node.name()))

    selection = None
    if selection_only:
        selection = doc.selection()
        if selection is None:
            raise _err(
                "selection_only=true needs a selection to keep the paste "
                "inside; make one with set_selection, or pass "
                "selection_only=false.")

    # Read the header first: a hostile or mistaken file must not make Krita
    # decode a gigapixel buffer before any limit applies.
    header = QImageReader(target).size()
    if header.isValid():
        check_pixel_budget((0, 0, header.width(), header.height()),
                           "Importing", "crop the image to the patched area")
    image = QImage()
    if not image.load(target):
        raise _err("Could not read {0!r} as an image; export it with "
                   "export_region, or check that the file is not damaged."
                   .format(target))
    image = image.convertToFormat(QImage.Format_ARGB32)
    if expect is not None and (image.width(), image.height()) != expect:
        raise _err(
            "expect_size is {0}x{1} but {2} is {3}x{4}; patch a copy of the "
            "exported file at its original size.".format(
                expect[0], expect[1], os.path.basename(target),
                image.width(), image.height()))

    placed, clipped = place_rect(x, y, image.width(), image.height(),
                                 (0, 0, doc.width(), doc.height()))
    dst = QRect(*placed)
    source = image.copy(QRect(placed[0] - x, placed[1] - y,
                              placed[2], placed[3]))

    o._refresh(doc)
    try:
        before = imaging.read_node_image(node, dst)
    except imaging.ImagingError as exc:
        raise _err(str(exc))
    before_bytes = _image_bytes(before)

    coverage = None
    if selection is not None:
        coverage = bytes(selection.pixelData(dst.x(), dst.y(),
                                             dst.width(), dst.height()))
        if len(coverage) != dst.width() * dst.height():
            raise _err(
                "Krita returned {0} selection bytes for a {1}x{2} "
                "region.".format(len(coverage), dst.width(),
                                 dst.height()))

    if mode == "replace" and coverage is None:
        # Straight copy: going through QPainter's premultiplied buffers would
        # round semi-transparent pixels (measured on Qt 5.15), so the patch
        # bytes are written untouched.
        after = source
        after_bytes = _image_bytes(after)
    else:
        composed, touched = _compose(before, source, mode, alpha_locked,
                                     coverage)
        after_bytes = _image_bytes(composed)
        if touched is not None:
            after_bytes = merge_by_mask(before_bytes, after_bytes, touched)
        after = QImage(after_bytes, dst.width(), dst.height(), dst.width() * 4,
                       QImage.Format_ARGB32).copy()

    changed = count_changed_pixels(before_bytes, after_bytes)
    if changed:
        imaging.write_node_image(node, dst, after)
        o._refresh(doc)
    return {
        "layer": node.name(),
        "rect": {"x": dst.x(), "y": dst.y(),
                 "width": dst.width(), "height": dst.height()},
        "clipped": clipped,
        "pixels_changed": changed,
        "mode": mode,
        "used_selection": selection is not None,
    }


def register(op):
    op("export_region", timeout=90.0)(op_export_region)
    op("import_region", timeout=90.0, mutates=True)(op_import_region)
