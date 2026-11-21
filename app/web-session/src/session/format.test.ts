import { describe, expect, it } from "vitest";

import { baseName, clip, diffStat, formatDuration, plural } from "./format";

describe("formatDuration", () => {
  it("says a span as a person does", () => {
    expect(formatDuration(0.2)).toBe("under 1s");
    expect(formatDuration(14)).toBe("14s");
    expect(formatDuration(59.6)).toBe("1m");
    expect(formatDuration(149)).toBe("2m 29s");
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(3780)).toBe("1h 3m");
    expect(formatDuration(-1)).toBe("");
    expect(formatDuration(Number.NaN)).toBe("");
  });
});

describe("small rules", () => {
  it("pluralises", () => {
    expect(plural(1, "file")).toBe("1 file");
    expect(plural(2, "file")).toBe("2 files");
    expect(plural(3, "tool call")).toBe("3 tool calls");
  });

  it("counts a diff's added and removed lines, not its headers", () => {
    expect(diffStat("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n+more\n context")).toEqual({ added: 2, removed: 1 });
    expect(diffStat(undefined)).toEqual({ added: 0, removed: 0 });
  });

  it("names a file and clips a long text", () => {
    expect(baseName("src/app.py")).toBe("app.py");
    expect(baseName("dir/")).toBe("dir");
    expect(clip("abcdef", 4)).toEqual({ text: "abcd", hidden: 2 });
    expect(clip("abc", 4)).toEqual({ text: "abc", hidden: 0 });
  });
});
