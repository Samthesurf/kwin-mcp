# Changelog

All notable changes to kwin-mcp.

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
- **MCP safety contract**: every tool now carries `ToolAnnotations` so hosts
  can surface `readOnly` vs `destructive` vs `openWorld` risk before calling.
- **Graceful disconnected-pipe exit**: an MCP client disconnecting mid-shutdown
  no longer prints a stack trace / crashes; the server exits cleanly.

### Fixed
- The FastMCP stdout writer no longer surfaces `BrokenPipeError` as an
  unhandled task-group error on client disconnect.

### Changed
- Package version bumped to 0.2.0; added `kwin-mcp-doctor` console script and
  a `dev` extra (pytest / build / twine).

## [0.1.0] - 2026-07-26

- Initial release: window listing/focus/close via kdotool, screenshots via
  spectacle with crop + validation, click/type/drag/scroll via /dev/uinput
  with closed-loop positioning, optional AT-SPI element clicks, one-command
  `uvx` + `setup.sh` wiring for Hermes / Claude / Codex / Cursor / Zed.
