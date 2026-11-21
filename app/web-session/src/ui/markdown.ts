// A small Markdown reader for the agent's replies. It makes a tree of plain data, never HTML: the
// view draws the tree with React elements, so reply text cannot inject markup, and a link's address
// is checked here before it can become an href. It reads what replies use (paragraphs, headings,
// lists, quotes, fenced code, tables, rules, and code, bold, italic and links inside a line); anything
// else stays text. It also reads a reply that is still being written: an open code fence runs to the
// end, an unclosed `*` or backtick stays as typed.

export type Inline =
  | { t: "text"; text: string }
  | { t: "code"; text: string }
  | { t: "strong"; children: Inline[] }
  | { t: "em"; children: Inline[] }
  | { t: "link"; href: string; children: Inline[] };

export type Block =
  | { t: "paragraph"; children: Inline[] }
  | { t: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; children: Inline[] }
  | { t: "code"; lang: string; text: string; open: boolean }
  | { t: "list"; ordered: boolean; start: number; items: { children: Inline[]; blocks: Block[] }[] }
  | { t: "quote"; blocks: Block[] }
  | { t: "rule" }
  | { t: "table"; head: Inline[][]; align: ("left" | "right" | "center" | null)[]; rows: Inline[][][] };

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

/** The address of a link if it is safe to follow, else null (no `javascript:`, no `data:`, no relative links). */
export function safeHref(raw: string): string | null {
  const trimmed = raw.trim();
  try {
    const url = new URL(trimmed);
    return SAFE_PROTOCOLS.has(url.protocol) ? trimmed : null;
  } catch {
    return null;
  }
}

/** Each pattern runs only when the text holds what it needs (`needs`), so text with no closing mark costs nothing to scan. */
const INLINE_PATTERNS: { kind: "code" | "link" | "strong" | "em"; re: RegExp; needs: string[] }[] = [
  { kind: "code", re: /(?<!`)(`+)(?!`)([^\n]*?[^`\n])\1(?!`)/, needs: ["`"] },
  { kind: "link", re: /\[([^\]\n]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/, needs: ["]("] },
  { kind: "strong", re: /\*\*([^*\n]+?)\*\*|__([^_\n]+?)__/, needs: ["**", "__"] },
  { kind: "em", re: /\*([^*\s][^*\n]*?)\*|(?<![\w])_([^_\s][^_\n]*?)_(?![\w])/, needs: ["*", "_"] },
];

/**
 * How much of one run of text is read for formatting. Finding the next mark scans what is left, so a
 * hostile run of marks would cost the square of its length; what is past this many characters stays text.
 */
export const INLINE_READ_MAX = 6_000;
/** How deep formatting inside formatting is read (bold in a link), and how deep quotes and lists nest. */
export const MAX_DEPTH = 8;

/** Read the formatting inside one line of text. */
export function parseInline(text: string, depth = 0): Inline[] {
  const out: Inline[] = [];
  if (depth >= MAX_DEPTH) return text === "" ? [] : [{ t: "text", text }];
  let rest = text.length > INLINE_READ_MAX ? text.slice(0, INLINE_READ_MAX) : text;
  const beyond = text.length > INLINE_READ_MAX ? text.slice(INLINE_READ_MAX) : "";
  const pushText = (value: string) => {
    if (value === "") return;
    const last = out[out.length - 1];
    if (last?.t === "text") last.text += value;
    else out.push({ t: "text", text: value });
  };
  while (rest !== "") {
    let best: { kind: (typeof INLINE_PATTERNS)[number]["kind"]; match: RegExpExecArray } | null = null;
    for (const { kind, re, needs } of INLINE_PATTERNS) {
      if (!needs.some((mark) => rest.includes(mark))) continue;
      const match = re.exec(rest);
      if (match !== null && (best === null || match.index < best.match.index)) best = { kind, match };
    }
    if (best === null) {
      pushText(rest);
      break;
    }
    const { kind, match } = best;
    pushText(rest.slice(0, match.index));
    rest = rest.slice(match.index + match[0].length);
    if (kind === "code") {
      // A run of backticks closes with a run as long, so a span can hold a single backtick; one space
      // each side is the way to write that and is not part of the code.
      const inner = match[2] ?? "";
      out.push({ t: "code", text: /^ .*[^ ].* $|^ [^ ] $/.test(inner) ? inner.slice(1, -1) : inner });
    }
    else if (kind === "link") {
      const href = safeHref(match[2] ?? "");
      // An address that is not safe leaves the words, not a dead link.
      if (href === null) pushText(match[1] ?? "");
      else out.push({ t: "link", href, children: parseInline(match[1] ?? "", depth + 1) });
    } else if (kind === "strong") out.push({ t: "strong", children: parseInline(match[1] ?? match[2] ?? "", depth + 1) });
    else out.push({ t: "em", children: parseInline(match[1] ?? match[2] ?? "", depth + 1) });
  }
  pushText(beyond);
  return out;
}

const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)[^\n]*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const LIST = /^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

function splitRow(line: string): string[] {
  let body = line.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
  return body.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function alignOf(cell: string): "left" | "right" | "center" | null {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  return left ? "left" : null;
}

const indentOf = (line: string): number => line.length - line.trimStart().length;

/** Read a Markdown text into blocks. */
export function parseMarkdown(text: string): Block[] {
  return parseLines(text.replace(/\r\n?/g, "\n").split("\n"), 0);
}

function startsBlock(line: string, next: string | undefined): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    LIST.test(line) ||
    (line.includes("|") && next !== undefined && next.includes("-") && TABLE_SEPARATOR.test(next) && next.includes("|"))
  );
}

function parseLines(lines: string[], depth: number): Block[] {
  // Quotes and lists inside quotes and lists go this deep and no further: what is left is plain text, so a
  // reply of ten thousand ">" cannot overflow the stack that reads it.
  if (depth >= MAX_DEPTH) return lines.some((line) => line.trim() !== "") ? [{ t: "paragraph", children: [{ t: "text", text: lines.join("\n") }] }] : [];
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (line.trim() === "") {
      i++;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence !== null) {
      const marker = fence[1] as string;
      const body: string[] = [];
      i++;
      let closed = false;
      while (i < lines.length) {
        const current = lines[i] as string;
        if (new RegExp(`^\\s{0,3}${marker[0] === "`" ? "`" : "~"}{${marker.length},}\\s*$`).test(current)) {
          closed = true;
          i++;
          break;
        }
        body.push(current);
        i++;
      }
      blocks.push({ t: "code", lang: fence[2] ?? "", text: body.join("\n"), open: !closed });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading !== null) {
      blocks.push({ t: "heading", level: (heading[1] as string).length as 1, children: parseInline(heading[2] ?? "") });
      i++;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ t: "rule" });
      i++;
      continue;
    }
    if (QUOTE.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i] as string)) {
        inner.push((QUOTE.exec(lines[i] as string) as RegExpExecArray)[1] ?? "");
        i++;
      }
      blocks.push({ t: "quote", blocks: parseLines(inner, depth + 1) });
      continue;
    }
    const next = lines[i + 1];
    if (line.includes("|") && next !== undefined && next.includes("|") && TABLE_SEPARATOR.test(next)) {
      const head = splitRow(line);
      const align = splitRow(next).map(alignOf);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && (lines[i] as string).includes("|") && (lines[i] as string).trim() !== "") {
        rows.push(splitRow(lines[i] as string));
        i++;
      }
      blocks.push({
        t: "table",
        head: head.map((cell) => parseInline(cell)),
        align,
        rows: rows.map((row) => head.map((_, c) => parseInline(row[c] ?? ""))),
      });
      continue;
    }
    const listStart = LIST.exec(line);
    if (listStart !== null) {
      const base = (listStart[1] as string).length;
      const ordered = /\d/.test(listStart[2] as string);
      const start = ordered ? Number.parseInt(listStart[2] as string, 10) : 1;
      const items: { children: Inline[]; blocks: Block[] }[] = [];
      while (i < lines.length) {
        const current = lines[i] as string;
        const match = LIST.exec(current);
        if (match === null || (match[1] as string).length !== base || /\d/.test(match[2] as string) !== ordered) break;
        const first = match[4] ?? "";
        // Where the item's own text starts: what its continuation lines are indented by (a marker and one to four spaces).
        const content = base + (match[2] as string).length + Math.min((match[3] as string).length, 4);
        i++;
        // What belongs to the item: lines indented deeper than its marker, and blank lines between them.
        const inner: string[] = [];
        while (i < lines.length) {
          const following = lines[i] as string;
          if (following.trim() === "") {
            const after = lines[i + 1];
            if (after !== undefined && after.trim() !== "" && indentOf(after) > base) {
              inner.push("");
              i++;
              continue;
            }
            break;
          }
          if (indentOf(following) <= base) break;
          inner.push(following.slice(Math.min(indentOf(following), content)));
          i++;
        }
        items.push({ children: parseInline(first), blocks: parseLines(inner, depth + 1) });
      }
      blocks.push({ t: "list", ordered, start, items });
      continue;
    }
    // A paragraph runs to a blank line or the start of another block.
    const para: string[] = [line.trim()];
    i++;
    while (i < lines.length && (lines[i] as string).trim() !== "" && !startsBlock(lines[i] as string, lines[i + 1])) {
      para.push((lines[i] as string).trim());
      i++;
    }
    blocks.push({ t: "paragraph", children: parseInline(para.join(" ")) });
  }
  return blocks;
}
