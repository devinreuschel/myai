CREATE TABLE projects (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  workspace_path TEXT NOT NULL,
  config_json   TEXT NOT NULL
);

CREATE TABLE epics (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id),
  title         TEXT NOT NULL,
  goal          TEXT NOT NULL,
  status        TEXT NOT NULL,
  branch        TEXT,
  base_branch   TEXT NOT NULL,
  version       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE tasks (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id),
  epic_id       INTEGER REFERENCES epics(id),
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,
  status        TEXT NOT NULL,
  stage         TEXT,
  role          TEXT,
  priority      INTEGER NOT NULL DEFAULT 0,
  blocked_by    TEXT,
  gate          TEXT,
  version       INTEGER NOT NULL DEFAULT 1,
  loop_count    INTEGER NOT NULL DEFAULT 0,
  branch        TEXT,
  worktree_path TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE runs (
  id            INTEGER PRIMARY KEY,
  task_id       INTEGER NOT NULL REFERENCES tasks(id),
  stage         TEXT NOT NULL,
  backend       TEXT NOT NULL,
  task_version  INTEGER NOT NULL,
  started_at    TEXT,
  ended_at      TEXT,
  outcome       TEXT,
  result_json   TEXT,
  transcript_path TEXT,
  worktree_path TEXT,
  branch        TEXT,
  daemon_epoch  TEXT,
  pid           INTEGER,
  pgid          INTEGER,
  proc_start    TEXT
);

CREATE TABLE events (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL,
  task_id       INTEGER,
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL,
  payload_json  TEXT
);

CREATE TABLE approvals (
  id            INTEGER PRIMARY KEY,
  task_id       INTEGER,
  epic_id       INTEGER,
  project_id    INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  summary       TEXT NOT NULL,
  requested_at  TEXT NOT NULL,
  resolved_at   TEXT,
  applied_at    TEXT,
  decision      TEXT,
  human_note    TEXT,
  payload_json  TEXT
);

CREATE UNIQUE INDEX approvals_open_task ON approvals(task_id)
  WHERE resolved_at IS NULL AND task_id IS NOT NULL;
CREATE UNIQUE INDEX approvals_open_epic ON approvals(epic_id)
  WHERE resolved_at IS NULL AND epic_id IS NOT NULL;

CREATE TABLE messages (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL,
  epic_id       INTEGER REFERENCES epics(id),
  direction     TEXT NOT NULL,
  body          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  processed_at  TEXT
);
