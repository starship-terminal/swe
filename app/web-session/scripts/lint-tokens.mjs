// pnpm lint:tokens: the session view styles only through SWE's tokens.
//
// Fails on
//   - an arbitrary Tailwind value or property (`bg-[#171717]`, `w-[13px]`,
//     `[color:red]`): add a token to tokens.css and map it in src/index.css;
//   - a colour literal (#hex, rgb(), hsl(), oklch(), ...) in a stylesheet or
//     component: colours come from tokens.css;
//   - ALL CAPS text (`uppercase`, `text-transform: uppercase`);
//   - the mono font anywhere but a code, pre, kbd or samp element.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const RULES = [
  {
    // `name-[...]` with an optional variant prefix, and `[prop:value]`.
    test: /(?:^|[\s"'`{(])!?(?:[\w-]+:)*-?[a-z][\w-]*-\[[^\]\s]*\]|(?:^|[\s"'`])!?\[[a-z-]+:[^\]\s]+\]/,
    message: "arbitrary Tailwind value; map a token in src/index.css instead",
  },
  {
    test: /#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?|oklch|oklab|lab|lch|color)\(/,
    message: "colour literal; use a token from tokens.css",
    skip: (line) => /^\s*(?:\/\/|\*|\/\*)/.test(line),
  },
  {
    test: /\buppercase\b/,
    message: "ALL CAPS text; SWE uses sentence case",
  },
  {
    test: /\bfont-mono\b|var\(--font-mono\)/,
    message: "mono font outside code; only code, pre, kbd and samp may use it",
    // A JSX code element, a CSS rule whose selector names one, or the token.
    skip: (line) => /<(?:code|pre|kbd|samp)\b|\b(?:code|pre|kbd|samp)\b[^{}]*\{|--font-mono:/.test(line),
  },
];

/** Every rule a file's text breaks, as `file:line: message` strings. */
export function lintText(text, file) {
  const found = [];
  text.split("\n").forEach((line, i) => {
    for (const rule of RULES) {
      if (rule.skip?.(line)) continue;
      if (rule.test.test(line)) found.push(`${file}:${i + 1}: ${rule.message}: ${line.trim()}`);
    }
  });
  return found;
}

const LINTED = /\.(?:tsx?|css|html)$/;

function walk(dir, out) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".") || entry.name === "node_modules") continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (LINTED.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** Lint src/ and index.html under `root`; returns the problems found. */
export function lintTree(root) {
  const files = [...walk(join(root, "src"), []), join(root, "index.html")];
  return files.flatMap((file) => lintText(readFileSync(file, "utf8"), relative(root, file)));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const problems = lintTree(root);
  for (const p of problems) console.error(p);
  if (problems.length) {
    console.error(`lint:tokens: ${problems.length} problem(s)`);
    process.exit(1);
  }
  console.log("lint:tokens: clean");
}
