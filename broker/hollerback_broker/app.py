"""hollerback broker.

A message bus between concurrently-running Claude Code sessions. Two surfaces:

  POST /v1/ask             a session asks a peer a question (returns immediately)
  POST /v1/answer          a session answers a question it was asked
  POST /v1/note            fire-and-forget note, no reply expected
  POST /v1/announce        a session records what it is, for discover()
  POST /v1/forget          drop an agent that will never come back
  POST /v1/send            dispatches on a 'kind' field; exists for curl testing
  POST /v1/file            upload a file for a peer (base64 in JSON)
  GET  /v1/file/{id}       download one, and mark it fetched
  GET  /v1/message/{id}    full untruncated text of one message
  GET  /v1/stream/{agent}  long-lived SSE feed of that agent's messages
  GET  /v1/peers           who is online, where, what they do, what they owe
  GET  /v1/pending/{agent} open questions (used by the read-only permission hook)
  GET  /v1/health          liveness + broker version (unauthenticated)
  GET  /v1/dashboard       counts, peers and recent messages, for the web page
  GET  /v1/plugin.zip      the plugin, so a peer machine can install without SSH
  GET  /                   the dashboard itself
  GET  /install.sh /install.ps1 /uninstall.sh /uninstall.ps1 /uninstall-broker.sh

Everything durable lives in SQLite (see store.py): the unit is Restart=always,
so anything held only in memory would vanish on every code change.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import pathlib
import re
import sys
import time
import zipfile
from collections import defaultdict

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, store

# --- config -----------------------------------------------------------------

BIND = os.getenv("HOLLERBACK_BIND", "127.0.0.1")
PORT = int(os.getenv("HOLLERBACK_PORT", "8850"))
TOKEN = os.getenv("HOLLERBACK_TOKEN", "")
KEEPALIVE_SECS = int(os.getenv("HOLLERBACK_KEEPALIVE_SECS", "20"))
MAX_FILE_BYTES = int(os.getenv("HOLLERBACK_MAX_FILE_BYTES", str(10 * 1024 * 1024)))
PLUGIN_DIR = pathlib.Path(
    os.getenv(
        "HOLLERBACK_PLUGIN_DIR",
        str(pathlib.Path(__file__).resolve().parents[2] / "plugin"),
    )
)
_ZIP_SKIP = {"__pycache__", ".git", ".venv", ".DS_Store"}

# agent -> live subscriber queues (brief overlap is normal on reconnect)
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def _authorized(request: Request) -> bool:
    if not TOKEN:
        return True
    return request.headers.get("authorization", "") == f"Bearer {TOKEN}"


def _unauth() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


def _deliver(msg: dict) -> bool:
    """Push to live subscribers if any; otherwise it waits in SQLite."""
    subs = _subscribers.get(msg["to"])
    if not subs:
        return False
    for q in list(subs):
        q.put_nowait(msg)
    store.mark_delivered(msg["id"])
    return True


# How long an agent must stay gone before its askers are told. listen.py
# reconnects on its own and a network blip closes the stream too, so firing
# immediately would emit a notice every time a laptop changes wifi.
DEPART_NOTICE_SECS = float(os.getenv("HOLLERBACK_DEPART_NOTICE_SECS", "45"))
NOTICE_SENDER = "hollerback"
# Agents nudged to announce in this process, so a session that declines to
# announce is not pestered on every reconnect.
_announce_nudged: set[str] = set()


async def _notify_departure(agent: str) -> None:
    """Tell whoever is waiting on this agent that it went away.

    The gap this closes: asking is fire-and-forget, so a peer whose answerer dies
    mid-question simply never hears back, and has no way to tell "still thinking"
    from "that session no longer exists". Nothing else in the system ever pushes
    a departure -- connected=False is written once, in the stream's finally block,
    and read only by whoever thinks to call discover.

    Sent as kind='note' on purpose. A new kind would fall through the `file`/`note`
    branches of an older listen.py straight into the *question* formatter, and the
    peer would try to answer a system message -- exactly the stale-client failure
    this project already hit once. Every shipped client understands notes.
    """
    await asyncio.sleep(DEPART_NOTICE_SECS)
    if _subscribers.get(agent):
        return  # reconnected inside the grace window; nothing happened

    waiting: dict[str, list[str]] = defaultdict(list)
    for q in store.open_questions(agent):
        if q["from"] != agent:
            waiting[q["from"]].append(q["id"])

    for asker, ids in waiting.items():
        # Only tell someone who is here to hear it. A stored notice would be
        # re-offered on their next attach (take_undelivered picks up anything
        # undelivered), by which point the peer may well be back and the notice
        # is a lie in the other direction.
        if not _subscribers.get(asker):
            continue
        n = len(ids)
        msg = store.add_message(
            to_agent=asker,
            from_agent=NOTICE_SENDER,
            kind="note",
            text=(
                f"the '{agent}' session went offline with {n} question"
                f"{'s' if n > 1 else ''} from you still unanswered "
                f"({', '.join(ids)}). Nothing is lost -- each one is re-delivered "
                f"automatically if a session reconnects under the name '{agent}'. "
                f"Stop waiting on it and carry on; you will be notified if it answers."
            ),
        )
        _deliver(msg)


def resolve_peer(target: str) -> tuple[str | None, list[dict]]:
    """Map what the caller typed onto a real agent id.

    Ids are mechanical now (<host>:<project-dir>), so nobody types them from
    memory -- they come out of discover(). Accept an exact id, or any unique
    substring of an id or of what that agent said it does, and report the
    candidates instead of guessing when it is ambiguous. Guessing here would
    silently route a question to the wrong session, which is unrecoverable: the
    asker waits on an answer that another agent is busy not writing.

    Returns (resolved_name, candidates). Exactly one of them is meaningful.
    """
    agents = store.list_agents()
    for a in agents:
        if a["name"] == target:
            return target, []
    q = target.strip().lower()
    if not q:
        return None, agents
    hits = [
        a for a in agents
        if q in a["name"].lower() or q in (a.get("capabilities") or "").lower()
    ]
    if len(hits) == 1:
        return hits[0]["name"], []
    return None, hits or agents


def _offline(target: str) -> str:
    """Why a send was refused, and what to do instead."""
    return (
        f"'{target}' is not connected, so nothing was sent. hollerback does not "
        f"hold messages for absent sessions -- a queued message to a session that "
        f"never comes back is worse than a refusal, because you would wait on an "
        f"answer nobody is writing. Call discover() to see who is online."
    )


def _online(name: str) -> bool:
    """Live subscribers, not the stored flag -- the flag can lag a hard drop."""
    return bool(_subscribers.get(name))


def _ambiguous(target: str, candidates: list) -> str:
    listing = "\n".join(
        f"  {a['name']} — {a.get('capabilities') or '(has not announced)'}"
        for a in candidates
    ) or "  (no sessions have ever connected)"
    return (
        f"{target!r} does not identify one peer. Call discover() and use an id "
        f"from it. Closest matches:\n{listing}"
    )


async def forget(request: Request) -> JSONResponse:
    """Drop an agent record outright.

    Nothing expires here by design, so a session that will never return -- a
    renamed directory, a machine that is gone, a smoke test -- otherwise sits in
    discover() forever looking like something you could ask.
    """
    if not _authorized(request):
        return _unauth()
    b = await _body(request)
    name = (b.get("agent") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "need 'agent'"}, status_code=400)
    if _subscribers.get(name):
        return JSONResponse(
            {"ok": False, "error": f"{name!r} is connected right now; stop it first"},
            status_code=409,
        )
    gone = store.forget_agent(name)
    return JSONResponse({"ok": True, "agent": name, "existed": gone})


async def announce(request: Request) -> JSONResponse:
    """Record what a session is, so peers arriving later can still find out.

    A broadcast would only reach whoever happened to be connected at the time,
    and sessions come and go constantly. Storing it means discover() is complete
    regardless of who announced when.
    """
    if not _authorized(request):
        return _unauth()
    b = await _body(request)
    frm = (b.get("from") or "").strip()
    text = (b.get("capabilities") or "").strip()
    if not frm or not text:
        return JSONResponse(
            {"ok": False, "error": "need 'from' and 'capabilities'"}, status_code=400
        )
    store.set_capabilities(frm, text)
    _announce_nudged.discard(frm)
    return JSONResponse({"ok": True, "agent": frm, "capabilities": text})


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# --- routes -----------------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    # version lets an installer notice it is pairing a plugin with a broker
    # from a different commit -- the two are deployed separately and drift.
    return JSONResponse({"ok": True, "version": __version__,
                         "peers": store.list_agents()})


async def peers(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauth()
    return JSONResponse({"ok": True, "peers": store.list_agents()})


async def ask(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauth()
    b = await _body(request)
    to = (b.get("to") or "").strip()
    text = (b.get("text") or "").strip()
    frm = (b.get("from") or "unknown").strip()
    if not to or not text:
        return JSONResponse(
            {"ok": False, "error": "need 'to' and 'text'"}, status_code=400
        )

    resolved, candidates = resolve_peer(to)
    if resolved is None:
        return JSONResponse({"ok": False, "error": _ambiguous(to, candidates)}, status_code=404)
    to = resolved
    if not _online(to):
        return JSONResponse({"ok": False, "error": _offline(to)}, status_code=409)

    msg = store.add_message(to, frm, "question", text, b.get("context") or "")
    live = _deliver(msg)

    return JSONResponse(
        {"ok": True, "request_id": msg["id"], "peer_online": live,
         "note": f"delivered to {to} now"}
    )


async def answer(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauth()
    b = await _body(request)
    request_id = (b.get("request_id") or "").strip()
    text = (b.get("text") or "").strip()
    frm = (b.get("from") or "unknown").strip()
    if not request_id or not text:
        return JSONResponse(
            {"ok": False, "error": "need 'request_id' and 'text'"}, status_code=400
        )

    question = store.get_question(request_id)
    if question is None:
        # Be specific about WHY. The old message ("no question with request_id")
        # read as "it expired" and cost two rounds of misdiagnosis -- the real
        # causes were an id that pointed at a file message, and ids that had been
        # deleted out from under the sender. Nothing in this broker expires.
        other = store.get_message(request_id)
        if other is not None:
            err = (
                f"{request_id} is a {other['kind']} message, not a question, so it "
                f"cannot be answered. "
                + (
                    f"It is a file -- save it with get_file(file_id="
                    f"\"{other.get('file_id', '')}\") instead."
                    if other["kind"] == "file"
                    else "Use tell_peer if you want to respond to it."
                )
            )
        else:
            err = (
                f"No message with id {request_id!r} exists. Nothing in hollerback "
                "expires or times out, so this id was either mistyped or never "
                "existed -- check the request_id in the notification."
            )
        return JSONResponse({"ok": False, "error": err}, status_code=404)

    # Route the answer back to whoever asked -- this is the whole point of
    # threading; without it, two questions in flight get mismatched answers.
    msg = store.add_message(
        to_agent=question["from"],
        from_agent=frm,
        kind="answer",
        text=text,
        context="",
        request_id=request_id,
    )
    store.mark_answered(request_id)
    live = _deliver(msg)
    return JSONResponse(
        {
            "ok": True,
            "answered": request_id,
            "to": question["from"],
            "delivered_now": live,
        }
    )


async def note(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauth()
    b = await _body(request)
    to = (b.get("to") or "").strip()
    text = (b.get("text") or "").strip()
    frm = (b.get("from") or "unknown").strip()
    if not to or not text:
        return JSONResponse(
            {"ok": False, "error": "need 'to' and 'text'"}, status_code=400
        )

    # "*" broadcasts to every known peer except the sender. Notes only -- a
    # broadcast question would have N recipients and no defined answerer.
    if to != "*":
        resolved, candidates = resolve_peer(to)
        if resolved is None:
            return JSONResponse({"ok": False, "error": _ambiguous(to, candidates)}, status_code=404)
        to = resolved
        if not _online(to):
            return JSONResponse({"ok": False, "error": _offline(to)}, status_code=409)

    if to == "*":
        targets = [n for n in _subscribers if n != frm]
        if not targets:
            return JSONResponse(
                {"ok": False, "error": "no other sessions are connected right now"},
                status_code=404,
            )
        sent = []
        for peer in targets:
            msg = store.add_message(peer, frm, "note", text)
            sent.append({"to": peer, "id": msg["id"], "delivered_now": _deliver(msg)})
        return JSONResponse({"ok": True, "broadcast": True, "sent": sent})

    msg = store.add_message(to, frm, "note", text)
    return JSONResponse({"ok": True, "id": msg["id"], "delivered_now": _deliver(msg)})


async def send(request: Request) -> JSONResponse:
    """Back-compat / curl testing: dispatch by 'kind'."""
    b = await _body(request)
    kind = (b.get("kind") or "question").strip()
    if kind == "answer":
        return await answer(request)
    if kind == "note":
        return await note(request)
    return await ask(request)


async def pending(request: Request) -> JSONResponse:
    """Open questions for an agent. Drives the read-only auto-allow hook."""
    if not _authorized(request):
        return _unauth()
    agent = request.path_params["agent"]
    return JSONResponse(
        {
            "ok": True,
            "open_questions": store.open_questions(agent),
            "awaiting_answers": store.awaiting_answers(agent),
            "pending_files": store.pending_files(agent),
        }
    )


async def upload_file(request: Request) -> JSONResponse:
    """Accept a file and notify the recipient that it is waiting.

    Base64 over JSON rather than multipart: the clients are stdlib-only Python
    on two OSes, and multipart encoding by hand is not worth the bugs.
    """
    if not _authorized(request):
        return _unauth()
    b = await _body(request)
    to = (b.get("to") or "").strip()
    frm = (b.get("from") or "unknown").strip()
    name = os.path.basename((b.get("name") or "").strip())
    if not to or not name or not b.get("data"):
        return JSONResponse(
            {"ok": False, "error": "need 'to', 'name' and 'data'"}, status_code=400
        )
    resolved, candidates = resolve_peer(to)
    if resolved is None:
        return JSONResponse({"ok": False, "error": _ambiguous(to, candidates)}, status_code=404)
    to = resolved
    if not _online(to):
        return JSONResponse({"ok": False, "error": _offline(to)}, status_code=409)
    try:
        data = base64.b64decode(b["data"], validate=True)
    except Exception:
        return JSONResponse({"ok": False, "error": "data is not valid base64"}, status_code=400)
    if len(data) > MAX_FILE_BYTES:
        return JSONResponse(
            {
                "ok": False,
                "error": f"file is {len(data)} bytes; limit is {MAX_FILE_BYTES}",
            },
            status_code=413,
        )

    meta = store.add_file(
        name=name,
        rel_path=(b.get("rel_path") or "").strip(),
        data=data,
        from_agent=frm,
        to_agent=to,
        is_text=bool(b.get("is_text", True)),
    )
    note = (b.get("note") or "").strip()
    msg = store.add_message(
        to_agent=to,
        from_agent=frm,
        kind="file",
        text=note or f"sent you {name}",
        context=b.get("rel_path") or "",
        request_id=(b.get("request_id") or "").strip(),
        file_id=meta["id"],
    )
    live = _deliver(msg)
    return JSONResponse(
        {"ok": True, "file_id": meta["id"], "size": meta["size"], "peer_online": live}
    )


async def download_file(request: Request) -> Response:
    if not _authorized(request):
        return _unauth()
    file_id = request.path_params["id"]
    meta = store.get_file_meta(file_id)
    data = store.get_file_bytes(file_id)
    if meta is None or data is None:
        return JSONResponse(
            {"ok": False, "error": f"no file with id {file_id!r}"}, status_code=404
        )
    store.mark_fetched(file_id)
    return JSONResponse(
        {"ok": True, "file": meta, "data": base64.b64encode(data).decode()}
    )


async def message(request: Request) -> JSONResponse:
    """Full, untruncated text of one message."""
    if not _authorized(request):
        return _unauth()
    msg = store.get_message(request.path_params["id"])
    if msg is None:
        return JSONResponse(
            {"ok": False, "error": f"no message with id {request.path_params['id']!r}"},
            status_code=404,
        )
    return JSONResponse({"ok": True, "message": msg})


async def stream(request: Request) -> Response:
    if not _authorized(request):
        return _unauth()
    agent = request.path_params["agent"]
    qp = request.query_params
    queue: asyncio.Queue = asyncio.Queue()

    async def events():
        _subscribers[agent].add(queue)
        # Ids are <host>:<dirname>, so two repos with the same folder name on one
        # machine collide -- and the second session silently joins the first one's
        # inbox. The broker is the only place that can see both.
        prev = next((a for a in store.list_agents() if a["name"] == agent), None)
        here = qp.get("cwd", "")
        if prev and prev.get("cwd") and here and prev["cwd"] != here:
            print(
                f"[hollerback] WARNING: agent {agent!r} previously connected from "
                f"{prev['cwd']!r} and is now connecting from {here!r}. Two workspaces "
                f"share this id and will share an inbox; rename one directory or set "
                f"HOLLERBACK_AGENT.",
                file=sys.stderr, flush=True,
            )
        store.touch_agent(
            agent,
            connected=True,
            session_id=qp.get("session_id", ""),
            cwd=qp.get("cwd", ""),
            host=qp.get("host", ""),
        )
        # Anything queued while this agent had no session connected.
        for msg in store.take_undelivered(agent):
            queue.put_nowait(msg)
        # A session nobody can identify is invisible in discover(), so peers
        # cannot tell whether it is worth asking. Nudge once per process, and
        # only if it has never said what it is.
        if agent not in _announce_nudged and not store.has_announced(agent):
            _announce_nudged.add(agent)
            queue.put_nowait(store.add_message(
                to_agent=agent, from_agent=NOTICE_SENDER, kind="note",
                text=(f"you are connected as '{agent}', but peers cannot see what you "
                      f"do. If you have the announce() tool, call it with a short "
                      f"description of this codebase and what you own, so discover() "
                      f"can route questions to you. If you do not, your plugin predates "
                      f"it -- re-run the installer."),
            ))
        try:
            yield b": hollerback connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    store.touch_agent(agent, connected=True)
                    yield b": keepalive\n\n"
                    continue
                store.mark_delivered(msg["id"])
                yield f"data: {json.dumps(msg, separators=(',', ':'))}\n\n".encode()
        finally:
            _subscribers[agent].discard(queue)
            if not _subscribers[agent]:
                _subscribers.pop(agent, None)
                store.touch_agent(agent, connected=False)
                asyncio.create_task(_notify_departure(agent))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def dashboard(request: Request) -> Response:
    """The human view: who is on the network and what they are saying."""
    page = pathlib.Path(__file__).with_name("dashboard.html")
    if not page.is_file():
        return JSONResponse({"ok": False, "error": "dashboard.html missing"}, status_code=404)
    return Response(page.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8")


async def dashboard_data(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "counts": store.counts(),
            "peers": store.list_agents(),
            "messages": store.recent_messages(40),
        }
    )


async def plugin_zip(request: Request) -> Response:
    if not PLUGIN_DIR.is_dir():
        return JSONResponse(
            {"ok": False, "error": f"plugin dir not found: {PLUGIN_DIR}"},
            status_code=404,
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PLUGIN_DIR.rglob("*")):
            if any(part in _ZIP_SKIP for part in path.parts):
                continue
            if path.is_file():
                zf.write(path, path.relative_to(PLUGIN_DIR).as_posix())
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="hollerback-plugin.zip"'},
    )


# The scripts ship with a 127.0.0.1 default so they still work when run from a local checkout.
# But a broker bound to a specific address (--bind 100.x.y.z, the normal Tailscale setup) is NOT
# reachable on 127.0.0.1, so anyone piping `curl <broker>/install.sh | bash` got the loopback default
# and a "CANNOT REACH BROKER" failure — while following the instructions exactly.
# The broker serves these scripts, so it knows the URL the caller reached it on: stamp that in as the
# default. Explicit --broker/-Broker still wins, since it overwrites the default at parse time.
_DEFAULT_BROKER_URL = "http://127.0.0.1:8850"

# Anchored on the ASSIGNMENT, not on a whole exact line. Matching the full line
# by substring already broke once, silently: install.sh's default became
# BROKER="${HOLLERBACK_BROKER:-http://127.0.0.1:8850}" and the old literal stopped
# matching, so every piped install silently got the loopback default back. The
# URL also appears in usage comments, so a bare replace of the first occurrence
# would rewrite documentation instead of the default.
# uninstall.sh / uninstall-windows.ps1 are absent on purpose: they contact no
# broker and have no such default. They were listed here and never matched.
_BROKER_ANCHORS = {
    "install.sh": re.compile(
        r"^(BROKER=[^\n]*?)" + re.escape(_DEFAULT_BROKER_URL), re.MULTILINE
    ),
    "install-windows.ps1": re.compile(
        r'^(\s*\[string\]\$Broker\s*=\s*")' + re.escape(_DEFAULT_BROKER_URL),
        re.MULTILINE,
    ),
}


def _public_base_url(request: Request) -> str | None:
    """The base URL the client actually reached us on, from the Host header it sent."""
    host = request.headers.get("host")

    if not host:
        return None

    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{scheme}://{host}"


def _serve_script(name: str, request: Request | None = None) -> Response:
    script = PLUGIN_DIR.parent / name
    if not script.is_file():
        return JSONResponse({"ok": False, "error": f"{name} not found"}, status_code=404)

    text = script.read_text()
    anchor = _BROKER_ANCHORS.get(name)
    base = _public_base_url(request) if request is not None else None

    if anchor is not None and base:
        text, n = anchor.subn(lambda m: m.group(1) + base, text, count=1)
        if n == 0:
            # Say so instead of quietly serving a script that points at loopback.
            # This is the only signal that the anchor has drifted from the file.
            print(
                f"[hollerback] WARNING: {name} has no recognisable broker default; "
                f"serving it unmodified, so a piped install will fall back to "
                f"{_DEFAULT_BROKER_URL} unless --broker is passed explicitly.",
                file=sys.stderr,
                flush=True,
            )

    return Response(text, media_type="text/plain; charset=utf-8")


async def install_ps1(request: Request) -> Response:
    return _serve_script("install-windows.ps1", request)


async def install_sh(request: Request) -> Response:
    return _serve_script("install.sh", request)


async def uninstall_ps1(request: Request) -> Response:
    return _serve_script("uninstall-windows.ps1", request)


async def uninstall_sh(request: Request) -> Response:
    return _serve_script("uninstall.sh", request)


async def uninstall_broker_sh(request: Request) -> Response:
    return _serve_script("uninstall-broker.sh", request)


app = Starlette(
    routes=[
        Route("/", dashboard),
        Route("/v1/dashboard", dashboard_data),
        Route("/v1/health", health),
        Route("/v1/peers", peers),
        Route("/v1/ask", ask, methods=["POST"]),
        Route("/v1/answer", answer, methods=["POST"]),
        Route("/v1/note", note, methods=["POST"]),
        Route("/v1/announce", announce, methods=["POST"]),
        Route("/v1/forget", forget, methods=["POST"]),
        Route("/v1/send", send, methods=["POST"]),
        Route("/v1/pending/{agent}", pending),
        Route("/v1/message/{id}", message),
        Route("/v1/file", upload_file, methods=["POST"]),
        Route("/v1/file/{id}", download_file),
        Route("/v1/stream/{agent}", stream),
        Route("/v1/plugin.zip", plugin_zip),
        Route("/install.ps1", install_ps1),
        Route("/install.sh", install_sh),
        Route("/uninstall.ps1", uninstall_ps1),
        Route("/uninstall.sh", uninstall_sh),
        Route("/uninstall-broker.sh", uninstall_broker_sh),
    ],
    on_startup=[store.init],
)


def main() -> None:
    import uvicorn

    store.init()
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
