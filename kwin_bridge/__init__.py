"""kwin-bridge: control native Wayland windows on KDE Plasma via MCP.

This package wraps kdotool (enumeration/geometry/focus), spectacle
(screen capture) and a /dev/uinput virtual device (synthetic input) to give
an MCP client the same capabilities cua-driver provides on X11. See the
module docstrings for the Wayland-specific caveats (focus-then-inject, single
cursor, no background targeting).
"""

from . import windows, screenshot, input as input_mod, a11y, _env  # noqa: F401

__all__ = ["windows", "screenshot", "input_mod", "a11y"]
