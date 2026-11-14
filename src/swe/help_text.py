"""CLI help text and help command."""

from swe.console import c
from swe.harness.registry import load_registry_readonly
from swe.harness.tools import is_installed
from swe.paths import HARNESS_FILE

HELP = {
    "list": (
        "List all registered AI coding tools (short for swe harness list)",
        f"""\
  {c('cyan', 'swe list')}                     List all tools (starred first, then 100d usage)
  {c('dim', 'Short for')} {c('cyan', 'swe harness list')}{c('dim', '; every harness verb lives under swe harness.')}
  {c('cyan', 'swe list <tag>')}               Filter by tag (e.g. swe list agentic)
  {c('cyan', 'swe list --usage')}             Add 100d sessions and remaining quota
  {c('cyan', 'swe list -u --exclude=a,b')}     Same, without polling usage for a and b
  {c('cyan', 'swe list --links')}             Add AGENTS.MD and SKILLS link status
  {c('cyan', 'swe list -n')}                  Fetch new session and rate-limit data
  {c('cyan', 'swe list edit')}                Choose which columns to show
  {c('cyan', 'swe list edit --reset')}        Restore the default columns
  {c('cyan', 'swe list legend')}             Explain the swe list column glyphs

  Starring pins a tool to the top of {c('cyan', 'swe list')} ({c('neon_pink', '★')}) and opts it into
  rate-limit / quota fetching — the {c('cyan', '--usage')} column only polls starred
  tools. It does not change what {c('cyan', 'swe use')}, {c('cyan', 'swe report')}, or {c('cyan', 'swe session')} do.
  Use {c('cyan', 'swe hs star <name>')} to favourite, {c('cyan', 'swe hs archive <name>')} to shelve.

  Archiving hides a tool from {c('cyan', 'swe list')} and records why; nothing gets
  uninstalled. {c('cyan', 'swe list --scope=all')} brings it back.

{c('bold', 'Flags')}
  {c('cyan', '--usage')} / {c('cyan', '-u')}            Show usage. This is the only part of swe list
                          that touches the network, so it is opt-in: the plain
                          listing runs in ~70ms, --usage costs ~700ms on a cold
                          cache. Implied by --refresh.
  {c('cyan', '--links')} / {c('cyan', '-L')}            Show swe link status instead of usage.
  {c('cyan', '--refresh')} / {c('cyan', '-r')} / {c('cyan', '-n')}   Bypass caches and fetch new data.
  {c('cyan', '--refresh-failed')}            Refetch only providers whose cached reading
                          failed (timeout, sign-in needed, a window unread);
                          healthy cached readings still serve.
  {c('cyan', '--cache-ttl')}=<seconds>       How fresh a cached reading must be to serve,
                          overriding the 5-minute default for this run.
  {c('cyan', '--scope')}=active|archived|all  Which harnesses show. Default active (archived
                          hidden); archived shows only shelved tools; all shows both.
  {c('dim', 'Collision: swe find also has a --scope, but it means something else there')}
  {c('dim', '(where a file lives, not archived-visibility). In swe find, archived-')}
  {c('dim', 'visibility is --harness=all instead.')}

{c('bold', 'Link states')}
  {c('green', '✓')}  linked to ~/.swe      {c('yellow', '○')}  nothing there yet, or safe to absorb
  {c('yellow', '↻')}  symlink points elsewhere  {c('red', '✗')}  real file in the way, needs --force
  {c('yellow', '○')}  holds files that exist nowhere else, left alone on purpose
  {c('dim', '·')}  no known convention, or harness not installed

  {c('cyan', 'swe list legend')} prints this with each state named.
  Run {c('cyan', 'swe init')} to link everything."""
    ),
    "info": (
        "Show full details for a tool",
        f"""\
  {c('cyan', 'swe info <name|alias>')}        Show command, version, path, tags, aliases

{c('bold', 'Examples')}
  swe info claude
  swe info cc"""
    ),
    "use": (
        "Launch a tool (replaces current process)",
        f"""\
  {c('cyan', 'swe use <name|alias> [args]')}  Launch a registered tool
  {c('cyan', 'swe run <name|alias> [args]')}  Same as use
  {c('cyan', 'swe use --provider=<slug> <name> [args]')}
                                Start it on a provider from the keys file

  Extra args are passed through to the underlying command.
  {c('dim', '--provider')} goes before the name. It sets only that provider's
  variables (key, base URL) as harness.json capabilities.provider
  declares, and drops the keys file's other variables.
  Uses {c('dim', 'os.execvp')} to replace the process cleanly.

{c('bold', 'Examples')}
  swe use cc
  swe use codex --help
  swe use gemini -p 'explain this codebase'"""
    ),
    "add": (
        "Register a new tool in the registry",
        f"""\
  {c('cyan', 'swe add <name> <command>')}             Add with defaults
  {c('cyan', 'swe add <name> <command> [desc]')}      Add with description
  {c('cyan', 'swe add <name> <cmd> --aliases=a,b')}   Set short aliases
  {c('cyan', 'swe add <name> <cmd> --tags=t1,t2')}    Set tags
  {c('cyan', 'swe add -i')}                           Interactive form (walk each field)
  {c('cyan', 'swe add <name> -i')}                    Interactive, pre-filled name
  {c('cyan', 'swe add <name> <cmd> --no-replace --json')}  For the app: refuse existing names

  The {c('dim', 'registry')} is one file, {c('bold', '~/.swe/config/harness.json')}, listing every
  tool swe knows. Adding a tool here does not install it — it tells
  swe the tool exists, so {c('cyan', 'swe list')}/{c('cyan', 'swe use')}/{c('cyan', 'swe edit')} can find it.
  If the tool already exists, it updates the entry.

{c('bold', 'Examples')}
  swe add aider aider "AI pair programmer" --aliases=ai --tags=agentic,coding
  swe add mytool /usr/local/bin/mytool"""
    ),
    "edit": (
        "Edit fields of a registered harness",
        f"""\
  {c('cyan', 'swe edit <name|alias>')}                Interactive field editor
  {c('cyan', 'swe edit <name> --description="..."')}  Set description
  {c('cyan', 'swe edit <name> --aliases=a,b')}        Replace aliases (comma-separated)
  {c('cyan', 'swe edit <name> --tags=t1,t2')}         Replace tags
  {c('cyan', 'swe edit <name> --command=<cmd>')}      Change launch command
  {c('cyan', 'swe edit <name> --display-name="..."')} Set the human name (e.g. "Qwen Code")
  {c('cyan', 'swe edit <name> --version=<ver>')}      Set version string
  {c('cyan', 'swe edit <name> --notes="..."')}        Set notes
  {c('cyan', 'swe edit <name> --primary-color=#RRGGBB')}  Set the brand colour
  {c('cyan', 'swe edit <name> --set=field=value')}    Compact multi-set form
  {c('cyan', 'swe edit <name> --notes= --json')}      Apply the flags, print the result as JSON

  Editable fields: command, display name, description, aliases, tags,
  version, notes, primary color. The display name is how the app (and swe info)
  names the harness; the registry name and command stay what they are.
  --primary-color accepts #RRGGBB or RRGGBB and stores it uppercase; an
  empty value drops the entry's own colour so the catalog default shows
  again. --display_name and --set=display_name=... work too.
  With no field flags, opens an interactive prompt loop (save / quit).
  An empty value clears a field: --display-name=, --version=, --notes= and
  --primary-color= become unset,
  --description= becomes "", --aliases= and --tags= become [].
  Alias collisions with other tools are rejected.
  With --json it never prompts: no field flag is an error, and every
  refusal (unknown harness, alias collision, empty command, unknown flag)
  prints a message on stderr, nothing on stdout, and exits 1. Success prints
  {{"name", "changes", "harness"}}, where harness is the swe list --json row.

{c('bold', 'Examples')}
  swe edit mastracode
  swe edit mastracode --description="Mastra Code — AI coding agent" --aliases=mc
  swe edit droid --set=tags=agentic,coding,autonomous"""
    ),
    "remove": (
        "Remove a tool from the registry",
        f"""\
  {c('cyan', 'swe remove <name|alias>')}      Remove by name or alias
  {c('cyan', 'swe rm <name|alias>')}          Same as remove

  Does not uninstall the tool, only removes it from the registry.
  {c('dim', 'See')} {c('cyan', 'swe help add')} {c('dim', 'for what the registry is.')}"""
    ),
    "check": (
        "Verify install status and refresh versions",
        f"""\
  {c('cyan', 'swe check')}                    Probe each tool for live version

  Tries version / --version / -v / -V flags.
  Stores bare version numbers only (no tool-name prefix).
  Also warns about off-PATH installs (e.g. nvm globals invisible to swe).
  Updates registry if version changed."""
    ),
    "doctor": (
        "Diagnose Node/PATH issues that hide global installs",
        f"""\
  {c('cyan', 'swe doctor')}                   Report node/npm, global bin PATH, nvm, off-PATH tools
  {c('cyan', 'swe doctor --json')}            Findings as JSON: ok, plus check, severity, message, fix
  {c('cyan', 'swe doctor --fix')}             Run every safe local fix, then check again
  {c('cyan', 'swe doctor --fix=<check>')}     Only findings under that check (setup, drift.hooks)
  {c('cyan', 'swe doctor --fix --dry-run')}   Show what --fix would run, run nothing (alias: --check)
  {c('cyan', 'swe doctor --fix=<check> --message=<text> [--action=<a>]')}
                               Run one finding's fix, as a Run button does

  Plain {c('cyan', 'swe doctor')} only reads. {c('cyan', '--fix')} runs the local repairs
  in order (setup, discover, init, plugin-fix), reading the report again
  after each, and prints what it fixed and what is left. Installs, plugin
  reinstalls, MCP entry overwrites (mcp-sync) and the drops that delete
  entries or links (undeclare, marketplace-drop, dangling-link) never run
  in a batch: they run only
  for one finding named with --message. With --json, --fix prints
  {{dry_run, ran, fixed, still_failing, skipped, not_cleared, fixable_left}}.

  Doctor also reports plugin drift (swe find plugins) as plugins.*,
  broken top-level links in ~/.swe, and MCP servers failing a local
  check (swe mcp doctor without the network) as mcp.server, each with
  its fix.

  Catches the common failure mode: {c('dim', 'npm install -g')} under nvm while
  interactive/non-interactive shells use Homebrew Node (tool missing from PATH).

  Also runs read-only drift checks: places two parts of swe were supposed
  to agree and quietly did not — help text vs dispatch, registry shape,
  hardcoded fallback tables vs harness.json, dangling symlinks. A finding
  looks like:
    {c('dim', "! [registry] cursor: state 'disabled' is not one of ['active', 'archived', 'starred']")}

{c('bold', 'Exit codes')}
  0  healthy
  1  off-PATH tools, global bin not on PATH, or an error-severity drift
     finding. Warn-level drift (stale help, code-table mismatches, symlink
     notices) is printed but does not fail the exit code.
  With --fix: 0 when every fix worked and none in scope is left; findings
  that need a person (logins, installs, PATH edits) do not count."""
    ),
    "install": (
        "Show or run a harness's install command per package manager",
        f"""\
  {c('cyan', 'swe install <name>')}                    List its install commands (runs nothing)
  {c('cyan', 'swe install <name> --via=<method>')}     Run one: brew, cask, npm, uv, nix or script
  {c('cyan', 'swe install <name> --package=<pkg>')}    Explicit npm package (works for unknown names)
  {c('cyan', 'swe install <name> --command=<cmd>')}    Override CLI binary name to register
  {c('cyan', 'swe install <name> --dry-run')}          Print the command, run nothing (alias: -n)
  {c('cyan', 'swe install [--json]')}                  Known installers as JSON (never runs)

  Methods are the package managers a harness is published through; npm uses a
  PATH-visible npm (prefers Homebrew over nvm) so the binary lands where
  {c('cyan', 'swe list')} / {c('cyan', 'swe check')} can see it. A name with no known
  installer is refused — swe never guesses package names.

{c('bold', 'Examples')}
  swe install gemini
  swe install gemini --via=npm
  swe install claude --via=cask --dry-run
  swe install mastracode --package=mastracode
  swe install --json"""
    ),
    "session": (
        "Show recent AI sessions across all agents",
        f"""\
  {c('cyan', 'swe session')}                  Show last 10 sessions
  {c('cyan', 'swe session <N>')}              Show last N sessions
  {c('cyan', 'swe session use <N>')}          Resume session #N
  {c('cyan', 'swe session -i')}               Pick with arrow keys; space reads a transcript, Enter resumes
  {c('cyan', 'swe session index')}            Index agent transcripts for --search
  {c('cyan', 'swe session index --on|--off')} Whether the remote hub keeps the index current
  {c('cyan', 'swe session index --status')}   What the index holds, without a pass
  {c('cyan', 'swe session mirror')}           Copy Claude Code and Codex transcripts into ~/.swe so they outlive
                                            the harnesses' 30-day pruning (a pass never deletes a copy)
  {c('cyan', 'swe session mirror --on|--off')} Whether the hub and swe session index keep mirroring (default on)
  {c('cyan', 'swe session mirror --status')}  What the mirror holds, without a pass
  {c('cyan', 'swe session mirror --purge=<N|id>')} Delete one mirrored session's copies, backups and manifest
                                            entries; lists the files and asks first (--yes skips the prompt)
  {c('cyan', 'swe session mirror --sweep-tmp')} Remove the mirror's own temp files a crash left over a day ago
  {c('cyan', 'swe session restore <N|id>')}   Put a mirrored transcript back where its harness kept it, so the
                                            session resumes again; never overwrites an existing file

{c('bold', 'Flags')}
  {c('cyan', '--agent=<name>')}               Filter by agent (claude, codex, opencode, droid, ...)
  {c('cyan', '--here')}                       Filter to current directory only
  {c('cyan', '--search=<text>')}              Filter title/path/agent/session id, and transcript
                                            content when a search index exists (the SWE
                                            app's, or swe session index) (alias: -q, --grep)
  {c('cyan', '-d N, --days=<N>')}             Include today and the preceding N-1 calendar dates
  {c('cyan', '-w N, --weeks=<N>')}            Include the latest N times 7 calendar dates
  {c('cyan', '-s, --start=<YYYY-MM-DD>')}     Inclusive range start; use together with --end
  {c('cyan', '-e, --end=<YYYY-MM-DD>')}       Inclusive range end; use together with --start
  {c('cyan', '-i, --interactive')}            Pick with arrow keys; Enter resumes, space opens the transcript
  {c('cyan', '--json')}                       Print the listing as one JSON document; not with use or -i
                                            (a row with mirrored: true lost its transcript to the
                                            harness; it lists and reads from swe's copy but cannot resume)

{c('bold', 'Examples')}
  swe session
  swe session 20
  swe session use 3
  swe session -i
  swe session --agent=claude
  swe session --here
  swe session -d 5
  swe session -w 3
  swe session -s 2026-07-01 -e 2026-07-30
  swe session --search=login
  swe session 30 -q refactor
  swe session index --on
  swe session mirror --json
  swe session restore 3
  swe session mirror --purge=3"""
    ),
    "report": (
        "Summarize coding sessions and manage follow-ups",
        f"""\
  {c('cyan', 'swe report daily')}              Preview and generate a daily coding-session report
  {c('cyan', 'swe report weekly')}             Preview and generate a weekly coding-session report
  {c('cyan', 'swe report warnings <manifest>')} Print warnings recorded for one specific report
  {c('cyan', 'swe report followups')}                     List open follow-ups
  {c('cyan', 'swe report followups --status=open|done|dismissed')}  Filter by status (default open)
  {c('cyan', 'swe report followup done <id>')}  Mark a follow-up done manually
  {c('cyan', 'swe report followup work <id>')}  Resume its source or start a contextual session

Before any model runs, SWE displays session counts, exclusions, cache hits,
planned calls, and estimated input tokens. Normal plans ask for y/N. Plans over
configured limits require the exact phrase {c('bold', 'process all')}.
When a report completes with warnings, SWE prints the exact
{c('cyan', 'swe report warnings <manifest.json>')} command for that report.

{c('bold', 'Source flags')}
  {c('cyan', '-d N, --days=<N>')}              Override the report with N calendar dates
  {c('cyan', '-w N, --weeks=<N>')}             Override the report with N times 7 calendar dates
  {c('cyan', '-s, --start=<YYYY-MM-DD>')}      Inclusive custom start; requires --end
  {c('cyan', '-e, --end=<YYYY-MM-DD>')}        Inclusive custom end; requires --start
  {c('cyan', '--here')}                        Include only the current project
  {c('cyan', '--agent=<name>')}                Include only one coding harness
  {c('cyan', '--search=<text>')}               Match session title/path/agent/id (alias: -q)

Reports run in two passes: a cheap model summarizes each session, then a
stronger model writes the final report from those summaries. The flags below
pick the harness/model for each pass; leave any of them out and SWE falls
back to {c('cyan', 'report.session.*')} / {c('cyan', 'report.writer.*')} in {c('cyan', 'swe config')}.

{c('bold', 'Runner override flags')}
  {c('cyan', '--session-harness=<name>')}       Cheap summarizer harness: claude or codex
  {c('cyan', '--session-model=<model>')}        Model used for project/session summaries
  {c('cyan', '--session-arg=<arg>')}            Pass one extra summarizer argument; repeat as needed
  {c('cyan', '--writer-harness=<name>')}        Harness used for the final report
  {c('cyan', '--writer-model=<model>')}         Strong model used for the final report
  {c('cyan', '--writer-arg=<arg>')}             Pass one extra writer argument; repeat as needed

{c('bold', 'Follow-up actions')}
  add <text> [--project=PATH]  Add an item; project defaults to the current directory
  edit <id> <text>             Correct an item's text
  done|dismiss|reopen <id>     Change status explicitly
  work <id> --resume           Resume the newest referenced supported session
  work <id> --new --harness=X  Start a new contextual session with harness X"""
    ),
    "config": (
        "View or update SWE configuration",
        f"""\
  {c('cyan', 'swe config')}                     Print resolved configuration
  {c('cyan', 'swe config get <key>')}           Print one resolved dotted key
  {c('cyan', 'swe config set <key> <value>')}   Save a string, number, boolean, or JSON array
  {c('cyan', 'swe config unset <key>')}         Remove a user-set value
  {c('cyan', 'swe config edit')}                Open ~/.swe/config/config.json in VISUAL or EDITOR
  {c('cyan', 'swe config check')}               Validate types, report setup, and secret safety
  {c('cyan', 'swe config setup report')}        Configure cheap summary and strong writer models

{c('bold', 'Examples')}
  swe config set report.session.harness claude
  swe config get report.writer.model
  swe config unset report.session.model

SWE never stores model credentials in this file. Claude and Codex continue
to use their own login state and environment."""
    ),
    "models": (
        "Show model usage across all tools",
        f"""\
  {c('cyan', 'swe models')}                   Flat list, model name only, sorted by count
  {c('cyan', 'swe models -t')}                Group by tool
  {c('cyan', 'swe models -p')}                Show provider prefix (e.g. openai/gpt-5.4)
  {c('cyan', 'swe models -t -p')}             Both: grouped by tool with providers

{c('bold', 'Flags')}
  {c('cyan', '-t, --by-tool')}                Group results by tool instead of flat list
  {c('cyan', '-p, --providers')}              Show provider/model instead of just model

  Default aggregates across providers (gpt-5.4 = openai + codex combined).
  Flags can be combined: {c('dim', 'swe models -t -p')}"""
    ),
    "skills": (
        "List agent skills and their file paths",
        f"""\
  {c('cyan', 'swe skills')}                   The skills overview (same as swe skills --help)
  {c('cyan', 'swe skills list')}              Every skill, nested under the root that holds it
  {c('cyan', 'swe skills <filter>')}          Search name, scope or harness: one line per match
  {c('cyan', 'swe skills -d')}                One line per skill, with its description
  {c('cyan', 'swe skills tree')}              Symlink layout — now the same view as swe find skills
  {c('dim', 'swe skills scope list forwards to the same place; --sync is accepted but ignored.')}
  {c('cyan', 'swe skills link <harness> [target]')}   Symlink a harness root to shared/other
  {c('cyan', 'swe skills unlink <harness> [--mkdir]')} Break a harness symlink
  {c('cyan', 'swe skills move <name> --from=A --to=B')} Move a skill folder between roots
  {c('cyan', 'swe skills hub')}               Plugins in ~/.swe/plugins and where they are installed
  {c('cyan', 'swe skills discover [--apply]')} Scan Desktop/Documents for skill catalogs
  {c('cyan', 'swe skills catalog add [path] [label]')} Register a skills directory (default: .)
  {c('cyan', 'swe skills catalog .')}                Add the current directory as a catalog

{c('bold', 'Flags')}
  {c('cyan', '-d, --desc')}                   Show skill descriptions

{c('bold', 'Scopes scanned')}
  shared          ~/.swe/skills (the tree every harness skills/ symlinks to)
  cursor-builtin  ~/.cursor/skills-cursor
  cursor-plugin   ~/.cursor/plugins/cache
  claude-plugin   ~/.claude/plugins/cache
  project         ./.cursor/skills (current directory)
  <catalog>       Paths from ~/.swe/config/skill_catalogs.json

  {c('dim', 'Discover')} finds folders named skills under ~/Desktop and ~/Documents,
  then {c('cyan', 'swe skills discover --apply')} or {c('cyan', 'swe skills catalog add')} registers them.

{c('bold', 'Help')}  {c('cyan', 'swe skills help <topic>')} — catalog, discover, tree, link, unlink, move, scope"""
    ),
    "tags": (
        "Show all tags and which tools use them",
        f"""\
  {c('cyan', 'swe tags')}                     List tags with associated tools"""
    ),
    "aliases": (
        "Show all short aliases for tools",
        f"""\
  {c('cyan', 'aliases')}                      List alias → tool mappings"""
    ),
    "mcp": (
        "Manage MCP servers across AI tools",
        f"""\
  {c('cyan', 'swe mcp discover [--apply]')}          Find MCP servers across tool configs
  {c('cyan', 'swe mcp list [tool]')}                 Matrix view of MCP servers across tools
  {c('cyan', 'swe mcp status [tool]')}               List with health checks
  {c('cyan', 'swe mcp sync <source> <target...>')}   Copy servers source → target(s) (--force, --skip-conflicts)
  {c('cyan', 'swe mcp diff <t1> <t2>')}              Compare two tools' configs
  {c('cyan', 'swe mcp edit <tool> <name>')}          Edit one server's config in one tool
  {c('cyan', 'swe mcp validate [tool...]')}          Validate MCP config shape for one/all tools
  {c('cyan', 'swe mcp enable/disable <name>')}       Switch a server on/off per harness
  {c('cyan', 'swe mcp set-type <name> TYPE')}        Label a hub server
  {c('cyan', 'swe mcp info <name>')}                 One server's full detail
  {c('cyan', 'swe mcp tools <name>')}                List a server's tools by handshake
  {c('cyan', 'swe mcp doctor')}                      Deep diagnostics
  {c('cyan', 'swe mcp gateway [share|status]')}      Shared servers run once via warp-gate

{c('bold', 'Help')}  {c('cyan', 'swe mcp <command> help')} for details on each — flags, examples, the exact
        model each command uses.
{c('bold', 'Source of truth')}  ~/.swe/mcp.json"""
    ),
    "providers": (
        "Manage AI provider API keys and metadata",
        f"""\
  {c('cyan', 'swe providers list [-d] [--api-keys-dir=DIR] [<filter>]')}
      List registered providers + masked key status (`-` = no key)
  {c('cyan', 'swe providers info <name|alias>')}
      Show details for one provider, including key status + path
  {c('cyan', 'swe providers add <name> [--url=URL] [--env=ENV] [--file=NAME]')}
      Register a provider in ~/.swe/config/providers.json
  {c('cyan', 'swe providers remove <name>')}
      Unregister a provider (does not delete your key file)

  Keys live in {c('bold', '~/.swe/secrets/providers/.api_keys')} (override
  with --api-keys-dir=DIR). swe stores metadata only — never the
  raw key. Run {c('cyan', 'swe providers --help')} for full key-storage and masking details."""
    ),
    "harness": (
        "Everything about the harnesses you have",
        f"""\
  {c('cyan', 'swe harness list')}                  List them  {c('dim', '(swe list is the shortcut)')}
  {c('cyan', 'swe harness edit')}                  Review every harness at once
  {c('cyan', 'swe harness star <name>')}           Toggle a favourite
  {c('cyan', 'swe harness archive <name> [why]')}  Shelve one you have ruled out
  {c('cyan', 'swe harness discover')}              Scan PATH and home dirs for AI tools (dry-run)
  {c('cyan', 'swe harness discover --apply')}      Add high-confidence matches to harness.json
  {c('cyan', 'swe harness discover --apply-all')}  Add every match, including low-confidence finds
  {c('cyan', 'swe harness discover --json')}       Machine-readable output
  {c('cyan', 'swe harness discover --all')}        Include already-registered and missing tools too
  {c('cyan', 'swe harness usage <devin|cursor> --account-dir=<dir>')}  An extra account's rate
                                                   limit, from its own credentials (add --json)
  {c('cyan', 'swe harness usage-events cursor [--account-dir=<d>] [--since=<s>]')}
                                                   Cursor's per-event feed (add --json); status
                                                   "incomplete" means part of the window hit the
                                                   page cap
  {c('cyan', 'swe harness usage-daily codex [--account-dir=<d>]')}  A Codex account's per-day
                                                   usage via `codex app-server` (add --json);
                                                   status is ok / auth-required / unsupported /
                                                   unavailable

{c('bold', 'Alias')}  {c('cyan', 'swe discover')} is the same as {c('cyan', 'swe harness discover')}"""
    ),
    "find": (
        "Show where shared assets live and what links to them",
        f"""\
  Run this when a skill, plugin, or MCP server is not showing up somewhere
  and you want to know why.

  {c('cyan', 'swe find')}                   Counts for every view below
  {c('cyan', 'swe find amd')}               AGENTS.md and every harness pointing at it
  {c('cyan', 'swe find skills')}            Skills, plugins, and every harness skill root
  {c('cyan', 'swe find plugins')}           Plugins across every plugin-capable harness, and
                             where ~/.swe/plugins drifted from claude and codex
  {c('cyan', 'swe find mcps')}              MCP servers in the hub, and which harnesses have them
  {c('cyan', 'swe find skills --full')}     List every path, not just the counts
  {c('cyan', 'swe find plugins -i')}        Browse them: arrows to move and descend, q to quit
  {c('cyan', 'swe find amd --scope=all')}   Include project and vendored files
  {c('cyan', 'swe find --harness=all')}     Include archived harnesses too

{c('bold', 'Scope')}  {c('cyan', '--scope=global|local|all')}, default global
  {c('cyan', 'global')}   loaded into every harness session (for plugins: installed
           and enabled; a cached copy with no install record is not)
  {c('cyan', 'local')}    project files only
  {c('cyan', 'all')}      both, plus vendored plugin and extension copies

  {c('dim', 'Collision: swe list also has a --scope, but it means something else there')}
  {c('dim', '(active/archived/all harness visibility, not where a file lives).')}

{c('bold', 'Harness activity')}  {c('cyan', '--harness=active|all')}, default active, every view
  {c('cyan', 'active')}   hides rows whose harness is archived in harness.json
           (starred still counts as active — it is only pinned, not shelved)
  {c('cyan', 'all')}      shows archived harnesses' rows too

  A row hidden this way is never silent about it: the listing ends with
  {c('dim', 'N archived harnesses hidden; --harness=all to show')}. A row that cannot
  be mapped to a harness in harness.json — the shared swe copy, an
  unregistered config — is never filtered, active or not: unknown is the
  one thing {c('cyan', 'swe find')} exists to keep visible.

  Which harnesses get walked, and where each one's files live, comes from
  harness.json's own {c('cyan', 'capabilities')} first — the per-harness field in
  harness.json recording what a tool supports (skills? plugins?) and where —
  and only falls back to a hardcoded guess for a harness the registry has
  never heard of.

{c('bold', 'States')}
  {c('green', 'synced')}         symlinked to the shared copy
  {c('cyan', 'unsynced')}       a real directory that could be absorbed
  {c('yellow', 'own copy')}       a real (non-symlink) file, not pointing at the shared one
  {c('yellow', 'separate')}       holds content that exists nowhere else, left alone
  {c('yellow', 'wrong target')}   symlink pointing somewhere unexpected
  {c('red', 'in the way')}     a real file where a link should be
  {c('dim', 'not installed')}  harness absent from this machine

  {c('dim', 'Same ideas swe list and swe init print under different names:')}
  {c('dim', 'linked=synced, create=missing, relink=wrong target, conflict=in the way.')}

  Read-only. {c('cyan', 'swe init')} is what changes anything."""
    ),
    "plugins": (
        "Every plugin in every marketplace, and install it per harness",
        f"""\
  {c('cyan', 'swe plugins')}                       Every marketplace claude, codex or cursor has registered
  {c('cyan', 'swe plugins catalog <marketplace>')}   One marketplace's plugins and their state per harness
  {c('cyan', 'swe plugins catalog --json')}          The whole catalog as JSON (the app's Plugins page)
  {c('cyan', 'swe plugins import <ref>')}            Copy installed files into ~/.swe/plugins
  {c('cyan', 'swe plugins info <ref>')}              One plugin's servers, env keys, skills and calls
  {c('cyan', 'swe plugins install <ref> --harness=claude|codex')}
  {c('cyan', 'swe plugins uninstall <ref> --harness=claude|codex|cursor|devin|opencode')}
  {c('cyan', 'swe plugins enable <ref> --harness=claude|codex|cursor|opencode')}
  {c('cyan', 'swe plugins disable <ref> --harness=claude|codex|cursor|opencode')}
  {c('cyan', 'swe plugins marketplace add <source> --harness=claude,codex')}

  Devin supports native removal only (including personal cloud plugins).
  OpenCode supports global file/directory on/off and removal; restart existing
  sessions. Package/project configuration and runtime directives are unchanged.

  A {c('bold', 'ref')} is {c('cyan', '<plugin>@<marketplace>')}, the name both CLIs use. The catalog
  lists installed and uninstalled plugins alike, plus anything installed
  whose marketplace has no readable manifest. Claude Code and Codex listings
  read local files. Cursor queries its account API when credentials are available.

  Import copies an installed/cached plugin into SWE without changing a harness.
  Existing SWE files are never overwritten. The app lists local plugins only;
  marketplace-only entries remain available through swe plugins catalog.

{c('bold', 'Actions')}  Each one checks first and does nothing if the plugin is
  already in that state, then runs the harness's own CLI:
  claude   claude plugin install|uninstall|enable|disable <ref>
  codex    codex plugin add|remove <ref>
  Codex has no enable verb, so enable and disable set
  [plugins."<ref>"] enabled in ~/.codex/config.toml, an edit swe checks
  parses back to the same file with only that flag changed. When it cannot
  prove that, it writes nothing and says what to edit by hand.
  Cursor uses its account API for existing personal plugins. Team installations
  stay read-only. Configured or pinned plugins offer removal only, because
  on/off preservation is not verified for those settings. A missing login,
  protected credentials or failed API read leaves cached plugins unknown.
  Credential reads never open a keychain dialog. Refocus Cursor or start a
  new Cursor CLI session after a change. Cursor source install is not supported.

  {c('dim', 'Collision: swe find also has a --harness, but it means active|all there.')}
  {c('dim', 'swe find plugins shows drift between ~/.swe/plugins and each harness.')}"""
    ),
    "hooks": (
        "Manage the hook scripts under ~/.swe/hooks",
        f"""\
  {c('cyan', 'swe hooks')}                       Every hook, its tier, link state, declaration
  {c('cyan', 'swe hooks list [--harness=<h>]')}  One harness only
  {c('cyan', 'swe hooks info <name>')}           One script: path, docstring, state, events
  {c('cyan', 'swe hooks edit <name>')}           Open the hub file in VISUAL or EDITOR (default vi)
  {c('cyan', 'swe hooks new <harness> <name>')}  Write a Python skeleton + the stanza to paste
  {c('cyan', 'swe hooks new shared <name>')}     Same, but the shared normalized contract
  {c('cyan', 'swe hooks check')}                 Declared-but-missing, linked-but-undeclared,
                                    or a hub script never linked in
  {c('cyan', 'swe hooks sync')}                  Regenerate shared wrappers, re-apply links
  {c('cyan', 'swe hooks disable <name>')}        Unlink a hook without deleting it
  {c('cyan', 'swe hooks enable <name>')}         Re-link a disabled hook
  {c('cyan', 'swe hooks delete <name>')}         Remove the hub file (--foreign: the real file;
                                    --yes skips the confirmation)
  {c('cyan', 'swe hooks stats')}                 Fires, blocks, block rate and top reasons per hook
  {c('cyan', 'swe hooks log <name>')}            The hook's most recent runs, newest first

{c('bold', 'Stats')}
  {c('cyan', 'swe hooks stats [--since=7d] [--hook=<name>] [--harness=<h>] [--repo=<path>]')}
  {c('cyan', 'swe hooks log <name> [--since=7d] [--decision=block] [--limit=20]')}
  --since takes 90m, 24h, 7d, 2w or all. Both add {c('cyan', '--no-backfill')} to skip the
  Claude transcripts, {c('cyan', '--source=<path>')} to pick a hook by the source_path
  {c('cyan', 'swe hooks list --json')} gives it instead of by name, {c('cyan', '--sum-harnesses')}
  (stats) to fold every harness into one row per hook, and {c('cyan', '--json')} for
  the machine-readable form. A run counts for the hook whose script ran; a
  name two scripts share counts for neither.
  Each hook run is one line in {c('dim', '~/.swe/cache/hooks/events.jsonl')} (5 MB, one
  rotated copy; never committed or sent anywhere), written by the shared-hook
  shim: run {c('cyan', 'swe hooks sync')} once so it carries the logger. Only the first
  line of a hook's message is kept. Claude Code history from before that is
  read from its transcripts, and a run in both counts once. SWE_HOOK_LOG=0
  stops the logging.

{c('bold', 'Tiers')}
  {c('cyan', 'shared')}    ~/.swe/hooks/shared/<name>.py — one script deployed
           into every installed shim-able harness through a generated
           wrapper (hooks/<h>/_generated/). Its # events: [PreToolUse, ...]
           header says which normalized events it wants.
  {c('dim', 'harness')}   the per-harness scripts swe links into each hooks root
  {c('yellow', 'foreign')}  a script or declared command no ~/.swe source claims —
           another installer owns it; swe lists it, never writes it

  Scripts live one folder per harness under {c('bold', '~/.swe/hooks/')}, and
  {c('cyan', 'swe init')} links each into that harness's hooks dir (~/.claude/hooks,
  ~/.factory/hooks, or {c('dim', 'capabilities.hooks.root')} in harness.json).
  The contract registry (events, schema, settings file, quirks like
  cursor's must-print-JSON and codex's trusted command hashes) lives in
  hooks/contracts.py; harness.json capabilities.hooks entries override it.

  Declaring a hook — the event, matcher and timeout in settings.json or
  hooks.json — stays with you: swe reads those files for the DECLARED
  column and {c('cyan', 'swe hooks check')}, but never writes them, and
  delete never removes a stanza.

{c('bold', 'States')}
  {c('green', 'linked')}    already points at the hub copy
  {c('cyan', 'create')}    nothing at the destination yet (swe init links it)
  {c('yellow', 'relink')}    symlink points elsewhere
  {c('red', 'conflict')}  a real file is in the way, needs swe init --force
  {c('dim', 'skipped')}   no hooks root, or harness not installed
  {c('dim', 'ignored')}   .linkignore, archived in harness.json, or disabled
  {c('yellow', 'foreign')}   not managed by swe — list-only"""),
    "sync": (
        "Commit, merge and push the ~/.swe hub repo",
        f"""\
  {c('cyan', 'swe sync')}                   Commit local changes, merge the remote in, push,
                             then re-link every harness when content came in
  {c('cyan', 'swe sync --dry-run')}         Fetch and show what would happen, change nothing
  {c('cyan', 'swe sync status')}            Branch mode, ahead/behind, uncommitted changes
  {c('cyan', 'swe sync status --fetch')}    Fetch the remote first

  {c('bold', 'Device mode')}, on a {c('bold', 'device/<machine>')} branch: merges origin/device/<machine>
  and then origin/main into it, and pushes the device branch only.
  {c('bold', 'Single-branch mode')}, on any other branch: merges origin/<branch>, pushes it.

  Every merge is tried first without touching the repo. On a conflict sync
  lists the files and stops with ~/.swe exactly as it was. It never
  force-pushes, and never commits secrets/, cache/, data/ (this machine's
  ledgers, catalog, search index, remote state), Codex's skills/.system/
  or anything else swe's .gitignore lists, even when the hub's own
  .gitignore is older.

  Commit messages name this machine by its hostname; set
  {c('cyan', 'SWE_MACHINE_NAME')}=<name> to use another name.

  Both take {c('cyan', '--json')} for the app's document."""
    ),
    "instructions": (
        "Compile shared instruction chapters into ~/.swe/AGENTS.md",
        f"""\
  {c('cyan', 'swe instructions init')}     Fold the current AGENTS.md into a first chapter
  {c('cyan', 'swe instructions build')}    Build the complete shared instruction file
  {c('cyan', 'swe instructions check')}    Exit 1 if the generated file is missing or stale
  {c('cyan', 'swe instructions watch')}    Build now, then rebuild on source changes

  Edit chapters under {c('bold', '~/.swe/AGENTS_MD/')} and list their relative .md
  paths in {c('bold', 'index.txt')}, in order. Blank lines and # comments are ignored;
  a commented-out chapter line keeps its file but stays out of the build.
  {c('bold', 'targets.json')} next to it can pin a chapter to harnesses
  ({"only"} or {"except"}); a named harness whose chapters differ gets its own
  {c('bold', 'AGENTS.<name>.md')} and swe's links are repointed to it.
  Build writes {c('bold', '~/.swe/AGENTS.md')} atomically, only when its SHA256 changes.
  Check writes nothing. Watch reports source errors and keeps waiting; Ctrl-C stops it.
  Edit the chapters, not the generated file. A running chat may need a new chat
  to read the update.

  build, check and init take {c('cyan', '--json')} for the app's document."""
    ),
    "init": (
        "Create ~/.swe and symlink every harness to it",
        f"""\
  {c('cyan', 'swe init')}                    Create the layout and link all harnesses
  {c('cyan', 'swe init --full')}             List every path, not just the counts
  {c('cyan', 'swe init --check')}            Show what would change, write nothing (alias: -n)
  {c('cyan', 'swe init --force')}            Replace real files too (backed up first)
  {c('cyan', 'swe init --migrate')}          Move a pre-0.2.7 ~/.config/swe into ~/.swe
  {c('cyan', 'swe init --yes')}              Register every new harness without asking
                               (also the non-terminal default)
  {c('cyan', 'swe init --hub=<git url>')}     Clone your hub repo as ~/.swe first, then link
                               (only while ~/.swe has no hub: no AGENTS.md, no .git)
  {c('cyan', 'swe init --accounts')}         Only sync the extra account homes the
                               accounts.sync.* switches name (takes --check, --json)

  {c('dim', 'Collision: -n means dry-run here and on swe install, but fetch-fresh-data on swe list.')}

  A {c('dim', 'symlink')} is a shortcut file: each harness's folder points at the one
  real copy in ~/.swe, so editing once updates it everywhere.

{c('bold', 'New harnesses')}
  Found via their skills folder. Ticked ones are registered and linked,
  instructions included; unticked ones are archived in harness.json and
  left alone. {c('cyan', 'swe hs')} archives or stars them later.

{c('bold', 'What it owns')}
  ~/.swe/AGENTS.md   one instruction file, linked in under each harness's
                        own name (CLAUDE.md, QWEN.md, CRUSH.md, GEMINI.md)
  ~/.swe/skills/     one skill tree, linked in as every harness's skills/
  ~/.swe/hooks/      hook scripts, one folder per harness (hooks/claude/),
                        each script linked into that harness's hooks dir;
                        declaring the hook in its settings stays with you
  ~/.swe/.linkignore paths to leave alone, one gitignore-style pattern per
                        line, e.g. .agents/skills or .config/*/AGENTS.md
  ~/.swe/backups/    anything replaced, timestamped

{c('bold', 'States')}
  {c('green', 'linked')}    already points at the canonical file
  {c('cyan', 'create')}    nothing there yet, will symlink
  {c('yellow', 'relink')}    symlink pointing elsewhere, will repoint
  {c('red', 'conflict')}  a real file or directory, needs --force
  {c('dim', 'skipped')}   harness not installed on this machine
  {c('dim', 'ignored')}   listed in .linkignore or archived in harness.json,
               never touched or counted"""
    ),
    "setup": (
        "Sectioned setup wizard for SWE",
        f"""\
  {c('cyan', 'swe setup')}                    Run all six interactive setup stages
  {c('cyan', 'swe setup --quick')}            Only visit missing or actionable stages
  {c('cyan', 'swe setup harnesses')}          Discover and register coding CLIs
  {c('cyan', 'swe setup providers')}          Review provider credential coverage
  {c('cyan', 'swe setup mcp')}                Import MCP servers into mcp.json
  {c('cyan', 'swe setup skills')}             Unify safe roots under ~/.swe/skills
  {c('cyan', 'swe setup report')}             Configure summarizer and writer models
  {c('cyan', 'swe setup check')}              Verify the resulting setup

  Returning-user prompts show current values; Enter keeps them. SWE creates a
  timestamped backup before changing harness.json, mcp.json, or config.json.

  {c('cyan', 'swe setup --apply')}            Apply safe discovery changes without prompts
  {c('cyan', 'swe setup --json')}             Print the discovery preview as JSON
  {c('cyan', 'swe setup --non-interactive')}  Preview without prompts or writes
  {c('cyan', 'swe setup --hub=<git url>')}    Clone your hub repo as ~/.swe first, install
                               its plugins and sync its MCP servers into each
                               harness, then run any of the above (only while
                               ~/.swe has no hub yet)

  While ~/.swe has no hub yet (no AGENTS.md, no .git), the wizard first asks
  for your hub repo's URL; a blank answer starts an empty hub.

  Provider credentials remain external. Use {c('cyan', 'swe providers info <name>')} for the
  expected key file and environment variable; harness OAuth remains harness-owned."""
    ),
    "discover": (
        "Scan PATH and home dirs for unregistered AI tools",
        f"""\
  {c('cyan', 'swe discover [--apply]')}   Alias for {c('cyan', 'swe harness discover')}

  See {c('cyan', 'swe help harness')} for flags (--apply, --apply-all, --json, --all)."""
    ),
    "autocomplete": (
        "Generate and inject shell completion script",
        f"""\
  {c('cyan', 'swe autocomplete zsh')}    Generate + inject zsh completion
  {c('cyan', 'swe autocomplete bash')}   Generate + inject bash completion
  {c('cyan', 'swe autocomplete fish')}   Generate + inject fish completion

  Writes a completion script to {c('dim', '~/.swe/completions/')} and adds a
  source line to your shell profile (~/.zshrc, ~/.bashrc, or fish config).

  After running, restart your terminal or run:
    {c('cyan', 'source ~/.zshrc')}  (or the equivalent for your shell)

  The completion script calls {c('cyan', 'swe __complete')} under the hood to
  provide dynamic completions for tool names, aliases, tags, and flags."""
    ),
    "gh": (
        "GitHub surfaces: PRs, issues, actions, inbox, pinned repos",
        f"""\
  {c('cyan', 'swe gh')}                          gh status + pinned repos
  {c('cyan', 'swe gh sync')}                     Snapshot your repos + orgs into the
                                            local catalog (resync-only updates)
  {c('cyan', 'swe gh repo list|add|remove|reorder|search')}   The repos the dashboard watches
                                            (search reads the catalog, then live;
                                            --all is public)
  {c('cyan', 'swe gh board [--repo=R]')}            Every surface of a repo in one call
  {c('cyan', 'swe gh pr [--repo=R] [--state=S]')}     Pull requests (default: open)
  {c('cyan', 'swe gh pr status')}                Current branch, authored, needs-review
  {c('cyan', 'swe gh pr view <N>')}              One PR: checks, reviews, merge state
  {c('cyan', 'swe gh pr merge <N> [--method=M] [--auto]')}
                                            Merge a PR (default: squash)
  {c('cyan', 'swe gh pr worktree <N>')}           Checkout the PR head under
                                            ~/.swe/worktrees (a session's
                                            launch point)
  {c('cyan', 'swe gh issue [--repo=R]')}          Issues
  {c('cyan', 'swe gh issue view <N>')}           One issue: body, comments, and the
                                            prompt an agent resolving it gets
  {c('cyan', 'swe gh issue comment <N> --body=TEXT')}
                                            Comment on an issue as you
  {c('cyan', 'swe gh issue worktree <N>')}       Branch swe/issue-N off the default
                                            branch, checked out under
                                            ~/.swe/worktrees
  {c('cyan', 'swe gh run [--repo=R]')}            Workflow runs (alias: actions)
  {c('cyan', 'swe gh run rerun <id> [--failed]')}  Re-run a workflow run
  {c('cyan', 'swe gh pages [--repo=R]')}          GitHub Pages site + latest build
  {c('cyan', 'swe gh releases|tags|branches')}    Auxiliary repo surfaces
  {c('cyan', 'swe gh inbox')}                    Your GitHub notifications
  {c('cyan', 'swe gh inbox read <id>...|--all')}   Mark notifications read

  Repo resolution: {c('cyan', '--repo=owner/name')} wins, else the working
  directory's git remote, else every pinned repo. So a bare
  {c('cyan', 'swe gh pr')} inside a checkout shows that repo; outside one it
  shows all pinned repos in turn.

  {c('bold', 'Flags')}
  {c('cyan', '--repo=owner/name')}   Repo (a github.com URL works too;
                          comma-separate several: --repo=a/b,c/d)
  {c('cyan', '--state=open|closed|…')}  pr: open/closed/merged/all; issue: open/closed/all
  {c('cyan', '--limit=N')}            Rows to fetch (default 30, cap 200)
  {c('cyan', '--all')}                inbox: include read notifications
  {c('cyan', '--web')}                Open the matching github.com page instead
  {c('cyan', '--body=TEXT')}          issue comment: the comment, as one argument
                          (it reaches gh on stdin, so any text is safe)
  {c('cyan', '--body-file=PATH|-')}   issue comment: read the comment from a file,
                          or stdin with -, so it never shows in ps
  {c('cyan', '--json')}               {{schema, data}} envelope; data is
                          {{github: {{status, login}}, results: [...]}} with one
                          entry per repo: ok → rows, else a typed status
                          (not-found, upstream-error) plus the error text

  Auth is ambient: swe gh uses your {c('cyan', 'gh auth login')} and never
  stores tokens. The few writes (merge, rerun, inbox read, issue comment)
  are explicit commands, never side effects of a read."""
    ),
    "worktree": (
        "Find, add, remove, prune and slim git worktrees",
        f"""\
  {c('cyan', 'swe wt list [--all] [--repo=PATH] [--no-size]')}
                             Every worktree with its repo, branch, state, PR,
                             last commit and size (du estimates). Default
                             scope is the repo you are in; --all adds every
                             repo swe knows (worktree.roots in config, swe gh
                             mirrors, harness worktree folders).
  {c('cyan', 'swe wt show <path>')}         One worktree in full.
  {c('cyan', 'swe wt add <repo> --branch=B [--new] [--base=REF] [--path=P]')}
  {c('cyan', 'swe wt add <repo> --pr=N [--path=P]')}
                             A worktree for a branch (--new creates it) or a
                             PR's head, by default in ../<repo>-worktrees/.
  {c('cyan', 'swe wt remove <path> [--force] [--delete-branch]')}
                             git worktree remove. Anything but merged-clean
                             needs --force; main and protected never go.
  {c('cyan', 'swe wt lock <path> [--reason=R]')} / {c('cyan', 'unlock <path>')}
  {c('cyan', 'swe wt move <path> <new-path>')}
  {c('cyan', 'swe wt prune')}               Dry run: what would go and how much it frees.
  {c('cyan', 'swe wt prune --yes')}         Remove merged-clean worktrees (git worktree
                             remove --force, then git worktree prune).
      {c('cyan', '--include=merged-dirty')}   Also offer merged worktrees with changes,
                             one yes per worktree.
      {c('cyan', '--branch=<name>')}          Only that branch's worktree: for post-merge
                             hooks and the PR loop's cleanup step.
      {c('cyan', '--auto')}                   Act (like --yes) only in repos whose config
                             sets worktree.repos.<repo>.auto_prune to true.
      {c('cyan', '--delete-branches')}        Also delete local branches that are merged,
                             or whose tip is a merged PR's head. Never remote.
  {c('cyan', 'swe wt slim [<path> | --all] [--days=N] [--yes]')}
                             Delete git-ignored rebuildable folders (.venv,
                             node_modules, .next, build, dist, target,
                             DerivedData, .build). For a scope: unmerged
                             worktrees, folders untouched for N days (14).

  States: {c('green', 'merged-clean')}, {c('yellow', 'merged-dirty')}, {c('cyan', 'open-pr')}, no-pr, {c('red', 'orphaned')}, unknown,
  protected, main. Merged comes from the PR on GitHub (squash merges defeat
  git ancestry); a detached HEAD counts as merged when it is on origin/main.
  When gh cannot answer the state is unknown, and unknown is never removed.
  The main checkout, ~/.swe/cache/install/src and worktree.protect paths are
  never touched; prune also skips locked worktrees and the one you are in.
  Applied prune and slim runs log to ~/.swe/logs/worktree/. Every command
  takes --json; a failure prints {{"error": {{"code", "message"}}}}."""
    ),
    "remote": (
        "Watch and steer sessions from a phone",
        f"""\
  {c('cyan', 'swe remote serve [--host=H] [--port=N]')}   Run the hub: session catalog,
                                            transcript tailers, REST + SSE for
                                            the phone app. Binds this machine's
                                            Tailscale IP by default.
  {c('cyan', 'swe remote serve --public-url=URL')}      Run behind tailscale serve: binds
                                            127.0.0.1, trusts serve's client
                                            headers, and hands phones URL.
  {c('cyan', 'swe remote serve --install')}             Keep the hub running: a LaunchAgent
                                            on macOS, a systemd user unit on
                                            Linux. Starts at login or boot,
                                            restarts if it exits. --uninstall
                                            stops and removes it.
  {c('cyan', 'swe remote setup [--apply]')}             Check every step to a paired phone:
                                            Tailscale, MagicDNS, HTTPS, the
                                            certificate, the hub, tailscale
                                            serve. --apply does SWE's part.
  {c('cyan', 'swe remote pair [--name=N]')}             Mint a one-time pairing token;
                                            prints the URL to open on the phone
                                            (and a QR when qrencode is installed).
  {c('cyan', 'swe remote status [--host=H]')}           Probe the hub, list machines
                                            and paired devices.
  {c('cyan', 'swe remote devices [--revoke=<id>]')}     List or revoke paired devices.
                                            {c('cyan', '--requests')} lists pending
                                            pair requests; {c('cyan', '--approve=<code>')}
                                            and {c('cyan', '--deny=<code>')} decide one;
                                            {c('cyan', '--request-id=<id>')} holds the
                                            code to that one request.
                                            {c('cyan', '--auto-approve=on|off')} lets
                                            your own untagged tailnet
                                            devices pair without a code
                                            (off by default).
  {c('cyan', 'swe remote attach <session>')}            Take an observed session
                                            under supervision. Default
                                            {c('cyan', '--mode=auto')} picks the
                                            harness's API channel (opencode
                                            serve, codex app-server) when the
                                            binary exists, then its Agent
                                            Client Protocol mode (devin acp,
                                            cursor-agent acp, ...) when the
                                            CLI has one, else relaunches in
                                            tmux with resume flags.
                                            {c('cyan', '--mode=acp')} forces ACP
                                            (claude via claude-agent-acp).
                                            {c('cyan', '--mode=headless')} sends
                                            each prompt via claude -p instead.
                                            {c('cyan', '--model=<m>')} and
                                            {c('cyan', '--effort=<e>')} pick the
                                            launch tuning the channel can
                                            honour (tmux/headless/serve/
                                            appserver take model).
  {c('cyan', 'swe remote agent --hub=URL [--token=T]')}   Satellite: pushes this
                                            machine's catalog + transcript
                                            events to the hub and runs the
                                            commands it queues. Pair once with
                                            a token, then keep it running —
                                            outbound only, nothing listens.
  {c('cyan', 'swe remote agent --install')}             Keep the satellite running: its
                                            own LaunchAgent or systemd user
                                            unit, apart from the hub's.
                                            Refused on a machine whose hub is
                                            installed. --uninstall removes it;
                                            --status [--json] shows it.

  The phone only ever talks to the hub, over Tailscale. Sessions list as
  {c('bold', 'observed')} (a human terminal owns them: read-only) until they run
  under swe's supervisor, which marks them {c('bold', 'attached')} and writable.

  {c('cyan', 'swe remote notify --ntfy=<url> --topic=<t>')}  Push notifications via a
                                            self-hosted ntfy: needs-you, finished,
                                            error. Approval prompts get lock-screen
                                            Allow/Deny buttons posting back over
                                            the tailnet. {c('cyan', '--off')} disables.
                                            {c('cyan', '--service-status=on')} also pushes when
                                            a model provider or agent platform
                                            starts or ends an incident.

  {c('cyan', 'swe remote ssh [--on|--off]')}            The phone's SSH console, for when
                                            the hub itself is down. It dials
                                            this machine's sshd over the
                                            tailnet, not the hub. {c('cyan', '--on')}
                                            writes each paired phone's key into
                                            ~/.ssh/authorized_keys (one tagged
                                            line per device; revoking a device
                                            removes it), {c('cyan', '--off')} removes them.

  State lives under {c('dim', '~/.swe/data/remote/')}: device credentials in
  devices.json (0600), per-session event journals and transcript cursors."""
    ),
    "version": (
        "Print the swe version",
        f"""\
  {c('cyan', 'swe version')}    {c('cyan', 'swe -v')}    {c('cyan', 'swe --version')}

  Prints the installed version and exits. Reads no config, so it works even
  when ~/.swe is broken. {c('cyan', '--json')} emits the standard envelope:
  name, version and the Python version under `data`."""
    ),
    "bug": (
        "Report a bug on GitHub, versions filled in",
        f"""\
  {c('cyan', 'swe bug')}              Open the bug form in your browser with the
                       swe, Python and OS versions filled in
  {c('cyan', 'swe bug --feature')}    Open the feature-request form instead
  {c('cyan', 'swe bug --print')}      Print the link instead of opening it

  Nothing is sent until you submit the form on GitHub. Paste
  {c('cyan', 'swe doctor')} output into it when doctor reports a problem.
  {c('cyan', '--json')} prints kind, url, version and os under `data` and opens
  nothing. The Mac app's Help › Report a Bug opens the same form."""
    ),
}

COMMAND_CATEGORIES = [
    ("Setup", [
        ("init",    None),
        ("instructions", None),
        ("setup",   None),
        ("config",  None),
        ("doctor",  None),
        ("install", None),
    ]),
    ("Registry", [
        ("list",     "ls"),
        ("info",     None),
        ("add",      None),
        ("edit",     None),
        ("remove",   "rm"),
        ("check",    None),
        ("harness",  "hs"),
        ("discover", None),
    ]),
    ("Launch", [
        ("use",     "run"),
    ]),
    ("Analytics", [
        ("session", None),
        ("report",  None),
        ("models",  None),
    ]),
    ("Remote", [
        ("remote",  None),
        ("sync",    None),
    ]),
    ("Reference", [
        ("find",    None),
        ("plugins", None),
        ("hooks",   None),
        ("skills",  "sk"),
        ("tags",    None),
        ("aliases", None),
        ("providers", "pv"),
    ]),
    ("MCP", [
        ("mcp",     None),
    ]),
    ("GitHub", [
        ("gh",      None),
        ("worktree", "wt"),
    ]),
    ("Shell", [
        ("autocomplete", None),
        ("version",      None),
        ("bug",          None),
    ]),
]


def cmd_help(args):
    # ── per-command help ──────────────────────────────────────────────────────
    if args:
        cmd_name = args[0]
        if cmd_name in HELP:
            summary, detail = HELP[cmd_name]
            print(f"\n  {c('bold', 'swe ' + cmd_name)} — {summary}\n")
            print(detail)
            print()
            return
        # check aliases
        for cat, cmds in COMMAND_CATEGORIES:
            for primary, alias in cmds:
                if alias == cmd_name:
                    summary, detail = HELP[primary]
                    print(f"\n  {c('bold', 'swe ' + primary)} ({c('dim', alias)}) — {summary}\n")
                    print(detail)
                    print()
                    return
        print(c("red", f"  Unknown command: '{cmd_name}'"))
        return 1

    # ── full help ─────────────────────────────────────────────────────────────
    print(f"\n{c('bold', 'swe')} — Central manager for AI coding CLI tools\n")
    print("  One home for every AI coding CLI you use. Each one invents its own config")
    print("  files; swe gives them one shared instruction file, one skills folder,")
    print(f"  one MCP list, one registry. swe calls each AI coding tool (Claude Code,")
    print(f"  Codex, Cursor…) a {c('bold', 'harness')}.\n")
    print(f"  {c('dim', 'USAGE')}  swe <command> [arguments]\n")

    for cat_name, cmds in COMMAND_CATEGORIES:
        print(f"  {c('bold', cat_name)}")
        for primary, alias in cmds:
            summary = HELP[primary][0]
            if alias:
                print(f"    {c('cyan', primary):<22} {c('dim', '(' + alias + ')'):<14} {summary}")
            else:
                print(f"    {c('cyan', primary):<22} {'':14} {summary}")
        print()

    print(f"  {c('dim', 'FLAGS')}")
    print(f"    {c('cyan', 'swe help')}              Full help")
    print(f"    {c('cyan', 'swe <cmd> --help')}      Detailed help for a command")
    print(f"    {c('cyan', 'swe skills help <topic>')} Per-topic skills help\n")

    print(f"  {c('dim', 'ALIASES')}   cc=claude  gg=gemini  cx=codex  cp=copilot  oc=opencode")
    print(f"  {'':>14}fc=forge  df=droid  cs=cursor  cl=cline  ki=kilo  dv=devin\n")

    n_inst = 0
    n_total = 0
    try:
        tools = load_registry_readonly()
        n_total = len(tools)
        n_inst = sum(1 for i in tools.values()
                     if i.get("command") and is_installed(i["command"]))
    except Exception:
        pass
    print(f"  {c('dim', 'REGISTRY')}  {HARNESS_FILE}")
    print(f"  {c('dim', 'TOOLS')}     {n_inst}/{n_total} installed\n")
