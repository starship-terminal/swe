# Data model

Every persistent store `swe` and the SWE app touch, what each holds, and
how long its data is trusted. `paths.py` is the single source of truth for
the `~/.swe` layout; `AppModel.swift` names the app files under `cache/`
and `config/`.

"Rebuildable" means the file can be deleted and regenerated from source data
(transcripts, harness configs, the network). Files marked "authored" cannot
be rebuilt; deleting them loses user state.

## Store inventory

### Authored state (must never be dropped)

| path / key | owner | format | holds | written by | read by |
|---|---|---|---|---|---|
| `config/harness.json` | swe | JSON object keyed by harness name | registry: state (`active`/`starred`/`archived`), install evidence, capabilities, order | `harness/registry.py` only; versioned in the hub, and `swe sync` merges two machines' copies with `registry.merge_registries` | every swe command, the app via `swe list` |
| `config/config.json` | swe | JSON object | `swe config` values (report settings etc.), secret keys rejected | `configuration.save_config` | `configuration.load_config` |
| `mcp.json` | swe | JSON | hub MCP server definitions (`type` is the `swe mcp set-type` label, except on a URL entry where it stays the `sse` transport marker), `${NAME}` secret refs; the `swe` block holds the per-harness switches (`disabled`, `disabled_defs`), `master_off` — `{server: [harnesses]}` a bare `mcp disable` switched off, so a bare `mcp enable` restores exactly those — and `adopted_from` — `{server: harness}` the sync/discover/disable-stash adoption provenance | `swe mcp` commands | `swe mcp`, app via `swe mcp list` |
| `config/providers.json` | swe | JSON | provider registry, seeded from built-ins (`kind`: `inference`, `infra` or `services`), `_removed` list blocks re-merge | `providers/registry.py` | `swe providers`, doctor |
| `config/skill_catalogs.json` | swe | JSON list | extra skill catalog paths | `swe skills catalog` | `skills/discovery.py` |
| `config/skill_links.json` | swe | JSON `{links, adopted_from?, updated}` | recorded harness skill symlinks (`kind: "symlink"`) and per-skill selections (`kind: "selection"` with `mode: "except"|"only"` plus a `disabled`/`enabled` name list); `adopted_from` is `{skill folder name: donor harness}` — the root a `swe init` / `swe skills adopt` pass moved that shared skill out of | `skills` link ops, `skills adopt` | `swe skills`, `init --check` |
| `config/plugin-display-names.json` | app | JSON object | marketplace display-name overrides | `PluginDisplayNames` (SWECore) | plugin explorer, skill detail |
| `accounts/profiles.json` (+ `profiles.lock`, `<vendor>/<id>/home/`) | app | JSON + per-profile home dirs | extra harness accounts the app manages | `ProfileStore` | accounts page, ledger roots, session listing |
| `accounts/harness-names.json` | app | JSON `{schema, names}` | account names for harness cards, app-owned (renaming in Accounts never writes harness.json) | `HarnessAccountNameStore` | accounts cards, menu bar, widgets |
| `secrets/mcp/.api_keys` | swe | shell-style file, mode 600 | MCP credential values (was `secrets/.api_keys`, now a symlink here) | you, by hand | MCP format writers |
| `secrets/providers/.api_keys` | swe + app | shell-style file, mode 600 | LLM and infra API keys (was `~/.api_keys`, now a symlink here; `--api-keys-dir=DIR` may name a directory of per-provider files instead) | app's key editor | `swe providers`, app key checks |
| `data/ledger/ledger.sqlite` | app | SQLite (WAL) | deduplicated token events, dimension rows, scan cursors. Rows outlive their source logs (Claude Code prunes transcripts after 30 days), so this is the only copy of that history: never delete it to "reset" usage. A schema change archives it first (see below) | `Ledger` scans | Overview, Sessions, Calendar, menu bar, widgets |
| `data/ledger/backups/ledger-YYYY-MM-DD.sqlite` | app | SQLite, one file each | a copy of the ledger per local day through SQLite's online backup API, newest 7 kept | `Ledger.keepBackedUp` (hourly check) | restore by hand: quit SWE, copy one over `ledger.sqlite` |
| `data/web-ledger/ledger.sqlite` | swe (hub) | SQLite (WAL) | the web app's usage ledger: `events` keyed by the same dedup keys as the app's ledger, `files` read cursors, `meta`. Its own file, never the app's `data/ledger/`, so both can run on one Mac. Rows outlive their logs too | `ledger/scan.py`, when a `/v1/app/*` page asks | the web app's Overview (`docs/web-app.md`) |
| `cache/web-ledger/litellm.json` | swe (hub) | JSON | LiteLLM's price list, downloaded at most once a day; the snapshot in `ledger/pricing.json` stands in when it is missing | `ledger/pricing.py` | web ledger pricing |
| `data/search/search.sqlite` | app + swe | SQLite (WAL, FTS5) | per-message transcript text for session search: `sessions` + `files` cursors, `messages`, `messages_fts` (schema version 1 in `meta`). Both writers share one file: each commit is a `BEGIN IMMEDIATE` transaction fenced on the file's `files` row, and swe never touches an index at another schema version | app index writer (every harness), `swe session index` and the remote hub's loop (Claude Code, Codex, Pi, Cursor, Qoder, CodeBuddy, Grok, Kiro and Hermes transcripts, Devin's `sessions.db` plus its `transcripts/*.json` exports, and OpenCode's `opencode.db`, `sessions/search_writer.py`; a session inside a database indexes under the synthetic path `<db>#<id>`) | `swe session --search`, `GET /v1/search`, MCP `get_recent_sessions` (`sessions/search_index.py`). A Claude Code or Codex session whose transcript was pruned is read from its mirror copy under `data/transcripts/` and filed under the transcript's own path, never the copy's, so every row still names a source path. Inputs: the pass's live sources, one walk of `data/transcripts/` (`claude/`, `codex/`, `accounts/<vendor>/<id>/`; `*.jsonl`, hidden names and `subagents/` pruned) and the manifest. Key: a session's identity is `(harness, id)`, the id taken from the file name alone, lower-cased: a Claude file's stem when it is a uuid, a Codex rollout's first uuid (the thread id); a name with no session uuid is its own identity (the source path), so it never stands for another file. The same for live sources, copies (after their source path is known) and `files.path`. Per identity no live source carries: (1) candidates are the copies whose source path is known (manifest `source`, else `transcript_mirror.source_candidates`, the layout inverse, preferring a candidate that has a `files` row) and not on disk (`lstat`); (2) the newest by `(mtime_ns, path)` is used, the rule the listing and reads apply; (3) every row of the identity is fetched once per pass (`files` joined to `sessions` on `agent`, identity of `files.path`); any row at a path on disk ends it; (4) if a row covers the copy (state `current`, a session, offset not -1, `mtime_ms` and `size_bytes` at least the copy's), nothing is read, and the identity's other rows that this keeper dominates (its `mtime_ms` and size at least theirs) are deleted in one transaction fenced on the keeper and each row (`session_row_id, byte_offset, mtime_ms`) with each path re-checked gone; (5) if any row is not dominated by the copy, it ends; (6) otherwise the copy is read whole under the source path. A read that commits in pieces (over the 32 MiB text budget) stages them on a session already visible for the identity (the source path's row's, else another row's), touching no `files` row, so search sees one session holding at least what it held; the final commit mints the new session, moves the staged messages to it by the exact ids the read wrote (never "every message past an id": the Mac writer may append to the host meanwhile), writes the `files` row (the copy's ino, `mtime_ms` and size) and deletes the identity's other rows, all fenced the same way with the source path and every deleted row's path re-checked gone. A read that fails takes back exactly the messages it staged; a kill leaves them on a session the next whole read deletes. Every commit from a copy records `meta` `mirror_copy:<path>` = `"<agent> <session row id>"`, and any other commit for that path deletes the key. Live side: when a live file of an identity is read, or is already current, the identity's rows whose mark still names their session, under other paths that are gone, are deleted in that same commit (or a fenced commit of their own when the live file needs no read); a row without a mark, one the index read from a live file, is never deleted this way. Nothing else removes a row, so a lost manifest or a missing copy changes nothing; a deliberate purge unlinks the copies, then calls `search_writer.forget_session(home, harness, session_id, source_path=None)`, which deletes the identity's rows under gone paths, each only if unchanged and still gone inside the delete's transaction (`source_path` names a transcript whose file name has no session uuid). |
| `data/search/indexer.json`, `index.lock` | swe | JSON; empty lock file | whether the hub keeps the index current (`swe session index --on`/`--off`) and the last pass's counts; `index.lock` is an `flock` so only one swe pass runs at a time | `sessions/search_writer.py` | `swe session index --status` |
| `data/transcripts/` (`claude/<project>/<id>.jsonl`, `codex/<path under sessions/ or archived_sessions/>`, `accounts/<vendor>/<id>/...`) | swe | JSONL copies, mode 600, source layout kept, mtime = the source's | whole-file copies (APFS clones via `clonefile(2)` where possible, so no extra disk is used while the source exists; streamed otherwise) of every Claude Code and Codex transcript `search_writer.sources` enumerates, made while the source exists so the session outlives the harnesses' 30-day pruning. **A pass never deletes a copy**: not when its source disappears, not on failure. The one deliberate delete is `swe session mirror --purge=<id>` (an id, never a `#`), for one session whose source is gone, removing only the files the manifest ties to it (never through a symlink); `swe session restore` copies a copy back to its recorded source path and never overwrites. A copy replacing a longer one first keeps the longer as a hard link `<file>.shrunk-<mtime_ns>` | `sessions/transcript_mirror.py`: the hub's loop, `swe session index`, `swe session mirror` (and `--purge`, `--sweep-tmp`), `swe session restore` (reads a copy, writes the harness's file) | the session listing (only copies whose source is gone), `reports/transcripts.py::read_transcript` (fallback when the source is gone), the search index pass (copies of sessions no live file carries, filed under their source path; see `data/search/search.sqlite`), the web ledger's scan (`ledger/scan.py`: usage of sessions no live file carries, read once), the hub's chat tailer (`remote/tail.py::MirrorCopySource`, read-only), `swe doctor` |
| `data/transcripts/manifest.json`, `mirror.json`, `*.lock` | swe | JSON `{version: 1, files: {"<mirror-relative path>": {harness, source, account_home, ino, mtime_ns, size, mirror_size, copied_at}}}`; `{enabled, last_pass}`; empty lock files | the record of what each copy came from (`source` is the resolved path, `ino`/`mtime_ns`/`size` its stat taken from the open handle before the copy, `mirror_size` the bytes written); the on/off switch (`swe session mirror --on`/`--off`, on when the file is absent) and the last pass's counts; the `flock` that allows one pass at a time. An unreadable manifest is set aside as `manifest.json.corrupt-<epoch>`, never overwritten; losing it loses no transcript, since the next pass copies again, but a copy with no entry cannot be restored or purged by swe. Entries are checked where the manifest is loaded: one that is not an object, has no `source` path string or `harness` string, or a non-string `account_home` is skipped by every reader (its copy is never copied over, restored or purged) and kept verbatim by every writer; a wrong-typed number reads as 0. A restore rewrites its entry's `ino`/`mtime_ns`/`size` to the restored file's, so the next pass finds the copy current; a purge drops the session's entries after removing their files | `sessions/transcript_mirror.py` | same |
| `data/insights/insights.sqlite` | swe | SQLite (WAL) | workflow metrics derived from the search index: `session_metrics` (one row per indexed session; T0 counters on one half — `source_rev` fingerprints the rows they came from — the trigger-driven scoring pass owns `outcome`/`first_pass`/`causes` on the other), `daily_rollup`, `findings` (schema version 2 in `meta`). Rebuildable: the counter half re-derives from `search.sqlite`, and swe never opens a store at a newer schema version. Design in [docs/insights.md](insights.md) | `insights/store.py`; the T0 pass `insights/t0.py` (reads `search.sqlite` through `insights/metrics.py`) | `swe insights` (planned), the app's Insights tab (planned) |
| `data/catalog/catalog.sqlite` | app | SQLite (WAL) | the model catalog: every price each LiteLLM key has held, Artificial Analysis models and raw feeds, the price-key to AA-model join, and a row per fetch (see below). Price history exists only here: a dropped file reprices past usage at today's rates. `SWE_CATALOG_DB` overrides the path | `PricingStore`, `ArtificialAnalysisStore` | ledger pricing, Pricing and Models pages |
| `skills/`, `hooks/`, `plugins/`, `AGENTS.md`, `.linkignore` | swe | files and symlinks | shared instruction/skill/hook/plugin trees | `swe init`, `swe skills`, `swe plugins` | the harnesses that link them |
| `data/account-links.json` | swe | JSON `{version, links: [{path, source, primitive, account}]}` | every link `swe init` made in an extra account home for an `accounts.sync.*` switch; switching one off removes only the links listed here that still point at their `source` | `init/accounts.py` (malformed raises, the pass then touches nothing) | `swe init` |
| `data/account-mcp.json` | swe | JSON `{version, servers: [{account, path, name, digest}]}` | every MCP server `swe init` merged into an extra account's `.claude.json` or `config.toml` for `accounts.sync.mcp`. `digest` is an HMAC-SHA256 of the entry as written, keyed by `secrets/account-mcp.key` (the entries hold resolved secrets); swe updates or removes a server only while the file still holds exactly that entry | `init/account_mcp.py` (malformed raises, the MCP pass then touches nothing) | `swe init` |
| `fan/control.json` | app | JSON `{level, owner, requested}`, folder mode 700 | the Compute page's manual fan request: a level from 0 (each fan's minimum) to 1 (its maximum), the requesting SWE's pid and when it was asked. No level means automatic | `FanControl` (SWECore) | the root fan helper (`SWE --fan-daemon`, run by launchd from `/Library/LaunchDaemons/com.cwenlong.swe.fan.plist`), which holds the fans only while that pid is a running SWE. It answers in `/var/run/com.cwenlong.swe.fan.json` (`{state, reason, requested, owner, at}`, root-owned, mode 644, outside `~/.swe` so root never writes into a folder the user owns), which `FanControl` reads to show why a request was refused |
| `config/github.json` | swe | JSON `{schema: 1, repos}` | the repositories pinned for `swe gh` and the Repositories pages, in pin order | `github/repos.py` (`swe gh repo add`, `remove`, `reorder`) | `swe gh`, the hub's GitHub routes |
| `config/pricing.json` | app + swe (hub) | JSON `{schema: 1, models}`, USD per million tokens | your own per-model prices, which win over the downloaded list | the app's `PricingOverrides` and `ledger/pricing.py` (`save_override`), both atomic and keeping other entries | the usage ledgers, the Pricing and Models pages |
| `config/model-links.json` | app | JSON | manual links from a logged model name to an Artificial Analysis model | the Models page's Link sheet | `ModelMatcher`, `model_aliases` in `catalog.sqlite` |
| `data/remote/` | swe (hub, satellite) | JSON files and folders: `devices.json` (paired phone credentials, mode 600), `inbox_dismissals.json`, `session_reads.json`, `session_meta.json` and `tasks.json` (mode 600 each), `ssh.json`, `pairing.json`, `machine.json`, `pair_tokens.json`, `cursors/`, `journal/`, `inbox/` | remote-control state, under the hub repo's `/data/` ignore. Not rebuildable from anywhere: `devices.json` (paired credentials), `session_meta.json` (your pins and titles) and `tasks.json` (session-to-task links); the journals and cursors can be rebuilt from transcripts. The file list and modes are in [remote-control.md](remote-control.md) | `swe remote` and the hub | the hub, `swe remote status` |
| `data/github/catalog.sqlite`, `data/github/mirrors/`, `worktrees/` | swe | SQLite; bare git mirrors; per-PR and per-issue checkouts | the repo catalog behind `swe gh sync` and `swe gh repo search` (rebuilt by a sync); one bare mirror per repo; the checkouts `swe gh pr worktree` and `swe gh issue worktree` make (`worktrees/` sits at the `~/.swe` root, gitignored). Mirrors and worktrees can hold an issue branch's commits and edits not yet pushed, so check before deleting them | `github/catalog.py`, `github/manage.py` | `swe gh`, the hub's launch routes |
| `logs/worktree/<action>-<stamp>-<pid>.json`, `cache/worktree.lock` | swe | JSON, one file per applied run; a mkdir lock with the owner's pid | what each `swe wt prune --yes` / `slim --yes` removed, kept and freed (the `--json` plan plus `at`); dry runs write nothing. `logs/` is gitignored in the hub | `worktrees/actions.py` | `swe wt` |
| `data/web-status-services.json` | swe (hub) | JSON `{version: 1, services}` | the status pages a browser tracked from the web app's catalogue sheet, shared by every browser the hub serves | `status/tracked.py` (atomic, under an `flock`) | the web app's Service status page |
| `reports/` | swe | markdown + JSON | generated reports, `session-summaries/`, `exclusions.json`, `cursors.json`, `followups.json` | `reports/store.py` (atomic writes, malformed state raises) | `swe report` |

Legacy registry names `tools.json`, `stars.json`, `archived.json` (pre
`harness.json`) are lazily migrated by `harness/registry.py`; the old
`~/.config/swe` root is moved by `init/migrate.py`. Do not add new state
under those names.

### Caches (rebuildable)

| path | owner | format | holds | TTL / invalidation |
|---|---|---|---|---|
| `cache/session_cache.json` | swe | JSON `{cached_at, sessions}` | all parsed sessions | 60s; `invalidate_cache()`; bypassed with `use_cache=False` (`swe session` itself always reparses) |
| `cache/session_parse_cache.json` | swe | JSON `{version, entries: {"<harness>:<what>": {abs path: [[size, mtime_ns, ctime_ns, ino], value]}}}`, mode 600 | what each transcript (and freebuff `run-state.json`) says on its own: cwd, title, title source, cursor tool paths, claude handoff target | per file: a changed stat signature re-reads it; `version` holds format, package version and a hash of the parser modules, so any code change discards it; entries a harness's parser no longer visits are pruned when it ran; sidecars (codex index and thread store, cursor `meta.json`) are read fresh every run |
| `cache/session_status_cache.json` | swe | JSON `{version, entries: {"<harness>": {abs path: [[size, mtime_ns, ctime_ns, ino], [phase, followup]]}}}`, mode 600 | what each transcript's tail decides for the `swe session` status column (claude, codex, cursor, pi), plus `"cline"` entries whose value is the bare followup boolean of a `<id>.messages.json` | per file, like the parse cache; `version` hashes `status.py`, `parse_cache.py`, `cursor_store.py` and `engines/common.py`; an entry goes once its file is gone (swept at most every 5 min) |
| `cache/session_model_cache.json` | swe (hub and satellite catalog) | JSON `{version, entries: {"<harness>:model": {abs path: [[size, mtime_ns, ctime_ns, ino], "<model>"]}}}`, mode 600 | the model of each transcript's newest reply, for the remote session rows' `model` (claude, codex, pi) | per file, like the status cache; `version` hashes `session_model.py`, `recent_models.py`, `protocol.py` and `parse_cache.py`; an entry goes once its file is gone (swept at most every 5 min) |
| `cache/task_states.json` | swe (hub) | JSON `{states: {"<task id>": {state, pr}}}`, mode 600 | the last state each stored task's source reported (`pr` is the brief an issue's branch PR or a pull's own PR carries), so `GET /v1/tasks` serves a task the sources can't read this round as `state_source: cached` | written only when a fresh source read changes a stored task's `{state, pr}`, never on a quiet poll; a read that couldn't see the PR (the branch lookup failed, or only the open list saw the issue) keeps the remembered `pr` unless it was an open PR the open list no longer shows; a write drops tasks `tasks.json` no longer holds; corrupt or missing reads as empty |
| `cache/hooks/events.jsonl` (+ `events.jsonl.1`) | swe | JSON lines, folder mode 700, file mode 600 | one line per hook run: `v` (1), `ts` (epoch seconds, when the run finished), `hook` (script name without extension), `harness`, `event` (`Stop`, `PreToolUse:Bash`: the harness's own name, with `:<tool>` on tool events), `session_id`, `cwd`, `repo` (root found from `cwd`'s `.git`), `git_branch`, `decision` (`block` exit 2 or a JSON deny, `warn` exit 0 with stderr, `allow`, `error` any other exit or a shim failure: the hook failed open), `reason` (the first line of the hook's own message, at most 200 characters; never reply or command text), `duration_ms`, `exit_code`, `stop_hook_active` (true when a Stop hook ran again after blocking, so "blocked, then the rewrite was accepted" reads apart from a plain allow), `script` (added within v1, so a line without it is still read: the resolved absolute path of the file that ran, the shared script for a shim run and the running program for a hook calling `record` itself, or null; `swe hooks stats` tells two hooks with one name apart by it, and a line without it is matched by `hook` instead). Never transmitted, and `cache/` stays out of the hub repo. Not rebuildable: it is the only record of runs on harnesses with no transcripts, but the Claude Code part can be recounted from transcripts | `hooks/shim_runtime.py::record`, called by the shim for every shared hook (one `O_APPEND` write, no lock, fails open, `SWE_HOOK_LOG=0` turns it off); a harness-tier script may import `shim.py` from `~/.swe/hooks/_runtime/` and call `record` itself (`script=` names the file when the running program is not the hook) | `swe hooks stats`, `swe hooks log` (`hooks/events.py`); rotates at 5 MiB (`EVENTS_CAP_BYTES`) to `events.jsonl.1`, replacing the older copy, so at most 10 MiB (about 40,000 runs) is kept |
| `cache/hooks/transcripts.sqlite` | swe | SQLite (WAL, busy timeout), file mode 600 in the 700 `cache/hooks/` folder | the hook backfill's parse cache: one row per Claude Code transcript, `(version, path)` keyed, with the file's `size`, `mtime_ns`, `ctime_ns` and `inode` taken before the read, and the runs `transcripts.parse_raw` found in it (zlib-compressed JSON, one entry per field: decision, reason line, timestamps, session, cwd, branch, the command string and the session's first cwd; never the command's resolved script, which depends on the disk; a row that fails to decode or has the wrong shape reads as a miss). `version` is the digest of the deciding modules' source, so two swe versions share the file without wiping each other. Created with incremental auto-vacuum; new parses are written in batches of at most 8 MiB (`BATCH_BYTES`), each trimmed and vacuumed, so the file stays under the cap plus one batch (about 7 MB after a 30-day read of a 2,000-transcript library) | `hooks/transcript_cache.py`, during `swe hooks stats` / `swe hooks log` and the web Hooks page's read | the same commands; rebuildable at any time (delete it, or `SWE_HOOK_CACHE=0` to bypass); trimmed to 64 MiB of rows (`CAP_BYTES`), the least recently read first |
| `cache/session_counts.json` | swe | JSON per window (`"7"`, `"30"`, `"100"`, `"all"`) | session counts + broken parser names | 24h, `SWE_SESSION_COUNTS_TTL`; `invalidate_counts_cache()`; rebuilt on `swe list` |
| `cache/github_owners.json` | swe | JSON `{logins: {<login>: {owners, fetched_at}}}` | the login and orgs a repo search or `swe gh sync` may cover; written by `github/manage.py` `search_owners` only after a successful org read | 600s per login (`OWNERS_TTL`) |
| `cache/web-service-status.json` | swe (hub) | JSON, epoch seconds | the web app's status-page readings, with a tombstone (`removed_at`) per untracked page; written by `status/poller.py` (atomic, under an `flock`) | a page is asked at most every 5 minutes; a reading older than an hour is not shown |
| `cache/rate_limits_cache.json` | swe | JSON per provider | last limit reading per provider; Devin's optional `free_model_reset_at` is an observed CLI-log deadline, separate from plan quota, ignored after expiry | 300s, `SWE_RATE_LIMITS_TTL`; stale fallback up to 24h; per-provider `updated_at` |
| `cache/claude_usage_cache.json` | swe | JSON `{info, retry_at, credential_fingerprint, fetched_at}` | Claude usage endpoint's last reading + 429 cooldown | reading kept up to 24h; cooldown from `Retry-After` |
| `cache/app-usage.json` (+ `.lock`) | app | JSON `{version: 1, accounts}` | Claude/Codex usage readings (optional `monthlyCredits: {used, limit}` in provider credit units), cooldowns (including optional account-level `monthlyRetryAt` for Codex billing-only throttling), credential fingerprints, last failure per account (`lastFailure: {at, status}`) (no secrets) | 60s poll; flock-shared read-modify-write |
| `cache/app-other-limits.json` | app | JSON `{savedAt, rows, cards, starred, cliInstalled}` | the last `swe list --usage` cards OtherLimitsModel showed | written after each successful load; hydrate at init; corrupt or missing files are ignored |
| `cache/app-limit-alerts.json` | app | JSON | `LimitAlerter` dedupe state (alerted ids, reset timestamps) | pruned per evaluation |
| `cache/api-key-checks.json` (+ `.lock`) | app | JSON `{version: 2, files}` | per-key validity results salted by `apiKeyCheckSalt` | no expiry: kept until the next check, shown only while the key's salted fingerprint still matches; flock-shared |
| `cache/service-status.json` (+ `.lock`) | app | JSON `{version: 1}` | status-page readings per service | per-service refresh intervals in `ServiceStatusService` |
| `cache/skills.json` | app | snapshot v1 | last `swe skills --json` result | restored instantly, refreshed via `swe` |
| `cache/mcp-servers.json` | app | snapshot v1 | last `swe mcp list --json` result | `SnapshotLoader`, 60s stale |
| `cache/plugins.json` | app | snapshot v1 | installed plugin list from `swe find plugins --json` | restored, refreshed via `swe` |
| `cache/plugin-catalog.json` | app | snapshot v4 | `swe plugins catalog` output | 24h (`86400`s); v4 adds Cursor account controls to the v3 multi-harness catalog; v1 was bundled, never released |
| `cache/sessions.json` | app | snapshot v1 | last session listing shown | `SessionSnapshot` v1; swe asked again when listing is over 60s old |
| `cache/widgets/snapshot.json` | app | snapshot v2 | widget payload: summaries, heatmap, models, limit readings | rewritten on limit refresh; WidgetKit reads it |
| `cache/pricing/litellm.json` | app | JSON | superseded: the old LiteLLM download | imported once into `data/catalog/catalog.sqlite` (dated by its mtime) and left in place; nothing writes it |
| `cache/skills-hub.json` | app | superseded snapshot | old skills-hub payload | deleted at launch by `AppModel`; nothing reads it |

Files nothing in this repo owns but observed in real `cache/` dirs:
`.last-backup-stamp` (external backup tooling), `.DS_Store`, `evals/` and
`.backup/` at the `~/.swe` root. Left alone.

### UserDefaults keys (app)

All app preferences. Read/write goes through the `Key` enums and `@AppStorage`;
every key below is live.

| key | holds |
|---|---|
| `swePath`, `apiKeysPath`, `mcpServersPath`, `codeEditor`, `terminalApp` | user-set paths and app choices |
| `appearance`, `showsInDock`, `lastSection`, `settingsSection`, `menuBarPageTab`, `desktopWidgetTab`, `agentsMdView` | window and page state |
| `agentsProjectFolder` | last Agents.md project |
| `worktreesScope` | the Worktrees page's repository filter: a main checkout's path, or empty for all repositories (the page falls back to empty when that repository is gone) |
| `limitNotifications` | notification opt-in |
| `menuBarVisible`, `menuBarPrimary`, `menuBarSecondary`, `menuBarWatchedLimit` | menu bar display |
| `popupShowIncidents`, `popupShowStats`, `popupHiddenSources`, `popupHiddenSourceWindows`, `popupSourceOrder` | menu bar popup content and order |
| `popupHiddenHarnesses`, `popupHiddenWindows`, `popupHarnessOrder` | legacy popup keys, read-only migration fallbacks; keep until the new keys have covered every install |
| `mcpHiddenHarnesses` | harness columns hidden on the MCP page |
| `mcpColumnOrder` | harness column order on the MCP page's table |
| `skillsColumnOrder`, `pluginsColumnOrder` | folder and harness column order on the Skills and Plugins tables |
| `providerOrder`, `providerDisabled` | provider list order and switches |
| `inferenceOrder`, `infraOrder`, `servicesOrder` | row order on the Providers page's Inference, Infra and Services tabs |
| `infraKeys`, `servicesKeys` | variables filed to the Infra or Services tab that no catalogue claims |
| `computeTrendInterval`, `computeTrendTab`, `computeMemoryLimitGB`, `computeOrphanAgeMinutes`, `fanManualLevel`, `fanThresholdEnabled`, `fanThresholdCelsius`, `fanReleaseBelowCelsius`, `computeTemperatureLines` | Compute page preferences (`computeTrendTab` also takes `temperature`; `computeTemperatureLines` lists the Temperature tab's switched-on lines, default `cpu`, `gpu`, `ssd`, `battery`) |
| `refreshFreshMinutes` | the "Refresh only what failed for" window (default 5, clamped to 1..120) |
| `customStatusServices` | user-added status services |
| `apiKeyCheckSalt` | salt for cached key-check results (not a secret itself) |
| `widgetHarnesses.summary`, `widgetHarnesses.heatmap`, `widgetHarnesses.models` | per-widget harness picks |
| `widgetLimitsHiddenSources`, `widgetLimitsHiddenSourceWindows` | per-widget limit filters |
| `accountsProviderOrder` | Mac Accounts provider columns, comma-separated harness ids; hidden providers keep their position |
| `widgetLimitsSourceOrder`, `widgetSummaryShowsTotal` | limits source order; summary widget's total line |
| `migratedCursorAutoToIncluded` | one-time marker: Cursor `auto` rewritten to `included` |

## Ledger schema (`data/ledger/ledger.sqlite`, schema version 1)

Rows hold token counts, not dollars: `PricingCatalog` prices them at query
time, at the rate each event's price entry had when it happened (names
resolve through today's list; rates come from the catalog's history). `msg`, `head_hash` and
`tail_hash` are FNV-1a 64-bit hashes (`StableHash`), never raw text.

- `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`: the `schema` marker
  plus generic key-value cursors for non-file sources (SQLite databases).
- `dims(id, harness, account, session, cwd, model, tier,
  UNIQUE(harness, account, session, cwd, model, tier))`: every repeated
  string lives here once; events reference `dims.id`.
- `events(key INTEGER PRIMARY KEY, msg INTEGER, sidechain, dim REFERENCES
  dims(id), ts REAL, input, output, cache_read, cache_write,
  cache_write_1h, reasoning, cost REAL)`: one row per deduplicated response.
  `key` is `StableHash` of the response id; `msg` dedupes retried writes of
  the same message; `cost` is the log's own dollar figure when it has one.
- `cursors(path TEXT PRIMARY KEY, inode, size, modified REAL, offset,
  head_hash, head_length, tail_hash, tail_length, state BLOB, skipped)`:
  per-file scan position; a scan commits events and cursor in one
  transaction so a crash cannot double-count.

Indexes: `events_ts_cover(ts, dim, input, output, cache_read, cache_write,
cache_write_1h, reasoning, session_total, cost)`, `events_dim(dim)`,
`events_msg(msg) WHERE msg IS NOT NULL`, plus the `dims` UNIQUE index and
the `path`/`key` primary keys. Rows sit in `key` (hash) order, so a range
read through a plain `ts` index paid a table lookup per row scattered over
the file; the covering index answers range reads alone and replaced
`events_ts` (#1568). `groups()` groups in two passes: events by integer
`dim` and price inputs first, then only those groups join `dims` and merge
by the strings asked for, which roughly halves its CPU again.

```
groups()          SEARCH e USING COVERING INDEX events_ts_cover (ts>? AND ts<?) + temp b-tree group by dim, then PK join + group
groups(harness:)  SEARCH e USING INDEX events_dim (dim=?) over the harness's dims + temp b-tree group
events(in:)       SEARCH e USING COVERING INDEX events_ts_cover (ts>? AND ts<?) + PK join + sort of ts ties by key
selectByMessage   SEARCH events USING COVERING INDEX events_msg (msg=?)
selectDim         SEARCH dims USING COVERING INDEX sqlite_autoindex_dims_1 (all six cols)
cursor(for:)      SEARCH cursors USING INDEX sqlite_autoindex_cursors_1 (path=?)
MAX(ts)           SEARCH events USING COVERING INDEX events_ts_cover
```

### Migration history

- v1: initial schema (dims / events / cursors / meta). No v0 file ever
  shipped, so no migration exists.
- Location: builds before the move kept the ledger in `cache/ledger/`.
  `LedgerLocation.prepare` copies it to `data/ledger/` at launch through the
  backup API (rows still in the WAL included), checks every table's row
  count, renames the copy into place and only then deletes the old files.
  It holds the old file in an exclusive lock throughout, so while another
  SWE has it open the move waits for a later launch. A failure leaves the
  old rows untouched, opens the old file where it is, and tries again next
  launch. When `data/ledger/ledger.sqlite` exists already the old one is
  never read. `ledger.schema-*.sqlite` archives move at every launch (a
  `.N` suffix when the name is taken), so one that failed is retried.
- Versioning lives in `meta.schema`, not `user_version`. A mismatch rebuilds
  the tables (the ledger is a cache of logs) but first archives the old file
  via `VACUUM INTO` to `ledger.schema-<version>.sqlite` beside it (a `.N`
  suffix is appended when the name is taken), so history whose logs are gone
  stays recoverable. A future bump should still prefer a real `ALTER`
  migration over the rebuild.

## Catalog schema (`data/catalog/catalog.sqlite`, schema version 1)

`CatalogDatabase` owns it; one connection behind a lock.

- `fetches(id, source, url, fetched_at, status, error, content_sha,
  row_count, rate_limit_headers)`: every download attempt. `source` is
  `litellm` or `aa:<endpoint>`. A failure deletes the failures recorded
  since that source's last success, so an outage is one row.
- `prices(key, valid_from, valid_to, input, output, cache_read, cache_write,
  cache_write_1h, reasoning, entry, fetch_id, PRIMARY KEY(key, valid_from))`:
  one row per price a LiteLLM key held, USD per token; `entry` is the
  entry's price fields as sorted-key JSON. A row is added only when those
  fields change, and `valid_to` is null on the row in force. A key that
  leaves the list has its row closed.
- `models(id, position, slug, name, creator_*, aa_price_*, speed, ttft,
  ttfat, evaluations, raw, fetch_id)`: AA's language models in AA's order.
- `model_aliases(name, model_id, method)`: every price key and the AA model
  it names (`method` is `auto`, `manual` or `none`). Derived: rebuilt from
  `ModelMatcher` and `config/model-links.json` at launch and after any
  refresh or link. `prices.key` references it, so `prices JOIN
  model_aliases` lists every priced key of one model.
- `payloads(source, fetch_id, body)`: each AA endpoint's last good body.
  The language feed's pages (200 models each on AA's V2 API) are merged
  into one body before it is stored, so `models` is only ever replaced by
  the whole feed. Older rows hold the legacy `/api/v2/data/*` shape, which
  the decoder still reads.
- `meta(key, value)`: the `schema` marker.

Builds before the catalog kept `cache/pricing/litellm.json` and
`data/artificial-analysis/<endpoint>.json` + `meta.json`. They are imported
once (the price list per source, AA per endpoint, so an interrupted import
resumes) and left in place.

## JSON formats and versions

| file | envelope | notes |
|---|---|---|
| Snapshot stores (`skills.json`, `mcp-servers.json`, `plugins.json`, `plugin-catalog.json`, `sessions.json`, `widgets/snapshot.json`) | `{version, savedAt, ...}` via `SnapshotFile` | decoded only when `version` equals the type's `snapshotVersion`; missing, malformed or mismatched files read as absent; atomic private writes |
| `app-usage.json` | `{version: 1, accounts: {id: {reading, cooldownUntil, fingerprint, lastFailure?}}}` | fingerprints are SHA-256 of credentials; `lastFailure` is `{at, status}` and absent on success and in files from before it existed; no secret values |
| `app-other-limits.json` | `{savedAt, rows, cards, starred, cliInstalled}` | no version; decode failure reads as absent |
| `api-key-checks.json` | `{version: 2, files: {path: {salt, entries}}}` | version 1 reads as empty |
| `service-status.json` | `{version: 1, ...}` | readings and incident state per service |
| `session_cache.json` | `{cached_at, sessions}` | 60s |
| `session_parse_cache.json` | `{version: "<format>:<package version>:<source hash>", entries}` | per-file stat signature; a version mismatch, unreadable file or wrong-shaped entry reads as a miss |
| `session_status_cache.json` | `{version: "<format>:<package version>:<source hash>", entries}` | per-file stat signature; a version mismatch, unreadable file or wrong-shaped entry reads as a miss |
| `session_model_cache.json` | `{version: "<format>:<package version>:<source hash>", entries}` | per-file stat signature; a version mismatch, unreadable file or wrong-shaped entry reads as a miss |
| `session_counts.json` | `{windows: {<days>: {cached_at, counts, broken}}}` | 24h |
| `rate_limits_cache.json` | `{cached_at, limits, updated_at, no_data: {<provider>: ts}, no_data_status: {<provider>: "signed_out" \| "error"}}` | 300s, per-provider; a `signed_out` entry is refetched every call |
| `claude_usage_cache.json` | `{info, retry_at, credential_fingerprint, fetched_at}` | cooldown + last good reading |
| `harness.json` | `{<name>: {state, ...}}` | the registry; only `registry.py` writes it |

## Caching map

| fetch | cache | TTL / interval | coalesced? |
|---|---|---|---|
| `swe list --json` (provider detection) | `ProviderVisibilityModel` memory | once per swe path; activation rechecks throttled to 120s | yes, `isLoading` + generation |
| `swe list --usage --json` | `UsageModel` memory + `app-usage.json` disk | 60s poll | yes |
| `swe list --usage --exclude` (other providers) | `OtherLimitsModel` memory | 300s poll | yes |
| swe rate-limit fetches (Claude/Codex/...) | `rate_limits_cache.json` + `claude_usage_cache.json` | 300s, per-provider deadlines, negative caching | threads, one run per invocation |
| `swe session --json` | `sessions.json` snapshot + `SessionsModel` inflight task | listing reused under 60s; swe itself always reparses (freshness wins over the aggregator cache) | yes, shared inflight |
| swe session parsing | `session_cache.json` (`swe list` only) + `session_parse_cache.json` (every listing, per file) + `session_status_cache.json` (status column, per file) | 60s disk / until the file changes | n/a (one process) |
| `swe session` counts | `session_counts.json` | 24h per window | n/a |
| `swe skills --json` | `skills.json` snapshot + model | reload on visit/edit | yes |
| `swe find plugins --json` | `plugins.json` snapshot | reload on visit/edit | yes |
| `swe plugins catalog --json` | `plugin-catalog.json` | 24h | `SnapshotLoader` joins in-flight loads |
| `swe mcp list` / `mcp status` | `mcp-servers.json` | 60s via `SnapshotLoader` | yes |
| `swe doctor` | `DoctorModel` memory | page visit + manual refresh | generation guard |
| `swe init --check` | `AgentsModel` memory | page visit; kept current because link states change outside the app | generation guard |
| status pages | `service-status.json` + `ServiceStatusService` | per-service intervals; `ensureServices` registers once | yes, per-service tasks |
| Claude/Codex usage APIs | `app-usage.json` + model memory | 60s; cooldowns honored across processes via flock | yes |
| API key checks | `api-key-checks.json` | 900s, salted | flock-shared |
| ledger aggregates | `OverviewModel` memory summaries | per period, rebuilt after scans | n/a |
| ledger scans | `cursors` table | incremental by file offset; unchanged files skipped | n/a |
| git status for MCP folders | `McpServerRepoModel` memory | per page visit, 4 concurrent | ticket guard |
| GPU + process samplers | `sampledAt` reuse | half the trend interval, plus 3s on-page refresh | shared sampler |
| login shell environment | `LoginShell.cachedEnvironment` | once per launch | single shared task |
| pricing catalog | `data/catalog/catalog.sqlite` + bundled fallback | daily, from the last good fetch's time | n/a |

### Imported plugins

`swe plugins import <ref>` writes a local marketplace at `plugins/<marketplace>/.claude-plugin/marketplace.json` and an independent plugin copy at `plugins/<marketplace>/plugins/<name>` (or appends to an existing hub marketplace). It never overwrites a declared plugin. These are ordinary hub sources, with no new database or persistent rendering cache. Mac local inventory and action arrays are derived off the main actor per catalog/skills revision.

### Cursor plugin credentials and state

Cursor plugin status is read from its account API. No new persistent plugin state store is written. Existing editor/CLI credentials are read only, and keychain UI is disabled while decrypting macOS credentials. Catalog snapshots store only plugin metadata, enabled state and allowed action names, never credentials or configured values. Cursor's SQLite installedIds cache and downloaded plugin folders are never rewritten as an enable-state mechanism.

### Disabled OpenCode file plugins

`data/plugin-disabled/opencode/<sha256-of-absolute-discovery-root>/<filename>`
holds files or directories suspended from OpenCode's global discovery folder.
Re-enabling moves the original entry back; there is no second metadata record.
Active/disabled name collisions refuse actions. Registry `capabilities.plugins.root`
wins over the default `~/.config/opencode/plugins`. Both locations must be on
the same filesystem; a failed rename leaves the source intact. Removal deletes
only the selected discovery entry (a symlink itself, never its target).
