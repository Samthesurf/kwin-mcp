"""
Window enumeration and control for KDE Plasma on Wayland.

This module wraps `kdotool` (a xdotool-like utility for KDE 5/6) to expose
the full list of native Wayland windows that the X11-only cua-driver cannot
see. Every window is identified by a stable KDE window UUID of the form
{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}.

The kdotool quirks handled here (observed on Plasma 6 + Wayland):
  * `kdotool search ""` enumerates ALL top-level windows, including panels,
    the desktop, and docks, which have empty title/class. We surface them
    too (with empty name) so the count is honest.
  * `kdotool search --onlyvisible ""` returns nothing on this setup, so we
    implement "visible" ourselves by returning windows whose geometry is
    non-zero and that are not known desktop/panel surfaces.
  * UUIDs must be passed to kdotool exactly as printed (with the braces).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from ._env import base_env

UUID_RE = re.compile(r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}")


@dataclass
class Window:
    window_id: str           # KDE window UUID, e.g. {342b7b3d-...}
    title: str = ""
    app_name: str = ""       # window class (resource name)
    pid: Optional[int] = None
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    off_screen: bool = False
    visible: bool = True
    raw: dict = field(default_factory=dict)


def _run(args, timeout: float = 15.0) -> str:
    """Run a command and return stdout text, raising on failure."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=base_env(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required binary not found: {args[0]}") from exc
    if proc.returncode != 0:
        # kdotool prints errors to stdout in newer versions; do not hard-fail
        # on non-zero exit when there is useful output.
        combined = (proc.stdout or "") + (proc.stderr or "")
        if not proc.stdout.strip():
            raise RuntimeError(f"command failed: {' '.join(args)}\n{combined}")
    return proc.stdout


def _kdotool(args, timeout: float = 15.0) -> str:
    return _run(["kdotool", *args], timeout=timeout)


def is_uuid(candidate: str) -> bool:
    return bool(UUID_RE.fullmatch(candidate.strip()))


def _normalize_window_arg(window: str) -> str:
    """kdotool accepts a bare UUID; ensure braces are preserved."""
    w = window.strip()
    if not w:
        return "%1"
    return w


def _parse_geometry(text: str) -> tuple[int, int, int, int]:
    """Parse kdotool getwindowgeometry output.

    Expected shape (per line):
        Window {uuid}
          Position: 0,0
          Geometry: 1366x768
    """
    x = y = width = height = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Position:"):
            m = re.search(r"(-?\d+)\s*,\s*(-?\d+)", line)
            if m:
                x, y = int(m.group(1)), int(m.group(2))
        elif line.startswith("Geometry:"):
            m = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)", line)
            if m:
                width = int(float(m.group(1)))
                height = int(float(m.group(2)))
    return x, y, width, height


def list_windows() -> list[Window]:
    """Return every top-level window on the desktop (native Wayland + XWayland)."""
    out = _kdotool(["search", ""])
    uuids = UUID_RE.findall(out)
    windows: list[Window] = []
    for uuid in uuids:
        title = ""
        try:
            title = _kdotool(["getwindowname", uuid]).strip().splitlines()
            title = title[0] if title else ""
        except RuntimeError:
            title = ""
        try:
            cls = _kdotool(["getwindowclassname", uuid]).strip().splitlines()
            cls = cls[0] if cls else ""
        except RuntimeError:
            cls = ""
        pid = None
        try:
            pid_raw = _kdotool(["getwindowpid", uuid]).strip().splitlines()
            if pid_raw:
                m = re.search(r"\d+", pid_raw[0])
                if m:
                    pid = int(m.group(0))
        except RuntimeError:
            pid = None
        geo_text = _kdotool(["getwindowgeometry", uuid])
        x, y, w, h = _parse_geometry(geo_text)
        visible = (w > 0 and h > 0)
        windows.append(
            Window(
                window_id=uuid,
                title=title,
                app_name=cls,
                pid=pid,
                x=x,
                y=y,
                width=w,
                height=h,
                off_screen=False,
                visible=visible,
                raw={"geometry_raw": geo_text.strip()},
            )
        )
    return windows


def get_window(window_id: str) -> Window:
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    for w in list_windows():
        if w.window_id == window_id:
            return w
    raise KeyError(f"window not found: {window_id}")


def find_window(predicate) -> Optional[Window]:
    for w in list_windows():
        if predicate(w):
            return w
    return None


def active_window() -> Optional[Window]:
    out = _kdotool(["getactivewindow"]).strip()
    m = UUID_RE.search(out)
    if not m:
        return None
    return get_window(m.group(0))


def activate(window_id: str) -> None:
    """Raise + focus a window (switching virtual desktop if needed)."""
    _kdotool(["windowactivate", _normalize_window_arg(window_id)])


def raise_window(window_id: str) -> None:
    _kdotool(["windowraise", _normalize_window_arg(window_id)])


def minimize(window_id: str) -> None:
    _kdotool(["windowminimize", _normalize_window_arg(window_id)])


def close(window_id: str) -> None:
    _kdotool(["windowclose", _normalize_window_arg(window_id)])


def focus_and_screenshot_target(window_id: str) -> Window:
    """Activate a window so it becomes the 'active window' for spectacle -a.

    Returns the Window record (with geometry) for downstream coordinate math.
    """
    activate(window_id)
    return get_window(window_id)
