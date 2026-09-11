// Fix the imported session header cwd: Windows path is not absolute on macOS.
// Rewrites ONLY frame 1 (the header line); all event frames stay byte-identical.
// Then fully validates: whole-file frame scan, full round-trip to text,
// official header checks (isAbsolute etc.), and event seq contiguity.
import { readFileSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { isAbsolute } from "node:path";
import { zstdCompressSync, zstdDecompressSync, constants } from "node:zlib";

const STAGE = "/Users/blossomx/workspaces/Deepseek_Harness/_import_staging";
const NEW_CWD = "/Users/blossomx/workspaces/Deepseek_Harness";
const OLD_PROJ = "--C-Users-karajan-Documents-qt--";
const NEW_PROJ = "--Users-blossomx-workspaces-Deepseek_Harness--";
const ID = "session-78d0f149-56e8-4aa7-afd3-e2801289f26d";

const srcDir = join(STAGE, "sessions", OLD_PROJ, ID);
const dstDir = join(STAGE, "sessions", NEW_PROJ, ID);
const srcPath = join(srcDir, "session.jsonl.zstd");
const dstPath = join(dstDir, "session.jsonl.zstd");

const ZSTD_MAGIC = 4247762216;

// ---- frame scan (replicates the official scanZstdFrames) ----
function scanFrames(buffer) {
  const frames = [];
  let offset = 0;
  let tornStart;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) { tornStart = start; break; }
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) throw new Error(`bad frame magic at ${offset}`);
    offset += 4;
    if (offset === buffer.length) { tornStart = start; break; }
    const descriptor = buffer.readUInt8(offset);
    offset += 1;
    if ((descriptor & 24) !== 0) throw new Error(`reserved frame-header bit at ${offset - 1}`);
    const contentSizeFlag = descriptor >>> 6;
    const singleSegment = (descriptor & 32) !== 0;
    const checksum = (descriptor & 4) !== 0;
    const dictionaryFlag = descriptor & 3;
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag;
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : 1 << contentSizeFlag;
    const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes;
    if (buffer.length - offset < remainingHeaderBytes) { tornStart = start; break; }
    offset += remainingHeaderBytes;
    for (;;) {
      if (buffer.length - offset < 3) { tornStart = start; break; }
      const blockHeader = buffer.readUIntLE(offset, 3);
      offset += 3;
      const lastBlock = (blockHeader & 1) !== 0;
      const blockType = blockHeader >>> 1 & 3;
      const blockSize = blockHeader >>> 3;
      if (blockType === 3) throw new Error(`reserved block type at ${offset - 3}`);
      const payloadBytes = blockType === 1 ? 1 : blockSize;
      if (buffer.length - offset < payloadBytes) { tornStart = start; break; }
      offset += payloadBytes;
      if (lastBlock) break;
    }
    if (checksum) {
      if (buffer.length - offset < 4) { tornStart = start; break; }
      offset += 4;
    }
    frames.push({ start, end: offset });
  }
  return { frames, tornStart };
}

const buf = readFileSync(srcPath);
const { frames, tornStart } = scanFrames(buf);
if (tornStart !== undefined) throw new Error(`torn tail frame at byte ${tornStart} — export appears truncated`);
console.log(`frames: ${frames.length} (header + ${frames.length - 1} event frames), all complete`);

// ---- rewrite frame 1 header ----
const headerFrame = buf.subarray(frames[0].start, frames[0].end);
const headerText = zstdDecompressSync(headerFrame).toString("utf8");
if (!headerText.endsWith("\n")) throw new Error("header frame is not one newline-terminated line");
const headerJsonText = headerText.slice(0, -1);
const header = JSON.parse(headerJsonText);
if (header.id !== ID) throw new Error(`header id ${header.id} !== ${ID}`);
const oldCwdJson = JSON.stringify(header.cwd);
const newCwdJson = JSON.stringify(NEW_CWD);
const newHeaderText = headerJsonText.replace(oldCwdJson, newCwdJson);
if (newHeaderText === headerJsonText) throw new Error("cwd replacement did not change the header");
const fixedHeader = JSON.parse(newHeaderText);
if (!isAbsolute(fixedHeader.cwd)) throw new Error(`new cwd still not absolute: ${fixedHeader.cwd}`);

const frameOpts = { params: { [constants.ZSTD_c_checksumFlag]: 1 } };
const newHeaderFrame = zstdCompressSync(newHeaderText + "\n", frameOpts);
const tailFrames = buf.subarray(frames[1].start);
const rebuilt = Buffer.concat([newHeaderFrame, tailFrames]);
mkdirSync(dstDir, { recursive: true });
writeFileSync(dstPath, rebuilt);
console.log("rewrote header cwd ->", fixedHeader.cwd, "at", dstPath);

// ---- full round-trip: decompress everything, compare to original text with cwd swapped ----
const dshSession = await import("file:///Users/blossomx/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/dsh-session/lib/index.js");
const decodeStorageRecord = dshSession.decodeStorageRecord;
// Rebuild expected text from the ORIGINAL file's frames, then swap the cwd JSON.
let origAll = "";
for (const f of frames) origAll += zstdDecompressSync(buf.subarray(f.start, f.end)).toString("utf8");
const expected = origAll.replace(oldCwdJson, newCwdJson);
const rebuiltFrames = scanFrames(rebuilt);
let rebuiltAll = "";
for (const f of rebuiltFrames.frames) rebuiltAll += zstdDecompressSync(rebuilt.subarray(f.start, f.end)).toString("utf8");
if (rebuiltAll !== expected) throw new Error("round-trip mismatch: decompressed fixed file differs from expected text");
console.log("full round-trip OK: fixed file decompresses to original text with cwd swapped only");

// ---- event contract check on the fixed text (official decodeStorageRecord) ----
{
  let seq = 0, rows = 0, events = 0, lineNo = 0;
  const lines = rebuiltAll.split("\n");
  for (let i = 1; i < lines.length; i++) { // skip line 0: the session header
    const line = lines[i];
    lineNo = i + 1;
    if (line === "") continue;
    const value = JSON.parse(line);
    for (const event of decodeStorageRecord(value)) {
      if (event.seq !== seq) throw new Error(`seq gap at line ${lineNo}: expected ${seq}, got ${event.seq}`);
      seq++; events++;
    }
    rows++;
  }
  console.log(`event check OK: ${rows} rows -> ${events} events, seq 0..${seq - 1}`);
}

// ---- official header validation replication (validateSessionHeader) ----
{
  const h = fixedHeader;
  if (h.version !== 0) throw new Error("version != 0");
  if (h.id !== ID) throw new Error("id mismatch");
  if (typeof h.createdAt !== "number" || !Number.isSafeInteger(h.createdAt) || h.createdAt < 0) throw new Error("bad createdAt");
  if (h.cwd !== undefined) { if (typeof h.cwd !== "string" || !isAbsolute(h.cwd)) throw new Error("bad cwd"); }
  if (h.parentSession !== undefined && typeof h.parentSession !== "string") throw new Error("bad parentSession");
  if (h.origin !== undefined && h.origin !== "subagent") throw new Error("bad origin");
  if (h.delegationDepth !== undefined && (typeof h.delegationDepth !== "number" || !Number.isSafeInteger(h.delegationDepth) || h.delegationDepth < 0)) throw new Error("bad delegationDepth");
  if (h.agentPreset !== undefined && typeof h.agentPreset !== "string") throw new Error("bad agentPreset");
  console.log("official header validation checks OK");
}

// cleanup old staged project dir
rmSync(srcDir, { recursive: true, force: true });
try { rmSync(join(STAGE, "sessions", OLD_PROJ), { recursive: true, force: true }); } catch {}
console.log("\nDONE. staged fixed artifact:", dstPath);
