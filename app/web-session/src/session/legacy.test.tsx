// A session no keeper holds (the legacy path): the hub gives its send a 202 with no receipt, and its user
// message comes from the harness's transcript, which carries no command id. These tests build that
// sequence by hand: every recorded fixture is keeper-path, so none of them shows it.
import { describe, expect, it } from "vitest";

import { LEGACY_CONFIRM_MS } from "../core";
import { all, button, defaultFrames, frame, q, setup, settle, text } from "./test-harness";

/** What `protocol_compat.to_v2` makes of a v1 user `message.appended`: id `j<seq>`, no `command_id`. */
const legacyUserMessage = (seq: number, words: string) => frame("message.completed", seq, { id: `j${seq}`, kind: "user", text: words }, { turn_id: "t9" });

async function legacySend(words: string) {
  const t = setup();
  await settle(() => void t.transport.last.send(...defaultFrames()));
  // The legacy route: 202 {ok, queued, client_id, waiting}, no receipt.
  t.transport.on("/message", () => ({ status: 202, body: { ok: true, queued: false, client_id: "id1", waiting: false } }), "POST");
  await settle(async () => void (await t.store.getState().send({ text: words })));
  return t;
}

const userBubbles = (el: Element, words: string) => all(el, '[data-bubble="user"]').filter((b) => text(b).startsWith(words));

describe("a legacy send and its delivered message", () => {
  it("shows both until the send's result arrives: the message carries no command id, and nothing is matched by text", async () => {
    const t = await legacySend("one more thing");
    expect(t.store.getState().sends[0]).toMatchObject({ shown: "sent", reason: "Sent. Waiting for the session to take it." });

    await settle(() => void t.transport.last.send(legacyUserMessage(41, "one more thing")));
    // The timeline's message, and the outbox's bubble with the same words: told apart only by the id, which the message lacks.
    expect(userBubbles(t.el, "one more thing")).toHaveLength(2);
    const bubble = q(t.el, '[data-send="sent"]') as HTMLElement;
    expect(text(bubble)).toContain("Sent. Waiting for the session to take it.");
    // Nothing to act on while it is only sent: no Retry beside a delivered message.
    expect(all(bubble, "button")).toHaveLength(0);

    // The send's result (v2 serves a legacy result's client_id as command_id) takes the entry away: one message.
    await settle(() => void t.transport.last.send(frame("command.result", 42, { op: "send", ok: true }, { command_id: "id1" })));
    expect(t.store.getState().sends).toEqual([]);
    expect(userBubbles(t.el, "one more thing")).toHaveLength(1);
    expect(q(t.el, "[data-send]")).toBeNull();
  });

  it("keeps a pending send whose words match a message typed in the terminal", async () => {
    const t = await legacySend("run the tests");
    await settle(() => void t.transport.last.send(legacyUserMessage(41, "run the tests")));
    expect(all(t.el, "[data-send]")).toHaveLength(1);
  });

  it("offers Retry beside the delivered message when the result is lost and the send ends unconfirmed", async () => {
    const t = await legacySend("one more thing");
    await settle(() => void t.transport.last.send(legacyUserMessage(41, "one more thing")));
    await settle(() => t.timers.advance(LEGACY_CONFIRM_MS + 1000));
    const bubble = q(t.el, '[data-send="unconfirmed"]') as HTMLElement;
    expect(bubble).not.toBeNull();
    expect(text(bubble)).toContain("never said whether it reached the session");
    expect(button(bubble, "Retry")).toBeDefined();
    // The message is still there, once, above it: Retry would send it again (the spec's Limit).
    expect(userBubbles(t.el, "one more thing")).toHaveLength(2);
  });

  it("does hide a keeper-path send by its command id, even with the same words sent twice", async () => {
    const t = setup();
    await settle(() => void t.transport.last.send(...defaultFrames()));
    t.transport.on("/message", () => ({ status: 202, body: { receipt: "queued" } }), "POST");
    await settle(async () => void (await t.store.getState().send({ text: "same" })));
    await settle(async () => void (await t.store.getState().send({ text: "same" })));
    const [first] = t.store.getState().sends;
    await settle(() => void t.transport.last.send(frame("message.completed", 41, { id: "m41", kind: "user", text: "same" }, { turn_id: "t9", command_id: first?.commandId })));
    expect(all(t.el, "[data-send]").map((b) => b.getAttribute("data-command-id"))).toEqual(["id2"]);
  });
});
