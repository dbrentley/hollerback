# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

hollerback lets concurrently-running Claude Code sessions ask each other questions
and exchange files, without the human copy-pasting between terminals. One session
calls `holler(...)`, keeps working, and the answer arrives later as a
notification — the peer session wakes itself up to answer.

The repo holds **two separately-deployed halves** that only meet over HTTP:

| Half | Runtime | Deps |
|---|---|---|
| `broker/` — the message bus | Python **3.11+**, long-lived service on a machine you control | `starlette`, `uvicorn`, SQLite |
| `plugin/` — the Claude Code plugin | Bare `python3` (**3.8+**) on Windows *and* Linux, spawned by Claude Code | **standard library only** |

They are versioned and installed independently. Changing a wire message shape
means changing both sides and reinstalling both.

`docs/session-log.md` is the build history: why each decision went the way it did,
every failure and its *actual* root cause, and what was proven versus assumed. The
original session was recorded under the project's pre-rename path (`agentshare`),
so `/resume` cannot reach it from here — that document is the only copy.

**There is a live deployment.** A broker runs as a systemd user service from
`~/.local/share/hollerback` (not this checkout), with real sessions attached. So:
never test against it — stand up a scratch broker from the working tree on a spare
port with `HOLLERBACK_DB` pointed at a temp file, and **never `pkill -f
hollerback_broker`**, which matches the live one. Kill test brokers by the exact PID
you started. Broker changes reach the running service only after re-running
`install-broker.sh`; plugin changes need a reinstall plus a session restart.

## The mechanism that makes it work

This is the part that isn't obvious from any single file, and everything else is
downstream of it:

1. `plugin/monitors/monitors.json` declares a **monitor** — a background process
   Claude Code arms at session start.
2. **Every line that monitor prints to stdout becomes a `<task_notification>`
   delivered to the model**, which re-invokes a session sitting idle at the prompt.
3. So `plugin/bin/listen.py` holds one long-lived SSE connection to the broker and
   prints exactly one line per incoming message. There is no polling, no terminal
   automation, no stdin injection.
4. `plugin/bin/mcp_server.py` (stdio MCP) provides the outbound tools, POSTing to
   the broker.

Every connection is **client → broker**. Nothing ever connects *into* a session,
which is why this works across NAT/firewalls with no reachable address on either end.

```
holler()  → POST /v1/ask → broker → SQLite → SSE /v1/stream/{peer} → listen.py stdout
                                                                   → <task_notification>
                                                                   → peer wakes, reads code,
                                                                     calls holler_back()
                                     ← POST /v1/answer ←────────────────────────┘
```

`/v1/answer` routes by `request_id` back to `question["from"]` (`broker/.../app.py`),
which is why two questions in flight don't get mismatched answers.

## Non-obvious invariants

Break any of these and the failure is silent, not loud.

- **`listen.py` stdout is a delivery channel, not a log.** Anything printed there
  lands in a peer's context. All diagnostics go to stderr via `_common.log()`.
  Messages are flattened to one line and capped (`MAX_TEXT`).
- **`plugin/` must stay standard-library only.** It runs with whatever `python3`
  the peer machine has; `pip install` is not available in the install path.
- **`monitors.json` must be a JSON *array*.** An object fails the whole plugin load
  ("expected array, received object"). Both installers verify *shape*, not just
  syntax — a `json.load()` that parses proves nothing.
- **Monitor commands may not reference `${user_config.*}`.** Claude Code refuses
  and silently drops the monitor. That is the only reason `_common.load_config()`
  exists: the monitor command carries no config, so the script reads it from disk.
  (`listen.py` does accept `--agent`/`--broker`/`--token` for manual runs; the
  monitor must never use them.) The restriction is specific to *monitor commands*,
  whose substituted value would reach a shell — `${CLAUDE_PLUGIN_ROOT}` is allowed
  there, and `${user_config.*}` works normally in `.mcp.json` and arrives in hooks
  as `CLAUDE_PLUGIN_OPTION_<KEY>`.
- **The MCP server is stdio on purpose.** stdio servers inherit
  `CLAUDE_PROJECT_DIR` and `CLAUDE_CODE_SESSION_ID`; HTTP servers get neither, and
  their headers are static per config file. `project_root()` depends on this.
- **Force UTF-8 on stdio** in both `listen.py` and `mcp_server.py`. Python on
  Windows defaults to the locale codepage and mangles any non-ASCII (`—` → `â€”`).
- **Path confinement is a security boundary, not tidiness.** `resolve_in_project()`
  refuses anything outside `CLAUDE_PROJECT_DIR` because `request_file` lets the
  *peer* name the path — a compromised peer asking for `~/.ssh/id_rsa` must not be
  able to talk this session into sending it. It is checked against `~` expansion
  and `../` traversal, and returns an error *string* (not a path) on refusal.
- **The `PreToolUse` hook may only ever skip a prompt, never deny.** It auto-allows
  `Read|Grep|Glob|NotebookRead` and only while a question is open. A denying hook
  could break the user's own work in their own session.
- **Nothing is queued for an absent session.** `/v1/ask`, `/v1/note` and
  `/v1/file` all refuse with 409 when the target has no live subscriber, and
  broadcast reaches only connected peers. Presence comes from the in-memory
  `_subscribers` set, not the stored flag, because the flag can lag a hard drop.
  The old behaviour queued indefinitely, which paired badly with ids that outlive
  the sessions that made them: the asker waited forever on a peer that was gone.
- **Only the duplicate session is suffixed.** `resolve_agent_id()` keeps the plain
  `<host>:<dir>` unless another *live* session with a different
  `CLAUDE_CODE_SESSION_ID` already holds it, in which case it appends `#<4 hex>`.
  Suffixing unconditionally would make ids change every launch, and stable ids are
  what make announcements persist and addressing possible at all. It is not part of
  `load_config()` on purpose — the `PreToolUse` hook calls that before every Read
  and must never touch the network.
- **Nothing in the broker expires.** Messages, files and ids are permanent. If an
  error message implies otherwise, a peer session will infer a timeout mechanism
  that does not exist and write that conclusion to its memory — errors crossing
  into another agent's context are the only evidence they have, so say exactly
  what went wrong (see the `/v1/answer` 404 branch for the shape to copy).
- **"Delivered" only means bytes were written**, never that the model saw it.
  `store.take_undelivered()` therefore re-offers unanswered *questions* and
  unfetched *files* on every fresh attach, so a silent drop self-heals. Answers and
  notes are deliberately not re-sent.
- **Presence must be able to go stale on its own.** `agents.connected` describes a
  live SSE connection held in memory, so it cannot survive the process. `init()`
  clears it at startup (a crash or `kill -9` never runs the stream's `finally`),
  and `list_agents()` additionally treats `seconds_since_seen > PRESENCE_GRACE_SECS`
  as offline, since the stream refreshes `last_seen` on every keepalive. Never
  report presence from the flag alone — a suspended laptop leaves it set. Note
  `/v1/ask` uses the in-memory `_subscribers` set instead, which is the more
  accurate source; keep the two from drifting apart in what they claim.
- **A departure notice must be `kind="note"`.** `_notify_departure()` tells anyone
  waiting on an agent that it vanished. It is tempting to give system messages
  their own `kind`, but an older `listen.py` has no branch for an unknown kind and
  falls straight through to the *question* formatter — the peer would then try to
  answer a system message. Every shipped client understands `note`.
- **The auto-allow window is keyed by agent id, and the hook is user-scope.**
  `_common.state_path()` builds `~/.cache/hollerback/open_questions.<agent>.json`
  with no session id, and the `PreToolUse` hook runs in *every* session on the box.
  Since ids now embed the project directory, the blast radius is normally one
  workspace — but two directories with the same basename derive the same id and
  therefore share the window, as does anything pinned with `HOLLERBACK_AGENT`. It
  also allows the read regardless of path; `resolve_in_project()` constrains only
  `send_file`/`get_file`, not what the hook permits.
- **A `request_file` satisfied by `send_file` is never marked answered.**
  `request_file` has no endpoint of its own — it POSTs to `/v1/ask` and creates a
  real `kind='question'` row. `send_file` forwards the `request_id`, but
  `upload_file` only *stores* it; `store.mark_answered()` is called from `/v1/answer`
  and nowhere else. So the request stays open forever: `check_inbox` never clears,
  `discover` shows a permanent debt, and `take_undelivered()` re-offers it on
  every attach. Only a `holler_back` on that id closes it.

## Running and testing

There is **no test suite, linter, or CI** in this repo. Verification is by running
the thing. Nothing in the plugin needs a build step.

### Broker, from the working tree

```bash
cd broker
uv sync                                             # or: python3 -m venv .venv && .venv/bin/pip install -e .
HOLLERBACK_BIND=127.0.0.1 HOLLERBACK_PORT=8850 .venv/bin/python -m hollerback_broker.app
```

Point it at a scratch DB to avoid touching real message history:
`HOLLERBACK_DB=/tmp/hb-test.db`.

**SIGTERM will not stop it once anything is subscribed.** SSE streams are infinite
generators, so uvicorn's graceful shutdown waits on them forever: the port closes
and health checks fail while the process lives on, orphaned to PPID 1 in `ep_poll`.
Under systemd the unit's `FinalKillSignal=SIGKILL` handles it; by hand you need
`kill -9`. Always check with `pgrep -af hollerback_broker` rather than trusting a
failed health check to mean "stopped".

### Exercising the broker without any Claude Code session

```bash
curl -s localhost:8850/v1/health
curl -s localhost:8850/v1/peers

# open a subscriber for 'backend' in one terminal (this is what listen.py does)
curl -N localhost:8850/v1/stream/backend?cwd=/tmp

# ask it something from another
curl -s -XPOST localhost:8850/v1/ask \
  -H 'content-type: application/json' \
  -d '{"from":"frontend","to":"backend","text":"ping?"}'

curl -s -XPOST localhost:8850/v1/answer \
  -H 'content-type: application/json' \
  -d '{"from":"backend","request_id":"<id from /v1/ask>","text":"pong"}'

curl -s localhost:8850/v1/pending/backend
```

`POST /v1/send` dispatches on a `kind` field — it exists for exactly this kind of
curl testing.

### Plugin listener, standalone

```bash
# connection check only -- what both installers run (they grep stderr, discard stdout)
HOLLERBACK_AGENT=test HOLLERBACK_BROKER=http://127.0.0.1:8850 \
  timeout 5 python3 plugin/bin/listen.py

# to actually see message lines, defeat the startup grace period first
HOLLERBACK_STARTUP_GRACE_SECS=0 HOLLERBACK_AGENT=test \
  HOLLERBACK_BROKER=http://127.0.0.1:8850 timeout 10 python3 plugin/bin/listen.py
```

`connected to ...` on **stderr** means it works; message lines appear on stdout.
`emit()` holds the *first* line for `STARTUP_GRACE_SECS` (default **8**) and spaces
subsequent ones 0.4s apart — a session that has just started does not surface lines
printed in its first moments, and Claude Code coalesces stdout written within ~200ms.
So a naive `timeout 5` run prints nothing to stdout even with a backlog waiting.

### MCP server, standalone

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| HOLLERBACK_AGENT=test HOLLERBACK_BROKER=http://127.0.0.1:8850 \
  python3 plugin/bin/mcp_server.py
```

One JSON-RPC request per line in, one response line out.

### End-to-end, in real sessions

```bash
./install-broker.sh                          # or --bind <private-ip> --port <n>
./install.sh --broker http://127.0.0.1:8850
```

Then, in each session:

- **Start a new session — `/reload-plugins` is not enough.** It re-arms monitors but
  does *not* respawn the stdio MCP server (verified by PID). You get new
  notifications and the old tool list, which looks like a half-applied bug.
- **The workspace must be trusted**, or monitors are skipped with no error at all.
- **Monitors don't arm in `-p`/headless mode.** Test interactively.
- Status line should show `1 monitor`, and there should be **10** hollerback tools.
- **Nothing is named, anywhere.** An agent id is `<host>:<project-dir>`, derived by
  `_common.default_agent_name()`. This exists because `pluginConfigs` is user-scope
  only, so any name stored there is one-per-machine and collides the moment you want
  two agents on one box — which the installers used to handle with a rename-refusal
  and a per-workspace config file, both since deleted. An explicit `HOLLERBACK_AGENT`
  still wins if something really needs pinning.
- **`announce()` is stored, never broadcast.** A broadcast only reaches whoever is
  connected at that instant, and sessions come and go constantly, so a late joiner
  would be permanently ignorant. Capabilities live in `agents.capabilities` and
  `discover()` reads them, which makes arrival order irrelevant. The broker nudges
  an agent to announce once per process, only if it never has.
- **`/v1/ask` resolves the target before queueing.** Ids are mechanical, so nobody
  types them from memory — `resolve_peer()` takes an exact id or a unique substring
  of an id or of announced capabilities, and returns the candidates on ambiguity
  rather than guessing. Guessing would route a question to the wrong session and
  leave the asker waiting on an answer nobody is writing.
- `listen.py: READ_TIMEOUT = 60` — hardcoded client-side socket timeout.
- `app.py: KEEPALIVE_SECS = 20` — server-side, tunable via `HOLLERBACK_KEEPALIVE_SECS`
  and written into `~/.config/hollerback/broker.env` by `install-broker.sh`.

The second must stay comfortably below the first. Raise the broker keepalive past 60
and every client silently enters a permanent 60-second reconnect loop, replaying the
whole backlog each cycle. Nothing validates the relationship.

`HOLLERBACK_BACKLOG_MAX` is written into `broker.env` by the installer and
documented in `broker/etc/broker.env.example`, but **no code reads it**. The backlog
is unbounded and, since nothing expires, permanent. Do not trust it as a bound.

## Where things live at runtime vs. in the repo

`install-broker.sh` **copies** `broker/`, `plugin/` and the installer scripts to
`~/.local/share/hollerback/`, and the systemd unit runs from there. Editing this
working tree does not affect a running installed broker — re-run the installer, or
run from the tree directly as above.

The broker serves the plugin and its installers to peer machines, so a peer never
needs SSH or a git clone: `/v1/plugin.zip` zips `HOLLERBACK_PLUGIN_DIR` itself,
while `/install.sh`, `/install.ps1`, `/uninstall.sh` and `/uninstall.ps1` are read
from its **parent** (`_serve_script`) — which is why `install-broker.sh` copies the
scripts alongside `plugin/`, not into it. `install.sh` **leaves a symlinked
`~/.claude/skills/hollerback` alone** — that's the dev-machine case, where the
installed plugin *is* the working tree.

Durable state (never in the repo, all gitignored): `~/.local/share/hollerback/`
holds the SQLite DB and file blobs in plain text; `~/.cache/hollerback/` holds the
per-agent open-question window; `.hollerback/inbox/` inside each *user's* project
holds received files and self-ignores via a `.gitignore` written on first use.

## Touch-points when changing things

- **Adding/renaming an MCP tool:** the `TOOLS` schema list *and* the `call_tool`
  dispatch chain in `plugin/bin/mcp_server.py`, the notification wording in
  `listen.py::format_message` (it tells the peer which tool to call back with), the
  guidance in `plugin/skills/talking-to-your-peer/SKILL.md`, the tool table in
  `README.md`, and the "ten hollerback tools" count in `README.md` / `install.sh`.
  Tool descriptions are prompt surface — `holler`'s text is what stops a session
  blocking on a reply.
- **Plugin identity string** `hollerback@skills-dir` is duplicated in
  `plugin/bin/_common.py`, `install.sh`, `install-windows.ps1`, `uninstall.sh` and
  `uninstall-windows.ps1`. All five move together. Note it is *not* the only
  `pluginConfigs` key read: the runtime keys config by the plugin's source id
  (`${name}@${marketplace}`), so `claude plugin install` writes
  `hollerback@hollerback` instead. `_candidate_sources()` accepts `@skills-dir`
  first and then any `hollerback@*` key, which is what makes the marketplace route
  and forks work — reading only one of them produced the worst failure in the
  project: plugin loads, `1 monitor` shows, all 10 tools appear, and every tool
  answers *"hollerback is not configured"*. Both uninstallers match on the plugin
  half of the id (`hollerback@*`), so marketplace and fork configs are removed too.
- **Config precedence** (`_common.load_config`): env `HOLLERBACK_*` >
  `<project>/.hollerback/agent.json` > `~/.hollerback.json` >
  `pluginConfigs["hollerback@*"].options` in `~/.claude/settings.json`. The project
  layer resolves via `CLAUDE_PROJECT_DIR` when set (stdio MCP) and cwd otherwise
  (the monitor, which is spawned with the workspace as its cwd). Both *file* reads use `utf-8-sig` because PowerShell
  5.1 writes a BOM (the env layer needs no decoding). Note `_read_state()` in the
  same file deliberately omits the encoding — it reads our own cache, not a
  PowerShell-written config.
- **Roster hygiene:** nothing expires, so `POST /v1/forget` is the only way to
  drop an agent, and the dashboard hides offline peers by default for the same
  reason. A renamed directory leaves its old id behind forever otherwise.
- **New broker endpoint:** add the `Route` in `app.py` and gate it with
  `_authorized()` — it is per-handler, not middleware, so a new route is
  unauthenticated until you add the call. Eight routes skip it today — see Security
  posture, and note that one of the eight looks accidental.
- **Anything touching SSE lifetime:** the systemd unit needs
  `KillMode=mixed` + `TimeoutStopSec` — SSE generators never finish, so a graceful
  shutdown waits forever and the unit hangs in `deactivating`.
- **Schema changes** in `store.py` need an additive `PRAGMA table_info` migration in
  `init()` (see the `file_id` / `fetched_at` precedent). Existing DBs are live.
- **`plugin/monitors/monitors.json` is not the source of truth on Windows.**
  `install-windows.ps1` step 4 *overwrites* the extracted copy with a here-string
  that pins an absolute `python.exe` (the WindowsApps `python3` alias is
  unreliable), duplicating `name`, `description` and `when` literally. Editing the
  repo's `monitors.json` changes Linux only; Windows peers silently keep the old
  single-entry definition, and both files parse and pass the shape check. `.mcp.json`
  gets no such rewrite, so on Windows the MCP server still launches via the bare
  `python3` the monitor was pinned to avoid.
- **The shell installers must run on macOS**, which has BSD userland and no GNU
  coreutils. No `timeout` (use background + `sleep` + `kill`), no `stat -c`, no
  `sed -i` without an argument, no `\?` in a `sed` BRE, and `mktemp -t` takes a
  *prefix* there rather than a template. `timeout` shipped once and broke every
  macOS install at the smoke test, after everything else had succeeded.
- **Windows installer JSON:** never build JSON with `ConvertTo-Json` from a
  single-element array (it unrolls to an object), and never `Set-Content -Encoding
  UTF8` (BOM). `install-windows.ps1` uses a here-string plus a no-BOM writer for this.

## Settled decisions — don't re-litigate

Each of these was decided deliberately during the build; `docs/session-log.md` has
the reasoning and the rejected alternatives.

- **No authentication.** Raised three times, closed by the user: *"i don't need
  auth. stop asking me. it's all on tailscale."* The bind address is the boundary.
  Don't propose auth again; `HOLLERBACK_TOKEN` already exists if that ever changes.
- **Asking never blocks.** `holler()` returns immediately and the peer answers at
  its next stopping point. A blocking or synchronous ask mode was rejected outright.
- **No centrally hosted broker.** Files and source flow through it, which would make
  the host a data processor for other people's code.
- **Answers are not persisted as artifacts.** The durability critique (answers die at
  compaction, aren't greppable or reviewable) was heard and deliberately declined in
  favour of plain conversation — no `DECISIONS.jsonl`, no `CONTRACT.md`.
- **Broadcast is notes-only.** A broadcast *question* would have N recipients and no
  defined answerer.
- **Tool names stay plain** apart from `holler` / `holler_back` — *"over-cutesifying
  tool names makes them harder for a model to select correctly."*
- **The user runs the installers**, on both machines: *"i will do the install don't
  do it for me."* Prep and verify, but don't install, and revert anything unsanctioned.

Two known gaps that are *not* settled, just unaddressed: a woken peer that reaches
for **Bash** still stalls forever on a permission prompt (the hook covers only read
tools — and Bash is what actually stalled in the original wake test), and the
`AGENTSHARE_* → HOLLERBACK_*` migration note the rename needed was never written.

## Security posture

**There is no authentication by default.** The bind address is the boundary —
loopback, or a private overlay (Tailscale/WireGuard/VPN). Never `0.0.0.0` on an
untrusted network. `HOLLERBACK_TOKEN` is plumbed end-to-end as an optional bearer
token if set on the broker *and* in every plugin config.

Even with the token set, **eight routes never call `_authorized()`**: `/`,
`/v1/health`, `/v1/dashboard`, `/v1/plugin.zip`, and the four installer-script
routes. `/v1/dashboard` is the one that matters — it returns
`store.recent_messages(40)`, i.e. the *full untruncated text* of the last 40
messages plus the peer roster, to any caller that can reach the port. The token
does not protect message content.

At rest on the broker host: message text sits unencrypted in SQLite; file contents
are **not** in the DB at all — `store.add_file()` writes each upload as a loose file
under `~/.local/share/hollerback/files/<file_id>`, and only metadata (name, size,
sha256, agents) goes in the `files` table. Securing or scrubbing `hollerback.db`
alone leaves every shared file in plaintext beside it.
