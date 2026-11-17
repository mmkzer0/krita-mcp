#!/usr/bin/env python3
"""Offline contract tests for the region exchange helpers (no Krita, no Qt).

Loads ``pykrita/krita_mcp/exchange_ops.py`` by path and exercises the refusals,
the rect arithmetic, the argument parsing and the pixel-buffer merge.

    python3 test_exchange.py
"""

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest

MODULE = (Path(__file__).resolve().parent / "pykrita" / "krita_mcp"
          / "exchange_ops.py")
SPEC = importlib.util.spec_from_file_location("exchange_ops", MODULE)
exchange = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exchange)

CANVAS = (0, 0, 1200, 900)


class PathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp(prefix="exchange-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.folder, name)

    def test_export_refuses_relative_path(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path("page.png")
        self.assertIn("absolute", str(caught.exception))

    def test_export_refuses_empty_path(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path("")
        self.assertIn("non-empty", str(caught.exception))

    def test_export_refuses_other_extension(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path(self.path("page.jpg"))
        self.assertIn(".png", str(caught.exception))

    def test_export_accepts_upper_case_png_and_home(self):
        self.assertEqual(exchange.check_export_path(self.path("PAGE.PNG")),
                         self.path("PAGE.PNG"))
        home = exchange.check_export_path("~/exchange-test.png")
        self.assertTrue(os.path.isabs(home))
        self.assertTrue(home.startswith(os.path.expanduser("~")))

    def test_export_refuses_existing_file_unless_overwrite(self):
        target = self.path("taken.png")
        with open(target, "wb") as handle:
            handle.write(b"stand-in")
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path(target)
        self.assertIn("already exists", str(caught.exception))
        self.assertIn("overwrite=true", str(caught.exception))
        self.assertEqual(exchange.check_export_path(target, overwrite=True),
                         target)

    def test_export_refuses_missing_directory_without_creating_it(self):
        missing = self.path("not-created")
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path(os.path.join(missing, "page.png"))
        self.assertIn("does not exist", str(caught.exception))
        self.assertFalse(os.path.exists(missing))

    def test_export_refuses_a_directory_target(self):
        folder = self.path("folder.png")
        os.mkdir(folder)
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path(folder)
        self.assertIn("directory", str(caught.exception))

    def test_import_accepts_the_documented_extensions(self):
        for suffix in (".png", ".jpg", ".jpeg", ".webp", ".PNG"):
            target = self.path("region" + suffix)
            with open(target, "wb") as handle:
                handle.write(b"stand-in")
            self.assertEqual(exchange.check_import_path(target), target)

    def test_import_refuses_other_extensions(self):
        target = self.path("region.tif")
        with open(target, "wb") as handle:
            handle.write(b"stand-in")
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_import_path(target)
        self.assertIn(".webp", str(caught.exception))

    def test_import_refuses_missing_file_and_relative_path(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_import_path(self.path("absent.png"))
        self.assertIn("No file at", str(caught.exception))
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_import_path("region.png")
        self.assertIn("absolute", str(caught.exception))


class RectTests(unittest.TestCase):
    def test_expand_without_pad_is_the_region(self):
        self.assertEqual(exchange.expand_rect((10, 20, 30, 40), 0, CANVAS),
                         (10, 20, 30, 40))

    def test_expand_adds_pad_on_every_side(self):
        self.assertEqual(exchange.expand_rect((100, 100, 50, 60), 16, CANVAS),
                         (84, 84, 82, 92))

    def test_expand_clamps_negative_coordinates_to_the_canvas(self):
        self.assertEqual(exchange.expand_rect((-10, -20, 40, 50), 5, CANVAS),
                         (0, 0, 35, 35))

    def test_expand_clamps_at_the_far_edges(self):
        self.assertEqual(
            exchange.expand_rect((1180, 880, 20, 20), 16, CANVAS),
            (1164, 864, 36, 36))
        self.assertEqual(
            exchange.expand_rect((1180, 880, 20, 20), 0, CANVAS),
            (1180, 880, 20, 20))

    def test_expand_refuses_a_region_outside_the_canvas(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.expand_rect((1200, 10, 10, 10), 0, CANVAS)
        self.assertIn("outside", str(caught.exception))
        # Pad must not pull an off-canvas region back onto the page.
        with self.assertRaises(exchange.ExchangeError):
            exchange.expand_rect((1200, 10, 10, 10), 64, CANVAS)
        with self.assertRaises(exchange.ExchangeError):
            exchange.expand_rect((-40, 10, 30, 10), 0, CANVAS)

    def test_expand_keeps_a_region_that_starts_off_canvas(self):
        # Partially outside is clamped, not refused (same as get_image).
        self.assertEqual(exchange.expand_rect((-10, 10, 30, 10), 0, CANVAS),
                         (0, 10, 20, 10))

    def test_pixel_budget(self):
        self.assertEqual(exchange.check_pixel_budget((0, 0, 8000, 8000)),
                         exchange.MAX_PIXELS)
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_pixel_budget((0, 0, 8001, 8000))
        self.assertIn("64 megapixels", str(caught.exception))

    def test_place_inside_the_canvas(self):
        placed, clipped = exchange.place_rect(30, 40, 100, 80, CANVAS)
        self.assertEqual(placed, (30, 40, 100, 80))
        self.assertFalse(clipped)

    def test_place_clips_negative_origin(self):
        placed, clipped = exchange.place_rect(-25, -25, 100, 100, CANVAS)
        self.assertEqual(placed, (0, 0, 75, 75))
        self.assertTrue(clipped)

    def test_place_clips_past_the_far_edge(self):
        placed, clipped = exchange.place_rect(1150, 860, 100, 100, CANVAS)
        self.assertEqual(placed, (1150, 860, 50, 40))
        self.assertTrue(clipped)
        placed, clipped = exchange.place_rect(1100, 800, 100, 100, CANVAS)
        self.assertEqual(placed, (1100, 800, 100, 100))
        self.assertFalse(clipped)

    def test_place_refuses_an_image_off_the_canvas(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.place_rect(1200, 0, 10, 10, CANVAS)
        self.assertIn("outside", str(caught.exception))
        with self.assertRaises(exchange.ExchangeError):
            exchange.place_rect(0, -90, 10, 10, CANVAS)

    def test_place_an_image_larger_than_the_canvas(self):
        placed, clipped = exchange.place_rect(-100, -100, 1400, 1100, CANVAS)
        self.assertEqual(placed, CANVAS)
        self.assertTrue(clipped)


class RegionArgumentTests(unittest.TestCase):
    def test_defaults_and_rounding(self):
        self.assertEqual(exchange.parse_region({"width": 10, "height": 20}),
                         (0, 0, 10, 20))
        self.assertEqual(
            exchange.parse_region({"x": 1.6, "y": -2.4,
                                   "width": 10.4, "height": 20.6}),
            (2, -2, 10, 21))
        self.assertEqual(exchange.parse_region({"x": -5, "width": 1,
                                                "height": 1})[0], -5)

    def test_missing_size_names_the_field(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.parse_region({"width": 10})
        self.assertIn("region.height is required", str(caught.exception))
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.parse_region({"height": 10})
        self.assertIn("region.width is required", str(caught.exception))

    def test_non_object(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.parse_region(None)
        self.assertIn("object with x, y, width, height",
                      str(caught.exception))

    def test_empty_or_negative_size(self):
        for size in (0, -3):
            with self.assertRaises(exchange.ExchangeError):
                exchange.parse_region({"width": size, "height": 10})

    def test_numbers_must_be_numbers(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.parse_region({"width": "wide", "height": 10})
        self.assertIn("region.width", str(caught.exception))
        with self.assertRaises(exchange.ExchangeError):
            exchange.parse_region({"width": True, "height": 10})


class PadTests(unittest.TestCase):
    def test_default_and_range(self):
        self.assertEqual(exchange.parse_pad(None), 0)
        self.assertEqual(exchange.parse_pad("16"), 16)
        self.assertEqual(exchange.parse_pad(1024), 1024)
        for value in (-1, 1025):
            with self.assertRaises(exchange.ExchangeError) as caught:
                exchange.parse_pad(value)
            self.assertIn("between 0 and 1024", str(caught.exception))


class ModeTests(unittest.TestCase):
    def test_default_and_case(self):
        self.assertEqual(exchange.parse_mode(None), "over")
        self.assertEqual(exchange.parse_mode("OVER"), "over")
        self.assertEqual(exchange.parse_mode(" Replace "), "replace")

    def test_refusals_name_the_modes(self):
        for value in ("add", 3, ""):
            with self.assertRaises(exchange.ExchangeError) as caught:
                exchange.parse_mode(value)
            self.assertIn('"over" or "replace"', str(caught.exception))


class SelectionOnlyTests(unittest.TestCase):
    def test_default_and_accepts_bools_and_strings(self):
        self.assertFalse(exchange.parse_bool(None, "selection_only"))
        self.assertTrue(exchange.parse_bool(True, "selection_only"))
        self.assertTrue(exchange.parse_bool("yes", "selection_only"))
        self.assertFalse(exchange.parse_bool("false", "selection_only"))
        self.assertEqual(exchange.parse_bool("0", "overwrite"), False)
        self.assertFalse(exchange.parse_bool(None, "overwrite", False))
        self.assertTrue(exchange.parse_bool(None, "overwrite", True))

    def test_refusal_names_the_field(self):
        for value in ("maybe", 1, 1.5, []):
            with self.assertRaises(exchange.ExchangeError) as caught:
                exchange.parse_bool(value, "selection_only")
            self.assertIn("selection_only", str(caught.exception))
            self.assertIn("true or false", str(caught.exception))


class ExpectSizeTests(unittest.TestCase):
    def test_default_and_parsing(self):
        self.assertIsNone(exchange.parse_expect_size(None))
        self.assertEqual(exchange.parse_expect_size([432, 332]), (432, 332))
        self.assertEqual(exchange.parse_expect_size((10.4, 20.6)), (10, 21))

    def test_refusals(self):
        for value in ([432], [1, 2, 3], "432x332", {"width": 1}, [0, 10],
                      [True, 10]):
            with self.assertRaises(exchange.ExchangeError) as caught:
                exchange.parse_expect_size(value)
            self.assertIn("expect_size", str(caught.exception))


class BufferTests(unittest.TestCase):
    def test_count_matches_a_naive_count(self):
        before = bytes(bytearray(range(256)) * 16)
        after = bytearray(before)
        for index in (0, 3, 7, 100, 500):
            after[index * 4 + 2] ^= 0xFF
        changed = exchange.count_changed_pixels(before, bytes(after))
        naive = sum(1 for i in range(len(before) // 4)
                    if before[i * 4:i * 4 + 4]
                    != bytes(after)[i * 4:i * 4 + 4])
        self.assertEqual(changed, naive)
        self.assertEqual(changed, 5)

    def test_count_refuses_mismatched_buffers(self):
        with self.assertRaises(exchange.ExchangeError):
            exchange.count_changed_pixels(b"\x00" * 8, b"\x00" * 4)
        with self.assertRaises(exchange.ExchangeError):
            exchange.count_changed_pixels(b"\x00" * 6, b"\x00" * 6)

    def test_count_of_identical_and_empty(self):
        self.assertEqual(exchange.count_changed_pixels(b"", b""), 0)
        data = bytes(bytearray(range(64)))
        self.assertEqual(exchange.count_changed_pixels(data, data), 0)

    def test_merge_takes_only_covered_pixels(self):
        before = b"\x11" * 24
        after = b"\x22" * 24
        coverage = bytes((0, 1, 0, 0, 255, 255))
        merged = exchange.merge_by_mask(before, after, coverage)
        self.assertEqual(len(merged), 24)
        for pixel in range(6):
            expected = b"\x22" * 4 if coverage[pixel] else b"\x11" * 4
            self.assertEqual(merged[pixel * 4:pixel * 4 + 4], expected)

    def test_merge_leaves_everything_when_coverage_is_zero(self):
        before = bytes(bytearray(range(40)))
        after = b"\xff" * 40
        self.assertEqual(exchange.merge_by_mask(before, after, b"\x00" * 10),
                         before)

    def test_merge_takes_everything_when_fully_covered(self):
        before = b"\x00" * 40
        after = bytes(bytearray(range(40)))
        self.assertEqual(exchange.merge_by_mask(before, after, b"\xff" * 10),
                         after)

    def test_merge_refuses_size_mismatch(self):
        with self.assertRaises(exchange.ExchangeError):
            exchange.merge_by_mask(b"\x00" * 8, b"\x00" * 8, b"\xff" * 1)
        with self.assertRaises(exchange.ExchangeError):
            exchange.merge_by_mask(b"\x00" * 8, b"\x00" * 4, b"\xff" * 2)


class ErrorTypeTests(unittest.TestCase):
    def test_exchange_error_is_a_value_error(self):
        self.assertTrue(issubclass(exchange.ExchangeError, ValueError))

    def test_limits_are_the_documented_ones(self):
        self.assertEqual(exchange.MAX_PAD, 1024)
        self.assertEqual(exchange.MAX_PIXELS, 64 * 1000 * 1000)
        self.assertEqual(exchange.MODES, ("over", "replace"))


if __name__ == "__main__":
    unittest.main()
