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
  ["stops_reply", { steer_effect: "stops_reply" }, "Stop and send", "Stops the reply being written and sends this message now. A command that is already running may keep going.", "stop"],
  ["after_tool", { steer_effect: "after_tool" }, "Send next", "Sends this message when the current step ends. Nothing is stopped.", "queue"],
  ["next_turn", { steer_effect: "next_turn" }, "Send next", "Sends this message when the reply ends.", "queue"],
  ["absent", {}, "Send now", "Sends this message ahead of the queue. Some agents stop the current reply to do it.", "queue"],
];

describe("the steer item in the send menu", () => {
  for (const [name, extra, label, sentence] of CASES.filter(([name]) => name !== "next_turn")) {
    it(`says ${label} and what it does when steer_effect is ${name}`, async () => {
      const m = await busy(extra);
      expect(text(sendItem(m).querySelector("span > span:first-child"))).toBe(label);
      expect(text(sendItem(m))).toContain(sentence);
      // The tooltip says the same.
      expect(sendItem(m).title).toBe(sentence);
    });
  }

  it("treats a value it does not know like an absent field", async () => {
    const m = await busy({ steer_effect: "something_new" });
    expect(text(sendItem(m))).toContain("Some agents stop the current reply to do it.");
  });

  it("offers no menu at all for a steer that lands when the reply ends: it would be Queue again", async () => {
    const m = await busy({ steer_effect: "next_turn" });
    expect(q(m.el, '[role="menu"]')).toBeNull();
    expect(sendItem(m)).toBeNull();
    // A press on the main button still queues.
    await click(btn(m, "Queue message"));
    expect(m.transport.posts("/message")[0]).toMatchObject({ text: "hello", mode: "queue" });
  });

  it("sends the same steer whatever the label says", async () => {
    const m = await busy({ steer_effect: "after_tool" });
    await click(sendItem(m));
    expect(m.transport.posts("/message")[0]).toMatchObject({ text: "hello", mode: "steer" });
  });
});

describe("the steer item in a queued message's menu", () => {
  it("is not offered for a steer that lands when the reply ends", async () => {
    const m = mount();
    await play(m, "queued", 0, 3);
    await feed(m, frame("approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), frame("session.state", 14, { state: "running" }), caps({ steer_effect: "next_turn" }));
    await click(q(m.el, "[data-tray] button[aria-controls]"));
    expect(btn(m, "More for queued message")).toBeUndefined();
    expect(btn(m, "Edit queued message")).toBeDefined();
  });

  for (const [name, extra, label, sentence] of CASES.filter(([name]) => name !== "next_turn")) {
    it(`says ${label} when steer_effect is ${name}`, async () => {
      const m = mount();
      await play(m, "queued", 0, 3);
      await feed(m, frame("approval.resolved", 13, { request_id: "r1", source: "user", decision: "allow" }, { turn_id: "t1" }), frame("session.state", 14, { state: "running" }), caps(extra));
      await click(q(m.el, "[data-tray] button[aria-controls]"));
      await click(btn(m, "More for queued message"));
      expect(text(sendItem(m).querySelector("span > span:first-child"))).toBe(label);
      expect(text(sendItem(m))).toContain(sentence);
      expect(sendItem(m).title).toBe(sentence);
    });
  }

  it("says what a waiting request does to a steer only where an agent is known to do it", async () => {
    for (const [effect, said] of [["stops_reply", true], ["after_tool", false], ["next_turn", false], [undefined, true]] as const) {
      const m = mount();
      await play(m, "approval", 0, 2);
      await feed(m, caps(effect === undefined ? {} : { steer_effect: effect }), frame("session.state", 41, { state: "running" }));
      typeText(m, "hello");
      await settle(() => void (btn(m, "Queue message") as HTMLButtonElement).dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
      expect(text(sendItem(m)).includes("waiting for your answer")).toBe(said);
    }
  });
});

describe("the steer item in a queued message's menu while a request waits", () => {
  it("says what a waiting request may do to it, for an agent that stops the reply, and not for one that waits for the step", async () => {
    for (const [effect, said] of [["stops_reply", true], ["after_tool", false]] as const) {
      const m = mount();
      await play(m, "queued", 0, 3); // the approval r1 is still open
      await feed(m, caps({ steer_effect: effect }));
      await click(q(m.el, "[data-tray] button[aria-controls]"));
      await click(btn(m, "More for queued message"));
      expect(text(sendItem(m)).includes("waiting for your answer")).toBe(said);
    }
  });
});

describe("steerWording and steerEffectOf", () => {
  it("only a stopping steer is called Stop and send, and the others never claim to stop the reply", () => {
    expect(steerWording("stops_reply")).toMatchObject({ label: "Stop and send", stops: true });
    for (const effect of ["after_tool", "next_turn", null] as const) {
      const words = steerWording(effect);
      expect(words.stops).toBe(false);
      expect(words.label).toBe(effect === null ? "Send now" : "Send next");
      expect(words.sentence.startsWith("Stops the")).toBe(false);
    }
    expect(steerWording("after_tool").sentence).toContain("Nothing is stopped.");
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
