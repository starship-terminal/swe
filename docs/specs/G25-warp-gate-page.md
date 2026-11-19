# G25: The Warp-Gate view of the MCP servers page

warp-gate is planned to become one MCP server: each harness holds a single
`warp-gate` entry and reaches every service behind it through a few
meta-tools. This spec covers swe's part: the commands, the sync mode and
the page that manages it, on the Mac app and the web app.

The design of warp-gate itself is in the warp-gate repo,
`docs/specs/G1-single-endpoint.md`
([starship-terminal/warp-gate](https://github.com/starship-terminal/warp-gate/blob/main/docs/specs/G1-single-endpoint.md)).
Read it first. Its milestones G2, G3, G5 and G7 are what swe's milestones
wait on.

Milestones G25 to G30 are in
[docs/roadmap/milestones.yaml](../roadmap/milestones.yaml).

## Words

| Word | Meaning |
|---|---|
| Service | One MCP server behind the gate. A hub entry with `"shared": true`. |
| Catalogue | The services warp-gate ships knowledge of: titles, key names, read-only tool lists. |
| Connected | A harness whose config holds the one `warp-gate` entry. |
| On demand | A service whose tools agents reach through search. The default. |
| Listed | A service whose tools also appear by name in every session. |
| Per harness view | Today's page: the server by harness grid. |
| Warp-Gate view | The new view this spec adds. |

## What stays the same

- The Per harness view, unchanged, and still the default.
- Servers that cannot go behind the gate keep their per-harness switches:
  a server that keeps state per session (Playwright, a REPL, computer
  use) and, until warp-gate's G9, a server reached at a URL.
- `gateway.mode` defaults to `per_server`, which is today's behaviour:
  one `/mcp/<name>` entry per shared server.
- The hub (`~/.swe/mcp.json`) stays the source of truth for which servers
  exist. swe keeps owning add, remove, rename and secrets.

## Who does what

| Job | Owner |
|---|---|
| Serving `/mcp`, the meta-tools, the tool index, the catalogue | warp-gate |
| Service list with state, tool counts and key names as JSON | `warp-gate services --json` |
| A service's tools as JSON | `warp-gate tools <id> --json` |
| Writing harness configs | `swe mcp sync` |
| Which harnesses are connected | swe's per-harness enable and disable |
| Every screen | swe's Mac app and web app |

swe runs the `warp-gate` binary it finds on PATH and reads its `--json`
output. It does not port warp-gate's logic. When the binary is missing,
every command below says so and exits 1, and the page shows the "not
installed" state.

## Commands (G26)

`swe mcp gateway` gains these subcommands. Each takes `--json` and prints
swe's usual envelope.

| Command | What it does |
|---|---|
| `swe mcp gateway services` | Passes through `warp-gate services --json`, and adds `connected`: one row per harness with `{harness, connected, supported, why_not}`. |
| `swe mcp gateway tools <id>` | Passes through `warp-gate tools <id> --json`. |
| `swe mcp gateway set <id> --expose=on_demand\|listed` | Runs `warp-gate set`. |
| `swe mcp gateway mode [per_server\|single]` | Prints or writes `gateway.mode`. Writing it prints the sync that has to follow. |
| `swe mcp gateway install` / `uninstall` | Runs `warp-gate install` or `uninstall`. This is the header switch. |
| `swe mcp gateway connect <harness...>` / `disconnect <harness...>` | Turns the `warp-gate` entry on or off for those harnesses, then syncs them. |

`share`, `unshare` and `status` stay as they are.

Two fixes ride along:

- `settings_of` in `src/swe/mcp/gateway.py` also returns `mode`
  (`per_server` when absent or unknown).
- `_save_settings` in `src/swe/mcp/gateway_commands.py` rewrites the
  `gateway` key with only `enabled` and `port` today. It must change only
  the keys it sets and keep the rest, or it would drop `mode`,
  `idle_minutes`, `token` and `services`.

Each new command's `--json` shape goes into
[docs/json-contracts.md](../json-contracts.md) with a fixture for the
Mac app's contract tests.

## Sync with `gateway.mode: single` (G27)

When the gateway is enabled and the mode is `single`, a hub sync changes
in three ways:

1. It writes one entry named `warp-gate`, with the definition
   `{"url": "http://127.0.0.1:<port>/mcp"}`, into each connected harness.
2. It writes no `/mcp/<name>` entry for a shared server, and removes one
   it wrote earlier. Otherwise a session would see each tool twice.
3. Every other server syncs exactly as today.

Rules:

- `warp-gate` is a name swe reserves. It is not stored in `mcpServers`;
  sync adds it from the gateway settings. A hub or harness server already
  named `warp-gate` with a different definition is reported as a conflict
  and left alone.
- Whether a harness is connected uses the existing per-harness off list
  (`swe.disabled.<harness>`), under the name `warp-gate`. A harness is
  connected unless it is on that list. `connect` and `disconnect` edit it.
- A harness whose config file takes local servers only (Claude Desktop)
  cannot hold a URL entry. It gets no `warp-gate` entry and is reported
  as `supported: false`, `why_not: "local servers only"`, until
  warp-gate's stdio bridge (its G11) exists.
- The sync summary line counts the entry once: "1 through the gateway
  (warp-gate, 18 services)".
- `--dry-run` shows the `warp-gate` entry to add and each per-server
  gateway entry to remove.
- Switching the mode back to `per_server` and syncing restores the
  per-server entries and removes the `warp-gate` entry.
- With the gateway not enabled, the mode has no effect: shared servers
  sync as their stdio definitions, as today.

When warp-gate's access token is on, sync also writes the
`Authorization` header into the `warp-gate` entry and into every
per-server gateway entry. That part is built with warp-gate's G10, not in
G27.

## The page

The Mac app builds it first (G28, G29). The web app follows (G30) with
the same regions and labels. Both use the existing design tokens and
components: the catalogue card and category tabs from the service
catalogue sheet on the Service status page, the server sheet's Facts
rows, and the state arrows for running, idle and error.

### View switch

A two-part control in the page header: **Per harness** and **Warp-Gate**.
The choice is remembered per device. The Warp-Gate part is shown even
when warp-gate is not installed, so the feature can be found.

### Warp-Gate view

| Region | Content |
|---|---|
| Strip | A switch labelled **Warp-Gate**, the endpoint `http://127.0.0.1:8478/mcp` in the code font, and a **Connect harnesses** button showing how many are connected. |
| Tools row | A search field with the prompt "Search services", and **Add custom service**. |
| Category tabs | All, Dev, Productivity, Research, Social, Search, Other. These are the hub's existing type labels. |
| On | A two-column grid of cards for services that are on, with the count in the section title. |
| Catalogue | Cards for catalogue entries not added yet, each with **Add**, or the reason it cannot be added. |
| Not through the gate | Rows for servers that stay per harness, each with its reason ("Keeps state per session", "Reached at a URL") and a link to the Per harness view. |
| Footer | "N of M" for the filter in effect. |

A service card shows: the service icon or its first letter, the title,
one line with the tool count and the key state ("24 tools · key saved",
"8 tools · no key needed", "Needs MCP_NOTION_TOKEN"), and the state on
the right (a state arrow with "Running", "Idle" or "Error"). Clicking a
card opens the service sheet.

The strip's switch runs `swe mcp gateway install` or `uninstall`. Before
it runs, a confirmation shows the sync preview: which harness files will
change and how. Turning it off puts each shared server back as its own
entry in every harness that had it.

States the view must handle:

| State | What shows |
|---|---|
| warp-gate not installed | The strip with the switch off and one line: "Warp-Gate is not installed", with the install command. No cards. |
| Installed, switch off | The strip, and the catalogue greyed out. |
| On, not answering | The strip with "Not answering on port 8478" and a Retry button. Cards show their last known data. |
| Loading | Card placeholders in the grid. |
| No services on | The catalogue section first, with one line inviting the user to turn a service on. |
| A service in error | Its card shows the error state; the sheet shows the message. |
| Search with no match | "No services match" and a Clear button. |

### Service sheet

| Region | Content |
|---|---|
| Header | Back to Services, the icon, the title, a line with "Built in" or "Custom", the repo and the service id, and the **On** switch. |
| Facts | State, Tools, Calls, Last call. |
| Keys | One row per key name with "Saved" or "Missing", and **Replace** or **Set**. Values are never shown or fetched. |
| How agents see it | **On demand** or **Listed**, with one line explaining each. Listed shows the tool count that will be added to every session. |
| Tools | A search field and one row per tool: the name, a "Read only" or "Can write" chip, and a disclosure that shows the description. |
| Footer | **Done**. A custom service also has **Remove**. |

The On switch runs `swe mcp gateway share` or `unshare`. Setting a key
uses `swe mcp secret set`, as the server sheet does today. The choice
between on demand and listed runs `swe mcp gateway set`.

### Connect harnesses sheet

The endpoint, then one row per harness: its icon and name, and either
**Connected**, a **Connect** button, or the reason it cannot connect
("Local servers only"). Connect and disconnect run
`swe mcp gateway connect` and `disconnect`, which sync that harness.

In `per_server` mode the sheet has one extra control at the top: **Use
one entry per harness**, which sets the mode to `single` after showing
the sync preview.

### Add custom service sheet

Fields: Display name, Runs as (Command or URL; URL is disabled with
"Not supported yet" until warp-gate's G9), Command, Keys it needs, and a
checkbox "Keeps state per session, so don't share it". **Add** writes
the hub entry through `swe mcp add` and, unless the checkbox is ticked,
shares it.

### Rules for both clients

- Animate what changes: the view switch, cards appearing, the sheet, the
  switch. Honour reduced motion.
- No text in all caps, and the code font only for ids, key names, tool
  names and the endpoint.
- The catalogue's category labels and every state word are sentence
  case.
- Secret values never reach the client. The page shows key names and
  whether each is saved.

## Files

| Area | Files |
|---|---|
| Commands | `src/swe/mcp/gateway.py`, `src/swe/mcp/gateway_commands.py`, the `swe mcp` help and completion tables, `tests/test_mcp_gateway.py` |
| Sync | `src/swe/mcp/cli.py` (the sync path), `tests/test_mcp_gateway_single.py` |
| Contracts | `docs/json-contracts.md`, fixtures under `app/mac/Tests/SWECoreTests/Fixtures/` |
| Mac model | `app/mac/Sources/SWECore/Mcp/WarpGate.swift` (new), tests under `app/mac/Tests/SWECoreTests/` |
| Mac view | `app/mac/Sources/SWEApp/Pages/McpWarpGateView.swift` (new), `McpWarpGateSheets.swift` (new), and the view switch in `McpServersPage.swift` |
| Web | `src/swe/webapp/static/pages/mcp.js`, `mcp.css`, the page data in `src/swe/webapp/api.py`, `docs/web-app/mcp.md` |
| Inventory | `FEATURES.md`, `docs/ported-logic.md` for logic written in both Swift and JavaScript |

## Milestones

| Id | Title | Needs | Tier |
|---|---|---|---|
| G25 | This spec and the milestones | | standard |
| G26 | Gateway settings and `swe mcp gateway` commands | warp-gate G5 and G7 | standard |
| G27 | Single-entry sync | G26, warp-gate G3 | hard: it rewrites every harness config |
| G28 | Mac app: Warp-Gate view and connect sheet | G26 | standard |
| G29 | Mac app: service sheet and add sheet | G28 | standard |
| G30 | Web app: Warp-Gate view | G26 | standard |

G28 can be built against fixtures before warp-gate's G5 ships, because
the JSON shape is fixed in the warp-gate spec. It cannot be verified
against a live gateway until then.

## Screens to capture

Each UI milestone attaches screenshots at desktop width, and the web app
at phone width too:

- G28: Warp-Gate view with services on; not installed; not answering;
  loading; no services on; search with no match; the connect sheet.
- G29: service sheet for a service with a saved key, one with a missing
  key and one in error; the tools list with a row open; the add sheet
  empty and with a validation error.
- G30: the same states as G28 and G29 in the web app, in light and dark.

## Open questions

Q1 and Q5 are shared with the warp-gate spec. Q6 is swe's own.

| Id | Question | Default until answered |
|---|---|---|
| Q1 | Should one connected harness see a different set of services from another? | No. Use the Per harness view for servers that need it. |
| Q5 | How does a machine with swe get warp-gate: bundled in SWE.app, or its own package? | Its own package on PATH. The page shows the install command when it is missing. |
| Q6 | Should the Warp-Gate view become the default once a machine is in `single` mode? | No. The last view chosen is remembered. |
