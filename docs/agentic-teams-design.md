# Design: Agentic Teams Subsystem

**Status:** Draft
**Scope:** New `teams` subcommand group + background daemon for the existing CLI

## 1. Overview

The `teams` subsystem lets a user organize work into projects, staff each project with a
team of role-based agents (PM, designer, developer, QA, reviewer), and run those agents
through a **user-defined**, gated pipeline that keeps a human in the loop at the points
that actually need one. Work is organized around **epics**: the human describes a goal to
the PM ("add a 2D grid and graphics to the game"), the PM reads the repo and grooms the
goal into a task breakdown — asking the human clarifying questions about design forks and
unknowns — and once the human approves the scope, the agents execute the whole task set in
the background. The finished epic comes back as **one complete local feature branch** —
ready for the human to review, run, and push/PR themselves. The human interacts primarily
through the PM agent — describing goals, answering questions, approving scope, reviewing
the finished branch — while a persistent **daemon owns orchestration** (scheduling, gates,
state) and **spawns Cursor CLI agents** (`agent` / `cursor-agent`) headlessly inside each
project's repository to do the actual work. Other backends (Claude Code CLI, a local
llamacpp harness) plug in later behind the same adapter contract; Cursor CLI is the v1
path and the reference implementation.

### Goals

- Project/epic/task management with a role-based agent team per project, fully configurable as data.
- **Human effort scales with goals, not tasks.** Scope approval once per epic, review once
  per epic (a single finished branch); everything between — grooming, design → develop → QA
  loops with bounded retries, task-level integration — is automated, escalating only on
  genuine unknowns. An epic groomed into 100 tasks still costs the human one approval and
  one review.
- User-defined pipelines: the stage list, roles, prompts, and gates are project data, so a
  team can encode bespoke workflows (multiple design passes, a security stage) without
  source changes.
- **Cursor CLI as the agent runtime:** the daemon runs `agent -p` (headless) against a
  project's workspace path so each role executes as a real Cursor agent in that repo.
- **Concurrent multi-agent execution in v1:** many Cursor CLI processes can run at once.
  Each task advances its own pipeline independently. Per-stage (and optional global)
  concurrency numbers are **ceilings, not targets** — e.g. design may run up to 4 at once,
  but idle slots stay empty unless there is real ready work. Nothing invents tasks to fill
  capacity.
- **Worktree isolation for concurrent Cursor runs:** multiple `agent -p` processes must not
  share one working tree (they will overwrite each other's edits). Each task gets its own
  git worktree for its lifetime; Cursor is bound to that path, not the project's primary
  checkout. Task branches merge into an epic branch, handed over as one finished local
  branch (§7.3).
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

## 2. Architecture

Three components share one SQLite database:

```
┌─────────────┐   writes rows    ┌──────────────┐   spawns N   ┌──────────────────┐
│ CLI          │ ───────────────▶ │ SQLite (WAL) │ ◀────────── │ Daemon            │
│ (subcommands)│ ◀─────────────── │ tasks/events │             │ slot scheduler    │
└─────────────┘   reads status    │ approvals    │             └────────┬─────────┘
                                  │ messages     │                      │ 1 worktree +
                                  └──────────────┘                      │ 1 child / run
                                                                        ▼
                                                              ┌──────────────────┐
                                                              │ Cursor CLI × N   │
                                                              │ agent -p         │
                                                              │ --workspace wt   │
                                                              └──────────────────┘
```

**Division of labor:** the daemon never runs an agent loop itself. It selects ready work
up to the project's concurrency ceilings, prepares an isolated git worktree per run (§7.2),
composes each prompt, invokes Cursor CLI against that worktree (not the primary checkout),
waits for completions (or timeouts) across in-flight children, parses each structured
result, integrates successful tree changes into the task's epic branch (§7.3), and commits
that task's state transition. Cursor CLI owns tool use, file edits, and model calls inside its
worktree; we own the queue, gates, slot accounting, worktree lifecycle, and audit trail.

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

Main loop (single-threaded scheduler, one OS process / Cursor CLI child per in-flight run;
children are reaped with non-blocking `waitpid` each tick, so nothing ever blocks the
scheduler):

1. Poll for unprocessed `messages` → invoke PM agent. An `ask` message yields a read-only
   reply (`from_pm`); a `tell` message either opens/advances epic grooming (§4.2) or yields
   a `plan_revision` approval (§5.3) — never a direct task mutation. The reply/approval and `messages.processed_at` commit in one
   transaction (§2.4). PM invocations share a small dedicated concurrency budget so they
   don't starve task workers (default 1).
2. Poll for resolved-but-unapplied `approvals` → apply state transitions (advance stage, or
   route rejection feedback back to the PM as a re-plan trigger). The transition and
   `approvals.applied_at` commit together, so an approval can never be applied twice (§2.4).
3. Dispatch ready work into free stage slots (never pad): for each project, count
   in-flight runs by stage, compare against that project's `concurrency` ceilings
   (§4.1 / §7), and dispatch only tasks that are actually ready and still fit. Empty
   slots are normal — no work means no spawn. Each claimed task gets a worktree (§7.2),
   a `runs` row, and a Cursor CLI child bound to that worktree.
4. On run completion: parse structured result (repo-side edits landed in the task's
   worktree), integrate successful changes per §7.3, orchestrator performs the state
   transition, appends events, opens gates/inbox items as the pipeline dictates. The
   `runs.outcome` row and the resulting task transition commit in one transaction (§2.4).
   Sibling tasks are unaffected — each task's stage machine is independent.
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
- finished run → `runs.outcome` **+** task transition, one txn. The git side effects that
  precede it (rebase, promote) are idempotent (§7.3), so replaying an interrupted
  completion after a crash re-merges as a no-op before committing the same transition.

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

CREATE TABLE epics (
  id            INTEGER PRIMARY KEY,     -- displayed as E-<id>
  project_id    INTEGER NOT NULL REFERENCES projects(id),
  title         TEXT NOT NULL,
  goal          TEXT NOT NULL,           -- markdown; the human's ask + PM's refined goal statement
  status        TEXT NOT NULL,           -- grooming|awaiting_approval|executing|awaiting_review|done|abandoned
  branch        TEXT,                    -- epic integration branch (teams/E-<id>); cut at scope approval
  base_branch   TEXT NOT NULL,           -- eventual merge target; project default branch unless overridden
  version       INTEGER NOT NULL DEFAULT 1, -- bumped on scope edits
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE tasks (
  id            INTEGER PRIMARY KEY,     -- displayed as T-<id>
  project_id    INTEGER NOT NULL REFERENCES projects(id),
  epic_id       INTEGER REFERENCES epics(id), -- NULL = standalone task (per-task gates apply)
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,           -- markdown; the agent-facing spec
  status        TEXT NOT NULL,           -- draft|backlog|ready|running|waiting_human|blocked|done|failed
                                         -- draft = groomed but epic not yet approved; never dispatched
  stage         TEXT,                    -- current pipeline stage name
  role          TEXT,                    -- role assigned for the current stage
  priority      INTEGER NOT NULL DEFAULT 0, -- higher dispatches first; ties broken by age
  blocked_by    TEXT,                    -- JSON array of task ids
  gate          TEXT,                    -- pending gate kind, if any
  version       INTEGER NOT NULL DEFAULT 1, -- bumped on every human/PM edit
  loop_count    INTEGER NOT NULL DEFAULT 0, -- qa rejections in the current dev↔qa loop
  branch        TEXT,                    -- sticky task branch (teams/E-<epic>/T-<id>); null until first mutating run
  worktree_path TEXT,                    -- task-lifetime worktree (§7.2); null until first mutating run
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
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
  worktree_path TEXT,                    -- audit copy of tasks.worktree_path at dispatch
  branch        TEXT,                    -- branch checked out in that worktree
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
  task_id       INTEGER,                 -- NULL for epic/project-level items
  epic_id       INTEGER,                 -- set for epic-level items (epic_approval|question|epic_review)
  project_id    INTEGER NOT NULL,
  kind          TEXT NOT NULL,           -- stage_gate|escalation|loop_exhausted|budget_exhausted|
                                         -- plan_revision|epic_approval|question|epic_review
  summary       TEXT NOT NULL,           -- what the human is being asked to approve
  requested_at  TEXT NOT NULL,
  resolved_at   TEXT,
  applied_at    TEXT,                    -- transition committed under this stamp; guards reprocessing
  decision      TEXT,                    -- approved|rejected|answered
  human_note    TEXT,                    -- routed back to the PM/agent as feedback (the answer, for questions)
  payload_json  TEXT                     -- structured task-patch (plan_revision/epic_approval), question list, ...
);

-- At most one open approval per task and per epic; gates and grooming are sequential,
-- and this makes double-posting a bug the DB catches.
CREATE UNIQUE INDEX approvals_open_task ON approvals(task_id)
  WHERE resolved_at IS NULL AND task_id IS NOT NULL;
CREATE UNIQUE INDEX approvals_open_epic ON approvals(epic_id)
  WHERE resolved_at IS NULL AND epic_id IS NOT NULL;

CREATE TABLE messages (
  id            INTEGER PRIMARY KEY,     -- human <-> PM channel
  project_id    INTEGER NOT NULL,
  epic_id       INTEGER REFERENCES epics(id), -- set = part of that epic's grooming/review thread
  direction     TEXT NOT NULL,           -- to_pm|from_pm
  body          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  processed_at  TEXT                     -- NULL = daemon hasn't handled it yet
);
```

Large artifacts stay on disk with paths in the DB: transcripts under the daemon's data
directory, and agent-produced deliverables (design docs, QA reports) inside the **task
worktree** — e.g. `docs/design/T-12-auth-flow.md` — then promoted onto the epic branch
when the task promotes (§7.3). Downstream stages read them as ordinary workspace files on
the task branch; they travel with the repo once merged.

### 3.1 Mid-flight edit detection

Every human or PM edit to a task bumps `tasks.version`. Dispatch snapshots the version
into `runs.task_version`. At run completion, if `tasks.version` no longer matches, the
run's outcome is set to `stale` and an inbox item is opened instead of auto-applying the
transition — the human decides whether the result still stands. PM grooming edits inside
an epic bump versions the same way; the PM is prompted to prefer editing tasks that aren't
`running`, but if it must touch one, staleness is detected rather than prevented.

## 4. Core concepts

### 4.1 Project config

A team is data, not code. Everything lives in `projects.config_json` (authored as YAML via
`teams project edit`, stored as JSON):

```yaml
roster:
  pm:        { backend: cursor, model_hint: null, prompt: prompts/pm.md,
               groom_prompt: prompts/pm-groom.md }   # planning-mode persona (§4.2 step 1)
  designer:  { backend: cursor, prompt: prompts/designer.md }
  developer: { backend: cursor, prompt: prompts/developer.md }
  qa:        { backend: cursor, prompt: prompts/qa.md }

pipeline:
  - stage: design
    role: designer
    gate: epic             # auto inside an approved epic; human gate for standalone tasks
  - stage: develop
    role: developer
    gate: auto
  - stage: qa
    role: qa
    on_fail: develop       # bounce back with feedback
    max_loops: 3           # then escalate to human
  - stage: review
    gate: epic             # epic tasks: covered by the epic review; standalone tasks gate here

# Gate values — auto: never pause. human: always pause per task, even inside an epic
# (for pipelines that want it). epic: pause only for standalone tasks; epic tasks are
# covered by scope approval up front and the epic branch review at the end. The pipeline is data:
# bespoke stages (multiple design passes, a security pass) are just more entries.

# Upper bounds on simultaneous Cursor CLI agents for this project — not fill targets.
# Caps are per pipeline stage (matched by stage name). A free slot is used only when a
# real task is ready for that stage; otherwise it stays idle. Tasks beyond a cap stay
# `ready` until a slot frees. Each task still follows the pipeline on its own —
# T-12 can be bouncing develop↔qa while T-15 is still in design.
concurrency:
  max_total: 10            # ceiling across all stages (optional; omit = sum of stage ceilings)
  per_stage:
    design: 4              # "up to 4", not "always keep 4 busy"
    develop: 7
    qa: 3
    # stages omitted here default to 1
  pm: 1                    # ask/tell/chat/standup invocations

budgets:
  max_runs_per_task: 20    # total runs across all stages before forced escalation;
                           # coarse safety net above per-loop max_loops

epic_checks: null          # optional command run on the epic branch after promotes (§7.3),
                           # e.g. "make test"; failure opens an inbox item

standups:
  - schedule: "0 9 * * *"
    channel: terminal      # v1: terminal + file; later: webhook|matrix

notifications:
  inbox: [terminal]        # later: webhook, matrix
```

Adding a role (say, a security reviewer) means adding a roster entry and a pipeline stage —
no source changes. Tuning parallelism is the same kind of edit: bump `concurrency.per_stage`
and the scheduler honors the new ceiling on the next poll (still only when work exists).

### 4.2 Epics: goal → groomed plan → one finished branch

The **epic is the unit of human interaction; the task is the unit of execution.** Humans
deal in goals; agents deal in the 100 tasks a goal grooms into.

1. **Intake.** The human describes a goal to the PM (`teams tell "add a 2D grid and
   graphics to the game"`, or via `chat`). The PM creates an epic in `grooming` and starts
   from a dedicated grooming prompt: *you are planning with a human right now — explore
   the repo, think about implementation approaches and their implications for this
   project, hunt edge cases, surface design forks, ask rather than assume.*
2. **Groom (conversational loop).** Grooming is a conversation, not a form. Each turn: the
   PM digests the human's latest input, explores the repo as needed (read-only runs under
   the `pm` ceiling — "let me look into that and get back to you" is a legal move), and
   comes back with thoughts, implications, and questions; the human answers, redirects, or
   pushes back; repeat. Expect a real working session (30–60 minutes for a meaty epic).
   Mechanics that make it work:
   - **Transcript in the DB.** Grooming messages are threaded to the epic
     (`messages.epic_id`); every PM turn is a cold invocation that receives the running
     transcript plus its accumulated repo findings, so the PM stays stateless (§4.4) and
     the session is resumable — walk away mid-grooming and pick it up tomorrow, or answer
     queued questions from the inbox instead of live chat.
   - **Draft tasks materialize live.** As understanding firms up, the PM writes tasks with
     status `draft` under the epic. The human watches the board take shape during the
     conversation and can `$EDITOR`-edit or delete drafts mid-conversation; edits are just
     more grooming input.
   The loop ends one of two ways: **(a)** the PM judges marginal questions no longer worth
   the human's time and proposes finalization, or **(b)** the human says "you have enough
   — go with what you have," and the PM finalizes immediately, stating its remaining
   assumptions explicitly. This conversation is where the human spends their ideation and
   design-alignment budget — cheap now, expensive after 100 tasks execute against a
   misunderstanding.
3. **Scope approval.** The PM posts an `epic_approval`: refined goal, full task list with
   dependencies, and any assumptions it's proceeding on. This is the "did the PM
   understand me" checkpoint — usually a formality by now, approved inline in chat with a
   keystroke. Approve → `draft` tasks flip to `backlog`/`ready`, the epic branch is cut
   from `base_branch`, execution starts, and the human walks away. Reject with a note →
   back to grooming.
4. **Execute.** Tasks flow the pipeline concurrently under the normal ceilings. Inside an
   approved epic the PM grooms **without gates**: it may split, add, reorder, edit, and
   re-prioritize its epic's tasks as execution teaches it things. Still gated: changes to
   the epic's goal/scope itself (a fresh `epic_approval`), human directives
   (`plan_revision`, §5.3 — confirms understanding), and per-task escalations
   (`needs_human`, loop/budget exhaustion, stale results). Human effort scales with goals
   plus genuine unknowns — not with task count.
5. **Review.** When every task is `done`, the daemon parks the epic in `awaiting_review`
   with a PM-composed handoff summary (§7.3). What the agents deliver is a **local epic
   branch that is "complete" by their standards** — every task groomed, built, QA'd, and
   integrated. The human reviews it on their own terms: read the diff, run the tests, boot
   the app, then push and open the PR themselves (`gh` or otherwise) or merge locally.
   Review notes short of acceptance go back to the PM and become new tasks on the same
   branch. `teams epic approve` marks the epic `done` and prunes its task branches — the
   daemon never pushes.

Standalone tasks (no epic) remain supported with per-task gates — for one-off chores that
don't warrant grooming.

### 4.3 Task lifecycle (independent per task)

Every task is its own state machine. The pipeline definition is shared project config; the
*instance* of that machine (current `stage`, `loop_count`, pending `gate`, run history) is
per-task. Tasks do not wait on each other's stage transitions. T-12 can bounce
`develop → qa → develop` while T-15 is still gated on design approval and T-18 is mid-develop —
the scheduler only cares whether each task is ready and whether a free slot exists for its
current stage (§7).

```
backlog → ready → running(stage) → [gate?] → next stage → … → done
                        │
                        ├─ qa fail ──▶ develop (bounded by max_loops)
                        ├─ needs_human / loop exhausted ──▶ waiting_human (inbox item)
                        └─ stale (task edited mid-flight) ──▶ waiting_human
```

Gates fire from three triggers: **declared** (pipeline config, per the `gate:` policy in
§4.1 — `epic`-valued gates don't pause epic tasks), **agent-initiated** (`<needs_human>`
sentinel in output), and **bound exhaustion** (`max_loops` or `budgets.max_runs_per_task`
hit). All three
produce the same artifact — an `approvals` row surfaced in the inbox — so the human
experience is uniform regardless of why the system paused. A gated task frees its stage
slot immediately when it enters `waiting_human`; siblings keep running.

`tasks.loop_count` increments on each QA rejection (one `qa fail → develop` bounce). When it
reaches the stage's `max_loops` the task escalates to `waiting_human` instead of bouncing
again; it resets when the task advances past qa. So `max_loops: 3` reads as "QA may reject
three times, then escalate."

### 4.4 PM agent is stateless

The PM never answers from its own memory. Every PM invocation is cold and receives: the
project config, epic summaries, a task-board summary, recent `events` rows, relevant open
approvals, and the human's message. During grooming it additionally receives the epic's
message thread and draft tasks — a long grooming *conversation* is many cold *invocations*
over an ever-growing DB-resident transcript, which is what makes grooming resumable across
sittings and crashes. (Backend session resume can make consecutive turns cheaper, as an
optimization — never a correctness dependency.) Status answers, standup digests, and plan revisions are all derived
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
teams epic list|show|approve|abandon # E-7; approve resolves scope approvals & final review
teams task add|edit|list|show       # edit = $EDITOR round-trip (markdown/YAML), bumps version
teams daemon [start|stop|status]
teams status [PROJECT]              # board overview, derived from DB
teams inbox                         # pending approvals + escalations
teams approve T-12 [-m "note"]      # note is routed back as feedback
teams reject  T-12 -m "reason"
teams chat [PROJECT]                # interactive PM session; can approve drafted revisions inline
teams ask  "question"               # one-shot read-only query to the PM
teams tell "directive text"         # one-shot: new goal → epic grooming; correction → gated plan revision
teams standup [--now | --schedule "0 9 * * *"]
teams log T-12                      # events + run transcripts for a task
```

### 5.3 Directives and plan revisions

Read-only questions (`teams ask`, or a question in `chat`) get a direct `from_pm` reply and
change nothing. Directives never mutate tasks directly; they split by intent:

- **New goal** (“I want to add X”) → the PM opens an epic and starts grooming (§4.2). The
  gate is the eventual `epic_approval`.
- **Correction to existing work** (“I'd rather you did xyz”, “split T-40”, “you missed a
  story”) → the PM drafts a **plan revision**: a structured task-patch (creates/updates) in
  `approvals.payload_json` plus a free-form summary of what changes and why, posted as a
  gated `plan_revision`. The human approves; only then does the daemon apply the patch
  mechanically.

The asymmetry is deliberate: *human words* always round-trip through an approval — the
“double-check the PM understood before developers build” loop — while the PM's *own*
grooming inside an already-approved epic applies ungated (§4.2). In an interactive `chat`
session the same artifacts can be approved inline instead of via the inbox — the gate is
preserved either way. Auto-detecting whether a free-form message is a goal vs. a correction
is a later layer; v1 keys off explicit phrasing/flags in ask/tell (or chat).

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
CLI against an **isolated worktree** for that run (§7.2), not against
`projects.workspace_path` directly (except optional read-oriented PM invocations). Every
roster role that sets `backend: cursor` goes through this adapter (including the PM when
answering `ask`/`tell`/`chat`).

Typical invocation shape (exact flags locked by the M2 spike):

```bash
agent -p --output-format json \
  --workspace /path/to/worktree/for/this/run \
  [--model <model_hint>] \
  [--resume <session_id>] \
  "<composed prompt>"
```

(`agent` and `cursor-agent` are treated as the same CLI; resolve via `PATH` / config.)

Requirements on the adapter:

- **Worktree-scoped.** Always bind to the run's `worktree_path` (`--workspace` and/or CWD).
  Never point concurrent task agents at the primary checkout — two `agent -p` processes in
  the same working tree will clobber each other.
- **Fully non-interactive.** Print mode only; no TTY prompts. Force auto-approval /
  yolo-equivalent flags as needed so the daemon never blocks on Cursor's internal
  confirmations. A hang past the wall-clock timeout is `outcome=error`.
- **Structured I/O.** Prefer `--output-format json` (or `stream-json` if we need live
  progress). Parse the final result; tee raw stdout/stderr to `transcript_path`.
- **Auth is the operator's problem.** Cursor CLI must already be logged in on the machine
  (`agent login` / API key). The daemon does not manage Cursor accounts.
- **Process group.** Spawn with `setsid` so `daemon stop` can kill the whole Cursor
  subprocess tree (§7.4).

Spike items before freezing the contract (do these in M2):

1. Session resume (`--resume` / `--continue`) ergonomics — whether `resume_token` is real
   in v1 or a no-op cold-start. Task-lifetime worktrees (§7.2) keep the workspace path
   stable across dev↔qa bounces, which should make resume viable; verify.
2. Exact non-interactive / auto-approve flags for the current CLI release.
3. Whether `--workspace` alone is enough vs. also setting CWD (must be the worktree either way).
4. Model selection via `--model` from roster `model_hint`.
5. Worktree create/remove + branch naming against a real multi-agent conflict scenario.

Every run gets a wall-clock timeout and its raw output teed to `transcript_path`.

### 6.2 Later backends

- **Claude Code CLI:** near-clone of the Cursor adapter (`claude -p --output-format
  stream-json`, session resume via `--resume`).
- **Local (llamacpp):** different in kind — requires building the agent loop itself
  (tool-calling harness, file read/write tools, iteration limits). Own milestone; the
  adapter contract is designed so it slots in without orchestrator changes.

## 7. Concurrency & recovery

### 7.1 Parallel Cursor agents (v1)

v1 schedules many in-flight Cursor CLI runs at once. The unit of parallelism is a **task at
a stage**: each dispatched run is one `agent -p` process. Caps come from
`projects.config_json.concurrency` (§4.1).

**Ceilings, not targets.** `design: 4` means "at most four design agents," not "keep four
design agents busy." The scheduler (and the PM when planning) never invent work to consume
idle capacity. A slot is claimed only when a concrete task is already ready for that stage;
otherwise the slot stays empty. Under-subscription is the steady state whenever the backlog
is thin.

| Cap | Meaning |
| --- | --- |
| `per_stage.<name>` | Max simultaneous runs whose `runs.stage` equals that pipeline stage. |
| `max_total` | Optional project-wide ceiling across all stages. |
| `pm` | Max concurrent PM invocations (ask/tell/chat/standup). Default 1. |

Dispatch algorithm each poll tick (per project):

1. Load in-flight counts: `COUNT(*) FROM runs WHERE ended_at IS NULL GROUP BY stage`.
2. Select candidate tasks that already exist and are actionable: status allows work
   (`ready`, or continuing after an auto gate), `blocked_by` satisfied, no open unapplied
   approval for this task, and total run count below `budgets.max_runs_per_task` (at the
   bound, open a `budget_exhausted` inbox item instead of dispatching). No candidates ⇒
   stop; do not fabricate tasks.
3. Order candidates stably: `priority` descending, then older `ready` first.
4. For each candidate, if `in_flight[stage] < per_stage[stage]` and
   `sum(in_flight) < max_total`, claim it — **in this order**: insert the `runs` row and
   bump the task to `running` (one txn), then ensure task branch + worktree (§7.2), then
   spawn Cursor CLI in a fresh process group bound to that worktree. The DB row precedes
   the external effects, so a crash at any point in the claim leaves a record that epoch
   reconciliation (§7.4) can resolve; there is no window where a process or worktree
   exists that the DB doesn't know about. Stop when candidates are exhausted or no slots
   remain — whichever comes first.

A task that finishes or parks on a gate releases its slot in the same transaction as the
state change (§2.4), so the next tick can offer that capacity to another ready task —
possibly a different feature at a different point in the pipeline. If nothing is ready,
capacity sits unused.

`blocked_by` is the only cross-task coupling. Without it, features move through
design ↔ develop ↔ qa on independent clocks. One task looping on QA does not stall
another task's design gate or develop slot beyond the shared stage ceilings.

### 7.2 Workspace isolation (git worktrees)

Concurrent `agent -p` processes in the same working tree will step on each other (shared
index, unstaged files, Cursor workspace state). **v1 isolates every task in its own git
worktree for the task's lifetime.** Parallelism without isolation is not supported.

The unit is the **task, not the run**: git refuses to check out one branch in two
worktrees, a task's branch is sticky across stages, and a task has at most one in-flight
run — so one long-lived worktree per task is both what git wants and what session resume
needs. The workspace path stays stable across dev↔qa bounces, keeping `resume_token`
meaningful, and stage transitions stop paying worktree create/remove churn.

Lifecycle (orchestrator-owned; adapters only receive a workspace path):

1. **Branch.** On a task's first mutating dispatch, create sticky branch
   `teams/E-<epic>/T-<id>` (standalone tasks: `teams/T-<id>`, stored on `tasks.branch`)
   from the epic branch tip (standalone: the project integration tip). All stages of the
   task — design → develop → qa — share this one line of history.
2. **Worktree.** `git worktree add <path> <branch>` into a daemon-managed directory
   outside the primary checkout: `~/.local/share/<cli>/worktrees/<project-id>/T-<id>/`.
   Recorded on `tasks.worktree_path`; each dispatch snapshots it to `runs.worktree_path`
   for audit. Cursor's `--workspace` is this path only. The worktree survives daemon
   restarts — it is task state, not run state.
3. **Execute.** The agent edits freely inside the worktree. Sibling tasks never share a
   tree.
4. **Integrate.** Rebase and promote per §7.3. Failed / stale / error runs never promote;
   the worktree stays put for the next attempt.
5. **Cleanup.** `git worktree remove` when the task reaches `done` or is abandoned (force
   if a process was killed). At daemon startup, directories under the managed worktree
   parent that no live task references are swept — this covers crashes at any point in
   the dispatch sequence (§7.1). Task branches persist until the epic merges, then are
   pruned per project policy.

PM invocations that are read-only may use `workspace_path` directly (single-threaded under
the `pm` ceiling). If a PM path ever needs to edit the repo, it takes a worktree too.

Requirement on `workspace_path`: it must be a git repo (or the project opts into a later
non-git isolation mode — not v1). Non-git projects cannot use parallel Cursor agents until
that exists.

### 7.3 Integration, conflicts, and the epic handoff

Git topology: `base_branch` ◀— human push/PR/merge — epic branch `teams/E-<n>` ◀— promote —
task branches `teams/E-<n>/T-<m>` ◀— agent worktrees. The human reviews once, at the
finished epic branch. Standalone tasks skip the middle layer: their branch rebases against
and promotes directly to the project integration branch, gated per-task by the pipeline.

**Never merge in the primary checkout.** The human may have `workspace_path` dirty at any
moment. All daemon-side git integration happens in a dedicated daemon-owned worktree of
the epic branch; `workspace_path` is only ever read (branch tips, config).

**Rebase before final verification.** With N tasks landing on one epic branch,
"task branch diverged from epic tip" is the common case, not an edge — and a task that
passes QA in isolation can still break siblings after merge, because a clean git merge is
not a semantic guarantee. So when a task enters its final verifying stage (`qa`), the
orchestrator first rebases the task branch onto the current epic tip. QA then validates
the task *against the epic as it now exists*, and each successive task's QA incrementally
re-verifies the combination. A rebase conflict is treated exactly like a QA failure:
bounce to `develop` with the conflict context in the prompt (counts against `max_loops`),
escalating to the inbox on exhaustion. No bespoke conflict machinery — it reuses the
dev↔qa loop.

**Promote.** On final success the orchestrator merges the task branch into the epic
branch. Promotes are serialized by the single-threaded scheduler and near-fast-forward
after the rebase; if the epic tip moved anyway (a sibling promoted while QA ran), attempt
the merge, and on conflict take the same bounce-to-develop path. Promote is idempotent —
replaying a crashed completion re-merges an already-merged branch as a no-op — which is
what lets the `runs.outcome` transaction safely follow the git side effect (§2.4).

**Optional epic checks.** `epic_checks` in config (§4.1) names a command the daemon runs
on the epic branch after each promote (or only at epic completion, configurable); a
failure opens an inbox item rather than letting integration drift accumulate silently.

**Handoff, not PR.** The daemon's git responsibility ends at the epic branch. When the
last task is `done`, it composes a handoff summary via the PM (what was built, task list,
notable decisions, how to verify — PR-description-ready) and parks the epic in
`awaiting_review`. It never pushes and never opens PRs: no remote credentials, no forge
coupling, nothing leaves the machine. The human reviews the local branch — diff it, run
the tests, boot the app — then pushes and PRs with their own tooling (`gh` on their
machine, or a plain `git push` + web UI), or merges locally. Feedback short of acceptance
routes to the PM as new tasks on the same branch; `teams epic approve` marks the epic
`done` and prunes task branches.

### 7.4 Recovery

- WAL mode; both daemon and CLI write, but writes are short and serialized, so a
  `busy_timeout` (~5s) with retry absorbs the occasional `SQLITE_BUSY`. State changes are
  transactional and exactly-once (§2.4).
- **Single daemon** via advisory `flock` on `daemon.lock`; a second instance exits. Each
  daemon start mints a fresh `epoch` (a uuid), recorded in the lockfile and stamped on every
  `runs` row it launches.
- **Process supervision.** Each run is spawned in its own process group (`setsid`); the run
  row records `epoch`, `pid`, `pgid`, and `proc_start`. Graceful `daemon stop` kills **every**
  live run's process group — daemon down means all parallel work stops. Task worktrees are
  left in place: they are task state (§7.2), and work resumes in them on restart.
- **Restart reconciliation (correctness).** Any `running` run whose `epoch` isn't the current
  one belonged to a dead daemon: if it left a parseable transcript/result it's processed
  normally, otherwise it's marked `error` and the task re-queued or sent to the inbox per stage
  config. Many stale runs may exist after a crash; each is reconciled independently. This only
  touches DB rows and never signals a process, so it's safe across reboots and pid churn.
  Worktree dirs referenced by no live task are removed after reconciliation (or after the
  user confirms orphan-PID cleanup when a live process still holds the tree); a live task
  keeps its worktree and simply runs again in it.
- **Orphan cleanup (tidiness).** A hard crash can leave several backend orphans. We never kill
  by a bare recorded pid — pids get reused. Instead, on startup the daemon checks each
  stale-epoch pid against its recorded `proc_start`: a match is positively still our backend, a
  mismatch is an unrelated process we leave alone. Confirmed matches are shown to the user for
  approval before killing (`these look like orphaned agent processes, kill them? [y/N]`).
  Orphans can't corrupt task state regardless (§2.3), so this is cleanup, not correctness.

## 8. Milestones

1. **M1 — Skeleton:** schema + migrations, `teams init/project/epic/task` CRUD with `$EDITOR`
   round-trip, `teams status` from DB (including in-flight / queued-by-stage counts). No daemon.
2. **M2 — Concurrent single-stage execution:** daemon loop with stage-slot scheduler,
   git worktree create/bind/remove, Cursor CLI adapter spike + implementation (`agent -p`
   per worktree), one-stage pipeline (develop only), `concurrency.per_stage` / `max_total`
   honored as ceilings, multiple simultaneous Cursor children without shared-tree conflicts,
   `runs`/`events`, `teams log`. First end-to-end proof that the daemon can orchestrate
   parallel isolated Cursor agents.
3. **M3 — Pipeline + integration:** full per-task stage machine on task-lifetime worktrees,
   epic branch topology (`base ← epic ← task`), rebase-before-QA + promote with
   conflict-as-QA-failure bouncing (§7.3), declared/`epic` gates, inbox, approve/reject with
   feedback routing, qa `on_fail`/`max_loops` + `budgets.max_runs_per_task`, stale-version
   handling, epic handoff (`awaiting_review` + finished branch) at completion. Independent
   task FSMs under the same concurrency ceilings.
4. **M4 — PM layer:** stateless PM invocations, epic intake + conversational grooming loop
   (epic-threaded transcript, draft tasks, `question` batches, `epic_approval`,
   "go with what you have" finalization, ungated in-epic grooming), `teams chat`/`tell`,
   plan revisions as gated artifacts, PM-composed handoff summaries, standup generation +
   scheduling (PM concurrency budget).
5. **M5 — Polish & second backend:** Claude Code adapter, notification adapters
   (webhook/Matrix), TUI exploration, export/import groundwork.

## 9. Open questions

- Grooming transcript growth: a long conversation eventually outgrows a single PM
  invocation's context. Proposed: PM maintains a rolling "grooming notes" summary on the
  epic (updated each turn, stored in DB) and old transcript turns drop out of the prompt;
  confirm shape in M4.
- `epic_checks` default cadence — after every promote (catches drift early, costs runtime
  per promote) vs. only at epic completion. Proposed default: at completion only.
- Grooming-vs-scope boundary precision: task-patches within an epic are ungated, epic
  `goal` edits are gated. Should the daemon try to detect "PM added a task clearly outside
  the approved goal" in v1, or is the epic branch review the backstop? Proposed: review is
  the backstop; detection is a later layer.
- Git integration depth beyond promote: should the orchestrator always create the stage
  commit, or may the agent commit and the orchestrator only merge?
- Model routing per role (cheap model for QA triage, strong model for design) — supported
  by `model_hint` in the roster but semantics per backend are TBD.
- Default concurrency values when `concurrency` is omitted — proposed: `per_stage` default
  1 for every stage, `max_total` unset, `pm: 1`. Confirm before M2 freezes config schema.
- ~~How much of the PM's plan-revision output format to constrain.~~ **Resolved:** the applied
  patch is a strict structured task-patch (`approvals.payload_json`) the daemon applies
  mechanically; only the human-facing summary is free-form. Auto-detecting actionable free-form
  messages remains a later layer.
- ~~Parallel tasks in v1.~~ **Resolved:** in scope. Per-stage + optional `max_total` are
  ceilings (not fill targets); each task owns an independent pipeline instance; idle
  slots stay empty when there is no ready work.
- ~~Shared workspace for concurrent agents.~~ **Resolved:** not allowed. Each task gets a
  task-lifetime git worktree (§7.2); Cursor binds to that path. Primary `workspace_path`
  is never written by the daemon.
- ~~Merge / promote policy + conflict handling.~~ **Resolved (§7.3):** rebase task branch
  onto epic tip before final QA; promote merges into the epic branch, serialized; rebase or
  merge conflicts bounce to `develop` like a QA failure, bounded by `max_loops`; handoff is
  one finished local branch per epic — the daemon never pushes or opens PRs; the human PRs
  with their own tooling.
- ~~Worktree parent directory location.~~ **Resolved:** XDG data dir
  (`~/.local/share/<cli>/worktrees/<project>/T-<id>/`) — outside the repo so it can never
  be committed, and per-task now that worktrees are task-lifetime.
