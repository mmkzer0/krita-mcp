#!/usr/bin/env python3
"""Unit tests for the bridge's discovery file (F4).

Krita-free: httpserver.py imports nothing from Qt or libkis at module level, so
the publication rules are exercised directly. The contract they defend is the
one that matters for a shared secret -- the token must never exist in a file
another account can read, a failed write must not damage the file that is
already published, and removing the file must respect a second instance.

    python3 test_httpserver.py
    python3 -m unittest test_httpserver -v
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _load(name, path):
    """Load a file by path under a private name (see test_gate.py)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


httpserver = _load("krita_httpserver_under_test",
                   HERE / "pykrita" / "krita_mcp" / "httpserver.py")


class DiscoveryFileTest(unittest.TestCase):
    """_write_info_file/_remove_info_file: publish the token safely."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="krita-discovery-")
        self._previous = os.environ.get("KRITA_MCP_STATE_DIR")
        os.environ["KRITA_MCP_STATE_DIR"] = self.state

        def restore():
            if self._previous is None:
                os.environ.pop("KRITA_MCP_STATE_DIR", None)
            else:
                os.environ["KRITA_MCP_STATE_DIR"] = self._previous

        self.addCleanup(restore)
        self.bridge = httpserver.Bridge(
            invoker=None, dispatch=None, timeout_for=None,
            plugin_version="1.0.0", krita_version="5.3.4",
            operations=("status",), logger=lambda text: None)
        self.bridge.port = 9797

    @property
    def info_path(self):
        return os.path.join(self.state, "krita_mcp_bridge.json")

    def _published(self):
        with open(self.info_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_publishes_what_a_client_needs_and_nothing_else(self):
        self.bridge._write_info_file()
        self.assertEqual(sorted(self._published()),
                         ["krita_version", "pid", "plugin_version", "port",
                          "token", "url"])

    @unittest.skipIf(sys.platform == "win32",
                     "POSIX file modes do not describe Windows ACLs")
    def test_the_token_file_is_owner_only_from_creation(self):
        # A permissive umask is the interesting case: the old implementation
        # wrote with the default mode and chmod'ed afterwards, so the token sat
        # in a readable file until that call ran -- and stayed there if the
        # process died in between.
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        with mock.patch.object(os, "chmod",
                               side_effect=AssertionError(
                                   "the mode must be set at creation")):
            self.bridge._write_info_file()
        mode = stat.S_IMODE(os.stat(self.info_path).st_mode)
        self.assertEqual(mode, 0o600, "expected mode 0600, got {0:o}".format(mode))

    def test_no_temporary_file_survives_a_successful_write(self):
        self.bridge._write_info_file()
        self.assertEqual(os.listdir(self.state), ["krita_mcp_bridge.json"])

    def test_a_failed_write_leaves_the_published_file_alone(self):
        self.bridge._write_info_file()
        before = self._published()
        with mock.patch.object(httpserver.json, "dump",
                               side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.bridge._write_info_file()
        self.assertEqual(self._published(), before,
                         "a failed write must not damage the live file")
        self.assertEqual(os.listdir(self.state), ["krita_mcp_bridge.json"],
                         "and must not leave its temporary file behind")

    def test_removing_the_file_respects_another_instance(self):
        self.bridge._write_info_file()
        self.bridge._remove_info_file()
        self.assertFalse(os.path.exists(self.info_path))

        other = {"port": 9999, "token": "theirs", "pid": os.getpid() + 1}
        with open(self.info_path, "w", encoding="utf-8") as handle:
            json.dump(other, handle)
        self.bridge._remove_info_file()
        self.assertEqual(self._published(), other,
                         "the discovery file belongs to the other instance")


if __name__ == "__main__":
    unittest.main()
