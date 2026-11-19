import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

// public/appearance.js is a classic script (it runs in <head> before the
// bundle), so the test runs its text the way the page does.
const source = readFileSync(join(import.meta.dirname, "../public/appearance.js"), "utf8");
const run = () => new Function(source)();
const root = document.documentElement;

afterEach(() => {
  localStorage.clear();
  delete root.dataset.theme;
});

describe("appearance.js", () => {
  it("applies Light and Dark from swe.appearance, as the web app's Settings stores them", () => {
    localStorage.setItem("swe.appearance", "dark");
    run();
    expect(root.dataset.theme).toBe("dark");
    localStorage.setItem("swe.appearance", "light");
    run();
    expect(root.dataset.theme).toBe("light");
  });

  it("follows the system when nothing or something unknown is stored", () => {
    root.dataset.theme = "dark";
    run();
    expect(root.dataset.theme).toBeUndefined();
    localStorage.setItem("swe.appearance", "sepia");
    run();
    expect(root.dataset.theme).toBeUndefined();
  });

  it("follows a change made in another tab", () => {
    run();
    localStorage.setItem("swe.appearance", "dark");
    window.dispatchEvent(new StorageEvent("storage", { key: "swe.appearance", newValue: "dark" }));
    expect(root.dataset.theme).toBe("dark");
  });
});
