/* Sessions chat: remote control inside the Sessions page. Opening a
 * session row shows its transcript, live over the hub's SSE, with the
 * composer (Queue/Steer, Stop, file attachments), approval cards and
 * Attach. Ported from
 * the remote PWA's chat, which this file replaced: the same session
 * routes, the same ownership rules and the same event handling.
 *
 * Not a page of its own: sessions.js calls SessionChat.open(key) for
 * #/sessions?chat=<key>, so the sidebar keeps Sessions selected. The
 * bundle runs this file in its own scope; SessionChat is its one global.
 * SSE is read with fetch streams, since EventSource can't send the
 * Authorization header.
 */
"use strict";

const SessionChat = (() => {
  const chat = {
    key: null, gen: 0, row: null, rowKnown: false, details: null,
    stream: null, retryTimer: null, retries: 0,
    // Events held while a resync reads the transcript again, or null.
    held: null,
    // Live events not yet shown. The hub polls a busy transcript about
    // once a second, so several messages arrive together; they are shown
    // one at a time instead of as a block.
    pending: [], revealTimer: null, lastRevealAt: 0,
    // A send went through and nothing from the agent has come back yet.
    awaitingReply: false,
    // The last journal seq shown in this view; a dropped stream resumes
    // from here.
    seq: 0,
    // Replies still being written (message.delta, #1444): the bubbles'
    // state, the timer of the next repaint, and the box at the log's end
    // that holds them.
    replies: null, streamPaint: null, streamBox: null,
    // A repaint skipped while the tab was hidden or a batch landed, and
    // whether a batch is landing (flushPending).
    streamsStale: false, paintHeld: false,
    toolCards: new Map(), // call_id -> tool card
    // The scrollback cap (#1615): the oldest entries the log no longer
    // holds, oldest first, and how many entries it keeps drawn.
    earlier: [], keep: 0,
    // While set, the stream is replaying events the backfill already
    // showed, looking for one waiting approval (restoreApproval).
    history: null,
    confirmAttach: false, attaching: false, stopping: false,
    // A detach mode=when_idle the hub still holds: it fires when the
    // turn ends, or an attach cancels it.
    detaching: false,
    // The session rail (the Mac app's SessionRail): its own load state,
    // and a copy of the list's rows for when sessions.js's page state
    // isn't there (a chat opened from a link, the list never visited).
    rail: { loading: false, error: "" }, railData: null,
  };

  chat.replies = replyStreams();

  /* A mirrored session (#1918): the harness deleted its transcript and swe
   * kept a read-only copy. Same wording as the list's tag (sessions.js).
   * The copy may not be readable through the hub yet: then the log says so
   * calmly, with no error styling. */
  const MIRRORED_NOTE = "Its transcript was deleted by the harness. swe kept a read-only copy.";
  const MIRRORED_EMPTY = "This session's chat is not available yet.";
  const isMirrored = () => !!(chat.row && chat.row.mirrored);

  const STATUS = { active: "Active", done: "Done", followup: "Waiting", error: "Error",
    interrupted: "Interrupted", approval: "Approval needed" };
  const DECISIONS = { allow: "Allowed", always: "Always allowed", deny: "Denied" };

  const $ = (id) => document.getElementById(id);

  /* ------------------------------------------------------ live reply text
     message.delta (#1444, docs/json-contracts.md): live text for a reply
     still being written. A port of ReplyStreams.swift (the Mac's and the
     iPhone's copy); keep the three in step.
     - Deltas append per id in seq order. A gap, or a stream first heard
       mid-way, stops drawing that bubble, but it keeps its place in line
       so its final message still finds it.
     - A live message.appended claims the oldest open bubble of its kind
       (the transcript doesn't carry the channel's id). Deltas for a
       claimed id are dropped; the bubble stays drawn until the final is
       shown in its place (release), since the reveal queue may hold it.
     - done closes the id; the bubble still waits for its final. One that
       never comes goes when the session goes idle (sweep) or the stream
       reconnects (clear). */

  function replyStreams() {
    let bubbles = [];
    const ended = [];
    const end = (id) => {
      if (ended.includes(id)) return;
      ended.push(id);
      if (ended.length > 256) ended.splice(0, ended.length - 256);
    };
    // Whether a bubble is drawn. Each change below asks this of the one
    // bubble it touches, rather than comparing every bubble's text before
    // and after (#1555).
    const drawn = (b) => !b.broken && b.kind === "assistant" && !!b.text;
    const shown = () => bubbles.filter(drawn);
    return {
      shown,
      openIds: () => bubbles.filter((b) => !b.claimed).map((b) => b.id),
      /* One delta's data; whether what's drawn changed. */
      apply(d) {
        const id = d && typeof d.id === "string" ? d.id : "";
        const seq = d && Number.isInteger(d.seq) ? d.seq : 0;
        if (!id || ended.includes(id)) return false;
        const b = bubbles.find((x) => x.id === id);
        if (!b) {
          const nb = { id, kind: d.kind || "assistant", text: seq === 1 ? d.text || "" : "", seq,
            done: d.done === true, broken: seq !== 1, claimed: false };
          bubbles.push(nb);
          return drawn(nb);
        }
        if (d.done === true) b.done = true;
        if (b.broken) return false;
        if (seq !== b.seq + 1) {
          const was = drawn(b);
          b.broken = true;
          b.text = "";
          return was;
        }
        b.text += d.text || "";
        b.seq = seq;
        return !!d.text && drawn(b);
      },
      /* A live final of ``kind``: the id of the bubble it replaces, or "". */
      claim(kind) {
        const b = bubbles.find((x) => !x.claimed && x.kind === (kind || "assistant"));
        if (!b) return "";
        b.claimed = true;
        end(b.id);
        return b.id;
      },
      release(id) {
        const had = bubbles.some((b) => b.id === id && drawn(b));
        bubbles = bubbles.filter((b) => b.id !== id);
        return had;
      },
      /* The session went idle: of ``ids`` (open when it did), the ones no
       * final claimed go. */
      sweep(ids) {
        const gone = new Set(ids);
        const goes = (b) => gone.has(b.id) && !b.claimed;
        const had = bubbles.some((b) => goes(b) && drawn(b));
        for (const b of bubbles) if (goes(b)) end(b.id);
        bubbles = bubbles.filter((b) => !goes(b));
        return had;
      },
      clear() {
        const had = shown().length > 0;
        for (const b of bubbles) end(b.id);
        bubbles = [];
        return had;
      },
    };
  }

  /* ------------------------------------------------------------ requests */

  /* app.js's apiPost, with its authHeaders: the paired bearer when
   * there is one, and X-Swe-Pair, which lets the hub owner's own
   * tailnet devices in without pairing. status 0 means the hub never
   * answered (offline, connection lost). */
  const post = (path, body) => apiPost(path, body);

  const sessionPath = (key, action) => `/v1/sessions/${encodeURIComponent(key)}/${action}`;
  const errorText = (data) => (data && data.error && data.error.message) || "";

  /* An answer saying the session is observed now: the hub's word wins. */
  function noteRefusal(data) {
    if (data && data.error && data.error.code === "not_attached" && chat.row) {
      chat.row.ownership = "observed";
      updateOwnership();
    }
  }

  /* ------------------------------------------------------------- helpers */

  function keyFromHash(hash = location.hash) {
    const m = String(hash || "").match(/^#\/?sessions\?(.*)$/);
    return m ? new URLSearchParams(m[1]).get("chat") : null;
  }

  /* ------------------------------------------------------------ markdown */
  /* Rich Markdown for agent messages, a port of the phone's Markdown
   * (app/ios/Sources/SWERemote/Hub/Markdown.swift): the subset the
   * harnesses actually write — headings, lists, tables, quotes, rules,
   * fenced code — as blocks, with inline spans inside each. Text is
   * escaped; only web links open, so a link to the agent's own files
   * shows as a file name (the phone's code-intent rule), and a lone
   * `![alt](src)` or a harness's `<json-render>` folds into a note. */

  /* `marks`, when given, collects every blank line the parse stops on
   * between blocks, as [line, blocks before it]: nothing before such a
   * line looks past it, so the blocks before it never change when text
   * is added after it (mdStream relies on that). */
  function mdBlocks(text, marks) {
    const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    if (marks) marks.lines = lines.length;
    const blocks = [];
    let paragraph = [];
    const flush = () => {
      const joined = paragraph.join("\n").trim();
      if (joined) blocks.push({ t: "p", text: joined });
      paragraph = [];
    };

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      // Fenced code: everything up to the closing fence, verbatim.
      if (trimmed.startsWith("```") || trimmed.startsWith("~~~")) {
        flush();
        const fence = trimmed.slice(0, 3);
        // A fence closed on its opening line keeps that line as
        // content — its first word is not a language tag.
        if (trimmed.length > fence.length && trimmed.endsWith(fence)) {
          blocks.push({ t: "code", lang: "", code: trimmed.slice(3, -3).trim() });
          i += 1;
          continue;
        }
        // The info string's first word is the language tag; any rest
        // (a title, run flags) is metadata, not part of the code.
        const lang = trimmed.slice(3).trim().split(/\s+/)[0];
        // A fence indented under a list item: its lines lose that indent.
        const indent = (line.match(/^ */) || [""])[0].length;
        const body = [];
        i += 1;
        while (i < lines.length && !fenceClose(lines[i], fence)) {
          const raw = lines[i];
          const lead = Math.min(indent, (raw.match(/^ */) || [""])[0].length);
          body.push(raw.slice(lead));
          i += 1;
        }
        i += 1; // the closing fence (or past the end)
        blocks.push({ t: "code", lang: lang || "", code: body.join("\n") });
        continue;
      }

      if (!trimmed) {
        flush();
        if (marks) marks.push([i, blocks.length]);
        i += 1;
        continue;
      }

      // A view a harness drew (Droid's <json-render>) folds into a note;
      // text after the closing tag on its line parses as usual.
      if (trimmed.startsWith("<json-render>")) {
        flush();
        let body = trimmed;
        // Only the line just added can hold the tag: asking the whole
        // body each line made an unclosed view quadratic in its length.
        let closed = trimmed.includes("</json-render>");
        while (!closed && i + 1 < lines.length) {
          i += 1;
          body += "\n" + lines[i];
          closed = lines[i].includes("</json-render>");
        }
        const close = body.indexOf("</json-render>");
        if (close >= 0) {
          const rest = body.slice(close + "</json-render>".length).trim();
          body = body.slice(0, close + "</json-render>".length);
          if (rest) {
            lines[i] = rest;
            blocks.push({ t: "rendered", body });
            continue;
          }
        }
        i += 1;
        blocks.push({ t: "rendered", body });
        continue;
      }

      const img = imageLine(trimmed);
      if (img) {
        flush();
        blocks.push({ t: "img", alt: img[0], src: img[1] });
        i += 1;
        continue;
      }

      // A table: a row of cells, then a |---|:--:| separator row.
      if (trimmed.includes("|") && i + 1 < lines.length) {
        const align = separatorAlignments(lines[i + 1]);
        if (align) {
          flush();
          const header = cells(trimmed);
          const rows = [];
          let extraRows = 0;
          i += 2;
          while (i < lines.length) {
            const row = lines[i].trim();
            // A line that opens its own block — `> Warning: a | b` is a
            // quote, ```a|b``` a fence — is never a row.
            if (!row || !row.includes("|") || startsBlock(lines, i, false)) break;
            if (rows.length < TABLE_MAX_ROWS) rows.push(cells(row));
            else extraRows += 1;
            i += 1;
          }
          blocks.push({ t: "table", header, align, rows, extraRows });
          continue;
        }
      }

      const h = heading(trimmed);
      if (h) {
        flush();
        blocks.push({ t: "h", level: h[0], text: h[1] });
        i += 1;
        continue;
      }

      if (isRule(trimmed)) {
        flush();
        blocks.push({ t: "hr" });
        i += 1;
        continue;
      }

      if (trimmed.startsWith(">")) {
        flush();
        const quoted = [];
        while (i < lines.length) {
          const q = lines[i].trim();
          if (!q.startsWith(">")) break;
          let rest = q.slice(1);
          if (rest.startsWith(" ")) rest = rest.slice(1);
          quoted.push(rest);
          i += 1;
        }
        blocks.push({ t: "quote", text: quoted.join("\n") });
        continue;
      }

      const item = listItem(line);
      if (item) {
        flush();
        let text = item.text;
        i += 1;
        // Indented lines that aren't a new item continue this one, unless
        // they open a block of their own (a fence, a table, a quote, a
        // heading), which then parses on its own.
        while (i < lines.length) {
          const next = lines[i];
          const nt = next.trim();
          if (!nt || listItem(next) || !(next.startsWith("  ") || next.startsWith("\t"))) break;
          if (startsBlock(lines, i)) break;
          text += "\n" + nt;
          i += 1;
        }
        blocks.push({ t: "li", indent: item.indent, marker: item.marker, text });
        continue;
      }

      paragraph.push(line);
      i += 1;
    }
    flush();
    return blocks;
  }

  /* `![alt](source)` alone on a line. */
  function imageLine(line) {
    if (!line.startsWith("![") || !line.endsWith(")")) return null;
    const close = line.indexOf("](");
    if (close < 0 || line.indexOf("]") !== close) return null;
    const alt = line.slice(2, close);
    const source = line.slice(close + 2, -1);
    // Exactly one image: `![a](x)![b](y)` stays for inline rendering.
    if (source.includes(" ") || source.includes(")") || source.includes("![")) return null;
    return [alt, source];
  }

  /* A line that closes the open fence: the marker alone, or a longer
     run of the same character — "```swift build" is content, not a
     close. (CommonMark agrees; the Mac chat parser got this first.) */
  function fenceClose(line, marker) {
    const t = line.trim();
    return t.startsWith(marker) && [...t].every((c) => c === marker[0]);
  }

  /* Whether `lines[i]` opens a block that isn't list-item text.
     `orTable: false` inside a table's row loop: a data row followed by
     a separator-looking line is still a row, not a second table. */
  function startsBlock(lines, i, orTable = true) {
    const t = lines[i].trim();
    if (t.startsWith("```") || t.startsWith("~~~") || t.startsWith(">") || heading(t)) return true;
    if (t.startsWith("<json-render>") || imageLine(t)) return true;
    if (listItem(lines[i])) return true;
    return orTable && t.includes("|") && i + 1 < lines.length && !!separatorAlignments(lines[i + 1]);
  }

  function heading(line) {
    const m = line.match(/^#{1,6}/);
    if (!m || line[m[0].length] !== " ") return null;
    let text = line.slice(m[0].length).trim();
    // An optional closing run ("## Title ##") goes only when a space
    // sets it apart, so "C#" and "#123" keep their hashes. Walked by
    // hand: a /\s+#+$/ restarts at every space, quadratic on agent text.
    let j = text.length;
    while (j > 0 && text[j - 1] === "#") j -= 1;
    if (j === 0) text = "";
    else if (j < text.length && /\s/.test(text[j - 1])) text = text.slice(0, j).trimEnd();
    return [m[0].length, text];
  }

  function isRule(line) {
    const compact = line.replace(/ /g, "");
    return /^([-*_])\1{2,}$/.test(compact);
  }

  /* `- `/`1. `/`2) ` list row → its item; two-space steps nest (a tab is
   * four). A leading `[ ]`/`[x]` keeps its box in the text. */
  function listItem(line) {
    const leading = (line.match(/^[ \t]*/) || [""])[0];
    const width = [...leading].reduce((w, c) => w + (c === "\t" ? 4 : 1), 0);
    const rest = line.slice(leading.length);
    if ("-*+".includes(rest[0] || "") && rest[1] === " ") {
      return { indent: Math.floor(width / 2), marker: "•", text: rest.slice(2) };
    }
    const dm = rest.match(/^\d{1,3}/);
    if (!dm) return null;
    const after = rest.slice(dm[0].length);
    if (!".)".includes(after[0] || "") || after[1] !== " ") return null;
    return { indent: Math.floor(width / 2), marker: dm[0] + after[0], text: after.slice(2) };
  }

  /* Cells of a `| a | b |` row. Only `\|` is an escape here: it stays
   * in its cell as `|`. Every other backslash (`C:\Users`, `\d+`) is
   * left for the inline parser. */
  function cells(row) {
    let s = row.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const out = [];
    let cur = "";
    for (let j = 0; j < s.length; j++) {
      const ch = s[j];
      if (ch === "\\" && s[j + 1] === "|") { cur += "|"; j += 1; }
      else if (ch === "|") { out.push(cur.trim()); cur = ""; }
      else cur += ch;
    }
    out.push(cur.trim());
    return out;
  }

  /* The alignments a `|---|:--:|--:|` row sets, or null when the line
   * isn't a separator row. */
  function separatorAlignments(line) {
    const trimmed = line.trim();
    if (!trimmed.includes("-")) return null;
    const parts = cells(trimmed);
    if (!parts.length) return null;
    const out = [];
    for (const p of parts) {
      const core = p.replace(/^:+|:+$/g, "");
      if (!core || !/^-+$/.test(core)) return null;
      const left = p.startsWith(":"), right = p.endsWith(":");
      out.push(left && right ? "c" : right ? "r" : "l");
    }
    // A single dashed "cell" without pipes is a rule, not a table.
    if (out.length === 1 && !trimmed.includes("|")) return null;
    return out;
  }

  /* A task item's box, read off the start of a list item's text. */
  function taskBox(text) {
    const lower = text.slice(0, 4).toLowerCase();
    if (lower === "[ ] ") return ["open", text.slice(4)];
    if (lower === "[x] ") return ["done", text.slice(4)];
    return null;
  }

  /* --------------------------------------------------- inline markdown */

  /* An inline code span: a run of backticks, then the same run closing
   * it, so ``a `tick` inside`` stays one span. */
  const CODE_SPAN = /(`+)(?!`)([^\n]*?[^`\n])\1(?!`)/g;
  /* A link target: no spaces, parentheses only in balanced pairs. */
  const URL_PART = "((?:[^()\\s]|\\([^()\\s]*\\))+)";
  const safeUrl = (url) => /^(https?:\/\/|mailto:|tel:)/i.test(url);

  /* `![alt](src)` inside a sentence reads "*Image: alt*", as the phone
   * shows it; code spans and an escaped `\![` keep what the agent wrote. */
  function inlineImagesAsText(text) {
    if (!text.includes("![")) return text;
    return text.replace(/(`+)[\s\S]*?\1|(?<!\\)!\[([^\]]*)\]\([^)\s]+\)/g,
      (m, _ticks, alt) => (alt === undefined ? m : `*Image: ${alt}*`));
  }

  /* `\<punctuation>` (CommonMark's escapable set) is literal text: `\_`
   * shows as `_`, never italics. Escapes are set aside before the rest
   * of the inline rules and put back at the end. */
  const ESCAPED = /\\([!-/:-@[-`{-~])/g;

  function emphasis(text) {
    return text
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w])__([^_]+)__(?!\w)/g, "$1<strong>$2</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_([^_\s][^_]*?)_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  }

  function inline(s) {
    const codes = [];
    let t = inlineImagesAsText(s).replace(CODE_SPAN, (_m, _ticks, c) => {
      // One space padding both ends is the delimiter's, not the code's.
      codes.push(/^ .* $/.test(c) && c.trim() ? c.slice(1, -1) : c);
      return `\u0001${codes.length - 1}\u0001`;
    });
    const escaped = [];
    t = t.replace(ESCAPED, (_m, ch) => `\u0004${escaped.push(ch) - 1}\u0004`);
    t = esc(t);
    // A label holds no "[": a run of them would each scan to the line's end.
    t = t.replace(new RegExp(`\\[([^\\][]*)\\]\\(${URL_PART}(?:\\s+&quot;.*?&quot;)?\\)`, "g"), (m, label, url) => {
      // Code spans bind tighter than links (CommonMark): a target holding
      // one is no link, just text with code in it.
      if (url.includes("\u0001")) return m;
      const text = label ? emphasis(label) : url;
      if (safeUrl(url)) return `<a href="${url}" target="_blank" rel="noopener">${text}</a>`;
      // A link to the agent's own files opens nothing: its label shows
      // as a file name, like the phone's code intent.
      return `<code>${text}</code>`;
    });
    // Emphasis and bare links only in text, never inside a tag.
    t = t.split(/(<a [^>]*>[\s\S]*?<\/a>|<[^>]+>)/).map((part, i) => {
      if (i % 2) return part;
      // Bare URLs are set aside first, so emphasis never reaches inside one.
      // esc() wrote `<`, `>`, `"` and `'` as entities: an `&` starting one
      // of them ends a URL, while `&amp;` — a real `&` — stays inside it.
      // They go back only after emphasis has run: a `__init__.py` in a
      // path is part of the link, never bold.
      const urls = [];
      return emphasis(part
        .replace(/\bhttps?:\/\/(?:[^\s<>"&\u0001]|&(?!lt;|gt;|quot;|#39;))+[^\s<>"&\u0001.,;:!?)\]']/g,
          (url) => { urls.push(url); return `\u0002${urls.length - 1}\u0002`; }))
        .replace(/\u0002(\d+)\u0002/g, (_m, n) => `<a href="${urls[+n]}" target="_blank" rel="noopener">${urls[+n]}</a>`);
    }).join("");
    t = t.replace(/\u0004(\d+)\u0004/g, (_m, i) => esc(escaped[+i]));
    return t.replace(/\u0001(\d+)\u0001/g, (_m, i) => `<code>${esc(codes[+i])}</code>`);
  }

  /* The grid is built eagerly, so a malformed or huge table is cut to
   * this many columns and rows, with a note saying what was left out. */
  const TABLE_MAX_COLUMNS = 24, TABLE_MAX_ROWS = 200;

  function noteChip(kind, title, detail) {
    return `<div class="md-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${MD_ICONS[kind]}</svg><span class="md-note-main"><span>${esc(title)}</span><span class="md-note-sub">${esc(detail)}</span></span></div>`;
  }
  const MD_ICONS = {
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m5 18 5-5 3 3 4-4 2 2v4"/>',
    render: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
  };

  function tableHtml(b) {
    let width = b.header.length;
    for (const r of b.rows) if (r.length > width) width = r.length;
    const cols = Math.min(width, TABLE_MAX_COLUMNS);
    const hiddenRows = b.extraRows || 0;
    const hiddenCols = width - cols;
    const alCls = (c) => (b.align[c] === "c" ? ' class="al-c"' : b.align[c] === "r" ? ' class="al-r"' : "");
    let html = '<div class="md-table"><table><thead><tr>';
    for (let c = 0; c < cols; c++) html += `<th${alCls(c)}>${inline(b.header[c] || "")}</th>`;
    html += "</tr></thead><tbody>";
    for (const row of b.rows) {
      html += "<tr>";
      for (let c = 0; c < cols; c++) html += `<td${alCls(c)}>${inline(row[c] || "")}</td>`;
      html += "</tr>";
    }
    html += "</tbody></table></div>";
    const more = [hiddenRows > 0 ? `${hiddenRows} more rows` : "",
      hiddenCols > 0 ? `${hiddenCols} more columns` : ""].filter(Boolean).join(" and ");
    if (more) html += `<div class="md-table-note">${more} not shown</div>`;
    return html;
  }

  function renderMd(blocks) {
    const perLine = (text) => text.split("\n").map(inline).join("<br>");
    return blocks.map((b) => {
      switch (b.t) {
        case "h": return `<div class="md-h md-h${Math.min(Math.max(b.level, 1), 4)}">${inline(b.text)}</div>`;
        case "p": return `<p>${perLine(b.text)}</p>`;
        case "li": {
          const box = taskBox(b.text);
          const marker = box
            ? `<input type="checkbox" disabled${box[0] === "done" ? " checked" : ""}>`
            : esc(b.marker);
          return `<div class="md-li" style="padding-left:${Math.min(b.indent, 4) * 18}px"><span class="md-marker">${marker}</span><span class="md-li-text">${perLine(box ? box[1] : b.text)}</span></div>`;
        }
        case "quote": return `<blockquote>${perLine(b.text)}</blockquote>`;
        case "code": return `<pre class="code"><code>${hl(b.code, b.lang)}</code></pre>`;
        case "table": return tableHtml(b);
        case "hr": return "<hr>";
        case "img": return noteChip("image", b.alt || "Image", `${b.src.split("/").pop() || b.src} · on the machine`);
        case "rendered": return noteChip("render", "Rendered view", "Open the session on the machine to see it");
        default: return "";
      }
    }).join("");
  }

  function md(text) {
    return renderMd(mdBlocks(text));
  }

  /* md() for a reply still being written (#1555). Blocks that end at a
   * blank line the parse stops on, before the last line (which may still
   * grow), are settled: `state.head` keeps the text they came from and
   * `state.html` their HTML, and only the text after them is parsed again.
   * Returns { reset, fresh, tail }: whether the settled part started over
   * (the text no longer starts with `head`), the HTML settled by this
   * call, and the HTML of the unsettled rest. state.html + tail is always
   * md(text). Before this a long reply was parsed and highlighted whole
   * on every repaint. */
  function mdStream(state, text) {
    text = String(text || "");
    const reset = state.head === undefined || !text.startsWith(state.head);
    if (reset) { state.head = ""; state.html = ""; }
    const rest = text.slice(state.head.length);
    const marks = [];
    const blocks = mdBlocks(rest, marks);
    let mark = null;
    for (let k = marks.length - 1; k >= 0; k--) {
      if (marks[k][0] < marks.lines - 1) { mark = marks[k]; break; }
    }
    if (!mark) return { reset, fresh: "", tail: renderMd(blocks) };
    const fresh = renderMd(blocks.slice(0, mark[1]));
    state.html += fresh;
    // Settle up to the start of the mark's line, counting breaks the way
    // mdBlocks reads them (\r\n, \r or \n). The blank line itself stays in
    // the rest, where it opens nothing.
    const br = /\r\n?|\n/g;
    for (let n = 0; n < mark[0]; n++) br.exec(rest);
    state.head = text.slice(0, state.head.length + br.lastIndex);
    return { reset, fresh, tail: renderMd(blocks.slice(mark[1])) };
  }

  function newClientId() {
    // The hub accepts 1-64 of [A-Za-z0-9_-]; uuid dashes are already legal.
    if (crypto.randomUUID) return crypto.randomUUID();
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
    return [...crypto.getRandomValues(new Uint8Array(24))].map((b) => alphabet[b % 64]).join("");
  }

  /* The log is its own scroller (the composer is pinned below it), so
   * "near the bottom" is measured on it, not the window. */
  function nearBottom() {
    const log = $("chat-log");
    if (!log) return true;
    return log.scrollTop + log.clientHeight >= log.scrollHeight - 160;
  }

  function scrollBottom() {
    const log = $("chat-log");
    if (!log) return;
    // At the bottom, entries folded off the top are out of sight; the
    // history "Show earlier" brought back folds again here too.
    chat.keep = LOG_KEEP;
    trimLog(log);
    log.scrollTop = log.scrollHeight;
  }

  /* ------------------------------------------------------ scrollback cap */
  /* A long transcript, or a chat left open for hours, kept every entry it
   * ever drew in the DOM, so layout, style and memory grew with it
   * (#1615). The log keeps its newest LOG_KEEP entries (a tool run counts
   * once); older ones leave the DOM for chat.earlier, nodes and all, so a
   * tool result still lands on its folded card, and "Show earlier" puts
   * them back EARLIER_STEP at a time. The fold runs only when the log is
   * scrolled to its end (scrollBottom): a reader up in the history never
   * has entries taken from above them. A send still waiting (a failed
   * one waits for nothing) and an unanswered approval never fold (the
   * views that settle them look for them in the log), and the fold
   * stops at one: chat.earlier is always
   * the run of entries just above the oldest drawn one, so putting it
   * back keeps every entry's place. */
  const LOG_KEEP = 200, EARLIER_STEP = 200;
  chat.keep = LOG_KEEP;

  /* The log's children that are entries: not the loading note, the
   * "Show earlier" stub or the streaming bubbles' box. */
  function isEntry(el) {
    return el !== chat.streamBox && el.id !== "chat-loading" && el.id !== "chat-earlier";
  }

  function foldable(el) {
    if (!isEntry(el)) return false;
    const c = el.classList;
    if (c.contains("approval") && !c.contains("resolved")) return false;
    // A failed send waits for nothing: no echo or result settles it.
    return !(c.contains("user") && el.dataset.clientId && !c.contains("failed"));
  }

  function entryCount(log) {
    let n = log.children.length;
    if (chat.streamBox && chat.streamBox.parentNode === log) n -= 1;
    if ($("chat-loading")) n -= 1;
    if ($("chat-earlier")) n -= 1;
    return n;
  }

  /* Folds the oldest entries past chat.keep out of the log, up to the
   * first that may not fold. */
  function trimLog(log) {
    let extra = entryCount(log) - chat.keep;
    if (extra <= 0) return;
    const fold = [];
    for (const c of log.children) {
      if (extra <= 0) break;
      if (!isEntry(c)) continue;
      if (!foldable(c)) break;
      fold.push(c);
      extra -= 1;
    }
    // Put back later, an entry plays no arrival again.
    for (const c of fold) { c.remove(); c.classList.remove("arrive"); }
    chat.earlier.push(...fold);
    paintEarlier(log);
  }

  /* The stub at the log's top while entries are folded. */
  function paintEarlier(log) {
    let stub = $("chat-earlier");
    const n = chat.earlier.length;
    if (!n) { if (stub) stub.remove(); return; }
    if (!stub) {
      stub = document.createElement("div");
      stub.id = "chat-earlier";
      stub.className = "chat-earlier";
      stub.innerHTML = '<button type="button" class="chat-earlier-btn"></button>';
      stub.querySelector(".chat-earlier-btn").onclick = () => showEarlier();
      log.prepend(stub);
    }
    const step = Math.min(n, EARLIER_STEP);
    stub.querySelector(".chat-earlier-btn").textContent = step === n
      ? `Show ${n} earlier ${n === 1 ? "entry" : "entries"}`
      : `Show ${step} earlier entries (${n} hidden)`;
  }

  /* Puts the newest `n` folded entries back above the oldest drawn one,
   * which stays where it was on screen. Returns the entries put back. */
  function showEarlier(n = EARLIER_STEP) {
    const log = $("chat-log");
    const stub = $("chat-earlier");
    if (!log || !stub || !chat.earlier.length) return [];
    const back = chat.earlier.splice(Math.max(0, chat.earlier.length - n));
    chat.keep = Math.max(chat.keep, entryCount(log) + back.length);
    const ref = stub.nextElementSibling;
    const y = ref ? ref.getBoundingClientRect().top : 0;
    stub.after(...back);
    for (const el of back) {
      if (el.matches("details.tool[data-pages]")) paintPreviewButton(el);
      for (const card of el.querySelectorAll("details.tool[data-pages]")) paintPreviewButton(card);
    }
    paintEarlier(log);
    if (ref) log.scrollTop += ref.getBoundingClientRect().top - y;
    return back;
  }

  /* Where the entry with journal seq `seq` is among `list`: { top, el },
   * `top` being the run that holds a tool folded into one, or null. */
  function findSeq(list, seq) {
    for (const top of list) {
      if (Number(top.dataset.seq || 0) === seq) return { top, el: top };
      if (top.classList.contains("tool-run")) {
        for (const el of top.querySelector(".tool-run-items").children) {
          if (Number(el.dataset.seq || 0) === seq) return { top, el };
        }
      }
    }
    return null;
  }

  /* Scroll-to-message: brings back the folded history down to the entry
   * with journal seq `seq` first, then scrolls it into view. Returns the
   * entry, or null when the log never drew it. */
  function revealSeq(seq) {
    const log = $("chat-log");
    if (!log) return null;
    const folded = findSeq(chat.earlier, seq);
    if (folded) showEarlier(chat.earlier.length - chat.earlier.indexOf(folded.top));
    const hit = findSeq(log.children, seq);
    if (!hit) return null;
    // A tool in a closed run: the run opens to show it.
    if (hit.top !== hit.el) hit.top.open = true;
    hit.el.scrollIntoView({ block: "center" });
    return hit.el;
  }

  /* An entry placed by seq (a restored approval) above the oldest drawn
   * one: the folded entries after it come back first, so it lands
   * among them in order. */
  function unfoldAfter(at) {
    const i = chat.earlier.findIndex((c) => Number(c.dataset.seq || 0) > at
      || (c.classList.contains("tool-run")
        && [...c.querySelector(".tool-run-items").children].some((it) => Number(it.dataset.seq || 0) > at)));
    if (i >= 0) showEarlier(chat.earlier.length - i);
  }

  /* The log's entries go above the streaming bubbles, which stay the
   * newest thing in it until their final messages take their place. */
  function logAppend(log, el) {
    const box = chat.streamBox;
    if (box && box.parentNode === log) log.insertBefore(el, box);
    else log.appendChild(el);
  }

  function lastEntry(log) {
    const last = log.lastElementChild;
    return last && last === chat.streamBox ? last.previousElementSibling : last;
  }

  /* One streaming bubble's text, drawn through mdStream: the settled
   * blocks' nodes stay put (a selection in them survives), and only the
   * nodes after them are replaced. `st.kept` counts the settled nodes. */
  function paintBubble(el, text) {
    const st = el.mdState || (el.mdState = { kept: 0 });
    const out = mdStream(st, text);
    let box = el.querySelector(".text");
    if (!box || out.reset) {
      el.innerHTML = '<div class="text"></div>';
      box = el.querySelector(".text");
      st.kept = 0;
    }
    while (box.childNodes.length > st.kept) box.lastChild.remove();
    if (out.fresh) {
      box.insertAdjacentHTML("beforeend", out.fresh);
      st.kept = box.childNodes.length;
    }
    if (out.tail) box.insertAdjacentHTML("beforeend", out.tail);
  }

  /* Draws the bubbles as they are now. Streaming text gets the same
   * Markdown as the final message: it only grows, so a half-written block
   * (an unclosed fence, a ** not yet closed) is the only flicker, and
   * switching to Markdown at the end would reflow the whole reply at once.
   * Only a bubble whose text changed is drawn again, and only from its
   * last settled block on (paintBubble). A hidden tab draws nothing: the
   * bubbles' state keeps up, and showing the tab draws it (#1554). */
  function paintStreams() {
    clearTimeout(chat.streamPaint);
    chat.streamPaint = null;
    if (document.hidden || chat.paintHeld) { chat.streamsStale = true; return; }
    chat.streamsStale = false;
    const log = $("chat-log");
    const shown = chat.replies.shown();
    let box = chat.streamBox;
    if (!log || !shown.length) {
      if (box) box.remove();
      chat.streamBox = null;
      return;
    }
    const stick = nearBottom();
    // A first reply streaming in: "No messages yet." no longer holds.
    const empty = $("chat-loading");
    if (empty) empty.remove();
    if (!box || box.parentNode !== log) {
      box = document.createElement("div");
      box.className = "chat-streams";
      chat.streamBox = box;
      log.appendChild(box);
    }
    const keep = new Set(shown.map((b) => b.id));
    const els = new Map();
    for (const el of [...box.children]) {
      if (keep.has(el.dataset.streamId)) els.set(el.dataset.streamId, el);
      else el.remove();
    }
    for (const b of shown) {
      let el = els.get(b.id);
      if (!el) {
        el = document.createElement("div");
        el.className = "msg assistant streaming arrive";
        el.dataset.streamId = b.id;
        box.appendChild(el);
      }
      // The text drawn, kept as a property: an attribute would copy the
      // whole reply into the DOM on every paint.
      if (el.streamText !== b.text) {
        el.streamText = b.text;
        paintBubble(el, b.text);
      }
    }
    if (stick) scrollBottom();
  }

  const STREAM_MS = {
    // The hub sends at most one delta per reply every 100 ms; repaints
    // are coalesced on top of that, across replies.
    paint: 80,
    // How long a bubble outlives its session going idle: the final
    // message comes from the transcript, which can land a poll later.
    idleGrace: 3000,
  };

  function receiveDelta(d) {
    if (!chat.replies.apply(d) || chat.streamPaint !== null) return;
    if (document.hidden) { chat.streamsStale = true; return; }
    chat.streamPaint = setTimeout(paintStreams, STREAM_MS.paint);
  }

  function sweepStreams() {
    const ids = chat.replies.openIds();
    if (!ids.length) return;
    const gen = chat.gen;
    setTimeout(() => {
      if (gen === chat.gen && chat.replies.sweep(ids)) paintStreams();
    }, STREAM_MS.idleGrace);
  }

  /* Reconnect, resync or close: every bubble goes. */
  function clearStreams() {
    chat.replies.clear();
    paintStreams();
  }

  /* ------------------------------------------------- syntax highlighting
     The code a transcript shows — shell commands, tool arguments, file
     bodies and fenced blocks — tokenised by a small hand-rolled scanner
     (the web app has no build step, so no highlighter library). The same
     rules live, in Swift, in
     app/mac/Sources/SWECore/Syntax/SyntaxHighlight.swift; keep the
     two in step. Token classes live in chat.css, their colours in
     styles.css's --tk-* variables (Theme.swift's syntax* colours). */

  const tkPattern = (k, src, boundary, flags) => ({ k, r: new RegExp(src, flags || "y"), b: !!boundary });
  const tkSet = (s) => new Set(s.split(" "));

  /* A language spec: comment/string delimiters, word sets and extra
   * patterns. At each position the scanner tries, in order: line-number
   * prefix, line comment, block comment, string, patterns, number,
   * keyword-ish word. Anything unmatched is plain text. */
  function langSpec(over) {
    const s = Object.assign({
      line: [], block: [],
      strings: ["'", '"'],
      multiline: null,   // delimiters allowed to span newlines
      noEscape: null,    // delimiters where \ does not escape
      strPrefix: null,   // an optional run before a delimiter (r"", f'')
      keyAfter: false,   // a string followed by `:` is a key (JSON)
      number: /0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?/y,
      keywords: null, builtins: null, constants: null, variables: null,
      patterns: [],      // [{k, r, b?}] tried before words; b = needs a break before
      call: false,       // name( → builtin (a call site)
      caps: false,       // Capitalised word → builtin (a type)
      fold: false,       // word sets match case-insensitively (SQL, dockerfile)
    }, over);
    /* fold lowercases the word before lookup; the sets fold too, so
     * SQL's lowercase tables and dockerfile's uppercase both work. */
    if (s.fold) {
      for (const k of ["keywords", "builtins", "constants", "variables"]) {
        if (s[k]) s[k] = new Set([...s[k]].map((w) => w.toLowerCase()));
      }
    }
    return s;
  }

  /* `kws` is the C family's shared shape: line and block comments, strings,
   * words with keyword/constant sets, call sites and Capitalised types. */
  function cLang(kws, extra) {
    return langSpec(Object.assign({
      line: ["//"], block: [["/*", "*/"]],
      keywords: tkSet(kws),
      constants: tkSet("true false null nil nullptr None TRUE FALSE"),
      call: true, caps: true,
      patterns: [
        tkPattern("tk-m", String.raw`@[A-Za-z_][\w.]*`, true),   // decorators, annotations
        tkPattern("tk-m", String.raw`#[A-Za-z_][\w!]*`, true),   // C #include, rust attrs
        tkPattern("tk-v", String.raw`\$\w+`, true),              // php
      ],
    }, extra || {}));
  }

  const JS_COMMON = {
    line: ["//"], block: [["/*", "*/"]],
    strings: ["'", '"', "`"], multiline: ["`"],
    constants: tkSet("true false null undefined NaN Infinity"),
    builtins: tkSet("console window document global globalThis process require module exports fetch Promise Symbol Math JSON Object Array String Number Boolean BigInt Map Set WeakMap WeakSet Reflect Proxy Date RegExp Error TypeError RangeError SyntaxError parseInt parseFloat isNaN isFinite setTimeout setInterval clearTimeout clearInterval queueMicrotask structuredClone Intl"),
    call: true, caps: true,
    patterns: [tkPattern("tk-m", String.raw`@[A-Za-z_][\w.]*`, true)],
  };

  const LANGS = {
    bash: langSpec({
      line: ["#"],
      multiline: ["'", '"'], noEscape: ["'"],
      keywords: tkSet("if then else elif fi for while until do done case esac function select in time coproc break continue return exit"),
      builtins: tkSet("cd echo printf read test source eval exec export local declare typeset readonly set unset shift trap wait alias unalias umask ulimit getopts command builtin type hash dirs pushd popd jobs fg bg kill disown suspend logout true false let mapfile readarray enable help caller sudo doas env xargs nohup nice install"),
      patterns: [
        tkPattern("tk-v", String.raw`\$\{(?:[^}"'\\]|\\.)*\}|\$[A-Za-z_]\w*|\$[@#?$!*0-9_-]+`),
        tkPattern("tk-b", String.raw`\[(?=\s)`, true),
        tkPattern("tk-m", String.raw`-{1,2}[A-Za-z][\w=-]*`, true),
      ],
    }),
    dockerfile: langSpec({
      line: ["#"],
      keywords: tkSet("FROM RUN CMD ENTRYPOINT COPY ADD WORKDIR EXPOSE ENV ARG LABEL USER VOLUME SHELL HEALTHCHECK ONBUILD STOPSIGNAL AS"),
      fold: true,
      patterns: [tkPattern("tk-v", String.raw`\$\{?\w+\}?`)],
    }),
    python: langSpec({
      line: ["#"],
      strings: ["'''", '"""', "'", '"'],
      multiline: ["'''", '"""'],
      strPrefix: /[rRfFbBuU]{1,3}/y,
      keywords: tkSet("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case"),
      builtins: tkSet("print len range str int float bool bytes list dict set tuple frozenset object type isinstance issubclass enumerate zip map filter sorted reversed min max sum any all abs round divmod pow input open super property staticmethod classmethod getattr setattr hasattr delattr callable iter next repr format chr ord hex bin oct id vars dir exec eval compile globals locals breakpoint hash slice memoryview bytearray complex"),
      constants: tkSet("True False None NotImplemented Ellipsis"),
      variables: tkSet("self cls"),
      patterns: [
        tkPattern("tk-m", String.raw`@[A-Za-z_][\w.]*`, true),
        tkPattern("tk-b", String.raw`__\w+__`),
      ],
      number: /0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?[jJ]?/y,
    }),
    javascript: langSpec(Object.assign({}, JS_COMMON, {
      keywords: tkSet("await async break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch this throw try typeof var void while with yield"),
    })),
    typescript: langSpec(Object.assign({}, JS_COMMON, {
      keywords: tkSet("await async abstract as break case catch class const continue debugger declare default delete do else enum export extends finally for from function get if implements import in instanceof infer interface is keyof let namespace new of private protected public readonly return satisfies set static super switch this throw try type typeof using var void while with yield"),
    })),
    json: langSpec({
      strings: ['"'], keyAfter: true,
      keywords: tkSet("true false null"),
    }),
    css: langSpec({
      block: [["/*", "*/"]],
      number: /\d[\d.]*(?:px|em|rem|%|vh|vw|vmin|vmax|s|ms|fr|deg|turn|ch|ex|lh|cm|mm|in|pt|pc|dppx)?/y,
      patterns: [
        tkPattern("tk-k", String.raw`@[\w-]+`),                       // @media and friends
        tkPattern("tk-b", String.raw`--[\w-]+`),                      // --custom-property
        /* [^{};\s] keeps whitespace positions O(1): retried at each
         * space, an unbounded run would rescan to the next { } ; —
         * quadratic on a declaration block with a long indent. */
        tkPattern("tk-m", String.raw`[^{};\s][^{};]{0,300}(?=\s*\{)`, true), // a selector before {
        tkPattern("tk-n", String.raw`#[0-9a-fA-F]{3,8}\b`),           // #rrggbb
        tkPattern("tk-b", String.raw`[a-zA-Z-][\w-]*(?=\s*:)`, true), // a property before :
        tkPattern("tk-v", String.raw`[.#:][\w-]+`),                   // .class, #id, :pseudo
        tkPattern("tk-k", String.raw`!\w+`),                          // !important
      ],
    }),
    html: langSpec({
      strings: ['"', "'"],
      block: [["<!--", "-->"]],
      number: null,
      patterns: [
        tkPattern("tk-k", String.raw`<![A-Za-z]+|<\?[\w:-]*|\?>`),
        tkPattern("tk-k", String.raw`<\/?[A-Za-z][\w:-]*|\/?>`),
        tkPattern("tk-b", String.raw`[\w:-]+(?==)`),
        tkPattern("tk-n", String.raw`&[\w#]+;`),
      ],
    }),
    yaml: langSpec({
      line: ["#"],
      keywords: tkSet("true false yes no on off null"),
      patterns: [
        tkPattern("tk-b", String.raw`[\w."'\[\]-]+(?=\s*:(?:\s|$))`, true),
        tkPattern("tk-v", String.raw`[&*][\w-]+`, true),
      ],
    }),
    toml: langSpec({
      line: ["#"],
      strings: ['"""', "'''", '"', "'"],
      multiline: ['"""', "'''"],
      keywords: tkSet("true false"),
      number: /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?|\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}(?:\.\d+)?|0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[+-]?(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?|\b(?:inf|nan)\b/y,
      patterns: [
        tkPattern("tk-k", String.raw`^[ \t]*\[{1,2}[^\]\n]+\]{1,2}`, false, "my"),
        tkPattern("tk-b", String.raw`[\w.-]+(?=\s*=)`, true),
      ],
    }),
    sql: langSpec({
      line: ["--"], block: [["/*", "*/"]],
      strings: ["'", '"', "`"],
      keywords: tkSet("select from where and or not in is null like between join inner left right outer full cross on group by order having limit offset insert into values update set delete create alter drop table index view trigger procedure function if exists unique primary key foreign references default check constraint union all distinct as case when then else end cast with recursive returning begin commit rollback transaction grant revoke vacuum explain analyze asc desc fetch first next only"),
      builtins: tkSet("count sum avg min max coalesce now date datetime substr substring length upper lower trim abs round concat replace nullif ifnull current_timestamp current_date current_user row_number rank dense_rank over partition"),
      constants: tkSet("true false"),
      call: true, fold: true,
    }),
    ruby: langSpec({
      line: ["#"], block: [["=begin", "=end"]],
      strings: ["'", '"', "`"],
      keywords: tkSet("def end class module if else elsif unless while until do begin rescue ensure raise return yield lambda proc require include extend attr_accessor attr_reader attr_writer puts print p gets self nil true false super private protected public initialize new each for in case when then break next redo retry loop and or not alias undef"),
      caps: true,
      patterns: [
        tkPattern("tk-v", String.raw`@{1,2}\w+|\$\w+`),
        tkPattern("tk-v", String.raw`:[a-zA-Z_]\w*[!?=]?`),
      ],
    }),
    php: langSpec({
      line: ["//", "#"], block: [["/*", "*/"]],
      keywords: tkSet("function echo if else elseif foreach while do for switch case break continue return class extends implements interface trait use namespace new public private protected static const var global fn try catch finally throw instanceof clone yield from match readonly enum abstract final print include require list unset isset empty array or and xor as declare"),
      constants: tkSet("true false null TRUE FALSE NULL"),
      call: true, caps: true,
      patterns: [
        tkPattern("tk-k", String.raw`<\?(?:php|=)?|\?>`),
        tkPattern("tk-v", String.raw`\$\w+`),
      ],
    }),
    c: cLang("auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while bool true false"),
    cpp: cLang("alignas alignof and asm auto bool break case catch char char8_t char16_t char32_t class compl concept const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype default delete do double dynamic_cast else enum explicit export extern false float for friend goto if inline int long mutable namespace new noexcept nullptr operator or private protected public register reinterpret_cast requires return short signed sizeof static static_assert static_cast struct switch template this thread_local throw true try typedef typeid typename union unsigned using virtual void volatile wchar_t while xor override final"),
    java: cLang("abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public record return sealed short static strictfp super switch synchronized this throw throws transient try void volatile while yield permits var true false null"),
    kotlin: cLang("as break class continue do else false for fun if in interface is null object package return super this throw true try typealias val var when while by catch companion constructor data delegate enum external finally get import init inline inner internal lateinit noinline open operator out override private protected public reified sealed suspend tailrec vararg where crossinline actual expect const abstract annotation"),
    swift: cLang("associatedtype associativity async await actor break case catch class continue convenience default defer deinit didSet do dynamic else enum extension fallthrough fileprivate final for func get guard if import in indirect infix init inout internal is isolated lazy let mutating nil nonisolated open operator optional override postfix precedencegroup prefix private protocol public repeat required rethrows return self Self set some static struct subscript super switch throw throws try typealias unowned var weak where while willSet true false"),
    rust: cLang("as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true try type unsafe use where while abstract become box do final macro override priv typeof unsized virtual yield", {
      strings: ['"'],
      patterns: [
        tkPattern("tk-s", String.raw`'(?:\\.|[^\\'])'`),
        tkPattern("tk-v", String.raw`'[a-zA-Z_]\w*`),
        tkPattern("tk-m", String.raw`#!?\[[^\]\n]*\]|#\w+`),
        tkPattern("tk-b", String.raw`[a-zA-Z_]\w*!`),
      ],
    }),
    go: cLang("break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false iota nil", {
      strings: ["'", '"', "`"], multiline: ["`"],
      builtins: tkSet("string int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr byte rune float32 float64 complex64 complex128 bool error any comparable append cap close complex copy delete imag len make new panic print println real recover"),
    }),
    csharp: cLang("abstract as base bool break byte case catch char checked class const continue decimal default delegate do double else enum event explicit extern false finally fixed float for foreach goto if implicit in int interface internal is lock long namespace new null object operator out override params private protected public readonly record ref required return sbyte sealed short sizeof stackalloc static string struct switch this throw true try typeof uint ulong unchecked unsafe ushort using var virtual void volatile while init file"),
  };

  /* Fence tags and file extensions onto the language table. Anything not
   * listed renders plain ("text" is an explicit opt-out). */
  const LANG_ALIASES = {
    sh: "bash", shell: "bash", zsh: "bash", fish: "bash", ksh: "bash",
    bashrc: "bash", zshrc: "bash", shellscript: "bash",
    py: "python", python3: "python",
    js: "javascript", jsx: "javascript", node: "javascript", mjs: "javascript", cjs: "javascript",
    ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
    json5: "json", jsonc: "json", jsonl: "json",
    scss: "css", less: "css",
    htm: "html", svg: "html", xhtml: "html", xml: "html", xsl: "html", plist: "html", vue: "html", svelte: "html",
    yml: "yaml",
    rb: "ruby", rake: "ruby", gemfile: "ruby",
    "c++": "cpp", cplusplus: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", h: "cpp",
    "c#": "csharp", cs: "csharp",
    kt: "kotlin", kts: "kotlin",
    rs: "rust",
    golang: "go",
    diff: "diff", patch: "diff",
    containerfile: "dockerfile",
    makefile: "bash", gnumakefile: "bash", mk: "bash",
    ini: "bash", env: "bash", cfg: "bash", conf: "bash",
    md: "text", markdown: "text", plaintext: "text", txt: "text", log: "text",
  };

  /* Basenames that carry a language without an extension. */
  const PATH_LANGS = {
    dockerfile: "dockerfile", containerfile: "dockerfile",
    makefile: "bash", gnumakefile: "bash", rakefile: "ruby", gemfile: "ruby",
    ".bashrc": "bash", ".zshrc": "bash", ".bash_profile": "bash", ".zprofile": "bash",
    ".profile": "bash", ".env": "bash", ".gitignore": "bash", ".dockerignore": "bash",
  };

  function langName(tag) {
    const t = String(tag || "").trim().toLowerCase().replace(/^\./, "").split(/[ \t]/)[0];
    if (!t) return "";
    if (Object.hasOwn(LANG_ALIASES, t)) return LANG_ALIASES[t];
    return Object.hasOwn(LANGS, t) ? t : "";
  }

  function langFromPath(path) {
    const base = String(path || "").split("/").pop().toLowerCase();
    if (Object.hasOwn(PATH_LANGS, base)) return PATH_LANGS[base];
    const dot = base.lastIndexOf(".");
    if (dot < 0) return "";
    return langName(base.slice(dot + 1)) || "";
  }

  /* Read/View output carries the file's own line numbers (`  12→code`,
   * or cat -n's `12<TAB>code`): when nearly every line starts with one,
   * each number becomes a meta token and the rest is the file's code. */
  const NUMBERED_LINE = /^[ \t]*\d+[→|\t]/;
  function isNumbered(code) {
    const lines = code.split("\n").filter((l) => l.trim());
    if (lines.length < 3) return false;
    return lines.filter((l) => NUMBERED_LINE.test(l)).length / lines.length >= 0.8;
  }

  /* The string starting at i: its end index (prefix included), whether
   * its delimiter was found, and the delimiter itself; or null. A
   * 1-char delimiter unclosed by end of line stops before the newline
   * unless the language lets it wrap (bash's, js's backtick). */
  function stringAt(s, i, sp) {
    let d = "", pre = "";
    for (const q of sp.strings) {
      if (s.startsWith(q, i)) { d = q; break; }
    }
    if (!d && sp.strPrefix) {
      sp.strPrefix.lastIndex = i;
      const m = sp.strPrefix.exec(s);
      if (m && m[0]) {
        for (const q of sp.strings) {
          if (s.startsWith(q, i + m[0].length)) { d = q; pre = m[0]; break; }
        }
      }
    }
    if (!d) return null;
    const multi = !!(sp.multiline && sp.multiline.includes(d));
    const escapable = !sp.noEscape || !sp.noEscape.includes(d);
    let j = i + pre.length + d.length;
    while (j < s.length) {
      const c = s[j];
      if (escapable && c === "\\") { j += 2; continue; }
      if (s.startsWith(d, j)) return { end: j + d.length, closed: true, d, multi, escapable };
      if (c === "\n" && !multi) return { end: j, closed: false, d, multi, escapable };
      j += 1;
    }
    return { end: s.length, closed: false, d, multi, escapable };
  }

  /* [tokenClass, text] pairs covering `code` end to end; "" class = plain.
   * `ctx` carries an unfinished comment or multiline string between calls
   * ({s: {k, close, esc} | null}): diff rows and numbered output scan one
   * line at a time, and a token open at the line's end resumes inside the
   * next chunk the way it would have without the break. */
  function scanBlock(code, sp, ctx) {
    const toks = [];
    const n = code.length;
    let i = 0, plain = "";
    const emit = (k, j) => {
      if (k === "") { plain += code.slice(i, j); }
      else {
        if (plain) { toks.push(["", plain]); plain = ""; }
        toks.push([k, code.slice(i, j)]);
      }
      i = j;
    };
    /* A pattern flagged `b` only fires after whitespace or punctuation,
     * so `foo-bar` is not a flag and `input:hover` not a YAML key. */
    const atBreak = () => i === 0 || " \t\n;|&(){}<>=,".includes(code[i - 1]);
    /* Resume a token left open by the previous chunk: run to its
     * delimiter (escapes skipped inside strings) or the chunk's end. */
    if (ctx.s) {
      const s0 = ctx.s;
      let j = 0;
      while (j < n) {
        if (s0.esc && code[j] === "\\") { j += 2; continue; }
        if (code.startsWith(s0.close, j)) { j += s0.close.length; ctx.s = null; break; }
        j += 1;
      }
      emit(s0.k, j);
    }
    while (i < n) {
      let hit = false;
      for (const c of sp.line) {
        if (code.startsWith(c, i)) {
          const e = code.indexOf("\n", i);
          emit("tk-c", e < 0 ? n : e); hit = true; break;
        }
      }
      if (hit) continue;
      for (const pair of sp.block) {
        if (code.startsWith(pair[0], i)) {
          const e = code.indexOf(pair[1], i + pair[0].length);
          if (e < 0) {
            emit("tk-c", n);
            ctx.s = { k: "tk-c", close: pair[1], esc: false };
          } else emit("tk-c", e + pair[1].length);
          hit = true; break;
        }
      }
      if (hit) continue;
      const sa = stringAt(code, i, sp);
      if (sa) {
        let k = "tk-s";
        if (sp.keyAfter) {
          let j = sa.end;
          while (j < n && " \t".includes(code[j])) j++;
          if (code[j] === ":") k = "tk-b";
        }
        if (!sa.closed && sa.multi) ctx.s = { k, close: sa.d, esc: sa.escapable };
        emit(k, sa.end); continue;
      }
      for (const p of sp.patterns) {
        if (p.b && !atBreak()) continue;
        p.r.lastIndex = i;
        const m = p.r.exec(code);
        if (m && m.index === i && m[0]) { emit(p.k, i + m[0].length); hit = true; break; }
      }
      if (hit) continue;
      if (sp.number) {
        sp.number.lastIndex = i;
        const m = sp.number.exec(code);
        if (m && m[0]) { emit("tk-n", i + m[0].length); continue; }
      }
      if (/[A-Za-z_$]/.test(code[i])) {
        let j = i + 1;
        while (j < n && /[\w$]/.test(code[j])) j++;
        const w = code.slice(i, j);
        const key = sp.fold ? w.toLowerCase() : w;
        let k = "";
        if (sp.keywords && sp.keywords.has(key)) k = "tk-k";
        else if (sp.constants && sp.constants.has(key)) k = "tk-n";
        else if (sp.variables && sp.variables.has(key)) k = "tk-v";
        else if (sp.builtins && sp.builtins.has(key)) k = "tk-b";
        else if (sp.call && code[j] === "(") k = "tk-b";
        else if (sp.caps && w[0] >= "A" && w[0] <= "Z") k = "tk-b";
        emit(k, j); continue;
      }
      plain += code[i]; i++;
    }
    if (plain) toks.push(["", plain]);
    return toks;
  }

  /* Whole-input scan. Numbered output splits at each line so a line
   * number is never swallowed by a multiline token reaching across it —
   * the number peels off first, then the open token resumes. */
  function scanCode(code, sp, numbered, ctx) {
    ctx = ctx || { s: null };
    if (!numbered) return scanBlock(code, sp, ctx);
    const toks = [];
    code.split("\n").forEach((ln, idx) => {
      if (idx) toks.push(["", "\n"]);
      let off = 0;
      const m = NUMBERED_LINE.exec(ln.slice(0, 16));
      if (m) { toks.push(["tk-m", m[0]]); off = m[0].length; }
      for (const t of scanBlock(ln.slice(off), sp, ctx)) toks.push(t);
    });
    return toks;
  }

  /* Escaped, span-wrapped HTML for `code` in `lang` (a fence tag or a
   * language id); unknown and empty langs stay plain. `diff` colours by
   * line sigil the way the patch view does. `ctx` threads multiline
   * state between calls (the diff rows below). The scanner's patterns
   * can retry from every position, so only the first HL_MAX characters
   * are coloured and the rest of a huge block shows plain; the tool
   * cards already cut their text there. */
  const HL_MAX = 8000;
  function hl(code, lang, ctx) {
    if (code.length > HL_MAX) {
      const head = hl(code.slice(0, HL_MAX), lang, ctx);
      // The plain rest wasn't scanned, so a comment or string open at the
      // cut is not known to close: the next diff row starts afresh rather
      // than wearing a state that may have ended in the skipped text.
      if (ctx) ctx.s = null;
      return head + esc(code.slice(HL_MAX));
    }
    const name = langName(lang);
    if (!name || name === "text" || name === "diff") {
      if (ctx) ctx.s = null;
      if (!name || name === "text") return esc(code);
      return code.split("\n").map((ln) => {
        const k = ln.startsWith("+") && !ln.startsWith("+++") ? "diff-add"
          : ln.startsWith("-") && !ln.startsWith("---") ? "diff-del"
          : /^(@@|\*\*\*|diff |index |--- |\+\+\+ )/.test(ln) ? "tk-m" : "";
        return k ? `<span class="${k}">${esc(ln)}</span>` : esc(ln);
      }).join("\n");
    }
    return scanCode(code, LANGS[name], isNumbered(code), ctx)
      .map(([k, t]) => (k ? `<span class="${k}">${esc(t)}</span>` : esc(t)))
      .join("");
  }

  /* ----------------------------------------------------------- tool args */

  const filePathOf = (obj) =>
    obj.file_path || obj.path || obj.filename || obj.notebook_path || obj.filePath || "";

  /* A shell word shown the way it could be pasted back: bare when safe,
   * single-quoted when it holds whitespace or shell syntax, so a
   * ["bash","-lc","echo hi"] array reads as `bash -lc 'echo hi'`. */
  const shQuote = (s) => /^[A-Za-z0-9_@%+=:,./~-]+$/.test(s) ? String(s)
    : `'${String(s).replace(/'/g, "'\\''")}'`;

  /* The tool's args as readable text plus a language: a shell command
   * leaves its JSON wrapper, a file's contents go under their own
   * language, and everything else becomes pretty-printed JSON (the card
   * used to show the raw one-line blob). Mirrors ToolDisplay.readable in
   * the Mac app; keep them in step. */
  function readableArgs(tool) {
    const full = String(tool.args_full || "");
    if (!full) return null;
    let obj;
    try { obj = JSON.parse(full); } catch { return { text: full, lang: "" }; }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return { text: full, lang: "json" };
    for (const key of ["command", "cmd", "script"]) {
      const v = obj[key];
      if (typeof v === "string") return { text: v, lang: "bash" };
      if (Array.isArray(v)) return { text: v.map(shQuote).join(" "), lang: "bash" };
    }
    const file = filePathOf(obj);
    for (const key of ["content", "new_source", "code", "source"]) {
      if (typeof obj[key] === "string" && obj[key]) {
        return { text: obj[key], lang: file ? langFromPath(file) : "" };
      }
    }
    let pretty = full;
    try { pretty = JSON.stringify(obj, null, 2); } catch { /* keep raw */ }
    return { text: pretty, lang: "json" };
  }

  /* A tool card's output language, stored on the card so its result can
   * colour as the file it came from. Only read-ish calls claim one —
   * Edit's "file updated" note is prose, not the file. */
  const READISH = /read|view|cat/i;
  const PATH_ARG_KEYS = new Set([
    "file_path", "path", "filename", "notebook_path", "filePath",
    "offset", "limit", "start_line", "end_line", "view_range",
  ]);
  function toolOutLang(tool) {
    let obj;
    try { obj = JSON.parse(tool.args_full); } catch { return ""; }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return "";
    const path = filePathOf(obj);
    if (!path) return "";
    const name = String(tool.name || "");
    if (READISH.test(name)) return langFromPath(path);
    if (Object.keys(obj).every((k) => PATH_ARG_KEYS.has(k))) return langFromPath(path);
    return "";
  }

  function looksJson(text) {
    const t = String(text || "").trim();
    if (!t || (t[0] !== "{" && t[0] !== "[")) return false;
    try { const v = JSON.parse(t); return v !== null && typeof v === "object"; }
    catch { return false; }
  }

  /* ---------------------------------------------------------------- diffs
     Edit/Write tool calls carry parseable file args in args_full: they
     render as a unified diff instead of raw JSON. */

  const DIFF_TOOLS = new Set(["edit", "multiedit", "write", "notebookedit", "apply_patch", "patch", "file_edit"]);

  /* Common-affix diff: identical leading and trailing lines collapse to
     context; the differing middle renders removed, then added. */
  /* An empty side has no lines, as the Swift ToolDiff.diff reads it: a
   * Write's missing old text must not show a removed blank line, nor the
   * file's trailing newline a blank context row. */
  function diffLines(oldText, newText) {
    const a = oldText ? oldText.split("\n") : [];
    const b = newText ? newText.split("\n") : [];
    let pre = 0;
    while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++;
    let suf = 0;
    while (suf < a.length - pre && suf < b.length - pre && a[a.length - 1 - suf] === b[b.length - 1 - suf]) suf++;
    return { ctxTop: a.slice(0, pre), del: a.slice(pre, a.length - suf), add: b.slice(pre, b.length - suf),
      ctxBot: a.slice(a.length - suf) };
  }

  const diffFile = (path) => (path ? `<div class="diff-file">${esc(path)}</div>` : "");

  /* A diff row: the sigil unstyled, the line's own code highlighted in
   * the file's language; the add/del tint stays on the row's background
   * (meta rows are prose, not code). `st` threads multiline comment and
   * string state through the file the row belongs to — removed rows see
   * the old file, added and context rows the new one — so a /* open on
   * one row still comments out the next. */
  const diffRow = (cls, text, lang, st) =>
    `<div class="diff-line ${cls}">${cls === "diff-meta" ? esc(text) : hl(text, lang, st)}</div>`;

  function renderDiff(filePath, oldText, newText) {
    const lang = langFromPath(filePath);
    const d = diffLines(oldText, newText);
    const stOld = { s: null }, stNew = { s: null };
    const rows = [];
    /* A context line exists in both files: show it through the new
     * side's state, and walk the old side over it silently too. */
    const ctxRow = (ln) => {
      rows.push(diffRow("diff-ctx", ` ${ln}`, lang, stNew));
      hl(` ${ln}`, lang, stOld);
    };
    if (d.ctxTop.length > 3) rows.push(diffRow("diff-meta", `${d.ctxTop.length - 3} unchanged lines`));
    for (const ln of d.ctxTop.slice(-3)) ctxRow(ln);
    for (const ln of d.del) rows.push(diffRow("diff-del", `- ${ln}`, lang, stOld));
    for (const ln of d.add) rows.push(diffRow("diff-add", `+ ${ln}`, lang, stNew));
    if (d.ctxBot.length) {
      for (const ln of d.ctxBot.slice(0, 2)) ctxRow(ln);
      if (d.ctxBot.length > 2) rows.push(diffRow("diff-meta", `${d.ctxBot.length - 2} more lines`));
    }
    return `<div class="diff">${diffFile(filePath)}${rows.join("")}</div>`;
  }

  /* Raw patch text (codex apply_patch): colour by line sigil, with the
   * file's language inside the changed lines. A hunk header jumps to
   * unrelated code, so any token left open resets there. */
  function renderPatch(filePath, patch) {
    const lang = langFromPath(filePath);
    const stOld = { s: null }, stNew = { s: null };
    const rows = String(patch || "").split("\n").slice(0, 400).map((ln) => {
      if (ln.startsWith("@@") || ln.startsWith("***")) {
        stOld.s = stNew.s = null;
        return diffRow("diff-meta", ln);
      }
      const cls = ln.startsWith("+") ? "diff-add" : ln.startsWith("-") ? "diff-del" : "diff-ctx";
      if (cls === "diff-ctx") {
        const html = diffRow(cls, ln, lang, stNew);
        hl(ln, lang, stOld);
        return html;
      }
      return diffRow(cls, ln, lang, cls === "diff-del" ? stOld : stNew);
    });
    return `<div class="diff">${diffFile(filePath)}${rows.join("")}</div>`;
  }

  /* The file a patch names first (codex's "*** Update File: x"), if any. */
  function patchFile(patch) {
    const m = String(patch || "").match(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/m);
    return m ? m[1].trim() : "";
  }

  /* A diff's HTML for edit-ish tools, or null for anything else. The
   * args are whatever the agent sent, so a field that isn't a string
   * (an array content, say) counts as missing rather than reaching
   * .split. */
  function toolDiff(tool) {
    const name = String(tool.name || "").toLowerCase();
    if (!DIFF_TOOLS.has(name) || !tool.args_full) return null;
    let args;
    try { args = JSON.parse(tool.args_full); } catch { return null; }
    const patching = name === "apply_patch" || name === "patch";
    // Codex's apply_patch is a custom tool whose input is the patch text
    // itself, so the hub's args_full is a JSON string, not an object.
    if (patching && typeof args === "string") return args ? renderPatch(patchFile(args), args) : null;
    if (!args || typeof args !== "object") return null;
    const text = (v) => (typeof v === "string" ? v : "");
    const isText = (v) => typeof v === "string";
    const file = text(args.file_path) || text(args.path) || text(args.filename);
    if (name === "write" || name === "notebookedit" || name === "file_edit") {
      const content = text(args.content) || text(args.new_source);
      return content ? renderDiff(file, "", content) : null;
    }
    if (patching) {
      const patch = text(args.patch) || text(args.diff) || text(args.input) || tool.args_full;
      return renderPatch(file || patchFile(patch), patch);
    }
    if (name === "multiedit" && Array.isArray(args.edits)) {
      const parts = args.edits.slice(0, 8)
        .filter((e) => e && (isText(e.old_string) || isText(e.new_string)))
        .map((e) => renderDiff(file, text(e.old_string), text(e.new_string)));
      return parts.length ? parts.join("") : null;
    }
    // edit and friends: {old_string, new_string}
    if (isText(args.old_string) || isText(args.new_string)) {
      return renderDiff(file, text(args.old_string), text(args.new_string));
    }
    return null;
  }

  /* --------------------------------------------------------- page preview
     A tool call that writes an .html, .htm or .svg file gets a Preview
     button. It asks the hub for the file as it is on disk now
     (GET /v1/sessions/{key}/preview?path=, remote/preview.py) and draws
     it in a locked frame. Which calls count and how a path becomes the
     route's is a port of the phone's PagePreview.swift
     (app/ios/Sources/SWERemote/Hub/PagePreview.swift); keep them in
     step. */

  const PAGE_EXTENSIONS = new Set(["html", "htm", "svg"]);
  /* Tools that write or edit a file, by lowercased name, across the
   * harnesses the hub decodes. */
  const PAGE_TOOLS = new Set([
    "write", "edit", "multiedit", "file_edit", "write_file", "edit_file",
    "create", "create_file", "replace", "str_replace_editor",
    "str_replace_based_edit_tool", "apply_patch", "patch",
  ]);
  const PAGE_PATH_KEYS = ["file_path", "filePath", "path", "filename", "target_file"];
  const PAGE_MAX_PATH = 4096; // the route refuses longer paths

  /* The name ends in .html, .htm or .svg, any case. */
  function isPage(path) {
    const name = String(path || "").split("/").pop();
    const dot = name.lastIndexOf(".");
    return dot > 0 && PAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
  }

  /* The page files a Codex-style patch adds or updates. An update that
   * moves the file counts where it lands; a deleted file has nothing to
   * show. */
  function patchPageFiles(patch) {
    const out = [];
    let updated = -1; // where in out this section's update went
    const after = (line, marker) => (line.startsWith(marker) ? line.slice(marker.length).trim() : null);
    for (const raw of String(patch || "").split("\n")) {
      const line = raw.trim();
      if (!line.startsWith("*** ")) continue;
      let p;
      if ((p = after(line, "*** Add File: ")) != null) {
        updated = -1;
        if (isPage(p)) out.push(p);
      } else if ((p = after(line, "*** Update File: ")) != null) {
        updated = -1;
        if (isPage(p)) { out.push(p); updated = out.length - 1; }
      } else if ((p = after(line, "*** Move to: ")) != null) {
        if (updated >= 0) out.splice(updated, 1);
        updated = -1;
        if (isPage(p)) out.push(p);
      } else {
        updated = -1;
      }
    }
    return out;
  }

  /* The page files a write tool call names, as it names them (absolute
   * or relative). Empty for every other call. */
  function pageFiles(tool) {
    const full = String(tool.args_full || "");
    if (!PAGE_TOOLS.has(String(tool.name || "").toLowerCase()) || !full) return [];
    // Every call that could match names a page file somewhere, so most
    // cards skip the JSON parse.
    if (!/\.htm|\.svg/i.test(full)) return [];
    let args;
    try { args = JSON.parse(full); } catch { return []; }
    if (typeof args === "string") return patchPageFiles(args);
    if (!args || typeof args !== "object" || Array.isArray(args)) return [];
    // The text editor tool's `view` reads the file.
    if (args.command === "view") return [];
    const out = [];
    for (const key of PAGE_PATH_KEYS) {
      if (typeof args[key] === "string" && args[key]) { out.push(args[key]); break; }
    }
    for (const key of ["patch", "input", "diff"]) {
      if (typeof args[key] === "string") out.push(...patchPageFiles(args[key]));
    }
    return out.filter(isPage);
  }

  /* A path's parts without empty and "." ones; null when one is "..". */
  function pathParts(path) {
    const parts = path.split("/").filter((p) => p && p !== ".");
    return parts.includes("..") ? null : parts;
  }
  /* /private/var and /private/tmp are /var and /tmp on macOS. */
  const unprivate = (parts) =>
    (parts.length >= 2 && parts[0] === "private" && ["var", "tmp", "etc"].includes(parts[1]) ? parts.slice(1) : parts);

  /* `path` relative to the working folder `cwd`, or null when the route
   * couldn't serve it: outside the folder, a ".." part, a home-relative
   * "~" path, a NUL, or an absolute path with no absolute cwd to strip.
   * A relative path is taken as relative to cwd already. */
  function relativePath(path, cwd) {
    path = String(path || "").trim();
    if (!path || path.length > PAGE_MAX_PATH || path.includes("\0") || path.startsWith("~")) return null;
    const parts = pathParts(path);
    if (!parts) return null;
    if (!path.startsWith("/")) return parts.length ? parts.join("/") : null;
    cwd = String(cwd || "").trim();
    const root = cwd.startsWith("/") ? pathParts(cwd) : null;
    if (!root) return null;
    const file = unprivate(parts), base = unprivate(root);
    if (file.length <= base.length || base.some((p, i) => file[i] !== p)) return null;
    return file.slice(base.length).join("/");
  }

  /* The paths the route can serve for these files, relative to cwd,
   * first-seen order, no repeats. */
  function pageTargets(files, cwd) {
    const seen = new Set();
    return files.map((f) => relativePath(f, cwd))
      .filter((p) => p && isPage(p) && !seen.has(p) && seen.add(p));
  }

  /* The route for one page. encodeURIComponent, not URLSearchParams:
   * the hub's parse_qs reads a raw "+" as a space, and the phone once
   * sent "a+b.html" as "a b.html". */
  const previewUrl = (key, path) => `${sessionPath(key, "preview")}?path=${encodeURIComponent(path)}`;

  /* The frame's lockdown:
   *
   * - sandbox with no tokens: no scripts (no allow-scripts), an opaque
   *   origin that can't read the app's storage or credential (no
   *   allow-same-origin), no forms, popups, modals or top navigation.
   *   Plugins and autoplay are off with it.
   * - PREVIEW_CSP: sandbox alone still lets <img>, <link> and CSS
   *   url()s load. This is the hub's own preview header
   *   (remote/preview.py CSP) minus `sandbox`, which a <meta> policy
   *   can't carry: nothing loads but inline styles and data: images,
   *   fonts and media, and <base>/<form> can't point anywhere.
   * - Two frames, both sandboxed: a wrapper of our own whose only
   *   content is the page's frame. A frame may still navigate itself
   *   (a `target="_self"` link click), and neither its sandbox nor its
   *   own policy stops that; the browser checks such a navigation
   *   against the parent's frame-src, which the wrapper's policy sets to
   *   'none' (via default-src). The page's frame also inherits the
   *   wrapper's policy, so it holds even if the page's own copy were
   *   placed wrong.
   *
   * <base target="_blank"> sends a plain link click to a new window,
   * which the sandbox refuses (no allow-popups). */
  const PREVIEW_SANDBOX = "";
  const PREVIEW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    + "font-src data:; media-src data:; base-uri 'none'; form-action 'none'";
  const PREVIEW_HEAD = `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}">`
    + '<base target="_blank">';

  /* The hub types its answer from the file it read (a link keeps its
   * target's type); without a type, the name decides. */
  function previewKind(contentType, path) {
    const t = String(contentType || "").toLowerCase();
    if (t.startsWith("image/svg")) return "svg";
    if (t.startsWith("text/html")) return "html";
    return String(path || "").toLowerCase().endsWith(".svg") ? "svg" : "html";
  }

  function bytesBase64(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  /* The page's own frame's srcdoc. The policy has to come before
   * anything that loads, and a <meta> policy counts only inside <head>,
   * so it goes first: straight after a leading doctype (keeping the page
   * out of quirks mode), else at the very start, where the parser opens
   * <head> for it. Only HTML whitespace may precede the doctype: a
   * comment (`<!-->` closes at once) or any other character could open
   * <body> first, where the policy would be ignored. A doctype ends at
   * its first ">", even inside a quoted identifier (the tokenizer's
   * abrupt-doctype-public-identifier rule), as [^>]* does.
   *
   * An SVG is drawn as an image (a data: URL in an <img>), where the
   * browser runs none of its scripts and loads none of its links, as on
   * the phone. */
  function pageDocument(kind, bytes) {
    if (kind === "svg") {
      return `<!doctype html><html><head><meta charset="utf-8">${PREVIEW_HEAD}`
        // Scaled down to fit the frame either way; an SVG with only a
        // viewBox would otherwise grow to the frame's width.
        + "<style>html,body{margin:0;background:#fff}img{display:block;max-width:100%;max-height:100vh;margin:0 auto}</style>"
        + `</head><body><img alt="" src="data:image/svg+xml;base64,${bytesBase64(bytes)}"></body></html>`;
    }
    const text = new TextDecoder("utf-8").decode(bytes); // drops a BOM
    const lead = text.match(/^[\t\n\f\r ]*<!doctype[^>]*>/i);
    return lead ? lead[0] + PREVIEW_HEAD + text.slice(lead[0].length) : PREVIEW_HEAD + text;
  }

  /* The outer frame's srcdoc: our wrapper, holding the page's frame
   * (same sandbox, the page as an escaped srcdoc attribute). */
  function previewDocument(kind, bytes) {
    return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}">`
      + "<style>html,body{margin:0;height:100%;background:#fff}iframe{display:block;border:0;width:100%;height:100%}</style>"
      + `</head><body><iframe sandbox="${PREVIEW_SANDBOX}" referrerpolicy="no-referrer" title="Page" srcdoc="${esc(pageDocument(kind, bytes))}"></iframe></body></html>`;
  }

  /* The frame's attributes; sandbox is set before srcdoc, so the very
   * first load is already locked. */
  function previewFrameAttrs(doc, title) {
    return { sandbox: PREVIEW_SANDBOX, referrerpolicy: "no-referrer", title, srcdoc: doc };
  }

  /* The plain message for a refused preview, as the phone words it. */
  function previewMessage(status, data) {
    const err = (data && data.error) || {};
    switch (err.code) {
      case "not_found":
        // A hub from before the route answers every unknown path so.
        return err.message === "unknown route" ? "Update the hub to preview pages here."
          : "That file isn't on disk any more.";
      case "too_large": return "This file is too big to preview (the hub's limit is 2 MB).";
      case "unsupported_type": return "Only .html, .htm and .svg files preview.";
      case "bad_path": return "That path can't be previewed.";
      case "not_a_file": return "That path isn't a file.";
      case "outside_folder": return "That file is outside the session's folder.";
      case "not_readable": return "The hub can't read that file.";
      case "preview_unavailable": return "Preview works only for sessions on the hub's own machine for now.";
      case "cwd_missing": return "This session's folder is gone.";
      case "bad_cwd": return "This session has no working folder.";
      case "unknown_session": return "The hub doesn't know this session any more.";
      default: break;
    }
    switch (status) {
      case 0: return "Couldn't reach the hub. Check the connection, then try again.";
      case 401: return "The hub doesn't know this browser's credential.";
      case 404: return "That file isn't on disk any more.";
      case 413: return "This file is too big to preview.";
      case 415: return "Only .html, .htm and .svg files preview.";
      case 422: return "That path can't be previewed.";
      default: return err.message || `The hub answered ${status}.`;
    }
  }

  /* ----------------------------------------------------------- link cards
     A reply that links to web pages gets a card per page under the text:
     host, path and Open (a new tab). Port of the phone's LinkCards.swift.
     The links come from md()'s own anchors, so code spans and fences,
     which md() never links, are skipped the same way, and only http and
     https count (md() links mailto: and tel: too). Both take the reply's
     md() HTML, the markup its bubble shows, so it is rendered once. */

  const LINK_CARDS_MAX = 3;

  function linkUrls(html) {
    const out = [], seen = new Set();
    for (const m of html.matchAll(/<a href="([^"]*)"/g)) {
      const href = m[1].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">").replace(/&amp;/g, "&");
      let url;
      try { url = new URL(href); } catch { continue; }
      if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) continue;
      if (seen.has(url.href)) continue;
      seen.add(url.href);
      out.push(url);
      if (out.length === LINK_CARDS_MAX) break;
    }
    return out;
  }

  /* "claude.ai" for https://www.claude.ai/code/x. */
  const linkHost = (url) => url.hostname.replace(/^www\./i, "");
  /* "/code/artifact/abc?x=1", or "" for a bare host. */
  const linkPath = (url) => (url.pathname === "/" && !url.search ? "" : url.pathname + url.search);

  function linkCardsHtml(html) {
    const urls = linkUrls(html);
    if (!urls.length) return "";
    return `<div class="link-cards">${urls.map((u) => `<a class="link-card" href="${safeHref(u.href)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${esc(linkHost(u))}">
      <span class="link-card-ic">${iconSvg("link")}</span>
      <span class="link-card-main"><span class="link-card-host">${esc(linkHost(u))}</span>${linkPath(u) ? `<span class="link-card-path">${esc(linkPath(u))}</span>` : ""}</span>
      <span class="link-card-open">Open ${iconSvg("out")}</span></a>`).join("")}</div>`;
  }

  /* The phone's icon per message kind (app/ios's ToolCard.icon), as
   * 24px stroke svg in the bar chevron's style. */
  const KIND_ICONS = {
    terminal: '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>',
    pencil: '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/><path d="m15 5 4 4"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.4-4.4"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.6 2.3 4 5.4 4 9s-1.4 6.7-4 9c-2.6-2.3-4-5.4-4-9s1.4-6.7 4-9z"/>',
    people: '<circle cx="9" cy="7.5" r="3.5"/><path d="M3 20.5c.5-3.5 3-5.5 6-5.5s5.5 2 6 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M15.7 15.3c2.7.6 4.6 2.6 4.8 5.2"/>',
    wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-8 8l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 8-8z"/>',
    thought: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5a6 6 0 0 0-12 0c0 1.3.5 2.6 1.5 3.5.8.8 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
    warn: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    out: '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    reload: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
  };
  const TOOL_ICONS = {
    bash: "terminal", shell: "terminal", exec: "terminal", exec_command: "terminal", run_shell_command: "terminal",
    read: "doc", view: "doc", read_file: "doc",
    edit: "pencil", multiedit: "pencil", write: "pencil", apply_patch: "pencil", patch: "pencil",
    file_edit: "pencil", notebookedit: "pencil",
    grep: "search", glob: "search", search: "search", find: "search",
    webfetch: "globe", websearch: "globe", fetch: "globe",
    task: "people", agent: "people",
  };
  function iconSvg(kind) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${KIND_ICONS[kind]}</svg>`;
  }
  function toolIcon(name) {
    return iconSvg(TOOL_ICONS[String(name || "").toLowerCase()] || "wrench");
  }

  /* mcp__server__tool reads as "server · tool", the way the phone shows
   * an MCP tool; other names pass through. */
  function toolDisplayName(name) {
    name = String(name || "");
    if (!name.startsWith("mcp__")) return name;
    const parts = name.slice(5).split("__").filter(Boolean);
    return parts.length >= 2 ? `${parts[0]} · ${parts.slice(1).join("__")}` : (parts[0] || name);
  }

  /* The hub's summary leads with the tool's name ("Bash · make test"),
   * which the card already shows. */
  function toolArgsSummary(tool) {
    const s = String(tool.args_summary || "");
    const lead = `${tool.name || ""} · `;
    return s.startsWith(lead) ? s.slice(lead.length) : s;
  }

  /* Three or more tool cards in a row fold into one "Used N tools" card
   * (the phone's ToolRunCard rule); tools arriving later join the run at
   * the end. */
  function toolRunNames(items) {
    const counts = new Map(), order = [];
    for (const el of items) {
      const name = (el.querySelector(".tool-name") || {}).textContent || "Tool";
      if (!counts.has(name)) order.push(name);
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    return order.sort((a, b) => counts.get(b) - counts.get(a))
      .map((n) => (counts.get(n) > 1 ? `${n} ×${counts.get(n)}` : n)).join(", ");
  }

  function paintToolRun(run) {
    const items = run.querySelector(".tool-run-items").children;
    run.querySelector(".tool-run-count").textContent = `Used ${items.length} tools`;
    run.querySelector(".tool-run-names").textContent = toolRunNames(items);
  }

  function groupTool(el) {
    const prev = el.previousElementSibling;
    if (prev && prev.classList.contains("tool-run")) {
      prev.querySelector(".tool-run-items").appendChild(el);
      paintToolRun(prev);
      return;
    }
    const run = [el];
    for (let n = prev; n && n.matches("details.tool"); n = n.previousElementSibling) run.unshift(n);
    if (run.length < 3) return;
    const box = document.createElement("details");
    box.className = "tool-run";
    box.innerHTML = `<summary><span class="tool-ic">${iconSvg("wrench")}</span><span class="tool-run-count"></span><span class="tool-run-names"></span></summary><div class="tool-run-items"></div>`;
    el.parentNode.insertBefore(box, run[0]);
    const items = box.querySelector(".tool-run-items");
    for (const it of run) items.appendChild(it);
    paintToolRun(box);
  }

  /* An entry seq-dated inside a folded run (a restored approval asked
   * between two calls) splits it; each side keeps its fold only if it
   * still has three tools in a row. */
  function splitToolRun(run, beforeEl, el) {
    const items = [...run.querySelector(".tool-run-items").children];
    const cut = items.indexOf(beforeEl);
    const parent = run.parentNode, ref = run.nextSibling;
    if (cut === 0) { parent.insertBefore(el, run); return; }
    run.remove();
    for (const it of items.slice(0, cut)) parent.insertBefore(it, ref);
    parent.insertBefore(el, ref);
    for (const it of items.slice(cut)) parent.insertBefore(it, ref);
    for (const it of items) groupTool(it);
  }

  /* One SSE frame's event, or null for a comment, heartbeat or junk. */
  function parseFrame(frame) {
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("data:")) data += line.slice(5).replace(/^ /, "") + "\n";
    }
    if (!data.trim()) return null;
    try { return JSON.parse(data); } catch { return null; }
  }

  /* ---------------------------------------------------------------- shell */

  function harnessLabel(row) {
    return row ? (row.agent && row.agent !== row.harness ? row.agent : Fmt.harnessName(row.harness || "")) : "";
  }

  function paintShell() {
    $("panel").innerHTML = `<div class="page chat-page">
      <div class="chat-dash">
      <div class="chat-main">
      <div class="chat-bar" id="chat-bar"></div>
      <div class="card error-card hidden" id="chat-error"></div>
      <div class="card chat-banner hidden" id="chat-banner"></div>
      <div class="chat-log" id="chat-log"><div class="chat-skel" id="chat-loading" role="status" aria-label="Loading the transcript">
        <div class="skel-lines"><div class="skel skel-line" style="--w:72%"></div><div class="skel skel-line" style="--w:88%"></div><div class="skel skel-line" style="--w:55%"></div></div>
        <div class="skel skel-bubble"></div>
        <div class="skel skel-tool"></div>
        <div class="skel-lines"><div class="skel skel-line" style="--w:80%"></div><div class="skel skel-line" style="--w:62%"></div></div>
        <div class="skel skel-bubble short"></div>
        <div class="skel-lines"><div class="skel skel-line" style="--w:78%"></div><div class="skel skel-line" style="--w:90%"></div><div class="skel skel-line" style="--w:48%"></div></div>
      </div></div>
      <div class="chat-working hidden" id="chat-working" role="status" aria-label="Agent is working"><span></span><span></span><span></span></div>
      <div class="chat-announce" id="chat-announce" role="status" aria-live="polite" aria-atomic="true"></div>
      <div class="chat-tray hidden" id="chat-tray" role="region" aria-label="Queued messages"></div>
      <div class="chat-editbar hidden" id="chat-editbar"></div>
      <div class="chat-menu hidden" id="chat-menu" role="listbox" aria-label="Commands"></div>
      <div class="chat-files hidden" id="chat-files" aria-label="Attached files" aria-live="polite"></div>
      <form class="chat-composer" id="chat-composer" autocomplete="off">
        <select class="field chat-mode" id="chat-mode" aria-label="Send mode"
          title="Queue waits for the agent to finish its turn; Steer writes now">
          <option value="queue">Queue</option><option value="steer">Steer</option>
        </select>
        <button type="button" class="icon-button chat-clip" id="chat-clip" aria-label="Attach files"
          title="Attach images, PDFs or text files, up to 20 MB each" disabled>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5 12.4 19a5 5 0 0 1-7-7l7.8-7.8a3.3 3.3 0 0 1 4.7 4.7l-7.8 7.8a1.7 1.7 0 0 1-2.4-2.4l7.2-7.2"
            fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
        <input type="file" class="hidden" id="chat-file" multiple tabindex="-1" aria-hidden="true">
        <textarea class="field chat-text" id="chat-text" rows="1" placeholder="Loading…" disabled
          role="combobox" aria-controls="chat-menu" aria-expanded="false" aria-autocomplete="list"></textarea>
        <button type="button" class="icon-button chat-stop" id="chat-stop" aria-label="Stop" title="Stop the agent's turn" disabled>
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/></svg></button>
        <button type="submit" class="chat-send" id="chat-send" disabled>Send</button>
      </form>
      </div>
      <aside class="chat-rail" id="chat-rail" aria-label="Sessions"></aside>
      </div>
    </div>
    <dialog class="sheet" id="session-sheet"></dialog>
    <dialog class="sheet preview-sheet" id="preview-sheet" aria-label="Page preview"></dialog>`;
    paintBar();
    paintRail();
    bindComposer();
    bindTray();
    paintTray();
  }

  /* The bar's second line: harness, folder, then the model of the newest
   * reply as on the phone (nothing while the hub doesn't know it), and
   * the machine of a satellite's session. */
  function barSub(r) {
    r = r || {};
    return [harnessLabel(r), r.cwd, Fmt.modelName(r.model), r.remote ? r.machine : ""]
      .filter(Boolean).map(esc).join(" · ");
  }

  function paintBar() {
    const bar = $("chat-bar");
    if (!bar) return;
    const r = chat.row || {};
    const status = r.mirrored
      ? `<span class="badge mirrored" title="${esc(MIRRORED_NOTE)}">Mirrored</span>`
      : STATUS[r.status] ? `<span class="badge status-${esc(r.status)}">${esc(STATUS[r.status])}</span>` : "";
    // Ownership needs no chip: an observed session says so in the banner
    // below, and an attached one shows an enabled composer.
    const sub = barSub(chat.row);
    // The tmux pane name rides the catalog row for an attached tmux-mode
    // session and for a kept one; the chip copies its attach command.
    const tmuxChip = r.tmux
      ? `<button type="button" class="tmux-chip" id="chat-tmux" title="Copy: tmux attach -t ${esc(r.tmux)}">${esc(r.tmux)}${Fmt.copyIcon()}</button>` : "";
    bar.innerHTML = `
      <a class="icon-button chat-back" href="#/sessions" aria-label="Back to Sessions" title="Back to Sessions">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m14.5 6-6 6 6 6"/></svg></a>
      ${r.harness ? `<span class="chat-logo" title="${esc(Fmt.harnessName(r.harness))}">${Fmt.logo(r.harness)}</span>` : ""}
      <div class="chat-titles">
        <h1 class="chat-title">${esc(r.title || r.id || chat.key || "Session")}</h1>
        <div class="chat-sub">${sub}</div>
      </div>
      <div class="chat-badges">${status}${tmuxChip}</div>
      ${r.mirrored ? "" : '<span class="chat-conn" id="chat-conn"><span class="dot"></span><span class="caption" id="chat-conn-label"></span></span>'}
      ${r.ownership === "attached" ? `<button type="button" class="icon-button" id="chat-detach" aria-label="Detach" title="Detach, or keep it running in tmux">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg></button>` : ""}
      ${chat.row && typeof openSessionMenu === "function" ? `<button type="button" class="icon-button" id="chat-more"
        aria-label="More actions" aria-haspopup="menu" title="Pin, rename or fork">${MORE_ICON}</button>` : ""}
      ${chat.details ? `<button type="button" class="icon-button" id="chat-details" aria-label="Details" title="Details">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg></button>` : ""}`;
    setConn(chat.connState || "connecting");
    const chip = $("chat-tmux");
    if (chip) Fmt.copyTick(chip, () => `tmux attach -t ${Fmt.shellWord((chat.row || {}).tmux || "")}`);
    const detachBtn = $("chat-detach");
    if (detachBtn) detachBtn.onclick = openDetachSheet;
    // sessions.js's row menu: Pin, Rename and Fork, on this session.
    const more = $("chat-more");
    if (more) more.onclick = () => openSessionMenu(Object.assign({ key: chat.key }, chat.row), more);
    const details = $("chat-details");
    if (details) {
      details.onclick = async () => {
        details.disabled = true;
        details.title = "Loading…";
        const ok = await chat.details();
        details.disabled = false;
        details.title = "Details";
        if (!ok) showError("The session's details could not be loaded.");
      };
    }
  }

  function setConn(state) {
    chat.connState = state;
    const conn = $("chat-conn");
    if (!conn || !$("chat-conn-label")) return;
    conn.className = `chat-conn ${state === true ? "on" : state === "err" ? "err" : "off"}`;
    conn.title = state === true ? "Streaming the session live"
      : state === "err" ? "Reconnecting to the session stream" : "Connecting to the session stream";
    $("chat-conn-label").textContent = state === true ? "" : state === "err" ? "Reconnecting…" : "Connecting…";
  }

  function showError(message) {
    const card = $("chat-error");
    if (!card) return;
    card.innerHTML = `<p class="error">${esc(message)}</p>`;
    card.classList.remove("hidden");
  }

  /* ------------------------------------------------------------ ownership */

  function updateOwnership() {
    const row = chat.row;
    const input = $("chat-text");
    if (!input) return;
    const observed = !row || row.ownership !== "attached";
    // The composer always renders: on observed sessions it's disabled so
    // it still shows where typing will happen once attached.
    input.disabled = !chat.rowKnown || observed;
    if (input.disabled && menu.open) closeMenu();
    input.placeholder = isMirrored() ? "Read-only: a copy of a deleted transcript"
      : !chat.rowKnown ? "Loading…"
      : observed ? "Read-only until this session is attached" : "Message the session";
    const clip = $("chat-clip");
    if (clip) clip.disabled = !chat.rowKnown || observed;
    // A detach deletes the session's staged files on the hub, so refs
    // held from before it are dead: the chips go, and any upload still
    // in flight is aborted.
    if (observed && uploads.items.length) resetUploads();
    paintSendState();
    $("chat-stop").disabled = !chat.rowKnown || observed || chat.stopping;
    $("chat-mode").classList.toggle("hidden", observed);
    // attachable means "has resume flags"; the hub also refuses while the
    // session is mid-turn, so the button hides then rather than have every
    // click bounce off the 409. A kept tmux pane is the exception: attach
    // adopts it as it is, so mid-turn is fine.
    const busy = !!row && row.status === "active";
    const kept = !!row && !!row.kept;
    const mirrored = isMirrored();
    const canAttach = !mirrored && observed && !!row && (!!row.attachable || kept) && (!busy || kept);
    if (!canAttach) chat.confirmAttach = false;
    // The notice does not wait for the catalog row: the list's row
    // already says the session is mirrored.
    paintBanner(mirrored || (chat.rowKnown && (observed || chat.detaching)), canAttach, busy);
    if (mirrored) settleMirroredLog();
    updateWorking();
    paintBar();
    paintRailRows();
    paintTray();
  }

  function paintBanner(show, canAttach, busy) {
    const banner = $("chat-banner");
    if (!banner) return;
    banner.classList.toggle("hidden", !show);
    banner.classList.toggle("calm", show && isMirrored());
    if (!show) { banner.innerHTML = ""; return; }
    if (isMirrored()) {
      // One line, and nothing to press: it cannot be attached or resumed.
      banner.classList.add("arrive");
      banner.innerHTML = `<div class="chat-banner-text"><p>${esc(MIRRORED_NOTE)}</p></div>`;
      return;
    }
    const r = chat.row || {};
    const kept = !!r.kept;
    // A detach that waits out the turn: say it's pending, with a way to
    // keep the session attached instead.
    if (chat.detaching && r.ownership === "attached") {
      banner.innerHTML = `<div class="chat-banner-text"><p>Detaches when this turn ends.</p></div>
        <div class="chat-banner-actions"><button type="button" class="tab" id="chat-detach-cancel">Stay attached</button></div>`;
      $("chat-detach-cancel").onclick = cancelDetach;
      return;
    }
    const text = kept ? `Still running in tmux${r.tmux ? ` as ${r.tmux}` : ""}: read-only here, but the pane is alive for a terminal.`
      : canAttach ? "Observed session: read-only until attached."
      : busy ? "Observed session: mid-turn. Attach once it is idle."
        : "Observed session: read-only (this harness can't be attached).";
    if (canAttach && chat.confirmAttach) {
      const where = r.remote && r.machine ? ` on ${r.machine}` : "";
      const [title, note, go] = kept
        ? ["Reattach to the running tmux session?",
          `The pane ${esc(r.tmux || "")} is still alive${esc(where)}: attach takes it back as it is, mid-turn included. Nothing is stopped or relaunched.`,
          "Reattach"]
        : ["Attach this session?",
          `Attach stops ${esc(harnessLabel(r) || "the harness")} in the terminal running this session${esc(where)},
          then resumes the session under SWE so you can send to it from here.`,
          "Stop it and attach"];
      banner.innerHTML = `<div class="chat-banner-text"><div class="card-title">${esc(title)}</div>
          <p class="caption">${note}</p></div>
        <div class="chat-banner-actions">
          <button type="button" class="tab" id="chat-attach-cancel">Cancel</button>
          <button type="button" class="chat-send" id="chat-attach-go"${chat.attaching ? " disabled" : ""}>${chat.attaching ? "Attaching…" : esc(go)}</button>
        </div>`;
      $("chat-attach-cancel").onclick = () => { chat.confirmAttach = false; updateOwnership(); };
      $("chat-attach-go").onclick = attach;
      return;
    }
    banner.innerHTML = `<div class="chat-banner-text"><p>${esc(text)}</p></div>${canAttach
      ? `<div class="chat-banner-actions"><button type="button" class="chat-send" id="chat-attach">${kept ? "Reattach" : "Attach"}</button></div>` : ""}`;
    const btn = $("chat-attach");
    if (btn) btn.onclick = () => { chat.confirmAttach = true; updateOwnership(); };
  }

  /* ------------------------------------------------------------- detach */

  /* The lifecycle the hub supports for an attached session: the phone's
   * detach sheet, with "keep" only where a tmux pane exists to keep. */
  function openDetachSheet() {
    const sheet = $("session-sheet");
    const r = chat.row || {};
    if (!sheet || r.ownership !== "attached") return;
    const tmux = r.mode === "tmux" && !!r.tmux;
    const busy = r.status === "active";
    const opt = (mode, name, desc, danger = false) =>
      `<button type="button" class="detach-opt${danger ? " danger" : ""}" data-detach="${mode}">
        <span class="opt-name">${name}</span><span class="opt-desc">${desc}</span></button>`;
    sheet.innerHTML = `<form method="dialog" class="sheet-body">
      <div class="sheet-title">Detach this session?</div>
      <p class="caption" style="margin:0">Detaching stops SWE's control of it;
        the transcript stays readable here either way.</p>
      <div class="detach-options">
        ${opt("when_idle", "Detach when it finishes",
          busy ? "Waits out the current turn, then detaches." : "Detaches as soon as the hub next checks.")}
        ${tmux ? opt("keep", "Keep it running in tmux",
          `Leaves <code>${esc(r.tmux)}</code> alive for <code>tmux attach -t ${esc(r.tmux)}</code>. Auto-closed after 12h idle.`) : ""}
        ${opt("now", "Detach now",
          busy ? "Detaches immediately, stopping the turn." : "Detaches immediately.", true)}
      </div>
      <div class="sheet-actions"><button class="tab selected" value="cancel">Cancel</button></div>
    </form>`;
    for (const b of sheet.querySelectorAll("[data-detach]")) {
      b.onclick = () => { sheet.close(); detach(b.dataset.detach); };
    }
    sheet.showModal();
  }

  /* -------------------------------------------------------- preview sheet */

  const preview = { targets: [], i: 0, gen: 0, view: "page", page: null, error: "" };
  /* The Source view shows at most this much of the file (hl colours
   * its first HL_MAX characters). */
  const PREVIEW_SOURCE_MAX = 200000;

  /* A tool card's Preview button. An absolute path needs the session's
   * folder to become the route's relative one, so the button shows once
   * the row is known; a card whose pages are all outside the folder has
   * none (the hub would refuse them). */
  function paintPreviewButton(card) {
    let files = [];
    try { files = JSON.parse(card.dataset.pages || "[]"); } catch { /* none */ }
    const targets = pageTargets(files, chat.row && chat.row.cwd);
    const summary = card.querySelector(":scope > summary");
    let btn = summary && summary.querySelector(".tool-preview");
    if (!summary || !targets.length) {
      if (btn) btn.remove();
      return;
    }
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tool-preview";
      btn.innerHTML = `${iconSvg("eye")}<span>Preview</span>`;
      summary.prepend(btn); // floats right, ahead of the text it sits beside
    }
    btn.setAttribute("aria-label", targets.length === 1
      ? `Preview ${targets[0].split("/").pop()}` : `Preview ${targets.length} pages`);
    // The button sits in the card's toggle row: a click previews and
    // leaves the card as it was.
    btn.onclick = (e) => { e.preventDefault(); e.stopPropagation(); openPreview(targets); };
  }

  function repaintPreviewButtons() {
    const log = $("chat-log");
    if (log) for (const card of log.querySelectorAll("details.tool[data-pages]")) paintPreviewButton(card);
  }

  function openPreview(targets, i = 0) {
    const sheet = $("preview-sheet");
    if (!sheet || !targets.length) return;
    Object.assign(preview, { targets, i, view: "page" });
    paintPreview();
    if (!sheet.open) sheet.showModal();
    loadPreview();
  }

  /* The page as it is on disk now. Every load fetches again (no-store):
   * Reload after the agent edits the file shows the edit. */
  async function loadPreview() {
    const gen = ++preview.gen;
    const path = preview.targets[preview.i];
    preview.page = null;
    preview.error = "";
    paintPreviewStage();
    let status = 0, data = null, page = null;
    try {
      const resp = await fetch(previewUrl(chat.key, path), {
        headers: authHeaders(), credentials: "same-origin", cache: "no-store",
      });
      status = resp.status;
      if (resp.ok) {
        const bytes = new Uint8Array(await resp.arrayBuffer());
        const kind = previewKind(resp.headers.get("Content-Type"), path);
        page = { kind, bytes, doc: previewDocument(kind, bytes) };
      } else {
        try { data = await resp.json(); } catch { /* no body */ }
      }
    } catch { /* status 0: the hub never answered, or the body broke off */ }
    if (gen !== preview.gen) return;
    if (page) preview.page = page;
    // A 200 whose body broke off reads as a lost connection.
    else preview.error = previewMessage(status === 200 ? 0 : status, data);
    paintPreviewStage();
  }

  function paintPreview() {
    const sheet = $("preview-sheet");
    if (!sheet) return;
    const path = preview.targets[preview.i] || "";
    // A patch that wrote several pages picks between them here.
    const pick = preview.targets.length > 1
      ? `<select class="field preview-pick" id="preview-pick" aria-label="Page">${preview.targets.map((t, n) =>
        `<option value="${n}"${n === preview.i ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>`
      : "";
    sheet.innerHTML = `<div class="preview-head">
        <div class="preview-titles"><div class="preview-name">${esc(path.split("/").pop())}</div>
          <div class="preview-path">${esc(path)}</div></div>
        ${pick}
        <button type="button" class="tab" id="preview-source" aria-pressed="false" title="Show the file's text">Source</button>
        <button type="button" class="icon-button" id="preview-reload" aria-label="Reload" title="Load the file again">${iconSvg("reload")}</button>
        <button type="button" class="icon-button" id="preview-close" aria-label="Close">${iconSvg("close")}</button>
      </div>
      <div class="preview-stage" id="preview-stage"></div>
      <div class="preview-foot">Scripts and network are off in previews.</div>`;
    $("preview-close").onclick = () => sheet.close();
    $("preview-reload").onclick = () => loadPreview();
    $("preview-source").onclick = () => {
      preview.view = preview.view === "source" ? "page" : "source";
      paintPreviewStage();
    };
    const sel = $("preview-pick");
    if (sel) sel.onchange = () => { preview.i = Number(sel.value) || 0; preview.view = "page"; paintPreview(); loadPreview(); };
    // A click on the backdrop closes it; the sheet's own content fills it.
    sheet.onclick = (e) => { if (e.target === sheet) sheet.close(); };
    // Closing drops the frame and any load still out.
    sheet.onclose = () => { preview.gen++; sheet.innerHTML = ""; };
  }

  function paintPreviewStage() {
    const stage = $("preview-stage");
    if (!stage) return;
    const name = (preview.targets[preview.i] || "").split("/").pop();
    const src = $("preview-source");
    const showSource = preview.view === "source" && !!preview.page;
    if (src) {
      src.classList.toggle("selected", showSource);
      src.setAttribute("aria-pressed", String(showSource));
      src.disabled = !preview.page;
    }
    if (preview.error) {
      stage.innerHTML = `<div class="preview-msg"><div class="preview-msg-title">Couldn't preview ${esc(name)}</div>
        <div class="caption">${esc(preview.error)}</div>
        <button type="button" class="tab selected" id="preview-retry">Try again</button></div>`;
      $("preview-retry").onclick = () => loadPreview();
      return;
    }
    if (!preview.page) {
      stage.innerHTML = `<div class="preview-msg caption" role="status">Loading ${esc(name)}…</div>`;
      return;
    }
    if (showSource) {
      const text = new TextDecoder("utf-8").decode(preview.page.bytes);
      const cut = text.length > PREVIEW_SOURCE_MAX;
      stage.innerHTML = `<pre class="preview-source">${hl(text.slice(0, PREVIEW_SOURCE_MAX), preview.page.kind === "svg" ? "xml" : "html")}</pre>`
        + (cut ? `<div class="preview-foot">Showing the first ${PREVIEW_SOURCE_MAX.toLocaleString()} characters.</div>` : "");
      return;
    }
    // Attributes go on before the frame joins the page, sandbox first,
    // so its one load happens locked.
    const frame = document.createElement("iframe");
    for (const [k, v] of Object.entries(previewFrameAttrs(preview.page.doc, `Preview of ${name}`))) frame.setAttribute(k, v);
    frame.className = "preview-frame";
    stage.replaceChildren(frame);
  }

  async function detach(mode) {
    if (!chat.key) return;
    const gen = chat.gen;
    const { status, data } = await post(sessionPath(chat.key, "control"), { op: "detach", mode });
    if (gen !== chat.gen) return;
    if (status === 200 || status === 202) {
      if (mode === "when_idle") {
        // The hub detaches when the turn ends (or at once when idle):
        // the ownership event, or a detach command.result, closes it.
        chat.detaching = true;
      } else if (status === 200 && chat.row) {
        chat.detaching = false;
        chat.row.ownership = "observed";
        if (mode === "keep") chat.row.kept = true;
        else { delete chat.row.kept; delete chat.row.tmux; }
        delete chat.row.mode;
      }
      updateOwnership();
      return;
    }
    appendMessage({ kind: "error", text: `Detach failed: ${errorText(data) || (status ? `the hub answered ${status}` : "connection lost")}` }, true);
    noteRefusal(data);
    scrollBottom();
  }

  /* A pending when_idle detach is cancelled by attaching again; one that
   * already fired re-attaches, which is what Stay attached means either
   * way. */
  async function cancelDetach() {
    if (!chat.key) return;
    const gen = chat.gen;
    const { status, data } = await post(sessionPath(chat.key, "control"), { op: "attach" });
    if (gen !== chat.gen) return;
    if (status === 200 || status === 202) {
      chat.detaching = false;
      updateOwnership();
      return;
    }
    appendMessage({ kind: "error", text: `Stay attached failed: ${errorText(data) || (status ? `the hub answered ${status}` : "connection lost")}` }, true);
    scrollBottom();
  }

  /* --------------------------------------------------------------- events */

  /* A pin or rename (`session.meta`, or sessions.js after its own write):
   * the header's title, and the row the menu reads. */
  function applyMeta(d) {
    if (!chat.row) return;
    if ("harness_title" in d) chat.row.harness_title = d.harness_title || "";
    if ("title" in d) chat.row.title = d.title || chat.row.harness_title || "";
    if ("pinned" in d) {
      chat.row.pinned = !!d.pinned;
      chat.row.pinned_at = d.pinned ? d.pinned_at ?? null : null;
    }
    paintBar();
    paintRail();
  }

  /* sessions.js's SessionUnread, when loaded: a failed read post must
   * never break the chat. */
  function unreadHook(name, ...args) {
    const u = globalThis.SessionUnread;
    if (!u) return;
    try {
      const p = u[name](...args);
      if (p && p.catch) p.catch(() => {});
    } catch { /* best effort */ }
  }

  function appendEvent(ev, live = true) {
    const stick = live && nearBottom();
    const d = ev.data || {};
    if (ev.type === "message.appended") {
      // A final taking its bubble's place swaps in as it is: no arrival.
      // The bubble goes even if the final can't be drawn.
      chat.replacing = !!ev.replaces;
      try {
        appendMessage(d, live, ev.seq);
      } finally {
        chat.replacing = false;
        if (ev.replaces && chat.replies.release(ev.replaces)) paintStreams();
      }
      // A hidden tab isn't reading: its read waits for the tab to show.
      if (live && document.visibilityState !== "hidden") unreadHook("activity", chat.key, d.ts);
    } else if (ev.type === "session.status") {
      const status = d.status || "";
      // The turn ended (or stopped to ask) without a message to show.
      if (ev.session === chat.key && status && status !== "active") {
        chat.awaitingReply = false;
        // A reply just finished: the header's model may be new.
        if (live) refreshModel(true);
      }
      if (chat.row && ev.session === chat.key) {
        chat.row.status = status;
        // An open chat polls nothing: a turn ending here should show the
        // Attach button and reword the banner on its own.
        updateOwnership();
      }
    } else if (ev.type === "session.ownership") {
      if (chat.row && ev.session === chat.key) {
        chat.row.ownership = d.ownership || chat.row.ownership;
        // Newer hubs carry the supervised fields so the chip and banner
        // repaint right; absent keys mean an older hub — leave them.
        if ("tmux" in d) { if (d.tmux) chat.row.tmux = d.tmux; else delete chat.row.tmux; }
        if ("mode" in d) { if (d.mode) chat.row.mode = d.mode; else delete chat.row.mode; }
        if ("kept" in d) chat.row.kept = !!d.kept;
        if (chat.row.ownership !== "attached") chat.detaching = false;
        updateOwnership();
      }
    } else if (ev.type === "session.meta") {
      // Through sessions.js when it's there, so the list's row (and a
      // pin chosen here and not yet answered) agree; it calls applyMeta.
      if (ev.session === chat.key) {
        if (typeof patchMeta === "function") patchMeta(chat.key, d);
        else applyMeta(d);
      }
    } else if (ev.type === "approval.requested") {
      // Asked again under the same id (a satellite resends a refused batch
      // whole): an open card moves to the newest ask, as it is, so an
      // answer still on its way (disabled buttons, the pick, data-queued)
      // keeps its card; an answered one stands, folded into chat.earlier
      // or not. One card per request (the Mac and iPhone chats agree).
      const rid = d.request_id || "";
      const log = $("chat-log");
      const same = !rid || !log ? [] : [...log.querySelectorAll(".msg.approval"), ...chat.earlier]
        .filter((c) => c.classList.contains("approval") && c.dataset.req === rid);
      const open = same.find((c) => !c.classList.contains("resolved"));
      if (open) {
        if (ev.seq) open.dataset.seq = String(ev.seq);
        logAppend(log, open);
      } else if (!same.length) {
        appendMessage({ kind: "approval", text: d.prompt || "Approval requested", request_id: rid, options: d.options || [] }, live, ev.seq);
      }
    } else if (ev.type === "command.result") {
      commandResult(d);
    } else if (ev.type === "approval.resolved") {
      const rid = d.request_id || "";
      for (const card of $("chat-log").querySelectorAll(".msg.approval")) {
        if (!rid || card.dataset.req === rid) settleCard(card, d.decision);
      }
    }
    if (stick) scrollBottom();
  }

  /* Sends answer async: settle the optimistic bubble this result belongs
   * to, plus any queued approval card it answers. */
  function commandResult(d) {
    const log = $("chat-log");
    let settled = false;
    if (d.op === "message" && d.client_id) {
      // A queued send left the tray: removed, it's just gone (here or
      // from another device, never a failure); sent or failed, it joins
      // the transcript as a send and settles below.
      const entry = takeQueued(d.client_id, d.cancelled ? "That message was removed" : undefined);
      chat.tray.settled.add(d.client_id);
      if (d.cancelled) {
        settled = true;
        const gone = log.querySelector(`.msg.user[data-client-id="${CSS.escape(d.client_id)}"]`);
        if (gone) gone.remove();
        // A failed send may have folded out of the log (trimLog).
        chat.earlier = chat.earlier.filter((b) => b.dataset.clientId !== d.client_id);
        paintEarlier(log);
      } else if (entry) {
        appendOptimistic(entry.text, d.client_id);
      }
      const b = log.querySelector(`.msg.user.pending[data-client-id="${CSS.escape(d.client_id)}"]`);
      if (b) {
        settled = true;
        b.classList.remove("pending");
        const note = b.querySelector(".send-note");
        if (d.ok) {
          // A degraded send still landed: keep the why visible.
          if (d.degraded) {
            // Kept when the transcript echoes the message (appendMessage).
            b.classList.add("degraded");
            if (note) note.textContent = d.note || "Sent (queued: mid-turn input isn't supported here)";
          } else if (note) {
            note.remove();
          }
        } else {
          b.classList.add("failed");
          const why = d.cancelled ? "Removed before sending" : (d.note || "Send failed");
          if (note) note.textContent = why;
          announceFailure(why);
          chat.awaitingReply = false;
          updateWorking();
        }
      }
    }
    // Any reported detach outcome ends the pending banner: a fired one
    // (the when_idle firing as the turn ended, or another client's) flips
    // ownership; an expired or cancelled one leaves the session attached
    // with nothing pending.
    if (d.op === "detach") {
      chat.detaching = false;
      if (d.ok === true && chat.row && chat.row.ownership === "attached") {
        chat.row.ownership = "observed";
      }
      updateOwnership();
    }
    if (d.op === "approval" && d.request_id) {
      for (const card of log.querySelectorAll(".msg.approval")) {
        if (card.dataset.req !== d.request_id || !card.dataset.queued) continue;
        delete card.dataset.queued;
        // The request's own answer may still be on its way: it must not
        // undo what this result settled.
        card.dataset.settled = "1";
        if (d.ok) {
          settleCard(card, "");
          const note = card.querySelector(".approval-note");
          if (note) note.remove();
        } else {
          // A rejection after the card folded: unfold it so the failure
          // note and the re-enabled rows are visible.
          card.classList.remove("resolved", "open");
          const t = card.querySelector(".approval-toggle");
          if (t) { t.hidden = true; t.setAttribute("aria-expanded", "false"); }
          const v = card.querySelector(".approval-verdict");
          if (v) v.textContent = "";
          card.querySelectorAll(".approval-actions .chosen")
            .forEach((b) => b.classList.remove("chosen"));
          card.querySelectorAll(".approval-actions button").forEach((b) => { b.disabled = false; });
          approvalNote(card).textContent = `Approval failed: ${d.note || "the satellite rejected it"}`;
          announceFailure(approvalNote(card).textContent);
        }
      }
    }
    if (!settled && (d.ok === false || d.degraded || d.note === "approval_not_supported")) {
      appendMessage({ kind: "system", text: d.ok === false
        ? `${d.op ? d.op[0].toUpperCase() + d.op.slice(1) : "Command"} failed: ${d.note || "unknown error"}`
        : (d.note || "Queued") }, true);
    }
  }

  /* ``seq`` stamps the entry with its journal seq; ``at`` places it among
   * the entries by that seq instead of at the end (a restored approval). */
  function appendMessage(msg, live = true, seq = 0, at = 0) {
    const log = $("chat-log");
    if (!log) return;
    const put = (el) => {
      if (seq) el.dataset.seq = String(seq);
      if (!at) { logAppend(log, el); return; }
      if (chat.earlier.length) unfoldAfter(at);
      for (const c of log.children) {
        if (Number(c.dataset.seq || 0) > at) { log.insertBefore(el, c); return; }
        // A folded run has no seq of its own: the point may fall inside
        // it, where the entry belongs between the run's tools.
        if (c.classList.contains("tool-run")) {
          for (const it of c.querySelector(".tool-run-items").children) {
            if (Number(it.dataset.seq || 0) > at) { splitToolRun(c, it, el); return; }
          }
        }
      }
      logAppend(log, el);
    };
    const loading = $("chat-loading");
    if (loading) loading.remove();
    const kind = msg.kind || "assistant";
    if (kind === "tool_result" && msg.result_ref && chat.toolCards.has(msg.result_ref)) {
      const card = chat.toolCards.get(msg.result_ref);
      const body = card.querySelector(".tool-body");
      const outLang = card.dataset.lang || (looksJson(msg.text) ? "json" : "");
      if (body && msg.text) body.insertAdjacentHTML("beforeend", `<div class="caption">Output</div><pre>${hl(msg.text.slice(0, 8000), outLang)}</pre>`);
      return;
    }
    if (kind === "tool_use" && msg.tool) {
      const card = document.createElement("details");
      card.className = "tool";
      card.innerHTML = `<summary><span class="tool-ic">${toolIcon(msg.tool.name)}</span><span class="tool-name">${esc(toolDisplayName(msg.tool.name))}</span><span class="tool-args">${esc(toolArgsSummary(msg.tool))}</span></summary><div class="tool-body"></div>`;
      const diff = toolDiff(msg.tool);
      const body = card.querySelector(".tool-body");
      if (diff) {
        card.classList.add("has-diff");
        body.innerHTML = diff;
      } else {
        const readable = readableArgs(msg.tool);
        if (readable) body.innerHTML = `<pre>${hl(readable.text.slice(0, 8000), readable.lang)}</pre>`;
      }
      card.dataset.lang = toolOutLang(msg.tool);
      const pages = pageFiles(msg.tool);
      if (pages.length) {
        card.dataset.pages = JSON.stringify(pages);
        paintPreviewButton(card);
      }
      if (msg.tool.call_id) chat.toolCards.set(msg.tool.call_id, card);
      if (live) { card.classList.add("arrive"); chat.awaitingReply = false; }
      put(card);
      groupTool(card);
      return;
    }
    // A degraded send's note survives its transcript echo.
    let carried = "";
    if (kind === "user" && live) {
      // A queued send's real bubble lands via message.appended: swap it for
      // the optimistic one already drawn (attachments prefix the stored text
      // with "@path ", hence the endsWith). Backfill never dedupes: history
      // replay must not erase a queued bubble.
      const hit = [...log.querySelectorAll(".msg.user[data-client-id]:not(.failed)")].find((b) => echoesBubble(msg, b));
      if (hit && hit === lastEntry(log)) {
        // Still the newest entry: settle the bubble where it is, so it doesn't
        // leave and come back in. Dropping .pending also tells a late POST
        // reply or command.result it's already delivered.
        hit.classList.remove("pending");
        delete hit.dataset.clientId;
        const note = hit.querySelector(".send-note");
        if (note && !hit.classList.contains("degraded")) note.remove();
        hit.querySelector(".text").innerHTML = md(msg.text);
        return;
      }
      if (hit) {
        const note = hit.querySelector(".send-note");
        if (note && hit.classList.contains("degraded")) carried = note.textContent;
        hit.remove();
      } else {
        echoLeavesTray(msg.text);
      }
    }
    if (live && kind !== "user" && kind !== "tool_result") chat.awaitingReply = false;
    if (kind === "tool_result") {
      // Output whose call isn't on screen: a closed card of its own.
      const card = document.createElement("details");
      card.className = "tool";
      card.innerHTML = `<summary><span class="tool-ic">${toolIcon("output")}</span><span class="tool-name">Tool output</span></summary><div class="tool-body"><pre>${esc((msg.text || "").slice(0, 8000))}</pre></div>`;
      if (live) card.classList.add("arrive");
      put(card);
      groupTool(card);
      return;
    }
    const div = document.createElement("div");
    div.className = `msg ${kind}`;
    if (kind === "compaction") {
      div.innerHTML = '<span class="text">Context compacted</span>';
    } else if (kind === "system") {
      div.innerHTML = `<span class="text">${esc(msg.text || "")}</span>`;
    } else if (kind === "thinking") {
      div.innerHTML = `<details><summary><span class="tool-ic">${iconSvg("thought")}</span>${esc(Fmt.thought(msg.duration_s))}</summary><div class="text">${md(msg.text)}</div></details>`;
    } else if (kind === "approval") {
      approvalCard(div, msg);
    } else if (kind === "error") {
      div.innerHTML = `<div class="role"><span class="tool-ic">${iconSvg("warn")}</span>Error</div><div class="text">${md(msg.text)}</div>`;
    } else if (kind === "user" || kind === "assistant") {
      const html = md(msg.text);
      div.innerHTML = `<div class="text">${html}</div>`;
      // The text as stored, so a resync can tell this message is shown.
      if (kind === "user") div.dataset.text = msg.text || "";
      if (kind === "assistant") div.insertAdjacentHTML("beforeend", linkCardsHtml(html));
      if (carried) {
        div.classList.add("degraded");
        div.insertAdjacentHTML("beforeend", `<div class="send-note">${esc(carried)}</div>`);
      }
    } else {
      div.innerHTML = `<div class="role">${esc(kind)}</div><div class="text">${md(msg.text)}</div>`;
    }
    if (live && !chat.replacing) div.classList.add("arrive");
    put(div);
    if (live && ANNOUNCED[kind] !== undefined) announce(ANNOUNCED[kind], div);
  }

  /* The log is no live region (#1555): a screen reader would read a
   * streaming bubble out again on every repaint. A finished reply, an
   * error or a prompt that waits for an answer is said once instead, in
   * the small status node under the log, cut to its start. Entries drawn
   * in one pass (a backlog landing at once) are said together, so none
   * overwrites another: past ANNOUNCE_BATCH of them, a count, then every
   * error and prompt, then the newest entry. */
  const ANNOUNCED = { assistant: "", error: "Error: ", approval: "Approval needed: ", system: "" };
  const ANNOUNCE_MAX = 300, ANNOUNCE_BATCH = 3;
  let announcing = [];

  function announce(lead, div) {
    const text = String((div.querySelector(".text") || div).textContent || "").replace(/\s+/g, " ").trim();
    queueAnnouncement(lead, lead + (text.length > ANNOUNCE_MAX ? `${text.slice(0, ANNOUNCE_MAX)}…` : text));
  }

  /* A failure written into a card already on screen (a send or an
   * approval the hub refused): said like an error, since the log that
   * holds the card is no live region. */
  function announceFailure(text) {
    queueAnnouncement("failure", text);
  }

  function queueAnnouncement(lead, text) {
    if (!announcing.length) queueMicrotask(sayAnnounced);
    announcing.push({ lead, text });
  }

  function sayAnnounced() {
    const all = announcing;
    announcing = [];
    const node = $("chat-announce");
    if (!node || !all.length) return;
    let said = all;
    if (all.length > ANNOUNCE_BATCH) {
      const last = all[all.length - 1];
      said = [{ text: `${all.length} new entries.` },
        ...all.slice(0, -1).filter((a) => a.lead), last];
    }
    node.textContent = said.map((a) => a.text).join(" ");
  }

  /* An answer folds the card like a folded tool callout: the chevron
   * row carries the picked option and expands back to the full card. */
  function settleCard(card, decision) {
    // The event's decision beats a locally-picked row: another client may
    // have answered first. The .chosen mark is only the fallback for a
    // bare command.result that carries none.
    const btn = (decision
        ? card.querySelector(`[data-dec="${CSS.escape(decision)}"]`)
        : null)
      || card.querySelector(".approval-actions .chosen");
    const said = DECISIONS[decision]
      || (btn && btn.textContent.trim()) || "Answered";
    card.querySelectorAll(".approval-actions button").forEach((b) => {
      b.disabled = true;
      b.classList.toggle("chosen", b === btn);
    });
    // A second settle (command.result, then approval.resolved, for the
    // same answer) refreshes the verdict but must not re-fold a card the
    // user opened in between.
    if (!card.classList.contains("resolved")) card.classList.remove("open");
    card.classList.add("resolved");
    const t = card.querySelector(".approval-toggle");
    if (t) t.hidden = false;
    const v = card.querySelector(".approval-verdict");
    if (v) v.textContent = `Approval · ${said}`;
  }

  function approvalCard(div, msg) {
    div.dataset.req = msg.request_id || "";
    // The pane's menu rows ride the event: show them verbatim so an
    // option the 3 semantic buttons can't name is still answerable.
    // Older events (and non-tmux approvals) carry none — the Allow /
    // Always / Deny fallback covers those.
    const opts = (msg.options || []).filter((o) => o && o.label);
    const actions = msg.request_id ? (opts.length
      ? opts.map((o) => `<button type="button" class="tab approval-opt" data-dec="${esc(o.n)}">${esc(o.label)}</button>`).join("")
      : `<button type="button" class="tab" data-dec="allow">Allow</button>
         <button type="button" class="tab" data-dec="always">Always</button>
         <button type="button" class="tab deny" data-dec="deny">Deny</button>`) : "";
    // The generic "Approval requested" fallback repeats the role line —
    // only a real question or context goes under it. The prompt is text
    // scraped from the agent's pane, so it shows as plain monospace text
    // with its spacing kept, never as markdown: a link, image or heading
    // in the pane must not render (as the iOS app shows it).
    const prompt = (msg.text || "").trim();
    const textRow = prompt && prompt !== "Approval requested"
      ? `<div class="text approval-prompt">${esc(prompt)}</div>` : "";
    div.innerHTML = `
      <button type="button" class="approval-toggle" hidden aria-expanded="false">
        <svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>
        <span class="approval-verdict"></span></button>
      <div class="role">Approval needed</div>${textRow}${actions
      ? `<div class="approval-actions${opts.length ? " menu" : ""}">${actions}</div>` : ""}`;
    const toggle = div.querySelector(".approval-toggle");
    toggle.onclick = () => {
      const on = div.classList.toggle("open");
      toggle.setAttribute("aria-expanded", on ? "true" : "false");
    };
    const buttons = () => div.querySelectorAll(".approval-actions button");
    for (const btn of buttons()) {
      btn.onclick = async () => {
        const gen = chat.gen;
        buttons().forEach((b) => { b.disabled = true; });
        btn.classList.add("chosen");
        // Waiting from before the request: a satellite's command.result
        // can land before the hub's 202 does, and must find the card.
        div.dataset.queued = "1";
        delete div.dataset.settled;
        const { status, data } = await post(sessionPath(chat.key, "approval"),
          { decision: btn.dataset.dec, request_id: msg.request_id });
        // The chat moved on, or an approval.resolved or command.result that
        // raced the answer already settled the card: theirs wins.
        if (gen !== chat.gen || div.classList.contains("resolved") || div.dataset.settled) return;
        const note = approvalNote(div);
        if (status === 202) {
          // The hub queued it for a satellite; the card stays pending until
          // a command.result or approval.resolved event closes it.
          note.classList.add("queued");
          note.textContent = "Queued: waiting for the satellite";
          return;
        }
        delete div.dataset.queued;
        if (status >= 400 || status === 0) {
          btn.classList.remove("chosen");
          buttons().forEach((b) => { b.disabled = false; });
          note.textContent = status === 0 ? "Request failed: connection lost"
            : `Approval failed: ${errorText(data) || "the prompt may have closed"}`;
          announceFailure(note.textContent);
          noteRefusal(data);
        } else {
          note.remove();
          settleCard(div, btn.dataset.dec);
        }
      };
    }
  }

  function approvalNote(card) {
    let note = card.querySelector(".approval-note");
    if (!note) {
      note = document.createElement("div");
      card.appendChild(note);
    }
    note.className = "approval-note";
    return note;
  }

  /* ---------------------------------------------------------- live pacing
     Live events reveal one at a time with a short gap that shrinks as more
     wait; a replay after reconnecting (more than 24) lands at once. */

  function revealDelay(waiting) {
    if (waiting <= 0 || waiting > 24) return null;
    return Math.min(180, 1200 / waiting);
  }

  function enqueue(ev) {
    chat.pending.push(ev);
    // A hidden tab keeps every event, in order, and shows them when it is
    // shown again (#1554); the cursor moves only as each is shown, so a
    // stream that drops meanwhile replays them.
    if (document.hidden) return;
    updateWorking();
    if (chat.revealTimer !== null) return;
    // Frames often arrive in separate reads a few ms apart: the queue
    // empties between them, so keep the gap from the last entry shown.
    const gap = (revealDelay(chat.pending.length) || 0) - (performance.now() - chat.lastRevealAt);
    if (gap > 0) chat.revealTimer = setTimeout(revealNext, gap);
    else revealNext();
  }

  function revealNext() {
    chat.revealTimer = null;
    if (document.hidden) return;
    if (!chat.pending.length) { updateWorking(); return; }
    const delay = revealDelay(chat.pending.length);
    if (delay === null) { flushPending(); return; }
    const log = $("chat-log");
    const before = log ? log.childElementCount : 0;
    const ev = chat.pending.shift();
    showLive(ev);
    updateWorking();
    // Only a new entry earns a pause; a status change or a result landing
    // on its card goes straight through, and so does a final taking its
    // bubble's place (its text was already on screen).
    const added = log && !ev.replaces ? log.childElementCount > before : false;
    if (added) chat.lastRevealAt = performance.now();
    if (chat.pending.length) chat.revealTimer = setTimeout(revealNext, added ? delay : 0);
  }

  /* Every waiting event at once. Finals taking their bubbles' places
   * would each redraw the bubbles left; they are drawn once at the end. */
  function flushPending() {
    clearTimeout(chat.revealTimer);
    chat.revealTimer = null;
    const all = chat.pending;
    chat.pending = [];
    chat.paintHeld = true;
    try {
      for (const ev of all) showLive(ev);
    } finally {
      chat.paintHeld = false;
    }
    if (chat.streamsStale) paintStreams();
    updateWorking();
  }

  function showLive(ev) {
    appendSafely(ev, true);
    remember(ev.seq);
  }

  /* One event the view can't draw is logged and skipped: it mustn't
   * stop the history after it, leave the composer on "Loading…", or
   * break the stream, whose reconnect would replay it again. */
  function appendSafely(ev, live) {
    try {
      appendEvent(ev, live);
    } catch (err) {
      console.error("chat: skipped an event it couldn't show", ev && ev.type, ev && ev.seq, err);
    }
  }

  function dropPending() {
    clearTimeout(chat.revealTimer);
    chat.revealTimer = null;
    chat.pending = [];
  }

  function remember(seq) {
    if (typeof seq === "number" && seq > chat.seq) chat.seq = seq;
  }

  /* Three pulsing dots under the last message while the agent is mid-turn,
   * a send waits for its reply, or messages are still being revealed. */
  function updateWorking() {
    const dots = $("chat-working");
    if (!dots) return;
    const on = !!chat.key && ((chat.row && chat.row.status === "active") || chat.awaitingReply || chat.pending.length > 0);
    if (dots.classList.contains("hidden") === !on) return;
    const stick = nearBottom();
    dots.classList.toggle("hidden", !on);
    if (on && stick) scrollBottom();
  }

  /* --------------------------------------------------------------- stream */

  /* How long the nth reconnect in a row waits: 2s, doubling, at most 30s.
   * A hub that stays down is asked twice a minute, not every 2 seconds. */
  const RETRY_MS = 2000, RETRY_MAX_MS = 30000;
  const retryDelay = (n) => Math.min(RETRY_MS * 2 ** Math.min(n, 10), RETRY_MAX_MS);

  function openStream(gen, after) {
    closeStream();
    // A dropped stream may have lost deltas and finals alike.
    clearStreams();
    const key = chat.key;
    const ctrl = new AbortController();
    const mine = { ctrl };
    chat.stream = mine;
    setConn("connecting");
    fetch(`${sessionPath(key, "events")}?last=${after}`, {
      headers: authHeaders(), credentials: "same-origin", cache: "no-store", signal: ctrl.signal,
    }).then(async (resp) => {
      if (chat.stream !== mine) return;
      if (resp.status === 401) {
        chat.stream = null;
        gate();
        return;
      }
      if (resp.status === 404) {
        chat.stream = null;
        setConn("err");
        showError("This session isn't in the hub's catalog any more.");
        return;
      }
      if (!resp.ok || !resp.body) throw new Error(`sse ${resp.status}`);
      setConn(true);
      chat.retries = 0;
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (chat.stream !== mine) return;
        buf += dec.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          handleFrame(key, frame);
        }
      }
      throw new Error("stream closed");
    }).catch(() => {
      // Only the live stream reconnects: a closed or replaced one is done.
      if (chat.stream !== mine) return;
      chat.stream = null;
      setConn("err");
      // Reconnect with replay from the last seq shown, backing off while
      // it keeps failing. A hidden tab waits: coming back reconnects at
      // once (the visibilitychange handler below).
      clearTimeout(chat.retryTimer);
      chat.retryTimer = setTimeout(() => {
        if (gen === chat.gen && !chat.stream && !document.hidden) openStream(gen, resumeSeq());
      }, retryDelay(chat.retries++));
    });
  }

  function handleFrame(key, frame) {
    const ev = parseFrame(frame);
    if (!ev || key !== chat.key) return;
    // Live only and never journaled: not held through a resync (its
    // redraw clears the bubbles) or taken from a replay.
    if (ev.type === "message.delta") {
      if (!chat.held && !chat.history) receiveDelta(ev.data || {});
      return;
    }
    // A resync is reading the transcript again: what streams meanwhile
    // waits for it, so nothing lands above the history it redraws.
    if (chat.held) {
      if (ev.type !== "journal.truncated" && ev.type !== "journal.caught-up") chat.held.push(ev);
      return;
    }
    const h = chat.history;
    if (h) {
      // Replaying what came before the backfill's last message, only to
      // find the approval the hub says is still waiting.
      if (ev.type === "journal.caught-up" || (typeof ev.seq === "number" && ev.seq > h.until)) {
        restoreApproval();
      } else {
        const d = ev.data || {};
        if (ev.type === "approval.requested" && d.request_id === h.rid) Object.assign(h, { prompt: d.prompt, options: d.options || [], seq: ev.seq, resolved: false });
        if (ev.type === "approval.resolved" && (!d.request_id || d.request_id === h.rid)) h.resolved = true;
        return;
      }
    }
    if (ev.type === "journal.truncated") {
      resync(ev.seq);
      return;
    }
    if (ev.type === "journal.caught-up") {
      if (ev.seq && !chat.pending.length) remember(ev.seq);
      return;
    }
    // A final is matched to its bubble as it arrives (the contract's order
    // is the wire's) and takes its place when the reveal shows it.
    if (ev.type === "message.appended" && typeof ev.seq === "number" && ev.seq > chat.seq) {
      const id = chat.replies.claim((ev.data || {}).kind);
      if (id) ev.replaces = id;
    }
    // The idle sweep takes the bubbles open as the status arrives: by the
    // time the reveal shows it, the next turn's may have started.
    if (ev.type === "session.status" && ev.session === chat.key) {
      const status = (ev.data || {}).status || "";
      if (status && status !== "active") sweepStreams();
    }
    // The cursor advances as each event is shown (showLive), so events
    // still waiting when the stream drops are replayed on reconnect.
    enqueue(ev);
  }

  /* journal.truncated: the journal no longer holds what this view needs
   * to carry on from its cursor. Its ring dropped events past where the
   * view stopped, or it was wiped (hub state reset) and its numbering now
   * starts below the cursor, which would skip every new event as already
   * seen. The view adopts the seq the hub sent, lower or not, and reads
   * the transcript again, as the phone does. The stream stays open; what
   * arrives meanwhile is held, then applied, except messages the redrawn
   * history already shows. */
  async function resync(head) {
    const gen = chat.gen, key = chat.key;
    dropPending();
    clearStreams();
    chat.held = [];
    chat.history = null;
    chat.seq = typeof head === "number" && head >= 0 ? head : 0;
    const { status, data } = await api(`${sessionPath(key, "messages")}?limit=500`);
    if (gen !== chat.gen) return;
    const held = chat.held || [];
    chat.held = null;
    if (status === 401) {
      gate();
      return;
    }
    const log = $("chat-log");
    // A failed read leaves the history on screen rather than an error.
    if (status === 200 && data && log) {
      // Unanswered approval cards stay, buttons and all: the transcript
      // has no approvals, and their asks are behind the cursor.
      const open = [...log.querySelectorAll(".msg.approval:not(.resolved)")];
      // So do sends the transcript doesn't show yet (still sending, sent
      // and waiting for their echo, or failed): their result, or a
      // failed POST, still lands on the bubble.
      // A failed one may have folded out of the log (trimLog).
      const sends = [...chat.earlier.filter((b) => b.matches(".msg.user[data-client-id]")),
        ...log.querySelectorAll(".msg.user[data-client-id]")];
      // User messages the log already showed as delivered (drawn from
      // the transcript, or a settled bubble) each claim their own echo
      // first, so a send repeated within seconds isn't taken for the one
      // before it.
      const shown = [...chat.earlier.filter((b) => b.matches(".msg.user[data-text]")),
        ...log.querySelectorAll(".msg.user[data-text]")]
        .filter((b) => !b.dataset.clientId).map((b) => b.dataset.text);
      log.innerHTML = "";
      chat.earlier = [];
      chat.streamBox = null;
      chat.toolCards.clear();
      const messages = data.messages || [];
      for (const ev of messages) appendSafely(ev, false);
      if (messages.length) remember(messages[messages.length - 1].seq);
      // A send the redraw already shows is settled, as a live echo
      // would; each echo answers one bubble. A failed one never landed.
      const echoed = messages.filter((ev) => ev.type === "message.appended" && ev.data && ev.data.kind === "user")
        .map((ev) => ev.data);
      for (const t of shown) {
        const i = echoed.findIndex((m) => echoes(m.text, t));
        if (i >= 0) echoed.splice(i, 1);
      }
      for (const b of sends) {
        const i = b.classList.contains("failed") ? -1 : echoed.findIndex((m) => echoesBubble(m, b));
        if (i >= 0) echoed.splice(i, 1);
        else logAppend(log, b);
      }
      for (const card of open) logAppend(log, card);
      scrollBottom();
    }
    // Status, ownership, command results and approvals aren't in the
    // transcript, so they apply whatever their seq.
    for (const ev of held) {
      if (ev.type === "message.appended" && typeof ev.seq === "number" && ev.seq <= chat.seq) continue;
      enqueue(ev);
    }
    // An attach or detach may have happened behind the cursor as well.
    loadRow(gen);
  }

  function closeStream() {
    dropPending();
    clearTimeout(chat.streamPaint);
    chat.streamPaint = null;
    clearTimeout(chat.retryTimer);
    chat.retryTimer = null;
    const s = chat.stream;
    chat.stream = null;
    if (s) { try { s.ctrl.abort(); } catch { /* already done */ } }
  }

  /* An approval the hub still waits on, asked before the last message the
   * backfill showed (codex's app-server journals the ask before the tool
   * call it belongs to): its card goes back where it was asked. */
  function restoreApproval() {
    const h = chat.history;
    chat.history = null;
    const log = $("chat-log");
    // Not found before the backfill's last message: asked after it, so the
    // stream's live part brings it like any other.
    if (!h || !h.seq || h.resolved || !log || log.querySelector(`.msg.approval[data-req="${CSS.escape(h.rid)}"]`)) return;
    const stick = nearBottom();
    appendMessage({ kind: "approval", text: h.prompt || "Approval requested", request_id: h.rid, options: h.options || [] }, false, h.seq, h.seq);
    if (stick) scrollBottom();
  }

  /* Where a reconnect picks up: the last seq shown, or the start again
   * while the search for a waiting approval hasn't finished. */
  function resumeSeq() {
    return chat.history ? 0 : chat.seq;
  }

  /* The request_id of this session's approval the hub still waits on
   * (the Inbox lists every pending one), or "". */
  async function pendingApproval(gen) {
    const key = chat.key;
    const { status, data } = await api("/v1/inbox");
    if (gen !== chat.gen || status !== 200 || !data) return "";
    const item = (data.items || []).find((i) => i.key === key && i.status === "approval" && i.request_id);
    return item ? String(item.request_id) : "";
  }

  /* ----------------------------------------------------------------- rail
     The Mac app's SessionRail (SessionsPage.swift): the sessions the page
     lists, grouped by day the same way, so a chat can switch sessions
     without going back to the list. Rows come from the page's own data
     when it has loaded, else one fetch kept in the page's state; the
     open session stays listed even when that data predates it. */

  /* sessions.js's page state, guarded so the script stands alone. */
  const railPage = () => (typeof sessionsPage !== "undefined" ? sessionsPage : null);

  /* The page's sessions array, or the rail's own copy of an answer. */
  function railSessions() {
    const page = railPage();
    const d = (page && page.data) || chat.railData;
    return d && Array.isArray(d.sessions) ? d.sessions : null;
  }

  /* The list's day bucket (sessions.js's localDay): the local calendar
   * day an epoch falls on. */
  function railDay(epoch) {
    if (typeof localDay === "function") return localDay(epoch);
    const d = new Date(epoch * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  /* The list's badge colours as a rail dot (styles.css's .badge.status-*,
   * plus the chat bar's status-approval); nothing for a finished one. */
  const RAIL_DOT = { active: "status-active", error: "status-error",
    followup: "status-followup", interrupted: "status-interrupted", approval: "status-approval" };

  /* The rail's rows: every session the page lists, its own filters left
   * to the page. The open one merges in what the stream last said, and
   * joins when the data predates it or the filters would leave it out. */
  function railRows() {
    const rows = (railSessions() || []).slice()
      .sort((a, b) => (b.last_active_at || 0) - (a.last_active_at || 0));
    const i = chat.key ? rows.findIndex((r) => r.key === chat.key) : -1;
    if (i >= 0) {
      if (chat.row) {
        rows[i] = Object.assign({}, rows[i], {
          status: "status" in chat.row ? chat.row.status : rows[i].status,
          title: chat.row.title || rows[i].title,
        });
      }
    } else if (chat.key && chat.row) {
      rows.push(chat.row);
      rows.sort((a, b) => (b.last_active_at || 0) - (a.last_active_at || 0));
    }
    return rows;
  }

  /* The list's day grouping (sessionsTable) over the rail's rows. */
  function railGroups(rows) {
    const days = [];
    for (const r of rows) {
      const day = railDay(r.last_active_at || 0);
      if (!days.length || days[days.length - 1].day !== day) days.push({ day, rows: [] });
      days[days.length - 1].rows.push(r);
    }
    return days;
  }

  function railRowHtml(r) {
    const sel = r.key === chat.key;
    const dot = r.status && RAIL_DOT[r.status]
      ? `<span class="cr-dot ${RAIL_DOT[r.status]}" title="${esc(STATUS[r.status] || r.status)}"></span>` : "";
    return `<button type="button" class="cr-row${sel ? " selected" : ""}" data-key="${esc(r.key)}"
        ${sel ? 'aria-current="true" ' : ""}title="${esc(r.title || r.id || "Untitled")}">
      <span class="cr-logo">${Fmt.logo(r.harness)}</span><span class="cr-name">${esc(r.title || r.id || "Untitled")}</span>
      <span class="cr-spacer"></span>${dot}</button>`;
  }

  function paintRail() {
    const rail = $("chat-rail");
    if (!rail) return;
    rail.innerHTML = `<div class="cr-head"><span class="caption">Sessions</span><span class="cr-spacer"></span>
      <button type="button" class="cr-btn${chat.rail.loading ? " spinning" : ""}" id="cr-refresh"
        title="List sessions again" aria-label="List sessions again">${icon("rescan")}</button></div>
    <div class="cr-list" id="cr-list"></div>`;
    $("cr-refresh").onclick = () => loadRail(chat.gen, true);
    paintRailRows();
  }

  function paintRailRows() {
    const list = $("cr-list");
    if (!list) return;
    // A repaint for a status change rebuilds the rows; keep a focused
    // row's focus instead of dropping it to <body>.
    const focus = document.activeElement;
    const focusKey = focus && list.contains(focus) && focus.dataset ? focus.dataset.key : null;
    const sessions = railSessions();
    // The open session rides into the rows even when the list itself is
    // empty (a cached empty list and a chat opened from a link).
    const rows = sessions ? railRows() : null;
    let html;
    if (chat.rail.error) {
      html = `<div class="cr-note">${esc(chat.rail.error)}</div>`;
    } else if (!rows) {
      html = chat.rail.loading ? `<div role="status" aria-label="Loading"><div class="cr-day">${Sk.bar(70, 11)}</div>${
        Array.from({ length: 6 }, (_, i) => `<div class="cr-row"><span class="cr-logo">${Sk.bar(14, 14)}</span>
          <span class="cr-name">${Sk.bar(118 - (i % 4) * 16, 12)}</span><span class="cr-spacer"></span>${
          i % 3 ? "" : Sk.bar(8, 8, "cr-dot")}</div>`).join("")}</div>` : '<div class="cr-note">No sessions listed.</div>';
    } else if (!rows.length) {
      html = '<div class="cr-note">No sessions listed.</div>';
    } else {
      html = railGroups(rows).map(({ day, rows }) => {
        const [y, m, dd] = day.split("-").map(Number);
        const label = new Date(y, m - 1, dd).toLocaleDateString("en-US",
          { month: "short", day: "numeric", year: "numeric" });
        return `<div class="cr-day">${label}</div>${rows.map(railRowHtml).join("")}`;
      }).join("");
    }
    list.innerHTML = html;
    for (const btn of list.querySelectorAll(".cr-row[data-key]")) {
      btn.onclick = () => {
        // A row opens its chat the way the list's do: the hash is the
        // chat, so Back returns to whichever session was open before.
        if (btn.dataset.key !== chat.key) {
          location.hash = `#/sessions?chat=${encodeURIComponent(btn.dataset.key)}`;
        }
      };
    }
    if (focusKey) {
      const again = list.querySelector(`.cr-row[data-key="${CSS.escape(focusKey)}"]`);
      if (again) again.focus();
    }
  }

  /* The rail's rows are the page's `/v1/app/sessions` answer: its own
   * copy when the list already loaded, else one fetch (a plain relist;
   * the refresh button adds rescan, like the page's own) that also fills
   * the page's state for when the chat is left. */
  async function loadRail(gen, rescan = false) {
    if (railSessions() && !rescan) { paintRail(); return; }
    if (chat.rail.loading) return;
    chat.rail.loading = true;
    chat.rail.error = "";
    paintRail();
    // sessions.js's pin ledger: a pin chosen while this read was out
    // isn't undone by it.
    const mark = typeof pinLedger !== "undefined" ? pinLedger.clock : 0;
    const { status, data } = await api(`/v1/app/sessions${rescan ? "?rescan=1" : ""}`);
    if (gen !== chat.gen) return;
    chat.rail.loading = false;
    if (status === 401) {
      gate();
      return;
    }
    if (status !== 200 || !data) {
      chat.rail.error = errorText(data)
        || (status ? `The hub answered ${status}.` : "Couldn't list the sessions.");
      paintRail();
      return;
    }
    if (typeof pinOverlay === "function") pinOverlay(data.sessions || [], mark);
    const page = railPage();
    if (page) page.data = data; else chat.railData = data;
    paintRail();
  }

  /* -------------------------------------------------------------- opening */

  /* The model alone, read again from the catalog row. The row is read
   * once at open and no event carries the model, so one that first
   * appears with the opening reply, or changes from another client or
   * the terminal, would show as it was until a reopen. A failed read, or
   * a hub that doesn't know the model, leaves the header as it is.
   *
   * The hub answers from a catalog it rebuilds every 10 seconds, and a
   * turn's end (`afterTurn`) can reach the stream before that, so one
   * read may still hold the old model. Until the model differs, it reads
   * again after each of MODEL_RETRY_MS (each read is one row, #1556). A
   * tab coming back reads once, unless a turn's retries are still
   * running, which it then takes over. A newer call takes over from one
   * still waiting. Returns the model now shown. */
  const MODEL_RETRY_MS = [6000, 6000];
  let modelCall = 0, modelSettling = 0;
  async function refreshModel(afterTurn = false) {
    if (!chat.key || !chat.row) return "";
    const gen = chat.gen;
    const call = ++modelCall;
    afterTurn = afterTurn || modelSettling > 0;
    if (afterTurn) modelSettling = call;
    try {
      for (let i = 0; ; i++) {
        const { row } = await fetchRow(chat.key);
        if (gen !== chat.gen || call !== modelCall) break;
        if (row && row.model && chat.row && row.model !== chat.row.model) {
          chat.row.model = row.model;
          paintBar();
          break;
        }
        if (!afterTurn || i >= MODEL_RETRY_MS.length) break;
        await new Promise((resolve) => setTimeout(resolve, MODEL_RETRY_MS[i]));
        if (gen !== chat.gen || call !== modelCall) break;
      }
    } finally {
      if (modelSettling === call) modelSettling = 0;
    }
    return (gen === chat.gen && chat.row && chat.row.model) || "";
  }

  /* This chat's catalog row and its machine, from `GET /v1/sessions?key=`
   * (#1556), which serialises that one row instead of the whole catalog.
   * A session the catalog doesn't hold answers 404. */
  async function fetchRow(key) {
    const { status, data } = await api(`/v1/sessions?key=${encodeURIComponent(key)}`);
    const row = status === 200 && data ? (data.sessions || []).find((r) => r.key === key) || null : null;
    const machine = row ? (data.machines || []).find((m) => m.id === row.machine_id) || null : null;
    return { status, data, row, machine };
  }

  /* The catalog row: ownership, attachable and status as the hub has them
   * now, not as the list had them when it loaded. */
  async function loadRow(gen) {
    const { status, data, row, machine } = await fetchRow(chat.key);
    if (gen !== chat.gen) return;
    if (status === 401) return status;
    if (status === 404 || (status === 200 && data && !row)) {
      showError("This session isn't in the hub's catalog. It may have ended, or its machine may be offline.");
      return 404;
    }
    if (status !== 200 || !data) {
      showError(errorText(data) || (status ? `The hub answered ${status}.` : "Couldn't reach the hub. Check the connection, then reload."));
      return status;
    }
    chat.row = Object.assign({}, chat.row || {}, row, machine && machine.name ? { machine: machine.name } : {});
    chat.rowKnown = true;
    unreadHook("seen", chat.key, row.last_active_at);
    updateOwnership();
    // The folder is known now: absolute page paths become previewable.
    repaintPreviewButtons();
    return 200;
  }

  function mirroredEmptyHtml() {
    return `<p class="empty chat-empty-calm arrive" id="chat-loading" data-mirrored="1">${esc(MIRRORED_EMPTY)}</p>`;
  }

  /* A chat opened from a link learns it is mirrored only when the catalog
   * row lands, after the log already said "No transcript" or "Couldn't
   * load": say the calm thing instead. A skeleton still loading, and a log
   * with messages, are left alone. */
  function settleMirroredLog() {
    const el = $("chat-loading");
    if (!el || el.dataset.mirrored || !el.classList.contains("empty")) return;
    el.outerHTML = mirroredEmptyHtml();
  }

  async function backfill(gen) {
    const { status, data } = await api(`${sessionPath(chat.key, "messages")}?limit=500`);
    if (gen !== chat.gen) return status;
    const loading = $("chat-loading");
    if (status !== 200 || !data) {
      if (loading) loading.outerHTML = isMirrored() ? mirroredEmptyHtml()
        : `<p class="empty" id="chat-loading">${status === 404 ? "No transcript for this session." : "Couldn't load the transcript."}</p>`;
      return status;
    }
    const messages = data.messages || [];
    for (const ev of messages) appendSafely(ev, false);
    if (messages.length) remember(messages[messages.length - 1].seq);
    else if (loading) loading.outerHTML = isMirrored() ? mirroredEmptyHtml() : '<p class="empty" id="chat-loading">No messages yet.</p>';
    scrollBottom();
    return status;
  }

  async function open(key, opts = {}) {
    close();
    const gen = ++chat.gen;
    chat.key = key;
    chat.retries = 0;
    chat.details = opts.details || null;
    chat.row = opts.row ? Object.assign({}, opts.row) : null;
    // This browser's read cursor (#1439): now, and as replies arrive.
    unreadHook("opened", key, chat.row ? chat.row.last_active_at : 0);
    chat.rowKnown = false;
    chat.awaitingReply = false;
    chat.seq = 0;
    chat.history = null;
    chat.replies = replyStreams();
    chat.streamBox = null;
    // Set once the opening reads are done and the stream is started; a
    // reconnect before that would replay what the backfill is drawing.
    chat.opened = false;
    chat.toolCards.clear();
    chat.earlier = [];
    chat.keep = LOG_KEEP;
    chat.confirmAttach = false;
    chat.attaching = false;
    chat.stopping = false;
    chat.detaching = false;
    chat.connState = "connecting";
    chat.rail = { loading: false, error: "" };
    // Fixed-height shell: the window stops scrolling, the log takes it.
    document.body.classList.add("chat-open");
    paintShell();
    updateOwnership();
    // The rail lists what the page already knows at once, and fills in
    // after its own fetch when the list never loaded (a chat from a link).
    loadRail(gen);
    // The / menu's list is warmed now, not on the first /, so the menu
    // opens filled; a kept list shows at once and is still refreshed.
    if (!isMirrored()) warmCommands();
    // The catalog is read after the backfill, never before: an attach or a
    // status change journaled before the backfill's last message is then
    // already in the row, and one after it comes with the stream.
    const logStatus = await backfill(gen);
    if (gen !== chat.gen) return;
    // A mirrored copy has no approval, send queue or live stream to read.
    const [rowStatus, waiting] = logStatus === 401 ? [401, ""]
      : isMirrored() ? [await loadRow(gen), ""]
      : await Promise.all([loadRow(gen), pendingApproval(gen), loadQueue(gen)]);
    if (gen !== chat.gen) return;
    if (rowStatus === 401 || logStatus === 401) {
      gate();
      return;
    }
    if (rowStatus === 404) { setConn("err"); return; }
    if (isMirrored()) return;
    // The stream starts after the last message backfill showed, so an
    // approval asked or a result reported since is replayed into view. A
    // waiting approval may be older than that message: then the stream
    // replays from the start, and only that ask is taken from the part
    // the backfill already showed.
    chat.opened = true;
    const shown = waiting && $("chat-log").querySelector(`.msg.approval[data-req="${CSS.escape(waiting)}"]`);
    if (waiting && !shown && chat.seq) {
      chat.history = { until: chat.seq, rid: waiting, prompt: "", seq: 0, resolved: false };
      openStream(gen, 0);
    } else {
      openStream(gen, chat.seq);
    }
  }

  function close() {
    chat.gen++;
    chat.held = null;
    closeStream();
    if (chat.key) unreadHook("closed", chat.key);
    document.body.classList.remove("chat-open");
    chat.key = null;
    chat.row = null;
    chat.toolCards.clear();
    chat.earlier = [];
    resetMenus();
    resetUploads();
    resetTray();
  }

  /* The hub refused this browser's credential: the chat closes first
   * (stream, reveal timers, chat-open) so nothing keeps running behind
   * the pair card, then the card takes the panel. */
  function gate() {
    close();
    renderGate();
  }

  /* ------------------------------------------------------------- composer */

  function growComposer() {
    const input = $("chat-text");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }

  function bindComposer() {
    const form = $("chat-composer");
    const input = $("chat-text");
    input.oninput = growComposer;
    input.onkeydown = (e) => {
      if (menu.open) {
        const rows = menu.rows.length;
        // Enter waits only while the first fetch is still out; an
        // answered list with nothing pickable sends the text as typed.
        const waiting = menu.token.kind === "mention"
          ? files.loading && files.answered === null
          : cmds.loading && !(cmds.response && cmds.key === chat.key);
        const key = menuKey(e, rows, waiting);
        if (key === "dismiss") {
          e.preventDefault();
          menu.dismissed = menu.token;
          closeMenu();
          return;
        }
        if (key === "next" || key === "prev") {
          e.preventDefault();
          menu.active = (menu.active + (key === "next" ? 1 : rows - 1)) % rows;
          renderMenu();
          return;
        }
        // While the menu is open Enter picks, or waits for the first
        // fetch; it sends once the list answered empty, and once the
        // menu is closed (Escape sends the text as typed).
        if (key === "pick" || key === "wait") {
          e.preventDefault();
          if (key === "pick") pickMenu(menu.active);
          return;
        }
      }
      if (e.key === "Escape" && chat.tray.editingId) {
        e.preventDefault();
        cancelEdit();
        return;
      }
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        form.requestSubmit();
      }
    };
    input.addEventListener("input", trackMenus);
    input.addEventListener("focus", trackMenus);
    // The caret moved without typing: an arrow key or a click in the text.
    input.addEventListener("click", trackMenus);
    input.addEventListener("keyup", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") trackMenus();
    });
    // Closing on blur waits a beat, so a click on a row still lands.
    input.addEventListener("blur", () => {
      setTimeout(() => { if (document.activeElement !== input) closeMenu(); }, 150);
    });
    // A row picks on a completed click: pointerdown only keeps the box's
    // focus, and a drag that scrolls the list picks nothing.
    const box = $("chat-menu");
    let menuTap = null;
    box.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const row = e.target.closest(".menu-row");
      menuTap = row ? { i: Number(row.dataset.i), x: e.clientX, y: e.clientY, id: e.pointerId } : null;
    });
    box.addEventListener("pointermove", (e) => {
      if (menuTap && e.pointerId === menuTap.id &&
          Math.hypot(e.clientX - menuTap.x, e.clientY - menuTap.y) > 8) menuTap = null;
    });
    box.addEventListener("pointercancel", () => { menuTap = null; });
    box.addEventListener("pointerup", (e) => {
      const tap = menuTap;
      menuTap = null;
      if (!tap || e.pointerId !== tap.id) return;
      const row = e.target.closest(".menu-row");
      if (row && Number(row.dataset.i) === tap.i) pickMenu(tap.i);
    });
    form.onsubmit = (e) => { e.preventDefault(); send(); };
    $("chat-stop").onclick = interrupt;
    bindAttachments(form, input);
  }

  /* ---------------------------------------------------------- attachments
     Files ride a message the iPhone's way: each is uploaded on its own
     through POST .../attachments (raw bytes, the name in ?name=), which
     stages it on the session's machine and answers a `ref`; the send
     then lists the refs in `attachments`, and the hub's injector puts
     `@path` in front of the text. The hub's caps are checked here first
     so a refusal reads plainly (attachments.py: MAX_BYTES, ALLOWED_EXTS). */

  const ATTACH = {
    maxBytes: 20 * 1024 * 1024,
    exts: [".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".pdf",
      ".txt", ".md", ".csv", ".json", ".log", ".diff", ".patch"],
  };

  /* A name's extension as the hub reads it (Path.suffix, lowercased): a
   * dotfile such as ".png" has none. */
  function fileExt(name) {
    const base = String(name || "").split(/[\\/]/).pop();
    const dot = base.lastIndexOf(".");
    return dot > 0 ? base.slice(dot).toLowerCase() : "";
  }

  /* Why the hub would refuse this file, or "". */
  function attachProblem(file) {
    if (!file || !file.size) return "The file is empty";
    if (file.size > ATTACH.maxBytes) return "Too big: files can be up to 20 MB";
    const ext = fileExt(file.name);
    if (!ATTACH.exts.includes(ext)) {
      return `${ext ? `${ext} files` : "Files without an extension"} can't be attached: images, PDFs and text only`;
    }
    return "";
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
    const mb = n / (1024 * 1024);
    return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  }

  /* The line a failed upload's chip shows. */
  function uploadError(status, data) {
    if (status === 413) return "Too big: files can be up to 20 MB";
    if (!status) return "Upload failed: connection lost";
    const msg = errorText(data);
    if (msg) return msg.charAt(0).toUpperCase() + msg.slice(1);
    return status === 404 ? "Update the hub to attach files from here" : `Upload failed (${status})`;
  }

  /* The composer's files, in the order they were added. Each item is
   * { id, name, size, state: "uploading" | "ready" | "failed", ref,
   * error, ctrl }. `note` is a one-off line under the chips. */
  const uploads = { items: [], seq: 0, note: "" };

  const uploading = () => uploads.items.some((it) => it.state === "uploading");

  function resetUploads() {
    for (const it of uploads.items) if (it.ctrl) it.ctrl.abort();
    uploads.items = [];
    uploads.note = "";
    paintFiles();
  }

  /* Adds files from the picker, a drop or a paste. Ones the hub would
   * refuse show as failed chips at once; the rest upload side by side.
   * Returns the uploads it started, for tests. */
  function addFiles(list) {
    const input = $("chat-text");
    if (!chat.key || !input || input.disabled) return [];
    uploads.note = "";
    const started = [];
    for (const file of Array.from(list || [])) {
      const item = { id: ++uploads.seq, name: file.name || "file", size: file.size || 0,
        state: "uploading", ref: "", error: "", ctrl: null };
      const problem = attachProblem(file);
      uploads.items.push(item);
      if (problem) {
        item.state = "failed";
        item.error = problem;
      } else {
        started.push(upload(item, file));
      }
    }
    paintFiles();
    paintSendState();
    return started;
  }

  async function upload(item, file) {
    const key = chat.key, gen = chat.gen;
    const ctrl = typeof AbortController === "function" ? new AbortController() : null;
    item.ctrl = ctrl;
    let status = 0, data = null;
    try {
      const resp = await fetch(`${sessionPath(key, "attachments")}?name=${encodeURIComponent(item.name)}`, {
        method: "POST", credentials: "same-origin",
        headers: authHeaders({ "Content-Type": file.type || "application/octet-stream" }),
        body: file, signal: ctrl ? ctrl.signal : undefined,
      });
      status = resp.status;
      try { data = await resp.json(); } catch { /* empty body */ }
    } catch {
      status = 0;
    }
    // Removed, or another chat opened, while it was in flight.
    if (gen !== chat.gen || !uploads.items.includes(item)) return;
    item.ctrl = null;
    // A satellite session answers 202: the hub queued the bytes for it.
    if ((status === 200 || status === 202) && data && typeof data.ref === "string" && data.ref) {
      item.state = "ready";
      item.ref = data.ref;
      if (typeof data.size === "number") item.size = data.size;
    } else {
      item.state = "failed";
      item.error = uploadError(status, data);
      noteRefusal(data);
    }
    paintFiles();
    paintSendState();
  }

  function removeUpload(id) {
    const item = uploads.items.find((it) => it.id === id);
    if (!item) return;
    if (item.ctrl) item.ctrl.abort();
    uploads.items = uploads.items.filter((it) => it !== item);
    uploads.note = "";
    paintFiles();
    paintSendState();
  }

  function paintFiles() {
    const box = $("chat-files");
    if (!box) return;
    box.classList.toggle("hidden", !uploads.items.length && !uploads.note);
    box.innerHTML = uploads.items.map((it) => {
      const meta = it.state === "uploading" ? "Uploading…" : it.state === "failed" ? it.error : formatBytes(it.size);
      return `<span class="att-chip ${it.state}" data-id="${it.id}">` +
        `<span class="att-name" title="${esc(it.name)}">${esc(it.name)}</span>` +
        `<span class="att-meta">${esc(meta)}</span>` +
        `<button type="button" class="att-x" data-id="${it.id}" aria-label="Remove ${esc(it.name)}">` +
        `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7 7 17"/></svg></button></span>`;
    }).join("") + (uploads.note ? `<div class="att-note">${esc(uploads.note)}</div>` : "");
  }

  /* Send waits while any file is still uploading. */
  function paintSendState() {
    const btn = $("chat-send");
    const input = $("chat-text");
    if (!btn || !input) return;
    const busy = uploading();
    btn.disabled = !chat.rowKnown || input.disabled || busy;
    btn.title = busy ? "Waiting for files to upload" : "";
  }

  function bindAttachments(form, input) {
    const picker = $("chat-file");
    picker.accept = ATTACH.exts.join(",");
    $("chat-clip").onclick = () => { if (!input.disabled) picker.click(); };
    picker.onchange = () => {
      addFiles(picker.files);
      picker.value = "";
    };
    $("chat-files").addEventListener("click", (e) => {
      const x = e.target.closest(".att-x");
      if (x) removeUpload(Number(x.dataset.id));
    });
    // Dropped files: only a drag that carries files lights the box.
    const carriesFiles = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    form.addEventListener("dragover", (e) => {
      if (!carriesFiles(e) || input.disabled) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      form.classList.add("dropping");
    });
    form.addEventListener("dragleave", (e) => {
      if (!form.contains(e.relatedTarget)) form.classList.remove("dropping");
    });
    form.addEventListener("drop", (e) => {
      form.classList.remove("dropping");
      if (!carriesFiles(e)) return;
      e.preventDefault();
      addFiles(e.dataTransfer.files);
    });
    // A pasted screenshot attaches; a paste that also carries text (a
    // copy from a document) stays a text paste.
    input.addEventListener("paste", (e) => {
      const data = e.clipboardData;
      if (!data || !data.files || !data.files.length) return;
      if (Array.from(data.types || []).includes("text/plain")) return;
      e.preventDefault();
      addFiles(data.files);
    });
  }

  /* A send the hub refused gives its files back to the composer, ahead
   * of any added since, so the next try needn't upload them again. Not
   * after a lost connection (the hub may have taken it), a refused ref
   * (`bad_attachment`: the file is gone) or a detach. */
  function restoreUploads(sent, status, data) {
    const input = $("chat-text");
    const code = data && data.error && data.error.code;
    if (!sent.length || !status || code === "bad_attachment" || !input || input.disabled) return;
    const held = new Set(uploads.items.map((it) => it.ref).filter(Boolean));
    uploads.items = sent.filter((it) => !held.has(it.ref)).concat(uploads.items);
    paintFiles();
    paintSendState();
  }

  /* The message body: the refs of every uploaded file ride along. */
  function messageBody(text, mode, clientId, items) {
    const body = { text, mode, client_id: clientId };
    const refs = items.filter((it) => it.state === "ready").map((it) => it.ref);
    if (refs.length) body.attachments = refs;
    return body;
  }

  function appendOptimistic(text, clientId, names = []) {
    const loading = $("chat-loading");
    if (loading) loading.remove();
    const div = document.createElement("div");
    div.className = "msg user pending arrive";
    div.dataset.clientId = clientId;
    div.dataset.text = text;
    div.dataset.sentAt = String(Math.floor(Date.now() / 1000));
    div.innerHTML = `<div class="text">${md(text)}</div>` +
      (names.length ? `<div class="msg-files">${names.map((n) => `<span>${esc(n)}</span>`).join("")}</div>` : "") +
      `<div class="send-note">Sending…</div>`;
    logAppend($("chat-log"), div);
    scrollBottom();
    return div;
  }

  function failBubble(bubble, text) {
    bubble.classList.remove("pending");
    bubble.classList.add("failed");
    const note = bubble.querySelector(".send-note") || document.createElement("div");
    note.className = "send-note";
    note.textContent = text;
    bubble.appendChild(note);
    announceFailure(text);
  }

  async function send() {
    const input = $("chat-text");
    const text = input.value.trim();
    if (!chat.key || input.disabled) return;
    if (chat.tray.editingId) { if (text) saveEdit(); return; }
    const sent = uploads.items.filter((it) => it.state === "ready");
    // Enter submits even with Send disabled; the files go with this
    // message, so it waits for them.
    if (uploading() || (!text && sent.length)) {
      uploads.note = uploading() ? "Waiting for files to upload" : "Add a message to send with the files";
      paintFiles();
      return;
    }
    if (!text) return;
    input.value = "";
    growComposer();
    // An open / menu's token is gone with the text.
    trackMenus();
    chat.tray.editNote = "";
    chat.tray.note = "";
    paintTray();
    sendText(text, $("chat-mode").value, sent);
  }

  /* A Queue-mode send while the agent works shows in the tray, anything
   * else as a bubble in the transcript; the hub's answer can move it
   * (posted). */
  async function sendText(text, mode, sent = []) {
    const clientId = newClientId();
    const gen = chat.gen, key = chat.key;
    const names = sent.map((it) => it.name);
    const body = messageBody(text, mode, clientId, sent);
    // Sent files leave the composer; failed ones were never part of it.
    if (sent.length) { resetUploads(); paintSendState(); }
    let bubble = null;
    if (sendsToTray(mode, chat.row)) {
      chat.tray.queued.push({ cid: clientId, text, files: names, phase: "posting", note: "", since: ++chat.tray.clock });
      paintTray();
    } else {
      bubble = appendOptimistic(text, clientId, names);
      chat.awaitingReply = true;
      updateWorking();
    }
    const { status, data } = await post(sessionPath(key, "message"), body);
    // Another chat may be open by now; its tray and dots aren't ours.
    if (gen !== chat.gen) return;
    // A fast command.result may already have settled the send (its row
    // left the tray, or its bubble lost .pending): posted() leaves it.
    if (status === 202) { posted(clientId, bubble, !!(data && data.waiting)); return; }
    const why = errorText(data) || (status ? `Send failed (${status})` : "Request failed: connection lost");
    noteRefusal(data);
    restoreUploads(sent, status, data);
    if (trayPostFailed(clientId, why)) return;
    if (!bubble || !bubble.classList.contains("pending")) return;
    chat.awaitingReply = false;
    updateWorking();
    failBubble(bubble, why);
  }

  async function interrupt() {
    if (!chat.key || chat.stopping) return;
    const gen = chat.gen;
    chat.stopping = true;
    updateOwnership();
    const { status, data } = await post(sessionPath(chat.key, "control"), { op: "interrupt" });
    if (gen !== chat.gen) return;
    chat.stopping = false;
    updateOwnership();
    if (status !== 200 && status !== 202) {
      appendMessage({ kind: "error", text: `Stop failed: ${errorText(data) || (status ? `the hub answered ${status}` : "connection lost")}` }, true);
      noteRefusal(data);
      scrollBottom();
    }
  }

  async function attach() {
    if (!chat.key || chat.attaching || isMirrored()) return;
    const gen = chat.gen;
    chat.attaching = true;
    updateOwnership();
    const { status, data } = await post(sessionPath(chat.key, "control"), { op: "attach" });
    if (gen !== chat.gen) return;
    chat.attaching = false;
    chat.confirmAttach = false;
    if (status === 200) {
      // The hub also emits session.ownership; its answer is enough to
      // open the composer now. A kept pane is adopted under its old tmux
      // name, and the note names a fresh one ("attached under tmux swe-…",
      // "qv-…" from a hub before the rename), so the chip can show before
      // the ownership event lands.
      chat.detaching = false;
      chat.row.ownership = "attached";
      if (chat.row.kept) delete chat.row.kept;
      const m = data && String(data.note || "").match(/\b(?:swe|qv)-[a-z0-9]+\b/i);
      if (m) chat.row.tmux = m[0];
    } else if (status === 202) {
      // The attach went to a satellite's command journal: ownership
      // changes when its command.result lands.
      appendMessage({ kind: "system", text: "Attach queued: waiting for the satellite" }, true);
    } else {
      appendMessage({ kind: "error", text: `Attach failed: ${errorText(data) || (status ? `the hub answered ${status}` : "connection lost")}` }, true);
    }
    updateOwnership();
    scrollBottom();
  }

  /* --------------------------------------------------------- queued tray
     The iPhone's Queued tray (ChatModel's queue half, ChatView's
     QueuedTray): a Queue-mode send made while the agent works waits
     above the composer instead of in the transcript, until the hub
     sends it on idle. Each row can be edited (its text goes into the
     box, Send becomes Save), removed, or sent now (written into the
     running turn as a steer); the header removes them all. The hub
     holds the sends, so the tray is read again from GET .../queue when
     the chat opens. Rows are {cid, text, phase, note}: phase is posting
     (the POST is out), waiting, sendingNow, removing (hidden while the
     hub confirms) or left (the hub says it already went). */

  function freshTray() {
    return {
      queued: [],
      // Sends that left the hub's queue while the chat was closed; kept
      // so a replayed echo or result still lands in the transcript.
      departed: [],
      // Client ids that left the tray: a queue list fetched before that
      // can't bring them back.
      settled: new Set(),
      // Live echoes that matched no row while a queue list was on its way
      // (`listing`).
      unmatched: [], listing: false,
      // The edit whose older-hub fallback (remove, then queue again) is
      // out: its own removal doesn't end the edit.
      requeueing: null,
      // Ticks when a queue read starts and when a row is staged: a row
      // newer than the read (`since` above its mark) isn't in its answer
      // yet, so the answer can't move it out.
      clock: 0,
      editingId: null, draftBeforeEdit: "", savingEdit: false,
      editNote: "", note: "", confirmAll: null,
    };
  }
  chat.tray = freshTray();

  /* An echoed user message is the stored text, which attachments prefix
   * with "@path ", hence the endsWith. */
  function echoes(stored, sent) {
    if (!sent) return false;
    return stored === sent || (stored || "").endsWith(sent);
  }

  /* A transcript message is an optimistic bubble's echo when its text
   * matches and it was stored no earlier than the send (give or take
   * clock skew), so an older identical message isn't taken for it. */
  function echoesBubble(msg, bubble) {
    if (!echoes(msg.text, bubble.dataset.text)) return false;
    const sentAt = Number(bubble.dataset.sentAt || 0);
    return !msg.ts || !sentAt || msg.ts >= sentAt - 5;
  }

  /* Where a send shows before the hub answers: the tray for Queue mode
   * while the agent works on a session this hub holds sends for, else
   * the transcript. A satellite's sends wait on the satellite, so they
   * never join the tray (the iPhone's holdsQueuedSends). */
  function sendsToTray(mode, row) {
    return mode === "queue" && !!row && row.status === "active" && !row.remote;
  }

  /* The hub's list wins: what it holds shows in its order (keeping what
   * this page knows about each, but the hub's text, which another device
   * may have edited), and what it no longer holds leaves for `departed`,
   * except a send whose POST hasn't been answered yet. `known` are sends
   * shown elsewhere or gone, which a list can't bring back; a send whose
   * echo arrived while the list was on its way (`unmatched`) is gone too,
   * and a row staged after the read began (`since` above `mark`) stays.
   * Pure: answers {queued, departed, gone} for the caller to apply. */
  function mergeQueue(local, departedBefore, sends, known, unmatched, mark = Infinity) {
    const queued = [], gone = [];
    const echoed = unmatched.slice();
    for (const send of sends) {
      const cid = send.client_id;
      if (!cid || known.has(cid) || queued.some((q) => q.cid === cid)) continue;
      const mine = local.find((q) => q.cid === cid);
      if (!mine) {
        const i = echoed.findIndex((t) => echoes(t, send.text));
        if (i >= 0) { echoed.splice(i, 1); gone.push(cid); continue; }
      }
      // An edit saved here after the read began is newer than its text.
      const text = send.text == null || (mine && (mine.editedAt || 0) > mark)
        ? (mine ? mine.text : "") : send.text;
      if (mine) queued.push({ ...mine, text, phase: mine.phase === "posting" ? "waiting" : mine.phase });
      else queued.push({ cid, text, phase: "waiting", note: "" });
    }
    const departed = departedBefore.slice();
    for (const mine of local) {
      if (queued.some((q) => q.cid === mine.cid)) continue;
      if (mine.phase === "posting" || (mine.since || 0) > mark) queued.push(mine);
      else if (!known.has(mine.cid) && !departed.some((d) => d.cid === mine.cid)) departed.push(mine);
    }
    return { queued, departed, gone };
  }

  /* The short note a tray row shows when the hub refuses Send now or
   * Remove (the iPhone's queueNote). */
  function queueNote(status, data) {
    const e = (data && data.error) || {};
    switch (e.code || (status ? "" : "network")) {
      case "not_waiting": {
        const why = String(e.message || "").toLowerCase();
        if (why.includes("removed")) return "Already removed";
        if (why.includes("failed")) return "Already failed";
        return "Already sent";
      }
      case "not_attached": return "Attach to send now";
      case "queue_unsupported": return "Not available on this machine";
      case "backlog_full": return "Too many waiting. Try again shortly";
      case "unknown_send": return "The hub lost this message";
      case "network": return "Can't reach the hub";
      default: return e.message || "Something went wrong";
    }
  }

  async function loadQueue(gen) {
    const t = chat.tray;
    // Only echoes that arrive while this list is on its way can be
    // missing from it.
    t.unmatched = [];
    t.listing = true;
    const mark = ++t.clock;
    const { status, data } = await api(sessionPath(chat.key, "queue"));
    if (gen !== chat.gen) return;
    t.listing = false;
    // An older hub has no queue route: the tray shows this page's sends.
    if (status === 200 && data) {
      const known = new Set(t.settled);
      for (const b of $("chat-log").querySelectorAll(".msg.user[data-client-id]")) known.add(b.dataset.clientId);
      const merged = mergeQueue(t.queued, t.departed, data.sends || [], known, t.unmatched, mark);
      t.queued = merged.queued;
      t.departed = merged.departed;
      for (const cid of merged.gone) t.settled.add(cid);
      if (t.editingId && !t.queued.some((q) => q.cid === t.editingId && q.phase === "waiting")) cancelEdit();
      paintTray();
    }
    t.unmatched = [];
  }

  function resetTray() {
    const t = chat.tray;
    clearTimeout(t.confirmAll);
    chat.tray = freshTray();
  }

  /* A send that left the hub's queue: out of the tray (or the hidden
   * departed list), remembered as settled. An edit of it ends, its text
   * left in the box to send as a new message. */
  function takeQueued(cid, gone = "That message already went out") {
    const t = chat.tray;
    for (const list of ["queued", "departed"]) {
      const entry = t[list].find((q) => q.cid === cid);
      if (!entry) continue;
      t[list] = t[list].filter((q) => q !== entry);
      t.settled.add(cid);
      if (t.editingId === cid && t.requeueing !== cid) {
        t.editingId = null;
        t.draftBeforeEdit = "";
        t.editNote = gone;
      }
      paintTray();
      return entry;
    }
    return null;
  }

  /* A live user message: a queued send went out, so it leaves the tray
   * and the echo lands in the transcript where it really is. */
  function echoLeavesTray(text) {
    const t = chat.tray;
    for (const list of ["departed", "queued"]) {
      // Two rows can hold the same text: the one sent now is the one that
      // went out first, else the oldest, as the hub sends them in order.
      const hits = t[list].filter((q) => echoes(text, q.text));
      const entry = hits.find((q) => q.phase === "sendingNow") || hits[0];
      if (entry) { takeQueued(entry.cid); return; }
    }
    if (t.listing) t.unmatched.push(text);
  }

  /* The hub took the send; `waiting` says whether it holds it for idle.
   * Where it shows follows the hub, not the status the page last saw. */
  function posted(cid, bubble, waiting) {
    const t = chat.tray;
    const entry = t.queued.find((q) => q.cid === cid);
    if (entry) {
      if (entry.phase !== "posting") return;
      if (waiting) {
        entry.phase = "waiting";
      } else {
        // The agent was idle after all: it goes out now.
        t.queued = t.queued.filter((q) => q !== entry);
        const b = appendOptimistic(entry.text, cid, entry.files || []);
        b.querySelector(".send-note").textContent = "Sent";
        chat.awaitingReply = true;
        updateWorking();
      }
      paintTray();
      return;
    }
    if (!bubble || !bubble.classList.contains("pending")) return;
    if (waiting && !t.settled.has(cid)) {
      // The agent was busy after all: it waits in the tray.
      bubble.remove();
      t.queued.push({ cid, text: bubble.dataset.text, phase: "waiting", note: "", since: ++t.clock });
      chat.awaitingReply = false;
      updateWorking();
      paintTray();
      return;
    }
    const note = bubble.querySelector(".send-note");
    if (note) note.textContent = "Sent";
  }

  /* The POST failed for a send staged in the tray: a failed bubble in
   * the transcript instead. False when the tray doesn't hold it. */
  function trayPostFailed(cid, why) {
    const t = chat.tray;
    const entry = t.queued.find((q) => q.cid === cid);
    if (!entry) return false;
    t.queued = t.queued.filter((q) => q !== entry);
    t.settled.add(cid);
    paintTray();
    failBubble(appendOptimistic(entry.text, cid, entry.files || []), why);
    return true;
  }

  /* The tray's row for `cid` now: a queue read replaces the row objects,
   * so an action re-finds its row after its request. */
  const trayRow = (cid) => chat.tray.queued.find((q) => q.cid === cid) || null;

  async function queueAction(cid, op, text) {
    const body = text === undefined ? { client_id: cid, op } : { client_id: cid, op, text };
    const { status, data } = await post(sessionPath(chat.key, "queue"), body);
    return status === 200 ? null : { status, data, code: (data && data.error && data.error.code) || "" };
  }

  /* Send now: the hub stops holding it and writes it into the running
   * turn. It reads "Sending now" until its result or echo arrives. */
  async function sendNow(cid) {
    const t = chat.tray;
    const entry = t.queued.find((q) => q.cid === cid);
    if (!entry || entry.phase !== "waiting") return;
    const gen = chat.gen;
    if (t.editingId === cid) cancelEdit();
    entry.phase = "sendingNow";
    entry.note = "";
    t.note = "";
    paintTray();
    const err = await queueAction(cid, "steer");
    if (!err || gen !== chat.gen) return;
    if (err.status === 401) return gate();
    noteRefusal(err.data);
    const row = trayRow(cid);
    if (!row || row.phase !== "sendingNow") return;
    row.phase = err.code === "not_waiting" ? "left" : "waiting";
    row.note = queueNote(err.status, err.data);
    paintTray();
  }

  /* Remove: gone from the tray at once, back with a note if the hub
   * refuses. Its command.result says cancelled, never a failure. */
  async function removeQueued(cid) {
    const t = chat.tray;
    const entry = t.queued.find((q) => q.cid === cid);
    if (!entry) return;
    if (entry.phase === "left") { dropQueued(cid); return; } // nothing to ask
    if (entry.phase !== "waiting") return;
    const gen = chat.gen;
    if (t.editingId === cid) cancelEdit();
    entry.phase = "removing";
    entry.note = "";
    t.note = "";
    paintTray();
    const err = await queueAction(cid, "cancel");
    if (gen !== chat.gen) return;
    if (!err) { dropQueued(cid); return; }
    if (err.status === 401) return gate();
    const row = trayRow(cid);
    if (!row || row.phase !== "removing") return;
    // The hub never held it, or forgot it: there's nothing to remove.
    if (err.code === "unknown_send") { dropQueued(cid); return; }
    row.phase = err.code === "not_waiting" ? "left" : "waiting";
    row.note = queueNote(err.status, err.data);
    paintTray();
  }

  function dropQueued(cid) {
    const t = chat.tray;
    t.settled.add(cid);
    t.queued = t.queued.filter((q) => q.cid !== cid);
    paintTray();
  }

  /* Remove all: the first click arms it for a few seconds and asks; the
   * second removes every row the hub still holds, each on its own. */
  function removeAllQueued() {
    const t = chat.tray;
    if (t.confirmAll === null) {
      t.confirmAll = setTimeout(() => { chat.tray.confirmAll = null; paintTray(); }, 3000);
      paintTray();
      return;
    }
    clearTimeout(t.confirmAll);
    t.confirmAll = null;
    for (const q of [...t.queued]) {
      if (q.phase === "waiting" || q.phase === "left") removeQueued(q.cid);
    }
    paintTray();
  }

  /* Edit: the message's text goes into the box, and Send saves it back.
   * Only a send the hub still holds can be edited. */
  function beginEdit(cid) {
    const t = chat.tray;
    const entry = t.queued.find((q) => q.cid === cid);
    const input = $("chat-text");
    if (!entry || entry.phase !== "waiting" || !input) return;
    if (!t.editingId) t.draftBeforeEdit = input.value;
    t.editingId = cid;
    t.editNote = "";
    input.value = entry.text;
    growComposer();
    trackMenus();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    paintTray();
  }

  function cancelEdit() {
    const t = chat.tray;
    // Not while a save is out: its answer decides what the box holds.
    if (!t.editingId || t.savingEdit) return;
    t.editingId = null;
    const input = $("chat-text");
    if (input) { input.value = t.draftBeforeEdit; growComposer(); }
    t.draftBeforeEdit = "";
    t.editNote = "";
    paintTray();
  }

  async function saveEdit() {
    const t = chat.tray;
    const cid = t.editingId;
    const input = $("chat-text");
    const text = input.value.trim();
    if (!cid || t.savingEdit || !text) return;
    const entry = t.queued.find((q) => q.cid === cid);
    if (!entry || entry.phase !== "waiting") { editWentOut(cid, text); return; }
    const gen = chat.gen;
    t.savingEdit = true;
    paintTray();
    const err = await queueAction(cid, "edit", text);
    if (gen !== chat.gen) return;
    if (!err || !(err.status === 400 || err.status === 422)) t.savingEdit = false;
    if (!err) {
      // The row keeps its place with the new text.
      const row = trayRow(cid);
      if (row) { row.text = text; row.note = ""; row.editedAt = ++t.clock; }
      if (t.editingId === cid) cancelEdit();
      paintTray();
      return;
    }
    if (err.status === 401) return gate();
    if (err.code === "not_waiting") { editWentOut(cid, text); return; }
    // An older hub can't edit in place: remove the message, then queue
    // the new text, which lands at the end. It stays saving meanwhile.
    if (err.status === 400 || err.status === 422) { requeueEdit(cid, text, gen); return; }
    t.editNote = queueNote(err.status, err.data);
    paintTray();
  }

  /* Too late to edit: the new text stays in the box to send as a new
   * message. */
  function editWentOut(cid, text) {
    const t = chat.tray;
    const entry = t.queued.find((q) => q.cid === cid);
    if (entry && entry.phase === "waiting") {
      entry.phase = "left";
      entry.note = "Already sent";
    }
    t.editingId = null;
    t.draftBeforeEdit = "";
    const input = $("chat-text");
    if (input) { input.value = text; growComposer(); }
    t.editNote = "That message already went out";
    paintTray();
  }

  /* Runs with savingEdit set, so the edit can't be cancelled under it,
   * and `requeueing` keeps the cancel's own command.result (which can
   * land before the request answers) from ending the edit. */
  async function requeueEdit(cid, text, gen) {
    const t = chat.tray;
    t.requeueing = cid;
    const err = await queueAction(cid, "cancel");
    if (gen !== chat.gen) return;
    t.requeueing = null;
    t.savingEdit = false;
    if (err) {
      if (err.code === "not_waiting") { editWentOut(cid, text); return; }
      t.editNote = queueNote(err.status, err.data);
      paintTray();
      return;
    }
    dropQueued(cid);
    if (t.editingId === cid) {
      t.editingId = null;
      const input = $("chat-text");
      if (input) { input.value = t.draftBeforeEdit; growComposer(); }
      t.draftBeforeEdit = "";
      t.editNote = "";
    }
    t.note = "Edited. It moved to the end of the queue";
    sendText(text, "queue");
  }

  const TRAY_ICON = {
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    remove: '<path d="M6 6l12 12M18 6 6 18"/>',
    sendNow: '<path d="M20 5v7a3 3 0 0 1-3 3H5"/><path d="m9 11-4 4 4 4"/>',
  };
  const trayIcon = (name) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${TRAY_ICON[name]}</svg>`;

  /* The tray and the edit bar, from chat.tray. Pure: the HTML of both. */
  function trayHtml(t) {
    const shown = t.queued.filter((q) => q.phase !== "removing");
    const n = shown.length;
    const btn = (act, label, icon, off = "") =>
      `<button type="button" class="tray-btn" data-act="${act}" aria-label="${label}" title="${label}"${off}>${trayIcon(icon)}</button>`;
    let tray = "";
    if (n) {
      const arming = t.confirmAll !== null;
      tray = `<div class="tray-head"><span class="caption">${n === 1 ? "1 message queued" : `${n} messages queued`}</span>
        <button type="button" class="tray-all${arming ? " arming" : ""}" data-act="remove-all">${
          arming ? (n === 1 ? "Remove it?" : `Remove all ${n}?`) : "Remove all"}</button></div>`
        + (t.note ? `<div class="tray-note">${esc(t.note)}</div>` : "")
        + `<div class="tray-rows">${shown.map((q) => {
          const editing = q.cid === t.editingId;
          const actions = (off) => btn("edit", "Edit", "edit", off) + btn("remove", "Remove", "remove", off)
            + btn("send-now", "Send now", "sendNow", off);
          // A refused action can be tried again: the buttons stay, the
          // note goes under the text.
          const retry = !!q.note && q.phase === "waiting";
          const right = retry ? actions("")
            : q.note ? `<span class="tray-row-note${q.phase === "left" ? " left" : ""}">${esc(q.note)}</span>${
              q.phase === "left" ? btn("remove", "Remove", "remove") : ""}`
              : q.phase === "sendingNow" ? '<span class="tray-row-note left">Sending now</span>'
                : actions(q.phase === "posting" ? " disabled" : "");
          return `<div class="tray-row${editing ? " editing" : ""}${q.phase === "posting" ? " posting" : ""}" data-cid="${esc(q.cid)}">
            <span class="tray-mark${q.phase === "sendingNow" ? " spin" : ""}" aria-hidden="true">${editing ? trayIcon("edit") : ""}</span>
            <span class="tray-col"><span class="tray-text">${esc(q.text.replace(/\s*\n\s*/g, " "))}</span>${
              retry ? `<span class="tray-row-note sub">${esc(q.note)}</span>` : ""}</span>${right}</div>`;
        }).join("")}</div>`;
    }
    const edit = t.editingId
      ? `<span class="caption">Editing a queued message${t.editNote ? ` · <span class="edit-note">${esc(t.editNote)}</span>` : ""}</span>
        <button type="button" class="link" data-act="cancel-edit"${t.savingEdit ? " disabled" : ""}>Cancel</button>`
      : t.editNote ? `<span class="caption edit-note">${esc(t.editNote)}</span>` : "";
    return { tray, edit };
  }

  /* The browser re-serialises innerHTML, so the markup last drawn into
   * an element is kept here to compare against. */
  const drawnHtml = new WeakMap();
  function drawOnce(el, html) {
    if (drawnHtml.get(el) === html) return;
    el.innerHTML = html;
    drawnHtml.set(el, html);
  }

  function paintTray() {
    const t = chat.tray;
    const tray = $("chat-tray"), bar = $("chat-editbar");
    if (!tray || !bar) return;
    if (!t.queued.some((q) => q.phase !== "removing") && t.confirmAll !== null) {
      clearTimeout(t.confirmAll);
      t.confirmAll = null;
    }
    const html = trayHtml(t);
    // Unchanged markup stays put: an ownership or status repaint calls
    // this too, and must not take the focus off a tray button.
    drawOnce(tray, html.tray);
    tray.classList.toggle("hidden", !html.tray);
    drawOnce(bar, html.edit);
    bar.classList.toggle("hidden", !html.edit);
    const input = $("chat-text"), sendBtn = $("chat-send");
    if (input && !input.disabled) input.placeholder = t.editingId ? "Edit the queued message" : "Message the session";
    if (sendBtn) {
      sendBtn.textContent = t.editingId ? (t.savingEdit ? "Saving…" : "Save") : "Send";
      if (t.savingEdit) sendBtn.disabled = true;
      // Send still waits for uploads in flight; an edit takes no files.
      else if (chat.rowKnown && chat.row && chat.row.ownership === "attached") sendBtn.disabled = !t.editingId && uploading();
    }
  }

  function bindTray() {
    const tray = $("chat-tray"), bar = $("chat-editbar");
    if (tray) {
      tray.onclick = (e) => {
        const b = e.target.closest("button[data-act]");
        if (!b || b.disabled) return;
        const cid = (b.closest(".tray-row") || { dataset: {} }).dataset.cid;
        const act = b.dataset.act;
        if (act === "remove-all") removeAllQueued();
        else if (act === "remove") removeQueued(cid);
        else if (act === "send-now") sendNow(cid);
        else if (act === "edit") beginEdit(cid);
      };
    }
    if (bar) {
      bar.onclick = (e) => {
        const b = e.target.closest("button[data-act=cancel-edit]");
        if (b && !b.disabled) cancelEdit();
      };
    }
  }

  /* -------------------------------------------------------- command menu
     Typing / as the box's first character lists the commands this
     session's harness runs (the hub's GET .../commands), filtered here
     as the name grows. The hub only lists them: a pick inserts
     "/name " (or the command's own `insert`), and the harness expands
     it when the message lands. Ported from the remote PWA's "/" menu.
     The same menu lists files for an `@path` typed anywhere in the box
     (the hub's GET .../files), with the same keys and rows; its token
     and insertion rules are the iPhone's (PromptCompletion.swift,
     `Mention`). Positions are offsets into the box's value; the caret
     is the input's selectionEnd. */

  /* A `/command` being typed: the box starts with / and the caret is
   * still in that first word. */
  function slashToken(text, caret) {
    if (caret < 1 || text[0] !== "/" || /\s/.test(text.slice(0, caret))) return null;
    let end = caret;
    while (end < text.length && !/\s/.test(text[end])) end++;
    return { start: 0, end, query: text.slice(1, caret) };
  }

  /* What the box gets on a pick, always ending in a space: /name, or
   * the hub's own `insert` (Codex skills are $name). */
  function commandInsert(c) {
    const text = c.insert || `/${c.name}`;
    return /\s$/.test(text) ? text : `${text} `;
  }

  const SOURCE_LABEL = { builtin: "Built-in", user: "User", project: "Project", skill: "Skill", plugin: "Plugin" };

  function sourceLabel(src) {
    if (!src) return "";
    return SOURCE_LABEL[src] || src.charAt(0).toUpperCase() + src.slice(1);
  }

  /* Commands whose name starts with the query, then those that hold it
   * anywhere, case-insensitively; the hub's order within each. */
  function filterCommands(commands, query) {
    const q = query.toLowerCase();
    if (!q) return commands;
    const name = (c) => c.name.toLowerCase();
    return commands.filter((c) => name(c).startsWith(q))
      .concat(commands.filter((c) => !name(c).startsWith(q) && name(c).includes(q)));
  }

  /* The box after picking a command, with the caret after its space,
   * ready for an argument. An existing space after the word is reused. */
  function insertCommand(text, token, c) {
    const end = Math.min(Math.max(token.end, 0), text.length);
    let inserted = commandInsert(c);
    const reuse = end < text.length && /\s/.test(text[end]);
    if (reuse) inserted = inserted.slice(0, -1);
    return { text: inserted + text.slice(end), caret: inserted.length + (reuse ? 1 : 0) };
  }

  /* A key with the / menu open: "dismiss", "next"/"prev" over the rows,
   * "pick", or "wait" while the first fetch is still out and the list is
   * empty. Null falls through to the box's usual keys — Shift-Enter's
   * newline, Enter's send on an answered-but-empty list, and a no-row
   * Tab's focus move among them. */
  function menuKey(e, rows, waiting) {
    if (e.isComposing) return null;
    if (e.key === "Escape") return "dismiss";
    if (rows && (e.key === "ArrowDown" || e.key === "ArrowUp"))
      return e.key === "ArrowDown" ? "next" : "prev";
    if (e.key === "Enter") return e.shiftKey ? null : rows ? "pick" : waiting ? "wait" : null;
    if (e.key === "Tab") return rows ? "pick" : null;
    return null;
  }

  /* An `@path` being typed: the @ sits at the start of the text or after
   * whitespace and runs to the caret with no whitespace in between, so
   * an email (a@b.com) never starts one. A path with spaces is quoted,
   * `@"My Documents/notes.txt"`, as Claude Code writes it; while a
   * folder is browsed the quote stays open (`@"My Documents/`). `end`
   * is the end of the word the caret is in, which a pick replaces. */
  function mentionToken(text, caret) {
    caret = Math.min(Math.max(caret, 0), text.length);
    const quoted = quotedMention(text, caret);
    if (quoted) return quoted;
    let start = caret;
    while (start > 0 && !/\s/.test(text[start - 1])) start--;
    if (start >= caret || text[start] !== "@") return null;
    let end = caret;
    while (end < text.length && !/\s/.test(text[end])) end++;
    return { start, end, query: text.slice(start + 1, caret) };
  }

  /* `@"` at the start or after whitespace, with no quote or newline
   * between it and the caret. It runs to its closing quote or, left
   * open, to the end of the word the caret is in. */
  function quotedMention(text, caret) {
    let q = caret - 1;
    while (q >= 0 && text[q] !== '"' && !/[\n\r]/.test(text[q])) q--;
    if (q < 1 || text[q] !== '"' || text[q - 1] !== "@") return null;
    const start = q - 1;
    if (start > 0 && !/\s/.test(text[start - 1])) return null;
    let end = caret;
    while (end < text.length && text[end] !== '"' && !/[\n\r]/.test(text[end])) end++;
    if (end < text.length && text[end] === '"') {
      end++;
    } else {
      end = caret;
      while (end < text.length && !/\s/.test(text[end])) end++;
    }
    return { start, end, query: text.slice(q + 1, caret) };
  }

  /* A path without its trailing slashes ("/" itself stays). */
  function trimmedSlash(path) {
    let p = String(path || "");
    while (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
    return p;
  }

  /* "src/app/main.py" as its name and the folder it's in. */
  function splitPath(path) {
    const clean = trimmedSlash(path);
    const slash = clean.lastIndexOf("/");
    return slash < 0 ? { name: clean, parent: "" } : { name: clean.slice(slash + 1), parent: clean.slice(0, slash) };
  }

  /* The box after picking `path`, and where the caret goes. A file
   * becomes `@path ` with the caret after the space (an existing space
   * is reused); a folder becomes `@path/` with the caret after the
   * slash, so the menu stays open to pick inside it. A path with
   * whitespace is quoted, `@"a b.txt" `, and a folder's quote is left
   * open, `@"a b/`, until a file closes it. */
  function insertMention(text, token, path, isDir) {
    const start = Math.min(token.start, text.length);
    const end = Math.min(Math.max(token.end, start), text.length);
    const clean = trimmedSlash(path);
    const quoted = /\s/.test(clean);
    let inserted = (quoted ? '@"' : "@") + clean;
    if (isDir) {
      inserted += "/";
    } else {
      if (quoted) inserted += '"';
      if (end >= text.length || !/\s/.test(text[end])) inserted += " ";
    }
    const next = text.slice(0, start) + inserted + text.slice(end);
    let caret = start + inserted.length;
    // A file before existing whitespace: step over it, as if the space
    // had been typed.
    if (!isDir && !inserted.endsWith(" ") && caret < next.length) caret++;
    return { text: next, caret };
  }

  /* The one muted line the @ menu shows instead of rows. */
  function filesError(status, data, machine) {
    const code = data && data.error && data.error.code;
    if (code === "files_unavailable") return `File search isn't available for sessions on ${machine || "that machine"}`;
    if (code === "cwd_missing") return "This session's folder is gone";
    if (code === "bad_cwd") return "This session has no working folder";
    if (code === "not_readable") return "The hub can't read this session's folder";
    if (code === "unknown_session") return "The hub doesn't know this session any more";
    if (!status) return "Can't reach the hub";
    // A hub from before the route answers any unknown path 404.
    return status === 404 ? "Update the hub to search files from here" : "Couldn't search files";
  }

  /* The / menu's list: warmed when the chat opens so the first / opens
   * it already filled, filtered as you type. A failed load retries the
   * next time the menu opens. */
  const cmds = { key: null, response: null, loading: false, error: "", gen: 0 };
  /* Answered lists by session key, kept for the page's life: a chat the
   * menu was fetched for once opens filled while the fetch refreshes. */
  const cmdLists = {};
  /* The newest request's serial per key: an older answer landing late
   * can still be kept for a chat that was left, but it never replaces
   * a newer request's answer. */
  const cmdSeqs = {};

  /* The @ menu's search, the iPhone's MentionSearch: debounced, one
   * request at a time (a newer query aborts an older request, and its
   * answer is dropped if it lands anyway), and the last results stay up
   * while the next query loads. `query` is what the newest search asked;
   * `answered` is what `entries` answer, null before any came back.
   * `ctrl` aborts the request in flight; `settle` resolves the pending
   * search's promise when it is replaced. */
  const files = { gen: 0, timer: null, ctrl: null, settle: null, query: null, entries: [], answered: null,
    loading: false, error: "" };

  /* Drops the pending search: its timer, its request and its promise. */
  function dropSearch() {
    files.gen++;
    clearTimeout(files.timer);
    files.timer = null;
    if (files.ctrl) files.ctrl.abort();
    files.ctrl = null;
    if (files.settle) files.settle();
    files.settle = null;
  }
  const FILES = { debounce: 120, limit: 50 };

  function resetFiles() {
    dropSearch();
    files.query = null;
    files.entries = [];
    files.answered = null;
    files.loading = false;
    files.error = "";
  }

  /* Searches for `query` after the debounce. Resolves once the answer
   * is in (or the search was replaced), so tests can wait on it. */
  function searchFiles(query) {
    dropSearch();
    const gen = files.gen;
    const key = chat.key;
    files.query = query;
    files.loading = true;
    return new Promise((resolve) => {
      files.settle = resolve;
      files.timer = setTimeout(async () => {
        files.timer = null;
        if (gen !== files.gen || key !== chat.key) return resolve();
        const ctrl = typeof AbortController === "function" ? new AbortController() : null;
        files.ctrl = ctrl;
        const { status, data } = await api(
          `${sessionPath(key, "files")}?q=${encodeURIComponent(query)}&limit=${FILES.limit}`,
          { signal: ctrl ? ctrl.signal : undefined });
        if (gen !== files.gen || key !== chat.key) return resolve();
        files.ctrl = null;
        files.settle = null;
        const ok = status === 200 && data && Array.isArray(data.entries);
        files.entries = ok ? data.entries.filter((e) => e && typeof e.path === "string") : [];
        files.error = ok ? "" : filesError(status, data, (chat.row && chat.row.machine) || "");
        files.answered = query;
        files.loading = false;
        if (menu.open && menu.token.kind === "mention") {
          menu.active = 0;
          renderMenu();
        }
        resolve();
      }, FILES.debounce);
    });
  }

  /* The menu's state: the token it opened on (`kind` "slash" or
   * "mention"), the rows it shows and the highlighted one. `dismissed`
   * is the token Escape closed it on: it stays closed until the token
   * changes. */
  const menu = { open: false, token: null, rows: [], active: 0, dismissed: null };

  function resetMenus() {
    cmds.gen++;
    cmds.key = null;
    cmds.response = null;
    cmds.loading = false;
    cmds.error = "";
    resetFiles();
    menu.open = false;
    menu.token = null;
    menu.rows = [];
    menu.active = 0;
    menu.dismissed = null;
  }

  function sameToken(a, b) {
    return !!a && !!b && a.kind === b.kind && a.start === b.start && a.end === b.end && a.query === b.query;
  }

  function closeMenu() {
    resetFiles();
    menu.open = false;
    menu.token = null;
    menu.rows = [];
    menu.active = 0;
    renderMenu();
  }

  /* Called whenever the box's text, caret or focus changes. */
  function trackMenus() {
    const input = $("chat-text");
    const caret = input.selectionEnd;
    const live = document.activeElement === input && !input.disabled && input.selectionStart === caret;
    const slash = live ? slashToken(input.value, caret) : null;
    const mention = live && !slash ? mentionToken(input.value, caret) : null;
    const token = slash ? { ...slash, kind: "slash" } : mention ? { ...mention, kind: "mention" } : null;
    if (!token || !sameToken(token, menu.dismissed)) menu.dismissed = null;
    if (!token || menu.dismissed) {
      if (menu.open) closeMenu();
      return;
    }
    const opening = !menu.open || menu.token.kind !== token.kind;
    if (opening && menu.open) resetFiles();
    if (!opening && token.query !== menu.token.query) menu.active = 0;
    menu.open = true;
    menu.token = token;
    if (token.kind === "mention") {
      // Search when the query changed, or when nothing answered it and
      // nothing is out for it; a caret move inside the word re-renders.
      if (files.query !== token.query || (files.answered === null && !files.loading && !files.error)) {
        searchFiles(token.query);
      }
      renderMenu();
      return;
    }
    const loaded = !!cmds.response && cmds.key === chat.key;
    // Fetch when there's no list and no failure to show; retry a failure
    // only on opening, so typing doesn't hammer a hub that refused.
    if (!cmds.loading && (cmds.error ? opening : !loaded)) loadCommands();
    else renderMenu();
  }

  /* Fetches the list ahead of the first / — called when the chat opens.
   * A kept list from an earlier open fills `cmds` at once and is still
   * refreshed here. */
  function warmCommands() {
    const key = chat.key;
    if (!key || cmds.loading) return;
    const kept = cmdLists[key];
    if (!cmds.response || cmds.key !== key) {
      cmds.key = key;
      cmds.response = (kept && kept.response) || null;
      cmds.error = (kept && kept.error) || "";
    }
    return loadCommands();
  }

  async function loadCommands() {
    const key = chat.key;
    const gen = ++cmds.gen;
    const seq = (cmdSeqs[key] = (cmdSeqs[key] || 0) + 1);
    cmds.key = key;
    cmds.loading = true;
    renderMenu();
    const { status, data } = await api(sessionPath(key, "commands"));
    // Kept by key even when the answer lands after the chat was left —
    // unless a newer request for this key already answered. A failed
    // refresh keeps the last good list beside its error.
    if (seq === cmdSeqs[key]) {
      const prev = cmdLists[key];
      cmdLists[key] = status === 200 && data
        ? { response: data, error: "" }
        : { response: (prev && prev.response) || null, error: commandsError(status, data) };
    }
    if (gen !== cmds.gen || key !== chat.key) return;
    cmds.loading = false;
    cmds.response = (cmdLists[key] || {}).response || null;
    cmds.error = (cmdLists[key] || {}).error || "";
    // The list is kept either way; only an open menu redraws.
    if (menu.open && menu.token.kind === "slash") {
      menu.active = 0;
      renderMenu();
    }
  }

  /* The one muted line the menu shows instead of rows. */
  function commandsError(status, data) {
    const code = data && data.error && data.error.code;
    if (code === "unknown_session") return "The hub doesn't know this session any more";
    if (!status) return "Can't reach the hub";
    // A hub from before the route answers any unknown path 404.
    return status === 404 ? "Update the hub to list commands from here" : "Couldn't load commands";
  }

  /* What the menu says besides its rows: the line in place of an empty
   * list, and muted footnotes under it. */
  function commandNotes(r) {
    const row = chat.row || {};
    const harness = harnessLabel({ agent: row.agent, harness: r.harness || row.harness || "" }) || "this harness";
    const supported = r.supported !== false;
    const empty = !supported || !(r.commands || []).length ? `No commands for ${harness}` : "";
    const footnotes = [];
    const customMissing = supported && r.custom_available === false;
    if (customMissing) {
      footnotes.push(row.remote
        ? `Custom commands from ${row.machine || "that machine"} aren't available yet`
        : "Custom commands don't work in this session's mode");
    }
    const note = String(r.note || "").trim();
    if (note && !(customMissing && note.toLowerCase().includes("custom commands"))) {
      footnotes.push(note.charAt(0).toUpperCase() + note.slice(1));
    }
    return { empty, footnotes };
  }

  function renderMenu() {
    const box = $("chat-menu");
    const input = $("chat-text");
    if (!box || !input) return;
    box.classList.toggle("hidden", !menu.open);
    input.setAttribute("aria-expanded", String(menu.open));
    if (!menu.open) {
      box.innerHTML = "";
      input.removeAttribute("aria-activedescendant");
      return;
    }
    const mention = menu.token.kind === "mention";
    box.setAttribute("aria-label", mention ? "Files" : "Commands");
    if (mention) {
      box.innerHTML = fileMenuHtml();
      showActive(box, input);
      return;
    }
    const r = cmds.key === chat.key ? cmds.response : null;
    menu.rows = r ? filterCommands(r.commands || [], menu.token.query) : [];
    if (menu.active >= menu.rows.length) menu.active = 0;
    let html = "";
    // A failed load shows above whatever list is kept from before.
    if (cmds.error) html += `<div class="menu-note error-note">${esc(cmds.error)}</div>`;
    let footnotes = [];
    if (cmds.loading && !r) {
      if (!cmds.error) html += `<div class="menu-note">Loading commands</div>`;
    } else if (r) {
      const notes = commandNotes(r);
      footnotes = notes.footnotes;
      if (notes.empty) {
        menu.rows = [];
        html += `<div class="menu-note">${esc(notes.empty)}</div>`;
      } else if (!menu.rows.length) {
        html += `<div class="menu-note">No command matches /${esc(menu.token.query)}</div>`;
      } else {
        html += menu.rows.map((c, i) => {
          const label = commandInsert(c).trim();
          const tag = sourceLabel(c.source);
          const aria = [label, c.argument_hint, c.description, tag].filter(Boolean).join(", ");
          return `<div class="menu-row cmd${i === menu.active ? " active" : ""}" role="option" id="chat-menu-opt-${i}"` +
            ` aria-selected="${i === menu.active}" data-i="${i}" aria-label="${esc(aria)}">` +
            `<span class="cmd-body"><span class="cmd-top"><span class="menu-name">${esc(label)}</span>` +
            (c.argument_hint ? `<span class="menu-sub">${esc(c.argument_hint)}</span>` : "") + `</span>` +
            (c.description ? `<span class="cmd-desc">${esc(c.description)}</span>` : "") + `</span>` +
            (tag ? `<span class="own">${esc(tag)}</span>` : "") + `</div>`;
        }).join("");
      }
    }
    if (footnotes.length) {
      html += `<div class="menu-foot">${footnotes.map((f) => `<div>${esc(f)}</div>`).join("")}</div>`;
    }
    box.innerHTML = html;
    showActive(box, input);
  }

  function showActive(box, input) {
    if (menu.rows.length) {
      input.setAttribute("aria-activedescendant", `chat-menu-opt-${menu.active}`);
      const row = box.querySelector(".menu-row.active");
      if (row) row.scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  const FILE_ICON = '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>';
  const FOLDER_ICON = '<path d="M3 6h6l2 2h10v11H3z"/>';

  /* The @ menu's body: the error, "Searching files" before the first
   * answer, "No files match", or a row per file (name, then its folder). */
  function fileMenuHtml() {
    menu.rows = files.error ? [] : files.entries;
    if (menu.active >= menu.rows.length) menu.active = 0;
    if (files.error) return `<div class="menu-note error-note">${esc(files.error)}</div>`;
    if (!menu.rows.length) {
      return `<div class="menu-note">${files.answered === null ? "Searching files" : "No files match"}</div>`;
    }
    return menu.rows.map((f, i) => {
      const { name, parent } = splitPath(f.path);
      const dir = f.kind === "dir";
      return `<div class="menu-row file${i === menu.active ? " active" : ""}" role="option" id="chat-menu-opt-${i}"` +
        ` aria-selected="${i === menu.active}" data-i="${i}" aria-label="${esc(parent ? `${name}, in ${parent}` : name)}">` +
        `<svg class="file-icon" viewBox="0 0 24 24" aria-hidden="true">${dir ? FOLDER_ICON : FILE_ICON}</svg>` +
        `<span class="menu-name">${esc(name)}</span>` +
        (parent ? `<span class="menu-sub">${esc(parent)}</span>` : "") + `</div>`;
    }).join("");
  }

  function pickMenu(i) {
    const entry = menu.rows[i];
    if (!entry || !menu.open) return;
    const input = $("chat-text");
    const next = menu.token.kind === "mention"
      ? insertMention(input.value, menu.token, entry.path, entry.kind === "dir")
      : insertCommand(input.value, menu.token, entry);
    input.value = next.text;
    input.focus();
    input.setSelectionRange(next.caret, next.caret);
    growComposer();
    trackMenus();
  }

  /* Leaving the chat by any route closes its stream, so it never holds
   * one of the browser's few connections to the hub. */
  window.addEventListener("hashchange", () => {
    if (chat.key && keyFromHash() !== chat.key) close();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && chat.key && chat.opened) {
      // Back on the tab: a model switched elsewhere meanwhile shows now,
      // and so do sends another device queued, edited or removed.
      refreshModel();
      // What arrived while hidden. A stream that dropped meanwhile is
      // opened again below, which clears what is waiting: show it all
      // now, so nothing received is left to a replay the hub may have
      // trimmed. A backlog over 24 lands at once anyway, its bubbles drawn
      // once after it. Otherwise the bubbles as they are now, then the
      // held events through the reveal.
      if (chat.pending.length && (!chat.stream || revealDelay(chat.pending.length) === null)) flushPending();
      if (chat.streamsStale) paintStreams();
      if (chat.pending.length && chat.revealTimer === null) revealNext();
      unreadHook("activity", chat.key);
      if (chat.rowKnown) loadQueue(chat.gen);
      if (!chat.stream) {
        chat.retries = 0;
        openStream(chat.gen, resumeSeq());
      }
    }
  });

  return {
    open, close, keyFromHash, applyMeta, revealSeq,
    get key() { return chat.key; },
    // Pure helpers, for tests.
    _test: { md, mdBlocks, cells, separatorAlignments, listItem, heading, imageLine,
      inlineImagesAsText, inline, taskBox, tableHtml,
      hl, langName, langFromPath, isNumbered, readableArgs, toolOutLang, looksJson,
      diffLines, renderDiff, renderPatch, toolDiff, parseFrame, revealDelay, retryDelay, openStream, chat, post, handleFrame, approvalCard,
      isPage, patchPageFiles, pageFiles, relativePath, pageTargets, previewUrl, previewKind,
      pageDocument, previewDocument, previewFrameAttrs, previewMessage, PREVIEW_CSP, linkUrls, linkHost, linkPath, linkCardsHtml,
      barSub, refreshModel, railDay, railGroups, slashToken, commandInsert, sourceLabel, filterCommands, insertCommand, menuKey,
      cmds, cmdLists, warmCommands, loadCommands, resetMenus, LOG_KEEP, trimLog, showEarlier, revealSeq, appendEvent, scrollBottom, resync,
      ATTACH, fileExt, attachProblem, formatBytes, uploadError, uploads, addFiles, removeUpload,
      messageBody, send, updateOwnership, restoreUploads, failBubble,
      mentionToken, insertMention, splitPath, filesError, files, FILES, searchFiles, trackMenus, menu,
      echoes, sendsToTray, mergeQueue, queueNote, trayHtml, posted, takeQueued, echoLeavesTray, sendText,
      sendNow, removeQueued, removeAllQueued, beginEdit, cancelEdit, saveEdit, loadQueue, commandResult, appendMessage,
      backfill, showLive, cancelDetach, loadRail, attach, paintBanner, settleMirroredLog, isMirrored, MIRRORED_NOTE, MIRRORED_EMPTY,
      replyStreams, receiveDelta, paintStreams, clearStreams, revealNext, enqueue, STREAM_MS, mdStream, renderMd },
  };
})();

globalThis.SessionChat = SessionChat;
