// The source hash written to build-info.json. tests/test_webapp_session_view.py
// recomputes it in Python with the same rule, so change both together:
//
//   1. Walk app/web-session. Skip any file or folder whose name starts with
//      "." or is "node_modules", and any file ending in ".md" or
//      ".tsbuildinfo".
//   2. For each file left, take its path relative to app/web-session with
//      "/" separators, and the sha256 hex of its raw bytes.
//   3. Sort by path (plain code-unit order), join "<path>\0<sha256>\n" lines,
//      and take the sha256 hex of that UTF-8 text.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const skipName = (name) => name.startsWith(".") || name === "node_modules";
const skipFile = (name) => name.endsWith(".md") || name.endsWith(".tsbuildinfo");

/** Every hashed file under `root`: relative path to sha256 hex. */
export function sourceFiles(root) {
  const out = {};
  const walk = (dir, prefix) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (skipName(entry.name)) continue;
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      const full = join(dir, entry.name);
      if (entry.isDirectory()) walk(full, rel);
      else if (entry.isFile() && !skipFile(entry.name)) {
        out[rel] = createHash("sha256").update(readFileSync(full)).digest("hex");
      }
    }
  };
  walk(root, "");
  return Object.fromEntries(Object.keys(out).sort().map((k) => [k, out[k]]));
}

/** The hash over `files` (the output of sourceFiles). */
export function sourceHash(files) {
  const lines = Object.keys(files)
    .sort()
    .map((rel) => `${rel}\0${files[rel]}\n`)
    .join("");
  return createHash("sha256").update(lines, "utf8").digest("hex");
}
