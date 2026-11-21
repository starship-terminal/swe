// The composer's surface rules, read as files (jsdom has no layout and no backdrop-filter): the glass has a
// solid fallback, honours reduced transparency, comes from tokens with a light and a dark value, and no
// transition runs outside the no-preference block.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "composer.css"), "utf8");
const tokens = readFileSync(join(here, "../../../../src/swe/webapp/static/tokens.css"), "utf8");
const theme = readFileSync(join(here, "../index.css"), "utf8");

/** The text of the first block that starts with `head`, braces balanced. */
function block(text: string, head: string): string {
  const at = text.indexOf(head);
  if (at < 0) return "";
  let depth = 0;
  for (let i = text.indexOf("{", at); i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return text.slice(at, i + 1);
  }
  return "";
}

describe("the glass", () => {
  it("is solid by default, translucent with a blur only where backdrop-filter exists, and solid again under reduced transparency", () => {
    const plain = css.slice(css.indexOf(".swe-sv .sv-glass {"), css.indexOf("}", css.indexOf(".swe-sv .sv-glass {")));
    expect(plain).toContain("background: var(--glass-solid)");
    expect(plain).not.toContain("backdrop-filter");
    const supported = block(css, "@supports");
    expect(supported).toContain("backdrop-filter: blur(var(--glass-blur))");
    expect(supported).toContain("background: var(--glass)");
    const reduced = block(css, "@media (prefers-reduced-transparency: reduce)");
    expect(reduced).toContain("background: var(--glass-solid)");
    expect(reduced).toContain("backdrop-filter: none");
  });

  it("takes its colours from tokens that have a light and a dark value", () => {
    for (const name of ["--glass:", "--glass-solid:", "--glass-border:"]) {
      // Once in the light block, once in each of the two dark ones.
      expect(tokens.split(name).length - 1).toBe(3);
    }
    for (const name of ["--glass-blur:", "--bar-radius:", "--glass-radius:"]) expect(tokens.split(name).length - 1).toBe(1);
    expect(theme).toContain("--radius-bar: var(--bar-radius)");
    expect(theme).toContain("--radius-glass: var(--glass-radius)");
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
  });

  it("rides the keyboard and the home indicator, and leaves its height to the timeline", () => {
    expect(css).toContain("max(var(--safe-bottom, 0px), var(--keyboard-inset, 0px))");
    expect(css).toContain("var(--sv-composer-h, 0px)");
  });
});

describe("reduced motion in the composer", () => {
  it("runs no transition or animation outside the no-preference block", () => {
    const open = "@media (prefers-reduced-motion: no-preference)";
    const outside = css.slice(0, css.indexOf(open));
    expect(outside).not.toMatch(/\b(?:transition|animation)\s*:/);
    expect(css.slice(css.indexOf(open))).toContain(".sv-chev-up");
  });

  it("scopes every rule to the view's root", () => {
    const selectors = Array.from(css.matchAll(/^\s*(\.[^{};]*?)\s*\{/gm), (m) => (m[1] as string).trim());
    expect(selectors.length).toBeGreaterThan(5);
    expect(selectors.every((s) => s.startsWith(".swe-sv"))).toBe(true);
  });

  it("puts every Tailwind transition behind motion-safe:", () => {
    const bad: string[] = [];
    for (const name of readdirSync(here)) {
      if (!/\.tsx$/.test(name) || /\.test\.|harness/.test(name)) continue;
      readFileSync(join(here, name), "utf8")
        .split("\n")
        .forEach((line, index) => {
          if (/(?<![:-])\b(?:transition(?:-[a-z]+)?|animate-[a-z]+)\b/.test(line.replace(/motion-safe:[\w-]+/g, "")) && !/^\s*(\/\/|\*|\/\*)/.test(line)) bad.push(`${name}:${index + 1}`);
        });
    }
    expect(bad).toEqual([]);
  });
});

describe("text that must be read on the glass", () => {
  it("has no danger- or warning-coloured text on the glass: a message of that kind has its own solid backing and full ink", () => {
    const bad: string[] = [];
    for (const name of readdirSync(here)) {
      if (!/\.tsx$/.test(name) || /\.test\.|harness/.test(name)) continue;
      readFileSync(join(here, name), "utf8")
        .split("\n")
        .forEach((line, index) => {
          if (/(?<![:\w-])text-(?:danger|warning)-text/.test(line)) bad.push(`${name}:${index + 1}`);
        });
    }
    expect(bad).toEqual([]);
  });

  it("draws an error message on the card token with ink, a red border and an arrow", () => {
    const note = readFileSync(join(here, "ErrorNote.tsx"), "utf8");
    expect(note).toContain("bg-card");
    expect(note).toContain("text-primary");
    expect(note).toContain("border-danger");
    expect(note).toContain('colour="red"');
  });
});
