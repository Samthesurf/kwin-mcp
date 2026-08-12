"""
Synthetic input for KDE Plasma on Wayland.

On Wayland there is no X server between applications and the compositor, so
input cannot be injected "into a specific window" the way cua-driver does on
X11. Instead we inject via a kernel virtual input device backed by
/dev/uinput, which the compositor treats as a real mouse + keyboard. The
compositor routes that input to whatever window is focused and under the
cursor. The bridge therefore follows a FOCUS-THEN-INJECT pattern:

    1. focus the target window (kdotool windowactivate)
    2. move the virtual pointer to the target coordinate (in screen space)
    3. emit the click / key / drag

Because the pointer is global, this relocates the real cursor. That is an
inherent property of the Wayland input model, not a bug. It matches how
ydotool and all other Wayland automation tools behave.

Requirements:
  * The user must be in the `input` group (or otherwise have write access to
    /dev/uinput). On this machine the bridge's owner is a member of `input`,
    so no root daemon (ydotoold) is required; python-uinput opens the device
    directly.
  * A Wayland session must be running (the compositor reads the device).

If python-uinput cannot open /dev/uinput, the module raises a clear error
rather than silently doing nothing.
"""

from __future__ import annotations

import time
from typing import Optional

try:
    import uinput
except ImportError as exc:  # pragma: no cover - surfaced to caller
    raise RuntimeError(
        "python-uinput is required for synthetic input. "
        "Install it (pip install python-uinput) and ensure you are in the "
        "'input' group with write access to /dev/uinput."
    ) from exc

from .windows import activate, get_window, is_uuid

# IMPORTANT: split the virtual pointer and virtual keyboard into TWO separate
# uinput devices. A single device that mixes REL (mouse) and a large KEY set
# is classified inconsistently by KWin on Wayland and the relative motion can
# be ignored. Keeping the pointer device minimal (REL + BTN only) makes KWin
# treat it as a real mouse, so relative motion actually moves the cursor.

# Pointer (mouse) device capabilities: only movement + buttons + wheel.
_POINTER_EVENTS = (
    uinput.REL_X,
    uinput.REL_Y,
    uinput.BTN_LEFT,
    uinput.BTN_RIGHT,
    uinput.BTN_MIDDLE,
    uinput.REL_WHEEL,
)

# Keyboard device capabilities.
_KEYBOARD_EVENTS = (
    uinput.KEY_A, uinput.KEY_B, uinput.KEY_C, uinput.KEY_D, uinput.KEY_E,
    uinput.KEY_F, uinput.KEY_G, uinput.KEY_H, uinput.KEY_I, uinput.KEY_J,
    uinput.KEY_K, uinput.KEY_L, uinput.KEY_M, uinput.KEY_N, uinput.KEY_O,
    uinput.KEY_P, uinput.KEY_Q, uinput.KEY_R, uinput.KEY_S, uinput.KEY_T,
    uinput.KEY_U, uinput.KEY_V, uinput.KEY_W, uinput.KEY_X, uinput.KEY_Y,
    uinput.KEY_Z,
    uinput.KEY_0, uinput.KEY_1, uinput.KEY_2, uinput.KEY_3, uinput.KEY_4,
    uinput.KEY_5, uinput.KEY_6, uinput.KEY_7, uinput.KEY_8, uinput.KEY_9,
    uinput.KEY_SPACE, uinput.KEY_ENTER, uinput.KEY_TAB, uinput.KEY_BACKSPACE,
    uinput.KEY_ESC, uinput.KEY_DELETE, uinput.KEY_LEFT, uinput.KEY_RIGHT,
    uinput.KEY_UP, uinput.KEY_DOWN, uinput.KEY_HOME, uinput.KEY_END,
    uinput.KEY_PAGEUP, uinput.KEY_PAGEDOWN, uinput.KEY_MINUS, uinput.KEY_EQUAL,
    uinput.KEY_LEFTCTRL, uinput.KEY_RIGHTCTRL, uinput.KEY_LEFTSHIFT,
    uinput.KEY_RIGHTSHIFT, uinput.KEY_LEFTALT, uinput.KEY_RIGHTALT,
    uinput.KEY_LEFTMETA, uinput.KEY_GRAVE, uinput.KEY_SLASH, uinput.KEY_DOT,
    uinput.KEY_COMMA, uinput.KEY_SEMICOLON, uinput.KEY_APOSTROPHE,
    uinput.KEY_LEFTBRACE, uinput.KEY_RIGHTBRACE, uinput.KEY_BACKSLASH,
)

# Closed-loop movement tuning. Because the compositor applies mouse
# acceleration, a single relative emit rarely lands exactly on target. We move
# in small steps and re-read the real cursor until we are within tolerance.
_MOVE_STEP = 40          # max pixels per relative emit
_MOVE_TOLERANCE = 3      # px considered "on target"
_MOVE_MAX_ITERS = 60

# Mapping from human key names to uinput keys.
_KEY_NAME_MAP = {
    "enter": uinput.KEY_ENTER, "return": uinput.KEY_ENTER,
    "tab": uinput.KEY_TAB, "backspace": uinput.KEY_BACKSPACE,
    "delete": uinput.KEY_DELETE, "del": uinput.KEY_DELETE,
    "escape": uinput.KEY_ESC, "esc": uinput.KEY_ESC,
    "space": uinput.KEY_SPACE, "up": uinput.KEY_UP, "down": uinput.KEY_DOWN,
    "left": uinput.KEY_LEFT, "right": uinput.KEY_RIGHT,
    "home": uinput.KEY_HOME, "end": uinput.KEY_END,
    "pageup": uinput.KEY_PAGEUP, "pagedown": uinput.KEY_PAGEDOWN,
    "ctrl": uinput.KEY_LEFTCTRL, "control": uinput.KEY_LEFTCTRL,
    "shift": uinput.KEY_LEFTSHIFT, "alt": uinput.KEY_LEFTALT,
    "meta": uinput.KEY_LEFTMETA, "super": uinput.KEY_LEFTMETA,
    "win": uinput.KEY_LEFTMETA,
    "-": uinput.KEY_MINUS, "=": uinput.KEY_EQUAL, "/": uinput.KEY_SLASH,
    ".": uinput.KEY_DOT, ",": uinput.KEY_COMMA,
    ";": uinput.KEY_SEMICOLON, "'": uinput.KEY_APOSTROPHE,
    "[": uinput.KEY_LEFTBRACE, "]": uinput.KEY_RIGHTBRACE,
    "\\": uinput.KEY_BACKSLASH, "`": uinput.KEY_GRAVE,
}

# Mouse-button name -> uinput code.
_BUTTON_MAP = {
    "left": uinput.BTN_LEFT,
    "right": uinput.BTN_RIGHT,
    "middle": uinput.BTN_MIDDLE,
}

_DEVICE: Optional["uinput.Device"] = None


_DEVICE_POINTER: Optional["uinput.Device"] = None
_DEVICE_KEYBOARD: Optional["uinput.Device"] = None


def _pointer() -> "uinput.Device":
    global _DEVICE_POINTER
    if _DEVICE_POINTER is None:
        try:
            _DEVICE_POINTER = uinput.Device(_POINTER_EVENTS, "kwin-mcp-pointer")
        except (PermissionError, OSError) as exc:
            raise RuntimeError(
                "Cannot open /dev/uinput for the virtual pointer. Are you a "
                "member of the 'input' group? (usermod -aG input $USER, then "
                f"re-login.) Underlying error: {exc}"
            ) from exc
    return _DEVICE_POINTER


def _keyboard() -> "uinput.Device":
    global _DEVICE_KEYBOARD
    if _DEVICE_KEYBOARD is None:
        try:
            _DEVICE_KEYBOARD = uinput.Device(
                _KEYBOARD_EVENTS, "kwin-mcp-keyboard"
            )
        except (PermissionError, OSError) as exc:
            raise RuntimeError(
                "Cannot open /dev/uinput for the virtual keyboard. Are you a "
                "member of the 'input' group? Underlying error: {exc}"
            ) from exc
    return _DEVICE_KEYBOARD


def _device() -> "uinput.Device":
    # Kept for backward compatibility; input functions now use the split
    # pointer/keyboard devices.
    return _pointer()


# Direct character -> (uinput KEY, needs_shift) map for a US keyboard layout.
# Anything not listed falls back to the a-z / 0-9 heuristic in _char_to_key.
_CHAR_TO_KEY = {
    " ": (uinput.KEY_SPACE, False),
    "\t": (uinput.KEY_TAB, False),
    "\n": (uinput.KEY_ENTER, False),
    "`": (uinput.KEY_GRAVE, False),
    "~": (uinput.KEY_GRAVE, True),
    "!": (uinput.KEY_1, True),
    "@": (uinput.KEY_2, True),
    "#": (uinput.KEY_3, True),
    "$": (uinput.KEY_4, True),
    "%": (uinput.KEY_5, True),
    "^": (uinput.KEY_6, True),
    "&": (uinput.KEY_7, True),
    "*": (uinput.KEY_8, True),
    "(": (uinput.KEY_9, True),
    ")": (uinput.KEY_0, True),
    "-": (uinput.KEY_MINUS, False),
    "_": (uinput.KEY_MINUS, True),
    "=": (uinput.KEY_EQUAL, False),
    "+": (uinput.KEY_EQUAL, True),
    "[": (uinput.KEY_LEFTBRACE, False),
    "{": (uinput.KEY_LEFTBRACE, True),
    "]": (uinput.KEY_RIGHTBRACE, False),
    "}": (uinput.KEY_RIGHTBRACE, True),
    "\\": (uinput.KEY_BACKSLASH, False),
    "|": (uinput.KEY_BACKSLASH, True),
    ";": (uinput.KEY_SEMICOLON, False),
    ":": (uinput.KEY_SEMICOLON, True),
    "'": (uinput.KEY_APOSTROPHE, False),
    "\"": (uinput.KEY_APOSTROPHE, True),
    ",": (uinput.KEY_COMMA, False),
    "<": (uinput.KEY_COMMA, True),
    ".": (uinput.KEY_DOT, False),
    ">": (uinput.KEY_DOT, True),
    "/": (uinput.KEY_SLASH, False),
    "?": (uinput.KEY_SLASH, True),
}


def _char_to_key(ch: str):
    """Return (uinput KEY code, needs_shift) for a single character, or None."""
    if ch in _CHAR_TO_KEY:
        return _CHAR_TO_KEY[ch]
    o = ord(ch)
    if 97 <= o <= 122:  # a-z
        return (getattr(uinput, f"KEY_{ch.upper()}"), False)
    if 65 <= o <= 90:   # A-Z
        return (getattr(uinput, f"KEY_{ch}"), True)
    if 48 <= o <= 57:   # 0-9
        return (getattr(uinput, f"KEY_{ch}"), False)
    return None


def move_cursor(x: int, y: int) -> None:
    """Move the virtual pointer to absolute screen coordinates (x, y).

    python-uinput only emits RELATIVE motion, and KWin applies mouse
    acceleration, so a single emit rarely lands on target. We loop: read the
    real cursor, emit a bounded relative delta toward the target, re-read, and
    repeat until within tolerance. This yields deterministic absolute moves.
    """
    dev = _pointer()
    for _ in range(_MOVE_MAX_ITERS):
        cur = get_cursor_position()
        dx = x - cur["x"]
        dy = y - cur["y"]
        if abs(dx) <= _MOVE_TOLERANCE and abs(dy) <= _MOVE_TOLERANCE:
            break
        step_x = max(-_MOVE_STEP, min(_MOVE_STEP, dx))
        step_y = max(-_MOVE_STEP, min(_MOVE_STEP, dy))
        dev.emit(uinput.REL_X, step_x)
        dev.emit(uinput.REL_Y, step_y)
        dev.syn()
        time.sleep(0.008)
    time.sleep(0.01)


def get_cursor_position() -> dict:
    try:
        out = subprocess_getmouselocation()
    except Exception:
        return {"x": 0, "y": 0, "screen": 0}
    data = {}
    for token in out.replace("\n", " ").split():
        if "=" in token:
            k, v = token.split("=", 1)
            try:
                data[k] = int(v)
            except ValueError:
                data[k] = v
    return {"x": data.get("X", 0), "y": data.get("Y", 0),
            "screen": data.get("SCREEN", 0)}


def _mouse_location() -> str:
    import subprocess
    from ._env import base_env
    proc = subprocess.run(
        ["kdotool", "getmouselocation", "--shell"],
        capture_output=True, text=True, timeout=10,
        env=base_env(), stdin=subprocess.DEVNULL,
    )
    return proc.stdout


# Bind for get_cursor_position without a circular import at module load.
subprocess_getmouselocation = _mouse_location


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    """Click at absolute screen coordinates (x, y)."""
    if button not in _BUTTON_MAP:
        raise ValueError(f"unknown button: {button!r}")
    move_cursor(x, y)
    time.sleep(0.03)
    dev = _pointer()
    btn = _BUTTON_MAP[button]
    dev.emit(btn, 1); dev.syn(); time.sleep(0.03)
    dev.emit(btn, 0); dev.syn()
    if double:
        time.sleep(0.06)
        dev.emit(btn, 1); dev.syn(); time.sleep(0.03)
        dev.emit(btn, 0); dev.syn()


def click_window(window_id: str, x: int, y: int, button: str = "left",
                 double: bool = False) -> None:
    """Focus a window, then click at window-local (x, y)."""
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    win = get_window(window_id)
    activate(window_id)
    time.sleep(0.25)
    click(win.x + x, win.y + y, button=button, double=double)


def drag(from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left",
         steps: int = 20) -> None:
    """Drag from (from_x, from_y) to (to_x, to_y) in screen coordinates."""
    if button not in _BUTTON_MAP:
        raise ValueError(f"unknown button: {button!r}")
    move_cursor(from_x, from_y)
    time.sleep(0.03)
    dev = _pointer()
    btn = _BUTTON_MAP[button]
    dev.emit(btn, 1); dev.syn(); time.sleep(0.03)
    # Closed-loop drag: step toward the target, re-reading the cursor so the
    # path actually follows the intended line despite acceleration.
    cur = get_cursor_position()
    cx, cy = cur["x"], cur["y"]
    remaining_x, remaining_y = to_x - cx, to_y - cy
    step = max(1, steps)
    for _ in range(step):
        if abs(remaining_x) <= _MOVE_TOLERANCE and abs(remaining_y) <= _MOVE_TOLERANCE:
            break
        sx = max(-_MOVE_STEP, min(_MOVE_STEP, remaining_x))
        sy = max(-_MOVE_STEP, min(_MOVE_STEP, remaining_y))
        dev.emit(uinput.REL_X, sx)
        dev.emit(uinput.REL_Y, sy)
        dev.syn()
        time.sleep(0.01)
        cur = get_cursor_position()
        remaining_x = to_x - cur["x"]
        remaining_y = to_y - cur["y"]
    # Final nudge to exact target.
    if remaining_x or remaining_y:
        dev.emit(uinput.REL_X, remaining_x)
        dev.emit(uinput.REL_Y, remaining_y)
        dev.syn()
        time.sleep(0.01)
    dev.emit(btn, 0); dev.syn()


def drag_window(window_id: str, from_x: int, from_y: int, to_x: int, to_y: int,
                button: str = "left", steps: int = 20) -> None:
    """Focus a window, then drag using window-local coordinates."""
    if not is_uuid(window_id):
        raise ValueError(f"not a valid KDE window UUID: {window_id!r}")
    win = get_window(window_id)
    activate(window_id)
    time.sleep(0.25)
    drag(win.x + from_x, win.y + from_y, win.x + to_x, win.y + to_y,
         button=button, steps=steps)


def type_text(text: str) -> dict:
    """Type a string as keyboard events at the current cursor focus.

    Returns a dict instead of None so callers can see what actually happened:
    ``typed`` (chars emitted), ``dropped`` (chars skipped because they have no
    mapping in the US-layout map and are not a-z/0-9), ``dropped_chars`` (the
    actual skipped characters) and ``had_shift``. This makes typing honest:
    previously any unsupported character was silently skipped, so a long string
    could be corrupted with zero signal.
    """
    dev = _keyboard()
    typed = 0
    dropped_chars: list = []
    for ch in text:
        result = _char_to_key(ch)
        if result is None:
            if ch.isspace():
                code, needs_shift = uinput.KEY_SPACE, False
            else:
                dropped_chars.append(ch)  # unsupported character; report it
                continue
        else:
            code, needs_shift = result
        if needs_shift:
            dev.emit(uinput.KEY_LEFTSHIFT, 1)
        dev.emit(code, 1); dev.syn(); time.sleep(0.006)
        dev.emit(code, 0); dev.syn(); time.sleep(0.006)
        if needs_shift:
            dev.emit(uinput.KEY_LEFTSHIFT, 0)
        typed += 1
    return {
        "typed": typed,
        "dropped": len(dropped_chars),
        "dropped_chars": dropped_chars,
        "requested": len(text),
        "ok": typed > 0 or len(text) == 0,
    }


def paste(text: str) -> dict:
    """Paste text into the focused widget via the Wayland clipboard + Ctrl+V.

    Uses ``wl-copy`` to set the clipboard, then sends Ctrl+V through the
    virtual keyboard. This is far more reliable and much faster than
    char-by-char typing for long text (any unsupported character is preserved
    verbatim via the clipboard, and a single keystroke replaces thousands).
    Returns the clipboard length and delivery status.

    ``wl-copy`` keeps running to serve the clipboard until a reader consumes
    it, so we spawn it non-blocking (Popen) and do NOT wait for it to exit:
    waiting would deadlock if the Ctrl+V doesn't land (which happens when the
    target widget lacks real input focus). We give it a moment to own the
    clipboard, then send Ctrl+V.
    """
    import subprocess
    from ._env import base_env

    try:
        proc = subprocess.Popen(
            ["wl-copy"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=base_env(),
        )
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except FileNotFoundError:
        return {"ok": False, "error": "wl-clipboard (wl-copy) is not installed"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"wl-copy error: {exc}"}

    # Give the clipboard a moment to be owned, then paste.
    time.sleep(0.3)
    press_key("v", modifiers=["ctrl"])
    return {"ok": True, "chars": len(text), "method": "wl-copy + Ctrl+V"}


def press_key(key: str, modifiers: Optional[list] = None) -> None:
    """Press a single key, optionally with held modifiers (e.g. ['ctrl'])."""
    dev = _keyboard()
    mod_codes = []
    for m in (modifiers or []):
        mc = _KEY_NAME_MAP.get(m.lower())
        if mc is None:
            raise ValueError(f"unknown modifier: {m!r}")
        mod_codes.append(mc)
    for mc in mod_codes:
        dev.emit(mc, 1)
    code = _KEY_NAME_MAP.get(key.lower())
    if code is None:
        res = _char_to_key(key)
        if res is not None:
            code, _ = res
    if code is None:
        raise ValueError(f"unknown key: {key!r}")
    dev.emit(code, 1); dev.syn(); time.sleep(0.03)
    dev.emit(code, 0); dev.syn()
    for mc in reversed(mod_codes):
        dev.emit(mc, 0)
    time.sleep(0.01)


def scroll(direction: str = "down", amount: int = 3) -> None:
    """Scroll the wheel. direction: 'up' or 'down'."""
    dev = _pointer()
    value = amount if direction == "down" else -amount
    for _ in range(amount):
        dev.emit(uinput.REL_WHEEL, value)
        dev.syn()
        time.sleep(0.01)


def close_device() -> None:
    global _DEVICE, _DEVICE_POINTER, _DEVICE_KEYBOARD
    for dev in (_DEVICE_POINTER, _DEVICE_KEYBOARD, _DEVICE):
        if dev is not None:
            try:
                dev.destroy()
            except Exception:
                pass
    _DEVICE = None
    _DEVICE_POINTER = None
    _DEVICE_KEYBOARD = None
