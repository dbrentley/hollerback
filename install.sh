#!/usr/bin/env bash
# hollerback — Linux/macOS installer, the counterpart to install-windows.ps1.
#
# Pulls the plugin straight from the broker, installs it at USER scope (project
# scope silently strips monitors), and points it at the broker under a name you
# choose. Re-run any time to update.
#
#   # straight from GitHub -- no clone, broker need not even be up yet
#   curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/install.sh \
#     | bash -s -- --agent backend --broker http://100.64.0.5:8850
#
#   # first agent on this machine -- becomes the machine default
#   curl -fsSL http://100.64.0.5:8850/install.sh | bash -s -- --agent backend
#
#   # ANOTHER agent on the SAME machine: name the workspace, not the machine
#   cd ~/work/optimizer && ./install.sh --agent power-optimizer --here
#
#   # deliberately change the machine default
#   ./install.sh --agent docs --default
#
# Claude Code reads plugin config from user scope only, so a machine has exactly
# one default AGENT_NAME. --here writes .hollerback/agent.json in a workspace,
# which outranks it, so every repo can be its own agent with nothing to remember
# at launch. Re-running with a new name will NOT silently rename an existing one.
set -uo pipefail

BROKER="${HOLLERBACK_BROKER:-http://127.0.0.1:8850}"
AGENT="${HOLLERBACK_AGENT:-}"
PLUGIN_SOURCE="hollerback@skills-dir"
REPO="${HOLLERBACK_REPO:-dbrentley/hollerback}"   # plugin source when no broker
REF="${HOLLERBACK_REF:-main}"
PROJECT=""        # non-empty => write workspace config there
SET_DEFAULT=0     # 1 => intentionally overwrite the machine default

while [ $# -gt 0 ]; do
  case "$1" in
    --agent)   AGENT="${2:-}"; shift 2 ;;
    --broker)  BROKER="${2:-}"; shift 2 ;;
    --here)    PROJECT="$PWD"; shift ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --default) SET_DEFAULT=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
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
  # Prefer the broker: it serves the exact plugin build it speaks to. Fall back to
  # GitHub so `curl <raw>/install.sh | bash` is self-sufficient.
  TMPZIP="$(mktemp -t hollerback-XXXXXX.zip)"
  SRC_DESC=""
  if [ "$BROKER_UP" = "1" ] && curl -fsS --max-time 60 "$BROKER/v1/plugin.zip" -o "$TMPZIP" 2>/dev/null; then
    SRC_DESC="broker"
  elif curl -fsSL --max-time 90 "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" -o "$TMPZIP.tgz" 2>/dev/null; then
    SRC_DESC="github ($REPO@$REF)"
  else
    echo "    could not fetch the plugin from $BROKER or from GitHub ($REPO@$REF)" >&2
    echo "    Set HOLLERBACK_REPO=you/hollerback, or run this from a clone." >&2
    rm -f "$TMPZIP" "$TMPZIP.tgz"; exit 1
  fi

  rm -rf "$DEST"; mkdir -p "$DEST"
  if [ "$SRC_DESC" = "broker" ]; then
    "$PY" - "$TMPZIP" "$DEST" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
PYEOF
  else
    # The tarball is the whole repo; the plugin is one directory inside it.
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
"$PY" - "$SETTINGS" "$PLUGIN_SOURCE" "$AGENT" "$BROKER" "$PROJECT" "$SET_DEFAULT" <<'PYEOF'
import json, pathlib, sys
settings, source, agent, broker, project, set_default = sys.argv[1:7]
set_default = set_default == "1"

if project:
    # Workspace identity. Beats the machine default in load_config(), so several
    # named agents can share a machine without an env var at every launch.
    root = pathlib.Path(project).expanduser().resolve()
    if not root.is_dir():
        print(f"    --project {root} is not a directory"); raise SystemExit(1)
    d = root / ".hollerback"
    d.mkdir(parents=True, exist_ok=True)
    # Received files land here too; a name is personal, so never let it be committed.
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# hollerback local state; not part of the repo\n*\n")
    target = d / "agent.json"
    target.write_text(
        json.dumps({"agent": agent, "broker": broker}, indent=2) + "\n", encoding="utf-8")
    # Verify shape, not just that we wrote something -- a file that parses but has
    # the wrong shape is exactly how this project shipped a broken monitors.json.
    check = json.loads(target.read_text(encoding="utf-8-sig"))
    assert isinstance(check, dict), "agent.json must be a JSON object"
    assert check.get("agent") == agent, "agent.json did not record the name"
    print(f"    workspace named: {target} (agent={agent})")
    print(f"    verified: agent.json -- ok {check['agent']}")
    raise SystemExit(0)

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

current = (data.get("pluginConfigs", {}).get(source, {}).get("options", {})
           .get("AGENT_NAME", ""))
if current and current != agent and not set_default:
    # Silently renaming the existing agent is the one thing this must never do:
    # the old session keeps answering under a name nobody is addressing any more.
    import os as _os
    here = _os.getcwd()
    print(f"    REFUSING: this machine's default agent is already '{current}'.")
    print(f"    Overwriting it would rename that session on its next restart.")
    print(f"    To add '{agent}' alongside it, name a workspace instead.")
    print(f"    Re-run from the directory that agent should own -- you are in:")
    print(f"        {here}")
    print(f"    and the command is this one plus --here.")
    print(f"    Or to genuinely replace the default, pass --default.")
    raise SystemExit(2)

data.setdefault("pluginConfigs", {})[source] = {
    "options": {"AGENT_NAME": agent, "BROKER_URL": broker}
}
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
what = "machine default changed" if current and current != agent else "settings.json updated"
print(f"    {what} (AGENT_NAME={agent})")
PYEOF
rc=$?
[ $rc -eq 2 ] && exit 2
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

if [ -n "$PROJECT" ]; then
  cat <<EOF

Done. '$AGENT' is the agent for $PROJECT.
Start a NEW session with that directory as the workspace and it connects under
that name -- nothing to remember at launch.
EOF
else
  cat <<EOF

Done. '$AGENT' is this machine's default agent.
EOF
fi

cat <<EOF
  * START A NEW SESSION -- /reload-plugins does NOT respawn the MCP server.
  * The workspace must be TRUSTED, or monitors are silently skipped.
  * Status line should show '1 monitor'; you should have 9 hollerback tools.
  * Another agent on this machine? Name its workspace, don't rename the machine:
        cd <that project> && install.sh --agent <other-name> --here
    (a one-off still works: HOLLERBACK_AGENT=other-name claude)
EOF
