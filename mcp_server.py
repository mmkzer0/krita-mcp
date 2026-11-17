#!/usr/bin/env python3
"""MCP stdio server that drives Krita through the in-app HTTP bridge.

Standard library only: nothing to install, nothing to fail at startup.

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, so stdout is
reserved exclusively for protocol messages -- all logging goes to stderr.

`run_python`, which executes arbitrary Python inside Krita, is neither
advertised nor served unless the server is started with `--enable-exec` and the
plugin allows it as well; see "Arbitrary Python is opt-in" in the README.

Run `python mcp_server.py --selftest` to exercise the bridge from a shell.
"""

import argparse
import base64
import http.client
import json
import os
import socket
import sys
import threading
import time

SERVER_NAME = "krita"
SERVER_VERSION = "1.2.0"

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL = "2025-06-18"

HEALTH_TIMEOUT = 3.0
CALL_TIMEOUT = 315.0  # backstop only; the bridge enforces its own per-op limit


def log(message):
    sys.stderr.write("[krita-mcp] {0}\n".format(message))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# bridge client
# ---------------------------------------------------------------------------

class BridgeUnavailable(Exception):
    pass


class BridgeError(Exception):
    """The bridge answered, but the operation failed."""

    def __init__(self, kind, message, detail=None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


class _TransportFailure(Exception):
    """A socket failure, tagged with the phase it happened in.

    The phase decides whether a retry is honest. A connection that was never
    established proves the bridge never saw the request; a connection lost
    after that leaves the operation's outcome unknown.
    """

    def __init__(self, phase, cause):
        super().__init__("{0}: {1}".format(type(cause).__name__, cause))
        self.phase = phase          # "connect", "send" or "response"
        self.cause = cause


def state_dir():
    """Krita's per-user data directory -- must match the plugin's copy."""
    override = os.environ.get("KRITA_MCP_STATE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "krita")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/krita")
    base = (os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~/.local/share"))
    return os.path.join(base, "krita")


def info_file_path():
    override = os.environ.get("KRITA_MCP_INFO_FILE")
    if override:
        return override
    return os.path.join(state_dir(), "krita_mcp_bridge.json")


NOT_RUNNING_HELP = (
    "Could not reach the Krita MCP bridge.\n"
    "  1. Is Krita running?\n"
    "  2. Is the bridge plugin enabled? Settings > Configure Krita > Python "
    "Plugin Manager > 'MCP Bridge', then restart Krita.\n"
    "  3. Check Tools > Scripts > 'MCP Bridge Status...' inside Krita.\n"
    "Connection details are read from: {path}"
)


class BridgeClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._info = None

    # -- discovery --------------------------------------------------------
    def _load_info(self, force=False):
        if self._info is not None and not force:
            return self._info

        host = os.environ.get("KRITA_MCP_HOST", "127.0.0.1")
        port = os.environ.get("KRITA_MCP_PORT")
        token = os.environ.get("KRITA_MCP_TOKEN")
        if port and token:
            self._info = {"host": host, "port": int(port), "token": token}
            return self._info

        path = info_file_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise BridgeUnavailable(NOT_RUNNING_HELP.format(path=path))
        except (OSError, ValueError) as exc:
            raise BridgeUnavailable(
                "The bridge file at {0} could not be read ({1}). Restart Krita "
                "to rewrite it.".format(path, exc))

        if not data.get("port") or not data.get("token"):
            raise BridgeUnavailable(
                "The bridge file at {0} is missing the port or token. Restart "
                "Krita.".format(path))
        self._info = {"host": host, "port": int(data["port"]),
                      "token": data["token"]}
        return self._info

    # -- requests ---------------------------------------------------------
    def _request(self, method, path, body, timeout, info):
        conn = http.client.HTTPConnection(info["host"], info["port"],
                                          timeout=timeout)
        phase = "connect"
        try:
            # Connect explicitly: a failure here is the one transport error
            # that proves the bridge never saw the request.
            conn.connect()
            headers = {"Connection": "close"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(payload))
                headers["X-Krita-MCP-Token"] = info["token"]
            phase = "send"
            conn.request(method, path, body=payload, headers=headers)
            phase = "response"
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except OSError as exc:      # covers refused, reset and timed out
            raise _TransportFailure(phase, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BridgeError(
                "bad_response",
                "The bridge returned HTTP {0} with a body that is not JSON."
                .format(status),
                raw[:500].decode("utf-8", "replace"))
        return status, parsed

    def _listening(self, info, timeout=2.0):
        """Is anything accepting connections where the discovery file points?

        Only used to classify a failure: a refused connection right after one
        proves the bridge is gone; an accepted one means it is still there.
        """
        conn = http.client.HTTPConnection(info["host"], info["port"],
                                          timeout=timeout)
        try:
            conn.connect()
        except OSError:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True

    def _transport_reason(self, op, info, failure):
        """The error a transport failure should surface as.

        Only a connection that was never established is retried (see _post),
        so what arrives here has an honest but possibly incomplete story: a
        refused connection means nothing was sent, while a connection lost
        afterwards means the operation may already have been applied. That is
        never replayed; the caller is told to check instead.
        """
        cause = "{0}: {1}".format(type(failure.cause).__name__, failure.cause)
        if failure.phase == "connect":
            return BridgeUnavailable(
                "{0}\n\nThe operation was not sent: {1}.".format(
                    NOT_RUNNING_HELP.format(path=info_file_path()), cause))
        if not self._listening(info):
            return BridgeUnavailable(
                "The bridge stopped answering while {0!r} was in flight "
                "({1}). It may have crashed, restarted or be wedged, and "
                "whether the operation took effect is unknown -- check the "
                "document before repeating it.\n\n{2}".format(
                    op, cause, NOT_RUNNING_HELP.format(path=info_file_path())))
        return BridgeError(
            "outcome_unknown",
            "{0!r} was sent to Krita but its response was lost ({1}). The "
            "operation may or may not have been applied, so it was not "
            "retried; check the document (status, inspect_document) before "
            "repeating it.".format(op, cause))

    def _post(self, op, body, timeout):
        """POST one /rpc call, following at most one discovery reload.

        A reload is only allowed when the first attempt provably never reached
        the bridge: the connection was refused before anything was written, or
        the token was rejected before the operation ran. Anything that failed
        after the request was transmitted is reported, never replayed.
        """
        with self._lock:
            info = self._load_info()
        try:
            status, parsed = self._request("POST", "/rpc", body, timeout, info)
        except _TransportFailure as failure:
            if failure.phase != "connect":
                raise self._transport_reason(op, info, failure)
            return self._post_after_reload(op, body, timeout)
        if status != 401:
            return status, parsed
        # A stale token is refused before the operation runs, so re-reading
        # discovery and trying once more is safe.
        return self._post_after_reload(op, body, timeout)

    def _post_after_reload(self, op, body, timeout):
        """The one retry both retryable failures share: reload, then resend."""
        with self._lock:
            info = self._load_info(force=True)
        try:
            return self._request("POST", "/rpc", body, timeout, info)
        except _TransportFailure as failure:
            raise self._transport_reason(op, info, failure)

    def call(self, op, params=None, timeout=CALL_TIMEOUT):
        body = {"op": op, "params": params or {}}
        status, parsed = self._post(op, body, timeout)
        if parsed.get("ok"):
            return parsed.get("result")
        error = parsed.get("error") or {}
        raise BridgeError(error.get("type", "error"),
                          error.get("message", "The operation failed."),
                          error.get("detail"))

    def health(self):
        with self._lock:
            info = self._load_info(force=True)
        try:
            status, parsed = self._request("GET", "/health", None,
                                           HEALTH_TIMEOUT, info)
        except _TransportFailure as failure:
            raise BridgeUnavailable(
                "{0}\n\nUnderlying error: {1}: {2}".format(
                    NOT_RUNNING_HELP.format(path=info_file_path()),
                    type(failure.cause).__name__, failure.cause))
        if status != 200 or not parsed.get("ok"):
            raise BridgeUnavailable(
                "The bridge answered HTTP {0}: {1}".format(status, parsed))
        return parsed


BRIDGE = BridgeClient()


# ---------------------------------------------------------------------------
# tool definitions
# ---------------------------------------------------------------------------

DOCUMENT_PROP = {
    "type": ["string", "integer"],
    "description": ("Which open document: its index from `status`, part of its "
                    "name or file name, or omit for the active one."),
}

LAYER_PROP = {
    "type": "string",
    "description": ("Which layer: a name (\"Sky\"), a path through groups "
                    "(\"Background/Sky\"), a top-first index path (\"#0/#1\"), "
                    "\"uuid:<id>\", or omit for the active layer."),
}

LAYER_FLAGS = {
    "alpha_lock": {"type": "boolean",
                   "description": "Paint only where the layer already has "
                                  "pixels."},
    "clip_to_below": {"type": "boolean",
                      "description": "Show this layer only where the layers "
                                     "below it (same group) have pixels -- "
                                     "a clipping layer."},
    "color_label": {"type": "integer", "minimum": 0, "maximum": 8,
                    "description": "Krita's layer colour label; 0 is none."},
}

REGION_PROP = {
    "type": "object",
    "description": "Pixel rectangle on the canvas.",
    "properties": {
        "x": {"type": "integer", "default": 0},
        "y": {"type": "integer", "default": 0},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    },
    "required": ["width", "height"],
}

DRAW_COMMAND = {
    "type": "object",
    "description": (
        "One drawing primitive. `type` decides which other fields apply:\n"
        "- fill_rect: x, y, w, h, color\n"
        "- rect: x, y, w, h, plus optional fill, color (outline), "
        "stroke_width, radius (rounded corners)\n"
        "- ellipse: x, y, w, h (bounding box), fill, color, stroke_width\n"
        "- circle: cx, cy, radius, fill, color, stroke_width\n"
        "- line: x1, y1, x2, y2, color, stroke_width, cap (flat|square|round)\n"
        "- polyline / polygon: points [[x,y],...], color, fill, stroke_width, "
        "close\n"
        "- text: x, y, text, size (pixels), color, font, bold, italic, "
        "anchor (top-left|baseline|center); \\n starts a new line\n"
        "- linear_gradient: x, y, w, h, stops [[0,\"#000\"],[1,\"#fff\"]], "
        "direction (horizontal|vertical|diagonal)\n"
        "- radial_gradient: x, y, w, h, stops\n"
        "- image: x, y, w, h, data (base64 PNG/JPEG) to paste a bitmap\n"
        "- clear: x, y, w, h to erase back to transparency\n"
        "Colours accept #rrggbb, #rrggbbaa, SVG names, or [r,g,b,a]."
    ),
    "properties": {
        "type": {
            "type": "string",
            "enum": ["fill_rect", "rect", "ellipse", "circle", "line",
                     "polyline", "polygon", "text", "linear_gradient",
                     "radial_gradient", "image", "clear"],
        },
        "x": {"type": "number"}, "y": {"type": "number"},
        "w": {"type": "number"}, "h": {"type": "number"},
        "x1": {"type": "number"}, "y1": {"type": "number"},
        "x2": {"type": "number"}, "y2": {"type": "number"},
        "cx": {"type": "number"}, "cy": {"type": "number"},
        "radius": {"type": "number"},
        "color": {"type": ["string", "array"]},
        "fill": {"type": ["string", "array"]},
        "stroke_width": {"type": "number", "default": 1},
        "cap": {"type": "string", "enum": ["flat", "square", "round"]},
        "join": {"type": "string", "enum": ["miter", "bevel", "round"]},
        "close": {"type": "boolean"},
        "points": {"type": "array", "items": {
            "type": "array", "items": {"type": "number"},
            "minItems": 2, "maxItems": 2}},
        "text": {"type": "string"},
        "size": {"type": "integer"},
        "font": {"type": "string"},
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
        "anchor": {"type": "string",
                   "enum": ["top-left", "baseline", "center"]},
        "direction": {"type": "string",
                      "enum": ["horizontal", "vertical", "diagonal"]},
        "stops": {"type": "array", "items": {"type": "array"}},
        "data": {"type": "string", "description": "base64 image bytes"},
    },
    "required": ["type"],
}

_COLOR = {"type": ["string", "array"],
          "description": "#rrggbb, #rrggbbaa, a colour name, or [r,g,b(,a)]."}

VECTOR_SHAPE = {
    "type": "object",
    "description": (
        "One editable vector shape, in canvas pixels. `type` picks the "
        "fields:\n"
        "- text: text, plus EITHER x, y (first baseline; with `align` x is "
        "the left edge/centre/right edge) and optional width (wrap width) "
        "OR box {x,y,width,height} to wrap inside, with valign "
        "(top|middle|bottom, default middle), fit (default true: shrink the "
        "font until the block fits) and min_size. Styling: font (family or "
        "comma list), size (px, default 24), color, stroke + stroke_width "
        "(outline drawn under the fill, for SFX/legibility), bold, italic, "
        "align (left|center|right), line_height (multiplier), "
        "letter_spacing (px), rotation (degrees clockwise), vertical "
        "(top-to-bottom, right-to-left columns). \\n keeps a line break.\n"
        "- bubble: x, y, width, height (the elliptical body), tail [x,y] "
        "(tip, outside the body), tail_width, fill (white), stroke (black), "
        "stroke_width (3): one seamless speech-bubble outline.\n"
        "- ellipse: cx, cy, rx, ry, rotation; rect: x, y, width, height, "
        "radius, rotation; polygon: points [[x,y],...]; path: d (SVG path "
        "data). These take fill (default black), stroke (default none), "
        "stroke_width.\n"
        "Every shape takes an optional unique `name`, which is how "
        "list/remove_vector_shapes refer to it."),
    "properties": {
        "type": {"type": "string",
                 "enum": ["text", "bubble", "ellipse", "rect", "polygon",
                          "path"]},
        "name": {"type": "string"},
        "text": {"type": "string"},
        "x": {"type": "number"}, "y": {"type": "number"},
        "width": {"type": "number"}, "height": {"type": "number"},
        "box": {"type": "object",
                "properties": {"x": {"type": "number"},
                               "y": {"type": "number"},
                               "width": {"type": "number"},
                               "height": {"type": "number"}},
                "required": ["x", "y", "width", "height"],
                "additionalProperties": False},
        "valign": {"type": "string", "enum": ["top", "middle", "bottom"]},
        "fit": {"type": "boolean"},
        "min_size": {"type": "number", "minimum": 1},
        "align": {"type": "string", "enum": ["left", "center", "right"]},
        "font": {"type": "string"},
        "size": {"type": "number", "minimum": 1},
        "color": _COLOR, "fill": _COLOR, "stroke": _COLOR,
        "stroke_width": {"type": "number", "minimum": 0},
        "bold": {"type": "boolean"}, "italic": {"type": "boolean"},
        "vertical": {"type": "boolean"},
        "line_height": {"type": "number"},
        "letter_spacing": {"type": "number"},
        "rotation": {"type": "number"},
        "cx": {"type": "number"}, "cy": {"type": "number"},
        "rx": {"type": "number"}, "ry": {"type": "number"},
        "radius": {"type": "number", "minimum": 0},
        "points": {"type": "array", "maxItems": 5000, "items": {
            "type": "array", "items": {"type": "number"},
            "minItems": 2, "maxItems": 2}},
        "d": {"type": "string"},
        "tail": {"type": "array", "items": {"type": "number"},
                 "minItems": 2, "maxItems": 2},
        "tail_width": {"type": "number"},
    },
    "required": ["type"],
    "additionalProperties": False,
}

VECTOR_UPDATE = {
    "type": "object",
    "description": ("One in-place change to an existing shape: `shape` "
                    "replaces it with a new spec (the same fields as "
                    "add_vector_shapes; name and stacking position are kept; "
                    "shape.name, if given, must equal `name`), or "
                    "`translate` moves it by [dx, dy] canvas pixels. Give "
                    "exactly one."),
    "properties": {
        "name": {"type": "string",
                 "description": "Existing shape name from "
                                "list_vector_shapes."},
        "shape": VECTOR_SHAPE,
        "translate": {"type": "array", "items": {"type": "number"},
                      "minItems": 2, "maxItems": 2,
                      "description": "[dx, dy] in canvas pixels."},
    },
    "required": ["name"],
    "additionalProperties": False,
}

STROKE = {
    "type": "object",
    "description": ("One brush stroke: points [[x,y],...] or, with pen "
                    "pressure, [[x,y,p],...] with p in 0..1 for every point. "
                    "Without pressure the stroke is one smooth curve through "
                    "the points (smooth=false for straight segments)."),
    "properties": {
        "points": {"type": "array", "minItems": 2, "maxItems": 5000,
                   "items": {"type": "array", "items": {"type": "number"},
                             "minItems": 2, "maxItems": 3}},
        "smooth": {"type": "boolean", "default": True},
    },
    "required": ["points"],
    "additionalProperties": False,
}


STYLE_COLOR = {"type": ["string", "array"],
               "description": "#rgb, #rrggbb, an SVG colour name or [r,g,b]. "
                              "No transparency: use `opacity`."}

LAYER_STYLE = {
    "stroke": {"type": "object", "additionalProperties": False,
               "description": "Outline around the layer's pixels or text.",
               "properties": {
                   "size": {"type": "number", "minimum": 1, "maximum": 250,
                            "description": "Width in canvas pixels."},
                   "color": STYLE_COLOR,
                   "position": {"type": "string",
                                "enum": ["outside", "inside", "center"],
                                "default": "outside"},
                   "opacity": {"type": "number", "minimum": 0, "maximum": 1,
                               "default": 1}},
               "required": ["size", "color"]},
    "drop_shadow": {"type": "object", "additionalProperties": False,
                    "description": "Shadow behind the layer; {} gives the "
                                   "defaults.",
                    "properties": {
                        "color": dict(STYLE_COLOR, description=(
                            "Default black. " + STYLE_COLOR["description"])),
                        "opacity": {"type": "number", "minimum": 0,
                                    "maximum": 1, "default": 0.75},
                        "angle": {"type": "number", "minimum": -180,
                                  "maximum": 180, "default": 120,
                                  "description": "Light direction in degrees; "
                                                 "120 casts the shadow down "
                                                 "and right."},
                        "distance": {"type": "number", "minimum": 0,
                                     "maximum": 30000, "default": 5,
                                     "description": "Offset in canvas pixels."},
                        "size": {"type": "number", "minimum": 0,
                                 "maximum": 250, "default": 5,
                                 "description": "Blur in canvas pixels."},
                        "spread": {"type": "number", "minimum": 0,
                                   "maximum": 100, "default": 0,
                                   "description": "Percent."}}},
    "clear": {"type": "boolean", "default": False,
              "description": "Remove the style. Use alone."},
}


def tool(name, description, properties, required=None, op=None, image=False,
         transform=None, exec_required=False):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        "_op": op or name,
        "_image": image,
        "_transform": transform,
        "_exec_required": exec_required,
    }


def _transform_image(args):
    action = args.pop("action")
    mapping = {
        "resize_canvas": "resize_canvas",
        "scale": "scale_image",
        "rotate": "rotate_image",
        "crop": "crop_image",
        "flatten": "flatten_image",
    }
    if action not in mapping:
        raise BridgeError("invalid_request",
                          "action must be one of: " + ", ".join(mapping))
    return mapping[action], args


_CAPABILITY_OPS = {
    "filters": "list_filters",
    "blending_modes": "list_blending_modes",
    "fonts": "list_fonts",
    "brush_presets": "list_brush_presets",
}


def _list_capabilities(args):
    kind = args.pop("kind", "filters")
    if kind not in _CAPABILITY_OPS:
        raise BridgeError("invalid_request",
                          "kind must be one of: " + ", ".join(_CAPABILITY_OPS))
    if kind in ("fonts", "brush_presets"):
        args.pop("include_parameters", None)
    else:
        args.pop("filter", None)
    return _CAPABILITY_OPS[kind], args


TOOLS = [
    tool("status",
         "Krita's version plus every open document (index, size, colour "
         "space, unsaved state). Start here to see what you are working with.",
         {}),

    tool("inspect_document",
         "Full detail for one document: canvas size, resolution, colour "
         "space, the whole layer tree in docker order (top first), and the "
         "current selection.",
         {"document": DOCUMENT_PROP,
          "max_depth": {"type": "integer", "default": -1,
                        "description": "Group nesting to descend; -1 for all."}},
         op="document_info"),

    tool("create_document",
         "Create a new document. It opens in a Krita view unless `view` is "
         "false, which keeps it off the canvas -- the mode for long scripted "
         "work, and the one that survives Krita's OpenGL crashes (see the "
         "README's crash note).",
         {"width": {"type": "integer"},
          "height": {"type": "integer"},
          "name": {"type": "string", "default": "Untitled"},
          "resolution_dpi": {"type": "number", "default": 300},
          "color_model": {"type": "string", "default": "RGBA",
                          "description": "RGBA, GRAYA, CMYKA, LABA, XYZA, YCbCrA"},
          "color_depth": {"type": "string", "default": "U8",
                          "description": "U8, U16, F16 or F32. Drawing needs U8."},
          "color_profile": {"type": "string", "default": "",
                            "description": "Empty for Krita's default."},
          "background": {"type": ["string", "array"],
                         "description": "Optional fill colour, e.g. #ffffff."},
          "view": {"type": "boolean", "default": True,
                   "description": "false keeps the document off the canvas; "
                                  "`views` in status reports the result."}},
         required=["width", "height"]),

    tool("open_document",
         "Open an image file from disk in Krita. `view` works as it does for "
         "create_document.",
         {"path": {"type": "string", "description": "Absolute path."},
          "view": {"type": "boolean", "default": True,
                   "description": "false keeps the document off the canvas."}},
         required=["path"]),

    tool("save_document",
         "Save a document. Give `path` to save-as (the format follows the "
         "extension); omit it to save over the existing file.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string"}}),

    tool("export_document",
         "Export a flattened copy to any format Krita can write (.png, .jpg, "
         ".webp, .tif, ...) without changing what the document is bound to.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string", "description": "Absolute output path."},
          "options": {"type": "object",
                      "description": "Exporter settings, e.g. {\"quality\": 90}."}},
         required=["path"]),

    tool("close_document",
         "Close a document. Refuses to discard unsaved work unless you say "
         "so. The bridge lets Krita finish the document's pending canvas "
         "work before the teardown (Krita 5.3.4 otherwise crashes in its "
         "layer docker on some closes; see the README's crash note).",
         {"document": DOCUMENT_PROP,
          "save": {"type": "boolean", "default": False},
          "discard_changes": {"type": "boolean", "default": False}}),

    tool("transform_image",
         "Whole-image geometry. `action` picks the operation: resize_canvas "
         "(change the canvas, keeping layer pixels where they are), scale "
         "(resample everything), rotate (by degrees, clockwise), crop, or "
         "flatten (merge all layers into one). Everything except flatten "
         "changes the document's size, which Krita aborts on while a canvas "
         "shows the document, so those four are refused there (kind "
         "`unsafe_on_view`) -- create or open the document with view=false to "
         "run them; flatten stays available either way.",
         {"document": DOCUMENT_PROP,
          "action": {"type": "string",
                     "enum": ["resize_canvas", "scale", "rotate", "crop",
                              "flatten"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "degrees": {"type": "number"},
          "x_res": {"type": "number", "minimum": 1,
                    "description": "scale: new horizontal resolution in dpi "
                                   "(default unchanged)."},
          "y_res": {"type": "number", "minimum": 1,
                    "description": "scale: new vertical resolution in dpi "
                                   "(default unchanged)."},
          "strategy": {"type": "string", "default": "Bicubic",
                       "description": "Scaling filter: Bicubic, Bilinear, "
                                      "NearestNeighbor, Lanczos3, Box."}},
         required=["action"], transform=_transform_image),

    tool("create_layer",
         "Add a layer. New layers go to the top of their parent unless you "
         "pass `index` (0 is topmost, negative counts from the bottom). "
         "Layers nest in group layers; masks (transparencymask, ...) take "
         "the layer they apply to as `parent`.",
         {"document": DOCUMENT_PROP,
          "name": {"type": "string", "default": "Layer"},
          "type": {"type": "string", "default": "paintlayer",
                   "enum": ["paintlayer", "grouplayer", "filelayer",
                            "vectorlayer", "transparencymask",
                            "selectionmask"]},
          "parent": {"type": "string",
                     "description": "A group layer to nest inside (omit for "
                                    "the top level); for a mask, the layer "
                                    "it applies to."},
          "index": {"type": "integer"},
          "select": {"type": "boolean", "default": True},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string"},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          **LAYER_FLAGS}),

    tool("set_layer",
         "Change a layer's properties: rename it, set opacity (0-255, or 0-1), "
         "blending mode, visibility, lock, alpha lock, clipping to the layers "
         "below (shading/colour flats), colour label, and optionally make it "
         "active.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "new_name": {"type": "string"},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string",
                            "description": "See list_capabilities."},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          **LAYER_FLAGS,
          "select": {"type": "boolean", "default": False,
                     "description": "Also make this the active layer."}}),

    tool("delete_layer", "Delete a layer and everything inside it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP},
         required=["layer"]),

    tool("duplicate_layer", "Copy a layer, placing the copy just above it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "name": {"type": "string"},
          "select": {"type": "boolean", "default": True}}),

    tool("move_layer",
         "Restack a layer, optionally into a different group. `index` is "
         "top-first within the new parent.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "parent": {"type": "string",
                     "description": "Target group; omit to stay put."},
          "index": {"type": "integer", "default": 0}},
         required=["layer"]),

    tool("merge_layer_down", "Merge a layer into the one directly beneath it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP}),

    tool("get_image",
         "Render the canvas and return it as a PNG you can actually look at. "
         "Use this to check your work. Defaults to the merged image; pass "
         "`layer` for one layer in isolation. For pixel-precise work, `grid` "
         "overlays labelled canvas coordinates and `zoom` magnifies a small "
         "region with crisp pixels; both only change the returned picture.",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string",
                    "description": "Omit or \"merged\" for the composite."},
          "region": REGION_PROP,
          "max_size": {"type": "integer", "default": 1024,
                       "description": "Longest edge of the returned image; "
                                      "the canvas is never upscaled unless "
                                      "`zoom` is set."},
          "grid": {"type": "integer", "minimum": 4,
                   "description": "Grid spacing in canvas pixels; lines are "
                                  "labelled with their canvas coordinate."},
          "zoom": {"type": "integer", "minimum": 1, "maximum": 16,
                   "description": "Nearest-neighbour magnification of the "
                                  "region (overrides max_size); the result "
                                  "may be at most 4096 px on a side."}},
         image=True),

    tool("get_pixel", "Read the exact colour at one pixel.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "x": {"type": "integer"}, "y": {"type": "integer"}},
         required=["x", "y"]),

    tool("draw",
         "Paint shapes, text, gradients and bitmaps onto an RGBA/8-bit paint "
         "layer, as exact pixels. Commands are drawn in order, so later ones "
         "sit on top. Note: this writes pixels directly and does not enter "
         "Krita's undo history, so draw onto a layer you can delete. For "
         "brushwork use paint_strokes; for editable text and speech bubbles "
         "use add_vector_shapes.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "antialias": {"type": "boolean", "default": True},
          "commands": {"type": "array", "items": DRAW_COMMAND,
                       "minItems": 1}},
         required=["commands"]),

    tool("apply_filter",
         "Run one of Krita's filters over a layer, optionally limited to a "
         "region. Call list_capabilities first to see names and parameters.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "filter": {"type": "string", "description": "e.g. blur, gaussianblur, "
                                                      "invert, desaturate."},
          "settings": {"type": "object",
                       "description": "Filter parameters; must match the "
                                      "filter's own names."},
          "region": REGION_PROP},
         required=["filter"]),

    tool("set_selection",
         "Change the active selection, which constrains filters, fill and "
         "Krita's own painting. Shape modes: rect, ellipse (x, y, width, "
         "height), polygon (points). Pixel modes: contiguous (magic wand from "
         "seed x, y with tolerance), color (every pixel near `color`), "
         "layer_alpha (a layer's opaque pixels, e.g. text to outline); they "
         "sample the merged image or `sample` layer, optionally only inside "
         "`region`. `combine` merges the new shape into the existing "
         "selection. Modifiers act on the current selection: invert, grow, "
         "shrink, feather, border (amount px), smooth, fill_holes (take in "
         "everything the selection encloses -- after a magic-wand pick of a "
         "bubble interior this swallows the old lettering); none clears it.",
         {"document": DOCUMENT_PROP,
          "mode": {"type": "string", "default": "rect",
                   "enum": ["rect", "ellipse", "polygon", "contiguous",
                            "color", "layer_alpha", "all", "none", "invert",
                            "grow", "shrink", "feather", "border", "smooth",
                            "fill_holes"]},
          "combine": {"type": "string", "default": "replace",
                      "enum": ["replace", "add", "subtract", "intersect"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "points": {"type": "array", "minItems": 3, "maxItems": 5000, "items": {
              "type": "array", "items": {"type": "number"},
              "minItems": 2, "maxItems": 2}},
          "value": {"type": "integer", "default": 255,
                    "description": "Selection strength 0-255 (rect)."},
          "add": {"type": "boolean", "default": False,
                  "description": "Same as combine=add."},
          "tolerance": {"type": "integer", "minimum": 0, "maximum": 255,
                        "default": 32,
                        "description": "Per-channel difference that still "
                                       "matches (contiguous, color)."},
          "connectivity": {"type": "integer", "enum": [4, 8], "default": 4},
          "color": _COLOR,
          "sample": {"type": "string",
                     "description": "Layer to sample; omit for the merged "
                                    "image."},
          "layer": {"type": "string",
                    "description": "The layer whose alpha layer_alpha "
                                   "reads."},
          "threshold": {"type": "integer", "minimum": 0, "maximum": 255,
                        "default": 0,
                        "description": "layer_alpha: 0 keeps soft edges; "
                                       "otherwise alpha >= threshold is "
                                       "fully selected."},
          "region": REGION_PROP,
          "amount": {"type": "integer", "default": 1,
                     "description": "Pixels, for grow/shrink/feather/"
                                    "border."}}),

    tool("fill",
         "Fill the selection (or a region, or their overlap) on a paint "
         "layer with a colour, or erase it to transparency. Soft selection "
         "edges blend. The cleanup step for scanlation: select the bubble "
         "interior or the old lettering, then fill it white. Writes pixels "
         "directly, outside Krita's undo history.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "color": _COLOR,
          "erase": {"type": "boolean", "default": False},
          "opacity": {"type": "number", "minimum": 0, "maximum": 1,
                      "default": 1},
          "region": REGION_PROP,
          "use_selection": {"type": "boolean", "default": True}}),

    tool("paint_strokes",
         "Paint with Krita's real brush engines (any preset, pressure, "
         "undoable) along point lists -- the tool for drawing and inking. "
         "brush/size/color/opacity/erase are applied for this call only and "
         "the user's settings are restored afterwards; they need some "
         "document to have a view open.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "strokes": {"type": "array", "items": STROKE, "minItems": 1,
                      "maxItems": 200},
          "brush": {"type": "string",
                    "description": "Brush preset name or a unique part of "
                                   "it; see list_capabilities "
                                   "kind=brush_presets."},
          "size": {"type": "number", "minimum": 0.5, "maximum": 1000},
          "color": _COLOR,
          "opacity": {"type": "number", "minimum": 0, "maximum": 1},
          "erase": {"type": "boolean"}},
         required=["strokes"]),

    tool("transform_layer",
         "Move, scale or rotate one layer's content (Krita's own undoable "
         "transforms). Applied in the order scale, rotate (both about the "
         "content's centre), translate.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "translate": {"type": "array", "items": {"type": "number"},
                        "minItems": 2, "maxItems": 2,
                        "description": "[dx, dy] in pixels."},
          "scale": {"type": ["number", "array"],
                    "description": "Factor, or [sx, sy]."},
          "rotate": {"type": "number",
                     "description": "Degrees, clockwise."},
          "strategy": {"type": "string", "default": "Bicubic",
                       "enum": ["Bicubic", "Bilinear", "NearestNeighbor",
                                "Lanczos3", "Box"]}}),

    tool("add_vector_shapes",
         "Add editable vector shapes and text -- typesetting, speech "
         "bubbles, SFX lettering, clean line art -- to a vector layer (a new "
         "one unless `layer` names an existing vector layer). Everything "
         "stays editable with Krita's text and shape tools. Returns each "
         "shape's name and its bounds in canvas pixels; text placed in a "
         "`box` reports the font size it was fitted at. Boxed text on an "
         "existing layer rebuilds it (new uuid) and is refused when the "
         "layer has masks.",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string",
                    "description": "An existing vector layer; omit to "
                                   "create one."},
          "new_layer_name": {"type": "string", "default": "Vector"},
          "shapes": {"type": "array", "items": VECTOR_SHAPE, "minItems": 1,
                     "maxItems": 500}},
         required=["shapes"]),

    tool("list_vector_shapes",
         "List the shapes on a vector layer with their names, types and "
         "bounds in canvas pixels (optionally their SVG).",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string"},
          "include_svg": {"type": "boolean", "default": False}},
         required=["layer"]),

    tool("remove_vector_shapes",
         "Remove shapes from a vector layer by name. All names must exist; "
         "otherwise nothing is removed. The layer is rebuilt from its SVG "
         "(Krita's per-shape removal is unsafe from scripts), which keeps "
         "its name, position, properties and layer style but changes its "
         "uuid. Refused on a locked layer or one with masks (a rebuild would "
         "drop them).",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string"},
          "names": {"type": "array", "items": {"type": "string"},
                    "minItems": 1}},
         required=["layer", "names"]),

    tool("update_vector_shapes",
         "Change existing vector shapes in place -- retype a balloon, refit "
         "text to its box, nudge a bubble -- keeping names and stacking "
         "order. Each update replaces a shape's whole spec or translates it "
         "by [dx, dy] canvas pixels. Everything is checked first, so a "
         "refusal changes nothing. Box-fitted text reports fitted_size. A "
         "replacement rebuilds the layer like remove_vector_shapes "
         "(layer_rebuilt=true, new uuid; refused when the layer has masks); "
         "a translate does not.",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string",
                    "description": "The vector layer holding the shapes."},
          "updates": {"type": "array", "items": VECTOR_UPDATE,
                      "minItems": 1, "maxItems": 200}},
         required=["layer", "updates"]),

    tool("checkpoint",
         "Snapshot a document's layers in memory so `restore` can undo work "
         "Krita's undo does not cover: draw, fill, sub-region filters, and "
         "anything on a view-less document. Saves layer properties (visible, "
         "locked, opacity, blending mode, clip to below, alpha lock) and the "
         "pixels of RGBA/8-bit paint layers; vector layers, groups and masks "
         "keep properties only (listed in `skipped`). Not saved: layer names, "
         "order, selection, canvas size. Memory only, gone when the document "
         "closes; KRITA_MCP_CHECKPOINT_MB (default 1024) caps all checkpoints "
         "together, oldest evicted first (reported in `evicted`), at most 20 "
         "per document.",
         {"document": DOCUMENT_PROP,
          "layers": {"type": "array", "items": {"type": "string"},
                     "minItems": 1,
                     "description": "Layers to save (a group takes its "
                                    "children). Omit for every layer."},
          "label": {"type": "string",
                    "description": "Up to 80 characters."}}),

    tool("restore",
         "Put a checkpoint's pixels and layer properties back. The checkpoint "
         "stays, so restoring again works. Writes outside Krita's undo "
         "history. Layers deleted since come back as `missing` (not "
         "recreated); layers added since are left alone (`added_since`). "
         "Rebuilding a vector layer (update_vector_shapes, "
         "remove_vector_shapes, boxed text added to an existing layer) gives "
         "it a new uuid, so it then shows up as `missing` plus `added_since`.",
         {"document": DOCUMENT_PROP,
          "checkpoint": {"type": "string",
                         "description": "An id from checkpoint, e.g. cp3."}},
         required=["checkpoint"]),

    tool("list_checkpoints",
         "List a document's checkpoints and the shared memory budget.",
         {"document": DOCUMENT_PROP}),

    tool("drop_checkpoints",
         "Free checkpoint memory: the given ids, or all of the document's.",
         {"document": DOCUMENT_PROP,
          "checkpoints": {"type": "array", "items": {"type": "string"},
                          "minItems": 1}}),

    tool("set_layer_style",
         "Give a layer an outline (stroke) and/or a drop shadow as a real "
         "Krita layer style, editable later in Layer > Layer Style; for SFX "
         "and lettering on paint or vector layers. The call sets the whole "
         "style: an effect you leave out is off, and other effects the layer "
         "had (bevel, glow, ...) are removed and listed in "
         "`discarded_effects`. Krita stores whole numbers, so values are "
         "rounded. Not covered by checkpoint/restore. Returns the style as "
         "read back from the layer.",
         dict({"document": DOCUMENT_PROP, "layer": LAYER_PROP},
              **LAYER_STYLE),
         required=["layer"]),

    tool("get_layer_style",
         "Read a layer's style: {has_style, stroke?, drop_shadow?, "
         "other_effects} in the units set_layer_style takes; other_effects "
         "names enabled effects these tools do not model.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP},
         required=["layer"]),

    tool("export_region",
         "Write a canvas region to a PNG file that an outside tool (an "
         "inpainting model, an image editor) can patch, then paste back with "
         "import_region -- no base64 image data in the conversation. Omit "
         "`layer` for the merged image (the page as it renders, not the "
         "active layer); pass one for that layer's own pixels (RGBA/U8 "
         "only). `pad` adds context on every side, clamped to the canvas. The "
         "returned region is the rect actually exported, so import it back at "
         "region.x/region.y. Refuses a relative path, a non-.png extension, "
         "an existing file without overwrite=true, a missing directory (it "
         "does not create one), a region off the canvas, and more than 64 "
         "megapixels. Reads only.",
         {"document": DOCUMENT_PROP,
          "region": REGION_PROP,
          "layer": LAYER_PROP,
          "path": {"type": "string",
                   "description": "Absolute path of the .png to write."},
          "pad": {"type": "integer", "minimum": 0, "maximum": 1024,
                  "default": 0,
                  "description": "Extra context in pixels on every side, "
                                 "clamped to the canvas."},
          "overwrite": {"type": "boolean", "default": False,
                        "description": "Replace the file if it already "
                                       "exists."}},
         required=["region", "path"]),

    tool("import_region",
         "Paste a PNG/JPEG/WebP file into a paint layer at a canvas position "
         "-- the return half of export_region. `x`/`y` are the canvas pixels "
         "of the image's top-left corner, typically the exported region's "
         "x/y. mode over (default) alpha-composites onto the layer; replace "
         "writes the image's pixels as they are, transparency included, and "
         "is refused on an alpha-locked layer (over keeps the layer's alpha, "
         "like fill). selection_only limits the change to the current "
         "selection, weighted by its coverage, and needs a selection. Pixels "
         "outside the canvas are clipped and reported, not refused; images "
         "over 64 megapixels are refused before decoding. Writes pixels "
         "directly, which Krita's undo does not record: take a checkpoint "
         "first.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "path": {"type": "string",
                   "description": "Absolute path of the .png/.jpg/.jpeg/"
                                  ".webp to paste."},
          "x": {"type": "integer",
                "description": "Canvas x of the image's top-left corner."},
          "y": {"type": "integer",
                "description": "Canvas y of the image's top-left corner."},
          "mode": {"type": "string", "enum": ["over", "replace"],
                   "default": "over",
                   "description": "over alpha-composites; replace overwrites "
                                  "pixels, transparency included."},
          "selection_only": {"type": "boolean", "default": False,
                             "description": "Change only pixels inside the "
                                            "current selection, weighted by "
                                            "its coverage."},
          "expect_size": {"type": "array", "items": {"type": "integer"},
                          "minItems": 2, "maxItems": 2,
                          "description": "Required [width, height] of the "
                                         "image; refuse a size mismatch."}},
         required=["layer", "path", "x", "y"]),

    tool("list_capabilities",
         "List what this Krita build supports: filter names (with their "
         "parameters), blending modes, installed fonts or brush presets.",
         {"kind": {"type": "string", "default": "filters",
                   "enum": ["filters", "blending_modes", "fonts",
                            "brush_presets"]},
          "include_parameters": {"type": "boolean", "default": False},
          "filter": {"type": "string",
                     "description": "fonts/brush_presets: case-insensitive "
                                    "substring to match."}},
         transform=_list_capabilities),

    tool("trigger_action",
         "Fire a Krita menu action by its internal id -- the escape hatch for "
         "anything with no dedicated tool. Actions act on the ACTIVE "
         "document and view, not on a `document` you name. Useful ids: "
         "edit_undo, edit_redo, select_all, invert_selection.",
         {"name": {"type": "string"}},
         required=["name"]),

    tool("run_python",
         "Execute Python inside Krita with the full libkis API, for anything "
         "the other tools do not cover. `Krita`, `krita` (the instance) and "
         "`doc` (the active document) are predefined; assign to `result` to "
         "return a value. Runs on the UI thread, so keep it quick. Disabled "
         "unless the server was started with --enable-exec and the plugin "
         "allows it (see the README).",
         {"code": {"type": "string"}},
         required=["code"], exec_required=True),

    tool("self_test",
         "Verify the bridge end to end: document creation, pixel round-trip, "
         "channel order, drawing, PNG encoding and layer handling. Run this "
         "first if something looks wrong.",
         {}),
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools(allow_exec=False):
    return [{k: v for k, v in t.items() if not k.startswith("_")}
            for t in TOOLS
            if allow_exec or not t["_exec_required"]]


# ---------------------------------------------------------------------------
# tool execution
# ---------------------------------------------------------------------------

def _summarise(result):
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


EXEC_HINT = ("start the MCP server with --enable-exec, and allow it in Krita "
             "with allow_python=true under [krita_mcp] in kritarc (or "
             "KRITA_MCP_ALLOW_PYTHON=1 before starting Krita)")


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------

MAX_SCHEMA_ERRORS = 10


def _json_type(value):
    """The JSON type name of a decoded value (bool is not a number)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(type_name, value):
    if type_name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float)
                                          and value.is_integer())
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, (list, tuple))
    raise ValueError("unsupported schema type {0!r}".format(type_name))


def _show(value):
    """How a value appears in an error: scalars inline, containers by type."""
    kind = _json_type(value)
    if kind in ("array", "object"):
        return kind
    text = json.dumps(value, ensure_ascii=False, default=repr)
    if len(text) > 40:
        text = text[:37] + "..."
    return "{0} {1}".format(kind, text)


def _same_json(a, b):
    """Equality where True is not 1 (Python would say it is)."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _check(schema, value, path, errors):
    if len(errors) > MAX_SCHEMA_ERRORS:
        return

    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_matches_type(n, value) for n in names):
            errors.append("{0}: expected {1}, got {2}".format(
                path, " or ".join(names), _show(value)))
            return

    if "enum" in schema and not any(_same_json(value, c)
                                    for c in schema["enum"]):
        errors.append("{0}: {1} is not one of: {2}".format(
            path, _show(value),
            ", ".join(json.dumps(c, ensure_ascii=False)
                      for c in schema["enum"])))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                errors.append("{0}: missing required property {1!r}".format(
                    path, key))
        for key, item in value.items():
            if key in properties:
                _check(properties[key], item, "{0}.{1}".format(path, key),
                       errors)
            elif schema.get("additionalProperties") is False:
                errors.append(
                    "{0}.{1}: unexpected property; allowed properties: {2}"
                    .format(path, key, ", ".join(properties) or "(none)"))
            if len(errors) > MAX_SCHEMA_ERRORS:
                return

    elif isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("{0}: expected at least {1} item(s), got {2}".format(
                path, schema["minItems"], len(value)))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("{0}: expected at most {1} item(s), got {2}".format(
                path, schema["maxItems"], len(value)))
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _check(item_schema, item, "{0}[{1}]".format(path, index),
                       errors)
                if len(errors) > MAX_SCHEMA_ERRORS:
                    return

    elif (isinstance(value, (int, float)) and not isinstance(value, bool)):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("{0}: {1} is below the minimum of {2}".format(
                path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("{0}: {1} is above the maximum of {2}".format(
                path, value, schema["maximum"]))


def validate_arguments(schema, value, path="arguments"):
    """Check `value` against the JSON-Schema subset our tool schemas use.

    Returns human-readable error strings, empty when the value conforms. At
    most MAX_SCHEMA_ERRORS entries come back, the last one saying that more
    were left out, so an enormous bad payload cannot produce an enormous
    message (the walk stops as soon as the cap is exceeded).

    Supported: type (one name or a list), enum, properties, required,
    additionalProperties: false, items, minItems, maxItems, minimum, maximum.
    Other keywords (default, description) are ignored. An integer may be a
    float with no fractional part; a bool is neither integer nor number.
    """
    errors = []
    _check(schema, value, path, errors)
    if len(errors) > MAX_SCHEMA_ERRORS:
        errors = errors[:MAX_SCHEMA_ERRORS - 1]
        errors.append("... further errors not shown")
    return errors


def drop_optional_nulls(schema, value):
    """Copy of `value` without null-valued optional properties.

    Clients often send `null` for a parameter they mean to omit, and the
    plugin already reads null as absent. A null for a `required` property is
    kept so validation still reports it, and so is a null under a name the
    schema does not declare, so a misspelt key is not hidden. Objects are
    cleaned wherever a schema with `properties` describes them, including
    array items (draw commands); schemaless objects such as filter settings
    are passed through untouched.
    """
    if isinstance(value, dict) and "properties" in schema:
        properties = schema["properties"]
        required = schema.get("required", ())
        cleaned = {}
        for key, item in value.items():
            if key in properties:
                if item is None and key not in required:
                    continue
                item = drop_optional_nulls(properties[key], item)
            cleaned[key] = item
        return cleaned
    if isinstance(value, (list, tuple)) and isinstance(schema.get("items"),
                                                       dict):
        return [drop_optional_nulls(schema["items"], item) for item in value]
    return value


def _version_skew(tool_name, op, original):
    """The plugin lacks an operation this server advertises: say why."""
    try:
        plugin = BRIDGE.health().get("plugin_version") or "unknown"
    except Exception:  # noqa: BLE001 - never mask the original error
        return original
    return BridgeError(
        "plugin_outdated",
        "{0} needs the Krita plugin operation {1!r}, which the running plugin "
        "(version {2}) does not have; this MCP server is version {3}. Install "
        "the plugin from the same checkout as this server (install.sh or "
        "install.ps1) and restart Krita.".format(tool_name, op, plugin,
                                                SERVER_VERSION))


def call_tool(name, arguments, allow_exec=False):
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise BridgeError("unknown_tool",
                          "No tool named {0!r}. Available: {1}".format(
                              name, ", ".join(sorted(TOOLS_BY_NAME))))
    if spec["_exec_required"] and not allow_exec:
        raise BridgeError(
            "exec_disabled",
            "{0} is not enabled on this MCP server: it runs arbitrary Python "
            "inside Krita. To use it, {1}.".format(name, EXEC_HINT))

    arguments = drop_optional_nulls(
        spec["inputSchema"], {} if arguments is None else arguments)
    problems = validate_arguments(spec["inputSchema"], arguments)
    if problems:
        raise BridgeError(
            "invalid_arguments",
            "{0} was not called: the arguments do not match its input "
            "schema.\n{1}".format(name, "\n".join(
                "- " + line for line in problems)))

    args = dict(arguments)
    op = spec["_op"]
    if spec["_transform"] is not None:
        op, args = spec["_transform"](args)

    try:
        result = BRIDGE.call(op, args)
    except BridgeError as exc:
        if exc.kind == "unknown_op":
            raise _version_skew(name, op, exc)
        raise

    if spec["_image"] and isinstance(result, dict) and result.get("png_base64"):
        png = result.pop("png_base64")
        return [
            {"type": "text", "text": _summarise(result)},
            {"type": "image", "data": png, "mimeType": "image/png"},
        ]
    return [{"type": "text", "text": _summarise(result)}]


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------

def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class Server:
    def __init__(self, allow_exec=False):
        self.initialized = False
        self.protocol = PREFERRED_PROTOCOL
        self.allow_exec = allow_exec

    def handle(self, message):
        """Return a response dict, or None for notifications."""
        if not isinstance(message, dict):
            return _error(None, -32600, "Request must be a JSON object.")

        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method is None:
            # A response to something we never sent; ignore it.
            return None

        try:
            if method == "initialize":
                payload = self._initialize(message.get("params") or {})
            elif method == "notifications/initialized":
                self.initialized = True
                return None
            elif method in ("notifications/cancelled", "notifications/progress",
                            "notifications/roots/list_changed"):
                return None
            elif method == "ping":
                payload = {}
            elif method == "tools/list":
                payload = {"tools": public_tools(self.allow_exec)}
            elif method == "tools/call":
                payload = self._call(message.get("params") or {})
            elif method in ("resources/list", "resources/templates/list"):
                payload = {"resources": [], "resourceTemplates": []}
            elif method == "prompts/list":
                payload = {"prompts": []}
            elif method == "logging/setLevel":
                payload = {}
            else:
                if is_notification:
                    return None
                return _error(request_id, -32601,
                              "Method not found: {0}".format(method))
        except BridgeError as exc:
            if is_notification:
                return None
            return _error(request_id, -32603, str(exc))
        except Exception as exc:
            log("internal error handling {0}: {1!r}".format(method, exc))
            if is_notification:
                return None
            return _error(request_id, -32603,
                          "{0}: {1}".format(type(exc).__name__, exc))

        if is_notification:
            return None
        return _result(request_id, payload)

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol = (requested if requested in SUPPORTED_PROTOCOLS
                         else PREFERRED_PROTOCOL)
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Drives a running Krita instance. Call `status` to see open "
                "documents, `inspect_document` for the layer tree, `draw` to "
                "paint onto a paint layer, and `get_image` to look at the "
                "result. If nothing responds, run `self_test`."
            ),
        }

    def _call(self, params):
        name = params.get("name")
        if not isinstance(name, str):
            raise BridgeError("invalid_request", "tools/call needs a `name`.")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_request", "`arguments` must be an object.")

        started = time.time()
        try:
            content = call_tool(name, arguments, self.allow_exec)
        except BridgeUnavailable as exc:
            return {"content": [{"type": "text", "text": str(exc)}],
                    "isError": True}
        except BridgeError as exc:
            text = "{0}: {1}".format(exc.kind, exc)
            if exc.detail:
                text += "\n\n" + exc.detail
            return {"content": [{"type": "text", "text": text}],
                    "isError": True}
        except Exception as exc:
            log("tool {0} blew up: {1!r}".format(name, exc))
            return {"content": [{"type": "text", "text": "{0}: {1}".format(
                type(exc).__name__, exc)}], "isError": True}

        log("{0} ok in {1:.0f}ms".format(name, (time.time() - started) * 1000))
        return {"content": content, "isError": False}


def serve(allow_exec=False):
    stdin = sys.stdin
    stdout = sys.stdout
    try:  # never let the console codepage mangle protocol bytes
        stdin.reconfigure(encoding="utf-8", errors="replace")
        stdout.reconfigure(encoding="utf-8", newline="\n")
    except AttributeError:
        pass

    server = Server(allow_exec=allow_exec)
    log("ready (pid {0}, run_python {1})".format(
        os.getpid(), "enabled" if allow_exec else "disabled"))

    while True:
        try:
            line = stdin.readline()
        except (KeyboardInterrupt, ValueError):
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except ValueError as exc:
            response = _error(None, -32700, "Parse error: {0}".format(exc))
        else:
            if isinstance(message, list):  # JSON-RPC batch
                responses = [r for r in (server.handle(m) for m in message)
                             if r is not None]
                response = responses or None
            else:
                response = server.handle(message)

        if response is None:
            continue
        try:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError):
            break

    log("stdin closed, exiting")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_selftest():
    print("bridge file: {0}".format(info_file_path()))
    try:
        health = BRIDGE.health()
    except BridgeUnavailable as exc:
        print("UNAVAILABLE\n{0}".format(exc))
        return 1
    print("health: Krita {0}, plugin {1}, {2} operations".format(
        health.get("krita_version"), health.get("plugin_version"),
        len(health.get("operations", []))))

    result = BRIDGE.call("self_test", {})
    for check in result.get("checks", []):
        print("  [{0}] {1:<22} {2}".format(
            "ok" if check["ok"] else "FAIL", check["check"], check["detail"]))
    print("{0}/{1} checks passed".format(result.get("passed"),
                                         result.get("total")))
    return 0 if result.get("all_ok") else 2


def cli_call(op, params_json, params_file=None):
    if params_file:
        try:
            # utf-8-sig: PowerShell's Set-Content writes a BOM.
            with open(params_file, "r", encoding="utf-8-sig") as handle:
                params_json = handle.read()
        except OSError as exc:
            print("could not read {0}: {1}".format(params_file, exc))
            return 1
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except ValueError as exc:
        print("params is not valid JSON: {0}".format(exc))
        return 1
    try:
        result = BRIDGE.call(op, params)
    except (BridgeError, BridgeUnavailable) as exc:
        print("ERROR: {0}".format(exc))
        return 1
    if isinstance(result, dict) and "png_base64" in result:
        data = result.pop("png_base64")
        out = os.path.abspath("krita_mcp_output.png")
        with open(out, "wb") as handle:
            handle.write(base64.b64decode(data))
        result["png_written_to"] = out
    print(json.dumps(result, indent=2, default=str))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true",
                        help="check the bridge and run its self test")
    parser.add_argument("--call", metavar="OP",
                        help="invoke one bridge operation and print the result")
    parser.add_argument("--params", metavar="JSON", default="",
                        help="JSON parameters for --call")
    parser.add_argument("--params-file", metavar="PATH",
                        help="read --call parameters from a JSON file "
                             "(avoids shell quoting)")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the MCP tool names and exit")
    parser.add_argument("--enable-exec", action="store_true",
                        help="advertise and allow run_python, which executes "
                             "arbitrary Python inside Krita; the plugin must "
                             "also allow it (see the README)")
    args = parser.parse_args()

    if args.list_tools:
        for spec in TOOLS:
            marker = "   [needs --enable-exec]" if spec["_exec_required"] else ""
            print("{0:<20} -> op {1}{2}".format(spec["name"], spec["_op"],
                                                marker))
        return 0
    if args.selftest:
        return cli_selftest()
    if args.call:
        return cli_call(args.call, args.params, args.params_file)
    serve(allow_exec=args.enable_exec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
