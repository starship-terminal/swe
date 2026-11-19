import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { lintText, lintTree } from "../scripts/lint-tokens.mjs";

const kinds = (text: string, file: string) =>
  lintText(text, file).map((p: string) => {
    if (p.includes("arbitrary")) return "arbitrary";
    if (p.includes("colour literal")) return "colour";
    if (p.includes("ALL CAPS")) return "caps";
    if (p.includes("mono font")) return "mono";
    return p;
  });

describe("lint:tokens", () => {
  it("refuses arbitrary values and properties", () => {
    for (const bad of [
      '<div className="bg-[#171717]" />',
      '<div className="hover:w-[13px] p-2" />',
      "<div className={`[color:red]`} />",
      '<div className="-mt-[3px]" />',
    ]) {
      expect(kinds(bad, "x.tsx"), bad).toContain("arbitrary");
    }
    expect(kinds(".a { @apply bg-[#111]; }", "x.css")).toContain("arbitrary");
  });

  describe("colours", () => {
    it("refuses literals in CSS declarations, named colours included", () => {
      for (const bad of [
        ".a { color: #fff; }",
        ".a { color: rgb(0 0 0); }",
        ".swe-sv p { color: red; }",
        ".a {\n  border: 1px solid white;\n}",
        "@layer base { .a { background: oklch(0.5 0.1 200); } }",
        "@theme inline { --color-brand: rebeccapurple; }",
      ]) {
        expect(kinds(bad, "x.css"), bad).toEqual(["colour"]);
      }
    });

    it("refuses literals in inline style objects, HTML style attributes and SVG colour attributes", () => {
      for (const bad of [
        '<p style={{ color: "red" }} />',
        '<p\n  style={{\n    backgroundColor: "#fafafa",\n  }}\n/>',
        "<p style={{ borderColor: 'hsl(0 0% 50%)' }} />",
        '<p style="color: navy">x</p>',
        '<path fill="#000" />',
        '<path stroke={"black"} />',
      ]) {
        expect(kinds(bad, "x.tsx"), bad).toEqual(["colour"]);
      }
    });

    it("lets tokens, keywords and text that only mentions a colour through", () => {
      for (const [ok, file] of [
        ["<p>See issue #123</p>", "x.tsx"],
        ["<p>Pick red or white, then rgb(1, 2, 3) is just text</p>", "x.tsx"],
        ["// #fff and rgb() in a comment", "x.tsx"],
        ["/* #fff and red in a comment */ .a { color: var(--text-primary); }", "x.css"],
        [".a { background-color: transparent; border-color: currentColor; color: inherit; }", "x.css"],
        [".a { font-family: Red Hat Text; animation-name: white; }", "x.css"],
        ['<p style={{ color: "var(--text-primary)" }} />', "x.tsx"],
        ['<path fill="none" stroke="currentColor" />', "x.tsx"],
        ["@theme inline { --color-wash: var(--wash); --color-*: initial; }", "x.css"],
      ] as const) {
        expect(kinds(ok, file), ok).toEqual([]);
      }
    });
  });

  describe("mono", () => {
    it("allows it on a code element's own tag, across line breaks", () => {
      for (const ok of [
        '<code className="font-mono">x</code>',
        '<code\n  className="font-mono"\n>\n  x\n</code>',
        '<pre\n  className={join(\n    "p-2",\n    "font-mono",\n  )}\n/>',
        '<kbd style={{ fontFamily: "var(--font-mono)" }}>K</kbd>',
      ]) {
        expect(kinds(ok, "x.tsx"), ok).toEqual([]);
      }
    });

    it("refuses it anywhere else, across line breaks too", () => {
      for (const bad of [
        '<p className="font-mono" />',
        '<code>\n  <span\n    className="font-mono"\n  />\n</code>',
        '<div\n  className="font-mono"\n/>',
        '<p style={{ fontFamily: "monospace" }} />',
      ]) {
        expect(kinds(bad, "x.tsx"), bad).toEqual(["mono"]);
      }
    });

    it("judges a stylesheet's mono font by its rule's selector, wherever the line breaks fall", () => {
      for (const ok of [
        ".swe-sv :where(code, pre) { font-family: ui-monospace, monospace; }",
        ".swe-sv :where(code, kbd)\n{\n  font-size: 1em;\n  font-family: var(--font-mono);\n}",
        "@layer base {\n  .swe-sv pre {\n    font-family: var(--font-mono);\n  }\n}",
        "@theme inline { --font-mono: ui-monospace, monospace; }",
      ]) {
        expect(kinds(ok, "x.css"), ok).toEqual([]);
      }
      for (const bad of [
        ".swe-sv p { font-family: var(--font-mono); }",
        ".swe-sv .label {\n  font-family:\n    ui-monospace, monospace;\n}",
        ".a { @apply font-mono; }",
      ]) {
        expect(kinds(bad, "x.css"), bad).toEqual(["mono"]);
      }
    });
  });

  it("refuses ALL CAPS on a tag or in a declaration, not in text", () => {
    expect(kinds('<p className="uppercase" />', "x.tsx")).toEqual(["caps"]);
    expect(kinds('<p style={{ textTransform: "uppercase" }} />', "x.tsx")).toEqual(["caps"]);
    expect(kinds(".a { text-transform: uppercase; }", "x.css")).toEqual(["caps"]);
    expect(kinds("<p>Never write uppercase labels</p>", "x.tsx")).toEqual([]);
  });

  it("lets token classes and ordinary code through", () => {
    for (const ok of [
      '<div className="bg-card text-primary rounded-card pt-safe-top" />',
      "const last = items[items.length - 1];",
      "const roots = new WeakMap<Element, Root>();",
    ]) {
      expect(kinds(ok, "x.tsx"), ok).toEqual([]);
    }
  });

  it("passes on this tree", () => {
    expect(lintTree(join(import.meta.dirname, ".."))).toEqual([]);
  });
});
