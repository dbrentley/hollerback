"""Shared helpers for the hollerback plugin scripts. Standard library only."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

PLUGIN_SOURCE = "hollerback@skills-dir"


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
        opts = (
            json.loads(settings.read_text(encoding="utf-8-sig"))
            .get("pluginConfigs", {})
            .get(PLUGIN_SOURCE, {})
            .get("options", {})
        )
        cfg["agent"] = opts.get("AGENT_NAME", "") or cfg["agent"]
        cfg["broker"] = opts.get("BROKER_URL", "") or cfg["broker"]
        cfg["token"] = opts.get("BROKER_TOKEN", "") or cfg["token"]
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

    cfg["agent"] = os.environ.get("HOLLERBACK_AGENT") or cfg["agent"]
    cfg["broker"] = os.environ.get("HOLLERBACK_BROKER") or cfg["broker"]
    cfg["token"] = os.environ.get("HOLLERBACK_TOKEN") or cfg["token"]
    cfg["broker"] = cfg["broker"].rstrip("/")
    return cfg


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
