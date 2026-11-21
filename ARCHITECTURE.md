# Architecture

SWE (`swe`) is a manager for AI coding CLIs. It unifies the things those
CLIs each invented separately: instruction files, skills, plugins, MCP servers,
session history and rate limits.

The CLI is stdlib-only, with no runtime dependencies at all.

Three clients sit on top of it: the macOS app in `app/mac/`, the iPhone
app in `app/ios/`, and the web app in `src/swe/webapp/`, which ships in
the Python package so the hub can serve it.

This file is the overview; each topic below has a deeper document in `docs/`:

| Document | What it covers |
|---|---|
| [docs/data-model.md](docs/data-model.md) | Every persistent store, the ledger schema and migrations, JSON file formats, UserDefaults keys, the caching map |
| [docs/app-architecture.md](docs/app-architecture.md) | The macOS app: folder layout, AppModel and the page models, Theme, the component catalogue, navigation |
| [docs/backend-architecture.md](docs/backend-architecture.md) | The controller / service / model split per domain, the bottom layer, where each `--json` payload lives, how to extend |
| [docs/instructions.md](docs/instructions.md) | Ordered instruction chapters, atomic generation, validation and watching |
| [docs/json-contracts.md](docs/json-contracts.md) | Every `swe --json` command the app calls, its shape, and the Swift type that decodes it |
| [docs/testing.md](docs/testing.md) | Running both suites, the throwaway-HOME rule, fixture capture, coverage and floors |
| [docs/COVERAGE.md](docs/COVERAGE.md) | The dated coverage audit and current risk areas |
| [docs/remote-control.md](docs/remote-control.md) | The hub (`swe remote serve`), satellites, pairing, devices, every `/v1/*` route and the threat model |
| [docs/web-app.md](docs/web-app.md) | The web app the hub serves at `/`: its pages, their `/v1/app/*` data and how to add one |

## The shape

```
                         cli.py  (dispatch only)
                            │
        ┌──────────┬────────┼────────┬──────────┬─────────┐
        │          │        │        │          │         │
      init       harness  sessions  skills     mcp     reports
        │          │        │        │          │         │
        └──────────┴────────┼────────┴──────────┴─────────┘
                            │
           console · table · paths · configuration · keys
```

A bottom layer of small modules (`paths`, `console`, `table`,
`configuration`, `keys`, `flags`, `json_output`) may not import a domain
package; their allowed ordering and what each owns is in
[docs/backend-architecture.md](docs/backend-architecture.md).

## The layout

`~/.swe` is split by lifecycle, so the versioned half and the disposable
half never mix:

```
~/.swe/
  AGENTS.md          canonical instructions, symlinked into every harness
  AGENTS.<name>.md   a harness's own compiled chapters (targets.json), if any
  AGENTS_MD/         optional authored chapters, index.txt and targets.json
  .linkignore        paths `swe init` must leave alone
  skills/            the shared skill tree, symlinked into every skills root
  hooks/<harness>/   hook scripts, each file symlinked into that harness only
  plugins/           the hub's own plugin marketplaces
  config/            registry (harness.json), catalogs, link records (versioned)
  secrets/providers/.api_keys  provider API keys, mode 600    (gitignored)
  secrets/mcp/.api_keys        MCP credential values, mode 600 (gitignored)
  mcp.json           server definitions, ${REF} placeholders    (versioned)
  mcp/servers/       server checkouts, ~2.7 GB of nested repos (gitignored)
  worktrees/         per-PR and per-issue checkouts `swe gh` makes   (gitignored)
  reports/           generated daily and weekly reports
  completions/       generated shell completion scripts
  data/              this machine's stores: ledgers, catalog,   (gitignored)
                     search index, Devin cloud, remote state
    remote/remote.sqlite        session runtime store: sessions, keepers,
                                launches, command receipts
    remote/keepers/<slug>/      one session's keeper: keeper.json, spec.json,
                                keeper.out and log/ (the event log)
    remote/local.json           the loopback API's token and address
    remote/agent-kept.json      satellite only: keeper sessions the hub copies
  cache/             sessions, rate limits, counts             (gitignored)
  backups/           pre-overwrite copies                      (gitignored)
```

`$HOME` rather than `$HOME/.config` because 24 of 28 harness config dirs live
there.

## Data plane vs control plane

Two repos, two lifecycles. This repo is the control plane: code, in git,
public. `~/.swe` is the data plane: state, in its own private git repo,
versioned separately from the CLI that reads it. Cloning this repo and
building it gets you a `swe` with no harnesses, no stars, nothing archived —
that state lives on the machine, or in whichever private repo the operator
points `~/.swe` at.

The boundary is a rule, not just a directory split: code in this repo never
hardcodes what `harness.json` can say. `skills/layout.py` and
`find/plugins.py` keep small fallback tables for machines with no registry
data (see Capabilities-first, below), but those tables are read-only
defaults, never a second source of truth standing beside the registry.
Anything this repo needs to know about a specific harness — is it starred,
does it support plugins, where do its skills live — is a question for
`harness.json`, not a constant in source.

`.gitignore` enforces the boundary mechanically: `harness.json`, `mcp.json`,
`tools.json`, `providers.json`, `stars.json`, and the caches are all listed,
so a machine's actual state can never land in this repo by accident.
`~/.swe` carries its own `.gitignore` — written by `swe init` from
`paths.GITIGNORE_BODY`, and listing `cache/`, `backups/`, `/data/` (this
machine's stores, anchored so a skill's or plugin's own `data/` folder still syncs; which the app and swe rewrite while they run: the usage
ledgers, the model catalog, the FTS index of session message text, Devin
session titles, remote state, account link records and their lock files),
`worktrees/` (the checkouts `swe gh pr worktree` and `swe gh issue worktree`
make, each with its own `.git`), `skills/.system/` (the bundled skills Codex
writes into its linked skills folder), `secrets/` (provider API keys and MCP
credential values) and `mcp/servers/` —
plus its own git history, independent of this one. `swe init` writes that
file only when there is none, so it never rewrites one the operator has
edited; a root created before `secrets/` joined the list has to add the line
by hand before versioning `~/.swe`.

Both credential stores are shell-style `export NAME=value` files, each in
its own folder under `secrets/`. Provider keys used to sit at `~/.api_keys`
and MCP values at `secrets/.api_keys`; `swe init` moves each file into its
folder (`init/migrate.move_secrets`) and leaves a symlink at each old path:
shell profiles source `~/.api_keys`, and an older swe on the same machine (a
Nix-pinned build, say) still reads `secrets/.api_keys` for `swe mcp sync`. Before anything lands there,
`secrets/` gets its own `.gitignore` of `*` (`seal_secrets_dir`; the app's
`APIKeysStore` does the same before a first save). Git lets that file win
over the root one, so an older root `.gitignore`, or one with a
`!secrets/...` exception, never puts keys in a commit. Until a machine is
moved, every reader (`paths.provider_keys_path_for`,
`paths.mcp_secrets_path_for`, the app's `APIKeysStore.defaultURL`) falls back
to the old path.

### Syncing the hub repo between machines

`swe sync` (`src/swe/sync/`) moves config between machines through that git
repo, so git stays the only transport. It commits what changed, merges the
remote in, pushes, and, when something came in, runs what `swe init --hub`
runs after a clone: the link pass, `register_marketplaces`, and `swe mcp sync
swe --all --force --only=<servers>` for the servers the merge added or changed
in `mcp.json` (removed ones are a note). The branch decides the mode. On
`device/<machine>` it merges `origin/device/<machine>` and then the remote's
default branch, and pushes only the device branch, so `main` changes only when
someone merges into it on purpose. On any other branch every machine shares
it. Commit and merge messages name the machine by its hostname, or by
`SWE_MACHINE_NAME` when that is set.

Sync merges and never rebases, and each merge is tried first with `git
merge-tree --write-tree` on unreferenced commits built from a scratch index.
A conflict therefore stops the sync before the branch, the index or the
working tree moves, and a merge that still fails late is aborted and rolled
back to the saved HEAD and index tree. It never force-pushes. One conflict it
settles itself: `config/harness.json`, which every machine writes (`swe setup`
registers what it finds), so two machines set up from one hub differ at least
in each entry's `added` time. When the line merge stops on that file alone,
`registry.merge_registries` merges it harness by harness and field by field;
`added` keeps the earlier time, `version` and `discovered_via` keep this
machine's value, and any other field both sides changed is still a conflict. Every `git add`
excludes `paths.GITIGNORE_BODY`'s paths by pathspec, so a hub with an older
`.gitignore` still never commits `secrets/`, `cache/`, `data/` or `skills/.system/`; those files
are reported as held back. One the hub already tracks (committed before the
line was there) is untracked by the next sync commit with its file left in
place. An incoming ref that still tracks one, from an older clone, is merged
through a child commit that stops tracking it, so neither side of the merge
has the path and git never writes this machine's file (a store the app has
open, a secret behind an incoming symlink).

## The core idea: one file, many names

Every harness wants its own instruction filename (`CLAUDE.md`, `AGENTS.md`,
`CRUSH.md`) in its own directory. SWE writes one file and symlinks it under
each name, so editing one changes all of them.

With chapter mode, `swe instructions build` generates that complete shared
file from `~/.swe/AGENTS_MD/index.txt`. An optional `targets.json` next to
it pins chapters to harnesses: a named harness whose chapter set differs
gets its own `AGENTS.<name>.md`, and build repoints links and prunes
retired outputs. `check` detects stale output and
`watch` keeps rebuilding after edits. Edit chapters in this mode: direct
changes to the generated file are overwritten. See
[docs/instructions.md](docs/instructions.md).

The same trick covers skills: one tree at `~/.swe/skills`, symlinked into
every harness's `skills/` directory.

Hooks do not fit that shape, so they get their own. A Claude Code Stop hook
and a Codex hook speak different contracts and most harnesses have none, so
nothing is shared: `~/.swe/hooks/<harness>/` holds one harness's scripts,
named by its `harness.json` key, and a harness only takes part once that
directory exists. Each script is linked on its own into the harness's hooks
directory rather than linking the directory, because installers keep their
own scripts beside it (herdr writes into `~/.claude/hooks`). The destination
is `capabilities.hooks.root`, then `init/hooks.py`'s `HOOK_FALLBACK`.

Only the file is linked. Declaring the hook (event, matcher, timeout in
`~/.claude/settings.json`, `~/.codex/hooks.json`) stays with whatever manages
that file, which on a Nix machine is a read-only link into the store. A plain
file at the destination with the same bytes as the source is `absorb`, not
`linked`: it is a copy, and edits to it would never reach the versioned one.

Link state is a small vocabulary shared by `swe init`, `swe list` and
`swe find`:

| state | meaning |
|---|---|
| `linked` | points at the shared copy |
| `relink` | a symlink aimed somewhere else |
| `create` | nothing there yet |
| `absorb` | a real directory whose contents are all duplicates or empty |
| `keep` | a real directory holding files that exist nowhere else |
| `conflict` | a real file where the link should go |
| `skipped` | harness not installed (`swe init` lists it only when `harness.json` has the harness), or a new skills folder nobody has reviewed yet |
| `ignored` | listed in `~/.swe/.linkignore`, archived in `harness.json`, or owned by a known non-harness app (`NOT_HARNESSES` in `init/layout.py`); never touched or counted |

`keep` is the safety valve. It is never overwritten on a plain run, because
absorbing it would hide the only copy of something behind the shared tree.
A folder a harness writes into its own skills root and rewrites on start is
not something to hide: Codex's `.system`, at the top of the root and carrying
its `.codex-system-skills.marker` (`HARNESS_SKILL_MARKERS` in
`init/layout.py`), is left out when a skills root is classified, so a root
holding only that is `absorb`. The absorb backs it up with the rest.

`ignored` is the opt-out. `~/.swe/.linkignore` holds home-relative
patterns (`fnmatch`, `*` crosses `/`, naming a directory covers its
contents), and `init/layout.plan` stamps every matching target with that
state before classifying it. `swe init` and `swe list` show it; `swe find`
still reports the path's real state, since it is an inventory rather than a
plan.

## Registry: harness.json

One file says everything the CLI knows about a harness:
`~/.swe/config/harness.json`, keyed by name. `registry.py` is the only
module that touches it — `load_registry()`, `save_registry()`,
`state_of()`, `is_active()`, `starred_names()`, `archived_names()`,
`active_names()`, and the read-only `load_registry_if_present()` that
`swe find` uses, so a look-only command can never seed the file on a
machine that has never run a write. On a machine with neither
`harness.json` nor a legacy `tools.json`, `load_registry()` saves an empty
registry rather than seeding defaults: only `swe discover` knows what is
installed, matching against `HARNESS_CATALOG`.

Each entry carries the harness's own fields (command, aliases, tags, ...)
plus:

| field | meaning |
|---|---|
| `display_name` | optional human name ("Qwen Code"); absent or empty means unset. Set with `swe edit --display-name=`; shown by `swe info` and the app, never used to find the harness |
| `state` | `"active"` \| `"starred"` \| `"archived"`; absent means active |
| `pin` | starred only — 1 is top, higher sorts lower |
| `archived` | archived only — `{reason, archived_at, usage}` |
| `capabilities` | optional — see Capabilities-first, below |

Three states, matching `multiselect.py`'s `STATES` tuple exactly — the
interactive editor cycles a row through the same three words the registry
stores, so there is no translation layer between what you pick on screen
and what lands on disk.

This used to be three files: `tools.json` said what a harness is,
`stars.json` said which ones you favourite, `archived.json` said which
ones you shelved. They always moved together in practice — starring meant
resolving an alias against the registry and then writing a second file —
and anything that wanted the full picture had to load all three and join
them by name. `stars.py` and `archive.py` are now compatibility shims:
same function signatures, same return shapes, but every read and write
goes through `registry.py` underneath, so `swe hs star`, `swe hs archive`,
and a handful of other old call sites kept working unmodified.

A fresh machine, or one upgrading from before this consolidation, gets a
lazy migration the first time `load_registry()` runs: `tools.json` plus
whichever of `stars.json` / `archived.json` exist get merged into
`harness.json`, and the legacy files are moved — never deleted — into
`~/.swe/.backup/registry-migration-<date>/`. Once `harness.json` exists
on disk, it is authoritative; the migration never runs again, even if the
legacy files are still sitting there.

Archiving is deliberately not `swe remove`. Removing forgets the harness,
which loses the fact that you evaluated it, so a month later it reads as
untried and gets reinstalled. `swe list --scope` decides which state you
see: `active` (default), `archived`, or `all`.

## Capabilities-first

Hardcoded tables predate the registry knowing about capabilities:
`skills/layout.py`'s `HARNESS_ROOTS` (which harness's skills root lives
where), `find/plugins.py`'s `PLUGIN_FALLBACK` (which harnesses support
plugins, and where their install record lives), `init/hooks.py`'s
`HOOK_FALLBACK` (where hook scripts go), `remote/launch_model.py`'s
`MODEL_FALLBACK` (how a remote launch puts a picked model on the
harness's command line), `remote/launch_yolo.py`'s `YOLO_FALLBACK`
(the argv tokens a `yolo: true` launch appends to switch a harness's
approval checks off), `remote/launch_provider.py`'s `PROVIDER_FALLBACK`
(which provider keys and base URLs a `provider` launch puts in a harness's
environment) and `remote/attachments.py`'s `ATTACH_FALLBACK`
(whether a harness's prompt expands `@/abs/path` for an attached file).
All are fallbacks, not sources of
truth. A harness entry's own `capabilities.skills.{supported,root}`,
`capabilities.plugins.{...}`, `capabilities.hooks.{...}`,
`capabilities.model.{supported,flag,subcommand,settings_flag,settings_key,provider}`,
`capabilities.yolo.{supported,argv}`
`capabilities.provider.{supported,providers}`
or `capabilities.attachments.{supported,at_refs}`
wins whenever the registry has one; the table only fires for a harness the
registry has never heard of, or for a machine with no registry data at
all.

This is why a registry entry knowing a root the table lacks is healthy —
capabilities extending the table is the design working — and why a table
entry the registry contradicts is also healthy: the fallback losing to a
capability that overrides it is the design working too. What is *not*
healthy: the same path filed under two different names (the join between
table and registry breaks), a `supported: true` capability with no `root`
to join on, or a table entry naming a harness the registry has never
heard of while its root exists on this machine. A row for a harness that
is neither registered nor installed is the table covering someone else's
setup, so it stays quiet. `swe doctor`'s code-vs-data check (see Drift, below) enforces
exactly that distinction.

## Scope

A file is **global** when it sits directly in a harness root (`~/.<tool>/` or
`~/.config/<tool>/`) or is symlinked to the shared copy. One level deeper is
**vendored** (plugin caches, editor extensions). Everything else is **local**.

This rule drives `--scope=[all,global,local]` across `swe find`.

For `swe init`, `archived` also means unmanaged: a path of an archived harness that would change is reported `ignored` ("archived in harness.json"), while an already-`linked` path stays linked — init never unlinks. Known harnesses are declared in `init/layout.py`'s `HARNESS_SIGNATURES`: skills and instruction targets plus `evidence`, an any-of list of home-relative paths that mean "installed" (the targets' own parents count implicitly). Evidence exists because it can live away from the targets — kilo proves itself with `~/.config/kilo` while its skills root is `~/.kilo/skills`. Registry `capabilities.instructions.file` / `capabilities.skills.root` entries always override or extend signatures. A discovered skills root that no registry entry, alias or signature claims triggers the init picker (`--yes` registers all without asking): ticked harnesses are registered and linked, unticked ones are archived and left alone.

## Discovery

Two scans, deliberately different:

- **`init/layout.discover_skill_roots`** joins three sources: the
  `HARNESS_SIGNATURES` table (a known harness's skills root, emitted when
  any of its `evidence` paths exists — how kilo is found via
  `~/.config/kilo` while its skills root is `~/.kilo/skills`), a glob of
  `~/.*/skills` and `~/.config/*/skills` for harnesses with no signature,
  and `capabilities.skills.root` declared in harness.json. This is the
  authority on where harnesses keep skills.
- **`skills/discovery.skill_roots`** builds on it and adds the three kinds it
  cannot reach: plugin caches (a level deeper), non-standard names
  (`~/.cursor/skills-cursor`), and user-registered catalogs.

Roots are deduped by `realpath`, so 57 symlinked roots collapse to the 8 real
directories behind them.

## Find: the harness activity filter

`swe find plugins` also reports plugin drift. Claude Code and Codex both
register a `~/.swe/plugins` marketplace in place. Codex runs an installed
cached copy, so an edit under `~/.swe` reaches it only after a reinstall.
Claude Code reads a directory-marketplace plugin from its folder, so its
cache is never compared. `swe skills --json` follows the same rule: such a
Claude plugin's skills are listed from the source path its marketplace
manifest gives and its cached versions are skipped, while a plugin Claude
fetched (github, git, a URL) is listed from the cache it runs.
`find/plugin_drift.py` compares each locally sourced plugin with both
harnesses' registration, install record or cache, and enable state, and
returns findings of five kinds (`unregistered`, `not-installed`,
`linked-manifest`, `stale`, `disabled-here`), each with a fix command. Codex
follows no symlink when it copies a plugin, so the source is compared
without its symlinks, the way Codex copies it. A symlinked `.codex-plugin`
is its own finding, `linked-manifest`: Codex skips it and installs a
manifest it generates, with no version, so no reinstall can help until the
link in the source is replaced by a real folder. It never writes: registration
lives in `~/.claude/settings.json` (often Nix-managed) and
`~/.codex/config.toml` (rewritten by Codex), which swe must not edit.
`swe find plugins --fix=<id>` (the app's one-click fix) is the only path
that acts, and it does so through the harness's own CLI (or, for
`linked-manifest`, a `cp -R` of the link's target inside the marketplace
to a new folder beside the link, then `rm` and `mv` to swap it in; only
the link is ever deleted, and that is the user's own source tree): it recomputes
drift, finds the finding by id (`<harness>:<kind>:<ref>`, with the source
directory in place of the ref for `unregistered`, since two directories can
declare one marketplace name), refuses an id that matches more than one, and
runs that finding's `argv` without a shell, stopping at the first failure.
The harness runs with `HOME` set to the home drift was read from and
`CLAUDE_CONFIG_DIR` / `CODEX_HOME` removed, so it changes the account that
was checked. A fix with
no verb (codex has no `plugin enable`) has no `argv` and is refused.

Every `swe find` view prints section tallies by default, the same shape as
`swe init`'s summary, and `--full` prints the tree with one row per path.
The tally helper lives in `find/summary.py`; each view's summary reads only
the data its full view already computes.

`swe find` and every one of its subviews (`amd`, `skills`, `plugins`,
`mcps`) take `--harness=active|all`, default `active`. `active` hides rows
belonging to an archived harness; starred still counts as active, since
starring is a pin, not a shelf. `all` shows archived harnesses too.

A hidden row is never silent. Every view that filters ends with a footer —
`N archived harnesses hidden; --harness=all to show` — so `--harness=active`
narrows what you see without ever costing you the knowledge that something
was narrowed. A row that cannot be resolved to a harness in `harness.json`
at all — the shared swe copy, an unregistered config — is never
filtered either way: unknown is exactly the thing `swe find` exists to
keep visible, regardless of `--harness`.

Which harnesses get walked, and where each one's files live, comes from
`harness.json`'s own `capabilities` first, falling back to the hardcoded
tables above only for a harness the registry has never heard of — the same
capabilities-first rule the rest of the CLI follows.

## Sessions

Every harness stores history differently, so parsing is split into engines:

| engine | format | harnesses |
|---|---|---|
| `jsonl_engine` | newline-delimited JSON | claude, codex, most others |
| `json_engine` | single JSON documents | cursor, several forks |
| `sqlite_engine` | SQLite databases | editor-embedded tools |

`aggregator.py` fans out across them and merges. `failures.py` records parser
crashes so a broken parser reports an error rather than reading as "no
history" — added after Cursor silently showed 0 sessions for months.

## Caching

`swe list` runs in ~70ms because usage is opt-in. It was 870ms when usage was
fetched unconditionally. The full map of persistent stores, their formats,
TTLs and the app's ledger schema lives in
[docs/data-model.md](docs/data-model.md).

## MCP

`~/.swe/mcp.json` is the hub. Credentials are stored as `${NAME}`
references; values live in `~/.swe/secrets/mcp/.api_keys` (mode 600,
gitignored). Resolution happens when a harness config is written, not by
exporting to the environment, because Claude and Cursor launch from the Dock
and never read a shell profile.

Servers are namespaced by function: `rf` reference, `pd` productivity,
`dv` development, `so` social, `sr` search. `swe find mcps` shows what the
hub holds against what each harness actually has.

One server may appear as both a local and a remote entry, since those are
two ways to reach it. Two locals, or two remotes, under different names
are the same thing filed twice, and are reported as duplicates.

`swe find mcps` reads two sources deliberately. The registered config
paths tell you how far a sync got; a walk of the harness directories
tells you about configs nobody registered, which is where an unmanaged
server hides. The walk covers JSON and TOML, files sitting directly in
`$HOME` (`~/.claude.json` belongs to no harness directory), and follows
the same vendored rule as the rest of `find`.

### The gateway

Each harness session starts its own copy of every stdio server it is
configured with. With 17 servers that was 26 processes and about 1.3 GB
per Claude Code session. A hub entry marked `"shared": true` (`swe mcp
gateway share`) runs once instead, in the standalone `warp-gate` daemon
(repo `starship-terminal/warp-gate`), which `warp-gate install` puts
under launchd (`com.starship-terminal.warp-gate`) and which serves it as
MCP streamable HTTP at `http://127.0.0.1:8478/mcp/<name>`. While
mcp.json's `gateway.enabled` is set (by `warp-gate install`), `swe mcp
sync swe ...` writes that URL for a shared server instead of its
command; Claude Desktop takes no URL server, so it keeps the command.

One stdio process is one MCP session, so the gateway multiplexes: it
runs the handshake once and answers each client's `initialize` from it,
renumbers request ids, forwards only the spec's client requests (Claude
Code probes HTTP servers with `server/discover`, which crashes older
Python SDK servers), refuses the server's own requests to a client, and
drops notifications. That is only right for servers without per-session
state, so `share` refuses a browser or REPL server without `--force`.
`discover` and `list` read a synced gateway URL as the hub's own server
(`gateway.matches_hub`), so the URL never flows back into the hub.

swe keeps the manifest side — `share`/`unshare`/`status` in
`mcp/gateway_commands.py` — and the wire contract with the daemon:
same port default, same `/mcp/<name>` paths, same `/status` and
`/health` endpoints. `gateway status` reports a daemon under any of the
labels it has run as (warp-gate's, plus the retired `swe`/`quiver`
agents a machine may still carry).

## Drift

`swe doctor` used to check one thing: Node/PATH mismatches hiding a
globally installed harness. `harness/drift.py` adds a second, unrelated
kind of check — places where two parts of swe that are supposed to
agree have quietly drifted apart. The checks are all read-only and cheap
enough to run on every invocation:

| check | compares |
|---|---|
| help vs dispatch | `help_text.py`'s HELP topics against `cli.py`'s COMMANDS |
| subcommand help | the `mcp` and `instructions` subcommands against their help and completion lists, and the subcommands the help prose names |
| completion | `completion.py`'s tables against `cli.py`'s COMMANDS |
| registry schema | every `harness.json` entry's shape (state, pin, archived, capabilities) |
| code-table vs data | `HARNESS_ROOTS` / `PLUGIN_FALLBACK` against the registry's capabilities |
| dangling symlinks | top-level symlinks in the repo root and `~/.swe` pointing nowhere |
| data root | a real `~/.quiver` left beside `~/.swe`, or still in use because the move to `~/.swe` could not run |
| hook drift | declared hooks, link states and the files on disk |

Every check exists as a pure `check_*` function (plain data in, findings
out, no I/O) for unit testing, and `run_drift_checks()` wires the real
data in for `cmd_doctor`.

Fixing is separate from finding. `harness/doctor_fix.py` holds one runner
per finding `action` and the order a batch runs them in; `swe doctor
--fix` and the web Doctor page's Run both call it. Batch actions only add or relink (`setup`,
`discover`, `init`, `plugin-fix`) and run in that order, re-reading the
report after each step, since a missing hub hides every link check; the
order repeats (at most three passes) while a step reveals more. Click actions
(`install`, `plugin-reinstall`, and the drops that delete entries or
links a synced file may still want elsewhere: `undeclare`,
`marketplace-drop`, `dangling-link`; `mcp-sync`, which overwrites a
broken MCP entry with the hub's) run only for one named finding. Plain `swe doctor`
never writes.

The principle behind all of them, made explicit once capabilities-first
landed: a warning must be actionable. Before that change, a registry entry
the fallback table didn't know about, or a table entry the registry
happened to override, both read as drift — which meant `swe doctor` warned
about the system working exactly as designed. Now those are asymmetries
the design expects, not findings; what still warns is a broken join (a
name mismatch, a `supported` capability missing its `root`, a table entry
for a harness the registry has never heard of) — something an operator can
actually go fix.

## Machine-readable output

The macOS app under `app/mac/` reads `swe` through `--json`, never by parsing
tables. The app's own structure is [docs/app-architecture.md](docs/app-architecture.md),
and each command below maps to its Swift decoder in
[docs/json-contracts.md](docs/json-contracts.md). Every `--json` command prints exactly one document on stdout,
`{"schema": 1, "data": ...}` (`swe/json_output.py`), and nothing else:
refusals and errors go to stderr with stdout left empty. `schema` changes
only for a breaking change to an existing shape; adding a field is not one,
and the app refuses a schema it does not know rather than misreading it.

Each command's `data` shape and the Swift type that decodes it are listed in
[docs/json-contracts.md](docs/json-contracts.md); the model that defines each
payload on the Python side is in the payload table in
[docs/backend-architecture.md](docs/backend-architecture.md).

A rate limit's `fetched_at` is when the reading was taken. A value served
from the cache keeps its original time, so the app can show its age
instead of presenting an old reading as current.

No `--json` output carries a secret value. MCP env and header values are
reduced to their names, `${NAME}` references are never resolved, and
credential-shaped parts of URLs and arguments are masked
(`swe/mcp/secrets.py`, `scrub_url` and `scrub_args`).

## Command tree

Two layers, on purpose. The top level is shortcuts for the harness verbs
run most often, plus a few one-shot commands that belong to no domain;
everything else lives under the domain command that owns it.

```
swe <verb>                          top level: shortcuts and one-shot commands
  init · list · info · add · edit · remove · use · check · doctor · install
  discover                          alias for `swe harness discover`
  tags · aliases · models · autocomplete · bug · version · help

swe <domain> <verb>                 domains: everything else
  harness    star · archive · discover · list · edit · usage · usage-events · usage-daily
  mcp        discover · list · status · sync · diff · edit · validate · doctor
             add · remove · secret · gateway
  find       amd · skills · plugins · mcps
  plugins    catalog · marketplace · install · uninstall · enable · disable
  providers  list · info · add · remove
  report     daily · weekly · followups · followup · warnings
  skills     list · tree · link · unlink · move · new · discover · hub · catalog · scope
  session    use · index
  setup      harnesses · providers · mcp · skills · report · check
  config     get · set · unset · edit · check · setup
  instructions  init · build · check · watch
  hooks      list · info · new · edit · enable · disable · delete · check · sync
  sync       status
  gh         pr · issue · run · inbox · repo · board · sync · pages · releases · tags · branches
  remote     serve · agent · setup · pair · status · devices · attach · notify · ssh
```

`swe harness star` and `swe harness archive` live under the harness domain
deliberately, not at the top level: they change what you *decided* about a
harness rather than what it *is*, and grouping them with `list` / `info` /
`edit` keeps every verb that touches a harness's registry row in one
place instead of scattering favourite/shelve logic across the top-level
namespace. `swe doctor`'s help-vs-dispatch check enforces the shortcut
half of this policy mechanically — every top-level `COMMANDS` entry needs
a HELP topic (or a whitelisted alias), and every topic needs a matching
command. The dead top-level `star` help topic this consolidation removed
was the leftover of an earlier layout where that boundary did not exist.

## Layering rules

The domain packages each follow a controller / service / model split:
`commands.py` files own argv and presentation, logic modules never print,
and `models.py` holds the records and `--json` payload shapes. The full
map, the bottom-layer ordering and the per-extension checklists are in
[docs/backend-architecture.md](docs/backend-architecture.md).

1. `console`, `table`, `paths`, `configuration` and `keys` are the bottom
   layer and never import a domain package. Among themselves the order is
   fixed: `table` imports `console`, `configuration` imports `paths`.
2. Command modules own presentation. Logic modules mostly do not print;
   `setup/wizard.py` is the deliberate exception (it is an interactive
   flow), and `harness/rate_limits.py` prints diagnostics from inside
   fetch logic, which is a leak rather than a decision.
3. `cli.py` is dispatch only.

The session runtime in `src/swe/remote/` adds two pieces to this layering,
both behind `remote.session_view` (off by default). `remote/adapters/` holds
one class per structured agent channel (`claude_stream`, `codex_appserver`,
`opencode_serve`, `acp`), registered by id in `adapters/registry.py`. Each
harness has one module that knows its wire format (`claude_wire.py`,
`codex_wire.py`, `opencode_wire.py`; the ACP wire is `remote/acp.py`), and the
adapters share `base.py`, `core.py` and `events.py`. No adapter imports the hub. `remote/keeper/` is a process, started as `python -m
swe.remote.keeper`, that holds one adapter, its send queue and its event log;
the hub's `runtime.py` and a satellite's agent talk to it over a Unix socket.
`hub.py` imports `agent.py`, so `agent.py` imports `hub` only inside a function.
[docs/remote-control.md](docs/remote-control.md) has the package map.

One cycle exists, `harness <-> sessions`, and both directions are
function-local imports rather than module-level, so it does not break loading.
