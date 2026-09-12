#!/usr/bin/env node
// Enforcement of the project's npm-audit policy for PRODUCTION dependencies.
//
// Policy (see docs/dependency-security-audit.md and docs/phase10-ci-security.md):
//  - The gate reports `npm audit --omit=dev --json` for the production closure.
//  - The advisory IDs below are DOCUMENTED EXCEPTIONS: the affected packages are
//    build-time or not runtime-reachable:
//      * postcss (transitive via next): 1117015, 1124252, 1130709, 1139510  (F-02)
//  - F-03 (sharp 1124066, 1193725) was RESOLVED 2026-09-11 by upgrading to
//    sharp@^0.35.4; the IDs were removed here so any reappearing sharp advisory
//    FAILS the gate (must not silently regress).
//  - ANY OTHER advisory (new package or new advisory ID) FAILS the gate.
//  - Do not extend the allowlist silently: every entry requires a justification
//    in docs/dependency-security-audit.md and docs/phase10-ci-security.md.
//  - If `npm audit` output cannot be parsed (registry/network failure, malformed
//    JSON), the gate FAILS so the audit stays reproducible and honest.
//
// Usage: node check-npm-audit.cjs <npm-audit.json>

const fs = require("node:fs");
const process = require("node:process");

const ALLOWED_ADVISORIES = new Set([
  // postcss@8.4.31 (build-time CSS emission only; not shipped to the browser)
  "1117015", // PostCSS: XSS via unescaped </style> in CSS stringify output
  "1124252", // PostCSS: arbitrary file read via attacker-controlled sourceMappingURL
  "1130709", // PostCSS: incomplete fix of GHSA-6g55-p6wh-862q
  "1139510", // PostCSS: path traversal in previous source-map auto-loading
]);

const file = process.argv[2];
if (!file) {
  console.error("[npm-audit-policy] usage: node check-npm-audit.mjs <npm-audit.json>");
  process.exit(2);
}

let data;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch {
  console.error(
    `[npm-audit-policy] FATAL: could not parse npm audit JSON from "${file}" ` +
      "(registry/network failure or malformed output). Gate fails closed.",
  );
  process.exit(1);
}

const vulns = data.vulnerabilities ?? {};
const seen = new Set();
const unexpected = [];

for (const [name, entry] of Object.entries(vulns)) {
  for (const item of entry.via ?? []) {
    let ids = [];
    if (typeof item === "string") {
      // Indirect propagation (e.g. `next` -> via=["postcss"]): resolve the
      // referenced package's own advisory entries.
      const ref = vulns[item]?.via ?? [];
      ids = ref.filter((r) => typeof r === "object" && r.source).map((r) => String(r.source));
    } else if (typeof item === "object" && item !== null && item.source) {
      ids = [String(item.source)];
    }
    for (const id of ids) {
      if (seen.has(id)) continue;
      seen.add(id);
      if (!ALLOWED_ADVISORIES.has(id)) {
        unexpected.push({ id, package: name });
      }
    }
  }
}

if (unexpected.length > 0) {
  console.error("[npm-audit-policy] GATE FAILED - unexpected advisories in production dependencies:");
  for (const u of unexpected) console.error(`  - ${u.id}  (package: ${u.package})`);
  console.error(
    "[npm-audit-policy] Fix the dependency, or add a DOCUMENTED exception to " +
      "ALLOWED_ADVISORIES here plus a justification in docs/dependency-security-audit.md.",
  );
  process.exit(1);
}

const present = [...seen];
console.log(
  present.length === 0
    ? "[npm-audit-policy] OK - no advisories in the production dependency closure."
    : `[npm-audit-policy] OK - advisories present are all documented exceptions ` +
      `(postcss via next): ${present.join(", ")}`,
);
process.exit(0);