"""SQLite persistence for hollerback.

In-memory queues lose in-flight questions whenever the broker restarts (and it
restarts on every code change, since the unit is Restart=always). A question
you asked and then forgot about because the broker bounced is worse than no
system at all, so durability lives here.

WAL mode, short-lived connections, no ORM.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import time
import uuid

DB_PATH = pathlib.Path(
    os.getenv("HOLLERBACK_DB", str(pathlib.Path.home() / ".local/share/hollerback/hollerback.db"))
)

# Presence is a claim about a live SSE connection, so it has to be able to go
# stale on its own. The stream refreshes last_seen on every keepalive, so an
# agent that has missed two of them is not there -- whatever the connected flag
# says. Without this, a broker killed mid-stream leaves the flag set and the peer
# reads "online" forever.
KEEPALIVE_SECS = int(os.getenv("HOLLERBACK_KEEPALIVE_SECS", "20"))
PRESENCE_GRACE_SECS = float(
    os.getenv("HOLLERBACK_PRESENCE_GRACE_SECS", str(max(2 * KEEPALIVE_SECS + 5, 30)))
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    to_agent    TEXT NOT NULL,
    from_agent  TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- question | answer | note
    text        TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '',
    request_id  TEXT NOT NULL DEFAULT '',  -- answers point at their question
    created_at  REAL NOT NULL,
    delivered_at REAL,                  -- set when a live stream took it
    answered_at REAL                    -- questions only
);
CREATE INDEX IF NOT EXISTS idx_messages_undelivered
    ON messages(to_agent, delivered_at);
CREATE INDEX IF NOT EXISTS idx_messages_open_questions
    ON messages(to_agent, kind, answered_at);

CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,          -- basename as sent
    rel_path    TEXT NOT NULL DEFAULT '', -- path relative to the sender's project
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL DEFAULT '',
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    is_text     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    fetched_at  REAL
);

CREATE TABLE IF NOT EXISTS agents (
    name         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    cwd          TEXT NOT NULL DEFAULT '',
    host         TEXT NOT NULL DEFAULT '',
    last_seen    REAL NOT NULL,
    connected    INTEGER NOT NULL DEFAULT 0,
    -- What this session says it is. Stored rather than broadcast on purpose: an
    -- announcement is useless to anyone who connects afterwards, and peers join
    -- and leave constantly. discover() reads this, so arrival order stops
    -- mattering.
    capabilities TEXT NOT NULL DEFAULT '',
    announced_at REAL
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


BLOB_DIR = DB_PATH.parent / "files"


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        # Additive migration for databases created before file sharing existed.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        if "file_id" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN file_id TEXT NOT NULL DEFAULT ''")
        fcols = {r["name"] for r in c.execute("PRAGMA table_info(files)")}
        if fcols and "fetched_at" not in fcols:
            c.execute("ALTER TABLE files ADD COLUMN fetched_at REAL")
        # No SSE connection can outlive the process that held it, so nothing is
        # legitimately "connected" at startup. A clean shutdown clears the flag in
        # the stream's finally block; a crash, a kill -9 or an OOM does not -- and
        # the unit is Restart=always, so that path is routine. Anything still set
        # here is a leftover lie about an agent that may never come back.
        c.execute("UPDATE agents SET connected=0 WHERE connected=1")
        acols = {r["name"] for r in c.execute("PRAGMA table_info(agents)")}
        if acols and "capabilities" not in acols:
            c.execute("ALTER TABLE agents ADD COLUMN capabilities TEXT NOT NULL DEFAULT ''")
        if acols and "announced_at" not in acols:
            c.execute("ALTER TABLE agents ADD COLUMN announced_at REAL")
    BLOB_DIR.mkdir(parents=True, exist_ok=True)


def set_capabilities(name: str, text: str, session_id: str = "") -> tuple[bool, str]:
    """Record what an agent says it is. Survives its disconnection on purpose.

    Refuses a takeover: if this name was announced by a DIFFERENT session, the
    write is rejected rather than silently overwriting. Two sessions sharing a
    working directory used to derive one id and clobber each other here, so
    discover() showed one of them wearing the other's description.

    Returns (accepted, reason).
    """
    now = time.time()
    with _conn() as c:
        r = c.execute(
            "SELECT session_id, capabilities FROM agents WHERE name=?", (name,)
        ).fetchone()
        if (r is not None and session_id and (r["capabilities"] or "").strip()
                and r["session_id"] and r["session_id"] != session_id):
            return False, (
                f"'{name}' is already announced by a different session "
                f"({r['session_id'][:8]}...). Refusing to overwrite it -- two "
                f"sessions are sharing one id, so neither can be addressed "
                f"reliably. Restart this session so it derives its own id, or set "
                f"HOLLERBACK_AGENT to something distinct."
            )
        c.execute(
            "INSERT INTO agents (name,session_id,last_seen,connected,capabilities,announced_at)"
            " VALUES (?,?,?,0,?,?)"
            " ON CONFLICT(name) DO UPDATE SET capabilities=excluded.capabilities,"
            "  announced_at=excluded.announced_at,"
            "  session_id=CASE WHEN excluded.session_id!='' THEN excluded.session_id"
            "                  ELSE agents.session_id END",
            (name, session_id, now, text, now),
        )
    return True, ""


def has_announced(name: str) -> bool:
    with _conn() as c:
        r = c.execute("SELECT capabilities FROM agents WHERE name=?", (name,)).fetchone()
    return bool(r and (r["capabilities"] or "").strip())


def add_file(
    name: str,
    rel_path: str,
    data: bytes,
    from_agent: str,
    to_agent: str,
    is_text: bool,
) -> dict:
    import hashlib

    file_id = uuid.uuid4().hex[:10]
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    (BLOB_DIR / file_id).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO files (id,name,rel_path,size,sha256,from_agent,to_agent,is_text,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (file_id, name, rel_path, len(data), digest, from_agent, to_agent, int(is_text), now),
        )
    return {
        "id": file_id,
        "name": name,
        "rel_path": rel_path,
        "size": len(data),
        "sha256": digest,
        "from": from_agent,
        "to": to_agent,
        "is_text": is_text,
    }


def get_file_meta(file_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if r is None:
        return None
    return {
        "id": r["id"],
        "name": r["name"],
        "rel_path": r["rel_path"],
        "size": r["size"],
        "sha256": r["sha256"],
        "from": r["from_agent"],
        "to": r["to_agent"],
        "is_text": bool(r["is_text"]),
    }


def get_file_bytes(file_id: str) -> bytes | None:
    path = BLOB_DIR / file_id
    return path.read_bytes() if path.is_file() else None


def _row_to_msg(r: sqlite3.Row) -> dict:
    msg = {
        "id": r["id"],
        "to": r["to_agent"],
        "from": r["from_agent"],
        "kind": r["kind"],
        "text": r["text"],
        "context": r["context"],
        "request_id": r["request_id"],
        "ts": r["created_at"],
    }
    keys = r.keys()
    if "file_id" in keys and r["file_id"]:
        msg["file_id"] = r["file_id"]
        meta = get_file_meta(r["file_id"])
        if meta:
            msg["file"] = meta
    return msg


def add_message(
    to_agent: str,
    from_agent: str,
    kind: str,
    text: str,
    context: str = "",
    request_id: str = "",
    file_id: str = "",
) -> dict:
    msg_id = uuid.uuid4().hex[:8]
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO messages (id,to_agent,from_agent,kind,text,context,request_id,created_at,file_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (msg_id, to_agent, from_agent, kind, text, context, request_id, now, file_id),
        )
    out_file = get_file_meta(file_id) if file_id else None
    return {
        **({"file_id": file_id, "file": out_file} if out_file else {}),
        "id": msg_id,
        "to": to_agent,
        "from": from_agent,
        "kind": kind,
        "text": text,
        "context": context,
        "request_id": request_id,
        "ts": now,
    }


def mark_delivered(msg_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE messages SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
            (time.time(), msg_id),
        )


def take_undelivered(to_agent: str) -> list[dict]:
    """What to hand a session the moment it attaches.

    Two categories, and the second one matters more than it looks:

    1. Anything never written to a stream at all. Rare now that sends to an
       absent session are refused outright, but a message can still race a
       subscriber disappearing between the check and the write.
    2. Any QUESTION that was written to a stream but is still unanswered.
    3. Any FILE that was announced but never actually fetched.

    (2) exists because "delivered" only ever meant "bytes were written". It did
    not mean the peer's model ever saw it -- a monitor that is starting up, or
    a plugin that failed to load, swallows the line silently and the question is
    lost forever. Re-offering unanswered questions on each fresh attach makes
    that self-healing: the worst case is the peer sees a reminder for something
    it is already working on, and answering clears it for good.

    Answers and notes are deliberately NOT re-sent -- they are not actionable,
    so repeating them would just be noise.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT m.* FROM messages m"
            " LEFT JOIN files f ON f.id = m.file_id"
            " WHERE m.to_agent=?"
            " AND ("
            "   m.delivered_at IS NULL"
            "   OR (m.kind='question' AND m.answered_at IS NULL)"
            "   OR (m.kind='file' AND f.fetched_at IS NULL)"
            " )"
            " AND NOT (m.kind='question' AND m.answered_at IS NOT NULL)"
            " ORDER BY m.created_at",
            (to_agent,),
        ).fetchall()
        now = time.time()
        for r in rows:
            c.execute("UPDATE messages SET delivered_at=? WHERE id=?", (now, r["id"]))
    return [_row_to_msg(r) for r in rows]


def get_message(msg_id: str) -> dict | None:
    """Any message by id, with its answer attached if it is an answered question.

    Exists because task notifications are truncated for display, so a long
    answer can arrive unreadable. The id is always in the notification line.
    """
    with _conn() as c:
        r = c.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if r is None:
            return None
        msg = _row_to_msg(r)
        msg["answered"] = r["answered_at"] is not None
        if r["kind"] == "question":
            a = c.execute(
                "SELECT * FROM messages WHERE kind='answer' AND request_id=?"
                " ORDER BY created_at DESC LIMIT 1",
                (msg_id,),
            ).fetchone()
            if a is not None:
                msg["answer"] = _row_to_msg(a)
        elif r["request_id"]:
            q = c.execute(
                "SELECT * FROM messages WHERE id=?", (r["request_id"],)
            ).fetchone()
            if q is not None:
                msg["in_reply_to"] = _row_to_msg(q)
    return msg


def get_question(request_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM messages WHERE id=? AND kind='question'", (request_id,)
        ).fetchone()
    return _row_to_msg(r) if r else None


def mark_answered(request_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE messages SET answered_at=? WHERE id=? AND answered_at IS NULL",
            (time.time(), request_id),
        )


def open_questions(to_agent: str) -> list[dict]:
    """Questions this agent has been asked and has not answered yet."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE to_agent=? AND kind='question'"
            " AND answered_at IS NULL ORDER BY created_at",
            (to_agent,),
        ).fetchall()
    return [_row_to_msg(r) for r in rows]


def awaiting_answers(from_agent: str) -> list[dict]:
    """Questions this agent asked that are still unanswered."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE from_agent=? AND kind='question'"
            " AND answered_at IS NULL ORDER BY created_at",
            (from_agent,),
        ).fetchall()
    return [_row_to_msg(r) for r in rows]


def touch_agent(
    name: str, connected: bool, session_id: str = "", cwd: str = "", host: str = ""
) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO agents (name,session_id,cwd,host,last_seen,connected)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen,"
            "  connected=excluded.connected,"
            "  session_id=CASE WHEN excluded.session_id!='' THEN excluded.session_id ELSE agents.session_id END,"
            "  cwd=CASE WHEN excluded.cwd!='' THEN excluded.cwd ELSE agents.cwd END,"
            "  host=CASE WHEN excluded.host!='' THEN excluded.host ELSE agents.host END",
            (name, session_id, cwd, host, now, 1 if connected else 0),
        )


def forget_agent(name: str) -> bool:
    """Remove an agent record. Messages are left alone -- they are history."""
    with _conn() as c:
        cur = c.execute("DELETE FROM agents WHERE name=?", (name,))
        return cur.rowcount > 0


def list_agents() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY name").fetchall()
    now = time.time()
    out = []
    for r in rows:
        keys = r.keys()
        since = now - r["last_seen"]
        # The flag alone is not evidence -- see PRESENCE_GRACE_SECS. A suspended
        # laptop or a severed link can leave the flag set long after the peer is
        # unreachable, and reporting that as "online" is worse than saying nothing.
        out.append(
            {
                "name": r["name"],
                "online": bool(r["connected"]) and since <= PRESENCE_GRACE_SECS,
                "cwd": r["cwd"],
                "host": r["host"],
                "session_id": r["session_id"],
                "capabilities": (r["capabilities"] if "capabilities" in keys else "") or "",
                "last_seen": r["last_seen"],
                "seconds_since_seen": round(since, 1),
                "open_questions": len(open_questions(r["name"])),
            }
        )
    return out


def mark_fetched(file_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE files SET fetched_at=? WHERE id=? AND fetched_at IS NULL",
            (time.time(), file_id),
        )


def pending_files(to_agent: str) -> list[dict]:
    """Files sent to this agent that it has not saved yet.

    check_inbox reported only questions, so a session could be told its inbox
    was "clear" while files sat waiting -- which is exactly what the frontend
    session reported. Anything the peer is waiting on us to act on belongs here.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM files WHERE to_agent=? AND fetched_at IS NULL"
            " ORDER BY created_at",
            (to_agent,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "rel_path": r["rel_path"],
            "size": r["size"],
            "from": r["from_agent"],
        }
        for r in rows
    ]


def recent_messages(limit: int = 40) -> list[dict]:
    """Newest-first activity feed for the dashboard."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        m = _row_to_msg(r)
        m["answered"] = r["answered_at"] is not None
        m["delivered"] = r["delivered_at"] is not None
        out.append(m)
    return out


def counts() -> dict:
    with _conn() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "peers_online": q("SELECT COUNT(*) FROM agents WHERE connected=1"),
            "peers_total": q("SELECT COUNT(*) FROM agents"),
            "open_questions": q(
                "SELECT COUNT(*) FROM messages WHERE kind='question' AND answered_at IS NULL"
            ),
            "messages_total": q("SELECT COUNT(*) FROM messages"),
            "messages_24h": q(
                "SELECT COUNT(*) FROM messages WHERE created_at > ?", time.time() - 86400
            ),
            "files_total": q("SELECT COUNT(*) FROM files"),
            "files_unfetched": q("SELECT COUNT(*) FROM files WHERE fetched_at IS NULL"),
            "bytes_shared": q("SELECT COALESCE(SUM(size),0) FROM files"),
        }
