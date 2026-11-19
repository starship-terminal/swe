// pnpm lint:tokens: the session view styles only through SWE's tokens.
//
// Fails on
//   - an arbitrary Tailwind value or property (`bg-[#171717]`, `w-[13px]`,
//     `[color:red]`): add a token to tokens.css and map it in src/index.css;
//   - a colour literal in a real style value: a CSS declaration, a JSX
//     `style={{...}}` object, an HTML `style="..."` attribute or an SVG
//     colour attribute. Hex, colour functions (rgb(), hsl(), oklch(), ...)
//     and named colours (red, white, ...) all count; colours come from
//     tokens.css. Text and comments that merely mention one do not;
//   - ALL CAPS text (`uppercase`, `text-transform: uppercase`);
//   - the mono font anywhere but a code, pre, kbd or samp element: a
//     `font-mono` class must sit on such an element's opening tag, and a
//     mono `font-family` must sit in a rule whose selector names one.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const MESSAGES = {
  arbitrary: "arbitrary Tailwind value; map a token in src/index.css instead",
  colour: "colour literal; use a token from tokens.css",
  caps: "ALL CAPS text; SWE uses sentence case",
  mono: "mono font outside code; only code, pre, kbd and samp may use it",
};

const ARBITRARY =
  /(?:^|[\s"'`{(])!?(?:[\w-]+:)*-?[a-z][\w-]*-\[[^\]\s]*\]|(?:^|[\s"'`])!?\[[a-z-]+:[^\]\s]+\]/;
const CODE_TAGS = new Set(["code", "pre", "kbd", "samp"]);
const CODE_SELECTOR = /(?:^|[\s,(>+~])(?:code|pre|kbd|samp)\b/;

// CSS named colours (CSS Color 4), besides the keywords that carry no colour
// of their own (transparent, currentColor, inherit and the like).
const NAMED_COLOURS = new Set(
  (
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet " +
    "brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan " +
    "darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta " +
    "darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue " +
    "darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey " +
    "dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray " +
    "green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush " +
    "lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray " +
    "lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray " +
    "lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon " +
    "mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue " +
    "mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin " +
    "navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen " +
    "paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple " +
    "rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna " +
    "silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle " +
    "tomato turquoise violet wheat white whitesmoke yellow yellowgreen"
  ).split(" "),
);
const HEX = /#[0-9a-fA-F]{3,8}(?![\w-])/;
const COLOUR_FN = /\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/i;
// Properties whose value is (or carries) a colour: named colours are
// checked only here, so a font or animation called "Red" is not a colour.
const COLOUR_PROPERTY =
  /^(?:color|background(?:-color)?|border(?:-(?:top|right|bottom|left|block|inline)(?:-(?:start|end))?)?(?:-color)?|outline(?:-color)?|fill|stroke|caret-color|accent-color|text-decoration(?:-color)?|text-emphasis(?:-color)?|column-rule(?:-color)?|box-shadow|text-shadow|stop-color|flood-color|lighting-color|scrollbar-color|--color-[\w-]*)$/i;
const SVG_COLOUR_ATTR = /\b(fill|stroke|color|stopColor|floodColor|lightingColor)=(?:"([^"]*)"|'([^']*)'|\{\s*["'`]([^"'`]*)["'`]\s*\})/g;

/** Whether a declaration's value names a colour other than a token. */
function colourLiteral(prop, value) {
  if (HEX.test(value) || COLOUR_FN.test(value)) return true;
  if (!COLOUR_PROPERTY.test(prop)) return false;
  const words = value.toLowerCase().match(/[a-z]+/g) ?? [];
  // var(--name) never counts: only the words outside var() do.
  const bare = value.toLowerCase().replace(/var\([^)]*\)/g, " ");
  return words.some((w) => NAMED_COLOURS.has(w) && new RegExp(`(?<![\\w-])${w}(?![\\w-])`).test(bare));
}

const isMonoValue = (value) => /var\(--font-mono\)|\bmonospace\b|ui-monospace/.test(value);

/** Replace every comment with spaces, keeping offsets and line breaks. */
function blankComments(text, kind) {
  const re = kind === "css" ? /\/\*[\s\S]*?\*\//g : /\/\*[\s\S]*?\*\/|\/\/[^\n]*|<!--[\s\S]*?-->/g;
  return text.replace(re, (m) => m.replace(/[^\n]/g, " "));
}

/** Every declaration in a stylesheet, with the selector of its innermost
 * style rule (at-rules such as @media and @layer are looked through). */
export function cssDeclarations(text) {
  const css = blankComments(text, "css");
  const out = [];
  const stack = [];
  let start = 0;
  let quote = "";
  let paren = 0;
  const flush = (end) => {
    const raw = css.slice(start, end);
    const body = raw.trim();
    const at = start + raw.indexOf(body);
    start = end + 1;
    const selector = [...stack].reverse().find((s) => !s.startsWith("@"));
    if (!body || stack.length === 0) return;
    if (body.startsWith("@apply")) {
      out.push({ prop: "@apply", value: body.slice(6).trim(), selector: selector ?? "", offset: at });
      return;
    }
    const colon = body.indexOf(":");
    if (colon <= 0) return;
    out.push({
      prop: body.slice(0, colon).trim(),
      value: body.slice(colon + 1).trim(),
      selector: selector ?? "",
      offset: at,
    });
  };
  for (let i = 0; i < css.length; i++) {
    const ch = css[i];
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === quote) quote = "";
      continue;
    }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === "(") paren++;
    else if (ch === ")") paren = Math.max(0, paren - 1);
    else if (paren > 0) continue;
    else if (ch === "{") {
      stack.push(css.slice(start, i).trim());
      start = i + 1;
    } else if (ch === ";") flush(i);
    else if (ch === "}") {
      flush(i);
      stack.pop();
    }
  }
  return out;
}

/** The opening tag (name, start offset) that `offset` sits inside, if any:
 * the last `<name` before it whose `>` has not come yet, outside quotes and
 * braces. Works across line breaks. */
export function enclosingTag(text, offset) {
  const re = /<([A-Za-z][\w.:-]*)/g;
  let found = null;
  for (let m = re.exec(text); m && m.index < offset; m = re.exec(text)) found = m;
  if (!found) return null;
  let quote = "";
  let depth = 0;
  for (let i = found.index + found[0].length; i < offset; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === quote) quote = "";
    } else if (ch === '"' || ch === "'" || ch === "`") quote = ch;
    else if (ch === "{") depth++;
    else if (ch === "}") depth = Math.max(0, depth - 1);
    else if (ch === ">" && depth === 0) return null;
  }
  return { name: found[1], offset: found.index };
}

const lineOf = (text, offset) => text.slice(0, offset).split("\n").length;
const lineText = (text, offset) => text.split("\n")[lineOf(text, offset) - 1]?.trim() ?? "";

function lintCss(text, file) {
  const found = [];
  const report = (offset, key) => found.push(`${file}:${lineOf(text, offset)}: ${MESSAGES[key]}: ${lineText(text, offset)}`);
  for (const d of cssDeclarations(text)) {
    if (d.prop === "@apply") {
      if (ARBITRARY.test(` ${d.value}`)) report(d.offset, "arbitrary");
      if (/\buppercase\b/.test(d.value)) report(d.offset, "caps");
      if (/\bfont-mono\b/.test(d.value) && !CODE_SELECTOR.test(d.selector)) report(d.offset, "mono");
      continue;
    }
    if (colourLiteral(d.prop, d.value)) report(d.offset, "colour");
    if (d.prop === "text-transform" && /\buppercase\b/.test(d.value)) report(d.offset, "caps");
    const fontProp = d.prop === "font-family" || d.prop === "font";
    if (fontProp && isMonoValue(d.value) && !CODE_SELECTOR.test(d.selector)) report(d.offset, "mono");
  }
  return found;
}

/** An inline style object's entries: `{ color: "red", fontFamily: ... }`. */
function styleEntries(object) {
  const out = [];
  const re = /([A-Za-z_$][\w$]*|"[^"]+"|'[^']+')\s*:\s*("[^"]*"|'[^']*'|`[^`]*`|[^,}]+)/g;
  for (let m = re.exec(object); m; m = re.exec(object)) {
    const key = m[1].replace(/^["']|["']$/g, "");
    const prop = key.startsWith("--") ? key : key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
    out.push({ prop, value: m[2].trim().replace(/^["'`]|["'`]$/g, ""), at: m.index });
  }
  return out;
}

function lintMarkup(text, file) {
  const src = blankComments(text, "markup");
  const found = [];
  const report = (offset, key) => found.push(`${file}:${lineOf(text, offset)}: ${MESSAGES[key]}: ${lineText(text, offset)}`);
  src.split("\n").forEach((line, i) => {
    if (ARBITRARY.test(line)) {
      found.push(`${file}:${i + 1}: ${MESSAGES.arbitrary}: ${line.trim()}`);
    }
  });
  // A class on a tag; the word in running text is fine, and a style
  // object's textTransform is checked with the other inline styles.
  for (const m of src.matchAll(/\buppercase\b/g)) {
    const tag = enclosingTag(src, m.index);
    const attr = tag && [...src.slice(tag.offset, m.index).matchAll(/([\w-]+)=/g)].at(-1)?.[1];
    if (attr === "className" || attr === "class") report(m.index, "caps");
  }
  for (const m of src.matchAll(/\bfont-mono\b/g)) {
    const tag = enclosingTag(src, m.index);
    if (!tag || !CODE_TAGS.has(tag.name)) report(m.index, "mono");
  }
  // Inline styles: JSX objects and HTML attributes.
  const styles = [
    ...[...src.matchAll(/style=\{\{([\s\S]*?)\}\}/g)].map((m) => ({ body: m[1], at: m.index + 8 })),
    ...[...src.matchAll(/style="([^"]*)"/g)].map((m) => ({ body: m[1].replace(/;/g, ","), at: m.index + 7 })),
  ];
  for (const { body, at } of styles) {
    for (const e of styleEntries(body)) {
      const offset = at + e.at;
      if (colourLiteral(e.prop, e.value)) report(offset, "colour");
      if (e.prop === "text-transform" && /uppercase/.test(e.value)) report(offset, "caps");
      if ((e.prop === "font-family" || e.prop === "font") && isMonoValue(e.value)) {
        const tag = enclosingTag(src, offset);
        if (!tag || !CODE_TAGS.has(tag.name)) report(offset, "mono");
      }
    }
  }
  for (const m of src.matchAll(SVG_COLOUR_ATTR)) {
    const value = m[2] ?? m[3] ?? m[4] ?? "";
    if (colourLiteral("color", value)) report(m.index, "colour");
  }
  return found;
}

/** Every rule a file's text breaks, as `file:line: message` strings. */
export function lintText(text, file) {
  return file.endsWith(".css") ? lintCss(text, file) : lintMarkup(text, file);
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
