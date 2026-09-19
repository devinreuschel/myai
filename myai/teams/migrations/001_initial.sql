-- Users and bots share one id space, so conversations are N-participant from day one.
CREATE TABLE principals (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('user', 'bot')),
  name          TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

-- A bot's address book is an ACL: sends to anyone not listed are rejected.
CREATE TABLE contacts (
  bot_id        TEXT NOT NULL REFERENCES principals(id),
  contact_id    TEXT NOT NULL REFERENCES principals(id),
  PRIMARY KEY (bot_id, contact_id)
);

CREATE TABLE conversations (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('dm', 'group')),
  team_id       INTEGER,
  title         TEXT,
  created_at    TEXT NOT NULL
);

-- Membership controls visibility.
CREATE TABLE participants (
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  principal_id    TEXT NOT NULL REFERENCES principals(id),
  joined_at       TEXT NOT NULL,
  PRIMARY KEY (conversation_id, principal_id)
);

CREATE TABLE tasks (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  title           TEXT NOT NULL,
  owner_id        TEXT REFERENCES principals(id),
  status          TEXT NOT NULL CHECK (
    status IN ('open', 'active', 'blocked', 'needs_input', 'done', 'dropped')
  ),
  handoff         TEXT NOT NULL DEFAULT '',
  created_by      TEXT NOT NULL REFERENCES principals(id),
  version         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  task_id         INTEGER REFERENCES tasks(id),
  sender_id       TEXT NOT NULL REFERENCES principals(id),
  body            TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE INDEX messages_conversation ON messages(conversation_id, id);
CREATE INDEX messages_task ON messages(task_id) WHERE task_id IS NOT NULL;

-- Addressing controls waking.
CREATE TABLE message_recipients (
  message_id    INTEGER NOT NULL REFERENCES messages(id),
  principal_id  TEXT NOT NULL REFERENCES principals(id),
  PRIMARY KEY (message_id, principal_id)
);

-- Lowest priority value wakes first, then oldest.
CREATE TABLE mailbox (
  id            INTEGER PRIMARY KEY,
  bot_id        TEXT NOT NULL REFERENCES principals(id),
  kind          TEXT NOT NULL CHECK (
    kind IN ('user_message', 'job_event', 'bot_message', 'schedule')
  ),
  ref_id        INTEGER,
  priority      INTEGER NOT NULL,
  enqueued_at   TEXT NOT NULL,
  claimed_at    TEXT,
  done_at       TEXT
);

CREATE INDEX mailbox_pending ON mailbox(bot_id, priority, id) WHERE done_at IS NULL;

-- A wake or a job runs one or more sessions; a session can roll over.
CREATE TABLE sessions (
  id            INTEGER PRIMARY KEY,
  bot_id        TEXT NOT NULL REFERENCES principals(id),
  mailbox_id    INTEGER REFERENCES mailbox(id),
  job_id        INTEGER,
  route         TEXT NOT NULL,
  daemon_epoch  TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  end_reason    TEXT CHECK (
    end_reason IN ('done', 'rollover', 'compact', 'parked', 'error')
  )
);

-- The episodic log. origin is set by the harness, never by the model.
CREATE TABLE turns (
  id            INTEGER PRIMARY KEY,
  session_id    INTEGER NOT NULL REFERENCES sessions(id),
  seq           INTEGER NOT NULL,
  role          TEXT NOT NULL,
  content_json  TEXT NOT NULL,
  origin        TEXT NOT NULL CHECK (origin IN ('first_hand', 'recalled')),
  model         TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  event_time    TEXT NOT NULL,
  UNIQUE (session_id, seq)
);

-- Append-only; clients sync from it with a cursor.
CREATE TABLE events (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL,
  payload_json  TEXT
);

-- The fact store is rebuilt from turns and clients replay events, so neither
-- may change after the fact.
CREATE TRIGGER turns_no_update BEFORE UPDATE ON turns
BEGIN
  SELECT RAISE(ABORT, 'turns are immutable');
END;

CREATE TRIGGER turns_no_delete BEFORE DELETE ON turns
BEGIN
  SELECT RAISE(ABORT, 'turns are immutable');
END;

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;
