#!/usr/bin/env bash
# Legacy shim: delegates to the installed `kwin-mcp setup`.
# Prefer `kwin-mcp setup <agent>` after `pipx install kwin-mcp-server`.
# This shim exists so old README links and curl pipes keep working.
set -euo pipefail
if command -v kwin-mcp >/dev/null 2>&1; then
  exec kwin-mcp setup "$@"
fi
# Fallback: no installed binary, try uvx with the new setup entrypoint
if command -v uvx >/dev/null 2>&1; then
  exec uvx --from git+https://github.com/Samthesurf/kwin-mcp kwin-mcp setup "$@"
fi
echo "kwin-mcp not found. Install it first:" >&2
echo "  pipx install kwin-mcp-server   # then: kwin-mcp setup hermes" >&2
echo "or run without install:" >&2
echo "  curl -sSL https://raw.githubusercontent.com/Samthesurf/kwin-mcp/main/setup.sh | bash -s hermes" >&2
exit 1
