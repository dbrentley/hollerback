#!/usr/bin/env bash
# hollerback — broker installer.
#
# Stands up the message bus your sessions talk through. Nobody hosts this for
# you: it is a small local service, and everything your sessions say to each
# other (including whole files) passes through it. Run it on a machine you
# control.
#
#   # one machine, several sessions -- the common case, no network at all
#   curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/install-broker.sh | bash
#
#   # several machines -- bind the private address peers reach you on
#   ./install-broker.sh --bind 100.88.173.55
#
# There is NO AUTHENTICATION. Bind loopback, or a private overlay network
# (Tailscale/WireGuard/VPN). Never bind 0.0.0.0 on a network you don't trust.
set -uo pipefail

REPO="${HOLLERBACK_REPO:-dbrentley/hollerback}"   # <-- set to your fork
REF="${HOLLERBACK_REF:-main}"
BIND="127.0.0.1"
PORT="8850"
PREFIX="$HOME/.local/share/hollerback"
SRC=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bind)   BIND="${2:-}"; shift 2 ;;
    --port)   PORT="${2:-}"; shift 2 ;;
    --source) SRC="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "==> hollerback broker -> http://$BIND:$PORT"

PY=""
for c in python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -z "$PY" ] && { echo "    need Python 3.11+" >&2; exit 1; }
echo "    python: $PY"

# --- source: local checkout if we're in one, else fetch the repo ------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SRC" ]; then
  :
elif [ -n "$HERE" ] && [ -f "$HERE/broker/pyproject.toml" ]; then
  SRC="$HERE"
  echo "    source: local checkout at $SRC"
else
  TMP="$(mktemp -d)"
  URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF"
  echo "    source: downloading $REPO@$REF"
  if ! curl -fsSL "$URL" | tar xz -C "$TMP" --strip-components=1; then
    echo "    could not download $URL" >&2
    echo "    Set HOLLERBACK_REPO=you/hollerback, or run this from a clone." >&2
    exit 1
  fi
  SRC="$TMP"
fi
[ -f "$SRC/broker/pyproject.toml" ] || { echo "    $SRC is not a hollerback checkout" >&2; exit 1; }

# --- install into its own prefix, independent of the checkout ---------------
mkdir -p "$PREFIX"
rm -rf "$PREFIX/broker"
cp -R "$SRC/broker" "$PREFIX/broker"
rm -rf "$PREFIX/broker/.venv"
# plugin.zip + the installers are served from here, so they must travel too
for f in plugin install.sh install-windows.ps1 uninstall.sh uninstall-windows.ps1; do
  [ -e "$SRC/$f" ] && cp -R "$SRC/$f" "$PREFIX/"
done
echo "    installed to $PREFIX"

if command -v uv >/dev/null 2>&1; then
  (cd "$PREFIX/broker" && uv sync --quiet) || { echo "    uv sync failed" >&2; exit 1; }
  VENV_PY="$PREFIX/broker/.venv/bin/python"
else
  "$PY" -m venv "$PREFIX/broker/.venv" || exit 1
  VENV_PY="$PREFIX/broker/.venv/bin/python"
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet "starlette==0.42.0" "uvicorn==0.34.0" || exit 1
fi
echo "    dependencies ready"

# --- config -----------------------------------------------------------------
mkdir -p "$HOME/.config/hollerback"
ENVF="$HOME/.config/hollerback/broker.env"
cat > "$ENVF" <<EOF
# hollerback broker. There is no auth -- the bind address IS the boundary.
HOLLERBACK_BIND=$BIND
HOLLERBACK_PORT=$PORT
HOLLERBACK_TOKEN=
HOLLERBACK_BACKLOG_MAX=200
HOLLERBACK_KEEPALIVE_SECS=20
HOLLERBACK_PLUGIN_DIR=$PREFIX/plugin
EOF
echo "    config: $ENVF"

# --- run it -----------------------------------------------------------------
STARTED=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/hollerback-broker.service" <<EOF
[Unit]
Description=hollerback broker (message bus between Claude Code sessions)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PREFIX/broker
EnvironmentFile=$ENVF
ExecStart=$VENV_PY -m hollerback_broker.app
Restart=always
RestartSec=3
# SSE streams never end, so a graceful stop would hang forever.
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=5
FinalKillSignal=SIGKILL

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now hollerback-broker.service >/dev/null 2>&1 && STARTED=1
  # survive logout / start at boot
  command -v loginctl >/dev/null 2>&1 && loginctl enable-linger "$USER" >/dev/null 2>&1
  sleep 2
fi

if [ "$STARTED" = "1" ] && curl -fsS --max-time 5 "http://$BIND:$PORT/v1/health" >/dev/null 2>&1; then
  echo "    running as a systemd user service (survives logout and reboot)"
else
  cat <<EOF
    systemd not available -- start it yourself:
        set -a; . $ENVF; set +a
        $VENV_PY -m hollerback_broker.app
    (macOS: wrap that in a launchd plist, or just run it in a terminal.)
EOF
fi

cat <<EOF

Broker:    http://$BIND:$PORT
Dashboard: http://$BIND:$PORT/          <- who is on the network

Now install the plugin in each session's machine:

    curl -fsSL http://$BIND:$PORT/install.sh | bash -s -- --agent backend
    # Windows:
    #   iwr http://$BIND:$PORT/install.ps1 -OutFile \$env:TEMP\\hb.ps1
    #   powershell -ExecutionPolicy Bypass -File \$env:TEMP\\hb.ps1 -AgentName frontend

Then START A NEW SESSION in each (not /reload-plugins), in a TRUSTED workspace.
EOF
