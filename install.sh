#!/usr/bin/env bash
# hollerback — Linux/macOS installer, the counterpart to install-windows.ps1.
#
# Pulls the plugin straight from the broker (or GitHub), installs it at USER
# scope -- project scope silently strips monitors -- and points it at a broker.
# Re-run any time to update.
#
#   curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/install.sh \
#     | bash -s -- --broker http://100.64.0.5:8850
#
# There is nothing to name. Each session identifies itself as <host>:<project-dir>
# and says what it does at runtime via the announce() tool, which peers read back
# with discover(). One install per machine; every workspace on it is its own agent.
set -uo pipefail

BROKER="${HOLLERBACK_BROKER:-http://127.0.0.1:8850}"
AGENT="${HOLLERBACK_AGENT:-}"
PLUGIN_SOURCE="hollerback@skills-dir"
REPO="${HOLLERBACK_REPO:-dbrentley/hollerback}"   # plugin source when no broker
REF="${HOLLERBACK_REF:-main}"

while [ $# -gt 0 ]; do
  case "$1" in
    --broker) BROKER="${2:-}"; shift 2 ;;
    --agent)  AGENT="${2:-}"; shift 2 ;;   # escape hatch; normally auto-derived
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

DEST="$HOME/.claude/skills/hollerback"
SETTINGS="$HOME/.claude/settings.json"

echo "==> broker: $BROKER"

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
# Not fatal any more. Piping this straight from GitHub is a supported install
# path, and there the broker URL is whatever you passed -- it may legitimately be
# down, or on a tailnet this shell cannot see yet. The plugin is inert without a
# broker, so installing it now and connecting later is fine; only the plugin
# SOURCE has to come from somewhere, and GitHub can supply that.
BROKER_UP=0
if curl -fsS --max-time 10 "$BROKER/v1/health" >/dev/null 2>&1; then
  BROKER_UP=1
  echo "    broker reachable"
else
  echo "    broker NOT reachable at $BROKER (installing anyway)"
  if [ "$BROKER" = "http://127.0.0.1:8850" ]; then
    echo "    NOTE: that is the default. If your broker is elsewhere, pass:" >&2
    echo "          --broker http://<host>:8850" >&2
  fi
fi

# --- 3. install --------------------------------------------------------------
# On the dev machine the plugin is a symlink to the working tree. Replacing it
# with a downloaded copy would silently detach it from the repo.
if [ -L "$DEST" ]; then
  echo "    $DEST is a symlink to $(readlink "$DEST")"
  echo "    leaving it alone (this looks like the dev machine)"
else
  # Prefer GitHub, fall back to the broker. This used to be the other way round,
  # on the theory that the broker serves the build it speaks to -- but a broker is
  # a long-lived deployment that lags the repo, so a fresh `curl <raw>/install.sh`
  # would install a stale plugin and pair a new installer with old code. GitHub is
  # what the documented one-liner points at, so make that authoritative; the broker
  # remains the fallback for a fork, an air-gap, or GitHub being unreachable.
  TMPZIP="$(mktemp -t hollerback-XXXXXX.zip)"
  SRC_DESC=""
  if curl -fsSL --max-time 90 "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" -o "$TMPZIP.tgz" 2>/dev/null; then
    SRC_DESC="github ($REPO@$REF)"
  elif [ "$BROKER_UP" = "1" ] && curl -fsS --max-time 60 "$BROKER/v1/plugin.zip" -o "$TMPZIP" 2>/dev/null; then
    SRC_DESC="broker"
  else
    echo "    could not fetch the plugin from GitHub ($REPO@$REF) or from $BROKER" >&2
    echo "    Set HOLLERBACK_REPO=you/hollerback, or run this from a clone." >&2
    rm -f "$TMPZIP" "$TMPZIP.tgz"; exit 1
  fi

  rm -rf "$DEST"; mkdir -p "$DEST"
  if [ "$SRC_DESC" = "broker" ]; then
    "$PY" - "$TMPZIP" "$DEST" <<'ZIPEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
ZIPEOF
  else
    TMPX="$(mktemp -d)"
    tar xzf "$TMPZIP.tgz" -C "$TMPX" --strip-components=1 || {
      echo "    could not unpack the GitHub tarball" >&2; exit 1; }
    [ -d "$TMPX/plugin" ] || { echo "    tarball has no plugin/ directory" >&2; exit 1; }
    cp -R "$TMPX/plugin/." "$DEST/"
    rm -rf "$TMPX"
  fi
  rm -f "$TMPZIP" "$TMPZIP.tgz"
  echo "    installed to $DEST (from $SRC_DESC)"
fi

# --- 4. config ---------------------------------------------------------------
# Only the broker URL. Identity is derived per workspace at runtime, so there is
# no name to store, nothing to collide, and no second install for a second agent.
"$PY" - "$SETTINGS" "$PLUGIN_SOURCE" "$BROKER" "$AGENT" <<'PYEOF'
import json, pathlib, sys
settings, source, broker, agent = sys.argv[1:5]
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
opts = {"BROKER_URL": broker}
if agent:
    opts["AGENT_NAME"] = agent
data.setdefault("pluginConfigs", {})[source] = {"options": opts}
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"    settings.json updated (BROKER_URL={broker})")
if agent:
    # Worth shouting about: this is stored at user scope, so it applies to EVERY
    # workspace on the machine and switches off the per-directory derivation that
    # makes several agents possible here.
    print(f"    WARNING: --agent pinned AGENT_NAME={agent} for this whole machine.")
    print( "          Every workspace here will use that one name, and per-directory")
    print( "          identity is disabled. Re-run without --agent to undo.")
PYEOF
rc=$?
[ $rc -ne 0 ] && exit 1

# --- 5. verify shape, not just syntax ---------------------------------------
"$PY" - "$DEST/monitors/monitors.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8-sig"))
assert isinstance(d, list), "monitors.json must be a JSON ARRAY, got %s" % type(d).__name__
assert d and d[0].get("command"), "monitor entry incomplete"
print("    verified: monitors.json -- ok", d[0]["name"])
PYEOF
[ $? -ne 0 ] && { echo "    monitors.json is invalid" >&2; exit 1; }

# --- 5b. broker/plugin version handshake -------------------------------------
# The two halves are deployed separately, so they drift. Silence here is how a
# fresh installer ends up paired with a plugin from another commit.
if [ "$BROKER_UP" = "1" ]; then
  "$PY" - "$DEST/.claude-plugin/plugin.json" "$BROKER" <<'VEOF'
import json, pathlib, sys, urllib.request
mine = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig")).get("version", "?")
try:
    theirs = json.loads(urllib.request.urlopen(sys.argv[2] + "/v1/health", timeout=10).read()).get("version")
except Exception:
    theirs = None
if theirs is None:
    print(f"    NOTE: broker predates version reporting; plugin is {mine}.")
    print( "          Re-run install-broker.sh so both halves match.")
elif theirs != mine:
    print(f"    WARNING: plugin {mine} vs broker {theirs} -- they are from different commits.")
    print( "          Re-run install-broker.sh on the broker machine.")
else:
    print(f"    versions match (plugin and broker both {mine})")
VEOF
fi

# --- 6. smoke test -----------------------------------------------------------
echo "==> smoke test: connecting for 4s ..."
ERRLOG="$(mktemp)"
HOLLERBACK_BROKER="$BROKER" timeout 4 "$PY" "$DEST/bin/listen.py" >/dev/null 2>"$ERRLOG"
if grep -q "connected to" "$ERRLOG"; then
  WHO=$(grep -o '/v1/stream/[^?]*' "$ERRLOG" | head -1 | sed 's|/v1/stream/||')
  echo "    listener connected OK as '${WHO:-?}'"
else
  echo "    listener did NOT connect:"; sed 's/^/    /' "$ERRLOG"
fi
rm -f "$ERRLOG"

cat <<EOF

Done. Nothing to name -- each session identifies itself as <host>:<project-dir>.
  * START A NEW SESSION -- /reload-plugins does NOT respawn the MCP server.
  * The workspace must be TRUSTED, or monitors are silently skipped.
  * Status line should show '1 monitor'; you should have 10 hollerback tools.
  * In each session call announce() once to say what it is, then discover() to
    see everyone else. Every workspace on this machine is automatically its own
    agent -- no second install, no naming.
EOF
