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
# value can contain a NUL, so this cannot collide with a real setting.
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

    The environment variable wins whenever it is present; otherwise the
    setting decides; with neither configured the operation stays off.
    """
    if env_value is not None:
        return truthy(env_value), "env"
    if setting_value is not UNSET:
        return truthy(setting_value), "setting"
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
        self._enabled = False
        self._source = "default"

    @property
    def enabled(self):
        return self._enabled

    @property
    def source(self):
        return self._source

    def refresh(self):
        """Re-read both switches and return the decision. Owner thread only."""
        self.assert_owner_thread()
        self._enabled, self._source = decision(
            self._read_setting(), os.environ.get(ENV_VAR))
        return self._enabled

    def advertised(self, names):
        """The operation names clients may see. Safe on any thread."""
        if self._enabled:
            return list(names)
        return [name for name in names if name not in GATED_OPERATIONS]

    def assert_owner_thread(self):
        if threading.current_thread() is not self._owner:
            raise RuntimeError(
                "the gate may only be refreshed from the thread that owns "
                "Krita ({0!r}), not {1!r}".format(
                    self._owner.name, threading.current_thread().name))
