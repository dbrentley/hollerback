#!/usr/bin/env bash
# hollerback — Linux/macOS installer, the counterpart to install-windows.ps1.
#
# Pulls the plugin straight from the broker, installs it at USER scope (project
# scope silently strips monitors), and points it at the broker under a name you
# choose. Re-run any time to update.
#
#   curl -fsSL http://100.88.173.55:8850/install.sh | bash -s -- --agent docs
#   ./install.sh --agent docs --broker http://100.88.173.55:8850
set -uo pipefail

BROKER="http://127.0.0.1:8850"
AGENT=""
PLUGIN_SOURCE="hollerback@skills-dir"

while [ $# -gt 0 ]; do
  case "$1" in
    --agent)  AGENT="${2:-}"; shift 2 ;;
    --broker) BROKER="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$AGENT" ]; then
  echo "ERROR: --agent is required. It is how other sessions address this one," >&2
  echo "       e.g. --agent backend / --agent docs / --agent infra." >&2
  exit 1
fi

DEST="$HOME/.claude/skills/hollerback"
SETTINGS="$HOME/.claude/settings.json"

echo "==> broker: $BROKER   agent: $AGENT"

# --- 1. python ---------------------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -z "$PY" ] && { echo "    no Python 3.8+ on PATH" >&2; exit 1; }
echo "    python: $PY"

# --- 2. reachability ---------------------------------------------------------
if ! curl -fsS --max-time 10 "$BROKER/v1/health" >/dev/null 2>&1; then
  echo "    CANNOT REACH BROKER at $BROKER" >&2
  echo "    Is Tailscale up, and is hollerback-broker running?" >&2
  exit 1
fi
echo "    broker reachable"

# --- 3. install --------------------------------------------------------------
# On the dev machine the plugin is a symlink to the working tree. Replacing it
# with a downloaded copy would silently detach it from the repo.
if [ -L "$DEST" ]; then
  echo "    $DEST is a symlink to $(readlink "$DEST")"
  echo "    leaving it alone (this looks like the dev machine)"
else
  TMPZIP="$(mktemp -t hollerback-XXXXXX.zip)"
  curl -fsS --max-time 60 "$BROKER/v1/plugin.zip" -o "$TMPZIP" || {
    echo "    download failed" >&2; exit 1; }
  rm -rf "$DEST"; mkdir -p "$DEST"
  "$PY" - "$TMPZIP" "$DEST" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
PYEOF
  rm -f "$TMPZIP"
  echo "    installed to $DEST"
fi

# --- 4. config ---------------------------------------------------------------
"$PY" - "$SETTINGS" "$PLUGIN_SOURCE" "$AGENT" "$BROKER" <<'PYEOF'
import json, os, pathlib, sys
settings, source, agent, broker = sys.argv[1:5]
p = pathlib.Path(settings)
p.parent.mkdir(parents=True, exist_ok=True)
data = {}
if p.is_file():
    backup = p.with_suffix(".json.bak.hollerback")
    if not backup.exists():
        backup.write_bytes(p.read_bytes())
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"    existing settings.json unparseable ({exc}); refusing to clobber it")
        raise SystemExit(1)
data.setdefault("pluginConfigs", {})[source] = {
    "options": {"AGENT_NAME": agent, "BROKER_URL": broker}
}
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"    settings.json updated (AGENT_NAME={agent})")
PYEOF
[ $? -ne 0 ] && exit 1

# --- 5. verify shape, not just syntax ---------------------------------------
"$PY" - "$DEST/monitors/monitors.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8-sig"))
assert isinstance(d, list), "monitors.json must be a JSON ARRAY, got %s" % type(d).__name__
assert d and d[0].get("command"), "monitor entry incomplete"
print("    verified: monitors.json -- ok", d[0]["name"])
PYEOF
[ $? -ne 0 ] && { echo "    monitors.json is invalid" >&2; exit 1; }

# --- 6. smoke test -----------------------------------------------------------
echo "==> smoke test: connecting as '$AGENT' for 4s ..."
ERRLOG="$(mktemp)"
HOLLERBACK_AGENT="$AGENT" HOLLERBACK_BROKER="$BROKER" \
  timeout 4 "$PY" "$DEST/bin/listen.py" >/dev/null 2>"$ERRLOG"
if grep -q "connected to" "$ERRLOG"; then
  echo "    listener connected OK"
else
  echo "    listener did NOT connect:"; sed 's/^/    /' "$ERRLOG"
fi
rm -f "$ERRLOG"

cat <<EOF

Done. Now:
  * START A NEW SESSION -- /reload-plugins does NOT respawn the MCP server.
  * The workspace must be TRUSTED, or monitors are silently skipped.
  * Status line should show '1 monitor'; you should have 9 hollerback tools.
  * Running several sessions on this machine? Override per launch:
        HOLLERBACK_AGENT=other-name claude
EOF
