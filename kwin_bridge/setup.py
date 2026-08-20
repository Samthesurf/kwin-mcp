"""One-command setup for kwin-mcp (installed as `kwin-mcp setup`).

Replaces the old `git clone && ./setup.sh hermes` flow. After
`pipx install kwin-mcp-server` a newcomer just runs:

    kwin-mcp setup hermes   # or: claude | codex | cursor | vscode | opencode | openclaw | antigravity | pi | zed

No clone, no venv, no uvx needed. When the package is installed the
wired command is simply `kwin-mcp` (the installed console script).

A `--uvx` flag is available for the curl-pipe use-case where the
package is NOT installed and the agent should launch via uvx:
    uvx will fetch git+https://github.com/Samthesurf/kwin-mcp on first run.

The script preflights first (kdotool, spectacle, /dev/uinput). If
anything is missing it prints exactly what to install and refuses to
half-wire. This mirrors setup.sh behaviour.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = "git+https://github.com/Samthesurf/kwin-mcp"

# ---------------------------------------------------------------------------
# Supported agents and their config locations
# ---------------------------------------------------------------------------
# Each entry maps agent name -> { description, config_paths, kind }
# kind: "json" (mcpServers at top level), "yaml_hermes", "toml_codex", "json_vscode", etc.
# For multi-path agents (VS Code) we try each and wire the first that exists,
# otherwise the first path is created.

SUPPORTED = {
    "hermes": {
        "desc": "Hermes Agent (~/.hermes/config.yaml, mcp_servers)",
        "kind": "yaml_hermes",
        "paths": [Path.home() / ".hermes" / "config.yaml"],
    },
    "claude": {
        "desc": "Claude Code (~/.claude.json)",
        "kind": "json_mcpServers",
        "paths": [Path.home() / ".claude.json"],
    },
    "codex": {
        "desc": "Codex (~/.codex/config.toml)",
        "kind": "toml_codex",
        "paths": [Path.home() / ".codex" / "config.toml"],
    },
    "cursor": {
        "desc": "Cursor (~/.cursor/mcp.json)",
        "kind": "json_mcpServers",
        "paths": [Path.home() / ".cursor" / "mcp.json"],
    },
    "vscode": {
        "desc": "VS Code (global settings.json + workspace .vscode/mcp.json)",
        "kind": "json_vscode",
        "paths": [
            Path.home() / ".config" / "Code" / "User" / "settings.json",
            Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json",
            Path.home() / ".vscode" / "mcp.json",
        ],
    },
    "opencode": {
        "desc": "OpenCode (~/.config/opencode/opencode.json)",
        "kind": "json_mcpServers",
        "paths": [
            Path.home() / ".config" / "opencode" / "opencode.json",
            Path.home() / ".config" / "opencode" / "config.json",
        ],
    },
    "openclaw": {
        "desc": "OpenClaw (~/.openclaw/openclaw.json or ~/.config/openclaw/config.json)",
        "kind": "json_mcpServers",
        "paths": [
            Path.home() / ".openclaw" / "openclaw.json",
            Path.home() / ".config" / "openclaw" / "config.json",
            Path.home() / ".config" / "openclaw" / "settings.json",
        ],
    },
    "antigravity": {
        "desc": "Antigravity (Google) (~/.config/antigravity/mcp.json)",
        "kind": "json_mcpServers",
        "paths": [
            Path.home() / ".config" / "antigravity" / "mcp.json",
            Path.home() / ".config" / "antigravity" / "settings.json",
        ],
    },
    "pi": {
        "desc": "Pi agent (~/.pi/settings.json or ~/.config/pi/mcp.json)",
        "kind": "json_mcpServers",
        "paths": [
            Path.home() / ".pi" / "settings.json",
            Path.home() / ".config" / "pi" / "mcp.json",
            Path.home() / ".config" / "pi" / "settings.json",
        ],
    },
    "zed": {
        "desc": "Zed (~/.config/zed/settings.json, context_servers)",
        "kind": "json_zed",
        "paths": [Path.home() / ".config" / "zed" / "settings.json"],
    },
    "windsurf": {
        "desc": "Windsurf (~/.codeium/windsurf/mcp_config.json)",
        "kind": "json_mcpServers",
        "paths": [
            Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
        ],
    },
}


def _mcp_entry(use_uvx: bool) -> dict:
    if use_uvx:
        return {"command": "uvx", "args": ["--from", REPO, "kwin-mcp"]}
    return {"command": "kwin-mcp", "args": []}


def _hermes_entry(use_uvx: bool) -> str:
    """YAML block for hermes config (inserted under mcp_servers)."""
    if use_uvx:
        cmd, args = "uvx", f'["--from", "{REPO}", "kwin-mcp"]'
        # yaml-ish for args is handled in the python inserter below; we return a block string
        return (
            "  kwin-mcp:\n"
            "    command: uvx\n"
            "    args:\n"
            "      - --from\n"
            f"      - {REPO}\n"
            "      - kwin-mcp\n"
            "    env:\n"
            "      DBUS_SESSION_BUS_ADDRESS: ${DBUS_SESSION_BUS_ADDRESS}\n"
            "      DISPLAY: ${DISPLAY}\n"
            "      WAYLAND_DISPLAY: ${WAYLAND_DISPLAY}\n"
            "      XDG_SESSION_TYPE: ${XDG_SESSION_TYPE}\n"
            "    enabled: true\n\n"
        )
    return (
        "  kwin-mcp:\n"
        "    command: kwin-mcp\n"
        "    args: []\n"
        "    env:\n"
        "      DBUS_SESSION_BUS_ADDRESS: ${DBUS_SESSION_BUS_ADDRESS}\n"
        "      DISPLAY: ${DISPLAY}\n"
        "      WAYLAND_DISPLAY: ${WAYLAND_DISPLAY}\n"
        "      XDG_SESSION_TYPE: ${XDG_SESSION_TYPE}\n"
        "    enabled: true\n\n"
    )


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------

def _wire_json_mcpServers(path: Path, use_uvx: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            # strip comments for jsonc-ish files (opencode.jsonc)
            raw = path.read_text()
            # naive comment strip: remove // line comments before parse
            # only for jsonc; keep it simple
            if path.suffix == ".jsonc" or "opencode" in str(path):
                lines = []
                for line in raw.splitlines():
                    stripped = line.lstrip()
                    if stripped.startswith("//"):
                        continue
                    # remove trailing // comments (simple)
                    if "//" in line and line.count('"') % 2 == 0:
                        line = line.split("//", 1)[0]
                    lines.append(line)
                raw = "\n".join(lines)
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {}
    data.setdefault("mcpServers", {})["kwin-mcp"] = _mcp_entry(use_uvx)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path)


def _wire_json_zed(path: Path, use_uvx: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except Exception:
            data = {}
    data.setdefault("context_servers", {})["kwin-mcp"] = _mcp_entry(use_uvx)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path)


def _wire_json_vscode(path: Path, use_uvx: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except Exception:
            data = {}
    # VS Code supports "mcp.servers" (new) and "mcpServers" (compat).
    # We write the modern key and keep the old one in sync.
    entry = _mcp_entry(use_uvx)
    # settings.json style: mcp.servers
    if "mcp" not in data:
        data["mcp"] = {}
    if isinstance(data["mcp"], dict):
        data["mcp"].setdefault("servers", {})["kwin-mcp"] = entry
    # also ensure top-level mcpServers for older clients reading the same file
    data.setdefault("mcpServers", {})["kwin-mcp"] = entry
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path)


def _wire_yaml_hermes(path: Path, use_uvx: bool) -> str:
    if not path.exists():
        raise SystemExit(f"Hermes config not found at {path} (run hermes once to create it, then re-run setup).")
    text = path.read_text()
    if "kwin-mcp:" in text:
        return f"already present in {path} (skipped)"
    block = _hermes_entry(use_uvx)
    lines = text.splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        if not inserted and line.startswith("platform_toolsets:"):
            out.append(block)
            inserted = True
        out.append(line)
    if not inserted:
        out.append("\n" + block)
    path.write_text("".join(out))
    return str(path)


def _wire_toml_codex(path: Path, use_uvx: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and "kwin-mcp" in path.read_text():
        return f"already present in {path} (skipped)"
    entry = _mcp_entry(use_uvx)
    cmd = entry["command"]
    args = json.dumps(entry["args"])  # toml array as json is valid
    block = f'\n[mcp_servers.kwin-mcp]\ncommand = "{cmd}"\nargs = {args}\n'
    with open(path, "a") as f:
        f.write(block)
    return str(path)


def wire(agent: str, use_uvx: bool = False) -> str:
    agent = agent.lower()
    if agent not in SUPPORTED:
        raise SystemExit(f"Unknown agent '{agent}'. Supported: {', '.join(sorted(SUPPORTED))}")
    info = SUPPORTED[agent]
    kind = info["kind"]
    paths = info["paths"]

    # pick first existing path, otherwise first path will be created
    target = None
    for p in paths:
        if p.exists():
            target = p
            break
    if target is None:
        target = paths[0]

    if kind == "yaml_hermes":
        return _wire_yaml_hermes(target, use_uvx)
    if kind == "toml_codex":
        return _wire_toml_codex(target, use_uvx)
    if kind == "json_zed":
        return _wire_json_zed(target, use_uvx)
    if kind == "json_vscode":
        return _wire_json_vscode(target, use_uvx)
    # default json mcpServers
    return _wire_json_mcpServers(target, use_uvx)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HELP = """\
kwin-mcp setup - wire kwin-mcp into an MCP host (no clone needed)

Usage:
  kwin-mcp setup <agent> [--uvx]     wire into an agent's config
  kwin-mcp setup check               run dependency preflight only
  kwin-mcp setup verify              preflight + launch real server and confirm ready
  kwin-mcp setup list                list supported agents
  kwin-mcp setup --help              show this help

Agents:
  hermes       Hermes Agent          (~/.hermes/config.yaml)
  claude       Claude Code           (~/.claude.json)
  codex        Codex                 (~/.codex/config.toml)
  cursor       Cursor                (~/.cursor/mcp.json)
  vscode       VS Code               (~/.config/Code/User/settings.json)
  opencode     OpenCode              (~/.config/opencode/opencode.json)
  openclaw     OpenClaw              (~/.openclaw/openclaw.json)
  antigravity  Antigravity (Google)  (~/.config/antigravity/mcp.json)
  pi           Pi                    (~/.pi/settings.json)
  zed          Zed                   (~/.config/zed/settings.json)
  windsurf     Windsurf              (~/.codeium/windsurf/mcp_config.json)

Options:
  --uvx   wire as `uvx --from git+https://github.com/Samthesurf/kwin-mcp kwin-mcp`
          instead of the installed `kwin-mcp` binary. Use this when running
          via `curl ... | bash` without a pip install.

Examples:
  pipx install kwin-mcp-server
  kwin-mcp setup hermes
  kwin-mcp setup vscode
  kwin-mcp setup opencode
"""


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    # list
    if args[0] == "list":
        for name, info in sorted(SUPPORTED.items()):
            print(f"  {name:12s} {info['desc']}")
        return 0

    # check: preflight only
    if args[0] == "check":
        from .preflight import main as preflight_main
        return preflight_main()

    # verify: preflight + live doctor via real server
    if args[0] == "verify":
        from .preflight import run_checks
        from .doctor import run_doctor
        checks = run_checks()
        failed = [name for name, ok, _ in checks if not ok]
        if failed:
            print("Preflight failed for:", ", ".join(failed))
            print("Install the missing deps, then re-run: kwin-mcp setup verify")
            return 1
        print(">> Preflight passed. Launching the real server for a live check...")
        doc = run_doctor()
        ready = doc.get("readiness", {})
        if ready.get("ready"):
            print("OK: server starts and reports ready (windows enumerable).")
            if doc.get("accessibility", {}).get("backend") == "dbus":
                print("OK: AT-SPI semantic targeting available (D-Bus backend).")
            print("\nWire it into your agent with: kwin-mcp setup <agent>")
            return 0
        print("WARNING: server started but did NOT report ready.")
        print("  ready:", ready.get("ready"))
        for b in ready.get("blockers", []):
            print("  blocker:", b)
        print("  next:", ready.get("recommended_next_step"))
        return 1

    # wire an agent
    agent = args[0]
    use_uvx = "--uvx" in args

    # 1) preflight first (never half-wire)
    print(">> Running dependency preflight...")
    from .preflight import run_checks
    checks = run_checks()
    failed = [(n, hint) for n, ok, hint in checks if not ok]
    if failed:
        print("Preflight failed. Fix these, then re-run: kwin-mcp setup", agent)
        for name, _, hint in checks:
            mark = "OK" if not any(name == f[0] for f in failed) else "MISS"
            # re-print nicely
            pass
        # use the preflight's own pretty printer instead
        from .preflight import main as preflight_main
        preflight_main()
        return 1
    print("   preflight passed.")

    try:
        result = wire(agent, use_uvx=use_uvx)
    except SystemExit as e:
        print(e)
        return 1

    if "already present" in result:
        print(result)
    else:
        print(f"Wired kwin-mcp into {result}")
        print("Done. Restart/reload the agent to load kwin-mcp.")
        if agent == "hermes":
            print("  hermes: restart the gateway or reload the config")
        elif agent == "vscode":
            print("  vscode: reload window (Developer: Reload Window) or restart Code")
    return 0
