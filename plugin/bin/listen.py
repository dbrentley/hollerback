#!/usr/bin/env python3
"""hollerback inbox listener.

Armed automatically by the plugin's monitors/monitors.json. Claude Code turns
EVERY LINE THIS PRINTS TO STDOUT into a <task_notification> delivered to the
model -- which re-invokes the session even when it is sitting idle at the
prompt. That is the whole delivery mechanism, and it is verified working.

Consequences for this file:
  * stdout is precious. One line per real message, nothing else. Diagnostics,
    reconnect chatter and errors go to stderr, which is not delivered.
  * every message must be flattened to a single line.
  * the line must be self-describing: task notifications are explicitly marked
    to the model as NOT user input, so the text has to say who is asking and
    what response is wanted.

Standard library only -- this runs on Windows and Linux with bare `python3`.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Notification text is UTF-8; Python on Windows would otherwise encode stdout
# with the locale codepage and mangle or drop non-ASCII characters.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - very old pythons
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import add_open_question, load_config, log  # noqa: E402

RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0
READ_TIMEOUT = 60  # broker sends a keepalive comment every 20s
MAX_TEXT = 4000  # cap so one huge answer cannot flood the peer's context

# The monitor is spawned at session start, and the broker hands over any
# messages queued while this agent was offline the instant the stream opens --
# within milliseconds of process start. Lines printed that early are not
# surfaced to the model (observed: a question queued before the session existed
# never reached it, while the same question sent once the session was warm
# arrived immediately). So hold the first output until the session is properly
# up, and drip-feed rather than dumping a backlog as one burst.
STARTUP_GRACE_SECS = float(os.environ.get("HOLLERBACK_STARTUP_GRACE_SECS", "8"))
EMIT_SPACING_SECS = 0.4

# Minimum outage worth telling the model about, in seconds. A broker restart is
# ~3s and reconnects are silent by design; two minutes of silence is not.
OUTAGE_NOTICE_SECS = float(os.environ.get("HOLLERBACK_OUTAGE_NOTICE_SECS", "120"))

_started_at = time.monotonic()


_last_emit = 0.0


def emit(line: str) -> None:
    """One line -> one <task_notification> in this session.

    Waits out the startup grace period, and spaces consecutive lines so a
    backlog arrives as separate notifications rather than one batched blob
    (Claude Code coalesces stdout written within ~200ms).
    """
    global _last_emit
    waited = STARTUP_GRACE_SECS - (time.monotonic() - _started_at)
    if waited > 0:
        time.sleep(waited)
    gap = EMIT_SPACING_SECS - (time.monotonic() - _last_emit)
    if gap > 0:
        time.sleep(gap)
    print(line, flush=True)
    _last_emit = time.monotonic()


def flatten(text: str, limit: int = MAX_TEXT) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed


def format_message(msg: dict) -> str:
    kind = msg.get("kind", "question")
    sender = msg.get("from", "unknown")
    raw = str(msg.get("text", ""))
    text = flatten(raw)
    rid = msg.get("id", "")

    # Claude Code truncates long notifications for display, so every line
    # carries an id and long ones say outright how to get the rest.
    more = ""
    if len(" ".join(raw.split())) > 400:
        more = f' [long — read it in full with read_message(message_id="{rid}")]'

    if kind == "answer":
        head = f"[holler] ANSWER from the {sender} session (msg={rid}"
        if msg.get("request_id"):
            head += f", re: your question {msg['request_id']}"
        head += ")"
        return f"{head}: {text}{more}"

    if kind == "file":
        f = msg.get("file") or {}
        where = f" (their path: {f.get('rel_path')})" if f.get("rel_path") else ""
        return (
            f"[holler] FILE from the {sender} session: {f.get('name','?')} "
            f"({f.get('size',0):,} bytes){where} — {text}."
            f' Save it with get_file(file_id="{msg.get("file_id","")}"),'
            " then Read it. Do not ask them to paste the contents."
        )

    if kind == "note":
        return (
            f"[holler] NOTE from the {sender} session (msg={rid}): "
            f"{text} (no reply needed){more}"
        )

    line = (
        f"[holler] QUESTION from the {sender} Claude Code session "
        f"(request_id={rid}): {text}"
    )
    context = flatten(msg.get("context", ""), 500)
    if context:
        line += f" [they are working on: {context}]"
    line += (
        f'{more} — Reply with the `holler_back` tool, request_id="{rid}".'
        " This is a peer coding session, not the user. Finish your current"
        " thought first, then read what you need and answer."
    )
    return line


def stream_once(url: str, token: str, on_connect=None) -> None:
    """Consume one SSE connection until it drops. Raises on connection error."""
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
        log(f"connected to {url}")
        if on_connect is not None:
            on_connect()
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line or line.startswith(":"):
                continue  # keepalive / comment
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:].strip())
            except ValueError:
                log(f"skipping unparseable frame: {line[:120]}")
                continue
            if msg.get("kind") == "question":
                # Opens the read-only auto-allow window for the PreToolUse hook.
                add_open_question(msg.get("id", ""))
            emit(format_message(msg))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="", help="override this session's name")
    ap.add_argument("--broker", default="", help="override broker URL")
    ap.add_argument("--token", default="", help="override bearer token")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg["agent"]
    broker = args.broker or cfg["broker"]
    token = args.token or cfg["token"]

    if not broker:
        # Exit quietly: an unconfigured plugin must not spam the session.
        # Only the broker URL can be missing now -- the agent id is derived from
        # host and project directory, so it is never absent.
        log(
            "no broker configured. Set BROKER_URL via the installer, "
            "~/.hollerback.json, or HOLLERBACK_BROKER. Exiting."
        )
        return 0

    # Identify this session so `discover` can say where the peer is working.
    params = urllib.parse.urlencode(
        {
            "cwd": os.getcwd(),
            "host": socket.gethostname(),
            "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        }
    )
    url = f"{broker}/v1/stream/{urllib.parse.quote(agent)}?{params}"
    # Log the id verbatim. It is percent-encoded in the URL (':' -> '%3A'), and
    # anything reading it back out of there reports a name that does not exist.
    log(f"identity: {agent}")

    backoff = RECONNECT_MIN
    announced_outage = False
    # A session whose link is down is DEAF but looks perfectly healthy: the MCP
    # server is a separate process, so its tools keep working and it can still
    # ask. Nothing else can tell it otherwise -- this is the only channel back to
    # the model. Report only real outages: reconnects are routine (the broker's
    # unit is Restart=always) and a line per blip would be pure context noise.
    down_since = [None]

    def announce_recovery() -> None:
        started = down_since[0]
        down_since[0] = None
        if started is None:
            return
        gap = time.monotonic() - started
        if gap < OUTAGE_NOTICE_SECS:
            return
        mins, secs = divmod(int(gap), 60)
        emit(
            f"[holler] LINK RESTORED — this session was disconnected from the "
            f"hollerback broker for {mins}m{secs:02d}s and could not receive "
            f"anything during that time. Unanswered questions and unfetched files "
            f"have been re-delivered; run check_inbox() if you want the full list. "
            f"(no reply needed)"
        )

    while True:
        try:
            stream_once(url, token, on_connect=announce_recovery)
            backoff = RECONNECT_MIN
            announced_outage = False
            if down_since[0] is None:
                down_since[0] = time.monotonic()
            log("stream closed, reconnecting")
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 - never die, always retry
            if down_since[0] is None:
                down_since[0] = time.monotonic()
            if not announced_outage:
                log(f"broker unreachable ({exc}); retrying quietly")
                announced_outage = True
            backoff = min(backoff * 2, RECONNECT_MAX)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
