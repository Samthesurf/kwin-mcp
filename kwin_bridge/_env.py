"""Shared environment setup for kwin-bridge subprocess calls.

kdotool / spectacle / kdotool talk to the KDE session bus over D-Bus. When the
server is launched by an MCP client (Claude, Hermes, Codex, ...) the parent
process may NOT forward DBUS_SESSION_BUS_ADDRESS, which makes kdotool fail
with "Using X11 for dbus-daemon autolaunch was disabled". On a systemd user
session the bus is always at unix:path=/run/user/<uid>/bus, so we default to
that when the variable is missing. This keeps the bridge working regardless of
how the parent spawned it.
"""

from __future__ import annotations

import os


def base_env() -> dict:
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        uid = os.getuid()
        candidate = f"unix:path=/run/user/{uid}/bus"
        if os.path.exists(f"/run/user/{uid}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = candidate
    return env
