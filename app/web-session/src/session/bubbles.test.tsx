import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import type { SendShown, SendView } from "../core";
import { SendBubble } from "./Bubbles";
import { asArrived } from "./format";
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

describe("text from the hub, as it arrived", () => {
  it("never changes a letter, and adds a full stop only after a letter or a digit", () => {
    expect(asArrived("the session cannot take a message now")).toBe("the session cannot take a message now.");
    expect(asArrived("npm ERR!")).toBe("npm ERR!");
    expect(asArrived("src/x failed")).toBe("src/x failed.");
    expect(asArrived("  Already a sentence.  ")).toBe("Already a sentence.");
    expect(asArrived("exit code 1")).toBe("exit code 1.");
    expect(asArrived("Really?")).toBe("Really?");
    expect(asArrived("done)")).toBe("done)");
    expect(asArrived("")).toBe("");
  });

  it("draws a hub note in a bubble with its letters untouched", async () => {
    const { el, done } = await draw(view("failed", { canRetry: true, reason: "npm ERR! code E404" }));
    expect(text(el.querySelector("p"))).toBe("npm ERR! code E404.");
    await done();
  });
});

describe("attachments", () => {
  it("draws at most fifty chips, and says how many more there are", async () => {
    const paths = Array.from({ length: 80 }, (_, i) => `/work/f${i}.txt`);
    const { el, done } = await draw(view("sent", { attachments: paths }));
    expect(el.querySelectorAll('ul[aria-label="Attachments"] li')).toHaveLength(50);
    expect(text(el)).toContain("and 30 more attachments");
    await done();
  });
});
