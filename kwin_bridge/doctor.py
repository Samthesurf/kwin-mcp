"""Structured readiness report for kwin-mcp (the `doctor` capability).

Returns one JSON document describing the platform, the accessibility stack,
the windowing backend, the input path, and the screenshot path, plus a
``readiness`` summary with explicit blockers and a recommended next step. This
mirrors the ``doctor`` command from agent-sh/computer-use-linux so an MCP host
(or a human) can render a single capability report instead of parsing prose.

Everything here is best-effort and non-throwing: a missing probe is reported
with its ``ok`` flag false and a short ``detail``, never as a crash. Nothing
is a hard dependency for listing windows (kdotool) or input is considered
"ready"; the summary aggregates the pieces that actually gate the toolset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from ._env import base_env  # resolves session env when a client stripped it
from .preflight import _user_groups  # internal reuse


def _b(present: bool, detail: str = "") -> dict:
    return {"ok": present, "detail": detail}


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _portal_capability(interface: str, method: str) -> dict:
    """Introspect an XDG portal interface over the session bus for a method.

    Uses ``busctl`` with a short timeout. Returns an honest ``ok``/``detail``.
    If the bus or busctl is unavailable we report it as unknown, not a blocker.
    """
    try:
        env = base_env()  # resolve DBUS_SESSION_BUS_ADDRESS / sockets
        if not shutil.which("busctl"):
            return _b(False, "busctl not found; can't introspect portal")
        proc = subprocess.run(
            ["busctl", "--user", "introspect", "org.freedesktop.portal.Desktop",
             f"/org/freedesktop/portal/desktop"],
            capture_output=True, text=True, timeout=4, env=env,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return _b(False, f"portal interface not introspectable: {proc.stderr.strip()[:120]}")
        return _b(method in proc.stdout, f"interface exposes {method}")
    except subprocess.TimeoutExpired:
        return _b(False, "portal introspection timed out")
    except Exception as exc:  # noqa: BLE001
        return _b(False, f"portal probe failed: {exc}")


def _display_server(resolved_env: dict) -> str:
    """Determine the display server from a (possibly resolved) env dict."""
    if resolved_env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if resolved_env.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    if resolved_env.get("DISPLAY"):
        return "x11"
    return "unknown"


def run_doctor() -> dict:
    env = base_env()  # resolved env (survives MCP env stripping)
    ds = _display_server(env)
    groups = _user_groups()
    kdotool = _which("kdotool")
    spectacle = _which("spectacle")
    ydotool = _which("ydotool")
    uinput_path = "/dev/uinput"
    uinput_w = os.access(uinput_path, os.W_OK) if os.path.exists(uinput_path) else False

    # Window list probe: can we actually enumerate windows right now?
    wind_list = _b(False, "not probed")
    try:
        from . import windows as winmod
        n = len(winmod.list_windows())
        wind_list = _b(True, f"kdotool enumerated {n} top-level window(s)")
    except Exception as exc:  # noqa: BLE001
        wind_list = _b(False, f"kdotool window listing failed: {exc}")

    # Accessibility stack.
    atspi = False
    try:
        import pyatspi  # noqa: F401
        registry = pyatspi.Registry
        registry.getDesktop(0)
        atspi = True
    except Exception:  # noqa: BLE001
        atspi = False

    auto_report = {
        "platform": {
            "os": "linux",
            "arch": getattr(os, "uname")().machine if hasattr(os, "uname") else "unknown",
            "display_server": ds,
            "wayland_display": env.get("WAYLAND_DISPLAY"),
            "x11_display": env.get("DISPLAY"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION"),
            "xdg_session_type": env.get("XDG_SESSION_TYPE"),
            "dbus_session_bus_address": env.get("DBUS_SESSION_BUS_ADDRESS"),
            "pid": os.getpid(),
        },
        "windowing": {
            "kdotool_present": kdotool is not None,
            "list_windows": wind_list,
            "backend": "kdotool (KDE/KWin over D-Bus)",
        },
        "input": {
            "in_input_group": "input" in groups,
            "uinput_writable": uinput_w,
            "uinput_path": uinput_path if os.path.exists(uinput_path) else "absent",
            "ydotool_present": ydotool is not None,
            "backend": "/dev/uinput virtual device (python-uinput)",
        },
        "accessibility": {
            "pyatspi_available": atspi,
            "element_targeting": atspi,
        },
        "screenshot": {
            "spectacle_present": spectacle is not None,
            "backend": "spectacle (non-interactive) + Pillow crop/validate",
        },
        "portals": {
            "remote_desktop": _portal_capability("org.freedesktop.portal.RemoteDesktop", ".Start"),
            "screenshot_portal": _portal_capability("org.freedesktop.portal.Screenshot", ".Screenshot"),
        },
    }

    blockers: list[str] = []
    if ds != "wayland":
        blockers.append(f"not on Wayland (got {ds!r}); kwin-mcp targets KDE Plasma on Wayland")
    if not kdotool:
        blockers.append("kdotool is not installed; install it to enumerate/focus windows")
    if not wind_list.get("ok"):
        blockers.append("window listing did not succeed; fix kdotool/session first")
    if not uinput_w:
        blockers.append("/dev/uinput is not writable; join the 'input' group and re-login")
    if not spectacle:
        blockers.append("spectacle is not installed; install it for screenshots")

    can_list = bool(wind_list.get("ok"))
    can_input = bool(uinput_w)
    can_capture = spectacle is not None
    can_atspi = atspi

    report = {
        "platform": auto_report["platform"],
        "windowing": auto_report["windowing"],
        "input": auto_report["input"],
        "accessibility": auto_report["accessibility"],
        "screenshot": auto_report["screenshot"],
        "portals": auto_report["portals"],
        "can_list_windows": can_list,
        "can_focus_windows": can_list,
        "can_send_input": can_input,
        "can_capture": can_capture,
        "can_semantic_target": can_atspi and can_input,
        "readiness": {
            "ready": can_list and can_input and len(blockers) == 0,
            "blockers": blockers,
            "recommended_next_step": (
                "kwin-mcp is ready" if not blockers
                else "; ".join(blockers[:3])
            ),
        },
    }
    return report


def main() -> int:
    import json
    print(json.dumps(run_doctor(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
