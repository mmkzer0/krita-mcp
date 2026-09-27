#!/usr/bin/env python3
"""Soak one close strategy until Krita dies (or the run completes).

Each iteration builds a document with a history like the integration suite's
-- draw, filter, geometry, save, export -- then closes it with the chosen
strategy and immediately asks for status, which is what exposed the crash.

    python stress_close.py [strategy] [iterations]

Strategies: ``op`` (the default) closes through the bridge's ``close_document``
operation and needs nothing but a running bridge. ``document_close``,
``file_close_action``, ``close_views_first`` and ``deferred`` run Python inside
Krita, so they need the plugin's allow_python opt-in ("Arbitrary Python is
opt-in" in the README); the harness refuses to start them while the gate is
closed instead of tripping over it mid-run.

``STRESS_GROUPS`` (default ``geometry,restack,errors,reads``) selects the work
inside an iteration: ``geometry`` runs the canvas crop/scale/rotate/flatten
sequence, ``restack`` the layer surgery, ``reads`` the image and pixel reads,
and ``python`` the two run_python probes (which need the same opt-in). Leaving
``geometry`` out still builds, edits, saves, exports and closes a document
every iteration, which is what isolates the close path from the canvas path.
``STRESS_VIEW=0`` asks for view-less documents and verifies the bridge honoured
that. ``STRESS_ALLOW_OPEN_DOCS=1`` overrides the refusal to soak while Krita
holds other documents.

The last line of output is ``RESULT`` plus a JSON summary: per-iteration
outcomes, new crash reports and the operation the trace log left unfinished.
Exit codes: 0 survived, 1 usage, 2 Krita died, 3 operation error, 4 Python gate
closed, 5 bridge unreachable at start, 6 other documents open.
"""

import json
import os
import sys
import tempfile
import time

from mcp_server import (BRIDGE, CALL_TIMEOUT, BridgeError, BridgeUnavailable,
                        state_dir)

NAME = "stress doc"
TRACE_FILE = "krita_mcp_trace.log"
DEFAULT_STRATEGY = "op"
DEFAULT_ITERATIONS = 8
DEFAULT_GROUPS = ("geometry", "restack", "errors", "reads")
KNOWN_GROUPS = DEFAULT_GROUPS + ("python",)
CRASH_SETTLE_S = 20.0

STRATEGIES = {
    # The current implementation: setModified(False) then Document.close().
    "document_close": """
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
doc.close()
result = 'closed'
""",

    # Close through Krita's own menu action, the path Ctrl+W uses.
    "file_close_action": """
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
krita.setActiveDocument(doc)
a = krita.action('file_close')
if a is None:
    raise RuntimeError('no file_close action')
a.trigger()
result = 'triggered'
""",

    # Drop the views through the window first, then retire the document.
    "close_views_first": """
from krita_mcp.compat import QCoreApplication, QEvent, QEventLoop
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
closed = 0
for w in krita.windows():
    for v in w.views():
        if v.document() is not None and v.document().fileName() == doc.fileName() \\
                and v.document().name() == doc.name():
            v.close()
            closed += 1
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
QCoreApplication.instance().processEvents(QEventLoop.ExcludeUserInputEvents, 50)
doc.close()
result = 'views closed: %d' % closed
""",

    # Hand the close to the event loop instead of running it in this stack.
    "deferred": """
from krita_mcp.compat import QTimer
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
QTimer.singleShot(0, doc.close)
result = 'scheduled'
""",
}

PREAMBLE = """
from krita import Krita
krita = Krita.instance()
_target = None
for d in krita.documents():
    if d.name() == %r:
        _target = d
        break
if _target is None:
    raise RuntimeError('target document not found')
"""

# Counters for the summary: operations attempted, and the deliberate failures
# the `errors` group produces.
TOTALS = {"operations": 0, "expected_failures": 0, "unexpected_successes": 0}
LAST_OP = None


def call(op, params=None, timeout=CALL_TIMEOUT):
    """BRIDGE.call, remembering which operation was last in flight.

    A BridgeError on its own does not say what failed; the run reports LAST_OP
    next to it, and a crash is attributed the same way.
    """
    global LAST_OP
    LAST_OP = op
    TOTALS["operations"] += 1
    return BRIDGE.call(op, params or {}, timeout=timeout)


def _is_crash_report(name):
    return name.startswith("krita-") and name.endswith((".ips", ".crash"))


def crash_reports():
    """Krita crash reports this host keeps, or None when that is unknown.

    Only macOS is implemented: its reporter writes ``krita-*.ips`` into the
    per-user diagnostic directory, so the delta across a run is the crash
    count. Elsewhere the summary says so rather than guessing.
    """
    if sys.platform != "darwin":
        return None
    roots = (os.path.expanduser("~/Library/Logs/DiagnosticReports"),
             "/Library/Logs/DiagnosticReports")
    found = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if _is_crash_report(name) and os.path.isfile(path):
                found.append(path)
    return found


def _wait_for_crash_reports(known):
    """Let the reporter finish writing, then return the reports that are new."""
    if known is None:
        return []
    end = time.time() + CRASH_SETTLE_S
    while True:
        new = [path for path in crash_reports() if path not in known]
        if new or time.time() >= end:
            return new
        time.sleep(1.0)


def _trace_offset():
    """Where this run's trace entries begin.

    The log is append-only and survives across sessions, so a crash from an
    earlier run would otherwise be reported as this run's unfinished
    operation.
    """
    try:
        return os.path.getsize(os.path.join(state_dir(), TRACE_FILE))
    except OSError:
        return None


def trace_pending_op(offset=None):
    """The operation the trace log shows as started and never finished.

    The bridge writes a line when an operation starts and another when it
    ends, so a crash leaves a start without its end -- a lead, not proof: only
    a crash report and its stack name the faulting code. ``offset`` skips the
    entries of earlier runs. None when tracing was off (start Krita with
    KRITA_MCP_TRACE=1), the log rotated, or nothing is pending.
    """
    path = os.path.join(state_dir(), TRACE_FILE)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            if offset:
                handle.seek(offset)
            lines = handle.readlines()
    except OSError:
        return None
    pending = None
    for line in lines:
        if "-->" in line:
            pending = line.split("-->", 1)[1].strip().split(" ", 1)[0]
        elif "<--" in line:
            pending = None
    return pending


def _harness_document(doc, name, tmpdir=None):
    """Is this document one the harness owns?

    Its own target, a file it wrote into this run's temp directory, or a file
    in a ``krita-stress-*`` temp directory -- leftovers from an earlier run,
    which a crash or a failed iteration can leave behind.
    """
    if doc.get("name") == name:
        return True
    file_name = doc.get("file_name") or ""
    if tmpdir and file_name.startswith(tmpdir):
        return True
    return os.path.basename(os.path.dirname(file_name)).startswith("krita-stress-")


def _sweep(name, tmpdir):
    """Close documents this harness owns, left over from an earlier iteration.

    A failed iteration can leave its document (or the exported PNG) open;
    letting those pile up would skew the next iteration and confuse
    resolve_document's name matching.
    """
    stray = []
    for _ in range(5):
        status = call("status", {}, timeout=15)
        stray = [d for d in status.get("documents", [])
                 if _harness_document(d, name, tmpdir)]
        if not stray:
            return
        for doc in stray:
            call("close_document",
                 {"document": _document_ref(doc), "discard_changes": True},
                 timeout=60)
    raise BridgeError(
        "stale_documents",
        "Could not close {0} leftover document(s); Krita is not accepting "
        "close_document for them.".format(len(stray)))


def _document_ref(doc):
    """A reference resolve_document matches exactly.

    A document opened from a file has an empty ``name`` in Krita 5.3 (the
    suite's `document: ""` therefore closes whichever document is active);
    the file path is the unambiguous reference.
    """
    return doc.get("file_name") or doc.get("name") or ""


def _other_documents(name):
    """Documents an unexpected crash would take down with this run."""
    status = call("status", {}, timeout=15)
    return [d.get("name") or _document_ref(d)
            for d in status.get("documents", [])
            if not _harness_document(d, name)]


def _python_gate_open():
    """Does the plugin half of the gate let run_python through right now?

    The probe runs ``result = None``: cheap, and it answers about the live
    policy rather than about the advertised list, which can trail a refresh.
    """
    try:
        call("run_python", {"code": "result = None"}, timeout=30)
    except BridgeError as exc:
        if exc.kind == "disabled":
            return False
        raise
    return True


def _verify_view(doc_name, want_view):
    """Fail loudly when STRESS_VIEW=0 did not produce a view-less document."""
    if want_view:
        return
    info = call("document_info", {"document": doc_name}, timeout=15)
    views = info.get("views")
    if views is None:
        raise BridgeError(
            "view_not_supported",
            "STRESS_VIEW=0 needs a bridge that reports view counts in "
            "document_info; this one does not.")
    if views:
        raise BridgeError(
            "view_attached",
            "{0!r} was created with view=False but {1} view(s) are "
            "attached.".format(doc_name, views))


def _expect_refusal(op, params, kind):
    """Call an operation the bridge is expected to refuse.

    A refusal counts as an expected outcome, an unrefused call as an
    unexpected one: a viewed document must not reach Krita's canvas code here.
    """
    try:
        call(op, params, timeout=60)
    except BridgeError as exc:
        if exc.kind != kind:
            raise
        TOTALS["expected_failures"] += 1
    else:
        TOTALS["unexpected_successes"] += 1
        print("    note: {0} was allowed on a viewed document".format(op))


def _geometry(name, view):
    """The canvas geometry a suite-like iteration runs on a document.

    Its own group because these are the operations that destabilise a document
    that is on a canvas: the bridge refuses the size-changing ones there
    (see the README's crash note), so a viewed run records those refusals and
    exercises flatten, which leaves the size alone. STRESS_VIEW=0 runs all of
    them, which is the mode long scripted work is supposed to use.
    """
    size_changing = (
        ("crop_image", {"x": 0, "y": 0, "width": 220, "height": 160}, 60),
        ("scale_image", {"width": 150, "height": 110}, 90),
        ("rotate_image", {"degrees": 90}, 60),
    )
    for op, params, timeout in size_changing:
        params = dict(params, document=name)
        if view:
            _expect_refusal(op, params, "unsafe_on_view")
        else:
            call(op, params, timeout=timeout)
    call("flatten_image", {"document": name}, timeout=60)


def _restack(name):
    """The layer surgery the integration suite performs."""
    call("duplicate_layer", {"document": name, "layer": "Art",
                             "name": "Art copy"}, timeout=60)
    call("move_layer", {"document": name, "layer": "Art copy",
                        "parent": "G", "index": 0}, timeout=60)
    call("delete_layer", {"document": name, "layer": "G/Art copy"},
         timeout=60)


def _errors(name):
    """Operations that fail on purpose, to leave failed-call debris behind."""
    for params, op in (
        ({"document": name, "commands": [{"type": "banana"}]}, "draw"),
        ({"document": name, "commands": [{"type": "rect", "x": 0, "y": 0}]},
         "draw"),
        ({"document": name, "x": 99999, "y": 99999}, "get_pixel"),
        ({"document": "does-not-exist"}, "document_info"),
        ({"document": name, "layer": "no such layer", "opacity": 100},
         "set_layer"),
        ({"document": name, "filter": "not-a-filter"}, "apply_filter"),
    ):
        try:
            call(op, params, timeout=30)
        except BridgeError:
            TOTALS["expected_failures"] += 1
        else:
            TOTALS["unexpected_successes"] += 1
            print("    note: {0} accepted {1} without complaining".format(
                op, json.dumps(params)[:120]))


def _reads(name):
    """Pixel and image reads that need no Python opt-in."""
    call("get_image", {"document": name, "max_size": 200})
    call("get_image", {"document": name, "layer": "Art",
                       "region": {"x": 0, "y": 0, "width": 60,
                                  "height": 60}})
    call("get_pixel", {"document": name, "x": 5, "y": 5})


def _python_probes():
    """The run_python calls the old `reads` group made; now its own group."""
    call("run_python", {"code": "result = len(krita.documents())"})
    try:
        call("run_python", {"code": "raise ValueError('boom')"})
    except BridgeError:
        # run_python reports a script exception as a successful call, so this
        # only fires if the operation itself failed.
        pass


def _with_view(params, view):
    """create_document/open_document params, asking for no view when told to."""
    if view:
        return params
    out = dict(params)
    out["view"] = False
    return out


def _roundtrip(tmpdir, index, view):
    """Write the document out and read it back, the way the suite does."""
    png = os.path.join(tmpdir, "s{0}.png".format(index))
    kra = os.path.join(tmpdir, "s{0}.kra".format(index))
    call("export_document", {"document": NAME, "path": png}, timeout=90)
    call("save_document", {"document": NAME, "path": kra}, timeout=90)
    opened = call("open_document", _with_view({"path": png}, view), timeout=90)
    opened_ref = _document_ref(opened)
    _verify_view(opened_ref, view)
    call("close_document", {"document": opened_ref,
                            "discard_changes": True}, timeout=60)


def _build(tmpdir, index, groups, view):
    """One iteration's document history, up to (not including) the close."""
    call("create_document", _with_view(
        {"width": 300, "height": 220, "name": NAME,
         "background": "#203040"}, view))
    _verify_view(NAME, view)
    call("create_layer", {"document": NAME, "name": "Art"})
    call("create_layer", {"document": NAME, "name": "G",
                          "type": "grouplayer"})
    call("create_layer", {"document": NAME, "name": "Nested",
                          "parent": "G"})
    call("draw", {"document": NAME, "layer": "Art", "commands": [
        {"type": "fill_rect", "x": 0, "y": 0, "w": 300, "h": 220,
         "color": "#ffffff"},
        {"type": "rect", "x": 10, "y": 10, "w": 90, "h": 60, "fill": "#e94f37",
         "color": "#111111", "stroke_width": 3},
        {"type": "ellipse", "x": 120, "y": 10, "w": 80, "h": 60,
         "fill": "#3f88c5"},
        {"type": "text", "x": 12, "y": 150, "text": "stress", "size": 22,
         "color": "#111111"},
    ]})
    if "restack" in groups:
        _restack(NAME)
    if "reads" in groups:
        _reads(NAME)
    if "python" in groups:
        _python_probes()
    call("apply_filter", {"document": NAME, "layer": "Art",
                          "filter": "invert"}, timeout=60)
    call("apply_filter", {"document": NAME, "layer": "Art",
                          "filter": "blur",
                          "region": {"x": 0, "y": 0, "width": 150,
                                     "height": 120}}, timeout=60)
    call("set_selection", {"document": NAME, "mode": "rect", "x": 5,
                           "y": 5, "width": 40, "height": 40})
    call("set_selection", {"document": NAME, "mode": "none"})
    if "geometry" in groups:
        _geometry(NAME, view)
    _roundtrip(tmpdir, index, view)
    if "errors" in groups:
        _errors(NAME)
    # dirty it again, exactly as the suite does before its final close
    call("set_layer", {"document": NAME, "blending_mode": "multiply"})


def _iterate(tmpdir, index, strategy, groups, view):
    """One iteration: build, close, and prove the document is gone.

    Returns the iteration's result entry. BridgeUnavailable (Krita died) and
    BridgeError (the iteration failed) are left for the caller to classify.
    """
    started = time.time()
    _sweep(NAME, tmpdir)
    _build(tmpdir, index, groups, view)
    if strategy == "op":
        call("close_document", {"document": NAME, "discard_changes": True},
             timeout=90)
    else:
        code = PREAMBLE % NAME + STRATEGIES[strategy]
        call("run_python", {"code": code}, timeout=90)
    # the call that used to read freed memory
    status = call("status", {}, timeout=15)
    still = [d for d in status.get("documents", []) if d.get("name") == NAME]
    if still:
        # the deferred strategy needs a beat before it takes effect
        time.sleep(0.5)
        status = call("status", {}, timeout=15)
        still = [d for d in status.get("documents", []) if d.get("name") == NAME]
    print("  iteration {0}: ok ({1} docs open, target closed={2})".format(
        index, status.get("open_document_count"), not still))
    return {"iteration": index, "outcome": "ok", "target_closed": not still,
            "seconds": round(time.time() - started, 1)}


def _arguments(args):
    """(strategy, iterations) from the command line, or ValueError."""
    strategy = args[0] if args else DEFAULT_STRATEGY
    if strategy != "op" and strategy not in STRATEGIES:
        raise ValueError("unknown strategy {0!r}".format(strategy))
    try:
        iterations = int(args[1]) if len(args) > 1 else DEFAULT_ITERATIONS
    except ValueError:
        raise ValueError("iterations must be a whole number")
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    return strategy, iterations


def _parse_groups():
    raw = os.environ.get("STRESS_GROUPS", ",".join(DEFAULT_GROUPS))
    groups = [g.strip() for g in raw.split(",") if g.strip()]
    unknown = sorted(set(groups) - set(KNOWN_GROUPS))
    if unknown:
        raise ValueError("unknown STRESS_GROUPS entries: {0}; known: {1}"
                         .format(", ".join(unknown), ", ".join(KNOWN_GROUPS)))
    return groups


def _print_usage():
    print("usage: stress_close.py [strategy] [iterations]")
    print("strategies: op (default), " + ", ".join(sorted(STRATEGIES)))
    print("STRESS_GROUPS: " + ", ".join(KNOWN_GROUPS) + " (default " +
          ",".join(DEFAULT_GROUPS) + ")")


def _preflight(strategy, groups):
    """Refuse a run that cannot produce usable evidence.

    Returns an exit code when the run must not start, None to go ahead. Every
    refusal prints what is wrong and what to do about it.
    """
    needs_python = strategy != "op" or "python" in groups
    try:
        if needs_python and not _python_gate_open():
            print("run_python is disabled, so this configuration cannot run:")
            print("  strategy {0!r}, groups {1}".format(
                strategy, ",".join(groups)))
            print("Turn on the plugin's allow_python switch (README: "
                  "'Arbitrary Python is opt-in'), or use the default 'op' "
                  "strategy without the python group.")
            return 4
        others = _other_documents(NAME)
        if others and os.environ.get("STRESS_ALLOW_OPEN_DOCS") != "1":
            print("Krita has other documents open: {0}".format(
                ", ".join(others)))
            print("A crash takes them down with it. Save and close them "
                  "first, or set STRESS_ALLOW_OPEN_DOCS=1 to accept that "
                  "risk.")
            return 6
    except BridgeError as exc:
        print("preflight failed: {0}".format(exc))
        return 3
    return None


def _crash_names(results):
    """Crash reports the run collected, one name each."""
    return sorted({name for entry in results
                   for name in entry.get("new_crash_reports", [])})


def _iterate_all(tmpdir, strategy, iterations, groups, view, known_reports):
    """Run every iteration and classify what each one did.

    Returns (results, exit code). A crash stops the run -- nothing after it is
    comparable -- while a failed iteration is recorded and the next one starts
    from a swept document.
    """
    results = []
    exit_code = 0
    for index in range(1, iterations + 1):
        started = time.time()
        try:
            results.append(_iterate(tmpdir, index, strategy, groups, view))
        except BridgeUnavailable as exc:
            new = _wait_for_crash_reports(known_reports)
            results.append({"iteration": index, "outcome": "crashed",
                            "op": LAST_OP,
                            "seconds": round(time.time() - started, 1),
                            "new_crash_reports": [os.path.basename(p)
                                                  for p in new]})
            print("  iteration {0}: KRITA DIED during {1} ({2})".format(
                index, LAST_OP, str(exc)[:200]))
            exit_code = 2
            break
        except BridgeError as exc:
            results.append({"iteration": index, "outcome": "error",
                            "op": LAST_OP, "kind": exc.kind,
                            "message": str(exc)[:300],
                            "seconds": round(time.time() - started, 1)})
            print("  iteration {0}: error {1} in {2}: {3}".format(
                index, exc.kind, LAST_OP, str(exc)[:200]))
            exit_code = 3
    return results, exit_code


def _soak(strategy, iterations, groups, view, health, tmpdir):
    """Run the iterations, print the RESULT line, return the exit code."""
    print("strategy: {0}, {1} iterations, groups: {2}, view: {3}".format(
        strategy, iterations, ",".join(groups), "on" if view else "off"))
    print("krita {0}, plugin {1}, {2} operations advertised, pid {3}".format(
        health.get("krita_version"), health.get("plugin_version"),
        len(health.get("operations") or []), health.get("pid")))

    known_reports = crash_reports()
    trace_offset = _trace_offset()
    started = time.time()
    results, exit_code = _iterate_all(
        tmpdir, strategy, iterations, groups, view, known_reports)
    if exit_code == 0:
        print("survived all {0} iterations".format(iterations))

    counts = {"ok": 0, "error": 0, "crashed": 0}
    for entry in results:
        counts[entry["outcome"]] += 1

    summary = {
        "strategy": strategy,
        "groups": groups,
        "view": view,
        "iterations_requested": iterations,
        "iterations": results,
        "outcomes": counts,
        "operations": TOTALS["operations"],
        "expected_failures": TOTALS["expected_failures"],
        "unexpected_successes": TOTALS["unexpected_successes"],
        "krita_version": health.get("krita_version"),
        "plugin_version": health.get("plugin_version"),
        "bridge_pid": health.get("pid"),
        "advertised_operations": len(health.get("operations") or []),
        "run_python_advertised": "run_python" in (health.get("operations") or []),
        "new_crash_reports": _crash_names(results),
        "crash_reports_available": known_reports is not None,
        "crash_settle_seconds": CRASH_SETTLE_S,
        "trace_pending_op": trace_pending_op(trace_offset),
        "seconds": round(time.time() - started, 1),
        "exit_code": exit_code,
    }
    print("RESULT " + json.dumps(summary, sort_keys=True))
    return exit_code


def main():
    try:
        strategy, iterations = _arguments(sys.argv[1:])
        groups = _parse_groups()
    except ValueError as exc:
        print(str(exc))
        _print_usage()
        return 1
    view = os.environ.get("STRESS_VIEW", "1") != "0"
    try:
        health = BRIDGE.health()
    except BridgeUnavailable as exc:
        print("bridge unreachable before the run started:\n{0}".format(exc))
        return 5
    refused = _preflight(strategy, groups)
    if refused is not None:
        return refused
    tmpdir = tempfile.mkdtemp(prefix="krita-stress-")
    return _soak(strategy, iterations, groups, view, health, tmpdir)


if __name__ == "__main__":
    sys.exit(main())
