/* Number, name and colour formatting, ported from the Mac app's
 * UsageFormat, HarnessMark and HarnessColor so both read the same. */
"use strict";

const Fmt = (() => {
  function compact(value) {
    const r = Math.round(value * 10) / 10;
    return r === Math.round(r) ? String(Math.round(r)) : r.toFixed(1);
  }

  function tokens(count) {
    const units = [[1e3, "K"], [1e6, "M"], [1e9, "B"]];
    let unit = -1;
    units.forEach(([limit], i) => { if (count >= limit) unit = i; });
    if (unit < 0) return String(count || 0);
    // The unit after rounding: 999,950 rounds to 1000K, which reads as 1M.
    while (unit + 1 < units.length
           && Math.round((count / units[unit][0]) * 10) / 10 >= 1000) unit += 1;
    return compact(count / units[unit][0]) + units[unit][1];
  }

  function dollars(amount) {
    if (!amount) return "$0";
    if (amount < 0.01) return "<$0.01";
    // Judged on the amount as it rounds: 99.996 prints "$100", not "$100.00".
    const digits = Math.round(amount * 100) / 100 < 100 ? 2 : 0;
    return amount.toLocaleString("en-US", {
      style: "currency", currency: "USD",
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }

  function ago(epoch, now = Date.now() / 1000) {
    const minutes = Math.floor((now - epoch) / 60);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    if (minutes < 24 * 60) return `${Math.floor(minutes / 60)}h ago`;
    return `${Math.floor(minutes / (24 * 60))}d ago`;
  }

  /* HarnessMark.names */
  const NAMES = {
    "claude": "Claude Code", "claude-desktop": "Claude Desktop", "codex": "Codex",
    "opencode": "OpenCode", "devin": "Devin", "devin-cloud": "Devin cloud",
    "droid": "Factory Droid", "copilot": "GitHub Copilot", "lmstudio": "LM Studio",
    "qwen-code": "Qwen Code", "mistral-vibe": "Mistral Vibe", "commandcode": "Command Code",
    "qoder-cn": "Qoder CN", "grok-bot": "Grok Bot", "grok": "Grok Build", "kilo": "Kilo Code",
    "goose": "Goose", "aider": "Aider", "zcode": "ZCode",
  };
  const nameOf = (id) => NAMES[id] || (id ? id[0].toUpperCase() + id.slice(1) : "");
  /* HarnessMark(id).name: a second account ("codex-acc2") reads
   * "Codex (acc2)", as every Mac page names it. */
  function harnessName(id) {
    return mark(id).name;
  }

  /* HarnessColor.fixed, then the same fallback palette slot as the Mac's
   * HarnessColor.stableIndex and iOS Theme.harnessHex: 64-bit FNV-1a over
   * the id's UTF-8 bytes, modulo the palette (BigInt, as 64-bit products
   * don't fit a double). */
  const COLORS = {
    claude: "#d97757", codex: "#3b7ff0", opencode: "#139f8b", devin: "#c4860f",
    cursor: "#c08ce0", "grok-bot": "#8a73e8", pi: "#5fb3d9", droid: "#e06c8f",
    freebuff: "#8ac74f", cline: "#9aa0aa", gemini: "#e3b51e",
  };
  const PALETTE = ["#5aa2ff", "#f0a04b", "#4ec9b0", "#e06c8f", "#9b7ff0",
    "#8ac74f", "#5ac8e0", "#f0755f", "#d9b44a", "#c08ce0"];
  function harnessColor(id) {
    if (Object.prototype.hasOwnProperty.call(COLORS, id)) return COLORS[id];
    let h = 0xcbf29ce484222325n;
    for (const byte of new TextEncoder().encode(id || "")) {
      h = ((h ^ BigInt(byte)) * 0x100000001b3n) & 0xffffffffffffffffn;
    }
    return PALETTE[Number(h % BigInt(PALETTE.length))];
  }

  /* HarnessIcons: every harness-<id>.svg shipped beside the page. Colour ones
   * are drawn as they are (harness-<id>-color.svg), mono ones inverted in
   * dark mode. Keep in step with the files beside this script. */
  const COLOR_LOGOS = new Set(("antgroup apify arcade brave browserbase cartesia claude cloudflare codex cohere deepseek exa "
    + "firecrawl fireworks gemini honeycomb meta minimax mistral-vibe nvidia opencode perplexity qwen-code sourcegraph "
    + "statsig stepfun tinyfish upstage workos zapier").split(" "));
  const MONO_LOGOS = new Set(("agentmail ai21 anyscale assemblyai astronomer atlassian augment baseten bitbucket blackbox bolt braintrust browseruse buildkite "
    + "cerebras circleci clerk cline clickhouse cockroach codecov contentful convex copilot crates cursor datadog deepgram devin "
    + "digitalocean discord docker droid e2b elevenlabs expo figma flyio freebuff grafana grok groq hashicorp heroku "
    + "incidentio inngest kilo kimi lambda langfuse launchdarkly linear linode loom lovable mailgun make manus maven mongodb morph "
    + "nebius neon netlify newrelic ngrok notion npm ona openai parallel pi pinecone planetscale poe postman prefect prisma pypi railway render replicate replit "
    + "revenuecat rubygems runloop sambanova sanity semaphore sentry snowflake snyk sonatype stability supermemory supabase tailscale tavily "
    + "temporal travisci turso twilio upstash v0 vercel warp windsurf writer xiaomimimo yi zai").split(" "));
  // HarnessIcons.aliases: other names the same harness goes by.
  const ALIASES = { "claude-code": "claude", "github-copilot": "copilot", factory: "droid", qwen: "qwen-code",
    mistral: "mistral-vibe", codebuff: "freebuff", "command-code": "commandcode", muse: "meta" };
  function logoFile(id) {
    const key = ALIASES[String(id).toLowerCase()] || String(id).toLowerCase();
    if (COLOR_LOGOS.has(key)) return `harness-${key}-color.svg`;
    if (MONO_LOGOS.has(key)) return `harness-${key}.svg`;
    return null;
  }

  /* HarnessMark: the logo to draw (or null), a word after it where the
   * logo alone is ambiguous (or alone when there is no logo), and the
   * full name for tooltips. Claude's logo stands for the CLI and the
   * Desktop app; a second account ("codex-acc2") shows its base harness's
   * logo and the suffix. */
  function mark(id) {
    id = String(id || "");
    const special = {
      claude: ["claude", "CLI"], "claude-desktop": ["claude", "Desktop"],
      "grok-bot": ["grok", null], "devin-cloud": ["devin", "Cloud"],
    };
    if (special[id]) return { icon: special[id][0], word: special[id][1], name: nameOf(id) };
    if (logoFile(id)) return { icon: id, word: null, name: nameOf(id) };
    const dash = id.lastIndexOf("-");
    if (dash > 0) {
      const base = id.slice(0, dash), suffix = id.slice(dash + 1);
      if (suffix && logoFile(base)) return { icon: base, word: suffix, name: `${nameOf(base)} (${suffix})` };
    }
    return { icon: null, word: nameOf(id), name: nameOf(id) };
  }

  /* A harness's logo (claude-desktop and codex-acc2 draw their base
   * harness's). One with no logo keeps an empty square, as the Mac's
   * HarnessIcon does, so rows line up; mark().word names it where text
   * has to stand in. */
  function logo(id, cls = "logo") {
    const m = mark(id);
    const file = m.icon && logoFile(m.icon);
    if (!file) return `<span class="${cls}" aria-hidden="true"></span>`;
    const mono = file.endsWith("-color.svg") ? "" : " mono";
    return `<img class="${cls}${mono}" src="/${file}" alt="${m.name.replace(/"/g, "&quot;")}">`;
  }

  // format.js loads before app.js and runs alone in node tests, so it
  // keeps its own copy of app.js's esc().
  const escHtml = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  /* HarnessMarkView: the logo and the mark's word after it (CLI,
   * Desktop, a second account's suffix), or the name alone with no logo,
   * in a span of class `wrap` that names the harness. */
  function markView(id, wrap, cls = "logo") {
    const m = mark(id);
    const inner = m.icon ? logo(id, cls) + (m.word ? `<span>${escHtml(m.word)}</span>` : "")
      : `<span>${escHtml(m.name)}</span>`;
    return `<span class="${wrap}" title="${escHtml(m.name)}" aria-label="${escHtml(m.name)}">${inner}</span>`;
  }

  /* `~/…` for a path under the hub's home; any other path as it is. */
  function tilde(path, home) {
    return home && path.startsWith(home + "/") ? "~" + path.slice(home.length) : path;
  }

  /* MarbleScale: one marble per round amount of spend (1, 2, 5 times a
   * power of ten, from a cent), the smallest that keeps the pile within
   * `capacity`. With no priced spend it counts tokens instead. */
  function marbleScale(shares, capacity) {
    const shown = shares.filter((s) => s.tokens > 0);
    const bySpend = shown.some((s) => s.cost_usd > 0);
    const values = shown.map((s) => (bySpend ? s.cost_usd : s.tokens));
    const largest = Math.max(0, ...values);
    const countsAt = (unit) => values.map((v) => Math.max(1, Math.round(v / unit)));
    const nextStep = (step) => {
      const magnitude = Math.pow(10, Math.floor(Math.log10(step) + 1e-9));
      const lead = Math.round(step / magnitude);
      const next = lead < 2 ? 2 * magnitude : lead < 5 ? 5 * magnitude : 10 * magnitude;
      return Math.round(next * 100) / 100;
    };
    let step = bySpend ? 0.01 : 1;
    let counts = countsAt(step);
    const sum = (xs) => xs.reduce((a, b) => a + b, 0);
    while (sum(counts) > Math.max(capacity, 1) && step <= largest) {
      step = nextStep(step);
      counts = countsAt(step);
    }
    return {
      bySpend, step,
      counts: shown.map((s, i) => ({ harness: s.harness, count: counts[i] })),
    };
  }

  /* Foundation's .relative(presentation: .named), as Settings' devices and
   * Service status show times: "now", "5 minutes ago", "yesterday". Every
   * time shown this way is past; a hub clock a little ahead of this
   * browser's must not read "in 2 minutes". */
  function relative(epoch, now = Date.now() / 1000) {
    const diff = Math.min(0, epoch - now);
    const abs = Math.abs(diff);
    const rtf = new Intl.RelativeTimeFormat("en-US", { numeric: "auto" });
    for (const [unit, size] of [["year", 31536000], ["month", 2592000], ["week", 604800],
      ["day", 86400], ["hour", 3600], ["minute", 60]]) {
      if (abs >= size) return rtf.format(Math.trunc(diff / size), unit);
    }
    return abs < 1 ? "now" : rtf.format(Math.trunc(diff), "second");
  }

  /* UsageFormat.resetIn: "in 12m", "in 4h 28m", "in 6d 13h"; minutes round up. */
  function resetIn(epoch, now = Date.now() / 1000) {
    const seconds = epoch - now;
    if (seconds <= 0) return "now";
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60) return `in ${minutes}m`;
    if (minutes < 24 * 60) {
      const rest = minutes % 60;
      return rest ? `in ${Math.floor(minutes / 60)}h ${rest}m` : `in ${minutes / 60}h`;
    }
    const days = Math.floor(minutes / (24 * 60));
    const rest = Math.floor((minutes % (24 * 60)) / 60);
    return rest ? `in ${days}d ${rest}h` : `in ${days}d`;
  }

  /* UsageFormat.window: a limit window named as the harness's CLI does;
   * Cursor's "included" and "api" read "Cursor models" and "Other models". */
  function windowLabel(harness, label) {
    if (harness === "cursor" && label === "included") return "Cursor models";
    if (harness === "cursor" && label === "api") return "Other models";
    return { included: "Included", auto: "Auto", api: "API", grok_bot: harnessName("grok-bot") }[label]
      || label || "Usage";
  }

  /* UsageFormat.windowInfo: what a meter bills for, for the click-pinned
   * tip beside its row on Accounts. Only meters worth explaining have a
   * line; the rest get no button. "on_demand" stands for the on-demand
   * row, not a window. Same table as the Mac app's windowInfo. */
  const METER_INFO = {
    cursor: {
      included: "Everything billed this cycle: the included credit pool and on-demand spend together.",
      auto: "The draw on the credit pool by Auto-routed requests, where Cursor picks the model.",
      api: "The draw on the credit pool by named-model requests; at 100% they only continue if on-demand billing is on.",
      grok_bot: "Grok Bot's own weekly allowance, separate from the billing cycle.",
      on_demand: "Metered spend once the credit pool runs out; uncapped when the account sets no spend limit.",
    },
  };
  function meterInfo(harness, label) {
    return (METER_INFO[harness] || {})[label] || null;
  }

  /* "max_20x" -> "Max 20x"; blank or a dash means no plan. */
  function planTitle(plan) {
    if (!plan || plan === "—" || plan === "-") return "";
    return plan.replace(/_/g, " ").split(" ").filter(Boolean)
      .map((w) => (/^\d/.test(w) ? w : w[0].toUpperCase() + w.slice(1))).join(" ");
  }

  /* Money: whole dollars when integral, else cents. Balances: always cents. */
  function money(amount) {
    const whole = Math.round(amount) === amount;
    return amount.toLocaleString("en-US", { style: "currency", currency: "USD",
      minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2 });
  }
  function balance(amount) {
    return amount.toLocaleString("en-US", { style: "currency", currency: "USD",
      minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /* Harness.onDemandLines: [value, detail] for Cursor's on-demand spend. */
  function onDemand(od) {
    const used = od.used_usd || 0;
    if (od.state === "fixed" && od.limit_usd != null) {
      return [`${money(used)} / ${money(od.limit_usd)}`, `${money(Math.max(0, od.limit_usd - used))} remaining`];
    }
    if (od.state === "unlimited") {
      return [money(used), od.scope === "team-member" ? "No personal limit" : "No monthly limit"];
    }
    if (od.state === "disabled") return [used > 0 ? money(used) : "Disabled", "On-demand usage is off"];
    return [used > 0 ? money(used) : "Unavailable", "On-demand limit unavailable"];
  }

  /* Copy icon → green tick for a moment, the Mac app's doc.on.doc →
   * checkmark port. `copy` writes and answers whether it landed (plain
   * http has no clipboard API, so it falls back to the old command);
   * `copyTick` wires a button so a copy swaps its icon for a tick. */
  const COPY_PATHS = '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/>';
  const TICK_PATHS = '<path d="m5 12 5 5 9-10"/>';
  const strokeIcon = (paths) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
  const copyIcon = () => strokeIcon(COPY_PATHS);

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    // Inside a modal the rest of the page is inert, so the fallback
    // lands in the open dialog when there is one.
    (document.querySelector("dialog[open]") || document.body).appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    area.remove();
    return ok;
  }

  /* ``text`` may be a string or a function answering it (the value is
   * read at click time). ``onFail`` runs when neither clipboard path
   * worked, so the caller can show its own hint. */
  function copyTick(btn, text, onFail) {
    if (!btn || btn.dataset.copyWired) return;
    btn.dataset.copyWired = "1";
    const svg = btn.querySelector("svg");
    const restore = svg ? svg.outerHTML : btn.innerHTML;
    btn.addEventListener("click", async () => {
      // ``text`` may answer a promise (a value fetched at click time).
      const value = await (typeof text === "function" ? text() : text);
      if (value == null || value === "") return;
      if (!(await copy(String(value)))) {
        if (onFail) onFail(btn);
        return;
      }
      clearTimeout(Number(btn.dataset.copyTimer || 0));
      btn.classList.add("copied");
      const cur = btn.querySelector("svg");
      if (cur) cur.outerHTML = strokeIcon(TICK_PATHS);
      else btn.innerHTML = strokeIcon(TICK_PATHS);
      btn.dataset.copyTimer = String(setTimeout(() => {
        btn.classList.remove("copied");
        const done = btn.querySelector("svg");
        if (done) done.outerHTML = restore;
        else btn.innerHTML = restore;
        delete btn.dataset.copyTimer;
      }, 2000));
    });
  }

  /* One shell word as it can be pasted back: bare when it holds only
   * safe characters, otherwise single-quoted. */
  const shellWord = (s) => (/^[\w@%+=:,./-]+$/.test(String(s)) ? String(s)
    : `'${String(s).replace(/'/g, "'\\''")}'`);

  /* A session's model as a header or list row shows it, the iPhone's
   * ModelLabel.short: the id without its provider path or an 8-digit
   * date stamp ("anthropic/claude-opus-4-5" and "claude-opus-4-5-20251101"
   * both read "claude-opus-4-5"; "openrouter/nvidia/nemotron-3:free"
   * reads "nemotron-3:free"). "" for a missing or blank one. */
  function modelName(raw) {
    const trimmed = typeof raw === "string" ? raw.trim() : "";
    if (!trimmed) return "";
    const parts = trimmed.split("/").filter(Boolean);
    const name = parts.length ? parts[parts.length - 1] : trimmed;
    const dash = name.lastIndexOf("-");
    return dash > 0 && /^[0-9]{8}$/.test(name.slice(dash + 1)) ? name.slice(0, dash) : name;
  }

  /* A thinking card's title from the hub's duration_s, the Mac's and
   * the iPhone's ThoughtLabel.text: "Thought for 12s", "Thought for
   * 1m 5s", "Thought for 1h 2m", rounded to the second and never under
   * 1s; plain "Thought" when no usable duration came. */
  function thought(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "Thought";
    const total = Math.max(1, Math.round(seconds));
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    if (h > 0) return m > 0 ? `Thought for ${h}h ${m}m` : `Thought for ${h}h`;
    if (m > 0) return s > 0 ? `Thought for ${m}m ${s}s` : `Thought for ${m}m`;
    return `Thought for ${s}s`;
  }

  /* The words for the hub's session statuses, as the chat badge says
   * them (chat.js's STATUS, the Mac app's HubSessionRow.statusTitle);
   * an unknown status reads as itself. */
  const SESSION_STATUS = Object.freeze({ active: "Active", done: "Done", followup: "Waiting", error: "Error",
    interrupted: "Interrupted", approval: "Approval needed" });
  const sessionStatus = (raw) => SESSION_STATUS[raw] || raw || "";

  return { tokens, dollars, ago, relative, harnessName, modelName, thought, harnessColor, logo, mark, markView, tilde, marbleScale,
    resetIn, windowLabel, meterInfo, planTitle, money, balance, onDemand,
    copyIcon, copy, copyTick, shellWord, sessionStatus };
})();

/* Loading skeletons: blocks laid out like the rows or document a fetch
 * is still reading, styled by the .skel-* rules in styles.css. A page's
 * skeleton reuses its own row markup with Sk bars in place of content,
 * so what arrives lands without the layout jumping. Sk.block marks
 * itself role="status"; everything else stays silent inside a wrapper
 * that carries role="status" aria-label="Loading". */
const Sk = (() => {
  const width = (v) => (typeof v === "number" ? `${v}px` : v);
  const status = 'role="status" aria-label="Loading"';
  /* One shimmering block; w as %/px, h in px. Marks itself so it can
   * stand alone as a field or button's placeholder. */
  const block = (w, h, cls = "") =>
    `<div class="skel${cls ? ` ${cls}` : ""}" ${status} style="width:${width(w)};height:${h}px"></div>`;
  /* A bare block with no status: for cells inside a wrapper that
   * carries the role. ``cls`` borrows a real component's class so its
   * own margins and radius apply; ``style`` adds a one-off rule. */
  const bar = (w, h, cls = "", style = "") =>
    `<div class="skel${cls ? ` ${cls}` : ""}" style="width:${width(w)};height:${h}px${style ? `;${style}` : ""}"></div>`;
  const dot = (size = 16) => bar(size, size, "", "border-radius:50%;flex:none");
  const pill = (w = 48, h = 16) => bar(w, h, "", `border-radius:${h / 2}px;flex:none`);
  /* Right-aligned, for the numeric cells of tables and stat rows. */
  const num = (w = 40, h = 12) => bar(w, h, "", "margin-left:auto");
  /* Paragraph or file lines, the last one short. */
  const linesInner = (n) => Array.from({ length: n }, (_, i) =>
    `<div class="skel skel-line" style="width:${i === n - 1 ? 45 : 90 - (i % 4) * 12}%"></div>`).join("");
  return { block, bar, dot, pill, num, linesInner };
})();
