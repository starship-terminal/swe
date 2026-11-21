import { describe, expect, it } from "vitest";

import type { ConnectionState, ConnectionStatus } from "../core";
import { describeConnection, phaseLabel, secondsUntil } from "./connection";

const status = (state: ConnectionState, over: Partial<ConnectionStatus> = {}): ConnectionStatus => ({
  state,
  attempt: 0,
  since: 0,
  retryAt: null,
  detail: "",
  ...over,
});

describe("what each connection state says", () => {
  const STATES: ConnectionState[] = ["connecting", "live", "reconnecting", "offline", "hub_down", "unauthorized", "gone", "outdated"];

  it("has words for all eight states, in the arrow colours AGENTS.md assigns", () => {
    const looks = Object.fromEntries(STATES.map((state) => [state, describeConnection(status(state), true)]));
    expect(Object.fromEntries(STATES.map((s) => [s, looks[s]?.colour]))).toEqual({
      connecting: "grey",
      live: "green",
      reconnecting: "yellow",
      offline: "grey",
      hub_down: "red",
      unauthorized: "red",
      gone: "red",
      outdated: "red",
    });
    for (const state of STATES) {
      expect(looks[state]?.label).not.toBe("");
      // The live state has nothing to explain, and connecting is told by the loading state and the header.
      expect(looks[state]?.banner === null).toBe(state === "live" || state === "connecting");
    }
  });

  it("is yellow, not green, between the stream opening and the state frame", () => {
    expect(describeConnection(status("live"), false)).toMatchObject({ colour: "yellow", label: "Catching up", banner: null });
  });

  it("counts down only where a retry is on its way", () => {
    expect(STATES.filter((s) => describeConnection(status(s), true).counts)).toEqual(["reconnecting", "hub_down"]);
  });

  it("says the way to the next try in whole seconds, rounded up, and nothing once it is due", () => {
    expect(secondsUntil(10_000, 5_500)).toBe(5);
    expect(secondsUntil(10_000, 9_999)).toBe(1);
    expect(secondsUntil(10_000, 10_000)).toBeNull();
    expect(secondsUntil(null, 0)).toBeNull();
  });

  it("names the session's own phase", () => {
    expect(phaseLabel("running")).toBe("Working");
    expect(phaseLabel("waiting")).toBe("Waiting for you");
    expect(phaseLabel("agent_down")).toBe("Agent not running");
    expect(phaseLabel(null)).toBeNull();
  });
});
