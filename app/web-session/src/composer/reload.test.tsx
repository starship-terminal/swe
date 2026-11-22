// One decision per request survives a reload, a second tab and storage that is off (posted.ts).
import { describe, expect, it, vi } from "vitest";

import { answer, epochOf } from "./answer";
import { clearAllDrafts } from "./draft";
import { feed, frame, KEY, mount, off, play, posts, q, all, settle, text, type Mounted } from "./harness";
import { forgetMemoryPosted, POSTED_MAX_AGE_MS, readPosted, writePosted } from "./posted";

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const ready = () => settle(() => wait(120));
const STORAGE_KEY = `swe.sv.answer.v1:${KEY}`;
const at = (m: Mounted, requestId = "r1", seq = 8) => ({ requestId, seq, epoch: epochOf(m.store) });
/** The key of the open request in a session's stored record. */
const idOf = (m: Mounted, requestId = "r1", seq = 8) => `${epochOf(m.store)}\u0000${requestId}\u0000${seq}`;
const held = () => ({ status: 202, body: { ok: true, receipt: "held", command_id: "x" } });
const accepted = () => ({ status: 200, body: { ok: true, receipt: "accepted", command_id: "x" } });

async function open(reply: () => object | Promise<object> = accepted): Promise<Mounted> {
  const m = mount();
  m.transport.on("/approval", reply as never, "POST");
  await play(m, "approval", 0, 2);
  await ready();
  return m;
}

function press(el: Element | undefined | null): Promise<void> {
  if (el === undefined || el === null) throw new Error("nothing to press");
  return settle(() => {
    el.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
  });
}

const dock = (m: Mounted) => q(m.el, "[data-dock]") as HTMLElement;
const opt = (m: Mounted, id: string) => q(m.el, `[data-option="${id}"]`) as HTMLButtonElement;
const status = (m: Mounted) => text(q(m.el, "[data-dock-status]"));
const kept = (): Record<string, Record<string, unknown>> => JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}");

/** A reload: the page's memory is gone, storage stays, and the store, the dock and the stream are new. */
async function reload(m: Mounted, reply: () => object | Promise<object> = accepted): Promise<Mounted> {
  void m;
  forgetMemoryPosted();
  return open(reply);
}

describe("what was answered survives a reload", () => {
  it("keeps a held answer: after the reload the dock says so and no control can post Deny", async () => {
    const first = await open(held);
    await press(opt(first, "allow"));
    expect(status(first)).toContain("Your answer waits for that machine to wake.");
    const second = await reload(first);
    expect(status(second)).toContain("You answered: Allow.");
    expect(status(second)).toContain("Your answer waits for that machine to wake.");
    expect(all(dock(second), "button[data-option]").every(off)).toBe(true);
    for (const id of ["deny", "always", "allow"]) await press(opt(second, id));
    expect(posts(second, "approval")).toHaveLength(0);
    expect(await answer(second.store, KEY, at(second), "deny", "Deny")).toMatchObject({ ok: false, reason: "decided" });
    expect(await answer(second.store, KEY, at(second), "allow", "Allow")).toMatchObject({ ok: false, reason: "decided" });
    expect(posts(second, "approval")).toHaveLength(0);
  });

  it("keeps an answer that was in doubt: the same one can be sent again, no other", async () => {
    const first = await open(() => Promise.reject(new Error("no answer")));
    await press(opt(first, "deny"));
    expect(status(first)).toContain("We could not tell whether your answer (Deny) reached the hub.");
    const second = await reload(first);
    expect(status(second)).toContain("We could not tell whether your answer (Deny) reached the hub.");
    expect(all(dock(second), "button[data-option]").every(off)).toBe(true);
    expect(await answer(second.store, KEY, at(second), "allow", "Allow")).toMatchObject({ ok: false, reason: "decided" });
    expect(text(q(dock(second), "[data-resend]"))).toBe("Send the same answer again");
    await settle(() => void (q(dock(second), "[data-resend]") as HTMLButtonElement).click());
    expect(posts(second, "approval").map((p) => p.decision)).toEqual(["deny"]);
    expect(status(second)).toContain("You answered: Deny.");
  });

  it("finds a post that was still on its way when the page went: it is in doubt, not forgotten", async () => {
    const first = await open(() => new Promise<never>(() => {}));
    await press(opt(first, "allow")); // never answered: the page is reloaded now
    expect(kept()[Object.keys(kept())[0] as string]).toMatchObject({ d: "allow", o: "unknown" });
    const second = await reload(first);
    expect(status(second)).toContain("We could not tell whether your answer (Allow) reached the hub.");
    expect(await answer(second.store, KEY, at(second), "deny", "Deny")).toMatchObject({ ok: false, reason: "decided" });
  });

  it("holds only the decision, the label shown, the outcome, the receipt and the time", async () => {
    const m = await open(held);
    await press(opt(m, "allow"));
    const entries = Object.values(kept());
    expect(entries).toHaveLength(1);
    expect(Object.keys(entries[0] as object).sort()).toEqual(["a", "d", "l", "o", "r"]);
    expect(entries[0]).toMatchObject({ d: "allow", l: "Allow", o: "taken", r: "held" });
    expect(JSON.stringify(kept())).not.toContain("rm -rf");
  });

  it("is kept per session, not for every session", async () => {
    const m = await open();
    await press(opt(m, "allow"));
    expect(readPosted("claude:other", at(m))).toBeUndefined();
    expect(readPosted(KEY, at(m))?.decision).toBe("allow");
  });

  it("brings the answers back after a definite refusal, and forgets the entry (nothing reached the hub)", async () => {
    const m = await open(() => ({ status: 409, body: { error: { code: "approval_failed", message: "that request is no longer open" } } }));
    await press(opt(m, "allow"));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(all(dock(m), "button[data-option]").some(off)).toBe(false);
  });
});

describe("what is kept stays small and true", () => {
  it("says the post is on its way, not that it is in doubt, while it is", async () => {
    const m = await open(() => new Promise<never>(() => {}));
    await press(opt(m, "allow"));
    expect(status(m)).toBe("Sending your answer.");
    expect(q(dock(m), "[data-resend]")).toBeNull();
  });

  it("keeps a doubt a refused resend did not settle", async () => {
    let calls = 0;
    const m = await open(() => {
      calls += 1;
      return calls === 1 ? Promise.reject(new Error("no answer")) : { status: 409, body: { error: { code: "approval_failed", message: "that request is no longer open" } } };
    });
    await press(opt(m, "deny"));
    await settle(() => void (q(dock(m), "[data-resend]") as HTMLButtonElement).click());
    expect(calls).toBe(2);
    expect(kept()[Object.keys(kept())[0] as string]).toMatchObject({ d: "deny", o: "unknown" });
    expect(all(dock(m), "button[data-option]").every(off)).toBe(true);
    expect(await answer(m.store, KEY, at(m), "allow", "Allow")).toMatchObject({ ok: false, reason: "decided" });
  });

  it("lets a dock in the same page learn of an answer at once", async () => {
    const one = await open(held);
    const two = await open(held);
    await press(opt(one, "allow"));
    expect(status(two)).toContain("You answered: Allow.");
  });

  it("forgets an entry of this page's memory that is older than the limit", async () => {
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    try {
      const m = await open(held);
      await press(opt(m, "allow"));
      expect(readPosted(KEY, at(m))?.decision).toBe("allow");
      expect(readPosted(KEY, at(m), Date.now() + POSTED_MAX_AGE_MS + 1000)).toBeUndefined();
    } finally {
      set.mockRestore();
    }
  });

  it("drops the entry of a request that is not open, from storage, when the page opens", async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ [`e\u0000old\u00001`]: { d: "allow", l: "Allow", o: "taken", a: Date.now() - POSTED_MAX_AGE_MS - 5000 } }));
    const m = await open();
    await press(opt(m, "allow"));
    expect(Object.keys(kept())).toEqual([idOf(m)]);
  });

  it("ignores an entry whose outcome is not one this view writes", async () => {
    const first = await open();
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ [idOf(first)]: { d: "allow", l: "Allow", o: "bogus", a: Date.now() } }));
    const m = await reload(first);
    expect(status(m)).toBe("You can answer now.");
    await press(opt(m, "deny")); // nothing is known of an answer, so any decision goes
    expect(posts(m, "approval").map((p) => p.decision)).toEqual(["deny"]);
  });

  it("keeps at most forty entries for a session, the newest", async () => {
    for (let i = 0; i < 45; i++) writePosted(KEY, { requestId: `q${i}`, seq: i, epoch: "e" }, { decision: "allow", label: "Allow", outcome: "taken" });
    const entries = Object.keys(kept());
    expect(entries).toHaveLength(40);
    expect(entries).toContain("e\u0000q44\u000044");
    expect(entries).not.toContain("e\u0000q0\u00000");
  });

  it("cuts a label to two hundred characters", () => {
    writePosted(KEY, { requestId: "r1", seq: 8, epoch: "e" }, { decision: "1", label: "x".repeat(500), outcome: "taken" });
    expect((Object.values(kept())[0] as { l: string }).l).toHaveLength(200);
  });
});

describe("the review's findings", () => {
  it("does not prune while a reconnect is still replaying: only a caught-up list is complete", async () => {
    const m = await open(held);
    await press(opt(m, "allow"));
    expect(Object.keys(kept())).toHaveLength(1);
    const session = m.store.getState().session;
    // A connection opened again: nothing has caught up, and the replay has given none of the open requests yet.
    await settle(() => m.store.setState({ session: { ...session, caughtUp: false, pending: [] } }));
    expect(Object.keys(kept())).toHaveLength(1);
    // It catches up with the request still open: the record stays and the dock knows it.
    await settle(() => m.store.setState({ session: { ...session, caughtUp: true } }));
    expect(Object.keys(kept())).toHaveLength(1);
    expect(status(m)).toContain("You answered: Allow.");
  });

  it("is a new request, not the old answer, when the journal's epoch changed and the id and seq came back", async () => {
    const m = await open(held);
    await press(opt(m, "allow"));
    const old = at(m);
    const session = m.store.getState().session;
    await settle(() => m.store.setState({ session: { ...session, epoch: "after-a-reset" as never } }));
    // A request that comes after a reset is a new one: its answers are off for a moment, as for any request that appears.
    expect(status(m)).toBe("A new request just appeared. Read it before you answer.");
    await ready();
    expect(status(m)).toBe("You can answer now.");
    expect(all(dock(m), "button[data-option]").some(off)).toBe(false);
    // An answer made for the old epoch is not sent to the new request.
    expect(await answer(m.store, KEY, old, "allow", "Allow")).toMatchObject({ ok: false, reason: "changed" });
    expect(posts(m, "approval")).toHaveLength(1);
    // The new request can be answered, and its record is its own.
    await press(opt(m, "deny"));
    expect(posts(m, "approval").map((p) => p.decision)).toEqual(["allow", "deny"]);
  });

  it("writes nothing for a post that was on its way when the device signed out", async () => {
    let release: (value: object) => void = () => {};
    const m = await open(() => new Promise<object>((resolve) => (release = resolve)));
    await press(opt(m, "allow"));
    expect(Object.keys(kept())).toHaveLength(1);
    await settle(() => clearAllDrafts());
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    await settle(() => release(accepted()));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readPosted(KEY, at(m))).toBeUndefined();
  });

  it("restores nothing after a refusal that comes after the device signed out", async () => {
    let release: (value: object) => void = () => {};
    const m = await open(() => new Promise<object>((resolve) => (release = resolve)));
    await press(opt(m, "allow"));
    await settle(() => clearAllDrafts());
    await settle(() => release({ status: 409, body: { error: { code: "approval_failed", message: "no" } } }));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readPosted(KEY, at(m))).toBeUndefined();
  });

  it("keeps a restored doubt from coming back after sign-out, when a resend is refused late", async () => {
    let calls = 0;
    let release: (value: object) => void = () => {};
    const m = await open(() => {
      calls += 1;
      return calls === 1 ? Promise.reject(new Error("no answer")) : new Promise<object>((resolve) => (release = resolve));
    });
    await press(opt(m, "deny"));
    await settle(() => void (q(dock(m), "[data-resend]") as HTMLButtonElement).click());
    await settle(() => clearAllDrafts());
    await settle(() => release({ status: 409, body: { error: { code: "approval_failed", message: "no" } } }));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readPosted(KEY, at(m))).toBeUndefined();
  });

  it("takes the newer copy in memory when the receipt could not be written", async () => {
    const m = await open(held);
    let writes = 0;
    const real = Storage.prototype.setItem;
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, name: string, value: string) {
      if (name === STORAGE_KEY) {
        writes += 1;
        if (writes > 1) throw new Error("quota");
      }
      return real.call(this, name, value);
    });
    try {
      await press(opt(m, "allow"));
      expect(kept()[idOf(m)]).toMatchObject({ o: "unknown" }); // what storage holds: the first write
      expect(status(m)).toContain("You answered: Allow. Your answer waits for that machine to wake.");
      expect(q(dock(m), "[data-resend]")).toBeNull();
    } finally {
      set.mockRestore();
    }
  });
});

describe("what was answered is dropped", () => {
  it("when its request is no longer open", async () => {
    const m = await open();
    await press(opt(m, "allow"));
    expect(Object.keys(kept())).toHaveLength(1);
    await feed(m, frame("approval.resolved", 12, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readPosted(KEY, at(m))).toBeUndefined();
  });

  it("but not before the session has loaded: nothing is open yet, and the record must still be there when it is", async () => {
    const first = await open(held);
    await press(opt(first, "allow"));
    forgetMemoryPosted();
    const second = mount(); // no frames yet
    await ready();
    expect(Object.keys(kept())).toHaveLength(1);
    expect(dock(second)).toBeNull();
    await play(second, "approval", 0, 2);
    await ready();
    expect(status(second)).toContain("You answered: Allow.");
  });

  it("when it is older than the age limit", async () => {
    const first = await open(held);
    await press(opt(first, "allow"));
    const row = kept();
    const key = Object.keys(row)[0] as string;
    (row[key] as { a: number }).a = Date.now() - POSTED_MAX_AGE_MS - 1000;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(row));
    const second = await reload(first);
    expect(status(second)).toBe("You can answer now.");
    expect(all(dock(second), "button[data-option]").some(off)).toBe(false);
  });

  it("when the device signs out, with the drafts", async () => {
    const m = await open(held);
    await press(opt(m, "allow"));
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull();
    await settle(() => clearAllDrafts());
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readPosted(KEY, at(m))).toBeUndefined();
    // The open dock draws again and finds nothing.
    expect(status(m)).toBe("You can answer now.");
  });

  it("and is ignored when it is not what this view wrote", async () => {
    const first = await open();
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ [idOf(first)]: { d: 1, l: "x", o: "taken", a: Date.now() }, junk: "x" }));
    const m = await reload(first);
    expect(status(m)).toBe("You can answer now.");
    window.localStorage.setItem(STORAGE_KEY, "not json");
    const again = await reload(m);
    expect(status(again)).toBe("You can answer now.");
  });
});

describe("two tabs", () => {
  it("a second tab's open dock learns of an answer through the storage event and cannot post another", async () => {
    const one = await open(held);
    const two = await open(held);
    expect(status(two)).toBe("You can answer now.");
    await press(opt(one, "allow"));
    // What another window's storage event is: the entry is in storage, and the event names the key.
    forgetMemoryPosted();
    await settle(() => void window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY })));
    expect(status(two)).toContain("You answered: Allow.");
    expect(all(dock(two), "button[data-option]").every(off)).toBe(true);
    await press(opt(two, "deny"));
    expect(posts(two, "approval")).toHaveLength(0);
    expect(await answer(two.store, KEY, at(two), "deny", "Deny")).toMatchObject({ ok: false, reason: "decided" });
  });

  it("an event for another key changes nothing", async () => {
    const m = await open(held);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ [idOf(m)]: { d: "allow", l: "Allow", o: "taken", r: "held", a: Date.now() } }));
    await settle(() => void window.dispatchEvent(new StorageEvent("storage", { key: "something.else" })));
    expect(status(m)).toBe("You can answer now.");
    await settle(() => void window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY })));
    expect(status(m)).toContain("You answered: Allow.");
  });
});

describe("storage that is off", () => {
  it("keeps the record in memory for as long as the page lives, and says nothing more than it knows", async () => {
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    try {
      const m = await open(held);
      await press(opt(m, "allow"));
      expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
      m.remount(); // the dock is drawn again, the page lives
      await ready();
      expect(status(m)).toContain("You answered: Allow.");
      expect(await answer(m.store, KEY, at(m), "deny", "Deny")).toMatchObject({ ok: false, reason: "decided" });
      // A reload loses it: the limit the spec states.
      forgetMemoryPosted();
      const again = await open(held);
      expect(status(again)).toBe("You can answer now.");
    } finally {
      set.mockRestore();
    }
  });

  it("works when storage cannot be read at all", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    try {
      const m = await open(held);
      await press(opt(m, "allow"));
      expect(status(m)).toContain("You answered: Allow.");
    } finally {
      get.mockRestore();
    }
  });
});
