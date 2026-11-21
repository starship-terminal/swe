import { act } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { mount, type MountedView } from "./main";
import { COMPOSER_PLACEHOLDER } from "./SessionShell";

declare global {
  // React reads this to know act() wraps the updates.
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

let view: MountedView | undefined;
let fetches: { url: string; init: RequestInit | undefined }[] = [];

beforeEach(() => {
  fetches = [];
  // The hub never answers: the view stays where it starts, and nothing leaves the test.
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      fetches.push({ url, init });
      return new Promise(() => {});
    }),
  );
});

afterEach(() => {
  act(() => view?.unmount());
  view = undefined;
  document.body.innerHTML = "";
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

async function render(key: string): Promise<HTMLElement> {
  const el = document.createElement("div");
  document.body.append(el);
  await act(async () => {
    view = mount(el, { key });
  });
  return el;
}

describe("the session shell", () => {
  it("connects to the session's stream with the saved credential, and shows the breadcrumb, a loading timeline and the composer", async () => {
    window.localStorage.setItem("swe.remote", JSON.stringify({ credential: "sk-test" }));
    const el = await render("claude:3f2a");

    const shell = el.firstElementChild;
    expect(shell?.classList.contains("swe-sv")).toBe(true);

    expect(el.querySelector('nav[aria-label="Breadcrumb"]')?.textContent).toBe("claude/3f2a");
    expect(el.querySelector('[aria-current="page"]')?.textContent).toBe("3f2a");
    expect(el.querySelector('main[aria-label="Timeline"]')?.textContent).toContain("Loading the session");

    expect(fetches[0]?.url).toBe("/v1/sessions/claude%3A3f2a/events?v=2");
    expect(fetches[0]?.init?.headers).toMatchObject({ Authorization: "Bearer sk-test", "X-Swe-Pair": "1", Accept: "text/event-stream" });

    const box = el.querySelector("textarea");
    expect(box?.placeholder).toBe(COMPOSER_PLACEHOLDER);
    // A message can be typed while the session loads: it is kept and sent once the hub has been heard from.
    expect(box?.disabled).toBe(false);
    expect(el.querySelector<HTMLButtonElement>('button[aria-label="Send message"]')?.disabled).toBe(true); // nothing typed yet
    expect(el.querySelector('[data-composer="form"]')).not.toBeNull();
  });

  it("sends no credential header when none is saved (the native shells add theirs)", async () => {
    await render("claude:3f2a");
    expect(fetches[0]?.init?.headers).not.toHaveProperty("Authorization");
  });

  it("opens no connection for an empty key, and says no session was picked", async () => {
    const el = await render("");
    expect(fetches).toEqual([]);
    expect(el.querySelector('nav[aria-label="Breadcrumb"]')?.textContent).toBe("SWE/No session");
    expect(el.querySelector('main[aria-label="Timeline"]')?.textContent).toContain("No session selected");
  });

  it("stops the connection when it unmounts", async () => {
    const el = await render("claude:3f2a");
    // StrictMode starts, stops and starts again: the first stream is closed, the last one is the view's.
    const signals = fetches.map((f) => (f.init as RequestInit).signal as AbortSignal);
    expect(signals.at(-1)?.aborted).toBe(false);
    await act(async () => view?.unmount());
    view = undefined;
    expect(signals.at(-1)?.aborted).toBe(true);
    expect(el.innerHTML).toBe("");
  });

  it("is exposed as SWESessionView.mount, and a second mount replaces the first", async () => {
    expect(window.SWESessionView?.mount).toBe(mount);
    const el = await render("codex:one");
    await act(async () => {
      view = mount(el, { key: "codex:two" });
    });
    expect(el.querySelectorAll(".swe-sv")).toHaveLength(1);
    expect(el.querySelector('[aria-current="page"]')?.textContent).toBe("two");
  });
});
