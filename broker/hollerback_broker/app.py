"""hollerback broker.

A message bus between concurrently-running Claude Code sessions. Two surfaces:

  POST /v1/ask             a session asks a peer a question (returns immediately)
  POST /v1/answer          a session answers a question it was asked
  POST /v1/note            fire-and-forget note, no reply expected
  GET  /v1/stream/{agent}  long-lived SSE feed of that agent's messages
  GET  /v1/peers           who is online, where, and what they owe answers on
  GET  /v1/pending/{agent} open questions (used by the read-only permission hook)
  GET  /v1/plugin.zip      the plugin, so a peer machine can install without SSH
  GET  /install.ps1        the Windows installer

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
import time
import zipfile
from collections import defaultdict

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import store

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


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# --- routes -----------------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "peers": store.list_agents()})


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

    known = {a["name"] for a in store.list_agents()}
    msg = store.add_message(to, frm, "question", text, b.get("context") or "")
    live = _deliver(msg)

    if live:
        note = f"delivered to {to} now"
    elif to in known:
        note = f"{to} is not connected right now; it will get this when its session starts"
    else:
        note = (
            f"no session named {to!r} has ever connected"
            f" (known peers: {sorted(known) or 'none'}) — check the name"
        )
    return JSONResponse(
        {"ok": True, "request_id": msg["id"], "peer_online": live, "note": note}
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
    if to == "*":
        targets = [a["name"] for a in store.list_agents() if a["name"] != frm]
        if not targets:
            return JSONResponse(
                {"ok": False, "error": "no other peers have ever connected"},
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
_BROKER_DEFAULTS = {
    "install.sh": 'BROKER="http://127.0.0.1:8850"',
    "uninstall.sh": 'BROKER="http://127.0.0.1:8850"',
    "install-windows.ps1": '[string]$Broker    = "http://127.0.0.1:8850",',
    "uninstall-windows.ps1": '[string]$Broker    = "http://127.0.0.1:8850",',
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
    default = _BROKER_DEFAULTS.get(name)
    base = _public_base_url(request) if request is not None else None

    if default and base and default in text:
        text = text.replace(default, default.replace("http://127.0.0.1:8850", base), 1)

    return Response(text, media_type="text/plain; charset=utf-8")


async def install_ps1(request: Request) -> Response:
    return _serve_script("install-windows.ps1", request)


async def install_sh(request: Request) -> Response:
    return _serve_script("install.sh", request)


async def uninstall_ps1(request: Request) -> Response:
    return _serve_script("uninstall-windows.ps1", request)


async def uninstall_sh(request: Request) -> Response:
    return _serve_script("uninstall.sh", request)


app = Starlette(
    routes=[
        Route("/", dashboard),
        Route("/v1/dashboard", dashboard_data),
        Route("/v1/health", health),
        Route("/v1/peers", peers),
        Route("/v1/ask", ask, methods=["POST"]),
        Route("/v1/answer", answer, methods=["POST"]),
        Route("/v1/note", note, methods=["POST"]),
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
    ],
    on_startup=[store.init],
)


def main() -> None:
    import uvicorn

    store.init()
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
