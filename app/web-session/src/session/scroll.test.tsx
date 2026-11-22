// Where the timeline's window sits: it follows the newest content while the person reads the end, keeps
// the row they were reading when older history lands, and does not jump when they open something.
// jsdom has no layout, so each test installs a small one: every row is a block of a set height, in order.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { snapshotFrame } from "../core/test-support";
import { all, bodyOf, button, click, defaultFrames, frame, q, setup, settle, text } from "./test-harness";

const ROW = 100;
const VIEW = 400;

interface Layout {
  scroller: HTMLElement;
  /** Heights by row key; a row not named is ROW tall. */
  heights: Record<string, number>;
  /** Extra height below the rows (a reply growing). */
  extra: number;
}

/** Give the scroller a window VIEW tall, and give each `[data-row-key]` a box from the heights, in order. */
function installLayout(scroller: HTMLElement): Layout {
  const layout: Layout = { scroller, heights: {}, extra: 0 };
  const rows = () => Array.from(scroller.querySelectorAll<HTMLElement>("[data-row-key]"));
  const height = (el: HTMLElement) => layout.heights[el.dataset["rowKey"] ?? ""] ?? ROW;
  Object.defineProperty(scroller, "clientHeight", { configurable: true, value: VIEW });
  Object.defineProperty(scroller, "scrollHeight", {
    configurable: true,
    get: () => rows().reduce((sum, el) => sum + height(el), 0) + layout.extra,
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
    if (this === scroller) return box(0, VIEW);
    if (this.dataset["rowKey"] === undefined) return box(0, 0);
    let top = 0;
    for (const el of rows()) {
      if (el === this) break;
      top += height(el);
    }
    top -= scroller.scrollTop;
    return box(top, top + height(this));
  });
  return layout;
}

const message = (seq: number, words = `message ${seq}`) =>
  frame("message.completed", seq, { id: `m${seq}`, kind: "user", text: words }, { turn_id: `t${seq}` });

/** A session of user messages at these seqs, with older ones available. */
async function openWith(seqs: number[], more = true) {
  const t = setup();
  const body = bodyOf("default");
  await settle(() =>
    void t.transport.last.send(
      snapshotFrame({ ...body, messages: seqs.map((seq) => message(seq)) as never, has_more: more, next_before: more ? (seqs[0] as number) : null, state: "idle", open_turn: null, head: 98 }),
      frame("session.state", 0, { state: "idle", live: [], pending: [], head: 98, epoch: 0 }),
    ),
  );
  const scroller = q(t.el, 'main[aria-label="Timeline"]') as HTMLElement;
  return { t, scroller, layout: installLayout(scroller) };
}

async function scrollTo(scroller: HTMLElement, top: number) {
  scroller.scrollTop = top;
  await settle(() => void scroller.dispatchEvent(new Event("scroll")));
}

afterEach(() => vi.restoreAllMocks());

describe("older history landing above the reader", () => {
  const SEQS = [20, 21, 22, 23, 24, 25, 26, 27, 28, 29];
  const older = (seqs: number[]) => ({ status: 200, body: { session: "claude:session-1", messages: seqs.map((seq) => message(seq)), has_more: false, next_before: null } });

  async function ask(env: Awaited<ReturnType<typeof openWith>>) {
    let finish: (r: { status: number; body: unknown }) => void = () => {};
    env.t.transport.on("/messages", () => new Promise((resolve) => (finish = resolve)));
    await scrollTo(env.scroller, 250);
    await click(button(env.t.el, "Load earlier messages"));
    return (response: { status: number; body: unknown }) => settle(() => finish(response));
  }

  it("keeps the row the person was reading where it was", async () => {
    const env = await openWith(SEQS);
    const arrive = await ask(env);
    await arrive(older([10, 11, 12]));
    // Three rows were added above, so the reader's row moved down three rows' height, and the window went with it.
    expect(env.scroller.scrollTop).toBe(250 + 3 * ROW);
  });

  it("is not moved by a reply growing at the bottom while the page is on its way", async () => {
    const env = await openWith(SEQS);
    const arrive = await ask(env);
    env.layout.extra = 50;
    await settle(() => void env.t.transport.last.send(frame("message.delta", undefined, { id: "a1", kind: "assistant", text: "Hel", seq: 1, offset: 0, done: false }, { turn_id: "t99" })));
    expect(env.scroller.scrollTop).toBe(250);
    env.layout.extra = 90;
    await arrive(older([10, 11, 12]));
    expect(env.scroller.scrollTop).toBe(250 + 3 * ROW);
  });

  it("goes by the row being read, not the first row: a row above it that changed size does not count", async () => {
    const env = await openWith(SEQS);
    const arrive = await ask(env);
    // While the page loads, the top row of what was shown grows (an image arrived); the reader is below it.
    env.layout.heights["item-20"] = 160;
    await arrive(older([10, 11, 12]));
    // Row item-22 sits at 3 rows + 160 + 100 now; it was at 200: the window moves by the difference.
    expect(env.scroller.scrollTop).toBe(250 + 3 * ROW + 60);
  });

  it("finds a work group again when older steps join its front and change its key", async () => {
    const t = setup();
    const tool = (seq: number) => [
      frame("tool.started", seq, { call_id: `c${seq}`, name: "Bash", summary: `step ${seq}` }, { turn_id: "t1" }),
      frame("tool.completed", seq + 1, { call_id: `c${seq}`, status: "ok", name: "Bash" }, { turn_id: "t1" }),
    ];
    const tail = [30, 31, 32, 33, 34, 35].map((seq) => message(seq));
    await settle(
      () =>
        void t.transport.last.send(
          snapshotFrame({ ...bodyOf("default"), messages: [...tool(20), ...tail] as never, has_more: true, next_before: 20, state: "idle", open_turn: null, head: 98 }),
          frame("session.state", 0, { state: "idle", live: [], pending: [], head: 98, epoch: 0 }),
        ),
    );
    const scroller = q(t.el, 'main[aria-label="Timeline"]') as HTMLElement;
    installLayout(scroller);
    let finish: (r: { status: number; body: unknown }) => void = () => {};
    t.transport.on("/messages", () => new Promise((resolve) => (finish = resolve)));
    await scrollTo(scroller, 50); // the work group is the row being read
    const before = q(t.el, '[data-row-kind="work"]')?.getAttribute("data-row-key");
    await click(button(t.el, "Load earlier messages"));
    // An older page: a user message, then steps of the same turn, which join the group at its front.
    await settle(() => finish({ status: 200, body: { session: "claude:session-1", messages: [frame("message.completed", 5, { id: "u", kind: "user", text: "ask" }, { turn_id: "t1" }), ...tool(10)], has_more: false, next_before: null } }));
    expect(q(t.el, '[data-row-kind="work"]')?.getAttribute("data-row-key")).not.toBe(before);
    // The user message added a row above the group: the window moves by its height, though the group's key changed.
    expect(scroller.scrollTop).toBe(50 + ROW);
  });

  it("leaves the window alone when the page brings nothing", async () => {
    const env = await openWith(SEQS);
    const arrive = await ask(env);
    await arrive({ status: 200, body: { session: "claude:session-1", messages: [], has_more: false, next_before: null } });
    expect(env.scroller.scrollTop).toBe(250);
  });
});

describe("following the end", () => {
  it("goes to the end for a new row while the person is at the end, and stays put once they scrolled up", async () => {
    const env = await openWith([20, 21, 22, 23, 24, 25], false);
    const end = () => env.scroller.scrollHeight;
    // The person has not scrolled: they are at the end.
    await settle(() => void env.t.transport.last.send(message(30)));
    expect(env.scroller.scrollTop).toBe(end());
    expect(end()).toBe(7 * ROW);

    await scrollTo(env.scroller, 100);
    expect(button(env.t.el, "Jump to latest")).toBeDefined();
    await settle(() => void env.t.transport.last.send(message(31)));
    expect(env.scroller.scrollTop).toBe(100);
  });
});

describe("opening something while following the end", () => {
  const observers: (() => void)[] = [];
  const boxes: string[] = [];

  beforeEach(() => {
    observers.length = 0;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          observers.push(callback);
        }
        observe(el: Element, options?: ResizeObserverOptions) {
          if (el.classList.contains("sv-pad-end")) boxes.push(options?.box ?? "content-box");
        }
        disconnect() {}
      },
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("watches the timeline's border box, so a taller composer (its height is the content's bottom padding) follows the end", async () => {
    const t = setup();
    await settle(() => void t.transport.last.send(...defaultFrames()));
    expect(boxes).toEqual(["border-box"]);
  });

  it("does not carry the pressed header off the top: a height the person caused is not a reason to jump to the end", async () => {
    const t = setup();
    await settle(() => void t.transport.last.send(...defaultFrames()));
    const scroller = q(t.el, 'main[aria-label="Timeline"]') as HTMLElement;
    const layout = installLayout(scroller);
    await scrollTo(scroller, scroller.scrollHeight - VIEW);

    // A change of height while following the end does follow it (this is what the observer is for).
    layout.extra = 40;
    observers.forEach((notify) => notify());
    expect(scroller.scrollTop).toBe(scroller.scrollHeight);

    // The person opens a group: the height grows, and the observer must not pin the window to the end.
    const before = scroller.scrollTop;
    await click(button(t.el, "Worked for"));
    layout.extra = 340;
    observers.forEach((notify) => notify());
    expect(scroller.scrollTop).toBe(before);
    expect(all(t.el, "button").find((b) => text(b).startsWith("Worked"))?.getAttribute("aria-expanded")).toBe("true");
  });

  it("follows the end again once the change has settled and the window says it is at the end", async () => {
    const t = setup();
    await settle(() => void t.transport.last.send(...defaultFrames()));
    const scroller = q(t.el, 'main[aria-label="Timeline"]') as HTMLElement;
    installLayout(scroller);
    await scrollTo(scroller, scroller.scrollHeight - VIEW);
    await click(button(t.el, "Worked for"));
    // The window is still at the end when the disclosure has finished animating.
    scroller.scrollTop = scroller.scrollHeight - VIEW;
    await new Promise((resolve) => setTimeout(resolve, 450));
    await settle();
    await settle(() => void t.transport.last.send(message(500)));
    expect(scroller.scrollTop).toBe(scroller.scrollHeight);
  });
});
