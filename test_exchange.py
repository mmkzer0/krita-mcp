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

    def test_export_refuses_a_symlink_target(self):
        real = self.path("real.png")
        with open(real, "wb") as handle:
            handle.write(b"stand-in")
        link = self.path("link.png")
        os.symlink(real, link)
        for overwrite in (False, True):
            with self.assertRaises(exchange.ExchangeError) as caught:
                exchange.check_export_path(link, overwrite=overwrite)
            self.assertIn("symbolic link", str(caught.exception))

    def test_export_refuses_a_dangling_symlink(self):
        absent = self.path("absent-target.png")
        link = self.path("dangling.png")
        os.symlink(absent, link)
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_export_path(link)
        self.assertIn("symbolic link", str(caught.exception))
        self.assertFalse(os.path.exists(absent))
        # "xb" refuses the link at the kernel level too: no target appears.
        with self.assertRaises(exchange.ExchangeError):
            exchange.write_png_file(link, b"data")
        self.assertFalse(os.path.exists(absent))

    def test_import_refuses_a_symlink(self):
        real = self.path("real-image.png")
        with open(real, "wb") as handle:
            handle.write(b"stand-in")
        link = self.path("link-image.png")
        os.symlink(real, link)
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_import_path(link)
        self.assertIn("symbolic link", str(caught.exception))

    def test_paths_with_a_nul_byte_are_refused(self):
        for check in (exchange.check_export_path, exchange.check_import_path):
            with self.assertRaises(exchange.ExchangeError) as caught:
                check(self.path("page\x00.png"))
            self.assertIn("NUL", str(caught.exception))

    def test_import_refuses_missing_file_and_relative_path(self):
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_import_path(self.path("absent.png"))
        self.assertIn("No file at", str(caught.exception))
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_import_path("region.png")
        self.assertIn("absolute", str(caught.exception))


class WriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp(prefix="exchange-write-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.folder, name)

    def test_exclusive_create_writes_once(self):
        target = self.path("exclusive.png")
        exchange.write_png_file(target, b"first")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"first")
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.write_png_file(target, b"second")
        self.assertIn("already exists", str(caught.exception))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"first")

    def test_overwrite_renames_a_sibling_temp_over_the_target(self):
        target = self.path("atomic.png")
        exchange.write_png_file(target, b"old")
        inode = os.stat(target).st_ino
        exchange.write_png_file(target, b"new", overwrite=True)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"new")
        # os.replace() puts the temp file's inode at the target: a rename,
        # not a truncate-then-write a reader could catch half done.
        self.assertNotEqual(os.stat(target).st_ino, inode)
        leftovers = [name for name in os.listdir(self.folder)
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_write_failures_are_reported_separately_from_arguments(self):
        target = self.path(os.path.join("absent-dir", "page.png"))
        with self.assertRaises(exchange.ExchangeWriteError) as caught:
            exchange.write_png_file(target, b"data")
        self.assertIn("Could not write", str(caught.exception))
        self.assertEqual(exchange.ExchangeWriteError.kind, "internal")


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

    def test_import_budget_is_lower_than_the_export_budget(self):
        self.assertEqual(
            exchange.check_pixel_budget((0, 0, 4000, 4000),
                                        limit=exchange.MAX_IMPORT_PIXELS),
            16 * 1000 * 1000)
        with self.assertRaises(exchange.ExchangeError) as caught:
            exchange.check_pixel_budget((0, 0, 4001, 4000),
                                        limit=exchange.MAX_IMPORT_PIXELS,
                                        what="Importing")
        self.assertIn("16 megapixels", str(caught.exception))
        self.assertIn("16004000", str(caught.exception))

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


class FiniteNumberTests(unittest.TestCase):
    def test_non_finite_numbers_are_refused(self):
        for value in (float("inf"), float("-inf"), float("nan"),
                      "1e999", "Infinity", 10 ** 400):
            with self.assertRaises(exchange.ExchangeError) as caught:
                exchange.parse_region({"x": value, "width": 10,
                                       "height": 10})
            self.assertIn("finite", str(caught.exception))
            self.assertIn("region.x", str(caught.exception))

    def test_every_numeric_field_holds_the_line(self):
        for call in (
            lambda: exchange.parse_pad(float("inf")),
            lambda: exchange.parse_region({"width": "1e999", "height": 10}),
            lambda: exchange.parse_expect_size([float("inf"), 10]),
            lambda: exchange.parse_expect_size([10, 10 ** 400]),
        ):
            with self.assertRaises(exchange.ExchangeError) as caught:
                call()
            self.assertIn("finite", str(caught.exception))

    def test_ordinary_values_still_parse(self):
        self.assertEqual(exchange.parse_region(
            {"x": -5, "y": "7", "width": 10.4, "height": 2}), (-5, 7, 10, 2))


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

    def test_merge_replace_is_byte_exact_at_full_coverage(self):
        before = bytes(bytearray((0x01, 0x02, 0x03, 0x04) * 6))
        # A semi-transparent stamp: premultiplied Qt would round these bytes.
        stamp = bytes(bytearray((0x80, 0x40, 0x20, 0x10) * 6))
        blended = bytes(bytearray((0xEE, 0xDD, 0xCC, 0xBB) * 6))
        coverage = bytes((0, 1, 128, 254, 255, 255))
        merged = exchange.merge_replace(before, stamp, coverage, blended)
        for pixel in range(6):
            chunk = merged[pixel * 4:pixel * 4 + 4]
            if coverage[pixel] == 255:
                expected = stamp[pixel * 4:pixel * 4 + 4]
            elif coverage[pixel] == 0:
                expected = before[pixel * 4:pixel * 4 + 4]
            else:
                expected = blended[pixel * 4:pixel * 4 + 4]
            self.assertEqual(chunk, expected, "pixel {0}".format(pixel))
        self.assertEqual(merged[16:24], stamp[16:24])

    def test_merge_replace_starts_from_the_blend(self):
        before = bytes((1, 2, 3, 4)) * 4
        stamp = bytes((9, 8, 7, 6)) * 4
        blended = bytes((5, 5, 5, 5)) * 4
        # Every pixel partly covered: the blend is the answer, untouched.
        self.assertEqual(exchange.merge_replace(before, stamp, b"\x80" * 4,
                                                blended), blended)
        merged = exchange.merge_replace(before, stamp,
                                        bytes((0, 128, 255, 1)), blended)
        self.assertEqual(merged[0:4], before[0:4])
        self.assertEqual(merged[4:8], blended[4:8])
        self.assertEqual(merged[8:12], stamp[8:12])
        self.assertEqual(merged[12:16], blended[12:16])

    def test_merge_replace_without_a_blend_keeps_partial_pixels(self):
        before = b"\x11" * 8
        stamp = b"\x22" * 8
        merged = exchange.merge_replace(before, stamp, bytes((255, 128)))
        self.assertEqual(merged[0:4], stamp[0:4])
        self.assertEqual(merged[4:8], before[4:8])

    def test_merge_replace_refuses_size_mismatch(self):
        with self.assertRaises(exchange.ExchangeError):
            exchange.merge_replace(b"\x00" * 8, b"\x00" * 8, b"\xff" * 1)
        with self.assertRaises(exchange.ExchangeError):
            exchange.merge_replace(b"\x00" * 8, b"\x00" * 4, b"\xff" * 2)
        with self.assertRaises(exchange.ExchangeError):
            exchange.merge_replace(b"\x00" * 8, b"\x00" * 8, b"\xff" * 2,
                                   b"\x00" * 4)

    def test_partial_coverage_detection(self):
        self.assertFalse(exchange.has_partial_coverage(b""))
        self.assertFalse(exchange.has_partial_coverage(b"\x00\xff\x00"))
        self.assertTrue(exchange.has_partial_coverage(b"\x00\x01\xff"))
        self.assertTrue(exchange.has_partial_coverage(b"\xfe"))

    def test_alpha_within_keeps_transparent_pixels_out(self):
        before_alpha = bytes((0, 0, 1, 128, 255, 0))
        stamp_alpha = bytes((255, 0, 255, 7, 9, 4))
        self.assertEqual(exchange.alpha_within(before_alpha, stamp_alpha),
                         bytes((0, 0, 255, 7, 9, 0)))
        with self.assertRaises(exchange.ExchangeError):
            exchange.alpha_within(b"\x00", b"\x00\x00")


class ErrorTypeTests(unittest.TestCase):
    def test_exchange_error_is_a_value_error(self):
        self.assertTrue(issubclass(exchange.ExchangeError, ValueError))

    def test_write_failure_has_its_own_kind(self):
        self.assertTrue(issubclass(exchange.ExchangeWriteError,
                                   exchange.ExchangeError))
        self.assertEqual(exchange.ExchangeError.kind, "invalid_request")
        self.assertEqual(exchange.ExchangeWriteError.kind, "internal")

    def test_limits_are_the_documented_ones(self):
        self.assertEqual(exchange.MAX_PAD, 1024)
        self.assertEqual(exchange.MAX_PIXELS, 64 * 1000 * 1000)
        self.assertEqual(exchange.MAX_IMPORT_PIXELS, 16 * 1000 * 1000)
        self.assertEqual(exchange.MODES, ("over", "replace"))
        self.assertEqual(exchange.IMPORT_SUFFIXES,
                         (".png", ".jpg", ".jpeg", ".webp"))


if __name__ == "__main__":
    unittest.main()
