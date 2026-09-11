// Final verification v2: harness-style discovery scan + header validation checks
// (mirrors validateSessionHeader incl. the isAbsolute cwd rule that failed before).
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { isAbsolute } from "node:path";
import { zstdDecompressSync } from "node:zlib";

const ROOT = "/Users/blossomx/.dsh/sessions";
const ZSTD_MAGIC = 4247762216;
const TARGET_ID = "session-78d0f149-56e8-4aa7-afd3-e2801289f26d";

function checkHeader(h, id) {
  if (h.version !== 0) throw new Error("version != 0");
  if (h.id !== id) throw new Error("id mismatch");
  if (typeof h.createdAt !== "number" || !Number.isSafeInteger(h.createdAt) || h.createdAt < 0) throw new Error("bad createdAt");
  if (h.cwd !== undefined && (typeof h.cwd !== "string" || !isAbsolute(h.cwd))) throw new Error(`cwd not absolute: ${h.cwd}`);
  if (h.parentSession !== undefined && typeof h.parentSession !== "string") throw new Error("bad parentSession");
  if (h.origin !== undefined && h.origin !== "subagent") throw new Error("bad origin");
  if (h.delegationDepth !== undefined && (typeof h.delegationDepth !== "number" || !Number.isSafeInteger(h.delegationDepth) || h.delegationDepth < 0)) throw new Error("bad delegationDepth");
  if (h.agentPreset !== undefined && typeof h.agentPreset !== "string") throw new Error("bad agentPreset");
  return true;
}

let found = 0, problems = 0, targetOk = false;
for (const project of readdirSync(ROOT, { withFileTypes: true })) {
  if (!project.isDirectory()) {
    if (project.name.endsWith(".jsonl") || project.name.endsWith(".jsonl.zstd")) { console.log("PROBLEM legacy file in root:", project.name); problems++; }
    continue;
  }
  for (const sess of readdirSync(join(ROOT, project.name), { withFileTypes: true })) {
    if (!sess.isDirectory()) {
      if (sess.name.endsWith(".jsonl") || sess.name.endsWith(".jsonl.zstd")) { console.log(`PROBLEM legacy file in ${project.name}:`, sess.name); problems++; }
      continue;
    }
    for (const art of readdirSync(join(ROOT, project.name, sess.name))) {
      if (art.endsWith(".jsonl")) { console.log(`PROBLEM encoding mismatch: plaintext .jsonl in ${project.name}/${sess.name}`); problems++; }
      if (!art.endsWith(".jsonl") && !art.endsWith(".jsonl.zstd")) continue;
      const buf = readFileSync(join(ROOT, project.name, sess.name, art));
      const h = JSON.parse(zstdDecompressSync(buf.subarray(0, 4096)).toString("utf8").split("\n")[0]);
      const ok = checkHeader(h, h.id);
      found++;
      if (h.id === TARGET_ID) { targetOk = ok; console.log(`>>> IMPORTED (header valid): id=${h.id} cwd=${h.cwd} v=${h.version}`); }
      else console.log(`      ${h.id} cwd=${h.cwd} v=${h.version}`);
    }
  }
}
console.log(`\nsessions: ${found}; header problems: ${problems}; imported session header valid: ${targetOk}`);
if (!targetOk || problems > 0) process.exit(1);
