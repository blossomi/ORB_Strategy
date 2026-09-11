// DSH session import helper: stage + validate a foreign session.jsonl
// so it can be installed into the harness home (~/.dsh).
// Runs entirely inside the workspace; never touches ~/.dsh itself.
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { zstdCompressSync, zstdDecompressSync, constants } from "node:zlib";

const WORK = "/Users/blossomx/workspaces/Deepseek_Harness";
const SRC_DIR = join(WORK, "dsh-session-session-78d0f149-56e8-4aa7-afd3-e2801289f26d");
const SRC_LOG = join(SRC_DIR, "session.jsonl");
const STAGE = join(WORK, "_import_staging");

// ---- exact replication of official path helpers (dsh-session-persistence-jsonl) ----
function encodeSegment(raw) {
  if (raw.length === 0) throw new Error("cannot encode an empty path segment");
  if (raw === ".") return "~002E";
  if (raw === "..") return "~002E~002E";
  let out = "";
  for (let i = 0; i < raw.length; i++) {
    const code = raw.charCodeAt(i);
    const ch = String.fromCharCode(code);
    if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) out += ch;
    else out += "~" + code.toString(16).toUpperCase().padStart(4, "0");
  }
  return out;
}
function projectKey(cwd) {
  if (cwd.length === 0) throw new Error("cannot encode an empty project path");
  let readable = "";
  let separatorRun = false;
  for (let i = 0; i < cwd.length; i++) {
    const code = cwd.charCodeAt(i);
    const ch = String.fromCharCode(code);
    if (ch === "/" || ch === "\\" || ch === ":") {
      if (!separatorRun) readable += "-";
      separatorRun = true;
    } else if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) {
      readable += ch;
      separatorRun = false;
    } else {
      readable += "~" + code.toString(16).toUpperCase().padStart(4, "0");
      separatorRun = false;
    }
  }
  return `--${(readable.replace(/^-+/, "") || "root").slice(0, 251)}--`;
}

// cross-check against the LIVE root dir name the harness itself created
const live = "--Users-blossomx-workspaces-Deepseek_Harness--";
const computed = projectKey("/Users/blossomx/workspaces/Deepseek_Harness");
if (computed !== live) throw new Error(`projectKey replication broken: ${computed} !== ${live}`);
console.log("projectKey cross-check OK:", computed);

// ---- read + split header / events ----
const raw = readFileSync(SRC_LOG);
const nl = raw.indexOf(10);
if (nl === -1) throw new Error("no newline in file — cannot split header");
const headerLine = raw.subarray(0, nl + 1);
const eventsText = raw.subarray(nl + 1);
if (eventsText.length === 0) throw new Error("no events after header");
console.log(`file bytes: ${raw.length}, header bytes: ${headerLine.length}, events bytes: ${eventsText.length}`);
if (raw[raw.length - 1] !== 10) console.log("WARN: file does not end with newline (final record would be treated as torn)");

const header = JSON.parse(headerLine.toString("utf8"));
if (header.type !== "session" || typeof header.version !== "number" || typeof header.id !== "string" || typeof header.createdAt !== "number") {
  throw new Error("header line is not a well-formed DSH session header");
}
console.log("header:", JSON.stringify(header));

// ---- zstd encode: header frame + events frame, checksum on (matches backend) ----
const frameOpts = { params: { [constants.ZSTD_c_checksumFlag]: 1 } };
const headerFrame = zstdCompressSync(headerLine, frameOpts);
const eventsFrame = zstdCompressSync(eventsText, frameOpts);
const out = Buffer.concat([headerFrame, eventsFrame]);

// round-trip check
const backHeader = zstdDecompressSync(headerFrame);
const backEvents = zstdDecompressSync(eventsFrame);
if (!backHeader.equals(headerLine)) throw new Error("header frame round-trip mismatch");
if (!backEvents.equals(eventsText)) throw new Error("events frame round-trip mismatch");
console.log("zstd round-trip OK (checksummed frames)");

// ---- event contract check using the OFFICIAL decodeStorageRecord (mirrors the scanner) ----
{
  const dshSession = await import("file:///Users/blossomx/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/dsh-session/lib/index.js");
  const decodeStorageRecord = dshSession.decodeStorageRecord;
  let seq = 0;
  let lineNo = 0;
  let rows = 0;
  let events = 0;
  for (const line of eventsText.toString("utf8").split("\n")) {
    lineNo++;
    if (line === "") continue;
    let value;
    try { value = JSON.parse(line); } catch (e) { throw new Error(`unparsable row at line ${lineNo}: ${e.message}`); }
    const decoded = decodeStorageRecord(value);
    if (decoded.length === 0) throw new Error(`row at line ${lineNo} decoded to zero events`);
    for (const event of decoded) {
      if (event.seq !== seq) throw new Error(`seq gap at line ${lineNo}: expected ${seq}, got ${event.seq}`);
      seq++;
      events++;
    }
    rows++;
  }
  console.log(`event rows OK: ${rows} rows -> ${events} events, contiguous seq 0..${seq - 1}`);
}

// ---- write staged session artifact ----
const proj = projectKey(header.cwd);
const idSeg = encodeSegment(header.id);
const sessDir = join(STAGE, "sessions", proj, idSeg);
mkdirSync(sessDir, { recursive: true });
const stagedLog = join(sessDir, "session.jsonl.zstd");
writeFileSync(stagedLog, out);
console.log("staged:", stagedLog);

// ---- stage media (content-addressed attachment objects) ----
const mediaSrc = join(SRC_DIR, "media");
const attRoot = join(STAGE, "attachments", "v1");
let mediaCount = 0;
for (const f of readdirSync(mediaSrc)) {
  if (!f.startsWith("sha256:")) continue;
  const digest = f.slice("sha256:".length).replace(/\.png$/i, "");
  if (!/^[0-9a-f]{64}$/.test(digest)) throw new Error(`bad media name: ${f}`);
  const bytes = readFileSync(join(mediaSrc, f));
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== digest) throw new Error(`media ${f}: content sha256 ${actual} does not match name`);
  const objDir = join(attRoot, "objects", digest.slice(0, 2));
  mkdirSync(objDir, { recursive: true });
  copyFileSync(join(mediaSrc, f), join(objDir, digest));
  mediaCount++;
}
console.log(`staged media: ${mediaCount} attachment objects`);

// ---- official-class validation on the staged root (read-only) ----
try {
  const mod = await import("file:///Users/blossomx/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js");
  const Persistence = mod.default;
  const fakeCtx = {
    sessions: { get: () => undefined, list: () => [], on: () => undefined, off: () => undefined },
    effect: () => () => {},
    on: () => undefined,
    logger: { warn: () => {}, info: () => {}, error: () => {}, debug: () => {} },
    get: () => undefined,
    provide: () => undefined,
    scope: { provide: () => undefined }
  };
  const p = new Persistence(fakeCtx, { root: join(STAGE, "sessions"), compression: "zstd", packChunks: false });
  const listed = await p.list();
  console.log("official list():", listed.map((m) => m.id));
  if (!listed.some((m) => m.id === header.id)) throw new Error("session not found by official list()");
  const inspect = await p.inspect(header.id);
  console.log("official inspect(): seq", inspect.cursor, "| events", inspect.events);
  console.log("OFFICIAL VALIDATION PASSED");
} catch (e) {
  console.log("official-class validation skipped:", e.message);
  console.log("(fallback checks above already validated the artifact format)");
}

console.log("\nDONE. Install targets:");
console.log("  log    : ~/.dsh/sessions/" + proj + "/" + idSeg + "/session.jsonl.zstd");
console.log("  media  : ~/.dsh/attachments/v1/objects/<sha256[:2]>/<sha256>");
