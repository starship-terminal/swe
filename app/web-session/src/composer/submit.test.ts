import { describe, expect, it } from "vitest";

import { settle, mount, idle, play, feed, frame } from "./harness";
import { isSending, MAX_MESSAGE_CHARS, submitMessage, turnOpen } from "./submit";

describe("submitMessage", () => {
  it("sends the text as typed, minus the blank space at its end, in the outbox", async () => {
    const m = mount();
    await idle(m);
    const result = await submitMessage(m.store, { text: "  fix it\n\n", attachments: [], mode: "queue" });
    expect(result.ok).toBe(true);
    await settle();
    expect(m.transport.posts("/message")).toHaveLength(1);
    expect(m.transport.posts("/message")[0]).toMatchObject({ text: "  fix it", mode: "queue", attachments: [] });
  });

  it("sends a message once when it is asked twice in the same moment", async () => {
    const m = mount();
    await idle(m);
    const first = submitMessage(m.store, { text: "once", attachments: [], mode: "queue" });
    const second = submitMessage(m.store, { text: "once", attachments: [], mode: "queue" });
    expect(isSending(m.store)).toBe(true);
    const [a, b] = await Promise.all([first, second]);
    await settle();
    expect([a.ok, b.ok]).toEqual([true, false]);
    expect(b).toMatchObject({ ok: false, reason: "busy" });
    expect(m.store.getState().outbox.entries).toHaveLength(1);
    expect(isSending(m.store)).toBe(false);
  });

  it("refuses an empty message, and one too long, without touching the store", async () => {
    const m = mount();
    await idle(m);
    expect(await submitMessage(m.store, { text: " \n ", attachments: [], mode: "queue" })).toMatchObject({ ok: false, reason: "empty" });
    expect(await submitMessage(m.store, { text: "x".repeat(MAX_MESSAGE_CHARS + 1), attachments: [], mode: "queue" })).toMatchObject({ ok: false, reason: "too_long" });
    expect(m.store.getState().outbox.entries).toHaveLength(0);
  });

  it("sends attachments with no words", async () => {
    const m = mount();
    await idle(m);
    expect((await submitMessage(m.store, { text: "", attachments: ["1-a-b.png"], mode: "queue" })).ok).toBe(true);
    await settle();
    expect(m.transport.posts("/message")[0]).toMatchObject({ text: "", attachments: ["1-a-b.png"] });
  });

  it("does not send while the outbox is still reading what the device kept", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ outbox: { ...m.store.getState().outbox, loading: true } });
    const result = await submitMessage(m.store, { text: "too early", attachments: [], mode: "queue" });
    expect(result).toMatchObject({ ok: false, reason: "loading" });
    expect(m.store.getState().outbox.entries).toHaveLength(0);
    expect(m.transport.posts("/message")).toHaveLength(0);
  });

  it("answers with the store's refusal, and leaves the rest to the caller", async () => {
    const m = mount();
    await idle(m);
    const row = { ...(m.store.getState().session.row as object), control: "read_only", control_reason: "Started in a terminal" };
    await feed(m, { v: 2, type: "session.row", session: "claude:session-1", ts: 1, data: { row } } as never);
    const result = await submitMessage(m.store, { text: "hello", attachments: [], mode: "queue" });
    expect(result).toMatchObject({ ok: false, reason: "refused", message: "Started in a terminal" });
  });

  it("gives the error and sends nothing when the store throws", async () => {
    const m = mount();
    await idle(m);
    m.store.setState({ send: () => Promise.reject(new Error("storage exploded")) });
    const result = await submitMessage(m.store, { text: "hello", attachments: [], mode: "queue" });
    expect(result).toMatchObject({ ok: false, reason: "error", message: "storage exploded" });
    expect(isSending(m.store)).toBe(false);
  });

  it("sends a steer as a plain message when the turn it would stop has ended", async () => {
    const m = mount();
    await idle(m); // no turn open
    expect(turnOpen(m.store)).toBe(false);
    await submitMessage(m.store, { text: "now", attachments: [], mode: "steer" });
    await settle();
    expect(m.transport.posts("/message")[0]).toMatchObject({ mode: "queue" });
  });

  it("keeps a steer a steer while a turn is open", async () => {
    const m = mount();
    await play(m, "streaming", 0, 3);
    expect(turnOpen(m.store)).toBe(true);
    await submitMessage(m.store, { text: "now", attachments: [], mode: "steer" });
    await settle();
    expect(m.transport.posts("/message")[0]).toMatchObject({ mode: "steer" });
    void frame;
  });
});
