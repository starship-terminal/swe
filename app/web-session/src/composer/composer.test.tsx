// The message box: one press sends once, and the draft is never lost.
import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { all, box, btn, click, feed, frame, idle, key, KEY, mount, play, posts, q, settle, snapshotWith, text, typeText } from "./harness";
import { readDraft, writeDraft } from "./draft";
import { SAVE_AFTER_MS } from "./useDraft";

const sent = (m: ReturnType<typeof mount>) => posts(m, "message");
const hold = () => vi.fn(() => new Promise<never>(() => {}));
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("sending", () => {
  it("posts the message once under one id, clears the box and the kept draft, and keeps the focus", async () => {
    const m = mount();
    await idle(m);
    document.body.append(m.el);
    typeText(m, "Please fix the build");
    await click(btn(m, "Send message"));
    expect(sent(m)).toHaveLength(1);
    expect(sent(m)[0]).toMatchObject({ text: "Please fix the build", mode: "queue", command_id: "id1", client_id: "id1" });
    expect(box(m).value).toBe("");
    expect(readDraft(KEY).text).toBe("");
    expect(window.localStorage.length).toBe(0);
    expect(document.activeElement).toBe(box(m));
  });

  it("sends once when the button is pressed twice in the same moment, without a complaint", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "double");
    const send = btn(m, "Send message") as HTMLButtonElement;
    await settle(() => {
      send.click();
      send.click();
    });
    expect(sent(m)).toHaveLength(1);
    expect(m.store.getState().outbox.entries).toHaveLength(1);
    expect(q(m.el, '[data-composer-reason="error"]')).toBeNull();
  });

  it("sends once on Enter, and a held Enter does not send again or type a blank line", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "enter");
    const first = key(m, { key: "Enter", keyCode: 13 });
    expect(first.defaultPrevented).toBe(true);
    await settle();
    typeText(m, "next draft");
    const held = key(m, { key: "Enter", keyCode: 13, repeat: true });
    expect(held.defaultPrevented).toBe(true);
    await settle();
    expect(sent(m)).toHaveLength(1);
    expect(box(m).value).toBe("next draft");
  });

  it("leaves Shift+Enter to the box for a new line", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "a");
    const event = key(m, { key: "Enter", keyCode: 13, shiftKey: true });
    expect(event.defaultPrevented).toBe(false);
    expect(sent(m)).toHaveLength(0);
  });

  it("does not send on the Enter that ends an input method composition", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "にほん");
    // Chrome and Firefox: the key press says it is part of a composition.
    expect(key(m, { key: "Enter", keyCode: 13, isComposing: true }).defaultPrevented).toBe(false);
    // Safari: the composition has ended, and the key press says keyCode 229.
    expect(key(m, { key: "Enter", keyCode: 229 }).defaultPrevented).toBe(false);
    // The box tracks the composition itself too.
    act(() => void box(m).dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
    expect(key(m, { key: "Enter", keyCode: 13 }).defaultPrevented).toBe(false);
    await settle();
    expect(sent(m)).toHaveLength(0);
    act(() => void box(m).dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })));
    key(m, { key: "Enter", keyCode: 13 });
    await settle();
    expect(sent(m)).toHaveLength(1);
  });

  it("sends once when the form is submitted again by a re-render's replay (strict mode) or by a held key", async () => {
    const m = mount({ strict: true });
    await idle(m);
    const spy = hold();
    m.store.setState({ send: spy });
    typeText(m, "strict");
    const form = q(m.el, "form") as HTMLFormElement;
    await settle(() => {
      form.requestSubmit();
      form.requestSubmit();
    });
    await feed(m, frame("ping", undefined, { ts: 1, head: 1 })); // a re-render while the send is out
    await settle(() => form.requestSubmit());
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("refuses a press made while the same message is on its way from another instance of the box", async () => {
    const m = mount();
    await idle(m);
    const spy = hold();
    m.store.setState({ send: spy });
    typeText(m, "remount");
    await click(btn(m, "Send message"));
    m.remount(); // the box is drawn again, as when the view remounts, and finds the draft kept
    expect(box(m).value).toBe("remount");
    await click(btn(m, "Send message"));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("Still sending your last message.");
  });

  it("locks the box while the message is being saved, with the draft in it", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: hold() });
    typeText(m, "saving");
    await click(btn(m, "Send message"));
    expect(box(m).readOnly).toBe(true);
    expect(box(m).value).toBe("saving");
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(true);
  });

  it("does not send while the outbox is loading, and keeps the draft", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "early");
    act(() => m.store.setState({ outbox: { ...m.store.getState().outbox, loading: true } }));
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(true);
    expect(text(q(m.el, '[data-composer-reason="blocked"]'))).toBe("Loading the sends kept on this device.");
    // The keyboard and the form bypass the button: the outbox itself refuses.
    key(m, { key: "Enter", keyCode: 13 });
    await settle(() => void (q(m.el, "form") as HTMLFormElement).requestSubmit());
    expect(sent(m)).toHaveLength(0);
    expect(m.store.getState().outbox.entries).toHaveLength(0);
    expect(box(m).value).toBe("early");
  });
});

describe("the draft", () => {
  it("stays in the box, in storage and on screen when the send is refused", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: () => Promise.resolve({ ok: false, reason: "gone", message: "This session no longer exists." } as never) });
    typeText(m, "do not lose me");
    await click(btn(m, "Send message"));
    expect(box(m).value).toBe("do not lose me");
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("This session no longer exists.");
    await wait(SAVE_AFTER_MS + 50);
    expect(readDraft(KEY).text).toBe("do not lose me");
    expect(box(m).readOnly).toBe(false);
  });

  it("stays when the store throws", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: () => Promise.reject(new Error("storage exploded")) });
    typeText(m, "still here");
    await click(btn(m, "Send message"));
    expect(box(m).value).toBe("still here");
    expect(text(q(m.el, '[data-composer-reason="error"]'))).toBe("storage exploded");
  });

  it("is written soon after typing, and comes back after a reload", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "half a thought");
    await wait(SAVE_AFTER_MS + 50);
    expect(window.localStorage.getItem(`swe.sv.draft.v1:${KEY}`)).toContain("half a thought");
    const again = mount(); // a new page: a new store and a new box, the same browser storage
    await idle(again);
    expect(box(again).value).toBe("half a thought");
  });

  it("is written when the box goes away, before the pause is over", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "just typed");
    act(() => void window.dispatchEvent(new Event("pagehide")));
    expect(readDraft(KEY).text).toBe("just typed");
  });

  it("is written when the box is taken off the page before the pause is over", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "typed just now");
    act(() => void m.el.ownerDocument.defaultView?.dispatchEvent(new Event("noop")));
    m.remount(); // unmounts the old box at once
    expect(readDraft(KEY).text).toBe("typed just now");
  });

  it("is still there after the connection drops and comes back", async () => {
    const m = mount();
    await idle(m);
    typeText(m, "keep this across a reconnect");
    await settle(() => m.environment.goOffline());
    await settle(() => m.environment.goOnline());
    await settle(() => m.timers.advance(60_000));
    expect(box(m).value).toBe("keep this across a reconnect");
  });

  it("says when it cannot be kept on this device", async () => {
    const m = mount();
    await idle(m);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    typeText(m, "no room");
    act(() => void window.dispatchEvent(new Event("pagehide")));
    await settle();
    expect(text(q(m.el, "[data-draft-note]"))).toContain("will not survive a reload");
    vi.restoreAllMocks();
  });

  it("is gone from storage after a send even when the stored copy was older than the box", async () => {
    const m = mount();
    await idle(m);
    writeDraft(KEY, { text: "older", files: [] }); // what the pause timer had written so far
    typeText(m, "newer words"); // not written yet
    await click(btn(m, "Send message"));
    expect(readDraft(KEY).text).toBe("");
    await wait(SAVE_AFTER_MS + 50);
    expect(readDraft(KEY).text).toBe("");
  });
});

describe("while a reply is in progress", () => {
  it("offers Stop, Stop and send, and Queue; and nothing of the kind when idle", async () => {
    const idleOne = mount();
    await idle(idleOne);
    expect(btn(idleOne, "Stop the reply")).toBeUndefined();
    expect(btn(idleOne, "Stop and send")).toBeUndefined();
    expect(btn(idleOne, "Send message")).toBeDefined();
    const m = mount();
    await play(m, "streaming", 0, 3);
    expect(btn(m, "Stop the reply")).toBeDefined();
    expect(btn(m, "Stop and send")).toBeDefined();
    expect(btn(m, "Queue message")).toBeDefined();
    expect(box(m).placeholder).toBe("Queue a message for when the reply ends");
  });

  it("queues with the main button and steers with Stop and send, in the mode each says", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    typeText(m, "after");
    await click(btn(m, "Queue message"));
    typeText(m, "now");
    await click(btn(m, "Stop and send"));
    expect(sent(m).map((p) => p["mode"])).toEqual(["queue", "steer"]);
  });

  it("says what Stop and send does", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    expect(btn(m, "Stop and send")?.title).toBe("Stops the current reply, then sends this message now.");
  });

  it("disables Stop and send, with the reason, on an agent that cannot steer", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    await feed(m, frame("session.caps", 40, { caps: { steer: false, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] } }));
    typeText(m, "hello");
    expect((btn(m, "Stop and send") as HTMLButtonElement).disabled).toBe(true);
    expect(text(q(m.el, "[data-composer-reason]"))).toContain("cannot take a message while it is replying");
    expect((btn(m, "Queue message") as HTMLButtonElement).disabled).toBe(false);
  });

  it("stops once on a double press", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    m.transport.on("/control", () => ({ status: 200, body: { receipt: "accepted" } }), "POST");
    const stop = btn(m, "Stop the reply") as HTMLButtonElement;
    await settle(() => {
      stop.click();
      stop.click();
    });
    expect(posts(m, "control")).toHaveLength(1);
    expect(posts(m, "control")[0]).toMatchObject({ op: "interrupt" });
    expect(text(btn(m, "Stop the reply"))).toBe("Stopping");
    // At phone width Stop is an icon with the name `aria-label` gives; its word shows from the small breakpoint up.
    expect(btn(m, "Stop the reply")?.querySelector("span.hidden")).not.toBeNull();
  });

  it("disables Stop, with the reason, when the connection is down", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    await settle(() => m.environment.goOffline());
    expect((btn(m, "Stop the reply") as HTMLButtonElement).disabled).toBe(true);
    expect(btn(m, "Stop the reply")?.title).toBe("You are offline. Reconnect to do this.");
    await click(btn(m, "Stop the reply"));
    expect(posts(m, "control")).toHaveLength(0);
  });
});

describe("offline, storage and read-only", () => {
  it("accepts a message while offline and says in the timeline it waits", async () => {
    const m = mount();
    await idle(m);
    await settle(() => m.environment.goOffline());
    typeText(m, "typed offline");
    await click(btn(m, "Send message"));
    expect(sent(m)).toHaveLength(0);
    const view = m.store.getState().sends[0];
    expect(view?.shown).toBe("waiting");
    expect(view?.reason).toBe("Waiting: you are offline. It will be sent when you are back.");
    expect(box(m).value).toBe("");
  });

  it("shows the outbox's note when storage will not survive a reload", async () => {
    const m = mount();
    await idle(m);
    expect(text(q(m.el, "[data-outbox-note]"))).toBe("no database in tests.");
  });

  it("is replaced by a bar with the reason when the session cannot be driven", async () => {
    const m = mount();
    await settle(() => void m.transport.last.send(...snapshotWith("default", { row: { ...(JSON.parse("{}") as object), key: KEY, control: "read_only", control_reason: "Started in a terminal." } })));
    expect(q(m.el, "textarea")).toBeNull();
    expect(text(q(m.el, '[data-composer="read-only"]'))).toBe("Started in a terminal.");
  });
});

describe("attachments", () => {
  const fileInput = (m: ReturnType<typeof mount>) => q(m.el, 'input[type="file"]') as HTMLInputElement;
  const choose = async (m: ReturnType<typeof mount>, ...names: string[]) => {
    const files = names.map((name) => new File(["abc"], name, { type: "text/plain" }));
    Object.defineProperty(fileInput(m), "files", { value: files, configurable: true });
    await settle(() => void fileInput(m).dispatchEvent(new Event("change", { bubbles: true })));
    await settle();
  };

  it("stages a file, shows it, and sends its ref with the message", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ref: "1-a-notes.txt", name: "notes.txt", size: 3 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const m = mount();
    await idle(m);
    await choose(m, "notes.txt");
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0]?.[0]).toBe("/v1/sessions/claude%3Asession-1/attachments?name=notes.txt");
    expect(calls[0]?.[1].method).toBe("POST");
    expect(text(q(m.el, '[data-file="1-a-notes.txt"]'))).toBe("notes.txt");
    await click(btn(m, "Send message"));
    expect(sent(m)[0]).toMatchObject({ text: "", attachments: ["1-a-notes.txt"] });
    expect(all(m.el, "[data-file]")).toHaveLength(0);
    vi.unstubAllGlobals();
  });

  it("says why an upload failed, and does not stop a message without it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { message: "unsupported file type .exe" } }), { status: 422 })));
    const m = mount();
    await idle(m);
    await choose(m, "tool.exe");
    expect(text(q(m.el, '[data-upload="failed"]'))).toContain("unsupported file type .exe");
    typeText(m, "no file then");
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("holds the send back until an upload has finished", async () => {
    let finish: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (finish = resolve))));
    const m = mount();
    await idle(m);
    typeText(m, "with a file");
    await choose(m, "slow.txt");
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(true);
    expect(text(q(m.el, '[data-composer-reason="blocked"]'))).toBe("Waiting for the attachment to upload.");
    await settle(() => finish(new Response(JSON.stringify({ ref: "1-a-slow.txt", name: "slow.txt", size: 3 }), { status: 200 })));
    expect((btn(m, "Send message") as HTMLButtonElement).disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("keeps staged files in the draft across a reload", async () => {
    writeDraft(KEY, { text: "with files", files: [{ ref: "1-a-x.txt", name: "x.txt", size: 3 }] });
    const m = mount();
    await idle(m);
    expect(text(q(m.el, '[data-file="1-a-x.txt"]'))).toBe("x.txt");
  });
});
