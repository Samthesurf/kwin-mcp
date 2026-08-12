#!/usr/bin/env bash
# Build kwin-mcp-server and publish it to PyPI.
#
# Credentials - provide a PyPI API token one of two ways:
#   1. ~/.pypirc  (twine's standard config), e.g.
#        [pypi]
#        username = __token__
#        password = pypi-AgEI...
#   2. $PYPI_TOKEN (shell env var) - this script writes a temp .pypirc from it.
#
# Usage:
#   ./scripts/publish.sh            # build + upload to PyPI
#   ./scripts/publish.sh --testpypi # build + upload to Test PyPI first
#
# Set PYTHON to a python that has `build` and `twine` installed if they are not
# on the default python3 (e.g. PYTHON=.venv/bin/python).
set -euo pipefail
cd "$(dirname "$0")/.."

PYBIN="${PYTHON:-$(command -v python3)}"

echo ">> Building sdist + wheel..."
rm -rf build dist
"$PYBIN" -m build

if [ "${1:-}" = "--testpypi" ]; then
  REPO="testpypi"
else
  REPO="pypi"
fi

PYPIRC_TMP=""
if [ -n "${PYPI_TOKEN:-}" ]; then
  PYPIRC_TMP="$(mktemp)"
  cat > "$PYPIRC_TMP" <<EOF
[$REPO]
username = __token__
password = $PYPI_TOKEN
EOF
  echo ">> Using \$PYPI_TOKEN for provider ${REPO}."
elif [ ! -f "$HOME/.pypirc" ]; then
  echo "ERROR: no PyPI token found. Set \$PYPI_TOKEN or create ~/.pypirc."
  echo "  Create one at https://pypi.org/manage/account/token/"
  exit 1
fi

echo ">> Uploading to ${REPO}..."
if [ -n "$PYPIRC_TMP" ]; then
  "$PYBIN" -m twine upload --config-file "$PYPIRC_TMP" --repository "$REPO" dist/*
  rm -f "$PYPIRC_TMP"
else
  "$PYBIN" -m twine upload --repository "$REPO" dist/*
fi

echo
echo "Done. Install it anywhere with:"
echo "  pipx install kwin-mcp-server"
