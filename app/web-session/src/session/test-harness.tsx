// Shared by the session view's DOM tests: a real store over a scripted transport, drawn by the real view.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll } from "vitest";

import { createSessionStore, type Frame, type SessionStore } from "../core";
import { MemoryStorage } from "../core/outbox-storage";
import { FakeEnvironment, FakeTimers, FakeTransport, drain, loadFixture, type Fixture } from "../core/test-support";
import { SessionView } from "./SessionView";

declare global {
  // React reads this to know act() wraps the updates.
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

export const KEY = "claude:session-1";

let root: Root | undefined;
let host: HTMLElement | undefined;
const stores: SessionStore[] = [];

afterEach(() => {
  act(() => root?.unmount());
  for (const store of stores.splice(0)) store.getState().stop();
  host?.remove();
  root = undefined;
  host = undefined;
});

export function setup(over: { online?: boolean } = {}) {
  const timers = new FakeTimers();
  const transport = new FakeTransport();
  const environment = new FakeEnvironment();
  environment.isOnline = over.online ?? true;
  let ids = 0;
  const store = createSessionStore({
    sessionKey: KEY,
    transport,
    timers,
    random: () => 0,
    environment,
    openStorage: async () => new MemoryStorage("no database in tests"),
    newId: () => `id${++ids}`,
  });
  stores.push(store);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  act(() => {
    root?.render(<SessionView now={() => timers.now()} sessionKey={KEY} store={store} />);
    store.getState().start();
  });
  return { store, transport, timers, environment, el: host };
}

/** Run something that changes the store, and let the view and the store's own async work settle. */
export async function settle(work: () => unknown = () => {}): Promise<void> {
  await act(async () => {
    await work();
    await drain();
  });
}

export const frames = (fixture: Fixture, connection = 0): Frame[] => (fixture.connections[connection]?.frames ?? []) as Frame[];

export async function play(t: ReturnType<typeof setup>, name: string, from = 0, to?: number): Promise<Fixture> {
  const fixture = loadFixture(name);
  await settle(() => void t.transport.last.send(...frames(fixture).slice(from, to)));
  return fixture;
}

export const text = (el: Element | null | undefined) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
export const q = (el: Element, selector: string) => el.querySelector(selector);
export const all = (el: Element, selector: string) => Array.from(el.querySelectorAll(selector));

export function button(el: Element, label: string): HTMLButtonElement | undefined {
  return all(el, "button").find((b) => text(b).includes(label)) as HTMLButtonElement | undefined;
}

export async function click(target: Element | undefined | null): Promise<void> {
  if (target === undefined || target === null) throw new Error("nothing to click");
  await settle(() => (target as HTMLElement).click());
}


/** A v2 frame by hand, for sequences the recordings do not hold. */
export const frame = (type: string, seq: number | undefined, data: unknown, extra: object = {}): Frame =>
  ({ v: 2, type, session: KEY, ...(seq === undefined ? {} : { seq }), ts: 1760000200, data, ...extra }) as unknown as Frame;

export const defaultFrames = (): Frame[] => frames(loadFixture("default"));

/** The recorded snapshot body of a fixture, to build a variation on. */
export const bodyOf = (name: string) => loadFixture(name).snapshots[0]?.body as Fixture["snapshots"][number]["body"];
