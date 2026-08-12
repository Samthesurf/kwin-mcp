"""kwin-bridge: control native Wayland windows on KDE Plasma via MCP.

This package wraps kdotool (enumeration/geometry/focus), spectacle
(screen capture) and a /dev/uinput virtual device (synthetic input) to give
an MCP client the same capabilities cua-driver provides on X11. See the
module docstrings for the Wayland-specific caveats (focus-then-inject, single
cursor, no background targeting).

Deliberately imports nothing heavy here: submodules are imported explicitly by
the code that uses them (server.py, preflight.py, tests). This keeps
``import kwin_bridge`` light so the dependency preflight can run with a plain
system Python that does not yet have uinput/jeepney installed.
"""

__all__ = ["windows", "screenshot", "input", "a11y", "doctor"]
