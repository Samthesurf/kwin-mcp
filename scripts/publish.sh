#!/usr/bin/env bash
# Build kwin-mcp and publish it to PyPI.
#
# Requires a PyPI API token. Provide it one of two ways:
#   1. ~/.pypirc  (twine's standard config) - example:
#        [pypi]
#        username = __token__
#        password = pypi-AgEIcHlwaS5vcmc...
#   2. $PYPI_TOKEN (shell env var) - the script writes a temp .pypirc from it.
#
# Usage:
#   ./scripts/publish.sh            # build + upload to PyPI
#   ./scripts/publish.sh --testpypi # build + upload to Test PyPI first
#
# After the first successful release, publish is just:
#   git tag v0.2.0 && git push --tags
# (the GitHub Actions workflow in .github/workflows/release.yml also publishes
#  via a trusted-publisher build when the repo is configured for it).
set -euo pipefail
cd "$(dirname "$0")/.."

PYBIN="${PYTHON:-$(command -v python3)}"
echo ">> Building sdist + wheel..."
rm -rf build dist
"$PYBIN" -m build

if [ "${1:-}" = "--testpypi" ]; then
  REPO="testpypi"; REPO_URL="https://test.pypi.org/legacy/"
else
  REPO="pypi"; REPO_URL="https://upload.pypi.org/legacy/"
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
  "$PYBIN" -m twine upload --config-file "$PYPIRC_TMP" --repository "$REPO" \
      --repository-url "$REPO_URL" dist/*
  rm -f "$PYPIRC_TMP"
else
  "$PYBIN" -m twine upload --repository "$REPO" --repository-url "$REPO_URL" dist/*
fi

echo
echo "Done. Install it anywhere with:"
echo "  pipx install $REPO_URL  # or once on PyPI:  pipx install kwin-mcp-server"
