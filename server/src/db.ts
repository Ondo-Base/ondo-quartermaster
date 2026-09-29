// The control plane's system of record: identity, devices, agents, grants,
// runs and their events, approvals, and the audit log. SQLite through
// node:sqlite, so a pilot needs no database server; the SQL is portable.

import { DatabaseSync } from "node:sqlite";

export type DB = DatabaseSync;

const SCHEMA = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL,
  policy_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'member',        -- member | admin
  active INTEGER NOT NULL DEFAULT 1,
  password_hash TEXT,
  external_id TEXT,                            -- SCIM externalId
  groups_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,                         -- sha256 of the cookie value
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_id TEXT,
  stage TEXT NOT NULL,                         -- needs_device | verified
  method TEXT NOT NULL,                        -- password | oidc | saml | dev-sso
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  os TEXT NOT NULL DEFAULT '',
  managed INTEGER NOT NULL DEFAULT 0,
  trusted_until INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_codes (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  to_addr TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pairing_codes (
  code_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_id TEXT,
  expires_at INTEGER NOT NULL,
  agent_id TEXT
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_id TEXT,
  token_hash TEXT NOT NULL UNIQUE,
  hostname TEXT NOT NULL DEFAULT '',
  os TEXT NOT NULL DEFAULT '',
  revoked INTEGER NOT NULL DEFAULT 0,
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS grants (
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                          -- files | screen | input
  granted INTEGER NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER NOT NULL,
  updated_by TEXT NOT NULL,
  PRIMARY KEY (agent_id, kind)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  request TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,                        -- queued | running | waiting | finished | stopped | error
  answer TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_events (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  source TEXT NOT NULL,
  data_json TEXT NOT NULL,
  ts REAL NOT NULL,
  hash TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  effects_json TEXT NOT NULL,
  values_json TEXT NOT NULL,
  diff TEXT,
  tool TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | refused | expired
  resolved_by TEXT,
  resolved_at INTEGER,
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS siem_sinks (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  url TEXT NOT NULL,
  format TEXT NOT NULL DEFAULT 'jsonl',        -- jsonl | cef
  secret TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  cursor INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scim_tokens (
  token_hash TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  label TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pilot_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

-- A person's yes to one connector on one agent, until they or an administrator
-- take it back. Policy (orgs.policy_json allowed_connectors) bounds what can be here.
CREATE TABLE IF NOT EXISTS consents (
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  connector TEXT NOT NULL,
  consented_by TEXT NOT NULL,
  consented_at INTEGER NOT NULL,
  PRIMARY KEY (agent_id, connector)
);

-- A request someone runs again and again, saved by name. Running one starts an
-- ordinary run with the same grants, gates and approvals as any other.
CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  request TEXT NOT NULL,
  created_from TEXT,                            -- the run it was saved from, if any
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Organisation-wide tokens IT puts in its device management, so an agent can set
-- itself up for the signed-in person without a pairing code. An agent enrolled
-- this way does nothing until that person confirms the computer is theirs.
CREATE TABLE IF NOT EXISTS enrollment_tokens (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER,
  uses INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS runs_user ON runs(user_id, created_at);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS audit_org ON audit(org_id, id);
`;

export function openDb(path: string): DB {
  const db = new DatabaseSync(path);
  db.exec(SCHEMA);
  // Columns added after a database was first created.
  const cols = (db.prepare("PRAGMA table_info(agents)").all() as { name: string }[]).map((c) => c.name);
  if (!cols.includes("capabilities_json")) db.exec("ALTER TABLE agents ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}'");
  // 0 until the person confirms a computer IT enrolled for them; paired agents are confirmed.
  if (!cols.includes("confirmed")) db.exec("ALTER TABLE agents ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1");
  if (!cols.includes("enrolled_via")) db.exec("ALTER TABLE agents ADD COLUMN enrolled_via TEXT");
  const acols = (db.prepare("PRAGMA table_info(approvals)").all() as { name: string }[]).map((c) => c.name);
  const rcols = (db.prepare("PRAGMA table_info(runs)").all() as { name: string }[]).map((c) => c.name);
  if (!rcols.includes("workflow_id")) db.exec("ALTER TABLE runs ADD COLUMN workflow_id TEXT");
  if (!acols.includes("kind")) db.exec("ALTER TABLE approvals ADD COLUMN kind TEXT NOT NULL DEFAULT 'effect'");
  if (!acols.includes("connector")) db.exec("ALTER TABLE approvals ADD COLUMN connector TEXT");
  return db;
}

export function now(): number {
  return Date.now();
}

// Small typed helpers over node:sqlite's statement API.
export function one<T>(db: DB, sql: string, ...params: unknown[]): T | undefined {
  return db.prepare(sql).get(...(params as never[])) as T | undefined;
}

export function all<T>(db: DB, sql: string, ...params: unknown[]): T[] {
  return db.prepare(sql).all(...(params as never[])) as T[];
}

export function run(db: DB, sql: string, ...params: unknown[]): { changes: number } {
  const r = db.prepare(sql).run(...(params as never[]));
  return { changes: Number(r.changes) };
}

export function tx<T>(db: DB, fn: () => T): T {
  db.exec("BEGIN");
  try {
    const out = fn();
    db.exec("COMMIT");
    return out;
  } catch (e) {
    db.exec("ROLLBACK");
    throw e;
  }
}

export function parse<T>(s: string | null | undefined, fallback: T): T {
  if (!s) return fallback;
  try {
    return JSON.parse(s) as T;
  } catch {
    return fallback;
  }
}
