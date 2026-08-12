"""
Accessibility tree (AT-SPI) introspection for KDE Plasma on Wayland.

Many GTK/Qt/KDE applications expose an AT-SPI accessibility tree even on
Wayland, where the old X11 `get_window_state` trick does not work. This
module returns a structured list of interactive elements (with their on-screen
bounds, state flags and the actions they expose) so a caller can target them
three ways:

  * by index        (``click_element``, ``perform_action``, ``set_value``)
  * semantically     (``resolve_elements`` with role / name / text filters)
  * by coordinates   (fall back to the input module's absolute clicks)

Two backends are supported and chosen automatically:

  * ``atspi_dbus`` (preferred) - a pure-D-Bus AT-SPI client (jeepney) with no
    system dependency. This is what works on Arch, where pyatspi is not
    packaged.
  * ``pyatspi`` - only if the legacy pyatspi module happens to be importable.

The module degrades gracefully when neither backend is available, so calls
fall back to coordinate input instead of crashing.

Note: AT-SPI element bounds are in screen coordinates, so they feed directly
into the input module's absolute click coordinates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from . import atspi_dbus


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
    # Opaque handle for acting on the node: (bus, path) for the D-Bus backend,
    # a live pyatspi node for the pyatspi backend. Not part of the public shape.
    handle: object = None


# Roles we consider "interactive" for SOM overlays.
_INTERACTIVE_ROLES = {
    "push button", "button", "text", "entry", "text entry", "edit",
    "check box", "check button", "radio button", "combo box", "spin button",
    "slider", "link", "menu item", "list item", "tab", "page tab",
    "toggle button", "icon", "image", "label", "table cell", "scroll bar",
}


def _atspi_available() -> bool:
    """True if the legacy pyatspi module is importable."""
    try:
        import pyatspi  # noqa: F401
        return True
    except Exception:
        return False


_backend_cache = None


def _backend() -> Optional[str]:
    """Pick the AT-SPI backend: 'dbus' (preferred), 'pyatspi', or None."""
    global _backend_cache
    if _backend_cache is None:
        if atspi_dbus.available():
            _backend_cache = "dbus"
        elif _atspi_available():
            _backend_cache = "pyatspi"
        else:
            _backend_cache = None
    return _backend_cache


def _el_to_dict(el: A11yElement) -> dict:
    d = {
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
    return d


def _collect_nodes(window_id: str, max_elements: int = 500,
                   only_interactive: bool = True):
    """Return (elements: list[(A11yElement, handle)], win) for a window.

    Renders nothing publicly; raises RuntimeError if no AT-SPI backend is
    available.
    """
    be = _backend()
    if be is None:
        raise RuntimeError(
            "AT-SPI unavailable: neither the D-Bus backend (jeepney) nor "
            "pyatspi is usable"
        )
    from .windows import get_window, is_uuid
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    win = get_window(window_id)

    if be == "dbus":
        if not win.pid:
            raise RuntimeError(
                "window reports no PID; cannot match its AT-SPI tree"
            )
        children = atspi_dbus.elements_for_window(
            win.pid, max_elements=max_elements, only_interactive=only_interactive)
        out = []
        for c in children:
            handle = c.get("handle")  # read-only: cached lists must not mutate
            out.append((A11yElement(
                index=c["index"], role=c["role"], name=c["name"],
                x=c["x"], y=c["y"], width=c["width"], height=c["height"],
                states=c["states"], actions=c["actions"], editable=c["editable"],
                handle=handle,
            ), handle))
        return out, win

    # pyatspi backend.
    import pyatspi

    registry = pyatspi.Registry
    desktop = registry.getDesktop(0)
    pairs: list = []
    idx = 0

    def _state_flags(node) -> tuple[list, bool, list]:
        states, editable, actions = [], False, []
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
            states, editable, actions = _state_flags(node)
        except Exception:
            return
        if not only_interactive or role in _INTERACTIVE_ROLES:
            el = A11yElement(
                index=idx, role=role, name=name,
                x=x, y=y, width=w, height=h,
                states=states, actions=actions, editable=editable, handle=node,
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
    index, role, name, bounds, state flags, actions, editable) and 'error' when
    unavailable. When 'available' is False the caller should fall back to
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
        "backend": _backend(),
    }


def resolve_elements(window_id: str, *, role: str = "", name: str = "",
                     text: str = "", max_elements: int = 500,
                     only_interactive: bool = True) -> list[A11yElement]:
    """Semantic search: return elements matching role / name / text filters.

    All filters are case-insensitive substrings. ``text`` additionally matches
    an element's name (buttons and text nodes expose their label as the name).
    Returns a flat list ordered by depth-first document order. When no AT-SPI
    backend is available this returns an empty list (so callers fall back to
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
    for el, handle in pairs:
        if el.index == element_index:
            return el, handle
    return None, None


def click_element(window_id: str, element_index: int, max_elements: int = 100,
                  button: str = "left", double: bool = False) -> dict:
    """Resolve an AT-SPI element index to its center and click it."""
    el, _handle = _node_by_index(window_id, element_index, max_elements=max_elements)
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
    Requires an AT-SPI backend; returns an error dict when unavailable.
    """
    el, handle = _node_by_index(window_id, element_index, max_elements=max_elements)
    if el is None:
        return {"ok": False, "error": f"element {element_index} not found"}
    if isinstance(handle, tuple) and _backend() == "dbus":
        ok, detail = atspi_dbus.perform_action(handle, action)
        return {"ok": ok, "action": action or "primary",
                "element": el.index, **( {"error": detail} if not ok else {})}
    # pyatspi backend.
    try:
        atn = handle.queryAction()
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
    el, handle = _node_by_index(window_id, element_index, max_elements=max_elements)
    if el is None:
        return {"ok": False, "error": f"element {element_index} not found"}
    if isinstance(handle, tuple) and _backend() == "dbus":
        ok, detail = atspi_dbus.set_value(handle, value)
        return {"ok": ok, "element": el.index, "value": str(value),
                **( {"error": detail} if not ok else {})}
    # pyatspi backend.
    try:
        et = handle.queryEditableText()
        et.setTextContents(str(value))
        return {"ok": True, "element": el.index, "value": str(value)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"element {element_index} not settable: {exc}"}
