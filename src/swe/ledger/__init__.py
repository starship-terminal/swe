"""Usage ledger: token counts read from harness logs, priced at query time.

A Python port of the Mac app's ``SWECore/Ledger`` for the web app, so
the hub can show the same Overview on any machine. The rules (which lines
count, dedup keys, the sidechain upsert, Codex's cumulative totals, the
price lookup) follow the Swift code; each module names the file it ports.
Its store is ``~/.swe/data/web-ledger/``, never the Mac app's
``data/ledger/``, so the two can run on one machine without sharing a file.

Every source the Mac ledger reads is covered: Claude and Codex logs, the
OpenCode and Devin databases, the Pi logs, the Droid and Cline
session-total files, Grok's per-turn ``usage.json``, and Cursor's usage
feed (polled through
``rate_limits.fetch_cursor_usage_events`` — the reader behind
``swe harness usage-events cursor --json`` — rather than a subprocess).
"""
