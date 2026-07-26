#!/usr/bin/env bash
#
# kwin-mcp one-command setup.
#
# Usage:
#   ./setup.sh hermes      # wire into ~/.hermes/config.yaml (mcp_servers)
#   ./setup.sh claude      # wire into ~/.claude.json
#   ./setup.sh codex       # wire into ~/.codex/config.toml
#   ./setup.sh cursor      # wire into ~/.cursor/mcp.json
#   ./setup.sh zed         # wire into ~/.config/zed/settings.json
#   ./setup.sh check       # just run the dependency preflight, no wiring
#
# What it does:
#   1. Runs a dependency preflight. If system deps are missing it prints
#      exactly what to install and stops (it will NOT silently half-wire).
#   2. Adds the kwin-mcp MCP server entry to the chosen agent's config so the
#      agent can launch it with `uvx` on first use (auto-download + run, like
#      npx). No manual clone or venv needed.
#
# The server is launched via `uvx --from git+https://github.com/Samthesurf/kwin-mcp
# kwin-mcp`. uvx fetches and caches the package on first run, then runs it.
#
set -euo pipefail

REPO="git+https://github.com/Samthesurf/kwin-mcp"
SERVER_ENTRY="uvx"
SERVER_ARGS=(--from "$REPO" kwin-mcp)

PYBIN="$(command -v python3 || command -v python)"
[ -n "$PYBIN" ] || { echo "python3 not found"; exit 1; }

action="${1:-hermes}"

# 1) Preflight -----------------------------------------------------------------
echo ">> Running dependency preflight..."
if ! "$PYBIN" preflight.py; then
  echo
  echo "Preflight failed. Install the missing dependencies above, then re-run:"
  echo "  ./setup.sh $action"
  exit 1
fi

# 2) Wire into the agent ---------------------------------------------------------
case "$action" in
  check)
    echo "Check complete; no wiring requested."
    exit 0
    ;;
  hermes)
    CONF="$HOME/.hermes/config.yaml"
    [ -f "$CONF" ] || { echo "Hermes config not found at $CONF"; exit 1; }
    # Inject the mcp_servers.kwin-mcp block if absent (idempotent).
    if grep -q "kwin-mcp:" "$CONF"; then
      echo "kwin-mcp already present in $CONF (skipping)."
    else
      # Insert before the 'platform_toolsets:' line if present, else append.
      BLOCK=$(cat <<'EOF'
  kwin-mcp:
    command: uvx
    args:
      - --from
      - git+https://github.com/Samthesurf/kwin-mcp
      - kwin-mcp
    env:
      DBUS_SESSION_BUS_ADDRESS: ${DBUS_SESSION_BUS_ADDRESS}
      DISPLAY: ${DISPLAY}
      WAYLAND_DISPLAY: ${WAYLAND_DISPLAY}
      XDG_SESSION_TYPE: ${XDG_SESSION_TYPE}
    enabled: true

EOF
)
      if grep -q "^platform_toolsets:" "$CONF"; then
        # Insert the block right before platform_toolsets, under mcp_servers.
        python3 - "$CONF" <<PY
import sys, re
p = sys.argv[1]
s = open(p).read()
block = r'''  kwin-mcp:
    command: uvx
    args:
      - --from
      - git+https://github.com/Samthesurf/kwin-mcp
      - kwin-mcp
    env:
      DBUS_SESSION_BUS_ADDRESS: \${DBUS_SESSION_BUS_ADDRESS}
      DISPLAY: \${DISPLAY}
      WAYLAND_DISPLAY: \${WAYLAND_DISPLAY}
      XDG_SESSION_TYPE: \${XDG_SESSION_TYPE}
    enabled: true

'''
        s = s.replace("platform_toolsets:", block + "platform_toolsets:", 1)
        open(p, "w").write(s)
        print("Wired kwin-mcp into", p)
PY
      else
        printf "%s" "$BLOCK" >> "$CONF"
        echo "Appended kwin-mcp to $CONF"
      fi
    fi
    echo "Done. Restart/refresh Hermes to load kwin-mcp."
    ;;

  claude)
    CONF="$HOME/.claude.json"
    python3 - "$CONF" "$REPO" <<'PY'
import sys, json
p, repo = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d.setdefault("mcpServers", {})["kwin-mcp"] = {
    "command": "uvx",
    "args": ["--from", repo, "kwin-mcp"],
}
json.dump(d, open(p, "w"), indent=2)
print("Wired kwin-mcp into", p)
PY
    echo "Done. Restart Claude Code to load kwin-mcp."
    ;;

  codex)
    CONF="$HOME/.codex/config.toml"
    python3 - "$CONF" "$REPO" <<'PY'
import sys
p, repo = sys.argv[1], sys.argv[2]
block = f'''
[mcp_servers.kwin-mcp]
command = "uvx"
args = ["--from", "{repo}", "kwin-mcp"]
'''
open(p, "a").write(block)
print("Appended kwin-mcp to", p)
PY
    echo "Done. Restart Codex to load kwin-mcp."
    ;;

  cursor)
    CONF="$HOME/.cursor/mcp.json"
    python3 - "$CONF" "$REPO" <<'PY'
import sys, json
p, repo = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d.setdefault("mcpServers", {})["kwin-mcp"] = {
    "command": "uvx",
    "args": ["--from", repo, "kwin-mcp"],
}
json.dump(d, open(p, "w"), indent=2)
print("Wired kwin-mcp into", p)
PY
    echo "Done. Restart Cursor to load kwin-mcp."
    ;;

  zed)
    CONF="$HOME/.config/zed/settings.json"
    python3 - "$CONF" "$REPO" <<'PY'
import sys, json
p, repo = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p))
except Exception:
    d = {}
d.setdefault("context_servers", {})["kwin-mcp"] = {
    "command": "uvx",
    "args": ["--from", repo, "kwin-mcp"],
}
json.dump(d, open(p, "w"), indent=2)
print("Wired kwin-mcp into", p)
PY
    echo "Done. Restart Zed to load kwin-mcp."
    ;;

  *)
    echo "Unknown target: $action"
    echo "Usage: ./setup.sh [hermes|claude|codex|cursor|zed|check]"
    exit 1
    ;;
esac
