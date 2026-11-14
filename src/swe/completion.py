"""Context-aware completion engine for `swe __complete`."""

from __future__ import annotations

from swe.harness.registry import load_registry

# Primary subcommands shown in completion (excludes aliases and hidden commands).
# (name, description) — descriptions are short for shell display.
_PRIMARY_COMMANDS: list[tuple[str, str]] = [
    ("list", "List all tools"),
    ("info", "Show tool details"),
    ("add", "Register a new tool"),
    ("edit", "Edit tool fields"),
    ("remove", "Remove a tool"),
    ("use", "Launch a tool"),
    ("check", "Verify installs + versions"),
    ("doctor", "Diagnose Node/PATH issues"),
    ("install", "Show or run a harness's install command"),
    ("session", "Show recent sessions"),
    ("models", "Show model usage"),
    ("skills", "List agent skills"),
    ("tags", "Show all tags"),
    ("aliases", "Show aliases"),
    ("mcp", "Manage MCP servers"),
    ("gh", "GitHub: pinned repos, PRs, issues, CI, inbox"),
    ("harness", "Harness registry utils"),
    ("hooks", "Manage per-harness hook scripts"),
    ("find", "Locate shared assets across harnesses"),
    ("plugins", "Browse every marketplace, install per harness"),
    ("providers", "Manage API keys"),
    ("config", "View or update configuration"),
    ("remote", "Watch and steer sessions from a phone"),
    ("sync", "Commit, merge and push the ~/.swe hub repo"),
    ("worktree", "Find, add, remove, prune and slim git worktrees"),
    ("report", "Summarize coding sessions"),
    ("discover", "Scan PATH for AI coding CLIs"),
    ("setup", "Onboarding wizard"),
    ("init", "Create ~/.swe and link every harness"),
    ("instructions", "Compile shared instruction chapters"),
    ("autocomplete", "Generate shell completion"),
    ("version", "Print the swe version"),
    ("bug", "Report a bug on GitHub, versions filled in"),
]

# Commands that take a tool name/alias as their first positional argument.
_TOOL_TARGET_COMMANDS = frozenset({
    "use", "run",
    "info", "edit", "remove", "rm", "install",
})

# Flags for specific commands. Keys are either a bare command ("list") or a
# "<domain> <sub>" pair referenced by _NESTED_FLAGS below.
_COMMAND_FLAGS: dict[str, list[tuple[str, str]]] = {
    "version": [
        ("--json", "Print the version as JSON"),
    ],
    "bug": [
        ("--feature", "Open a feature request instead"),
        ("--print", "Print the link instead of opening it"),
        ("--json", "Print the link and versions as JSON"),
    ],
    "list": [
        ("--scope=active", "Hide archived harnesses (default)"),
        ("--scope=archived", "Show only archived harnesses"),
        ("--scope=all", "Show everything, archived marked"),
        ("--usage", "Show rate-limit column"),
        ("-u", "Short for --usage"),
        ("--exclude=", "Skip these harnesses when polling usage (comma-separated)"),
        ("--links", "Show skills/instruction link state"),
        ("-L", "Short for --links"),
        ("--refresh", "Fetch new data"),
        ("-r", "Short for --refresh"),
        ("-n", "Fetch new data"),
        ("--refresh-failed", "Refetch only providers whose cached reading failed"),
        ("--cache-ttl=", "Override the rate-limit cache TTL (seconds)"),
    ],
    "list edit": [
        ("--reset", "Restore default columns and window"),
    ],
    "sync": [
        ("--dry-run", "Show what would be committed, merged and pushed"),
        ("--json", "Print the result as JSON"),
    ],
    "sync status": [
        ("--fetch", "Fetch the remote before counting"),
        ("--json", "Print the status as JSON"),
    ],
    "worktree list": [
        ("--all", "Every repo swe knows, not just this one"),
        ("--repo=", "A repo, or a folder of repos, to scan"),
        ("--no-size", "Skip du"),
        ("--json", "Print the list as JSON"),
    ],
    "worktree show": [
        ("--json", "Print the worktree as JSON"),
    ],
    "worktree add": [
        ("--branch=", "Check out this branch"),
        ("--new", "Create the branch"),
        ("--base=", "Start the new branch here (default origin/main)"),
        ("--pr=", "Check out this PR's head"),
        ("--path=", "Where to put it (default ../<repo>-worktrees/)"),
        ("--json", "Print the worktree as JSON"),
    ],
    "worktree remove": [
        ("--force", "Remove even if it is not merged-clean"),
        ("--delete-branch", "Also delete its local branch"),
        ("--json", "Print the result as JSON"),
    ],
    "worktree lock": [
        ("--reason=", "Why it is locked"),
        ("--json", "Print the worktree as JSON"),
    ],
    "worktree unlock": [
        ("--json", "Print the worktree as JSON"),
    ],
    "worktree move": [
        ("--json", "Print the worktree as JSON"),
    ],
    "worktree prune": [
        ("--yes", "Remove them (the default is a dry run)"),
        ("--all", "Every repo swe knows, not just this one"),
        ("--repo=", "A repo, or a folder of repos, to scan"),
        ("--include=", "merged-dirty: also offer merged worktrees with changes"),
        ("--branch=", "Only this branch's worktree"),
        ("--auto", "Act only where worktree.repos.<repo>.auto_prune is on"),
        ("--delete-branches", "Also delete merged local branches"),
        ("--json", "Print the plan as JSON"),
    ],
    "worktree slim": [
        ("--yes", "Delete them (the default is a dry run)"),
        ("--all", "Every repo swe knows, not just this one"),
        ("--repo=", "A repo, or a folder of repos, to scan"),
        ("--days=", "Only folders untouched this long (default 14)"),
        ("--json", "Print the plan as JSON"),
    ],
    "session index": [
        ("--on", "Let the remote hub keep the index current"),
        ("--off", "Stop the hub keeping the index current"),
        ("--status", "Show what the index holds, without a pass"),
        ("--json", "Print the index status as JSON"),
    ],
    "session mirror": [
        ("--on", "Keep mirroring transcripts (the default)"),
        ("--off", "Stop mirroring transcripts"),
        ("--status", "Show what the mirror holds, without a pass"),
        ("--json", "Print the mirror status as JSON"),
    ],
    "remote serve": [
        ("--host=", "Bind address (default: this machine's Tailscale IP)"),
        ("--port=", "Port (default 8477)"),
        ("--public-url=", "URL phones use when tailscale serve fronts the hub"),
        ("--install", "Keep the hub running as a LaunchAgent (macOS)"),
        ("--uninstall", "Stop the hub LaunchAgent and remove it"),
        ("--tailnet", "Exit instead of binding localhost when Tailscale is down"),
    ],
    "remote pair": [
        ("--host=", "Hub address to put in the pairing URL"),
        ("--port=", "Hub port for the pairing URL"),
        ("--name=", "Device name recorded on pair"),
        ("--app", "QR encodes the quiver:// deep link for the iOS app"),
        ("--json", "Print the pair payload as JSON"),
    ],
    "remote status": [
        ("--host=", "Hub address to probe"),
        ("--port=", "Hub port to probe"),
        ("--json", "Print the result as JSON"),
    ],
    "remote devices": [
        ("--revoke=", "Revoke one device id"),
        ("--requests", "List pending pair requests"),
        ("--approve=", "Approve a pair request by its 6-digit code"),
        ("--deny=", "Deny a pair request by its 6-digit code"),
        ("--request-id=", "Hold --approve/--deny to one pending request"),
        ("--auto-approve=", "on|off: pair your own untagged devices without a code"),
        ("--json", "Print the device list as JSON"),
    ],
    "remote attach": [
        ("--mode=", "Channel: auto|tmux|headless|serve|appserver|acp"),
    ],
    "remote agent": [
        ("--hub=", "Hub URL (http://<tailscale-ip>:8477)"),
        ("--token=", "One-time pair token from swe remote pair"),
        ("--name=", "Device name recorded on pair"),
        ("--install", "Keep the agent running (LaunchAgent or systemd unit)"),
        ("--uninstall", "Stop the agent service and remove it"),
        ("--status", "Show whether the agent service is installed and running"),
        ("--json", "Print --status as JSON"),
    ],
    "remote setup": [
        ("--apply", "Do every step SWE can do itself"),
        ("--recheck", "Fetch the certificate again instead of the cached result"),
        ("--port=", "Hub port (default 8477)"),
        ("--json", "Print the steps as JSON"),
    ],
    "remote notify": [
        ("--ntfy=", "ntfy base URL (e.g. https://ntfy.example.com)"),
        ("--topic=", "ntfy topic to publish to"),
        ("--ntfy-token=", "ntfy access token, if the topic needs one"),
        ("--on", "Enable push notifications"),
        ("--off", "Disable push notifications"),
        ("--service-status=", "on|off: push when a provider starts or ends an incident"),
    ],
    "remote ssh": [
        ("--on", "Let paired phones' SSH keys log in here"),
        ("--off", "Remove every phone key from authorized_keys"),
        ("--json", "Print the SSH setup as JSON"),
    ],
    "session": [
        ("--search=", "Filter sessions"), ("-q", "Short for --search"),
        ("--grep=", "Alias for --search"),
        ("--days=", "Past N calendar days"), ("-d", "Short for --days"),
        ("--weeks=", "Past N calendar weeks"), ("-w", "Short for --weeks"),
        ("--start=", "Range start date"), ("-s", "Short for --start"),
        ("--end=", "Range end date"), ("-e", "Short for --end"),
        ("--agent=", "Filter by agent"), ("--here", "Current project only"),
        ("--interactive", "Pick a session with arrow keys"),
        ("-i", "Short for --interactive"),
        ("--json", "Print the listing as JSON"),
    ],
    "models": [
        ("--by-tool", "Group by harness"), ("-t", "Short for --by-tool"),
        ("--providers", "Group by provider"), ("-p", "Short for --providers"),
    ],
    "skills": [
        ("--desc", "Show skill descriptions"), ("-d", "Short for --desc"),
        ("--json", "Every skill and which harnesses see it, as JSON"),
    ],
    "skills hub": [
        ("--json", "Print the hub's plugins as JSON"),
    ],
    "skills link": [
        ("--force", "Back up and replace a non-empty skills dir"),
        ("--json", "Print the result as JSON"),
    ],
    "skills unlink": [
        ("--mkdir", "Leave an empty directory behind"),
        ("--json", "Print the result as JSON"),
    ],
    "skills new": [
        ("--description=", "One-line description"),
        ("--json", "Print the result as JSON"),
    ],
    "skills move": [
        ("--from=", "Source scope"), ("--to=", "Destination scope"),
        ("--force", "Back up and replace an existing destination"),
        ("--json", "Print the result as JSON"),
    ],
    "skills enable": [
        ("--harness=", "The skills folder to link the skill into"),
        ("--json", "Print the result as JSON"),
    ],
    "skills disable": [
        ("--harness=", "The skills folder to drop the link from"),
        ("--json", "Print the result as JSON"),
    ],
    "skills sync": [
        ("--json", "Print what changed as JSON"),
    ],
    "skills adopt": [
        ("--all", "Adopt every harness-only skill"),
        ("--dry-run", "Show what would move without writing"),
        ("--json", "Print the result as JSON"),
    ],
    "skills remove": [
        ("--json", "Print the result as JSON"),
    ],
    "skills usage": [
        ("--json", "Print the call stats as JSON"),
    ],
    "skills discover": [
        ("--apply", "Write catalog entries"),
        ("--json", "Machine-readable output"),
        ("--all", "Include already-cataloged skills"),
    ],
    "find": [
        ("--root", "Search ~/ instead of cwd"), ("-r", "Short for --root"),
        ("--scope=global", "Global roots only"),
        ("--scope=local", "Project-local roots only"),
        ("--scope=all", "Global and local roots"),
        ("--harness=active", "Only active harnesses (default)"),
        ("--harness=all", "Include archived harnesses"),
        ("--full", "Show every match, not just counts"),
        ("--interactive", "Browse matches with arrow keys"),
        ("-i", "Short for --interactive"),
        ("--json", "Skills or plugins: print as JSON"),
        ("--fix=", "Plugins only: run one drift finding's fix by id"),
    ],
    "hooks": [
        ("--harness=", "Only one harness's scripts"),
        ("--json", "Every hook script, state and declaration as JSON"),
    ],
    "hooks list": [
        ("--harness=", "Only one harness's scripts"),
        ("--json", "Every hook script, state and declaration as JSON"),
    ],
    "hooks info": [
        ("--harness=", "Only one harness's copy"),
        ("--json", "The script's state and declarations as JSON"),
    ],
    "hooks edit": [
        ("--harness=", "Which harness's copy to open"),
    ],
    "hooks new": [
        ("--json", "Path, command and settings stanza as JSON"),
    ],
    "hooks check": [
        ("--json", "The drift findings as JSON"),
    ],
    "hooks delete": [
        ("--harness=", "Only one harness's copy"),
        ("--foreign", "Delete the real file of a foreign hook"),
        ("--yes", "Skip the confirmation prompt"),
        ("--json", "What was deleted as JSON"),
    ],
    "hooks disable": [
        ("--harness=", "Only one harness's copy"),
        ("--json", "The new state as JSON"),
    ],
    "hooks enable": [
        ("--harness=", "Only one harness's copy"),
        ("--json", "The new state as JSON"),
    ],
    "hooks sync": [
        ("--force", "Replace conflicting real files"),
        ("--json", "Generated, pruned and link results as JSON"),
    ],
    "hooks stats": [
        ("--since=", "Window to count: 90m, 24h, 7d, 2w or all (default 7d)"),
        ("--hook=", "Only one hook"),
        ("--harness=", "Only one harness"),
        ("--repo=", "Only runs in one repository"),
        ("--no-backfill", "Skip the Claude Code transcripts"),
        ("--sum-harnesses", "One row per hook, every harness folded in"),
        ("--json", "Per-hook counts, rates and reasons as JSON"),
    ],
    "hooks log": [
        ("--since=", "Window to read: 90m, 24h, 7d, 2w or all (default 7d)"),
        ("--harness=", "Only one harness"),
        ("--repo=", "Only runs in one repository"),
        ("--decision=", "Only block, warn, allow or error runs"),
        ("--limit=", "How many runs to show (default 20)"),
        ("--no-backfill", "Skip the Claude Code transcripts"),
        ("--json", "The recent runs as JSON"),
    ],
    "plugins catalog": [
        ("--json", "Every marketplace and plugin as JSON"),
    ],
    "plugins import": [("--json", "Import installed plugin files into SWE as JSON")],
    "plugins info": [
        ("--json", "Print the detail sheet as JSON"),
    ],
    "plugins install": [
        ("--harness=claude", "Act in Claude Code"),
        ("--harness=codex", "Act in Codex"),
        ("--json", "Print the result as JSON"),
    ],
    "plugins enable": [
        ("--harness=claude", "Act in Claude Code"),
        ("--harness=codex", "Act in Codex"),
        ("--harness=cursor", "Act on existing Cursor personal plugins"),
        ("--harness=opencode", "Act on global file plugins"),
        ("--json", "Print the result as JSON"),
    ],
    "plugins disable": [
        ("--harness=claude", "Act in Claude Code"),
        ("--harness=codex", "Act in Codex"),
        ("--harness=cursor", "Act on existing Cursor personal plugins"),
        ("--harness=opencode", "Act on global file plugins"),
        ("--json", "Print the result as JSON"),
    ],
    "plugins uninstall": [
        ("--harness=claude", "Act in Claude Code"),
        ("--harness=codex", "Act in Codex"),
        ("--harness=cursor", "Act on existing Cursor personal plugins"),
        ("--harness=opencode", "Act on global file plugins"),
        ("--harness=devin", "Remove through Devin"),
        ("--json", "Print the result as JSON"),
    ],
    "plugins marketplace add": [
        ("--harness=", "Which harnesses register it: claude,codex"),
        ("--json", "Print the result as JSON"),
    ],
    "find mcp": [
        ("--root", "Search ~/ instead of cwd"), ("-r", "Short for --root"),
        ("--scope=global", "Global roots only"),
        ("--scope=local", "Project-local roots only"),
        ("--scope=all", "Global and local roots"),
        ("--harness=active", "Only active harnesses (default)"),
        ("--harness=all", "Include archived harnesses"),
        ("--full", "Show every match, not just counts"),
    ],
    "report": [
        ("--days=", "Override with N calendar days"), ("-d", "Short for --days"),
        ("--weeks=", "Override with N calendar weeks"), ("-w", "Short for --weeks"),
        ("--start=", "Override range start"), ("-s", "Short for --start"),
        ("--end=", "Override range end"), ("-e", "Short for --end"),
        ("--here", "Current project only"), ("--agent=", "Filter by agent"),
        ("--search=", "Filter sessions"), ("-q", "Short for --search"),
        ("--session-harness=", "Cheap summarizer harness"),
        ("--session-model=", "Cheap summarizer model"),
        ("--session-arg=", "Extra arg for the summarizer"),
        ("--writer-harness=", "Final writer harness"),
        ("--writer-model=", "Final writer model"),
        ("--writer-arg=", "Extra arg for the writer"),
    ],
    "report followups": [
        ("--status=open", "Open follow-ups only"),
        ("--status=done", "Done follow-ups only"),
        ("--status=dismissed", "Dismissed follow-ups only"),
    ],
    "report followup add": [
        ("--project=", "Attach to a project"),
    ],
    "report followup work": [
        ("--resume", "Resume the session the follow-up came from"),
        ("--new", "Start a new session"),
        ("--harness=", "Harness to run the work in"),
    ],
    "providers list": [
        ("--desc", "Show provider descriptions"), ("-d", "Short for --desc"),
        ("--api-keys-dir=", "Read key files from this directory"),
    ],
    "providers add": [
        ("--url=", "Provider base URL"),
        ("--env=", "Environment variable holding the key"),
        ("--file=", "Path to a key file"),
        ("--kind=", "Provider kind: inference, infra or services"),
    ],
    "mcp discover": [
        ("--apply", "Add discoveries to ~/.swe/mcp.json"),
        ("--json", "Machine-readable output"),
        ("--all", "Include servers already in source-of-truth"),
        ("--prune", "Remove hub servers no harness configures"),
    ],
    "mcp sync": [
        ("--all", "Sync to every known tool"),
        ("--only=", "Limit to these targets"),
        ("--except=", "Skip these targets"),
        ("--force", "Overwrite conflicting servers"),
        ("--skip-conflicts", "Leave conflicting servers alone"),
        ("--no-interactive", "Never prompt"),
        ("--no-adopt", "Skip pulling harness-only servers into the hub"),
        ("--dry-run", "Show what would change"),
        ("--strict", "Exit nonzero on any conflict"),
        ("--prune", "Delete target servers not in the selection"),
        ("--json", "Print what moved as JSON (implies --no-interactive)"),
    ],
    "mcp doctor": [
        ("--strict", "Exit nonzero on warnings"),
    ],
    "mcp list": [
        ("--json", "Machine-readable output"),
    ],
    "mcp status": [
        ("--json", "Machine-readable output"),
    ],
    "mcp secret set": [
        ("--json", "Machine-readable output"),
    ],
    "mcp secret remove": [
        ("--json", "Machine-readable output"),
    ],
    "mcp add": [
        ("--to=", "Also write these harnesses"),
        ("--env=", "Env pair KEY=VALUE (repeatable)"),
        ("--header=", "Header pair KEY=VALUE (repeatable)"),
        ("--url=", "Server URL for an http server"),
        ("--transport=", "http or sse"),
        ("--force", "Replace an existing server"),
        ("--json", "Machine-readable output"),
    ],
    "mcp remove": [
        ("--from=", "Where to remove from (default: everywhere it is)"),
        ("--json", "Machine-readable output"),
    ],
    "mcp disable": [
        ("--for=", "Only these harnesses"),
        ("--json", "Machine-readable output"),
    ],
    "mcp enable": [
        ("--for=", "Only these harnesses"),
        ("--json", "Machine-readable output"),
    ],
    "mcp info": [
        ("--json", "Machine-readable output"),
    ],
    "mcp set-type": [
        ("--clear", "Remove the label"),
        ("--json", "Machine-readable output"),
    ],
    "mcp tools": [
        ("--timeout=", "Seconds to wait for initialize and tools/list"),
        ("--json", "Machine-readable output"),
    ],
    "mcp gateway status": [
        ("--json", "Machine-readable output"),
    ],
    "mcp gateway share": [
        ("--force", "Share a server that looks stateful anyway"),
    ],
    "doctor": [
        ("--json", "Machine-readable output"),
        ("--fix", "Run every safe local fix, then check again"),
        ("--fix=", "Only fix findings under this check"),
        ("--dry-run", "With --fix: show what would run"),
        ("--message=", "With --fix=<check>: the one finding to fix"),
        ("--action=", "With --message: which of its fixes to run"),
    ],
    "harness star": [
        ("--json", "Print the new state as JSON"),
    ],
    "harness usage": [
        ("--json", "Machine-readable output"),
        ("--account-dir=", "The extra account's data dir"),
    ],
    "harness usage-events": [
        ("--json", "Machine-readable output"),
        ("--account-dir=", "The extra account's data dir"),
        ("--since=", "Only events after this epoch-seconds stamp"),
    ],
    "harness usage-daily": [
        ("--json", "Machine-readable output"),
        ("--account-dir=", "The Codex account's home (default ~/.codex)"),
    ],
    "harness archive": [
        ("--json", "Print the result as JSON"),
        ("--usage=none", "Mark the harness as unused"),
        ("--usage=trial", "Mark the harness as tried once"),
        ("--usage=used", "Mark the harness as in use"),
        ("--usage=heavy", "Mark the harness as heavily used"),
    ],
    "discover": [
        ("--apply", "Register high/medium-confidence finds"),
        ("--apply-all", "Register every match"),
        ("--json", "Machine-readable output"),
        ("--all", "Include already-registered tools"),
    ],
    "add": [
        ("-i", "Interactive form"), ("--interactive", "Interactive form"),
        ("--aliases=", "Comma-separated aliases"),
        ("--tags=", "Comma-separated tags"),
        ("--command=", "Prefill the command (-i only)"),
        ("--description=", "Prefill the description (-i only)"),
        ("--no-replace", "Refuse if the name is registered"),
        ("--json", "Print the result as JSON"),
    ],
    "edit": [
        ("--set=", "Set field=value"),
        ("--command=", "New launch command"),
        ("--display-name=", "Human name shown in the app"),
        ("--description=", "New description"),
        ("--aliases=", "Replace aliases"),
        ("--tags=", "Replace tags"),
        ("--version=", "Recorded version"),
        ("--notes=", "Free-form notes"),
        ("--primary-color=", "Brand colour #RRGGBB (empty restores the catalog default)"),
        ("--json", "Apply the flags and print the result as JSON (never prompts)"),
    ],
    "install": [
        ("--via=", "Install method: brew, cask, npm, uv, nix or script"),
        ("--package=", "Package spec for the installer"),
        ("--command=", "Binary the package provides"),
        ("--dry-run", "Show what would happen"),
        ("-n", "Short for --dry-run"),
        ("--json", "List known installers as JSON (never runs)"),
    ],
    "init": [
        ("--full", "List every path, not just the counts"),
        ("--check", "Show what would change, write nothing"),
        ("-n", "Short for --check"),
        ("--json", "With --check, print link states as JSON"),
        ("--force", "Replace real files too (backed up first)"),
        ("--migrate", "Move a pre-0.2.7 ~/.config/swe into ~/.swe"),
        ("--hub=", "Clone your hub repo as ~/.swe first"),
        ("--yes", "Register every new harness without asking"),
        ("--accounts", "Only sync the extra account homes (accounts.sync.*)"),
        ("--no-adopt", "Leave harness-only skills where they are"),
    ],
    "setup": [
        ("--quick", "Only missing or actionable stages"),
        ("--apply", "Apply safe discovery changes"),
        ("--json", "Print discovery preview as JSON"),
        ("--non-interactive", "Preview without prompts or writes"),
        ("--hub=", "Clone your hub repo as ~/.swe first"),
    ],
    "gh": [
        ("--repo", "Repo as owner/name or a github.com URL"),
        ("--state", "Row state filter (open/closed/…)"),
        ("--limit", "Rows to fetch"),
        ("--all", "Include read notifications (inbox)"),
        ("--web", "Open the github.com page instead"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh repo": [
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh repo search": [
        ("--limit", "Rows to fetch"),
        ("--all", "Search all of GitHub, not just your repos and orgs"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh sync": [
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh pr merge": [
        ("--method", "squash | merge | rebase (default: squash)"),
        ("--auto", "Enable auto-merge instead of merging now"),
        ("--repo", "Repo as owner/name or a github.com URL"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh issue comment": [
        ("--body", "The comment text (one argument; reaches gh on stdin)"),
        ("--repo", "Repo as owner/name or a github.com URL"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh issue view": [
        ("--repo", "Repo as owner/name or a github.com URL"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh issue worktree": [
        ("--repo", "Repo as owner/name or a github.com URL"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh run rerun": [
        ("--failed", "Re-run only the failed jobs"),
        ("--repo", "Repo as owner/name or a github.com URL"),
        ("--json", "Print the {schema, data} envelope"),
    ],
    "gh inbox read": [
        ("--all", "Mark every notification read"),
        ("--json", "Print the {schema, data} envelope"),
    ],
}

# Nested flag tables: cmd -> {subcommand: _COMMAND_FLAGS key}. `swe mcp sync
# --<TAB>` consults the "mcp sync" table; `swe hs list` shares `swe list`'s.
_NESTED_FLAGS: dict[str, dict[str, str]] = {
    "list": {"edit": "list edit"},
    "session": {"index": "session index", "mirror": "session mirror"},
    "sync": {"status": "sync status"},
    "worktree": {"list": "worktree list", "ls": "worktree list",
                 "show": "worktree show", "add": "worktree add",
                 "remove": "worktree remove", "rm": "worktree remove",
                 "lock": "worktree lock", "unlock": "worktree unlock",
                 "move": "worktree move", "mv": "worktree move",
                 "prune": "worktree prune", "slim": "worktree slim"},
    "wt": {"list": "worktree list", "ls": "worktree list",
                 "show": "worktree show", "add": "worktree add",
                 "remove": "worktree remove", "rm": "worktree remove",
                 "lock": "worktree lock", "unlock": "worktree unlock",
                 "move": "worktree move", "mv": "worktree move",
                 "prune": "worktree prune", "slim": "worktree slim"},
    "ls": {"edit": "list edit"},
    "harness": {
        "list": "list", "ls": "list",
        "discover": "discover", "archive": "harness archive",
        "star": "harness star", "usage": "harness usage",
        "usage-events": "harness usage-events",
        "usage-daily": "harness usage-daily",
    },
    "hs": {
        "list": "list", "ls": "list",
        "discover": "discover", "archive": "harness archive",
        "star": "harness star", "usage": "harness usage",
        "usage-events": "harness usage-events",
        "usage-daily": "harness usage-daily",
    },
    "mcp": {
        "discover": "mcp discover", "sync": "mcp sync", "doctor": "mcp doctor",
        "list": "mcp list", "ls": "mcp list", "status": "mcp status",
        "secret": "mcp secret set", "secret set": "mcp secret set",
        "secret remove": "mcp secret remove",
        "add": "mcp add", "remove": "mcp remove",
        "disable": "mcp disable", "enable": "mcp enable",
        "set-type": "mcp set-type",
        "info": "mcp info", "tools": "mcp tools",
        "gateway": "mcp gateway status", "gateway status": "mcp gateway status",
        "gateway share": "mcp gateway share",
    },
    "gh": {
        "pr": "gh", "prs": "gh", "pulls": "gh",
        "issue": "gh", "issues": "gh",
        "run": "gh", "runs": "gh", "actions": "gh", "ci": "gh",
        "pages": "gh", "page": "gh",
        "release": "gh", "releases": "gh", "tag": "gh", "tags": "gh",
        "branch": "gh", "branches": "gh", "board": "gh",
        "inbox": "gh", "notifications": "gh", "notifs": "gh",
        "repo": "gh repo", "repo search": "gh repo search",
        "sync": "gh sync",
        "pr merge": "gh pr merge", "run rerun": "gh run rerun",
        "inbox read": "gh inbox read",
        "issue comment": "gh issue comment",
        "issue view": "gh issue view", "issue worktree": "gh issue worktree",
    },
    "skills": {
        "link": "skills link", "unlink": "skills unlink",
        "move": "skills move", "discover": "skills discover",
        "hub": "skills hub", "new": "skills new",
        "enable": "skills enable", "disable": "skills disable",
        "sync": "skills sync", "usage": "skills usage",
        "adopt": "skills adopt", "remove": "skills remove",
        "rm": "skills remove", "delete": "skills remove",
    },
    "sk": {
        "link": "skills link", "unlink": "skills unlink",
        "move": "skills move", "discover": "skills discover",
        "hub": "skills hub",
        "enable": "skills enable", "disable": "skills disable",
        "sync": "skills sync", "usage": "skills usage",
        "adopt": "skills adopt", "remove": "skills remove",
        "rm": "skills remove", "delete": "skills remove",
    },
    "find": {
        "amd": "find", "agents": "find", "agents.md": "find",
        "instructions": "find", "skills": "find", "skill": "find",
        "plugins": "find", "plugin": "find",
        "mcp": "find mcp", "mcps": "find mcp", "servers": "find mcp",
    },
    "plugins": {
        "catalog": "plugins catalog", "marketplace": "plugins marketplace add",
        "info": "plugins info", "import": "plugins import",
        # every action takes the same flags
        "install": "plugins install", "uninstall": "plugins uninstall",
        "enable": "plugins enable", "disable": "plugins disable",
    },
    "hooks": {
        "list": "hooks list", "ls": "hooks list",
        "info": "hooks info", "edit": "hooks edit",
        "new": "hooks new", "check": "hooks check",
        "delete": "hooks delete", "rm": "hooks delete",
        "disable": "hooks disable", "enable": "hooks enable",
        "sync": "hooks sync", "stats": "hooks stats", "log": "hooks log",
    },
    "providers": {
        "list": "providers list", "ls": "providers list", "add": "providers add",
    },
    "pv": {
        "list": "providers list", "ls": "providers list", "add": "providers add",
    },
    "remote": {
        "serve": "remote serve", "pair": "remote pair",
        "status": "remote status", "devices": "remote devices",
        "attach": "remote attach", "agent": "remote agent",
        "notify": "remote notify", "setup": "remote setup",
        "ssh": "remote ssh",
    },
    "report": {
        "daily": "report", "weekly": "report", "followups": "report followups",
        # `followup <action>` takes its own tables below; bare `followup`
        # must not fall through to report's generation flags.
        "followup": "report followup",
        "followup add": "report followup add",
        "followup work": "report followup work",
    },
}

# Subcommands whose first positional argument is a tool name,
# e.g. `swe hs star cl<TAB>` or `swe mcp status oc<TAB>`.
_NESTED_TOOL_TARGETS: dict[str, frozenset[str]] = {
    "harness": frozenset({"star", "archive", "favourite", "favorite", "shelve"}),
    "hs": frozenset({"star", "archive", "favourite", "favorite", "shelve"}),
    "mcp": frozenset({"list", "ls", "status", "edit"}),
    # `swe hooks new <harness> <name>` — the first positional is a harness.
    "hooks": frozenset({"new"}),
}

# Subcommands that take several tool arguments (`swe mcp sync cc oc <TAB>`).
_NESTED_MULTI_TOOL: dict[str, frozenset[str]] = {
    "mcp": frozenset({"sync", "diff", "validate"}),
}

# Subcommands whose first positional is a provider name.
_NESTED_PROVIDER_TARGETS: dict[str, frozenset[str]] = {
    "providers": frozenset({"info", "remove", "rm"}),
    "pv": frozenset({"info", "remove", "rm"}),
}

# Third level of subcommands, e.g. `swe report followup <TAB>`.
_NESTED_SUBCOMMANDS: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("gh", "repo"): [
        ("list", "Pinned repos"),
        ("add", "Pin a repo"),
        ("remove", "Unpin a repo"),
        ("reorder", "Rewrite pin order"),
        ("search", "Search github.com for repos to pin"),
    ],
    ("gh", "pr"): [
        ("list", "List pull requests"),
        ("status", "Current branch, authored, needs-review"),
        ("view", "One PR: checks, reviews, merge state"),
        ("merge", "Merge a PR (squash by default)"),
        ("worktree", "Checkout the PR head under ~/.swe/worktrees"),
    ],
    ("gh", "issue"): [
        ("list", "List issues"),
        ("view", "One issue: body, comments, resolve prompt"),
        ("comment", "Comment on an issue (--body=TEXT)"),
        ("worktree", "Branch swe/issue-N under ~/.swe/worktrees"),
    ],
    ("gh", "run"): [
        ("list", "List workflow runs"),
        ("rerun", "Re-run a workflow run"),
    ],
    ("gh", "inbox"): [
        ("read", "Mark notifications read"),
    ],
    ("report", "followup"): [
        ("add", "Record a follow-up"),
        ("edit", "Edit a follow-up"),
        ("done", "Mark a follow-up done"),
        ("dismiss", "Dismiss a follow-up"),
        ("reopen", "Reopen a follow-up"),
        ("work", "Hand a follow-up to a harness"),
    ],
    ("skills", "catalog"): [
        ("add", "Register a skills directory"),
        ("list", "Show registered catalogs"),
        ("remove", "Unregister a catalog"),
        ("rm", "Short for remove"),
    ],
    ("sk", "catalog"): [
        ("add", "Register a skills directory"),
        ("list", "Show registered catalogs"),
        ("remove", "Unregister a catalog"),
        ("rm", "Short for remove"),
    ],
    ("config", "setup"): [
        ("report", "Configure report models"),
    ],
    ("mcp", "secret"): [
        ("set", "Store a ${NAME} value from stdin"),
        ("remove", "Drop a stored ${NAME} value"),
    ],
    ("mcp", "gateway"): [
        ("status", "Show the gateway and its shared servers"),
        ("share", "Run hub servers once for every session"),
        ("unshare", "Give each session its own copy again"),
    ],
    ("plugins", "marketplace"): [
        ("add", "Register a plugin marketplace"),
    ],
}

_SUBCOMMANDS: dict[str, list[tuple[str, str]]] = {
    "sync": [
        ("status", "Branch mode, ahead/behind and uncommitted changes"),
    ],
    "instructions": [
        ("init", "Fold the current AGENTS.md into a first chapter"),
        ("build", "Compile chapters into the shared AGENTS.md"),
        ("check", "Check generated instructions without writing"),
        ("watch", "Rebuild instructions when their sources change"),
    ],
    "harness": [
        ("list", "List every harness (swe list is the shortcut)"),
        ("ls", "Short for list"),
        ("edit", "Review every harness at once"),
        ("star", "Toggle a favourite"),
        ("archive", "Shelve a harness you have ruled out"),
        ("discover", "Scan PATH for AI coding CLIs"),
        ("usage", "An extra account's rate limit"),
        ("usage-events", "Cursor's per-event usage feed"),
        ("usage-daily", "A Codex account's per-day usage"),
    ],
    "hs": [
        ("list", "List every harness (swe list is the shortcut)"),
        ("ls", "Short for list"),
        ("edit", "Review every harness at once"),
        ("star", "Toggle a favourite"),
        ("archive", "Shelve a harness you have ruled out"),
        ("discover", "Scan PATH for AI coding CLIs"),
        ("usage", "An extra account's rate limit"),
        ("usage-events", "Cursor's per-event usage feed"),
        ("usage-daily", "A Codex account's per-day usage"),
    ],
    "session": [
        ("use", "Resume a session by index"),
        ("index", "Index transcripts for session search"),
        ("mirror", "Keep copies of transcripts the harnesses prune"),
    ],
    "worktree": [
        ("list", "Every worktree with its state, PR and size"),
        ("show", "One worktree in full"),
        ("add", "A worktree for a branch or a PR"),
        ("remove", "Remove one worktree"),
        ("lock", "Lock a worktree"),
        ("unlock", "Unlock a worktree"),
        ("move", "Move a worktree"),
        ("prune", "Remove merged worktrees (dry run unless --yes)"),
        ("slim", "Delete rebuildable folders"),
    ],
    "wt": [
        ("list", "Every worktree with its state, PR and size"),
        ("show", "One worktree in full"),
        ("add", "A worktree for a branch or a PR"),
        ("remove", "Remove one worktree"),
        ("lock", "Lock a worktree"),
        ("unlock", "Unlock a worktree"),
        ("move", "Move a worktree"),
        ("prune", "Remove merged worktrees (dry run unless --yes)"),
        ("slim", "Delete rebuildable folders"),
    ],
    "remote": [
        ("serve", "Run the hub (REST + SSE for the phone)"),
        ("agent", "Satellite: dial out to the hub, no listener"),
        ("pair", "Pair a device: URL, QR and one-time token"),
        ("status", "Probe the hub, list machines and devices"),
        ("devices", "List or revoke paired devices"),
        ("attach", "Move an observed session under supervision"),
        ("notify", "Configure ntfy push notifications"),
        ("setup", "Check every step to a paired phone; --apply does SWE's part"),
        ("ssh", "Phone SSH console: keys and host keys"),
    ],
    "mcp": [
        ("discover", "Find MCP servers across tool configs"),
        ("list", "Matrix of MCP servers across tools"),
        ("ls", "Short for list"),
        ("status", "List with health checks"),
        ("sync", "Copy servers between tools"),
        ("diff", "Compare two tools' configs"),
        ("edit", "Edit one server in one tool"),
        ("validate", "Check config shapes"),
        ("add", "Add a server to the hub"),
        ("remove", "Remove a server"),
        ("disable", "Switch a server off per harness"),
        ("enable", "Switch a server back on"),
        ("set-type", "Label a hub server"),
        ("info", "One server's full detail"),
        ("tools", "List a server's tools by handshake"),
        ("secret", "Store a ${NAME} value"),
        ("doctor", "Deep diagnostics"),
        ("gateway", "Shared-server flags + daemon status"),
        ("help", "Detailed help for one command"),
    ],
    "gh": [
        ("board", "Every surface of a repo in one call"),
        ("sync", "Snapshot your repos + orgs into the local catalog"),
        ("repo", "Pin or search the repos the dashboard watches"),
        ("pr", "Pull requests (alias: prs, pulls)"),
        ("issue", "Issues (alias: issues)"),
        ("run", "Workflow runs (alias: runs, actions, ci)"),
        ("pages", "GitHub Pages site and latest build"),
        ("releases", "Releases (alias: release)"),
        ("tags", "Tags (alias: tag)"),
        ("branches", "Branches (alias: branch)"),
        ("inbox", "GitHub notifications (alias: notifications)"),
        ("help", "Detailed help"),
    ],
    "find": [
        ("amd", "Locate AGENTS.md / CLAUDE.md instruction files"),
        ("skills", "Locate skill folders"),
        ("plugins", "Locate installed plugins"),
        ("mcp", "Locate MCP server configs"),
    ],
    "plugins": [
        ("catalog", "Every plugin in every registered marketplace"),
        ("marketplace", "Add a marketplace source to a harness"),
        ("info", "One plugin's servers, env keys, skills and calls"),
        ("import", "Copy installed plugin files into SWE"),
        ("install", "Install a plugin in one harness"),
        ("uninstall", "Uninstall a plugin from one harness"),
        ("enable", "Enable an installed plugin"),
        ("disable", "Disable an installed plugin"),
    ],
    "hooks": [
        ("list", "Every hook script, its tier, link state, declaration"),
        ("ls", "Short for list"),
        ("info", "One script across harnesses"),
        ("edit", "Open the hub file in $EDITOR"),
        ("new", "Write a Python hook skeleton + settings stanza"),
        ("check", "Declarations vs links vs disk, read-only"),
        ("delete", "Remove a hook's hub file (--foreign: the real file)"),
        ("disable", "Unlink a hook without deleting it"),
        ("enable", "Re-link a disabled hook"),
        ("sync", "Regenerate shared wrappers and re-apply the link plan"),
        ("stats", "Fires, blocks and block rate per hook"),
        ("log", "One hook's recent runs"),
    ],
    "skills": [
        ("list", "Every skill, nested under the root that holds it"),
        ("ls", "Short for list"),
        ("tree", "Show the symlink layout (alias: swe find skills -r)"),
        ("scope", "Show which scope each harness uses"),
        ("link", "Point a harness at a shared skills root"),
        ("unlink", "Remove a skills-root symlink"),
        ("move", "Move a skill between scope roots"),
        ("enable", "Link one shared skill into a harness"),
        ("disable", "Drop one shared skill's link in a harness"),
        ("sync", "Re-apply each harness's skill selection"),
        ("adopt", "Pull harness-only skills into the shared tree"),
        ("remove", "Delete a shared skill and its links"),
        ("usage", "Call counts from the session index"),
        ("new", "Create a SKILL.md in the hub"),
        ("discover", "Find skills not yet cataloged"),
        ("hub", "Plugins the swe hub offers"),
        ("catalog", "Manage extra skills directories"),
        ("help", "Detailed help for one topic"),
    ],
    "sk": [
        ("list", "Every skill, nested under the root that holds it"),
        ("ls", "Short for list"),
        ("tree", "Show the symlink layout (alias: swe find skills -r)"),
        ("scope", "Show which scope each harness uses"),
        ("link", "Point a harness at a shared skills root"),
        ("unlink", "Remove a skills-root symlink"),
        ("move", "Move a skill between scope roots"),
        ("enable", "Link one shared skill into a harness"),
        ("disable", "Drop one shared skill's link in a harness"),
        ("sync", "Re-apply each harness's skill selection"),
        ("adopt", "Pull harness-only skills into the shared tree"),
        ("remove", "Delete a shared skill and its links"),
        ("usage", "Call counts from the session index"),
        ("new", "Create a SKILL.md in the hub"),
        ("discover", "Find skills not yet cataloged"),
        ("hub", "Plugins the swe hub offers"),
        ("catalog", "Manage extra skills directories"),
        ("help", "Detailed help for one topic"),
    ],
    "providers": [
        ("list", "List providers and key coverage"),
        ("ls", "Short for list"),
        ("info", "Show one provider"),
        ("add", "Register a provider"),
        ("remove", "Unregister a provider"),
        ("help", "Detailed help"),
    ],
    "pv": [
        ("list", "List providers and key coverage"),
        ("ls", "Short for list"),
        ("info", "Show one provider"),
        ("add", "Register a provider"),
        ("remove", "Unregister a provider"),
        ("help", "Detailed help"),
    ],
    "report": [
        ("daily", "Report since the previous daily report"),
        ("weekly", "Report since the previous weekly report"),
        ("warnings", "Show warnings for one report manifest"),
        ("followups", "List follow-ups"),
        ("followup", "Manage or work on a follow-up"),
    ],
    "config": [
        ("get", "Read a resolved value"),
        ("set", "Set a value"),
        ("unset", "Remove a value"),
        ("edit", "Open config in an editor"),
        ("check", "Validate configuration"),
        ("setup", "Run interactive setup"),
    ],
    "setup": [
        ("harnesses", "Discover and register coding CLIs"),
        ("providers", "Review provider credential coverage"),
        ("mcp", "Import MCP servers"),
        ("skills", "Unify shared skill roots"),
        ("report", "Configure report models"),
        ("check", "Verify setup state"),
    ],
}


def get_completions(words: list[str]) -> list[tuple[str, str]]:
    """Return [(candidate, description)] for the given word stack.

    ``words`` is the list of words after ``swe`` on the command line.
    The last element may be empty (user pressed TAB after a space) or a
    partial word being typed.
    """
    if not words:
        return list(_PRIMARY_COMMANDS)

    # Only one word — completing the subcommand itself
    if len(words) == 1:
        partial = words[0]
        if partial.startswith("-"):
            return []
        return _filter_by_prefix(_PRIMARY_COMMANDS, partial)

    cmd = words[0]
    # Drop the partial last word for context analysis
    partial = words[-1]
    rest = words[1:-1]  # positional args between cmd and partial

    # Flag completion
    if partial.startswith("-"):
        nested = _NESTED_FLAGS.get(cmd)
        if nested and rest:
            # Two-word subcommands first — but only once their positional
            # is filled (`followup work <id> --<TAB>`), else the flags would
            # be suggested where a required argument belongs.
            if len(rest) > 2:
                key = nested.get(" ".join(rest[:2]))
            else:
                key = None
            key = key or nested.get(rest[0])
            if key is not None:
                return _filter_by_prefix(_COMMAND_FLAGS.get(key, []), partial)
        flags = _COMMAND_FLAGS.get(cmd, [])
        return _filter_by_prefix(flags, partial)

    # Third-level subcommands, e.g. `swe report followup <TAB>`
    if len(rest) == 1 and (cmd, rest[0]) in _NESTED_SUBCOMMANDS:
        return _filter_by_prefix(_NESTED_SUBCOMMANDS[(cmd, rest[0])], partial)

    # Tool-name completion for commands that take a tool argument.
    # `install` also offers installable names not yet registered.
    if cmd in _TOOL_TARGET_COMMANDS and len(rest) == 0:
        if cmd == "install":
            return _install_completions(partial)
        return _tool_completions(partial)

    # `swe hs star <tool>` / `swe mcp status <tool>` — the tool name sits one
    # level deeper than the flat commands handled above.
    nested = _NESTED_TOOL_TARGETS.get(cmd)
    if nested and len(rest) == 1 and rest[0] in nested:
        return _tool_completions(partial)

    # `swe mcp sync cc oc <TAB>` — every positional is a tool; don't re-offer
    # ones already typed (by name or by alias).
    multi = _NESTED_MULTI_TOOL.get(cmd)
    if multi and rest and rest[0] in multi:
        try:
            registry = load_registry()
        except Exception:
            return []
        used = set()
        for arg in rest[1:]:
            used.add(arg if arg in registry else next(
                (n for n, t in registry.items() if arg in (t.get("aliases") or [])),
                arg,
            ))
        return _tool_completions(partial, exclude=used)

    # `swe mcp edit <tool> <server>` — server names from that tool's config.
    if cmd == "mcp" and len(rest) == 2 and rest[0] == "edit":
        return _mcp_server_completions(rest[1], partial)

    # `swe mcp info|tools|enable|disable|set-type <server>` — every name
    # swe knows.
    if (cmd == "mcp" and len(rest) == 1
            and rest[0] in ("info", "tools", "enable", "disable", "set-type")):
        return _mcp_all_server_completions(partial)

    # `swe mcp set-type <server> <type>` — the expected labels; the field
    # is free-form, so these are hints, not a filter.
    if cmd == "mcp" and len(rest) == 2 and rest[0] == "set-type":
        from swe.mcp.gateway import EXPECTED_TYPES
        return _filter_by_prefix(
            [(t, "server label") for t in EXPECTED_TYPES], partial)

    # `swe providers info <name>` — provider names from the registry.
    ptargets = _NESTED_PROVIDER_TARGETS.get(cmd)
    if ptargets and len(rest) == 1 and rest[0] in ptargets:
        return _provider_completions(partial)

    if cmd in _SUBCOMMANDS and len(rest) == 0:
        return _filter_by_prefix(_SUBCOMMANDS[cmd], partial)

    # Tag completion for `swe list [tag]`
    if cmd in ("list", "ls") and len(rest) == 0:
        return _tag_completions(partial)

    return []


def _filter_by_prefix(
    candidates: list[tuple[str, str]], prefix: str
) -> list[tuple[str, str]]:
    if not prefix:
        return list(candidates)
    return [(c, d) for c, d in candidates if c.startswith(prefix)]


def _tool_completions(
    partial: str = "", exclude: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """Return tool names + aliases from the registry."""
    try:
        registry = load_registry()
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for name, tool in sorted(registry.items()):
        if name in exclude:
            continue
        desc = tool.get("description") or ""
        if not partial or name.startswith(partial):
            out.append((name, desc))
        for alias in tool.get("aliases") or []:
            if not partial or alias.startswith(partial):
                out.append((alias, f"alias for {name}"))
    return out


def _provider_completions(partial: str = "") -> list[tuple[str, str]]:
    """Return provider names + aliases from the provider registry."""
    try:
        from swe.providers.registry import load_registry as load_providers

        providers = load_providers()
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for name, info in sorted(providers.items()):
        if not isinstance(info, dict):
            continue
        if not partial or name.startswith(partial):
            out.append((name, str(info.get("name") or name)))
        for alias in info.get("aliases") or []:
            if alias == name:
                continue
            if not partial or alias.startswith(partial):
                out.append((alias, f"alias for {name}"))
    return out


def _install_completions(partial: str = "") -> list[tuple[str, str]]:
    """Registered tools plus installable names not yet registered."""
    out = _tool_completions(partial)
    have = {name for name, _ in out}
    try:
        from swe.harness.catalog import HARNESS_CATALOG
        from swe.harness.installers import INSTALLERS
    except Exception:
        return out
    for name in sorted(INSTALLERS):
        if name in have or (partial and not name.startswith(partial)):
            continue
        spec = HARNESS_CATALOG.get(name, {})
        out.append((name, str(spec.get("description") or "installable")))
    return out


def _mcp_server_completions(tool: str, partial: str = "") -> list[tuple[str, str]]:
    """Return server names configured for ``tool`` (for `mcp edit`)."""
    try:
        from swe.mcp.cli import get_tool_config, get_tool_loader, load_registry as mcp_tools, resolve_tool_arg

        registry = mcp_tools()
        resolved = resolve_tool_arg(registry, tool)
        if not resolved:
            return []
        cfg = get_tool_config(resolved)
        loader = get_tool_loader(resolved)
        if not cfg or loader is None:
            return []
        names = sorted(loader(cfg["path"]))
    except Exception:
        return []
    return _filter_by_prefix([(n, f"server in {resolved}") for n in names], partial)


def _mcp_all_server_completions(partial: str = "") -> list[tuple[str, str]]:
    """Every MCP server name across harnesses, the hub and disabled marks."""
    try:
        from swe.mcp.cli import (disabled_map, get_hub_servers,
                                 get_mcp_tools, get_tool_servers,
                                 load_registry as mcp_tools)

        names: set[str] = set(get_hub_servers())
        registry = mcp_tools()
        for tool in get_mcp_tools(registry):
            names.update(get_tool_servers(tool))
        for held in disabled_map().values():
            names.update(held)
    except Exception:
        return []
    return _filter_by_prefix([(n, "server") for n in sorted(names)], partial)


def _tag_completions(partial: str = "") -> list[tuple[str, str]]:
    """Return tag names from the registry."""
    try:
        registry = load_registry()
    except Exception:
        return []
    tags: dict[str, int] = {}
    for tool in registry.values():
        for tag in tool.get("tags") or []:
            tags[tag] = tags.get(tag, 0) + 1
    out = [(tag, f"{count} tool(s)") for tag, count in sorted(tags.items())]
    return _filter_by_prefix(out, partial)
