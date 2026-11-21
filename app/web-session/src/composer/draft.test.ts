import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DRAFT_KEPT_CHARS, forgetMemoryDrafts, readDraft, writeDraft } from "./draft";

beforeEach(() => {
  window.localStorage.clear();
  forgetMemoryDrafts();
});
afterEach(() => vi.restoreAllMocks());

const file = { ref: "1-a-x.png", name: "x.png", size: 3 };

describe("the draft kept for a session", () => {
  it("is read back after a reload, with its files", () => {
    expect(writeDraft("s", { text: "half a thought", files: [file] })).toBe("storage");
    forgetMemoryDrafts(); // a new page: nothing in memory
    expect(readDraft("s")).toEqual({ text: "half a thought", files: [file] });
  });

  it("is kept apart for each session", () => {
    writeDraft("a", { text: "one", files: [] });
    writeDraft("b", { text: "two", files: [] });
    expect(readDraft("a").text).toBe("one");
    expect(readDraft("b").text).toBe("two");
  });

  it("is removed when it is written empty", () => {
    writeDraft("s", { text: "x", files: [] });
    writeDraft("s", { text: "", files: [] });
    expect(window.localStorage.length).toBe(0);
    expect(readDraft("s")).toEqual({ text: "", files: [] });
  });

  it("stays in memory, and says so, when the browser refuses to store it", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(writeDraft("s", { text: "keep me", files: [] })).toBe("memory");
    expect(readDraft("s").text).toBe("keep me");
  });

  it("stays in memory when there is no storage at all", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(writeDraft("s", { text: "keep me", files: [] })).toBe("memory");
    expect(readDraft("s").text).toBe("keep me");
  });

  it("is kept in memory only when it is too long for storage", () => {
    expect(writeDraft("s", { text: "x".repeat(DRAFT_KEPT_CHARS + 1), files: [] })).toBe("memory");
    expect(window.localStorage.length).toBe(0);
    expect(readDraft("s").text).toHaveLength(DRAFT_KEPT_CHARS + 1);
  });

  it("ignores a stored value that is not a draft", () => {
    window.localStorage.setItem("swe.sv.draft.v1:s", "{not json");
    expect(readDraft("s")).toEqual({ text: "", files: [] });
    window.localStorage.setItem("swe.sv.draft.v1:s", JSON.stringify({ t: 5 }));
    expect(readDraft("s")).toEqual({ text: "", files: [] });
  });
});
