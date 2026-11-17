#!/usr/bin/env python3
"""Offline contract tests for the pure pixel-mask helpers.

Loads ``pykrita/krita_mcp/raster.py`` by path; no Krita or Qt import.

    python3 test_raster.py
"""

import importlib.util
from pathlib import Path
import time
import unittest


SPEC = importlib.util.spec_from_file_location(
    "raster", Path(__file__).resolve().parent / "pykrita" / "krita_mcp" / "raster.py")
raster = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(raster)

WHITE = b"\xff\xff\xff\xff"
BLACK = b"\x00\x00\x00\xff"

PAGE_WIDTH = 2500
PAGE_HEIGHT = 3600
PAGE_MARGIN = 4
PAGE_OUTLINE = 3
INNER_BOX = (600, 600, 900, 900)
INNER_OUTLINE = 2


def _pixel(b, g, r, a=255):
    return bytes((b, g, r, a))


def _solid(width, height, color):
    return bytearray(color) * (width * height)


def _set_pixel(pixels, width, x, y, color):
    offset = (y * width + x) * 4
    pixels[offset:offset + 4] = color


def _fill_rect(pixels, width, x0, y0, x1, y1, color):
    stride = len(color)
    segment = color * (x1 - x0)
    for y in range(y0, y1):
        offset = (y * width + x0) * stride
        pixels[offset:offset + len(segment)] = segment


def _rectangle_outline(pixels, width, x0, y0, x1, y1, thickness, color):
    _fill_rect(pixels, width, x0, y0, x1, y0 + thickness, color)
    _fill_rect(pixels, width, x0, y1 - thickness, x1, y1, color)
    _fill_rect(pixels, width, x0, y0 + thickness, x0 + thickness, y1 - thickness, color)
    _fill_rect(pixels, width, x1 - thickness, y0 + thickness, x1, y1 - thickness, color)


def _ring_mask(width, height, x0, y0, x1, y1, value=200):
    mask = bytearray(width * height)
    _rectangle_outline(mask, width, x0, y0, x1, y1, 1, bytes((value,)))
    return mask


class ColorMatchMaskTests(unittest.TestCase):
    def test_bgra_channel_order(self):
        red = b"\x00\x00\xff\xff"
        blue = b"\xff\x00\x00\xff"
        self.assertEqual(red, bytes((0, 0, 255, 255)))
        pixels = blue + red
        mask = raster.color_match_mask(pixels, 2, 1, (0, 0, 255, 255), 0)
        self.assertIsInstance(mask, bytes)
        self.assertEqual(mask, b"\x00\x01")
        self.assertEqual(
            raster.color_match_mask(pixels, 2, 1, (255, 0, 0, 255), 0), b"\x01\x00")

    def test_tolerance_boundary(self):
        pixel = _pixel(10, 20, 30, 40)
        self.assertEqual(
            raster.color_match_mask(pixel, 1, 1, (20, 20, 30, 40), 10), b"\x01")
        self.assertEqual(
            raster.color_match_mask(pixel, 1, 1, (20, 20, 30, 40), 9), b"\x00")
        self.assertEqual(
            raster.color_match_mask(pixel, 1, 1, (0, 20, 30, 40), 10), b"\x01")
        self.assertEqual(
            raster.color_match_mask(pixel, 1, 1, (0, 20, 30, 40), 9), b"\x00")

    def test_all_channels_participate(self):
        pixel = _pixel(10, 20, 30, 40)
        for channel in range(4):
            color = [10, 20, 30, 40]
            color[channel] += 1
            with self.subTest(channel=channel):
                self.assertEqual(
                    raster.color_match_mask(pixel, 1, 1, tuple(color), 0), b"\x00")
                self.assertEqual(
                    raster.color_match_mask(pixel, 1, 1, tuple(color), 1), b"\x01")

    def test_multi_pixel_mask(self):
        pixels = _pixel(0, 0, 0) + _pixel(1, 1, 1) + _pixel(2, 2, 2)
        self.assertEqual(
            raster.color_match_mask(pixels, 3, 1, (0, 0, 0, 255), 1),
            b"\x01\x01\x00")


class AlphaMaskTests(unittest.TestCase):
    def setUp(self):
        self.pixels = b"".join((
            _pixel(10, 20, 30, 0),
            _pixel(1, 2, 3, 1),
            _pixel(4, 5, 6, 127),
            _pixel(7, 8, 9, 128),
            _pixel(10, 11, 12, 200),
            _pixel(13, 14, 15, 255),
        ))

    def test_raw_alpha(self):
        self.assertEqual(
            raster.alpha_mask(self.pixels, 6, 1),
            bytes((0, 1, 127, 128, 200, 255)))
        self.assertEqual(
            raster.alpha_mask(self.pixels, 6, 1, threshold=0),
            bytes((0, 1, 127, 128, 200, 255)))

    def test_threshold(self):
        self.assertEqual(
            raster.alpha_mask(self.pixels, 6, 1, threshold=128),
            bytes((0, 0, 0, 255, 255, 255)))
        self.assertEqual(
            raster.alpha_mask(self.pixels, 6, 1, threshold=200),
            bytes((0, 0, 0, 0, 255, 255)))
        self.assertEqual(
            raster.alpha_mask(self.pixels, 6, 1, threshold=1),
            bytes((0, 255, 255, 255, 255, 255)))

    def test_rejects_bad_threshold_and_buffer(self):
        for threshold in (-1, 256, 1.5, "8"):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                raster.alpha_mask(self.pixels, 6, 1, threshold)
        with self.assertRaises(ValueError):
            raster.alpha_mask(self.pixels[:-1], 6, 1)
        with self.assertRaises(ValueError):
            raster.alpha_mask(self.pixels, 0, 1)


class MaskBboxTests(unittest.TestCase):
    def test_empty_mask(self):
        self.assertIsNone(raster.mask_bbox(bytes(12), 4, 3))

    def test_single_pixel(self):
        width, height = 5, 4
        mask = bytearray(width * height)
        mask[1 * width + 2] = 255
        self.assertEqual(raster.mask_bbox(mask, width, height), (2, 1, 1, 1))

    def test_nonzero_values(self):
        width, height = 8, 6
        mask = bytearray(width * height)
        for y in range(2, 5):
            for x in range(1, 4):
                mask[y * width + x] = 7
        self.assertEqual(raster.mask_bbox(bytes(mask), width, height), (1, 2, 3, 3))

    def test_sparse_rows(self):
        width, height = 6, 5
        mask = bytearray(width * height)
        mask[0 * width + 4] = 1
        mask[2 * width + 2] = 1
        mask[4 * width + 1] = 1
        self.assertEqual(raster.mask_bbox(mask, width, height), (1, 0, 4, 5))

    def test_rejects_bad_size_and_buffer(self):
        for width, height, mask in (
                (2, 2, bytes(3)),
                (2, 2, bytes(5)),
                (0, 2, b""),
                (2, -1, b"")):
            with self.subTest(width=width, height=height, length=len(mask)), \
                    self.assertRaises(ValueError):
                raster.mask_bbox(mask, width, height)


class ContiguousMaskTests(unittest.TestCase):
    def test_region_bounded_by_ring(self):
        width = height = 12
        pixels = _solid(width, height, BLACK)
        _fill_rect(pixels, width, 3, 3, 9, 9, WHITE)
        for connectivity in (4, 8):
            with self.subTest(connectivity=connectivity):
                mask, bbox, count = raster.contiguous_mask(
                    pixels, width, height, 6, 6, tolerance=0,
                    connectivity=connectivity)
                self.assertIsInstance(mask, bytes)
                self.assertEqual(count, 36)
                self.assertEqual(bbox, (3, 3, 6, 6))
                self.assertEqual(sum(mask), 36 * 255)
                self.assertLessEqual(set(mask), {0, 255})
                for x, y in ((3, 3), (8, 8), (3, 8), (8, 3)):
                    self.assertEqual(mask[y * width + x], 255)
                for x, y in ((2, 2), (9, 9), (2, 3), (3, 2), (8, 9), (9, 8)):
                    self.assertEqual(mask[y * width + x], 0)

    def test_diagonal_gap_ring(self):
        width = height = 7
        pixels = _solid(width, height, WHITE)
        for y in range(1, 6):
            for x in range(1, 6):
                if not (2 <= x <= 4 and 2 <= y <= 4):
                    _set_pixel(pixels, width, x, y, BLACK)
        _set_pixel(pixels, width, 1, 1, WHITE)  # diagonal-only pinhole
        mask4, bbox4, count4 = raster.contiguous_mask(
            pixels, width, height, 3, 3, tolerance=0, connectivity=4)
        self.assertEqual(count4, 9)
        self.assertEqual(bbox4, (2, 2, 3, 3))
        self.assertEqual(mask4[1 * width + 1], 0)
        self.assertEqual(mask4[0], 0)
        mask8, bbox8, count8 = raster.contiguous_mask(
            pixels, width, height, 3, 3, tolerance=0, connectivity=8)
        self.assertEqual(count8, 34)
        self.assertEqual(bbox8, (0, 0, 7, 7))
        self.assertEqual(mask8[1 * width + 1], 255)
        self.assertEqual(mask8[0], 255)
        self.assertEqual(mask8[6 * width + 6], 255)

    def test_connectivity_diagonal_pixels(self):
        width = height = 4
        pixels = _solid(width, height, BLACK)
        _set_pixel(pixels, width, 1, 1, WHITE)
        _set_pixel(pixels, width, 2, 2, WHITE)
        mask4, bbox4, count4 = raster.contiguous_mask(
            pixels, width, height, 1, 1, tolerance=0, connectivity=4)
        self.assertEqual((count4, bbox4), (1, (1, 1, 1, 1)))
        self.assertEqual(mask4[2 * width + 2], 0)
        self.assertEqual(mask4[1 * width + 2], 0)
        mask8, bbox8, count8 = raster.contiguous_mask(
            pixels, width, height, 1, 1, tolerance=0, connectivity=8)
        self.assertEqual((count8, bbox8), (2, (1, 1, 2, 2)))
        self.assertEqual(mask8[1 * width + 1], 255)
        self.assertEqual(mask8[2 * width + 2], 255)
        self.assertEqual(mask8[1 * width + 2], 0)

    def test_region_touching_edges(self):
        width, height = 8, 6
        pixels = _solid(width, height, WHITE)
        _fill_rect(pixels, width, 3, 2, 5, 4, BLACK)
        mask, bbox, count = raster.contiguous_mask(
            pixels, width, height, 0, 0, tolerance=0)
        self.assertEqual(count, width * height - 4)
        self.assertEqual(bbox, (0, 0, width, height))
        for x, y in ((0, 0), (7, 0), (0, 5), (7, 5)):
            self.assertEqual(mask[y * width + x], 255)
        self.assertEqual(mask[2 * width + 3], 0)
        mask2, bbox2, count2 = raster.contiguous_mask(
            pixels, width, height, 7, 5, tolerance=0)
        self.assertEqual((count2, bbox2), (count, bbox))

    def test_isolated_pixel_seed(self):
        width, height = 5, 7
        pixels = _solid(width, height, BLACK)
        _set_pixel(pixels, width, 3, 4, WHITE)
        mask, bbox, count = raster.contiguous_mask(
            pixels, width, height, 3, 4, tolerance=0)
        self.assertEqual((count, bbox), (1, (3, 4, 1, 1)))
        self.assertEqual(mask[4 * width + 3], 255)
        self.assertEqual(sum(mask), 255)

    def test_bbox_and_count(self):
        width, height = 10, 8
        pixels = _solid(width, height, BLACK)
        _fill_rect(pixels, width, 2, 1, 6, 4, WHITE)
        mask, bbox, count = raster.contiguous_mask(
            pixels, width, height, 4, 2, tolerance=0)
        self.assertEqual(count, 12)
        self.assertEqual(bbox, (2, 1, 4, 3))
        self.assertEqual(raster.mask_bbox(mask, width, height), bbox)
        selected = [index for index, value in enumerate(mask) if value]
        self.assertEqual(len(selected), 12)
        self.assertEqual(min(index % width for index in selected), 2)
        self.assertEqual(max(index % width for index in selected), 5)
        self.assertEqual(min(index // width for index in selected), 1)
        self.assertEqual(max(index // width for index in selected), 3)

    def test_tolerance_relative_to_seed(self):
        pixels = b"".join(bytes((value, 20, 30, 255)) for value in (10, 15, 20, 25, 30))
        mask, bbox, count = raster.contiguous_mask(
            pixels, 5, 1, 2, 0, tolerance=5)
        self.assertEqual((count, bbox), (3, (1, 0, 3, 1)))
        self.assertEqual(list(mask), [0, 255, 255, 255, 0])

    def test_default_tolerance(self):
        pixels = b"".join(bytes(color) for color in (
            (200, 20, 30, 255), (168, 20, 30, 255), (167, 20, 30, 255)))
        mask, bbox, count = raster.contiguous_mask(pixels, 3, 1, 0, 0)
        self.assertEqual((count, bbox), (2, (0, 0, 2, 1)))
        self.assertEqual(list(mask), [255, 255, 0])


class FillHolesTests(unittest.TestCase):
    def test_ring_hole_filled_outside_untouched(self):
        width = height = 9
        mask = _ring_mask(width, height, 2, 2, 7, 7, 200)
        filled = raster.fill_holes(mask, width, height)
        self.assertIsInstance(filled, bytes)
        self.assertEqual(len(filled), width * height)
        for y in range(height):
            for x in range(width):
                if 2 < x < 6 and 2 < y < 6:
                    self.assertEqual(filled[y * width + x], 255)
                elif 2 <= x <= 6 and 2 <= y <= 6:
                    self.assertEqual(filled[y * width + x], 200)
                else:
                    self.assertEqual(filled[y * width + x], 0)

    def test_c_shape_open_to_outside_not_filled(self):
        width = height = 7
        mask = bytearray(width * height)
        for y in range(2, 6):
            mask[y * width + 2] = 200
        for x in range(2, 5):
            mask[2 * width + x] = 200
            mask[5 * width + x] = 200
        filled = raster.fill_holes(mask, width, height)
        for x, y in ((3, 3), (4, 3), (3, 4), (4, 4), (5, 3), (6, 4)):
            self.assertEqual(filled[y * width + x], 0)
        for x, y in ((2, 2), (4, 2), (2, 3), (2, 5), (4, 5)):
            self.assertEqual(filled[y * width + x], 200)

    def test_soft_edges_preserved(self):
        width = height = 9
        values = (128, 160, 200, 255)
        mask = bytearray(width * height)
        ring = []
        for x in range(2, 7):
            ring.append((x, 2))
            ring.append((x, 6))
        for y in range(3, 6):
            ring.append((2, y))
            ring.append((6, y))
        for index, (x, y) in enumerate(ring):
            mask[y * width + x] = values[index % len(values)]
        original = bytes(mask)
        filled = raster.fill_holes(original, width, height, threshold=128)
        for x, y in ring:
            self.assertEqual(filled[y * width + x], original[y * width + x])
        self.assertIn(128, original)
        for y in range(3, 6):
            for x in range(3, 6):
                self.assertEqual(filled[y * width + x], 255)
        self.assertEqual(filled[0], 0)
        self.assertEqual(filled[8 * width + 8], 0)

    def test_nested_ring_in_hole_in_ring(self):
        width = height = 13
        mask = bytearray(width * height)
        _rectangle_outline(mask, width, 1, 1, 12, 12, 1, b"\xc8")
        _rectangle_outline(mask, width, 4, 4, 9, 9, 1, b"\x96")
        filled = raster.fill_holes(mask, width, height)
        self.assertEqual(filled[6 * width + 6], 255)
        self.assertEqual(filled[6 * width + 3], 255)
        self.assertEqual(filled[2 * width + 2], 255)
        self.assertEqual(filled[1 * width + 1], 200)
        self.assertEqual(filled[4 * width + 4], 150)
        self.assertEqual(filled[0], 0)
        self.assertEqual(filled[12 * width + 12], 0)

    def test_threshold_boundary(self):
        width = height = 5
        mask = bytearray(width * height)
        _rectangle_outline(mask, width, 0, 0, 5, 5, 1, b"\xff")
        for y in range(1, 4):
            for x in range(1, 4):
                mask[y * width + x] = 127
        mask[2 * width + 2] = 128
        mask[1 * width + 1] = 129
        filled = raster.fill_holes(mask, width, height, threshold=128)
        self.assertEqual(filled[1 * width + 1], 129)
        self.assertEqual(filled[2 * width + 2], 128)
        self.assertEqual(filled[2 * width + 1], 255)
        self.assertEqual(filled[3 * width + 3], 255)
        self.assertEqual(filled[0], 255)
        filled = raster.fill_holes(mask, width, height, threshold=129)
        self.assertEqual(filled[1 * width + 1], 129)
        self.assertEqual(filled[2 * width + 2], 255)
        self.assertEqual(filled[2 * width + 1], 255)
        self.assertEqual(
            raster.fill_holes(mask, width, height, threshold=0), bytes(mask))
        filled = raster.fill_holes(mask, width, height, threshold=255)
        self.assertEqual(filled[2 * width + 2], 255)
        self.assertEqual(filled[1 * width + 1], 255)
        self.assertEqual(filled[0], 255)

    def test_all_zero_and_all_255(self):
        width, height = 6, 4
        zeros = bytes(width * height)
        self.assertEqual(raster.fill_holes(zeros, width, height), zeros)
        full = b"\xff" * (width * height)
        self.assertEqual(raster.fill_holes(full, width, height), full)
        mids = b"\x64" * (width * height)
        self.assertEqual(raster.fill_holes(mids, width, height), mids)
        self.assertEqual(raster.fill_holes(b"\x00", 1, 1), b"\x00")


class ValidationTests(unittest.TestCase):
    def test_color_match_mask_rejects_bad_input(self):
        pixels = bytes(16)
        cases = {
            "short buffer": lambda: raster.color_match_mask(
                pixels[:-1], 2, 2, (0, 0, 0, 255), 0),
            "long buffer": lambda: raster.color_match_mask(
                pixels + b"\x00", 2, 2, (0, 0, 0, 255), 0),
            "zero width": lambda: raster.color_match_mask(
                pixels, 0, 2, (0, 0, 0, 255), 0),
            "negative height": lambda: raster.color_match_mask(
                pixels, 2, -2, (0, 0, 0, 255), 0),
            "short color": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0), 0),
            "channel too high": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0, 256), 0),
            "negative channel": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0, -1), 0),
            "non-integer channel": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0, 1.5), 0),
            "negative tolerance": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0, 255), -1),
            "tolerance too high": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0, 255), 256),
            "non-integer tolerance": lambda: raster.color_match_mask(
                pixels, 2, 2, (0, 0, 0, 255), 1.0),
        }
        for name, call in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                call()

    def test_contiguous_mask_rejects_bad_input(self):
        pixels = bytes(16)
        cases = {
            "short buffer": lambda: raster.contiguous_mask(
                pixels[:-1], 2, 2, 0, 0, tolerance=0),
            "zero height": lambda: raster.contiguous_mask(
                pixels, 2, 0, 0, 0, tolerance=0),
            "seed x negative": lambda: raster.contiguous_mask(
                pixels, 2, 2, -1, 0, tolerance=0),
            "seed x too large": lambda: raster.contiguous_mask(
                pixels, 2, 2, 2, 0, tolerance=0),
            "seed y negative": lambda: raster.contiguous_mask(
                pixels, 2, 2, 0, -1, tolerance=0),
            "seed y too large": lambda: raster.contiguous_mask(
                pixels, 2, 2, 0, 2, tolerance=0),
            "bad connectivity": lambda: raster.contiguous_mask(
                pixels, 2, 2, 0, 0, tolerance=0, connectivity=6),
            "float connectivity": lambda: raster.contiguous_mask(
                pixels, 2, 2, 0, 0, tolerance=0, connectivity=4.0),
            "bad tolerance": lambda: raster.contiguous_mask(
                pixels, 2, 2, 0, 0, tolerance=256),
        }
        for name, call in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                call()

    def test_fill_holes_rejects_bad_input(self):
        mask = bytes(16)
        cases = {
            "short buffer": lambda: raster.fill_holes(mask[:-1], 2, 2),
            "long buffer": lambda: raster.fill_holes(mask + b"\x00", 2, 2),
            "zero width": lambda: raster.fill_holes(mask, 0, 2),
            "negative height": lambda: raster.fill_holes(mask, 2, -2),
            "negative threshold": lambda: raster.fill_holes(mask, 2, 2, -1),
            "threshold too high": lambda: raster.fill_holes(mask, 2, 2, 256),
            "non-integer threshold": lambda: raster.fill_holes(mask, 2, 2, 128.0),
            "string threshold": lambda: raster.fill_holes(mask, 2, 2, "128"),
        }
        for name, call in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                call()


class LargeImageTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pixels = _solid(PAGE_WIDTH, PAGE_HEIGHT, WHITE)
        _rectangle_outline(
            cls.pixels, PAGE_WIDTH, PAGE_MARGIN, PAGE_MARGIN,
            PAGE_WIDTH - PAGE_MARGIN, PAGE_HEIGHT - PAGE_MARGIN,
            PAGE_OUTLINE, BLACK)
        _rectangle_outline(
            cls.pixels, PAGE_WIDTH, INNER_BOX[0], INNER_BOX[1],
            INNER_BOX[2], INNER_BOX[3], INNER_OUTLINE, BLACK)

    def test_large_enclosed_region_timing(self):
        interior_w = PAGE_WIDTH - 2 * (PAGE_MARGIN + PAGE_OUTLINE)
        interior_h = PAGE_HEIGHT - 2 * (PAGE_MARGIN + PAGE_OUTLINE)
        inner_area = (INNER_BOX[2] - INNER_BOX[0]) * (INNER_BOX[3] - INNER_BOX[1])
        expected_count = interior_w * interior_h - inner_area
        center_x = PAGE_WIDTH // 2
        center_y = PAGE_HEIGHT // 2

        started = time.perf_counter()
        mask, bbox, count = raster.contiguous_mask(
            self.pixels, PAGE_WIDTH, PAGE_HEIGHT, center_x, center_y,
            tolerance=32, connectivity=4)
        elapsed = time.perf_counter() - started

        self.assertEqual(count, expected_count)
        self.assertEqual(
            bbox,
            (PAGE_MARGIN + PAGE_OUTLINE, PAGE_MARGIN + PAGE_OUTLINE,
             interior_w, interior_h))
        self.assertEqual(mask[center_y * PAGE_WIDTH + center_x], 255)
        self.assertEqual(mask[750 * PAGE_WIDTH + 750], 0)

        inner_w = (INNER_BOX[2] - INNER_BOX[0]) - 2 * INNER_OUTLINE
        inner_h = (INNER_BOX[3] - INNER_BOX[1]) - 2 * INNER_OUTLINE

        started = time.perf_counter()
        small_mask, small_bbox, small_count = raster.contiguous_mask(
            self.pixels, PAGE_WIDTH, PAGE_HEIGHT, 750, 750,
            tolerance=32, connectivity=4)
        small_elapsed = time.perf_counter() - started

        self.assertEqual(small_count, inner_w * inner_h)
        self.assertEqual(
            small_bbox,
            (INNER_BOX[0] + INNER_OUTLINE, INNER_BOX[1] + INNER_OUTLINE,
             inner_w, inner_h))
        self.assertEqual(small_mask[750 * PAGE_WIDTH + 750], 255)
        self.assertEqual(small_mask[750 * PAGE_WIDTH + 600], 0)
        self.assertEqual(small_mask[750 * PAGE_WIDTH + 599], 0)

        print("full-page white fill: {0:.3f}s ({1} px)".format(elapsed, count))
        print("300x300 enclosed fill: {0:.3f}s ({1} px)".format(
            small_elapsed, small_count))
        self.assertLess(elapsed, 8.0)

    def test_fill_holes_timing(self):
        mask = bytearray(PAGE_WIDTH * PAGE_HEIGHT)
        _rectangle_outline(
            mask, PAGE_WIDTH, PAGE_MARGIN, PAGE_MARGIN,
            PAGE_WIDTH - PAGE_MARGIN, PAGE_HEIGHT - PAGE_MARGIN,
            PAGE_OUTLINE, b"\xff")
        _rectangle_outline(
            mask, PAGE_WIDTH, INNER_BOX[0], INNER_BOX[1],
            INNER_BOX[2], INNER_BOX[3], INNER_OUTLINE, b"\xff")

        started = time.perf_counter()
        filled = raster.fill_holes(mask, PAGE_WIDTH, PAGE_HEIGHT, threshold=128)
        elapsed = time.perf_counter() - started

        outer_w = PAGE_WIDTH - 2 * PAGE_MARGIN
        outer_h = PAGE_HEIGHT - 2 * PAGE_MARGIN
        self.assertEqual(len(filled), PAGE_WIDTH * PAGE_HEIGHT)
        self.assertEqual(filled[0], 0)
        self.assertEqual(
            filled[(PAGE_HEIGHT - 1) * PAGE_WIDTH + (PAGE_WIDTH - 1)], 0)
        self.assertEqual(
            filled[PAGE_MARGIN * PAGE_WIDTH + PAGE_MARGIN], 255)
        self.assertEqual(
            filled[(PAGE_MARGIN + PAGE_OUTLINE) * PAGE_WIDTH
                   + PAGE_MARGIN + PAGE_OUTLINE], 255)
        self.assertEqual(filled[750 * PAGE_WIDTH + 750], 255)
        self.assertEqual(sum(filled), outer_w * outer_h * 255)

        print("fill_holes {0}x{1}: {2:.3f}s".format(
            PAGE_WIDTH, PAGE_HEIGHT, elapsed))
        self.assertLess(elapsed, 8.0)


if __name__ == "__main__":
    unittest.main()
