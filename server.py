#!/usr/bin/env python3
"""
kwin-mcp: an MCP server that controls native Wayland windows on KDE Plasma.

It exposes the same kinds of operations cua-driver provides on X11, but built
on KDE-native primitives so they work on Wayland:

  * list_windows        - enumerate ALL top-level windows (incl. native Wayland)
  * active_window       - the currently focused window
  * capture             - screenshot desktop (mode=desktop) or a window
                          (mode=window, window_id=...); with mode=som it also
                          returns AT-SPI element overlays when available
  * click               - click at screen (x, y) or window-local (window_id,x,y)
  * double_click        - same, but a double click
  * drag                - drag between two screen or window-local points
  * type                - type text into the focused target
  * press_key           - press a key, optionally with modifiers
  * scroll              - scroll the wheel
  * get_window_state    - AT-SPI element list for a window (when available)
  * activate/raise/close/minimize - window management
  * get_cursor_position - current pointer location
  * health              - environment / dependency status

Run it:
    python server.py            # stdio MCP server (for Claude/Codex/Hermes)
    python server.py --http 8080   # streamable HTTP transport

It depends only on the system tools kdotool, spectacle and (for input) a
writable /dev/uinput. See README.md for the full dependency list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from mcp.server.fastmcp import FastMCP

from kwin_bridge import windows, screenshot, input as input_mod, a11y

mcp = FastMCP("kwin-mcp")


def _win_to_dict(w: "windows.Window") -> dict:  # noqa: F821
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


@mcp.tool()
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


@mcp.tool()
def active_window_tool() -> dict:
    """Return the currently focused window, or an empty result if none."""
    try:
        w = windows.active_window()
        if w is None:
            return {"window": None}
        return {"window": _win_to_dict(w)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
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


@mcp.tool()
def click(x: int = 0, y: int = 0, window_id: str = "", button: str = "left",
          double: bool = False) -> dict:
    """Click at a point.

    Provide screen coordinates (x, y). OR set window_id and give window-local
    (x, y); the window is focused first and the click is issued at
    window.x + x, window.y + y. button: left|right|middle.
    """
    try:
        if window_id:
            input_mod.click_window(window_id, x, y, button=button, double=double)
        else:
            input_mod.click(x, y, button=button, double=double)
        return {"ok": True, "x": x, "y": y, "window_id": window_id or None,
                "button": button, "double": double}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
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


@mcp.tool()
def type_text(text: str) -> dict:
    """Type a string into whatever window is currently focused."""
    try:
        input_mod.type_text(text)
        return {"ok": True, "length": len(text)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def press_key(key: str, modifiers: list = None) -> dict:
    """Press a single key, optionally with held modifiers.

    key example: "enter", "tab", "a", "f". modifiers: list like ["ctrl"].
    """
    try:
        input_mod.press_key(key, modifiers=modifiers or [])
        return {"ok": True, "key": key, "modifiers": modifiers or []}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def scroll(direction: str = "down", amount: int = 3) -> dict:
    """Scroll the mouse wheel. direction: 'up' or 'down'."""
    try:
        input_mod.scroll(direction=direction, amount=amount)
        return {"ok": True, "direction": direction, "amount": amount}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def get_window_state(window_id: str, max_elements: int = 100) -> dict:
    """Return the AT-SPI accessibility tree for a window (when available).

    Each element has an index, role, name and screen-space bounds (x, y,
    width, height). Use these indices with capture(mode='som') overlays or
    click_element. Returns available=False if pyatspi is missing.
    """
    try:
        return a11y.get_window_state(window_id, max_elements=max_elements)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def click_element(window_id: str, element_index: int, button: str = "left",
                  double: bool = False) -> dict:
    """Click an AT-SPI element (by index from get_window_state) in a window."""
    try:
        return a11y.click_element(window_id, element_index, button=button,
                                  double=double)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def activate(window_id: str) -> dict:
    """Raise and focus a window (switching virtual desktop if needed)."""
    try:
        windows.activate(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def raise_window(window_id: str) -> dict:
    """Raise a window to the top of the stacking order."""
    try:
        windows.raise_window(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def minimize(window_id: str) -> dict:
    """Minimize a window."""
    try:
        windows.minimize(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def close_window(window_id: str) -> dict:
    """Close a window."""
    try:
        windows.close(window_id)
        return {"ok": True, "window_id": window_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def get_cursor_position() -> dict:
    """Return the current mouse cursor position (x, y, screen)."""
    try:
        return input_mod.get_cursor_position()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
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


def _groups():
    import grp
    import os
    try:
        # Map the process's supplementary+primary group gids to names.
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
    import os
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="kwin-mcp server")
    parser.add_argument("--http", type=int, default=0,
                        help="Serve over Streamable HTTP on this port instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host for --http (default 127.0.0.1)")
    args = parser.parse_args()

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
