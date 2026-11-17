"""Explicit checkpoints: undo for work that bypasses Krita's undo history.

``draw``, ``fill``, region imports and sub-region filters write pixels
directly, so Krita's own undo cannot take them back, and its ``edit_undo``
action only reaches the active view's document -- a view-less document has no
undo at all. ``checkpoint`` snapshots a document's layers in memory and
``restore`` puts them back.

The module has two halves. ``CheckpointStore`` and the rectangle helpers are
plain Python (no Krita, no Qt, no relative imports) so test_history.py can load
this file by path. Everything below "Krita side" talks to libkis and reaches
the rest of the plugin lazily through ``_o()``, like paint_ops and vector_ops.

What a checkpoint keeps, per layer and keyed by the layer's uuid:

* properties: visible, locked, and on layers also opacity, blending mode and
  clip-to-below (``inherit_alpha``); paint layers add alpha lock
* pixels of RGBA/U8 paint layers, over ``node.bounds()``

Not kept: layer names, order and parenting, selections, canvas size, vector
shapes (see F31 below), mask contents. A layer deleted after the checkpoint is
reported as ``missing``; it is not brought back.

Vector layers are saved as properties only. Restoring shapes means removing
the current ones, and libkis ``Shape.remove()`` acts on
``Krita.activeDocument()`` instead of the shape's own document: with no active
document (only view-less documents open) it dereferences null and kills Krita
(F31, crash report krita-2026-11-17-194121.ips).
"""

import os
import time

CHECKPOINT_MB_ENV = "KRITA_MCP_CHECKPOINT_MB"
DEFAULT_LIMIT_MB = 1024
MAX_PER_DOCUMENT = 20
MAX_LABEL_CHARS = 80

VECTOR_SKIP_REASON = ("vector shapes cannot be restored safely: libkis "
                      "Shape.remove() acts on the active document (F31)")


# --------------------------------------------------------------------------
# Krita-free half
# --------------------------------------------------------------------------

class StoreError(Exception):
    """A refusal the caller can act on; ``kind`` matches OpError kinds."""

    def __init__(self, message, kind="invalid_request"):
        super(StoreError, self).__init__(message)
        self.kind = kind


def parse_limit_mb(text):
    """Bytes for a ``KRITA_MCP_CHECKPOINT_MB`` value; None/"" -> the default."""
    if text is None or str(text).strip() == "":
        return DEFAULT_LIMIT_MB * 1024 * 1024
    try:
        megabytes = float(text)
    except (TypeError, ValueError):
        megabytes = None
    if megabytes is None or megabytes != megabytes or megabytes <= 0:
        raise StoreError(
            "{0} must be a positive number of megabytes, got {1!r}. Unset it "
            "for the default of {2} MB.".format(
                CHECKPOINT_MB_ENV, text, DEFAULT_LIMIT_MB))
    return int(megabytes * 1024 * 1024)


def rect_is_empty(rect):
    return rect is None or rect[2] <= 0 or rect[3] <= 0


def rect_union(a, b):
    """Bounding rectangle of two (x, y, w, h) tuples; empties are ignored."""
    if rect_is_empty(a):
        return None if rect_is_empty(b) else tuple(b)
    if rect_is_empty(b):
        return tuple(a)
    left = min(a[0], b[0])
    top = min(a[1], b[1])
    right = max(a[0] + a[2], b[0] + b[2])
    bottom = max(a[1] + a[3], b[1] + b[3])
    return (left, top, right - left, bottom - top)


def rect_contains(outer, inner):
    """Does ``outer`` cover all of ``inner``? An empty ``inner`` is covered."""
    if rect_is_empty(inner):
        return True
    if rect_is_empty(outer):
        return False
    return (outer[0] <= inner[0] and outer[1] <= inner[1] and
            outer[0] + outer[2] >= inner[0] + inner[2] and
            outer[1] + outer[3] >= inner[1] + inner[3])


class Checkpoint(object):
    __slots__ = ("id", "number", "doc_key", "label", "created", "size",
                 "layers", "data")

    def __init__(self, number, doc_key, label, created, size, layers, data):
        self.id = "cp{0}".format(number)
        self.number = number
        self.doc_key = doc_key
        self.label = label
        self.created = created
        self.size = size
        self.layers = layers
        self.data = data


class CheckpointStore(object):
    """Ids, byte budget, eviction and the per-document cap.

    Ids are ``cp1``, ``cp2``, ... and never reused, so a larger number is
    always a newer checkpoint and "oldest" is the first one in ``_items``.
    ``limit_bytes`` is a plain attribute: the plugin re-reads the environment
    on every operation and assigns it.
    """

    def __init__(self, limit_bytes, per_document=MAX_PER_DOCUMENT,
                 clock=time.time):
        self.limit_bytes = limit_bytes
        self.per_document = per_document
        self._clock = clock
        self._next = 1
        self._items = {}
        self._used = 0

    @property
    def used_bytes(self):
        return self._used

    def __len__(self):
        return len(self._items)

    def check_fits(self, size):
        """Refuse a checkpoint larger than the whole budget (stores nothing)."""
        if size > self.limit_bytes:
            raise StoreError(
                "This checkpoint needs {0} bytes but the whole checkpoint "
                "budget is {1} bytes. Checkpoint fewer layers (pass `layers`) "
                "or raise {2}.".format(size, self.limit_bytes,
                                       CHECKPOINT_MB_ENV),
                kind="too_large")

    def add(self, doc_key, label, size, layers, data):
        """Store a checkpoint; returns (checkpoint, [evicted ids])."""
        self.check_fits(size)
        evicted = []
        while sum(1 for c in self._items.values()
                  if c.doc_key == doc_key) >= self.per_document:
            evicted.append(self._evict(next(
                c for c in self._items.values() if c.doc_key == doc_key)))
        while self._used + size > self.limit_bytes:
            evicted.append(self._evict(next(iter(self._items.values()))))
        checkpoint = Checkpoint(self._next, doc_key, label,
                                int(self._clock()), size, layers, data)
        self._next += 1
        self._items[checkpoint.id] = checkpoint
        self._used += size
        return checkpoint, evicted

    def _evict(self, checkpoint):
        del self._items[checkpoint.id]
        self._used -= checkpoint.size
        return checkpoint.id

    def get(self, checkpoint_id):
        return self._items.get(checkpoint_id)

    def list(self, doc_key):
        return [c for c in self._items.values() if c.doc_key == doc_key]

    def drop(self, doc_key, ids=None):
        """Drop ``ids`` (all of the document's when None); returns the ids.

        Every id must exist and belong to ``doc_key``, otherwise nothing is
        dropped.
        """
        if ids is None:
            chosen = [c.id for c in self.list(doc_key)]
        else:
            chosen = list(dict.fromkeys(ids))
            for checkpoint_id in chosen:
                found = self._items.get(checkpoint_id)
                if found is None or found.doc_key != doc_key:
                    raise StoreError(
                        "No checkpoint {0!r} on this document; nothing was "
                        "dropped. list_checkpoints shows the ids.".format(
                            checkpoint_id), kind="not_found")
        for checkpoint_id in chosen:
            self._evict(self._items[checkpoint_id])
        return chosen

    def forget_closed(self, open_keys):
        """Drop every checkpoint whose document is not in ``open_keys``."""
        keep = set(open_keys)
        gone = [c for c in self._items.values() if c.doc_key not in keep]
        return [self._evict(c) for c in gone]


# --------------------------------------------------------------------------
# Krita side
# --------------------------------------------------------------------------

_STORE = CheckpointStore(DEFAULT_LIMIT_MB * 1024 * 1024)

# (key, getter, setter) for properties every layer has / only paint layers have
_LAYER_PROPS = (("visible", "visible", "setVisible", bool),
                ("locked", "locked", "setLocked", bool),
                ("opacity", "opacity", "setOpacity", int),
                ("blending_mode", "blendingMode", "setBlendingMode", str),
                ("inherit_alpha", "inheritAlpha", "setInheritAlpha", bool))
_MASK_PROPS = _LAYER_PROPS[:2]
_PAINT_PROPS = (("alpha_lock", "alphaLocked", "setAlphaLocked", bool),)


def _o():
    from . import ops
    return ops


def _err(message, kind="invalid_request"):
    return _o().OpError(message, kind=kind)


def _prop_table(ntype):
    if ntype.endswith("mask"):
        return _MASK_PROPS
    if ntype == "paintlayer":
        return _LAYER_PROPS + _PAINT_PROPS
    return _LAYER_PROPS


def _read_props(node, ntype):
    return {key: cast(getattr(node, getter)())
            for key, getter, _setter, cast in _prop_table(ntype)}


def _apply_props(node, ntype, props):
    for key, getter, setter, cast in _prop_table(ntype):
        if key in props and cast(getattr(node, getter)()) != props[key]:
            getattr(node, setter)(props[key])


def _apply_limit():
    try:
        _STORE.limit_bytes = parse_limit_mb(os.environ.get(CHECKPOINT_MB_ENV))
    except StoreError as exc:
        raise _err(str(exc), exc.kind)


def _doc_key(doc):
    """A document's identity that outlives its Python wrapper: root uuid."""
    key = _o()._node_uuid(doc.rootNode())
    if not key:
        raise _err("Krita gave the document's root layer no uuid, so it "
                   "cannot be checkpointed.", kind="unsupported")
    return key


def _open_keys():
    return [_doc_key(d) for d in _o()._documents()]


def forget_closed_documents():
    """Drop the checkpoints of documents that are no longer open.

    Called by close_document once the close has returned; also catches
    documents closed by hand. Skips Krita entirely when nothing is stored.
    """
    if len(_STORE):
        _STORE.forget_closed(_open_keys())


def _walk(node, prefix):
    """(path, node) for every descendant, docker order, parents first."""
    o = _o()
    for child in o._children(node):
        path = "{0}/{1}".format(prefix, child.name()) if prefix else child.name()
        yield path, child
        for item in _walk(child, path):
            yield item


def _select_nodes(doc, refs):
    o = _o()
    if refs is None:
        return list(_walk(doc.rootNode(), ""))
    if not isinstance(refs, list) or not refs or \
            not all(isinstance(r, str) and r.strip() for r in refs):
        raise _err("`layers` must be a non-empty array of layer references; "
                   "omit it to checkpoint every layer.")
    chosen, seen = [], set()
    for ref in refs:
        node = o.resolve_node(doc, ref)
        paths = [(node.name(), node)] + [
            (node.name() + "/" + p, n) for p, n in _walk(node, "")]
        for path, member in paths:
            uuid = o._node_uuid(member)
            if uuid is None or uuid not in seen:
                seen.add(uuid)
                chosen.append((path, member))
    return chosen


def _plan_layer(path, node):
    """What to save for one node. Reads properties and bounds, no pixels."""
    o = _o()
    ntype = node.type()
    plan = {"uuid": o._node_uuid(node), "name": node.name(), "type": ntype,
            "path": path, "props": _read_props(node, ntype),
            "saved": "properties", "reason": None, "rect": None,
            "pixels": None, "size": 0}
    if ntype == "paintlayer":
        from . import imaging
        if imaging.supports_pixel_ops(node):
            bounds = node.bounds()
            rect = (bounds.x(), bounds.y(), bounds.width(), bounds.height())
            plan["saved"] = "pixels"
            if not rect_is_empty(rect):
                plan["rect"] = rect
                plan["size"] = rect[2] * rect[3] * 4
        else:
            plan["reason"] = (
                "paint layer is {0}/{1}; pixels are saved for RGBA/U8 only "
                "(properties were saved)".format(node.colorModel(),
                                                 node.colorDepth()))
    elif ntype == "vectorlayer":
        plan["reason"] = VECTOR_SKIP_REASON
    elif ntype == "grouplayer":
        pass
    elif ntype.endswith("mask"):
        plan["reason"] = ("{0}: its content is not saved (properties "
                          "only)".format(ntype))
    else:
        plan["reason"] = "{0}: only its properties are saved".format(ntype)
    return plan


def _read_pixels(node, plan):
    x, y, w, h = plan["rect"]
    raw = bytes(node.pixelData(x, y, w, h))
    if len(raw) != w * h * 4:
        raise _err("Krita returned {0} bytes for the {1}x{2} pixels of {3!r}, "
                   "expected {4}.".format(len(raw), w, h, plan["name"],
                                          w * h * 4), kind="internal")
    plan["pixels"] = raw


def op_checkpoint(params):
    """Snapshot a document's layers in memory so ``restore`` can undo later work.

    Saves layer properties (visible, locked, opacity, blending mode, clip to
    below, alpha lock) and the pixels of RGBA/U8 paint layers. Vector layers,
    groups, masks and other kinds keep properties only; the reason is in
    ``skipped``. ``layers`` limits the snapshot (a group takes its children);
    default is every layer. Checkpoints live in memory only, share the
    ``KRITA_MCP_CHECKPOINT_MB`` budget (default 1024) across documents -- older
    checkpoints are evicted to make room and listed in ``evicted`` -- and
    keep at most 20 per document. One bigger than the whole budget is refused
    (kind ``too_large``) before anything is stored or read.
    """
    o = _o()
    doc = o.resolve_document(params.get("document"))
    label = o._arg(params, "label", "")
    if not isinstance(label, str):
        raise _err("label must be a string")
    if len(label) > MAX_LABEL_CHARS:
        raise _err("label is {0} characters; the limit is {1}.".format(
            len(label), MAX_LABEL_CHARS))
    _apply_limit()

    members = _select_nodes(doc, params.get("layers"))
    plans, skipped = [], []
    for path, node in members:
        plan = _plan_layer(path, node)
        if plan["uuid"] is None:
            skipped.append({"layer": path, "reason":
                            "Krita gave this layer no uuid, so it cannot be "
                            "matched on restore; not saved"})
            continue
        plans.append((node, plan))
        if plan["reason"]:
            skipped.append({"layer": path, "reason": plan["reason"]})

    total = sum(plan["size"] for _node, plan in plans)
    try:
        _STORE.check_fits(total)
    except StoreError as exc:
        raise _err(str(exc), exc.kind)
    for node, plan in plans:
        if plan["rect"] is not None:
            _read_pixels(node, plan)

    known = frozenset(o._node_uuid(n) for _p, n in _walk(doc.rootNode(), "")
                      if o._node_uuid(n))
    saved = [{k: v for k, v in plan.items() if k not in ("reason", "size")}
             for _node, plan in plans]
    try:
        checkpoint, evicted = _STORE.add(
            _doc_key(doc), label, total, len(saved),
            {"layers": saved, "known": known})
    except StoreError as exc:
        raise _err(str(exc), exc.kind)
    return {"checkpoint": checkpoint.id, "document": doc.name(),
            "label": label,
            "layers": [{"name": s["name"], "uuid": s["uuid"],
                        "type": s["type"], "saved": s["saved"]}
                       for s in saved],
            "bytes": total, "skipped": skipped, "evicted": evicted}


def _restore_pixels(node, rect, pixels):
    from .compat import QByteArray
    current = node.bounds()
    current_rect = (current.x(), current.y(), current.width(),
                    current.height())
    # Whatever the layer holds outside the saved rectangle was painted after
    # the checkpoint: clear it, then lay the saved pixels over the top.
    if not rect_contains(rect, current_rect):
        x, y, w, h = current_rect
        node.setPixelData(QByteArray(bytes(w * h * 4)), x, y, w, h)
    if rect is not None:
        x, y, w, h = rect
        node.setPixelData(QByteArray(pixels), x, y, w, h)


def op_restore(params):
    """Put a checkpoint's layer state back. Repeatable; the checkpoint stays.

    Writes properties and pixels directly, OUTSIDE Krita's undo history: Edit >
    Undo cannot take a restore back (checkpoint first if you may want the
    current state again). Layers deleted since are reported in ``missing`` and
    not recreated; layers added since are left untouched (``added_since``).
    Layer names, order and parenting are not part of a checkpoint. Everything
    that can be refused is refused before the document changes.
    """
    o = _o()
    checkpoint_id = o._req(params, "checkpoint")
    if not isinstance(checkpoint_id, str):
        raise _err("checkpoint must be a checkpoint id such as 'cp1'")
    open_docs = {_doc_key(d): d for d in o._documents()}
    _STORE.forget_closed(open_docs)
    checkpoint = _STORE.get(checkpoint_id)
    if checkpoint is None:
        raise _err("No checkpoint {0!r}: it was never made, was dropped or "
                   "evicted, or its document was closed. list_checkpoints "
                   "shows what exists.".format(checkpoint_id),
                   kind="not_found")
    doc = open_docs[checkpoint.doc_key]
    if params.get("document") not in (None, ""):
        given = o.resolve_document(params["document"])
        if _doc_key(given) != checkpoint.doc_key:
            raise _err(
                "Checkpoint {0!r} belongs to document {1!r}, not {2!r}. Omit "
                "`document` or pass the right one.".format(
                    checkpoint_id, doc.name(), given.name()))

    current = {}
    for _path, node in _walk(doc.rootNode(), ""):
        uuid = o._node_uuid(node)
        if uuid:
            current[uuid] = node
    from . import imaging
    targets, missing = [], []
    for layer in checkpoint.data["layers"]:
        node = current.get(layer["uuid"])
        if node is None:
            missing.append({"name": layer["name"], "uuid": layer["uuid"]})
        elif node.type() != layer["type"]:
            raise _err(
                "Layer {0!r} was a {1} at the checkpoint and is a {2} now; "
                "nothing was restored.".format(node.name(), layer["type"],
                                               node.type()), kind="conflict")
        elif layer["saved"] == "pixels" and \
                not imaging.supports_pixel_ops(node):
            raise _err(
                "Layer {0!r} is {1}/{2} now but was RGBA/U8 at the "
                "checkpoint; nothing was restored.".format(
                    node.name(), node.colorModel(), node.colorDepth()),
                kind="conflict")
        else:
            targets.append((node, layer))

    # Pixels before properties: a layer that was locked at the checkpoint is
    # locked again only after its pixels are back.
    for node, layer in targets:
        if layer["saved"] == "pixels":
            _restore_pixels(node, layer["rect"], layer["pixels"])
        _apply_props(node, layer["type"], layer["props"])
    o._refresh(doc)

    known = checkpoint.data["known"]
    added = [n.name() for uuid, n in current.items() if uuid not in known]
    return {"checkpoint": checkpoint.id, "document": doc.name(),
            "restored": [node.name() for node, _layer in targets],
            "missing": missing, "added_since": added}


def op_list_checkpoints(params):
    """List a document's checkpoints and the shared memory budget."""
    o = _o()
    doc = o.resolve_document(params.get("document"))
    _apply_limit()
    _STORE.forget_closed(_open_keys())
    name = doc.name()
    return {"checkpoints": [
        {"checkpoint": c.id, "document": name, "label": c.label,
         "created": c.created, "layers": c.layers, "bytes": c.size}
        for c in _STORE.list(_doc_key(doc))],
        "budget": {"used_bytes": _STORE.used_bytes,
                   "limit_bytes": _STORE.limit_bytes}}


def op_drop_checkpoints(params):
    """Drop checkpoints to free memory: the given ids, or all of the document's."""
    o = _o()
    doc = o.resolve_document(params.get("document"))
    ids = params.get("checkpoints")
    if ids is not None and (not isinstance(ids, list) or not ids or
                            not all(isinstance(i, str) for i in ids)):
        raise _err("`checkpoints` must be a non-empty array of checkpoint "
                   "ids; omit it to drop all of the document's.")
    try:
        return {"dropped": _STORE.drop(_doc_key(doc), ids)}
    except StoreError as exc:
        raise _err(str(exc), exc.kind)


def register(op):
    op("checkpoint", timeout=120.0, mutates=False)(op_checkpoint)
    op("restore", timeout=180.0, mutates=True)(op_restore)
    op("list_checkpoints", timeout=15.0)(op_list_checkpoints)
    op("drop_checkpoints", timeout=15.0)(op_drop_checkpoints)
