import { describe, expect, it } from "vitest";

import { describeKey, keyFromSearch } from "./session-key";

describe("keyFromSearch", () => {
  it("reads and trims the key parameter", () => {
    expect(keyFromSearch("?key=claude%3Aabc")).toBe("claude:abc");
    expect(keyFromSearch("?other=1&key=%20codex:1%20")).toBe("codex:1");
    expect(keyFromSearch("")).toBe("");
  });
});

describe("describeKey", () => {
  it("puts the harness where the project goes and the id where the title goes", () => {
    expect(describeKey("claude:3f2a")).toEqual({ project: "claude", title: "3f2a" });
  });

  it("drops a satellite's machine suffix", () => {
    expect(describeKey("codex:t1@mac-mini")).toEqual({ project: "codex", title: "t1" });
  });

  it("keeps an id that itself has a colon", () => {
    expect(describeKey("pi:path-abc:def")).toEqual({ project: "pi", title: "path-abc:def" });
  });

  it("says when there is no session or no harness", () => {
    expect(describeKey("")).toEqual({ project: "SWE", title: "No session" });
    expect(describeKey("  ")).toEqual({ project: "SWE", title: "No session" });
    expect(describeKey("loose")).toEqual({ project: "SWE", title: "loose" });
    expect(describeKey(":x")).toEqual({ project: "SWE", title: ":x" });
  });
});
