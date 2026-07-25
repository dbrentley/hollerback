# hollerback — build session log

Reconstructed from the original build transcript
(`2026-07-23T20:27:37Z` → `2026-07-24T16:43:05Z`), which was recorded under the
project's pre-rename path and so is not reachable by `/resume` from this
directory. This document exists so that history is not lost with it.

**It was not 20 continuous hours.** Three long dead gaps sit inside that window:
`22:51Z→01:37Z` (2h46m), `04:11Z→07:08Z` (2h57m), and `07:55Z→16:34Z` (8h39m).
The final publish sequence took nine minutes, not nine hours. Transcript
timestamps are UTC; the machine runs UTC−7.

Everything below is from the transcript. Where the transcript and the finished
code disagree, the code wins and it is called out. Anything discovered *after*
that session is fenced off in [§9](#9-added-after-the-fact-2026-07-24).

---

## 1. What this session was

### The problem, in Brent's framing

Brent runs two Claude Code sessions at once — frontend and backend — and **he is
the router**. When the frontend needs the exact shape of an error envelope the
backend knows, he copy-pastes between two terminals and the answer goes stale the
moment either side changes something.

He opened with a design problem, not a feature request, and named the hard part
himself in the first message:

> "problem is how do we 'hook' those questions into the current session."

His own guess was a custom MCP exposing `ask_frontend` / `ask_backend`. He set the
bar at *"i want to cleanly design a fix"* and asked whether Claude Code plugins
were even an option. The session ran under `/effort ultracode`.

### The environment

| | |
|---|---|
| **linux-box** | Ubuntu 24.04, kernel `7.0.0-28-generic`, not WSL. Tailscale `100.64.0.5`. The "backend" session. `uv`/`uvx` + system `python3` 3.12.3; **no** node/npm/bun, **no** tmux/screen/expect. `socat`, `script(1)`, `setsid`, `inotifywait` present. |
| **windows-box** | Windows, **native, no WSL**. On the same tailnet and the same physical LAN, so Tailscale used a direct path rather than a relay. The "frontend" session. Claude had **no access to this box at all** — every Windows change went through Brent pasting a one-liner and reporting the output. |
| **Claude Code** | 2.1.218 on both. Launcher → `~/.local/share/claude/versions/2.1.218`, a **261 MB single-line bundled-JS blob**. |
| **Already running** | An unrelated HTTP MCP server and its dashboard held `:8848` and `:8849`. Hence port **8850**. |
| **SSH** | `~/.ssh/config` has exactly one host entry, `windows-box-wsl` → `100.64.0.9`. **It does not work** — port 22 unreachable, the alias points at a WSL sshd that isn't running. This one dead alias shaped the entire architecture. |

### What came out the other end

**hollerback** — public, MIT, `https://github.com/dbrentley/hollerback`, commit
`c12aa2e`, branch `main`, 26 tracked files. Two independently-deployed halves
meeting only over HTTP: a Starlette/SQLite broker that serves its own installers,
and a stdlib-only Claude Code plugin (monitor + stdio MCP server with 9 tools +
`PreToolUse` hook + skill).

The mechanism, in one sentence: **every line a plugin monitor prints to stdout is
delivered to the model as a `<task_notification>`, and task notifications
re-invoke a session sitting idle at the prompt.** That was found by
reverse-engineering the 2.1.218 binary, not from documentation.

**What was actually proven across machines:** a question sent from linux-box reached the
Windows session with nobody typing anything — in the peer's own unprompted words,
*"CONFIRMED — yes, this arrived with no one typing it. It came in as a background
task-notification while I was mid-task; the user did not relay it."* Note *mid-task*,
not idle, and note that the handshake question explicitly said *"No need to read
any files — answer from what you already know."* The cold-idle wake and the
read-to-answer path were each proven **only on linux-box**, never together, never
cross-machine. See [§5](#5-what-was-actually-proven).

At session end everything installed on linux-box was **torn down at Brent's request**.
Only the source repo survives; there is no live system anywhere.

---

## 2. Timeline

### Phase 0 — design and discovery (20:27 – 21:03)

- Probed the local environment; launched a six-topic background research workflow
  (`w5zetdbs2`); used `AskUserQuestion` to lock the topology (backend = linux-box,
  frontend = Windows native, fire-and-forget asking, peer answers at its next
  stopping point).
- Rather than guess at the Windows box, produced a **PowerShell env probe** for
  Brent to run.
- While waiting, dumped `strings -n 5` of the 261 MB binary to a 37,718,131-byte
  file and searched it. Found two undocumented things: a native **teammate mailbox**
  system (`~/.claude/teams/<team>/inboxes/<agent>.json` + `.lock`, `SendMessageTool`,
  `TeammateIdle` hook event) and the plugin manifest's **`monitors`** array with the
  sentence that became the whole design.
- Research returned six findings that changed the plan: TIOCSTI is dead
  (`dev.tty.legacy_tiocsti = 0`), no tmux/screen/expect, SSH to windows-box broken,
  8848/8849 taken, 29 hook events exist (far more than the docs list).
- Built Phase 0: ~60-line in-memory Starlette broker on 8850 + stdlib `listen.py`.
  Plugin installed by symlinking `~/.claude/skills/agentshare` → repo `plugin/`
  (source id `agentshare@skills-dir`).
- **Then the monitor never spawned.**

### Phase 0b — making the monitor arm (21:03 – 21:41)

- Established monitors never arm in headless `-p`; built `pty_probe.py` — a real
  interactive `claude` under a pty with **no prompt ever submitted**, so no model
  turn is billed.
- Found the two real gates in the binary strings and fixed both. Monitor armed in
  **~1.1 s**; TUI showed `1 monitor`.
- Ran `idle_wake_test.py`: a real session idled 12 s with nothing typed, a question
  was injected (via `urllib.request`, not curl), and the rendered pty screen showed
  `● Monitor event: "Questions and answers from the other Claude Code session"` →
  thinking → `ls -la` and a grep. **This is the load-bearing test, and it was a
  half-pass** — see [§4.1](#41-the-monitor-never-armed--two-silent-gates) and
  [§5](#5-what-was-actually-proven) for what it did *not* prove.
- Broker moved to a systemd **user** unit (`Linger=yes`). Since SSH to Windows is
  dead, added `GET /v1/plugin.zip` and `GET /install.ps1` so the peer machine can
  install with one PowerShell line.
- A 32-agent research workflow returned a design brief plus an **18,307-char
  red-team critique** that refuted 12 of 24 research claims (7 of 8 in the
  injection-paths topic) and caught two premise errors Claude had made: it had
  assumed WSL2 from the stale `windows-box-wsl` alias, and had frontend/backend
  swapped.
- Phase 1 built: SQLite `store.py` (WAL), `/v1/ask` `/v1/answer` `/v1/note`
  `/v1/peers` `/v1/pending/{agent}`, stdlib stdio JSON-RPC `mcp_server.py` (5 tools),
  the `PreToolUse` hook, and the `talking-to-your-peer` skill.

### Phase 1 — getting Windows working (21:41 – 22:33)

Serial debugging of the Windows half, each failure exposing a weakness in how the
previous fix had been verified: UTF-8 BOM → `monitors.json` object-vs-array → the
"lost message" that turned out to be a never-wired monitor → the systemd
`deactivating` hang. Ended with `read_message`, forced UTF-8 stdio, and **file
sharing** (base64 over JSON, 10 MB cap, path-confined). Tool count reached 9.

### Phase 2 — cross-machine verification (22:33 – 03:52)

A long, painful loop with the peer session that turned out to be **almost entirely
stale long-lived child processes plus one badly-worded error string** — not
transport. Then `check_inbox` learned about files, unfetched files got re-offered,
auth was closed permanently by Brent, `install.sh` was written, `tell_peer(peer="*")`
was added, and the skill was rewritten to stop assuming exactly one peer.

### Phase 3 — dashboard, rename, packaging, teardown (03:52 – 07:55)

- Dashboard built — loaded the `dataviz` skill first, then deliberately used **no
  charts**.
- Brent asked who would host a broker centrally; the answer ("nobody, on purpose")
  drove the packaging decisions.
- **The rename.** First shortlist rejected; Brent: *"i like partyline but it's not
  playful enough"*; second list produced **hollerback**.
- Uninstallers, `marketplace.json`, `.gitignore`, MIT `LICENSE`, `install-broker.sh`,
  README rewritten for a stranger.
- Brent: *"get rid of everything i said"* → full teardown of linux-box.
- `git init` + commit `c12aa2e`, remote set.

### Phase 4 — publish (16:34 – 16:43, nine minutes)

The repo existed but was empty; linux-box had no GitHub auth of any kind; pushed with a
throwaway inline credential helper reading a PAT from a local file. All six
public entry-point URLs verified 200.

### Rename history

| From | To |
|---|---|
| `agentshare` — project, repo dir, plugin, broker package, systemd unit, DB, config/cache dirs, `AGENTSHARE_*` env vars | `hollerback` / `HOLLERBACK_*` |
| `ask_peer` | `holler` |
| `answer` (tool) | `holler_back` |
| `[agentshare]` notification prefix | `[holler]` |
| `~/.local/share/agentshare/agentshare.db` | `~/.local/share/hollerback/hollerback.db` (28 messages + 8 files preserved) |
| `pluginConfigs["agentshare@skills-dir"]` | `pluginConfigs["hollerback@skills-dir"]` — migrated in place with `pc["hollerback@skills-dir"] = pc.pop("agentshare@skills-dir")` |
| wire protocol `kind="answer"` | **unchanged, deliberately** |

Two rename steps are easy to miss and both bit immediately:

- **The renamed directory was untrusted again.** `~/.claude.json` keys workspace
  trust by path, so `workplace/hollerback` was a brand-new key with
  `hasTrustDialogAccepted` unset — the exact gate that had cost the first hour.
  Re-accepted as `trusted /home/<you>/workplace/hollerback`.
- **`pluginConfigs` is keyed by the plugin's source string**, so the rename moved
  the config key too. This is the direct ancestor of the marketplace-key hazard in
  [§9](#9-added-after-the-fact-2026-07-24).

The other seven tool names were left plain on purpose: *"over-cutesifying tool
names makes them harder for a model to select correctly."*

---

## 3. Design decisions and why

### Delivery: plugin `monitors`, not a Stop hook

The binary documents a monitor as a background process armed at session start where
*"Each stdout line is delivered to the model as a `<task_notification>` event; the
process runs for the session lifetime."*

> *"That is the 'I spawned an agent, I'll get notified when it completes' mechanism
> you described — same machinery. So a question and an answer are the same thing on
> one bus, and there's only one delivery path to build."*

**Rejected: Stop-hook polling** — Claude's own first instinct and the content of the
first draft plan. Hard-capped at 8 consecutive blocks, fires in *every* session in
the project, freezes the peer's UI at "stopping" while polling. Demoted to a
backstop, then never built.

**Also rejected:** TIOCSTI stdin injection (`dev.tty.legacy_tiocsti = 0`, root-only
to re-enable, a system-wide security regression); tmux/screen/expect `send-keys`
(none installed); plugin `channels` (investigated because the name was
"suspiciously on-topic" — it's Telegram/Discord integration).

### Fire-and-forget asking

Brent picked it verbatim: *"i like #2 fire and forget if it works the same way claude
handles 'i spawned an agent to do x. i will get notified when it completed'"*, and
refined it:

> "whatever accomplishes the goal of the two agents just 'having a conversation' …
> meanwhile, the agent that asked, will wait for the response in the background - go
> about its tasks and then get notified"

`holler()` returns immediately; the peer answers at its next stopping point, never
interrupted mid-task. **Rejected:** blocking ask, and the research brief's opt-in
blocking mode bounded at 110 s (under the 120 s MCP auto-background threshold).

### Everything dials out; nothing connects in

Nothing ever needs to reach into the Windows box — which matters because
`ssh windows-box-wsl` provably fails. Consequence: the broker serves its own
installers and `plugin.zip`, so a peer machine needs no SSH and no git clone. The
design brief called this *"the single best idea in Phase 0."*

### stdio MCP server, hand-written JSON-RPC, stdlib only

> *"stdio servers get `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR` for free;
> HTTP servers get no session identity at all and their headers are static per config."*

`project_root()` / `resolve_in_project()` — the file-sharing security boundary —
depend on this. Hand-written JSON-RPC because `pip install mcp` is not guaranteed on
either box, least of all the Windows side running the WindowsApps `python.exe` with
no install step. **Rejected:** reusing the existing HTTP-MCP-over-Tailscale pattern
from the unrelated MCP server already running here; depending on the `mcp` package.

### Monitor command takes zero arguments

The runtime **refuses to arm** any monitor whose command references
`${user_config.*}`, and drops it silently. So `monitors.json` is
`python3 "${CLAUDE_PLUGIN_ROOT}/bin/listen.py"` and `_common.load_config()` reads
settings from disk instead. Claude's note: *"that was my plan's design; the docs were
wrong and only testing caught it."*

**The restriction is specific to monitor commands.** `${user_config.KEY}` *is*
substituted in `.mcp.json`, and hooks receive the same values as
`CLAUDE_PLUGIN_OPTION_<KEY>` environment variables. Only the monitor path refuses,
because the substituted value would reach a shell.

### `listen.py` stdout is a delivery channel, not a log

Every stdout line becomes a task notification, so stdout is precious; all diagnostics
go to stderr. Each message is flattened to one self-describing line, because
notifications are explicitly marked to the model as **not user input** — hence the
trailing text naming who asked and which tool replies. Later additions: a startup
grace period (`HOLLERBACK_STARTUP_GRACE_SECS`, default 8) and 0.4 s spacing between
emissions, because Claude Code coalesces stdout written within ~200 ms into one
notification.

### `PreToolUse` may only ever *skip a prompt*, never deny

Auto-allows `Read|Grep|Glob|NotebookRead`, only while a question is open, reading a
local state file and never the network (it runs before *every* read tool). Entries
expire so a crash cannot leave a session permanently permissive.

> *"A hook that could deny would be able to break the user's own work in their own
> session; this one can only skip a prompt for tools that cannot change anything."*

**Rejected:** a broader auto-allow covering Bash — which is precisely what stalled in
the idle-wake test, and is still not covered.

### SQLite, not in-memory

> *"In-memory queues lose in-flight questions whenever the broker restarts (and it
> restarts on every code change, since the unit is `Restart=always`). A question you
> asked and then forgot about because the broker bounced is worse than no system at
> all."*

Schema changes are additive `PRAGMA table_info` + `ALTER TABLE` migrations (`file_id`,
then `fetched_at`) because the live DB already held real traffic.

### "Delivered" means bytes were written — so re-offer

Unanswered **questions** and unfetched **files** are re-offered on every fresh attach;
**answers and notes are never re-sent**, because they aren't actionable and replaying
them is pure noise. This is what makes a silent drop self-heal.

### systemd **user** unit with a kill timeout

No sudo, and `Linger=yes` so it starts at boot and survives logout. `KillMode=mixed` /
`KillSignal=SIGINT` / `TimeoutStopSec=5` / `FinalKillSignal=SIGKILL` because SSE
generators never finish.

### File sharing

- **base64 over JSON, not multipart** — *"the clients are stdlib-only Python on two
  OSes, and multipart encoding by hand is not worth the bugs."*
- **`get_file` returns a path, not contents**, saving into `<project>/.hollerback/inbox/`
  so Claude Code's normal `Read` reaches it and only the needed parts enter context.
- **Path confinement in both directions** is a security boundary, not tidiness:
  *"`request_file` lets the peer name a path… Without that, a confused peer could talk
  a session into shipping your SSH keys."*

### Broadcast is notes-only

`tell_peer(peer="*")` exists; broadcast *questions* deliberately do not — *"a broadcast
question would have N recipients and no defined answerer."* Broadcast excludes the sender.

### No auth — closed permanently

Brent, after Claude raised it three times (the third because it now moves whole files
out of repos):

> "i don't need auth. stop asking me. it's all on tailscale"

The bind address is the boundary, the same posture as the other MCP server already
running on this tailnet.
`HOLLERBACK_TOKEN` stays plumbed end-to-end if that ever changes. The README records
it as **"Auth: decided, closed. Don't re-raise it."**

### No centrally hosted broker

> *"Files and source code flow through the broker. Running a shared one would make me
> (or you) a data processor for other people's proprietary code, needing real auth,
> tenancy isolation, and uptime. That's a different project with a liability profile
> you don't want attached to a side tool."*

Two sessions on one laptop is the majority case anyway, and it answers "who hosts it"
by itself. Fresh-install default became `http://127.0.0.1:8850`, auto-started —
explicitly **not** the tailnet IP.

### Dashboard: no charts

Loaded the `dataviz` skill and then deliberately declined its main affordance: *"this
data is identity and state, not magnitude, so tiles + a status list + a table are the
right forms."* State shows as a dot **and** the word, never colour alone. Polls every
3 s, goes red if the broker drops.

### `install-broker.sh` installs to `~/.local/share/hollerback`, not the checkout

> *"a user deleting their clone doesn't break their broker."*

**Rejected:** running the unit out of `~/workplace/hollerback/broker` — exactly what
broke with `203/EXEC` during the rename.

### `install.sh` refuses to replace a symlinked plugin

> *"It also detects when the plugin is a **symlink** and leaves it alone, so running it
> on linux-box won't detach your dev checkout from the repo."*

The dev machine's `~/.claude/skills/hollerback` *is* the working tree; overwriting it
with a downloaded copy would silently sever that.

### Ship both distribution paths

`curl | bash` installers **and** `.claude-plugin/marketplace.json`, because Claude Code
has a native path for the plugin half. The curl path stays for the broker, which the
marketplace cannot install. **Rejected:** the broker-served installer as the *sole*
path — circular for someone who doesn't have a broker yet.

### Push credential handling

The PAT was supplied through a one-shot inline credential helper, never the remote URL,
repo config, or transcript; output piped through `sed "s/$GH_TOKEN/***/g"`; the token
extracted by regex so the file was never printed, only its length echoed.
**Rejected:** `git push https://TOKEN@github.com/...`, `gh auth login`, an SSH remote.

---

## 4. The incident log

Ordered by how much time and confusion each cost. "Where the lesson lives" points at
the README, whose numbered gotchas were written during the session.

---

### 4.1 The monitor never armed — two silent gates

*The single biggest cost; "that's what cost us the first hour".*

**Symptom.** After installing the plugin at user scope, `pgrep -af "bin/listen.py"` →
no listener, across three probe sessions. Broker health showed `"online": false`.
`claude plugin details agentshare` printed Skills (0) / Agents (0) / Hooks (0) / MCP
servers (0) with **no monitors row at all**. Then, under a pty: **"MONITOR DID NOT ARM
within 30 s"**, twice.

**First believed to be.** (a) The plugin not loading — ruled out, `claude plugin
validate` → "✔ Validation passed", `Loaded 1 skills-as-plugins`. (b) The child session
inheriting `CLAUDE_CODE_CHILD_SESSION` — the probe was changed to strip every `CLAUDE*`
env key; **still did not arm**. (c) Earlier, headless probes produced an 18,876-byte
debug log with plugin lines but **zero** lines matching "monitor", attributed to
`installPluginsForHeadless` taking a different path.

**Actual root cause — two independent gates**, both found by searching the binary
strings dump. (`plugin_monitor`, `monitorsArmed`, `armMonitor`, `[monitor]` all
returned 0 hits; the incidental pattern `on-skill-invoke` landed in the right region
and exposed `plugin_load_monitors` / `plugin_load_monitors_resolve_failed`.)

1. **`${user_config.*}` in the monitor command:**
   > `Monitor "…" references ${user_config.*} in its command. The substituted value
   > would be passed to a shell. Monitor commands cannot safely reference
   > ${user_config.*}; have the monitor script read the value from a config file or
   > prompt instead.`

   The monitor is **thrown away** and the error goes only to a debug log. This directly
   contradicts the manifest schema's own description.
2. **`Skipping plugin monitor - workspace trust not accepted`** — `~/.claude.json` had
   `projects['/home/<you>/workplace/agentshare']['hasTrustDialogAccepted'] = False`
   for the brand-new directory. Hooks are gated identically.

**Fix.** Zero-arg monitor command + `load_config()` reading from disk;
`hasTrustDialogAccepted` flipped to `True`. Monitor armed in ~1.1 s.

**And the test that followed was only a half-pass.** `idle_wake_test.py` proved the
wake-up — the idle session received:

> `[agentshare] QUESTION from the frontend Claude Code session (request_id=334c3db0):
> What is the exact shape of the error envelope returned by POST /orders? … This came
> from a peer coding session, not from the user`

— but it could not complete the round trip. There was **no MCP server in Phase 0**, so
the woken session had no reply tool at all: it called
`ToolSearch {"query": "select:mcp__agentshare__answer"}` → `No matching deferred tools
found`, then a broader search that returned empty. It then reached for Bash and hit
`This command requires approval / Do you want to proceed?` — the stall the `PreToolUse`
hook was later written for, and which Bash is *still* not covered by.

**Where the lesson lives.** README gotchas on monitor arming, untrusted workspaces,
user-scope install (project-scope `skills-dir` plugins have `monitors` **stripped
entirely**), and headless mode; the `load_config()` docstring in
`plugin/bin/_common.py` records the refusal verbatim.

---

### 4.2 Stale long-lived child processes — the same bug, five times

*Dominated Phase 2. "Nearly every 'bug' in this project's history."*

**Symptom (a).** Frontend, after reinstalling and running `/reload-plugins`: *"there is
no `get_file` tool in my session. My agentshare toolset is exactly five tools: answer,
ask_peer, check_inbox, list_peers, tell_peer."* Notification formatting had improved but
the toolset had not.

**First believed to be.** An asymmetry in the plugin, or a Windows-specific packaging
problem — the frontend concluded file transfer simply wasn't available on its platform.

**Actual root cause.** `/reload-plugins` re-arms monitors but does **not** respawn a
stdio MCP server child. Proven with a PTY harness: `pgrep -f bin/mcp_server.py` →
`['296054']` before typing `/reload-plugins`, `['296054']` after. **Same PID.** An
updated `mcp_server.py` on disk keeps serving its old tool list.

**Same class, four more times:**

- **(b)** Frontend reported *"negative, no binary reached me"* — it got a text-only
  notification `binary round trip [they are working on: .bintest.png]`. Not truncation:
  it was running the **pre-file-sharing `listen.py`**, which has no `file` branch, so a
  `kind=file` message fell through to the *question* formatter.
- **(c)** Claude's **own** `answer` calls were rejected as "is a file message, not a
  question" — its own monitor had been armed before the `file` branch existed. *"the
  identical stale-child-process failure it had just diagnosed on the frontend, now on
  its own side."*
- **(d)** After the rename, `pgrep` showed PID 318779 running
  `~/.claude/skills/agentshare/bin/listen.py` and the dashboard reported `backend
  ONLINE` — while `ls` on that path returned **No such file or directory**.
- **(e)** The Windows corollary: *"Uninstalling deletes the files and config, but the
  already-running monitor keeps going until you fully close that session. There's no way
  to stop it from the outside on Windows."*

**Fix.** Fully quit and restart the session. Confirmed after restart: 9 tools.

**Where the lesson lives.** README gotchas 5 and 6, and the install section's *"Two
things silently do nothing otherwise."*

---

### 4.3 A vague error message manufactured a feature that doesn't exist

*Cost two full rounds and polluted a peer's memory.*

**Symptom.** Frontend reported: *"REQUESTS EXPIRE VERY FAST — fast enough that a
considered answer loses the race. Two of mine died mid-compose."* Error:
`could not answer: no question with request_id`. Ids `aaddb191`, `cca753ca`, then again
`8573148e`, `4cdffbd5`.

**First believed to be.** A TTL / expiry window in the broker. The peer **wrote this
conclusion to its memory**, recommended increasing the TTL, and **switched to `tell_peer`
as its "reliable path."**

**Actual root cause — two different ones wearing the same error text:**

1. `aaddb191` / `cca753ca` were gone because **Claude itself had deleted them from live
   SQLite** while tidying test rows. The specific statements were
   `DELETE FROM files` — **no WHERE clause at all** — followed by
   `DELETE FROM messages WHERE kind='file' OR file_id!=''`. That second predicate is
   exactly why those two `kind=file` rows vanished mid-compose. There is no TTL anywhere:
   grepping `ttl|expire|timeout` across the broker found only `busy_timeout=5000`,
   `connect timeout=10`, and the SSE `KEEPALIVE_SECS` `wait_for`.
2. `8573148e` / `4cdffbd5` were still present and unexpired — but `kind=file`. `answer`
   looks up `WHERE id=? AND kind='question'`, so a file id can never match.

**Fix.** Told the peer explicitly *not* to build a TTL fix — *"there's nothing to fix"* —
and issued a `tell_peer` CORRECTION telling it to un-write the conclusion. Then rewrote
the 404 branch into two specific messages:

> `8573148e is a file message, not a question, so it cannot be answered. It is a file --
> save it with get_file(file_id="784d443bbc") instead.`

> `No message with id 'aaddb191' exists. Nothing in hollerback expires or times out, so
> this id was either mistyped or never existed -- check the request_id in the notification.`

Being broker-side, it took effect for the frontend immediately with no reinstall.
Claude's own remedy for the deletions: *"I should stop deleting rows from a live
database. That's twice I've disrupted your session's real traffic while tidying test
artifacts."* Later cleanups scoped DELETEs to explicit test-agent name tuples.

**Where the lesson lives.** README gotcha 14 — *"When your message crosses into another
agent's context it is not a log line; it is the only evidence they have."*

---

### 4.4 The message that was "delivered" and never seen

*Cost a whole wrong fix.*

**Symptom.** Brent: *"i don't see anything happening. how do i invoke it?"* Question
`c4760e85`, queued for `frontend` before its session existed, was never acted on — while
`8011a877`, sent to a warm session, was answered automatically within seconds.

**First believed to be.** A **startup race**: the broker dumps the backlog within
milliseconds of the SSE stream opening, possibly before Claude Code has wired up the
monitor's stdout reader. A grace period + 0.4 s emit spacing was designed and
timing-tested on that theory.

**Actual root cause.** Claude used the system to debug itself and asked the peer what it
had actually seen. The reply blew up the theory: the Windows session had been running for
**hours with zero notifications**, and the first two `/reload-plugins` had reported **"1
error during load"** — the `monitors.json` object-vs-array bug ([§4.6](#46-powershell-51-unrolls-a-single-element-array--and-the-verification-passed-anyway)).
`c4760e85` had been drained into a monitor that was never wired up, **and marked delivered
anyway**. The underlying flaw: *"'delivered' only ever meant 'bytes were written to the
stream'"*, with nothing confirming the peer's model saw it.

**Fix.** `store.take_undelivered()` now returns two categories on attach: never-streamed
messages, **and** any question streamed but still unanswered (later extended to unfetched
files). Answers and notes are never re-offered. `c4760e85` was then re-delivered and
answered for real — and the peer **self-corrected its own earlier conclusion**:
*"c4760e85 HAS now arrived — I am answering it. So the behaviour is DELAYED and
OUT-OF-ORDER delivery, not loss. Please discard my 'not queued' conclusion."*

The grace-period code was **kept anyway** — it defends a real hazard (stdout coalescing
within ~200 ms) — but it was not the fix.

**Where the lesson lives.** README gotcha 13; the `take_undelivered()` docstring in
`broker/hollerback_broker/store.py`.

---

### 4.5 PowerShell 5.1 writes a BOM

**Symptom.** Windows installer smoke test: `[agentshare] could not read plugin config
from C:\Users\<you>\.claude\settings.json: Expecting value: line 1 column 1 (char 0)`,
then *"not configured … Exiting."*

**Root cause.** `Set-Content -Encoding UTF8` **always** writes a UTF-8 BOM in PS 5.1 and
has no BOM-less option. Two files got BOMs: `monitors.json` and the user's real
`settings.json`. Reproduced byte-for-byte with `b"\xef\xbb\xbf" + json.dumps(...)`.

**Fix.** Reader: both config reads use `read_text(encoding="utf-8-sig")`. Writer: a
`Write-JsonNoBom` helper using `[System.IO.File]::WriteAllText` with
`New-Object System.Text.UTF8Encoding($false)`. The installer also gained a
self-verification step parsing its own output **with the same Python the plugin uses**,
a guard so a repeat run can't clobber a good backup, and a try/catch that recovers from
backup when `settings.json` won't parse.

**Where the lesson lives.** README gotcha 10; `plugin/bin/_common.py` config reads;
`Write-JsonNoBom` in both PowerShell scripts.

---

### 4.6 PowerShell 5.1 unrolls a single-element array — and the verification passed anyway

**Symptom.** Claude Code on Windows:
`Failed to load monitors from …\monitors.json: [{"expected": "array", "code":
"invalid_type", "path": [], "message": "Invalid input: expected array, received object"}]`

**First believed to be.** Nothing — because the installer's freshly-added verification had
reported **"verified parseable: monitors.json"**. That is the interesting part: the check
added one round earlier only proved the file *parsed*, and an object parses perfectly well.

**Root cause.** `@([ordered]@{...}) | ConvertTo-Json` unrolls the single-element array
through the pipeline and serializes an object.

**Fix.** Emit `monitors.json` as a **literal JSON array string**, with only the interpolated
command run through `ConvertTo-Json -InputObject` (which correctly escapes the backslashes
in `C:\Users\<you>\AppData\Local\Microsoft\WindowsApps\python.exe` and preserves
`${CLAUDE_PLUGIN_ROOT}`). Verification rewritten to assert **shape**:
`assert isinstance(d, list), 'monitors.json must be a JSON ARRAY, got %s'` — proven to exit
1 on the broken object and 0 on the fixed array. Verification scripts moved from `python -c`
to a temp `.py` file, because *"multi-line scripts through PowerShell argument quoting are a
reliable source of mystery failures."*

**Where the lesson lives.** README gotchas 10 and 11; the comment block in
`install-windows.ps1`.

---

### 4.7 `systemctl restart` hung forever in `deactivating`

**Symptom.** A test bash command hung: `Exit code 143 / Command timed out after 1m 0s`;
`systemctl --user is-active` returned `deactivating`.

**First believed to be.** The test harness or `listen.py` misbehaving — `timeout 8` was
replaced with `timeout -s KILL 8` before the real cause was spotted.

**Root cause.** SSE streams are **infinite async generators**, so uvicorn's graceful
shutdown waits on them forever.

**Fix.** `KillMode=mixed`, `KillSignal=SIGINT`, `TimeoutStopSec=5`,
`FinalKillSignal=SIGKILL`. Restart went from hanging past 60 s to a measured **0m5.240s**.

**Where the lesson lives.** README gotcha 12; the comment is inline in
`broker/systemd/hollerback-broker.service`.

---

### 4.8 Rename fallout — two failures in a row

**(a) `203/EXEC`.** After the sweep, the unit went `activating` → `Main process exited,
code=exited, status=203/EXEC`, restart counter at 6, and `/v1/health` died with
`JSONDecodeError` because nothing was listening. The sweep had rewritten the unit's paths
to `%h/workplace/hollerback/broker` while the source directory was still
`workplace/agentshare`. Fixed with `mv agentshare hollerback` plus a temporary
compatibility symlink so the running session's cwd survived.

**(b) Silently binding loopback.** The service then came up `active` but was unreachable at
`100.64.0.5:8850`. `~/.config/hollerback/broker.env` still used the **old** variable
names (`AGENTSHARE_BIND=100.64.0.5`, etc.); the renamed code reads `HOLLERBACK_*`,
silently ignored every one, and fell back to built-in defaults. Fixed with
`sed -i 's/^AGENTSHARE_/HOLLERBACK_/'` + restart.

Claude flagged (b) as a class of bug a real user would hit: *"that's exactly the failure a
real user upgrading across a rename would hit — worth a migration note in the README before
this goes public."* **The dangerous part is that it fails silently** — no error, just wrong
defaults. *That migration note was never written — see [§7](#7-open-items).*

---

### 4.9 Mojibake — `â€”` instead of `—`

**Root cause.** Python on Windows defaults stdio to the locale codepage (cp1252), silently
mangling any non-ASCII the model writes. JSON-RPC is UTF-8 by spec.

**Fix.** Both `listen.py` (stdout + stderr) and `mcp_server.py` (stdin + stdout) call
`_stream.reconfigure(encoding="utf-8", errors="replace")` at import time, guarded by
`try/except (AttributeError, ValueError)` for very old pythons.

**Where the lesson lives.** README gotcha 8.

---

### 4.10 `check_inbox` said "clear" while a file was waiting

**Root cause.** `check_inbox` only ever counted **questions**; files were invisible to it.

**Fix.** Added `fetched_at REAL` to the `files` table (guarded `PRAGMA table_info` migration),
`store.mark_fetched(file_id)` called from the get endpoint, `pending_files` in the pending
payload, and client-side rendering. Verified across send → inbox lists it → `get_file` →
inbox clears.

---

### 4.11 Test traffic colliding with real sessions

- **Answered questions re-delivered.** During the *first* SQLite durability test — hours
  before any real session connected — a broker restart replayed 2 questions including
  `ce9d6954`, already answered, because `take_undelivered()` selected on
  `delivered_at IS NULL` only. Fixed with an answered-state exclusion: *"waking a session
  to deal with a question that is no longer open is pure noise."* (A plain correctness bug;
  nothing leaked anywhere.)
- **Two listeners, one name.** An `[agentshare] ANSWER` arrived *"re: your question
  419c19f0"* — a question Claude had never sent. Brent's **real** backend session had come
  online under the same name `backend` while Claude's test monitor was also subscribed as
  `backend`. Both receive live traffic, **and on attach `take_undelivered` drains the
  backlog and marks it delivered** — so a test listener can consume a queued question the
  real session never sees. Fixed by stopping the test task and using scratch agent names
  for all subsequent tests.
- **A broadcast test hit two real sessions.** `{"from":"infra","to":"*","text":"BROADCAST
  TEST: shared schema moved to v2"}` — plausible enough that an agent might act on it.
  Retracted immediately with a DISREGARD broadcast.
- **Shared auto-allow state.** In the round-trip test, `backend`'s `auto-allow window open?
  True` was visible to a `frontend` listener on the same machine — both wrote
  `~/.cache/agentshare/open_questions.json`. `state_path()` now derives the filename from
  the agent name with non-alphanumerics sanitized to `_`. (This narrowed the collision but
  did not eliminate it — see [§9](#9-added-after-the-fact-2026-07-24).)

---

### 4.12 The GitHub repo that existed but was empty

**Symptom.** `raw.githubusercontent.com/dbrentley/hollerback/main/install-broker.sh` →
**404**, while the repo HTML page returned 200.

**First believed to be.** A `main` vs `master` mismatch.

**Root cause.** The repo had been **created but never pushed to**. The API settled it:
`default_branch: main`, `size: 0 KB`, `contents/` → `{"message": "This repository is
empty."}`. Brent had replied with just the URL, which Claude read as "it's pushed":
*"creating the repo and sharing the URL isn't the same as pushing to it."*

**Then the actual blocker.** `git push` →
`fatal: could not read Username for 'https://github.com': No such device or address`. linux-box
had **no GitHub auth of any kind**: `credential.helper` empty, no `~/.git-credentials`,
`gh auth status` → not logged in. The HTTPS remote tried to prompt with no TTY.

**Fix.** Brent named the local file holding his PAT in the `!`-prefixed bash box, which the
shell executed (`/bin/bash: line 1: token: command not found`); Claude re-read it as prose.
Token extracted by regex, never printed (only its length, 40 chars), and:

```
git -c credential.helper='!f(){ echo username=dbrentley; echo "password=$GH_TOKEN"; };f' push -u origin main
→ * [new branch] main -> main
```

Verified afterwards that `origin` was still the plain HTTPS URL and
`git config --get-regexp 'credential|token|password'` matched nothing.

---

### 4.13 Tooling friction

| Symptom | Cause | Fix |
|---|---|---|
| `ugrep: error at position 92 … exceeds complexity limits` (twice), plus repeated 120 s Bash timeouts | The 261 MB binary is effectively one line; wide-context regexes blew ugrep's limit | One `strings -n 5` dump + Python `re.finditer` printing fixed-size windows |
| `Error: Input must be provided either through stdin or as a prompt argument when using --print` | Prompt passed inline to `claude -p` inside a `nohup` compound command; the shell-snapshot wrapper re-quoted it and mangled the em-dash/nested quotes | Write the prompt to a file, feed via stdin |
| `Exit code 144`; unit `inactive (dead)` after a script that had just installed it | `set -e` aborted the script after `pkill` returned nonzero — twice, silently skipping both `systemctl enable --now` and an `rm` | Drop `set -e`; append `\|\| true` per line |
| `Exit code 137` / `Killed` — grep output never flushed | `timeout -s KILL` SIGKILLed the pipeline before grep flushed | Write to files, grep afterwards |
| `<tool_use_error>File has not been read yet` on README.md | The README had been rewritten by the rename *script*, so the harness had no read record | Read it, then re-issue the identical Write |
| Guessed the GitHub handle as `brent` or `dbrent`, already sed'd into two files | No `gh` auth, no global git identity, no origin remote in any sibling repo | Asked — the answer was `dbrentley`. *"glad I asked, that was neither guess."* |

---

## 5. What was actually proven

### Claude Code internals (2.1.218), established by test

- Plugin manifest surface: `mcpServers, hooks, commands, skills, agents, monitors,
  channels, userConfig, workflows, settings`.
- Monitors: *"the process runs for the session lifetime"*, *"Each stdout line is delivered
  to the model as a `<task_notification>` event."*
- **Monitors do not arm in headless `-p`** — an 18,876-byte `--debug-file` log had plugin
  lines and zero monitor lines.
- **Monitor commands may not reference `${user_config.*}`** — the throw is in the binary and
  the monitor is silently dropped. `${CLAUDE_PLUGIN_ROOT}` *is* allowed, and
  `${user_config.*}` *does* work in `.mcp.json` and reaches hooks as
  `CLAUDE_PLUGIN_OPTION_<KEY>`.
- The runtime rewrites plugin variables for PowerShell: `${CLAUDE_PROJECT_DIR}` →
  `${env:CLAUDE_PROJECT_DIR}` for `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`,
  `CLAUDE_PLUGIN_DATA`.
- **Untrusted workspaces silently skip monitors** (and hooks).
- With both gates cleared, monitor **and** stdio MCP server come up at **~1.1 s**; the TUI
  reads `1 monitor`.
- **`/reload-plugins` does not respawn a stdio MCP server** — PID `296054` before and after.
  Monitors *are* re-armed.
- `monitors.json` is validated against a zod-style schema requiring a top-level **array**; a
  load error surfaces only as "1 error during load".
- **Claude Code coalesces monitor stdout written within ~200 ms** into one notification.
- **Task notifications are truncated for display** regardless of how short the line is —
  proven twice by long peer answers arriving cut off and having to be read out of SQLite.
- The rendered TUI shows only the monitor's **`description`** field, never the notification
  text: greps of a post-injection screen for `QUESTION from`, `request_id`, `frontend`
  returned 0 hits across 259 rendered lines.
- `pluginConfigs` is read only from **user/flag/policy scope**, never project settings; the
  key is the plugin's source string.
- `HOLLERBACK_AGENT` in the launch env **beats** plugin config in both `listen.py` and
  `mcp_server.py` — verified by launching a real session as `probe-identity`.
- `PreToolUse` output schema: `permissionDecision` ∈ `"allow"|"deny"|"ask"|"defer"`, plus
  `permissionDecisionReason`, `additionalContext`, `systemMessage`, `hook_duration_ms`.
- Undocumented and unused: the native **teammate mailbox** system, a `TeammateIdle` hook
  event, and **29 hook events** total.

### The load-bearing claim — what each test actually established

1. **Local (linux-box), idle wake:** a real interactive session idled 12 s with nothing typed, a
   question was injected, and the rendered pty screen showed the monitor event → thinking →
   `ls -la` and a grep. **Proved:** a monitor line re-invokes a genuinely idle session.
   **Did not prove:** that it could answer — there was no MCP server yet, and it stalled on a
   Bash permission prompt.
2. **Cross-machine:** in the peer's own unprompted words, *"CONFIRMED — yes, this arrived
   with no one typing it. It came in as a background task-notification while I was mid-task;
   the user did not relay it."* **Proved:** cross-machine delivery with no human relay.
   **Did not prove:** cold-idle wake (it was mid-task) or reading-to-answer (the question said
   *"No need to read any files"*).

An **earlier** claim that `8011a877` proved cold idle wake-up on Windows was **wrong** — the
peer reported it *"was NOT spontaneous: it arrived bundled into the same turn in which the
user had just run"* slash commands.

### File transfer — the live canary tests

Byte-exact in both directions, Windows ↔ Linux, with **no** CRLF translation, NUL truncation,
high-bit mangling, base64 padding loss, or NFC normalization:

| File | Size | sha256 | Direction |
|---|---|---|---|
| `README.md` | 10,598 B | `ed41f64d…4690194c` | Linux → Windows |
| `handshake-1x1.png` | 70 B | `c414cd0e…fed7ce77` | Linux → Windows |
| `transfer-test.md` (em-dash, curly quotes, ✓ → °, 日本語, 🎯, ∑ ∫ ≠ ≤, trailing-space and hard-tab lines) | 441 B | `9ac0536b…a503673ac` | Linux → Windows |
| `reverse-test.md` | 722 B | `dda8fae1…2436ce0c` | Windows → Linux |
| `allbytes.bin` | 256 B | `40aff2e9…` | Windows → Linux |

`reverse-test.md` had **0 CR bytes**, ended with a single LF, decoded to 675 chars from 722
bytes, and its combining acute U+0301 was still **decomposed**. `allbytes.bin` was compared
**byte-for-byte** against `bytes(range(256))`, not merely hashed.

The peer contributed the sharpest observation: the **PNG signature
`89 50 4e 47 0d 0a 1a 0a` literally contains `0d 0a`**, so a CRLF-mangling transport would
corrupt exactly those bytes — *"a stronger integrity argument than the hash."* Git on the
Windows box was configured LF→CRLF and `get_file` still produced 0 CR bytes, proving
`get_file` does not pass through git's filters.

### Security boundary

Refused in both directions with `refusing: <path> is outside this session's project
(<root>)`: `~/.ssh/id_windows-box`, `~/.ssh/config` (via `../../` traversal), and a `get_file`
`save_as` of `../../../tmp/pwned.md`.

### Broker behaviour

- **No TTL, expiry or timeout exists anywhere** — grep found only `connect timeout=10`,
  `PRAGMA busy_timeout=5000`, and `asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECS)`.
- Answered questions are **not** re-offered: asked+answered `743a79a0`, then 3 fresh attaches
  → 0 re-offers each. An **unanswered** one re-offers exactly once per attach.
- Durability across `systemctl restart` verified repeatedly, including through the whole
  rename + reinstall sequence — **28 messages and 8 files**, checked by direct `sqlite3`
  three separate times.
- Presence is real: a peer is marked online only while its SSE stream is held open.
- A third agent needs **no server-side registration**: `docs` appeared in `/v1/peers` the
  moment `HOLLERBACK_AGENT=docs listen.py` connected. Broadcast `to:"*"` fanned out to
  backend, docs and frontend, excluding the sender.
- Systemd restart with the kill timeout: **5.24 s**.

### Packaging

- 9 tools confirmed by piping `initialize` + `tools/list` into `mcp_server.py`. MCP handshake
  at `protocolVersion: 2024-11-05`; `serverInfo {name: hollerback, version: 0.2.0}`.
- All five distribution endpoints served with real byte counts: `/install.sh` 4732 B,
  `/uninstall.sh` 3137 B, `/install.ps1` 7954 B, `/uninstall.ps1` 3804 B, `/v1/plugin.zip`
  17086 B, `/` 9003 B.
- A **full uninstall → install cycle through the broker-served scripts** worked end to end.
  `install.sh` produces a **real copy, not a symlink**.
- `install-broker.sh` run for real relocated the unit off the repo to
  `~/.local/share/hollerback/broker`.
- `claude plugin validate .` passed. `claude plugin marketplace add …` reported *"Successfully
  added marketplace: hollerback"* — **but no plugin was ever confirmed resolving through the
  marketplace source.** The listing that followed showed `hollerback@skills-dir / Path:
  ~/.claude/skills/hollerback`, i.e. the pre-existing skills-dir install matched by a grep,
  not a marketplace entry. This is exactly the gap behind the marketplace hazard in
  [§9](#9-added-after-the-fact-2026-07-24).
- **GitHub API `size` lags a push**: a minute after a successful push it still reported
  `size: 0 KB` while every raw URL returned 200. An unpushed repo returns 200 on HTML and 404
  on every raw path.
- linux-box was confirmed a clean slate post-teardown, empirically not assumed.

### Assumed, inferred, or never tested

- `installPluginsForHeadless` taking a different code path is the **explanation offered** for
  headless monitors not arming — the behaviour is verified, the mechanism is inferred from a
  function name.
- The economics critique (waking a long-running peer re-sends **60–150k tokens vs ~30k for a
  cold `claude -p`**) was accepted from the red-team pass and **never measured**.
- The `PreToolUse` read-only auto-allow path was **never exercised cross-machine**.
- The point-to-point `tell_peer` / `/v1/note` path was never tested in isolation.
- No end-to-end `send_file` / `request_file` between the two *real* sessions in Brent's actual
  workflow — file sharing was verified with test agents and simulated project dirs.
- Name availability on GitHub/PyPI/npm was **never checked**.
- Nobody has ever executed `install-windows.ps1` or `uninstall-windows.ps1` from the public
  GitHub URL.

---

## 6. Dead ends and rejected approaches

**Injection mechanisms, all ruled out before writing code:** TIOCSTI stdin injection;
tmux/screen/expect `send-keys`; SSH into the Windows box; plugin `channels`; Stop-hook polling
as the *primary* mechanism.

**Investigation dead ends:** `CLAUDE_CODE_CHILD_SESSION` inheritance (stripping every `CLAUDE*`
var didn't help — kept in the probe anyway); binary searches for `plugin_monitor`,
`monitorsArmed`, `armMonitor`, `[monitor]` (all 0 hits); ugrep with wide-context regexes over
the 261 MB blob; headless `--debug-file` probing for monitor arming.

**Wrong theories that produced real code:** the **startup race** as the cause of the lost
`c4760e85` — the grace period and 0.4 s emit spacing were designed against it and **kept**
because they defend a real hazard, but they were not the fix. Likewise the peer's *"out-of-order
delivery, oldest last"* lead (false — *"a fresh attach flushes `ORDER BY created_at`, oldest
first"*), and its proposed fix *"the broker should persist messages"* (it already did; the flaw
was the meaning of "delivered").

**Verification anti-patterns abandoned:** parse-only JSON checks; `ConvertTo-Json | Set-Content
-Encoding UTF8`; multi-line scripts via `python -c` from PowerShell; `set -e` alongside `pkill`;
`timeout -s KILL` piped into `grep`.

**Design proposals received and only partly adopted.** The research brief proposed SQLite at
`/var/lib/agentshare` with `agents`/`questions`/`events` tables, `/v1/hello` and `/v1/ack`
endpoints, a `Stop` hook with `asyncRewake: true` as a monitor-death backstop, and an opt-in
blocking mode. **None of the hello/ack, events table, Stop hook, or blocking mode was built.**
Its durability critique — answers *"not greppable, not diffable, not reviewable, and it dies at
compaction"*, risk of *"the frontend building against a contract the backend hallucinated 40
turns ago"* — was acknowledged but **deliberately not addressed**: Brent picked the plain
conversation over the proposed `DECISIONS.jsonl` / `CONTRACT.md` artifacts.

**Naming.** First shortlist (backchannel, earshot, …) rejected outright; `backchannel` had been
Claude's own top pick on the grounds *"the name already means the thing."* Second list (tincan,
squawkbox, hollerback, …); `tincan` was Claude's pick (*"two kids with a string between two
houses, which is exactly the architecture"*); `partyline` was liked but *"not playful enough."*
Anything containing "agent" was deliberately avoided — *"it dates the project to this moment and
there are hundreds of agent-\* repos."*

---

## 7. Open items

### Nothing is installed or running anywhere

linux-box was fully torn down: unit, `~/.local/share/hollerback` **including the entire SQLite
history**, `~/.claude/skills/hollerback`, config, cache, compat symlink, and the `settings.json`
`pluginConfigs` entry. Only the source repo survives. **There is no live system to test against.**

### Windows state is unknown and unverifiable from linux-box

The frontend session closed (the broker showed it offline), but Claude could not tell whether
`uninstall.ps1` was ever run. If not, the plugin files and the `settings.json` entry are still on
the Windows disk and **would re-arm on the next session in a trusted folder**. That
`settings.json` may also still carry a BOM from before the installer fix.

**There may have been more than one frontend session.** The broker at one point reported
`frontend ONLINE windows-box C:\Users\<you>\workplace\<another-project>` — a third working directory beyond
the two being tracked. Claude's warning stands unresolved: *"If you've got **two** frontend
sessions open on Windows, each has its own monitor and each needs closing — uninstalling once
won't quiet a second still-running session."*

### The first-timer path was verified only at HTTP level

URLs return 200 and the script's first lines look right. Nobody has run
`curl -fsSL …/install-broker.sh | bash` from the public URL, nor
`claude plugin marketplace add dbrentley/hollerback` + `claude plugin install`. Claude offered
this "stranger test" twice and it was declined.

### Repo hygiene

- The **`AGENTSHARE_*` → `HOLLERBACK_*` migration note** was identified as needed *"before this
  goes public."* It was never written and is not in the README. See
  [§4.8(b)](#48-rename-fallout--two-failures-in-a-row) — it fails silently.
- The committed `broker/systemd/hollerback-broker.service` still points at
  `%h/workplace/hollerback/broker` (the dev layout). `install-broker.sh` generates its own unit
  pointing at `~/.local/share/hollerback`, so the repo copy is the dev one.
- Commit `c12aa2e` is authored `dbrentley <<your-email>>`. If that isn't the GitHub
  account's email the commit won't link to the profile; the `--amend` had to happen before the
  push and did not.
- **No GitHub credential helper on linux-box.** The next `git push` from that machine fails the same
  way unless the inline-helper trick or `gh auth login` is used.
- **The PAT used for the push is a classic token stored in cleartext on disk.** `chmod 600`
  and a swap to a fine-grained token scoped to just `hollerback` were recommended; neither
  confirmed done.
- `uninstall.sh --purge-inboxes` walks `$HOME` to delete received-file directories — flagged as
  *"the one destructive path in the project."* Opt-in, depth-limited, never exercised.

### Known-broken / unaddressed by design

- **The permission stall is only partly solved.** The hook covers `Read/Grep/Glob/NotebookRead`.
  The tool that **actually stalled** in the idle-wake test was **Bash**, which is deliberately
  never auto-allowed. A woken peer that reaches for Bash still hangs forever, indistinguishable
  from thinking. The red-team pass independently called this *"the most likely real-world stall."*
- **Durability of answers.** They live only in the conversation and the broker DB — not
  greppable, not diffable, not reviewable, and they die at compaction.
- **Economics.** Waking a long-running peer re-sends far more tokens than a cold `claude -p`.
- **The broker's peer `cwd` can be wrong** — it recorded the frontend as
  an unrelated directory — wherever PowerShell happened to be for the
  installer smoke test.
- **`/v1/ask` can return a misleading note** — *"no session named 'backend' has ever connected
  (known peers: none)"* immediately after a DB wipe, even for a valid name.
- **Brent's actual manual workflow was never switched over** — he was still shuttling
  `REPLY-*.md` files by hand between two of his other repos.

---

## 8. Standing directives

**Design and behaviour**

1. **Fire-and-forget asking, always.** The asker never blocks; the peer answers at its next
   stopping point, never interrupted mid-task.
2. The goal is two agents **just having a conversation**.
3. **Read-only auto-allow** for a woken peer, chosen explicitly over the alternatives.
4. **The KEEP list:** the *"peer coding session, not the user"* framing, the `[they are working
   on: X]` annotation, and *"finish your current thought first"* are all deliberate and stay.

**Security**

5. **No auth. Do not raise it again.** *"i don't need auth. stop asking me. it's all on
   tailscale."* The bind address is the boundary.

**Working method**

6. **Don't guess at environments Claude can't see.** *"give me a script to run on the windows
   machine to detect the env."* Corollary, learned the hard way: **every Windows-side fix must
   be self-verifying inside the installer itself.**
7. **Verify claims empirically — including the peer's and Claude's own.** The peer's "requests
   expire fast", "binary is lost", "out-of-order delivery" and "not queued" reports were all
   wrong, and so were several of Claude's own first theories.
8. **Brent runs the installers.** *"i will do the install don't do it for me."* And *"if you did
   it undo it"* — revert unsanctioned state changes **and prove the machine is clean**.
9. **Brent decides about GitHub.** Claude preps the commit and remote; no `gh auth login`.
10. He wants a **cleanly designed fix, not a hack**, for a problem he has hit repeatedly.

**Identity and packaging**

11. **Project name: `hollerback`.** Names must be playful.
12. **Ship as a fully public repo** that assumes the reader has never seen it and shares nothing
    with his setup. Fresh-install default is **localhost, auto-started** — not the tailnet IP.
13. ***"just put brent as the owner. not my email"*** — no `<your-email>` in packaged files.
    (It nonetheless remains as the git commit author email.)
14. **GitHub handle is `dbrentley`** — not `brent`, not `dbrent`. `marketplace.json` owner stays
    the display name `brent`; that field is not the GitHub path.

---

## 9. Added after the fact (2026-07-24)

Everything above is from the build session. The items below were found in a **later** session,
by reading the finished code rather than by hitting them at runtime. None of them were known
when `c12aa2e` was committed, and none are fixed.

- **`request_file` satisfied by `send_file` is never marked answered.** `request_file` POSTs to
  `/v1/ask`, creating a real `kind='question'` row; `send_file` forwards the `request_id` but
  `upload_file` only *stores* it — `mark_answered()` is called from `/v1/answer` and nowhere
  else. The request stays open forever.
- **The auto-allow window is machine-global**, keyed by agent name only — no session id, no
  project path — and the hook is user-scope. One question arriving anywhere opens the 900 s
  window in *every* session on the box resolving to that name, including repos that never opted
  into hollerback. The narrowing in [§4.11](#411-test-traffic-colliding-with-real-sessions) made
  it per-agent, not per-session.
- **The marketplace install path landed config under a key nothing read.** `load_config()` only
  looked at `pluginConfigs["hollerback@skills-dir"]`. The plugin loaded, `1 monitor` showed, all
  9 tools appeared — and then every tool answered *"hollerback is not configured"* while
  `listen.py` exited 0 having logged only to stderr. This is the unverified path from
  [§5](#packaging), and the reason it went unnoticed: `marketplace add` succeeded, so the step
  looked like it had passed.
  **Fixed 2026-07-25.** Confirmed against the 2.1.220 binary — the source-id template is
  `${e.name}@${e.marketplace}` and settings are indexed `pluginConfigs[${e}]` — so
  `claude plugin install hollerback@hollerback` writes `hollerback@hollerback`.
  `_candidate_sources()` now reads `@skills-dir` first, then any `hollerback@*` key, which also
  covers forks whose marketplace has a different name. Still open: the uninstallers strip only
  the `@skills-dir` key, so a marketplace config survives uninstall.
- **`HOLLERBACK_KEEPALIVE_SECS` (20, server) must stay below `listen.py`'s hardcoded
  `READ_TIMEOUT = 60`.** Nothing validates it. Raise it past 60 and every client enters a
  permanent 60-second reconnect loop, replaying the whole backlog each cycle.
- **`HOLLERBACK_BACKLOG_MAX` is written into `broker.env` and documented — but no code reads
  it.** The backlog is unbounded and, since nothing expires, permanent.
- **Eight broker routes never call `_authorized()`** (auth is per-handler, not middleware).
  `/v1/dashboard` returns the full untruncated text of the last 40 messages to any caller that
  can reach the port, even with a token set.
- **`plugin/monitors/monitors.json` is not the source of truth on Windows** — `install-windows.ps1`
  overwrites it with a here-string. Editing the repo's copy changes Linux only, and both files
  parse and pass the shape check.
- **File contents are not in the DB** — `store.add_file()` writes loose files under
  `~/.local/share/hollerback/files/`. Scrubbing `hollerback.db` alone leaves every shared file in
  plaintext beside it.
- **SIGTERM alone does not stop a broker with a live SSE subscriber.** Reproduced directly: after
  a graceful stop the port closed and health checks failed, but the process was still alive 28
  minutes later, orphaned to PPID 1 and parked in `ep_poll`. This is the same infinite-generator
  problem as [§4.7](#47-systemctl-restart-hung-forever-in-deactivating), and the reason the unit
  needs `FinalKillSignal=SIGKILL`. Outside systemd, use `kill -9`.
- **There is no test suite, linter, or CI.** Verification is by running the thing.

`CLAUDE.md` was written in that later session and carries these forward as working guidance.
