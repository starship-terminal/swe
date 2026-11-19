import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { lintText, lintTree } from "../scripts/lint-tokens.mjs";

describe("lint:tokens", () => {
  it("refuses arbitrary values and properties", () => {
    for (const bad of [
      '<div className="bg-[#171717]" />',
      '<div className="hover:w-[13px] p-2" />',
      "<div className={`[color:red]`} />",
      '<div className="-mt-[3px]" />',
    ]) {
      expect(lintText(bad, "x.tsx").some((p) => p.includes("arbitrary")), bad).toBe(true);
    }
  });

  it("refuses colour literals, ALL CAPS and mono outside code", () => {
    expect(lintText(".a { color: #fff; }", "x.css")).toHaveLength(1);
    expect(lintText(".a { color: rgb(0 0 0); }", "x.css")).toHaveLength(1);
    expect(lintText('<p className="uppercase" />', "x.tsx")).toHaveLength(1);
    expect(lintText('<p className="font-mono" />', "x.tsx")).toHaveLength(1);
  });

  it("lets token classes, comments and code elements through", () => {
    for (const ok of [
      '<div className="bg-card text-primary rounded-card pt-safe-top" />',
      '<code className="font-mono">x</code>',
      ".swe-sv :where(code, pre) { font-family: ui-monospace, monospace; }",
      " * a comment that names #fff and rgb() is fine",
      "const last = items[items.length - 1];",
    ]) {
      expect(lintText(ok, "x.tsx"), ok).toEqual([]);
    }
  });

  it("passes on this tree", () => {
    expect(lintTree(join(import.meta.dirname, ".."))).toEqual([]);
  });
});
