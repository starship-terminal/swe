# G2: the session web view inside the iPhone app

Milestone G2 of the session runtime redesign (decision D10): prove that the
shared web session view can live inside the iPhone app before any iPhone
work is spent on it. [remote-control.md](../remote-control.md#phone-client)
records that the iOS app dropped an earlier WKWebView wrapper because of
its scrolling, its keyboard handling and a blank offline launch. This
document is the result of testing those three faults, plus the plan's
assumption that a WebKit custom scheme handler can stream fetch bodies and
use IndexedDB.

## Conclusion

The assumption holds on the iOS 27.1 simulator. A `WKURLSchemeHandler` on
`swe-hub://` streams a fetch body event by event (10 events 200 ms apart
arrived in 10 separate reads, 0 to 2 ms after the stub hub wrote each one),
and IndexedDB works on the `swe-hub://proof` origin and survives an app
relaunch. EventSource works on the scheme too. The three old faults did not
reproduce with the shell's settings: only the timeline scrolls, the composer
sits on the keyboard, and an offline relaunch shows the saved page and the
last saved snapshot. The cookie-auth fallback was not needed and was not
tested.

Two caveats carry into G18. WebKit still tries to scroll the outer view by
the full keyboard height when the composer takes focus (396 pt here); the
shell puts it back, and without that the old double shift would return.
WebKit's own form bar (up, down, done) sits between the composer and the
keys. Both are described under [What remains unproven](#what-remains-unproven).

## Results

All runs: iPhone 18 Pro simulator, iOS 27.1, Xcode 27.1, against
`Proof/stub_hub.py` on 127.0.0.1. One full run of `Proof/run-proof.sh`
passed all six UI tests; the numbers below are from that run.

| Check | Result | Test | Evidence |
|---|---|---|---|
| a. Streaming | Pass | `testAStreamingArrivesEventByEvent` | gaps 201, 205, 204, 200, 204, 202, 204, 201, 202 ms; latency 0 to 2 ms; each event in its own read (reads 2 to 11); `streaming.png`, `streaming-done.png` |
| b. Storage | Pass | `testBIndexedDBSurvivesARelaunch` | origin `swe-hub://proof`; launch 1 wrote a fresh token, launch 2 read it back (`launches` 1 then 2) |
| c. Scrolling | Pass | `testCOnlyTheTimelineScrolls` | timeline scrolled 1,625 px and back to its end; outer offset 0, outer resets 0, document scroll 0, zoom 1; `scrolling.png` |
| d. Keyboard | Pass | `testDComposerSitsOnTheKeyboard` | composer bottom 362 pt = keyboard top 362 pt (view coordinates); timeline scrolled 217 px with the keyboard up; closed layout back at 724 pt; `keyboard-open.png`, `keyboard-closed.png` |
| e. Offline launch | Pass | `testEOfflineLaunchShowsTheSavedCopy` | hub port closed (connection refused); `/index.html`, `/proof.js`, `/v1/snapshot` all answered from the cache; snapshot token matched the last live one; 2,001 rows shown; `offline.png` |
| Appearance | Pass | `testFAppearanceReachesThePage` | `data-theme` dark, `prefers-color-scheme: dark` true, background rgb(11, 11, 12); `dark.png` |

### a. Streaming

The page reads `/v1/events` with `fetch` and a `ReadableStream` reader,
the way the session view will read the hub's stream, and appends a row per
event. The handler hands WebKit each chunk URLSession delivers with its
own `didReceive(data)`. A buffered body would arrive in one read at the end
with gaps near 0 and latency up to 1.8 s; instead each of the 10 events had
its own read, gaps stayed at 200 to 205 ms, and latency (page arrival minus
the hub's send time, same clock) was 0 to 2 ms. An earlier cold run showed
jitter (one event 143 ms late while the page was laying out 2,000 rows) but
still one read per event, so the test bounds the median and the worst case
rather than every gap. The same run's EventSource probe got 3 of 3 events,
1 to 5 ms after sending. New rows kept the timeline pinned to its end.

The same test checks the shell's headers end to end: every request the stub
saw carried `Authorization: Bearer <the fake credential>`, `X-Swe-Pair: 1`
and `Origin: http://127.0.0.1:8765`; the stub refuses anything else with a
401. A POST body reached the hub intact. Nothing the page can read (its
URLs, response headers, cookies, its whole state dump) contains the
credential, and the `sweNative.wake()` call reached native code decoded.

### b. Storage

`location.origin` on the custom scheme is `swe-hub://proof`, a real origin
and not an opaque one. The page wrote a token to IndexedDB on the first
launch, the test killed the app, and the second launch read the same
token back. The web view uses a persistent `WKWebsiteDataStore` per hub
(`HubEndpoint.dataStoreID`), the same scheme as the Mac app's per-hub
store.

### c. Scrolling

With the outer scroll view off, not bouncing and ignoring the safe area,
fast swipes, drags past the end and drags on the banner and composer moved
only the timeline. The outer scroll view's offset never left 0, WebKit
never tried to move it during scrolling (0 resets), the document never
scrolled, and the zoom stayed at 1. Momentum and rubber banding inside
the timeline are WebKit's own and look native; whether they feel right is a
judgment for a person on a device.

### d. Keyboard

The shell turns the keyboard's frame into `--keyboard-inset` (396 pt here,
which includes WebKit's form bar and the suggestion row) and the page pads
its bottom by it. The composer's bottom edge matched the keyboard's top
edge exactly, and XCUITest's own frames agreed with the page's numbers. The
document did not scroll and the visual viewport did not move.

WebKit did try to scroll the outer view: when the composer took focus it
set the outer offset to 396 pt, the "reveal the focused field" move that
pushed the old wrapper's page up twice. The shell watches the offset and
puts it back at once (`SessionWebController.pinOuterScroll`), counted as
one reset in the run. This is the one place the proof depends on the shell
fighting WebKit rather than configuring it.

### e. Offline launch

The handler saves every successful plain GET (page, script, snapshot) and,
when the hub can't be reached (a connection error, a timeout, or a 502,
503 or 504 from `tailscale serve` in front of a stopped hub), answers from
that copy with `X-Swe-Shell-Source: cache`. The page shows "Offline:
showing the copy saved at ..." when the snapshot comes from the cache. With
the stub hub's listening socket closed, the relaunched app showed the full
page and the last saved snapshot. The POST in that run failed as it
should: sends never replay a saved answer.

## What the package holds

`app/shared/SessionWebShell` (iOS 17, macOS 26; no dependencies):

| File | What |
|---|---|
| `HubEndpoint.swift` | the pinned hub: page id, real origin, per-hub data store id |
| `RequestRewriter.swift` | page URL to hub URL, header injection, redirect rule, the response the page sees |
| `HubSchemeHandler.swift` | the `WKURLSchemeHandler`: forwards, streams, refuses, falls back to the cache |
| `ShellCache.swift` | the cache hook (`ShellCache`), `CachePolicy`, `DiskShellCache` |
| `NativeBridge.swift` | `sweNative.wake()`, `open(key)`, `back()`: the page script and the decoder |
| `SessionWebView.swift` | the web view factory, `SessionWebController` (navigation rule, keyboard inset, safe area, appearance) |
| `ShellNavigation.swift` | which navigations load, go to the browser, or are dropped |

`swift test` covers request rewriting, origin pinning and refusal (other
hubs, ports, user info, other schemes, redirects off the hub), header
injection, the credential staying out of every URL, response and cache file
the page can read, streaming through the handler chunk by chunk, stop, the
cache fallback choice, the disk cache, bridge decoding and frame trust, the
navigation rule and the injected scripts.

`Proof/` is the throwaway harness: an xcodegen iOS app with a native
navigation bar over the web view, the fixture page, the stub hub and the
UI tests. Run it with `app/shared/SessionWebShell/Proof/run-proof.sh
[output-dir]`; it starts the stub, generates the project, runs the tests on
"iPhone 18 Pro" and leaves the screenshots and JSON numbers in the output
folder. It is not built by CI; the Native workflow runs only `swift test`.

## What remains unproven

- A real iPhone and a real network. Everything ran on the simulator against
  loopback. Losing Wi-Fi or the tailnet gives timeouts, not refusals: the
  handler gives cacheable GETs a 6 s idle timeout before it falls back, so an
  offline launch off the network may sit blank for up to that long per
  request. The 502 path is covered by unit tests only.
- A first launch with nothing cached. The handler fails the navigation and
  the page stays empty; the app (G18) has to show a native error there.
- Long streams. The longest stream was 2 s. Minutes-long streams,
  backgrounding, suspension and the reconnect on wake are untested.
- WebKit's form bar. The up, down and done bar sits between the composer and
  the keys and is counted in `--keyboard-inset`. Removing it needs private
  API; G18 should decide whether to keep it.
- The outer-scroll pin. It worked in every run, but it reacts after WebKit
  moves the view; a person should check on a device that there is no
  one-frame jump when the composer takes focus.
- Feel: momentum, the keyboard animation curve (the page eases its padding
  over the keyboard's duration with a CSS curve, not the keyboard's spring)
  and touch response are for a person to judge.
- The Mac. The package builds and its tests run on macOS, but no Mac web
  view was driven; that is G17.
- Large uploads. A JSON POST body reached the hub; streamed request bodies
  (`httpBodyStream`, large attachments) were not tried.

## Findings for G17 and G18

- `WKWebsiteDataStore.remove(forIdentifier:)` crashed inside WebKit when it
  was the first WebKit call in the process (iOS 27.1 simulator). Clearing
  the store's data with `removeData` works.
- `env(safe-area-inset-bottom)` read 0 at first paint on one run. The shell
  now passes `--safe-area-top` and `--safe-area-bottom` itself, at document
  start and on every safe-area change.
- An iOS 27 app without a scene manifest is stopped at launch.
- While XCUITest polled a label in the app's accessibility tree every
  250 ms, the page's script request never got an answer and the page stayed
  blank; the same build loaded at once when launched by hand. Reading the
  harness's state from a file fixed it. G18's UI tests should do the same.
- WebKit lists no Resource Timing entries for custom-scheme loads, so the
  page's performance tooling sees none of its requests.
- Scroll pinning in the page must not judge "at the end" by distance alone:
  the scroll event from its own jump can land after the next row arrived.
