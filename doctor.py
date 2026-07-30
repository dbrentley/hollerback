#!/usr/bin/env python3
"""hollerback doctor -- why isn't this session talking to anyone?

    curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/doctor.py | python3

Run it from the workspace you are having trouble with, because half of what it
checks (trust, the derived id, the project config layer) depends on the cwd.

Standard library only, and macOS-safe: no /proc, no `pgrep -a`, no `stat -c`, no
`timeout`. It reads and reports; it changes nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request

OK, WARN, BAD, INFO = "ok", "warn", "BAD", "--"
_MARK = {OK: "  ok  ", WARN: " warn ", BAD: " FAIL ", INFO: "      "}
problems: list[str] = []
warnings: list[str] = []


def say(status: str, label: str, detail: str = "") -> None:
    print(f"[{_MARK[status]}] {label}" + (f"\n           {detail}" if detail else ""))
    if status == BAD:
        problems.append(label)
    elif status == WARN:
        warnings.append(label)


def head(title: str) -> None:
    print(f"\n=== {title} ===")


def read_json(path: pathlib.Path):
    """utf-8-sig: PowerShell 5.1 writes a BOM into these on Windows."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def http(url: str, timeout: int = 6):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


# --- 1. where are we, and what python is this ------------------------------

head("this machine")
cwd = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
say(INFO, f"python {sys.version.split()[0]} at {sys.executable}")
say(INFO, f"cwd    {cwd}")
say(INFO, f"host   {socket.gethostname().split('.')[0]}")
if cwd == pathlib.Path.home() and not os.environ.get("CLAUDE_PROJECT_DIR"):
    say(WARN, "running from your HOME directory, not a project",
        "trust, the derived id and the project config layer are all per-directory,\n"
        "           so those results below describe ~ and not the workspace you mean.\n"
        "           cd into the project and run this again.")
if sys.version_info < (3, 8):
    say(BAD, "python is older than 3.8", "the plugin needs 3.8+")

# --- 2. is the plugin installed, and which version -------------------------

head("installed plugin")
home = pathlib.Path.home()
plugin = home / ".claude" / "skills" / "hollerback"
installed_ver = None
if not plugin.exists():
    say(BAD, "plugin is NOT installed", f"nothing at {plugin}")
else:
    kind = "symlink -> " + str(plugin.resolve()) if plugin.is_symlink() else "directory"
    say(OK, f"present at {plugin}", kind)
    try:
        installed_ver = read_json(plugin / ".claude-plugin" / "plugin.json").get("version")
        say(INFO, f"version {installed_ver}")
    except Exception as exc:  # noqa: BLE001
        say(BAD, "cannot read plugin.json", str(exc))

    # monitors.json MUST be a JSON array; an object fails the whole plugin load
    mon = plugin / "monitors" / "monitors.json"
    try:
        data = read_json(mon)
        if isinstance(data, list):
            say(OK, f"monitors.json is an array ({len(data)} monitor(s))")
            for m in data:
                if isinstance(m, dict) and "${user_config." in json.dumps(m):
                    say(BAD, "monitor command references ${user_config.*}",
                        "Claude Code refuses to arm it and drops it silently")
        else:
            say(BAD, "monitors.json is not an array",
                f"got {type(data).__name__}; the whole plugin fails to load")
    except FileNotFoundError:
        say(BAD, "monitors.json missing", str(mon))
    except Exception as exc:  # noqa: BLE001
        say(BAD, "monitors.json is not valid JSON", str(exc))

    if not (plugin / "bin" / "listen.py").is_file():
        say(BAD, "listen.py missing", "the monitor has nothing to run")

    # Can the monitor COMMAND actually run? Claude Code spawns it with its own
    # environment, not your interactive shell -- so a python3 that only exists
    # because .zshrc puts Homebrew on PATH is not necessarily there. On macOS
    # /usr/bin/python3 can also be a stub that defers to the Command Line Tools
    # and fails when nothing can prompt for them.
    try:
        cmd = next((m.get("command", "") for m in read_json(mon)
                    if isinstance(m, dict)), "")
        if cmd:
            say(INFO, f"monitor command: {cmd}")
        interp = cmd.split()[0] if cmd else "python3"
        r = subprocess.run(["/bin/sh", "-c", f"command -v {interp}"],
                           capture_output=True, text=True, timeout=15)
        found = r.stdout.strip()
        if not found:
            say(BAD, f"'{interp}' is NOT on the default PATH",
                "your shell finds it, but the environment Claude Code spawns the\n"
                "           monitor in does not -- so the monitor dies instantly and\n"
                "           silently. Pin an absolute interpreter in monitors.json.")
        else:
            v = subprocess.run([found, "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
                               capture_output=True, text=True, timeout=30)
            ver = v.stdout.strip()
            if v.returncode != 0:
                say(BAD, f"'{found}' exists but fails to run",
                    (v.stderr.strip() or "no output")[:300] +
                    "\n           On macOS this is usually the Command Line Tools stub.")
            elif tuple(int(x) for x in ver.split(".")[:2]) < (3, 8):
                say(BAD, f"monitor would use {found} ({ver})", "the plugin needs 3.8+")
            else:
                say(OK, f"monitor interpreter: {found} ({ver})")
    except Exception as exc:  # noqa: BLE001
        say(WARN, "could not verify the monitor interpreter", str(exc))

    # Does this install predate the per-session identity work?
    try:
        common = (plugin / "bin" / "_common.py").read_text(encoding="utf-8", errors="replace")
        if "instance_tag" not in common:
            say(WARN, "this install predates per-session ids",
                "two sessions in one directory will collide -- reinstall")
    except Exception:  # noqa: BLE001
        pass

# --- 3. config resolution ---------------------------------------------------

head("configuration")
cfg = {"agent": "", "broker": "", "token": ""}
src = {}

settings = home / ".claude" / "settings.json"
try:
    configs = read_json(settings).get("pluginConfigs", {})
    keys = [k for k in configs if k.startswith("hollerback@")]
    if keys:
        say(OK, f"pluginConfigs key(s): {', '.join(keys)}")
    else:
        say(WARN, "no hollerback@* key in settings.json",
            "the tools will answer 'hollerback is not configured'")
    for k in keys:
        opts = (configs.get(k) or {}).get("options", {}) or {}
        for field, name in (("agent", "AGENT_NAME"), ("broker", "BROKER_URL"), ("token", "BROKER_TOKEN")):
            if not cfg[field] and opts.get(name):
                cfg[field] = opts[name]
                src[field] = f"settings.json[{k}]"
except FileNotFoundError:
    say(WARN, f"no {settings}")
except Exception as exc:  # noqa: BLE001
    say(BAD, "settings.json unreadable", str(exc))

for path in (home / ".hollerback.json", cwd / ".hollerback" / "agent.json"):
    try:
        if path.is_file():
            data = read_json(path)
            for field in cfg:
                if data.get(field):
                    cfg[field] = data[field]
                    src[field] = str(path)
            say(INFO, f"also read {path}")
    except Exception as exc:  # noqa: BLE001
        say(WARN, f"{path} unreadable", str(exc))

for field, env in (("agent", "HOLLERBACK_AGENT"), ("broker", "HOLLERBACK_BROKER"),
                   ("token", "HOLLERBACK_TOKEN")):
    if os.environ.get(env):
        cfg[field] = os.environ[env]
        src[field] = f"${env}"

cfg["broker"] = cfg["broker"].rstrip("/")
if cfg["broker"]:
    say(OK, f"broker = {cfg['broker']}", f"from {src.get('broker', '?')}")
else:
    say(BAD, "no broker URL configured", "reinstall with --broker http://<host>:8850")
if cfg["agent"]:
    say(WARN, f"agent id is PINNED to '{cfg['agent']}'",
        f"from {src.get('agent')} -- every workspace here shares one id")

# --- 4. the id this session would use ---------------------------------------

sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
if not cfg["agent"]:
    import hashlib
    root = cwd
    name = root.name or root.anchor.strip("/\\:") or "root"
    if root == home:
        name = "home"
    safe = lambda s: "".join(c if c.isalnum() or c in "-_." else "-" for c in s)  # noqa: E731
    base = f"{safe(socket.gethostname().split('.')[0])}:{safe(name)}"
    tag = hashlib.sha1(sid.encode()).hexdigest()[:4] if sid else ""
    say(INFO, f"derived id: {base}#{tag}" if tag else f"derived id: {base}  (no session tag)")
if not sid:
    say(INFO, "CLAUDE_CODE_SESSION_ID is unset",
        "expected when running this by hand; inside a session it is set")

# --- 5. is the workspace trusted (INHERITED from parents) -------------------

head("workspace trust")
try:
    projects = read_json(home / ".claude.json").get("projects", {})
    for anc in [cwd, *cwd.parents]:
        entry = projects.get(str(anc))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
            say(OK, "workspace is trusted", f"granted at {anc}")
            break
    else:
        say(BAD, "workspace is NOT trusted",
            "monitors are skipped with no error at all. Start a session here and "
            "accept the trust prompt.")
except FileNotFoundError:
    say(WARN, "no ~/.claude.json -- cannot determine trust")
except Exception as exc:  # noqa: BLE001
    say(WARN, "could not read trust state", str(exc))

# --- 6. is a monitor actually running ---------------------------------------

head("monitor process")
try:
    ps = subprocess.run(["ps", "-Ao", "pid,command"], capture_output=True, text=True, timeout=15)
    rows = [l.strip() for l in ps.stdout.splitlines()
            if "listen.py" in l and "doctor" not in l and " grep " not in l]
    # Anchor on the COMMAND field. Claude Code spawns the monitor through a
    # wrapper shell whose argv quotes the very command we are looking for, so a
    # substring match reports each listener twice and turns one live monitor
    # into "2 running".
    live = [r for r in rows if re.match(r"^\d+\s+\S*python[0-9.]*\s", r)]
    if live:
        say(OK, f"{len(live)} listener(s) running")
        for r in live:
            say(INFO, "  " + r[:150])
    else:
        say(BAD, "no listen.py is running",
            "this session can send but can NEVER receive -- no answers, no\n"
            "           questions, no files. Note a monitor retries forever, so if the\n"
            "           broker is down, fix that FIRST and check here again -- an\n"
            "           already-armed monitor reconnects on its own.")
except Exception as exc:  # noqa: BLE001
    say(WARN, "could not list processes", str(exc))

# --- 7. can we reach the broker ---------------------------------------------

head("broker")
broker_ver = None
if cfg["broker"]:
    try:
        h = http(f"{cfg['broker']}/v1/health")
        broker_ver = h.get("version")
        say(OK, f"reachable, version {broker_ver}")
    except urllib.error.URLError as exc:
        loopback = any(h in cfg["broker"] for h in ("127.0.0.1", "localhost", "::1"))
        envf = home / ".config" / "hollerback" / "broker.env"
        venv = home / ".local" / "share" / "hollerback" / "broker" / ".venv" / "bin" / "python"
        if loopback and venv.is_file():
            say(BAD, "the broker is installed here but NOT running",
                f"{exc}\n           "
                "Nothing starts it on a machine without systemd -- the installer\n"
                "           prints the command and launches nothing. Run, and leave open:\n\n"
                f"             set -a; . {envf}; set +a\n"
                f"             {venv} -m hollerback_broker.app\n")
        elif loopback:
            say(BAD, f"nothing is listening on {cfg['broker']}",
                f"{exc}\n           "
                "No broker is installed on this machine either. Either install one\n"
                "           here, or point the plugin at the machine that has it.")
        else:
            say(BAD, f"cannot reach {cfg['broker']}", f"{exc}\n           "
                "Is the broker running on that host, and is the network up\n"
                "           (Tailscale/VPN connected)?")
    except Exception as exc:  # noqa: BLE001
        say(BAD, "broker responded but not with health JSON", str(exc))

    if broker_ver:
        try:
            peers = http(f"{cfg['broker']}/v1/peers").get("peers", [])
            on = [p for p in peers if p.get("online")]
            say(INFO, f"{len(on)} online / {len(peers)} known")
            for p in peers[:25]:
                flag = "online " if p.get("online") else "offline"
                age = int(p.get("seconds_since_seen") or 0)
                say(INFO, f"  {flag} {age:>7}s  {p.get('name')}")
            if peers and not any("#" in (p.get("name") or "") for p in peers):
                say(WARN, "no peer id carries a #tag",
                    "the broker and/or clients predate per-session ids")
        except Exception as exc:  # noqa: BLE001
            say(WARN, "could not read the roster", str(exc))

# --- 8. version skew --------------------------------------------------------

head("version skew")
if installed_ver and broker_ver:
    if installed_ver == broker_ver:
        say(OK, f"plugin and broker agree ({installed_ver})")
    else:
        say(BAD, f"plugin {installed_ver} vs broker {broker_ver}",
            "wire behaviour differs between the halves. Reinstall BOTH, then "
            "start a new session.")
else:
    say(INFO, "cannot compare (one half unknown)")

# --- verdict ----------------------------------------------------------------

print()
for label, items in (("problem", problems), ("warning", warnings)):
    if items:
        print(f"{len(items)} {label}(s):")
        for i in items:
            print(f"  - {i}")
if problems or warnings:
    print("\nAfter reinstalling, START A NEW SESSION. /reload-plugins re-arms the")
    print("monitor but does NOT respawn the MCP server, which looks half-fixed.")
else:
    print("No problems found. If a peer still cannot reach this session, check that")
    print("its broker URL matches this one exactly.")
sys.exit(1 if problems else 0)
