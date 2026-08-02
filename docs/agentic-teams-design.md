# Design: Agentic Teams Subsystem

**Status:** Draft
**Scope:** New `teams` subcommand group + background daemon for the existing CLI

## 1. Overview

The `teams` subsystem lets a user organize work into projects, staff each project with a
team of role-based agents (PM, designer, developer, QA, reviewer), and run those agents
through a gated pipeline that keeps a human in the loop at defined checkpoints. The human
interacts primarily through a PM agent — asking for status, issuing directives, approving
or rejecting work — while a persistent **daemon owns orchestration** (scheduling, gates,
state) and **spawns Cursor CLI agents** (`agent` / `cursor-agent`) headlessly inside each
project's repository to do the actual work. Other backends (Claude Code CLI, a local
llamacpp harness) plug in later behind the same adapter contract; Cursor CLI is the v1
path and the reference implementation.

### Goals

- Project/task management with a role-based agent team per project, fully configurable as data.
- Automated design → develop → QA loops with bounded retries and human approval gates.
- **Cursor CLI as the agent runtime:** the daemon runs `agent -p` (headless) against a
  project's workspace path so each role executes as a real Cursor agent in that repo.
- Async human-in-the-loop: approvals and escalations queue in an inbox rather than blocking a terminal.
- A PM agent as the single conversational interface: status queries, directives, scheduled standups.
- Human-editable tasks via `$EDITOR` round-tripping (TUI later), with mid-flight-edit detection.
- Pluggable execution backends behind one adapter contract (Cursor CLI ships first).

### Non-goals (v1)

- Multi-user or remote/hosted operation. Single user, single machine.
- The local llamacpp agent harness (own milestone; requires building a tool-calling loop).
- A TUI (Textual is the intended path, but `$EDITOR` round-tripping covers v1).
- Import/export integrations (Jira, external task apps). The schema is designed so these are
  straightforward later, but nothing ships in v1.
- Parallel execution of multiple tasks per project (design allows it; v1 runs one task at a time
  per project for simplicity).

## 2. Architecture

Three components share one SQLite database:

```
┌─────────────┐   writes rows    ┌──────────────┐   spawns    ┌──────────────────┐
│ CLI          │ ───────────────▶ │ SQLite (WAL) │ ◀────────── │ Daemon            │
│ (subcommands)│ ◀─────────────── │ tasks/events │             │ orchestrator loop │
└─────────────┘   reads status    │ approvals    │             └────────┬─────────┘
                                  │ messages     │                      │ per-task
                                  └──────────────┘                      ▼
                                                              ┌──────────────────┐
                                                              │ Cursor CLI        │
                                                              │ agent -p          │
                                                              │ --workspace repo  │
                                                              └──────────────────┘
```

**Division of labor:** the daemon never runs an agent loop itself. It selects ready work,
composes the prompt, invokes Cursor CLI in the project's `workspace_path`, waits for
completion (or timeout), parses the structured result, and commits the state transition.
Cursor CLI owns tool use, file edits, and model calls inside the repo; we own the queue,
gates, and audit trail outside it.

### 2.1 SQLite as both state store and IPC

There is no socket protocol between the CLI and the daemon. The CLI writes rows (a new task,
a directive, an approval decision) and the daemon polls for actionable work. SQLite in WAL
mode handles the concurrent-readers/single-writer pattern comfortably at this scale.

Consequences:

- `teams tell "use SQLite here"` is `INSERT INTO messages`.
- `teams approve T-12` is `UPDATE approvals SET decision='approved' ...`.
- Crash recovery is free: all state is in the DB. On restart, any task in `running` is
  re-queued or flagged for human attention depending on run outcome.
- The CLI works fully (viewing, editing, queueing) even when the daemon is down; work
  simply doesn't execute until it comes back.

### 2.2 Daemon

A separate entrypoint (`<cli> teams daemon`), run detached or as a systemd user unit. It is
**not** hosted inside the interactive CLI process, so the existing CLI's startup time and
dependency footprint are untouched. A single instance is enforced with an advisory `flock` on
`~/.local/share/<cli>/daemon.lock`; a second daemon fails to acquire it and exits. The lockfile
also records the running daemon's pid and current `epoch` (a fresh id minted each start, see §7).

Main loop (single-threaded scheduler, worker per dispatched task):

1. Poll for unprocessed `messages` → invoke PM agent. An `ask` message yields a read-only
   reply (`from_pm`); a `tell` message yields a `plan_revision` approval, never a direct task
   mutation (see §5.3). The reply/approval and `messages.processed_at` commit in one
   transaction (§2.4).
2. Poll for resolved-but-unapplied `approvals` → apply state transitions (advance stage, or
   route rejection feedback back to the PM as a re-plan trigger). The transition and
   `approvals.applied_at` commit together, so an approval can never be applied twice (§2.4).
3. Poll for ready tasks (status `ready`, dependencies satisfied, no gate pending) →
   dispatch to the appropriate role/backend → record a `runs` row.
4. On run completion: parse structured result, write repo-side effects are already in
   place (the backend edited the workspace), orchestrator performs the state transition,
   appends events, opens gates/inbox items as the pipeline dictates. The `runs.outcome` row
   and the resulting task transition commit in one transaction (§2.4).
5. Fire scheduled jobs (standups) whose schedules are stored in project config.
6. Sleep; repeat. Poll interval ~1–2s; standup scheduling checked once a minute.

### 2.3 State authority (security model)

**Agents never write task state.** This is the load-bearing invariant.

The orchestrator reads the task row, composes a prompt containing everything the agent
needs, runs the backend inside the workspace, and parses the result. The agent's only
outputs are (a) file changes in the repository and (b) structured text the orchestrator
interprets. All state transitions — status changes, gate openings, approvals — are
performed by the orchestrator against the DB. An agent cannot mark its own task approved,
because there is no code path by which agent output mutates state directly; the analogy is
handler ordering as a security property: transition authority lives in exactly one place,
by construction rather than convention.

Corollary: the SQLite file lives **outside** agent workspaces (e.g.
`~/.local/share/<cli>/teams.db`), so a backend with workspace write access cannot touch it.

### 2.4 Exactly-once state changes

External effects (a PM invocation, a backend run) are deterministic-ish and their only durable
consequence lands in the DB, so they can safely repeat after a crash. What must be exactly-once
is the *state change*, achieved by making each unit of work a single SQLite transaction that
both performs the transition and flips its source marker to consumed:

- message → (`from_pm` reply or `plan_revision`) **+** `messages.processed_at`, one txn.
- resolved approval → task transition **+** `approvals.applied_at`, one txn. A
  resolved-but-unapplied row is the only actionable state, so there's no window to apply twice.
- finished run → `runs.outcome` **+** task transition, one txn.

Re-running an interrupted PM/backend after a crash wastes tokens but cannot double-write. An
optional `claimed_at` lease could avoid the wasted work later; it's an optimization, not a
correctness requirement.

## 3. Data model

```sql
CREATE TABLE projects (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  workspace_path TEXT NOT NULL,          -- repo/folder the team operates on
  config_json   TEXT NOT NULL            -- roster, pipeline, gates, backends, schedules
);

CREATE TABLE tasks (
  id            INTEGER PRIMARY KEY,     -- displayed as T-<id>
  project_id    INTEGER NOT NULL REFERENCES projects(id),
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,           -- markdown; the agent-facing spec
  status        TEXT NOT NULL,           -- backlog|ready|running|waiting_human|blocked|done|failed
  stage         TEXT,                    -- current pipeline stage name
  role          TEXT,                    -- role assigned for the current stage
  blocked_by    TEXT,                    -- JSON array of task ids
  gate          TEXT,                    -- pending gate kind, if any
  version       INTEGER NOT NULL DEFAULT 1, -- bumped on every human/PM edit
  loop_count    INTEGER NOT NULL DEFAULT 0  -- qa rejections in the current dev↔qa loop
);

CREATE TABLE runs (
  id            INTEGER PRIMARY KEY,
  task_id       INTEGER NOT NULL REFERENCES tasks(id),
  stage         TEXT NOT NULL,
  backend       TEXT NOT NULL,           -- cursor|claude_code|local
  task_version  INTEGER NOT NULL,        -- task.version snapshot at dispatch
  started_at    TEXT, ended_at TEXT,
  outcome       TEXT,                    -- success|fail|needs_human|error|stale
  result_json   TEXT,                    -- parsed structured result block
  transcript_path TEXT,                  -- raw session log on disk, not a blob
  daemon_epoch  TEXT,                    -- id of the daemon instance that launched this run
  pid           INTEGER,                 -- backend process pid (observability + cleanup)
  pgid          INTEGER,                 -- process group, for group-kill on stop
  proc_start    TEXT                     -- process start time; (pid,proc_start) survives pid reuse
);

CREATE TABLE events (
  id            INTEGER PRIMARY KEY,     -- append-only; feeds standups and status
  project_id    INTEGER NOT NULL,
  task_id       INTEGER,
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL,           -- assigned|completed|escalated|gate_opened|approved|...
  payload_json  TEXT
);

CREATE TABLE approvals (
  id            INTEGER PRIMARY KEY,
  task_id       INTEGER,                 -- NULL for project-level items (plan revisions)
  project_id    INTEGER NOT NULL,
  kind          TEXT NOT NULL,           -- stage_gate|escalation|loop_exhausted|plan_revision
  summary       TEXT NOT NULL,           -- what the human is being asked to approve
  requested_at  TEXT NOT NULL,
  resolved_at   TEXT,
  applied_at    TEXT,                    -- transition committed under this stamp; guards reprocessing
  decision      TEXT,                    -- approved|rejected
  human_note    TEXT,                    -- routed back to the PM/agent as feedback
  payload_json  TEXT                     -- structured task-patch for plan_revision (creates/updates)
);

CREATE TABLE messages (
  id            INTEGER PRIMARY KEY,     -- human <-> PM channel
  project_id    INTEGER NOT NULL,
  direction     TEXT NOT NULL,           -- to_pm|from_pm
  body          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  processed_at  TEXT                     -- NULL = daemon hasn't handled it yet
);
```

Large artifacts stay on disk with paths in the DB: transcripts under the daemon's data
directory, and agent-produced deliverables (design docs, QA reports) inside the repo
itself — e.g. `docs/design/T-12-auth-flow.md` — so downstream stages read them as ordinary
workspace files and they travel with the repo history.

### 3.1 Mid-flight edit detection

Every human or PM edit to a task bumps `tasks.version`. Dispatch snapshots the version
into `runs.task_version`. At run completion, if `tasks.version` no longer matches, the
run's outcome is set to `stale` and an inbox item is opened instead of auto-applying the
transition — the human decides whether the result still stands.

## 4. Core concepts

### 4.1 Project config

A team is data, not code. Everything lives in `projects.config_json` (authored as YAML via
`teams project edit`, stored as JSON):

```yaml
roster:
  pm:        { backend: cursor, model_hint: null, prompt: prompts/pm.md }
  designer:  { backend: cursor, prompt: prompts/designer.md }
  developer: { backend: cursor, prompt: prompts/developer.md }
  qa:        { backend: cursor, prompt: prompts/qa.md }

pipeline:
  - stage: design
    role: designer
    gate: human            # human approves design before dev starts
  - stage: develop
    role: developer
    gate: auto
  - stage: qa
    role: qa
    on_fail: develop       # bounce back with feedback
    max_loops: 3           # then escalate to human
  - stage: review
    gate: human            # end-of-pipeline approval

standups:
  - schedule: "0 9 * * *"
    channel: terminal      # v1: terminal + file; later: webhook|matrix

notifications:
  inbox: [terminal]        # later: webhook, matrix
```

Adding a role (say, a security reviewer) means adding a roster entry and a pipeline stage —
no source changes.

### 4.2 Task lifecycle

```
backlog → ready → running(stage) → [gate?] → next stage → … → done
                        │
                        ├─ qa fail ──▶ develop (bounded by max_loops)
                        ├─ needs_human / loop exhausted ──▶ waiting_human (inbox item)
                        └─ stale (task edited mid-flight) ──▶ waiting_human
```

Gates fire from three triggers: **declared** (pipeline config), **agent-initiated**
(`<needs_human>` sentinel in output), and **loop exhaustion** (`max_loops` hit). All three
produce the same artifact — an `approvals` row surfaced in the inbox — so the human
experience is uniform regardless of why the system paused.

`tasks.loop_count` increments on each QA rejection (one `qa fail → develop` bounce). When it
reaches the stage's `max_loops` the task escalates to `waiting_human` instead of bouncing
again; it resets when the task advances past qa. So `max_loops: 3` reads as "QA may reject
three times, then escalate."

### 4.3 PM agent is stateless

The PM never answers from its own memory. Every PM invocation is cold and receives: the
project config, a task-board summary, recent `events` rows, relevant open approvals, and
the human's message. Status answers, standup digests, and plan revisions are all derived
from the DB at invocation time. This keeps updates truthful across crashes and human
edits, and sidesteps context-window management entirely.

## 5. Human-in-the-loop

### 5.1 Inbox, not prompts

Nothing blocks a terminal on `[y/n]`. Gates and escalations write `approvals` rows; the
task parks in `waiting_human`; the daemon keeps working other tasks. The human drains the
inbox on their own schedule.

### 5.2 CLI surface

```
teams init                          # create DB, register first project
teams project new|edit|list
teams task add|edit|list|show       # edit = $EDITOR round-trip (markdown/YAML), bumps version
teams daemon [start|stop|status]
teams status [PROJECT]              # board overview, derived from DB
teams inbox                         # pending approvals + escalations
teams approve T-12 [-m "note"]      # note is routed back as feedback
teams reject  T-12 -m "reason"
teams chat [PROJECT]                # interactive PM session; can approve drafted revisions inline
teams ask  "question"               # one-shot read-only query to the PM
teams tell "directive text"         # one-shot directive → drafts a gated plan revision
teams standup [--now | --schedule "0 9 * * *"]
teams log T-12                      # events + run transcripts for a task
```

### 5.3 Directives and plan revisions

Read-only questions (`teams ask`, or a question in `chat`) get a direct `from_pm` reply and
change nothing. Directives (“I'd rather you did xyz” via `teams tell` or a chat message) do
**not** immediately mutate tasks. The PM drafts a plan revision — a structured task-patch
(creates/updates) in `approvals.payload_json` plus a free-form summary of what changes and why
— and posts it to the inbox as a `plan_revision` approval. The human approves; only then does
the daemon apply the patch mechanically and let the pipeline execute with its normal gates. A
plan revision is just another gated artifact, so the “double-check the PM understood before
developers build” loop needs no special-casing. In an interactive `chat` session the same
artifact can be approved inline instead of via the inbox — the gate is preserved either way.
Auto-detecting whether a free-form message is actionable is a later layer; v1 keys off the
explicit ask/tell (or chat) distinction.

### 5.4 Standups and notifications

A standup is the PM summarizing `events` since the last digest: completed work, in-flight
tasks, open approvals, anything stuck. v1 delivers to the terminal (on next CLI use) and a
digest file; the notification layer is a small adapter interface so webhook/Matrix
delivery for standups and new inbox items can be added without touching the scheduler.

## 6. Backend adapters

One contract, three implementations (Cursor CLI in v1):

```python
class RunResult(TypedDict):
    outcome: Literal["success", "fail", "needs_human", "error"]
    summary: str                 # agent's own summary of what it did
    details: dict                # structured result block, stage-specific
    needs_human_reason: str | None
    transcript_path: str

class AgentBackend(Protocol):
    def run(self, *, prompt: str, workspace: Path, role_config: RoleConfig,
            resume_token: str | None) -> RunResult: ...
```

Key points of the contract:

- **Prompt composition is the orchestrator's job.** The adapter receives a fully composed
  prompt (role instructions + task body + stage context + prior-stage artifacts + QA
  feedback if bouncing) and knows nothing about tasks or the DB.
- **Structured output by instruction.** Every prompt instructs the agent to end with a
  fenced result block (JSON: outcome, summary, details) and to emit
  `<needs_human>reason</needs_human>` if it judges human input necessary. The adapter
  parses both; a missing/unparseable result block is `outcome=error` and surfaces in the
  inbox rather than being guessed at.
- **Sessions.** `resume_token` supports cheap dev↔QA bounces by resuming a session with
  feedback instead of cold-starting with full context. Adapters that can't resume ignore
  the token and cold-start; the orchestrator treats resume as an optimization, never a
  requirement.

### 6.1 Cursor CLI adapter (v1) — primary backend

This is the supported way agents run in v1: the daemon shells out to Cursor's headless
CLI against the project's repo. Every roster role that sets `backend: cursor` goes through
this adapter (including the PM when answering `ask`/`tell`/`chat`).

Typical invocation shape (exact flags locked by the M2 spike):

```bash
agent -p --output-format json \
  --workspace /path/to/project/repo \
  [--model <model_hint>] \
  [--resume <session_id>] \
  "<composed prompt>"
```

(`agent` and `cursor-agent` are treated as the same CLI; resolve via `PATH` / config.)

Requirements on the adapter:

- **Repo-scoped.** Always bind to `projects.workspace_path` (`--workspace` and/or CWD) so
  the agent edits that tree, not the daemon's data dir or unrelated repos.
- **Fully non-interactive.** Print mode only; no TTY prompts. Force auto-approval /
  yolo-equivalent flags as needed so the daemon never blocks on Cursor's internal
  confirmations. A hang past the wall-clock timeout is `outcome=error`.
- **Structured I/O.** Prefer `--output-format json` (or `stream-json` if we need live
  progress). Parse the final result; tee raw stdout/stderr to `transcript_path`.
- **Auth is the operator's problem.** Cursor CLI must already be logged in on the machine
  (`agent login` / API key). The daemon does not manage Cursor accounts.
- **Process group.** Spawn with `setsid` so `daemon stop` can kill the whole Cursor
  subprocess tree (§7).

Spike items before freezing the contract (do these in M2):

1. Session resume (`--resume` / `--continue`) ergonomics — whether `resume_token` is real
   in v1 or a no-op cold-start.
2. Exact non-interactive / auto-approve flags for the current CLI release.
3. Whether `--workspace` alone is enough vs. also setting CWD.
4. Model selection via `--model` from roster `model_hint`.

Every run gets a wall-clock timeout and its raw output teed to `transcript_path`.

### 6.2 Later backends

- **Claude Code CLI:** near-clone of the Cursor adapter (`claude -p --output-format
  stream-json`, session resume via `--resume`).
- **Local (llamacpp):** different in kind — requires building the agent loop itself
  (tool-calling harness, file read/write tools, iteration limits). Own milestone; the
  adapter contract is designed so it slots in without orchestrator changes.

## 7. Concurrency & recovery

- One in-flight run per project in v1 (per-project worker). The schema supports parallel
  tasks via `blocked_by`; lifting the restriction is a scheduler change only.
- WAL mode; both daemon and CLI write, but writes are short and serialized, so a
  `busy_timeout` (~5s) with retry absorbs the occasional `SQLITE_BUSY`. State changes are
  transactional and exactly-once (§2.4).
- **Single daemon** via advisory `flock` on `daemon.lock`; a second instance exits. Each
  daemon start mints a fresh `epoch` (a uuid), recorded in the lockfile and stamped on every
  `runs` row it launches.
- **Process supervision.** Each run is spawned in its own process group (`setsid`); the run
  row records `epoch`, `pid`, `pgid`, and `proc_start`. Graceful `daemon stop` kills each live
  run's process group — daemon down means work stops.
- **Restart reconciliation (correctness).** Any `running` run whose `epoch` isn't the current
  one belonged to a dead daemon: if it left a parseable transcript/result it's processed
  normally, otherwise it's marked `error` and the task re-queued or sent to the inbox per stage
  config. This only touches DB rows and never signals a process, so it's safe across reboots
  and pid churn.
- **Orphan cleanup (tidiness).** A hard crash can leave a backend orphaned. We never kill by a
  bare recorded pid — pids get reused. Instead, on startup the daemon checks each stale-epoch
  pid against its recorded `proc_start`: a match is positively still our backend, a mismatch is
  an unrelated process we leave alone. Confirmed matches are shown to the user for approval
  before killing (`these look like orphaned agent processes, kill them? [y/N]`). Orphans can't
  corrupt state regardless (§2.3), so this is cleanup, not correctness.

## 8. Milestones

1. **M1 — Skeleton:** schema + migrations, `teams init/project/task` CRUD with `$EDITOR`
   round-trip, `teams status` from DB. No daemon.
2. **M2 — Single-stage execution:** daemon loop, Cursor CLI adapter spike + implementation
   (`agent -p` in a real repo workspace), one-stage pipeline (develop only),
   `runs`/`events`, `teams log`. This is the first end-to-end proof that the daemon can
   orchestrate Cursor agents against a checkout.
3. **M3 — Pipeline + gates:** full stage machine, declared human gates, inbox,
   approve/reject with feedback routing, qa `on_fail`/`max_loops`, stale-version handling.
4. **M4 — PM layer:** stateless PM invocations, `teams chat`/`tell`, plan revisions as
   gated artifacts, standup generation + scheduling.
5. **M5 — Polish & second backend:** Claude Code adapter, notification adapters
   (webhook/Matrix), TUI exploration, export/import groundwork.

## 9. Open questions

- Whether QA runs should get an isolated checkout (worktree per run) vs. operating on the
  developer's working tree — worktrees make dev↔QA bounces cleaner but complicate the
  “artifacts live in the repo” convention.
- Git integration depth: should the orchestrator commit per stage (audit trail, easy
  rollback of a rejected stage) or leave VCS entirely to the agents/human?
- Model routing per role (cheap model for QA triage, strong model for design) — supported
  by `model_hint` in the roster but semantics per backend are TBD.
- ~~How much of the PM's plan-revision output format to constrain.~~ **Resolved:** the applied
  patch is a strict structured task-patch (`approvals.payload_json`) the daemon applies
  mechanically; only the human-facing summary is free-form. Auto-detecting actionable free-form
  messages remains a later layer.
