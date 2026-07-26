"""
Screen capture for KDE Plasma on Wayland.

cua-driver relies on the X server for screenshots, which (a) only covers
XWayland windows and (b) is unavailable for native Wayland surfaces. On KDE
the reliable capture path is `spectacle` run in background/non-interactive
mode:

  * Full desktop .......... spectacle -b -n -f -o <file>
  * Active window .......... spectacle -b -n -a -o <file>   (focus target first)
  * Region (x,y,w,h) ....... spectacle -b -n -r -o <file>    (not used; we crop)

For a *per-window* capture we activate the window, screenshot the active
window, and then crop to its exact geometry with Pillow so the result is the
window's content only (spectacle's active-window mode may include a small
border/shadow depending on theme). If Pillow is unavailable we fall back to
the uncropped active-window image.

All capture is synchronous: we wait for the file to appear before returning,
so callers get a path to a complete PNG.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import Optional

from .windows import Window, activate, get_window, is_uuid
from ._env import base_env

DEFAULT_FORMAT = "png"
_CAPTURE_POLL = 0.15
_CAPTURE_TIMEOUT = 10.0
# Pre-capture settle delay. On KDE Wayland, spectacle --background can race the
# compositor and (rarely) capture the lock-screen splash layer instead of the
# live desktop. A short settle before invoking, plus a validation retry, makes
# the capture reliable.
_PRE_DELAY = 0.5
_POST_DELAY = 0.4
_MAX_RETRIES = 4


def _run(args, timeout: float = 20.0) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env=base_env(), stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        try:
            with open("/tmp/kwin_sp_err.log", "a") as fh:
                fh.write(f"ARGS: {' '.join(args)}\nRC: {proc.returncode}\n"
                         f"STDOUT: {proc.stdout!r}\nSTDERR: {proc.stderr!r}\n"
                         f"PID ENV DBUS={os.environ.get('DBUS_SESSION_BUS_ADDRESS')}\n"
                         f"PID ENV WAYLAND={os.environ.get('WAYLAND_DISPLAY')}\n"
                         f"PID ENV DISPLAY={os.environ.get('DISPLAY')}\n---\n")
        except Exception:
            pass
        raise RuntimeError(
            f"screenshot command failed: {' '.join(args)}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )


def _wait_for_file(path: str, timeout: float = _CAPTURE_TIMEOUT) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        time.sleep(_CAPTURE_POLL)
    raise RuntimeError(f"screenshot file did not appear: {path}")


def _looks_like_real_frame(path: str) -> bool:
    """Heuristic to reject splash/lock-screen captures.

    A real desktop frame is visually busy (many distinct colours), whereas the
    KDE lock-screen splash is a near-uniform wallpaper. We sample the image and
    reject frames whose colour variance is suspiciously low.
    """
    try:
        from PIL import Image
        import struct
    except ImportError:
        return True  # cannot validate; trust the capture
    try:
        with Image.open(path).convert("RGB") as im:
            small = im.resize((64, 36))
            px = list(small.getdata())
        # Compute variance of luminance across sampled pixels.
        lum = [0.299 * r + 0.587 * g + 0.114 * b for (r, g, b) in px]
        mean = sum(lum) / len(lum)
        var = sum((v - mean) ** 2 for v in lum) / len(lum)
        # A real desktop almost always has meaningful variance; the splash does not.
        return var > 50.0
    except Exception:
        return True


def _capture_with_retry(build_args, output_path: str, window_id: str = "") -> str:
    """Invoke spectacle with settle delay + validation retry.

    spectacle can occasionally return a non-zero exit while still having
    written the PNG (especially on its first launch in a fresh session). We
    therefore treat a non-zero exit as soft: if the file appeared, we proceed
    to validation; only if the file is truly missing do we retry.
    """
    last_err = None
    for attempt in range(_MAX_RETRIES):
        time.sleep(_PRE_DELAY)
        try:
            _run(build_args())
        except subprocess.TimeoutExpired as exc:
            last_err = exc
            continue
        except RuntimeError as exc:
            # Soft failure: if the PNG still landed, keep it.
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                last_err = None
            else:
                last_err = exc
                continue
        _wait_for_file(output_path)
        time.sleep(_POST_DELAY)
        if _looks_like_real_frame(output_path):
            return output_path
        last_err = RuntimeError("captured frame failed desktop validation")
    if last_err:
        raise last_err
    return output_path


def _crop_to_window(src: str, dst: str, win: Window) -> None:
    try:
        from PIL import Image
    except ImportError:
        # No Pillow: return the (possibly bordered) active-window screenshot.
        if src != dst:
            os.replace(src, dst)
        return
    with Image.open(src) as im:
        # spectacle -a may include a few px of padding/shadow; clamp crop.
        w = max(1, min(win.width, im.width))
        h = max(1, min(win.height, im.height))
        # Some themes add a border; prefer the central region of the geometry.
        box = (0, 0, w, h)
        im.crop(box).save(dst, format="PNG")
    # Remove the intermediate if distinct.
    if src != dst and os.path.exists(src):
        try:
            os.remove(src)
        except OSError:
            pass


def capture_full(output_path: Optional[str] = None) -> str:
    """Capture the entire desktop and return the PNG path."""
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".png", prefix="kwin_full_")
        os.close(fd)
    return _capture_with_retry(
        lambda: ["spectacle", "-b", "-n", "-f", "-o", output_path],
        output_path,
    )


def capture_window(
    window_id: str,
    output_path: Optional[str] = None,
    crop: bool = True,
) -> str:
    """Capture a specific window (by KDE window UUID) and return the PNG path.

    Focuses the window first so spectacle's active-window mode targets it.
    When crop=True the image is trimmed to the window's exact geometry.
    """
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    win = get_window(window_id)
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".png", prefix="kwin_win_")
        os.close(fd)
    intermediate = output_path
    if crop:
        fd, intermediate = tempfile.mkstemp(suffix=".png", prefix="kwin_raw_")
        os.close(fd)
    # Focus, give KWin a moment to raise it, then capture the active window.
    activate(window_id)
    time.sleep(0.35)
    _run(["spectacle", "-b", "-n", "-a", "-o", intermediate])
    _wait_for_file(intermediate)
    if crop:
        _crop_to_window(intermediate, output_path, win)
    return output_path


def capture_desktop_state(output_path: Optional[str] = None) -> str:
    """Alias for capture_full() used by the MCP desktop-state tool."""
    return capture_full(output_path)
