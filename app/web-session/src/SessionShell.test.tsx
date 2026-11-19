import { act } from "react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

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

afterEach(() => {
  act(() => view?.unmount());
  view = undefined;
  document.body.innerHTML = "";
});

function render(key: string): HTMLElement {
  const el = document.createElement("div");
  document.body.append(el);
  act(() => {
    view = mount(el, { key });
  });
  return el;
}

describe("the session shell", () => {
  it("renders the breadcrumb, an empty timeline and a disabled composer", () => {
    const el = render("claude:3f2a");

    const shell = el.firstElementChild;
    expect(shell?.classList.contains("swe-sv")).toBe(true);

    const crumbs = el.querySelector('nav[aria-label="Breadcrumb"]');
    expect(crumbs?.textContent).toBe("claude/3f2a");
    expect(el.querySelector('[aria-current="page"]')?.textContent).toBe("3f2a");

    expect(el.querySelector('main[aria-label="Timeline"]')?.textContent).toContain("No messages yet");

    const box = el.querySelector("textarea");
    expect(box?.placeholder).toBe(COMPOSER_PLACEHOLDER);
    expect(box?.disabled).toBe(true);
    expect(el.querySelector<HTMLButtonElement>('button[aria-label="Send"]')?.disabled).toBe(true);
  });

  it("is exposed as SWESessionView.mount, and a second mount replaces the first", () => {
    expect(window.SWESessionView?.mount).toBe(mount);
    const el = render("codex:one");
    act(() => {
      view = mount(el, { key: "codex:two" });
    });
    expect(el.querySelectorAll(".swe-sv")).toHaveLength(1);
    expect(el.querySelector('[aria-current="page"]')?.textContent).toBe("two");
  });
});
