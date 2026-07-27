#!/usr/bin/env bash
# hollerback — Linux/macOS uninstaller.
#
# Removes the plugin, every config entry it wrote, and its local state. Does NOT
# touch the broker (that is a service you installed separately) and does NOT
# delete files peers sent you -- those live in .hollerback/inbox/ inside your
# projects and are yours to keep or bin.
#
#   curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall.sh | bash
#   curl -fsSL <broker>:8850/uninstall.sh | bash
#   ./uninstall.sh --purge-inboxes    # also delete received files under $HOME
#
# Needs no broker and no network: it only removes local files.
set -uo pipefail

PURGE_INBOXES=0
SCAN_DEPTH="${HOLLERBACK_SCAN_DEPTH:-6}"

while [ $# -gt 0 ]; do
  case "$1" in
    --purge-inboxes)   PURGE_INBOXES=1; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

SKILLS="$HOME/.claude/skills"
SETTINGS="$HOME/.claude/settings.json"
removed=0

echo "==> uninstalling hollerback"

# Both the current name and the pre-rename one, so an old install goes too.
for name in hollerback agentshare; do
  target="$SKILLS/$name"
  if [ -L "$target" ]; then
    echo "    removing symlink $target -> $(readlink "$target")"
    rm -f "$target"; removed=1
  elif [ -d "$target" ]; then
    echo "    removing $target"
    rm -rf "$target"; removed=1
  fi
done

# A marketplace install puts files under ~/.claude/plugins, not skills/.
for d in "$HOME/.claude/plugins/hollerback" "$HOME/.claude/plugins/agentshare"; do
  [ -e "$d" ] && { echo "    removing $d"; rm -rf "$d"; removed=1; }
done

PY="$(command -v python3 || command -v python || true)"
if [ -n "$PY" ] && [ -f "$SETTINGS" ]; then
  "$PY" - "$SETTINGS" <<'PYEOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
backup = p.with_suffix(".json.bak.hollerback-uninstall")
try:
    d = json.loads(p.read_text(encoding="utf-8-sig"))
except Exception as exc:
    print(f"    settings.json unparseable ({exc}); leaving it alone")
    raise SystemExit(0)
backup.write_bytes(p.read_bytes())

# Match on the PLUGIN half of the source id, not the whole string. Config is
# keyed "<plugin>@<marketplace>", so install.sh writes hollerback@skills-dir
# while `claude plugin install` writes hollerback@hollerback -- and a fork writes
# hollerback@<their-marketplace>. Listing exact ids left marketplace installs
# behind on every uninstall.
def ours(key):
    return key.split("@")[0] in ("hollerback", "agentshare")

pc = d.get("pluginConfigs", {})
gone = [k for k in list(pc) if ours(k)]
for k in gone:
    pc.pop(k)
if not pc:
    d.pop("pluginConfigs", None)

ep = d.get("enabledPlugins", {})
gone_ep = [k for k in list(ep) if ours(k)]
for k in gone_ep:
    ep.pop(k)
if not ep:
    d.pop("enabledPlugins", None)

p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
print(f"    settings.json: removed {gone + gone_ep or 'nothing'} (backup: {backup.name})")
PYEOF
fi

# XDG_CACHE_HOME is honoured by the plugin, so honour it here too.
for d in "${XDG_CACHE_HOME:-$HOME/.cache}/hollerback" "${XDG_CACHE_HOME:-$HOME/.cache}/agentshare" \
         "$HOME/.cache/hollerback" "$HOME/.cache/agentshare"; do
  [ -d "$d" ] && { echo "    removing state $d"; rm -rf "$d"; removed=1; }
done
for f in "$HOME/.hollerback.json" "$HOME/.agentshare.json"; do
  [ -f "$f" ] && { echo "    removing $f"; rm -f "$f"; removed=1; }
done

# .hollerback/agent.json is a leftover from when installers wrote per-workspace
# names. Nothing creates it now (ids are derived), but old installs have them and
# load_config still reads them, so an uninstall that left them behind would keep
# renaming sessions. Received files in the same directory are the user's and
# survive unless --purge-inboxes is given.
if [ "$PURGE_INBOXES" = "1" ]; then
  echo "    searching \$HOME for hollerback directories (inboxes included) ..."
  find "$HOME" -maxdepth "$SCAN_DEPTH" -type d \( -name .hollerback -o -name .agentshare \) \
       -not -path "*/.venv/*" -not -path "*/node_modules/*" \
       -print -exec rm -rf {} + 2>/dev/null | sed 's/^/      deleted /'
else
  echo "    searching \$HOME for workspace agent names ..."
  found=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    echo "      removing $f"
    rm -f "$f"; found=1; removed=1
  done <<EOT
$(find "$HOME" -maxdepth "$SCAN_DEPTH" -type f \
       \( -path "*/.hollerback/agent.json" -o -path "*/.agentshare/agent.json" \) \
       -not -path "*/.venv/*" -not -path "*/node_modules/*" 2>/dev/null)
EOT
  [ "$found" = "0" ] && echo "      none found"
  echo "    keeping received files (.hollerback/inbox/ in your projects)"
  echo "    re-run with --purge-inboxes to delete those too"
fi

for b in "$SETTINGS.bak.hollerback" "$SETTINGS.bak.hollerback-uninstall"; do
  [ -f "$b" ] && echo "    kept settings backup: $b"
done

[ "$removed" = "0" ] && echo "    nothing was installed"

cat <<'EOF'

Done. RESTART any running Claude Code session -- the monitor and MCP server are
long-lived child processes and keep running until their session exits.

The broker was left alone. To remove it too:
    systemctl --user disable --now hollerback-broker
    rm -f  ~/.config/systemd/user/hollerback-broker.service
    rm -rf ~/.config/hollerback ~/.local/share/hollerback
EOF
