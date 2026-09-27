"""Policy for the arbitrary-Python gate.

Pure logic: no Krita, no Qt, no sockets. The plugin's worker threads answer
``/health`` without touching libkis, and this module is what makes that
possible: :meth:`Gate.advertised` reads a cached decision, while
:meth:`Gate.refresh` -- the only path that reaches Krita -- refuses to run off
the thread that owns the gate.

Both halves of the bridge consult the same policy, so the MCP server and the
plugin cannot drift apart on what "opt-in" means.
"""

import os
import threading

# Krita reads [krita_mcp] from kritarc; the matching environment variable
# exists for pipelines and tests that cannot edit the config file.
SETTING_KEY = "allow_python"
ENV_VAR = "KRITA_MCP_ALLOW_PYTHON"

# readSetting() returns the default it is handed when the key is absent, which
# is how the gate tells "explicitly off" from "never configured". No KConfig
# value can contain a NUL, so this cannot collide with a real setting. Compare
# it by value, never by identity: Krita hands the default back as a fresh
# string object, so `is` would read a missing key as an explicit deny.
UNSET = "\x00unset"

# Operations that stay hidden and refused until the operator opts in.
GATED_OPERATIONS = ("run_python",)

# Action ids that execute code. Krita publishes no "this action runs Python"
# metadata, so this is a denylist of the runners shipped with stock Krita:
# Ten Scripts executes a configured .py path, the Scripter action opens the
# script editor, and a plugin the user installs can register runners nothing
# here can see. A guard, not a sandbox.
SCRIPT_ACTIONS = ("ten_scripts", "python_scripter")
SCRIPT_ACTION_PREFIXES = ("execute_script_",)

_TRUTHY = ("true", "1", "yes", "on")


def truthy(raw):
    """Settings and environment variables arrive as strings."""
    return str(raw).strip().lower() in _TRUTHY


def runs_code(action_name):
    """Would triggering this Krita action execute Python?"""
    return (action_name in SCRIPT_ACTIONS
            or action_name.startswith(SCRIPT_ACTION_PREFIXES))


def decision(setting_value, env_value):
    """Resolve the two switches into (enabled, source of truth).

    An explicit "off" anywhere wins. That ordering matters: a stale
    ``KRITA_MCP_ALLOW_PYTHON=1`` exported in a shell profile must not override
    ``allow_python=false`` in kritarc, and ``KRITA_MCP_ALLOW_PYTHON=0`` must
    still force one session off. Enabling takes an explicit "on" from one of
    the two, with the setting deciding when both allow, and UNSET meaning the
    key was never configured rather than that it is off.
    """
    setting_configured = setting_value != UNSET
    env_configured = env_value is not None

    if setting_configured and not truthy(setting_value):
        return False, "setting"
    if env_configured and not truthy(env_value):
        return False, "env"
    if setting_configured:
        return True, "setting"
    if env_configured:
        return True, "env"
    return False, "default"


class Gate:
    """Cached gate state plus the rules about which thread may touch Krita.

    Constructed on the thread that owns libkis (the GUI thread, inside the
    plugin). ``refresh()`` is the only method that calls the injected settings
    reader, and it fails loudly when called from anywhere else, so a regression
    that wires a worker thread back into libkis is a visible error rather than
    unsynchronised access to Krita's C++ objects.
    """

    def __init__(self, read_setting):
        self._read_setting = read_setting
        self._owner = threading.current_thread()
        self._setting = UNSET  # what the last refresh read from Krita
        self._unavailable = False

    def state(self):
        """(enabled, source) without touching Krita. Safe on any thread.

        The environment half is evaluated on every call, so an export applies
        immediately, including in the window after a module reload and before
        the next operation. Only the kritarc value comes from the cache.
        """
        if self._unavailable:
            return False, "unavailable"
        return decision(self._setting, os.environ.get(ENV_VAR))

    @property
    def enabled(self):
        return self.state()[0]

    @property
    def source(self):
        return self.state()[1]

    def refresh(self):
        """Re-read the setting and return the decision. Owner thread only."""
        self.assert_owner_thread()
        self._setting = self._read_setting()
        self._unavailable = False
        return self.state()[0]

    def refresh_or_closed(self, log=None):
        """Refresh, or report the gate closed when this thread may not.

        The owner-thread rule keeps libkis off the worker threads, but the
        refusal must not travel further than the gate. Callers use this
        adapter so a bridge keeps serving its other operations with run_python
        closed and one log line explaining why, instead of answering every
        operation with an error.

        A refusal also marks the gate unavailable until the next successful
        refresh, so the advertised list cannot outlive a refusal that already
        happened.
        """
        try:
            return self.refresh()
        except Exception as exc:  # the owner-thread refusal, or a reader bug
            self._unavailable = True
            if log is not None:
                log("gate refresh failed on this thread, treating run_python "
                    "as disabled: {0}".format(exc))
            return False

    def advertised(self, names):
        """The operation names clients may see. Safe on any thread.

        Only the kritarc half can trail here: it follows the last refresh,
        which bridge start and every operation perform. The environment is
        read on each call.
        """
        if self.state()[0]:
            return list(names)
        return [name for name in names if name not in GATED_OPERATIONS]

    def assert_owner_thread(self):
        if threading.current_thread() is not self._owner:
            raise RuntimeError(
                "the gate may only be refreshed from the thread that owns "
                "Krita ({0!r}), not {1!r}".format(
                    self._owner.name, threading.current_thread().name))
