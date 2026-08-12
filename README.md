# kwin-mcp

An MCP (Model Context Protocol) server that controls **native Wayland windows
on KDE Plasma** from an AI agent.

It does what `cua-driver` cannot on Linux/Wayland: see and drive the real
desktop. `cua-driver` (trycua) only enumerates X11/XWayland clients, so on a
KDE Wayland session it sees 1 of ~20 windows. `kwin-mcp` sees all of them.

It is built entirely on KDE-native primitives, so it needs no modifications to
trycua's binary and no root daemon. You point your MCP client (Claude Code,
Codex, Hermes, etc.) at `server.py` and get the same capabilities cua offers on
X11: window listing, screenshots, clicks, typing, dragging, key presses, and
(optionally) AT-SPI element targeting.

---

## Install

kwin-mcp targets **KDE Plasma on Wayland**. It is a Python MCP server, so any
MCP host (Claude Code, Codex, Cursor, Zed, Hermes) can use it.

### One command (recommended)

`kwin-mcp-server` is published to PyPI, so install is a single command with no
git clone and no build:

```bash
pipx install kwin-mcp-server        # or: uv tool install kwin-mcp-server
kwin-mcp --doctor                    # print the readiness report
kwin-mcp                              # start the stdio MCP server
```

Then give it system access and wire it into your agent (both shown below).

### System deps + input permission (one-time)

```bash
sudo pacman -S kdotool spectacle                 # Arch
sudo usermod -aG input "$USER"                   # allow /dev/uinput
# log out and back in so the new group applies
```

Not on Arch? See the Dependencies table below for the per-distro package names.

### Wire it into your agent

Any MCP host can point at the `kwin-mcp` command. Use setup.sh for the
convenience of auto-wiring your agent's config (it preflights, prints exactly
what is missing, and never half-wires):

```bash
git clone https://github.com/Samthesurf/kwin-mcp.git /tmp/kwin-mcp && cd /tmp/kwin-mcp
./setup.sh hermes     # or: claude | codex | cursor | zed
```

Or wire it manually by running the `kwin-mcp` command in your agent's MCP
config. `./setup.sh verify` launches the real server and confirms it reports
ready; `./setup.sh help` prints usage; `./setup.sh check` runs only the
preflight.

### Manual run (no agent)

```bash
kwin-mcp              # stdio MCP server
kwin-mcp --doctor     # readiness report
kwin-mcp --check      # dependency preflight
```

---

## What it can do

| Tool | Purpose |
|------|---------|
| `list_windows` | Enumerate **every** top-level window (native Wayland + XWayland), with UUID, title, class, pid, geometry |
| `active_window` | Return the currently focused window |
| `capture` | Screenshot the desktop (`mode=desktop`) or a specific window (`mode=window`, `window_id=...`); crops to exact window bounds |
| `click` / `double_click` | Click at screen or window-local coordinates, OR target an element by `element_index` or semantic `role`/`name`/`text` |
| `drag` | Drag between two points (screen or window-local) |
| `type` | Type a string into the focused target |
| `press_key` | Press a key, optionally with modifiers (e.g. `["ctrl"]`) |
| `scroll` | Scroll the wheel up/down |
| `get_window_state` | AT-SPI accessibility tree for a window (index, role, name, bounds, state flags, actions, editable) |
| `click_element` | Click an AT-SPI element by index |
| `perform_action` | Invoke any AT-SPI action on an element (press, activate, toggle, ...) |
| `set_value` | Write a value to a settable element (text fields, sliders, spinners) |
| `activate` / `raise` / `minimize` / `close_window` | Window management |
| `get_cursor_position` | Current pointer location |
| `health` | Environment/dependency diagnostics |
| `doctor` | One JSON readiness report (platform, windowing, input, AT-SPI, screenshot, portals, blockers) |

Windows are identified by a stable KDE window UUID of the form
`{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}` (exactly what `kdotool` prints).

---

## Readiness report (`doctor`) and safety contract

### `doctor` / `kwin-mcp-doctor`

Run `kwin-mcp --doctor` (or `kwin-mcp-doctor`) to get a single structured JSON
document describing the desktop, the windowing backend (with a *live* window
list probe), the input path, AT-SPI, the screenshot path, and XDG portal
availability. It ends with a `readiness` summary carrying explicit `blockers`
and a `recommended_next_step`, so an MCP host or a human can render one report
instead of parsing prose:

```bash
kwin-mcp --doctor | jq .readiness
```

The same report is exposed as the `doctor` MCP tool.

### MCP safety annotations

Since v0.2 every tool carries an MCP `ToolAnnotations` so hosts can warn before
invoking a mutating tool:

| Class | Tools | Contract |
|-------|-------|----------|
| Read-only observation | `list_windows`, `active_window`, `get_window_state`, `get_cursor_position`, `health`, `doctor` | `readOnlyHint=true` |
| UI-state mutators | `capture`, `activate`, `raise_window`, `minimize`, `scroll` | `readOnlyHint=false`, `destructiveHint=false` |
| Desktop-action mutators | `click`, `click_element`, `drag`, `type_text`, `press_key`, `perform_action`, `set_value`, `close_window` | `destructiveHint=true` (+ `openWorldHint=true`) |

Annotations are safety hints, not an authorization system. Treat any call that
could submit, delete, send, or purchase as requiring user approval.

## Dependencies

### System packages (must be installed on the machine)
These are the KDE/Wayland tools the server shells out to. Install with your
distro's package manager.

| Tool | Package (Arch) | Package (Debian/Ubuntu) | Used for |
|------|----------------|--------------------------|----------|
| `kdotool` | `kdotool` (AUR) | `kdotool` (build from source) | Window enumeration, geometry, focus |
| `spectacle` | `spectacle` | `kde-spectacle` | Screen capture |
| `ydotool` | `ydotool` | `ydotool` | (Optional) alternative input backend reference |
| `grim` | `grim` | `grim` | (Optional) future per-output capture |

On Arch this machine already had `kdotool`, `spectacle`, `grim`, `ydotool`,
`slurp`, and `busctl` available.

### Kernel / group requirements (input)
Synthetic input is sent through a virtual device on `/dev/uinput`. You must:

1. Be a member of the `input` group:
   ```bash
   groups | grep -w input || sudo usermod -aG input "$USER"
   # then log out and back in
   ```
2. Have write access to `/dev/uinput` (group `input` owns it:
   `crw-rw---- root input`). No root daemon (`ydotoold`) is required because
   `python-uinput` opens the device directly as a group member.

Verify with:
```bash
ls -l /dev/uinput          # should show group 'input' with rw
id -nG | tr ' ' '\n' | grep -x input   # should print 'input'
```

### Python packages
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# AT-SPI element/action/value targeting + semantic clicks work out of the box:
# kwin-mcp talks to AT-SPI directly over D-Bus via jeepney (already a
# dependency), so no pyatspi is required. On distros where the legacy pyatspi
# module happens to be installed, it is used as a fallback backend.
```

Installed and verified on this build: `mcp 1.28.1`, `python-uinput 1.0.1`,
`Pillow 12.3.0` (Python 3.14).

---

## Running

```bash
. .venv/bin/activate

# dependency preflight (also run automatically by setup.sh)
python server.py --check

# JSON readiness report
python server.py --doctor

# stdio MCP server (for Claude/Codex/Hermes MCP clients)
python server.py

# or via the convenience wrapper
python run.py

# Streamable HTTP transport on 127.0.0.1:8080
python server.py --http 8080
```

The smoothest path, however, is the one-command `uvx` setup described in the
next section, which needs no local venv at all.

### Wiring into an MCP client (one command)

The recommended way is `uvx`, the Python equivalent of `npx`: it downloads and
runs the server on first use, then caches it. No clone, no venv, no manual
install. After `uvx` runs once, the agent just launches
`uvx --from git+https://github.com/Samthesurf/kwin-mcp kwin-mcp`.

**Automatic (recommended):** run the setup script, which checks dependencies
and injects the correct config into your agent.

```bash
git clone https://github.com/Samthesurf/kwin-mcp.git /tmp/kwin-mcp && cd /tmp/kwin-mcp
./setup.sh hermes      # or: claude | codex | cursor | zed | check
```

`setup.sh` runs a preflight first. If a system dependency is missing it prints
exactly what to install (e.g. `sudo pacman -S kdotool`) and stops, so you never
end up with a half-wired, broken server. If all checks pass it writes the
`uvx --from ... kwin-mcp` entry into the chosen agent's config.

**Manual:** point the client at the `uvx` launcher. Example
(`mcp-config.example.json`):

```json
{
  "mcpServers": {
    "kwin-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Samthesurf/kwin-mcp", "kwin-mcp"]
    }
  }
}
```

- **Hermes**: `./setup.sh hermes` writes it under `mcp_servers` in
  `~/.hermes/config.yaml`, or paste the JSON there. Restart Hermes to load it.
  This replaces `cua-driver` for the `computer_use` toolset on a Wayland box.
- **Claude Code**: `claude mcp add kwin-mcp -- uvx --from git+https://github.com/Samthesurf/kwin-mcp kwin-mcp`
- **Codex / Cursor / Zed**: `./setup.sh codex|cursor|zed`, or paste the JSON
  into their MCP config file.

The server is self-sufficient about its environment: when an MCP client does
not forward `DBUS_SESSION_BUS_ADDRESS` / `WAYLAND_DISPLAY` / `DISPLAY` /
`XDG_RUNTIME_DIR`, the server discovers the correct session values from
`/run/user/<uid>/` so `kdotool` and `spectacle` always work.

No API keys, no network calls, no cloud. Everything runs locally against your
compositor.

### Running from a local checkout (alternative)

If you prefer a local venv instead of `uvx`:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python server.py            # stdio MCP server
python server.py --check   # dependency preflight
```

---

## How it works (and the Wayland caveats)

On Wayland there is no X server between apps and the compositor, so input
cannot be injected "into a specific window" the way cua-driver does on X11.
The bridge follows a **focus-then-inject** model:

1. `kdotool windowactivate <uuid>` raises and focuses the target window.
2. The virtual pointer (a `python-uinput` device) is moved to the target
   coordinate. Because the compositor applies mouse acceleration and uinput
   only emits *relative* motion, movement is **closed-loop**: read the real
   cursor, emit a bounded delta, re-read, repeat until within ~3 px. This makes
   absolute positioning deterministic.
3. The click / key / drag is emitted on the now-focused window.

What this costs versus X11 (inherent to Wayland, not a bug):

- **No background targeting.** The window must be focused first; the real
  cursor moves. It is not invisible the way background X11 input can be.
- **Single cursor.** Parallel multi-pointer drags (cua's `parallel_mouse_drag`)
  are not available on Wayland.
- **Secure-input surfaces** (some password fields, the lock screen) may reject
  synthetic input.
- **Small focus race.** Between focusing and injecting there is a brief window
  where focus could shift; the code waits ~250 ms after activation.

Screenshots use `spectacle` in background/non-interactive mode. On KDE Wayland
`--background` can occasionally race the compositor and capture the lock-screen
splash instead of the live desktop; the capture path adds a settle delay and a
variance-based validation that retries up to 3 times, so the returned frame is
always the real desktop.

AT-SPI (`get_window_state`, `click_element`, `perform_action`, `set_value`,
semantic clicks) works for GTK/Qt/KDE apps that expose an accessibility tree.
It talks to AT-SPI directly over D-Bus (via `jeepney`, a pure-Python client),
so it needs no `pyatspi` and works on Arch; the legacy `pyatspi` module is used
only as a fallback if present. It degrades gracefully to coordinate input when
no AT-SPI backend is available.

---

## Project layout

```
kwin-mcp/
├── server.py              # MCP server (FastMCP) exposing all tools
├── run.py                 # convenience entry point
├── requirements.txt
├── pyproject.toml
├── mcp-config.example.json
├── README.md
└── kwin_bridge/
    ├── __init__.py
    ├── windows.py         # kdotool wrapper: enumerate/geometry/focus/close
    ├── screenshot.py      # spectacle wrapper + crop + retry/validate
    ├── input.py           # /dev/uinput virtual pointer+keyboard, closed-loop move
    ├── a11y.py            # AT-SPI front-end (semantic resolve / action / value)
    ├── atspi_dbus.py      # pure-D-Bus AT-SPI backend (jeepney, no pyatspi)
    ├── doctor.py          # structured JSON readiness report
    └── preflight.py       # actionable dependency check
```

---

## Testing

A quick smoke test against the live desktop:

```bash
. .venv/bin/activate
python - <<'PY'
from kwin_bridge import windows, screenshot, input as inp
ws = windows.list_windows()
print("windows:", len(ws))
wid = ws[0].window_id
print("capturing", wid)
p = screenshot.capture_window(wid, "/tmp/test.png")
print("shot:", p)
inp.click_window(wid, 100, 100)
inp.type_text("hello from kwin-mcp")
PY
```

---

## License

MIT. Use it, fork it, ship it.
