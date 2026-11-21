import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import type { SendShown, SendView } from "../core";
import { SendBubble } from "./Bubbles";
import { sentence } from "./format";
import { all, text } from "./test-harness";

function view(shown: SendShown, over: Partial<SendView> = {}): SendView {
  return { commandId: "c1", text: "hello", attachments: [], mode: "queue", createdAt: 0, shown, reason: "A reason.", canRetry: false, canDiscard: false, ...over };
}

async function draw(send: SendView, note?: string) {
  const el = document.createElement("div");
  document.body.append(el);
  const root = createRoot(el);
  const onDiscard = vi.fn();
  const onRetry = vi.fn();
  await act(async () => root.render(<SendBubble note={note} onDiscard={onDiscard} onRetry={onRetry} send={send} />));
  return { el, onDiscard, onRetry, done: () => act(async () => root.unmount()).then(() => el.remove()) };
}

describe("a send's actions are the selector's flags, exactly", () => {
  const SHOWN: SendShown[] = ["waiting", "sending", "sent", "failed", "unconfirmed"];
  for (const shown of SHOWN) {
    for (const canRetry of [false, true]) {
      for (const canDiscard of [false, true]) {
        it(`${shown}, canRetry ${canRetry}, canDiscard ${canDiscard}`, async () => {
          const { el, onDiscard, onRetry, done } = await draw(view(shown, { canRetry, canDiscard }));
          const names = all(el, "button").map(text);
          expect(names).toEqual([...(canDiscard ? ["Discard"] : []), ...(canRetry ? ["Retry"] : [])]);
          for (const button of all(el, "button")) await act(async () => (button as HTMLButtonElement).click());
          expect(onDiscard).toHaveBeenCalledTimes(canDiscard ? 1 : 0);
          expect(onRetry).toHaveBeenCalledTimes(canRetry ? 1 : 0);
          await done();
        });
      }
    }
  }

  it("shows a spinner while sending, and an arrow otherwise", async () => {
    const sending = await draw(view("sending"));
    expect(sending.el.querySelector("[data-arrow]")).toBeNull();
    expect(sending.el.querySelector(".sv-spin")).not.toBeNull();
    await sending.done();
    const failed = await draw(view("failed", { canRetry: true }));
    expect(failed.el.querySelector('[data-arrow="red"]')).not.toBeNull();
    await failed.done();
  });

  it("says why a Retry was refused, as an alert", async () => {
    const { el, done } = await draw(view("failed", { canRetry: true }), "Read-only now.");
    expect(text(el.querySelector('[role="alert"]'))).toBe("Read-only now.");
    await done();
  });
});

describe("a reason as a sentence", () => {
  it("starts with a capital and ends with a full stop, whatever the hub wrote", () => {
    expect(sentence("the session cannot take a message now")).toBe("The session cannot take a message now.");
    expect(sentence("  Already a sentence.  ")).toBe("Already a sentence.");
    expect(sentence("Really?")).toBe("Really?");
    expect(sentence("")).toBe("");
  });
});
