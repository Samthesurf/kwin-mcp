"""
Accessibility tree (AT-SPI) introspection for KDE Plasma on Wayland.

Many GTK/Qt/KDE applications expose an AT-SPI accessibility tree even on
Wayland, where the old X11 `get_window_state` trick does not work. This
module walks the desktop accessibility tree and returns a structured list of
interactive elements (with their on-screen bounds, state flags and the actions
they expose) so a caller can target them three ways:

  * by index        (``click_element``, ``perform_action``, ``set_value``)
  * semantically     (``resolve_elements`` with role / name / text filters)
  * by coordinates   (fall back to the input module's absolute clicks)

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
    states: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    editable: bool = False
    raw: dict = field(default_factory=dict)


def _atspi_available() -> bool:
    try:
        import pyatspi  # noqa: F401
        return True
    except Exception:
        return False


def _el_to_dict(el: A11yElement) -> dict:
    return {
        "index": el.index,
        "role": el.role,
        "name": el.name,
        "x": el.x,
        "y": el.y,
        "width": el.width,
        "height": el.height,
        "center_x": el.x + el.width // 2,
        "center_y": el.y + el.height // 2,
        "states": el.states,
        "actions": el.actions,
        "editable": el.editable,
    }


def _collect_nodes(window_id: str, max_elements: int = 500,
                   only_interactive: bool = True):
    """Walk the AT-SPI tree for the app owning ``window_id``.

    Returns (elements: list[(A11yElement, node)], window) where ``node`` is
    the live pyatspi object for action/value stateful operations. Raises
    ValueError for a bad UUID; returns only_interactive filtering.
    """
    if not _atspi_available():
        raise RuntimeError(
            "pyatspi not installed or accessibility bus unavailable"
        )
    import pyatspi

    from .windows import get_window, is_uuid
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    win = get_window(window_id)

    registry = pyatspi.Registry
    desktop = registry.getDesktop(0)
    pairs: list = []
    idx = 0

    def _state_flags(node, role) -> tuple[list, bool, list]:
        states: list = []
        actions: list = []
        editable = False
        try:
            st = node.getState()
            for flag, label in (
                (pyatspi.STATE_CHECKED, "checked"),
                (pyatspi.STATE_FOCUSED, "focused"),
                (pyatspi.STATE_SELECTED, "selected"),
                (pyatspi.STATE_SHOWING, "showing"),
                (pyatspi.STATE_VISIBLE, "visible"),
                (pyatspi.STATE_ENABLED, "enabled"),
                (pyatspi.STATE_SENSITIVE, "sensitive"),
                (pyatspi.STATE_EDITABLE, "editable"),
            ):
                try:
                    if st.contains(flag):
                        states.append(label)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            atn = node.queryAction()
            n = int(atn.nActions)
            actions = [str(atn.getName(i) or "") for i in range(n)]
        except Exception:
            actions = []
        try:
            node.queryEditableText()
            editable = True
        except Exception:
            editable = False
        return states, editable, actions

    def walk(node, depth=0):
        nonlocal idx
        if node is None or idx >= max_elements:
            return
        try:
            role = str(node.getRoleName()).strip().lower()
            name = str(node.name or "").strip()
            x = y = w = h = 0
            try:
                comp = node.queryComponent()
                ext = comp.getExtents(pyatspi.DESKTOP_coords)
                x, y, w, h = int(ext.x), int(ext.y), int(ext.width), int(ext.height)
            except Exception:
                pass
            states, editable, actions = _state_flags(node, role)
        except Exception:
            return
        if not only_interactive or role in _INTERACTIVE_ROLES:
            el = A11yElement(
                index=idx, role=role, name=name,
                x=x, y=y, width=w, height=h,
                states=states, actions=actions, editable=editable,
                raw={"role": role, "name": name},
            )
            pairs.append((el, node))
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
    return pairs, win


def get_window_state(window_id: str, max_elements: int = 100,
                     only_interactive: bool = True) -> dict:
    """Return the AT-SPI tree for the app owning a KDE window UUID.

    Returns a dict with 'available' (bool), 'elements' (list of dicts with
    index, role, name, bounds, state flags, actions, editable) and 'error'
    when unavailable. When 'available' is False the caller should fall back to
    coordinate-based clicks.
    """
    try:
        pairs, win = _collect_nodes(window_id, max_elements=max_elements,
                                    only_interactive=only_interactive)
    except (RuntimeError, ValueError) as exc:
        return {
            "available": False,
            "elements": [],
            "error": str(exc),
            "window_id": window_id,
        }
    return {
        "available": True,
        "window_id": window_id,
        "window_title": win.title,
        "elements": [_el_to_dict(el) for (el, _n) in pairs],
        "count": len(pairs),
    }


def resolve_elements(window_id: str, *, role: str = "", name: str = "",
                     text: str = "", max_elements: int = 500,
                     only_interactive: bool = True) -> list[A11yElement]:
    """Semantic search: return elements matching role / name / text filters.

    All filters are case-insensitive substrings. ``text`` additionally matches
    an element's name (buttons and text nodes expose their label as the name).
    Returns a flat list ordered by depth-first document order. When pyatspi is
    unavailable this returns an empty list (so callers can fall back to
    coordinate input) rather than raising.
    """
    try:
        pairs, _win = _collect_nodes(window_id, max_elements=max_elements,
                                     only_interactive=only_interactive)
    except (RuntimeError, ValueError):
        return []
    role = role.lower()
    name = name.lower()
    text = text.lower()
    out = []
    for el, _n in pairs:
        r_ok = (not role) or role in el.role
        n_ok = (not name) or (name in el.name.lower())
        t_ok = (not text) or (text in el.name.lower())
        if r_ok and n_ok and t_ok:
            out.append(el)
    return out

def _node_by_index(window_id: str, element_index: int,
                   max_elements: int = 500):
    # Use the SAME numbering as get_window_state (interactive elements only),
    # so an index returned by get_window_state is valid here and in
    # click_element / perform_action / set_value.
    try:
        pairs, _win = _collect_nodes(window_id, max_elements=max_elements,
                                     only_interactive=True)
    except (RuntimeError, ValueError):
        return None, None
    for el, node in pairs:
        if el.index == element_index:
            return el, node
    return None, None


def click_element(window_id: str, element_index: int, max_elements: int = 100,
                  button: str = "left", double: bool = False) -> dict:
    """Resolve an AT-SPI element index to its center and click it."""
    el, _node = _node_by_index(window_id, element_index, max_elements=max_elements)
    if el is None:
        return {"ok": False, "error": f"element {element_index} not found"}
    if el.width == 0 or el.height == 0:
        return {"ok": False, "error": f"element {element_index} has no bounds"}
    from .input import click_window
    from .windows import get_window
    win = get_window(window_id)
    local_x = (el.x + el.width // 2) - win.x
    local_y = (el.y + el.height // 2) - win.y
    click_window(window_id, local_x, local_y, button=button, double=double)
    return {"ok": True, "element": el.index,
            "center_screen": [el.x + el.width // 2, el.y + el.height // 2]}


def click_semantic(window_id: str, *, role: str = "", name: str = "",
                   text: str = "", button: str = "left",
                   double: bool = False, max_elements: int = 500) -> dict:
    """Click the first element matching semantic role/name/text filters."""
    matches = resolve_elements(window_id, role=role, name=name, text=text,
                               max_elements=max_elements)
    if not matches:
        return {"ok": False,
                "error": f"no element matched role={role!r} name={name!r} text={text!r}"}
    el = matches[0]
    if el.width == 0 or el.height == 0:
        return {"ok": False, "error": f"matched element {el.index} has no bounds"}
    return click_element(window_id, el.index, button=button, double=double,
                         max_elements=max_elements)


def perform_action(window_id: str, element_index: int, action: str = "",
                   max_elements: int = 500) -> dict:
    """Invoke an AT-SPI action on an element (e.g. 'press', 'activate').

    When ``action`` is empty the element's primary action (index 0) is used.
    Requires pyatspi; returns an error dict when unavailable.
    """
    el, node = _node_by_index(window_id, element_index, max_elements=max_elements)
    if el is None or node is None:
        return {"ok": False, "error": f"element {element_index} not found"}
    try:
        atn = node.queryAction()
        n = int(atn.nActions)
        if n == 0:
            return {"ok": False, "error": f"element {element_index} exposes no actions"}
        idx = -1
        if action:
            idx = next((i for i in range(n)
                        if atn.getName(i).strip().lower() == action.lower()), -1)
            if idx == -1:
                names = [str(atn.getName(i)) for i in range(n)]
                return {"ok": False, "error": f"no action {action!r}; available: {names}"}
        else:
            idx = 0
        name = str(atn.getName(idx))
        ok = bool(atn.doAction(idx))
        return {"ok": ok, "action": name, "element": el.index}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def set_value(window_id: str, element_index: int, value: str,
              max_elements: int = 500) -> dict:
    """Write a value to a settable AT-SPI element (text field, slider, ...)."""
    el, node = _node_by_index(window_id, element_index, max_elements=max_elements)
    if el is None or node is None:
        return {"ok": False, "error": f"element {element_index} not found"}
    try:
        et = node.queryEditableText()
        et.setTextContents(str(value))
        return {"ok": True, "element": el.index, "value": str(value)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"element {element_index} not settable: {exc}"}
