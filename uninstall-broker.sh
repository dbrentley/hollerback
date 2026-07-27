#!/usr/bin/env bash
# hollerback — broker uninstaller, the counterpart to install-broker.sh.
#
# Stops and removes the broker service, its config, and its installed copy.
#
#   curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall-broker.sh | bash
#   ./uninstall-broker.sh --purge-data    # ALSO delete every message and file
#
# By default the message history and received-file blobs in
# ~/.local/share/hollerback are KEPT, so reinstalling resumes where you left off.
# --purge-data deletes them: every question, answer, note, shared file and agent
# record, permanently. Nothing in hollerback expires, so that history can be the
# only remaining record of what two sessions agreed.
#
# This does NOT touch the plugin on this machine -- that is uninstall.sh.
set -uo pipefail

# Printed by --help. Held here rather than sed'd out of the comment header,
# because `curl ... | bash` leaves $0 as "bash" and there is no file to read.
usage() {
  cat <<'USAGEEOF'
  hollerback — remove the broker.

    curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall-broker.sh | bash

    --purge-data   ALSO delete every message, answer, note and shared file

  Stops and removes the service, its config and its installed code. Message
  history is KEPT by default, so reinstalling resumes where you left off; nothing
  in hollerback expires, so that history may be the only record of what two
  sessions agreed. Does not touch the plugin -- that is uninstall.sh.
USAGEEOF
}

PURGE_DATA=0
UNIT="hollerback-broker.service"
PREFIX="$HOME/.local/share/hollerback"
CONFIG="$HOME/.config/hollerback"
UNIT_FILE="$HOME/.config/systemd/user/$UNIT"

while [ $# -gt 0 ]; do
  case "$1" in
    --purge-data) PURGE_DATA=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "==> uninstalling the hollerback broker"
removed=0

# --- stop it first, or the port stays bound and files vanish under a live
#     process -- the same stale-process trap the installer used to fall into.
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  if systemctl --user list-unit-files "$UNIT" >/dev/null 2>&1; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 && {
      echo "    stopped and disabled $UNIT"; removed=1; }
  fi
  if [ -f "$UNIT_FILE" ]; then
    rm -f "$UNIT_FILE"; echo "    removed $UNIT_FILE"; removed=1
  fi
  systemctl --user daemon-reload 2>/dev/null || true
fi

# Anything started by hand rather than by systemd. Match the module path, never
# a bare name -- a broad pattern here would take out unrelated processes.
if pgrep -f "hollerback_broker.app" >/dev/null 2>&1; then
  echo "    stopping processes still running hollerback_broker.app"
  pkill -f "hollerback_broker.app" 2>/dev/null || true
  sleep 1
  # SSE streams are infinite generators, so a graceful stop can hang forever.
  pkill -9 -f "hollerback_broker.app" 2>/dev/null || true
  removed=1
fi

# --- config -----------------------------------------------------------------
if [ -d "$CONFIG" ]; then
  rm -rf "$CONFIG"; echo "    removed $CONFIG"; removed=1
fi

# --- installed code, and optionally the data -------------------------------
if [ -d "$PREFIX" ]; then
  if [ "$PURGE_DATA" = "1" ]; then
    rm -rf "$PREFIX"
    echo "    removed $PREFIX INCLUDING all message history and shared files"
  else
    for sub in broker plugin install.sh install-windows.ps1 uninstall.sh uninstall-windows.ps1; do
      [ -e "$PREFIX/$sub" ] && rm -rf "${PREFIX:?}/$sub"
    done
    echo "    removed the installed broker code from $PREFIX"
    if [ -e "$PREFIX/hollerback.db" ] || [ -d "$PREFIX/files" ]; then
      echo "    KEPT your data: $PREFIX/hollerback.db and $PREFIX/files"
      echo "    re-run with --purge-data to delete those too"
    fi
  fi
  removed=1
fi

[ "$removed" = "0" ] && echo "    no broker was installed"

PORT_LEFT=""
command -v ss >/dev/null 2>&1 && PORT_LEFT="$(ss -lnt 2>/dev/null | grep ':8850' || true)"
if [ -n "$PORT_LEFT" ]; then
  echo "    WARNING: something is still listening on 8850:"
  echo "$PORT_LEFT" | sed 's/^/      /'
fi

cat <<'EOF'

Done. The plugin on this machine is separate -- remove it with uninstall.sh.
Peers on other machines will just see this broker as unreachable; their
plugins keep retrying quietly and need no action.
EOF
