// One row per cell of the primary button's press machine (docs/specs/G13-composer.md has the table).
import { describe, expect, it } from "vitest";

import { stepPrimary, type PrimaryEvent, type PrimaryState } from "./primary";

type Row = [state: PrimaryState, event: PrimaryEvent, next: PrimaryState, action: string];
const down = { type: "down", holdable: true } as const;

const ROWS: Row[] = [
  // down: a new press, from anywhere
  ["idle", down, "pressed", "start-hold"],
  ["released", down, "pressed", "start-hold"],
  ["held", down, "pressed", "start-hold"],
  ["idle", { type: "down", holdable: false }, "pressed", "none"],
  // the hold timer
  ["pressed", { type: "hold" }, "held", "open-menu"],
  ["idle", { type: "hold" }, "idle", "none"],
  ["released", { type: "hold" }, "released", "none"],
  // up
  ["pressed", { type: "up" }, "idle", "stop-hold"],
  ["held", { type: "up" }, "released", "none"],
  ["idle", { type: "up" }, "idle", "none"],
  ["released", { type: "up" }, "released", "none"],
  // leave and cancel
  ["pressed", { type: "leave" }, "idle", "stop-hold"],
  ["held", { type: "leave" }, "released", "none"],
  ["released", { type: "leave" }, "released", "none"],
  ["idle", { type: "leave" }, "idle", "none"],
  ["pressed", { type: "cancel" }, "idle", "stop-hold"],
  ["held", { type: "cancel" }, "released", "none"],
  // click from a pointer
  ["idle", { type: "click", detail: 1 }, "idle", "send"],
  ["pressed", { type: "click", detail: 1 }, "idle", "send"],
  ["held", { type: "click", detail: 1 }, "idle", "swallow"],
  ["released", { type: "click", detail: 1 }, "idle", "swallow"],
  // click with no pointer (a key, an assistive technology): always a press
  ["idle", { type: "click", detail: 0 }, "idle", "send"],
  ["held", { type: "click", detail: 0 }, "idle", "send"],
  ["released", { type: "click", detail: 0 }, "idle", "send"],
  // contextmenu
  ["idle", { type: "contextmenu", menuOffered: true }, "idle", "open-menu"],
  ["pressed", { type: "contextmenu", menuOffered: true }, "held", "open-menu"],
  ["released", { type: "contextmenu", menuOffered: true }, "released", "open-menu"],
  ["idle", { type: "contextmenu", menuOffered: false }, "idle", "none"],
  // key and blur
  ["released", { type: "key" }, "idle", "none"],
  ["held", { type: "key" }, "held", "none"],
  ["idle", { type: "key" }, "idle", "none"],
  ["pressed", { type: "blur" }, "idle", "stop-hold"],
  ["held", { type: "blur" }, "held", "none"],
  ["released", { type: "blur" }, "released", "none"],
];

describe("the primary button's press machine", () => {
  for (const [state, event, next, action] of ROWS) {
    it(`${state} + ${event.type}${"detail" in event ? ` (detail ${event.detail})` : ""}${"holdable" in event ? ` (holdable ${event.holdable})` : ""}${"menuOffered" in event ? ` (menu ${event.menuOffered ? "offered" : "not offered"})` : ""} -> ${next}, ${action}`, () => {
      expect(stepPrimary(state, event)).toEqual({ state: next, action });
    });
  }

  // The orders a browser sends the events of a hold in: whatever the order, nothing after the menu opened sends.
  const ORDERS: Record<string, PrimaryEvent[]> = {
    mouse: [down, { type: "hold" }, { type: "up" }, { type: "click", detail: 1 }],
    touch: [down, { type: "hold" }, { type: "up" }, { type: "leave" }, { type: "click", detail: 1 }],
    "touch, leave first": [down, { type: "hold" }, { type: "leave" }, { type: "up" }, { type: "click", detail: 1 }],
    "click before the leave": [down, { type: "hold" }, { type: "up" }, { type: "click", detail: 1 }, { type: "leave" }],
    "blur from the menu in between": [down, { type: "hold" }, { type: "blur" }, { type: "up" }, { type: "leave" }, { type: "click", detail: 1 }],
    "cancelled": [down, { type: "hold" }, { type: "cancel" }, { type: "click", detail: 1 }],
  };
  for (const [name, order] of Object.entries(ORDERS)) {
    it(`never sends for a hold's release: ${name}`, () => {
      let state: PrimaryState = "idle";
      const actions: string[] = [];
      for (const event of order) {
        const step = stepPrimary(state, event);
        state = step.state;
        actions.push(step.action);
      }
      expect(actions).not.toContain("send");
      expect(actions).toContain("open-menu");
      // And the press after it is a press.
      const pressed = stepPrimary(state, down);
      const released = stepPrimary(pressed.state, { type: "up" });
      expect(stepPrimary(released.state, { type: "click", detail: 1 }).action).toBe("send");
    });
  }
});
