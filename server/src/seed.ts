// Demo data, matching the sample people and organisation in the design. Only
// loaded with ONDO_SEED=demo. Passwords here are for a local demo and nothing else.

import { type DB, now, one, run } from "./db.js";
import { hashPassword } from "./lib/crypto.js";
import { DEFAULT_POLICY } from "./lib/policy.js";

export const DEMO_PASSWORD = "quartermaster-demo";

export function seedDemo(db: DB): void {
  if (one(db, "SELECT id FROM orgs WHERE id = 'org_northwind'")) return;
  const t = now();
  run(db, "INSERT INTO orgs (id, name, domain, policy_json) VALUES (?,?,?,?)",
    "org_northwind", "Northwind operations", "northwind-ops.com", JSON.stringify({ ...DEFAULT_POLICY, allowed_connectors: ["ticketing", "mail", "calendar", "documents", "ledger"] }));
  const pw = hashPassword(DEMO_PASSWORD);
  const users: [string, string, string, string, string][] = [
    ["usr_mara", "mara.okonjo@northwind-ops.com", "Mara Okonjo", "Revenue operations", "member"],
    ["usr_admin", "it.admin@northwind-ops.com", "Sam Adeyemi", "IT administration", "admin"],
  ];
  for (const [id, email, name, title, role] of users) {
    run(db, "INSERT INTO users (id, org_id, email, name, title, role, password_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
      id, "org_northwind", email, name, title, role, pw, t);
  }
}
