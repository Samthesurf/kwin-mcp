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
    if grep -q "kwin-mcp:" "$CONF"; then
      echo "kwin-mcp already present in $CONF (skipping)."
    else
      export KWIN_REPO="$REPO"
      python3 - "$CONF" <<'PY'
import sys, io
p = sys.argv[1]
repo = __import__("os").environ["KWIN_REPO"]
s = open(p).read()
block = (
    "  kwin-mcp:\n"
    "    command: uvx\n"
    "    args:\n"
    "      - --from\n"
    "      - " + repo + "\n"
    "      - kwin-mcp\n"
    "    env:\n"
    "      DBUS_SESSION_BUS_ADDRESS: ${DBUS_SESSION_BUS_ADDRESS}\n"
    "      DISPLAY: ${DISPLAY}\n"
    "      WAYLAND_DISPLAY: ${WAYLAND_DISPLAY}\n"
    "      XDG_SESSION_TYPE: ${XDG_SESSION_TYPE}\n"
    "    enabled: true\n"
    "\n"
)
# Insert the server block right before the first top-level 'platform_toolsets:'
# line so it ends up inside the mcp_servers mapping.
lines = s.splitlines(keepends=True)
out = []
inserted = False
for line in lines:
    if not inserted and line.startswith("platform_toolsets:"):
        out.append(block)
        inserted = True
    out.append(line)
if not inserted:
    out.append("\n" + block)
open(p, "w").write("".join(out))
print("Wired kwin-mcp into", p)
PY
      echo "Done. Restart/refresh Hermes to load kwin-mcp."
    fi
    ;;
  claude)
    CONF="$HOME/.claude.json"
    export KWIN_REPO="$REPO"
    python3 - "$CONF" <<'PY'
import sys, json, os
p = sys.argv[1]
repo = os.environ["KWIN_REPO"]
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
    {
      echo ""
      echo "[mcp_servers.kwin-mcp]"
      echo "command = \"uvx\""
      echo "args = [\"--from\", \"$REPO\", \"kwin-mcp\"]"
    } >> "$CONF"
    echo "Appended kwin-mcp to $CONF"
    echo "Done. Restart Codex to load kwin-mcp."
    ;;
  cursor)
    CONF="$HOME/.cursor/mcp.json"
    export KWIN_REPO="$REPO"
    python3 - "$CONF" <<'PY'
import sys, json, os
p = sys.argv[1]
repo = os.environ["KWIN_REPO"]
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
    export KWIN_REPO="$REPO"
    python3 - "$CONF" <<'PY'
import sys, json, os
p = sys.argv[1]
repo = os.environ["KWIN_REPO"]
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
