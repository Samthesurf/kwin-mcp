# Changelog

All notable changes to kwin-mcp.

## [0.3.0] - 2026-08-12

### Added
- **Keyboard-first semantic navigation** (new tools): `focus_element` moves
  AT-SPI focus directly via `Component.GrabFocus` (no pixel coordinates / no
  Tab-count guessing); `focused_element` reports which element owns focus;
  `keyboard_navigate` moves focus next/prev through the focusable elements and
  returns the `from` / `to` elements so a host can drive a whole workflow
  without screenshots or coordinates.
- **`paste` tool**: writes long or non-ASCII text via the Wayland clipboard
  (`wl-copy`) + Ctrl+V. A single keystroke replaces char-by-char typing, and
  unsupported characters (em dashes, curly quotes) are preserved verbatim.

### Fixed
- **`type_text` is now honest**: it returns `typed`, `dropped`, `dropped_chars`
  and `requested` instead of a bare `ok:True` with length. Unsupported
  characters are reported rather than silently skipped, so a corrupted string
  is no longer hidden.
- **Dropped leading characters** when typing right after establishing focus
  (observed: `KEYBOARD` -> `BOARD`): a short settle delay before the first
  keystroke lets the compositor route focus so the whole string lands.
- **`perform_action` crashed on the D-Bus backend** because `GetNActions`
  returns a type-tag-wrapped tuple (`('i', 2),`); added a `_norm_int` unwrapper
  used in both `_node_info` and `perform_action`. Actions now list correctly
  (e.g. `['Press', 'SetFocus']`) instead of crashing.
- **`keyboard_navigate` failed on apps that report `focusable=False`**
  (Kate marks everything non-focusable): it now falls back to interactive
  roles when no element reports the focusable state, and tracks the last
  position in memory because many Qt apps lie about the `focused` flag.
- **~4.5s window-resolution hot path**: `get_window()` re-enumerated the whole
  desktop (~96 kdotool subprocesses) on every call. Added a short TTL cache on
  `list_windows()`, invalidated by mutating operations, so repeated calls drop
  to ~0.000s and `get_window_state` goes from ~6s to ~0.02s after warmup.

### Changed
- Package version bumped to 0.3.0. README tool table updated with the new
  keyboard-first and paste tools.

## [0.2.0] - 2026-08-12

### Added
- **`doctor` readiness report** (`doctor` MCP tool, `kwin-mcp-doctor`,
  `server.py --doctor`): one structured JSON document covering platform,
  windowing (live window-list probe), input (/dev/uinput), AT-SPI,
  screenshot path, and XDG portal availability, with explicit blockers and a
  recommended next step. Mirrors agent-sh/computer-use-linux for easy host
  rendering.
- **Semantic AT-SPI targeting**: new `perform_action` and `set_value` tools,
  plus `click` now accepts `element_index` or semantic `role`/`name`/`text`
  selectors in addition to coordinates. `get_window_state` now returns state
  flags (focused, checked, enabled, selected...), available AT-SPI actions,
  and per-element `editable` / `center_x` / `center_y`.
- **Pure-D-Bus AT-SPI backend** (`kwin_bridge/atspi_dbus.py` via `jeepney`):
  talks to AT-SPI over the at-spi bus directly, so element/action/value
  targeting works on Arch where the legacy `pyatspi` module is no longer
  packaged (it is now only a fallback backend). Per-app results are cached and
  walks are bounded by a deadline + per-call timeout so slow AT-SPI bridges
  (we measured a Qt app at ~10s/20 elements) cannot hang a call.
- **MCP safety contract**: every tool now carries `ToolAnnotations` so hosts
  can surface `readOnly` vs `destructive` vs `openWorld` risk before calling.
- **Graceful disconnected-pipe exit**: an MCP client disconnecting mid-shutdown
  no longer prints a stack trace / crashes; the server exits cleanly.

### Fixed
- `get_window_state` / `click_element` / `perform_action` / `set_value` now
  share ONE element index numbering (interactive-only), fixing a latent bug
  where a listing's index did not match the action/value lookups.
- The FastMCP stdout writer no longer surfaces `BrokenPipeError` as an
  unhandled task-group error on client disconnect.
- D-Bus AT-SPI reads survive MCP clients that strip the session env: the
  at-spi socket is resolved from /run/user/<uid> independently of the
  environment, matching the bridge's existing env self-sufficiency.

### Changed
- Package version bumped to 0.2.0; added `jeepney` as a dependency, a
  `kwin-mcp-doctor` console script, and a `dev` extra (pytest / build / twine).

## [0.1.0] - 2026-07-26

- Initial release: window listing/focus/close via kdotool, screenshots via
  spectacle with crop + validation, click/type/drag/scroll via /dev/uinput
  with closed-loop positioning, optional AT-SPI element clicks, one-command
  `uvx` + `setup.sh` wiring for Hermes / Claude / Codex / Cursor / Zed.
