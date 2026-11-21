// What the connection's eight states say to a person. Pure, so each state's words are tested.
import type { ConnectionStatus } from "../core";
import type { ArrowColour } from "../ui";

export interface ConnectionLook {
  colour: ArrowColour;
  /** A word or two for the header. */
  label: string;
  /** One sentence for the banner under the header, or null when nothing needs saying. */
  banner: string | null;
  /** The banner's sentence may end with a countdown to the next try. */
  counts: boolean;
}

/** How a connection looks. `caughtUp` is false from the moment a stream opens until its state frame arrives. */
export function describeConnection(connection: ConnectionStatus, caughtUp: boolean): ConnectionLook {
  switch (connection.state) {
    case "connecting":
      // The loading state and the header already say it; a banner would say it a third time.
      return { colour: "grey", label: "Connecting", banner: null, counts: false };
    case "live":
      return caughtUp
        ? { colour: "green", label: "Live", banner: null, counts: false }
        : { colour: "yellow", label: "Catching up", banner: null, counts: false };
    case "reconnecting":
      return {
        colour: "yellow",
        label: "Reconnecting",
        banner: "The connection dropped. Reconnecting. Messages you send wait on this device.",
        counts: true,
      };
    case "offline":
      return {
        colour: "grey",
        label: "Offline",
        banner: "You are offline. What is loaded stays here, and messages you send wait on this device until you are back.",
        counts: false,
      };
    case "hub_down":
      return {
        colour: "red",
        label: "Hub not answering",
        banner: "The hub is not answering. Trying again. Messages you send wait on this device.",
        counts: true,
      };
    case "unauthorized":
      return {
        colour: "red",
        label: "Signed out",
        banner: "This device is not signed in to the hub. Messages you send wait on this device until it is.",
        counts: false,
      };
    case "gone":
      return { colour: "red", label: "Session gone", banner: "This session no longer exists.", counts: false };
    case "outdated":
      return { colour: "red", label: "Hub too old", banner: "The hub is too old for this view. Update SWE on that machine.", counts: false };
  }
}

/** Whole seconds until `retryAt`, or null when there is nothing to count down to. */
export function secondsUntil(retryAt: number | null, now: number): number | null {
  if (retryAt === null) return null;
  const seconds = Math.ceil((retryAt - now) / 1000);
  return seconds > 0 ? seconds : null;
}

const PHASE: Record<string, string> = {
  starting: "Starting",
  running: "Working",
  waiting: "Waiting for you",
  idle: "Idle",
  parked: "Paused",
  agent_down: "Agent not running",
  closed: "Closed",
};

/** The session's own state in words, for the header. */
export function phaseLabel(phase: string | null): string | null {
  return phase === null ? null : (PHASE[phase] ?? null);
}
