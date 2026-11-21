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
      break;
    case "composer-queued-edit":
      send(
        ...queued.slice(0, 3),
        frame(k, "approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow", by: "device-1" }),
        frame(k, "session.state", 14, { state: "running" }),
        frame(k, "queue.updated", 15, { items: [{ command_id: "q1", text: "then lint it", by: "device-1", mode: "queue" }] }),
      );
      await press(c, "Edit queued message");
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
