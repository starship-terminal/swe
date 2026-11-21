import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { createSessionStore, useSessionStore } from "./store";
import { FakeEnvironment, FakeTimers, FakeTransport, loadFixture } from "./test-support";
import { MemoryStorage } from "./outbox-storage";
import type { Frame } from "./types";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});
afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = undefined;
});

describe("useSessionStore", () => {
  it("re-renders a view when the part of the state it selects changes", () => {
    const transport = new FakeTransport();
    const store = createSessionStore({
      sessionKey: "claude:session-1",
      transport,
      timers: new FakeTimers(),
      environment: new FakeEnvironment(),
      openStorage: async () => new MemoryStorage(),
    });
    store.getState().start();

    function Count() {
      const items = useSessionStore(store, (s) => s.session.items.length);
      const state = useSessionStore(store, (s) => s.connection.state);
      return createElement("p", null, `${state}:${items}`);
    }
    const el = document.createElement("div");
    const root = createRoot(el);
    act(() => root.render(createElement(Count)));
    expect(el.textContent).toBe("connecting:0");

    const frames = loadFixture("default").connections[0]?.frames as Frame[];
    act(() => transport.last.send(...frames.slice(0, 2)));
    expect(el.textContent).toMatch(/^live:\d+$/);
    expect(Number(el.textContent?.split(":")[1])).toBeGreaterThan(0);
    act(() => root.unmount());
    store.getState().stop();
  });
});
