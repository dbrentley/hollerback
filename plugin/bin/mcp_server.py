#!/usr/bin/env python3
"""hollerback MCP server (stdio, JSON-RPC 2.0 over stdin/stdout).

Gives each Claude Code session the tools to talk to its peer:
    holler       ask a question, return immediately, get notified later
    holler_back  answer a question this session was asked
    check_inbox  what am I owed / what do I owe
    announce     say what this session is, so peers can find it
    discover     who is out there and what each one does

stdio rather than HTTP on purpose: stdio servers inherit CLAUDE_PROJECT_DIR and
CLAUDE_CODE_SESSION_ID from the host, so a session can identify itself. HTTP
servers get neither, and their headers are static per config file.

Standard library only -- must run on Windows and Linux with bare python3, and
`pip install mcp` is not guaranteed on either box.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# JSON-RPC is UTF-8 by spec, but Python on Windows defaults stdio to the locale
# codepage (cp1252 here), which silently mangles any non-ASCII the model writes
# -- an em-dash arrives as "â€”". Force UTF-8 on both directions.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - very old pythons
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urllib.parse  # noqa: E402

from _common import clear_open_question, http_json, load_config, log  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
MAX_FILE_BYTES = int(os.environ.get("HOLLERBACK_MAX_FILE_BYTES", str(10 * 1024 * 1024)))

CFG = load_config()
AGENT = CFG["agent"]
BROKER = CFG["broker"]
TOKEN = CFG["token"]


def project_root() -> pathlib.Path:
    """The workspace this session is attached to.

    stdio MCP servers are handed CLAUDE_PROJECT_DIR by the host -- one of the
    main reasons this server is stdio rather than HTTP.
    """
    return pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


def resolve_in_project(path: str) -> pathlib.Path | str:
    """Resolve a path, refusing anything outside the project.

    This is a real boundary, not tidiness: `request_file` lets the PEER name a
    path, and a confused or compromised peer asking for ~/.ssh/id_rsa must not
    be able to talk this session into sending it. Returns an error string
    rather than a path when the target escapes the workspace.
    """
    root = project_root()
    candidate = pathlib.Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"could not resolve {path!r}: {exc}"
    if resolved != root and root not in resolved.parents:
        return (
            f"refusing: {resolved} is outside this session's project ({root}). "
            "hollerback only shares files from within the workspace."
        )
    return resolved


def relative_label(p: pathlib.Path) -> str:
    try:
        return p.relative_to(project_root()).as_posix()
    except ValueError:
        return p.name

TOOLS = [
    {
        "name": "holler",
        "description": (
            "Ask the other Claude Code session a question about its side of the "
            "codebase, then keep working -- this returns immediately and does NOT "
            "block. The peer answers at its next stopping point and the answer "
            "arrives later as a notification, like a background agent finishing.\n\n"
            "Use it for things only the live peer session knows: what it just "
            "decided, what it is midway through changing, why an interface looks "
            "the way it does. For questions the code already answers, just read "
            "the code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Peer id from discover(), or any unique part of one. Call discover() first.",
                },
                "question": {
                    "type": "string",
                    "description": "The question. Be specific and self-contained -- the peer cannot see your screen.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional: what you are working on, so the peer can aim its answer.",
                },
            },
            "required": ["peer", "question"],
        },
    },
    {
        "name": "holler_back",
        "description": (
            "Answer a question a peer session asked you. Use the request_id from "
            "the [holler] notification. Answer from what you know and from "
            "reading files; if you would need to run commands or make changes to "
            "be sure, say so plainly rather than guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "From the notification."},
                "text": {"type": "string", "description": "Your answer."},
            },
            "required": ["request_id", "text"],
        },
    },
    {
        "name": "read_message",
        "description": (
            "Read the full, untruncated text of a message by its id. Notifications "
            "are truncated for display, so use this whenever an [holler] line "
            "looks cut off or you need the exact wording. For a question it also "
            "returns the answer; for an answer it returns the original question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The msg= or request_id= value from the notification.",
                }
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "send_file",
        "description": (
            "Send a file to the peer session -- a spec, a doc you wrote for them, a "
            "log, a patch, a schema. Use this instead of pasting long file contents "
            "into a question. Path is relative to your project root (or absolute). "
            "The peer gets a notification and saves it with get_file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string", "description": "Who to send it to."},
                "path": {"type": "string", "description": "File to send, relative to the project root."},
                "note": {"type": "string", "description": "Why you are sending it."},
                "request_id": {
                    "type": "string",
                    "description": "Optional: if this file answers a question, its request_id.",
                },
            },
            "required": ["peer", "path"],
        },
    },
    {
        "name": "get_file",
        "description": (
            "Save a file the peer sent you, using the file_id from the notification. "
            "It is written under .hollerback/inbox/ in your project so you can Read "
            "it with normal tools; this returns the path rather than the contents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "From the notification."},
                "save_as": {
                    "type": "string",
                    "description": "Optional path (relative to project root) to save to instead.",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "request_file",
        "description": (
            "Ask the peer to send you a specific file from their side of the stack. "
            "Returns immediately; they send it when they next have the floor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {"type": "string"},
                "path": {"type": "string", "description": "Path as you believe it exists on their side."},
                "reason": {"type": "string", "description": "Why you need it."},
            },
            "required": ["peer", "path"],
        },
    },
    {
        "name": "check_inbox",
        "description": (
            "Everything outstanding for this session: questions you have been "
            "asked and not answered, questions you asked that have no answer yet, "
            "and files a peer sent that you have not saved."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "discover",
        "description": (
            "Find out which other Claude Code sessions exist and what each one is "
            "for. Call this BEFORE holler/send_file/request_file -- peer ids are "
            "derived from host and directory, not chosen, so you cannot guess "
            "them. Shows each session's self-description, whether it is online, "
            "and what it owes answers on. Safe to call any time; it reads stored "
            "state, so it is complete no matter who announced when."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "announce",
        "description": (
            "Tell the network what THIS session is, so peers can find you with "
            "discover(). Do this once, early -- a session nobody can identify does "
            "not get asked anything. Describe the codebase you are in, what you "
            "own, and what you can answer authoritatively: e.g. 'OpenDAoC game "
            "server. Combat, spells and NPC AI in GameServer/. Can answer about "
            "packet handlers and the DB schema.' It is stored, not broadcast, so "
            "sessions that start later still see it. Call again to update."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "capabilities": {
                    "type": "string",
                    "description": "What this session is and what it can answer. A few sentences.",
                }
            },
            "required": ["capabilities"],
        },
    },
    {
        "name": "tell_peer",
        "description": (
            "Send a heads-up that expects no reply, e.g. 'I just changed the "
            "/orders payload shape'. Set peer to \"*\" to tell EVERY connected "
            "session at once -- use that for changes that affect the whole system. "
            "Use sparingly either way; it interrupts them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": 'Peer id from discover(), or any unique part of one. Use "*" to broadcast to everyone.',
                },
                "note": {"type": "string"},
            },
            "required": ["peer", "note"],
        },
    },
]


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def call_tool(name: str, args: dict) -> dict:
    if not BROKER:
        # AGENT cannot be missing -- it is derived. Only the broker URL can be.
        return _err(
            "hollerback has no broker configured. Set BROKER_URL via the installer, "
            "/plugin, ~/.hollerback.json, or HOLLERBACK_BROKER."
        )

    try:
        if name == "holler":
            peer = (args.get("peer") or "").strip()
            question = (args.get("question") or "").strip()
            if not peer or not question:
                return _err("holler needs both 'peer' and 'question'.")
            if peer == AGENT:
                return _err(f"You are '{AGENT}' -- you cannot ask yourself.")
            r = http_json(
                f"{BROKER}/v1/ask",
                {
                    "from": AGENT,
                    "to": peer,
                    "text": question,
                    "context": args.get("context") or "",
                },
                TOKEN,
            )
            if not r.get("ok"):
                return _err(f"broker rejected the question: {r.get('error')}")
            msg = (
                f"Question sent to '{peer}' (request_id={r['request_id']}).\n"
                f"{r.get('note', '')}\n\n"
                "Do NOT wait for the reply -- carry on with your work. The answer "
                "will arrive on its own as an [holler] notification."
            )
            return _ok(msg)

        if name == "holler_back":
            rid = (args.get("request_id") or "").strip()
            text = (args.get("text") or "").strip()
            if not rid or not text:
                return _err("holler_back needs both 'request_id' and 'text'.")
            r = http_json(
                f"{BROKER}/v1/answer", {"from": AGENT, "request_id": rid, "text": text}, TOKEN
            )
            if not r.get("ok"):
                return _err(f"could not answer: {r.get('error')}")
            # Closes the read-only auto-allow window for this question.
            clear_open_question(rid)
            where = "delivered now" if r.get("delivered_now") else "queued until their session is up"
            return _ok(f"Answer sent to '{r.get('to')}' ({where}).")

        if name == "send_file":
            peer = (args.get("peer") or "").strip()
            rel = (args.get("path") or "").strip()
            if not peer or not rel:
                return _err("send_file needs both 'peer' and 'path'.")
            src = resolve_in_project(rel)
            if isinstance(src, str):
                return _err(src)
            if not src.is_file():
                return _err(f"no such file: {src}")
            data = src.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                return _err(
                    f"{src.name} is {len(data):,} bytes; the limit is {MAX_FILE_BYTES:,}. "
                    "Send an excerpt, or split it."
                )
            try:
                data.decode("utf-8")
                is_text = True
            except UnicodeDecodeError:
                is_text = False
            r = http_json(
                f"{BROKER}/v1/file",
                {
                    "from": AGENT,
                    "to": peer,
                    "name": src.name,
                    "rel_path": relative_label(src),
                    "data": base64.b64encode(data).decode(),
                    "is_text": is_text,
                    "note": args.get("note") or "",
                    "request_id": (args.get("request_id") or "").strip(),
                },
                TOKEN,
                timeout=120,
            )
            if not r.get("ok"):
                return _err(f"upload failed: {r.get('error')}")
            where = "delivered now" if r.get("peer_online") else "queued until their session is up"
            return _ok(
                f"Sent {src.name} ({r['size']:,} bytes) to '{peer}' — {where}. "
                f"file_id={r['file_id']}"
            )

        if name == "get_file":
            fid = (args.get("file_id") or "").strip()
            if not fid:
                return _err("get_file needs 'file_id'.")
            r = http_json(f"{BROKER}/v1/file/{fid}", None, TOKEN, timeout=120)
            if not r.get("ok"):
                return _err(r.get("error", "not found"))
            meta = r["file"]
            blob = base64.b64decode(r["data"])
            save_as = (args.get("save_as") or "").strip()
            if save_as:
                dest = resolve_in_project(save_as)
                if isinstance(dest, str):
                    return _err(dest)
            else:
                dest = project_root() / ".hollerback" / "inbox" / meta["name"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            # The inbox lives inside the user's repo so Claude Code can Read it;
            # self-ignore so received files never show up in their diffs.
            marker = project_root() / ".hollerback" / ".gitignore"
            if not marker.exists():
                try:
                    marker.write_text("# received via hollerback; not part of the repo\n*\n")
                except OSError:
                    pass
            dest.write_bytes(blob)
            note = [
                f"Saved {meta['name']} ({meta['size']:,} bytes) from {meta['from']} to:",
                f"  {dest}",
                "Read it with the normal Read tool.",
            ]
            if meta.get("rel_path"):
                note.append(f"(their path: {meta['rel_path']})")
            return _ok("\n".join(note))

        if name == "request_file":
            peer = (args.get("peer") or "").strip()
            path = (args.get("path") or "").strip()
            if not peer or not path:
                return _err("request_file needs both 'peer' and 'path'.")
            reason = (args.get("reason") or "").strip()
            text = (
                f"Please send me the file `{path}` from your side"
                + (f" — {reason}" if reason else "")
                + ". Use the hollerback `send_file` tool with that path"
                " (and pass this request_id so I can thread it)."
            )
            r = http_json(
                f"{BROKER}/v1/ask", {"from": AGENT, "to": peer, "text": text}, TOKEN
            )
            if not r.get("ok"):
                return _err(f"broker rejected the request: {r.get('error')}")
            return _ok(
                f"Asked '{peer}' for {path} (request_id={r['request_id']}). "
                f"{r.get('note','')} Carry on -- it will arrive as a notification."
            )

        if name == "read_message":
            mid = (args.get("message_id") or "").strip()
            if not mid:
                return _err("read_message needs 'message_id'.")
            r = http_json(f"{BROKER}/v1/message/{mid}", None, TOKEN)
            if not r.get("ok"):
                return _err(r.get("error", "not found"))
            m = r["message"]
            out = [f"{m['kind'].upper()} {m['id']} from {m['from']} to {m['to']}:", m["text"]]
            if m.get("in_reply_to"):
                q = m["in_reply_to"]
                out += ["", f"-- in reply to your question {q['id']}:", q["text"]]
            if m.get("answer"):
                a = m["answer"]
                out += ["", f"-- answered by {a['from']}:", a["text"]]
            elif m["kind"] == "question" and not m.get("answered"):
                out += ["", "(not answered yet)"]
            return _ok("\n".join(out))

        if name == "check_inbox":
            # AGENT lands in a URL PATH here, unlike every other call which puts it
            # in a JSON body. It contains ':' by construction and can contain
            # anything a directory name can, so it has to be encoded.
            r = http_json(
                f"{BROKER}/v1/pending/{urllib.parse.quote(AGENT, safe='')}",
                None, TOKEN,
            )
            if not r.get("ok"):
                return _err(f"could not reach the broker: {r.get('error', 'unknown error')}")
            owed = r.get("open_questions", [])
            waiting = r.get("awaiting_answers", [])
            files = r.get("pending_files", [])
            if not owed and not waiting and not files:
                return _ok("Inbox clear: nothing to answer, no files waiting, nothing outstanding.")
            lines = []
            if files:
                lines.append("Files sent to you, not saved yet:")
                for f in files:
                    lines.append(
                        f"  [{f['id']}] {f['name']} ({f['size']:,} bytes) from {f['from']}"
                        f" -- get_file(file_id=\"{f['id']}\")"
                    )
            if owed:
                lines.append("Questions waiting on YOUR answer:")
                for q in owed:
                    lines.append(f"  [{q['id']}] from {q['from']}: {q['text']}")
            if waiting:
                lines.append("Questions you asked, still unanswered:")
                for q in waiting:
                    lines.append(f"  [{q['id']}] to {q['to']}: {q['text']}")
            return _ok("\n".join(lines))

        if name == "discover":
            r = http_json(f"{BROKER}/v1/peers", None, TOKEN)
            if not r.get("ok"):
                return _err(f"could not reach the broker: {r.get('error', 'unknown error')}")
            rows = list(r.get("peers", []))
            # Always lead with your own id. It is derived, not configured, so this
            # is the only place a session can find out what peers will call it.
            lines = [f"You are '{AGENT}'.", ""]
            if not rows:
                lines += [
                    "No sessions have connected to this broker yet.",
                    "Call announce() anyway -- it is stored, so peers starting later",
                    "will still see what you are.",
                ]
                return _ok("\n".join(lines))
            for p in rows:
                me = "  (this session)" if p["name"] == AGENT else ""
                state = "online" if p["online"] else f"offline {int(p['seconds_since_seen'])}s"
                owes = f", owes {p['open_questions']} answer(s)" if p["open_questions"] else ""
                lines.append(f"{p['name']}  [{state}{owes}]{me}")
                cap = (p.get("capabilities") or "").strip()
                lines.append(f"    {cap}" if cap else
                             "    (has not announced -- unknown what it does)")
                if p.get("cwd"):
                    lines.append(f"    dir: {p['cwd']}")
            lines += ["", "Address a peer by its id, or any unique part of one."]
            if not (next((p for p in rows if p["name"] == AGENT), {}) or {}).get("capabilities"):
                lines.append("You have not announced yet -- peers cannot see what you do.")
            return _ok("\n".join(lines))

        if name == "announce":
            text = (args.get("capabilities") or "").strip()
            if not text:
                return _err("announce needs 'capabilities' -- what is this session for?")
            r = http_json(
                f"{BROKER}/v1/announce", {"from": AGENT, "capabilities": text}, TOKEN
            )
            if not r.get("ok"):
                err = str(r.get("error", ""))
                if "404" in err or "not found" in err.lower():
                    return _err(
                        "this broker has no /v1/announce -- it is older than this "
                        "plugin. Update it (install-broker.sh) and try again. "
                        "Everything else still works."
                    )
                return _err(f"could not announce: {err}")
            return _ok(
                f"Announced as '{AGENT}'. Peers calling discover() now see:\n  {text}"
            )

        if name == "tell_peer":
            peer = (args.get("peer") or "").strip()
            note = (args.get("note") or "").strip()
            if not peer or not note:
                return _err("tell_peer needs both 'peer' and 'note'.")
            r = http_json(f"{BROKER}/v1/note", {"from": AGENT, "to": peer, "text": note}, TOKEN)
            if not r.get("ok"):
                return _err(f"broker rejected the note: {r.get('error')}")
            if r.get("broadcast"):
                who = ", ".join(
                    f"{s['to']}{'' if s['delivered_now'] else ' (queued)'}"
                    for s in r.get("sent", [])
                )
                return _ok(f"Broadcast to {len(r.get('sent', []))} peer(s): {who}")
            return _ok(f"Note sent to '{peer}'.")

        return _err(f"unknown tool: {name}")

    except urllib.error.URLError as exc:
        return _err(
            f"Cannot reach the hollerback broker at {BROKER} ({exc}). "
            "The peer cannot be reached right now; continue without it."
        )
    except Exception as exc:  # noqa: BLE001
        return _err(f"hollerback error: {exc}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hollerback", "version": "0.2.0"},
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    log(f"mcp server up (agent={AGENT or '<unset>'}, broker={BROKER or '<unset>'})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            resp = handle(req)
        except Exception as exc:  # noqa: BLE001
            log(f"handler crashed: {exc}")
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
