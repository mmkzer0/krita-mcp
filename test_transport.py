#!/usr/bin/env python3
"""Regression tests for the bridge client's transport failure handling.

Deterministic and Krita-free: a throwaway HTTP server stands in for the
bridge, so the phases of a failure -- refused, transmitted-but-unanswered,
answered -- can be produced exactly.

    python3 test_transport.py
    python3 -m unittest test_transport -v

The contract under test: a request is sent twice only when the first attempt
provably never reached the bridge. A response lost after transmission surfaces
as an unknown outcome instead of a silent replay, and a stale token is retried
because it is rejected before the operation runs.
"""

import http.server
import importlib.util
import json
import socket
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, path):
    """Load a file by path under a private name (see test_gate.py)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mcp_server = _load("krita_transport_mcp_server", HERE / "mcp_server.py")


class _StubBridge(http.server.BaseHTTPRequestHandler):
    """One recorded POST, then whatever the stub was told to do."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            request = json.loads(body.decode("utf-8"))
        except ValueError:
            request = None
        stub = self.server.stub
        stub.posts.append(request)

        unauthorized = (
            stub.mode == "unauthorized"
            or (stub.mode == "unauthorized-once" and len(stub.posts) == 1))
        if unauthorized:
            return self._json(401, {"ok": False, "error": {
                "type": "unauthorized", "message": "wrong token"}})
        if stub.mode == "drop":
            # The request was received and would have run; the client cannot
            # know that, which is exactly the ambiguous case.
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        return self._json(200, {"ok": True, "op": (request or {}).get("op"),
                                "result": {"stub": True}})


class _StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The "drop" mode breaks the connection on purpose; a traceback here
        # would only be noise.
        pass


class StubBridge(object):
    """A throwaway bridge with the failure modes the client must tell apart."""

    def __init__(self):
        self.posts = []
        self.mode = "ok"          # ok | drop | unauthorized | unauthorized-once
        self.server = _StubServer(("127.0.0.1", 0), _StubBridge)
        self.server.stub = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ScriptedClient(mcp_server.BridgeClient):
    """A BridgeClient whose discovery file is scripted by the test.

    ``infos`` is what each reload would find: entry 0 the first time, entry 1
    (and any later reload) afterwards -- which is how a restarted Krita on a
    new port looks from the client.
    """

    def __init__(self, infos):
        super().__init__()
        self._infos = list(infos)
        self.loads = 0

    def _load_info(self, force=False):
        self.loads += 1
        return self._infos[min(self.loads - 1, len(self._infos) - 1)]


class TransportFailureTest(unittest.TestCase):

    def setUp(self):
        self.stub = StubBridge()
        self.addCleanup(self.stub.stop)

    def _info(self, port=None):
        return {"host": "127.0.0.1", "port": port or self.stub.port,
                "token": "stub-token"}

    @staticmethod
    def _closed_port():
        """A port nothing listens on: bind it, note it, release it."""
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        finally:
            sock.close()

    def test_answered_call_posts_once(self):
        client = ScriptedClient([self._info()])
        self.assertEqual(client.call("status", {}), {"stub": True})
        self.assertEqual([post["op"] for post in self.stub.posts], ["status"])
        self.assertEqual(client.loads, 1)

    def test_lost_response_is_unknown_and_never_replayed(self):
        self.stub.mode = "drop"
        client = ScriptedClient([self._info()])
        with self.assertRaises(mcp_server.BridgeError) as caught:
            client.call("create_document", {"width": 10, "height": 10})
        self.assertEqual(caught.exception.kind, "outcome_unknown")
        self.assertEqual(len(self.stub.posts), 1,
                         "a request that may have run must not be replayed")
        self.assertEqual(client.loads, 1)
        self.assertIn("may or may not", str(caught.exception))

    def test_refused_connection_reloads_discovery_and_retries(self):
        client = ScriptedClient([self._info(self._closed_port()),
                                 self._info()])
        self.assertEqual(client.call("status", {}), {"stub": True})
        self.assertEqual(len(self.stub.posts), 1)
        self.assertEqual(client.loads, 2)

    def test_unreachable_bridge_reports_not_running(self):
        port = self._closed_port()
        client = ScriptedClient([self._info(port), self._info(port)])
        with self.assertRaises(mcp_server.BridgeUnavailable) as caught:
            client.call("status", {})
        self.assertIn("was not sent", str(caught.exception))
        self.assertEqual(self.stub.posts, [])
        self.assertEqual(client.loads, 2)

    def test_stale_token_reloads_discovery_once(self):
        self.stub.mode = "unauthorized-once"
        client = ScriptedClient([self._info(), self._info()])
        self.assertEqual(client.call("draw", {"document": "x"}), {"stub": True})
        self.assertEqual(len(self.stub.posts), 2,
                         "a 401 is refused before dispatch, so one replay is safe")
        self.assertEqual(client.loads, 2)

    def test_token_refused_twice_surfaces_the_error(self):
        self.stub.mode = "unauthorized"
        client = ScriptedClient([self._info(), self._info()])
        with self.assertRaises(mcp_server.BridgeError) as caught:
            client.call("status", {})
        self.assertEqual(caught.exception.kind, "unauthorized")
        self.assertEqual(len(self.stub.posts), 2)


if __name__ == "__main__":
    unittest.main()
