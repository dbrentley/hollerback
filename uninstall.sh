#!/usr/bin/env bash
# hollerback — Linux/macOS uninstaller.
#
# Removes the plugin, its config entry, and its local state. Does NOT touch the
# broker (that is a service you installed separately) and does NOT delete files
# peers sent you -- those live in .hollerback/inbox/ inside your projects and are
# yours to keep or bin.
#
#   curl -fsSL <broker>/uninstall.sh | bash
#   ./uninstall.sh --purge-inboxes   # also delete received files under $HOME
set -uo pipefail

PURGE_INBOXES=0
[ "${1:-}" = "--purge-inboxes" ] && PURGE_INBOXES=1

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
pc = d.get("pluginConfigs", {})
gone = [k for k in ("hollerback@skills-dir", "agentshare@skills-dir") if k in pc]
for k in gone:
    pc.pop(k)
if not pc:
    d.pop("pluginConfigs", None)
ep = d.get("enabledPlugins", {})
for k in list(ep):
    if k.split("@")[0] in ("hollerback", "agentshare"):
        ep.pop(k)
if not ep:
    d.pop("enabledPlugins", None)
p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
print(f"    settings.json: removed {gone or 'nothing'} (backup: {backup.name})")
PYEOF
fi

for d in "$HOME/.cache/hollerback" "$HOME/.cache/agentshare"; do
  [ -d "$d" ] && { echo "    removing state $d"; rm -rf "$d"; removed=1; }
done
for f in "$HOME/.hollerback.json" "$HOME/.agentshare.json"; do
  [ -f "$f" ] && { echo "    removing $f"; rm -f "$f"; removed=1; }
done

if [ "$PURGE_INBOXES" = "1" ]; then
  echo "    searching \$HOME for received-file inboxes ..."
  find "$HOME" -maxdepth 6 -type d \( -name .hollerback -o -name .agentshare \) \
       -not -path "*/.venv/*" -print -exec rm -rf {} + 2>/dev/null | sed 's/^/      deleted /'
else
  echo "    keeping received files (.hollerback/inbox/ in your projects)"
  echo "    re-run with --purge-inboxes to delete those too"
fi

[ "$removed" = "0" ] && echo "    nothing was installed"

cat <<'EOF'

Done. RESTART any running Claude Code session -- the monitor and MCP server are
long-lived child processes and keep running until their session exits.

The broker was left alone. To remove it too:
    systemctl --user disable --now hollerback-broker
    rm -f  ~/.config/systemd/user/hollerback-broker.service
    rm -rf ~/.config/hollerback ~/.local/share/hollerback
EOF
