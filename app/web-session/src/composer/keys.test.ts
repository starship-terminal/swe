import { describe, expect, it } from "vitest";

import { enterAction, type EnterInput } from "./keys";

const enter = (over: Partial<EnterInput> = {}): EnterInput => ({
  key: "Enter",
  shiftKey: false,
  altKey: false,
  metaKey: false,
  ctrlKey: false,
  repeat: false,
  keyCode: 13,
  isComposing: false,
  ...over,
});

describe("what Enter does in the message box", () => {
  it("sends on a plain Enter", () => {
    expect(enterAction(enter(), false, false)).toBe("send");
  });

  it("adds a line on Shift+Enter and Alt+Enter", () => {
    expect(enterAction(enter({ shiftKey: true }), false, false)).toBe("none");
    expect(enterAction(enter({ altKey: true }), false, false)).toBe("none");
  });

  it("leaves other keys alone", () => {
    expect(enterAction(enter({ key: "a" }), false, false)).toBe("none");
    expect(enterAction(enter({ key: "Tab" }), false, false)).toBe("none");
  });

  it("does not send while an input method is composing, however the browser reports it", () => {
    // The event's own flag (Chrome, Firefox).
    expect(enterAction(enter({ isComposing: true }), false, false)).toBe("none");
    // keyCode 229 on the key press that ends a composition (Safari fires compositionend first).
    expect(enterAction(enter({ keyCode: 229 }), false, false)).toBe("none");
    // The composition events the box tracks itself.
    expect(enterAction(enter(), true, false)).toBe("none");
  });

  it("never sends a second time for a key held down: the repeats are swallowed", () => {
    expect(enterAction(enter({ repeat: true }), false, false)).toBe("swallow");
  });

  it("adds a line on a touch screen, and sends with Ctrl or Cmd", () => {
    expect(enterAction(enter(), false, true)).toBe("none");
    expect(enterAction(enter({ metaKey: true }), false, true)).toBe("send");
    expect(enterAction(enter({ ctrlKey: true }), false, true)).toBe("send");
  });

  it("sends with Ctrl or Cmd on a desktop too", () => {
    expect(enterAction(enter({ ctrlKey: true }), false, false)).toBe("send");
  });
});
