# 0001: Session runtime redesign

Status: accepted. The design is in [docs/specs/G1-session-runtime-redesign.md](../specs/G1-session-runtime-redesign.md) and the milestones are in [docs/roadmap/milestones.yaml](../roadmap/milestones.yaml).

Each decision below lists what was chosen, what was rejected and why.

## D1: SWE-started sessions only

**Decision.** SWE controls only sessions it starts itself. A session started in a terminal is read-only history with "Continue in SWE".

**Rejected.**
- Keep terminal sessions fully controllable through a terminal wrapper. This keeps screen reading, which is the unreliable part.
- Ship SWE's own terminal client. It is a third client to keep in step with the Mac and iPhone apps.

**Reason.** SWE never has to read a terminal screen.

## D2: One web session view inside the current apps

**Decision.** The hub serves one web session view, embedded in the Swift Mac and iOS apps. No Electron now.

**Rejected.**
- An Electron app replacing the Mac app. It retires a 110,000-line Swift app and has to bundle a Python runtime, since the hub is Python.
- Native SwiftUI session views. Every feature would be written three times, as the chat is today.

**Reason.** One view written once, with no rewrite of the existing native apps.

## D3: The agent survives a hub restart, through a keeper

**Decision.** A keeper process, separate from the hub, holds each running agent so a hub restart does not touch it.

**Rejected.**
- Stop the turn and auto-continue. This is t3code's behaviour. It redoes the half-finished step and loses a pending approval.
- Stop and wait for the user.

**Evidence.** Claude Code cancels pending permission prompts when its stdin closes. Omnigent runs one runner process per session under a daemon that outlives its server.

**Reason.** A restart should not cost the user a half-finished step or a pending approval.

## D4: tmux removed completely

**Decision.** Remove tmux. Harnesses with no structured protocol are read-only until they ship one.

**Rejected.**
- Keep tmux as a labelled best-effort fallback.
- A raw streamed terminal view for terminal-only harnesses.

**Reason.** Both keep screen reading, the source of the current unreliability (decision D1).

## D5: Speak each harness's protocol directly in stdlib Python

**Decision.** SWE implements each harness's protocol itself, in stdlib Python.

**Rejected.**
- Vendor SDK packages as an optional extra. They add third-party dependencies to the pipx install and to the copy bundled in SWE.app.
- A Node sidecar. It adds a second runtime on every machine.

**Known cost.** Anthropic states no stability promise for Claude Code's control wire format. Recorded fixtures per CLI version guard it.

**Reason.** The core stays stdlib-only with no extra runtime.

## D6: Every machine stands alone, the hub gathers

**Decision.** Each machine runs its own keepers and a loopback API. The hub aggregates and is the one address a phone knows.

**Rejected.**
- Devices also connecting straight to each machine. This needs pairing per machine and splits the inbox.
- Hub required for everything. The Mac app could not drive its own sessions offline.

**Reason.** The Mac app works with no network, and phones still have one place to connect.

## D7: First version includes the full feature set

**Decision.** The first version includes the changes panel, plan cards and plan mode, checkpoints with restore, and subagent rows.

**Rejected.** No alternative was recorded for this decision.

**Reason.** These were chosen as part of the first version in the interview.

## D8: The view follows t3code's look

**Decision.** The session view follows t3code's look: prose-first replies, "Worked for 2m 29s" work groups, approvals docked in the composer.

**Rejected.** No alternative was recorded for this decision. The finding below rules out adopting a shared library.

**Finding.** No shared open-source harness UI exists. t3code, Harnss and Omnigent each wrote their own on general component kits.

**Reason.** t3code is the reference to copy from and rewrite against, under its MIT notice.

## D9: React with a build step, for the session view only

**Decision.** The session view is React with a build step. The rest of the web app stays plain JS.

**Rejected.**
- Plain JS with no build step. The chat page is already one 4,990-line file.
- A small vendored component library with no build. React-only kits such as the diff viewer could not be used.

**Reason.** The view needs component structure and React-only libraries, and limiting the build to this one view keeps the rest of the web app unchanged.

## D10: Prove the iPhone web view first, keep the SwiftUI chat as fallback

**Decision.** Milestone G2 proves scrolling, keyboard and offline launch in a web view on iPhone before any iPhone work is spent. The SwiftUI chat stays as the fallback.

**Rejected.**
- iPhone stays native SwiftUI. Every feature would be built twice.
- Commit to the web view with no proof.

**Evidence.** [docs/remote-control.md](../remote-control.md) says the iOS app "replaced an earlier WKWebView wrapper around the SPA once the webview's scrolling, keyboard handling and blank offline launch showed."

**Reason.** The earlier web view failed on these exact points, so the new one must pass them first.

## D11: The built bundle is committed, with a CI freshness gate

**Decision.** The built bundle is committed under `src/swe/webapp/static/session/`. CI checks that it matches its source.

**Rejected.**
- Build at install and release. Node would be needed on every installing machine, and all installs copy `src/swe` as source.
- A release download fetched on first use.

**Reason.** Every install path keeps working from source with no Node, and the CI gate keeps the bundle honest.

## Defaults chosen (open to change)

These defaults were chosen in the plan and can be changed.

- Mid-turn sends queue unless the user picks steer. First approval answer wins. Any device can edit any queued message. Each message records the sending device.
- Checkpoints go in a shadow store under `~/.swe/data/checkpoints/`, never as refs in the user's repos. Restore is files only in v1; conversation rollback only where the adapter declares it.
- The hub holds messages for a sleeping machine for 24 hours. A keeper parks an idle session after 30 minutes.
- Autonomy keeps today's default. Presence ("also open on iPhone") is a later milestone.
- For one release, the hub may still run `tmux kill-session` to convert an old pane.
