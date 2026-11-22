// Shared by the composer's DOM tests: the real composer over a real store, over a scripted transport and
// the recorded fixtures. Tests only.
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach } from "vitest";

import { createSessionStore, type Frame, type SessionStore } from "../core";
import { MemoryStorage } from "../core/outbox-storage";
import { drain, FakeEnvironment, FakeTimers, FakeTransport, loadFixture, snapshotFrame, type Fixture } from "../core/test-support";
import { Composer, type ComposerProps } from "./Composer";
import { forgetMemoryDrafts } from "./draft";
import { forgetKeysDown } from "./Menu";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

export const KEY = "claude:session-1";

// Every part a test mounts (some mount two), so cleanup takes all of them down.
const roots: Root[] = [];
const hosts: HTMLElement[] = [];
const stores: SessionStore[] = [];

beforeEach(() => {
  window.localStorage.clear();
  forgetMemoryDrafts();
  forgetKeysDown();
});

afterEach(() => {
  act(() => {
    for (const root of roots.splice(0)) root.unmount();
  });
  for (const store of stores.splice(0)) store.getState().stop();
  for (const host of hosts.splice(0)) host.remove();
});

export interface Mounted {
  store: SessionStore;
  transport: FakeTransport;
  timers: FakeTimers;
  environment: FakeEnvironment;
  el: HTMLElement;
  /** Draw the composer again, as a new part would be (a remount), over the same store. */
  remount(over?: Partial<ComposerProps>): void;
  render(over?: Partial<ComposerProps>): void;
}

export function mount(over: { online?: boolean; strict?: boolean; props?: Partial<ComposerProps>; openStorage?: () => Promise<MemoryStorage> } = {}): Mounted {
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
    openStorage: over.openStorage ?? (async () => new MemoryStorage("no database in tests")),
    newId: () => `id${++ids}`,
  });
  stores.push(store);
  transport.on("/message", () => ({ status: 202, body: { receipt: "accepted" } }), "POST");
  const host = document.createElement("div");
  document.body.append(host);
  hosts.push(host);
  let root = createRoot(host);
  roots.push(root);
  const render = (extra: Partial<ComposerProps> = {}): void => {
    const tree = <Composer sessionKey={KEY} settleMs={30} store={store} {...over.props} {...extra} />;
    act(() => root.render(over.strict === true ? <StrictMode>{tree}</StrictMode> : tree));
  };
  render();
  act(() => store.getState().start());
  return {
    store,
    transport,
    timers,
    environment,
    el: host,
    render,
    remount(extra = {}) {
      act(() => root.unmount());
      roots.splice(roots.indexOf(root), 1);
      root = createRoot(host);
      roots.push(root);
      render(extra);
    },
  };
}

export async function settle(work: () => unknown = () => {}): Promise<void> {
  await act(async () => {
    await work();
    await drain();
  });
}

export const frames = (fixture: Fixture, connection = 0): Frame[] => (fixture.connections[connection]?.frames ?? []) as Frame[];

/** Feed a recorded fixture's frames (from `from` up to, not including, `to`) to the stream. */
export async function play(m: Mounted, name: string, from = 0, to?: number): Promise<Fixture> {
  const fixture = loadFixture(name);
  await settle(() => void m.transport.last.send(...frames(fixture).slice(from, to)));
  return fixture;
}

export const feed = (m: Mounted, ...list: Frame[]): Promise<void> => settle(() => void m.transport.last.send(...list));

export const frame = (type: string, seq: number | undefined, data: unknown, extra: object = {}): Frame =>
  ({ v: 2, type, session: KEY, ...(seq === undefined ? {} : { seq }), ts: 1760000200, data, ...extra }) as unknown as Frame;

/** A session at rest: the default recording's end, and caught up. */
export const idle = (m: Mounted): Promise<Fixture> => play(m, "default");

/** A snapshot with the given changes to its body, and the state frame that ends the catch-up. */
export function snapshotWith(name: string, change: Record<string, unknown>): Frame[] {
  const fixture = loadFixture(name);
  const body = { ...(fixture.snapshots[0]?.body as object), ...change } as Fixture["snapshots"][number]["body"];
  return [snapshotFrame(body), frame("session.state", 0, { state: body.state, live: [], pending: body.pending.map((p) => (p as { data: { request_id: string } }).data.request_id), head: body.head, epoch: body.epoch })];
}

export const text = (el: Element | null | undefined) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
export const q = (el: Element, selector: string) => el.querySelector(selector);
export const all = (el: Element, selector: string) => Array.from(el.querySelectorAll(selector));

export function box(m: Mounted): HTMLTextAreaElement {
  return q(m.el, 'textarea[aria-label="Message"]') as HTMLTextAreaElement;
}

export function btn(m: Mounted, label: string): HTMLButtonElement | undefined {
  return all(m.el, "button").find((b) => b.getAttribute("aria-label") === label || text(b) === label) as HTMLButtonElement | undefined;
}

/** Type into the box the way a person does: the box sees an `input` event. */
export function typeText(m: Mounted, words: string): void {
  const el = box(m);
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(el, words);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

export async function click(target: Element | undefined | null): Promise<void> {
  if (target === undefined || target === null) throw new Error("nothing to click");
  await settle(() => (target as HTMLElement).click());
}

/** A key press in the box. Returns the event, so a test can see whether it was kept from typing. */
export function key(m: Mounted, init: KeyboardEventInit & { keyCode?: number }): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  if (init.keyCode !== undefined) Object.defineProperty(event, "keyCode", { value: init.keyCode });
  act(() => void box(m).dispatchEvent(event));
  return event;
}

/** The POSTs the transport saw to a route, by its last path segment. */
export const posts = (m: Mounted, tail: string): Record<string, unknown>[] => m.transport.posts(`/${tail}`);
