// Organisation policy: what the administrator excludes and disables. The agent's
// broker enforces it on the device; the control plane refuses to *grant* anything
// it excludes, so the UI and the agent can never disagree about what is allowed.

export interface OrgPolicy {
  excluded_paths: string[];
  excluded_windows: string[];
  disabled_grants: string[];
  writes_require_approval: boolean;
  // The only web origins the agent's browser may open.
  allowed_origins: string[];
  // Connectors people may allow for themselves. Nothing else can be used.
  allowed_connectors: string[];
}

export const DEFAULT_POLICY: OrgPolicy = {
  excluded_paths: ["**/HR/**", "**/*payroll*", "**/*password*"],
  excluded_windows: ["Personal mail", "HR portal", "Password manager"],
  disabled_grants: [],
  writes_require_approval: true,
  allowed_origins: [],
  allowed_connectors: [],
};

export function normalisePolicy(p: Partial<OrgPolicy> | undefined): OrgPolicy {
  const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x) => typeof x === "string").map(String) : []);
  return {
    excluded_paths: strs(p?.excluded_paths ?? DEFAULT_POLICY.excluded_paths),
    excluded_windows: strs(p?.excluded_windows ?? DEFAULT_POLICY.excluded_windows),
    disabled_grants: strs(p?.disabled_grants).filter((g) => ["files", "screen", "input"].includes(g)),
    writes_require_approval: p?.writes_require_approval !== false,
    allowed_origins: strs(p?.allowed_origins).filter((o) => /^https?:\/\/[^/]+$/.test(o.replace(/\/$/, ""))).map((o) => o.replace(/\/$/, "")),
    allowed_connectors: [...new Set(strs(p?.allowed_connectors).map((c) => c.trim().toLowerCase()).filter((c) => /^[a-z0-9_-]{1,40}$/.test(c)))],
  };
}

// The same glob semantics as the agent's broker (permissions.glob_to_regex).
export function globToRegex(pattern: string): RegExp {
  let out = "^";
  const p = pattern.replace(/\\/g, "/");
  for (let i = 0; i < p.length; ) {
    if (p.startsWith("**/", i)) { out += "(?:.*/)?"; i += 3; continue; }
    if (p.startsWith("**", i)) { out += ".*"; i += 2; continue; }
    const c = p[i];
    if (c === "*") out += "[^/]*";
    else if (c === "?") out += "[^/]";
    else out += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    i += 1;
  }
  return new RegExp(out + "$", "i");
}

export function pathExcluded(policy: OrgPolicy, path: string): boolean {
  const s = path.replace(/\\/g, "/");
  return policy.excluded_paths.some((g) => { const rx = globToRegex(g); return rx.test(s) || rx.test(s + "/"); });
}

export function windowExcluded(policy: OrgPolicy, name: string): boolean {
  const n = name.toLowerCase();
  return policy.excluded_windows.some((w) => n.includes(w.toLowerCase()));
}

export type GrantKind = "files" | "screen" | "input";

export function checkGrant(policy: OrgPolicy, kind: GrantKind, scope: string[]): string | null {
  if (policy.disabled_grants.includes(kind)) return `${kind} is disabled by your administrator`;
  if (kind === "files") {
    const bad = scope.find((f) => pathExcluded(policy, f));
    if (bad) return `${bad} is excluded by policy and cannot be granted`;
  }
  if (kind === "screen") {
    const bad = scope.find((w) => windowExcluded(policy, w));
    if (bad) return `${bad} is excluded by policy and cannot be granted`;
  }
  return null;
}
