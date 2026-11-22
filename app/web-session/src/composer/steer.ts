// What steer does, as far as the session says. `Caps.steer` is a boolean, true for Claude, Codex and opencode
// alike, and the three do different things (G16 gap 6): Claude cuts the reply and kills a running tool call, Codex
// cuts the reply but lets a running command finish and holds the steer behind an open approval, opencode cuts
// nothing and lands the steer when the current step ends. A hub that knows says so in `caps.steer_effect`; this view
// reads it when it is there and says only what is true of every agent when it is not (an older hub, without the field).
// The type is the hub schema's (G32); the value is still checked, since a newer hub may name an effect this view
// does not know.
import type { Caps } from "../core";

export type SteerEffect = NonNullable<Caps["steer_effect"]>;

export interface SteerWording {
  /** The menu item's name. */
  label: string;
  /** One sentence for what it does. */
  sentence: string;
  /** Whether it stops the reply in progress. */
  stops: boolean;
}

const EFFECTS = new Set<unknown>(["stops_reply", "after_tool", "next_turn"] satisfies SteerEffect[]);

/** The effect the session names, or null (the field is absent, or a value this view does not know). */
export function steerEffectOf(caps: Caps | null): SteerEffect | null {
  const value: unknown = caps?.steer_effect;
  return EFFECTS.has(value) ? (value as SteerEffect) : null;
}

/** Said when a request is waiting for an answer: what that does to a steer differs by agent, and only a stopping one is known to touch it. */
const WITH_APPROVAL = " A request waiting for your answer may be withdrawn, or may hold this message back until you answer it.";

export function steerWording(effect: SteerEffect | null, requestOpen = false): SteerWording {
  switch (effect) {
    case "stops_reply":
      return {
        label: "Stop and send",
        sentence: `Stops the reply being written and sends this message now. A command that is already running may keep going.${requestOpen ? WITH_APPROVAL : ""}`,
        stops: true,
      };
    case "after_tool":
      return { label: "Send next", sentence: "Sends this message when the current step ends. Nothing is stopped.", stops: false };
    case "next_turn":
      return { label: "Send next", sentence: "Sends this message when the reply ends.", stops: false };
    case null:
      return { label: "Send now", sentence: `Sends this message ahead of the queue. Some agents stop the current reply to do it.${requestOpen ? WITH_APPROVAL : ""}`, stops: false };
  }
}
