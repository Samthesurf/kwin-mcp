#!/usr/bin/env bash
#
# kwin-mcp one-command setup.
#
# Usage (run from anywhere, not just the repo root):
#   ./setup.sh [hermes|claude|codex|cursor|zed|check|verify|help]
#     hermes   wire into ~/.hermes/config.yaml (mcp_servers)
#     claude   wire into ~/.claude.json
#     codex    wire into ~/.codex/config.toml
#     cursor   wire into ~/.cursor/mcp.json
#     zed      wire into ~/.config/zed/settings.json
#     check    just run the dependency preflight, no wiring
#     verify   preflight + launch the REAL server via uvx and confirm it
#              starts and reports ready (live smoke test)
#     help     print this usage text
#   With no argument it defaults to hermes.
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

# Make this script work no matter which directory it is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="git+https://github.com/Samthesurf/kwin-mcp"
PYBIN="$(command -v python3 || command -v python || true)"
if [ -z "$PYBIN" ]; then
  echo "python3 not found. Install Python 3 first, then re-run ./setup.sh."
  exit 1
fi

action="${1:-hermes}"

case "$action" in
  help|-h|--help)
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

# uv is required for the one-command wiring (agents launch the server via uvx).
if [ "$action" != "check" ] && ! (command -v uvx >/dev/null || command -v uv >/dev/null); then
  echo ">> uv is not installed. kwin-mcp's one-command setup launches the server"
  echo "   with uvx, so it needs uv. Install it with:"
  echo
  echo "     curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "   (or: python3 -m pip install --user uv)"
  echo
  echo "   Then re-run:  ./setup.sh $action"
  exit 1
fi

# 1) Preflight -----------------------------------------------------------------
echo ">> Running dependency preflight..."
if ! "$PYBIN" "$SCRIPT_DIR/preflight.py"; then
  echo
  echo "Preflight failed. Install the missing dependencies above, then re-run:"
  echo "  ./setup.sh $action"
  exit 1
fi


# 2) Wire into the agent ---------------------------------------------------------
case "$action" in
  verify)
    # Live smoke test: launch the REAL server the same way the agent will
    # (uvx --from ... kwin-mcp) and ask its doctor for a readiness report.
    # This proves uv + package build + python deps + system deps + the live
    # window listing all work, not just that the binaries exist.
    echo ">> Launching the real server via uvx for a live check..."
    echo "   (first run downloads and builds; this can take a minute)"
    TMP_OUT="$(mktemp /tmp/kwin-mcp-readiness.XXXXXX)"
    if ! uvx --from "$REPO" kwin-mcp --doctor >"$TMP_OUT" 2>&1; then
      echo "FAILED: the server could not be started. Output:"
      tail -n 25 "$TMP_OUT"
      rm -f "$TMP_OUT"
      exit 1
    fi
    if grep -q '"ready": true' "$TMP_OUT"; then
      echo "OK: the server starts and reports ready (windows enumerable)."
      if grep -q '"backend": "dbus"' "$TMP_OUT"; then
        echo "OK: AT-SPI semantic targeting is available (D-Bus backend)."
      fi
      rm -f "$TMP_OUT"
      echo
      echo "kwin-mcp is verified working. Now wire it into your agent with:"
      echo "  ./setup.sh <hermes|claude|codex|cursor|zed>"
      exit 0
    fi
    echo "WARNING: the server started but did NOT report ready. Readiness block:"
    python3 - "$TMP_OUT" <<'PY2'
import sys, json
d = json.load(open(sys.argv[1]))
r = d.get("readiness", {})
print("  ready:", r.get("ready"))
for b in r.get("blockers", []):
    print("  blocker:", b)
print("  next:", r.get("recommended_next_step"))
PY2
    rm -f "$TMP_OUT"
    exit 1
    ;;
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
    echo "Usage: ./setup.sh [hermes|claude|codex|cursor|zed|check|verify|help]"
    exit 1
    ;;
esac
