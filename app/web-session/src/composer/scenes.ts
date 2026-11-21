// The composer's scenes for the screens page (src/session/screens): each one scripts the store to a
// state worth photographing, over the recorded fixtures plus a few hand-built frames. Dev only; the
// build never imports it (it is reached from the screens page alone).
import type { Frame, OutboxEntry, SessionStore, Snapshot } from "../core";
import type { OutboxView } from "../core";
import { writeDraft } from "./draft";

interface Recording {
  connections: { frames: Frame[] }[];
  snapshots: { body: Snapshot }[];
}

export interface SceneContext {
  key: string;
  store: SessionStore;
  transport: { frames(...frames: Frame[]): void };
  env: { set(online: boolean): void };
  fixture(name: string): Recording;
  showSends(entries: OutboxEntry[], over?: Partial<OutboxView>): void;
  entry(over: Partial<OutboxEntry> & Pick<OutboxEntry, "commandId" | "text" | "status">): OutboxEntry;
  sleep(ms: number): Promise<void>;
}

const frame = (key: string, type: string, seq: number, data: unknown, turn = "t1"): Frame =>
  ({ v: 2, type, session: key, seq, ts: 1760000100 + seq, turn_id: turn, data }) as unknown as Frame;

/** Typing into the message box the way a person does: the box sees an `input` event. */
async function type(c: SceneContext, words: string): Promise<void> {
  let box: HTMLTextAreaElement | null = null;
  for (let i = 0; i < 50 && box === null; i++) {
    box = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message"]');
    if (box === null) await c.sleep(20);
  }
  if (box === null) throw new Error("no message box");
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  setter?.call(box, words);
  box.dispatchEvent(new Event("input", { bubbles: true }));
  await c.sleep(30);
}

async function press(c: SceneContext, label: string): Promise<void> {
  await c.sleep(40);
  const found = Array.from(document.querySelectorAll("button")).find((b) => b.getAttribute("aria-label") === label || (b.textContent ?? "").trim() === label);
  found?.click();
  await c.sleep(40);
}

/** Press the first button whose text starts with `prefix` (a work group's header says how long it worked). */
async function pressStarting(c: SceneContext, prefix: string): Promise<void> {
  await c.sleep(60);
  Array.from(document.querySelectorAll("button")).find((b) => (b.textContent ?? "").trim().startsWith(prefix))?.click();
  await c.sleep(80);
}

/** The strip of the queued tray opens its list upward. */
async function openTray(c: SceneContext): Promise<void> {
  await c.sleep(60);
  document.querySelector<HTMLButtonElement>("[data-tray] button[aria-controls]")?.click();
  await c.sleep(300);
}

/** A right click, which opens the primary button's menu while a reply runs. */
async function rightClick(c: SceneContext, label: string): Promise<void> {
  await c.sleep(60);
  const el = Array.from(document.querySelectorAll("button")).find((b) => b.getAttribute("aria-label") === label);
  el?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
  await c.sleep(200);
}

/** Scroll the timeline so `target` sits under the bar, to see the glass over it. */
async function putUnderBar(c: SceneContext, target: () => Element | null): Promise<void> {
  await c.sleep(300);
  const main = document.querySelector<HTMLElement>('main[aria-label="Timeline"]');
  const bar = document.querySelector<HTMLElement>("form[aria-label='Composer']");
  const el = target();
  if (main === null || bar === null || el === null) return;
  const barBox = bar.getBoundingClientRect();
  const box = el.getBoundingClientRect();
  main.scrollTop += box.top + box.height / 2 - (barBox.top + barBox.height / 2);
  await c.sleep(300);
}

/** A turn that edits a file: its work group, opened, shows a diff with a red line. */
function editTurn(k: string, seq: number): Frame[] {
  const turn = "td";
  return [
    frame(k, "turn.started", seq, {}, turn),
    frame(k, "message.completed", seq + 1, { id: "du", kind: "user", text: "Change the greeting in src/lib.py" }, turn),
    frame(k, "tool.started", seq + 2, { call_id: "dc", name: "Edit", summary: "Edit src/lib.py", input: { path: "src/lib.py" } }, turn),
    frame(k, "tool.completed", seq + 3, { call_id: "dc", status: "ok", name: "Edit", output: "ok", file_edits: [{ path: "src/lib.py", kind: "modify", diff: "@@ -1,3 +1,3 @@\n-print('hi')\n-print('there')\n+print('hello')\n+print('world')\n pass\n" }] }, turn),
    frame(k, "message.completed", seq + 4, { id: "da", kind: "assistant", text: "Done: both lines changed." }, turn),
    frame(k, "turn.ended", seq + 5, { status: "completed", duration_s: 4 }, turn),
  ];
}

/** Press the last button whose text starts with `prefix`. */
async function pressLast(c: SceneContext, prefix: string): Promise<void> {
  await c.sleep(60);
  Array.from(document.querySelectorAll("button")).filter((b) => (b.textContent ?? "").trim().startsWith(prefix)).at(-1)?.click();
  await c.sleep(80);
}

/** A long conversation: `turns` question-and-answer pairs, each answer with a list and a code block. */
function longHistory(k: string, from: number, turns: number): Frame[] {
  const out: Frame[] = [];
  for (let i = 0; i < turns; i++) {
    const seq = from + i * 4;
    const turn = `tl${i}`;
    out.push(
      frame(k, "turn.started", seq, {}, turn),
      frame(k, "message.completed", seq + 1, { id: `lu${i}`, kind: "user", text: `Step ${i + 1}: what does the retry helper do when the hub answers 502?` }, turn),
      frame(
        k,
        "message.completed",
        seq + 2,
        {
          id: `la${i}`,
          kind: "assistant",
          text: `It tries again after a pause that doubles each time, up to five tries:\n\n- the first pause is one second\n- the cap is thirty seconds\n- a 4xx is never retried\n\n\`\`\`python\ndef retry(call, tries=5):\n    pause = 1\n    for attempt in range(tries):\n        try:\n            return call()\n        except Busy:\n            time.sleep(min(pause, 30))\n            pause *= 2\n\`\`\`\n\nThat is the whole story for step ${i + 1}.`,
        },
        turn,
      ),
      frame(k, "turn.ended", seq + 3, { status: "completed", duration_s: 3 }, turn),
    );
  }
  return out;
}

/** What is typed before the page draws, for the scene that shows a draft coming back after a reload. */
export function seedDraft(scene: string, key: string): void {
  // Drafts live in the browser's storage, so a scene starts from none (the last one's would still be there).
  writeDraft(key, { text: "", files: [] });
  if (scene !== "composer-attachments") return;
  writeDraft(key, {
    text: "Here is the failing log and the config I changed. Can you check the retry settings?",
    files: [
      { ref: "1760000000-aa11bb-build.log", name: "build.log", size: 48213 },
      { ref: "1760000001-cc22dd-config.json", name: "config.json", size: 912 },
      { ref: "1760000002-ee33ff-screenshot-of-the-failing-checkout-flow-on-the-staging-site.png", name: "screenshot-of-the-failing-checkout-flow-on-the-staging-site.png", size: 220118 },
    ],
  });
}

const LONG_REPLY = "Update the retry logic so that a request which fails with a 502 is tried again after a pause that doubles each time, up to five tries, and add a test that fakes the clock.";

/** Plays a composer scene. Returns false when `scene` is not one of ours. */
export async function composerScene(scene: string, c: SceneContext): Promise<boolean> {
  if (!scene.startsWith("composer-")) return false;
  const k = c.key;
  const base = c.fixture("default").connections[0]?.frames ?? [];
  const approval = c.fixture("approval").connections[0]?.frames ?? [];
  const queued = c.fixture("queued").connections[0]?.frames ?? [];
  const streaming = c.fixture("streaming").connections[0]?.frames ?? [];
  const send = (...frames: Frame[]): void => c.transport.frames(...frames);

  switch (scene) {
    case "composer-empty":
      send(...base);
      break;
    case "composer-typing":
      send(...base);
      await type(c, "Can you also add a test for the empty-file case?");
      break;
    case "composer-multiline":
      send(...base);
      await type(c, "Two things:\n1. Rename `load_config` to `read_config` everywhere.\n2. Keep the old name as a thin wrapper that logs a deprecation warning.\n\nDo not touch the tests in tests/legacy/ yet.\nRun the linter at the end and tell me what it says.");
      break;
    case "composer-sending":
      send(...base);
      await type(c, "Run the whole suite and tell me what fails");
      // The device's storage is slow to take the message: the box is locked, the draft stays.
      c.store.setState({ send: () => new Promise(() => {}) });
      await press(c, "Send message");
      break;
    case "composer-busy":
      send(...streaming.slice(0, 5));
      await type(c, "Actually, use the retry helper in utils instead.");
      break;
    case "composer-queued":
      send(
        ...queued.slice(0, 3),
        frame(k, "approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow", by: "device-1" }),
        frame(k, "session.state", 14, { state: "running" }),
        frame(k, "queue.updated", 15, {
          items: [
            { command_id: "q1", text: "then lint it", by: "device-1", mode: "queue" },
            { command_id: "q2", text: LONG_REPLY, by: "device-2", mode: "queue", attachments: ["1760000000-aa11bb-build.log"] },
          ],
        }),
      );
      await type(c, "And bump the version");
      await openTray(c);
      break;
    case "composer-queued-many":
      send(
        ...queued.slice(0, 3),
        frame(k, "approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow", by: "device-1" }),
        frame(k, "session.state", 14, { state: "running" }),
        frame(k, "queue.updated", 15, {
          items: [
            { command_id: "q1", text: "then lint it", by: "device-1", mode: "queue" },
            { command_id: "q2", text: LONG_REPLY, by: "device-2", mode: "queue", attachments: ["1760000000-aa11bb-build.log"] },
            { command_id: "q3", text: "and update the changelog", by: "device-1", mode: "queue" },
            { command_id: "q4", text: "then open the pull request and ask for a review", by: "device-1", mode: "queue" },
            { command_id: "q5", text: "finally tag the release", by: "device-1", mode: "queue" },
          ],
        }),
      );
      await type(c, "And bump the version");
      await openTray(c);
      break;
    case "composer-approval-confirm":
      send(...approval.slice(0, 2));
      await press(c, "Always allow");
      break;
    case "composer-queued-edit":
      send(
        ...queued.slice(0, 3),
        frame(k, "approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow", by: "device-1" }),
        frame(k, "session.state", 14, { state: "running" }),
        frame(k, "queue.updated", 15, { items: [{ command_id: "q1", text: "then lint it", by: "device-1", mode: "queue" }] }),
      );
      await openTray(c);
      await press(c, "Edit queued message");
      break;
    case "composer-menu-send":
      send(...streaming.slice(0, 5));
      await type(c, "Actually, use the retry helper in utils instead.");
      await rightClick(c, "Queue message");
      break;
    case "composer-menu-steer-stops":
    case "composer-menu-steer-after-tool":
    case "composer-menu-steer-next-turn": {
      const effect = scene.endsWith("stops") ? "stops_reply" : scene.endsWith("after-tool") ? "after_tool" : "next_turn";
      send(...streaming.slice(0, 5), frame(k, "session.caps", 50, { caps: { steer: true, plan_mode: true, rollback: false, fork: true, stop_task: true, options: [], steer_effect: effect } }));
      await type(c, "Actually, use the retry helper in utils instead.");
      await rightClick(c, "Queue message");
      break;
    }
    case "composer-queued-menu":
      send(
        ...queued.slice(0, 3),
        frame(k, "approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow", by: "device-1" }),
        frame(k, "session.state", 14, { state: "running" }),
        frame(k, "queue.updated", 15, { items: [{ command_id: "q1", text: "then lint it", by: "device-1", mode: "queue" }] }),
      );
      await openTray(c);
      await press(c, "More for queued message");
      break;
    case "composer-error-note":
      send(...base);
      await type(c, "Run the whole suite and tell me what fails");
      c.store.setState({ send: () => Promise.resolve({ ok: false, reason: "gone", message: "This session no longer exists." } as never) });
      await press(c, "Send message");
      break;
    case "composer-approval-error":
      send(...approval.slice(0, 2));
      await press(c, "Allow");
      break;
    case "composer-menu-attach":
      send(...base);
      await press(c, "Attach");
      break;
    case "composer-over-code":
      send(...base, ...longHistory(k, 60, 10));
      await putUnderBar(c, () => Array.from(document.querySelectorAll("main pre")).at(-3) ?? null);
      break;
    case "composer-over-diff":
      send(...base, ...longHistory(k, 60, 6), ...editTurn(k, 100), ...longHistory(k, 110, 6));
      await pressLast(c, "Worked");
      await pressLast(c, "Edit src/lib.py");
      await putUnderBar(c, () => document.querySelector("main pre .text-danger-text"));
      await putUnderBar(c, () => document.querySelector("main pre .text-danger-text")); // the opening settled: aim again
      break;
    case "composer-keyboard-approval":
      send(...approval.slice(0, 2));
      document.documentElement.style.setProperty("--keyboard-inset", "336px");
      await type(c, "Thanks, go ahead");
      break;
    case "composer-keyboard":
      send(...base);
      document.documentElement.style.setProperty("--keyboard-inset", "336px");
      await type(c, "Can you also add a test for the empty-file case?");
      break;
    case "composer-approval":
      send(...approval.slice(0, 2));
      await type(c, "Thanks, go ahead");
      break;
    case "composer-approval-offline":
      send(...approval.slice(0, 2));
      c.env.set(false);
      break;
    case "composer-input-text":
      send(...approval.slice(0, 9));
      break;
    case "composer-input-choice":
      send(
        ...approval.slice(0, 4),
        frame(k, "input.requested", 16, {
          request_id: "r2",
          prompt: "Which branch should I push to?",
          kind: "choice",
          options: [
            { id: "main", label: "main" },
            { id: "release-2.4", label: "release-2.4" },
            { id: "a-new-branch", label: "Create a new branch named after the task" },
          ],
        }),
        frame(k, "session.state", 17, { state: "waiting" }),
      );
      break;
    case "composer-offline":
      send(...base);
      c.env.set(false);
      await type(c, "Check the build when you are done");
      break;
    case "composer-storage-down":
      send(...base);
      c.showSends(
        [c.entry({ commandId: "w-a", text: "Fix the failing test in src/app.py", status: "waiting", attempts: 0 })],
        { storageDown: true, persistent: false, note: "IndexedDB failed (connection lost). Sends saved before are not sent until it answers again, and new ones are kept in memory and will not survive a reload until then" },
      );
      await type(c, "Then run the linter");
      break;
    case "composer-loading":
      send(...base);
      c.showSends([c.entry({ commandId: "w-a", text: "Run the tests again", status: "failed", error: "Not sent." })], { loading: true });
      await type(c, "Then run the linter");
      break;
    case "composer-no-steer":
      send(
        ...streaming.slice(0, 5),
        frame(k, "session.caps", 30, { caps: { steer: false, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }),
        frame(k, "queue.updated", 31, { items: [{ command_id: "q1", text: "then lint it", by: "device-1", mode: "queue" }] }),
      );
      await type(c, "Also update the README");
      await openTray(c);
      break;
    case "composer-read-only":
      send(
        ...base,
        { v: 2, type: "session.row", session: k, ts: 1760000200, data: { row: { ...(c.fixture("default").snapshots[0]?.body.row ?? { key: k }), control: "read_only", control_reason: "Started in a terminal. Continue in SWE to drive it from here." } } } as unknown as Frame,
      );
      break;
    case "composer-attachments":
      send(...base);
      break;
    case "composer-hostile": {
      const nasty = '<img src=x onerror="alert(1)"> **not bold** [a link](javascript:alert(1)) ';
      send(
        ...approval.slice(0, 4),
        frame(k, "tool.started", 14, { call_id: "call-9", name: "Bash".repeat(60), summary: "x", input: { command: nasty.repeat(2000), env: { A: "b".repeat(50_000) } } }),
        frame(k, "approval.requested", 15, {
          request_id: "r9",
          kind: "command",
          call_id: "call-9",
          prompt: nasty.repeat(900),
          options: [
            { id: "allow", label: "Allow", kind: "allow" },
            { id: "always", label: `Always allow ${nasty.repeat(20)}`, kind: "always", standing: true },
            ...Array.from({ length: 40 }, (_, i) => ({ id: `row-${i}`, label: `Row ${i}: ${nasty}` })),
            { id: "deny", label: "Deny", kind: "deny" },
          ],
        }),
        frame(k, "session.state", 16, { state: "waiting" }),
      );
      break;
    }
    default:
      throw new Error(`no composer scene ${scene}`);
  }
  await c.sleep(60);
  return true;
}
