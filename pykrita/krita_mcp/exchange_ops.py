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
import math
import operator
import os
import re

# A full page at 2500x3600 is 9 megapixels; 64 caps the buffers on both sides
# of a single inpainting pass while leaving room for generous context.
MAX_PIXELS = 64 * 1000 * 1000
# Import is the heavier side: it keeps the decoded pixels, the ARGB32 copy,
# the layer read-back, the stamp copy, the Qt composite and the merge result
# alive together -- roughly 24-28 bytes per pixel. 16 megapixels is a little
# under two full pages and caps that at about 0.5 GB; export encodes one
# image at a time, so it keeps the 64 megapixel cap above.
MAX_IMPORT_PIXELS = 16 * 1000 * 1000
MAX_PAD = 1024
IMPORT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MODES = ("over", "replace")

_NONZERO = re.compile(b"[^\x00]+")
_FULL = re.compile(b"\xff+")
_ZERO = re.compile(b"\x00+")
# bytes.translate table: 0 -> 0, 1..254 -> 255, 255 -> 0.
_PARTIAL_TABLE = bytes([0x00] + [0xFF] * 254 + [0x00])


class ExchangeError(ValueError):
    """A caller-fixable problem, raised before a single pixel is written."""

    kind = "invalid_request"


class ExchangeWriteError(ExchangeError):
    """The filesystem failed a write the arguments asked for."""

    kind = "internal"


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
        number = float(value)
    except (TypeError, ValueError):
        raise ExchangeError(
            "{0} must be a number, got {1!r}".format(key, value))
    except OverflowError:
        # int far too large for a float: 10 ** 400 from a JSON client.
        raise ExchangeError(
            "{0} must be a finite number, got {1!r}".format(key, value))
    if not math.isfinite(number):
        # inf, -inf and "1e999" all land here; JSON clients can send them.
        raise ExchangeError(
            "{0} must be a finite number, got {1!r}".format(key, value))
    return int(round(number))


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
    if "\x00" in path:
        # open() and Qt raise ValueError on a NUL, which must be a refusal
        # here and not an escaped crash.
        raise ExchangeError(
            "{0} contains a NUL byte, which cannot be part of a file path"
            .format(key))
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
    if os.path.islink(target):
        # Checked before exists(): a dangling link is invisible to exists()
        # and open("wb") would create its target somewhere else.
        raise ExchangeError(
            "path {0!r} is a symbolic link; pass the path of a real file."
            .format(target))
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
    if os.path.islink(target):
        raise ExchangeError(
            "path {0!r} is a symbolic link; pass the image file itself."
            .format(target))
    if not os.path.isfile(target):
        raise ExchangeError(
            "No file at {0!r}. Export the region first with export_region, or "
            "fix the path.".format(target))
    return target


def _open_sibling_temp(parent, base):
    """An open "xb" handle next to ``target``, plus its path.

    Same directory as the target so os.replace() is a rename, not a copy.
    """
    for _ in range(8):
        candidate = os.path.join(
            parent, ".{0}.{1}.tmp".format(base, os.urandom(6).hex()))
        try:
            return open(candidate, "xb"), candidate
        except FileExistsError:
            continue
    raise ExchangeWriteError(
        "Could not create a temporary file next to {0!r}.".format(
            os.path.join(parent, base)))


def write_png_file(target, data, overwrite=False):
    """Write ``data`` to ``target`` exclusively, or atomically when replacing.

    ``overwrite=False`` opens with "xb": the kernel refuses a file that
    appeared after check_export_path's exists() check instead of truncating
    it. ``overwrite=True`` writes a sibling temp file, fsyncs it and renames
    it over the target, so a reader sees the old file or the whole new one
    and a crash cannot leave a half-written PNG behind. Filesystem failures
    raise ExchangeWriteError, which the bridge reports apart from bad
    arguments.
    """
    if not overwrite:
        try:
            with open(target, "xb") as handle:
                handle.write(data)
        except FileExistsError:
            raise ExchangeError(
                "path {0!r} already exists; pass overwrite=true to replace it "
                "or choose another name.".format(target))
        except (OSError, ValueError) as exc:
            raise ExchangeWriteError(
                "Could not write {0!r}: {1}".format(target, exc))
        return

    try:
        handle, temp = _open_sibling_temp(os.path.dirname(target),
                                          os.path.basename(target))
    except OSError as exc:
        raise ExchangeWriteError(
            "Could not write {0!r}: {1}".format(target, exc))
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        temp = None
    except (OSError, ValueError) as exc:
        raise ExchangeWriteError(
            "Could not write {0!r}: {1}".format(target, exc))
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


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


def check_pixel_budget(rect, limit=MAX_PIXELS, what="Exporting",
                       fix="export a smaller region or less pad"):
    """Refuse a buffer that would dwarf the page it came from."""
    pixels = rect[2] * rect[3]
    if pixels > limit:
        raise ExchangeError(
            "{0} {1}x{2} = {3} pixels exceeds the {4} pixel limit "
            "({5} megapixels); {6}."
            .format(what, rect[2], rect[3], pixels, limit,
                    limit // (1000 * 1000), fix))
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


def _partial_coverage(coverage):
    """``coverage`` with only the partly covered pixels (0 < c < 255) left."""
    return coverage.translate(_PARTIAL_TABLE)


def has_partial_coverage(coverage):
    """Does the selection partly cover any pixel at all?

    When it does not -- the common hard rectangle or full-lasso case -- the
    caller can skip the Qt blend, whose result no pixel would use.
    """
    return _partial_coverage(coverage).find(b"\xff") != -1


def merge_replace(before, stamp, coverage, blended=None):
    """The "replace" merge: the stamp's bytes where the selection is full.

    "replace" promises the file's pixels as they are, transparency included,
    and a premultiplied Qt composite cannot deliver that for semi-transparent
    colours. So the merge is explicit, per pixel:

    * coverage 255: the stamp's ARGB bytes, untouched;
    * coverage 0: ``before``, untouched;
    * any other coverage: ``blended``, the Qt weighted blend.

    The blend is the starting point and only the two extremes are copied over
    it, so a selection that is partly covered everywhere (a feather, a
    gradient) costs no Python work per pixel -- only the runs of 0 and 255 do.
    ``blended`` may be None when no pixel is partly covered; the partial
    pixels then keep ``before``.
    """
    if len(before) != len(stamp) or len(stamp) != len(coverage) * 4:
        raise ExchangeError(
            "Cannot merge {0}/{1} bytes with a {2} byte mask."
            .format(len(before), len(stamp), len(coverage)))
    if blended is not None and len(blended) != len(before):
        raise ExchangeError(
            "Cannot merge a {0} byte blend into {1} bytes."
            .format(len(blended), len(before)))
    out = bytearray(before if blended is None else blended)
    for match in _ZERO.finditer(coverage):
        start, end = match.span()
        out[start * 4:end * 4] = before[start * 4:end * 4]
    for match in _FULL.finditer(coverage):
        start, end = match.span()
        out[start * 4:end * 4] = stamp[start * 4:end * 4]
    return bytes(out)


def alpha_within(before_alpha, stamp_alpha):
    """Stamp alpha restricted to the pixels the layer already covers.

    source-atop keeps the layer's alpha, and on a pixel whose alpha is 0 it
    resolves to (0,0,0,0) -- which would overwrite the colour bytes Krita
    keeps behind a transparent pixel. Excluding those pixels from the merge
    keeps them byte-identical, which is what an alpha lock promises.
    """
    if len(before_alpha) != len(stamp_alpha):
        raise ExchangeError(
            "Cannot mask {0} alpha bytes with {1}."
            .format(len(stamp_alpha), len(before_alpha)))
    out = bytearray(stamp_alpha)
    for match in _ZERO.finditer(before_alpha):
        start, end = match.span()
        out[start:end] = b"\x00" * (end - start)
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
                # Pixels the layer does not cover must keep their bytes too:
                # source-atop yields (0,0,0,0) there, not "before".
                touched = alpha_within(_alpha_bytes(before), touched)
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
            raise _err(str(exc), kind=getattr(exc, "kind", "invalid_request"))
    handler.__doc__ = fn.__doc__
    handler.__name__ = fn.__name__
    return handler


@_reported
def op_export_region(params):
    """Write a canvas region to a PNG file for an outside tool to patch.

    Reads only: the document keeps its pixels and its undo history. Omit
    `layer` for the merged image (the page as it renders); pass one for that
    paint layer's own pixels, which must be RGBA/U8. `pad` adds context on
    every side, clamped to the canvas, and the returned `region` is the rect
    that was actually exported. The merged image is the page as it renders
    right now, so take a `checkpoint` first when exporting mid-edit.
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
        if node.type() != "paintlayer":
            # Measured on Krita 5.3.4: a group layer's pixelData is 0 bytes,
            # so exporting one would write a blank PNG instead of a refusal.
            raise _err(
                "Layer {0!r} is a {1}, which has no pixels of its own to "
                "export; omit layer for the merged image, or export a paint "
                "layer.".format(source, node.type()))
        try:
            image = imaging.read_node_image(node, QRect(*rect))
        except imaging.ImagingError as exc:
            # Colour model, depth or a short pixelData read: the caller can
            # act on all of these by converting the document.
            raise _err(str(exc))
    if image is None or image.isNull():
        raise _err("Krita returned an empty image for {0},{1} {2}x{3}."
                   .format(rect[0], rect[1], rect[2], rect[3]),
                   kind="internal")

    image = image.convertToFormat(QImage.Format_ARGB32)
    try:
        data = imaging.qimage_to_png_bytes(image)
    except imaging.ImagingError as exc:
        raise _err(str(exc), kind="internal")
    write_png_file(target, data, overwrite)

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

    The header must state the image size and fit the import pixel limit; a
    file that cannot be sized is refused before decoding. Only the part that
    lands on the canvas is decoded when the image hangs off it.

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
    x = _as_int(_require(
        params, "x", "the canvas x of the image's top-left corner"), "x")
    y = _as_int(_require(
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
    # decode a gigapixel buffer before any limit applies. A handler without a
    # readable header cannot be bounded, so it is refused rather than decoded.
    reader = QImageReader(target)
    header = reader.size()
    if not header.isValid():
        raise _err(
            "Could not read the image size from {0!r}; its format has no "
            "readable header, or the file is damaged. Re-save it as a PNG "
            "and pass that.".format(os.path.basename(target)))
    check_pixel_budget((0, 0, header.width(), header.height()),
                       limit=MAX_IMPORT_PIXELS, what="Importing",
                       fix="crop the file to the patched area")
    if expect is not None and (header.width(), header.height()) != expect:
        raise _err(
            "expect_size is {0}x{1} but {2} is {3}x{4}; patch a copy of the "
            "exported file at its original size.".format(
                expect[0], expect[1], os.path.basename(target),
                header.width(), header.height()))

    placed, clipped = place_rect(x, y, header.width(), header.height(),
                                 (0, 0, doc.width(), doc.height()))
    dst = QRect(*placed)
    crop = QRect(placed[0] - x, placed[1] - y, placed[2], placed[3])
    if clipped:
        # The off-canvas part never becomes layer pixels, so ask for only the
        # placed part instead of copying the whole image afterwards. Measured
        # on Krita 5.3.4/Qt 5.15: the PNG handler still materialises the full
        # decode (+92 MB for a 24 MP clip), so this bounds our buffers, not
        # Qt's; the header budget above is what bounds the decode.
        reader.setClipRect(crop)
    image = reader.read()
    if image.isNull():
        raise _err("Could not read {0!r} as an image; export it with "
                   "export_region, or check that the file is not damaged."
                   .format(os.path.basename(target)))
    if (image.width(), image.height()) != (crop.width(), crop.height()):
        raise _err(
            "{0} reports {1}x{2}, so {3}x{4} of it lands on the canvas, but "
            "Qt decoded {5}x{6}. The format does not support clipped "
            "decoding, or the header lies; re-save it as a PNG and retry."
            .format(os.path.basename(target), header.width(), header.height(),
                    crop.width(), crop.height(), image.width(),
                    image.height()))
    image = image.convertToFormat(QImage.Format_ARGB32)
    source = image

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

    if mode == "replace":
        # Straight copy of the stamp's bytes: going through QPainter's
        # premultiplied buffers would round semi-transparent pixels (measured
        # on Qt 5.15).
        stamp_bytes = _image_bytes(source)
        if coverage is None:
            after_bytes = stamp_bytes
        else:
            # Full coverage takes the stamp's bytes exactly, coverage 0 keeps
            # the layer's, and only partly covered pixels go through the Qt
            # blend -- whose result is unused when there are no partials.
            blended = None
            if has_partial_coverage(coverage):
                composed, _ = _compose(before, source, mode, False, coverage)
                blended = _image_bytes(composed)
            after_bytes = merge_replace(before_bytes, stamp_bytes, coverage,
                                        blended)
    else:
        composed, touched = _compose(before, source, mode, alpha_locked,
                                     coverage)
        after_bytes = _image_bytes(composed)
        if touched is not None:
            after_bytes = merge_by_mask(before_bytes, after_bytes, touched)

    changed = count_changed_pixels(before_bytes, after_bytes)
    if changed:
        after = QImage(after_bytes, dst.width(), dst.height(), dst.width() * 4,
                       QImage.Format_ARGB32).copy()
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
