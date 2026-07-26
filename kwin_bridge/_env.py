"""Shared environment setup for kwin-bridge subprocess calls.

kdotool / spectacle / kdotool talk to the KDE session bus over D-Bus and to
the compositor over a display connection. When an MCP client launches
server.py it may NOT forward DBUS_SESSION_BUS_ADDRESS / WAYLAND_DISPLAY /
DISPLAY. kdotool then fails with "X11 for dbus-daemon autolaunch was
disabled", and spectacle crashes (SIGABRT, rc -6) with no display. We default
all three from the well-known systemd-user-session locations so the bridge
works regardless of how the parent spawned it.
"""

from __future__ import annotations

import os
import glob


def _default_wayland_display() -> str:
    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    # Common default first.
    candidates = ["wayland-0", "wayland-1"]
    for c in candidates:
        if os.path.exists(os.path.join(runtime, c)):
            return c
    # Otherwise pick the first wayland-* socket present.
    found = sorted(glob.glob(os.path.join(runtime, "wayland-*")))
    if found:
        return os.path.basename(found[0])
    return "wayland-0"


def _default_display() -> str:
    # XWayland typically listens on :1 under a Wayland session.
    for d in (":1", ":0"):
        if os.path.exists(f"/tmp/.X11-unix/X{d[1:]}"):
            return d
    return ":1"


def base_env() -> dict:
    env = dict(os.environ)
    uid = os.getuid()

    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        candidate = f"unix:path=/run/user/{uid}/bus"
        if os.path.exists(f"/run/user/{uid}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = candidate

    if not env.get("WAYLAND_DISPLAY"):
        env["WAYLAND_DISPLAY"] = _default_wayland_display()

    if not env.get("DISPLAY"):
        env["DISPLAY"] = _default_display()

    return env
