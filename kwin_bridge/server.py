#!/usr/bin/env python3
"""MCP server module for kwin-mcp (installed as kwin_bridge.server).

Exposes the same operations cua-driver provides on X11, but built on
KDE-native primitives so they work on Wayland. See the package README for the
full tool list and architecture. This module is imported both by the installed
console script and by the repo-root ``server.py`` shim.

Since v0.2 this server also carries an MCP safety contract (ToolAnnotations on
every tool, so hosts can warn before mutating tools), a structured ``doctor``
readiness report, and semantic AT-SPI targeting (``perform_action``,
``set_value`` and role/name/text clicks).
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import sys
import traceback
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations as _TA

# Keep FastMCP's INFO chatter off stderr so logs stay clean alongside the
# MCP stdio/HTTP framing; WARNING+ only.
logging.getLogger("mcp").setLevel(logging.WARNING)

from . import windows, screenshot, input as input_mod, a11y, _env, doctor  # noqa: F401
from . import history  # noqa: F401  # encrypted, metadata-only Computer History


def _ann(*, read_only: bool = False, destructive: bool = False,
         world: bool = False, idempotent: bool = False,
         title: str | None = None) -> _TA:
    """Build an MCP ToolAnnotations for the given tool classification."""
    return _TA(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=world,
    )


# Classification helpers: keep the decorators readable.
RO = {"read_only": True}                      # observation
MUT = {"read_only": False, "destructive": False}   # UI-state mutators
ACT = {"read_only": False, "destructive": True, "world": True}  # arbitrary app action

mcp = FastMCP("kwin-mcp")


def _win_to_dict(w) -> dict:
    return {
        "window_id": w.window_id,
        "title": w.title,
        "app_name": w.app_name,
        "pid": w.pid,
        "x": w.x,
        "y": w.y,
        "width": w.width,
        "height": w.height,
        "off_screen": w.off_screen,
        "visible": w.visible,
    }


@mcp.tool(title="List all windows", annotations=_ann(**RO))
def list_windows_tool() -> dict:
    """List every top-level window on the desktop.

    Unlike X11-only tools, this includes native Wayland windows. Each entry
    has window_id (a KDE UUID like {xxxx-...}), title, app_name, pid and
    geometry (x, y, width, height). Use window_id with the other tools.
    """
    try:
        wins = windows.list_windows()
        return {
            "count": len(wins),
            "windows": [_win_to_dict(w) for w in wins],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "trace": traceback.format_exc()}


@mcp.tool(title="Active window", annotations=_ann(**RO))
def active_window_tool() -> dict:
    """Return the currently focused window, or an empty result if none."""
    try:
        w = windows.active_window()
        if w is None:
            return {"window": None}
        return {"window": _win_to_dict(w)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool(title="Capture screenshot", annotations=_ann(**MUT))
def capture(mode: str = "desktop", window_id: str = "", output_path: str = "") -> dict:
    """Capture a screenshot.

    mode: "desktop" (whole screen) or "window" (a specific window).
    window_id: required when mode="window" (a KDE window UUID).
    output_path: optional absolute path to save the PNG; a temp file is used
                 if omitted. Returns the path plus width/height.
    """
    try:
        if mode == "window":
            if not window_id:
                return {"error": "window_id is required when mode='window'"}
            path = screenshot.capture_window(window_id, output_path or None)
        else:
            path = screenshot.capture_full(output_path or None)
        from PIL import Image  # only for size reporting
        with Image.open(path) as im:
            w, h = im.size
        return {"path": path, "width": w, "height": h, "mode": mode}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "trace": traceback.format_exc()}


@history.record("click", route="synthetic_events")
@mcp.tool(title="Click", annotations=_ann(**ACT))
def click(x: int = 0, y: int = 0, window_id: str = "", button: str = "left",
          double: bool = False, element_index: int = -1,
          role: str = "", name: str = "", text: str = "") -> dict:
    """Click at a point, or target an element semantically.

    Coordinate mode: pass screen (x, y), OR set window_id and give
    window-local (x, y). button: left|right|middle.

    Element mode (preferred when the app exposes AT-SPI): pass element_index
    from get_window_state, or any of role / name / text as case-insensitive
    substrings to click the first matching element. If you give both, element
    targeting wins.
    """
    try:
        if element_index >= 0:
            return a11y.click_element(window_id, element_index, button=button,
                                      double=double)
        if role or name or text:
            return a11y.click_semantic(window_id, role=role, name=name, text=text,
                                       button=button, double=double)
        if window_id:
            input_mod.click_window(window_id, x, y, button=button, double=double)
        else:
            input_mod.click(x, y, button=button, double=double)
        return {"ok": True, "x": x, "y": y, "window_id": window_id or None,
                "button": button, "double": double}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("drag", route="synthetic_events")
@mcp.tool(title="Drag", annotations=_ann(**ACT))
def drag(from_x: int = 0, from_y: int = 0, to_x: int = 0, to_y: int = 0,
         window_id: str = "", button: str = "left", steps: int = 20) -> dict:
    """Drag from one point to another.

    Coordinates are screen-space, or window-local when window_id is given.
    """
    try:
        if window_id:
            input_mod.drag_window(window_id, from_x, from_y, to_x, to_y,
                                  button=button, steps=steps)
        else:
            input_mod.drag(from_x, from_y, to_x, to_y, button=button, steps=steps)
        return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y],
                "window_id": window_id or None}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("type_text", route="trusted_input")
@mcp.tool(title="Type text", annotations=_ann(**ACT))
def type_text(text: str) -> dict:
    """Type a string into whatever window is currently focused.

    Returns honest stats: ``typed`` (chars emitted), ``dropped`` (chars skipped
    because they are not representable in the US-layout key map), and
    ``dropped_chars``. If ``dropped`` > 0 the text was NOT fully entered; prefer
    the ``paste`` tool for long or non-ASCII text.
    """
    try:
        return input_mod.type_text(text)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("paste", route="trusted_input")
@mcp.tool(title="Paste text (clipboard)", annotations=_ann(**ACT))
def paste(text: str) -> dict:
    """Paste text into the focused widget via the Wayland clipboard + Ctrl+V.

    Reliable and fast for long or non-ASCII text: sets the clipboard with
    wl-copy then sends Ctrl+V, so unsupported characters are preserved verbatim
    and a single keystroke replaces char-by-char typing. Prefer this over
    type_text for anything longer than a few chars.
    """
    try:
        return input_mod.paste(text)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("press_key", route="global_input")
@mcp.tool(title="Press key", annotations=_ann(**ACT))
def press_key(key: str, modifiers: list = None) -> dict:
    """Press a single key, optionally with held modifiers.

    key example: "enter", "tab", "a", "f". modifiers: list like ["ctrl"].
    """
    try:
        input_mod.press_key(key, modifiers=modifiers or [])
        return {"ok": True, "key": key, "modifiers": modifiers or []}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("scroll", route="synthetic_events")
@mcp.tool(title="Scroll", annotations=_ann(**MUT))
def scroll(direction: str = "down", amount: int = 3) -> dict:
    """Scroll the mouse wheel. direction: 'up' or 'down'."""
    try:
        input_mod.scroll(direction=direction, amount=amount)
        return {"ok": True, "direction": direction, "amount": amount}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool(title="Get window state (AT-SPI)", annotations=_ann(**RO))
def get_window_state(window_id: str, max_elements: int = 100) -> dict:
    """Return the AT-SPI accessibility tree for a window (when available).

    Each element has an index, role, name, screen-space bounds (x, y, width,
    height), center, state flags (focused, checked, enabled...), the AT-SPI
    actions it exposes, and an editable flag. Use element_index with click,
    perform_action or set_value, or the role/name/text semantic selectors.
    Returns available=False if pyatspi is missing.
    """
    try:
        return a11y.get_window_state(window_id, max_elements=max_elements)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("click_element", route="accessibility")
@mcp.tool(title="Click element (by index)", annotations=_ann(**ACT))
def click_element(window_id: str, element_index: int, button: str = "left",
                  double: bool = False) -> dict:
    """Click an AT-SPI element (by index from get_window_state) in a window."""
    try:
        return a11y.click_element(window_id, element_index, button=button,
                                  double=double)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("perform_action", route="accessibility")
@mcp.tool(title="Perform AT-SPI action", annotations=_ann(**ACT))
def perform_action(window_id: str, element_index: int, action: str = "") -> dict:
    """Invoke an AT-SPI action on an element.

    Element indices come from get_window_state. action is optional: when empty
    the element's primary action is used. Examples: "press", "activate",
    "toggle". Use click_element first to point at the element if you only want
    a plain click; this tool prefers the semantic action when one exists.
    """
    try:
        return a11y.perform_action(window_id, element_index, action=action)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("set_value", route="accessibility")
@mcp.tool(title="Set value on element", annotations=_ann(**ACT))
def set_value(window_id: str, element_index: int, value: str) -> dict:
    """Write a value to a settable AT-SPI element.

    Works for text fields, sliders, spinners and other controls that expose
    EditableText. Elements come from get_window_state; the editable flag and
    states mark which are settable.
    """
    try:
        return a11y.set_value(window_id, element_index, value)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("focus_element", route="accessibility")
@mcp.tool(title="Focus element (keyboard-first)", annotations=_ann(**MUT))
def focus_element(window_id: str, element_index: int) -> dict:
    """Move keyboard focus to an AT-SPI element without clicking.

    Uses the AT-SPI Component.GrabFocus action, so focus jumps straight to the
    element (no Tab-count guessing, no cursor movement). This is the keyboard-
    first navigation primitive: focus the element, then activate it with
    press_key('enter') / perform_action / keyboard_navigate.
    """
    try:
        return a11y.focus_element(window_id, element_index)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool(title="Focused element", annotations=_ann(**RO))
def focused_element(window_id: str) -> dict:
    """Report which AT-SPI element currently has keyboard focus.

    Returns the element index, role and name that currently owns focus, or
    element=null if none does. Lets a host know its position in the tab order
    before navigating with keyboard_navigate.
    """
    try:
        return a11y.focused_element(window_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool(title="Keyboard navigate", annotations=_ann(**MUT))
def keyboard_navigate(window_id: str, direction: str = "next", steps: int = 1) -> dict:
    """Move keyboard focus to the next/prev focusable element and report it.

    direction: 'next' (Tab) or 'prev' (Shift+Tab); steps = how many positions
    to move. Returns the 'from' and 'to' elements so a host can reason without
    pixels. Combine with focus_element, press_key and get_window_state for a
    fully keyboard-driven flow.
    """
    try:
        return a11y.keyboard_navigate(window_id, direction=direction, steps=steps)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("activate", route="system_api")
@mcp.tool(title="Raise and focus window", annotations=_ann(**MUT))
def activate(window_id: str) -> dict:
    """Raise and focus a window (switching virtual desktop if needed)."""
    try:
        windows.activate(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("raise_window", route="system_api")
@mcp.tool(title="Raise window", annotations=_ann(**MUT))
def raise_window(window_id: str) -> dict:
    """Raise a window to the top of the stacking order."""
    try:
        windows.raise_window(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("minimize", route="system_api")
@mcp.tool(title="Minimize window", annotations=_ann(**MUT))
def minimize(window_id: str) -> dict:
    """Minimize a window."""
    try:
        windows.minimize(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@history.record("close_window", route="system_api")
@mcp.tool(title="Close window", annotations=_ann(**ACT))
def close_window(window_id: str) -> dict:
    """Close a window."""
    try:
        windows.close(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool(title="Cursor position", annotations=_ann(**RO))
def get_cursor_position() -> dict:
    """Return the current mouse cursor position (x, y, screen)."""
    try:
        return input_mod.get_cursor_position()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool(title="Environment health", annotations=_ann(**RO))
def health() -> dict:
    """Report environment and dependency status for diagnostics."""
    import shutil
    status = {
        "kdotool": shutil.which("kdotool") is not None,
        "spectacle": shutil.which("spectacle") is not None,
        "ydotool": shutil.which("ydotool") is not None,
        "grim": shutil.which("grim") is not None,
        "uinput_writable": os.access("/dev/uinput", os.W_OK),
        "in_input_group": "input" in _groups(),
        "pyatspi_available": a11y._atspi_available(),
        "display_server": _display_server(),
    }
    return status


# ── Computer History (port of Cua Driver's encrypted metadata-only history) ──
def _app_from_window_id(_result: dict, kwargs: dict) -> Optional[str]:
    """Best-effort non-sensitive application display name from a window_id.

    Never returns a window title or path, only the KDE window class. Returns
    None on miss so the event stays metadata-only.
    """
    wid = kwargs.get("window_id")
    if not wid:
        return None
    try:
        w = windows.get_window(wid)
        return w.app_name or None
    except Exception:  # noqa: BLE001
        return None


@mcp.tool(title="Computer History status", annotations=_ann(read_only=True))
def history_status() -> dict:
    """Inspect Computer History (the opt-in, encrypted, metadata-only record of
    kwin-mcp actions). Read-only. Reports supported, enabled, paused, encrypted,
    retention/quota, bytes used, dropped events, and health. Never returns events.

    History is OFF by default. To record, the local user enables it via the
    `history_control` tool from this server process (agents cannot enable it).
    """
    return history.status()


@mcp.tool(title="Computer History query", annotations=_ann(read_only=True))
def history_query(limit: int = 50, session_id: str = "",
                  since_sequence: int = 0, until_sequence: int = 0) -> dict:
    """Return a bounded, metadata-only slice of Computer History events.

    Read-only and permission-gated to the kwin_runtime agent. Never includes
    screenshots, typed text, clipboard, tool arguments, tool results, a11y
    trees, window titles, or paths. A successful non-empty query appends an
    encrypted access record (not returned). A returned event is metadata
    evidence, not a transcript.

    limit: 1..200 (default 50). session_id: opaque id from history. since_sequence
    / until_sequence: inclusive sequence bounds (0 = unbounded). Events are
    ordered by sequence and the newest `limit` are returned in ascending order.
    """
    try:
        return history.query(
            limit=limit,
            session_id=session_id or None,
            since_sequence=since_sequence or None,
            until_sequence=until_sequence or None,
        )
    except history.HistoryError as exc:
        return {"error": exc.category, "metadata_only": True,
                "model_context_disclosure": True}


@mcp.tool(title="Computer History control (local)", annotations=_ann(read_only=True))
def history_control(operation: str = "status") -> dict:
    """Local, server-side control of Computer History capture lifecycle.

    This mirrors Cua's `history_control_requires_local_cli`: capture, retention,
    and deletion are owned by the local user on this machine, NOT by a remote
    agent. It is exposed as an MCP tool for convenience (the server process is
    trusted local), but an agent calling it only flips the local daemon's own
    state; it cannot read the encryption key or the encrypted chunks.

    operation: enable | disable | pause | resume | flush | delete.
      enable  - create the encrypted store + in-memory key, start the writer.
      disable - stop recording new actions (encrypted history is preserved).
      pause   - pause new-action capture (existing history stays queryable).
      resume  - resume capture.
      flush   - drop buffered-but-unwritten events (keep the encrypted store).
      delete  - cryptographic deletion: destroy the key and erase the store.

    Default root: $XDG_STATE_HOME/kwin-mcp/computer-history.
    """
    op = operation.strip().lower()
    if op not in ("enable", "disable", "pause", "resume", "flush", "delete", "status"):
        return {"ok": False, "error": f"unknown operation {operation!r}"}
    if op == "status":
        return history.status()
    try:
        return history.control(op)
    except history.HistoryError as exc:
        return {"ok": False, "error": exc.category}


@mcp.tool(title="Readiness report (doctor)", annotations=_ann(**RO))
def doctor_tool() -> dict:
    """Return a structured JSON readiness report.

    Covers platform, windowing (can we list windows now?), input (/dev/uinput),
    accessibility/AT-SPI, the screenshot path, XDG portal availability, and a
    readiness summary with explicit blockers and a recommended next step. Same
    shape as the `--doctor` CLI flag.
    """
    try:
        return doctor.run_doctor()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "trace": traceback.format_exc()}


def _groups():
    try:
        gids = os.getgroups()
        try:
            gids.append(os.getgid())
        except Exception:
            pass
        names = set()
        for gid in gids:
            try:
                names.add(grp.getgrgid(gid).gr_name)
            except Exception:
                pass
        return sorted(names)
    except Exception:
        return []


def _display_server():
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def _run_server(args) -> None:
    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description="kwin-mcp server")
    parser.add_argument("--http", type=int, default=0,
                        help="Serve over Streamable HTTP on this port instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host for --http (default 127.0.0.1)")
    parser.add_argument("--check", action="store_true",
                        help="Run a dependency preflight and exit")
    parser.add_argument("--doctor", action="store_true",
                        help="Print the JSON readiness report and exit")
    args = parser.parse_args()

    if args.check:
        from .preflight import main as preflight_main
        sys.exit(preflight_main())

    if args.doctor:
        import json as _json
        print(_json.dumps(doctor.run_doctor(), indent=2))
        sys.exit(0)

    try:
        _run_server(args)
    except KeyboardInterrupt:
        sys.exit(0)
    except BrokenPipeError:
        # A client disconnected before we finished writing; this is a normal
        # stdio-shutdown condition, not an error the user needs to see.
        sys.exit(0)
    except BaseExceptionGroup as group:  # noqa: BLE001
        # FastMCP surfaces a broken pipe as an unhandled task-group error on
        # shutdown when the client closes the pipe. Treat it as a clean exit.
        if any(isinstance(e, (BrokenPipeError, OSError))
               and getattr(e, "errno", None) == 32
               for e in group.exceptions):
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
