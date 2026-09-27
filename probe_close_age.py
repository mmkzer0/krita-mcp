#!/usr/bin/env python3
"""Does a document's exposure before close_document make Krita die?

The integration suite dies in Krita's own teardown at the end of a long run:
its trace ends at ``close: close call returned`` with nothing after it, and the
crash report for the same event is a SIGSEGV in ``KisNodeDummy::parent()``
reached from ``KisNodeModel::processUpdateQueue`` while the plugin's post-close
pump was running the event loop (tmp/audit-2026-09-26.md section 3c). The soak,
whose documents live about 1.5 seconds, closes hundreds of them cleanly. This
probe separates the candidate variables:

    age       how long the document has been open before the close
    work      the suite-like history it accumulated while it was open
    view      whether a canvas shows the document

A cell is (scenario, view, age). ``--scenario idle`` creates the document and
waits; ``--scenario session`` replays a compact version of the suite's history
on it every few seconds until the age is up. Every repetition creates its own
document, closes it with discard_changes, and then asks the bridge whether
Krita is still answering and the document is gone.

A death is attributed with the crash reports that appeared, the Krita process
exit status (when this probe launched Krita), the tail of Krita's stderr and
the freshly written bridge-trace lines. With --launch the probe brings up its
own Krita and restarts it after a loss, so one sweep covers the whole matrix;
without --launch it uses the running Krita and stops at the first loss.

Exit codes: 0 every close survived, 2 at least one death (argument errors exit
the same way, with argparse's own message), 3 the bridge never answered,
5 refused to interfere (a document the probe does not own is open, or --launch
was given while Krita is already running), 6 a cell produced no verdict -- an
operation error, or a bridge that stopped answering without the process dying.

Ownership: the probe closes only the documents it created in this run. An open
document at start-up -- even one whose name looks like the probe's -- stops the
run unless --allow-open-docs accepts that a death would take it down too.

Outcomes are kept apart on purpose: "died" needs the launched process to have
left an exit status, while a bridge that merely stops answering is
"unreachable" with `krita_alive: null`.

Limits: the session scenario keeps its work on one document -- it does not open
a second one, so a death is always the close of the document this cell built.
Undo and deselect go through Krita's own window actions, which act on the
*active* document; the probe only fires them when its document is active and
records that per repetition.
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from mcp_server import (BRIDGE, CALL_TIMEOUT, BridgeError, BridgeUnavailable,
                        state_dir)
from stress_close import crash_reports

DOC_PREFIX = "close-probe"
# Outcomes that mean the cell did not produce a verdict: "died" when the
# process itself is gone, "unreachable" when only the bridge stopped answering.
LOST = ("died", "unreachable")
DEFAULT_AGES = (0, 5, 15, 60)
DEFAULT_REPS = 3
DEFAULT_SCENARIO = "session"
DEFAULT_VIEWS = ("on", "off")
SCENARIOS = ("idle", "session")
DEFAULT_SETTLE = 8.0
DEFAULT_BIN = "/Applications/krita.app/Contents/MacOS/krita"
HEALTH_TIMEOUT = 90.0
STOP_TIMEOUT = 20.0
ROUND_PERIOD = 4.0
TRACE_FILE = "krita_mcp_trace.log"
TRACE_TAIL_LINES = 8
STDERR_TAIL_LINES = 6

# The suite-like round. Structural layer changes, a stroke, an undo through
# Krita's own action, filters, a selection, reads, a save and an export -- the
# mix a long-lived scripted document accumulates before its close kills Krita.
# Nothing here changes the document's size, so a viewed cell does not trip the
# geometry guard (see the README's crash note).
ROUND_COMMANDS = [
    {"type": "fill_rect", "x": 0, "y": 0, "w": 320, "h": 240,
     "color": "#ffffff"},
    {"type": "rect", "x": 8, "y": 8, "w": 90, "h": 60, "fill": "#e94f37",
     "color": "#111111", "stroke_width": 3},
    {"type": "ellipse", "x": 120, "y": 20, "w": 80, "h": 60,
     "fill": "#3f88c5"},
]

class ProbeError(Exception):
    """The probe cannot trust what it just measured."""

def call(op, params=None, timeout=CALL_TIMEOUT):
    """BRIDGE.call with the probe's default timeout."""
    return BRIDGE.call(op, params or {}, timeout=timeout)

def trace_path():
    """The bridge trace log the plugin writes when KRITA_MCP_TRACE=1."""
    return os.path.join(state_dir(), TRACE_FILE)

def stderr_path(tmpdir):
    """Where the Krita this probe launched writes its stderr."""
    return os.path.join(tmpdir, "krita-stderr.log")

def status_path(tmpdir, launch=1):
    """Where one launch's wrapper leaves the exit status of *its* Krita."""
    return os.path.join(tmpdir, "krita-exit-status-{0}".format(launch))

def _tail(path, lines):
    """The last ``lines`` non-empty lines of a file, or [] when unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]

def _trace_since(offset):
    """Trace lines written since ``offset``, newest last."""
    try:
        with open(trace_path(), "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            text = fh.read()
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]

def _trace_offset():
    """Where this run's trace entries begin; None when there is no log yet."""
    try:
        return os.path.getsize(trace_path())
    except OSError:
        return None

class Launcher:
    """A Krita this probe started, with an attributable exit status.

    The binary runs under a tiny shell wrapper so the exit status survives a
    signal: 134 and 139 say the process aborted or faulted, and the crash
    reports say where.
    """

    def __init__(self, binary, tmpdir, trace):
        self.binary = binary
        self.tmpdir = tmpdir
        self.trace = trace
        self.pid = None            # what the bridge reports, for the record
        self.spawned = None        # the wrapper process this probe owns
        self.status_file = None
        self.launches = 0

    def start(self):
        """Start Krita, wait for its bridge, return the health payload.

        Ownership is the process group this creates -- ``start_new_session``
        makes the wrapper its own group leader -- never the pid the bridge
        reports, because two Krita instances can share discovery state.
        """
        self.launches += 1
        self.status_file = status_path(self.tmpdir, self.launches)
        wrapper = self._write_wrapper()
        env = dict(os.environ)
        if self.trace:
            env["KRITA_MCP_TRACE"] = "1"
        log = open(stderr_path(self.tmpdir), "a", encoding="utf-8")
        self.spawned = subprocess.Popen(["/bin/sh", wrapper], stdout=log,
                                        stderr=log,
                                        stdin=subprocess.DEVNULL, env=env,
                                        start_new_session=True)
        try:
            health = self._wait_for_bridge()
        except BridgeUnavailable:
            # Never leave a Krita behind because its bridge did not come up.
            self._terminate()
            raise
        self.pid = health.get("pid")
        return health

    def _write_wrapper(self):
        """Write the shell wrapper that records this launch's exit status."""
        wrapper = os.path.join(self.tmpdir,
                               "krita-launch-{0}.sh".format(self.launches))
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n{0} --nosplash\nstatus=$?\necho "
                         "\"$status\" > {1}\n".format(
                             shlex.quote(self.binary),
                             shlex.quote(self.status_file)))
        os.chmod(wrapper, 0o755)
        return wrapper

    def _wait_for_bridge(self):
        """Poll the bridge until Krita answers or HEALTH_TIMEOUT runs out."""
        deadline = time.time() + HEALTH_TIMEOUT
        last = None
        while time.time() < deadline:
            try:
                return BRIDGE.health()
            except BridgeUnavailable as exc:
                last = exc
                time.sleep(0.5)
        raise BridgeUnavailable(
            "Krita did not bring its bridge up within {0:.0f}s: {1}".format(
                HEALTH_TIMEOUT, last))

    def _terminate(self, timeout=STOP_TIMEOUT):
        """Stop the process group this launcher created, then insist."""
        if self.spawned is None or self.spawned.poll() is not None:
            return
        group = self.spawned.pid        # its own group leader
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except OSError:
                return
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.spawned.poll() is not None:
                    return
                time.sleep(0.5)

    def exit_status(self):
        """This launch's exit status, or None while it still runs."""
        if self.status_file is None:
            return None
        try:
            with open(self.status_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def stop(self):
        """Stop the Krita this launch started, unless it already exited."""
        if self.exit_status() is None and self.spawned is not None:
            self._terminate()
        self.pid = None

def document_name(view, age, scenario, rep):
    """A name this probe owns, unique per cell and repetition."""
    return "{0} {1} {2} {3}s r{4}".format(
        DOC_PREFIX, scenario, view, age, rep)

def _document_ref(doc):
    """A reference resolve_document matches: file name when there is one."""
    return doc.get("file_name") or doc.get("name") or ""

def open_documents():
    """Every document Krita currently has open."""
    return call("status", {}, timeout=15).get("documents", [])

def sweep_probe_documents(created):
    """Close probe documents this run created and left behind.

    Ownership is the set of names this run created, not a name pattern: a
    document that merely *looks* like one of the probe's is somebody's work,
    and this closes with discard_changes.
    """
    stray = [doc for doc in open_documents()
             if str(doc.get("name") or "") in created]
    for doc in stray:
        created.discard(str(doc.get("name") or ""))
        call("close_document", {"document": _document_ref(doc),
                                "discard_changes": True}, timeout=90)
    return len(stray)

def view_flag(label):
    """The ``view`` boolean create_document takes for a cell's on/off label."""
    return label == "on"

def create_document(name, view):
    """Create the probe document and prove its view state matches the cell."""
    call("create_document",
         {"width": 320, "height": 240, "name": name,
          "background": "#203040", "view": view}, timeout=60)
    info = call("document_info", {"document": name}, timeout=15)
    views = info.get("views")
    if views is None:
        raise ProbeError("this bridge does not report view counts")
    if view and not views:
        raise ProbeError("{0!r} asked for a view and got none".format(name))
    if not view and views:
        raise ProbeError("{0!r} has {1} view(s) despite view=false".format(
            name, views))
    return info

def active_document_name():
    """The name of the document Krita's own actions would act on."""
    active = call("status", {}, timeout=15).get("active_document") or {}
    return active.get("name")

def session_round(name, tmpdir, index):
    """One pass of the history the suite builds on a long-lived document.

    Returns whether the window actions ran: ``edit_undo`` and ``deselect`` are
    Krita's own actions and reach whichever document is active, so they only
    fire when that is this cell's document (a view-less document is not active
    and would silently edit someone else's). The undo is the round's last call
    -- in batch mode Krita pushes a layer creation onto the undo stack above
    the stroke that followed it, so an early undo can take the layer the rest
    of the round still needs.
    """
    normal = "R{0}".format(index)
    group = "G{0}".format(index)
    call("create_layer", {"document": name, "name": normal}, timeout=60)
    call("draw", {"document": name, "layer": normal,
                  "commands": ROUND_COMMANDS}, timeout=60)
    call("apply_filter", {"document": name, "layer": normal,
                          "filter": "invert"}, timeout=60)
    call("apply_filter", {"document": name, "layer": normal,
                          "filter": "blur",
                          "region": {"x": 0, "y": 0, "width": 160,
                                     "height": 120}}, timeout=60)
    call("create_layer", {"document": name, "name": group,
                          "type": "grouplayer"}, timeout=60)
    call("duplicate_layer", {"document": name, "layer": normal,
                             "name": normal + " copy"}, timeout=60)
    call("move_layer", {"document": name, "layer": normal + " copy",
                        "parent": group, "index": 0}, timeout=60)
    call("delete_layer", {"document": name,
                          "layer": group + "/" + normal + " copy"}, timeout=60)
    call("set_selection", {"document": name, "mode": "rect", "x": 5, "y": 5,
                           "width": 40, "height": 40}, timeout=30)
    call("get_pixel", {"document": name, "x": 5, "y": 5}, timeout=30)
    call("document_info", {"document": name}, timeout=60)
    call("save_document", {"document": name, "path": os.path.join(
        tmpdir, "round{0}.kra".format(index))}, timeout=90)
    call("export_document", {"document": name, "path": os.path.join(
        tmpdir, "round{0}.png".format(index))}, timeout=90)
    # The suite's last edits before its final close are a layer property
    # change and, through its own window action, a step back in the undo
    # history.
    call("set_layer", {"document": name, "blending_mode": "multiply"},
         timeout=30)
    actions = active_document_name() == name
    if actions:
        call("trigger_action", {"name": "deselect"}, timeout=30)
        call("trigger_action", {"name": "edit_undo"}, timeout=30)
    return actions

def expose(name, age, scenario, tmpdir):
    """Keep the document alive for ``age`` seconds.

    Returns (rounds run, whether the window actions ran at least once).
    """
    started = time.time()
    if scenario == "idle":
        remaining = started + age - time.time()
        if remaining > 0:
            time.sleep(remaining)
        return 0, False
    rounds = 0
    actions = False
    while True:
        rounds += 1
        actions = session_round(name, tmpdir, rounds) or actions
        remaining = started + age - time.time()
        if remaining <= 0:
            return rounds, actions
        time.sleep(min(ROUND_PERIOD, remaining))

def close_document(name, expected_count):
    """Close the document, then prove Krita is answering and it is gone.

    The count is checked as well as the name: a document that renamed itself
    would otherwise look closed while it is still open.
    """
    call("close_document", {"document": name, "discard_changes": True},
         timeout=90)
    status = call("status", {}, timeout=15)
    still = [doc for doc in status.get("documents", [])
             if doc.get("name") == name]
    if still:
        raise ProbeError("close_document left {0!r} open".format(name))
    count = status.get("open_document_count")
    if count != expected_count:
        raise ProbeError(
            "expected {0} document(s) open after the close, Krita reports "
            "{1}".format(expected_count, count))
    return status

def run_cell(cell, session):
    """Run one repetition; never raises, the outcome is part of the result.

    ``session`` carries the mutable state of the sweep: the temporary
    directory, the settle time, where this run's trace entries begin, the
    launcher (or None), the names this run created and the crash-report
    baseline.
    """
    view, age, scenario, rep = cell
    name = document_name(view, age, scenario, rep)
    started = time.time()
    result = {"cell": cell_label(cell), "rep": rep, "outcome": "closed",
              "name": name, "rounds": 0}
    stage = "sweep"
    try:
        result["swept"] = sweep_probe_documents(session["created"])
        baseline = len(open_documents())
        stage = "create"
        create_document(name, view_flag(view))
        session["created"].add(name)
        stage = "work"
        rounds, actions = expose(name, age, scenario, session["tmpdir"])
        result["rounds"] = rounds
        result["window_actions"] = actions
        stage = "close"
        result["open_after_close"] = close_document(name, baseline).get(
            "open_document_count")
        result["krita_alive"] = True
    except BridgeUnavailable as exc:
        result["outcome"] = "died" if _process_gone(session["launcher"]) \
            else "unreachable"
        result["stage"] = stage
        result["message"] = str(exc)[:300]
        _collect_death_evidence(result, session)
    except (BridgeError, ProbeError) as exc:
        result["outcome"] = "error"
        result["stage"] = stage
        result["kind"] = getattr(exc, "kind", "probe_error")
        result["message"] = str(exc)[:300]
        result["krita_alive"] = True
    result["seconds"] = round(time.time() - started, 1)
    return result

def _process_gone(launcher):
    """Did the Krita this run launched leave an exit status of its own?

    A bridge that stops answering is not the same thing as a process that
    died; only the wrapper's status file (or a crash report) proves the
    latter.
    """
    return launcher is not None and launcher.exit_status() is not None

def _collect_death_evidence(result, session):
    """Fill in what a lost bridge looked like; extend the report baseline."""
    time.sleep(session["settle"])
    new = _new_crash_reports(session["known"])
    result["new_crash_reports"] = [os.path.basename(p) for p in new]
    result["trace_tail"] = _trace_since(session["trace_offset"])[-TRACE_TAIL_LINES:]
    launcher = session["launcher"]
    status = launcher.exit_status() if launcher is not None else None
    if status is not None:
        result["krita_exit_status"] = status
        result["krita_alive"] = False
    else:
        result["krita_alive"] = None        # unknown: only the bridge went
    session["known"] = sorted(set(session["known"]) | set(new))

def _new_crash_reports(known):
    """Crash reports this host has now that ``known`` did not."""
    current = crash_reports()
    if current is None:
        return []
    return sorted(set(current) - set(known or []))

def cell_label(cell):
    """A stable one-line name for a cell."""
    view, age, scenario, _ = cell
    return "{0}/{1}s/{2}".format(view, age, scenario)

def cells(views, ages, scenario, reps):
    """Every (view, age, scenario, rep) the sweep will run."""
    out = []
    for view in views:
        for age in ages:
            for rep in range(1, reps + 1):
                out.append((view, age, scenario, rep))
    return out

def summarize(results, seconds, exit_code):
    """The RESULT payload: per-cell counts plus every repetition."""
    cells_summary = []
    for label in dict.fromkeys(entry["cell"] for entry in results):
        entries = [entry for entry in results if entry["cell"] == label]
        outcomes = {}
        for entry in entries:
            outcomes[entry["outcome"]] = outcomes.get(entry["outcome"], 0) + 1
        cells_summary.append({
            "cell": label,
            "outcomes": outcomes,
            "rounds": max(entry.get("rounds", 0) for entry in entries),
            "seconds": round(sum(entry["seconds"] for entry in entries), 1),
        })
    return {
        "probe": "close-age",
        "cells": cells_summary,
        "repetitions": results,
        "deaths": len([e for e in results if e["outcome"] == "died"]),
        "unreachable": len([e for e in results
                            if e["outcome"] == "unreachable"]),
        "new_crash_reports": sorted({name for entry in results
                                     for name in entry.get("new_crash_reports",
                                                           [])}),
        "seconds": seconds,
        "exit_code": exit_code,
    }

def _exit_code(results):
    """2 when any close killed Krita, 6 when a cell produced no verdict."""
    if any(entry["outcome"] == "died" for entry in results):
        return 2
    if any(entry["outcome"] in ("unreachable", "error")
           for entry in results):
        return 6
    return 0

def _preflight(allow_foreign):
    """Refuse a run against documents this probe does not own.

    At start-up it owns nothing, so any open document -- including a leftover
    from an earlier probe run -- stops the sweep unless the caller accepts the
    risk: a death takes the whole session with it, and the sweep only ever
    closes what the run created.
    """
    try:
        documents = open_documents()
    except BridgeUnavailable as exc:
        print("the bridge is not answering, so there is nothing to measure:")
        print(str(exc))
        return 3
    if not documents:
        return None
    names = [str(doc.get("name") or doc.get("file_name") or "?")
             for doc in documents]
    if allow_foreign:
        print("note: {0} document(s) this probe does not own stay open for "
              "this run".format(len(names)))
        return None
    print("Krita has documents this probe does not own: {0}".format(
        ", ".join(names)))
    print("A death takes them down with it, and this probe closes only the "
          "documents it creates. Close them first, or pass --allow-open-docs "
          "to accept that risk.")
    return 5

def _arguments(argv):
    """Parse the command line into an options namespace."""
    parser = argparse.ArgumentParser(
        description="Close documents after a controlled exposure and report "
                    "whether Krita survived.")
    parser.add_argument("--scenario", choices=SCENARIOS,
                        default=DEFAULT_SCENARIO,
                        help="idle: create and wait; session: replay the "
                             "suite-like history (default: %(default)s)")
    parser.add_argument("--views", default=",".join(DEFAULT_VIEWS),
                        help="comma-separated subset of on,off "
                             "(default: %(default)s)")
    parser.add_argument("--ages", default=",".join(str(age) for age
                                                   in DEFAULT_AGES),
                        help="comma-separated document ages in seconds "
                             "(default: %(default)s)")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS,
                        help="repetitions per cell (default: %(default)s)")
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                        help="seconds to wait for a crash report after a "
                             "death (default: %(default)s)")
    parser.add_argument("--launch", action="store_true",
                        help="start Krita for this run (and restart it after "
                             "a death); refuses when Krita is already up")
    parser.add_argument("--bin", default=os.environ.get("KRITA_BIN",
                                                        DEFAULT_BIN),
                        help="Krita binary for --launch (default: %(default)s)")
    parser.add_argument("--keep-krita", action="store_true",
                        help="leave the Krita this probe launched running")
    parser.add_argument("--allow-open-docs", action="store_true",
                        help="run even though foreign documents are open")
    parser.add_argument("--no-trace", action="store_true",
                        help="do not set KRITA_MCP_TRACE for the Krita this "
                             "probe launches")
    options = parser.parse_args(argv)
    options.view_list = _parse_views(parser, options.views)
    options.age_list = _parse_ages(parser, options.ages)
    if options.reps < 1:
        parser.error("--reps must be at least 1")
    return options

def _parse_views(parser, raw):
    """The view labels --views asked for: on or off, at least one."""
    views = [value.strip() for value in raw.split(",") if value.strip()]
    unknown = [value for value in views if value not in ("on", "off")]
    if unknown:
        parser.error("--views takes on,off; got {0}".format(",".join(unknown)))
    if not views:
        parser.error("--views needs at least one of on,off")
    return views

def _parse_ages(parser, raw):
    """The document ages --ages asked for, in whole seconds."""
    try:
        ages = [int(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError:
        parser.error("--ages takes whole seconds, e.g. 0,5,15,60")
    if not ages or min(ages) < 0:
        parser.error("--ages needs at least one age of zero or more seconds")
    return ages


def _refuse_when_krita_is_running(options):
    """With --launch, refuse to measure against somebody else's session."""
    if not options.launch:
        return None
    try:
        BRIDGE.health()
    except BridgeUnavailable:
        return None
    print("Krita is already running; quit it first, or drop --launch to "
          "measure against it.")
    return 5

def _launch_line(health):
    """The header line for the Krita this probe started."""
    return "launched krita {0} (pid {1}), plugin {2}".format(
        health.get("krita_version"), health.get("pid"),
        health.get("plugin_version"))

def _print_cell(result):
    """One line per repetition, with the detail when it did not just close."""
    print("  {0} r{1}: {2} ({3} rounds, {4}s)".format(
        result["cell"], result["rep"], result["outcome"], result["rounds"],
        result["seconds"]))
    if result["outcome"] in LOST:
        _print_loss(result)
    elif result["outcome"] == "error":
        print("    {0} in {1}: {2}".format(result.get("kind"),
                                           result.get("stage"),
                                           result.get("message")))

def _sweep_cells(options, session):
    """Run every cell, restarting Krita after a loss; return the results."""
    results = []
    for cell in cells(options.view_list, options.age_list, options.scenario,
                      options.reps):
        if session["launcher"] is None and any(entry["outcome"] in LOST
                                               for entry in results):
            print("bridge is gone and this run did not launch Krita; stopping")
            break
        result = run_cell(cell, session)
        results.append(result)
        _print_cell(result)
        if result["outcome"] in LOST and session["launcher"] is not None:
            session["launcher"].stop()
            session["launcher"].start()
            session["created"].clear()  # those documents died with it
    return results


def main(argv=None):
    options = _arguments(sys.argv[1:] if argv is None else argv)
    refused = _refuse_when_krita_is_running(options)
    if refused is not None:
        return refused
    session = {"tmpdir": tempfile.mkdtemp(prefix="krita-close-age-"),
               "settle": options.settle, "launcher": None, "created": set(),
               "known": [], "trace_offset": 0}
    started = time.time()
    try:
        if options.launch:
            session["launcher"] = Launcher(options.bin, session["tmpdir"],
                                           not options.no_trace)
            print(_launch_line(session["launcher"].start()))
        refused = _preflight(options.allow_open_docs)
        if refused is not None:
            return refused
        session["known"] = crash_reports() or []
        session["trace_offset"] = _trace_offset() or 0
        results = _sweep_cells(options, session)
        exit_code = _exit_code(results)
        print("RESULT " + json.dumps(
            summarize(results, round(time.time() - started, 1), exit_code),
            sort_keys=True))
        return exit_code
    finally:
        launcher = session["launcher"]
        if launcher is not None and not options.keep_krita:
            launcher.stop()
        print("evidence: {0}".format(session["tmpdir"]))

def _print_loss(result):
    """Say what a death or a vanished bridge looked like, straight away."""
    first = ("KRITA DIED during {0}: {1}" if result["outcome"] == "died" else
             "BRIDGE LOST during {0} (process state unknown): {1}")
    print("    " + first.format(result.get("stage"), result.get("message")))
    for line in (result.get("trace_tail") or [])[-3:]:
        print("    trace: {0}".format(line))
    if result.get("new_crash_reports"):
        print("    crash reports: {0}".format(
            ", ".join(result["new_crash_reports"])))
    if result.get("krita_exit_status"):
        print("    krita exit status: {0}".format(
            result["krita_exit_status"]))

if __name__ == "__main__":
    sys.exit(main())
