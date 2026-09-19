# Design: Agentic Teams Subsystem

**Status:** Draft. Supersedes the pipeline design (last revision `ec4334e`, in git history).
**Scope:** The `teams` daemon, its client protocol and TUI, and `myai cloud bootstrap`.

## 1. Overview

The `teams` subsystem is a harness for long-lived bots. A bot keeps an identity, gets
better at its job, holds a conversation with its human that can run for months, messages
other bots without a human relaying, and keeps working on the user's own box after the
laptop closes. Work is organized around bots and teams that persist, instead of an agent
spawned per task with the human bridging the gaps between them.

The previous design was a daemon-enforced pipeline: fixed stages, stateless agents, and a
black-box CLI backend. That bought bounded cost and guaranteed convergence at the price of
rigidity. This design moves the structure out of the workflow and into the harness: what a
bot remembers, what it may do, who it may talk to, and what the human can see.

The nearest product is xAI's Grok Bot. Two of its choices are deliberately reversed:

- Its bots share one VM, workspace, and connector set, so a bot is not a security
  boundary. Here each bot has its own environment, connectors, and egress rules (§9).
- It hides the model, the context, and the machine. Here all three are visible and
  adjustable: which model and provider, what was in context for any reply, and what
  survived a context rollover and why (§7, §8).

It should still run as smoothly as Grok Bot does. Every control in this document has a
default that lets the user just talk to a bot and have it go.

### Goals

- **Bots that last.** Durable identity, a job, memory that improves with use, and a chat
  that outlives any model session.
- **Bot-to-bot communication** with no human in the relay path, and with the human able
  to read all of it.
- **Teams as structure:** rosters, owned tasks, and shared knowledge rather than
  throwaway agents.
- **An owned agent loop.** Context assembly, memory, and tool policy are ours, so they
  can be shown and steered. API keys, the major providers, OpenRouter for one-key model
  switching, and local llama.cpp models.
- **Parallel-first.** A bot stays free to answer; long work runs in sub-bots or external
  workers. Cursor cloud agents are a first-class way to get development done.
- **Isolation per bot:** environment, connectors, egress, and blast radius set by role.
- **Bring your own cloud.** The daemon runs on the user's box. Every client is only an
  interface; closing one never stops a bot.
- **Power-user transparency with hands-off defaults.**
- **TUI-first.** The whole workflow is first-class in the TUI. The CLI is for scripting
  and as a fallback; it never does something the TUI cannot.

### Non-goals (v1)

Multi-bot teams, sandbox enforcement, cloud bootstrap, a chat bridge or web client, vector
search, the model-fallback engine, and reduced-permission sub-bots. Each is a later
milestone with its own section below stating the intent, the seam v1 must build so the
milestone is not a rewrite, and the open questions. They are planned, not deferred into
fog.

Also out: cost bounding (deliberately deferred; the hooks are mailbox throttling, wake
turn budgets, and per-reply usage capture), multi-user or hosted operation (single user,
user-owned infrastructure), and subscription login for the bot's own model calls (§8.1).

### Principles

1. **By construction rather than convention.** If a property matters, the harness makes
   it true; it is not requested of the model. Policy is enforced at the tool call,
   recalled text is tagged by the harness, the cloud box cannot read the laptop, and
   secrets cannot enter an environment.
2. **Memory is a hint; the world is the truth.** State (who owns a task, what merged,
   which script deploys) is re-read from its source at wake. Memory holds judgment: how
   this codebase works, how the user likes things done.
3. **Capture always, display optionally, block by policy.** Three independent axes (§7.2).
4. **Local is not a mode.** Clients always talk to a daemon. The daemon is on localhost or
   at the end of an SSH tunnel; nothing else differs.
5. **One mechanism where two would do.** Context rollover is an early end of session. Model
   loss, daemon crash, and reboot share one recovery path. A DM is a two-participant
   conversation. Cursor is one executor among several.

### Terms

| Term | Meaning |
| --- | --- |
| bot | Long-lived agent with identity, memory, mailbox, and address-book entry. |
| sub-bot | Ephemeral worker on the owned loop, spawned by a bot for one job. The parent's hands. |
| worker | External executor of a job, such as a Cursor cloud agent. |
| job | One unit of delegated work, run by an executor (§6). |
| wake | One activation of a bot, triggered by one mailbox item. |
| session | One model-visible context. Wakes and jobs run sessions; a session can roll over. |
| conversation | Durable N-participant message log. The user's chat with a bot is one. |
| task | A thread in a conversation with an owner and a status. |
| environment | Where a bot's tools execute: a directory, a container, or a microVM (§9). |

## 2. Architecture

```
 clients (any machine)                        the box (laptop or BYO cloud)
┌───────────────┐   JSON/HTTP + SSE    ┌─────────────────────────────────────────────┐
│ TUI           │   on a unix socket,  │ teams daemon                                │
│ web (later)   │◀─ SSH-forwarded ────▶│   mailboxes · wake scheduler · agent loop   │
│ bridge (later)│   when remote        │   policy checkpoint · librarian · routes    │
└───────────────┘                      │   SQLite (WAL) · bot home dirs · secrets    │
                                       └────────┬────────────────────────┬───────────┘
                                 environment    │                        │  executor
                                 boundary       ▼                        ▼  seam
                                       ┌──────────────────┐    ┌──────────────────────┐
                                       │ bot environment  │    │ external workers     │
                                       │ dir → container  │    │ Cursor cloud agents  │
                                       │ → microVM        │    │ later: Claude Code   │
                                       └──────────────────┘    └──────────────────────┘
```

**The loop runs daemon-side; only tool execution crosses into the environment.** Model
calls, context assembly, memory, and policy all happen in the daemon. The environment
sees commands and file operations, never a provider key and never the context. This is
what makes "secrets stay daemon-side" and "every tool call passes one checkpoint"
structural, and it means an environment needs no model egress at all.

Two interfaces carry the design, and they are easy to confuse:

- The **environment boundary** is how a bot's tools run: `exec`, file read/write, git.
  v1 implements it over a plain directory; later implementations are a container and a
  Gondolin microVM (§9).
- The **executor seam** is how a bot delegates a job: `spawn`, `status`, `steer`,
  `cancel`, `result`. `self` (a sub-bot) is always available; `cursor` plugs in when
  the connector exists (§6).

### 2.1 State layout

User-facing settings live under `~/.myai/`; mutable state lives under `state_root()`,
which `MYAI_HOME` relocates. No laptop-specific absolute paths are stored in state.

```
~/.myai/teams.json              hosts, providers, routes, presets, display toggles
state_root()/teams/
  teams.db                      conversations, episodic log, facts, jobs, inbox, events
  daemon.sock  daemon.lock
  bots/<bot>/bot.yaml           job, routes, connectors, contacts, preset, budgets
  bots/<bot>/persona.md
  bots/<bot>/constraints.md     the budgeted system-prompt tier (§7.1)
  bots/<bot>/playbooks/*.md     procedural memory
  bots/<bot>/workspace/         v1 environment root; the bot's clones live here
  artifacts/                    large tool outputs and transfers; paths in the DB
```

Files hold what a human edits in `$EDITOR`; the database holds what the system appends.
The fact store is a derived index over the episodic log (§4.7). Provider keys and
connector credentials come from the daemon's environment or a `0600` secrets file, and
are never written to the database, a bot home, or an environment.

### 2.2 Protocol

JSON over HTTP on a unix socket, with a Server-Sent Events stream for everything that
changes. Every mutation is a request the daemon validates. The old design used SQLite as
IPC between CLI and daemon; that cannot survive a daemon on another machine, and a single
writer makes exactly-once simpler.

A client is a stateless view over the append-only `events` log plus a cursor
(`Last-Event-ID`). A laptop that slept eight hours reconnects and replays from its cursor.
Any number of clients may attach at once.

Every capability is a protocol operation before it is anything else. Operations are
declared once, in a table that both the CLI subcommands and the TUI command palette are
generated from. Nothing can then exist in the CLI that the TUI cannot reach, and a new
operation is usable from the TUI the day it lands, before it has a screen of its own.

State changes stay exactly-once, as before: consuming a mailbox item and committing its
effects are one transaction, and a resolved approval is applied under an `applied_at`
stamp so it can never apply twice.

## 3. Bots

### 3.1 Identity

A bot is its home directory plus its rows: persona, constraints, playbooks, memory,
connector set, address book, mailbox. It is not a process and not a VM. Between wakes
nothing is running; an environment is hydrated when the bot has work and may be torn
down when it idles. Ten idle bots cost ten directories.

### 3.2 Wake lifecycle

1. **Trigger.** One mailbox item: a user message, an addressed bot message, a job event,
   or a schedule or external trigger.
2. **Assemble context** (§3.4).
3. **Loop.** Model call, tool calls, repeat. Every turn boundary is persisted before the
   next call, so the session is resumable from the log (§12).
4. **End.** Replies sent, jobs spawned, the task's handoff note refreshed if work remains.
5. **After the wake, off the hot path:** the librarian mines the transcript, reconciles
   candidate facts, and files debrief proposals (§4.2).
6. **Idle.**

### 3.3 Single-threaded, short wakes

A bot runs one wake at a time; its mailbox serializes them. This removes every
concurrent-session hazard (two sessions writing one memory, two loops in one workspace)
and it is the reason a bot must stay free: **a bot does small, quick actions itself and
delegates everything else to a job.**

- Mailbox priority: user message, then job event, then bot message, then schedule.
- A message that arrives mid-wake is queued and fires when the wake ends. Clients show
  it as queued with the wake's elapsed time.
- "Shortly" is enforced, not hoped for: each wake has a turn budget (defaults to tune:
  8 tool calls or 60 seconds). Past it the harness tells the bot to delegate the rest
  or wrap up.
- The parent's context always carries a summary of its open jobs, so answering "how's
  it going?" or deciding to steer a job never requires the job to stop.

### 3.4 Context assembly

Every wake is a cold start, so context assembly is the core of the product. Layout runs
from stable to volatile:

1. **Stable prefix:** persona, constraints (§7.1), playbook index, tool definitions.
2. **Semi-stable:** contacts and roster, open-jobs summary, the task's handoff note.
3. **Volatile tail:** the last N turns of this conversation verbatim, retrieval hits keyed
   on the incoming message (each shown with its age and how it was known), and the
   triggering item.

Within a session the context is append-only. Memory fetched mid-session arrives as tool
results at the tail; the prefix is never rewritten, so provider prompt caches hold for
the whole session.

### 3.5 Sessions and context-full handling

What happens when a context fills is a pluggable strategy:

- **`rollover`** (first implementation): end the session early. The bot writes the
  handoff note, the librarian mines the transcript, and a fresh session starts on the
  same wake or job with the normal assembly. No separate compaction machinery.
- **`compact`:** summarize in place and continue. Allowed; nothing here forbids it.

Both emit the same **survival record**: what was carried forward (handoff fields, facts
mined, proposals filed) and what now lives only in the log. The client shows survival
records and marks rollover points in the chat; one display toggle hides both.

The handoff note is a structured document per task that the bot maintains
incrementally: goal, plan, done, next, open questions, and pointers (files, branches,
PRs, job ids). It holds pointers rather than content; the repository and the task row
hold the real state.

### 3.6 The long-lived chat

The chat the user sees is a conversation: a durable, unbounded log. The session the
model sees is a bounded working set rebuilt each wake. The bot can dig into its history
on demand (`history.search`, `history.read`), limited to conversations it participates
in.

The failure mode this creates is a bot that pretends to remember. The user sees one
continuous thread and writes "same as last week"; a pull-only bot answers as if it
recalls. Two guards: the verbatim tail of recent turns is always in context, and
retrieval runs on the incoming message before the first model call, so the material is
present without the bot having to decide to look.

## 4. Memory

Repeated summarization compounds loss. Instead, precise facts are extracted once,
directly from the raw transcript, into a store the bot sees an index of and can drill
into. One-line facts make a bot informed, though, not better at its job; improvement is
procedural. So memory is split by kind, and each kind has its own write and read path.

### 4.1 Kinds

| Kind | Holds | Written | Read |
| --- | --- | --- | --- |
| Constraints | "never force-push main", user preferences | mined, then promoted per policy | pushed: harness policy, system prompt, or triggered by the pending action (§7.1) |
| Procedural | playbooks: multi-step, conditional know-how | bot proposes edits in a debrief | index in the prefix; body pulled on demand |
| Semantic facts | atomic, self-contained, scoped one-liners | librarian; supersede, not append | topic index, then search, then drill to source |
| Episodic | every turn of every session, immutable | automatically | never loaded wholesale; source of truth for drill-down and re-mining |
| Working state | the task's handoff note | bot, at the end of a session | pushed at the next wake on that task |

### 4.2 Writing: mine, reconcile, debrief

- **Mine.** After a session the librarian extracts candidate facts. Each is rewritten
  to stand alone (references resolved, scope named) and carries its scope, how it was
  known, its source turns, and its event time.
- **Reconcile.** Each candidate is compared with its neighbours and becomes one of
  add, supersede, expire, or no-op. Reconcile is serialized per memory scope and
  orders facts by event time.
- **Debrief.** Playbook edits and constraint promotions are proposals, applied or
  queued according to policy (§7.3).

Sub-bot transcripts mine into the **parent's** memory. In a parallel-first design all
the real work happens in sub-bots; if their episodes were not attributed to the parent,
the parent would only ever learn how to delegate.

### 4.3 Provenance rules

1. **A fact needs first-hand grounding in its own session:** a user message or a tool
   observation. "A bot told me" is never valid provenance.
2. **Recalled content is never valid provenance.** The harness tags everything that
   entered a session through recall (history reads, injected facts). The miner may read
   tagged spans as context but cannot ground a fact in them. Without this, a bot that
   re-reads Monday's "use script A" on Friday re-mines it with Friday's date and it
   supersedes Thursday's "use script B": recall would launder dead facts into live ones.
   If the bot re-verifies an old claim against the world, that is a fresh observation
   and mines normally.
3. **Facts carry event time, never mining time.** Re-mining old transcripts with a
   better miner is then idempotent and cannot resurrect anything.
4. **User corrections are events too.** Editing or reverting a fact is recorded in the
   log, so a rebuild replays it.

### 4.4 Staleness and poisoning

Acting on a stale fact usually fails loudly, and the bot investigates; memory is a hint.
The dangerous case is silent success: the old script still runs, against the old
cluster. Three cheap guards, all possible because the loop is ours:

- Reconcile supersedes on any change a bot witnessed.
- Each reply's manifest (§7.2) lists the facts that were in context, so a failure can be
  traced to the fact behind it and that fact superseded.
- Facts are injected with age and how-known ("observed 12d ago"), so the model can
  weigh them.

A change nobody witnessed that fails silently remains a risk. It is accepted for v1.

A wrong inference mined as a fact would be retrieved forever. The provenance rules bound
this, the mining diff makes it visible, and every fact records the model that mined it,
so facts from a weak model can be targeted for re-mining.

### 4.5 Reading: push and pull

Pull-only retrieval fails for the memories that matter most: a bot about to force-push
has no reason to search for a force-push policy. So constraints are pushed (§7.1), the
conversation tail and message-keyed retrieval are pushed (§3.4), and everything else is
pulled through tools: `memory.search`, `memory.open(topic)`, `memory.source(fact)`,
`history.search`, `history.read`.

The index the bot sees is hierarchical (topics, then facts, then source episodes), so it
stays small as the store grows.

### 4.6 Scopes

Two scopes: bot-private and team-shared. Knowledge moves between bots **by reference
through the store**, never by retelling in a message: a shared fact is one record with
its original provenance, so there is no telephone game at any hop count. Writing to the
shared scope is a per-bot permission, like a connector; a restricted bot can read the
team's knowledge without being able to pollute it.

**v1 seam:** every memory row carries a `scope` column; only private is used.
**Open:** promotion rules from private to shared, and whether some shared facts should
be readable only by some roles.

### 4.7 Storage

SQLite with FTS5 over facts and turns, plus tags and scopes, with the bot iterating its
own queries. Vectors are added when recall demonstrably fails, not before. Mining and
embeddings may run on the local llama.cpp stack as a user setting (§8.3), so memory work
need not consume an API or leave the box.

The fact store is a derived index. Transcripts are immutable and complete, so the store
can be rebuilt, and a better miner can be run over history.

## 5. Conversations and teams

### 5.1 One primitive

A conversation is a durable log with N participants. A DM has two. The user's chat with
a bot, a bot-to-bot DM, and a group chat are the same thing, in the same tables, and that
log is also the episodic store for messages. Bot-to-bot traffic needs no human in the
loop, and the human can read any of it.

- **Membership controls visibility; addressing controls waking.** A message wakes only
  the participants it addresses. Everyone else sees it in the tail the next time they
  wake in that conversation. This gives group chats without wake storms, and it makes
  "mass DM" and "group message" the same operation.
- **The address book is an access-control list.** A bot can message only its contacts;
  the harness rejects anything else. How far a confused bot's messages can spread is
  bounded the same way its tool reach is.
- **Messages carry coordination by value** ("the PR is open", "can you review?").
  Knowledge moves by reference (§4.6). State in a message is never mined.

### 5.2 Tasks and teams

A **task** is a thread with an owner and a status (`open`, `active`, `blocked`,
`needs_input`, `done`, `dropped`). The board is a query over tasks. Ownership lives in the
task row and is re-read at wake; it is never something a bot remembers.

A **team** is a roster, a shared-memory scope, and policy defaults.

A **lead** is not an architectural component. It is a bot with the `assign` permission
and a routing playbook, and it is the default recipient for unaddressed messages in a
group. With no lead configured, an unaddressed message wakes every member, which is
fine for two or three bots. The default team template ships a lead, because "just talk
to it and it goes" needs a front door; the user can still DM any specialist.

Schedules and external triggers are wake sources like any other mailbox item.

**v1 seam:** N-participant conversations, a shared id space for users and bots,
per-message recipients, `bot_id` on every row, and a contacts table whose one entry is
the user. v1 has one bot, which owns every task.
**Open:** bot-to-bot group chats in the UI (the data model already allows them), how a
lead's routing playbook is seeded, and cross-team contact defaults.

## 6. Jobs, sub-bots, and workers

### 6.1 The executor seam

`spawn(brief) → handle`, `status`, `steer(message)`, `cancel`, `result`. Job events
(`started`, `needs_input`, `finished`, `failed`) land in the owner's mailbox. This is the
old `AgentBackend` protocol, moved from the daemon's backend to the bot's toolbelt. The
bot chooses the executor; policy and the user can constrain the choice.

Parallelism never depends on an external service. `self` is always there.

### 6.2 `self`: sub-bots

A sub-bot is the parent's hands, not a teammate:

- It runs on the owned loop, so it is as transparent as the parent.
- It has no mailbox, no address-book entry, and no memory of its own. It receives a
  context package at spawn, reports only to the parent, and returns `done`, `failed`,
  or `needs_input` (the parent asks the human).
- Depth is one: sub-bots do not spawn.
- **Authority only narrows downward.** A sub-bot never holds more than its parent. In v1
  it shares the parent's environment and connectors and gets its own workspace
  directory, so two sub-bots never edit one tree. Later a sub-bot can be spawned with a
  subset, such as a web-research sub-bot with no forge token (§9).
- Its sessions roll over like any other; its transcripts mine into the parent (§4.2).

v1 allows one `self` job per bot at a time; delegated jobs are uncapped.

### 6.3 `cursor`: Cursor cloud agents

A first-class connector for "develop this slice", usable for other work at the user's
discretion and, later, the bot's. It maps directly onto the seam (Cursor Cloud Agents
API, verified 2026-09-19):

| Seam | API |
| --- | --- |
| spawn | `POST /v1/agents` (creates a durable agent and its first run) |
| steer | `POST /v1/agents/{id}/runs` (follow-up; the agent keeps its context) |
| status, result | `GET /v1/agents/{id}/runs/{runId}`; results include pushed branches and PR URLs |
| cancel | `POST /v1/agents/{id}/runs/{runId}/cancel` |
| observe | `GET .../stream` (SSE: `assistant`, `thinking`, `tool_call`); `GET /v1/agents/{id}/usage` |

Webhooks are not yet in v1 of that API, so completion is detected by stream or poll.
User API keys are self-service, held daemon-side. Cursor's SDK docs state that
programmatic runs follow the same pricing and request pools as IDE runs, so this work
bills to the user's Cursor plan rather than a per-token key. The REST API needs only
the standard library.

An external worker cannot see bot memory. It gets what the bot packs into the brief,
and it reads the repository's own rules. `myai sync` already renders rules into
`.cursor/rules/`, so a lesson promoted into the master rules reaches every future
worker through the repo. Whether cloud agents honour repo rules exactly as the IDE agent
does is unverified.

The unmodified Claude Code binary fits the same seam later (§8.1). So does pi, run
through the existing `myai sandbox`; that is the extent of pi's role here.

**The transparency promise covers bots and sub-bots.** For an external worker the
visible surface is the brief sent, the event stream, the usage, and the result.

### 6.4 Workspaces and git

A bot owns its workspace: its own clones, under its environment. It delivers through
branches and pull requests. It never touches the user's checkout, **including when the
daemon runs on the user's laptop**, so moving to a cloud box changes nothing and an
isolated environment has nothing to fight. The cost is that a bot cannot see uncommitted
local changes unless they are sent to it. When a sub-bot works in a repository, that
repository's `AGENTS.md` loads as project-scoped constraints; it is first-hand,
user-authored text. Pushing requires a forge credential, held daemon-side.

### 6.5 `transfer`

A bot can request files that git cannot provide: uncommitted changes, a dataset, a local
document.

- **The client pushes; the box never pulls.** A daemon on a cloud box cannot reach into a
  laptop, and this design keeps it that way.
- **Always approved by the user, never `auto`.** A prompt-injected bot asking for
  `~/.ssh` is the obvious abuse. The request shows exact paths and sizes.
- Secret-looking paths (`.env`, key files) are refused with a pointer to connectors.
- With no client connected, the request waits in the inbox like any `needs_input`.
- The reverse direction (bot delivers an artifact to the user) is low-risk: `notify`.

## 7. Control and transparency

### 7.1 Constraint tiers

Where a constraint lives, best first:

1. **Harness policy.** A pre-tool-call check that denies or requires approval. It is
   deterministic, and the denial message teaches the bot in context. Anything that must
   hold belongs here. The sandbox's egress allowlist and host-scoped secrets are the
   same idea one level down.
2. **The system prompt,** for soft constraints a rule cannot express. It has a hard line
   budget. Adding to a full budget means merging or demoting something, which forces
   curation and keeps the prefix stable for caching.
3. **Triggered injection,** keyed on the pending action, for the long tail.

A mined constraint is routed by type: enforceable ones become policy-rule proposals,
universal soft ones go to the system prompt, situational ones are triggered.

### 7.2 Three axes

- **Capture is always on.** A context manifest per reply (which turns, facts, playbooks,
  handoff note, and constraints were in context; which model and route; token counts),
  the mining diff per session, survival records, and every policy decision. It is a few
  rows per wake.
- **Display is a toggle.** Someone who ran hands-off for a month can still open any
  reply and see what the bot knew.
- **Blocking is a policy** per action class: `auto` (apply, log), `notify` (apply,
  surface, revertible), or `gate` (wait in the inbox).

The manifest also makes memory testable: "was the right fact in context?" is a
deterministic assertion that separates memory failures from model failures (§13).

### 7.3 Action classes and presets

| Action class | hands-off | supervised | locked-down |
| --- | --- | --- | --- |
| private fact write | auto | notify | gate |
| playbook edit, constraint promotion, shared-memory write | notify | gate | gate |
| tool execution inside the workspace | auto | auto | gate |
| spawn a job (`self` or external) | auto | notify | gate |
| bot-to-bot message | auto | auto | notify |
| irreversible external action (send, publish, pay, delete, production change) | gate | gate | gate |
| `transfer` request | gate | gate | gate |

Presets are per team with per-bot overrides. Memory writes are revertible because the
store is derived; actions in the world are not, which is why hands-off still gates them.

### 7.4 Inbox, not prompts

Nothing blocks a terminal on `[y/n]`. A gate writes an inbox item and parks only the
affected work; the bot keeps handling everything else. Approvals carry an optional note
that is routed back as feedback. Gates can also be resolved inline in the chat.

**v1 seam:** every side-effecting tool call already passes the policy checkpoint (v1:
allow and log, except the always-gated classes above), and the inbox exists.
**Open:** the rule language for deny and require-approval policies, and how
action-triggered injection keys on a pending call.

## 8. Models and providers

### 8.1 The owned loop

The loop is ours because the differentiating features (seeing the context, steering what
survives, policy at the tool call, retrieval keyed on a pending action) cannot exist on
a loop we do not own. Two provider clients cover the field: an OpenAI-compatible client
(OpenAI, OpenRouter, any llama.cpp server including myai's own) and a native Anthropic
client for its caching and thinking features. Model, provider, thinking or effort level,
and sampling settings are visible and editable per bot and per role.

**A custom loop in Python, not pi.** Pi would make a first demo faster and the product
slower. Its conveniences (context assembly, compaction, session storage, tool execution)
sit exactly where this design's differentiators live, so each would have to be overridden
through an extension against someone else's internals: a fork in all but name. It is a
Node agent that runs where its tools run, while this design keeps the loop in the Python
daemon and lets only tool execution cross into the environment. What pi would really
save is a provider layer and a set of tools; v1 needs one OpenAI-compatible client
(OpenRouter covers the model zoo) and a handful of tools, and the heavy coding goes to
Cursor. Pi stays useful as a reference for edge cases (tool-call repair, streaming and
provider quirks); ideas port freely, and its license should be checked before any code
does.

**API keys only.** Anthropic's Claude Code legal page (verified 2026-09-18) does not
permit third-party developers to offer Claude.ai login or to route requests through
Free, Pro, or Max credentials; it does permit a user signing in to the unmodified
Claude Code binary with their own subscription, including where a platform hosts it.
That policy changed several times in 2026. So subscriptions enter through the executor
seam, never through the brain: the bot thinks on an API key (small token volume) and
the heavy work can run on a subscription-backed worker.

### 8.2 Routes and availability

Roles (brain, sub-bot, miner, embedder) bind to **routes**, not model names. A route is
an ordered list of endpoints plus `on_unavailable: fallback | wait | fail`, and the
daemon health-checks endpoints.

The problem is endpoint availability, not local versus cloud: a laptop sleeps,
OpenRouter rate-limits, a key runs out of credit, a home GPU box reboots. One mechanism
covers all of them.

- **Fallback is consent as well as uptime.** People choose local models for privacy;
  silently rerouting a bot's context to a cloud provider defeats the choice. Fallback is
  opt-in per bot. For some bots the right policy is `wait`.
- Binding an always-on role to a local-only route with no fallback warns at config
  time: this bot only thinks while that machine is up. Each bot's status shows its
  current route and its health.
- Every reply and every mined fact records its model. A bad reply can be traced to the
  fallback; weak-model facts become re-mining targets.
- Endpoints declare capability (tool calling, context length); v1 checks context length.

| Layer | Needs a model | When its endpoint is down |
| --- | --- | --- |
| Retrieval at wake (FTS) | no | unaffected |
| Bot brain, sub-bots, handoff writing | yes, hot path | per-bot policy: fall back, park, or fail |
| Miner and reconcile | yes, deferrable | work queues; event time keeps late mining correct |
| Embeddings, if enabled | index deferrable, query hot | indexing queues; queries degrade to FTS |
| Cursor jobs | not ours | unaffected |

**Parking cannot depend on a model:** a bot with no brain cannot write a handoff note. So
a parked wake resumes from the log when its route is healthy (§12).

### 8.3 Local models with a daemon elsewhere

A cloud box cannot dial into a laptop behind NAT. The laptop client reverse-forwards its
`llama-server` over the SSH connection it already holds, and the last hop into an
isolated environment is the existing `model.host` loopback route in
`myai/sandbox/config.py`. "Local is available" means "a client is connected and
forwarding a healthy endpoint". An always-on machine at home can run a headless
forwarder.

**v1 seam:** route configuration (even with one entry), sessions resumable at turn
boundaries, the model recorded per reply and per fact.

## 9. Isolation and secrets

**Intent.** Each bot has its own environment, its own connectors and MCP servers, and
its own egress allowlist, so a role's blast radius is set by configuration rather than
trust. A bot is not its environment: environments are hydrated on wake and may be torn
down on idle, while durable state lives in the home directory and the database.

**Tiers** behind the environment boundary:

1. A plain directory (v1). **This is not a security boundary.** Tools run as the user's
   OS account; protection is the policy checkpoint and the user's review.
2. A container, for boxes without KVM.
3. A Gondolin microVM where KVM or HVF exists, reusing `myai/sandbox`: the VM spec,
   fail-closed egress allowlist, host-side secret injection, hidden paths, and
   `tcpHosts` loopback routes. This already runs on the development machine and is the
   first tier to wire in.

Credentials stay with the daemon and are applied at the boundary, the way the sandbox
injects secrets at the network layer today, so they never sit in an environment's files
or process environment.

We do not rely on an external worker's cloud for isolation. Cursor's VMs isolate Cursor's
work; our bots and sub-bots run real tools and need their own boundary.

**v1 seam:** every tool executes through the one environment interface; nothing calls
`subprocess` around it. Connector configuration is already per bot.
**Open (research):** which tier is viable on a typical VPS, many of which expose no
`/dev/kvm`; the cost of the Node and QEMU dependencies on the box; whether MCP servers
run daemon-side as proxies or inside the environment; per-sub-bot reduced permissions.

## 10. BYO cloud and clients

**Intent.** The box runs the daemon, the bots, the sub-bots, the connectors, and the
database. Clients run anywhere. Local models run on client machines. External workers
run in their vendor's cloud. Closing the TUI, the laptop, or a browser tab stops
nothing.

**Bootstrap.** `myai cloud bootstrap user@host`, entirely over SSH: run the existing
`install.sh` (idempotent, with preflight and uninstall), write a systemd user unit for
the daemon, run `loginctl enable-linger` (without it, user services stop when the SSH
session ends, which is exactly the failure "close the laptop" would hit), health-check,
and save the host in `~/.myai/teams.json`. Upgrading is re-running it.

**Transport.** The client drives an `ssh` subprocess that forwards the daemon's unix
socket. No open ports, no new authentication surface, nothing beyond the standard
library.

**The TUI is the product surface.** Everything a user does is first-class there: first
run and creating a bot, chat, bots and their homes, tasks and the board, the inbox, jobs
and their live streams, the memory browser, context manifests and survival records,
presets and policy, routes and their health, and connecting to a remote box. Frequent
flows get screens and hotkeys; the command palette (§2.2) reaches everything else, and a
help overlay lists every binding. Structured settings are forms; long-form text
(persona, constraints, playbooks, a handoff note) suspends to `$EDITOR` and returns.

**The CLI is the fallback and the scripting surface:** daemon control, bootstrap, and
the same operations for pipes, cron, and a machine with no usable terminal UI. It may
expose less than the TUI, never more.

```
myai teams                            # opens the TUI
myai teams daemon start|stop|status
myai teams bot new|edit|list          # fallback for what the TUI does
myai teams send <bot> "message"       # scripting; same operation as the TUI
myai teams inbox | approve | reject
myai cloud bootstrap user@host
```

A web client (desktop, browser, mobile from one stack) and a chat bridge are later
clients of the same protocol, not new architectures. A bridge maps a conversation onto a
chat app the user already has, which brings mobile and push notifications; inspection
(manifests, diffs, policy) stays in our own clients.

**v1 seam:** the daemon/client split and the protocol exist from day one, even on one
machine; state is relocatable; no laptop paths in state.
**Open:** Python 3.14 on stock VPS images (a uv-managed interpreter is the likely
answer), backups of the state directory, notifying the user when every client is
closed (the bridge), which bridge first, and the TUI toolkit (Textual was the earlier
intent; it would be the project's second dependency).

## 11. Data model (sketch)

The exact schema lands with the first milestone. These tables encode the decisions above.

```sql
-- Users and bots share one id space, so conversations are N-participant from day one.
CREATE TABLE principals (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,            -- user|bot
  name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE contacts (                               -- address book = ACL
  bot_id TEXT NOT NULL, contact_id TEXT NOT NULL, PRIMARY KEY (bot_id, contact_id)
);

CREATE TABLE conversations (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL,         -- dm|group
  team_id INTEGER, title TEXT, created_at TEXT NOT NULL
);
CREATE TABLE participants (                           -- membership = visibility
  conversation_id INTEGER NOT NULL, principal_id TEXT NOT NULL,
  PRIMARY KEY (conversation_id, principal_id)
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, task_id INTEGER,
  sender_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE message_recipients (                     -- addressing = waking
  message_id INTEGER NOT NULL, principal_id TEXT NOT NULL,
  PRIMARY KEY (message_id, principal_id)
);
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, title TEXT NOT NULL,
  owner_id TEXT, status TEXT NOT NULL,                -- open|active|blocked|needs_input|done|dropped
  handoff TEXT, created_by TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,                 -- guards $EDITOR round-trips
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE mailbox (                                -- one bot, one wake at a time
  id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL,
  kind TEXT NOT NULL,                                 -- user_message|job_event|bot_message|schedule
  ref_id INTEGER, priority INTEGER NOT NULL,
  enqueued_at TEXT NOT NULL, claimed_at TEXT, done_at TEXT
);
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, task_id INTEGER,
  executor TEXT NOT NULL,                             -- self|cursor|...
  external_ref TEXT, brief TEXT NOT NULL, status TEXT NOT NULL,
  result_json TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE sessions (                               -- a wake or a job runs 1..n sessions
  id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, mailbox_id INTEGER, job_id INTEGER,
  route TEXT NOT NULL, daemon_epoch TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT,
  end_reason TEXT                                     -- done|rollover|compact|parked|error
);
CREATE TABLE turns (                                  -- the episodic log; immutable
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  role TEXT NOT NULL, content_json TEXT NOT NULL,
  origin TEXT NOT NULL,                               -- first_hand|recalled (set by the harness)
  model TEXT, tokens_in INTEGER, tokens_out INTEGER, event_time TEXT NOT NULL
);

CREATE TABLE facts (                                  -- derived index; rebuildable
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL,        -- bot:<id>|team:<id>
  text TEXT NOT NULL, tags TEXT,
  how_known TEXT NOT NULL,                            -- user_said|tool_observed
  event_time TEXT NOT NULL, mined_at TEXT NOT NULL, mined_by TEXT NOT NULL,
  status TEXT NOT NULL,                               -- active|superseded|expired|reverted
  superseded_by INTEGER
);
CREATE TABLE fact_sources (fact_id INTEGER NOT NULL, turn_id INTEGER NOT NULL);
CREATE VIRTUAL TABLE facts_fts USING fts5(text, tags, content='facts', content_rowid='id');

CREATE TABLE manifests (                              -- what a reply had in context
  turn_id INTEGER PRIMARY KEY, items_json TEXT NOT NULL
);
CREATE TABLE survival_records (                       -- what survived a rollover or compaction
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
  strategy TEXT NOT NULL, diff_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE policy_decisions (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, action_class TEXT NOT NULL,
  target TEXT, decision TEXT NOT NULL, rule TEXT, created_at TEXT NOT NULL
);
CREATE TABLE approvals (                              -- the inbox
  id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, kind TEXT NOT NULL,
  summary TEXT NOT NULL, payload_json TEXT,
  requested_at TEXT NOT NULL, resolved_at TEXT, applied_at TEXT,
  decision TEXT, human_note TEXT
);
CREATE TABLE events (                                 -- append-only; the client sync cursor
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT
);
```

Large artifacts (tool outputs, transfers) stay on disk with paths in the database.

## 12. Recovery

- **Single daemon** by advisory `flock`; each start mints an `epoch` stamped on the
  sessions it runs.
- **Sessions resume from the last durable turn boundary.** This one path covers a lost
  model route, a daemon crash, and a reboot of the box. A model call that was in flight
  is re-issued; nothing had changed state.
- **A tool call that was in flight may have had its side effect.** On resume the harness
  marks it interrupted and says so, and the bot checks the world rather than blindly
  re-running it.
- **External jobs keep running** while the daemon is down. On restart the daemon
  reattaches by `external_ref` and catches up from status and stream.
- **Orphaned local processes** are matched by pid and start time and shown to the user
  for confirmation before anything is killed.

## 13. Milestones

1. **M1: one bot, end to end (v1).** Step one is merging and reworking the
   `agentic-teams` skeleton (§15). Then: one bot, one DM, local daemon, the owned loop on
   OpenRouter. The episodic log, resumable sessions, rollover, mining with event-time
   reconcile, the context manifest and survival records. Tasks and handoff notes.
   `self` sub-bot jobs and the Cursor connector. Bot-owned workspaces and `transfer`.
   The inbox with the always-gated classes. A TUI that covers all of it, from first run
   to reading a reply's context manifest, with the CLI as fallback. In practice: a long-lived dev
   lead that remembers the project and drives Cursor slices. M1 also builds every "v1
   seam" named above.
   **Proof:** a replay harness that teaches something in episode 1 and probes for it in
   episode N after intervening rollovers and mining, asserting at the manifest level;
   and daily use.
2. **M2: multi-bot and teams.** Several bots, bot-to-bot DMs, groups with addressed
   wakes, contacts, tasks with assignment, the lead template, team-shared memory with
   gated promotion, schedules and triggers, policy rules and action-triggered
   constraints (§5, §4.6, §7).
3. **M3: isolation tiers.** Gondolin first, then a container tier; per-bot connectors and
   egress enforced; reduced-permission sub-bots (§9).
4. **M4: BYO cloud and routes.** `myai cloud bootstrap`, the SSH transport, the route
   engine with `fallback | wait | fail`, reverse-forwarded local models (§10, §8).
5. **M5: more clients.** A chat bridge, then a web client (§10).

## 14. Key decisions and why

- **Own the loop; API keys for the brain; subscriptions only through executors.**
  Wrapping a vendor CLI gives no control of context, and vendor terms forbid routing a
  subscription through a third-party app. Cost: the best experience needs an API key.
- **A custom Python loop, not pi.** Everything pi would provide is what this design must
  control, and it is a Node agent that runs beside its tools (§8.1). Cost: we write a
  provider client and the basic tools ourselves, and own their edge cases.
- **Mine facts instead of re-summarizing; event time; recall is not provenance.**
  Summaries of summaries decay, and a naive miner resurrects dead facts through recall.
  Cost: a librarian pass after every session.
- **Single-threaded bots; work happens in jobs.** Concurrent sessions of one bot race on
  memory and workspace. Cost: a message waits for the current wake, so wakes are
  budgeted short.
- **Addressed wakes instead of broadcast channels.** Broadcast turns N bots into a wake
  storm and an unbounded reply loop.
- **Bots never touch the user's checkout; transfers are pushed by the client.** One
  behaviour locally and in the cloud, and the box has no path into the laptop. Cost: no
  view of uncommitted changes without a transfer.
- **A daemon API instead of SQLite-as-IPC.** A client on another machine cannot write
  rows into the box's database.
- **The loop runs daemon-side; tools run in the environment.** Keys and context never
  enter an environment, and one checkpoint sees every tool call.

## 15. What changed from the pipeline design

| Survives | As |
| --- | --- |
| Inbox, not prompts | §7.4, unchanged |
| The `gate:` vocabulary | `auto`/`notify`/`gate` on action classes instead of pipeline stages |
| Exactly-once state changes; state outside agent workspaces | §2.2, §2.1 |
| The `events` table | the client sync log |
| The `messages` table | generalized into N-participant conversations |
| `AgentBackend` and `needs_human` | the executor seam and `needs_input` |
| Never merge in the user's primary checkout | bots own their workspaces (§6.4) |
| Epoch reconciliation; confirm before killing orphans | §12 |

Gone: pipeline stages and per-task stage machines, epics and the epic-branch integration
(the forge's PR flow replaces it), the stateless PM, SQLite-as-IPC, the single-machine
non-goal, and `agent -p` as the runtime.

The unmerged `agentic-teams` branch holds a board skeleton of the pipeline model.
**Step one is to merge it and rework it in place** rather than start over. Kept: the
SQLite layer (`busy_timeout` before WAL, transactional migrations loaded through
`importlib.resources`), the `teams_*` helpers in `paths.py`, the `$EDITOR` round-trip,
the `teams` command group, PyYAML, and the test patterns. Reworked: `task` and `status`
onto the new schema. Retired: the pipeline schema (replaced by a fresh `001`; a pre-pivot
`teams.db` is moved aside, not migrated), the pipeline config validation, the role
prompts, and the `project` and `epic` commands.

## 16. Open questions

- The isolation tier for boxes without KVM (§9). A research ticket.
- The name. The README describes myai as a local LLM runner; whether this stays
  `myai teams` is undecided.
- An explicit in-session `remember` tool alongside the miner, or miner only.
- Promotion rules from private to shared memory, and role-scoped reads (§4.6).
- The policy rule language and action-triggered injection keys (§7.4).
- Which chat bridge first, and how notifications work with every client closed (§10).
- Cost bounding. Deferred on purpose; the hooks are mailbox throttling, wake turn
  budgets, and captured usage.
