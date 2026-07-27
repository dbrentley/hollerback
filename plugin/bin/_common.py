"""Shared helpers for the hollerback plugin scripts. Standard library only."""

from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request

PLUGIN_SOURCE = "hollerback@skills-dir"

# Where a workspace declares which agent it is. Lives inside .hollerback/ because
# that directory already carries a self-written .gitignore -- a session's name is
# personal, not something to commit into a shared repo.
PROJECT_CONFIG = ".hollerback/agent.json"


def project_root() -> pathlib.Path:
    """The workspace this session is attached to.

    stdio MCP servers are handed CLAUDE_PROJECT_DIR by the host. Monitors are not,
    but they are spawned with the project as their cwd -- verified against a live
    broker, which recorded exactly each session's directory. Both land here.
    """
    return pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _candidate_sources(configs: dict) -> list:
    """Every plugin-source key that could hold this plugin's config, best first.

    `pluginConfigs` is keyed by the plugin's *source id*, and there is more than
    one. install.sh / install-windows.ps1 write PLUGIN_SOURCE. `claude plugin
    install` instead uses "<plugin>@<marketplace>" -- "hollerback@hollerback" for
    this repo's marketplace.json, or "hollerback@<their-marketplace>" for a fork.

    Reading only PLUGIN_SOURCE made the documented marketplace install fail in the
    most confusing way available: the plugin loaded, the monitor armed, all nine
    tools appeared, and then every one of them answered "hollerback is not
    configured" -- while listen.py exited 0 having logged the reason to stderr,
    where nobody looks.
    """
    ordered = [PLUGIN_SOURCE] if PLUGIN_SOURCE in configs else []
    ordered += sorted(
        k for k in configs if k.startswith("hollerback@") and k != PLUGIN_SOURCE
    )
    return ordered


def log(msg: str) -> None:
    """Diagnostics go to stderr.

    For listen.py this is load-bearing: stdout lines become <task_notification>
    events in the peer session, so anything non-essential printed there is
    context pollution.
    """
    print(f"[holler] {msg}", file=sys.stderr, flush=True)


def _adopt_legacy_env() -> None:
    """Honour pre-rename AGENTSHARE_* variables, once, on stderr.

    Same silent-failure class as the broker's (see hollerback_broker/__init__):
    an AGENTSHARE_AGENT left in a shell alias or launcher is simply ignored after
    the rename, and the session comes up unconfigured with nothing said about why.
    """
    adopted = []
    for old, value in sorted(os.environ.items()):
        if not old.startswith("AGENTSHARE_") or not value:
            continue
        new = "HOLLERBACK_" + old[len("AGENTSHARE_") :]
        if not os.environ.get(new):
            os.environ[new] = value
            adopted.append(f"{old} -> {new}")
    if adopted:
        log("honouring deprecated AGENTSHARE_* env vars: " + ", ".join(adopted))
        log("rename them to HOLLERBACK_* -- this fallback will not last forever")


_adopt_legacy_env()


def load_config() -> dict:
    """Resolve settings without them ever passing through a shell.

    Claude Code REFUSES to arm a monitor whose command references
    ${user_config.*} -- "The substituted value would be passed to a shell...
    have the monitor script read the value from a config file or prompt
    instead." The monitor is dropped with only a debug-log line, so config must
    be read here rather than passed as argv.

    Precedence: env > ~/.hollerback.json > the plugin's userConfig values as
    written by /plugin into ~/.claude/settings.json.
    """
    cfg = {"agent": "", "broker": "", "token": ""}

    settings = pathlib.Path.home() / ".claude" / "settings.json"
    try:
        configs = json.loads(settings.read_text(encoding="utf-8-sig")).get(
            "pluginConfigs", {}
        )
        for source in _candidate_sources(configs):
            entry = configs.get(source) or {}
            opts = entry.get("options", {}) if isinstance(entry, dict) else {}
            if not isinstance(opts, dict):
                continue
            # First source that supplies a field wins, so an explicit
            # install.sh config still beats a stale marketplace entry.
            cfg["agent"] = cfg["agent"] or opts.get("AGENT_NAME", "")
            cfg["broker"] = cfg["broker"] or opts.get("BROKER_URL", "")
            cfg["token"] = cfg["token"] or opts.get("BROKER_TOKEN", "")
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"could not read plugin config from {settings}: {exc}")

    local = pathlib.Path.home() / ".hollerback.json"
    try:
        if local.is_file():
            data = json.loads(local.read_text(encoding="utf-8-sig"))
            for key in cfg:
                if data.get(key):
                    cfg[key] = data[key]
    except Exception as exc:  # noqa: BLE001
        log(f"could not read {local}: {exc}")

    # Per-workspace identity, and the only thing that makes several named agents
    # on one machine practical. Claude Code reads pluginConfigs from user scope
    # ONLY -- never project settings -- so plugin config can hold exactly one
    # AGENT_NAME per machine. Without this, a second agent means remembering an
    # env var at every launch, and re-running the installer under a new name
    # silently renames the first one. The name belongs to the workspace, which is
    # how these sessions are actually organised: one repo, one role.
    proj = project_root() / PROJECT_CONFIG
    try:
        if proj.is_file():
            data = json.loads(proj.read_text(encoding="utf-8-sig"))
            for key in cfg:
                if data.get(key):
                    cfg[key] = data[key]
    except Exception as exc:  # noqa: BLE001
        log(f"could not read {proj}: {exc}")

    cfg["agent"] = os.environ.get("HOLLERBACK_AGENT") or cfg["agent"]
    cfg["broker"] = os.environ.get("HOLLERBACK_BROKER") or cfg["broker"]
    cfg["token"] = os.environ.get("HOLLERBACK_TOKEN") or cfg["token"]
    cfg["broker"] = cfg["broker"].rstrip("/")
    cfg["agent"] = cfg["agent"] or default_agent_name()
    return cfg


def default_agent_name() -> str:
    """Derive an id from where this session actually is.

    Nothing should have to be named at install time. A name typed into an
    installer is one per machine, so a second session there either collides or
    forces a rename, and the name goes stale the moment that session moves to
    another repo. <host>:<project-dir> needs no configuration, is stable for as
    long as the session stays put, and is distinct per workspace by construction.

    It is deliberately mechanical, not descriptive -- what a session *does* is
    announce()d separately and read back by discover(), so the id never has to
    carry meaning.

    Separated by ':' rather than '/'. The id is a path segment in
    /v1/stream/{agent}, and a slash splits it into two segments and 404s the
    route -- percent-encoding does not save it either, since ASGI decodes %2F
    back to a separator before routing.
    """
    try:
        host = socket.gethostname().split(".")[0].strip() or "unknown-host"
    except Exception:  # noqa: BLE001
        host = "unknown-host"
    root = project_root()
    name = root.name or root.anchor.strip("/\\:") or "root"
    if root == pathlib.Path.home():
        name = "home"
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in s)  # noqa: E731
    return f"{safe(host)}:{safe(name)}"


# --- open-question state ----------------------------------------------------
#
# The PreToolUse hook runs before EVERY Read/Grep/Glob, so it must not touch the
# network. listen.py records questions here as they arrive and the `holler_back` tool
# clears them; entries also expire, so a crash or a missed clear cannot leave
# the session permanently permissive.

ANSWER_WINDOW_SECS = int(os.environ.get("HOLLERBACK_ANSWER_WINDOW_SECS", "900"))


def state_path() -> pathlib.Path:
    """Per-agent, so two sessions on one machine never share a window."""
    base = os.environ.get("XDG_CACHE_HOME") or (pathlib.Path.home() / ".cache")
    p = pathlib.Path(base) / "hollerback"
    p.mkdir(parents=True, exist_ok=True)
    agent = load_config()["agent"] or "default"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in agent)
    return p / f"open_questions.{safe}.json"


def _read_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_state(state: dict) -> None:
    path = state_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log(f"could not write {path}: {exc}")


def _fresh(state: dict) -> dict:
    now = time.time()
    return {k: v for k, v in state.items() if now - float(v) < ANSWER_WINDOW_SECS}


def add_open_question(request_id: str) -> None:
    state = _fresh(_read_state())
    state[request_id] = time.time()
    _write_state(state)


def clear_open_question(request_id: str) -> None:
    state = _fresh(_read_state())
    state.pop(request_id, None)
    _write_state(state)


def has_open_question() -> bool:
    """True while this session owes a peer an answer."""
    return bool(_fresh(_read_state()))


def http_json(url: str, payload: dict | None = None, token: str = "", timeout: int = 15) -> dict:
    """POST json (or GET when payload is None) and decode the json reply."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}: {body[:200]}"}
