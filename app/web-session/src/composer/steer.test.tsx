// What the steer control says, from what the session says steer does (`caps.steer_effect`, optional).
import { describe, expect, it } from "vitest";

import { click, feed, frame, mount, play, q, settle, text, typeText, btn, type Mounted } from "./harness";
import { steerEffectOf, steerWording } from "./steer";

const caps = (extra: object) => frame("session.caps", 40, { caps: { steer: true, plan_mode: true, rollback: false, fork: true, stop_task: true, options: [], ...extra } });
const sendItem = (m: Mounted) => q(m.el, '[data-menu-item="steer"]') as HTMLButtonElement;

async function busy(extra: object): Promise<Mounted> {
  const m = mount();
  await play(m, "streaming", 0, 3);
  await feed(m, caps(extra));
  typeText(m, "hello");
  await settle(() => void (btn(m, "Queue message") as HTMLButtonElement).dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
  return m;
}

const CASES: [string, object, string, string, string][] = [
  ["stops_reply", { steer_effect: "stops_reply" }, "Stop and send", "Stops the current reply, then sends this message now.", "stop"],
  ["after_tool", { steer_effect: "after_tool" }, "Send now", "Sends this message now. The current reply is cut, but a tool call that is running finishes first.", "queue"],
  ["next_turn", { steer_effect: "next_turn" }, "Send now", "Sends this message ahead of the queue. The agent reads it in its next turn, and the current reply goes on.", "queue"],
  ["absent", {}, "Send now", "Sends this message ahead of the queue. Some agents stop the current reply to do it.", "queue"],
];

describe("the steer item in the send menu", () => {
  for (const [name, extra, label, sentence] of CASES) {
    it(`says ${label} and what it does when steer_effect is ${name}`, async () => {
      const m = await busy(extra);
      expect(text(sendItem(m).querySelector("span > span:first-child"))).toBe(label);
      expect(text(sendItem(m))).toContain(sentence);
    });
  }

  it("treats a value it does not know like an absent field", async () => {
    const m = await busy({ steer_effect: "something_new" });
    expect(text(sendItem(m))).toContain("Some agents stop the current reply to do it.");
  });

  it("sends the same steer whatever the label says", async () => {
    const m = await busy({ steer_effect: "next_turn" });
    await click(sendItem(m));
    expect(m.transport.posts("/message")[0]).toMatchObject({ text: "hello", mode: "steer" });
  });
});

describe("the steer item in a queued message's menu", () => {
  for (const [name, extra, label, sentence] of CASES) {
    it(`says ${label} when steer_effect is ${name}`, async () => {
      const m = mount();
      await play(m, "queued", 0, 3);
      await feed(m, frame("approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), frame("session.state", 14, { state: "running" }), caps(extra));
      await click(q(m.el, "[data-tray] button[aria-controls]"));
      await click(btn(m, "More for queued message"));
      expect(text(sendItem(m).querySelector("span > span:first-child"))).toBe(label);
      expect(text(sendItem(m))).toContain(sentence);
    });
  }
});

describe("steerWording and steerEffectOf", () => {
  it("only a stopping steer is called Stop and send, and the others never claim to stop the reply", () => {
    expect(steerWording("stops_reply")).toMatchObject({ label: "Stop and send", stops: true });
    for (const effect of ["after_tool", "next_turn", null] as const) {
      const words = steerWording(effect);
      expect(words.stops).toBe(false);
      expect(words.label).toBe("Send now");
      expect(words.sentence.startsWith("Stops the current reply")).toBe(false);
    }
  });

  it("reads the field when it is a known string and nothing else", () => {
    const base = { steer: true, plan_mode: false, rollback: false, fork: false, stop_task: false, options: [] };
    expect(steerEffectOf(null)).toBeNull();
    expect(steerEffectOf(base)).toBeNull();
    expect(steerEffectOf({ ...base, steer_effect: "after_tool" } as never)).toBe("after_tool");
    expect(steerEffectOf({ ...base, steer_effect: 3 } as never)).toBeNull();
    expect(steerEffectOf({ ...base, steer_effect: "stops_reply " } as never)).toBeNull();
  });
});
