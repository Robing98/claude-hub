#!/bin/sh
# Installs or updates the collector inside WSL. Run from the repository
# folder, or use "hub wsl-setup" on Windows.
set -eu

VENV="$HOME/.claude-hub-venv"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/claude-hub/collector.toml"

if [ ! -f pyproject.toml ]; then
    echo "Run this script from the claude-hub repository folder." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or later is required. Found: $(python3 --version 2>&1)" >&2
    exit 1
fi
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    echo "== Install the Python venv module (asks for your sudo password)"
    sudo apt-get update -q
    sudo apt-get install -y -q python3-venv
fi

echo "== Install the collector"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check "$(pwd)"
# The version number does not change with every update, so force the new code in.
"$VENV/bin/pip" install --quiet --disable-pip-version-check --force-reinstall --no-deps "$(pwd)"

if [ ! -f "$CONFIG" ]; then
    "$VENV/bin/claude-hub-collector" init
    echo
    echo "Next:"
    echo "  1. Create a token for this WSL installation: hub token NAME"
    echo "  2. Put server_url and the token into the file: hub wsl-config"
    echo "  3. Test it: hub wsl-check"
else
    echo "Updated. The configuration in $CONFIG was kept."
fi
