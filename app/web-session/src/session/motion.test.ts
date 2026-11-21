import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "session.css"), "utf8");

/** The CSS outside every `@media (prefers-reduced-motion: no-preference)` block. */
function outsideMotion(text: string): string {
  let out = "";
  let i = 0;
  const open = "@media (prefers-reduced-motion: no-preference)";
  for (;;) {
    const at = text.indexOf(open, i);
    if (at < 0) return out + text.slice(i);
    out += text.slice(i, at);
    let depth = 0;
    let j = text.indexOf("{", at);
    for (; j < text.length; j++) {
      if (text[j] === "{") depth++;
      else if (text[j] === "}" && --depth === 0) break;
    }
    i = j + 1;
  }
}

describe("reduced motion", () => {
  it("lets no transition or animation run outside the no-preference block", () => {
    const rest = outsideMotion(css).replace(/@keyframes[^{]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "");
    expect(rest).not.toMatch(/\b(?:transition|animation)\s*:/);
  });

  it("animates every state change it advertises under the block", () => {
    const motion = css.slice(css.indexOf("@media (prefers-reduced-motion: no-preference)"));
    for (const name of [".sv-collapse", ".sv-chevron", ".sv-enter", ".sv-spin", ".sv-pulse", ".sv-live", ".sv-banner-enter"]) {
      expect(motion).toContain(name);
    }
  });

  it("scopes every rule to the view's root, so nothing reaches the web app around an embedded one", () => {
    const selectors = Array.from(css.matchAll(/^\s*([.a-z@][^{};]*?)\s*\{/gm), (m) => (m[1] as string).trim());
    const rules = selectors.filter((s) => !s.startsWith("@") && !/^(from|to|\d+%|50%)$/.test(s));
    expect(rules.length).toBeGreaterThan(5);
    expect(rules.every((s) => s.startsWith(".swe-sv"))).toBe(true);
  });
});

describe("reduced motion in utility classes", () => {
  it("puts every Tailwind transition or animation behind motion-safe:", () => {
    const dirs = [here, join(here, "../ui")];
    const bad: string[] = [];
    for (const dir of dirs) {
      for (const name of readdirSync(dir)) {
        if (!/\.tsx$/.test(name) || /\.test\./.test(name)) continue;
        readFileSync(join(dir, name), "utf8")
          .split("\n")
          .forEach((line, index) => {
            const code = line.trim();
            if (code.startsWith("*") || code.startsWith("//") || code.startsWith("/*")) return;
            if (/(?<![\w:-])(?:transition(?:-[a-z]+)?|animate-[a-z-]+|duration-\d+|ease-[a-z-]+)(?![\w-])/.test(code.replace(/motion-safe:[\w-]+/g, ""))) bad.push(`${name}:${index + 1}`);
          });
      }
    }
    expect(bad).toEqual([]);
  });
});
