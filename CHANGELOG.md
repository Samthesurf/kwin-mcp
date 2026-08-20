# Changelog

All notable changes to kwin-mcp.

## [0.5.0] - 2026-08-19

### Added
- **One-command agent wiring via the installed binary** (no clone, no venv, no
  uvx). After `pipx install kwin-mcp-server` a newcomer just runs
  `kwin-mcp setup <agent>` and the Console Script wires the agent's MCP config
  for them. New `kwin-mcp-setup` Console Script installed alongside `kwin-mcp`.
  - `kwin-mcp setup hermes` (or `claude`, `codex`, `cursor`, `vscode`,
    `opencode`, `openclaw`, `antigravity`, `pi`, `zed`, `windsurf`, ...):
    preflights first, then injects the `kwin-mcp` command entry into the chosen
    agent's config. `--uvx` is available for the curl-pipe use case where the
    package is not installed and the agent should launch via `uvx`.
  - `kwin-mcp setup list` shows supported agents;
    `kwin-mcp setup check` runs the preflight only;
    `kwin-mcp setup verify` preflights AND confirms the real server starts and
    reports ready.
  - `kwin_bridge/server.py` intercepts `setup` as a subcommand (and the
    `kwin-mcp-setup` binary) so it never collides with the server's CLI flags.
- Setup logic extracted into `kwin_bridge/setup.py` (404 lines) and shared across
  the binary and `setup.sh`; `setup.sh` is now a thin curl shim that points at the
  installed `kwin-mcp setup` workflow.

### Changed
- README rewritten into two install paths: **Path A** (pip install + `kwin-mcp
  setup`) and **Path B** (curl/uvx with no pip install). Preflight-first: a
  missing system dep prints the exact install command and stops, so config is
  never half-wired.

## [0.4.0] - 2026-08-19

### Added
- **Computer History** (port of Cua Driver's encrypted, metadata-only action
  history): an opt-in, off-by-default local record of what kwin-mcp did.
  - `history_status` (read-only): reports supported, enabled, paused, encrypted,
    retention/quota, bytes used, dropped events, and health. Never returns events.
  - `history_query` (read-only): bounded, metadata-only event slice (`limit`
    1..200, optional `session_id` / `since_sequence` / `until_sequence`). A
    successful read appends an encrypted access record that is not returned.
  - `history_control` (local only): `enable` / `disable` / `pause` / `resume` /
    `flush` / `delete` the encrypted store. Mirrors Cua's
    `history_control_requires_local_cli` (agents read, the local user owns capture).
  - Every event is a CloudEvents 1.0 envelope on
    `urn:kwin-mcp:schema:history-event:v0`, sealed with AES-256-GCM before it
    touches disk (no plaintext fallback). Records only fixed-field metadata: no
    screenshots, typed text, clipboard, raw tool args/results, a11y trees, window
    titles, URLs, or paths.
  - The 14 mutating/action tools (`click`, `drag`, `type_text`, `paste`,
    `press_key`, `scroll`, `click_element`, `perform_action`, `set_value`,
    `focus_element`, `activate`, `raise_window`, `minimize`, `close_window`) are
    wrapped so an `action_started` / `action_completed` pair is recorded (effect +
    route), nonblocking and never failing the action itself.
- **`cryptography` dependency** for AES-256-GCM at rest.

### Added (tests)
- `tests/test_history.py`: 14 tests mirroring Cua's computer-history contract
  (status/query contracts, the hard privacy boundary via `assert_no_private_fields`,
  encryption-at-rest with no plaintext on disk, bounded query limits/ordering,
  pause/resume/delete lifecycle, and decorator signature preservation).

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
