# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The web Sessions page shows mirrored sessions.** A session whose transcript Claude Code or Codex deleted, and swe kept a read-only copy of ([#1918](https://github.com/starship-terminal/swe/issues/1918)), now carries a quiet "Mirrored" tag in the web list (hover: "Its transcript was deleted by the harness. swe kept a read-only copy."), is never hidden as an accidental empty session, and has no Attach, Detach or Resume; Fork stays in the More actions menu but off, with the same reason. Its chat opens with a one-line notice, and reads "This session's chat is not available yet." when the hub cannot read the copy yet. Live sessions are unchanged. The Mac and iPhone apps are follow-ups.

- **Hook stats are fast on a repeat read, and plugin and project hooks are counted by their real file.** `swe hooks stats` and `swe hooks log` keep what each Claude Code transcript said in `~/.swe/cache/hooks/transcripts.sqlite` (private, bounded at 64 MiB, shared safely by several readers, rebuilt when the parser changes; `SWE_HOOK_CACHE=0` skips it), so an unchanged transcript is not read again: on a large library a repeat 7-day read drops from about 3 seconds to under half a second, a 30-day one from about 30 to under 3, and the Mac side panel's recent runs no longer wait seconds. A plugin hook's `${CLAUDE_PLUGIN_ROOT}` command now resolves to the installed plugin that declares it, and `$CLAUDE_PROJECT_DIR` to the session's project, so those runs are their own file's instead of a same-named hook's. A run through a shared hook's wrapper folder that has since been removed is matched to its shared script and counts once. Harness-tier hooks are still logged only when they call `record` (no logging wrapper: it cannot pass a hook through unchanged); Claude Code's runs of them come from its transcripts. Part of [#1888](https://github.com/starship-terminal/swe/issues/1888).

- **Transcript mirror: sessions survive the harnesses' 30-day pruning.** Claude Code and Codex delete transcripts after about 30 days. swe now copies each one into `~/.swe/data/transcripts/` while it exists (on by default; `swe session mirror --off` stops it) and never deletes a copy: not when its source disappears, not to save space. On macOS a copy is an APFS clone (`clonefile`), so a 13 GB projects folder costs almost no extra disk and no I/O; other systems stream it. `swe session mirror` runs a pass and `--json` reports cloned, copied, unchanged and orphaned counts and bytes; the hub's loop and `swe session index` run one too, from the same walk the search index uses. A session whose harness deleted the transcript stays in `swe session` (and `--json`, as `mirrored: true`, with no resume command), and `swe report` and the transcript view read it from the copy. Search is unchanged: the mirror writes no index rows. Searching copies the index never read, restoring a copy to the harness, purging on delete, a doctor check and the Mac app's side are follow-ups ([#1918](https://github.com/starship-terminal/swe/issues/1918)).

- **The Mac Hooks page counts what each hook did.** Every script on the timeline shows its fires, blocks and block rate for the last 24 hours, 7 days (default) or 30 days, picked at the top right of the card and remembered; a hook that blocked reads in red, and a rate under 1% reads "<1%" so it never shows "0%" beside a block. Counts add up every harness that runs the script. Opening a hook adds Fires, Blocks and Last block tiles, its top reasons and its five latest runs to the side panel, or "No runs in the last 7 days." when it had none. The numbers load with the page, the picker and Refresh, never on a timer; an swe from before `swe hooks stats` shows dashes and the page works as before. Which runs are which script's is now decided by the file that ran, not by its name, once in swe for the CLI, the web page and the app: the shim logs the shared script it ran (a new `script` field in each log line; `record` called by a hook logs the running program), transcript commands are resolved the way `swe hooks list` resolves them (quoted paths with spaces, `$HOME`, symlinks, a generated wrapper to its shared script), and `swe hooks stats --json` rows carry `source_paths` (the `swe hooks list` rows they belong to) and `scripts`. So a shim run and its transcript copy count once, a directory hook counts every declared executable and nothing its undeclared helpers ran, two files with one basename never share numbers, and a run swe cannot attribute (an old log line whose name two scripts share) marks those rows `uncertain`, which both pages show as dashes. `swe hooks stats --sum-harnesses` folds every harness and every name into one row per script before the five-reason cap, `--source=<path>` on `stats` and `log` picks a hook by its list `source_path`, and `--hook=` / `swe hooks log <name>` take a name exactly as it was logged (`claude-plugin/on-stop`) or the list name (`bundle`). The web Hooks page joins on `source_paths` too, and its list rows no longer carry `stats_keys`. Part of #1888.

- **The web Hooks page shows how often each hook fires and blocks.** Every hook row gains Fires, Blocks and Rate columns (every harness's runs added up) under a picker for the last 24 hours, 7 days (the default) or 30 days, which the browser remembers. The hook's own page gains a Runs card: Fires, Blocks and Last block tiles, the top reasons and the five newest runs, or "No runs in the last 7 days." The numbers come from the same read as `swe hooks stats --json` and `swe hooks log --json`, served by `GET /v1/app/hooks?stats=24h|7d|30d`. That read backfills from Claude Code transcripts and can take seconds, so a background thread runs it and the page paints its list first and fills the columns in when they arrive; while they load they are placeholders, and if the read fails they show dashes and the rest of the page works ([#1888](https://github.com/starship-terminal/swe/issues/1888)).

- **`swe hooks stats` and `swe hooks log` show what each hook did.** The shared-hook shim now appends one line per run to `~/.swe/cache/hooks/events.jsonl` (5 MiB, one rotated copy, never sent or committed): hook, harness, event, session, repo and branch, `block`/`warn`/`allow`/`error`, the first line of the hook's message and its duration, plus `stop_hook_active` so a Stop block followed by an accepted rewrite reads apart from a plain allow. Logging fails open and `SWE_HOOK_LOG=0` turns it off; run `swe hooks sync` once to refresh the shim. `swe hooks stats [--since=7d] [--hook=<name>] [--harness=<h>] [--repo=<path>] [--json]` prints fires, blocks, block rate and top reasons per hook, and `swe hooks log <name>` the recent runs. Claude Code history from before the log is recovered from its transcripts and merged without double counting (`--no-backfill` skips it). The Mac and web Hooks pages are still to come. Part of #1888.

- **Cursor personal plugin controls.** SWE reads account state instead of guessing from downloaded folders, adds on/off for unconfigured, unpinned personal plugins and personal removal, and keeps team-controlled plugins read-only. Protected credentials and failed API reads keep cache state unknown.

- **Devin and OpenCode plugin controls.** Devin resolved installs can be removed through its native CLI. OpenCode global file plugins can be suspended into SWE storage, restored, imported, and removed. Controls are available on Mac, web and CLI; Devin has no per-plugin toggle, and OpenCode package/project controls remain unsupported.

- **`swe worktree` (alias `swe wt`) manages git worktrees across repos.**
  `swe wt list` lists every linked worktree in the current repo, or with
  `--all` in every repo swe knows (`worktree.roots` in config, `swe gh`
  mirrors, `~/.swe/worktrees`, `~/.swe/accounts/*/*/home/worktrees`), with its
  PR, last commit, `du` size and the space in rebuildable folders. States
  come from the PR, since squash merges defeat `git merge-base
  --is-ancestor`; when `gh` cannot answer a worktree is `unknown` and never
  removed. `show`, `add` (a branch, a new branch or a PR's head),
  `remove`, `lock`, `unlock` and `move` handle one worktree; every command
  takes `--json` and the same calls are a Python API in `swe.worktrees`,
  pinned for the coming Mac and web Worktrees pages. `swe wt prune` is a dry run until `--yes`, removes only
  `merged-clean` worktrees through `git worktree remove --force` (plus
  `merged-dirty` with `--include=merged-dirty` and a yes each), re-reads
  each state right before removing, keeps branches unless
  `--delete-branches`, reports freed space measured on the volume and logs
  to `~/.swe/logs/worktree/`. `--branch=<name>` and `--auto` (per-repo
  `auto_prune` in config) are for post-merge hooks. `swe wt slim` deletes
  ignored `.venv`, `node_modules`, `build` and similar folders in unmerged
  worktrees. `scripts/reaper.sh` is now a thin wrapper over `swe wt prune`
  and `swe wt slim`; it no longer prunes build folders in the main
  checkout. Closes #1909.

- **Plugins are managed from SWE.** The Mac and web plugin lists hide marketplace-only entries and missing installs. Import to SWE copies installed plugin files into `~/.swe/plugins` without changing a harness or overwriting SWE files. The Mac table renders rows lazily and precomputes its local inventory and action menus.
- **Harness-only skills adopt into the shared tree.** `swe init` (skip
  with `--no-adopt`) and `swe skills adopt [<name>|--all]` move a skill
  that only lives in a harness's real skills folder into `~/.swe/skills`
  verbatim, leave a per-skill link in the donor root so that harness
  sees no change, record the donor in `skill_links.json`'s
  `adopted_from`, and mark the skill off for every other root — a
  whole-root link converts to an `except` selection — so nothing new
  appears anywhere. Same-named copies never merge: the first root in
  registry order donates, identical ones fold into links, divergent ones
  stay and flag `conflict` on the shared row in `swe skills --json`.
  `swe skills remove <name>` deletes a shared skill plus every link into
  it and purges its records so a later adopt cannot resurrect it.
  `swe init --check` previews the pass and reports it as
  `skills_adoption`. Closes #1882.
- **AGENTS.md page: drafts list and compiled size (macOS app).** A
  "Drafts (N)" menu beside the Edit/Compile tabs lists every chapter with
  unsaved changes and jumps to it; it appears only while there are drafts.
  The View AGENTS.md button and popup now show the generated file's size in
  KiB as last built, per harness output on the Compile tab. Part of #1894.
- **AGENTS.md Compile tab: a KiB column and one harness dropdown (macOS
  app).** The Include tree gains a `KiB` column: each chapter file's size
  on disk with one decimal, each folder the sum of its checked files, and a
  Total row while one harness is picked. The row of harness chips becomes a
  multi-select dropdown (All harnesses, the harness name, or "N harnesses")
  that keeps each logo and link-state dot in its menu; picking several
  narrows the Order matrix to those harnesses. The "Shared ~/.swe/AGENTS.md:
  N chapters" summary line under the harness bar is gone. Closes #1893.
- **The AGENTS.md chapter outline has search, keyboard navigation and drag
  handles (Mac).** A Filter chapters field above the Edit tab's outline
  matches file names and paths and keeps matching files' folders open;
  clearing it restores your folders and selection. With the outline focused,
  Up/Down move between rows, Left/Right collapse, expand or climb to the
  parent folder, and Return opens a chapter, while the text editor keeps its
  arrow keys. Rows show a grip on hover, a dragged chapter shows an insertion
  line, and the folder it will land in is outlined. Part of #1894.

- **AGENTS.md: resizable chapter pane and editor focus mode.** On the macOS
  app's Edit tab, drag the divider between the chapter outline and the editor
  (double-click resets; the width persists, with minimums that grow with text
  zoom). Focus mode (⇧⌘E or the button in the page row) folds away the outline
  and the harness strip; the file name, Unsaved dot, Save and View AGENTS.md
  stay visible. Part of #1894.
- **Devin is a verified MCP sync target.** `swe mcp sync` writes
  `~/.config/devin/mcp_config.json` (Devin CLI v3000.3+ /
  [MCP configuration docs](https://docs.devin.ai/cli/extensibility/mcp/configuration)),
  not the unverified `~/.devin/mcp.json` guess. `swe mcp list` shows a
  Devin column once the harness is registered.
- **Harness targets beside the chapter editor (Mac).** The AGENTS.md
  page's Edit tab shows a Targets strip above the open chapter: a logo
  and name chip per harness, bright when it gets the chapter, a line
  saying why (no rule, an `only` or `except` rule in targets.json, or
  off in index.txt), and a folder line when every chapter in its folder
  shares the same targets. Clicking a chip or the In AGENTS.md checkbox
  writes the same index.txt and targets.json edits as the Compile tab,
  through the same save queue, so both tabs always agree. Compilation
  rules are unchanged.

### Changed

- **FEATURES.md is now a feature inventory.** The cross-client matrix became `## <Area>` sections with one line per feature per surface (CLI, web, iOS, macOS, API or job), each giving its status (`stable`, `beta` or `planned`), entry point and the tests behind it, so QA checks can map onto it. Each client's notes and what it still owes moved onto its own line, and `tests/test_features_inventory.py` checks the line format and that every test path exists. The table of logic ported between Swift and Python moved to `docs/ported-logic.md`, and the README links FEATURES.md.

- **Mac Accounts now groups accounts into horizontally scrolling provider columns.**
  Narrow cards keep the plan visible, with usage rings and settings beneath. Click a
  card to smoothly expand its readings; drag provider columns horizontally to save their order.
  Expanded cards put settings beside the launch buttons and wrap their status text.

- **Mac Codex account cards show monthly credits used and the credit limit** beneath the usage bars when the workspace returns those amounts.

### Removed

- **The in-repo MCP gateway daemon.** `swe mcp gateway serve`,
  `install` and `uninstall` are gone: the gateway lives in the
  standalone `warp-gate` package (repo `starship-terminal/warp-gate`,
  installed as the LaunchAgent `com.starship-terminal.warp-gate` by
  `warp-gate install`), serving the identical
  `http://127.0.0.1:8478/mcp/<name>` contract. `swe mcp gateway`
  keeps `share`, `unshare` and `status` — status still reports a
  machine's installed agent, including a retired
  `com.cwenlong.swe.mcp-gateway`/`com.cwenlong.quiver.mcp-gateway` one
  left over from an older install — and `swe mcp sync` still writes the
  gateway URL for `"shared": true` servers while `gateway.enabled` is
  set. A stale agent left by an old swe keeps no home here: bootout and
  delete its plist (`launchctl bootout gui/$UID/<label>`, then remove
  `~/Library/LaunchAgents/<label>.plist`), then `warp-gate install`.


### Security

- **A task link can no longer point the hub's `gh` at any repo.**
  `POST /v1/sessions/{key}/task` stored a GitHub task for whatever repo
  the client named, and every `GET /v1/tasks` then ran `gh` against it
  as the hub owner. A new link to a repo the hub hasn't pinned now
  answers `422 not_pinned`. Tasks already stored keep their links and
  keep refreshing after their repo is unpinned.

- **The hub's session journals are private to your account.** Every
  prompt, reply and tool call a remote session sees is journaled under
  `~/.swe/data/remote/journal/`, which was created 0755 with 0644 files,
  so any other local account could read them. The folder is now 0700
  (an existing one is tightened) and its files 0600.

- **A pairing poll's secret no longer travels in the URL.** The web
  app and the iPhone app polled `/v1/pair/request/<id>?secret=…`, so
  the secret that collects a new device's credential could land in
  proxy and access logs. Both now send it in an `X-Pair-Secret` header;
  the hub still reads `?secret=` for iPhone builds already out.

- **An Open in session prompt no longer carries a stranger's PR text.**
  Anyone can open a pull request on a public repo, and its title, body
  and failing check names (which a fork's own workflow can name) went
  word for word into the first prompt of the agent reviewing it. The
  brief now follows the resolve prompt's rule: it quotes the title and
  body line by line behind a note that they are untrusted data, only
  when the author's `author_association` is `OWNER`, `MEMBER` or
  `COLLABORATOR`, and otherwise says in one line that they were left
  out. Failing check names are always quoted, and the web's Open in
  session sheet no longer puts the title in the prompt it prefills.
  The hub's PR detail gains `association`.

- **A resolve prompt no longer carries strangers' comments.** Anyone can
  comment on a public issue, and every comment went word for word into
  the first prompt of the agent resolving it. The prompt now quotes the
  issue's title, body and comments line by line behind a note that they
  are untrusted data the agent must not take instructions from, keeps
  only comments whose `authorAssociation` is `OWNER`, `MEMBER` or
  `COLLABORATOR`, and replaces the rest with "N comments from others,
  not included". Every client prefills the hub's `suggested_prompt`, so
  the Mac, iPhone and web resolve sheets all get the new prompt; issue
  comments in `--json` gain `association`.

### Fixed

- **The Mac app recognises sessions in `starship-terminal/swe` worktrees again.**
  After the rebrand, the issue Resolve dialog stopped matching checkouts and
  `starship-terminal__swe-…` worktrees to this repo, so it no longer
  preselected the harness last used there. Folders made under the old
  `c-wenlong` owner or the `quiver` name still match.

- **Models speed and latency survive Artificial Analysis dropping `performance` on Dec 10, 2026.** The Mac app reads output speed, time to first token and time to first answer token from `performance_v3` (means) first, then the old `performance` medians, then the legacy top-level keys, so the Models page columns stay filled once `performance` goes null.

- **`swe init` saves the harness executable even when it is off PATH.**
  Registering `~/.vibe/skills` before `vibe` was installed stored an empty
  `command` under `mistral-vibe`, so `swe list` kept reporting the harness as
  not installed after `vibe` appeared, because it looked for a binary named
  `mistral-vibe`. Init now takes the command from the built-in catalog for the
  canonical key (`vibe`, `qwen`, `droid`), and a `command` already set in
  harness.json is never overwritten. Follow-up to
  [#1916](https://github.com/starship-terminal/swe/pull/1916).

- **A collapsed sidebar no longer blocks clicks beside the rail (Mac).** The
  rail kept its icons laid out at the expanded width, and `.clipped()` hides
  that overflow without removing its hit area, so it swallowed clicks up to
  the expanded width, such as the AGENTS.md Edit tab. The list now takes
  clicks only over its visible width, and the title bar reader no longer
  takes clicks. Closes #1892.
- **Codex education monthly usage reads the workspace billing allowance** when the standard usage response omits it, showing both the percentage and credit amounts.
- **Installed plugins can be switched and removed before import.** Claude Code and Codex on/off and uninstall controls work for harness-owned plugins. The Mac row and per-harness action menus use themed popovers, with confirmed removal from one or all harnesses; import remains separate for SWE file ownership.

- **AGENTS.md editor header no longer wraps at narrow widths (Mac).** In a
  900 pt window the Edit tab's Revert, Save and Close buttons wrapped
  mid-word ("Rever t"). A narrow editor now shows them as icons (named in
  the tooltip and to VoiceOver) and the Unsaved marker as a dot, and the
  Chapters heading stays whole; wider windows keep the words.
- **The Mac app no longer offers to trash a skill's SKILL.md by
  itself.** A skill's Contents card put a trash button on its main
  SKILL.md (and on each skill folder's SKILL.md in a plugin), though a
  skill can't work without it. Those rows now have none; every other
  file keeps its button, and removing the whole skill stays its own
  action.

- **Devin's ACP approvals say what they run.** `devin acp` sends a
  permission request with only the tool call's id, so the card read
  "permission requested". It now shows the title and command from the
  tool call Devin announced earlier, such as "Ran rm" with
  `rm hello.txt`.

- **A skills root that links to itself no longer crashes the skills
  commands on Python 3.11 and 3.12.** Before Python 3.13, `resolve()`
  raises `RuntimeError` on a symlink loop, and the skills root scan
  and root lookups only caught `OSError`, so a `~/.codex/skills`
  pointing at itself ended `swe skills <filter>`, `swe skills tree`,
  `swe skills link` and `swe skills move` in a traceback. A looped root
  is now handled like a dangling one: `swe skills link` replaces it, a
  path argument skips it, and `move` names it in an error. Linking a
  root to a target that loops is refused before the old link is
  removed, on every Python version.
- **One unreadable log no longer empties `swe models` for its harness.**
  The model tally read each Claude, Codex and Freebuff log with the
  locale's strict decoder and no error handling, so a single file with a
  stray non-UTF-8 byte, or one it lacked permission to open, raised out
  of that harness's whole scan and dropped every model count it had
  gathered. Each log is now read as UTF-8 with bad bytes replaced, and a
  file that can't be opened counts nothing on its own.
- **A report no longer lists a follow-up it refused to create.** When
  the report writer suggested a new follow-up whose `evidence` was not a
  list, the pipeline dropped the suggestion with a warning but had
  already put its id in the report manifest's `follow_up_ids`, naming a
  follow-up that never reached the ledger. The evidence check now runs
  before the id is recorded.
- **A second launch in a folder no longer strands both.** Adoption tells
  a new session apart only by harness, folder and start time, so two
  launches of one harness waiting in one folder each saw both
  transcripts and neither was ever adopted. A launch while another is
  still starting in that folder is now refused (`409 launch_pending`,
  naming the waiting tmux pane), so the first is adopted as before.

- **Messages typed into a tmux session can't drive the TUI.** Text the
  hub sends to a tmux pane now loses C0 control characters other than
  tab and newline (an ESC or ^C in a message cancelled turns or picked
  menu rows), and text with line breaks goes in as one bracketed paste
  instead of one Enter per line.

- **Issue resolves work on `swe/issue-<n>`.** `swe gh issue worktree`
  and the hub's Resolve with agent still cut `quiver/issue-<n>`. New
  resolves branch `swe/issue-<n>`; a checkout or branch a resolve left
  under the old name is resumed on it (and the prompt names it), and
  the Tasks rows still find the PR such a branch opened, so no work in
  flight is orphaned. The Mac, iPhone and web sheets' fallback label
  says `swe/issue-<n>` too.

- **swe's tmux panes are named `swe-…`.** Sessions the hub runs under
  tmux were still named `qv-<slug>`. New panes are `swe-<slug>`, so
  `tmux attach -t swe-…` is what the web chip, the Run in terminal
  button and the docs give. Panes a hub started before the upgrade keep
  their `qv-` name: the hub still writes to them, a satellite's rows
  still carry them, and a launch never starts a second pane beside one.

- **Linux hubs and satellites run as `swe-remote-*.service`.** `swe
  remote serve --install` and `swe remote agent --install` still wrote
  `quiver-remote-hub.service` and `quiver-remote-agent.service`. They
  now write `swe-remote-hub.service` and `swe-remote-agent.service`, and
  once the new unit has started they stop, disable and remove the old
  one, so a machine never runs two hubs after the upgrade. `--uninstall`
  removes both names, `--status` and `swe remote setup` still see an old
  unit that is all a machine has, and `scripts/install.sh` restarts
  whichever is active.

- **SWE.app ships the licences of the Python and Sparkle it carries.**
  The bundle held only SWE's own licence and CPython's and OpenSSL's
  texts. `Contents/Resources/Licenses/` now has Sparkle's `LICENSE` and
  the licence of each library the bundled Python links (libffi, Expat,
  SQLite, xz, bzip2, mpdecimal, libuuid, OpenSSL, zlib, libedit,
  ncurses), and the build stops if one is missing.
  `app/mac/THIRD_PARTY_NOTICES.md` lists them all and now names every
  colour harness logo.

- **The web app installs as an app and respects the notch.** Its
  manifest now says `display: "standalone"` and carries an `id`, and its
  background and theme colours follow the light default and the dark
  scheme (`color_scheme_dark`). The page declares one `theme-color` per
  scheme, and a Light or Dark picked in Settings overrides both, so the
  status bar matches the page. The sidebar, pages and chat composer pad
  themselves by `env(safe-area-inset-*)`, since the viewport covers the
  whole screen.

- **Phone SSH keys carry the `swe-remote:` tag.** The hub still tagged
  the `~/.ssh/authorized_keys` lines it manages `quiver-remote:<device
  id>`. New lines say `swe-remote:<device id>`; a line under the old
  tag still counts as the hub's (and as installed), and the next
  rewrite swaps it for the new one, so a revoke after the upgrade
  still removes it.

- **`swe harness discover --json --apply NAME` answers in JSON when NAME
  is not a new match.** It printed red text, which the app could not
  decode. It now prints the usual document with `applied.refused` and the
  name under `applied.skipped`, and exits 1.

- **The web app's model search and price edits no longer wait on the
  daily price download.** The LiteLLM price list was fetched while
  holding the lock every catalog read and price save takes, so a slow
  network stalled the Models search and the Pricing page. The fetch now
  runs outside it, and a download cut short counts as tried for the day.

- **A model or effort switch is no longer typed into a TUI that can't
  take it.** On a tmux session the hub typed `/model <v>` or `/effort
  <v>` into any harness's pane, so opencode or codex (for effort) got the
  line as a prompt to the model. It now refuses with `unsupported_control`
  unless the harness's TUI is known to run the command, the rule the
  session's `model_modes` and a launch already follow.

- **A status feed with a `NaN` or out-of-range time no longer blanks
  the web Status page.** Python's JSON reader takes `NaN`, `Infinity`
  and `1e400`, and the incident time went back out as bare `NaN`, which
  the browser refuses to parse. Such a time now reads as unknown, as it
  does in the Mac app.

- **A late approval tap can no longer answer the next terminal prompt.**
  Once a prompt was answered at the terminal, its request id stopped
  being checked, so a phone's delayed tap on it could answer the next
  prompt the agent showed before the hub had named it. A replaced or
  cleared id is now refused. An approval's key walk also shares the
  pane's write lock, so a message steered in at the same moment can't
  land between its arrow keys.

- **One odd message from codex no longer stalls a Codex session.** An
  app-server message the hub couldn't read (an approval with list
  `params`, an unhashable id) ended the reader while codex kept running,
  so every later send waited its full 60 s and failed until a detach.
  The reader now skips such a message, and a server whose reader did
  die is replaced on the next send.

- **Archiving Qwen Code stops its shared hooks, and its hook settings
  count.** The hook contract table keyed Qwen Code as `qwen`, its
  command, while harness.json calls it `qwen-code`. Archiving qwen-code
  still deployed shared-hook wrappers into `~/.qwen/hooks`, and a
  `capabilities.hooks` entry on qwen-code was never read. The row is now
  `qwen-code`, and `swe init` (or `swe hooks sync`) moves an existing
  `~/.swe/hooks/qwen/` to `hooks/qwen-code/` once and relinks it; a hub
  that already has both folders is told to merge them by hand. The move
  waits while a link to the old folder could not be relinked (qwen-code
  archived, or the link in `.linkignore`) or a settings file names the
  old folder, so no hook stops running.

- **`swe skills new` says when it had to make `~/.swe/skills`.**
  `create()` promised to report whether it made the hub skills root but
  always answered no, so a skill written into a brand-new root (one no
  harness links to yet) printed only "Created". It now adds a line
  saying to run `swe init` to link the root into your harnesses.

- **Qwen Code sessions get `@` file references for attachments again.**
  The hub's attachment fallback table keyed Qwen Code as `qwen`, its
  command, but sessions carry the registry id `qwen-code`, so a file
  sent from a phone arrived as an `[Attached file: …]` line instead of
  an `@` reference.

- **A satellite's malformed push no longer breaks the hub's routes.**
  `/v1/agent/register` answered 500 when `machine` was not an object; an
  uploaded event whose `data` was not an object failed its whole batch,
  which the agent then resent forever, journaling the good events again;
  a "/" menu push with the wrong shapes turned that satellite's menu
  route into a 500; and `NaN`/`Infinity` (or `1e400`) in any request
  body was stored and served back, which no client's JSON parser
  accepts. Each is now a 422, a skipped event, a skipped row or a 400.

- **An approval resolved without a request id now kills its ntfy
  buttons.** The hub clears every pending approval on such a resolve,
  but the notification tokens were retired only by exact request id, so
  the buttons of the cleared prompts stayed spendable until they expired
  and could answer the next prompt on the session.

- **An opencode approval's request id can no longer reach another serve
  route.** The hub put the client's request id into the opencode serve
  URL as typed, so an id like `../../<session>/abort` posted to a
  different route; ids and session ids are now one quoted path segment.

- **A wildcard hub bind no longer turns off the Host check, and a
  second `swe remote serve` on a taken port no longer disturbs the
  running hub.** `--host=0.0.0.0 --unsafe-bind-any` left the hub's
  allowed Host names empty, which lets any Host through, so a page that
  rebound its name to the hub could pass as the owner's own tailnet
  device; `::` was not even read as a wildcard. Both now answer this
  node's loopback, hostname and tailnet names, plus any IP address on
  the hub's port (a LAN pairing link), since a rebound page always
  arrives under its own domain name. `serve` also binds before
  it starts the hub's loops, so a copy that can't get the port exits
  before it registers the machine, settles kept sessions or retires
  ntfy credentials under the running hub.

- **Codex sessions were undercounted when codex ran subagents.**
  `parse_codex` dropped every thread whose store `preview` was empty,
  treating it as opened and never typed into — but codex also leaves
  `preview` blank on spawned threads (subagents, automations) no matter
  how much work they did. On a machine running codex's multi-agent
  feature that hid almost every session. A thread now drops only when
  `preview` is empty *and* `tokens_used` is zero — the store's own record
  that it never ran — and kept spawned threads are titled from
  `agent_path`'s leaf or `agent_nickname` rather than their transcript's
  injected-context first line. `swe session` and the session counts on
  `swe list` also pick up `~/.codex/archived_sessions/` now: archiving a
  thread moves its rollout there, and an archived session still ran.

- **`swe init` no longer re-asks about a harness it already
  registered.** A skills root discovered under an aliased label
  (`~/.vibe/skills` is "vibe", the binary behind `mistral-vibe`) was
  filed in `harness.json` under that label, while every "is this root
  known?" check translates the label to the canonical registry key —
  so the entry never matched and the "New skills folders found" picker
  came back on every run, most visibly inside `darwin-rebuild`
  activations. Registration now writes the canonical key and absorbs a
  stale label-keyed entry, and the known, archived and disabled-name
  checks all count a key's canonical name too, so an entry an older
  init filed as `vibe` still reads as registered.

### Added

- **Devin free-model limit status.** Accounts and the Mac menu bar show
  a reached free-model limit and its reset time from Devin's CLI logs,
  separately from daily and weekly paid quota. The CLI marks an active
  block with `free:blocked`; web and iPhone cards show it too. Without an
  active observed block, the cards say Unknown because Devin does not
  report a remaining free-model percentage.

- **MCP auto-adoption and remove-everywhere.** `swe mcp sync` now ends a
  successful run by adopting every harness-only server into the hub:
  the first holder's definition in sorted harness order lands in
  `mcp.json` canonicalised and credential-shielded, `swe.adopted_from`
  records the donor, and a disabled mark for every registry harness not
  already holding the server keeps later hub syncs from pushing it
  anywhere it never lived — `swe mcp enable NAME --for=<h>` is how one
  deliberately leaves its scope. The same scoping applies to a
  `discover --apply` pull and to the hub copy a `disable` stashes.
  `--no-adopt` skips the pass, `--json` reports the names under
  `adopted`, `list`/`info --json` rows carry `adopted_from`, and `mcp
  doctor` names what the next sync will adopt. And `swe mcp remove
  NAME` no longer needs `--from`: it deletes the server from the hub
  and every harness file holding it — the only shape that stays gone,
  since a leftover file copy would be adopted back — purging its
  disabled marks and provenance, warning on any file it cannot write,
  and warning when a hub-only removal leaves file copies the next sync
  would pull back.

- **Skill and plugin usage: `swe skills usage`, `swe plugins info`.**
  `swe skills usage <name>` counts `Skill: <name>` tool calls in the
  session search index — calls, sessions, last used, split by harness,
  with `Skill` calls indexed before the `skill` input key tallied as
  `unattributed` since they carry no name. `swe plugins info
  <plugin>@<marketplace>` is the detail sheet the catalog table cannot
  hold: the plugin's declared MCP servers with env and header key names
  (values are never read) joined with `swe mcp list`'s state, hub
  membership and missing-secret names, its shipped skills, when its
  files last changed, and one usage rollup over `Skill: <name>` and
  `mcp__<server>__*` calls. Both answer `tracked: false` under `--json`
  when the index is off (`swe session index --on`). The `skill` input
  key joined the search writers' argument keys in Python and Swift, so
  calls indexed from now on carry the skill's name.

- **Per-harness skill selections: `swe skills enable|disable|sync`.** A
  harness's skills folder no longer has to be the whole shared tree or
  nothing: it can be a directory of one link per shared skill it keeps.
  `swe skills disable foo --harness=codex` on a linked folder converts it
  to an `except` selection (every shared skill but the ones switched
  off, and new shared skills still arrive); `swe skills enable foo
  --harness=claude` on a folder with no skills dir creates an `only`
  selection, the way a harness picks up a single shared skill while
  keeping its own private folders. `swe skills sync` re-applies every
  recorded selection: skills added to `~/.swe/skills` link into `except`
  roots (and `swe skills new` does the reconcile on the spot), links to
  deleted skills drop, and a link removed by hand comes back while its
  record still wants it. `swe init` now classifies such a directory as
  `selection` instead of `absorb` — it used to flatten the folder back
  into one symlink and erase every exclusion. `skill_links.json` gains
  `kind: "selection"` records holding the `mode` and the name list, so
  state survives hand edits; `swe skills --json` roots report `mode`,
  `enabled` and `disabled`, and per-skill `harnesses` attribution counts
  a folder that links the skill rather than resolving the whole root.
  Relinking a selection to the whole folder needs no `--force` while it
  holds only links (nothing private to lose), still needs it when a
  private folder sits inside, and `swe skills unlink` on a selection
  drops the shared links but keeps the private folders. On the Mac the
  harness-links panel marks a selection root with its own state and
  offers Link all skills / Unlink all; the web page mirrors it. Per-skill
  toggles in the clients come next.

- **Cursor's `store.db` sessions list and read.** `~/.cursor/acp-sessions/<id>/`
  and `~/.cursor/chats/<workspace>/<id>/` hold the sessions Cursor's editor
  agent, SDK and headless runs write without an `agent-transcripts` JSONL:
  title and cwd come from `meta.json` and the session manifest, and the
  transcript lives in `store.db` as content-addressed blobs ordered by the
  `latestRootBlobId` manifest chain. A new `sessions/cursor_store.py`
  decodes that format, `parse_cursor` lists its sessions, the transcript
  reader falls back to it when no JSONL exists, and the status probe reads
  a store's tail (a landed assistant text is `finished`; the store records
  no error or abort signal). A session id that exists in both formats keeps
  its JSONL row, and one the account-wide usage feed reports but no local
  machine holds stays details-only.

- **A Services tab under Settings › Providers.** Provider kinds are
  now three: `inference` for the AI providers, `infra` for deploy and
  platform keys, and the new `services` for the agent-facing APIs a
  tool calls rather than deploys onto: search, scraping, research and
  browser services. `DEFAULT_SERVICES` ships twenty-one — Parallel,
  Firecrawl, TinyFish, Browser Use, Exa, Tavily, Brave Search,
  Browserbase, Apify, E2B, Runloop, Supermemory, Pinecone, Langfuse,
  Braintrust, ElevenLabs, Deepgram, AssemblyAI, Cartesia, Stability AI
  and Replicate — seeded into providers.json
  like the other built-ins, so `swe providers list` prints a Service
  Providers table after the AI and Infra sections, a `services` filter,
  a slug or an env var narrows to it, and `swe providers add
  --kind=services` registers one (existing custom kinds still file
  under AI Providers). The setup wizard's LLM providers stage leaves
  both non-inference kinds out. On the Mac the Providers page gains a
  Services tab beside Infra sharing its catalogue browsing, filters,
  drag order, marks and add/edit/remove/check flow under its own
  `servicesOrder` and `servicesKeys` stores — inference claims a
  variable or heading first, then infra, then services — with a Move to
  Services action on the Inference tab to match Move to Infra. Each
  service's Check button probes a cheap authenticated endpoint, except
  Brave (its API answers a bad key with a 422, not a rejection the
  check can read) and Langfuse (the API wants the public/secret pair
  as basic auth). The web
  app reads the same merged registry, so service keys keep their names,
  env vars and key URLs in its provider data; it has an Infra tab but
  no Services tab yet.

- **Remote sessions can run over the Agent Client Protocol.** `swe
  remote attach` (and the apps' Attach) now picks a harness's ACP mode
  when its CLI has one installed (`devin acp`, and cursor-agent or
  copilot builds that list it), after the codex and opencode channels
  and before tmux. The hub keeps the
  agent running per session: replies, thoughts and tool calls stream
  live, permission requests arrive as approvals with the agent's own
  options as the answers, and Stop cancels the turn. Attach loads the
  session over ACP; if the agent can't load it or fails to start (not
  signed in, say), attach falls back to tmux and the note says why.
  `--mode=acp` on an agent that can't load sessions falls back to tmux
  too, since a new ACP session would never write to the chat. A new session launched from the apps for such a harness
  starts over ACP too (`session/new`, then the first prompt), unless
  the launch picks a model, effort or provider. With Autonomy on, the
  channel answers each permission request with the agent's "allow
  once" option (never an "always" one) and the chat shows it as
  allowed; a request without one still asks you; if the agent
  fails to start, the launch falls back to tmux. Claude can run through the `claude-agent-acp` adapter with `--mode=acp` when it is installed;
  harness.json's `capabilities.acp` overrides or turns off detection
  ([#1637](https://github.com/starship-terminal/swe/issues/1637)).

- **Folders nest in the AGENTS.md rail.** Dragging a folder row onto
  another folder (or onto a file row inside it, or the "Top level"
  strip) moves the directory on disk, and every chapter under it keeps
  its index.txt slot, its on/off state and its targets.json rule under
  the new path. A folder can't move into itself or its own subfolder,
  and a name already taken on disk, in index.txt or in targets.json is
  refused with a message under the rail's header. ⌘Z puts the folder
  back. A drag resting on a collapsed folder springs it open. A moved
  folder and its subfolders keep their open or closed state, on the
  Edit rail and the Compile tab, after the move and after undo.

- **Chapters drag between folders in the AGENTS.md rail.** A file row
  drops onto a folder row to move the chapter on disk; its index.txt
  entry keeps its slot (an off comment follows it) and any targets.json
  rule is re-keyed, so a move never widens the chapter's harness set.
  The folder row glows and grows as the drag crosses it.

- **Chapters reorder and undo in the AGENTS.md rail.** The rail lists
  each folder's files in index.txt order, so dragging a file up or down
  reorders the index: rows below slide up into the vacated slot as the
  drag passes them, and only the pointer's height counts — sideways
  drift never reparents. A drop on a folder row still moves the file
  there, a "Top level" strip appears under the outline for the root,
  and dropping between two rows lands the index entry in that gap.
  Every rail move registers on the window's UndoManager, so ⌘Z (and
  ⇧⌘Z under Edit) walks file moves and reorders back.

- **Hover tips no longer eat clicks.** A tip is a separate window and
  could float over its own control when it flipped below for room, so
  the first click only dismissed the tip — visible buttons like an API
  probe's Test needed two presses. The tip window now ignores mouse
  events everywhere it appears: a click on covered controls goes
  straight through, and a click on the tip itself still dismisses it.

- **Muse shows Meta's logo.** The `muse` harness id is aliased to the
  bundled `meta` mark (`harness-meta-color.svg`) on the Mac, iPhone and
  web apps, so its limit card carries Meta's logo and brand colour
  instead of a text chip.

- **`swe list` shows Muse's weekly quota.** Meta's Muse app
  (`com.meta.endo`) keeps its session tokens in a private keychain group
  nothing else can read, so the new `muse` rate-limit fetcher borrows the
  logged-in Dia browser instead: it decrypts the `hatch_sess` cookie from
  Dia's Chromium cookie stores with the "Dia Safe Storage" keychain item,
  reads the short-lived ABRA bearer token every `muse.ai` page embeds, and
  calls `hatch-api.meta.ai/hatch/subscription` for the tier, percent used
  and weekly reset. Register Muse as a harness (`swe add muse muse`) and
  star it to add it to the polled set; the first run prompts once for the
  Safe Storage item.

### Changed

- **The web app keeps its browser settings under `swe.` names.** Its
  `localStorage` keys (the device credential, appearance, hidden pages,
  the Settings section, Compute and Calendar choices, the Agents folder,
  hidden MCP harnesses, the setup offer) were still `quiver.*`. Each now
  lives under `swe.*`; the first load after the update moves a browser's
  old keys across and deletes them, so nobody is signed out or loses a
  setting. The old credential key stays until the next sign-in or
  sign-out, so a tab still open on an older page keeps working.

- **Pair QR codes stay on `quiver://pair` for one more release.** The
  iPhone app has opened both `swe://` and `quiver://` links since it
  became SWE Remote (2026-11-01), but a phone still running a build from
  before then registers only `quiver://`, and a `swe://` code would open
  nothing there. Hubs keep writing `quiver://pair` until the next
  release, which switches to `swe://pair`: rebuild the iPhone app from a
  current checkout before then.

- **The MCP pages name the hub `swe` when they sync or remove.** The
  Mac and web MCP sheets still sent the hub's pre-rename name, `quiver`,
  as a sync source and remove place. Both now send `swe`; the hub's web
  API, the Mac model and `swe mcp` keep accepting `quiver`, and server
  rows still list the hub under both names in `remote_in`, so an older
  app build or an open page keeps working.

- **The web app marks its own requests with `X-Swe-Pair`.** The header
  that lets the hub owner's tailnet identity in without a bearer was
  still called `X-Quiver-Pair`. The web app now sends `X-Swe-Pair: 1`;
  the hub accepts either, so a browser tab still holding an older page
  keeps working until it reloads.

- **A stored Linear task's refresh no longer downloads its
  description.** The hub asked Linear for each issue's full body on
  every task read and never used it. The unused Notion page-body
  renderer, built for a resolve route that never shipped, is gone
  too.

- **Cursor's limit meters read "Cursor models" and "Other models", and
  reset times show two units.** The rows the Cursor accounts and harness
  cards listed as "Included" and "API" now say what they count; any
  reset a day or more out adds the hours ("in 6d 13h" rather than
  "in 6d"), matching the shorter windows' "in 4h 28m". Same names and
  countdown on the Mac, iPhone and web apps.

- **The chapters rail's "+" opens New chapter in one click.** It was a menu behind a hover tip that swallowed the first click; New folder went with it since an empty File field already creates a folder.

- **The New chapter suggestions drop below their field and wait for typing.** The floating list now hangs 4pt under the focused field (the earlier overlay drew it upward over the field), opens only after a 250ms pause on a non-empty field or on Down Arrow, and an empty field shows no list at all.

- **The New chapter sheet no longer resizes as it suggests.** Its
  folder/file autocomplete now floats under the focused field instead of
  pushing the card taller, capped to the room between the field and the
  card's bottom edge with the list scrolling beyond.

- **The New chapter/folder sheet suggests what exists.** Typing in its
  Folder or File field opens an inline list of matching folders and
  chapters — ranked by closeness, the folder's own children first — plus
  a "New ..." row when nothing takes the name, picked with arrows and
  Return. An existing chapter opens straight into the editor.

- **The Compile tab chooses what every harness compiles.** The AGENTS.md
  page's new Compile tab turns the harness strip into a filter: All shows
  every chapter and folder's include checkbox (on, off, and unlisted
  index.txt states), and picking a harness toggles its only/except rules
  in targets.json. The Order list drags index.txt order back and, in All
  view, shows a chip per harness per chapter for one-click per-harness
  receipt. View AGENTS.md opens the filtered output's own file.

## [1.2.2] - 2026-11-04

### Added

- **A one-line installer for Linux.** `curl -fsSL
  https://github.com/starship-terminal/swe/releases/latest/download/install.sh | sh`
  downloads the released wheel (verified against the release's new
  `SHA256SUMS` asset) and installs it with `uv` when it can, into a venv
  under `~/.local/share/swe` when it can't, and bootstraps `uv` when
  neither Python 3.11 nor `uv` is around, which covers Ubuntu 22.04's
  Python 3.10. Passing `--hub` runs `swe remote setup --apply` so the web
  app and phone remote come up in the same step. No sudo, nothing written
  to shell rc files. Releases now attach the script and `SHA256SUMS`, and
  CI runs the installer end to end in `ubuntu:22.04` and
  `python:3.12-slim` containers.

- **Instruction chapters can be pinned to harnesses.** An optional
  `~/.swe/AGENTS_MD/targets.json` maps a chapter to `only` or `except`
  lists of harness names, and a chapter can be switched off by commenting
  its index.txt line out. `swe instructions build` compiles a differing
  harness's chapters into `~/.swe/AGENTS.<name>.md`, repoints swe's
  instruction symlinks (extra account homes included) and prunes retired
  outputs; `check` and the `--json` surveys report the per-harness state,
  and `swe init` links to a harness's own output once it exists.

- **The landing site gets a dedicated install page.** `install/` puts
  macOS and Linux behind a tab toggle: the Mac side carries the SWE.app
  DMG download, wired to the latest release like the homepage buttons,
  and the brew cask; the Linux side has the `install.sh` one-liner plus
  its `--hub` variant for the web app and phone remote. Install buttons
  across the site now open the page instead of scrolling to the
  quickstart, which stays as the on-page summary.

### Changed

- **The AGENTS.md page edits chapters as an outline.** The Mac app's
  chapter rail is now the `~/.swe/AGENTS_MD` folder as an indented tree —
  folders group chapters and files not in index.txt read muted — with a
  proper New chapter/New folder sheet instead of the alert. The compiled
  AGENTS.md moved behind a View AGENTS.md button into a centre popup that
  renders it as Markdown and jumps to a chapter on click. Reordering and
  unlisting chapters move to the upcoming Compile tab, so they are
  temporarily unavailable in the app; edit index.txt by hand meanwhile.

- **The web chat keeps its newest 200 entries in the DOM.** A long
  transcript, or a chat left open for hours, kept every entry it ever
  drew, so layout, style recalculation and memory grew with it. Older
  entries now leave the page for a "Show earlier" button at the top of
  the log, which puts them back 200 at a time, still in order and with
  their tool cards intact. On a 2,000-message transcript the log holds
  982 element nodes instead of 9,800. The fold runs only when the log
  is scrolled to its end, so a reader up in the history loses nothing
  above them; the fold stops at a send still waiting or an
  unanswered approval, so every entry keeps its place. `SessionChat.revealSeq(seq)` expands the history down to one
  message before it scrolls there. Browser find-in-page sees only the
  drawn entries. Closes [#1615](https://github.com/starship-terminal/swe/issues/1615).

- **Search indexing reads only a Devin session's new nodes.** Both index
  writers (the Mac app's `SearchDevin.swift` and swe's
  `search_containers.py`) re-read a Devin session's whole chain each
  time it grew, and the chain walk loaded every node's parent first:
  1.0 to 2.3 s for each of the largest sessions on every 60 s reconcile.
  A read now walks the chain node by node from the head and stops at the
  `rowid` watermark the last commit left in the index's `meta` table
  (`devin_chain:<path>`, written by either side). A growing session costs
  about 1 ms; one whose head moved to a streamed copy or whose indexed
  node was rewritten reads whole in 25 to 45 ms ([#1613](https://github.com/starship-terminal/swe/issues/1613)).

## [1.2.1] - 2026-11-04

### Fixed

- **The tag build's Swift test step compiles again.** `SilentHubStub`'s
  `await Self.started.bump()` inside a `Task` tripped a region-isolation
  checker bug in the macos-26 runner's Swift 6.3.3 ("pattern that the
  region-based isolation checker does not understand how to check"); the
  test now names the class. V1.2.0 never got its DMG, so this ships as a
  patch.

## [1.2.0] - 2026-11-04

### Added

- **The Mac app's chat renders the agent's Markdown tables.** The chat
  used to run the issue/PR-body parser, so a pipe table showed as raw
  `|` text. Session messages now parse with `ChatMarkdown`
  (`SWECore/Hub/ChatMarkdown.swift`), a port of the phone's chat parser
  (`SWERemote/Hub/Markdown.swift`, the same one the web app's `md()`
  ports): a pipe table draws as a grid that scrolls sideways inside its
  own box (bounded at 24 columns and 200 rows, with a note for the
  rest), a lone `![alt](src)` image or a harness-drawn `<json-render>`
  view folds into a note chip, and a link to the agent's own files —
  like the ones Codex ends a turn with — shows as a code-styled file
  name instead of a dead link, on user, reply, thinking and error text
  alike.

- **`SWE_MACHINE_NAME` names this machine in `swe sync` commits.**
  Sync's commit and merge messages say `swe sync from <hostname>`; set
  `SWE_MACHINE_NAME` to use another name, for a demo recording or a
  hostname you would rather keep out of the hub's history. The
  `docs/demo.md` helpers set it to `machine-a` and `machine-b`.

- **`swe setup --hub` brings the hub's plugins and MCP servers in too.**
  After cloning the hub and registering its plugin marketplaces, setup
  installs every plugin those marketplaces list into Claude Code and
  Codex (skipping ones already installed, and archived harnesses), and
  syncs the hub's `mcp.json` servers into every installed harness with a
  known MCP config, as `swe plugins install` and `swe mcp sync swe
  <targets>` do. A server whose `${NAME}` secret has no value is skipped
  with the command that sets it (`swe mcp secret set NAME`) while the
  rest sync, and a server a harness already has is left alone. A failed
  plugin or server is reported and never fails setup, and so is a hub
  `mcp.json` that can't be read or parsed. Registering marketplaces and
  installing plugins share a 150 s budget after the clone (90 s at most
  per install); a plugin it leaves no time for is reported as not
  attempted, with the `swe plugins install` command that installs it, so
  the Mac app always gets its answer. `swe setup --json` adds
  `hub.cloned.plugins` and `hub.cloned.mcp`, and the web Setup page and
  the Mac setup sheet name what didn't make it. The web Setup page's
  clone syncs MCP servers under the MCP page's write lock. Plain `swe mcp
  sync` still refuses to write anything while a secret is missing. Part of
  [#524](https://github.com/starship-terminal/swe/issues/524).
- **Demo hub link on every setup surface.** `DEMO_HUB_URL` now points at
  the public template [starship-terminal/swe-demo-hub](https://github.com/starship-terminal/swe-demo-hub),
  so `swe setup`, the setup wizard, the web Setup page and the Mac setup
  sheet show it beside the hub URL field. Fork it or use it as a
  template, then run `swe setup --hub=<your copy>`. Closes
  [#1446](https://github.com/starship-terminal/swe/issues/1446).
- **`scripts/demo_home.sh`: a safe stage for the primitives demo.** It
  builds a bare clone of the demo hub (`swe-demo-hub`, or `--hub=URL`)
  that stands in for the viewer's fork, so `swe sync` pushes stay on
  disk, plus two empty HOMEs (`machine-a`, `machine-b`) with a neutral
  git identity and nothing copied from the real HOME. `--home=PATH`
  seeds a dedicated macOS account as machine A instead, and refuses one
  whose `~/.swe` already holds a hub. It prints `export DEMO_ROOT`,
  `DEMO_FORK`, `DEMO_A` and `DEMO_B` lines to paste or `eval`, and
  leaves `swe setup` to the demo. Part of
  [#524](https://github.com/starship-terminal/swe/issues/524).
- **`scripts/demo_vm.sh record DIR` films the primitives demo.** It
  starts a throwaway run of the demo VM, runs every bash block of
  `docs/demo.md` segments 1 to 10 typed out in Terminal while it clicks
  SWE.app to each segment's page, and saves a voiceless 1920 by 1080
  `swe-demo-voiceless.mov` with `voiceover.md` and `voiceover.json`: the
  narration from `scripts/demo_recording/narration.json`, timed to that
  take. The VM's base now also builds the recorder, checks Terminal's
  Screen Recording and Accessibility grants, keeps its display awake and
  unlocked, and gets a short typeable password; a VM start that UTM drops
  is retried once after relaunching UTM. Part of
  [#524](https://github.com/starship-terminal/swe/issues/524).
- **`scripts/demo_vm.sh`: record the primitives demo in a UTM VM.** SWE.app
  reads the logged-in account's real `~/.swe`, so a take needs an account
  of its own. The script builds a macOS 27 guest in UTM (`create`: restore
  image, install, and a `demo` account with autologin and SSH set up on
  first boot), installs swe from main, Claude Code, Codex, Node and a clone
  of this repo over SSH, copies in SWE.app built from `origin/main` on the
  host (`provision`), and snapshots it as a clean base with no `~/.swe`
  (`save`). `take` starts it without saving changes and `reset` stops it
  and restores the snapshot; `shot FILE` screenshots it. No shared folders,
  clipboard, Apple ID or tokens in the guest. `docs/demo.md` gains a
  "Record in a UTM VM" section. Part of
  [#524](https://github.com/starship-terminal/swe/issues/524).
- **Overview across machines (web and iPhone).** Satellites now add
  their daily token and cost totals per harness to the usage report
  they post every five minutes (`daily_usage`, from their own ledger,
  priced offline). The hub adds them into the Overview: totals, harness
  split, daily rows and heatmap cover every machine, and `machine=<id>`
  shows one. The web Overview gets a Machines card with each machine's
  tokens and spend for the period (pick one to filter the page), and the
  iPhone's Usage tab a row of machine chips under the periods. A
  satellite's days are its own local days, and its view has no top
  models, since daily rows carry none. Closes
  [#525](https://github.com/starship-terminal/swe/issues/525).
- **Mac and web: Hub sync card.** Settings › Files shows `swe sync
  status` for `~/.swe` (branch mode, uncommitted changes by primitive,
  commits to push and pull, new commits on `origin/main`, files kept
  out) with a refresh that fetches first, and a Sync button that runs
  `swe sync` and says what it did. A conflict names the files and
  changes nothing; relink problems show as a warning. The web card
  syncs the hub machine's repo through `GET /v1/app/settings?part=hub_sync`
  and `POST /v1/app/settings/hub_sync` (one at a time). Sync stays
  manual; there is no timer. Part of
  [#525](https://github.com/starship-terminal/swe/issues/525).
- **`swe sync` and `swe sync status`.** Sync commits what changed in
  `~/.swe` with a generated message, merges the remote in, pushes, and
  re-links every harness (plus new plugin marketplaces, and MCP servers
  when `mcp.json` changed) when new content came in. On a
  `device/<machine>` branch it merges `origin/device/<machine>` and
  then `origin/main` and pushes only the device branch; on any other
  branch every machine shares it. Each merge is tried first with `git
  merge-tree`, so a conflict lists the files and leaves the repo exactly
  as it was. It never force-pushes and never commits `secrets/`,
  `cache/`, the `data/` stores (ledgers, search, remote) or anything else
  swe's `.gitignore` lists, even
  under an older hub `.gitignore`. `status` shows the mode, ahead and
  behind counts and uncommitted changes by primitive; both take
  `--json`, sync takes `--dry-run`. Part of
  [#525](https://github.com/starship-terminal/swe/issues/525).
- **Coding plans as launch providers for Claude Code.** Every plan in
  Settings › Providers › Inference is a provider Claude Code can launch
  on, one slug per plan region (`glm-coding-plan-china`): the launch
  sets `ANTHROPIC_BASE_URL` to the plan's endpoint, the plan key from
  its own variable (never the pay-as-you-go one), and `ANTHROPIC_MODEL`
  to the plan's default model. `plan_providers.PLANS` ports the Mac
  app's `InferenceCatalog`, and a test fails when they drift. Closes
  [#526](https://github.com/starship-terminal/swe/issues/526).
- **Provider menu on the launch sheets (Mac, web, iPhone).** The
  new-session sheet shows a Provider menu beside Model: Harness default,
  or a provider whose key is set on the target machine and that the
  harness takes (`providers_at_launch`, labelled from
  `provider_names`). The launch sends only the slug. The menu hides on
  a machine whose agent is too old or has no keyed provider for that
  harness. Picking a provider turns an untouched Model menu to Harness
  default, since the recent models ran on other providers; a model you
  pick yourself is still sent. Part of [#526](https://github.com/starship-terminal/swe/issues/526).
- **Doctor: Fix all on the Mac and web pages.** The findings card gets
  a Fix all (N) button whenever some finding has a batch fix. It runs
  `swe doctor --fix`'s ordered batch (setup, discover, init, plugin
  installs) behind a confirm on the web, then shows what it fixed, which
  fixes failed and how many findings are left for a person. Installs and
  the fixes that delete or overwrite never run in it and keep their own
  Run. The web route is `POST /v1/app/doctor/fix-all`, one at a time
  with the row Runs; the Mac runs `swe doctor --fix --json` behind the
  registry gate. The Mac app now also has Run for `undeclare`,
  `marketplace-drop`, `plugin-fix`, `plugin-reinstall`, `dangling-link`
  and `mcp-sync`, through `swe doctor --fix=<check> --message=<text>
  --action=<a>`. A test fails when the Python runners, the web page's
  wording or the Mac's action list drift apart. Closes
  [#529](https://github.com/starship-terminal/swe/issues/529).
- **Launch a harness on a provider from the keys file.** `POST
  /v1/sessions` takes `provider: "<slug>"` and `swe use` takes
  `--provider=<slug>`: the launching machine starts the harness with
  only that provider's variables from its own keys file, as
  `capabilities.provider` declares. In a tmux pane a small wrapper
  (`remote/provider_exec.py`) reads the file and execs the harness, so
  no key goes on argv, in the launch record, the marker or the journal.
  Machine rows list `providers_at_launch` (slugs whose key is set) and
  `provider_names`; the hub forwards only the slug to a satellite.
  A missing key or an undeclared slug is a 422. Part of
  [#526](https://github.com/starship-terminal/swe/issues/526).
- **Hub: service status pushes.** `swe remote notify
  --service-status=on` (the `service_status` flag in `notify.json`, off
  by default) starts a five-minute check of every tracked status page
  in `swe remote serve`, through the web app's shared poller. A model
  provider or agent platform starting an incident posts once to ntfy,
  and once more when it is back to normal; the hub emits these as
  `notify` events of kind `service`; it needs ntfy configured. Maintenance
  never posts, each page's first reading only records what is already
  open, and the delivered set in
  `~/.swe/cache/web-service-status-notified.json` keeps a restart
  from repeating a push. The rule is `status/alerts.py`, a port of the
  Mac's `ServiceStatusAlerts`. Closes
  [#528](https://github.com/starship-terminal/swe/issues/528).
- **Mac: service status banners.** With "Notify about service status"
  on (Settings › Notifications, off by default), the Mac banners when a
  model provider or agent platform starts an incident on its status
  page, and once more when it is back to normal. Each incident banners
  once: the same incident on the next poll, after a relaunch or after it
  flaps is quiet, two incidents opening at once are two banners, and
  scheduled maintenance never banners. Each page's first reading only
  records what is already open. A click opens Service status.
  Part of [#528](https://github.com/starship-terminal/swe/issues/528).
- **Launch providers declared per harness.** `harness.json` gains
  `capabilities.provider`: for each provider slug a harness takes at
  launch, the variables it reads for the key (filled from named
  variables in the keys file) and any fixed values such as
  `ANTHROPIC_BASE_URL`. `launch_provider.PROVIDER_FALLBACK` covers
  OpenCode (24 providers), Pi (16) and Claude Code (Anthropic, plus
  DeepSeek, Kimi, MiniMax and OpenRouter through their
  Anthropic-compatible endpoints), each tried on a real install with
  fake keys. No launch uses it yet. Part of
  [#526](https://github.com/starship-terminal/swe/issues/526).
- **Doctor folds in `swe mcp doctor`.** `swe doctor` now warns about
  each MCP server a harness config holds that fails a local check (a
  stdio binary missing, an empty env value, a bad URL scheme) as
  `mcp.server`; reachability stays `swe mcp doctor`'s, so doctor makes
  no network calls. When the hub's `mcp.json` holds the same name in a
  shape that passes, the finding gets `mcp-sync`: a click per finding
  that checks again, backs the config file up to `~/.quiver/backups`
  and runs `swe mcp sync quiver <tool> --only=<name> --force`, which
  replaces only that entry (through a symlink, the file it points at is
  what gets backed up), then checks again: a sync that skipped the entry
  is an error. The hub copy must name only stored secrets and have a
  command or url. A config the readers cannot parse is one
  `mcp.unchecked` warning. Batches never run it, since it overwrites.
  Part of [#529](https://github.com/starship-terminal/swe/issues/529).
- **iPhone: home screen widget.** Small and medium widgets show the
  hub's sessions waiting on you (approvals and replies), sessions still
  working, and today's spend
  ([#1442](https://github.com/starship-terminal/swe/issues/1442)). The
  widget fetches sessions, the inbox and today's overview about every
  15 minutes, and redraws when the app's refresh changes the snapshot
  it keeps in the App Group. When the hub can't be reached it draws
  that snapshot with "as of <time>". A medium widget's session row
  opens its chat through the new `quiver://session/<key>?hub=<hub>`
  deep link; anywhere else opens the app.
- **Doctor folds in plugin drift and broken hub links.** `swe doctor`
  now lists `swe find plugins`' drift as `plugins.<kind>` findings. An
  unregistered marketplace folder or an uninstalled plugin gets the
  `plugin-fix` action, which `--fix` and Fix all run (it only adds, via
  `plugin_drift.apply_fix`); a stale Codex copy gets `plugin-reinstall`,
  a click per finding since it removes the copy first; a disabled plugin
  or a linked manifest keeps its command. A broken top-level symlink in
  `~/.quiver` or its skills folder gets `dangling-link`, a click that
  copies the link to `~/.quiver/backups` and removes it, and refuses
  anything that is no longer a broken link directly in those folders.
  A marketplace `--fix` registers shows its uninstalled plugins to the
  next pass, which installs them. Part of
  [#529](https://github.com/starship-terminal/swe/issues/529).
- **Live reply text on iPhone, web and Mac.** The chat draws a reply
  word by word while a Codex app-server, opencode serve or headless
  Claude session writes it: `message.delta` text grows a bubble at the
  end of the transcript, rendered with the same Markdown as the final
  message and repainted at most every 80 ms. The next live
  `message.appended` of the same kind takes the oldest open bubble's
  place; on iPhone and web it is matched as it arrives and swaps in
  when the reveal queue shows it, without the arrival animation or
  pause. A missed delta stops drawing that bubble until its final
  lands, and a bubble whose final never comes goes 3 seconds after the
  session goes idle, or when the stream reconnects. The rules live in
  `ReplyStreams.swift` (the Mac's and iPhone's copies are identical)
  and its port in `pages/chat.js`. Closes
  [#1444](https://github.com/starship-terminal/swe/issues/1444).
- **`swe doctor --fix`.** Runs every safe local fix doctor found, in
  order (setup, then discover, then init), reads the report again after
  each step, and prints what it fixed and what is left.
  `--fix=<check>` scopes it (`--fix=setup`), `--dry-run` (or `--check`)
  previews, and `--json` prints `{dry_run, ran, fixed, still_failing,
  skipped, not_cleared, fixable_left}`. A fix that runs cleanly but
  leaves its finding (an empty discovery, a real file in the way) is
  listed as needing a person and never retried, so `--fix` settles.
  Installs and the drops that delete entries never run in a batch;
  `--fix=<check> --message=<text> [--action=<a>]` runs one finding's
  fix, as a Run button does. A failed fix is reported and the rest still
  run. Plain `swe doctor` still writes nothing. The runners moved to
  `harness/doctor_fix.py`, which the web Doctor page now calls too. Part
  of [#529](https://github.com/starship-terminal/swe/issues/529).
- **Unread dots on session rows: iPhone, web and Mac.** A session row
  shows a small dot when the agent said something since you last opened
  that chat on this device, from the hub's per-device `unread` flag.
  Opening a chat clears the dot at once and posts `POST
  /v1/sessions/<key>/read`; while it stays open, new messages post again
  at most every 3 seconds, and closing it posts once more. The next
  list refresh reconciles the local clear with the hub. The web app
  holds its reads while the tab is hidden. The Mac shows dots on local
  rows the hub lists and on Other machines rows, reading as its own
  paired device; the iPhone's app icon badge stays the Inbox count.
  Closes [#1439](https://github.com/starship-terminal/swe/issues/1439).
- **iPhone: share into a session.** A share extension ("Quiver" in the
  share sheet) sends text, links, images and files from any app to a
  session ([#1441](https://github.com/starship-terminal/swe/issues/1441)).
  The sheet lists the active hub's attached sessions, most recent
  first, with a hub picker when the phone has more than one, and an
  optional message. Text and links go in the message; images go up as
  JPEGs with the long edge at most 2048 px and other files stream from
  disk, one at a time, through the composer's attachments route and
  caps, then the message is sent with their refs. It reads the paired
  hubs from the App Group and shared Keychain group, and says so
  instead of sending when the phone isn't paired or the hub can't be
  reached.
- **Homebrew tap.** `brew install --cask starship-terminal/swe/swe`
  installs the Mac app and `brew install starship-terminal/swe/swe` the CLI,
  from [starship-terminal/homebrew-swe](https://github.com/starship-terminal/homebrew-swe).
  The release workflow's new `bump-homebrew-tap` job points both at each
  release's DMG and wheel with fresh SHA-256s (`scripts/bump_tap.py`),
  once the signed DMG is on the latest published release. It needs a
  `HOMEBREW_TAP_TOKEN` secret and skips with a notice without one.
  ([#1445](https://github.com/starship-terminal/swe/issues/1445))
- **Hub: live reply text (`message.delta`) for API channels.** Codex
  app-server, opencode serve and headless Claude sessions now send
  `message.delta` events while a reply is written: `{id, kind, text,
  seq, done}`, at most one per message every 100 ms, with the text
  redacted over the whole message so far. Headless Claude runs with
  `--include-partial-messages` (stdout only; transcripts and resume are
  unchanged). Deltas are never journaled and carry no `seq`, so replay
  and resume cursors are untouched; the final `message.appended`
  replaces the streamed text. Satellites coalesce their own and relay
  them only to a hub that advertises `message_delta`. tmux sessions
  stay record-by-record. Part of
  [#1444](https://github.com/starship-terminal/swe/issues/1444).
- **iPhone: the paired hubs move to an App Group and a shared Keychain
  group.** The hub list now lives in `group.com.quiver.remote` and each
  hub's credential in the `<team>.com.quiver.remote.shared` Keychain
  group, so an app extension can read them. Groundwork for the share
  sheet ([#1441](https://github.com/starship-terminal/swe/issues/1441)) and
  the widget ([#1442](https://github.com/starship-terminal/swe/issues/1442)).
  Existing pairings move on first launch: each credential is copied
  into the shared group and the old item dropped only after the copy
  is stored, and a copy that fails keeps reading the old item.
- **Hub: per-device unread state for sessions.** `POST
  /v1/sessions/<key>/read` moves the calling device's last-read cursor
  (body `{at?}`, default now), and every row from `GET /v1/sessions` and
  `GET /v1/app/sessions` carries `unread: true` when the session moved
  after that device's cursor. Cursors live per device in
  `data/remote/session_reads.json` (0600) beside `devices.json`, so a
  phone and a laptop keep separate state; the owner's tailnet browser
  gets its own `tailnet:<node>` cursors. A session the device never
  opened counts as unread only if it moved after the device paired, or
  after the hub learned this feature, whichever is later, so old rows
  start read. Satellite sessions work the same, stored on the hub. Each
  write prunes cursors for sessions the hub no longer lists and for
  revoked devices. Part of
  [#1439](https://github.com/starship-terminal/swe/issues/1439); the client
  dots follow.
- **Mac: the AGENTS.md page edits instruction chapters.** When the shared
  file is chapter-managed (`~/.quiver/AGENTS_MD/index.txt` exists), the
  Global tab becomes a chapter editor instead of a flat-file editor: a
  drag-to-reorder rail mirrors index.txt order, `+` creates a chapter with
  a heading and lists it, `−` unlists a chapter while leaving the file on
  disk, and every save runs `swe instructions build` so the file every
  harness reads never goes stale. The generated output shows read-only
  with a Current/Stale/Not built state and a Rebuild button for edits
  made elsewhere. With no index the flat editor stays, joined by a
  "Convert to chapters" card that runs the new `swe instructions init` —
  which folds the current AGENTS.md into `AGENTS_MD/main.md` (stripping
  compiler comments when it was generated) and builds. `build`, `check`
  and `init` take `--json`: `check` reports the chapter list, staleness
  and any source refusal, so the page knows which mode to draw.

- **Mac: a machine switcher opens another machine's hub in the window.**
  The sidebar's first row picks "This Mac" (the native pages) or another
  machine's hub, whose web app then fills the window in a web view. The
  hub signs it in as it does a browser on this Mac: the owner's own
  tailnet device needs no pairing, any other device gets the web app's
  pairing card. The view stays on that hub's origin (other links open in
  the default browser, `file:` never loads), keeps its own web data per
  hub, and a strip in the title bar names the machine. The list holds the
  hub this Mac's satellite agent reports to and hubs added by address in
  Settings › Integrations › Remote › Other hubs; the last pick is
  remembered ([#681](https://github.com/starship-terminal/swe/issues/681)).

- **iPhone and web: a machine strip on the Sessions page.** One chip
  per machine the hub lists, with an online dot, its name, "N working
  · M waiting" and, when it is offline, "last seen 2h". Tapping a chip
  shows only that machine's sessions; tapping it again shows all. The
  strip hides when the hub has one machine, and orders machines like
  the Mac's "Other machines" group (online first, then name). No new
  hub route
  ([#1443](https://github.com/starship-terminal/swe/issues/1443)).

- **iPhone, web and Mac: thinking cards say how long the model
  thought.** A thinking card reads "Thought for 12s" or "Thought for
  1m 5s" from the hub's `duration_s`, and plain "Thought" when the hub
  sent none. One formatting rule per client (`ThoughtLabel.text` on
  iOS and the Mac, `Fmt.thought` on the web)
  ([#1440](https://github.com/starship-terminal/swe/issues/1440)).

- **Remote approvals show and answer unnumbered menus too.** A
  tmux-mode prompt whose options are an arrow-select list ("❭ Option
  A", the shape of Devin's ask-question panel) now reaches the approval
  card as its real labels, numbered top to bottom, instead of the
  generic Allow / Always / Deny guess. Answering a row walks the pane's
  cursor with arrow keys and confirms; numbered menus keep sending
  their own digit, and Devin's select widget now parses its selected
  row ("❭" is a different glyph than Claude's "❯"). Option labels join
  the request fingerprint, so a question that swaps its choices can't
  be answered by a stale tap.

- **Approval pushes carry the menu's own option buttons.** When the
  event lists the pane's choices, the ntfy actions are those labels —
  up to the three actions a push can hold, with a longer menu's last
  slot linking to the session chat where every option answers —
  instead of a fixed Allow / Deny / Always. Prompts with no option
  list keep the three semantic buttons, and tapping the notification
  itself now opens the session's chat.

- **Hub: thinking messages carry how long the model thought.**
  `kind: "thinking"` messages from Claude Code, Codex and opencode now
  carry `duration_s`, so the clients can show "Thought for 12s". Claude
  Code and Codex time it from the record before the thinking record to
  the thinking record; opencode from its own reasoning `time` span.
  Other harnesses, and records with no usable timestamps, leave it out
  ([#1440](https://github.com/starship-terminal/swe/issues/1440)).

- **Mac: Settings › Remote can keep this Mac's agent running.** On a Mac
  paired as a satellite of another machine's hub, a "Keep this Mac's
  agent running" switch installs or removes the agent's LaunchAgent
  through `swe remote agent --install` / `--uninstall`, and says whether
  it is running. The switch is hidden on the Mac that hosts the hub and
  on a Mac that was never paired
  ([#1438](https://github.com/starship-terminal/swe/issues/1438)).

- **`swe remote agent --install` keeps a satellite running.** Like
  `swe remote serve --install`, it runs `swe remote agent` as a
  LaunchAgent on macOS (`com.cwenlong.quiver.remote-agent`) or a systemd
  user unit on Linux (`quiver-remote-agent.service`), separate from the
  hub's, so it starts at login or boot and restarts if it exits. It logs
  to `~/.quiver/data/remote/agent.log`, turns on lingering or says how,
  and refuses on a machine whose own hub is installed (and the hub's
  install refuses next to an installed agent), since the two would share
  `data/remote/cursors/`. Only one agent runs per `~/.quiver`: a second
  one exits at start, and `--install` refuses while a shell-started
  agent runs. `--uninstall` removes it and
  `--status [--json]` shows it. `install-app.sh` now restarts the agent
  on a new bundle as it does the hub
  ([#1438](https://github.com/starship-terminal/swe/issues/1438)).

- **`swe init`: the hub's MCP servers in extra accounts.** A fourth
  switch, `accounts.sync.mcp`, off by default (`swe config set
  accounts.sync.mcp true`, or the new "MCP servers" switch on the Mac
  Accounts page's Shared content card). With it on, `swe init` merges
  every server in `~/.quiver/mcp.json`, rendered as `swe mcp sync quiver`
  renders it, into each extra Claude account's `.claude.json` (the
  `mcpServers` member only) and each extra Codex account's `config.toml`
  (`[mcp_servers.<name>]` tables only). The login and every other key
  keep their bytes. A server the account already has under the same name
  is reported as a conflict and left alone, and a file that is not valid
  JSON or TOML is reported and never written. Writes go through a temp
  file and rename after a backup in `~/.quiver/backups`. Each server swe
  writes is recorded in `data/account-mcp.json`, and switching off then
  running `swe init` removes only those, and only while the account has
  not edited them. Closes
  [#888](https://github.com/starship-terminal/swe/issues/888).

- **Hub: switch the model of an opencode session attached through
  `opencode serve`.** The `model` control op on a `serve` session
  records a `provider/model` pick on the session's supervised row, and
  the next `prompt_async` carries it as `{providerID, modelID}`, so the
  switch applies from the next turn; a bare name is refused. opencode
  rows now list `serve` in `model_modes`, so the iPhone's Model menu
  shows on them with no app change. This closes model switching on
  every API channel
  ([#643](https://github.com/starship-terminal/swe/issues/643)).

- **Hub: switch the model of a headless Claude Code session.** The
  `model` control op on a `headless` session records the pick on the
  session's supervised row, and the next `claude -p --resume` run takes
  it as `--model`, so the switch applies from the next turn. Claude Code
  rows now list `headless` in `model_modes`, so the iPhone's Model menu
  shows on them with no app change
  ([#643](https://github.com/starship-terminal/swe/issues/643)).

- **Hub: switch the model of a Codex session attached through the
  app-server.** The `model` control op on an `appserver` session
  records the pick on the session's supervised row, and the next
  `turn/start` carries it as its model override, so the switch applies
  from the next turn. Codex rows now list `appserver` in `model_modes`,
  so the iPhone's Model menu shows on them with no app change
  ([#643](https://github.com/starship-terminal/swe/issues/643)).

- **Mac: Sessions lists other machines' sessions.** Below this Mac's
  Pinned and day groups, an "Other machines" group shows the hub's rows
  for each satellite: a subheader per machine with a green or grey dot
  ("<name> · offline" when it is down), then each session's title and
  "status · folder · harness · model" ("Working", "Idle 2h", or "Last
  seen 1d" for an offline machine). Tokens and Spend read "–", since the
  hub sends no usage. A click opens the chat through the hub, as on the
  iPhone, and Pin, Rename and Fork work as on this Mac's rows. The rows
  come from the `/v1/sessions` read the page already makes, so filters
  and search leave them alone, and the group hides when no other machine
  reports a session or the hub is down
  ([#970](https://github.com/starship-terminal/swe/issues/970)).

- **iPhone app: Tasks gets the scope menu and five-row groups** the Mac
  and web already have (#941, #942). A menu above the filter chips
  stands in for the Repositories rail: All tasks, then GitHub repos
  under their owner, Linear projects under "Linear" and other sources
  under their own name, each with its count on the current chip and
  "agent running" where one is on it; scopes with nothing on the chip
  fold into an "N quiet" submenu. A pick narrows the list and the chip
  counts and shows every row of that group. Otherwise each group shows
  five rows, then "Show N more" / "Show less". A failed repo's error
  sits under its own group header, and a scope the next refresh drops
  falls back to All tasks
  ([#970](https://github.com/starship-terminal/swe/issues/970)).

- **Mac: `app/mac/script/setup-machine.sh` sets up a new dev Mac.** It
  checks that full Xcode is selected (printing the `sudo xcode-select -s`
  fix when Command Line Tools are), puts the checkout on `main` at
  `origin/main` (renaming diverged branches aside with a dated name, never
  deleting one, and stopping on a dirty tree), runs `install-app.sh`,
  installs the remote hub with `swe remote serve --install`, and quits this
  checkout's dev build for the installed app. Every step can run again, and
  `--dry-run` prints the plan without changing anything. `install-app.sh`
  now runs under `caffeinate -i`, so the Mac can't idle to sleep during a
  build driven over SSH ([#1214](https://github.com/starship-terminal/swe/issues/1214)).

- **Mac app: Shared content switches on Accounts.** Once you have an
  extra Claude or Codex account, the Accounts page shows Instructions,
  Skills and Hooks switches. Each one saves `accounts.sync.<piece>` and
  runs `swe init --accounts`, which links that piece into every extra
  account's folder, or removes the links swe made when it is switched
  off. Anything an account already has in that place is named under the
  switches and left alone. A newly added account is synced at once when
  any switch is on. Part of
  [#888](https://github.com/starship-terminal/swe/issues/888).

- **Mac app: pin, rename and fork sessions.** The Sessions list still
  comes from `swe session --json` (tokens, cost, filters); when this
  Mac's hub is running, each row the hub also lists picks up the hub's
  pin, name and fork flag. Its right-click menu, and the chat header's
  More menu, offer Pin or Unpin, Rename, and Fork where the harness can
  fork. Pinned rows lead the list in a Pinned group, in the order they
  were pinned, whatever the time window. A name set on the hub replaces
  the row's title (and is searchable), and a rename made on another
  device shows in an open chat live. Rows the hub doesn't list, and
  every row while the hub is down, keep the list as it was
  ([#970](https://github.com/starship-terminal/swe/issues/970)).

- **`swe init`: shared instructions, skills and hooks in extra accounts.**
  Three switches in config.json, all off by default:
  `accounts.sync.instructions`, `accounts.sync.skills` and
  `accounts.sync.hooks` (`swe config set accounts.sync.skills true`).
  With one on, `swe init` links that piece into every extra Claude and
  Codex account home under `~/.quiver/accounts/<vendor>/<id>/home`, under
  the same name the default home uses (`CLAUDE.md` or `AGENTS.md`,
  `skills`, `hooks/<script>`). It only creates or repoints links there: a
  file, folder or link the account already has in that place is reported
  as a conflict and left alone, even with `--force`. Each link swe makes
  is recorded in `data/account-links.json`, and switching a piece off
  then running `swe init` removes only those links. `swe init --check`
  previews both directions and `--json` adds `account_sync` and
  `accounts`. `swe init --accounts` runs only this pass (with `--check`
  and `--json`), never touching the default harness homes. Part of
  [#888](https://github.com/starship-terminal/swe/issues/888).

- **Remote hub: optional owner auto-approve for pair requests.** Off by
  default. `swe remote devices --auto-approve=on|off` (or the new switch
  in the Mac app's Settings › Remote › Pending requests) saves it in
  `data/remote/pairing.json`, and `--requests` shows it. When on, a pair
  request is approved without a code only if the hub knows its owner's
  Tailscale login, the requesting node is untagged with exactly that
  login and a node id other than the hub machine's own, the owner hasn't revoked that node (its sign-in
  or its newest paired device), and the request has a JSON body and no
  foreign `Origin`. Anything that can't be determined leaves it pending. The
  `201` answer then carries `auto_approved: true` and the phone's first
  poll collects its credential. Every approval, by code over HTTP or
  automatic, writes one `pair:` line to the hub's log, and device rows
  record `approved_via` (`code` or `owner_auto`). Part of
  [#522](https://github.com/starship-terminal/swe/issues/522).

- **Remote hub: Notion databases as a task source.** A `notion` section
  in `config.json` names an integration token by `${NAME}` reference,
  the user whose rows to list (a user id or sign-in email) and each
  database with its property mapping (`status` and `owner` required,
  `title` and `url` optional). `GET /v1/tasks` then lists those rows as
  `{source: "notion", kind: "page"}` tasks beside GitHub and Linear,
  with `status` in the database's own words and a best-effort `state`
  from a status group, select option or checkbox. A mapping that
  doesn't fit the database answers `bad-config` naming the property,
  while other databases and sources keep listing. The adapter pins
  `Notion-Version: 2022-06-28`, paginates with a cap, waits out a 429's
  `Retry-After`, and renders a page body to markdown for a resolve
  prompt. Read-only. Part of
  [#847](https://github.com/starship-terminal/swe/issues/847).

- **Tasks on the Mac, the web and the iPhone show Notion like Linear.**
  The three clients read the hub's new `notion` env block beside
  `linear`: a configured Notion source that fails (`auth-required`,
  `bad-config`, `upstream-error`) gets its own banner or card with the
  hub's reason, and its stored rows read as last known; `disabled`
  shows nothing. Notion pages list under their database in the rail,
  their detail reads "Notion · <database>", and the link-out says
  "Open in Notion". Link and unlink work as for Linear; starting an
  agent stays GitHub-only. Part of
  [#847](https://github.com/starship-terminal/swe/issues/847).
- **Remote iPhone app: photo and file attachments in the chat
  composer.** A paperclip left of the prompt box offers Photos and
  Files. Each pick uploads at once to the hub's
  `POST /v1/sessions/{key}/attachments` and shows as a chip above the
  box with its name and size, or its upload progress, and an x to drop
  it; a failed upload stays red until you tap it to retry or remove it.
  The hub's caps (20 MB, images, PDFs and text files) are checked on the
  phone first, so a refused file gets a plain note instead of an upload.
  Photos, and HEIC or TIFF files from Files, go up as JPEG with the long
  edge at most 2048 px, since models can't read HEIC. Send waits while
  any upload is in flight and needs typed text (the hub refuses a
  message without it); the message carries the refs, the bubble names
  the files, and the hub's `@path` echo shows as the same names. A send
  held in the queued tray keeps its files (the row shows a paperclip
  count), and a retry or an edit on an older hub resends the same refs.
  Detaching clears the chips, since the hub drops the staged files.
  Part of [#516](https://github.com/starship-terminal/swe/issues/516).
- **Web app: Setup can start from your hub repo.** While `~/.quiver` on
  the hub machine has no hub yet, step 2 shows "Start from your hub
  repo": a URL field, the note "Fork a hub repo first (or use it as a
  template for a private copy), then paste your fork's URL.", and Clone.
  `POST /v1/app/setup/hub` runs `swe setup --hub`'s clone on the hub's
  own thread and answers at once; the page polls `GET
  /v1/app/setup?hub=1` until it ends, then reads the machine again so
  the step shows the cloned hub. An existing hub is a 409 `has_hub`,
  a clone or setup already running a 409 `busy`, and a URL git can't
  reach or a timeout shows under the field. The URL is only ever kept
  or shown without its credentials
  ([#696](https://github.com/starship-terminal/swe/issues/696)).

- **Web app: photo and file attachments in the chat composer.** A
  paperclip button, dropping files on the composer, or pasting a
  screenshot adds files to the next message. Each uploads at once
  through the hub's `POST /v1/sessions/{key}/attachments` route and
  shows as a chip with its name and size (or "Uploading…", or why it
  was refused) and a remove x. The hub's caps are checked before
  uploading, so a file over 20 MB or of a type the hub won't stage
  (anything but images, PDFs and text files) is refused in plain words
  and never sent. Send waits while any upload is in flight, and the
  message carries the uploaded files' refs in `attachments`, which the
  hub turns into `@path` for the harness. A message still needs text
  (the hub requires it); Enter on an empty box with files says so
  ([#516](https://github.com/starship-terminal/swe/issues/516)).

- **Mac app: the session's model in the chat header and Sessions
  list.** A session's chat header reads `harness · folder · model`, and
  each Sessions row names the model of the newest reply first in its
  details line, ahead of the ledger's other models (the ledger's own
  spelling of the same model isn't repeated). The provider path and the
  8-digit date stamp are dropped, as on the phone and the web app:
  `anthropic/claude-opus-4-5` and `claude-opus-4-5-20251101` both read
  `claude-opus-4-5`. The app reads it from this Mac's hub rows
  (`model`, optional), on the same read that tells which rows open a
  chat, and the open chat reads its row again when a turn ends (twice
  more, six seconds apart, while the hub's catalog still holds the old
  model), so the header follows a model that first appears with the opening reply or
  changes elsewhere. Nothing shows when the hub doesn't know it. Display
  only: the Mac doesn't switch models. Part of
  [#643](https://github.com/starship-terminal/swe/issues/643).


- **iOS app: comment on an issue, and post your Resolve context as a
  comment.** The issue page has the Mac's comment box under the thread,
  and the Resolve sheet gains "Your context (optional)", now sent with
  the resolve, and "Also post as a comment on #<n>", on by default.
  Both post through the hub's `gh` login with `POST /v1/issues/comment`.
  A context comment that fails is a warning; the launch still goes
  ahead. Part of [#970](https://github.com/starship-terminal/swe/issues/970).
- **Web app: the Cursor app on the Calendar.** The hub reads each Cursor
  account's `state.vscdb` (the app's own folder and every extra Cursor
  profile's) as the Mac app does, so editor chats show as blocks with
  time and responses, one legend row per account. Devin cloud stays
  Mac-only, since its sessions live in the Mac app's own store. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).
- **Web app: comment on an issue, and post your Resolve context as a
  comment.** The issue sheet in Repositories now has the Mac's comment
  box under the thread (Cmd or Ctrl+Return posts), and the Resolve
  panel has "Also post as a comment on #<n>", ticked by default. Both
  post through the hub's `gh` login with `POST /v1/issues/comment`. A
  context comment that fails is a warning; the launch still goes ahead.
  Part of [#970](https://github.com/starship-terminal/swe/issues/970).
- **Web app: Freebuff on the Calendar.** The hub reads Freebuff's
  `run-state.json` chats under `~/.config/manicode/projects` as the Mac
  app does, so its work shows as blocks with time and responses. Part
  of [#970](https://github.com/starship-terminal/swe/issues/970).
- **Web app: Pi and Factory Droid on the Calendar.** The hub now reads
  their session files the way the Mac app does (`~/.pi/agent/sessions`
  and `~/.factory/sessions`), so their work shows as blocks beside the
  ledger harnesses, with time and responses. Accidental sessions stay
  off, judged on the ledger's history as on the Mac. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).
- **Hub: `POST /v1/issues/comment` posts an issue comment.** The body
  `{repo, number, body}` goes through the hub's own `gh` login, the way
  the Mac's composer posts through `swe gh issue comment`, with the text
  on `gh`'s stdin rather than its command line. Only a paired phone or
  the owner's device can call it, only for a pinned repo, and only up
  to GitHub's 65,536 characters. The web and iPhone issue detail build
  their comment composer and Resolve's "Also post as a comment" on it.
  Part of [#970](https://github.com/starship-terminal/swe/issues/970).

- **`swe mcp gateway`: one copy of each shared MCP server for every
  session.** Each harness session started its own copy of every stdio
  server: with 17 servers, one Claude Code session held 26 processes and
  about 1.3 GB. `swe mcp gateway share <server...>` marks hub servers
  that keep no per-session state, `swe mcp gateway install` runs a
  LaunchAgent that starts each of them once and serves it on
  `http://127.0.0.1:8478/mcp/<server>`, and `swe mcp sync quiver ...`
  then writes that URL instead of the command. Measured with 18 shared
  servers and playwright left per session: one session's MCP processes
  went from 26 (1,375 MB) to 1 (63 to 85 MB), and the gateway held the
  shared servers once (25 processes, about 1.3 GB) with one session or
  four. Browser and REPL servers are refused without `--force`; Claude
  Desktop keeps the command, since it takes no URL server.

- **Web app: a tray for queued messages in the chat.** A Queue-mode
  send made while the agent works now waits in a tray above the
  composer, as on the iPhone, instead of a "Queued until the agent is
  idle" bubble. Each row can be edited (its text goes into the box and
  Send becomes Save), removed, or sent now into the running turn;
  Remove all asks first. The hub holds the sends, so the tray comes
  back when the chat reopens, with sends queued, edited or removed on
  another device ([#970](https://github.com/starship-terminal/swe/issues/970)).
- **Web app: issue detail and Resolve from Repositories.** An issue row
  now opens a sheet with the issue's body and comments (read only, from
  the hub's `GET /v1/issues/<owner>/<repo>/<n>`), Open on GitHub, and
  Resolve with agent: harness, model, your context and the first prompt,
  posted to `POST /v1/issues/resolve`, then the chat opens. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **iOS app: the overview's top models, quick figures, heatmap and
  daily table, over all seven periods.** The Usage tab's Spend half
  now offers This month, 90 days, 365 days and All time beside Today,
  7 and 30 days, in a chip row that scrolls sideways. Under the
  harness split it shows the Mac's active days, tokens a day and
  busiest day, the top five models with the harnesses that ran them
  and their share, an activity heatmap with as many recent weeks as
  the phone fits (tap a day for its tokens and cost), and the daily
  table (date, total, input, output, cached, spend), newest ten days
  first, then 30 more a tap. All of it comes from the `GET /v1/app/overview`
  payload the tab already read. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **iOS app: answer an approval with the pane's own menu rows.** When
  the hub reads the agent's numbered menu ("1. Approve once", "2.
  Always allow in this project", ...), the chat's approval card lists
  those rows as its answers, as the web app does, so a row the Allow,
  Always and Deny buttons can't name is still reachable. The row's
  number goes to the hub as the decision, and the answered card names
  the row it picked. Approvals without menu rows (older hubs,
  non-tmux sessions) keep Allow, Always and Deny. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **Web app: pin, rename and fork sessions.** Each Sessions row has a
  More actions button (and a right-click menu) with the iPhone's
  swipe-left actions: Pin or Unpin, Rename, and Fork where the harness
  can fork. Pinned rows lead the list in a Pinned group, in the order
  they were pinned, whatever the time filter. Rename follows the hub's
  rules (one line, 120 characters) and offers the harness's own name
  back; Fork takes a first message and opens the new chat once the hub
  names it. The chat's header has the same menu, and a rename made on
  another device shows there live
  ([#970](https://github.com/starship-terminal/swe/issues/970)).
- **Mac app: Needs you on the Inbox page.** Above the GitHub
  notifications, the Inbox now lists the sessions this Mac's hub says
  wait on you, broke or finished today (`GET /v1/inbox?done=1`), in the
  iPhone's sections: Waiting on you, Errors and Finished today. A row
  opens that session's chat; Clear, per row or per section, hides it
  for this Mac until the session moves again (`POST /v1/inbox/dismiss`),
  and never offers a pending approval. Nothing extra shows while the
  hub isn't running
  ([#970](https://github.com/starship-terminal/swe/issues/970)).

- **iOS app: merge, auto-merge and re-run on the pull request
  page.** The detail page has the Mac and web merge controls: a
  strategy menu (Squash and merge, Rebase and merge, Create a merge
  commit), Merge and Auto-merge. Both ask first, naming the PR, the
  base branch and the strategy, as the web's inline confirm does. Why a
  button is off ("Draft PRs can't merge", "Resolve the conflicts
  first", "Auto-merge is already armed") shows under the buttons, and
  an armed auto-merge shows as a badge. A check row from a GitHub
  Actions run has a menu with Re-run failed jobs (when a check in that
  run failed) and Re-run all jobs. The phone calls the web app's
  routes the hub already serves to paired devices,
  `POST /v1/app/repositories/merge` and `…/rerun`. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **Web app: Mine and Review requested pull request filters.** The
  Repositories dashboard's Pull requests tab now offers All, Mine and
  Review requested with counts, as the iOS app does, from the flags the
  hub's `GET /v1/pulls` puts on each open PR. A filter hides while the
  hub can't say (a failed `gh pr status`) instead of guessing. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **Web app: Needs you in the Inbox.** The Inbox page now opens with
  the iOS Inbox's Waiting on you, Errors and Finished today sections
  from the hub's `GET /v1/inbox`, above the GitHub notifications. A row
  opens its session's chat, and Clear (a row or a whole section) posts
  `POST /v1/inbox/dismiss`; a pending approval can't be cleared. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **Remote: a `limit` notification when a session hits its usage
  limit.** The hub and satellites now raise `notify` kind `limit` when
  a Claude Code session writes its rate-limit record ("You've hit your
  session limit · resets 9:30am") or a Codex turn ends with
  `usage_limit_exceeded`. The ntfy push carries the harness's own line,
  so the lock screen says when work can resume, and Claude Code's carry
  `resets_at` and `window`. It rings once per limit: old records read
  during backfill and retries into the same limit stay quiet. Other
  harnesses don't raise it yet
  ([#516](https://github.com/starship-terminal/swe/issues/516)).

- **Web app: the session's model in the chat header and Sessions
  list.** The chat's header shows the model of the transcript's newest
  reply after the folder, and each Sessions row names it first in its
  details line, ahead of the ledger's other models (the ledger's own
  spelling of the same model isn't repeated). The provider path and the
  8-digit date stamp are dropped, as on the phone:
  `anthropic/claude-opus-4-5` and `claude-opus-4-5-20251101` both read
  `claude-opus-4-5`. Nothing shows when the hub doesn't know it. An
  open chat reads the hub's row again when a turn ends (twice more, six
  seconds apart, while the hub's catalog still holds the old model) and
  when the tab becomes visible, so the header follows a model that first
  appears with the opening reply or changes elsewhere. The
  page's rows (`/v1/app/sessions`) now carry the hub's `model` as the
  catalog read it, so the page has no second transcript reader. Display
  only: the web app doesn't switch models. Part of
  [#643](https://github.com/starship-terminal/swe/issues/643).

- **Mac app: approve pair requests in Settings › Remote.** A "Pending
  requests" group under Paired devices lists the phones asking to pair
  over Tailscale, with each one's name, tailnet node, login and how long
  it has waited, a code field for the 6 digits the phone shows, and
  Approve / Deny. It reads `swe remote devices --requests --json` every
  3 seconds, only while the section is on screen, and an approval
  refreshes the device list. This covers a hub on the same Mac; a hub on
  another machine is [#681](https://github.com/starship-terminal/swe/issues/681).
  `swe remote devices --approve=<code>` and `--deny=<code>` gain
  `--request-id=<id>`, which holds the code to that one request, as the
  hub's HTTP route already did: a code typed against one row never
  decides another, and five wrong approve codes deny it. The app always
  sends it.
  Part of [#522](https://github.com/starship-terminal/swe/issues/522).

- **Web app: `@` file completion in the chat composer.** Typing `@` at
  the start of the box or after a space opens the composer's menu with
  files and folders under the session's folder, from the hub's
  `GET /v1/sessions/{key}/files` route, the same list the iPhone's `@`
  menu shows. It uses the `/` menu's box and keys (arrows, Enter or Tab
  to pick, Escape to close) and the iPhone's token rules: an `@` inside
  a word (an email) opens nothing, a path with spaces is quoted
  (`@"My Documents/notes.txt"`), a file goes in as `@path ` and a
  folder as `@path/` so the menu stays open inside it. Searches wait
  120 ms after the last keystroke, and a newer query drops an older
  answer. A satellite session's refusal and an older hub's 404 are
  worded in the menu
  ([#516](https://github.com/starship-terminal/swe/issues/516)).

- **Compute: auto clean orphaned processes, on the Mac and the web app.**
  The Compute settings gain an "Auto clean" switch (Mac: the cleanup
  sheet, web: the settings gear): while it is on, an orphan tree older
  than the "Orphans count as old after" age gets one SIGTERM, deepest
  member first, through the same kill plan and guards a manual Quit
  runs — never pid 1, the app or its ancestors, and a pid only while it
  is still the recorded process. A nested "Force quit" switch shows only
  while auto clean is on and sends SIGKILL to a marked tree still alive
  ten seconds later. On the web, the switches live in the hub's
  `config.json` (the `compute` section, so `swe config` reads them too),
  a `POST /v1/app/compute/settings` route writes them, and the background
  sampler starts at hub boot while the switch is on, so cleanup runs
  with no browser open. A signalled orphan shows a "Quitting" badge.

- **Mac app: the setup sheet can start from your hub repo.** While
  `~/.quiver` has no hub yet (no `AGENTS.md`, no `.git`; the app's own
  ledger and caches there don't count), the links step shows "Start
  from your hub repo": a URL field, the note "Fork a hub repo first (or
  use it as a template for a private copy), then paste your fork's
  URL.", the demo hub link once it exists, and Clone. Clone runs `swe
  setup --hub=<url> --json --non-interactive` and takes the preview it
  answers with, so the step then shows the cloned hub's AGENTS.md and
  Set up sends no adopt. While it runs the sheet's other buttons wait;
  swe's refusal (a hub already there, a URL git can't reach, a timeout)
  shows under the field, and the card names any plugin marketplace
  that couldn't be registered
  ([#696](https://github.com/starship-terminal/swe/issues/696)).

- **Remote iPhone app: switch a session's model from the chat.** On a
  session attached through tmux whose harness takes `/model <id>`
  (Claude Code and Codex; the row's `model_modes` says so), the chat's
  menu gains a Model submenu: the machine's recent models for that
  harness, newest first, the running one checked, then Other… for a
  typed id. A pick sends the `model` control op, which types
  `/model <id>` into the pane. A typed id must be one token of letters,
  digits and `._:/+@-`, so it can't carry a second command. The header
  shows the request as "→ id" until the transcript catches up: it
  settles when the row's model changes, or once a reply has come in and
  the list has refreshed after the hub's next read, so a switch the
  harness refused shows the model that actually ran. Elsewhere (watching
  only, headless, app-server, opencode) the submenu is hidden. Part of
  [#643](https://github.com/starship-terminal/swe/issues/643).

- **Web app: Preview on tool cards that write a page, and link cards
  under replies.** In a session's chat, a tool call that writes or
  edits an `.html`, `.htm` or `.svg` file inside the session's folder
  (Write, Edit, MultiEdit, a Codex patch and the like, the iPhone's
  rules) gets a Preview button. It fetches the file as it is on disk
  now from the hub's preview route and draws it in a sheet, inside an
  `<iframe sandbox>` with no permissions at all (no scripts, forms,
  popups or navigation out, and an opaque origin) and a
  `default-src 'none'` policy at the top of the page, so nothing loads
  from the network. That frame sits inside a second locked frame of the
  app's own, whose policy also stops the page navigating itself (a
  `target="_self"` link). An SVG is drawn as an image. Source shows the
  file's text, Reload fetches it again, and a refused file says why in
  plain words. A reply that links to web pages gets a card per page
  (host, path, Open in a new tab), up to three. File names with `+` are
  sent as `%2B`
  ([#634](https://github.com/starship-terminal/swe/issues/634)).

- **Remote iPhone app: the chat header and the session list show the
  model.** A session's chat header adds the model after its folder, and
  each session row adds it after the last-active time, both from the
  hub row's `model` (the newest reply's model). The provider path and a
  date stamp are dropped, so `anthropic/claude-opus-4-5` and
  `claude-opus-4-5-20251101` both read `claude-opus-4-5`. Nothing shows
  when the hub doesn't know the model or is older than the field. Part
  of [#643](https://github.com/starship-terminal/swe/issues/643).

- **Remote hub: session rows say which model each session runs.**
  `/v1/sessions` rows, the hub's own and a satellite's, carry `model`,
  the model of the newest reply in the session's transcript (left out
  when unknown), and `model_modes`, the supervised modes in which the
  `model` control op can switch it (`["tmux"]` for Claude Code and
  Codex, `[]` elsewhere). Claude Code, Codex and Pi transcripts are read
  from the tail and cached per file in
  `cache/session_model_cache.json`, so an unchanged transcript costs a
  `stat` per refresh; OpenCode, Kilo and Devin ask their database once
  per session whenever its activity time moves. Junk names (Claude's
  `<synthetic>`, Devin's `compactor`) are skipped for the reply before.
  Part of [#643](https://github.com/starship-terminal/swe/issues/643).

- **`swe setup` asks for your hub repo.** While `~/.quiver` is missing or
  empty, the wizard's first step asks for your hub repo's URL, beside
  the note "Fork a hub repo first (or use it as a template for a
  private copy), then paste your fork's URL." A blank answer starts an
  empty hub as before; a URL that can't be cloned says why and asks
  again. `swe setup --hub=<git url>` (or `--hub <git url>`) clones it
  first in every mode, so `--apply`, `--json` and the wizard then run on
  your hub. It is the same clone as `swe init --hub`: checked with `git
  ls-remote`, never over an existing `~/.quiver`, no token in any
  output, and the hub's plugin marketplaces registered afterwards.
  Under `--json` a refusal goes to stderr and exits 1, and the document
  gains `hub: {can_clone, demo_url, cloned}`. The demo hub link is one
  constant, `DEMO_HUB_URL` in `init/hub.py`, shown only once it is set
  ([#696](https://github.com/starship-terminal/swe/issues/696)).

- **Remote iPhone app: a Model menu on every launch sheet.** New
  session, Resolve issue, Open PR in session and a task's New agent
  session replace the free-text Model field with a menu: Harness
  default (sends no model), the machine's recent models for the
  harness newest first, and Other, which opens a field for a typed
  model id. It starts on the newest recent model. A harness or machine
  switch keeps a picked Harness default or Other, keeps a picked
  recent model only when the new harness lists it, and otherwise
  moves to the new harness's newest. A hub older than `recent_models`
  offers its `last_models` entry alone. Part of
  [#914](https://github.com/starship-terminal/swe/issues/914).

- **Web app: the launch sheets' Model box is now a menu.** New session,
  a task's New agent session and a PR's Open in session list "Harness
  default" (sends no model), the harness's recent models newest first
  (the hub's `recent_models`), and "Other…", which opens a box for a
  typed model id. The menu opens on the newest recent model, as the box
  was prefilled before. A pick of your own survives a harness or
  machine switch, except a recent model the new harness never used,
  which re-defaults so a sheet never sends another harness's model. An
  older hub that sends only `last_models` gives a one-item list. Part of
  [#914](https://github.com/starship-terminal/swe/issues/914), the same menu
  as the Mac app's below.

- **Mac app: the launch sheets pick a model from a menu.** New session,
  Resolve issue and a task's New agent session swap the free-text
  Model field for a menu: "Harness default" (no `model` sent), the
  harness's recent models newest first from `recent_models`, then
  "Other…", which opens a field for a typed id. It starts on the newest
  model, as the field's prefill did. A picked "Harness default" or
  typed id survives a harness or machine switch; a picked model the new
  harness hasn't run re-defaults instead of travelling to it. A hub
  without `recent_models` offers its `last_models` entry alone. Part of
  [#914](https://github.com/starship-terminal/swe/issues/914).

- **Remote iPhone app: Preview on tool cards that write a page.** When
  a tool call writes or edits an `.html`, `.htm` or `.svg` file inside
  the session's folder (Write, Edit, MultiEdit, a Codex patch and the
  like), its card gets a Preview button. Preview asks the hub for the
  file as it is on disk now and draws it in a sheet with scripts,
  network and navigation off; an SVG is drawn as an image. The card's
  raw content is still one tap away, and a missing, too-big or
  unsupported file says so in plain words
  ([#634](https://github.com/starship-terminal/swe/issues/634)).

- **Syntax highlighting in the chat, on the web app, the remote web
  client and the Mac app.** Fenced code blocks, tool arguments and tool
  output are tokenised and coloured: a shell command keeps its flags
  and variables apart, JSON keys differ from strings, and Python,
  JavaScript, CSS, HTML, YAML, TOML, SQL, Go, Rust, Swift and friends
  get their keywords and literals. A Bash call's `command` shows as the
  command itself instead of its JSON wrapper, a Read's output colours
  as the file it came from, and diff rows keep their added/removed
  tints with the file's own highlighting inside. The Mac app's tool
  cards also gain the diff view iOS and the web already had, with
  added/removed counts in the collapsed row.

- **Remote launches start on a picked model, with each harness's recent
  models to pick from.** `POST /v1/sessions`, `POST /v1/issues/resolve`
  and `POST /v1/pulls/<o>/<r>/<n>/open` put an optional `model` on the
  harness's command line, as a new `capabilities.model` entry in
  harness.json declares (claude, cursor, pi and devin `--model`, codex
  and cline `-m`, droid a per-launch `--settings` file, opencode
  `opencode mini -m provider/model`). A harness with no such entry
  answers `409 unsupported_launch_option` instead of starting on its
  default. Machine rows and the web app's `?harnesses=1` lists gain
  `recent_models`: up to five recently used models per launchable
  harness, newest first, read from the usage ledger without internal
  names like Devin's `compactor` or Cursor's `default`, and OpenCode
  names completed to `provider/model`. Part of
  [#914](https://github.com/starship-terminal/swe/issues/914); the client
  pickers come next.

- **A "Fan off temperature" for the Compute fan rule.** The popover's
  first field is now "Fan on temperature", and a second field under it
  picks where the held fans drop back to macOS. Until a temperature is
  picked it follows the line at five degrees under, as before; a pick
  is remembered in `fanReleaseBelowCelsius`, stays at least a degree
  under the line, and moves down with it. A CPU that hovers at the
  line no longer flaps the fans on and off. The menu bar panel's fan
  popover gains the same field as a "Fan off below" dropdown.

- **The web app's chat shows the tmux pane and can detach.** A session
  SWE runs under tmux shows its `qv-…` name in the chat's top bar;
  a click copies `tmux attach -t qv-…`. The bar's Detach button offers
  the phone's three choices: detach when the turn finishes, keep it
  running in tmux, or detach now. A kept session says it is still
  running and offers Reattach, which takes the live pane back, mid-turn
  included. `/v1/sessions` rows carry the supervised `mode`, the `tmux`
  name and `kept`, and so do `session.ownership` events. The details
  sheet adds the pane, its attach command and, on a macOS hub, Run in
  terminal ([#850](https://github.com/starship-terminal/swe/pull/850)).

- **`swe init --hub=<git url>` brings your hub onto a new machine.** It
  checks the URL with `git ls-remote`, clones it as `~/.quiver` with your
  URL as `origin`, then links it into every harness as a plain `swe init`
  does. It only clones into a missing or empty `~/.quiver`; an existing
  one is left alone and plain `swe init` repairs it instead. A token in
  the URL never reaches the terminal. Each marketplace under the hub's
  `plugins/` is then registered with Claude Code and Codex through their
  own `plugin marketplace add`, the same fix `swe find plugins` offers.
  That fix now covers a hub marketplace whose plugins all come from
  GitHub or a URL, which drift used to skip
  ([#696](https://github.com/starship-terminal/swe/issues/696)).

- **Hub route for previewing a page file.** `GET
  /v1/sessions/{key}/preview?path=` returns one `.html`, `.htm` or `.svg`
  file from a session's working folder as it is on disk now, for the
  phone's coming Preview button on tool cards that write a page. It
  reuses the `@` file search's folder confinement (no absolute paths,
  no `..`, no symlink out, opened without following links), reads only
  regular files up to 2 MB, needs a paired device, and answers with a
  `Content-Security-Policy` that blocks scripts and network.

- **Slash commands in the web app's chat.** Typing `/` at the start of a
  session's prompt box lists the commands that session's harness runs —
  the phone app's "/" menu on the web: built-ins plus user, project,
  skill and plugin commands from `GET /v1/sessions/{key}/commands`, with
  each command's argument hint, description and source tag. Arrows
  highlight, Enter or Tab inserts `/name ` (or the command's own insert
  text, like a Codex skill's `$name`), and the harness expands the
  command when the message arrives, whatever the session's channel.

- **Slash-command menu in the Mac app's session chat.** Typing `/` at
  the start of the prompt box opens the commands this session's harness
  will run — built-ins, user and project commands, skills and plugin
  commands as the hub lists them for the session — filtered as you type.
  Arrows move the highlight, Return or Tab inserts the command (an
  existing space is reused and `$`-style insert text is honoured),
  Escape closes the menu, and the command goes out as a normal message
  the harness expands on arrival, like the iPhone app's menu and the
  remote PWA's.

- **Report a bug from the app or the terminal.** The Mac app's Help menu
  now has Report a Bug, Request a Feature, Copy Diagnostics and SWE
  Documentation, and Settings › General has a Feedback card under Doctor
  with the same actions. `swe bug` (`--feature`, `--print`, `--json`)
  opens the same forms. Each link fills in the Area, version and OS
  fields; the diagnostics carry the versions and Doctor's findings with
  the home folder written as `~` and email addresses masked. The issue
  forms now cover every client with one Area question, add
  `needs-triage`, and a workflow labels each issue `mac`, `cli`, `ios`,
  `web` or `documentation` from its answer.

- **Account emails on every rate-limit card.** `swe list --usage --json`
  and `swe harness usage` now carry an `email` field where the sign-in
  names one: Claude's `~/.claude.json`, Codex's `id_token` claims,
  Cursor's `cursorAuth/cachedEmail` in its state db, and Devin's
  `userStatus`. The Mac app's Accounts page shows it on the Cursor,
  Devin and other harness cards, like it already did for Claude and
  Codex, and keeps showing it on a re-login card so a signed-out
  account still says who it is.

- **Resolve a GitHub issue with an agent (hub side).** `POST
  /v1/issues/resolve {repo, number, harness, prompt?, context?}` makes
  branch `quiver/issue-<n>` off the pinned repo's default branch in a
  worktree under `~/.quiver/worktrees`, starts the harness there with a
  prompt built from the issue and its comments, and names the session
  `<owner>/<repo>#<n> <title>` once it is adopted. `GET
  /v1/launches/<id>` shows each step in `note` while it runs. Asking
  again for the same issue resumes in the same checkout.
- **`swe gh issue view|comment|worktree`.** `view` shows one issue with
  its comments and the `suggested_prompt` the hub would start with;
  `comment --body=TEXT` posts a comment as you (the text reaches `gh` on
  stdin, so any text is safe); `worktree` makes or reuses the
  `quiver/issue-<n>` checkout. `swe gh issue` and `swe gh issue list`
  list issues as before.
- **Landing page for the docs site.** The site root is now a
  hand-written front page (`landing/index.html`) that says what quiver
  is, the three ways in (terminal, Mac app, phone) and how to install
  it. `scripts/docs_site.py` copies it to the site root, and the README
  page moved from `/` to `/overview/`.
- **The landing page shows the product.** It follows Orca's layout: a
  tabbed hero tour, 15 feature cards that each play a short looping clip
  of the Mac app or the CLI while on screen (click one to watch it
  larger), a phone section with the iOS remote, and an FAQ. The clips
  live in `landing/media/`, and `scripts/docs_site.py` now copies
  `landing/` recursively.
- **"Download for Mac" on the landing page.** The install buttons read the
  latest GitHub release and, when it carries a `.dmg`, link straight to it
  with the Apple mark. Until a release has one they keep pointing at the
  install commands.
- **Pixel logos drift behind the landing page hero.** Every harness and
  inference provider from Settings › Providers (68 marks from the app's
  icon set) floats up as pixel art. It pauses off screen, thins out on
  phones and stays still under reduced motion.

- **Mac app: editable Service status.** Every card on the Service status
  page can be edited or removed, not just the pages the user added: an
  edit to a built-in is kept in preferences as an override (name, page
  address, feed, group) and a remove hides it, beside the custom pages
  under `customStatusServices`. The catalogue sheet now lists the
  built-ins too, so a removed or edited page is tracked again — and an
  edited one reset — from the same one-click list, and the Check probe
  also tries `/api/compat/summary.json`, the path Cursor's new host
  serves.

- **Instruction chapters:** `swe instructions build`, `check` and `watch`
  compile the ordered Markdown paths in `~/.quiver/AGENTS_MD/index.txt`
  into the complete shared `~/.quiver/AGENTS.md`. Builds replace the file
  atomically and skip unchanged content; check reports drift without
  writing; watch rebuilds after edits and keeps running after source errors.

- **Mac app: the `@` file menu in the session chat.** Typing `@` in the
  chat's prompt box lists files under the session's folder, as the web
  app and the iPhone app do: the arrows move the highlight, Return or
  Tab picks, Escape closes. A file goes in as `@path `, a folder as
  `@path/` with the menu still open to pick inside it, and a path with
  spaces is quoted. Sessions on a satellite say file search isn't
  available there. Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

- **Mac app: Mine and Review requested pull request filters.** The
  Repositories dashboard's and a repo focus page's Pull requests tab
  now offer All, Mine and Review requested with counts, as the iOS and
  web apps do. Mine compares the `gh` login with each PR's author, so
  it covers the focus page's merged and closed PRs too; Review
  requested reads `swe gh pr status` per repo. A filter hides while
  its flag is unknown instead of guessing
  ([#970](https://github.com/starship-terminal/swe/issues/970)).

- **Mac app: the Queued tray in the session chat.** A Queue-mode
  send the hub holds until the agent's turn ends now waits in a tray
  above the prompt box instead of the transcript, as on the web and the
  iPhone: Edit puts its text in the box and Save writes it back in
  place, Remove drops it, Send now steers it into the running turn, and
  Remove all clears the tray after a confirm. It moves to the
  transcript once it goes out, and reopening the chat shows what the
  hub still holds (`GET`/`POST /v1/sessions/{key}/queue`). Part of
  [#970](https://github.com/starship-terminal/swe/issues/970).

### Changed

- **The Mac app redraws less for hovers, chat rows, skeletons and the
  closed menu bar panel.** A mouse move over a Compute chart now
  redraws only its rule and readout, not every line on every stacked
  chart. Chat rows keep their parsed Markdown, styled spans and
  highlighted code by text, so a redraw no longer parses again (about
  2 ms per 6.5 KB reply each time). A skeleton list sweeps one band
  over all its bars instead of one animation per bar, and the sweep
  stops after a minute so a stuck load doesn't animate forever. The
  menu bar panel's tabs unmount while the panel is closed rather than
  redraw behind it for the app's whole life. Closes
  [#1543](https://github.com/starship-terminal/swe/issues/1543).

- **Web Compute, Sessions search and Overview do less DOM work.**
  Compute rebuilt its totals, trend card, harness cards and chart with
  `innerHTML` on every 3 s sample. It now patches them in place: only the
  figures, attributes and line paths that changed are written, and a
  part whose markup is unchanged is skipped, so the nodes, the chart and
  its hover bubble stay put. The Sessions search built every row's
  lower-cased details again on each keystroke and redrew the list on
  each one. Each row's text is now built once, and the list redraws 80
  ms after typing pauses. Overview redraws its charts at most once per
  frame while the window is resized.
  Closes [#1557](https://github.com/starship-terminal/swe/issues/1557).

- **Web app tabs in the background stop polling the hub.** Settings ›
  Integrations relisted pair requests every 3 s (each relist costs the
  hub about five file reads and a JSON build), and "Pair with Tailscale"
  asked for the owner's answer every 2 s, both while the tab was
  hidden. Now a hidden tab asks nothing and asks once the moment it is
  shown again. A hidden chat keeps its stream and every event but draws
  nothing until it is shown, and the pairing countdown and Calendar's
  now-line stop ticking while hidden.
  Closes [#1554](https://github.com/starship-terminal/swe/issues/1554).

- **The web chat reads its one session row, not the whole catalog.**
  The chat read `GET /v1/sessions` (every row, read-state and machines)
  to find its own row on open, on resync, at every turn's end (up to
  three times, 6 s apart) and on every return to the tab. The hub now
  answers `GET /v1/sessions?key=<key>` with that row and its machine:
  0.4 ms and under 1 KB against 34 ms and 1.6 MB for a 2,000-row
  catalog. A return to the tab reads once, unless a turn's re-reads
  are still running.
  Closes [#1556](https://github.com/starship-terminal/swe/issues/1556).

- **Three small rendering costs in the Mac app are gone.** GitHub's
  octicons parsed their SVG path data on every layout; each glyph now
  parses once. An open Calendar hover card polled the pointer every
  50 ms; it polls every 100 ms, so a card closes at most 300 ms after
  the pointer leaves instead of 250. A sheet's shadow was worked out from its whole content on each
  frame of the open animation; it now comes from one rounded shape
  behind the card. Closes
  [#1545](https://github.com/starship-terminal/swe/issues/1545).

- **The Mac app's background polls slow down while nobody can see
  them.** The GitHub inbox poll ran `swe gh inbox` every minute for the
  app's whole life. It now keeps the minute only while the main window
  is on screen, polls every 3 minutes behind it while GitHub banners are
  on, and every 15 minutes with banners off. The window coming back
  wakes it at once. The Repositories and repo pages, the Inbox's hub
  cards, Remote pairing requests and the remote setup sheet skip their
  `swe` runs while the window is minimised, hidden or covered. A hub
  that doesn't answer is retried after 1, 2, 4 and then 5 minutes,
  where before it was every minute. The accounts timer reads the cached
  login `PATH` rather than starting `zsh -il` (the whole rc chain) every
  5 minutes. Closes [#1541](https://github.com/starship-terminal/swe/issues/1541).

- **The hub reads `harness.json` once per launch-field refresh and
  finds tmux once.** Every 10 s catalog tick read the registry twice
  per launchable harness to list its launch providers (about 1.1 ms
  of 2 ms with ten harnesses); it now reads it once. Every tmux call
  walked `PATH` for the tmux binary (21 us); the path is now kept
  while `PATH` is unchanged and the file is still executable (5 us). A
  missing tmux is looked up again each time, so one installed later is
  found. Closes [#1531](https://github.com/starship-terminal/swe/issues/1531).
- **A streaming Devin session costs the hub its new rows, not its
  whole window.** Each time a tailed Devin session changed, the hub
  re-read up to 1,064 chain nodes with one query and one JSON parse each:
  160 to 410 ms per read for long sessions on a 9.8 GB `sessions.db`,
  about once a second while the session streams. The tailer now keeps the
  window's decoded nodes and fetches only the rows written since its last
  read, splicing a switched chain head at its fork: about 2 ms a read on
  the same sessions, for 0.4 to 2 MB kept per session. A whole read still
  runs at least once a minute while the session changes, and the kept
  nodes go after five quiet minutes. Closes
  [#1522](https://github.com/starship-terminal/swe/issues/1522).

- **A long-running hub stops keeping state for sessions and panes that
  are gone.** The rate-limit notice memory kept one entry per session
  that ever hit a limit, and the tmux keystroke locks one per pane ever
  typed into. A session leaving the catalog now drops its limit entry,
  and a pane's lock lives only while a write holds it. Streamed messages
  whose channel died mid-reply were bounded in
  [#1532](https://github.com/starship-terminal/swe/issues/1532). Closes
  [#1533](https://github.com/starship-terminal/swe/issues/1533).
- **`swe`'s search indexer stops resolving every transcript's path.**
  Listing the transcripts called `realpath` on each one, a stat per
  folder in its path, every pass. The walk now resolves its root once
  and only resolves files that are symlinks, with the same paths and
  order as before: 87 to 66 ms to list 1,238 transcripts here. A Devin
  session still decodes its whole chain when it grows, as the Mac app's
  indexer does; FEATURES.md notes that both writers owe that change.
  Closes [#1538](https://github.com/starship-terminal/swe/issues/1538).
- **Live reply text costs the hub linear work, not quadratic.** Every
  100 ms flush of a streamed reply re-cleaned the whole message so far:
  a 100 KB reply took 3.3 s of cleaning, now 58 ms. Each message keeps
  its cleaned text up to a line break no redaction can reach across and
  re-cleans only what follows; the `done` flush still cleans the whole
  text. A reply with no line breaks makes each flush wait longer once
  8 KB of it is pending, so its cleaning stays linear too. A message
  whose channel dies mid-reply stops streaming and frees its text after
  15 idle minutes (a late chunk for it never streams), and its empty
  entry goes after a day. A hub event going to several open streams is now cleaned and
  encoded once, not once per stream. Closes
  [#1532](https://github.com/starship-terminal/swe/issues/1532).
- **The web chat parses only the growing end of a streaming reply.**
  Each repaint (at most every 80 ms) used to run the whole reply through
  the Markdown parser and syntax highlighter and replace the bubble's
  HTML. Blocks that end at a blank line are now settled: their nodes
  stay, and only the text after them is parsed and drawn again, with
  the same result as a full render. Over a 60 s, 12 KB reply (600
  paints) that is 18 ms of Markdown work instead of 824 ms, and 133 KB
  of HTML handed to the DOM instead of 9.5 MB. A delta no longer
  rebuilds every bubble's text twice to see whether anything changed,
  and the chat log is no longer a live region, so screen readers hear a
  finished reply once from a small status node instead of each repaint.
  Closes [#1555](https://github.com/starship-terminal/swe/issues/1555).

- **The hub's approval watcher reads the supervised registry once per
  pass.** Every 3 s it opened and parsed `supervised.json` once for each
  tailed session (up to 100), and again for each tmux-attached one to
  name its pane. It now reads the file once per pass and takes the pane
  name from the same row. With nothing attached it spawned no `tmux`
  before and spawns none now. Closes
  [#1530](https://github.com/starship-terminal/swe/issues/1530).

- **Devin change checks cost microseconds instead of milliseconds.**
  Every poll of a tailed Devin session opened a new connection to
  `sessions.db` and counted the session's rows: 13 to 22 ms a call for a
  37K to 52K-node session on a 9.8 GB store. The hub now keeps one
  read-only connection and asks SQLite's `data_version` whether anything
  committed. When nothing did, no query runs (0.02 ms); after a write it
  reads the newest row id and chain head (0.03 to 0.3 ms). The row count,
  which catches a delete below the newest row, is read again at most once
  a minute per session. Closes
  [#1521](https://github.com/starship-terminal/swe/issues/1521).

- **The Mac app keeps fewer things in memory for its whole life.**
  Switching machines kept a live web view and WebContent process for
  every hub ever shown; only the last two shown stay now, and an older
  one reloads when picked. The Calendar's day cache stops at 400 days,
  dropping the days farthest from the ones on show, so scrolling back
  through years no longer grows it without end. The Sessions page lists
  at most 20,000 sessions instead of 100,000, with the existing note
  when older ones are left out. Closes
  [#1544](https://github.com/starship-terminal/swe/issues/1544).
- **`swe check` and `swe mcp status` probe side by side.** `swe check`
  asked each installed harness for its version one after another (up to
  three subprocesses each); it now runs them on a small thread pool,
  7.5 s down to 2.4 s for 11 harnesses here. `swe mcp status` does the
  same for its server health checks, where each HTTP server can take its
  whole five-second timeout. The rows keep their order. `swe list` asks
  `which` once per command instead of twice, harness discovery lists
  each PATH directory once instead of probing every absent name in each
  (8.7 to 5.8 ms), and skill discovery enumerates the skill roots once
  instead of once per `SKILL.md`. Closes
  [#1537](https://github.com/starship-terminal/swe/issues/1537).
- **The hub stops re-parsing its small stores on every request.** The
  credential check, an open stream's revocation check, the session
  list's unread flags and the machine list read `devices.json`, the
  sign-ins file, `session_reads.json` (twice per session list) and
  `machines.json` from disk each time. They now share one memo that
  parses a file again only when it changes: a session list over 2,000
  read cursors went from 1.8 ms to 0.7 ms, and a credential check from
  37 to 11 us. Closes
  [#1528](https://github.com/starship-terminal/swe/issues/1528).
- **The Mac app's background polls no longer keep the Mac awake.**
  Every `swe` the app ran held a user-initiated activity, which also
  blocks idle system sleep while the child runs, so the GitHub poll, the
  3 s pair-request poll on Remote settings and the account, feed and
  limit refreshes each held it off several times a minute. Polls and
  timers now run their `swe` with no activity; runs from a button keep
  it. The activity always ended with each run, so App Nap was only off
  while a child ran, never for the whole app. Closes
  [#1540](https://github.com/starship-terminal/swe/issues/1540).
- **The Mac app's ledger ticks cost about a third of the CPU they did.**
  A background scan now counts as new usage only when it wrote a usage
  event, not when it merely read a file, so a tick that appended prompts
  or tool output rebuilds nothing. Ticks that do write usage read less:
  a covering index answers the time-range reads without a table lookup
  per row, grouped reads group by dimension id before joining names, and
  Codex's account comparison reads only Codex rows. Measured against a
  copy of a 415,000-event ledger, one ingest tick went from about 6.4 to
  2.0 CPU-seconds. Closes [#1568](https://github.com/starship-terminal/swe/issues/1568).
- **The hub slows down while nobody is watching.** With no client
  stream open (the ntfy notifier holds one whenever it can push;
  satellite command streams don't count) and no client request for two
  minutes, `swe remote serve` polls
  active sessions every 5 s instead of 1 s and idle ones every 60 s
  instead of 8 s, refreshes the catalog every 60 s instead of 10 s, and
  ticks its tail loop once a second instead of four times. Transcripts
  stay on disk, so this only delays the journal's copy. The next client
  request ends it at once: every tailer polls, the catalog refreshes, and
  the first `/v1/sessions` or `/v1/inbox` read waits up to 5 s for that
  refresh instead of serving a minute-old list. An unwatched hub on this
  Mac used about 8% of a core, split about evenly between the catalog
  and tail threads. Closes
  [#1520](https://github.com/starship-terminal/swe/issues/1520).
- **`swe` starts faster: each command imports only its own code.**
  `cli.py` imported every command's module tree before dispatching, so
  `import swe.cli` took about 330 ms on a loaded machine, nearly all of
  a `swe version` run, and every `swe` the Mac app spawns paid it. Each
  command now imports its module when it runs, which brings
  `import swe.cli` to about 33 ms. A rate limits call also reads its
  cache file once instead of five times, and `swe list` reads
  config.json's `list` section once instead of three times. Closes
  [#1536](https://github.com/starship-terminal/swe/issues/1536).
- **The status column reads less per session.** OpenCode's probe
  pulled the full JSON of a session's newest 50 messages and ran two
  `PRAGMA table_info` reads for every session; it now reads the table
  shape once per pass, picks the deciding row by type, and loads only
  that row's JSON. Devin's probe lets SQLite pull the role, tool calls
  and text out of the head message instead of decoding all of it.
  Cline caches the follow-up check against its `messages.json`
  signature and reads the metadata file once per pass, and the tail
  probes decode JSONL lines newest-first only until the walk decides.
  CPU per pass here: OpenCode 381 sessions about 66 to 50 ms, Devin
  216 sessions about 10 to 6 ms, a 64 KB transcript tail walk about
  half. Closes [#1535](https://github.com/starship-terminal/swe/issues/1535).
- **The Mac app's Compute sampler runs at full rate only while a
  trend view is on screen.** It used to sweep every process, the GPU
  clients and the SMC at the chosen interval (3 s on one Mac) for as
  long as the app ran, about 80 ms of CPU per sweep with 700 processes.
  The Compute page, the menu bar's Compute tab and the Widgets page's
  Compute preview now ask for the full rate while their window can be
  seen. Otherwise the sampler reads only what auto clean (processes),
  the fan threshold (temperatures) and a placed Compute widget need,
  every 30 s, and nothing at all when none of them is on. The page's
  own three-second refresh also stops while its window is minimised or
  covered, the temperature reads keep one SMC connection, and process
  attribution works each pid's harness out once. Closes
  [#1539](https://github.com/starship-terminal/swe/issues/1539).

- **The iPhone chat stops re-parsing a streaming reply from the top.**
  Every 80 ms repaint of a reply still being written parsed the whole
  reply twice (once for the text, once for its link cards), styled every
  block again with `AttributedString(markdown:)` and rescanned it all for
  bare links. A 16 KB reply over a minute meant about 750 full passes,
  13 s of CPU on a Mac simulator. The bubble now keeps the blocks before
  its last finished blank line and parses only what follows; styled
  text and link scans are cached by block text, and unchanged blocks
  skip layout. The same reply now costs 0.9 s, and every prefix parses
  to exactly the blocks a full parse gives. Closes
  [#1548](https://github.com/starship-terminal/swe/issues/1548).
- **A snapshot session whose store can't be found no longer costs a
  read attempt every poll.** When a Cline, Continue, Gemini, Copilot,
  Forge or Crush read found no source files or a store it couldn't read,
  the tailer had no change token to keep and tried a whole read on every
  poll, every second for an active session. It now waits 1 s, then 2, 4 and so on up
  to a minute between attempts, and goes back to the stat check once the
  files exist. Copilot and Crush tail reads also stop at the newest 1,000
  turns or rows instead of reading the whole session; the other stores
  keep one JSON document per session, which has to be parsed whole.
  Closes [#1526](https://github.com/starship-terminal/swe/issues/1526).

- **The usage ledger reads only new Devin and OpenCode rows.** Any
  write to Devin's `sessions.db` or its WAL made the next scan (each
  web Overview load after the 60 s cache, and every satellite usage
  push) JSON-decode the whole database: 76 s for 2.1 million rows in a
  9.8 GB file. `swe` now keeps a watermark per database, as the Mac app
  already did: Devin's highest `row_id` (a row still waiting for its
  metrics holds it back) and OpenCode's newest `time_updated`, so a scan
  after new activity reads only the rows past it, in about 3 ms. A
  recreated or rebuilt database is read again from the start. Closes
  [#1524](https://github.com/starship-terminal/swe/issues/1524).

- **The hub stops re-reading idle Devin sessions every minute.** A
  snapshot tailer trusted "nothing changed" for 60 s, then re-read the
  newest 1,000 chain nodes anyway. Devin's change token (the session's
  newest row, row count and chain head) already sees every write, since
  Devin rewrites a message with `INSERT OR REPLACE`, which gives it a new
  row id. Devin sessions now re-read only when that token moves, which
  saves about 2.5% of a core with 50 idle Devin sessions tailed. Closes
  [#1563](https://github.com/starship-terminal/swe/issues/1563).
- **Devin status checks stop forking `ps` for stale locks.** The hub's
  status pass asked `ps` about every pid named by a single Devin
  session lock, and most of those pids are long dead: 131 forks and
  about 0.4 s of CPU every 10 s on a machine with 318 locks. It now
  skips dead pids with a signal-0 check, and reads the lock directory
  only when a mid-turn Devin row has a quiet transcript. Closes
  [#1525](https://github.com/starship-terminal/swe/issues/1525).
- **The Mac app is SWE.app now** (SWE Wraps Everything — the product
  takes the `swe` command's name and Quiver is decommissioned). The
  bundle is `SWE.app` with bundle id `com.cwenlong.swe`, its Swift
  modules are `SWECore`/`SWEApp`/`SWEWidgets`/`SWEWidgetsUI`/`SWEFanHelper`,
  the DMG is `SWE-<version>.dmg`, and its launchd jobs, keychain
  service and fan helper moved to `com.cwenlong.swe.*` names. On
  install, the old Quiver.app is quit and removed, its `~/.quiver` data
  root moves to `~/.swe` (with a `~/.quiver` symlink left behind), its
  LaunchAgents are renamed onto the new labels, its keychain items move
  to the new service on first read, and its preferences carry over to
  the new defaults domain. Sparkle cannot update across a bundle id
  change, so a Quiver.app install must move to SWE.app once, manually;
  the release checklist covers it.
- **The iPhone app is SWE Remote now**, with bundle id `com.swe.remote`
  (was `com.quiver.remote`), extensions `SWEShare` and `SWEWidget`, App
  Group `group.com.swe.remote` and Keychain group
  `com.swe.remote.shared`. The shared store's Info.plist key is
  `SWEKeychainGroup`. Deep links move to `swe://` for pair, session and
  issue routes, and `quiver://` links keep opening, so pair QRs from a
  hub that has not upgraded still work. Pair and device metadata sent
  to the hub names `SWE Remote`; the hub reads the old
  `QuiverRemote`/`Quiver Remote` tokens the same way, so a phone on the
  old build still pairs and reports iOS correctly. Because the App
  Group and Keychain group change, a Quiver Remote install does not
  carry its pairing over: pair again after updating.
- **The GitHub repos are `starship-terminal/swe` now.** `starship-terminal/swe` was
  renamed to `starship-terminal/swe`, `swe-hub` to `swe-hub`,
  `quiver-demo-hub` to `swe-demo-hub` and `homebrew-quiver` to
  `homebrew-swe`; the old names redirect. The tap is now
  `starship-terminal/swe`: `brew install --cask starship-terminal/swe/swe` installs the
  Mac app and `brew install starship-terminal/swe/swe` the CLI. The first SWE
  release materializes `Casks/swe.rb` in the tap; `Casks/quiver.rb`
  stays frozen at the last Quiver build so old installs and uninstalls
  keep working.
- **New icon and logo everywhere; the pixel-art mascot is gone.** The
  Mac and iPhone app icons, the web app's icons, the landing site and
  the README now use the new mark: three arrowheads above `swe` on a
  dark tile. The web app serves `favicon.ico`, `favicon.svg`,
  `favicon-32.png` and `apple-touch-icon.png`, plus a
  `manifest.webmanifest` that lists `icon-192.png`, `icon-512.png` and
  a maskable `icon-maskable-512.png` for home-screen shortcuts. The
  landing site gets the same favicons. The mascot banner, the README's
  mascot picture and the landing page's mascot figure are removed. The
  source art (square, rounded and a transparent mark for dark
  backgrounds) lives in `assets/`. The new mark has one look, so the Mac
  app's Settings › App icon choice (Light or Dark) is gone, along with
  its placeholder row on the web Settings page: the app builds one
  `AppIcon.icns` and the Dock always shows it. The DMG window's
  background now scatters the mark's three arrowheads instead of the
  old icon's five glyphs.
- **Mac app: Artificial Analysis moves to its V2 API.** AA retires the
  legacy `/api/v2/data/*` routes on November 4, 2026, so the Models page
  refresh and the Settings › Services "Test" probe now call
  `/api/v2/language/models` and `/api/v2/media/<feed>/models/free`. The
  language feed tries the Pro route (`prompt_type=medium`, matching the
  old speeds) and drops to `/free` on a Free key's 403, then reads every
  page, so a refresh keeps all ~700 models instead of the first 200. Speed
  and latency are read from the new nested `performance` object; the
  creator slug AA dropped is derived from the creator name, so vendor
  colours still match; the blended price, Pro-only now, is computed as
  `(3 × input + output) / 4` when absent. On a Free key the Coding and
  Reasoning columns stay empty (AA puts per-benchmark scores behind Pro),
  and their hover notes say so. Stored rows in the old shape still
  decode. The quota text now says 100 requests a day, and a `410` reads
  "Artificial Analysis retired this API. Update SWE."
  [#948](https://github.com/starship-terminal/swe/issues/948).

- **Mac app: less CPU while idle.** The trend sampler no longer reads
  every HID temperature sensor each tick when the SMC already gave the
  CPU, SSD and battery temperatures, and the session search status line
  reads its index counts only while Settings shows it. On one Mac the
  app's idle CPU went from 4.2% to 2.9% of a core.

- **Remote: the web app lives at the hub's root.** `https://<hub>/`
  is the app and its assets sit beside it (`/app.js`, `/styles.css`),
  so there is no redirect on the way in. The `/app/…` paths earlier
  releases served redirect to the same name at the root, fragment
  intact, so bookmarks and `/app/#pair=` links keep working (#944).
- **Remote: the unused `compaction` event type is gone from the
  protocol.** Compactions have always reached clients as
  `message.appended` with `kind: "compaction"`; the separate
  `compaction` event in `EVENT_TYPES`, docs/json-contracts.md and
  docs/remote-control.md was never emitted. The hub still accepts one
  from an older satellite
  ([#516](https://github.com/starship-terminal/swe/issues/516)).

- **`--hub` clones around the state a machine already has.** `swe init
  --hub` and `swe setup --hub` used to refuse any `~/.quiver` with files
  in it, and the app, the remote hub and plain `swe` runs all write
  there on their own (caches, usage ledgers, pairing state). Now
  `~/.quiver` takes a clone while it has no hub yet: no `AGENTS.md` and
  no `.git`. The clone is moved in around what is there without
  overwriting anything; a path on both sides stops it before anything
  moves, naming the path ("~/.quiver/<path> is on this machine and in
  the hub"), and a move that fails puts back what it moved. A symlink or
  a file at `~/.quiver` is still refused; if putting something back
  fails too, the message names what stayed and the rest of the clone is
  kept for you. The hub `.gitignore` seed now also leaves out
  `data/web-ledger/`, the web app's ledger, and a clone whose own
  `.gitignore` is older gets the missing patterns in its
  `.git/info/exclude`, so this machine's state never reaches the hub's
  git ([#696](https://github.com/starship-terminal/swe/issues/696)).

- **Usage problems say what to do, graded by how much they need you.**
  A stale login or a credentials file macOS won't let SWE read puts
  an account card in its amber "Action needed" state with a Fix sheet,
  raises a banner over the window (it never takes focus) and counts on
  the Settings badge. A sign-out banners only where a read used to work,
  so a starred harness you never signed in to stays quiet. An outage
  stays on its card, with Refresh, and joins Settings › Doctor and
  `swe doctor` only after three polls in a row. A permission fix opens
  System Settings' Files and Folders pane from the sheet and from
  Doctor. Claude, Codex, Copilot, Droid and Freebuff now name their own
  login command, and an extra Devin or Cursor account's Doctor row keeps
  the fix aimed at that account's files. Issues of a harness you unstar
  close instead of holding the badge, and the issue store's file work
  runs off the main thread ([#797](https://github.com/starship-terminal/swe/issues/797)).

- **The menu bar fan popover's notes fold into an ⓘ mark.** The
  captions under the Automatic and Manual controls move into the app's
  usual info tip at the row's edge — a rest shows it, a click pins it —
  so the popover reads as controls alone.

- **The remote PWA folded into the web app.** The standalone remote
  client at `/` is gone: `/` is now the web app itself, so old
  `/#pair=<token>` and `/#approve` links land on it directly.
  Browser pairing moved to the web app's gate — "Pair with Tailscale"
  asks for approval like the iPhone app does, and the `#pair=` handler
  trades a token — while Tailscale pair requests are approved inline
  in Settings › Integrations › Remote, which polls for new ones, and
  sessions waiting on an approval are badged "Approval" in the
  Sessions list. `swe remote pair` and the pair QR now hand out
  `…/#pair=<token>` links; the `quiver://pair` deep link, the
  pairing APIs and stored device credentials are unchanged.

- **Flatter Mac app window.** The page now fills the window to its edges
  instead of sitting in a rounded, inset panel, split from the sidebar by
  a hairline. The SWE wordmark and the sidebar toggle moved up into the
  traffic lights' row, and cards, blocks, fields, buttons and sheets have
  smaller corner radii.

- **Flatter web app window.** The web app's page fills the browser to its
  edges like the Mac app's, split from the sidebar by a hairline (on a
  phone, under the page strip), instead of sitting in a rounded, inset
  panel. The SWE wordmark row is shorter, so the sidebar list starts
  higher, and cards, blocks, fields, buttons and sheets have the Mac app's
  smaller corner radii.

- **The web app's chat renders agent Markdown.** Messages used to pass
  through a minimal renderer that only restored fenced code, inline code
  and bold — tables showed as raw pipes and headings as raw hashes. The
  chat now runs a port of the phone's block parser
  (`app/ios/Sources/QuiverRemote/Hub/Markdown.swift`): headings, bullet
  and numbered lists (nested, with task boxes), quotes, rules, fenced
  code and tables become real markup. A table wider than the message
  scrolls sideways inside its own box (bounded at 24 columns and 200
  rows, with a note for the rest), a lone `![alt](src)` image or a
  harness-drawn `<json-render>` view folds into a note chip, and a link
  to the agent's own files — like the ones Codex ends a turn with —
  shows as a file name instead of a dead link.

- **The Plugins page opens on Explore.** In the Mac app and the web app,
  Explore is now the first tab and the view Plugins lands on: the
  catalog of every marketplace's plugins is what the page is for, and
  Health — where Claude Code and Codex drift from `~/.quiver/plugins` —
  stays one click away. The nudge that moved a Health-first page to
  Explore on an empty hub is gone with it.

- **Slash-command lists load when the chat opens, not on the first
  `/`.** Every client — the web app, iOS and the Mac
  app — now warms the command list as the chat opens and keeps the
  answer per session key, so the menu opens filled instead of showing
  "Loading commands" for a beat, and a reopened chat shows the kept
  list while it refreshes.

- **OpenCode models are named `provider/model` in both usage ledgers.**
  The ledgers used the bare model id, so `glm-5.1` from the
  `zai-coding-plan` plan and from `zai` read as one model. They now read
  `zai-coding-plan/glm-5.1`, from OpenCode's `providerID` (2.x:
  `model.providerID`), and keep the bare id when OpenCode stored no
  provider. The next scan of each ledger reads every OpenCode row again
  and renames its events in place: no event is added, and no token or
  cost changes. A row whose message OpenCode has since deleted keeps its
  bare name. Prices resolve as before, since they ignore the provider
  prefix; three names now match their provider's own price entry
  (`nebius/zai-org/GLM-5.2`, `groq/openai/gpt-oss-120b`,
  `anthropic/claude-opus-5-5`) and none lost its price. A model that two
  harnesses ran now shows as two rows on the Overview, Pricing and Models
  pages when only one of them logs the provider.
- **The landing page lives under `app/`.** It moved from `landing/` to
  `app/landing-page/`, next to the Swift apps. `scripts/docs_site.py`
  still copies it to the docs site root as is.
- **Both Swift apps live under `app/`.** The macOS app moved from `app/`
  to `app/mac/` and the iPhone app from `remote/ios/` to `app/ios/` (its
  roadmap is now `app/ios/ROADMAP.md`). Build with
  `app/mac/script/build-app.sh` and install with
  `app/mac/script/install-app.sh`. The web app stays in
  `src/swe/webapp/`, inside the Python package. An app installed
  before this change can't rebuild itself from Settings › Updates once
  the checkout has the new layout; run `app/mac/script/install-app.sh`
  once.
- **The Hooks page reads by event.** Pick a harness and its hooks sit on
  the events that fire them, in session order: PreToolUse with each
  script's tool (Bash, Task, any tool) and one-line description, Stop
  below it. Events nothing fires on fold into one line. Other tools'
  hooks (herdr, Agent Relay) are off by default behind one checkbox, and
  scripts nothing declares get a "Not running" card with the reason.
  Clicking a hook opens it in a panel beside the list: what fires it,
  its command and settings file, where it's linked, and the script in a
  raw editor. `swe hooks list --json` rows now carry `summary` and
  `detail` for it.
- **An SSE reconnect replays from the journal's end.** `read_since`
  parsed a session's whole journal front to back on every reconnect,
  even when the client was a few events behind: 31 to 36 ms for a
  4.4 MB, 3,519-event journal here. It now walks back from the end and
  stops at the client's cursor, in 0.2 ms for 5 events and 0.4 ms for
  100. A full replay (`last=0`) still reads the file once, forward.
  Appends encode each line once for both the write and the byte count.
  Closes [#1529](https://github.com/starship-terminal/swe/issues/1529).
- **The status and model caches stop stat'ing every entry on each
  rewrite.** The hub's status probes rewrite `session_status_cache.json`
  up to once a second while a session is busy, and every rewrite checked
  each of its 1,000 entries for a deleted file: about 5 of the 6.5 ms
  per rewrite here. That sweep now runs at most every 5 minutes per
  cache file in a process; a deleted file's entry just waits for the
  next one. A process-resident store was not worth it: the cache files
  are 210 to 245 KB here (not 10 to 50 MB) and load in about 1 ms.
  Closes [#1527](https://github.com/starship-terminal/swe/issues/1527).
- **Session listings read Devin prompts once and Cline files once per
  change.** Devin's title provenance ran one `prompt_history` query per
  session (210 here); it now runs one for all of them. Cline's session
  glob also matches every `<id>.messages.json` transcript, which a cold
  listing parsed in full every time (about 3 ms per 2 MB file); each file
  now goes through the parse cache, so an unchanged one costs a `stat`.
  `json_engine` joins the parse cache's fingerprint. Closes
  [#1534](https://github.com/starship-terminal/swe/issues/1534).
- **The iPhone chat's working pill stops redrawing every frame.** The
  cycling verb under a working agent ran on a `.animation` timeline,
  so for the whole turn it rebuilt its gradient mask and glow on every
  display frame (60 to 120 times a second), including the rest between
  sweeps. It now swaps the word on a 2.4 s periodic timeline and slides
  a fixed band with one linear animation per word: about 1,500 body
  evaluations an hour instead of 216,000 to 432,000, nothing animating
  while the word rests, and the same picture (the test renders both).
  The queued-send spinner turns with one repeating rotation instead of
  its own per-frame timeline. Closes
  [#1550](https://github.com/starship-terminal/swe/issues/1550).
- **The iPhone app's 15 s poll changes only what changed.** Every poll
  of `/v1/sessions` and `/v1/inbox` wrote the sessions, inbox and
  machines back even when the hub sent the same lists, and
  `@Observable` reports equal writes, so the Sessions page regrouped
  every row and an open search asked `/v1/search` again every 15 s.
  The lists are now written only when they differ, the page's relative
  times and Earlier cutoff follow a one-minute clock of their own, an
  open search reruns when the query or the sessions change, on pull to
  refresh, after a failed try, and otherwise once a minute (four times
  fewer requests), and the poll pauses
  while the app is inactive (the app switcher, Control Centre, a call),
  not only in the background. Closes
  [#1549](https://github.com/starship-terminal/swe/issues/1549).
- **The iPhone console, uploads and Devices page wake the app less.**
  The console hopped to the main queue once per SSH read, hundreds of
  times a second while a command printed; reads now queue up and reach
  the terminal in one batch per main-queue turn, in order. Upload
  progress redrew the chip for every chunk URLSession sent; it now
  reports at most ten times a second, plus the end. The Devices page
  fetched both pair requests and the device list every 5 s while open;
  it now loads devices once (and again after an approval, a removal or
  a pull to refresh) and asks for pending requests every 10 s. Closes
  [#1553](https://github.com/starship-terminal/swe/issues/1553).
- **The iPhone composer keeps picked files on disk, not in memory.** A
  file from Files was read whole into memory (up to 20 MB, or 200 MB
  for an image to convert), a photo from Photos loaded as one `Data`,
  and a failed upload kept those bytes for Retry. Picks now go into a
  spool folder in the app's temporary directory: files are copied disk
  to disk, photos arrive from Photos as a file and their JPEG is made
  from it through ImageIO, and every upload streams its body from the
  file. A failed upload keeps only the file; it goes once the hub has
  it or the chip is removed or sent, and what a crash left behind is
  swept at the next launch. Closes
  [#1551](https://github.com/starship-terminal/swe/issues/1551).

### Fixed

- **`build_ios.sh` reaches a real iPhone and a named signing team.**
  The device picker took the first paired iOS device `devicectl`
  reported, which could be a simulator (it reports `paired` like a real
  phone), so a connected iPhone was passed over; the filter now also
  requires `reality == "physical"`. And `DEVELOPMENT_TEAM` ships empty
  in `project.yml` with no override path, so device builds always died
  on "requires a development team" — the script now takes
  `SWE_DEVELOPMENT_TEAM` and passes it to xcodebuild. The README's
  claim that automatic signing registers the App Group was wrong for
  entitlements-file groups, so the fresh-team portal steps (create the
  group, assign it on each App ID, purge stale profiles) are
  documented.

- **The remote hub no longer drops a browser's parallel asset fetches,
  which left the web app a blank shell behind `tailscale serve`.** A
  page load fires all of its script requests at once, and serve opens a
  separate backend connection per stream in the same instant; the
  stdlib listen backlog of 5 let the kernel reset the overflow before
  `accept()`, so the proxy answered 502 for `app.js` or `pages.js` and
  nothing ever rendered. `_BoundedHTTPServer` now listens with a
  backlog of 128, so every burst connection is accepted.

- **The Mac app's AGENTS.md strip and Hub sync card keep up with the
  terminal.** After `swe instructions build` in a terminal, the AGENTS.md
  page's Generated strip kept saying Stale until the page was opened
  again, the app was brought to the front or ⌘R was pressed; after
  `swe sync`, Settings › Files kept listing the files it had just
  committed. Both now watch `~/.swe` while they are on screen (the
  chapters and the generated file; tracked files and git's refs) and read
  again when it changes, as `docs/demo.md` segments 7 and 10 show.
- **The MCP servers page sends you to Settings › Files.** With the
  servers folder missing, it said to set the folder in Settings › Paths,
  a section the Mac app no longer has; the setting is under Files. The
  Mac README and the web app's MCP notes said Paths too.
- **The Mac app no longer rebuilds every usage figure each minute.**
  The search index's 60 s scan and the 300 s ledger timer each re-ran
  every Overview query, warmed all seven periods (about 30 more grouped
  reads over the whole ledger) and woke every page that reloads after a
  scan, whether or not the scan found anything. On a ledger of about
  400K events that cost 10 s at 200-400% CPU every minute. A timer scan
  that reads nothing now runs no figure query. One that reads new usage
  rebuilds the menu bar's spend, the last 30 days the widgets show and,
  while the Overview is on screen, its selected period; the other
  periods and the heatmap's older days are rebuilt when the Overview
  next opens. The Rescan and refresh buttons still rebuild everything.
  [#1542](https://github.com/starship-terminal/swe/issues/1542).

- **`swe init` links a Codex skills folder that holds only Codex's own
  skills.** Codex 0.166 writes its bundled skills to
  `~/.codex/skills/.system` on start, so on a machine where Codex ran
  before `swe init`, init and `swe setup` read that folder as five skills
  that exist nowhere else and left `~/.codex/skills` unlinked: Codex never
  saw the hub's skills. A `.system` folder at the top of a skills root
  that carries Codex's `.codex-system-skills.marker` no longer counts as
  your content, so the root is absorbed like any folder of copies: backed
  up to `~/.swe/backups/`, then replaced with the link, and Codex writes
  `.system` again through it. A `.system` without the marker, or one
  deeper in the tree, is still treated as your skills.

- **A stray fence line or block-open line no longer corrupts the chat's
  code blocks and tables — on iOS and the web app too.** The chat
  Markdown parser closed a fenced code block on any line starting with
  the fence marker, so a quoted ` ```swift build ` inside a block ended
  the code early, and it let a table's rows swallow a line that opens
  another block, so a `> Warning: a | b` quote or a ` ```a|b ` fence
  after a table became a row. iOS's `Markdown.parse` and the web app's
  `md()` now match the Mac parser: a fence closes only on a marker-only
  line (a longer run of the same character still closes), and a table's
  rows stop when a line opens a block of its own — a quote, a fence or a
  list item is never a row.

- **`swe sync` no longer commits the app's stores or Codex's own
  skills.** While SWE.app runs it writes `~/.swe/data/catalog/catalog.sqlite`
  (and its WAL), `data/issues.json.lock` and other files under `data/`
  that the hub's `.gitignore` did not list, so a sync committed them. Codex
  0.166 writes its bundled skills to `~/.codex/skills/.system`, which is
  `~/.swe/skills/.system` once linked, so a sync committed about 50 files,
  and `swe skills` and the Mac Skills page listed them as shared skills.
  The `.gitignore` `swe init` writes now ignores the hub's own `/data/`
  (every file there is this machine's own; a skill's or plugin's `data/`
  folder still syncs) and `skills/.system/`. `swe sync` holds both back
  on a hub whose `.gitignore` is older, and `swe setup --hub` adds them
  to the clone's `.git/info/exclude`. A store the hub already committed
  is untracked by the next sync, its file left in place, and a merge
  never overwrites this machine's copy with another clone's. `swe skills` skips
  a `.system` folder at the top of a skills root; other dot folders are
  still listed. `data/web-status-services.json` no longer travels between
  machines. Found while rehearsing `docs/demo.md` segment 10
  ([#524](https://github.com/starship-terminal/swe/issues/524)).

- **`swe init` points at the chapters when the hub has them.** Its
  summary always said to edit `~/.swe/AGENTS.md` to change every
  harness's instructions, but with `AGENTS_MD/index.txt` that file is
  generated and the next `swe instructions build` replaces the edit. It
  now says to edit the chapters and run `swe instructions build`.

- **The web app's Model menu keeps your pick across a harness round
  trip.** On New session, a task's New agent session, Open in session
  and Resolve with agent, picking `sonnet`, switching to Gemini and back
  used to land on Claude's newest model instead. On New session it
  happened without a click: typing a folder whose partial path matched
  one last used with Gemini flipped the harness away and back, and Start
  sent the newest model. The menu now remembers a picked recent model
  until you pick something else from it. A harness that lists it shows
  it; one that doesn't shows and sends its own default, and the next
  harness that lists it gets it back. What the menu shows is still what
  gets sent, and a recent model is sent trimmed. The same rule as the
  Mac app's fix in [#951](https://github.com/starship-terminal/swe/pull/951).

- **The iPhone app's Model menu keeps your pick across a harness round
  trip.** On the New session, Resolve issue, pull request and task
  session sheets, picking `sonnet` on Claude, switching to Kilo (or any
  harness that doesn't list it) and back used to land on Claude's
  newest model instead. The menu now remembers a picked recent model
  until you pick something else, the rule the Mac app got in
  [#951](https://github.com/starship-terminal/swe/pull/951). A harness or
  machine that lists it shows it; one that doesn't shows and sends its
  own default, and the next one that lists it gets it back. What the
  menu shows is still what gets sent.

- **Hub marketplaces skip archived harnesses.** `swe setup --hub`,
  `swe init --hub` and `swe sync` registered the hub's plugin
  marketplaces in every installed harness, including ones the registry
  marks archived, which swe is meant to leave alone. They now skip
  archived harnesses, as the plugin installs and MCP sync already did.

- **CLI: three messages that said the wrong thing.** `swe hooks enable`
  printed disable's note ("the harness sees a missing script"); it now
  says the hook is linked back in and runs wherever its settings declare
  it. A `swe sync` conflict told you to run `git -C ~/.swe merge <ref>`
  with the placeholder unfilled; it now lists every merge sync planned,
  in order and shell-quoted (`origin/device/<machine>`, then
  `origin/main`, in device mode), and says to commit your
  own changes first when sync had some to commit, since git will not
  merge over them. Bare `swe skills` has printed the overview since
  0.2.5, yet that overview, `swe help skills`, the `list` completion and
  the hints after `swe skills catalog add` and `swe skills discover
  --apply` all said it listed skills; they now say it shows the
  overview and point at `swe skills list`, the tree of every skill
  under its root. The catalog hints name `swe skills <label>`, since
  `list` does not read registered catalogs.

- **`swe sync` no longer stops on two machines' `harness.json`.** The
  registry (`config/harness.json`) is versioned in the hub, so every
  machine that runs `swe setup --hub=<same hub>` writes it. When two
  did so before their first sync, the second machine's `swe sync`
  stopped on a merge conflict in a file whose copies differed only in
  each harness's `added` time. Sync now merges that file harness by
  harness, then field by field, when git's line merge stops on it: a
  change only one side made goes through, `added` keeps the earlier
  time, and `version` and `discovered_via` keep this machine's value.
  Both sides changing anything else (state, pin, capabilities ...)
  differently is still a conflict that changes nothing, as is a
  `harness.json` conflict next to a conflict in any other file. Hubs
  that already track the file keep it; nothing is untracked or deleted.
  The `--json` merge row gains `resolved: [path]` when this happened.

- **Mac: a picked hub's web app is the whole window now.** The machine
  switcher used to open the hub only in the panel, so the web app's own
  sidebar nested inside the native one. While a hub is up the window is
  all its web app; the title-bar strip names the machine and is the
  machine menu, the way back to This Mac or to another hub
  ([#681](https://github.com/starship-terminal/swe/issues/681)).

- **Hub: old `/app/` links keep their query string.** The redirects from
  `/app/…` and `/index.html` to the web app at `/` now carry the query
  onto `Location` (the browser already kept `#pair=` and `#approve`
  fragments), and a test pins that no `/v1/` route falls through to the
  app's page ([#943](https://github.com/starship-terminal/swe/issues/943)).

- **Remote: attached files reach harnesses that don't read `@` refs.**
  Every channel used to put a phone attachment into the prompt as
  `@/abs/path`, which only some harnesses expand. Claude Code, Gemini
  CLI and Qwen Code still get `@path`; every other harness (Codex,
  OpenCode, Cursor, Droid, Pi, ...) now gets the path in plain text,
  `[Attached file: /abs/path] <message>`, and reads the file with its
  own tools. Which harness gets which is harness.json
  `capabilities.attachments.at_refs`, over a fallback table in
  `remote/attachments.py`
  ([#516](https://github.com/starship-terminal/swe/issues/516)).

- **MCP gateway: no longer throttled by macOS.** The LaunchAgent was
  written with `ProcessType: Background`, as the remote hub's is, and
  macOS throttled its servers: `so__x`'s 6 MB tool list took 6.9 s
  through it against 1.3 s over stdio, and Claude Code's health check
  timed out on it. The gateway's agent is now `Interactive`; run `swe mcp
  gateway install` again to pick it up.

- **MCP gateway: a session's burst of connections is queued, not
  reset.** A Claude Code session connects to every shared server at
  once, and the Python 3.13 that SWE.app bundles queues only 5
  connections on a listening socket. With 18 shared servers, 13 of 18
  probes at once got "connection reset" and those servers would fail to
  connect for that session. The gateway now queues 128.

- **`swe mcp sync` writes remote servers Claude Code can see.** Claude
  Code skips an `mcpServers` entry that has a `url` and no `type`
  (`claude mcp get` says there is no such server), and sync wrote
  exactly that: a new remote server had no `type`, and overwriting one
  dropped its `type: http`. Claude Code now has its own format, like
  Droid's, that stamps `type: http` (or keeps `sse`) on every remote
  server.

- **Mac app: notarized builds keep Terminal automation.** Open in
  Terminal, account sign-in and session resume drive Terminal and iTerm
  through an `osascript` child, and macOS charges that Apple Event to
  SWE. Under the hardened runtime that prod and notarized builds use,
  an app without the `com.apple.security.automation.apple-events`
  entitlement is refused (`-1743`) before any prompt appears. Prod builds
  now sign the app, and the bundled Python that runs the hub, with
  `app/mac/script/SWE.entitlements`, which grants it; the existing
  `NSAppleEventsUsageDescription` gives the prompt its text
  ([#968](https://github.com/starship-terminal/swe/issues/968)).

- **Remote: queue-mode sends wait for idle on satellites too.** A
  `queue`-mode message to a session on another machine used to fail
  with "session is mid-turn; steer to write anyway" whenever that
  session was busy. `swe remote agent` now parks it on a lane of its
  own, as the hub does: it waits until the turn ends (up to 30 min,
  then fails honestly) and for the attachment pulls it names, while
  steers and their pulls go ahead. A send the agent stops before
  delivering replays on restart.
  The hub's and the satellite's idle wait both probe with the tailer's
  activity time, so a turn that started since the last catalog refresh
  no longer reads as "interrupted" and gets a queued send typed into it
  ([#516](https://github.com/starship-terminal/swe/issues/516)).

- **The Mac app's Model menu keeps your pick across a harness round
  trip.** On New session, Resolve issue and a task's New agent session,
  picking `claude-opus`, switching to Codex and back used to land on
  Claude's newest model instead, and the launch sent that if you missed
  it. The menu now remembers a picked recent model until you pick
  something else. A harness that lists it shows it; one that doesn't
  (Codex, or Kilo, which takes no model) shows and sends its own
  default, and the next harness that lists it gets it back. What the
  menu shows is still what gets sent. Follows
  [#914](https://github.com/starship-terminal/swe/issues/914).
- **The fan rule comes back on its own after the helper stops
  answering.** A fan request the root helper never took used to switch
  the rule off until someone toggled it or set a speed by hand, and a
  wedged SMC call could leave the helper alive but deaf to every
  request forever. The helper now exits when a pass runs over 30
  seconds without a beat, so launchd relaunches it with a fresh SMC
  connection, and a failed takeover retries after a minute instead of
  parking in its error.
- **The Mac app's Tasks page recovers when the hub comes back.** The
  hub restarts on every app install and at login, and a page opened
  while it was still starting said "This Mac's hub isn't running" until
  you clicked Refresh, even after the hub was up. The page now tries
  again on its own while the hub is down (after 3 s, doubling to 30 s)
  and fills in once it answers.
- **The Mac app's Tasks page says what the source says.** Like the web
  and iPhone pages before #916, it showed the hub's ladder as each
  task's status, so an issue whose agent had finished read "In
  progress" forever. It now shows the hub's `status` (`Agent running`,
  `PR open`, `Merged`, `Closed`, `Open`, or a Linear workflow state's
  name), and its stepper reads Open → Agent → PR open → Merged for
  GitHub and Open → Started → Done elsewhere
  ([#875](https://github.com/starship-terminal/swe/issues/875)).
- **The iPhone app's tab bar fits again.** The Tasks tab made six tabs,
  and iPhone shows four plus "More" past five, so Issues and Usage were
  hidden behind More and `quiver://issue` links landed inside it.
  Issues and Pull requests now share one GitHub tab with an Issues /
  Pull requests switch at the top, each side keeping what it loaded,
  and issue links open GitHub on its Issues side
  ([#875](https://github.com/starship-terminal/swe/issues/875)).
- **Tasks say what the source says, and say when GitHub didn't
  answer.** The web Tasks page and the iPhone Tasks tab showed the
  hub's internal ladder as each task's status, so an issue whose agent
  had finished (or whose launch failed) read "In progress" forever.
  Each `/v1/tasks` row now carries `status` in the source's words:
  GitHub's pipeline (`Agent running`, `PR open`, `Merged`, `Closed`,
  otherwise `Open`) and a Linear workflow state's own name. The stepper
  reads Open → Agent → PR open → Merged for GitHub and Open → Started →
  Done elsewhere. When `gh` times out or fails on the hub, both clients
  now say GitHub didn't answer and mark its rows as last known, where
  they used to show an empty or normal list. Also: a failed repo shows
  its error inside its own group instead of a second group, iOS shows a
  refresh error when only failed repos are left, a late link from a
  plain launch reloads the list, an old unlink error clears, both
  clients count a pull task as "PR open" until it lands, and linking,
  unlinking or closing the launch sheet no longer refetches every
  repo from `gh` ([#875](https://github.com/starship-terminal/swe/issues/875)).
- **Cursor usage works for editor-only users on macOS.** The Cursor
  editor now stores its session token encrypted
  (`cursor-safe-storage:v1:` in `state.vscdb`), so usage showed only
  while `cursor-agent` kept its own login fresh. `swe` now decrypts the
  editor's copy with the "Cursor Safe Storage" keychain key (Chromium's
  `v10` AES-128-CBC, through the system libcrypto, still with no runtime
  dependencies), and extra Cursor accounts' tokens too. Decryption runs
  only after every plaintext source came up empty or expired, and an
  expired `cursor-agent` login no longer hides a live editor session.
  macOS asks once whether `security` may read the key; the read gives
  up after 1 second and, after a timeout or refusal, waits 6 hours
  before asking again. To allow it for good, run
  `security find-generic-password -w -s "Cursor Safe Storage" >/dev/null`
  in Terminal and choose Always Allow. Other platforms keep reading
  plaintext tokens ([#796](https://github.com/starship-terminal/swe/issues/796)).
- **Stored tasks keep their last-known state when a source can't be
  read.** A linked issue last seen closed or merged read `in_progress`
  again on `GET /v1/tasks` while `gh` was signed out, Linear was down,
  or the task had dropped past the 24 most recently active tasks, since
  its state was guessed from its links alone. The hub now remembers
  each stored task's last reported state and PR in
  `~/.quiver/cache/task_states.json`, written only when a fresh read
  changes it, and serves that when no fresh read is possible. Every
  task row gains `state_source` (`live`, `cached` or `links`) so a
  client can show a cached row as last known. `tasks.json` still holds
  links only.
- **`swe doctor` names registered marketplace folders missing on this
  machine.** A `~/.claude/settings.json` or `~/.codex/config.toml` synced
  from another machine can register `directory` marketplaces whose
  folders never arrived. Doctor's Setup section now gives one warning per
  harness listing them, with a fix that names the file to edit, or says
  the file is managed by Nix when it is a store link
  ([#696](https://github.com/starship-terminal/swe/issues/696)).
- **The web app's Plugins page says once where missing marketplaces were
  looked for.** A `~/.claude/settings.json` from another machine can
  register several marketplaces whose folders are not on this one, and
  the page printed "no marketplace manifest on disk" once per name. Each
  harness now gets one line naming them and the folder they were looked
  in, and swe's catalog errors carry that folder as `path`
  ([#696](https://github.com/starship-terminal/swe/issues/696)).
- **`swe init` builds and repairs every hub folder.** It used to create
  only `skills/` and `backups/`, so a hub that reached a second machine
  as a bare `AGENTS.md` never got `plugins/`, `hooks/` or `mcp/`. The
  hub's folders are now one list in `paths.py` (`HUB_SUBDIRS`), and init
  creates whichever are missing on a new or existing `~/.quiver` without
  touching files already there
  ([#696](https://github.com/starship-terminal/swe/issues/696)).
- **Mac app sidebar toggle works in a normal window.** Since the toggle
  moved into the traffic lights' row, a click on it in a window that was
  not full screen did nothing: the sidebar list or the page reached up
  under it and took the click, and the title bar treated a click there
  as a window drag. The toggle is now a native button drawn over the
  list. In full screen, where the traffic lights are hidden, the wordmark
  and toggle sit at the left edge, and the toggle now follows the
  wordmark instead of the sidebar's trailing edge.
- **Every Activity heatmap cell shows its numbers on hover again.** The
  Overview's card first lays out 8 weeks while its width is still 0,
  then widens to about 15; the columns added then (July and August on a
  late-October grid) never reported a frame, so hovering them drew no
  bubble, and some earlier frames went stale. The cell frames and the
  cell under the pointer now come from the grid's fixed metrics
  (`HeatmapLayout` in SWECore), with one measured origin for the
  rows.
- **A unit test could freeze the whole Swift suite.** In
  `clearWaitsForACallInsideTheLock` the clearer ran as `Task.detached`,
  so `store.clear()` parked a cooperative-pool worker on `storeLock`
  while the `release` flag it was waiting for could only be set by the
  test's own continuation — which needed the same pool. On a narrow or
  loaded runner the pool starved and every test still in flight hung
  (the v1.1.0 release job sat for four hours). The clearer now runs on a
  dedicated thread and the test carries a one-minute `timeLimit`.
- **Devin approval menus reach the phone.** The tmux watcher only
  recognized claude-style permission menus; devin's (options numbered
  "1 Yes" with a bare space, footer "esc cancel") fired no
  `approval.requested`, so a remote devin session sat silently blocked
  until someone answered on the host. Detection now reads that shape,
  the approval card shows the "Running command" line as its prompt,
  and answers pick the menu digit by label instead of a fixed 1/2/3
  map — devin puts "No" at 8, where the old map would have sent a
  wider grant.
- **Cursor's status page moved.** status.cursor.com now redirects to
  status.x.ai, which serves the Statuspage feed under
  /api/compat/summary.json; the app's transport follows no redirects, so
  its card read "answered 301". Both catalogs (the app's and the web
  app's Python port) point at the final URL.
- **A failed Rebuild explains itself.** The Local code row used to show
  the build log's last six lines, which for a Swift build are progress
  counters like `[167/172]` and say nothing. The row now shows the
  lines that name the failure — compiler and tool `error:`/`fatal:`
  diagnostics, the build scripts' own `build-app:`/`bundle-swe:`
  messages, and shell complaints — with styling escapes stripped and
  overlong lines cut; a flooded log shows the first few errors and the
  last. The whole log is written to
  `~/.quiver/cache/rebuild/last-build.log`, which the message links to,
  and the warning text can be selected to copy either out.
- **Keys and plans: the "new terminals don't get these keys" card is
  honest about managed startup files.** It used to fire on any rc whose
  own text lacked the `source` line, so a home-manager `.zshrc` that
  sources a drop-in folder (`for f in .../**/*.zsh; do source "$f"`)
  showed it even when a drop-in already loaded the keys; and clicking
  Add failed with a bare "The file couldn't be saved", since the write
  goes through to a read-only Nix store path. The check now follows the
  rc's sourced files, globs and `for`-loop variables (bounded), and a
  not-sourced rc that can't be written shows the line to copy into the
  generating config instead of the button.
- **Remote: tmux launches no longer assume `/usr/bin/env` exists.** A
  bare `swe remote` launch wraps its command in `env` so tmux never
  hands a lone argument to `default-shell -c` (zsh's startup files can
  rebuild `PATH` there, and a binary path with a space splits), but the
  wrapper was hardcoded to `/usr/bin/env`, which is absent on NixOS and
  in the Nix build sandbox. It now resolves `env` through `PATH` first,
  falling back to `/usr/bin/env`.

### Security

- **Remote: ntfy approval buttons can no longer switch off later
  approvals.** A notification's action token accepted any menu row
  number, so anyone who could read the ntfy topic and reach the hub
  could answer with Devin's "switch to bypass mode" (row 5) although the
  push showed only the first rows. Each token now carries the exact
  choices its notification shows as buttons and spends nothing else.
  Rows that grant past this one prompt (always allow, don't ask again,
  session-wide allow, bypass or yolo modes, across Claude Code, Codex,
  Devin and Gemini menus) never get a button and never ride a token;
  the push links to the chat for them, and a paired device still
  answers them. Prompts without a menu show Allow and Deny (no Always).
  Any other decision on a live token burns it and gets a 403
  (`decision_not_allowed`) plus a hub log line
  ([#1464](https://github.com/starship-terminal/swe/issues/1464)).

- **Remote: revoking a lost phone works when it shares the owner's
  tailnet login.** The owner's own tailnet devices get in without
  pairing, and Revoke refused their `tailnet:<node>` ids, so a lost
  phone signed in to the owner's Tailscale account walked straight back
  in through Safari. The hub now records those sign-ins
  (`tailnet_signins.json`), lists them in `GET /v1/devices`, web
  Settings ("Signed in through Tailscale") and `swe remote devices`, and
  `POST /v1/devices/revoke` / `swe remote devices --revoke=tailnet:<node>`
  revoke one: that node gets no owner identity, and devices paired from
  it are signed out, until a pair request from it is approved again.
  Revoking a device paired through a tailnet request revokes its node
  too. The owner's other devices keep working
  ([#966](https://github.com/starship-terminal/swe/issues/966)).

- **Mac app: the fan helper is checked against SWE's own signature
  before it runs as root.** The one-time install copied
  `SWE.app/Contents/MacOS/SWEFanHelper` to
  `/Library/PrivilegedHelperTools` and only checked that the copy
  matched a hash taken before the password prompt. The bundle is
  user-writable, so anything running as the user could patch the helper
  beforehand and have it run as root once the user typed their password.
  A build signed by a team (Developer ID, or the Apple Development
  identity dev builds pick up) now has the root script verify the
  root-owned copy with `codesign --verify --strict` against
  `identifier "SWEFanHelper"` and that team, and deletes it and stops
  otherwise. Ad-hoc builds have no signature to check against and keep
  the hash check alone. Installed helpers are not reinstalled, so nobody
  is asked for their password again
  ([#967](https://github.com/starship-terminal/swe/issues/967)).

- **Mac app: the fan helper only takes requests from SWE itself,
  checked by its code signature.** The running root helper held the fans
  for any request whose owner was a live process named `SWE`, so a
  program called `/tmp/SWE` qualified. The install now pins the
  installing app's signature in the launchd job (`--owner-team <team>
  --owner-identifier <id>` for a team-signed build, since the team also
  signs the Python bundled for swe; `--owner-cdhash <hash>` for an
  ad-hoc one), and the helper checks each request's owner against it through its
  audit token and `SecCodeCheckValidity`, keeping the answer per process
  (pid and pid version) so a reused pid is checked afresh. A job without
  a valid pin starts nothing. The helper is now version 4 and writes its
  `version` into `/var/run/com.cwenlong.swe.fan.json`. On launch,
  SWE finds a helper older than that, says why it needs replacing,
  and offers "Update helper" (one administrator password prompt) or "Not
  now". Not now is remembered for this version: the old helper is left
  unused, the fans stay with macOS, and both fan popovers show the
  reason with an "Update fan helper" button; Set speed or the
  temperature rule's switch also updates it
  ([#967](https://github.com/starship-terminal/swe/issues/967)).

- **Remote: a local process can no longer claim the owner's tailnet
  identity.** Behind `tailscale serve` the hub read the client from
  `X-Forwarded-For` on every loopback connection, so any process on the
  hub machine, another user's included, could name the owner's phone
  and get owner powers (launch agents, approve pair requests, read API
  keys). It now believes the header only when the OS places the
  connection's far end in the Tailscale daemon: a root-owned socket in
  `/proc/net/tcp` on Linux, a root process or a binary in a root-owned
  `/Applications/Tailscale.app` per `netstat -anv` on macOS. Anyone else
  is read as a loopback client with no tailnet identity. The hub
  machine's own tailnet node also stops counting as the owner's device:
  serve's local listener forwards any local process as that node. The
  owner's phones and other computers get in as before, and paired
  devices are unaffected; a browser on the hub machine now pairs
  ([#965](https://github.com/starship-terminal/swe/issues/965)).

- **The remote hub refuses wildcard and non-tailnet binds.** `swe
  remote serve --host=0.0.0.0` — or a LAN address, a link-local
  address, or a name that resolves to one — used to be taken
  verbatim; `serve` and `serve --install` now refuse it and name
  `--unsafe-bind-any` as the explicit override, matching the
  tailnet-only spec. Bind addresses may be loopback or a Tailscale
  address (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), by literal or by
  resolved name, and `--install` carries the flag into the service's
  command line when it was given.

- **`GET /v1/status` answers `{ok, v}` only.** The unauthenticated
  probe used to hand the machine's id and name, the swe version and
  the hub's uptime to the whole tailnet; it is a reachability check
  again. `swe remote status` now reads the machine row and version
  from the local machine's own files (it was the only consumer of
  those fields — the iOS client keeps an unused `status()` method,
  and `swe remote setup` only checks whether the route answers).

- **`POST /v1/pair` is rate-limited.** Token exchanges get the same
  5-per-10-minutes per-source-IP cap as `/v1/pair/request`, counted
  in a budget of their own and checked before the `tailscale whois`
  fork — a flood can't turn into subprocess churn.

- **The unauthenticated routes answer only Host names the hub
  serves.** A plain loopback bind now records `127.0.0.1`, `localhost`
  and `[::1]` (with the bound port) as its Host values — where
  previously the DNS-rebinding check had nothing to compare against —
  and a loopback bind behind `tailscale serve` keeps them alongside
  the public URL's name. A tailnet bind names every spelling of its
  own node — the bind value, the tailnet IP and the MagicDNS names —
  so the link `swe remote pair` writes passes whichever one it uses.
  `/v1/status`, `/v1/pair`, `/v1/pair/request` and the request poll
  refuse any other Host with `403 bad_origin`, so a page that rebinds
  its domain to `127.0.0.1` can't drive the pairing surface.

- **iOS: the App Transport Security exception is scoped.** The
  blanket `NSAllowsArbitraryLoads` is gone. `*.ts.net` (MagicDNS hub
  names) gets an insecure-HTTP exception, and
  `NSAllowsLocalNetworking` covers the unqualified or `.local` names
  and IP literals — including Tailscale CGNAT addresses — a hub
  address can take; arbitrary domains are back under full ATS.
- **An enabled-but-incomplete ntfy config no longer parks a dead
  subscriber on the hub bus.** `enabled` without `url`/`topic`
  subscribed a queue nothing drained; it filled to the 2000-event cap
  and silently dropped events. The notifier now starts only when the
  config can actually post, and unsubscribes rather than leaving the
  queue behind.
- **ntfy approval actions carry a scoped token, not a device
  credential, and prompts are redacted.** The Allow/Deny/Always
  buttons used to send `Authorization: Bearer <cred>` for the
  `ntfy-actions` device row — a full-power credential good for every
  attached session — through the ntfy topic, where the host (and
  ntfy.sh's relay for iOS delivery) could read it. Each notification
  now mints a token bound to (session, request_id): single-use, ten
  minutes, accepted only by the approval route, and retired the moment
  that request resolves — overlapping requests on the session keep
  their buttons. The prompt text also runs the transcript
  secret-redactor plus a length cap, so a `Bearer sk-...` inside a
  permission menu can't reach the topic either. Every hub start
  revokes the legacy `ntfy-actions` device row and scrubs it from
  `notify.json` — notifications disabled or half-configured can't
  strand it live — and a failed revoke keeps the fields to retry
  rather than losing the only handle on a leaked credential. Paired
  devices keep working on the approval route unchanged.

## [1.1.1] - 2026-10-26

### Fixed

- **"Clear search data" could hang the app.** With the session-index
  switch still on, the clear ran its rebuild inside the same flight it
  then waited on, deadlocking itself. The first release run's `swift
  test` surfaced it; the rebuild now runs the flight's body directly.

- **Inbox header controls say what they do.** The "Unread only" switch
  read like a notification preference; it is now an Unread / All
  segmented filter that names both states, and mark-all-read is a
  labelled "Mark all read" button instead of a second unlabeled icon
  beside Refresh. The web app's inbox header matches.
- **App: appended transcripts index like a whole read.** A transcript
  that grew since its last index is read from the stored cursor, and the
  fresh decoder on that span made two mistakes: a paginated Codex
  rollout's tail decoded as non-paginated (`response_item` rows indexed
  over the real `item_completed` turns) and the span's first prompt
  overwrote the stored session title, sometimes with an injected turn
  like a `<local-command-caveat>` block. `SearchIndexer.beginFile` now
  primes appends the way `src/swe/sessions/search_writer.py` does:
  the head's first `session_meta` restores the `history_mode`, and only
  an explicit title record may rename the session. Appends are also
  gated to claude/codex/pi, Python's `APPENDABLE` set — any other
  harness's grown file re-reads whole.

- **`swe remote` prints a one-line error instead of a traceback when
  HOME is unset, empty or unwritable.** Every subcommand that writes
  state does so under `~/.quiver/data/remote`, and creating it surfaced
  as a bare `OSError`. The command boundary now catches it and prints
  `can't write <dir>: <reason>`, exit 1.
- **Cursor usage reads past the editor's encrypted token.** Cursor's
  `state.vscdb` now stores `cursorAuth/accessToken` as an opaque
  `cursor-safe-storage:` blob (Electron safeStorage) rather than a raw
  JWT, and the blob shadowed the CLI auth file and the macOS keychain
  behind it: it was taken as the session token, so the account card read
  "Couldn't fetch usage" and doctor reported an error. The token ladder
  now skips any value that doesn't decode as a JWT and tries the next
  source, and the signed-in probe counts a stored file token only when
  it decodes. An extra account's `state.vscdb` still reads alone — a
  blob there reports signed-out rather than borrowing the default
  account's keychain.

- **Remote tail, journal and SSE read-path bugs** (issue #514). Four
  bugs that could show duplicated, missing or stale messages on the
  phone:

  - A transcript that shrank was always re-read from byte zero, so a
    copytruncate-style rotation (logrotate, an in-place rewrite that
    kept the head) replayed the whole transcript as new
    `message.appended` events. `JsonlTailSource` now keeps a sample of
    the file head: an unchanged head means the stream was only cut
    shorter in place — its remaining bytes were already sent, so
    nothing is re-emitted — while a changed head is a new stream and
    still restarts. The sample rides in the cursor file and is
    verified on restore, even for a file cut shorter than the sample.
  - opencode `part` rows sharing a millisecond were skipped forever:
    the cursor was a bare `time_created` and the query asked for
    `time_created > cursor`. It is now a `(time_created, id)` position,
    and it only advances after a row is consumed, so an error
    mid-iteration re-reads the row instead of losing it. Cursor files
    holding the old scalar resume at that millisecond's start.
  - `Journal.read_since` read the file without `_LOCK` while appends
    and ring trims mutate under it; a read racing a trim could answer
    from the pre-trim file without `truncated=True`. The read now runs
    under the same lock.
  - The three SSE routes read only `?last=`, so a standard EventSource
    client reconnecting with `Last-Event-ID` got a full replay. The
    header is now honored as the resume cursor, with `?last=` as the
    fallback.
- **Remote: docs and CLI drift.** `swe remote status --json` now exits 1
  when the hub is unreachable while still emitting the envelope, matching
  docs/json-contracts.md and the Mac app's caller. `swe remote devices`
  and `swe remote attach` reject stray positional args like every other
  subcommand, and the `attach` usage line lists the real `--mode` set
  (`auto|tmux|headless|serve|appserver`). `import quiver.cli` no longer
  pulls `quiver.remote` in, so `swe version` and friends skip sessions,
  reports and the hub. docs/remote-control.md's status line reflects that
  R1–R5 shipped, and the README gains a remote-control section with the
  threat model.
- **Satellite command queue bugs (#513).** The hub's per-satellite
  backlog floor (`_sat_acked`) only moved when the command stream
  reconnected, so a healthy satellite eventually hit 429s after
  `MAX_SAT_PENDING` commands; `command.result` events now act as the
  in-band ack — the hub maps `command_id` back to the queued seq and
  advances the floor mid-stream, contiguously, so a result for a later
  seq cannot leap past a command still running. An agent restart could
  re-run a command that had finished out of order (e.g. a `control`
  completed while a long `message` send still ran): the done set above
  the contiguous cursor floor now persists in `agent-cursor.json`, and
  the replayed seq is skipped instead of re-dispatched. Inbox approvals
  no longer linger forever when a satellite dies or a pane is killed
  mid-prompt: pending entries expire after `APPROVAL_TTL_S` (24 h) on
  inbox read. `queue_command` is now typed `dict | None`, matching its
  backlog-full `None` return.
- **Paired devices can no longer impersonate satellites.** Any device
  credential used to reach every `/v1/agent/*` route with a
  caller-supplied machine id, so one paired phone could open another
  machine's command stream, fake its presence, overwrite its catalog,
  and journal events into its sessions. Device rows now carry a `role`
  (`phone`, `satellite`, `actions`): satellites claim theirs when they
  pair (`swe remote agent --token`), and the first
  `/v1/agent/register` pins the credential to that `machine_id` for
  good. Every agent route — register, catalog, usage, events, the
  command stream and attachment pulls — now requires a satellite
  credential bound to the machine it names and answers 403 otherwise,
  including for the `ntfy-actions` row and for devices paired before
  roles existed (they read as phones). A satellite also cannot
  register the hub's own machine id.
- **Agent event ingest is allowlisted.** `POST /v1/agent/events`
  journaled any event `type` and message `kind` a credential sent;
  events outside the protocol's vocabulary — and `message.*`/`notify`
  events whose `data.kind` is not in that type's known set — are now
  dropped instead of journaled.
- **The PWA escapes a message's kind.** `appendMessage` interpolated
  `kind` into `innerHTML` unescaped, so a journaled message with a
  markup-bearing kind could run script in the phone UI. The role label
  is now escaped, and an off-list kind no longer adds CSS classes to
  the bubble (a crafted kind could have faked the `user` style).
- **Remote web: the working pill names its harness.** The PWA's working
  indicator under the last chat message now shows the session's harness
  logo beside the pulsing dots, as the iPhone app does, with the
  harness's name as the fallback when it has no logo. The hub serves
  the bundled logos the Mac and iPhone apps draw at
  `/harness-<id>.svg`, resolved through their alias table
  (`claude-code` to `claude`, and so on), and a session row in the
  PWA's list shows the logo in place of the harness's name.

## [1.1.0] - 2026-10-26

### Added

- **Shared hooks across harnesses, plus hook CRUD.** The `hooks` domain
  gains a third tier next to per-harness scripts and foreign (unclaimed)
  destination files. A script under `~/.quiver/hooks/shared/` declares
  its events in a `# events: [PreToolUse, Stop]` header; `swe init` and
  the new `swe hooks sync` generate a thin wrapper per installed
  shim-able harness under `hooks/<harness>/_generated/` (linked whole
  into the harness's hooks root) plus one stdlib-only runtime at
  `hooks/_runtime/shim.py` that normalizes each harness's stdin payload
  into a Claude-style shape and maps the script's answer back — flat
  contracts (cursor) get `{"permission": ...}`/`{"continue": ...}`,
  must-print contracts (gemini, qwen) always get valid JSON, and every
  failure path fails open. Contracts now live in a registry
  (`hooks/contracts.py`): root, settings file, schema, event names,
  event map and JSON requirements per harness, overridable through
  `capabilities.hooks` in `harness.json` (including a `contract` key
  that adopts another row's schema for harnesses quiver has never met).
  `swe hooks list` shows a TIER column and the rows carry `enabled`,
  `events_supported` and the new stable `--json` shape;
  `swe hooks new shared <name>` scaffolds the normalized template;
  `swe hooks disable|enable` toggle a `.disabled` marker that unlinks
  and relinks; `swe hooks delete` removes hub files (real foreign files
  only with `--foreign`), always behind `--yes` or a prompt, and never
  touches declaration stanzas. Cline's hook dir is corrected to
  `~/Documents/Cline/Hooks`, and `shared`/`_runtime` are reserved hub
  dirs rather than harnesses.

- **Mac app: Doctor installs an off-PATH harness in the app.** An
  off-PATH finding's Run now runs `swe install <name> --via=<via>` as a
  subprocess, with "Installing… 0:14" while it works, instead of Run in
  Terminal. What the installer printed shows on the row after a failure,
  with Run in Terminal beside it for an installer that needs a password,
  and in an "Installed" card after a success. `swe doctor` itself now names
  the method (`action: "install"`, `via`, and the fix `swe install <name>
  --via=<via>`), which the web Doctor used to add on its own.

- **Web app: Add server on the MCP page.** The header's Add server opens
  the Mac app's "Add a server" sheet: a command line or a URL (HTTP or
  SSE), env vars or headers with a Secret switch per row, the harnesses to
  add it to, and Replace. It runs `swe mcp add` on the hub in process; a
  secret's value goes to the MCP secrets file, the hub's `mcp.json` gets
  `${NAME}`, and the harnesses picked get the value. `swe mcp add`'s body is now `add_plan` and `add_server`, which
  the CLI and the web app share.

- **Web app: add, edit, delete, reveal and copy API keys.** Settings ›
  Providers › Inference is now the Mac app's Keys and plans table, read
  from `~/.quiver/secrets/providers/.api_keys`: each key under its
  heading, masked as the app masks it, with reveal, copy, Add key (a
  catalog provider, saved as its variable) and a row's Edit sheet with
  Delete. It writes the file as the Mac app does, with its backup, mode
  600 and the changed-on-disk check (`quiver.providers.keyfile`). The tab
  used to list the registry's providers read only. Full values travel
  only in the answer to a reveal or copy.

- **Web app: set MCP secret values.** An MCP server's sheet lists each
  `${NAME}` its hub definition uses, like Settings › Providers ›
  Inference in the Mac app: set values show masked with reveal, copy and
  Replace, and a missing one gets a password field that saves to
  `~/.quiver/secrets/mcp/.api_keys` on the hub. The sheet used to say to
  run `swe mcp secret set NAME`. Full values travel only in the answer to
  a reveal or copy.

- **Web app: set your own model prices.** A Pricing row opens the Mac
  app's price form: price applies to, input, output and cache rates per
  1M tokens, Save, and Remove your price on a row priced by you. It writes
  `~/.quiver/config/pricing.json` on the hub, in the app's shape, and the
  page reprices at once. The footer and the unpriced help used to say to
  set prices in the Mac app.

- **Web app: Remove an MCP server.** The MCP server sheet has the Mac
  app's Remove…, which asks first, names `swe mcp remove <name>
  --from=…` and the places it goes from, and runs it on the hub.
  `swe mcp remove`'s body is now `remove_places` and `remove_server`,
  which the CLI and the web app share.

- **Web app: install, enable, disable and uninstall plugins.** A plugin's
  sheet on Explore lists each harness with its state and the Mac app's
  actions as buttons, where it used to say to use `swe plugins`. Each asks
  first, naming the command (or Codex's config edit) and the machine, then
  runs `swe plugins <verb> <ref> --harness=<h>` on the hub.

- **Web app: New skill.** The Skills page has a New skill button (and one
  in the empty list, which used to name `swe skills new <name>`). It opens
  the Mac app's form, name and description, and writes
  `~/.quiver/skills/<name>/SKILL.md` on the hub, then offers to open it.

- **Web app: Install buttons for missing agents.** Setup's Install sheet
  for an agent that isn't on the hub has an Install button beside each
  package manager's command, which runs `swe install <name> --via=<method>`
  on the hub and registers it; a failure shows the end of the installer's
  output. Doctor's off-PATH finding gets Run, reinstalling the harness
  with the manager that put the stray copy there. `swe install`'s run step
  is now a function (`install_harness`) the CLI and the web app share.

- **Web app: AGENTS.md and Doctor link with a button.** The AGENTS.md
  page's Global tab has "Link with swe init" where it used to say to run
  `swe init` on the hub, and "Replace and back up…" where it said to run
  `swe init --force` in Terminal. Replace asks first and lists every path
  it will copy to `~/.quiver/backups` and link over. Doctor offers the
  same Replace after a Run that left a real file in the way. The Mac app's
  AGENTS.md page gets Replace and back up too, behind a confirmation.

- **`swe session index` reads Grok, Kiro and Hermes.** Grok's
  `summary.json` and Kiro's `<uuid>.json` read together with the
  transcript beside them (`chat_history.jsonl`, `<uuid>.jsonl`), so a
  change to either file reads the session again; Hermes reads its
  `session_*.json` documents. The rows match the Quiver app's.

- **`swe session index` reads Pi, Cursor, Qoder, CodeBuddy and OpenCode.**
  swe's index writer now decodes these the way the Quiver app does, so a
  Linux hub's search matches what was said in them too. Cursor chats take
  the title, folder and times their transcript lacks from the chat's
  `meta.json`; OpenCode sessions (1.x and 2.x tables) index under
  `<opencode.db>#<id>`. Both writers produce the same rows for each file,
  so on a Mac neither re-reads what the other already read.

- **Session search reads Devin CLI sessions.** Both index writers (the
  Quiver app and `swe session index`) now read every Devin session from
  `~/.local/share/devin/cli/sessions.db`, along the chain that holds each
  message's final version, so a search matches what was said in any
  Devin session, not only the few exported to `transcripts/*.json`. swe
  also reads those exports, as the app already did. Each session indexes
  under `<sessions.db>#<id>` and is read again only when its activity
  time or node count moves; a session Devin hides or deletes leaves the
  index.

- **Web app: Settings › Experimental › Session search is a real switch.**
  It was greyed out as Mac app only. It now turns the hub's transcript
  index on and off (the same setting as `swe session index --on`/`--off`),
  starts a first pass at once, and shows what the index holds under the
  switch: indexing progress, "Synced: N sessions, M messages", files
  behind or failed, and the last pass's error if it failed.

- **Web app: the Sessions search box matches what was said.** Typing in
  the box still filters titles, folders and models at once; after a
  short pause it also asks the hub's `/v1/search`, so a session whose
  transcript holds the words stays listed, with the matching snippet
  under its row and the words marked. A hub with no search index says
  it is matching titles and details only, and how to turn the index on.

- **Session search without the Mac app: `swe session index`.** swe now
  writes the transcript search index itself, the same
  `~/.quiver/data/search/search.sqlite` file and schema the Quiver app
  writes, so `swe session --search` and the remote hub's `/v1/search`
  match what was said in Claude Code and Codex sessions on a Linux hub
  too. `swe session index` runs one pass; `--on` makes the hub keep the
  index current every two minutes, `--off` stops it, and `--status`
  shows what it holds. On a Mac both can write the one file: every
  commit is fenced on the transcript's row, so neither duplicates or
  undoes the other, and swe never touches an index at another schema
  version.

- **Web app: the Compute GPU tab draws a line per harness.** Like RAM
  and CPU, it plots each harness that went above the flag with its logo
  at the line's end, instead of one line per GPU device. The hub reads
  each process's GPU use from `nvidia-smi pmon` and adds it up per
  harness; the machine line still shows each device's total.

- **Remote: `swe remote serve --install` on Linux.** It writes a
  systemd user unit (`~/.config/systemd/user/quiver-remote-hub.service`)
  that starts the hub at boot and restarts it 15 s after it exits, the
  way the LaunchAgent does on a Mac, so a Linux home server no longer
  needs a hand-written unit. It turns on lingering so the hub outlives
  logout, or says to run `sudo loginctl enable-linger $USER` when the
  system won't let a user do that. `swe remote setup --apply` installs
  it too, and the setup list shows a Linux hub that stops at logout.

- **Web app: the remote setup checklist on Settings › Remote.** Until
  every step `swe remote setup` checks is done, Integrations › Remote
  lists them with a badge each (Done, Needs you, Quiver does this,
  Failed, Waiting) and a way forward: a link to the page a person needs
  (download, sign in, the Tailscale admin console), a command to copy,
  or Pair a phone. The steps Quiver does itself point to the Mac app or
  `swe remote setup --apply`, since running them from a page the hub
  serves could cut that page off. Check again re-reads the list.

- **`swe remote setup`: every step from nothing to a paired phone.**
  Remote control needs Tailscale installed and signed in, MagicDNS and
  HTTPS certificates on in the tailnet, a certificate this machine can
  fetch, the hub running in the background behind `tailscale serve`, and
  a paired device. `swe remote setup` checks each in order and says
  whether it's done, something Quiver does itself, or something only a
  person can do, with the page to open (the sign-in link Tailscale
  reports, the admin console's DNS page). `--apply` does Quiver's part:
  it starts the hub on loopback with its https address and sets up
  `tailscale serve`, checking the certificate first so a working hub is
  never moved somewhere it can't be reached. A certificate that fails
  says why in plain words, such as a DNS server Tailscale can't reach.
  `--json` gives the same list for the Mac app. The hub's LaunchAgent
  now loads on a retry when launchd answers "Input/output error", and
  every Tailscale call finds the CLI inside Tailscale.app too.

- **Mac app: read a session live from Sessions.** A click on a
  session now opens its chat over the list, as in the web app: the
  transcript from this Mac's hub, tool calls that open to their
  arguments and output, approvals and how they were answered, then each
  new message as the agent writes it, with a Live dot and the session's
  status and ownership. ⌘[ or Sessions goes back; Details and the
  context menu still open the details sheet, which is also what a click
  opens for cloud sessions, ones the hub doesn't list, and any while the
  hub is down. The first chat pairs the app with the hub on its own: it
  lists as "Quiver for Mac" in Settings › Remote and keeps its
  credential in `~/.quiver/secrets/remote/app-device.json`. An attached
  session takes messages from the box under the transcript: Queue waits
  for the agent's turn to end, Steer writes now, Return sends and
  Shift-Return starts a new line, and Stop ends the turn. Each message
  shows at once and says where it stands (Sending…, Queued until the
  agent is idle, or why it failed) until the transcript echoes it.
  An observed session offers Attach, which asks first (it stops the
  harness in its terminal and resumes the session under Quiver), and an
  approval the agent asks for gets Allow, Always and Deny on its card.

- **Mac app: a setup checklist for remote control.** Settings ›
  Integrations › Remote now offers "Set up remote control" until the
  phone path is finished, in place of the old "Run `swe remote serve`"
  hint. The sheet shows each step of `swe remote setup` with its state
  and one button: open the page it needs (download Tailscale, sign in,
  the admin console), open Tailscale, copy a command, or pair a phone.
  One Set up button does every step Quiver can do itself. The list
  checks again every 3 seconds while it's open, so a step finished in a
  browser ticks on its own, and once ready it shows the https address
  and a Pair a phone button. The first-run wizard ends with an optional
  Phone access step that opens the checklist when it closes.

- **Remote iPhone app: Console.** Console, in the hub menu on
  Sessions, opens a shell on the hub's machine over SSH, for debugging
  when chat can't help. It dials the machine's sshd over the tailnet,
  not the hub, so it still works when the hub is down. The phone's key
  is a Secure Enclave P-256 key sent to the hub on first use;
  `swe remote ssh --on` there lets it log in, and until then the console
  shows that command. The phone trusts only the host keys the hub
  listed and saves them, so with the hub down it dials with the saved
  ones and says so. Built on SwiftNIO SSH and SwiftTerm, with its keyboard
  bar for Esc, Ctrl, Tab and arrows.

- **Remote: SSH keys for the phone's console.** A paired phone can
  post the public half of an SSH key (`POST /v1/ssh/key`), and
  `GET /v1/ssh` tells it the login user, port 22, whether sshd answers,
  and this machine's ed25519 and ecdsa host keys to pin. With
  `swe remote ssh --on` the hub writes one line per device into
  `~/.ssh/authorized_keys`, tagged `quiver-remote:<device id>` and
  limited to no agent or X11 forwarding; every other line stays as it
  was. Revoking a device (CLI or web app) removes its line, and
  `--off` removes them all. Off by default, since it changes who can
  log in. The console dials sshd directly, so it still works when the
  hub is down.

- **Mac app: View › Zoom In / Zoom Out / Actual Size (⌘+ / ⌘− / ⌘0).**
  Text on every page grows or shrinks in steps from 75% to 150%, like a
  browser's zoom, so a smaller window still shows a whole page. ⌘= works
  without Shift. Zooming out stops at 75% and never takes text under
  9pt (text designed smaller keeps its size until you zoom in). The zoom is kept across
  launches and also applies to the menu bar panel.

- **Remote iPhone app: paired with more than one hub.** Pairing the
  app with another machine, such as a home server's hub, used to
  replace the hub it was paired with. Now the new hub joins a list: the
  hub menu at the top right of Sessions lists every paired hub with its
  name and address, a check on the one on screen, and Pair another hub;
  a tap switches, and the tabs, chats and Usage figures start afresh
  for that hub. Unpair drops only the hub on screen and shows the next.
  Each hub keeps its own Keychain credential, and a phone paired before
  this keeps its hub. A refused answer from a hub the app has switched
  away from no longer unpairs the hub it switched to.

- **Remote PWA: / command menu in the prompt box.** Typing `/` at the
  start of the browser client's prompt box lists the commands this
  session's harness will run, from the hub's `GET
  /v1/sessions/{id}/commands`: name, argument hint, one-line
  description, and where it comes from (Built-in, User, Project, Skill,
  Plugin). The list is fetched once per chat and filtered as you type,
  names that start with what's typed first; Enter or Tab picks and puts
  `/name ` in the box, ready for an argument. Footnotes say when custom
  commands don't work in the session's mode or aren't available from a
  satellite yet.

- **Remote PWA: @ file mentions in the prompt box.** Typing `@` in the
  browser client's prompt box opens a menu of files and folders under
  the session's folder, from the hub's `GET /v1/sessions/{id}/files`,
  filtered as you type. Arrow keys move through it, Enter or Tab picks,
  Escape closes it, and a tap works on touch. A file goes in as `@path `;
  a folder as `@path/` with the menu still open to pick inside it; a
  path with spaces is quoted, as Claude Code writes it. An `@` inside a
  word, such as an email address, opens nothing, and a satellite's
  session says file search isn't available for that machine yet.

- **Remote PWA: swipe a session to attach, detach, pin, rename or fork
  it.** The phone web app's Sessions list gets the iPhone app's swipe
  actions: right to attach a session, or to detach one you hold; left to
  pin, rename or fork it. A full swipe runs the first action, a drag that
  starts up or down still scrolls, and a long press, a right-click or the
  ⋯ button on a desktop browser shows them all in a sheet. An idle
  session detaches once the hub sees it idle, with Undo; a working one
  asks whether to detach when the turn ends, keep it running in tmux, or
  detach now. Pinned sessions sit in a Pinned section at the top. Fork
  asks for the new session's first message and opens its chat once it
  starts.

- **Remote PWA: queued messages wait in a tray above the prompt box,
  and stop lives in the send button.** The browser client now works like
  the iPhone app. A message sent while the agent works joins a Queued
  tray instead of a bubble that new output pushes off screen; each row
  has Edit (the text goes back into the prompt box and the button
  saves it in place), Remove, and Send now (written into the running
  turn), and the header removes them all after a second tap. A message
  leaves the tray for the transcript once it goes out, and the tray
  reloads from the hub when the chat opens or the page comes back. The
  Queue/Steer picker and the separate Stop button are gone: with the box
  empty and the agent working, the round send button is a red stop.

- **Web app: first-run setup for the hub machine.** `#/setup` ports the
  Mac app's setup sheet: pick the agents found on the hub machine (and
  star them), then link one AGENTS.md, skills folder and MCP list into
  each, through the same code as `swe setup --apply --json`, then pick
  a look. The page opens on a "Set up this machine" card while the hub
  has no shared AGENTS.md. The notifications step stays in the Mac app.
  Until then, Overview shows a Set up card, and opening a bare `/app/`
  goes to `#/setup` once per browser, as the Mac app offers its sheet
  once at launch.

- **Web app: remote control on the Sessions page.** Clicking a session
  opens its chat: the transcript with thinking, tool cards, diffs and
  compaction, live as the agent works, and for an attached session the
  composer (Queue or Steer, Stop), approval cards with Allow, Always and
  Deny, and Attach (after a confirm) for an observed one. It is the
  phone's chat on the hub's same routes, so the web app at the hub's
  address is the whole app; the old details sheet is the chat's Details
  button, and attached sessions carry an Attached badge in the list.

- **Web app: Doctor, and the Harnesses page's editor, Detect and Add.**
  `#/doctor` shows the `swe doctor` report as the Mac app's Settings ›
  Doctor does: each finding with its fix command, a copy button, and Run
  where swe can make the fix itself (`swe setup --apply`, `swe init`,
  `swe harness discover --apply`). Run asks first, saying what it will do
  on which machine; the hub runs the fix the finding names, never a
  command sent from the browser. On Harnesses, a click on a row opens the
  app's editor (name, command, description, version, aliases, tags,
  notes; an alias another harness uses is refused), Detect scans the hub
  for coding CLIs and registers the ticked ones, and Add harness adds a
  custom one without overwriting anything.

- **Web app: Settings, with remote pairing.** The Settings page from the
  Mac app, for the hub machine. Integrations › Remote shows the hub,
  the paired devices with "This device" marked and a Revoke that asks
  first (signing out the browser you're on needs its own confirm), and
  Pair a device, which mints a one-time code and shows the browser link
  and the `quiver://pair` app link as QR codes. The hub draws those
  itself with a small stdlib encoder (a port of Nayuki's MIT QR
  generator), since the web app loads no library. Appearance and which
  pages the sidebar lists are kept per browser; the API keys show
  masked and read only; the GitHub CLI and the hub's paths are listed;
  "Set up this machine" and Doctor link to their pages. Rows that set
  up the Mac app itself (Dock, login item, terminal, notifications,
  updates, Keychain services) are greyed out as "Mac app only".

- **Remote iPhone app: type @ to mention a file.** Typing `@` in the
  prompt box opens a list of files and folders from the session's
  working folder just above it, filtered as you type. Tapping a file
  puts `@path ` in the message; tapping a folder puts `@folder/` and
  lists what's inside. A space, a tap in the transcript, or deleting the
  `@` closes it. Sessions on a satellite say file search isn't available
  for that machine yet. A path with spaces goes in quoted,
  `@"My Documents/notes.txt"`, as Claude Code writes it. On iOS 17 the
  menu follows the end of the text rather than the caret.

- **Remote iPhone app: swipe a session to attach, detach, pin, rename or
  fork it.** Swipe right on a Sessions row to attach it, or to detach
  one you hold; swipe left to pin, rename or fork it. A full swipe runs
  the first action, and a long press shows them all in a menu. A
  session that looks idle detaches once the hub sees it idle, with Undo;
  a working one asks whether to detach when the turn ends, keep it
  running in tmux, or detach now.
  Pinned sessions sit in a Pinned section at the top. Fork asks for the
  new session's first message and opens its chat once it starts.

- **Remote iPhone app: type / for the session's commands.** A `/` at
  the start of the prompt box opens the commands this session's harness
  will run, from the hub's per-session list: each shows its name, a
  short description, any argument it takes and where it comes from
  (Built-in, Project, User, Skill, Plugin). Typing filters by name, and
  tapping one puts `/name ` (or a Codex skill's `$name `) in the box,
  ready for an argument. The list loads once per chat open and reloads
  on a pull. A harness with no commands says so, and a satellite
  session notes that its custom commands aren't available yet.

- **Web app: your own devices open it without pairing.** A browser on a
  device signed in to the hub owner's Tailscale account loads `/app/`
  straight away; the hub knows it from `tailscale whois` (cached a
  minute per address). Pairing stays for tagged nodes, other people's
  devices, phones' remote control and satellites.

- **Remote iPhone app: queued messages wait in a tray above the prompt
  box.** A message sent while the agent works no longer sits in the
  transcript, where new output pushed it off screen: it joins a tray
  pinned above the prompt box ("3 messages queued"). Each row has three
  icons: edit puts the text back in the prompt box to change it in place,
  remove drops it before the agent sees it, and send now writes it into
  the running turn. The header's remove button clears them all after
  asking. A message leaves the tray and lands in the transcript once it
  goes out. The tray reloads from the hub when you reopen the chat, and
  the Queue/Steer picker is gone. On a hub too old to edit, an edit
  removes the message and queues the new text at the end, and says so.

- **Remote: pin a session and give it a name.** `POST
  /v1/sessions/{key}/pin` and `/title` keep a pin and a display name on
  the hub, so the phone and the web app agree and watched or satellite
  sessions work too. Every session row gains `pinned`, `pinned_at` and
  `harness_title`, and `title` shows your name when you set one. Open
  clients hear about a change as a `session.meta` event.

- **Remote: fork a session from the phone.** `POST
  /v1/sessions/{id}/fork` starts a new run that continues from a
  session's history under tmux, leaves the original untouched, and is
  tracked like a new-session launch so the phone can open it. Claude
  Code, Codex, Kilo, Pi and Droid can fork, on the hub or a satellite;
  session rows carry `can_fork` so the app shows Fork only for those.

- **Remote: detach can wait for the turn to end or keep tmux running.**
  The control route's `detach` takes a `mode`. `now` is today's
  behaviour and stays the default. `when_idle` answers at once and
  detaches when the session stops working, unless an attach or another
  detach cancels it first. `keep` stops supervising but leaves the tmux
  session running for `tmux attach` on the Mac; attaching from the phone
  later takes that same tmux session back instead of starting a second
  run, and the hub closes a kept session nobody has used for 12 hours.
  Satellites support all three.

- **Remote: edit a queued message in place.** `POST
  /v1/sessions/{key}/queue` takes `op: "edit"` with a `text`, which
  replaces a waiting send's text without moving it in the queue and
  answers `200 {ok, client_id, op, text}`. An edit racing the send
  either lands before it goes out or answers `409 not_waiting`, so a
  `200` edit is never followed by the old text.

- **Remote iPhone app: links in replies get a preview card.** A reply
  that links to a web page shows a card under the text with the page's
  host, its path and an Open button (up to three per reply). Tapping the
  card, or the link in the text, opens the page in an in-app browser, so
  closing it brings you back to the chat.

- **Remote iPhone app: a Usage tab with limits and spend.** Each harness
  that reports a quota gets a card with its logo and a bar per window
  (5 h, weekly and so on) showing the percent used and when it resets,
  amber past 80% and red at 100%, with a quiet note when the reading is
  stale; satellites' harnesses group under their machine. Below it, cost
  and tokens for today, 7 or 30 days, a daily cost chart and each
  harness's share. Pull to refresh asks the hub for new readings.

- **Mac app DMG: a drag-to-install window.** Opening the DMG now shows
  Quiver on the left, Applications on the right and an arrow between
  them over a Quiver background, instead of a plain folder with LICENSE
  and THIRD_PARTY_NOTICES.md beside the app. Both files now ship inside
  the bundle, in `Contents/Resources`.

- **`swe hooks`: a management surface for hook scripts.** Hook scripts
  under `~/.quiver/hooks/<harness>/` were a first-class primitive with
  no way to see or edit them; `swe init` linked them invisibly and the
  only way to know one existed was to remember writing it. `swe hooks`
  (default `list`, `--harness=<h>`, `--json`) prints one row per script
  x destination with the `swe init` link state and a DECLARED column
  naming the event and matcher that invoke it, read from the harness's
  own settings file (`~/.claude/settings.json`, `~/.codex/hooks.json`,
  or `capabilities.hooks.settings`). `swe hooks info <name>` shows one
  script across harnesses — hub path, header docstring, every
  destination state, every declaration. `swe hooks edit <name>` opens
  the hub copy in `$VISUAL`/`$EDITOR` (default `vi`), never a symlinked
  destination, and asks (or takes `--harness`) when the name exists
  under several. `swe hooks new <harness> <name>` writes an executable
  fail-open Python skeleton and prints the exact `PreToolUse` settings
  stanza to paste — declaring stays user-owned, no settings file is
  ever written. `swe hooks check` reports the three ways the halves
  disagree — a declared command whose script is on disk nowhere, a
  linked script nothing declares, a hub script never linked — and
  `swe doctor` reports the same findings.

### Changed

- **MCP Add server: Local or Remote first, in both apps.** The sheet
  starts with "Local: runs on <machine>" (a command, args and env vars) or
  "Remote" (a URL, HTTP or SSE, and headers), in the grid's words, and
  shows only that kind's fields. Remote has Sign-in: None, a Bearer token
  (stored as a secret and sent as `Authorization: Bearer ${NAME}`), or
  OAuth (nothing saved; each harness signs in itself). A harness that
  can't take a remote server is left out with the reason. A local working
  folder isn't offered yet: the hub's server shape has no `cwd`.

- **MCP servers: grouped by Local and Remote, in both apps.** The grid's
  sections are now "Local: runs on <machine>", every server a harness
  launches with a command, and "Remote", every server at a URL. They used
  to split on whether a folder in `~/.quiver/mcp/servers` ran the server,
  which put an `npx` server under Remote; such a folder now shows as a
  "checkout" tag on its Local row. A server's sheet names its type
  ("Local · stdio", "Remote · http").

- **Mac app: Geist type and a pinned sidebar header.** The app's text is
  set in Geist and its code and IDs in Geist Mono, bundled with the app
  (SIL Open Font License) instead of the system font. AppKit text drawn
  or measured beside it (the Overview figures, chart labels, the
  instruction editor) uses the same faces through `Theme.nsFont`. The
  sidebar's top row now holds a "Quiver" wordmark with the collapse
  button at its right edge, and stays put while the pages scroll under
  it, with a hairline once they do. Collapsed, the button sits over the
  icon column as before.

### Fixed

- **MCP sync no longer writes remote servers into Claude Desktop.** Its
  config file launches local servers only (a remote one is added in
  Claude Desktop under Settings › Connectors), but `swe mcp sync` copied
  URL servers there too. Sync now leaves them out and says so ("1 remote
  left out"), with the names in a new `unsupported` list in its report;
  `swe mcp add --url --to=claude-desktop` is refused; and the web app and
  the Mac app no longer offer Claude Desktop as a target for a remote
  server.

- **`swe providers` read escaped keys with their backslashes.** The Mac
  app and the web app save a key as `export NAME="…"` with `"`, `\`, `$`
  and a backtick escaped, as a shell expects, but swe's own reader only
  stripped the quotes, so a key holding one of them came back with an
  extra backslash. It now reads double-quoted values as a shell does.

- **Web app: MCP secrets named like built-in object properties.** A
  `${constructor}` or `${__proto__}` in a server's definition showed
  JavaScript's own value in its paste field and counted as revealed before
  anything was fetched; the sheet keeps its values in maps now. A reveal
  that answers after the sheet closed is dropped too, so a reopened sheet
  shows nothing it forgot.

- **Web app and remote PWA: the Quiver icon replaces the "W"
  placeholder.** The favicon, home-screen icon, sidebar brand, setup
  screen and Pricing's Quiver logo showed a placeholder zigzag that read
  as a "W". `icon-192.png` and `icon-512.png` are now the Mac app's
  light icon (`app/Icons/AppIcon-light.png`), and the hub no longer
  serves `icon.svg`.

- **Web app: Calendar scrolls sideways across dates.** The day, week
  and N-day grids only moved with the arrow keys or the chevrons. They
  now slide like the Mac app's: a trackpad swipe, shift-scroll or a
  touch swipe moves through days, snapping to a day (a whole week in
  week mode), with the day header and hour axis pinned as the columns
  move. The hub sends a week or two of buffer days each side
  (`render_days`), and once a swipe comes to rest the page loads the
  range starting there and re-centres on it, keeping the hour. Keys and
  chevrons scroll the same grid one step at a time.

- **Models and Pricing: Devin's `MODEL_PRIVATE_11` shows as Claude
  Haiku 4.5.** Devin CLI logs that opaque id for Claude Haiku 4.5, so
  both apps listed it under "Other" with no logo or price. The price
  supplement now aliases it to `claude-haiku-4-5`, brand lookup follows
  the supplement's aliases before matching a name, and the Models and
  Pricing rows (web and Mac) show the real name with the logged id
  muted beside it. On the Mac, the id also matches its Artificial
  Analysis model through the alias.

- **Web app: Menu bar and Widgets say "Mac app only" and stay put.** The
  two sidebar items wear the same "Mac app only" pill as Settings' Mac-only
  rows and are no longer links, so neither a click nor Tab reaches them. A
  `#/menubar` or `#/widgets` link opens Overview instead of a placeholder
  page.

- **Remote iPhone app: the welcome screen says where a pair code comes
  from.** It told new users to run `swe remote pair --app` in a
  terminal on the hub. It now points to Quiver › Settings › Remote ›
  Pair a device on the hub machine (the Mac app or the hub's web app),
  with `swe remote setup` and `swe remote pair --app` as the fallback
  for a server with neither. The QR scanner's hint says the same.

- **`swe setup --apply` run twice no longer invents harnesses.** The
  second run linked skills into every harness in the `HARNESS_ROOTS`
  table, making `~/.augment`, `~/.kiro`, `~/.pi` and the rest to hold the
  links, and the linking pass then read those new folders as proof the
  harness was installed and registered and linked each one. The skills
  step now links only harnesses that are installed, by the rules
  `swe init` follows: the harness folder exists, or its command is on
  PATH (installed but never run). The web app's Setup page, Doctor's
  setup fix and the interactive wizard share this code, and the wizard,
  which never runs `swe init`, still links a never-run install. Folders
  an earlier run made are left in place; `rm` the ones for harnesses you
  don't use and archive their entries with `swe harness archive <name>`.

- **Remote: a refused request no longer breaks the next one on the same
  connection.** When the hub answered a POST before reading its body (a
  401, a bad Origin, an unknown session, a 409 `not_attached`, a 413),
  the body stayed on the keep-alive connection and was read as the start
  of the next request, which got a 400 or 501. The web app's Sessions
  chat and the PWA hit this after a refused send. Every response now
  discards an unread body first: up to 1 MB within 2 seconds, keeping
  the connection open, and anything larger, slower or sent chunked
  closes the connection with `Connection: close` instead.

- **Remote PWA: an open chat follows the session list's refresh.** The
  catalog reload (when the page comes back into view, or after a row
  action) updated the row but not the open chat, so a session that
  started working while the page was hidden kept a dimmed send button
  instead of the red stop, and an ownership change kept the old banner,
  until the next event. The chat now redraws its banner, prompt box and
  send button from the fresh row; an edit in progress keeps its
  placeholder.

- **Remote: a session's status follows a send within a second.** The
  hub re-read every session's status only on its 10 second catalog
  refresh, so after a send the phone could show the session idle while
  the reply was already streaming, and showed it working for up to 10
  seconds after the turn ended. Now a session that was just written to,
  or whose transcript grew, has its own status checked about once a
  second, and the change goes to the phone at once. An older catalog
  read can no longer flip it back. Satellites do the same.

- **Remote: a malformed write body gets an answer.** A list or object
  as a message's `mode`, an approval's `decision` or a control `op` made
  the hub drop the connection with no response. It now answers `422
  bad_command`, and only a missing, null or empty `mode` defaults to
  `queue`.

- **Remote: a send from the phone shows up within a second.** The hub
  used to poll an idle session's transcript only every 8 seconds, so the
  message, the agent starting and its first reply could lag that long
  after the send had already reached the session. Now every message,
  approval answer, interrupt or attach wakes that session's tailer at
  once and polls it every quarter second for the next 15 seconds, on the
  hub and on satellites alike. Devin and other stores that are re-read
  whole stay at once a second.

- **Remote web app: chats stream in instead of landing in blocks.** The
  browser client gets the iPhone app's treatment: messages that arrive
  together show one at a time with a short gap (a replay after
  reconnecting still lands at once), new messages fade and slide in
  (fade only with reduced motion), and three pulsing dots under the
  last message show the agent is working or a send is waiting.

- **Remote iPhone app: a delivered message stops saying "Sending".** A
  send settled only when the transcript echoed it back as a user
  message, so a slash command such as `/rename`, which never appears as
  one, kept "Sending" under it forever. The machine's confirmation now
  settles it. A message steered into a running turn says the agent
  reads it at its next step, since Claude Code takes mid-turn input
  between tool calls.

- **Remote iPhone app: the working indicator shows which agent is working.**
  While a session runs, the bar under the last message shows the
  harness's logo with the pulsing dots beside it, and new messages land
  above it.

- **Remote iPhone app: stop lives in the send button.** The separate Stop
  button above the prompt box is gone. With nothing typed while the agent
  works, the round button beside the box turns red with a white square
  and interrupts the turn; once you type it sends again.

- **Remote iPhone app: chats stream in instead of landing in blocks.**
  Messages that reach the phone together are shown one at a time with a
  short gap (a replay after reconnecting still lands at once), new
  messages fade and slide in, and three pulsing dots under the last
  message show the agent is working or a send is waiting for its reply.

- **Web app: one broken page no longer takes the others down.** Each page
  script in `/app/pages.js` is compiled and run on its own, so a page
  that fails to parse or throws while loading is logged in the console
  and skipped; the pages around it still load.

- **Accounts: "Not signed in" and "Couldn't fetch" instead of "No reading
  yet".** A starred harness with no usage reading now says why. swe
  checks for the credential its fetcher reads after an empty fetch, and
  `swe list --usage --json` and `/v1/usage` rows carry it as
  `no_reading` (`signed_out` or `error`). The web app's Accounts page
  names the machine and the login command (`codex login`, `cursor-agent
  login`, ...), and the Mac app's swe-fed cards say "Not signed in" or
  "Couldn't fetch usage" too, extra accounts included.
- **Cursor usage on Linux.** swe now reads the session `cursor-agent`
  keeps in `~/.config/cursor/auth.json`, so a machine signed in only
  through the CLI shows Cursor's limits.

### Added

- **Web app: merge a PR and mark notifications read.** The PR sheet on
  Repositories now has the Mac app's strategy menu, Merge and
  Auto-merge; either asks an inline confirm naming the PR, its repo, the
  base branch and the strategy before it posts. The hub reads the PR
  again first and refuses what the app would not offer (not open, draft,
  conflicts, auto-merge already armed) in the app's words. Inbox gets a
  Mark as read button per unread row and the header's mark-all envelope,
  behind an inline confirm. Both call the same code as `swe gh pr merge`
  and `swe gh inbox read`, through the hub machine's `gh` login, and
  drop the page's cached replies after the write.

- **Web app: link and unlink harness skills folders.** The Skills page's
  Harness links popover now does what the Mac app's does: a folder opens
  with Link to shared skills or Unlink, and Link all (N) links every loose
  folder at once, through the same functions as `swe skills link|unlink`.
  Each asks first and says exactly what will happen (a folder of shared
  copies is backed up before it is replaced). A folder holding skills that
  exist nowhere else, a file in the way, or one that changed since the
  page read it is refused with the reason; Link all leaves archived
  harnesses alone.

- **Web app: Sync on the MCP servers page.** A server's sheet copies it
  from one harness (or the Quiver hub) to the harnesses you switch on, as
  the Mac app's Sync does: Preview shows, per harness, the file it writes,
  the command line it gets and the env var and header names it gains,
  loses or changes (values never leave the hub), and Sync asks inline,
  naming every file, before writing exactly the previewed plan. It runs
  `swe mcp sync`'s own code in process, refuses with 409 if any config
  changed since the preview, and refuses a hub server whose `${NAME}` has
  no value rather than writing it literally.

- **Web app: Fix and Fix all on the Plugins page's Health tab.** Each
  finding swe can fix has a Fix button that opens an inline confirm with
  the exact command it will run; Fix all first lists every finding and
  command it will run. The hub runs them as `swe find plugins --fix=<id>`
  does, one at a time in report order, stopping at the first failure,
  and only for fix ids its own current findings list: the browser never
  sends a command. The cards repaint from the fresh findings the hub
  answers with.

- **Remote: list, send now or remove a queued message.** `GET
  /v1/sessions/{key}/queue` lists a session's queue-mode sends still
  waiting for the agent to go idle (`client_id`, `text`, `attachments`,
  `queued_at`), so the phone can show them after it reopens. `POST
  /v1/sessions/{key}/queue {client_id, op}` with `steer` sends one now
  as a steer, and with `cancel` drops it and reports a `command.result`
  with `cancelled: true` and the note "removed before sending". A send
  that already went out is `409 not_waiting`, an unknown one `404
  unknown_send`, and a cancel racing the send never both sends and
  removes it. Satellite sessions never hold queued sends on the hub, so
  their list is empty and the actions answer `409 queue_unsupported`.

- **Remote hub: the slash commands each session can run.**
  `GET /v1/sessions/{id}/commands` lists what typing "/" in the phone's
  prompt box should offer: the harness's built-ins plus the user's and
  the project's own commands, skills and plugin commands, each with a
  one-line description and where it came from. The list only holds what
  will work the way the phone reaches that session, so a session driven
  through print mode or a server API doesn't offer TUI-only commands,
  and commands that would open a picker on the computer are left out.
  Claude Code, Codex and opencode are covered, satellites included.

- **Remote: the hub lists a session's files for `@` mentions.**
  `GET /v1/sessions/{id}/files?q=` returns files and folders under the
  session's working folder, ranked for a typeahead (file name matches
  first, then shorter and more recently changed paths), so the phone's
  prompt box can offer `@path` as you type. Git repos are listed with
  their `.gitignore` applied; other folders skip hidden and build folders.
  Nothing outside the working folder is ever listed, symlinks included.
  Sessions on a satellite machine get a clear "not available yet" error.

- **Web app: Repositories and Inbox.** Repositories shows the hub's pinned
  repos as the Mac app does: pull requests, issues and Actions across
  every pin, all seven surfaces (Pages, releases, tags, branches too) for
  one repo picked in the rail, the rail's CI dots, counts and quiet fold,
  and the search over titles, branches and state words. A repo opens in
  full at `#/repositories?repo=owner/name` (every state, scroll to load
  more) and a PR row opens its sheet: checks, reviews and the body as
  markdown. Inbox lists the GitHub notifications with the Unread only
  switch. Both read through the hub machine's own `gh` login, cache
  replies for a minute, and are read only; merging, re-running, marking
  read and pinning stay in `swe gh` and the Mac app.

- **Web app: Skills and AGENTS.md.** Skills lists every skill on the hub
  machine outside a plugin, with the Mac app's Harness and Source filters,
  search, order and fold, the Harness links summary with a chip per skills
  folder, and a detail page per skill: source, link chips, frontmatter,
  SKILL.md and its files. AGENTS.md shows the shared file with each
  harness's link state from `swe init --check`, and a project folder's
  AGENTS.md and CLAUDE.md. Both are read only; linking, New skill and
  editing stay in `swe` and the Mac app. `swe init --check --json` is now
  built by `init.commands.check_document`, which the page calls in process.


- **Web app: star, archive and restore harnesses.** The Harnesses page
  can now change a harness's state, the web app's first writes: a star
  button on each row, Archive with an inline form that needs a reason,
  and Restore on archived rows. They post to `/v1/app/harness/<action>`
  (device bearer and Origin check), which writes through the same
  registry functions as `swe harness star|archive`, one at a time. The
  row changes when the hub answers; a refusal shows in an error card.

- **Web app: Compute.** What the starred harnesses run on the hub
  machine and what it costs it, read from Linux's `/proc` and `/sys`
  (and `nvidia-smi` when present): the totals line, the hub's CPU,
  memory, load and GPU, the RAM / CPU / GPU / Temperature trend card
  with its interval picker, a card per running harness with its
  processes, the Orphaned card, and the thresholds behind the gear.
  Read only; quitting processes and fan control stay in the Mac app.
  A non-Linux hub says the page needs Linux. docs/web-app/compute.md
  has the sources and the differences.

- **Web app: Service status.** The Mac app's Service status page in the
  browser: a card per status page (Claude, OpenAI / Codex, GitHub /
  Copilot, Cursor, Gemini, Devin, npm, Vercel, Cloudflare) with its
  health, and behind "Show details" the outage summary, affected
  components and open incidents with their latest update. The Swift
  status code is ported to a stdlib `quiver.status` package (catalog,
  Statuspage and Google Cloud parsers, poller): each page is asked at most
  every five minutes (one for Refresh), with ETags and `Retry-After`
  honoured, a per-request timeout, and readings cached in
  `~/.quiver/cache/web-service-status.json`. Read only; pages added in the
  Mac app stay there.

- **Web app: Pricing and Models.** Pricing lists every model the hub's
  ledger saw in the period, grouped by provider, with the price its name
  resolves to per 1M tokens (input, output, cache read and write), where
  that price came from (LiteLLM, Quiver, your own, free, billed by the
  harness, or none), the charged tiers and the tokens, plus a list of
  unpriced models; filter by harness and period. Models lists the models
  you used with their lab, harnesses, list price and tokens, and searches
  LiteLLM's price list. Read only: setting a price, and every Artificial
  Analysis benchmark (which needs the app's API key), stay in the Mac app.
  docs/web-app/pricing.md and models.md have the details.

- **Web app: Plugins.** The Mac app's Plugins page in the browser, read
  only. Health lists, per harness, where Claude Code and Codex no longer
  match `~/.quiver/plugins`, each finding with the fix swe would run and a
  copy button. Explore lists every plugin in every marketplace under Local,
  each harness and Not registered, with a harness filter, search across
  names, descriptions and shipped skills, per-harness state chips, drift
  badges and a detail sheet. The catalog is cached on the hub for a
  minute; Refresh reads it again. Fixes, installs, enables and Add
  marketplace stay in `swe` and the Mac app.
- **Web app: Calendar.** When each harness was working on the hub
  machine, as the Mac app draws it: blocks of activity per project (a
  session's responses join until 30 minutes of silence, and sessions of
  one repository merge under the same gap; every Grok Bot session is one
  "Grok Bot" project) on a Day, Week, N-day or Month grid in the
  browser's time zone, coloured by harness, with the mini month, the
  harness and account legend with time totals, and the same keys (D, W,
  M, 1 to 9, T, S, arrows). A block's hover card and session list add
  its tokens and spend; a session opens the Sessions page's detail.
  Read only, from the shared ledger.

- **Web app: MCP servers.** The Mac app's MCP servers page in the
  browser, read only: every server the hub machine's harnesses configure
  as a server × harness grid, split into Local (run from a folder in
  `~/.quiver/mcp/servers`, with its git branch and changes) and Remote,
  with the All / Out of sync / Conflicts tabs, Compare chips, lettered
  definitions where harnesses disagree, and a sheet per server with its
  transport, a redacted command line or URL, env and header key names,
  which harnesses hold which definition and the `swe mcp sync` command
  that would even them out. "Check health" runs `swe mcp status`'s probe
  on request only, in parallel under a 30-second cap. Add, Sync, Remove
  and secret values stay in `swe` and the Mac app.

- **Web app: pages as plugins.** A web app page is now one module in
  `quiver/webapp/pages/` (`GET /v1/app/<page>`, optional `POST
  /v1/app/<page>/<action>`) and one script in `static/pages/`, served
  together as `/app/pages.js`, so new pages are built side by side
  without editing a shared file. The sidebar marks Menu bar and Widgets
  as Mac only. docs/web-app.md, "Adding a page", has the layout.

- **Remote: search ranks by what matched, then by how recent.** A
  session whose title holds the query comes first, then one whose folder,
  harness or id does, then transcript matches (the exact phrase, then all
  the words, then any word). Inside each tier a match counts less the
  older the session is (half the decaying part every 14 days, never
  below 0.3 of full). A phrase match no longer hides sessions that have
  the words apart, and a title match no longer sits behind fifty passing
  mentions. The iPhone app's offline fallback ranks the same way.

- **Overview: the harness logos load as a pill while the marbles pour.**
  On the first pour after launch the logo row is a pill that fills in each
  harness's colour as its marbles land, left to right in the logos' order.
  Once every marble is down each colour springs into its logo's circle and
  the logos fade in; hovering works from then on. A wrapped logo row fades
  across instead, and Reduce motion shows the logos straight away.

- **Web app: Harnesses and Accounts.** Harnesses lists every harness swe
  knows on the hub machine, with the Mac app's In use / Archived scopes,
  search, starred-first order, a fold for the unstarred ones, version,
  command, aliases, tags, the archive reason, PATH state and 100-day
  session counts. Accounts shows a card per starred harness for the hub
  and each paired machine: plan badge, a bar per limit window (green,
  amber from 70%, red from 90%) with its reset time, on-demand spend,
  extra usage balance, and when the reading was taken. Both are read
  only; starring and archiving stay in `swe` and the Mac app.

- **Web app: Sessions.** The web app's second page lists every session the
  hub knows (this machine and its satellites) with tokens and spend from
  the web ledger, the Mac app's search and its tool, time (90 days by
  default), project and model filters, a matching sessions / tokens / spend
  strip, rows grouped by day (10 a day, then "Show every session"), and
  short or empty sessions hidden behind a footer toggle. A row opens a
  sheet with its folder, models, token split and a copyable resume
  command.

- **Remote: the new-session sheet finds folders and offers every active
  harness.** Machine, harness, prompt and Start now sit above the folder
  list, which starts folded to four, with Show all and a search field at
  its top. Browse walks the hub machine's folders (`GET /v1/folders`) to
  pick one no session has used yet. The harness row follows the Mac
  app's Settings › Provider visibility: every harness `swe list` shows
  whose command is on PATH, in that order, minus the ones switched off,
  in place of a fixed five. Launches run the registry command (`cursor`
  starts `cursor-agent`), sessions kept in databases are adopted by
  activity time, and the first prompt waits for a TUI's splash to settle
  and is typed again if it was dropped.

- **Web app: the Mac app's Overview in any browser.** The remote hub
  serves a web version of the Mac app at `/app/`, with the same sidebar
  and look, starting with Overview: total tokens and spend per period,
  the harness split, a daily breakdown, quick figures, top models and
  the activity heatmap, for the machine the hub runs on. It reads a new
  Python port of the app's usage ledger (`quiver.ledger`: the same dedup
  keys, Codex total handling and LiteLLM pricing) kept in
  `~/.quiver/data/web-ledger/`, apart from the app's own ledger. A
  browser paired with the hub's PWA is already signed in. The native app
  is unchanged; docs/web-app.md has the plan for the remaining pages.

- **Remote: the hub runs behind `tailscale serve`.** `swe remote serve
  --public-url=https://box.tailnet.ts.net:8477` binds `127.0.0.1` and
  reads the tailnet client from the headers serve sets, so "Pair with
  Tailscale", owner approval from a browser and the pair-request rate
  limit work over https. `swe remote pair` prints the https URL (the
  iOS deep link carries `scheme=https`), `swe remote status` probes the
  loopback bind, ntfy buttons point at the public URL, and the
  satellite agent verifies an https hub against the OS CA bundle when
  its Python ships none.

- **Remote: start a session from the iPhone app.** Sessions gets a plus
  button that opens a new-session sheet: pick a machine, one of its
  recent folders, a harness by its logo, and a first prompt, and the
  chat opens once the session shows up. `POST /v1/sessions` now accepts
  only harnesses it can launch and adopt (Claude Code, Codex, Cursor,
  Droid, Pi), checks the folder and requires a prompt, and answers a
  `launch_id`; `GET /v1/launches/<id>` names the new session once the
  hub or satellite adopts it. Machine rows list `launchable` harnesses.
  A first prompt whose Enter Claude's TUI swallowed is now pressed
  again instead of sitting unsent in the input box.

- **Remote: the hub survives a restart.** `swe remote serve --install`
  runs the hub as a macOS LaunchAgent that starts at login and restarts
  it whenever it exits; `--uninstall` removes it. Under launchd the hub
  waits for Tailscale instead of binding localhost, and `serve.log` now
  has timestamped start and stop lines. A hub started with `nohup` used
  to die on every reboot and stay down until someone noticed. The agent
  runs Quiver.app's bundled `swe`, since macOS blocks LaunchAgents from
  reading a checkout in ~/Desktop; `--install` checks that the hub came
  up, and `install-app.sh` restarts it on the new bundle.

- **Remote: satellites report usage, so a MacBook's quota shows on the
  hub.** `swe remote agent` posts the machine's `swe list --usage` rows
  every 5 minutes, and the hub keeps each machine's last report. The
  PWA's Usage tab gets a machine switcher, and a satellite that missed
  two reports says how old its figures are. The hub and agent also pin
  the 300 s rate-limit TTL, so a `SWE_RATE_LIMITS_TTL=inf` in the shell
  that started them no longer freezes the page.

- **Remote: a Usage tab in the PWA.** The hub serves `GET /v1/usage`,
  the same rows as `swe list --usage --json` for the hub machine: quota
  bars per window for every starred harness (with reset times, credits,
  extra usage and on-demand spend where the provider reports them) and
  sessions per harness over the last 100 days. Refresh retries failed
  readings and ones older than a minute, never more. Satellite machines
  don't report usage yet.

- **Overview: the totals sit over a pile of marbles.** The share bar and
  harness tiles give way to marbles that drop in from the top of the card
  on the first launch, one per round amount of spend ($1, $2, $5, $10 and
  so on, picked per period so the pile fills about half the card whatever
  you spend), each in its harness's colour with its logo, shaded and
  textured like glass. Marbles behind the figures fade so the text stays
  readable. Hovering a harness logo under the figures leaves only that
  harness's marbles and rolls the figures over to its tokens, spend, share
  and models. A new period or a rescan only drops in or fades out the
  difference. With Reduce motion on, marbles fade in on top of the pile
  instead of falling.

- **Repositories: state as icons, search instead of chips.** A PR row's
  conflicts, review and CI show as icons only, centred in the row, with
  the meaning on hover ("Ready to merge", "Merge conflicts"); running CI
  spins. The Failing CI, Drafts and Hide bots chips are gone: the search
  field on the tab row (⌘K) matches state words such as "failing",
  "draft", "conflict", "approved" or "bot". It replaces the jump dialog,
  since Add repository already searches your repos and orgs. Repo headers
  are flat strips, with only the first taking the box's rounded top.

- **Repositories: the rail runs the full height and holds the controls.**
  The header buttons move into the page: the manage gear and Refresh (⌘R)
  sit beside the rail's filter, "Add repository" is the rail's last row,
  and the jump search (⌘K) rides the right end of the tab row.

- **Repositories: one list beside a repo rail.** The page drops the card
  per repo for a single list of rows grouped under a plain header per repo,
  and a rail on the right lists the pins by owner with a CI dot and the
  tab's row count. Repos with nothing on the tab fold under "N quiet" and
  leave the list. A tap narrows the list to one repo, which also opens the
  Pages, Releases, Tags and Branches tabs; across all repos the tabs are
  Pull requests, Issues and Actions. Dragging a repo in the rail reorders
  the pins (within its owner), and the list follows. Chips filter by
  failing CI, drafts, and dependency bots (Devin's PRs stay).

- **⌘1 to ⌘9 switch Quiver's sidebar pages.** The digits follow the
  pages the sidebar shows, top to bottom, so a hidden page gives its number
  to the next one. Holding ⌘ shows a `⌘1` style key cap on each numbered
  row, lined up in one column at the sidebar's right edge (a digit on the
  icon when the sidebar is a rail). The shortcuts also sit in the View menu.

- **The remote hub's inbox stops growing forever.** `GET /v1/inbox` drops
  follow-ups, errors and interrupts whose last activity is over 3 days old
  (pending approvals never age out), so a phone's badge no longer counts
  every session that ever asked a question. A new `POST /v1/inbox/dismiss`
  (`{key, last_active_at}`) clears a row for the calling device only,
  stored per device in `~/.quiver/data/remote/inbox_dismissals.json`
  (0600); the row comes back once the session has new activity.
  `?done=1` adds sessions that finished today as `done` items.

- **`swe session` stops re-reading unchanged transcripts.** A per-file parse
  cache (`~/.quiver/cache/session_parse_cache.json`) keeps what each
  transcript said, keyed by path and stat signature, so a listing re-reads
  only files that changed. Sidecars (codex session index and thread store,
  cursor `meta.json`, freebuff `run-state.json`) are still honoured on every
  run, and any code change discards the cache. The status probes and
  transcript lookups also stop globbing the whole harness root once per
  session. On a machine with 1,143 sessions, `swe session 100000 --json`
  went from about 2.8 s to 1.7 s cold and 0.55 s warm.

- **The `swe session` status column stops re-reading unchanged tails.**
  What a transcript's tail decides (its phase, and whether the last reply
  asks the user something) is cached per file in
  `~/.quiver/cache/session_status_cache.json` under the same stat
  signature as the parse cache. The clock, live pids and background-job
  states still apply on every call. On 1,145 sessions the status column
  went from about 210 ms to 22 ms, and a warm `swe session 100000 --json`
  from about 0.49 s to 0.30 s.

- **Pair a phone by Tailscale identity, no QR needed.** The PWA's pair
  button posts `/v1/pair/request`; the hub `tailscale whois`es the peer
  and shows a 6-digit code the phone displays while it polls. A second
  device — an already-paired one, another Mac sharing the hub owner's
  tailnet login via `#approve`, or `swe remote devices
  --requests/--approve=<code>/ --deny=<code>` on the hub — decides it.
  Self-approval is refused, five wrong codes deny the request, and the
  credential is delivered to the poller exactly once. The QR/token flow
  stays as the fallback.

- **First-run setup in the app.** A new Mac with no `~/.quiver/AGENTS.md`
  opens a four-step window. First, tick the coding agents you use: the ones
  found on the Mac come first, and "Show more agents" lists the rest, with
  install commands for one you pick before installing it. Second, one
  shared AGENTS.md, skills folder and MCP server list, started from your
  own CLAUDE.md or AGENTS.md if you have one. Third, light, dark or system
  look. Fourth, limit notifications, with Allow and a test notification.
  Step two is one `swe setup --apply` call. Skip closes the window for
  good; Settings › General › First-run setup opens it again.

- **`swe setup --apply` sets up a blank Mac in one go.** After registering
  harnesses, importing MCP servers and linking skills roots, it now builds
  `~/.quiver` (seeded `AGENTS.md`, `.linkignore`, `.gitignore`) and links
  the shared `AGENTS.md`, skills and hooks into every harness, as `swe
  init` does. It asks nothing: a real file in the way stays as it is, and
  a new skills folder is registered only when its owner has a command or
  catalog entry. Two new flags serve the app's first-run setup.
  `--adopt=<file>` starts the shared `AGENTS.md` from one of your own
  instruction files (only while it is still the seed). `--starred=a,b`
  makes those harnesses exactly the starred set, registering catalog ones
  you haven't installed yet. `swe setup --json` adds an `instructions`
  preview with the files `--adopt` could take.

- **`swe init` links a file that already matches the shared copy.** A real
  instruction file whose text is identical to `~/.quiver/AGENTS.md` is now
  `absorb` (backed up, then linked) instead of a conflict that needs
  `--force`.

- **Coding plans in Settings › Providers › Inference.** 35 subscription
  plans (Ark, GLM, Kimi Code, MiniMax, Qwen, Step, MiMo, Tencent, JD Cloud,
  iFlytek, Huawei Cloud, Synthetic, Ollama Cloud, Vercel Token Pass and more)
  sit in one list with your API keys. Click a plan to paste its key: Quiver
  saves it to `~/.api_keys` under the plan's own variable and shows where the
  plan is served and where to get a key. Plan keys are checked against the
  plan's own endpoint. The list filters by status (valid, invalid, not
  checked, no key) and type (coding plan, API key), drags into your own
  order, and opens a row on click; reveal and copy sit beside each value.
  Sources and the plans left out are in `docs/coding-plans.md`.

- **`swe install --json` says how to uninstall each installed harness.**
  Every entry carries `uninstall`: `{"via", "package", "command",
  "available"}` read off where the binary resolves to (a Homebrew Caskroom
  or Cellar, an npm `node_modules`, a uv tool, the Nix store), or null when
  it isn't installed or a vendor script put it there. Quiver's Settings ›
  Providers › Harnesses shows it when you click an installed harness, and
  the install commands when you click a missing one; the Harnesses page's
  Add harness button is gone. The same section gains an Inference tab with
  the Ark plans (no longer listed as harnesses) and the API keys editor,
  which leaves the sidebar.

- **`swe harness usage-daily codex [--account-dir=<dir>] [--json]` reads a
  Codex account's per-day token usage.** It spawns `codex app-server` with
  `CODEX_HOME` at the account's home (`~/.codex` by default), runs the
  `account/usage/read` JSON-RPC call under a 30s cap, and reports
  `{"status", "summary", "days"}`: `ok` with lifetime/peak/streaks and
  `{"date", "tokens"}` days, `auth-required` when the account is not
  signed in with ChatGPT, `unsupported` when the codex build lacks the
  method, `unavailable` for a missing binary, timeout or other failure.
- **Hide accidental sessions in Quiver.** Sessions with no responses, no
  tokens, or a one-shot probe (≤ 2 responses inside a minute with under
  100 output tokens, judged on the session's whole stored history) drop
  out of the Sessions page and the calendar. Cursor, Droid and Cline
  logs carry no per-reply detail, so only empty sessions of theirs hide;
  harnesses without usage logs (Gemini, Freebuff, Kilo) never hide. The
  Sessions page footer counts what was hidden with a "Show" toggle, and
  Settings > Preferences > "Hide accidental sessions" (default on)
  switches both filters off.

- **Settings > Services in Quiver.** Keys for the services Quiver itself
  calls — Devin's API token and org id, and an Artificial Analysis key —
  live in the login keychain under `com.cwenlong.quiver.services`, read
  and written through `/usr/bin/security` so a rebuild never re-prompts.
  Only when each key was added and last used goes to
  `~/.quiver/config/services.json`; values never leave the keychain.
  Each service has a Test button: Devin's calls `v3/self` for free,
  Artificial Analysis spends one of its 1,000 daily requests on the
  models list.

- **Models page in Quiver.** A new Primitives page benchmarks the models
  the ledger saw against Artificial Analysis's leaderboard: a provider-
  coloured bar chart or table over Intelligence, Coding, Maths, Speed,
  time to first token and cost, scoped to your models plus the top ten,
  your models only, or the top 25. Data lands in
  `~/.quiver/data/artificial-analysis/` (raw responses for all six
  endpoints, refreshed on demand or when the models file is a week old);
  a failed fetch never clobbers what's stored. Logged names match AA
  entries by normalised base plus effort, and `model-links.json` can pin
  a match by hand from the page's ⚠︎ popover. Hovering a model here, in
  Overview's top models or in Pricing opens a card with its cost rank
  against the whole list and the headline benchmarks. The sidebar's
  Manage group is now Accounts, API keys, Harnesses, Compute.

- **`swe harness usage <devin|cursor> --account-dir=<dir>` reads an extra
  account's rate limit from its own files.** Devin resolves to
  `<dir>/devin/credentials.toml` (the CLI's `XDG_DATA_HOME`), Cursor to
  `<dir>/User/globalStorage/state.vscdb` (the app's `--user-data-dir`),
  and never touches the default account's keychain entry or the shared
  rate-limit cache. `--json` emits `{"name", "account_dir", "limit"}` where
  `limit` is the same object `swe list --usage --json` gives a row's
  `rate_limit` (null when the account is signed out); human output lists
  each window like the list's usage cells.

- **Calendar page in Quiver.** A week (or day) view of when each harness
  was actually working, built from the usage ledger's response
  timestamps: a session's responses join into a block until 15 quiet
  minutes pass, and blocks sit side-by-side when they overlap. The left
  pane's mini month jumps the grid and its legend toggles whole
  harnesses or single accounts, each with its total time for the days on
  show; a block opens the session's detail sheet.

- **Extra Devin and Cursor accounts in Quiver.** The Add account sheet
  now offers Devin and Cursor: each gets its own folders under
  `~/.quiver/accounts`, opens its app isolated (`open -n` with the
  account's `--user-data-dir`; Devin also exports `XDG_DATA_HOME`), and
  shows up as a card under its starred harness with usage read by
  `swe harness usage --account-dir`. Cursor's `cursor-agent` keeps one
  sign-in per Mac, so a Cursor account's Terminal runs the default
  account and Sign in opens its isolated Cursor app instead.

- **An "Add harness" sheet on the Harnesses page.** The plus button opens
  swe's installer catalog minus what's already registered: harnesses on
  PATH get a "Register" button (`swe add`), the rest expand to show every
  package manager's install command (brew, cask, npm, uv, nix, script) to
  copy or run in the terminal. Quiver never picks a manager for you. To
  feed it, `swe install --json` entries now carry `description` and
  `registered`; the sheet degrades cleanly against an older swe.

- **Fan control in the menu bar popup's Compute tab.** The header's
  third button is now the tab's own: Usage keeps Refresh, and Compute
  gets the fan button instead (its charts tick on their own, so a reload
  bought nothing). The button opens a compressed take on the Compute
  page's fan popover — one line per fan, the bar filling the gap to the
  rpm, the Mac's temperatures as icons, and full-width Automatic and
  Manual controls. Automatic offers a "Fans up above" dropdown for the
  above-temperature rule, the same as the page; Manual keeps the
  speed slider. Choosing Automatic hands the fans back to macOS. Clicks
  inside the popover no longer read as outside clicks that close the
  panel.

### Changed

- **Settings: a shorter sub-nav.** Preferences and Updates fold into
  General as its Display and Updates cards, and GitHub and Remote share one
  Integrations page under Advanced. The list goes from 12 items to 9.

### Fixed

- **OpenCode 2.x sessions show up again.** OpenCode 2.x writes new
  sessions only to `session_v2` and their turns only to
  `session_message`, so every session made since the upgrade was missing
  from `swe session`, search, status, transcripts, the phone app and
  usage. swe and the Mac app now read both stores, each session and
  reply once.

- **Quiver switches pages at once.** Clicking a sidebar row or pressing
  ⌘1 to ⌘9 used to keep the old page up for 70 to 460 ms while the new
  page's rows were laid out. The new page's header now draws in the first
  frame (17 to 49 ms) and its content joins on the next run-loop turn.

- **`swe session` lists OpenCode sessions again.** opencode 2.x turned its
  `workspace` table into provider bindings with no `directory` column, so
  the session query's `workspace` join failed with "no such column:
  w.directory" and every OpenCode row silently vanished. The query now
  checks `workspace`'s columns first and joins it only as a path fallback
  when `directory` is there (older opencode, Kilo today).

- **Quiver opens the Sessions page faster.** On a 2,450-session history a
  switch to Sessions held the main thread for about 155 ms; it now takes
  about 90 ms. The page used to filter every session several times per
  redraw and reread the ledger on every visit. It now keeps the filtered
  list, totals, day groups and filter choices until the sessions or
  filters change, and rereads usage only when the ledger changed.

- **Phone sends return immediately instead of hanging for the turn.**
  `POST /v1/sessions/{key}/message` now answers `202` and runs the send
  on a per-session worker: `queue` mode waits for the session to go idle
  (up to 30 min) before injecting, `steer` writes at once, and the
  outcome lands as a `command.result` event carrying the caller's
  `client_id`. The PWA draws an optimistic bubble, keeps the composer
  visible (disabled) on observed sessions, and treats queued attaches as
  success. Attaching a claude session with no tmux on PATH now resolves
  to the headless channel instead of failing.

- **A board load no longer crashes the app on the GitHub pages.** The
  Pages surface's persist closure — a literal formed on the main actor
  and handed to `quiver.gh-history` — inherited `@MainActor` isolation,
  and the runtime's queue assert trapped the moment the write queue
  called it. Repos with a Pages site hit it on their first board
  refresh. The persist parameters are `@Sendable` now, which keeps the
  closures nonisolated so the write queue can run them.

- **Resumed Codex threads are one session again.** Every `codex resume`
  writes another `rollout-<iso>-<thread>_<rollout>.jsonl`, and the parser
  keyed each file on the trailing uuid — the resume's own rollout id, which
  the session index, the thread store and `codex resume` have never heard
  of. The thread uuid is the first one, so `swe session` now folds every
  resume into the thread's single row (the newest file stands for it),
  emits a resume hint `codex resume` can resolve, and finds the thread's
  real name. Index records written after the rollout's own timestamp no
  longer feed the rename check, and a resume that only re-states the live
  name can't stretch its rename window — resumes stopped reading as renames.

- **Quiver's main window goes fullscreen.** A SwiftUI `Window` scene
  marks its NSWindow `.fullScreenNone`, so Enter Full Screen stayed off
  the green button's tiles and ⌃⌘F even though the window resized. The
  scene's content now opts in with `.windowFullScreenBehavior(.enabled)`.

- **Quiver's Pricing and Models pages open without the wait.** Their
  per-model usage read now runs on its own read-only ledger connection,
  off the main thread, instead of queueing behind a log scan (up to 3s
  on a busy ledger). Pricing runs one load on open instead of three in
  a row. Models draws the benchmarks at once and shows a spinner in the
  token cells until usage lands, rather than "No usage in this period".
  Its row build matches every logged name through one parsed index
  instead of re-parsing the whole Artificial Analysis list per name,
  which froze the window for over a second.

- **Quiver's Pricing page lets you price harness-billed models.** Rows
  Cursor or another harness bills itself (it logs a charge per request)
  now open the price form like any other. A saved price fills the rate
  columns and prices any harness that logs no charge; that harness's own
  logged charge still sets its spend, as the form and the row's hover
  text say. Such a row with no rates reads "Harness" instead of "Billed
  by harness", which overflowed the Source column onto the cache write
  rate.

- **"Open app" on the Accounts page now focuses the account's own running
  instance.** Quiver tells desktop-app instances apart by their
  `--user-data-dir` argument, so opening the default account while another
  account's instance is running starts a new default instance (instead of
  bringing that account forward), and opening an account that is already
  running focuses it (instead of starting a second copy on the same data
  directory).

- **`swe install` no longer guesses npm package names.** It used to run
  `npm install -g <name>` with a small lookup table, which installed
  strangers' packages (`gemini`, `crush`, `kiro-cli` are unrelated on
  npm) or names that do not exist (`opencode`, `mistral-vibe`,
  `@cursor/agent`). Now `swe install <name>` lists the harness's verified
  install command per package manager (brew, cask, npm, uv, nix, vendor
  script), `swe install <name> --via=<method>` runs one, `--package=<pkg>`
  installs an explicit npm package, `--dry-run` prints the command, and
  `--json` emits the whole table without running. Names with no known
  installer are refused instead of guessed.

- **Renaming a harness account on the Accounts page no longer rewrites
  harness.json.** The name (say "Cortex" for your Cursor account) now lives
  in the app's own `~/.quiver/accounts/harness-names.json` and shows in
  Accounts, the menu bar and widgets only, so the Harnesses page keeps the
  harness's display name. The card's Open app/Terminal buttons are icons now.

- **A fresh Linux + Nix install bootstraps cleanly.** Six first-run bugs
  together made a new machine look broken: any invocation — even a failed
  unknown command — created `~/.quiver/config/harness.json` before it was
  asked for anything (a read now stays a read; `save_registry` is the one
  scaffolder), `swe init` skipped harnesses that had never run yet because
  a config dir was the only install evidence (a binary on PATH counts now,
  so Nix/uv installs like `crush` get their `~/.config/crush` links, hooks
  included), `swe doctor` reported npm's global bin as the bare prefix when
  `<prefix>/bin` did not exist yet (the POSIX convention is the answer,
  unconditionally), `swe list` and `swe check` disagreed about the same
  registry because archived rows counted in one and vanished in the other
  (both now print the active count plus the shelved one, and an empty
  registry is a deliberate empty state instead of a header over 0/0), and
  `swe discover --apply-all` archived a home-scan find like `hermes` even
  when its binary probed a live version (the probe result, not the source,
  decides now; a find the confidence floor refuses is named with its
  remedy instead of silently dropped). Old seeded registry rows are left
  alone on upgrade — nothing in the file tells them from hand-authored
  ones, so deleting them could erase real entries; `swe discover
  --apply-all` after a `swe remove` remains the way to reset them.

- **Setup and provider fixes.** `swe setup` stops the wizard at Ctrl-D
  (exit 130) instead of marking every remaining stage failed and exiting
  0; `swe setup --apply --json` reports a failed apply step inside the
  `applied.errors` list and still emits its JSON envelope, so a run that
  half-applied no longer reads as success to the app. A failed
  instructions preview (`instructions.error`) exits 1 with a stderr line
  instead of looking like a clean run. `swe providers` messages name the
  real key store: a missing default store asks for one `export
  NAME=<key>` line per provider rather than a file-per-provider
  directory, and `swe providers remove` names the export line it left
  alone — the one the file actually holds, or every accepted name.
  `swe providers add` refuses a flag token as the provider name, and a
  `providers.json` entry that is not an object (or a `_removed` list
  holding non-strings) raises CorruptConfigurationError naming the row
  instead of crashing mid-merge.

### Added

- **A docs site** at <https://starship-terminal.github.io/swe/>, built with
  Zensical from the existing Markdown (README, architecture and
  contributing docs, the app docs) plus a generated `swe help` reference
  page. `scripts/docs_site.py` stages it into `build/docs-site/` and a
  GitHub Actions workflow deploys it to Pages.

- **The menu bar's harness logos launch the harness.** The logo on each
  account and harness row is now a button: it opens the harness's desktop
  app when installed, else its CLI in a terminal, and closes the panel.
  With neither present the logo is inert. Accounts and harness rows
  resolve the launch through one shared `LaunchTarget`, the same one the
  Accounts page's Open app/Terminal buttons now use.

- **`swe list --refresh-failed` and `--cache-ttl=<seconds>`.** The first
  refetches only providers whose cached reading failed — a timeout
  marker, an auth-required verdict, or a reading with a window left
  unread — while healthy cached readings still serve; a failed retry
  keeps the previous entry. The second overrides the 5-minute cache TTL
  for that run. Cursor's Grok Bot meter now stays listed when its call
  fails or misses its budget, marked `unavailable` and carrying the last
  known figures with an `as_of` timestamp, instead of the row silently
  vanishing for the cache's whole TTL.

- **Refresh re-checks only what failed, and nothing disappears when a
  reading does.** A new Settings field, "Refresh only what failed for"
  (minutes, default 5), sets how long a good reading counts as fresh:
  inside it, the Refresh buttons force only accounts whose reading is
  missing, failed or stale, and pass `--cache-ttl` / `--refresh-failed`
  to swe so other harnesses refetch only their failed entries (an swe
  too old for the flags is asked without them). Windows swe marks
  `unavailable` keep their last known values, greyed with a red label
  and an "as of" age on the Accounts cards and the menu bar, instead of
  vanishing — and a kept stale reading after a failed account fetch
  gets the same row treatment. Usage state (readings plus the last
  failure and its timestamp) persists under `~/.quiver/cache/`, so
  cards render on launch without a keychain prompt or a network call.

- **Collapsible sidebar.** A toggle row under the traffic lights (or
  View > Toggle Sidebar, ⌃⌘S) pins the sidebar to a 72pt icon rail;
  the choice persists. Hovering the rail floats it open over the page
  after a short delay, a collapse under the pointer stays shut until the
  pointer leaves once, and Reduce Motion applies every change without
  animating. The pinned sidebar's trailing edge is also draggable now:
  an 8pt strip resizes it between 160 and 360pt, double-clicking resets
  it to 196, and the choice persists (`sidebarWidth`).

- **Settings > Updates shows whether the installed app is outdated.**
  `build-app.sh` now stamps the channel (`QUIVER_CHANNEL=dev|prod`, dev
  default), commit, source checkout and dirty state into the app's
  Info.plist. The new section compares a dev build against its checkout's
  HEAD (unbuilt commits, another branch, dirty tree, upstream counts,
  `git fetch` on Check now) and offers a one-click Rebuild plus
  "Relaunch to update" that swaps the bundle through a detached script
  (old app to Trash, `lsregister -u`, reopen). Prod builds check GitHub's
  latest release (6-hour cache at `~/.quiver/cache/latest-release.json`)
  and link to it when a newer tag exists; no auto-install. The card also
  shows the app's and swe's versions side by side and warns when they
  differ.

- **`swe version` (and `-v` / `-V` / `--version`) prints the installed
  version.** `swe 1.0.0` on stdout, exit 0, and no config read, so it works
  even when `~/.quiver` is unparseable. `--json` emits the standard
  envelope with `name`, `version` and the Python version under `data`.

- **Refresh renews expired Claude sign-ins.** Claude Code's OAuth
  token lasts eight hours and only Claude Code renews it, so an unused
  account read "Sign-in expired. Open the CLI once." Pressing Refresh
  on the Accounts page or in the menu bar now runs a headless `claude`
  in that account's home, which renews its own keychain credential;
  Quiver still never reads or writes the token. Renewals run only on
  that press, never on launch, timers or plain reloads.

- **The Usage limits widget's sources reorder.** The Widgets page lists
  them like the menu bar popup does, draggable, with each source's
  windows grouped under it; until the first drag the widget still sorts
  by pressure. The preview now sits large left with medium over small
  beside it, taking less vertical room.

- **The Usage summary widget draws one line per counted harness.** Each
  takes its trend-chart colour, the aggregate total stays on top, and a
  Total switch at the top of the Counted list turns it off.

### Changed

- **API keys live under `~/.quiver/secrets/`.** Provider keys moved from
  `~/.api_keys` to `secrets/providers/.api_keys` and MCP values from
  `secrets/.api_keys` to `secrets/mcp/.api_keys`. `swe init` moves both and
  leaves a symlink at each old path, so a shell profile that sources
  `~/.api_keys`, or an older swe that reads `secrets/.api_keys`, keeps
  working. `secrets/` gets its own `.gitignore` of `*`, so no root
  `.gitignore` can let a key into a commit. Until the move, the CLI and the
  app read the old paths. A fresh `~/.quiver/.gitignore` now lists `secrets/`.

- **Compute's clean-up moved into the header and finds more leftovers.**
  The limits card left the bottom of the page for a settings popover
  behind a gear button, and the
  "Clean up" button is a broom icon with a count badge opening a
  centred sheet. The sheet groups candidates under an Orphaned header
  and one header per harness in registry order, each collapsible with a
  mini switch that ticks the whole group; quitting a selection now costs
  one process sample no matter how many items it holds. The rules grew:
  bundle helpers still hung on launchd from an earlier app launch (or
  after the app quit) are suggested, so are duplicate servers, identical
  children of the same harness parent older than the orphan age, while
  every orphan tree is listed ticked or not. Processes whose executable
  was deleted after launch (a versioned helper an update replaced) are
  seen again: the sampler falls back to argv[0] when proc_pidpath goes
  empty.
- **The orphan-age setting takes any number with a unit.** "Orphans
  count as old after" is now a number field plus a Min/Hour/Day menu
  instead of a fixed option list, stored as minutes so values between
  the old hour steps persist, and clamped to a minute through a year.
  The legacy hours setting still loads.

### Security

- **The `server` extra now requires `mcp>=1.30.0,<2`** (was `>=1.0.0`),
  the first 1.x release patched for
  [GHSA-84m7-p3x7-pcfv](https://github.com/advisories/GHSA-84m7-p3x7-pcfv)
  (CVE-2026-59951): a stateful Streamable HTTP server never reclaimed
  sessions, so a client could grow its memory without bound. Quiver's
  session server (`python -m swe.mcp_server`) runs over stdio, which
  the advisory lists as unaffected, so there was no live exposure; the
  floor only keeps a fresh install off the vulnerable range.

## [1.0.0] - 2026-10-13

First stable release. `swe` and the Quiver macOS app share one data model
under `~/.quiver`, every `--json` command uses the `{schema, data}`
envelope, and the design is documented under [docs/](docs/): data model,
app architecture, backend architecture, JSON contracts and testing.

Highlights since 0.2.10: Cursor's Grok Bot allowance, harness display names
and primary colours, Devin quota handling, and in the app: per-account menu
bar and widget projections with live previews, Compute trends, plugin and
skill editing, MCP Local/Remote groups, collapsed service status cards, API
key checks and editing, a cached and source-isolated Overview, and four
audits (databases and caches, app structure and contracts, swe controller /
service / model layering, tests with an 84% coverage floor).

### Added

- **Cursor's Grok Bot allowance shows with its other limits.** Cursor gives
  Grok Bot its own weekly allowance, apart from the billing cycle. `swe
  list --usage --json` now lists it as a `grok_bot` window on Cursor (used
  percent, reset time, a week long), read with the same Cursor login from
  `DashboardService/GetSandUsageStatus` on `api2.cursor.sh`. It is never
  the headline figure, and a seat on a pooled enterprise allowance or with
  no included Grok Bot limit gets no window. The call is optional like the
  plan and hard-limit ones: a failure leaves the reading as it was. Those
  optional calls now get 3 seconds instead of 1.2, since Grok Bot's host
  needs a connection of its own. The macOS app labels the window "Grok
  Bot".

- **Harnesses get a display name, editable with `swe edit --display-name`.**
  A harness is keyed by its registry name and launched by its command
  (`cursor` runs `cursor-agent`), which is no way to label it in the app.
  `harness.json` entries now take an optional `display_name` ("Cursor",
  "Qwen Code"); absent or empty means unset. `swe edit <name|alias>
  --display-name="Qwen Code"` sets it (whitespace is trimmed) and
  `--display-name=` clears it; `--display_name=` and
  `--set=display_name=...` work too, and the interactive editor lists it as
  "display name". `swe list --json` rows carry `display_name` next to `name`
  (null when unset), `swe edit --json` reports `"display_name"` in
  `changes`, and `swe info` shows it when set. Nothing else changes: the
  `swe list` table keeps its columns, and a harness without a display name
  prints exactly as before. Re-adding or reinstalling a harness
  (`swe add`, `swe add -i`, `swe install`) keeps the name you gave it.

### Changed

- **The desktop widgets get their own sidebar page.** "Menu bar and
  widgets" split in two: "Menu bar" keeps the status item and popup
  settings and previews, and a new "Widgets" page lists the four widget
  kinds with the same underline tabs, previews and per-widget settings.
  The stored tab settings keep working; a leftover "widgets" selection
  on the Menu bar page falls back to the first tab.

- **The discover and setup commands emit the standard JSON envelope.** `swe
  harness discover --json`, `swe mcp discover --json`, `swe skills discover
  --json` and `swe setup --json` used to print bare `json.dumps` output.
  They now emit the same `{schema: 1, data: ...}` document as every other
  `--json` command: discovery rows live under `data.findings`, and the
  setup preview under `data.harness` / `data.mcp` / `data.skills`. An
  `--apply` run adds an `applied` object inside the same `data`, so a
  `--json --apply` invocation prints one document instead of two.

### Fixed

- **`swe list --usage --json` served an hour-old limit reading instead of
  a fresh one.** Piped output gave each provider's fetch a 2 second cap,
  and a fresh `swe` process (which is how the macOS app reads limits) often
  needs longer for its first Cursor or Devin request, so it fell back to the
  last cached reading, up to 24 hours old. The app then showed "Fetched 1h
  ago, couldn't refresh". `--json` now waits up to the same 25 seconds an
  interactive terminal gets; the piped table keeps the short cap.

- **`swe skills --json` listed a hub plugin's skills from Claude's stale
  cache.** Claude Code runs a plugin from a directory marketplace (every
  one in `~/.quiver/plugins`) straight from the marketplace's folder, so a
  skill added to the source loads at once while the copy under
  `~/.claude/plugins/cache` keeps the skills it had at install time. The
  listing read that copy, so a new skill was missing from it and from the
  app's Skills page, and a deleted one lingered. Such a plugin's skills are
  now read from `<marketplace folder>/<source path in marketplace.json>/skills`,
  and its cached versions are skipped, so no skill appears twice. Plugins
  Claude fetched (github, git, a URL) still run from the cache and are
  listed from there.
- **A broken config file no longer corrupts `--json` output.** When a
  `--json` command hit an unreadable config (a malformed `harness.json`, say),
  the error went to stdout, where the app expects only JSON. It now goes to
  stderr for every `--json` command; without `--json` nothing changes.
- **`swe find plugins` flagged Claude Code plugins as out of date when
  they weren't.** Claude Code reads a directory-marketplace plugin straight
  from its folder, so the copy under `~/.claude/plugins/cache` is a leftover
  it never loads, and `claude plugin update` reports nothing to update.
  Comparing against that copy raised "N files differ" findings no fix could
  clear. Claude is no longer checked for stale copies; Codex, which runs its
  own copy, still is.
- **`swe find plugins` kept calling a Codex plugin stale after its fix ran.**
  Codex follows no symlink when it copies a plugin, so a source whose
  `.codex-plugin`, `.cursor-plugin` or `.factory-plugin` was a symlink never
  matched its copy, and `codex plugin remove && add` changed nothing. The
  source is now compared the way Codex copies it, without its symlinks. A
  symlinked `.codex-plugin` is reported as a new kind, `linked-manifest`:
  Codex skips it and installs a manifest it generates itself, with no
  version (the copy shows up as `local`). Its fix replaces the link with a
  real copy of its target (copied beside the link first, then swapped in,
  so a failed copy leaves the link and its finding), and the next run offers the
  reinstall. The app shows it as "Linked manifest".

- **`swe skills unlink ~/.x/skills` unlinked the wrong thing.** A path to a
  linked harness root resolved to the shared tree it points at, so the
  command tried to unlink `shared` and refused. A symlink given by path is
  now the root it names. This bit every harness `swe skills` has no label
  for (`claude-work`, `opencode`, `droid`, `hermes`, …).
- **`swe init` no longer registers and links every app with a `skills/`
  folder.** A non-terminal run (a nix rebuild's activation script, say)
  used to register each unknown `~/.<name>/skills` as a harness and link
  it to the shared tree, so a token usage tracker's cache folder and a
  browser's skills folder both ended up as "harnesses". Now a new folder
  is linked only once someone ticks it: a non-terminal run leaves it
  alone and reports it as `skipped`, a cancelled picker links nothing,
  and the picker pre-ticks only folders whose owner has a command on PATH
  or a catalog entry. Aside, Pinokio, TokenTracker and Ollama are named in
  `NOT_HARNESSES` and never offered or linked; an active registry entry
  under the same name still wins. `--yes` still takes everything.
- **`swe harness discover --apply NAME…` and `--pick`.** Registering was
  all-or-nothing by confidence tier (`--apply` or `--apply-all`). Naming
  findings now registers just those, at any confidence, and `--pick`
  opens a multiselect in a terminal with only catalog matches ticked. The
  home scan skips the `NOT_HARNESSES` apps too.
- **Ollama is out of the harness catalog.** It was listed "so discovery
  names it", but catalog matches are exactly what `discover --apply`
  registers, so a local model runtime kept landing in `harness.json`. It is
  now in the PATH scan's exclude list instead, and gone from the alias help.
- **Claude no longer cries `re-login` on a self-healing token.** An expired
  access token beside a `refreshToken` is a live login — Claude Code
  refreshes transparently on its next call — so the fetcher now serves
  the last reading from claude's own ≤24h cache instead of the red
  `re-login`, and a `re-login` marker in the shared cache is re-derived
  on sight rather than replayed for the TTL. The label is kept only for
  an expired token with no `refreshToken`, the case that genuinely needs
  `claude auth login`. Cache ages are honest now too: each provider's
  reading carries its own `updated_at`, a re-served stale reading keeps
  its real fetch time, and freshness is judged per provider — so a
  persistent refetch on one provider (the re-derived `re-login`) can no
  longer rewrite the envelope and keep every other provider's cached
  value alive forever.
- **`swe providers add` no longer erases removal tombstones.** `add` saved a registry loaded without the `_removed` bookkeeping list, silently dropping every recorded removal — previously-removed default providers all came back on the next load. `add` now preserves `_removed`, lifts only the re-added provider's own tombstone, seeds a re-added builtin's default fields instead of persisting a bare record, and resolves aliases to the canonical slug instead of forking a duplicate entry. On the codex side, `split_codex_toml` now folds stray `[mcp_servers*]` tables found later in `config.toml` into the region (TOML allows them anywhere) instead of writing them twice on save — an invalid duplicate-table file — and `_toml_value`/`render_codex_server` handle `datetime`/`date`/`time` values `tomllib` produces instead of crashing or silently dropping the key.
- **Every subcommand now answers `--help`.** `swe report followup --help`, `followup <action> --help`, `report followups --help`, and `report warnings --help` used to error out ("Unknown follow-up action", a bogus usage line, or a manifest-suffix complaint) — or worse, `followup add --help` recorded a follow-up literally named "--help". `swe providers remove --help` reported a missing provider named "--help", `swe session help` and `swe models help` were rejected as unknown arguments, and `swe providers --help` printed the short summary whose last line pointed back at `swe providers --help` itself — it now reaches the providers group's own detailed help.
- **Untrusted strings can no longer drive the terminal.** Session titles, transcript text, file previews, directory names, and MCP server names all originate outside quiver, and a crafted value carrying an OSC-8 hyperlink, an OSC-52 clipboard write, a cursor-moving CSI, or a bare BEL/CR previously printed verbatim — inside a table cell, a picker row, or a find preview it could paint itself, open a link, or rewrite rows. A new `console.sanitize` removes every escape class (OSC through BEL/ST/end-of-string, CSI including private modes, charset and two-byte escapes, lone ESC) and maps C0/C1 controls to spaces (`sanitize_document` keeps `\n`/`\t` for transcript text, turns `\r` into a newline, and deletes other controls so a byte inside a credential cannot split it before redaction). Applied at the render boundary: table `text`/`path`/`list` cells, `_display_title`, the agent column, picker preview sources and labels, the checklist and tri-state labels, find-browser labels/details/preview rows, tree and path printing in `swe find`, and MCP name/label display. `strip_ansi` widened to match the same escape classes so width math is right on hostile input, while `preformatted` cells remain an intentional trusted styling path. Transcript text is cleaned inside `_clean_text`, so reports and previews share the guarantee.
- **`swe mcp sync` preserves the target's own format keys on overwrite.** Syncing over an existing entry dropped everything outside the canonical shape: an opencode server marked `enabled: false` was silently re-enabled, a droid `type: sse` became `http`, and claude's `disabled`/`autoApprove` vanished. Overwrites now merge the target's extension keys back over the emitted fields. Also in the MCP domain: `redact()` now swaps long (16+ char) credentials wherever they appear in a string, so a `?key=TOKEN` URL resolved into a harness config redacts back to `${NAME}` in the hub instead of persisting plaintext (resolve() always substituted mid-string; redact never did); `server_summary` redacts before printing so `mcp sync`'s conflict display and `mcp doctor` no longer echo credential-bearing URLs; and an unverified registry name containing `/`, `\`, or `..` gets no optimistic `~/.<name>/mcp.json` path — `mcp sync`/`list`/`status`/`diff`/`validate` on it exit cleanly instead of a write outside the home directory or a `KeyError`/`AttributeError` crash.
- **Terminal widgets restore the terminal on every exit path.** The session picker, checklist, tri-state picker, and find browser each hand-rolled raw mode and cleanup, so a fatal signal (or an exception between `tty.setraw` and restore) left the terminal raw with mouse tracking on. A shared `keys.raw_terminal` now owns setup, mouse-off, cursor-show, and termios restore, plus signal handlers for SIGHUP/INT/QUIT/TERM that clean up through the raw fd before re-raising. `keys.read_key` polls before each byte of an escape sequence so a truncated `Esc [` can no longer block forever, and unknown sequences are ignored instead of cancelling the widget. Rows are padded by terminal cell width (`console.cellpad`), fixing misaligned columns under CJK text, combining marks, and wide emoji. `swe session` and friends also read CR, LF, and CRLF correctly from pipes now: a piped `a\r\nb\r\n` used to eat every second answer, and piped input no longer echoes back into the output.
- **Malformed config files can no longer be wiped by a write.** `swe mcp sync`/`discover --apply`/`edit` treated an unparseable harness config as `{}` and then rewrote it holding only the MCP slice — for `claude` that meant `~/.claude.json`'s OAuth tokens, projects, and settings destroyed. `load_json` now raises `CorruptConfigurationError` on malformed or non-object content; read paths (`mcp list`/`status`/`diff`/`discover`, `get_hub_servers`) warn and degrade while every write path refuses. The same guard covers quiver's own files: `harness.json` and `providers.json` raise a clean error on a corrupt file instead of a raw traceback or a silent overwrite-with-defaults (the providers "corrupt reads as read-only" path was fake — the seed branch overwrote it), `skill_catalogs.json` refuses to save over a malformed file, and both registries plus the catalog file now write atomically via `atomic_write_text` so a crash mid-write can't leave a truncated file that bricks the next run. Non-object JSON (`[1]` as `mcp.json`) warns instead of crashing with `AttributeError`, and the top-level `swe`, `swe mcp`, and `swe providers` entry points all turn `ConfigurationError` into a red message + exit 1.
- **One malformed session record no longer poisons `swe session`.** `json.loads` accepts `NaN`/`Infinity` literals and sqlite REAL columns can hold ±Inf — a cline meta file with `"started_at": NaN` killed the whole listing at `int(diff / 86400)`. `Session` now coerces `timestamp` (non-finite or unparseable → `0.0`), drops non-string `path` values to missing (a json number would otherwise survive as a relative path a `--here` filter could match), and stringifies `agent`/`title`/`session_id`/`tool_name` at construction, so every engine and the session-cache reader share the guarantee. `parse_iso_ts` rejects non-finite inputs too, and absurd `--days=`/`--end=` values (`--days=1000000`, `--end 9999-12-31`) now hit the same clean error path as other bad dates instead of an uncaught `OverflowError`.
- **Completion tables drifted from dispatch.** `swe <TAB>` never offered `find` or `discover` and listed `harness` twice; `list` completed no `--usage`/`--links`; `session` lacked `--grep`; `report` lacked `--search`/`--session-arg`/`--writer-arg`; and no subcommand completed under `mcp`, `find`, `skills`, or `providers`. All tables are rebuilt: nested flag sets (`swe mcp sync --<TAB>`, `swe skills move --<TAB>`, `swe report followups --<TAB>`), second-level subcommands for mcp/find/skills/providers/session, third-level actions (`report followup`, `skills catalog`, `config setup`), tool names for `mcp sync`/`diff` (excluding ones already typed, aliases included), provider names for `providers info/remove`, and server names for `mcp edit <tool>`. `swe doctor` now checks the completion tables against dispatch so the drift cannot silently regrow.
- **Audit leftovers: hidden flag, dead flag, 404 help topics, inconsistent exits.** `swe mcp sync --prune` worked but was missing from `swe mcp sync help`; it is documented now. `swe skills help tree`/`scope`/`scopes` 404'd on topics `swe help skills` advertises — they now print a help page explaining the `swe find skills -r` hand-off. `--json` on `swe skills link`/`unlink`/`move` was parsed and ignored; it now prints a machine-readable result, and the shared parser rejects flags that do not belong to the subcommand (`--mkdir` on link/move, `--force` on unlink, any unknown flag). `swe report daily --help` and bare `swe report`/`swe report help` print the report help and exit 0 instead of erroring, matching `mcp`/`providers`/`skills`/`config`. `swe session use abc`/`use` say "Usage: swe session use <N>" instead of "Unknown argument". `swe mcp ls` now aliases `mcp list`. Help texts also fixed: `--apply-all` adds every match (not "high + medium"), `-n` documented as `--check`/`--dry-run` on init/install, and a note marks that `-n` means fetch-fresh on `swe list` instead.
- **`swe mcp sync` picker uses the shared checklist widget.** `mcp/cli.py` carried its own `getch()` + escape-sequence parser (`interactive_select`), which blocked forever on a bare Esc and skipped the repo's key-handling rules. Both pickers (server selection, conflict overwrite) now call `multiselect.multiselect`, which cancels cleanly on q/Esc/Ctrl-C, scrolls with the wheel, and restores the terminal on every exit path. The widget defaults the conflict picker to all-ticked, matching the old behavior.
- **Error exits and unknown args are strict now.** `swe info`/`remove`/`use`/`star`/`add`/`help` printed a red error but exited 0 because the handlers fell off the end — each now returns 1. Leftover argv that used to be silently absorbed is rejected: a `--flag` typo in `swe list` no longer filters the listing on the typo'd word (`-x` and `--x` both fail "Unknown flag"), `swe models`/`skills`/`providers list` reject unknown flags, and trailing positionals are an "Unexpected args" error on `find`, `info`, `remove`, `star`, `list`, `providers`, `autocomplete`, and the no-arg commands (`check`, `tags`, `aliases`, `doctor`, `list edit`, `list legend`, `harness edit`). The `-tag` filter shorthand on `swe list` is gone as a consequence: tags are bare words now.
- **Cline sessions read as zero.** `parse_cline` only read the legacy `~/.cline/data/state/taskHistory.json` index; Cline 3.x writes one directory per session under `~/.cline/data/sessions/<id>/` (`<id>.json` metadata beside a `<id>.messages.json` transcript) and never creates `state/`. The parser now reads both layouts — the per-session dirs and, for older installs, the index. Transcript previews/reports follow: the cline reader loads `<id>.messages.json` (unwrapping `<user_input>` envelopes, dropping `thinking` blocks) and falls back to the old `data/tasks/<id>/` pair.

### Added

- **`swe doctor --json` names the harness an off-PATH finding is about.**
  An `off-path` row now carries `"harness": "<registry name>"`, so the app's
  Doctor page can show that harness's logo. Rows about the machine (node,
  npm, nvm, drift) leave the key out, so they read as before.
- **`swe plugins`: the whole plugin catalog, and install per harness.** The
  app's Plugins page gets an Explore tab listing every plugin in every
  marketplace Claude Code, Codex or Cursor has registered, installed or not
  (the 315 in claude-plugins-official included), and the hub cards move
  there from the Skills page. `swe plugins catalog --json` reads files only,
  never the network: each marketplace's source type, location and manifest
  per harness, and each plugin's metadata, its `components` and `skills`
  read from the copy each harness loads (the source folder for a Claude
  directory marketplace), its install and enable state per harness, and for
  `~/.quiver/plugins` plugins the same `drift` state and fix ids `swe skills
  hub --json` gives. Plugins installed from a marketplace with no readable
  manifest are listed too, so nothing installed goes missing, and every
  plugin skill `swe skills --json` reports belongs to exactly one catalog
  plugin. A malformed file costs only the marketplace or harness it
  describes, and URLs lose any credentials. `swe plugins
  install|uninstall|enable|disable <ref> --harness=claude|codex` checks the
  state first, does nothing when there is nothing to do, and otherwise runs
  `claude plugin <verb>` or `codex plugin add|remove` without a shell.
  Codex has no enable verb, so enable and disable rewrite the one
  `enabled` line in `~/.codex/config.toml` atomically, after proving the
  edited file parses to the original with only that flag changed; when it
  cannot prove it, nothing is written and the error says what to edit by
  hand. Cursor is listed read-only: it keeps no install record swe can read.

- **`swe edit <name> --field=value --json`.** The app's edit sheet needs to
  change a harness's command, description, aliases, tags, version and notes
  without a terminal. With `--json` the flags are applied (an empty value
  clears the field, as before) and stdout carries `{"name": <canonical
  name>, "changes": [<fields that changed>], "harness": <that harness's
  `swe list --json` row>}`. It never prompts: no field flag, an unknown
  harness, an alias collision, an empty command or an unknown flag print a
  message on stderr, leave stdout empty and exit 1. Without `--json`
  nothing changes. `swe list --json` rows also gain `notes` (null when
  unset) so the form can prefill it.
- **JSON for the app's Skills and Harnesses pages.** `swe skills --json`
  lists every skill on the machine with where it came from (`shared`,
  `plugin`, `harness`, `builtin`, `catalog`) and which harnesses can see
  it, plus each harness's skills-root link state in `swe init`'s
  vocabulary. `swe skills hub [--json]` lists the plugins in the
  `~/.quiver/plugins` marketplaces, the skills each ships, and whether
  Claude Code and Codex have them installed, with the command that closes
  any gap. `swe harness star <name> --json` and `swe harness archive <name>
  [why] --json` report the harness's new state, and `swe list --json` rows
  gain `installed` and `archived` (reason, date, usage).
- **`swe list --usage --exclude=a,b`.** Lists those harnesses but skips
  polling their usage. The macOS app fetches Claude and Codex per account
  itself and passes them here, so their rate-limited endpoints are not hit
  twice. Under `--json`, a provider that names no plan now gives
  `plan_type: null` instead of the table's `—` placeholder.
- **`--json` for `swe list`, `swe session`, `swe find skills` and `swe init
  --check`.** The macOS app under `app/` reads these. `list` gives each
  harness's state, aliases and 100-day session count, and with `--usage`
  its rate limit, where `status` replaces the `no-sub` / `auth-required` /
  `timeout` sentinels and `fetched_at` is when the reading was really
  taken, never the time it was served from cache. `session` gives each
  session's harness, cwd, title, `last_active_at`, status and the `argv`
  that resumes it. `find skills` gives the shared skills and each harness
  root's link state; `init --check` gives each harness's instruction file
  and skills root with its link state. Under `--json`, errors go to stderr
  and stdout stays empty.
- **`--json` for `swe mcp list`, `swe mcp status` and `swe doctor`.** Each
  prints one `{"schema": 1, "data": ...}` document for the macOS app.
  `mcp list` and `mcp status` give one entry per server name (transport,
  command, args, url, the names of its env vars and headers, the harnesses
  it appears in, and `conflict` when they define it differently); `status`
  adds `health` and `detail` from the checks it already ran. `doctor` gives
  `ok` and a list of findings with `check`, `severity`, `message` and
  `fix`, and keeps its exit status. Secret values never appear: env and
  header values are not printed, `${NAME}` references are never resolved,
  and credentials in a URL or argument are hidden.
- **Devin joins the discover catalog.** `HARNESS_CATALOG` now names the
  `devin` binary (alias `dv`), so `swe discover` finds it on PATH the
  same way it finds kilo. `swe init` gets a signature for
  `~/.config/devin/skills` plus `~/.config/devin/AGENTS.md`, with
  evidence at `~/.config/devin` or `~/.local/share/devin` — the homes
  the CLI actually creates. `skills/layout.py`'s `HARNESS_ROOTS` lists
  the same skills path so `swe find` can name it. Session parsing and
  the quota fetcher already knew Devin; discover did not.
- **Kilo MCP target and Cline models/hooks coverage.** `swe mcp` now maps kilo to `~/.config/kilo/kilo.jsonc` (opencode `mcp` schema), and `load_json` strips `//`/`/* */` comments on `.jsonc` paths so a commented config reads instead of parsing as empty. `swe models` counts cline sessions from the `provider`/`model` fields in each session's `<id>.json` metadata. `HOOK_FALLBACK` gains `cline` → `~/.cline/hooks`, opt-in via `~/.quiver/hooks/cline/` as usual.
- **Cline session status and resume.** The STATUS column now reads Cline 3.x's per-session metadata (`~/.cline/data/sessions/<id>/<id>.json`): its `status` lifecycle field maps `running`/`streaming` to active (with the recorded `pid` — not the never-advancing `started_at` timestamp — deciding whether an old turn is still live), `waiting`/`idle`/`completed` to done-or-followup via the last assistant text, and `failed`/`canceled`/`paused` to error/interrupted. `swe session use` resumes with `cline --id <session-id>`.
- **Kilo sessions.** `parse_kilo` reads `~/.local/share/kilo/kilo.db` — an opencode fork, same drizzle schema — so `swe session` lists kilo sessions, `--agent kilo`/`ki` filters them, `swe session use` resumes them via `kilo --session <id>`, the STATUS column probes the same `message`/`part` rows as opencode, and `swe models` mines `message.data` for provider/model counts.
- **`swe init` detects known harnesses from evidence, not just `skills/` dirs.** `init/layout.py`'s new `HARNESS_SIGNATURES` table declares, per harness, its skills root, its instruction file, and an any-of list of home-relative paths that mean "installed" — so cline is found via `~/.cline` (its CLI only makes `data/` on first run) and kilo via `~/.config/kilo` or `~/.local/share/kilo`, even though kilo's skills root lives at `~/.kilo/skills` whose parent is never created. Newly covered: cline (`~/.cline/skills` plus the cross-tool `~/.agents/AGENTS.md` it reads) and kilo (`~/.kilo/skills` plus `~/.config/kilo/AGENTS.md`). `capabilities.skills.root` in harness.json now also feeds root discovery directly, so a declared root need not match the `*/skills` glob. `kilo` joins `HARNESS_CATALOG` (so `swe discover` finds its binary) and `skills/layout.py`'s `HARNESS_ROOTS` (so `swe find` and drift checks can name the root).
- **`swe init` offers to manage harnesses it discovers.** A skills root the
  registry has never heard of now opens a checklist: ticked harnesses get an
  active `harness.json` entry with `capabilities.skills.root` and an
  instruction file (AGENTS.md by default, or another filename, or skip) that
  init links in the same run; unticked ones are archived and left alone.
  `--yes` registers everything without asking, which is also what a
  non-terminal run does. `--check` only notes how many new harnesses it
  would ask about. A home still on legacy `tools.json` is told to run
  `swe list` once first, since writing `harness.json` early would skip the
  lazy migration. Instruction targets are now registry-driven: an entry's
  `capabilities.instructions.file` wins over the built-in table.

### Changed

- **Plugins > Explore nests plugins under source and marketplace.** The
  flat list of about 385 plugins becomes two levels you open and close:
  Local (the hub), then each harness's own marketplaces (Claude Code,
  Codex, Cursor), then Not registered, and under each its marketplaces
  with "N plugins · K installed". A row shows only that source's harness
  chip; the hub's show both. The hub and its marketplaces start open,
  everything else closed, and what you open or close is kept across
  launches. A search opens everything it matches without changing that,
  and clearing it puts it back. The Source select is gone: the sources
  are now the list's top level.
- **The app's Skills page lists standalone skills; plugin skills move to
  Plugins > Explore.** Explore lists every plugin from `swe plugins
  catalog --json`, grouped Quiver hub first, then the other marketplaces,
  then those no harness registers any more, with a Source and a Harness
  select and a search that also matches the skills a plugin ships. Each
  row shows its state per harness, with install, uninstall, enable and
  disable for Claude Code and Codex behind a confirmation that shows the
  exact command or `config.toml` edit; Cursor is read-only. The old Quiver
  hub tab on Skills is gone, and the drift list becomes the Plugins page's
  Health tab. One function decides where each skill is shown, so a skill
  is listed once: on Skills, or in the one plugin whose folder holds it.
- **`swe skills link|unlink|move --json` print the shared envelope.** They
  printed a bare object; they now print `{"schema": 1, "data": …}` like
  every other `--json` command, and a refusal goes to stderr so stdout
  stays empty.
- **`swe list` waits out slow usage fetchers on a terminal.** The REMAINING
  fetchers ran under a 2s deadline so a hung provider can't stall the
  listing, but slow-but-working endpoints rendered `…` and cached the
  marker for the TTL. When stdout is a TTY the fetch now runs behind a
  spinner with a 25s budget sized to the slowest fetcher's own design
  (Copilot's `gh` subprocess plus a 10s HTTP read plus its cert-bundle
  retry), while piped output keeps the 2s cap and prints once, static.
- **Value-taking long flags are `--name=value` only.** Every long flag that
  takes a value — `--agent`, `--search`/`--grep`, `--days`, `--weeks`,
  `--start`, `--end`, `--aliases`, `--tags`, `--description`, `--command`,
  `--version`, `--notes`, `--set`, `--package`, `--url`, `--env`, `--file`,
  `--from`, `--to`, `--api-keys-dir`, `--status`, `--project`, `--harness`,
  the report `--session-*`/`--writer-*` overrides, and the existing
  `--scope`/`--usage`/`--only`/`--except` — now requires the `=` form, e.g.
  `--agent=claude`, `--tags=a,b`. A bare `--name` fails with
  "takes a value: write it as --name=<value>" instead of silently consuming
  the next token. Short flags (`-d 5`, `-q foo`) keep the space form.
- **`swe list` quota column is a pie glyph now.** REMAINING becomes QUOTA:
  `○◔◑◕●` show the tightest window's share left on a red→green ramp (green
  = full), followed by the dim reset countdown, so the column is ~5-10
  chars instead of 14. `swe list legend` explains it.
- **Archived means unmanaged in `swe init`.** Instructions, skills and hook
  scripts belonging to an archived harness that init would have created,
  relinked, absorbed or flagged are now reported `ignored` ("archived in
  harness.json") instead, so archiving a harness stops init from touching
  its config. A path already linked stays linked — init never unlinks —
  and `.linkignore` still wins when both apply.

### Fixed

- **`swe list` columns no longer drift when a field's content outgrows
  its declared width.** `preformatted` cells are now padded to the
  settled column width, and `cmd_list` stopped hand-padding each cell.
- **`~/.factory/skills` now maps to the `droid` registry row in `swe list`
  and `swe init`.** A `factory` -> `droid` alias in `REGISTRY_ALIASES` joins
  the skills root's directory-derived label to the harness's registry key;
  before, that root reported under a `factory` label no registry entry
  matched, so an archived droid was still touched.
- **A rate-limit fetch that misses the 2s deadline now shows `…` instead of
  a blank dash.** The worker used to leave no trace, so `swe list -n`
  retried every run and a slow provider read exactly like a harness with no
  usage endpoint. The marker is cached for the normal TTL (so a plain
  `swe list` stays fast), excluded from the 24h stale fallback, and retried
  for real on `swe list -n`. `swe list legend` now explains the REMAINING
  column.

### Removed

- **Kimi, Mimo, Tau and Antigravity support.** Their session parsers,
  transcript readers and `swe session` resume entries are gone, as are the
  Antigravity rate-limit fetcher (a loopback RPC to the running app) and its
  `gemini` launch mapping, and the Kimi and Mimo `swe discover` catalog
  entries. The Kimi and Mimo API providers stay, since those are model APIs
  rather than harnesses. Session parsers now cover 15 tools and discovery
  recognises 21.
- **Amp and Hermes support.** Their session parsers, transcript readers,
  `swe discover` catalog entry, instruction target (`~/.amp/AGENTS.md`),
  seeded skills root and `swe list` filename are gone. Session parsers now
  cover 19 tools and discovery recognises 23.

### Changed

- **`swe find` prints counts by default; `--full` prints the tree.** Every
  view (`amd`, `skills`, `plugins`, `mcps`, and bare `swe find`) now opens
  with the same short summary `swe init` gives: one bold line per section
  with a count per state and the harnesses in it, such as
  `Harness roots  6 synced (claude, codex, …), 1 on disk but unregistered (codeium)`.
  Name lists shorten to fit the terminal; counts never do. Footers for
  vendored files, scope-hidden plugins and archived harnesses stay, and each
  view ends with `swe find <topic> --full lists every path.` (one hint for
  bare `swe find`, echoing `-r`, `--scope` and `--harness` when given).
  `--full` prints exactly what `swe find` printed before, and combines with
  every other flag. `swe skills tree`, `swe skills list` and
  `swe skills scope list` still draw the full tree.

- **Uninstalled, unregistered harnesses stay out of the report.** `swe init`
  no longer lists an instruction target as `skipped` when the harness is
  neither installed nor in `harness.json`, so removing a harness you stopped
  using leaves no trace in the output. `swe doctor` likewise stops warning
  about a fallback-table row for such a harness; it still warns when that
  harness's root exists on disk.

- **Python floor raised from 3.10 to 3.11, and the CLI is now dependency-free.**
  `tomli` was the single runtime dependency and existed only to backfill
  `tomllib` below 3.11, so it is gone from `pyproject.toml` and from the Nix
  flake, and the three `try: import tomllib / except: import tomli` forks in
  `mcp/codex_io.py`, `harness/rate_limits.py` and `find/mcps.py` collapse to a
  plain import. The CI test matrix moves to 3.11 and 3.13 and coverage drops to
  a single leg, taking CI from 7 check runs to 6. Users on 3.10 should pin `v0.2.9`,
  the last release that runs on it: `pipx install
  git+https://github.com/starship-terminal/swe.git@v0.2.9`. Ubuntu 22.04 LTS ships
  3.10, while Debian 12 and Ubuntu 24.04 ship 3.11 and 3.12 respectively.

  One behaviour improves: a timestamp carrying nanosecond precision, such as
  `2026-08-01T00:00:00.123456789Z`, used to lose its fractional part entirely
  on 3.10 and now truncates to microseconds.

  The block in `_parse_iso8601_to_epoch` labelled "Python 3.10 fallback" was
  **not** removed, because the label was wrong. It salvages a malformed
  fractional part sitting next to a timezone offset, `fromisoformat` rejects
  those strings on every supported interpreter, and deleting it would turn a
  recoverable reset time into `0.0`. Its comments now say what it really does
  and 14 new tests pin the behaviour.

### Added

- **A status column in `swe session`.** A two-wide `ST` column after AGENT
  shows where each conversation stands as a single glyph, with a legend
  line under the title: `●` active (agent generating now), `✓` done
  (turn finished), `?` followup (finished turn that ends by asking the
  user something), `✗` error (API or agent-loop failure) and `■`
  interrupted (stopped mid-turn, nothing alive). Claude Code is read
  from the transcript tail plus the `~/.claude/sessions` pid registry and
  `~/.claude/jobs` state files, Cursor from its terminal `turn_ended`
  record, Devin from the head row of the session's main chain plus the
  `session_locks` pid files a running `devin acp` leaves behind, Codex
  from the rollout tail's decisive `event_msg` (`task_complete`,
  `turn_aborted`, `task_started`) with a `response_item` fallback,
  OpenCode from the last message row's `finish` field in `opencode.db`,
  and Pi from the transcript's trailing `message` record; every other
  harness shows `-`. The followup signal is a regex port of Claude
  Code's own `claude agents` needs-input heuristic, deliberately
  under-matching.

- **Plugin drift in `swe find plugins`.** Claude Code and Codex run a cached
  copy of each `~/.quiver/plugins` plugin, so edits there never reach them
  until they reinstall. A `Drift` line now counts, per harness, marketplaces
  that were never registered, plugins never installed, installed copies whose
  files differ from the source, and plugins disabled in one harness while
  enabled in the other. `--full` lists each finding with the command that
  fixes it. Read-only, covers claude and codex, and skips plugins sourced
  from git.
- **Hook scripts in `swe init`.** `~/.quiver/hooks/<harness>/` holds one
  harness's hook scripts, and `swe init` links each file into that harness's
  hooks directory: `capabilities.hooks.root` from `harness.json`, else
  `~/.claude/hooks` for claude and `~/.factory/hooks` for droid. Hooks are
  per harness, never shared, and opt-in by the directory existing. Scripts
  are linked one by one so installer-owned siblings are left alone, and
  `.linkignore` applies to the destination. A plain copy with identical bytes
  is absorbed into a link. `swe init` prints a third `Hooks` line and `swe
  doctor` checks `HOOK_FALLBACK` against the registry. Only the file is
  linked: declaring the hook in the harness's settings is left to whatever
  manages that file.
- **Community files and supply-chain hygiene.** `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), `SUPPORT.md`, `.github/CODEOWNERS`,
  `.editorconfig`, and a weekly Dependabot schedule for GitHub Actions. Every
  action in CI is pinned to a full commit SHA with the version as a trailing
  comment, and both workflows start from `permissions: contents: read`. A
  `release.yml` workflow builds the sdist and wheel on a `v*` tag, checks the
  tag against `project.version`, runs `twine check`, smoke-tests the wheel,
  and attaches both files to the GitHub release. Publishing to PyPI stays
  commented out because the name `quiver` belongs to another project.
- **A `flake.nix` in the repo.** `nix run github:starship-terminal/swe` builds
  and runs `swe` with no clone, and `packages.default` is a flake input any
  nix-darwin or Home Manager config can consume. The build runs the whole
  unittest suite inside the sandbox, so `nix flake check` doubles as the
  test gate; CI runs it on every push. Three tests now mock or skip what
  the sandbox cannot provide (`gh`, `git`, and a hyphen-free temp dir).
- **`~/.quiver/.linkignore`.** A gitignore-style list of paths `swe init`
  must leave alone: one home-relative pattern per line, `#` comments, `*`
  wildcards, and a directory name covers everything under it. A matching
  path shows as `ignored` in `swe init` and as a dim dash in `swe list`,
  and is never linked, backed up, or counted as left alone. `swe init` seeds
  the file with a commented example so the option is discoverable.
- **Devin sessions in `swe session`.** The Devin CLI's `sessions.db` is
  parsed: hidden rows are skipped, an empty title falls back to the first
  prompt of the session, an inline `/rename` marks the title as a rename,
  and `swe session` resumes with `devin --resume <id>`. The transcript
  view walks the session's main chain so a streamed reply appears once,
  and tool results are labelled with the call they answer.
- **Devin usage in `swe list`.** A starred Devin harness shows the more
  used of its daily and weekly quota (`1d` / `7d`) with the reset time,
  read through Windsurf's seat-management RPC with the key from
  `~/.local/share/devin/credentials.toml`. Credit-based plans show
  `remaining/total` prompt credits instead.
- **Cursor usage in `swe list`.** A starred Cursor harness now shows how
  much of the current billing cycle's included usage is gone, labelled
  `auto` (total usage, the figure Cursor quotes for Auto mode) or `api`
  (named models), whichever is more exhausted, with the cycle end as the
  reset. The session token comes from the editor's `state.vscdb` on any
  platform or from the `cursor-agent` keychain entry on macOS; an expired
  token shows `re-login` instead of a blank cell.
- `swe session -i` picks a session with arrow keys. Enter resumes the
  highlighted session and space opens its full transcript in a scrollable
  read-only view (Esc returns to the list, Enter resumes from inside it),
  so two forks that share a title can be told apart before resuming one.
  The mouse wheel moves the cursor in the list and scrolls the view;
  assistant replies render their markdown (headings, emphasis, code,
  lists, quotes, tables, links) instead of showing it raw.

### Changed

- **Docs audited against the code.** README: the `swe list` sample, the
  commands table, the quick start, the supported-tools list (now the 24 the
  catalog recognises), the skills layout (`~/.quiver/skills`), and the
  config paths were all re-derived from the source; a duplicated
  `swe providers` heading that carried a copy of the mcp table is gone, the
  `#contributing` nav link now lands on a real section, and the stdlib-only
  badge says `3.11+` because 3.10 pulls in the `tomli` backport.
  ARCHITECTURE.md drops `swe mcp export`/`import` (never existed), corrects
  the `~/.quiver/.gitignore` body, and restates the bottom layer's import
  order. CONTRIBUTING.md rebuilds the project layout table from the real
  tree, points MCP handlers at `mcp/formats.py`, and stops linking the
  disabled Discussions tab. SECURITY.md names `0.2.x`, the real secret
  paths under `~/.quiver`, and the fact that private vulnerability
  reporting is not on yet. `docs/COVERAGE.md` is re-measured at 77.2%
  combined over 1,695 tests. `pyproject.toml` uses the PEP 639
  `license = "MIT"` form with `license-files`.
- **`swe init` prints counts by default.** Every run used to list all sixty-odd
  harness paths, one row each, when nearly all of them read `linked`. The
  default is now one tally per section (`Instructions  9 linked`,
  `Skills  57 linked, 1 protected`), the total, and then only the paths that
  need a decision: directories left alone because their skills exist nowhere
  else, and paths blocked by a real file. `swe init --full` prints the
  per-path listing as before.
- **The transcript view shows the conversation, not the machinery.** A run
  of tool calls collapses to `called 12 tools` instead of listing each one,
  since their arguments were the bulk of the view and the least use for
  telling two sessions apart. The `you` / `ai` labels are gone: a prompt is
  marked by shading it, full width on every row it wraps to, the way a chat
  UI shades what you typed. Set `SWE_USER_BG` to a raw SGR sequence to
  change the shade for a light terminal (e.g. `\033[48;5;253m`).
- **Codex sessions read their real titles.** A codex thread's name lives
  outside its rollout transcript, so `swe session` used to title every
  codex row from the transcript's first user-role item, which is usually
  context codex injected (a plugin roster, an AGENTS.md) rather than
  anything you typed. Titles now come from codex's own thread store
  (`~/.codex/state_<n>.sqlite`), falling back to `session_index.jsonl`
  and then to the transcript. A `/name` you typed renders in bright
  italic like a Claude rename, inferred from the name history's shape
  and timing since codex records no flag for it. Threads codex considers
  empty — opened, never typed into, which its own listing hides — no
  longer appear.
- `swe session` sizes its IDX, LAST ACTIVE and AGENT columns to the rows
  on screen instead of a fixed 4/14/14, so the listing carries no dead
  columns. A three-digit index no longer overflows its cell and shifts
  every column after it.
- Directory columns shorten from the middle (`~/Desktop/Work/…/toolbox`)
  rather than the right. Paths in a listing tend to share a long prefix,
  and cutting the tail made whole runs of rows look identical.

### Fixed

- `swe session use <n>` resumes a Cursor session again. It treated
  Cursor as limited-resume and launched a bare `cursor-agent` in the
  session directory; `cursor-agent --resume <chatId>` takes the same
  UUID `parse_cursor` already stores as the session id.
- **`swe find mcps` no longer reports copies of configs as unmanaged.** The
  disk scan read harness folders in `~/.Trash` and snapshots under
  `~/.quiver/backups` as live configs, so every server in those copies
  showed up under "Configs quiver does not read" and "On disk but not in
  the hub". It now skips `.Trash`, `Trash`, `backups` and `backup`
  directories at any depth, plus the `.bak` / `.backup` / `.old` /
  `pre-bootstrap` names the skills scan already treats as snapshots.
- **Usage columns work again on a Python with no CA store.** A python.org
  build on macOS trusts no certificates and `certifi` is not a dependency,
  so since the insecure retry was removed every usage fetch failed TLS
  verification: Codex showed `—` and Claude kept showing whatever it had
  last read. The retry now uses the operating system's own bundle
  (`/etc/ssl/cert.pem` on macOS, the usual Linux paths), still verified,
  and falls back to `certifi` only when that is missing.
- **Claude no longer shows a weeks-old reading as current.** When its
  fetch failed for any reason the Claude fetcher returned its last reading
  from `claude_usage_cache.json`, which the aggregator then dated to that
  moment, so a 7d figure from three weeks earlier displayed as fresh with
  a reset of `now`. The reading is now dated, dropped after 24h, and only
  reused inside a 429 cooldown; every other failure reports no reading so
  the aggregator's own dated 24h fallback applies.

- One Claude Code conversation continued across two transcripts is listed
  once, not twice. Claude Code can carry a session into a fresh file and
  records `continued-in` in the old one; both then carry the same title, so
  the listing showed a duplicate row. The predecessor is dropped only when
  no conversation follows the handoff and the successor really exists, so a
  session that forked and then kept going still counts as two.
- `swe session use <n>` resumes a codex session again. It ran
  `codex --resume <rollout stem>`; codex has no `--resume` option and its
  `resume` subcommand wants the thread uuid, so every codex resume failed
  on argument parsing.
- **Claude usage no longer reads a stale `~/.claude/.credentials.json` over
  a fresh Keychain login.** On macOS Claude Code refreshes the Keychain
  entry and leaves the file behind, so an expired file token shadowed the
  valid one and the usage endpoint 401'd until the file was deleted. quiver
  now takes whichever source has the later `expiresAt`, and an expired
  token renders `re-login` whether or not a refresh token is present,
  since quiver cannot refresh it.

## [0.2.10] - 2026-08-25

### Changed

- **Harness state consolidated into one file: `config/harness.json`.**
  `tools.json`, `stars.json`, and `archived.json` used to move together in
  practice — starring or archiving a harness meant resolving an alias
  against the registry and then writing a second file — and reading the
  full picture meant loading all three and joining them by name. Every
  entry now carries its own `state` (`"active"` | `"starred"` | `"archived"`,
  absent means active), plus `pin` (starred, 1 = top) and an `archived`
  object (`reason`, `archived_at`, `usage`) matching `multiselect.py`'s
  `STATES` tuple exactly. `harness/registry.py` is the single module that
  reads or writes the file (`load_registry`, `state_of`, `is_active`,
  `active_names`, `starred_names`, `archived_names`,
  `load_registry_if_present` for read-only callers); `stars.py` and
  `archive.py` are now compatibility shims over it, so `swe hs star` /
  `swe hs archive` and older call sites kept their signatures unmodified.
  A fresh machine, or one upgrading from before this change, gets a lazy
  one-time migration the first time the registry loads: the legacy files
  are merged in and parked (never deleted) under
  `~/.quiver/.backup/registry-migration-<date>/`.
  - **Fixed:** default seeding used to hand out a shallow copy of
    `DEFAULT_TOOLS`, so starring or archiving a harness on a fresh machine
    mutated the module-level default in place for the rest of the process.
    Seeding now deep-copies it.
- **`swe find` filters by harness activity.** `--harness=active|all`
  (default `active`) on `swe find` and every subview (`amd`, `skills`,
  `plugins`, `mcps`) hides an archived harness's rows; starred still
  counts as active. A filtered view never hides silently — it ends with a
  dim `N archived harnesses hidden; --harness=all to show` footer. A row
  that cannot be resolved to a harness in `harness.json` is never
  filtered, active or not, since unknown is exactly what `swe find` exists
  to keep visible.
- **Capabilities-first, hardcoded tables fallback-second.** `swe find`'s
  view of which harnesses support skills or plugins, and where, now reads
  a harness's own `capabilities.{skills,plugins}.{supported,root}` from
  the registry first, and only falls back to `skills/layout.py`'s
  `HARNESS_ROOTS` / `find/plugins.py`'s `PLUGIN_FALLBACK` for a harness
  the registry has never heard of. `HARNESS_ROOTS` labels were renamed to
  match the registry's own names (`qwen-code`, `mistral-vibe`).
  - **Fixed:** `swe find plugins` now covers opencode (previously invisible
    to plugin discovery), skips grok's unsupported entry instead of
    guessing, and labels droid's plugins by its registry name instead of
    the `~/.factory` directory it happens to live in.

### Added

- **`swe doctor` catches drift.** A new `harness/drift.py` runs four
  read-only checks on every invocation: `help_text.py`'s HELP topics
  against `cli.py`'s COMMANDS dispatch, `harness.json` schema validation,
  a join check between the capabilities-first fallback tables and the
  registry, and a scan for dangling top-level symlinks in the repo root
  and `~/.quiver`. Since capabilities-first landed, a registry entry the
  fallback table doesn't know about (or a table entry the registry
  overrides) is the design working, not drift — only a broken join (a name
  mismatch, a `supported` capability missing its `root`, a table entry for
  a harness the registry has never heard of) is reported.

### Removed

- **The dead top-level `star` help topic.** `swe star` was never a
  top-level command — favouriting lives at `swe harness star` — so
  `help_text.py`'s `HELP["star"]` topic had no matching entry in
  `cli.py`'s `COMMANDS` and was pure drift. The `harness` help topic
  already documents `swe harness star`.
- Dead `demo` symlink, stale coverage artifacts, and `.jules/` session
  notes (parked under `.archive/`, gitignored) cleaned out of the repo.

## [0.2.9] - 2026-08-20

### Fixed

- **v0.2.8 could not start on Python 3.10 or 3.11.** `swe find`'s summary line
  used a nested f-string with implicit concatenation inside a replacement
  field, which is PEP 701 and valid only on 3.12+. It parsed fine locally and
  raised `SyntaxError` on import for anyone at the declared floor, taking the
  whole CLI down. Anyone on v0.2.8 should upgrade.

### Added

- **A syntax-floor test.** Compiles every file in `src/` with an interpreter at
  or below `requires-python`, so this class of bug fails locally instead of in
  the release. Two earlier attempts at this guard silently passed:
  `ast.parse(feature_version=...)` accepts the syntax even at `(3, 8)`, and
  `py_compile` with `cfile='/dev/null'` raises `FileExistsError` before it
  reaches the parser. It uses the builtin `compile()` now, and is verified
  against the actual v0.2.8 defect.

## [0.2.8] - 2026-08-20

### Added

- **`swe find`.** The read-only counterpart to `swe init`: where the shared
  assets live and what links to them. `swe find amd` for AGENTS.md, `swe find
  skills` for skills and plugins, bare `swe find` for both.
  - `--root` / `-r` scans from wherever `~/.quiver` lives; the default scans the
    current directory. Both recursive.
  - `--scope=global|local|all`, default `global`. A file is global when it sits
    directly in a harness root (`~/.<tool>/`, `~/.config/<tool>/`) or is
    symlinked to the shared copy. On a real machine that is 10 files out of 112.
  - Files nested deeper inside a harness directory (plugin caches, vendored
    repos, editor extensions) are counted in every view and listed under
    `--scope=all`, so an unexplained jump stays visible.

### Removed

- **The `~/.agents` legacy alias.** The old shared skills root, plus the 74
  per-skill symlinks six harnesses kept pointing into it (42 of which were
  already broken). Those roots now link to `~/.quiver/skills` directly.

- **`swe mcp discover --prune`.** Removes hub servers that no harness
  configures any more. Without it they are reported and left in place.

- **Credential indirection for the MCP hub.** `~/.quiver/mcp.json` now stores
  `${NAME}` references; values live in `~/.quiver/secrets/.api_keys` (mode 600,
  gitignored), so the hub itself is safe to commit. `swe mcp sync` resolves on
  emit rather than relying on the environment, because Claude, Cursor and
  Antigravity launch from the Dock and never read a shell profile. `swe mcp
  discover` redacts on ingest, so reading a harness config cannot write
  plaintext back into the hub. Names absent from the store are reported and
  left literal rather than blanked, since an empty credential authenticates as
  nobody and looks like a server bug.

- **`--only` and `--except` accept globs on `swe mcp sync`.** Server names carry
  a category prefix, so `--only=dv__*` takes a category and `--only='*__*'`
  takes every categorised server, which is also the line between servers worth
  sharing and servers bound to one tool's own binary. A pattern that matches
  nothing is now named instead of failing silently, so a typo no longer looks
  identical to an empty result.

### Changed

- **`swe mcp discover --apply` now merges three ways.** It previously skipped
  anything already in the hub by name, so a token rotated in a harness config
  never reached the hub and a later `sync quiver --all` pushed the stale value
  back out over it. It now adds new servers, updates ones a harness has edited,
  and reports servers the hub still lists that no harness configures.

- **MCP moved under `~/.quiver`.** The server checkouts moved from
  `~/.mcp-servers` to `~/.quiver/mcp/servers` (2.7 GB, instant rename, symlink
  left behind), and all 79 absolute path references across 5 harness configs
  were rewritten. `mcp.json` now sits at `~/.quiver/mcp.json` rather than in
  `config/`: it is the control plane for a subsystem, not one more settings
  file. `mcp/servers/` is gitignored, since it is 21 nested git repos and
  `mcp.json` records how to rebuild it.

## [0.2.7] - 2026-08-20

### Added

- **`swe init --migrate`.** Moves a pre-0.2.7 `~/.config/swe` into `~/.quiver`,
  routing authored state to `config/`, caches to `cache/`, and leaving
  `completions/` and `reports/` at the root. Unrecognised files go to `config/`
  rather than being dropped. Existing destinations are never overwritten. Also
  writes a `.gitignore` so the root can be a git repo without committing cache.

- **`swe list --usage`.** Usage columns (100d, REMAINING) are now opt-in.
  They were the only part of the listing that touches the network. Implied by
  `--refresh`.

- **`swe list --links`.** Swaps the usage columns (100d, REMAINING,
  DESCRIPTION) for AGENTS.MD and SKILLS link status, so the table gets narrower
  (124 cols vs 146) rather than wider. Needs no network, so it also runs in
  ~83 ms against ~170 ms warm and ~870 ms on a cold rate-limit cache.

- **`swe init` and the `~/.quiver` layout.** One directory now owns the assets
  every harness duplicates. `~/.quiver/AGENTS.md` is symlinked into each harness
  under that harness's own filename (CLAUDE.md, QWEN.md, CRUSH.md, GEMINI.md),
  and `~/.quiver/skills` is symlinked in as every harness's `skills/`. Run
  `swe init --check` for a dry run, `--force` to back up and replace real files.
  Instruction filenames were confirmed against the installed binaries; harnesses
  whose convention could not be confirmed are skipped rather than guessed at.

- **Freebuff GLM 5.2 session usage in `swe list`.** Starred Freebuff rows read
  the CLI's authenticated, read-only session status and display remaining/total
  premium sessions plus the server-provided reset countdown without joining a
  session.
- **Sectioned interactive setup wizard.** `swe setup` now walks through
  harness discovery, provider credential coverage, MCP import, shared skills
  roots, coding-session report models, and final verification. Returning-user
  prompts preserve current values on Enter, `swe setup <section>` reruns one
  stage, and `--quick` skips configured work. Quiver creates timestamped
  backups before changing its registry, MCP source-of-truth, or general config;
  the existing `--apply`, `--json`, and non-interactive preview paths remain
  available for scripts.

- **Coding-session reports and durable follow-ups.** `swe report daily` and
  `swe report weekly` normalize local histories from all 20 session parsers,
  remove startup-only noise, reuse digest-cached summaries, batch short useful
  sessions by Git repository, and use separate inexpensive summarizer and
  strong writer models. Every run shows model-call and input-token estimates
  before confirmation; over-budget runs require typing `process all` exactly.
  Reports, manifests, cursors, exclusions, and manually controlled follow-ups
  live under `~/.config/swe/reports/`. `swe config` provides credential-free
  report setup and dotted get/set/unset/edit/check operations.
  Warning-bearing runs now print a report-specific
  `swe report warnings <manifest.json>` command, which validates that manifest
  and prints its numbered warning list without mixing in other reports.
- **Calendar filters for `swe session`.** `-d/--days`, `-w/--weeks`, and the
  inclusive `-s/--start` plus `-e/--end` range filter session history using
  local calendar boundaries. Date-filtered listings show every match unless a
  positional maximum is supplied.
- **Antigravity rate limits in `swe list`.** Quiver discovers a running
  Antigravity app or CLI backend and reads its loopback-only
  `RetrieveUserQuotaSummary` RPC without accessing Google OAuth credentials.
  It compares Gemini and third-party model buckets across weekly and five-hour
  windows, displays the most restrictive one, and favors five-hour usage when
  utilization ties. Existing outage caching keeps the last successful reading
  visible when Antigravity is closed.
- **Security: SSRF / LFI fix in `swe mcp <tool> status` health check.**
  `src/swe/mcp/cli.py::check_server_health` now validates that any
  URL it fetches via `urllib.request.urlopen` strictly starts with
  `http://` or `https://` (case-insensitive) before issuing the
  HEAD request. Without this guard, an attacker who can write a
  configuration file can pass a `file:///etc/passwd` URL and have
  Python happily open and stat a local file as a "health check" —
  turning a LFI probe into a single endpoint. The check applies to
  both `http` server types (direct URL field) and `wrapped` server
  types (URL inside `args`). (PR #2.)
- **Security: Bandit B324 / FIPS-compliant `hashlib.md5` in Kimi
  parser.** `src/swe/sessions/parsers.py::parse_kimi::load_hash_map`
  now passes `usedforsecurity=False` to the single
  `hashlib.md5(p.encode())` call — the hash is used purely to build
  a stable map-key from a path string, never for authentication or
  integrity. This silences the Bandit B324 rule (weak hash for
  security-related use) and is required for FIPS-mode Python builds.
  `usedforsecurity` was added in Python 3.9; the project's
  `requires-python = ">=3.10"` covers it natively. (PR #17.)
- **Performance: memoize `os.path.realpath` in the session
  aggregator's `cwd` filter loop.** `src/swe/sessions/aggregator.py::get_all_sessions`
  used to issue an `os.path.realpath` syscall on every session's
  path when filtering by `cwd` — O(N) disk stats for what is
  functionally O(U) unique paths (most sessions share a project
  root, and a project tree has a small number of unique
  realpaths). Now caches realpath results in a `dict[str, str]`
  local to the loop, and hoists `target_path + os.sep` out of the
  loop to avoid redundant string concatenation. (PR #1.)
- **Performance: replace `os.listdir` with `os.scandir` across all
  session engines.** `os.listdir()` + `os.path.isdir()` /
  `os.path.isfile()` chains issue a separate `stat()` syscall per
  entry to compute the file kind, multiplying latency in deeply
  nested agent session trees. `os.scandir()` returns DirEntry
  objects whose `is_dir()` / `is_file()` / `stat()` reuse cached
  metadata from the directory iteration itself, collapsing the N+
  syscalls into one. Touched files: `src/swe/sessions/parsers.py`
  (parse_cursor, parse_antigravity), `src/swe/sessions/engines/jsonl_engine.py`
  (5 sites), `src/swe/sessions/engines/json_engine.py` (2 sites —
  nested parent+session-dir loops), `src/swe/sessions/models_analytics.py`
  (3 sites — claude + freebuff paths). Every site is wrapped in
  `with os.scandir(...) as <var>_it:` so the directory FD is
  released on scope exit (caught and fixed during triage — bare
  iterators leaked FDs on early-break / exception paths). (PR #18.)

### Changed

- **One root: `~/.quiver`.** Quiver used to own two directories, `~/.quiver`
  for what harnesses read and `~/.config/swe` for its own state. They are now
  one, split by lifecycle instead: `config/` and the shared `AGENTS.md` and
  `skills/` are worth versioning, `cache/` and `backups/` are not. The root
  sits in `$HOME` rather than `$HOME/.config` to match the 24 of 28 harness
  config directories that live there. `paths.py` is the single owner of the
  layout; `quiver.init.layout` re-exports its helpers instead of redefining
  them.
- **`DATA_DIR_NAME` replaces `CONFIG_DIR_NAME`.** Set to `quiver`, so the
  directory is named for the project. `swe` remains the command. The old name
  is kept as an alias.

- **`swe list` no longer fetches rate limits by default.** Cold runs went from
  ~870ms to ~73ms, and the table narrowed from 146 to 128 columns. Pass
  `--usage` for the old view.

- **Shared skills root moved to `~/.quiver/skills`.** It was `~/.agents/skills`,
  which stays as a symlink so existing references keep resolving. `swe skills`
  gained 10 more harness roots (qwen, forge, cline, kiro, vibe, augment,
  continue, pi, grok, crush), all pointing at the one tree.

- `swe list` now labels provider quota as **REMAINING** and displays the
  percentage still available instead of the percentage already used.
- `swe list` now runs usage and rate-limit fetchers only for starred harnesses.
  Unstarred tools retain registry details and local session counts without API
  calls or loopback probes; refresh flags only refetch the starred set.
- `swe list -n` is now an alias for `swe list --refresh` / `-r`. It fetches
  new session counts and provider rate limits instead of using either cache.
- **Triage workflow for auto-generated Bolt/Sentinel PRs.** When the
  Sentinel/Bolt auto-agents open multiple PRs for the same finding
  (e.g. three Sentinel PRs all adding `usedforsecurity=False` to
  the same line), the workflow now cherry-picks the highest-quality
  version onto a fresh branch from `origin/main`, appends the
  `.jules/{sentinel,bolt}.md` entry to the existing main copy
  (preserving prior entries), and closes the duplicates with a
  one-comment explanation pointing at the canonical PR. This batch
  triage closed PRs #3, #4, #5, #6, #7, #8, #9 as duplicates and
  landed the canonical versions as #2, #1, #17, #18.

- **Column-border consistency across `swe` listings.** Iteration on the
  `quiver.table.Table` visual contract: `swe models` and
  `swe providers list` now render with the same ` │ ` visible-bar
  column boundaries as `swe list` (via `Table(column_gap=" │ ")`,
  matching the gold-standard pattern in
  `harness/commands.py::cmd_list`). Cross-Table regression tests in
  `tests/test_sessions_commands.py::CmdModelsMigrationTest` and
  `tests/test_providers_commands.py::CmdListMigrationTest` pin the
  substring so a future revert to bare `Table()` is caught even if
  header/row visible-width parity still passes. The 2-column
  `providers::cmd_info` definition list intentionally stays at a
  single-space gap (column gap = 1) — the colon on each label already
  does separator work, and forcing a ` │ ` border there would clash
  with definition-list semantics. cmd_session and `cmd_check` are
  out of scope for this batch (a follow-up sweep is queued).
- **Two session handlers migrated to `quiver.table.Table`**: `cmd_models`
  and `cmd_session` (sessions domain). Removed the last hand-rolled
  ``f"{...:<{w}}"`` patterns in ``sessions/commands.py``. Eliminates
  the magic ``+9`` width offsets the old code used to compensate
  for ANSI-overhead in f-string padding — Table handles ANSI-aware
  width math natively.
  - `cmd_models` builds a Table once and renders it for either the
    3-column default mode (MODEL | PROVIDER | MSGS) or the 4-column
    ``--by-tool`` mode (TOOL | MODEL | PROVIDER | MSGS). The MSGS
    column uses ``kind="count_threshold"`` with ``threshold=100``, so
    cells ≥100 pick up green ANSI automatically — the green-cell logic
    moves into the kind renderer rather than being a per-row print
    imperative.
  - `cmd_session` builds a 5-column Table (IDX | LAST ACTIVE | AGENT |
    DIRECTORY | TITLE/SUMMARY). The four human-derived cells (IDX,
    LAST ACTIVE, AGENT, TITLE) use ``kind="preformatted"`` +
    ``trust_cell_width=True`` so they carry their own colour without
    ANSI bleed across column gaps. The DIRECTORY cell uses
    ``kind="text"``+``fit="content"`` so the longest visible path
    drives the column width adaptively (replacing the old
    ``max(45, max_path_len + 4)`` imperative calc). The TITLE cell
    pins a 50-char cap via ``max_width=50`` so long titles truncate
    in the Table instead of via an inline ``truncate()`` call.
    Search-filter footer lives BELOW the table as plain print() since
    it is a post-table notice, not a row. The ``use <N>`` resume
    branch is preserved verbatim — it still calls ``cmd_use`` after
    chdir.
  ~15 new tests in ``tests/test_sessions_commands.py``
  (``CmdModelsMigrationTest``, ``CmdSessionMigrationTest``) pin:
  3-vs-4 column mode, separator vs. header width, count_threshold
  green colour decision, summary footer math (grand total / model /
  tool counts), 5-column header order, body-row visible-width parity
  (regression guard), idx cell bracket padding for single-digit
  indices, all four relative-time branches ("Just now" / "5m ago" /
  "3h ago" / "2d ago"), cyan time + green agent colour escapes,
  tilde path substitution, dim title fallback for empty-title
  sessions, search-filter footer conditional rendering.
- **Three more `cmd_*` handlers migrated to `quiver.table.Table`**: `cmd_info`,
  `cmd_aliases`, `cmd_tags` (harness domain). Removed the last hand-rolled
  `f"{...:<{w}}"` string interpolation patterns in `harness/commands.py`.
  - `cmd_info` is now a 2-column `FIELD | VALUE` table; the `VALUE` column
    uses `kind="preformatted"` + `trust_cell_width=True` + `fit="content"`
    so colour-wrapped status values stay clean and variable-width paths
    expand instead of getting truncated.
  - `cmd_aliases` is a 2-column `ALIASES | NAME` table with `column_gap=" → "`
    so the arrow separator is part of the table itself (every row gets the
    same horizontal alignment structurally, not via padded f-string).
  - `cmd_tags` is a 2-column `TAG | TOOLS` table; `TAG` cell is cyan via
    `cpad("cyan", tag, 14)` exactly like the harness aliases column, and
    `TOOLS` uses the `list` kind with dim colour so multi-tool rows join
    with `, ` and re-fit. Empty-tag fixtures emit a single `-line notice
    instead of an empty table.
  ~17 new tests in `tests/test_harness_commands.py` (`CmdInfoMigrationTest`,
  `CmdAliasesMigrationTest`, `CmdTagsMigrationTest`) pin: column order,
  separator-width parity with header, body-row visible-width parity with
  header (regression guard), conditional rows (`Notes` only when defined,
  empty-aliases tools omitted), cyan/dim colour shapes, alphabetical tag
  order, alphabetical tool list per row, multi-alias comma-join.
- **Two skills handlers migrated to `quiver.table.Table`**: `cmd_skills_scopes`
  and `cmd_skills` (skills domain). Removes the last hand-rolled
  ``f"{...:<{w}}"`` patterns in ``skills/commands.py`` and eliminates
  the magic ``+9`` width offsets the old code used to compensate
  for ANSI-overhead in f-string padding.
  - `cmd_skills_scopes` is a 4-column `SCOPE | KIND | SKILLS | PATH`
    table. `SCOPE`/`KIND`/`SKILLS` use `kind="preformatted"` +
    `trust_cell_width=True` and are routed through `cpad` so each
    cell visibly matches its column width. `KIND` is yellow when
    the entry is a symlink, dim when the entry is an alias of another
    scope, green when the entry is a plain directory. `SKILLS` is
    right-aligned and green when `>0`, dim `"0"` when zero.
    `PATH` uses `kind="preformatted"` + `fit="content"` so the
    dim-coloured ``  → tgt`` link-note arrow is preserved end-to-end
    (the `text` kind strips ANSI before measuring and would silently
    drop the colour escapes). The dash-separator measures via
    `visible_len` so it spans row-to-row even when arrows carry
    visible-length ANSI escapes.
  - `cmd_skills` is a 3-column `NAME | SCOPE | VISIBLE_VIA` Table.
    All three columns use `kind="preformatted"` +
    `trust_cell_width=True` and are routed through `cpad` so each
    cell visibly matches its column width. `NAME` is bold, `SCOPE`
    cyan, `VISIBLE_VIA` cyan when reachable via multiple scopes
    (comma-joined) and dim otherwise. The VISIBLE_VIA width is
    pre-measured across the current skill set (`max(len("VISIBLE VIA"),
    max(len(via_text))`) and that exact value is passed to both
    `add_column(width=...)` and the per-row `cpad(..., width=...)`
    call — column and cpad agree by construction so every body row's
    visible cell width matches the header without drift. `PATH` and
    `DESCRIPTION` (with `--desc`) are NOT table columns — they are
    emitted as plain `print()` lines below each rendered row,
    indented 28 + 2 + 14 + 2 = 46 spaces so they visually align
    under the `VISIBLE_VIA` header column. The 2-3-line-per-skill
    layout from the pre-migration era is preserved; only the
    grid-row stripe goes through Table.render().
  ~17 new tests in `tests/test_skills_commands.py`
  (`CmdSkillsScopesMigrationTest`, `CmdSkillsMigrationTest`) pin:
  4-column header order for scopes; separator vs. header width;
  body-row visible-width non-exceeding-header (regression guard
  for the preformatted+content no-pad case where short paths leave
  the column right-aligned); symlink/alias/directory KIND colours;
  green/dim SKILLS-count branch; dim-coloured PATH arrow round-trip
  (kills the silent-ANSI-strip regression in the previous `text`
  kind pass); no-arrow for plain directories; footer `4 roots ·
  3 unique skills` math; 3-column default-mode header (no PATH,
  no DESCRIPTION columns); separator-width parity; body row
  rendered-cell parity to header width (each row arrives at the
  full visible width via `cpad`); multi-scope cyan / single-scope
  dim visible_via colour shapes; bold NAME + cyan SCOPE escape
  codes; PATH sub-line below each row, indented 46 spaces, in
  `dim` colour; PATH uses tilde substitution for `$HOME`; blank
  line between skills; filter drops non-matching; empty filter
  result notice; alphabetical sort within scope; long-name
  truncation at 28 chars; `--desc` flag emits description sub-line
  indented 46 spaces below PATH and skips empty descriptions.
- **`swe check` migrated to the new `quiver.table.Table` component.**
  Replaced hand-rolled `f"{name:<22}{alias_str:<20}"` string interpolation
  with a single 4-column `Table().add_column(...).add_row(...)` build
  (STATUS | NAME | ALIASES | INFO). STATUS uses `kind="preformatted"`
  + `trust_cell_width=True` for the green ✓ / red ✗ glyphs (mirrors
  cmd_list's INST column). INFO uses `kind="preformatted"` with explicit
  pre-pad to the 24-char column width so variable-width version strings
  ("0.5.91" vs "version unknown") keep the grid aligned. ALIASES reuses
  cmd_list's `kind="list"` + `color="cyan"` + `empty="—"` recipe. The
  off-PATH diagnostic block still lives BELOW the table as plain print()
  because its multi-line fix recipes don't fit the grid; only the table
  body went through Table.render(). The heal side-effect (write
  `live_version` back to `tools[name]["version"]`) is preserved
  verbatim, running BEFORE the row is constructed so the displayed
  version reflects what was just probed. ~13 new tests in
  `tests/test_harness_commands.py::CmdCheck*Test` pin the migration:
  column order, separator/header/body visible-width parity, green/red
  status colors, dim version text, `version unknown` fallback, three
  heal-side-effect cases (live overrides stored, live matches stored,
  dirty stored value cleared), off-PATH footer conditional rendering
  (with/without orphans), alphabetical row order, and bold header line.

- `swe list` now shows a **RATE** column with usage percentage and reset countdown for tools that expose rate limit APIs. Currently supports:
  - **Codex** via ChatGPT `backend-api/wham/usage` endpoint using OAuth
    tokens from `~/.codex/auth.json`
  - **GitHub Copilot** via `api.github.com/copilot_internal/user` using
    the OAuth token from `gh auth token`
  - **Claude Code** via `api.anthropic.com/api/oauth/usage` — surfaces
    whichever of the three Claude windows (5h / 7d / 7d_sonnet) has the
    highest utilization; OAuth token sourced from
    `~/.claude/.credentials.json` (Linux/portable) or the macOS
    Keychain entry `Claude Code-credentials`. A `_BETA_VERSIONS` table
    backs the `anthropic-beta: oauth-2025-04-20` header and prints a
    single-line stderr hint on 401 so the operator can rotate the
    beta-string when Anthropic bumps it.
  - **Droid (Factory AI)** via `api.factory.ai/api/billing/limits` —
    surfaces whichever billing window (`standard` / `premium` / etc.)
    has the highest utilization. Auth ladder: `FACTORY_API_KEY` env
    var (free, CI-friendly, takes priority) → macOS Keychain
    `Factory Safe Storage` → `Factory Key`. Headers include the
    `api.factory.ai` URL but `Origin: https://app.factory.ai` to
    match the web-app signature the WAF gates on. `RateLimitInfo`
    gained a `window: str = ""` field so the per-tool window label
    (`5h` / `7d` / `7ds` / `std` / `prm` / ...) surfaces inside the
    14-char RATE cell alongside the percentage.
  Pluggable architecture in `harness/rate_limits.py` allows adding more
  fetchers. Cached in `rate_limits_cache.json` (5-minute TTL);
  `swe list --refresh` bypasses the cache.
- `swe edit` interactive mode now shows a warning and continues the edit
  loop when an alias collision is detected on save, instead of exiting the
  program. The user can fix the alias and save again.
- `swe mcp sync` now works between any two registry harnesses, not just the
  10 with explicit MCP config entries. Harnesses without a verified entry
  (hermes, kiro, amp, crush, qwen-code, augment, continue, grok, etc.) get
  an optimistic default at `~/.<tool>/mcp.json` with the standard
  `mcpServers` shape, flagged `unverified`. Sync creates the file if absent
  and prints a note about the guessed path.
- Factory Droid (`droid` / `df`) is now a verified MCP peer, syncing to and
  from `~/.factory/mcp.json`. Remote servers get an explicit `type: "http"`
  field, which Droid requires.
- CI expanded with e2e smoke test step that exercises all major subsystems
  (`swe list`, `swe session`, `swe models`, `swe skills`, `swe mcp list`,
  `swe providers list`, `swe __complete`) against the installed binary —
  catches stale-install bugs where new modules are invisible to `swe`.
- AGENTS.md, CONTRIBUTING.md, and README.md now document the e2e
  verification & reinstall process required for features that add files or
  change command handlers.
- New **`quiver/table.py`** module: declarative, pluggable table renderer
  for the CLI. Provides ``Table().add_column(...).add_row(...).render()``
  with three width-fit modes (``fixed``, ``content``, ``bounded``) and six
  built-in kinds registered via ``@register_kind``: `text`, `number`,
  `count_threshold`, `list`, `timestamp`, `preformatted`. ANSI-safe width
  math (cells painted with ``c(...)`` are stripped before byte-truncating,
  so colour never bleeds across column gaps). Header + ``─`` separator
  auto-sized to the table's total visible width.
- **`swe mcp list` and `swe mcp status` matrix view migrated to the
  new `quiver.table.Table` component.** Replaced hand-rolled
  `f"{'SERVER':<{server_width}}  {t:^{col_width}}"` padding with a
  single `Table().add_column(...).add_row(...)` build per handler.
  Both handlers emit the same server × tool matrix layout:
  - SERVER column uses `kind="text"` so plain server names left-align
    and pad to `server_width` (pre-measured = max of header label
    + longest server name).
  - 1..N tool columns use `kind="preformatted"` + `trust_cell_width=True`
    and are routed through `cpad` so each cell visibly matches the
    pre-measured `col_width`. Header labels are pre-centered
    (`tool_name.center(col_width)`) so the rendered line matches the
    original f-string `^{col_width}` alignment without a custom kind.
  - Each tool cell is `cpad("green", "✓", col_width)` if the server
    is present in that tool, else `cpad("dim", "—", col_width)`.
  `cmd_status` adds a trailing HEALTH column with the result of
  `check_server_health()` (already ANSI-coloured) using
  `kind="preformatted"` + `trust_cell_width=True` + `fit="content"`.
  Width is pre-measured to the longest health-string `visible_len`
  so messages like `✗ missing env: FOO,BAR` don't misalign the
  trailing columns. ~13 new tests in
  `tests/test_mcp_commands.py::CmdListMatrixMigrationTest` +
  `CmdStatusMatrixMigrationTest` pin: header order matches tool
  keys, separator visible length matches header visible length,
  body rows all share header visible width (cpad-driven parity),
  green ✓ for present servers / dim — for absent, single-target
  filter renders a 2-column table with `1 tools` footer, empty
  matrix case prints only the `No MCP servers found.` notice,
  footer `N servers across M tools` math, HEALTH column header
  presence on cmd_status, ANSI green escape round-trip for the
  HEALTH cell when server is healthy.
- **`swe providers list` migrated to the new `quiver.table.Table` component.**
  Replaced hand-rolled `f"{...:<{w}}"` string interpolation with a single
  5-column `Table().add_column(...).add_row(...)` build (PROVIDER |
  ALIASES | ENV VAR | API KEY | URL). All five columns ship pre-coloured
  ANSI strings via `kind="preformatted"` + `trust_cell_width=True`, and
  each cell is routed through `cpad` so it visibly matches its column
  width. The column widths are pre-measured across the current row set
  via a single pre-measure pass that builds parallel text (+ colour)
  lists and takes `max(header_label_len, max(visible_len(cell)))` for
  each column. The same exact width flows to both `add_column(width=...)`
  AND the per-row `cpad(..., width=...)` call so column and cpad agree
  by construction — every body row arrives at the column visible
  width. Previously hardcoded:
  - PROVIDER width 14 → pre-measured, capped at 24 chars (long names
    truncate cleanly without breaking the grid).
  - ALIASES, ENV VAR, API KEY widths → pre-measured (no dead config,
    no width-math drift across rows).
  - URL width 28 → `kind="preformatted"` + `fit="content"` so the
    column grows to the longest observed URL; since URL is the
    rightmost column, missing auto-pad on shorter URL cells does not
    misalign any trailing column.
  The description sub-line (when `--desc` is passed) is now emitted as
  plain `print()` below the rendered row, indented
  `2 + provider_w + 2` spaces so it visually aligns under the ALIASES
  column header (the description logically extends the alias/identity
  block). This restores a 2-line-per-provider layout similar to the
  cmd_skills batch-3 migration. URL prefix stripping
  (`https://` / `http://` / `www.`) is preserved, the empty-aliases
  em-dash fallback (`—`) is preserved, and the env-var `(+N)` suffix
  for multi-env-var providers is preserved. ~17 new tests in
  `tests/test_providers_commands.py::CmdListMigrationTest` pin:
  5-column header order, separator width parity, body rows aligned to
  header visible width (cpad-driven parity), ANSI colour shapes per
  column (bold PROVIDER, dim ALIASES / ENV VAR, dim/green API KEY
  conditional on masked key present, cyan URL), URL prefix-stripping,
  em-dash fallback for empty aliases, matched-env rendering in
  shell-export layout, `--desc` flag emissions and indent,
  empty-description suppression, PROVIDER width cap at 24 chars,
  filter correctness, no-keys Tip advisory order, no-keys-dir advisory
  order, footer `N/N providers with keys` math, raw-key
  non-leakage.
- **`swe providers info` migrated to the new `quiver.table.Table` component.**
  Replaced the hand-rolled ``f"  {'  ' + label + ':':<20} {val}"`` loop in
  ``cmd_info`` with a single 2-column ``Table()`` build (LABEL | VALUE).
  ``column_gap=1`` preserves the original 1-space gap between the label box
  and the value. The label column width is pre-measured to
  ``max(len("  LABEL:"))`` across all rows (including the ``Key file`` row
  that previously was emitted outside the loop with its own inline pattern
  — now merged into ``rows_out`` so the full lookup-list renders through the
  single Table renderer). All cells use ``kind="preformatted"`` +
  ``trust_cell_width=True`` so cyan labels, green ``Key status``, and cyan
  ``Key file`` path flow through Table unmodified. The body rows only
  (header + separator skipped) are emitted because a definition list has
  no anchoring value from a header line. The ``No key found.`` advisory
  below the table, the leading ``Name`` header line, and the trailing
  ``Usage``/not-found red-error paths are all preserved verbatim. ~17 new
  tests in ``tests/test_providers_commands.py::CmdInfoMigrationTest`` pin:
  label column is cyan (\033[36m), body rows all share the same visible
  width (alignment parity), ``Key status`` is green ANSI when a masked
  token is present and is a plain ``-`` when not, ``Key file`` row is
  appended with cyan path when ``row["key_file"]`` exists and skipped
  when null, ``Env vars`` displays matched env first with fallbacks list
  in dim, ``Aliases`` filters out the canonical slug itself, em-dash
  fallback for empty aliases / empty description / empty URL / empty
  ``key_filename``, ``No key found.`` advisory conditional on
  ``row["key_file"]`` + missing ``raw_key``, red ``Usage:`` error on empty
  args and on unknown provider (return code 1), help-flag short-circuit
  → 0, raw key never leaks to stdout.
- **`swe list` migrated to the new `quiver.table.Table` component.**
  Replaced the hand-rolled `f"{...:<{w}}"` string interpolation with a
  single 9-column `Table().add_column(...).add_row(..., accent=...)`
  build, removing the favourited-vs-unfavourited print-loop divergence.
  Starred rows pass `accent="neon"` (rows 1–3 are prefix-pinned); the
  RATE column uses `trust_cell_width=True` so `RateLimitInfo.format_column()`
  controls its own width without Table re-padding. 12 new tests in
  `tests/test_harness_commands.py` pin the migrated layout: header
  column order, separator-width alignment with header, starred vs.
  unstarred row NAME alignment at the same visible column index (the
  whole reason for the migration), sentinel `_text`-strip-ANSI safety,
  three-state SESS coloring, green/red INST glyphs, sort order
  preservation, and tag-filter behavior.

### Changed

- `swe mcp list` / `status` / `validate` / `doctor` hide unverified
  harnesses whose config file does not exist yet, keeping the matrix
  readable. Name a harness explicitly to inspect it.
- `swe mcp sync <src> --all` broadcasts to verified peers plus unverified
  tools that already have a config file; it no longer risks creating many
  new files in one shot.
- `swe list --refresh` now also invalidates the rate limits cache (in
  addition to the session cache).

### Fixed

- **`mcp.json`'s `updated` timestamp never changed.** It used `setdefault`, so
  only the first write was ever stamped; the file claimed to be hours older
  than its contents.

- **Deleted 331 KB of dead source from the config directory.** `mcp.py`,
  `mcp_formats.py`, `mcp_server.py`, `tests/`, `__pycache__/` and
  `archive/swe.py` were left over from the single-file era. Nothing referenced
  them: not the package, not any MCP config, not the shell profiles.

- **Rate-limit cache never covered providers that report nothing.** A fetcher
  returning no data was absent from the cache, so it counted as a miss and
  re-ran on every invocation. For antigravity that meant spawning two
  subprocesses per `swe list`, 70ms even on a warm cache. Negative results are
  now remembered for the same TTL, taking the warm path to 0.1ms. Fetchers that
  hit the deadline are still retried rather than recorded as empty.

- Antigravity 2.3.x quota discovery now carries the per-language-server
  `--csrf_token` into the loopback `RetrieveUserQuotaSummary` request. Quiver
  previously found the correct listeners but received HTTP 401 responses, so
  the RATE cell stayed blank even when Antigravity was running.
- Claude Code usage is fetched again for paid subscriptions instead of always
  rendering the temporary `no-sub` placeholder. The RATE cell shows the most
  utilized 5-hour, 7-day, or 7-day Sonnet window, treats Anthropic's
  `utilization` value as an existing 0-100 percentage, and retains the last
  successful reading during a 429 `Retry-After` cooldown. HTTP diagnostics now
  also survive the macOS certificate fallback path. Expired, non-refreshable
  Claude credentials now render `re-login` instead of a blank RATE cell.
- Forced rate-limit refreshes retain each provider's last successful reading
  for up to 24 hours when its network request fails or misses the shared
  deadline. A slow refresh can no longer replace the complete RATE cache with
  an empty snapshot; per-provider timestamps prevent stale readings from being
  renewed merely because another provider succeeded.
- Droid now displays an exhausted monthly or weekly quota when that longer
  window is blocking usage, instead of always showing the available 5h quota.
  When no window is exhausted, 5h remains the default. The reset countdown
  now corresponds to the displayed window. Factory's current
  `usedPercent` field is treated as usage directly; the previous inversion
  incorrectly turned a live `100%` weekly lock into `0%` usage.
- Droid rate-limit lookups now have a two-second wall-clock deadline, so a
  stalled DNS lookup or Factory API request cannot freeze `swe list`. Slow or
  unavailable responses degrade to an empty RATE value for that cache refresh.
  All provider lookups now run concurrently under the same two-second overall
  budget, and empty results are cached to avoid repeated waits during outages.
- Rate-limit fetchers now print actionable stderr hints on HTTP errors:
  401 (stale token — re-auth required), 429 (parsed ``Retry-After``
  countdown so the user knows exactly when to retry), 403/404 (the
  URL or ``x-factory-client`` header may need updating), and 5xx
  (upstream or network error). Previously, an unreachable endpoint
  silently produced a `—` in the RATE column with no signal as to why;
  running `swe list --refresh 2>&1` now surfaces the root cause. The
  Claude fetcher's hint for 401 includes the offending
  ``anthropic-beta`` value so the operator can rotate it in
  `_BETA_VERSIONS`. The Droid hint for 401 names the macOS Keychain
  label (`Factory Safe Storage`) plus the `FACTORY_API_KEY` env var
  override so the user has the exact commands to fix it. The `_fetch_json`
  helper's `on_401` callback contract is preserved; an `on_http_error`
  callback is added as a new optional kwarg that defaults to None.
  Replaces the previous silent-degrade pattern in both `harness/rate_limits.py`
  -type fetcher wrappers.
- Rate limit fetcher now retries with an unverified SSL context when the
  default certificate verification fails. This fixes `SSL:
  CERTIFICATE_VERIFY_FAILED` errors on macOS python.org builds (Python 3.12+
  ships without system CA certificates until "Install Certificates.command"
  is run). The connection remains encrypted; only server-cert pinning is
  skipped as a fallback.
- Fixed Python exception hierarchy bug in `_fetch_json`: `URLError` (a
  subclass of `OSError`) was being caught by the `OSError` handler before
  the dedicated `URLError` handler could trigger the SSL retry, making the
  fallback dead code. Subclasses must be caught before their base classes.
- `_parse_iso8601_to_epoch` now correctly parses ISO 8601 timestamps with
  fractional seconds combined with a timezone offset (e.g.
  `"2026-08-01T00:00:00.000Z"`, `"...+00:00"`) on Python 3.10 — previously
  only Python 3.11+ accepted that combination. Without the fix, the parser
  silently returned `0.0` and the RATE column rendered `—` for the reset
  countdown.
- Naïve ISO 8601 timestamps (e.g. `"2026-08-01T00:00:00"`) are now
  explicitly treated as UTC. Previously, `datetime.fromisoformat(...).timestamp()`
  silently applied **local time** on naïve strings, producing a reset
  countdown that shifted with the user's TZ. Codex's parser was refactored
  to use the shared helper, eliminating a duplicated buggy inline parser.
- `_fetch_codex` now uses `not isinstance(reset_at, bool)` as a guard
  inside the numeric `reset_at` branch. Without it, a pathological
  `reset_at: true` payload would silently become `1.0` (since `bool`
  subclasses `int` in Python). Same fix applied conceptually to
  `_derive_copilot_fields` for `percent_remaining` and `entitlement`.
- Test fixture isolation: `test_fetch_codex_reset_at_type_dispatch` now
  uses `copy.deepcopy` on the shared `_SAMPLE_RESPONSE` fixture before
  mutating nested keys. A previous shallow-copy version leaked the
  per-subtest `reset_at` mutation into `_SAMPLE_RESPONSE`, which corrupted
  `test_fetch_codex_success` when the suite ran in alphabetical order.

## [0.2.6] - 2026-07-17

### Added

- `swe autocomplete [zsh|bash|fish]` — generate and inject shell tab-completion
- `swe __complete <words>` — hidden command powering dynamic completions (tool names, aliases, tags, flags)
- `swe star` / `swe unstar` — favourite harnesses, pinned with neon highlight at top of `swe list`
- `swe edit <name> [--field val]` — interactive or flag-based registry field editor
- `swe doctor` — diagnose Node/npm/nvm PATH issues hiding global installs
- `swe install <name>` — install a harness via PATH-visible npm and register it
- `swe providers` — API key management for 27+ LLM providers (metadata only, no key strings)
- `swe session --search` / `-q` — filter sessions by title, path, agent, or session ID text
- `swe list --refresh` / `-r` — bypass session cache and re-parse all sessions
- Three reusable session parser family engines: SQLite (`sqlite_engine.py`), JSONL (`jsonl_engine.py`), JSON (`json_engine.py`)
- 20 session parsers: opencode, Claude Code, Gemini/Antigravity, Codex, Cursor, pi, Freebuff, Droid, Copilot, Continue, Crush, Amp, Kimi, Hermes, Grok, Cline, Forge, Mimo, Tau
- `identity.py` — centralized launch/registry tool mapping (antigravity launches via gemini)
- Disk cache (`session_cache.json`, 60s TTL) for session counts in `swe list`
- `prompt.py` — CR-safe `read_line()` for interactive prompts (fixes `^M` on Enter in cooked TTY)
- Hermes title cleanup: extracts actual task from cron/skill boilerplate

### Changed

- Generalized one-off parsers into declarative engine configs with per-tool callbacks
- `parsers.py` slimmed to config-only adapters over family engines
- `commands.py` and `usage.py` import from central `identity.py` instead of local mappings
- README updated with autocomplete, favourites, providers, doctor, install, edit, and --search/--refresh docs

## [0.2.5] - 2026-07-06

### Added

- `swe skills help <topic>` — per-topic help (catalog, discover, tree, link, unlink, move, scope)
- README **Skills** section with discover, catalog, symlink, and move workflows

### Changed

- Centralized skills help in `skills/help_text.py` (used by subcommands and `swe skills help`)
- Expanded `swe help skills`, `swe help setup`, and `swe help discover`
- CONTRIBUTING and tests README updated for package layout and new test files

## [0.2.4] - 2026-07-06

### Added

- `swe skills tree` — show harness symlink layout (codex/claude/cursor → shared)
- `swe skills tree --sync` — persist observed symlinks to `skill_links.json`
- `swe skills link` / `unlink` / `move` — manage symlinks and move skills between roots
- Skills list shows **VISIBLE VIA** (all harness scopes that see each skill)

## [0.2.3] - 2026-07-06

### Added

- `swe skills discover` — scan `~/Desktop` and `~/Documents` for `*/skills/` catalogs
- `swe skills catalog add|list|remove` — register extra skill directories in `skill_catalogs.json`

## [0.2.2] - 2026-07-06

### Added

- `swe mcp discover` — find MCP servers across tool configs vs `~/.config/swe/mcp.json`
- Skills-root symlink hints in `swe setup` (step 3/3)
- `assets/social-preview.png` (1280×640) for GitHub link previews
- `scripts/upload_github_images.py` — upload avatar/social preview via GitHub token

### Changed

- `swe setup` now covers harnesses, MCP, and skills roots in one wizard
- README commands table documents `setup`, `discover`, and `harness discover`

## [0.2.1] - 2026-07-06

### Added

- `swe harness discover` — scan PATH for AI coding CLIs not yet in `tools.json` (dry-run by default)
- `swe harness discover --apply` / `--apply-all` — register discovered harnesses
- `swe setup` — thin onboarding wizard with optional interactive confirm
- `swe discover` alias for `swe harness discover`
- Repo mascot (`assets/mascot.png`) in README

## [0.2.0] - 2026-07-06

### Changed

- Refactored monolithic `cli.py` into domain packages: `harness/`, `sessions/`, `skills/`, `mcp/`
- Extracted shared `console.py` and `paths.py` utilities
- Added unit tests for harness registry, sessions aggregator, models analytics, and skills discovery (30 tests total)
- CI runs tests verbosely across Python 3.10–3.13

## [0.1.0] - 2026-07-06

### Added

- Initial open-source release of **quiver** (CLI command: `swe`)
- Tool registry with tags, aliases, install checks, and 100-day usage sorting
- Launch any registered AI coding CLI via `swe use`
- Cross-agent session listing and resume (`swe session`)
- Model usage analytics mined read-only from tool logs (`swe models`)
- Agent skills discovery across shared, Cursor, Claude, Codex, and plugin roots (`swe skills`)
- MCP server sync, diff, validate, and doctor commands (`swe mcp`)
- Stdlib-only core; optional FastMCP server extra for session history MCP tool
- Test suite (15 unit tests) with isolated `$HOME` fixtures

[Unreleased]: https://github.com/starship-terminal/swe/compare/v1.2.2...HEAD
[1.2.2]: https://github.com/starship-terminal/swe/releases/tag/v1.2.2
[1.2.1]: https://github.com/starship-terminal/swe/releases/tag/v1.2.1
[1.2.0]: https://github.com/starship-terminal/swe/releases/tag/v1.2.0
[1.1.1]: https://github.com/starship-terminal/swe/releases/tag/v1.1.1
[1.0.0]: https://github.com/starship-terminal/swe/releases/tag/v1.0.0
[0.2.9]: https://github.com/starship-terminal/swe/releases/tag/v0.2.9
[0.2.8]: https://github.com/starship-terminal/swe/releases/tag/v0.2.8
[0.2.7]: https://github.com/starship-terminal/swe/releases/tag/v0.2.7
[0.2.5]: https://github.com/starship-terminal/swe/releases/tag/v0.2.5
[0.2.2]: https://github.com/starship-terminal/swe/releases/tag/v0.2.2
[0.1.0]: https://github.com/starship-terminal/swe/releases/tag/v0.1.0
