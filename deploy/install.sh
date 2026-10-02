#!/bin/sh
# Installs or updates the hub inside its container. Run as root.
# Safe to run again: every step checks what is already there.
set -eu

APP=/opt/claude-hub
SRC="$APP/src"
DATA=/var/lib/claude-hub
PORT=8787

if [ "$(id -u)" != "0" ]; then
    echo "Run this script as root." >&2
    exit 1
fi
if [ ! -f "$SRC/pyproject.toml" ]; then
    echo "No source in $SRC. Unpack the repository there first." >&2
    exit 1
fi

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
    echo "== Install Python"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q python3 python3-venv
fi

# The server runs as its own user, because transcripts can contain secrets.
if ! id hub >/dev/null 2>&1; then
    echo "== Create the service user"
    useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin hub
fi
mkdir -p "$DATA"
chown hub:hub "$DATA"
chmod 700 "$DATA"

echo "== Install the hub"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --disable-pip-version-check "$SRC[server]"
# The version number does not change with every deploy, so force the new code in.
"$APP/venv/bin/pip" install --quiet --disable-pip-version-check --force-reinstall --no-deps "$SRC"

# A wrapper, so that `claude-hub token add ...` uses the service's data and user.
cat > /usr/local/bin/claude-hub <<WRAPPER
#!/bin/sh
# "pct exec" starts commands with an almost empty PATH, so set it here.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
exec runuser -u hub -- env HUB_DATA_DIR=$DATA $APP/venv/bin/claude-hub "\$@"
WRAPPER
chmod 755 /usr/local/bin/claude-hub

echo "== Set up the service"
# Keep an existing settings file: it can hold the password of the web view.
[ -f /etc/claude-hub.env ] || install -m 600 "$SRC/deploy/claude-hub.env" /etc/claude-hub.env
install -m 644 "$SRC/deploy/claude-hub.service" /etc/systemd/system/claude-hub.service
systemctl daemon-reload
systemctl enable --quiet claude-hub
systemctl restart claude-hub

echo "== Check"
attempt=0
until python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=3)" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 10 ]; then
        echo "The hub did not start. Last log lines:" >&2
        journalctl -u claude-hub -n 30 --no-pager >&2 || true
        exit 1
    fi
    sleep 1
done

# After a parser change, bring stored sessions up to date. Without one this does nothing.
/usr/local/bin/claude-hub reparse --outdated

ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "The hub runs at http://${ADDRESS:-ADDRESS}:$PORT"
echo "Create a collector token with: /usr/local/bin/claude-hub token add MACHINE --user USER"
