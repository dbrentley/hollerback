# hollerback

**Let your Claude Code sessions holler at each other.**

If you run more than one Claude Code session — frontend and backend, two repos,
two machines — you are the router. The frontend session needs the exact shape of
an error envelope; the backend session knows it; you copy-paste between
terminals, and it goes stale the moment either side changes something.

hollerback removes you from that loop. One session asks, keeps working, and gets
the answer back as a notification:

```
holler(peer="backend", question="How does the order service decide idempotency for POST /orders?")
→ Question sent to 'backend' (request_id=8f24). Do NOT wait -- carry on.

  ...you keep working...

[holler] ANSWER from the backend session (re: your question 8f24):
Idempotency-Key header, 24h dedupe window in Redis (src/orders/handler.py:88).
I did not run the tests, so I can't confirm the window from here.
```

Nobody typed anything. The peer session was idle, woke up on its own, read its
own code, and answered.

It also moves files, so neither side has to paste a schema into a chat message.

## How it wakes an idle session

This is the part that isn't obvious. A plugin can declare a **`monitors`** entry —
a background process armed automatically at session start, where **every line it
prints to stdout is delivered to the model as a `<task_notification>`**. Those
notifications re-invoke an agent that is sitting idle at the prompt.

So `bin/listen.py` holds one long-lived outbound connection to the broker and
prints a line when a message arrives. That's the whole delivery mechanism — no
polling, no terminal automation, no stdin injection.

```
   MACHINE A (frontend)                    MACHINE B (backend)
  ┌────────────────────┐                  ┌────────────────────┐
  │ claude             │                  │ claude             │
  │  monitor ──────────┼── SSE ──┐  ┌─────┼────────── monitor  │
  │  stdio MCP ────────┼── POST ─┤  ├─────┼──────── stdio MCP  │
  └────────────────────┘         ▼  ▼     └────────────────────┘
                          ┌──────────────────┐
                          │ broker + SQLite  │   (on a machine you control)
                          └──────────────────┘
```

Every connection is **client → broker**. Nothing ever connects *into* a session,
so this works when one machine is behind NAT, a firewall, or has no reachable
address at all.

## Install

**Nobody hosts the broker for you.** Your sessions' questions — and whole files
from your repos — pass through it, so it runs on a machine you control. It is a
single small Python service.

### 1. The broker

```bash
curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/install-broker.sh | bash
```

Defaults to `127.0.0.1:8850`, which is all you need if your sessions are on one
machine. For several machines, bind the private address peers reach you on:

```bash
curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/install-broker.sh \
  | bash -s -- --bind 100.64.0.5          # e.g. a Tailscale IP
```

It installs a systemd user service where available (surviving logout and
reboot), and tells you how to run it manually where not — which includes macOS,
where you either run it in a terminal or wrap it in a launchd plist. It finishes
by confirming the *live* broker reports the version it just installed, so an
upgrade that failed to replace a running process cannot pass silently.

The plugin installer is POSIX shell and works on macOS as-is; the broker needs
Python 3.11+ there.

### 2. The plugin, on each session's machine

```bash
curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/install.sh \
  | bash -s -- --broker http://<broker>:8850
```

```powershell
& ([scriptblock]::Create((iwr https://raw.githubusercontent.com/dbrentley/hollerback/main/install-windows.ps1 -UseBasicParsing).Content)) `
    -Broker http://<broker>:8850
```

The broker serves the same scripts, if you'd rather not go out to GitHub — and
that form fills in its own URL, so you can drop `--broker` / `-Broker` entirely:

```bash
curl -fsSL http://<broker>:8850/install.sh | bash
```
```powershell
iwr http://<broker>:8850/install.ps1 -OutFile $env:TEMP\hb.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\hb.ps1 -Broker http://<broker>:8850
```

The plugin itself comes from GitHub, falling back to the broker if GitHub is
unreachable — so the install works even when the broker is down or not up yet, and
a current installer never pairs itself with whatever older plugin a long-running
broker happens to be serving.

Or through Claude Code's own plugin system:

```bash
claude plugin marketplace add dbrentley/hollerback
claude plugin install hollerback@hollerback --config BROKER_URL=http://<broker>:8850
```

**Nothing is named.** Each session identifies itself as `<host>:<project-dir>`, and
says what it *does* at runtime with `announce()`. So this installs once per machine
and every workspace on it is automatically its own agent — no second install, no
naming, nothing to collide. Peers find each other with `discover()`, which reads
stored announcements and is therefore complete no matter who started when.

### 3. Start a new session

Two things silently do nothing otherwise:

- **`/reload-plugins` is not enough.** It re-arms monitors but does **not**
  respawn the MCP server, so you get new notifications and old tools. Quit and
  restart.
- **The workspace must be trusted.** Monitors are skipped in an untrusted
  workspace with no error at all.

You should see `1 monitor` in the status line and ten hollerback tools.

### Uninstall

Needs no broker and no network beyond fetching the script — it only removes local
files:

```bash
curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall.sh | bash
```
```powershell
& ([scriptblock]::Create((iwr https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall-windows.ps1 -UseBasicParsing).Content))
```

It removes the plugin, every `pluginConfigs` entry hollerback wrote (whichever way
it was installed), and local state. **Files peers sent you are kept** — add
`--purge-inboxes` / `-PurgeInboxes` to delete those too.

That leaves the broker running, since it is a separate service. To remove it as
well:

```bash
curl -fsSL https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall-broker.sh | bash
```

Message history and shared files are kept by default; `--purge-data` deletes them.

### Upgrading from `agentshare`

This project was called `agentshare` before it was released. If you ran it under
that name, three things move automatically and four do not.

**Handled for you.** `AGENTSHARE_*` environment variables are still honoured, with
a warning on stderr naming each one — so an old `broker.env` keeps working instead
of silently falling back to defaults. Rename them anyway; the fallback is
temporary:

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path.home() / ".config/hollerback/broker.env"
p.write_text(p.read_text().replace("AGENTSHARE_", "HOLLERBACK_"))
EOF
systemctl --user restart hollerback-broker
```

The uninstallers also remove both names, so an old install goes with `uninstall.sh`
whichever name it was installed under. Re-running `install.sh` writes the new
`pluginConfigs` key.

**Do these by hand:**

```bash
systemctl --user disable --now agentshare-broker      # the old unit is not replaced
mv ~/.local/share/agentshare/agentshare.db \
   ~/.local/share/hollerback/hollerback.db            # only if you want the history
mv ~/.local/share/agentshare/files ~/.local/share/hollerback/files
mv ~/.agentshare.json ~/.hollerback.json              # if you used the manual config
```

Received files in your projects stay in `.agentshare/inbox/` — move them to
`.hollerback/inbox/` or leave them; nothing reads the old path.

**Two tools were renamed:** `ask_peer` → `holler`, `answer` → `holler_back`. The
notification prefix changed from `[agentshare]` to `[holler]`. Anything holding the
old names — a memory, a note, a CLAUDE.md — will be wrong.

**Then restart every session.** Monitors and MCP servers are long-lived child
processes; an old one keeps running against the old broker until its session exits.

## The tools

| Tool | What it does |
|---|---|
| `holler(peer, question, context?)` | Ask. **Returns immediately** — never blocks. The answer arrives later as a notification. |
| `holler_back(request_id, text)` | Answer a question you were asked. Routed back to whoever asked. |
| `read_message(message_id)` | Full untruncated text — notifications get clipped for display. |
| `check_inbox()` | What you owe, what you're waiting on, files not yet saved. |
| `discover()` | Who exists, what each one *is*, online or not, and what they owe. Call before addressing anyone. |
| `announce(capabilities)` | Say what this session is. Stored, so sessions starting later still see it. |
| `tell_peer(peer, note)` | Heads-up, no reply expected. `peer="*"` broadcasts to everyone. |
| `send_file(peer, path, note?)` | Send a file instead of pasting it into a message. |
| `request_file(peer, path, reason?)` | Ask a peer for a file from their side. |
| `get_file(file_id, save_as?)` | Save a received file and return its **path**, so only the parts you need enter context. |

Received files land in `.hollerback/inbox/` **inside your project**, so Claude
Code can `Read` them. A `.gitignore` is written there automatically.

## Watching it

The broker serves a dashboard at `http://<broker>:8850/` — every session, whether
it's online, what directory it's working in, what it still owes an answer on, and
a live feed of questions, answers and files.

**Only connected sessions are shown by default**, with a `show offline (N)` toggle
— nothing here expires, so every session that ever connected stays in the roster
and the list would otherwise become mostly history. To drop a dead one for good:

```bash
curl -s -XPOST http://<broker>:8850/v1/forget \
  -H 'content-type: application/json' -d '{"agent":"host:old-project"}'
```

Presence expires on its own rather than relying on a clean disconnect, so a peer
killed mid-stream stops showing as online instead of lingering forever. If a
session disappears still owing an answer, whoever asked gets told — otherwise
fire-and-forget means waiting indefinitely on a session that no longer exists. And
a session that was cut off from the broker for a while is told when it comes back,
since its tools keep working while it is disconnected and nothing else would
reveal that it had gone deaf.

## More than two

Nothing has to be registered, named, or installed again. A session becomes a peer
by starting in a directory — it identifies itself as `<host>:<project-dir>`, and
`discover()` finds it.

- **A second agent on a machine costs nothing.** Open a session in another
  directory and it is another agent. This is why plugin config holds no name:
  Claude Code reads `pluginConfigs` from user scope only, so anything stored there
  is one-per-machine and would collide the moment you wanted two.
- **Two sessions in the *same* directory share an id**, so they receive the same
  traffic and either can drain the other's backlog. Give the second one its own
  directory, or pin it with `HOLLERBACK_AGENT=whatever claude`.
- **An id says where a session is, not what it knows.** That is what `announce()`
  is for, and why it is stored rather than broadcast — a session that starts
  tomorrow still sees what you announced today.

## Security model

Read this before putting it on a network.

- **There is no authentication.** The bind address is the boundary. Loopback, or
  a private overlay network (Tailscale, WireGuard, a VPN). Never `0.0.0.0` on a
  network you don't trust. `HOLLERBACK_TOKEN` is plumbed through if you want a
  bearer token.
- **File sharing is confined to the workspace.** `send_file` and `get_file`
  refuse any path outside the project — verified against `~` expansion and
  `../../` traversal. This matters because `request_file` lets the *peer* name a
  path: a confused or compromised peer asking for `~/.ssh/id_rsa` must not be
  able to talk your session into sending it.
- **A woken peer gets read-only tools only.** A `PreToolUse` hook auto-allows
  `Read/Grep/Glob/NotebookRead` — and nothing else — and only while a question is
  actually open, so an unattended session can answer without stalling on a
  permission prompt, and cannot write or run commands to do it. The hook only
  ever *skips a prompt*; it never denies anything.
- Everything the broker holds sits in plain SQLite at
  `~/.local/share/hollerback/`, including message text and file contents.

## Notes for anyone building Claude Code plugins

These cost real debugging time, and several contradict the documentation.
Verified against Claude Code 2.1.218.

1. **Monitor commands may not reference `${user_config.*}`.** The manifest schema
   says it's substituted; the runtime refuses — *"The substituted value would be
   passed to a shell… have the monitor script read the value from a config file
   instead"* — and **drops the monitor**, logging only in debug output. That's why
   `listen.py` reads its own config rather than taking argv. `${CLAUDE_PLUGIN_ROOT}`
   *is* allowed.
2. **An untrusted workspace silently skips monitors.** No error, no notification.
3. **Install at user scope.** Project-scope `skills-dir` plugins have `monitors`
   **stripped entirely**.
4. **Monitors don't arm in `-p`/headless mode** — that path never loads them, so
   test interactively.
5. **`/reload-plugins` does not respawn a stdio MCP server.** Verified by PID:
   identical before and after. An updated plugin looks half-applied — new
   notification format, old tool list.
6. **Long-lived child processes never pick up code changes.** Monitors and MCP
   servers both outlive edits. Nearly every "bug" in this project's history was a
   stale child process, not a transport fault.
7. **stdio MCP servers get `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`;
   HTTP servers get neither**, and their headers are static per config file. If a
   server needs to know which session it's serving, it has to be stdio.
8. **Force UTF-8 on stdio.** JSON-RPC is UTF-8, but Python on Windows defaults to
   the locale codepage — an em-dash arrives as `â€”`.
9. **Notifications are truncated for display** however short you make the line, so
   carry an id and offer a way to fetch the full text.
10. **PowerShell 5.1 is a JSON hazard.** `Set-Content -Encoding UTF8` writes a
    BOM, and piping a single-element array into `ConvertTo-Json` unrolls it into
    an *object* — which Claude Code rejects with "expected array, received
    object", failing the whole plugin load.
11. **Verify shape, not just syntax.** A "does it parse?" check passes happily on
    an object where an array was required.
12. **Give an SSE server a stop timeout.** Infinite generators mean a graceful
    shutdown waits forever and the unit hangs in `deactivating`.
13. **"Delivered" only means bytes were written.** It does not mean the model saw
    it. Unanswered questions and unfetched files are re-offered on every fresh
    attach, so a silent drop heals itself.
14. **Say what actually went wrong.** An error reading *"no question with
    request_id X"* — when the id was fine and only the *kind* was wrong — led a
    peer session to infer a message-expiry mechanism that does not exist, write
    that conclusion to memory, and switch to a worse channel to work around it.
    When your message crosses into another agent's context it is not a log line;
    it is the only evidence they have.

## Layout

```
broker/hollerback_broker/app.py     REST + SSE + dashboard + installer hosting
broker/hollerback_broker/store.py   SQLite: threaded messages, files, presence
broker/hollerback_broker/dashboard.html
plugin/.claude-plugin/plugin.json   userConfig: AGENT_NAME, BROKER_URL
plugin/.mcp.json                    stdio MCP server
plugin/monitors/monitors.json       the delivery mechanism
plugin/hooks/pretooluse.py          read-only auto-allow while answering
plugin/bin/listen.py                SSE consumer; stdout line ⇒ notification
plugin/bin/mcp_server.py            stdlib JSON-RPC MCP server (no dependencies)
plugin/skills/talking-to-your-peer/ when to ask, and how to answer honestly
```

The plugin is **standard library only**, so it runs on Windows and Linux with a
bare `python3` and no install step. The broker needs Python 3.11+, `starlette`
and `uvicorn`.

## License

MIT — see [LICENSE](LICENSE).
