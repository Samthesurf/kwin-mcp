"""
Accessibility tree (AT-SPI) introspection for KDE Plasma on Wayland.

Many GTK/Qt/KDE applications expose an AT-SPI accessibility tree even on
Wayland, where the old X11 `get_window_state` trick does not work. This
module walks the desktop accessibility tree and returns a structured list of
interactive elements (with their on-screen bounds) so a caller can target
"click element 14" and translate that to a screen coordinate.

This module is OPTIONAL: it imports pyatspi lazily and degrades gracefully
when pyatspi or an accessibility bus is unavailable. It is used by the
`get_window_state` / `capture(mode='som')` tools when available.

Note: AT-SPI element bounds are in screen coordinates, so they feed directly
into the input module's absolute click coordinates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# Roles we consider "interactive" for SOM overlays.
_INTERACTIVE_ROLES = {
    "push button", "button", "text", "entry", "text entry", "edit",
    "check box", "check button", "radio button", "combo box", "spin button",
    "slider", "link", "menu item", "list item", "tab", "page tab",
    "toggle button", "icon", "image", "label", "table cell", "scroll bar",
}


@dataclass
class A11yElement:
    index: int
    role: str
    name: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    raw: dict = field(default_factory=dict)


def _atspi_available() -> bool:
    try:
        import pyatspi  # noqa: F401
        return True
    except Exception:
        return False


def get_window_state(window_id: str, max_elements: int = 100,
                     only_interactive: bool = True) -> dict:
    """Return the AT-SPI tree for the app owning a KDE window UUID.

    Returns a dict with 'available' (bool), 'elements' (list of A11yElement),
    and 'error' when unavailable. When 'available' is False the caller should
    fall back to coordinate-based clicks.
    """
    if not _atspi_available():
        return {
            "available": False,
            "elements": [],
            "error": "pyatspi not installed or accessibility bus unavailable",
            "window_id": window_id,
        }
    import pyatspi

    from .windows import get_window, is_uuid
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    win = get_window(window_id)

    registry = pyatspi.Registry
    desktop = registry.getDesktop(0)
    elements: list[A11yElement] = []
    idx = 0

    def walk(node, depth=0):
        nonlocal idx
        if node is None or idx >= max_elements:
            return
        try:
            role = str(node.getRoleName()).strip().lower()
            name = str(node.name or "").strip()
            try:
                comp = node.queryComponent()
                ext = comp.getExtents(pyatspi.DESKTOP_coords)
                x, y, w, h = int(ext.x), int(ext.y), int(ext.width), int(ext.height)
            except Exception:
                x = y = w = h = 0
        except Exception:
            return
        if not only_interactive or role in _INTERACTIVE_ROLES:
            elements.append(A11yElement(
                index=idx, role=role, name=name,
                x=x, y=y, width=w, height=h,
                raw={"role": role, "name": name},
            ))
            idx += 1
        if idx >= max_elements:
            return
        try:
            for i in range(node.childCount):
                walk(node.getChildAtIndex(i), depth + 1)
        except Exception:
            pass

    # Find the application node matching the window's PID/app name.
    target = None
    for d in range(desktop.childCount):
        app = desktop.getChildAtIndex(d)
        try:
            if app.name and (win.app_name and win.app_name.lower() in app.name.lower()
                             or (win.pid and app.getApplication().get_process_id() == win.pid)):
                target = app
                break
        except Exception:
            continue
    if target is None and desktop.childCount:
        target = desktop.getChildAtIndex(0)

    if target is not None:
        walk(target)

    return {
        "available": True,
        "window_id": window_id,
        "window_title": win.title,
        "elements": elements,
        "count": len(elements),
    }


def click_element(window_id: str, element_index: int, max_elements: int = 100,
                  button: str = "left", double: bool = False) -> dict:
    """Resolve an AT-SPI element index to its center and click it."""
    state = get_window_state(window_id, max_elements=max_elements,
                             only_interactive=False)
    if not state.get("available"):
        return {"ok": False, "error": state.get("error")}
    el = next((e for e in state["elements"] if e.index == element_index), None)
    if el is None:
        return {"ok": False, "error": f"element {element_index} not found"}
    if el.width == 0 or el.height == 0:
        return {"ok": False, "error": f"element {element_index} has no bounds"}
    from .input import click_window
    from .windows import get_window
    # click_window expects window-local coordinates, so subtract the window origin.
    win = get_window(window_id)
    local_x = (el.x + el.width // 2) - win.x
    local_y = (el.y + el.height // 2) - win.y
    click_window(window_id, local_x, local_y, button=button, double=double)
    return {"ok": True, "element": el.index,
            "center_screen": [el.x + el.width // 2, el.y + el.height // 2]}
