"""Dependency preflight for kwin-mcp.

Run `kwin-mcp --check` (or `python -m kwin_bridge.preflight`) to verify the
environment. Prints a clear, actionable report and exits non-zero if anything
required is missing. This is what the one-command setup runs before wiring
the server into an agent, so the user always knows exactly what to install.
"""

from __future__ import annotations

import grp
import os
import sys


def _user_groups() -> set[str]:
    try:
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
        return names
    except Exception:
        return set()


def _display_server() -> str:
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def run_checks() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    import shutil
    kdotool = shutil.which("kdotool") is not None
    checks.append((
        "kdotool",
        kdotool,
        "sudo pacman -S kdotool   (Arch)  |  build from source (Debian/Ubuntu)",
    ))

    spectacle = shutil.which("spectacle") is not None
    checks.append((
        "spectacle",
        spectacle,
        "sudo pacman -S spectacle   (Arch)  |  sudo apt install kde-spectacle",
    ))

    in_input = "input" in _user_groups()
    checks.append((
        "you are in the 'input' group (for /dev/uinput)",
        in_input,
        "sudo usermod -aG input $USER   # then log out and back in",
    ))

    uinput_writable = os.access("/dev/uinput", os.W_OK) if os.path.exists("/dev/uinput") else False
    checks.append((
        "/dev/uinput is writable",
        uinput_writable,
        "ensure group 'input' owns /dev/uinput and you are in that group",
    ))

    ds = _display_server()
    checks.append((
        "running on Wayland (KDE)",
        ds == "wayland",
        f"detected display server: {ds}. kwin-mcp targets KDE Plasma on Wayland.",
    ))

    return checks


def main() -> int:
    print("kwin-mcp dependency check")
    print("=" * 48)
    checks = run_checks()
    ok_count = 0
    for name, ok, hint in checks:
        mark = "OK  " if ok else "MISS"
        print(f"  [{mark}] {name}")
        if not ok:
            print(f"         -> install: {hint}")
        else:
            ok_count += 1
    print("-" * 48)
    total = len(checks)
    print(f"{ok_count}/{total} checks passed")
    if ok_count == total:
        print("All good. kwin-mcp is ready to use.")
        return 0
    print("Some dependencies are missing. Install them, then re-run this check.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
