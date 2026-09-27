"""Run callables on Krita's GUI thread.

libkis (and Qt underneath it) is not thread safe: touching a Document or Node
from a worker thread corrupts state or crashes outright.  The HTTP bridge
serves requests on worker threads, so every Krita call has to be handed back
to the thread that owns those objects.

The mechanism is a queued signal.  ``MainThreadInvoker`` is constructed on the
GUI thread, so it lives there.  A worker emits ``_submit``; because emitter and
receiver are on different threads Qt delivers it through the GUI thread's event
loop, and the slot therefore runs where it is safe to.  The worker blocks on a
``threading.Event`` until the slot finishes, with a timeout so a stalled GUI
(a modal dialog, say) surfaces as an error rather than a hung socket.
"""

import threading
import traceback

from .compat import QObject, QtCompat as Qt, QTimer, pyqtSignal


class MainThreadTimeout(Exception):
    """The GUI thread did not run the job before the deadline.

    ``reason`` is "not_started" when the job was still queued and got
    cancelled -- it provably never ran -- or "still_running" when it had
    already begun and may yet complete.
    """

    def __init__(self, message, reason="not_started"):
        super().__init__(message)
        self.reason = reason


class MainThreadError(Exception):
    """The job raised on the GUI thread.

    Only a description of the failure is carried across, never the exception
    object. An exception keeps its traceback, the traceback keeps the frames,
    and those frames keep whatever libkis wrappers the failing operation had
    in scope -- Documents and Nodes among them. Holding one of those past the
    point where Krita destroys the underlying C++ object turns the next
    garbage collection into a use-after-free, which showed up as Krita dying
    a few operations after a close. Keeping strings only avoids that entirely.
    """

    def __init__(self, kind, type_name, message, formatted):
        super().__init__(message or type_name)
        self.kind = kind            # OpError.kind, or None for anything else
        self.type_name = type_name
        self.message = message
        self.formatted = formatted


class _Job(object):
    __slots__ = ("fn", "event", "result", "failure", "state")

    def __init__(self, fn):
        self.fn = fn
        self.event = threading.Event()
        self.result = None
        self.failure = None         # (kind, type_name, message, formatted)
        self.state = "queued"       # queued -> running -> finished | cancelled


class MainThreadInvoker(QObject):
    _submit = pyqtSignal(object)

    def __init__(self, parent=None):
        super(MainThreadInvoker, self).__init__(parent)
        # Queued explicitly rather than relying on auto-connection, so this
        # keeps working even if a future caller happens to be on the GUI thread.
        self._submit.connect(self._execute, Qt.QueuedConnection)
        self._home_ident = threading.get_ident()
        # Guards _Job.state between the waiting worker and the GUI thread, so
        # a cancelled job cannot start in the window after the deadline.
        self._state_lock = threading.Lock()
        self._busy = False

    def _execute(self, job):
        with self._state_lock:
            if job.state != "queued":
                # Its caller's deadline passed and cancelled it, or it has
                # already run. Running it now would mutate Krita for an
                # operation nobody is waiting for any more.
                return
            if self._busy:
                # Another job is running and is pumping the event loop (the
                # settle after a document close does this), which is how we got
                # delivered mid-operation. Re-entering libkis underneath a call
                # that is already in progress is not safe, so hand the job back
                # to the queue and pick it up once the outer one has finished.
                QTimer.singleShot(0, lambda: self._execute(job))
                return
            job.state = "running"

        self._busy = True
        try:
            job.result = job.fn()
        except BaseException as exc:  # reported to the caller, never swallowed
            # Plain strings only, in the order MainThreadError takes them: the
            # exception itself must not outlive the call, because its
            # traceback frames may hold libkis wrappers (see MainThreadError).
            job.failure = (getattr(exc, "kind", None), type(exc).__name__,
                           str(exc), traceback.format_exc())
            exc.__traceback__ = None
        finally:
            self._busy = False
            with self._state_lock:
                job.state = "finished"
            job.event.set()

    def call(self, fn, timeout=30.0):
        """Run ``fn`` on the GUI thread and return its value.

        Raises MainThreadTimeout when the deadline passes, or MainThreadError
        wrapping whatever ``fn`` raised.

        A job still queued at the deadline is cancelled and provably never runs
        (``reason`` "not_started"). One that had already started cannot be
        stopped from here, so the timeout says so (``reason`` "still_running")
        instead of implying that nothing happened.
        """
        if threading.get_ident() == self._home_ident:
            # Already home. Emitting would deadlock: a queued signal would not
            # be delivered until we returned to the event loop.
            return fn()

        job = _Job(fn)
        self._submit.emit(job)
        if not job.event.wait(timeout):
            with self._state_lock:
                state = job.state
                if state == "queued":
                    job.state = "cancelled"
            if state == "queued":
                raise MainThreadTimeout(
                    "Krita's UI thread did not pick this operation up within "
                    "{0:g}s, so it was cancelled before it started.".format(
                        timeout), reason="not_started")
            if state == "running":
                raise MainThreadTimeout(
                    "Krita's UI thread is still running this operation after "
                    "{0:g}s. It may still complete, so its outcome is "
                    "unknown; check the document before repeating it.".format(
                        timeout), reason="still_running")
            # "finished": it won the race with the deadline, so report the
            # outcome below instead of inventing a timeout.
        if job.failure is not None:
            raise MainThreadError(*job.failure)
        return job.result
