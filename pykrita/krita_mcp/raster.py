"""Krita-free pixel-mask helpers for magic-wand style selection.

Pixel buffers are the RGBA/U8 layer bytes Krita exposes: ``bytes`` of length
``width * height * 4`` in B, G, R, A byte order, row-major, no padding.
Channel matching runs at C speed through ``bytes.translate`` plus a big-integer
AND; the flood fill fills whole spans located with ``bytes.find``/``rfind``.
"""

_NONZERO_TABLE = bytes([0] + [1] * 255)
_INVERT_FLAGS = bytes([1] + [0] * 255)
_HOLE_FILL = bytes([0] + [255] * 255)


def _check_size(width, height):
    if not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("width and height must be integers")
    if width <= 0 or height <= 0:
        raise ValueError(
            "width and height must be positive, got {0}x{1}".format(width, height))


def _check_pixels(pixels, width, height):
    _check_size(width, height)
    expected = width * height * 4
    if len(pixels) != expected:
        raise ValueError(
            "pixel buffer has {0} bytes, expected width*height*4 = {1}".format(
                len(pixels), expected))


def _check_mask(mask, width, height):
    _check_size(width, height)
    expected = width * height
    if len(mask) != expected:
        raise ValueError(
            "mask has {0} bytes, expected width*height = {1}".format(
                len(mask), expected))


def _check_color(color_bgra):
    try:
        components = tuple(color_bgra)
    except TypeError:
        raise ValueError("color_bgra must be four integers in 0..255")
    if len(components) != 4 or any(
            not isinstance(component, int) or not 0 <= component <= 255
            for component in components):
        raise ValueError("color_bgra must be four integers in 0..255")


def _check_tolerance(tolerance):
    if not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
        raise ValueError("tolerance must be an integer in 0..255")


def _check_threshold(threshold):
    if not isinstance(threshold, int) or not 0 <= threshold <= 255:
        raise ValueError("threshold must be an integer in 0..255")


def color_match_mask(pixels, width, height, color_bgra, tolerance):
    """bytes(width*height): 1 where every channel |p[c]-color[c]| <= tolerance, else 0."""
    _check_pixels(pixels, width, height)
    _check_color(color_bgra)
    _check_tolerance(tolerance)
    tables = [
        bytes(1 if abs(value - target) <= tolerance else 0 for value in range(256))
        for target in color_bgra
    ]
    match = int.from_bytes(pixels[0::4].translate(tables[0]), "big")
    for channel in range(1, 4):
        match &= int.from_bytes(pixels[channel::4].translate(tables[channel]), "big")
    return match.to_bytes(width * height, "big")


def _span_fill(match, width, height, seed_x, seed_y, connectivity):
    """Scanline-fill one 4/8-connected 1-region of a 0/1 match buffer.

    Returns ``(out, bbox, count)`` where ``out`` is a 0/255 ``bytearray``.
    """
    total = width * height
    out = bytearray(total)
    stack = [seed_y * width + seed_x]
    count = 0
    min_x = width
    max_x = -1
    min_y = height
    max_y = -1
    eight_way = connectivity == 8

    while stack:
        index = stack.pop()
        if out[index] or not match[index]:
            continue
        y, x = divmod(index, width)
        row = y * width
        stop = match.rfind(0, row, row + x + 1)
        left = 0 if stop < 0 else stop - row + 1
        stop = match.find(0, row + x, row + width)
        right = width if stop < 0 else stop - row
        out[row + left:row + right] = b"\xff" * (right - left)
        count += right - left
        if left < min_x:
            min_x = left
        if right - 1 > max_x:
            max_x = right - 1
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
        if eight_way:
            low = left - 1 if left else 0
            high = right + 1 if right < width else width
        else:
            low = left
            high = right
        for neighbor_y in (y - 1, y + 1):
            if neighbor_y < 0 or neighbor_y >= height:
                continue
            base = neighbor_y * width
            found = match.find(1, base + low, base + high)
            while found >= 0:
                end = match.find(0, found + 1, base + high)
                if end < 0:
                    end = base + high
                if not out[found]:
                    stack.append(found)
                found = match.find(1, end, base + high)

    if count == 0:
        return out, None, 0
    bbox = (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
    return out, bbox, count


def contiguous_mask(pixels, width, height, seed_x, seed_y, tolerance=32,
                    connectivity=4):
    """Magic wand from a seed; returns ``(mask, bbox, count)``.

    ``mask`` is ``bytes(width*height)`` with 255 for selected pixels and 0
    otherwise, ``bbox`` is ``(x, y, w, h)`` or ``None``, and ``count`` is the
    number of selected pixels. Matching is against the seed pixel's own BGRA.
    """
    _check_pixels(pixels, width, height)
    _check_tolerance(tolerance)
    if not isinstance(connectivity, int) or connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")
    if not isinstance(seed_x, int) or not isinstance(seed_y, int):
        raise ValueError("seed coordinates must be integers")
    if not (0 <= seed_x < width and 0 <= seed_y < height):
        raise ValueError(
            "seed ({0}, {1}) is outside the image ({2}x{3})".format(
                seed_x, seed_y, width, height))

    seed_offset = (seed_y * width + seed_x) * 4
    seed_color = tuple(pixels[seed_offset:seed_offset + 4])
    match = color_match_mask(pixels, width, height, seed_color, tolerance)
    out, bbox, count = _span_fill(
        match, width, height, seed_x, seed_y, connectivity)
    return bytes(out), bbox, count


def fill_holes(mask, width, height, threshold=128):
    """Return bytes(width*height): ``mask`` (8-bit selection, any values) with
    every hole filled to 255.

    A hole is a connected (4-connectivity) group of pixels with value below
    ``threshold`` that cannot reach the image border through other
    below-threshold pixels. Selected pixels (>= threshold) keep their original
    value (soft edges preserved); pixels connected to the border keep theirs.
    """
    _check_mask(mask, width, height)
    _check_threshold(threshold)
    mask = bytes(mask)
    unselected = mask.translate(
        bytes(1 if value < threshold else 0 for value in range(256)))

    padded_width = width + 2
    padded_height = height + 2
    padded = bytearray(b"\x01" * (padded_width * padded_height))
    for y in range(height):
        source = y * width
        target = (y + 1) * padded_width + 1
        padded[target:target + width] = unselected[source:source + width]
    outside_padded, _, _ = _span_fill(
        padded, padded_width, padded_height, 0, 0, 4)

    outside = bytearray(width * height)
    for y in range(height):
        source = (y + 1) * padded_width + 1
        target = y * width
        outside[target:target + width] = outside_padded[source:source + width]

    enclosed = int.from_bytes(unselected, "big") & int.from_bytes(
        outside.translate(_INVERT_FLAGS), "big")
    if not enclosed:
        return mask
    fill = enclosed.to_bytes(width * height, "big").translate(_HOLE_FILL)
    return (int.from_bytes(mask, "big") | int.from_bytes(fill, "big")).to_bytes(
        width * height, "big")


def alpha_mask(pixels, width, height, threshold=0):
    """bytes(width*height): the A channel itself when threshold == 0; with
    threshold > 0, 255 where A >= threshold else 0."""
    _check_pixels(pixels, width, height)
    _check_threshold(threshold)
    alpha = pixels[3::4]
    if threshold == 0:
        return bytes(alpha)
    table = bytes(255 if value >= threshold else 0 for value in range(256))
    return bytes(alpha.translate(table))


def mask_bbox(mask, width, height):
    """(x, y, w, h) of nonzero mask bytes or None."""
    _check_mask(mask, width, height)
    flags = bytes(mask).translate(_NONZERO_TABLE)
    first = flags.find(1)
    if first < 0:
        return None
    last = flags.rfind(1)
    top = first // width
    bottom = last // width
    left = width
    right = -1
    for y in range(top, bottom + 1):
        start = y * width
        row_left = flags.find(1, start, start + width)
        if row_left < 0:
            continue
        row_right = flags.rfind(1, start, start + width)
        x = row_left - start
        if x < left:
            left = x
        x = row_right - start
        if x > right:
            right = x
    return (left, top, right - left + 1, bottom - top + 1)
