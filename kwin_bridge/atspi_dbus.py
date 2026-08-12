"""Pure-D-Bus AT-SPI client (no pyatspi required).

pyatspi is not packaged on Arch (dropped from repos and AUR), yet AT-SPI
itself is a standard D-Bus service that is alive on every accessibility-enabled
desktop. This module talks to it directly over the at-spi bus using ``jeepney``
(a pure-Python, pip-installable D-Bus client), so kwin-mcp's semantic AT-SPI
targeting (get_window_state, perform_action, set_value, semantic clicks) works
on Arch and any other distro without the legacy pyatspi module.

Addressing model
----------------
The desktop is enumerated from the registry root
``org.a11y.atspi.Registry`` at ``/org/a11y/atspi/accessible/root``. Its
children are the *applications*; each is addressed by its own unique D-Bus bus
name (e.g. ``:1.8``) at the root path. Every accessible is therefore addressed
by an opaque ``(bus_name, object_path)`` pair, and we recurse a child subtree
using the child's own bus name. An app's PID is resolved from its unique name
via ``org.freedesktop.DBus.GetConnectionUnixProcessID``, which lets us match an
accessibility tree to a KDE window (which carries a PID).

Only the pieces kwin-mcp needs are implemented: tree walk, role/name, screen
bounds, state flags, available actions, editable detection, invoking an action,
and writing a value. Everything degrades to ``available() == False`` when the
at-spi bus is unreachable.
"""

from __future__ import annotations

import glob
import os
import threading
import time
from typing import Optional

import jeepney
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import DBusAddress, new_method_call

_REGISTRY = "org.a11y.atspi.Registry"
_ROOT = "/org/a11y/atspi/accessible/root"
_ACC = "org.a11y.atspi.Accessible"
_COMP = "org.a11y.atspi.Component"
_ACTION = "org.a11y.atspi.Action"
_EDITABLE = "org.a11y.atspi.EditableText"
_VALUE = "org.a11y.atspi.Value"
_DBUS = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"

# AT-SPI state flags (powers of two, per at-spi2-core/atspi/atspi-constants.h).
_STATE_NAMES = {
    1 << 0: "invalid", 1 << 1: "active", 1 << 2: "armed", 1 << 3: "checked",
    1 << 4: "collapsed", 1 << 5: "defunct", 1 << 6: "editable",
    1 << 7: "enabled", 1 << 8: "expandable", 1 << 9: "expanded",
    1 << 10: "focusable", 1 << 11: "focused", 1 << 12: "has-tooltip",
    1 << 13: "horizontal", 1 << 14: "iconified", 1 << 15: "indeterminate",
    1 << 16: "modal", 1 << 17: "multi-line", 1 << 18: "multiselectable",
    1 << 19: "opaque", 1 << 20: "pressed", 1 << 21: "resizable",
    1 << 22: "selectable", 1 << 23: "selected", 1 << 24: "sensitive",
    1 << 25: "showing", 1 << 26: "single-line", 1 << 27: "stale",
    1 << 28: "transient", 1 << 29: "vertical", 1 << 30: "visible",
    1 << 31: "manages-descendants",
}

_INTERACTIVE_ROLES = {
    "push button", "button", "text", "entry", "text entry", "edit",
    "check box", "check button", "radio button", "combo box", "spin button",
    "slider", "link", "menu item", "list item", "tab", "page tab",
    "toggle button", "icon", "image", "label", "table cell", "scroll bar",
}

_lock = threading.Lock()
_connection = None
_connection_ok = False
# Hard time budget for any single tree walk. Most apps return in well under a
# second; some apps have a pathologically slow AT-SPI bridge (we measured a
# Qt app at ~10s per 20 elements). A deadline keeps get_window_state bounded so
# one bad app can never hang a call.
_WALK_DEADLINE = 6.0
# Timeout for a SINGLE D-Bus method call. A slow app's bridge can stall a call
# for many seconds; bounding each call keeps the whole walk within budget.
_CALL_TIMEOUT = 3.0
# Short TTL for per-app element results. get_window_state is usually followed
# immediately by perform_action / set_value / click on the same window, and a
# slow app should not be re-walked every time. 2s is short enough to avoid
# staleness while making repeated calls on one window cheap.
_CACHE_TTL = 2.0
_result_cache: dict = {}
_result_cache_lock = threading.Lock()


def _atspi_socket_address() -> Optional[str]:
    uid = os.getuid()
    sockets = sorted(glob.glob(f"/run/user/{uid}/at-spi/bus_*"))
    return f"unix:path={sockets[-1]}" if sockets else None


def _get_connection():
    """Return the shared blocking connection to the at-spi bus.

    Guarded by a lock: jeepney's blocking connection is not safe for
    concurrent ``send_and_get_reply``, and MCP tools may run concurrently.
    """
    global _connection, _connection_ok
    if _connection is not None:
        return _connection
    with _lock:
        if _connection is not None:
            return _connection
        addr = _atspi_socket_address()
        if not addr:
            _connection_ok = False
            return None
        old = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        try:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
            _connection = open_dbus_connection(bus="SESSION")
            _connection_ok = True
        except Exception:
            _connection = None
            _connection_ok = False
        finally:
            if old is None:
                os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            else:
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = old
        return _connection


def available() -> bool:
    """True if the at-spi bus is reachable and the registry responds."""
    conn = _get_connection()
    if conn is None:
        return False
    try:
        with _lock:
            conn.send_and_get_reply(_mcall(_ROOT, _ACC, "GetChildren", ""),
                                    timeout=_CALL_TIMEOUT)
        return True
    except Exception:
        return False


def _mcall(path, iface, method, signature, body=(), dest=None):
    return new_method_call(
        DBusAddress(path, bus_name=dest or _REGISTRY, interface=iface),
        method, signature, body)


def _call(path, iface, method, signature, body=(), dest=None):
    conn = _get_connection()
    if conn is None:
        raise RuntimeError("no at-spi D-Bus connection available")
    with _lock:
        reply = conn.send_and_get_reply(
            _mcall(path, iface, method, signature, body, dest), timeout=_CALL_TIMEOUT)
    return reply.body


def _pid_for_name(unique_name: str) -> Optional[int]:
    try:
        return _call(_DBUS_PATH, _DBUS, "GetConnectionUnixProcessID", "s",
                     (unique_name,), dest=_DBUS)[0]
    except Exception:
        return None


def _application_nodes():
    """Yield (bus_name, root_path, pid, app_name) for each running app."""
    try:
        apps = _call(_ROOT, _ACC, "GetChildren", "", ())[0]
    except Exception:
        return
    for bus_name, path in apps:
        pid = _pid_for_name(bus_name)
        name = ""
        try:
            name = _norm_str(_call(path, _ACC, "GetName", "", (), dest=bus_name)[0])
        except Exception:
            pass
        yield bus_name, path, pid, name


def _states(flags) -> list:
    out = []
    for bit, name in _STATE_NAMES.items():
        if flags & bit:
            out.append(name)
    return out


def _interfaces(bus, path) -> list:
    try:
        return _call(path, _ACC, "GetInterfaces", "", (), dest=bus)[0] or []
    except Exception:
        return []


def _node_info(bus, path, role, name):
    """Return (states, actions, editable, bounds) for a node."""
    states: list = []
    actions: list = []
    editable = False
    bounds = (0, 0, 0, 0)
    ifaces = _interfaces(bus, path)
    try:
        flags = _call(path, _ACC, "GetState", "", (), dest=bus)[0]
        if isinstance(flags, (list, tuple)) and flags:
            acc = 0
            for v in flags:
                acc |= int(v)
            states = _states(acc)
    except Exception:
        pass
    if _ACTION in ifaces:
        try:
            n = _norm_int(_call(path, _ACTION, "GetNActions", "", (), dest=bus)[0])
            actions = [
                _call(path, _ACTION, "GetName", "i", (i,), dest=bus)[0] or ""
                for i in range(int(n))
            ]
        except Exception:
            actions = []
    if _EDITABLE in ifaces:
        editable = True
    try:
        ex = _call(path, _COMP, "GetExtents", "u", (0,), dest=bus)[0]
        x, y, w, h = (int(v) for v in ex)
        # Some nodes report implausible sentinel bounds (e.g. INT_MIN). Treat
        # those as "no usable bounds" so click_element reports it cleanly.
        if w <= 0 or h <= 0 or abs(x) > 1_000_000 or abs(y) > 1_000_000:
            x = y = w = h = 0
        bounds = (x, y, w, h)
    except Exception:
        pass
    return states, actions, editable, bounds


def _norm_str(v) -> str:
    """Unwrap an AT-SPI value into a plain string.

    ``GetName`` can come back as a bare string or as a ``('s', name)`` tuple;
    normalize both so downstream compares are clean. Strings that are actually
    D-Bus error messages (some frameworks reply with the error text as a plain
    string when a method is unsupported) are collapsed to empty.
    """
    def clean(s):
        low = s.lower()
        if ("doesn't exist" in low or "no such method" in low
                or low.startswith('method "')):
            return ""
        return s
    if isinstance(v, (tuple, list)):
        # AT-SPI returns ('s', realname): the leading 's' is a type tag, the
        # real value is the LAST string part. Prefer the last non-empty string.
        picked = ""
        for part in v:
            if isinstance(part, str) and part:
                picked = part
        return clean(picked)
    return clean(str(v or ""))


def _norm_int(v) -> int:
    """Unwrap an AT-SPI integer reply into a plain int.

    ``GetNActions`` (and other int-returning methods) can come back as a bare
    int, ``(int,)``, or ``(('i', int),)``. Return the underlying int, or 0 if
    none can be extracted.
    """
    if isinstance(v, (tuple, list)):
        for part in reversed(v):
            if isinstance(part, (int,)) and not isinstance(part, bool):
                return int(part)
            if isinstance(part, (tuple, list)) and len(part) == 2:
                # ('i', 2) type-tag form
                if isinstance(part[1], (int,)) and not isinstance(part[1], bool):
                    return int(part[1])
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _walk(bus, path, max_elements, only_interactive,
          out, idx, depth=0, seen=None, deadline=0.0):
    """Depth-first walk of a subtree, appending (dict, handle) to out.

    ``idx`` is carried in a list so it mutates across recursion; ``seen`` is
    keyed by (bus, path) because every node in one app shares the same bus name.
    ``deadline`` (monotonic time) bounds the whole walk; when hit, the walk
    stops and returns whatever it has collected so far.
    """
    if seen is None:
        seen = set()
    if (bus, path) in seen or idx[0] >= max_elements:
        return idx
    if deadline and time.monotonic() > deadline:
        return idx
    seen.add((bus, path))
    try:
        role = _norm_str(_call(path, _ACC, "GetRoleName", "", (), dest=bus)[0])
    except Exception:
        role = ""
    interactive = (not only_interactive) or role.lower() in _INTERACTIVE_ROLES
    # Only fetch the expensive per-node detail (name, interfaces, state,
    # bounds, actions) for nodes we will actually include; non-interactive
    # scaffolding nodes only need their role so we can descend.
    name = ""
    states = []
    actions = []
    editable = False
    bounds = (0, 0, 0, 0)
    if interactive:
        try:
            name = _norm_str(_call(path, _ACC, "GetName", "", (), dest=bus)[0])
        except Exception:
            name = ""
        states, actions, editable, bounds = _node_info(bus, path, role, name)
    if not only_interactive or interactive:
        el = {
            "index": idx[0], "role": role.lower(), "name": name,
            "x": bounds[0], "y": bounds[1], "width": bounds[2], "height": bounds[3],
            "states": states, "actions": actions, "editable": editable,
            "handle": (bus, path),
        }
        out.append(el)
        idx[0] += 1
    if idx[0] >= max_elements:
        return idx
    try:
        children = _call(path, _ACC, "GetChildren", "", (), dest=bus)[0]
    except Exception:
        children = []
    if not isinstance(children, (list, tuple)):
        return idx
    for item in children:
        if idx[0] >= max_elements:
            break
        if deadline and time.monotonic() > deadline:
            break
        # Guard against a malformed reply (a flat list instead of (bus, path)).
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        _walk(item[0], item[1], max_elements, only_interactive, out, idx,
              depth=depth + 1, seen=seen, deadline=deadline)
    return idx


def _walk_app(pid: int, max_elements: int = 500,
              only_interactive: bool = True) -> list:
    """Walk an app's AT-SPI subtree and return element dicts for a PID.

    Internal: used by the cached ``elements_for_window``.
    """
    out: list = []
    deadline = time.monotonic() + _WALK_DEADLINE
    for bus_name, root_path, app_pid, app_name in _application_nodes():
        if app_pid == pid:
            _walk(bus_name, root_path, max_elements, only_interactive, out,
                  [0], deadline=deadline)
            break
    return out


def elements_for_window(pid: int, max_elements: int = 500,
                        only_interactive: bool = True) -> list:
    """Return interactively-filtered elements for an app by PID.

    Each element is a dict with index, role, name, bounds, states, actions,
    editable, plus an internal ``handle`` (bus, path) used for acting on it.
    Results are cached briefly per (pid, max_elements, filter) so a slow app is
    not re-walked on every call; the returned lists are read-only (callers must
    not mutate them or the ``handle`` field).
    """
    key = (pid, max_elements, only_interactive)
    now = time.monotonic()
    with _result_cache_lock:
        hit = _result_cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]
    out = _walk_app(pid, max_elements, only_interactive)
    with _result_cache_lock:
        # Stamp at completion, not start: a slow walk must not be instantly stale.
        _result_cache[key] = (time.monotonic(), out)
    return out


def perform_action(handle, action: str = "") -> tuple:
    bus, path = handle
    try:
        n = _norm_int(_call(path, _ACTION, "GetNActions", "", (), dest=bus)[0])
        if int(n) == 0:
            return False, "element exposes no actions"
        idx = 0
        if action:
            idx = -1
            for i in range(int(n)):
                nm = _call(path, _ACTION, "GetName", "i", (i,), dest=bus)[0] or ""
                if _norm_str(nm).strip().lower() == action.lower():
                    idx = i
                    break
            if idx == -1:
                names = [_call(path, _ACTION, "GetName", "i", (i,), dest=bus)[0]
                         for i in range(int(n))]
                return False, f"no action {action!r}; available: {names}"
        ok = bool(_call(path, _ACTION, "DoAction", "i", (idx,), dest=bus)[0])
        return ok, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def grab_focus(handle) -> tuple:
    """Move keyboard focus to a node via the AT-SPI Component.GrabFocus call.

    Returns (ok, detail). This is the keyboard-first navigation primitive: it
    moves focus to a specific element without guessing Tab counts, so a host
    can target an element from the a11y tree and act on it with Enter/Space.
    """
    bus, path = handle
    ifaces = _interfaces(bus, path)
    if _COMP not in ifaces:
        return False, "element exposes no Component interface (cannot grab focus)"
    try:
        _call(path, _COMP, "GrabFocus", "", (), dest=bus)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def set_value(handle, value: str) -> tuple:
    bus, path = handle
    ifaces = _interfaces(bus, path)
    if _EDITABLE in ifaces:
        try:
            ok = bool(_call(path, _EDITABLE, "SetTextContents", "s",
                            (str(value),), dest=bus)[0])
            return ok, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
    if _VALUE in ifaces:
        try:
            ok = bool(_call(path, _VALUE, "SetCurrentValue", "d",
                            (float(value),), dest=bus)[0])
            return ok, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
    return False, "element is not settable"