import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearAllDrafts, DRAFT_KEPT_CHARS, DRAFT_MAX_AGE_MS, dropDraftsOnSignOut, forgetMemoryDrafts, readDraft, sweepDrafts, writeDraft } from "./draft";

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

  it("prefers the newer copy in memory when the latest write could not reach storage", () => {
    writeDraft("s", { text: "saved", files: [] });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(writeDraft("s", { text: "saved, and then more", files: [] })).toBe("memory");
    expect(readDraft("s").text).toBe("saved, and then more");
    vi.restoreAllMocks();
    // Once storage takes a write again, storage is the truth again.
    writeDraft("s", { text: "back in storage", files: [] });
    forgetMemoryDrafts();
    expect(readDraft("s").text).toBe("back in storage");
  });
});

describe("how long a draft is kept", () => {
  const raw = (session: string) => window.localStorage.getItem(`swe.sv.draft.v1:${session}`);

  it("stores when it was last edited, and nothing but the text, the files and that", () => {
    writeDraft("s", { text: "x", files: [file] });
    expect(Object.keys(JSON.parse(raw("s") as string)).sort()).toEqual(["a", "f", "t"]);
  });

  it("is gone when it was last edited more than fourteen days ago, and still there just inside that", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z"));
      writeDraft("old", { text: "stale", files: [] });
      writeDraft("fresh", { text: "kept", files: [] });
      forgetMemoryDrafts();
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z").getTime() + DRAFT_MAX_AGE_MS - 1000);
      expect(readDraft("old").text).toBe("stale");
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z").getTime() + DRAFT_MAX_AGE_MS + 1000);
      writeDraft("fresh", { text: "kept", files: [] }); // edited again just now
      forgetMemoryDrafts();
      expect(readDraft("old").text).toBe("");
      expect(readDraft("fresh").text).toBe("kept");
    } finally {
      vi.useRealTimers();
    }
  });

  it("is gone from memory too once it is that old: a page left open for weeks does not bring it back", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z"));
      writeDraft("big", { text: "x".repeat(DRAFT_KEPT_CHARS + 1), files: [] }); // memory only
      expect(readDraft("big").text.length).toBe(DRAFT_KEPT_CHARS + 1);
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z").getTime() + DRAFT_MAX_AGE_MS + 1000);
      expect(readDraft("big").text).toBe("");
    } finally {
      vi.useRealTimers();
    }
  });

  it("is dropped, with no time on it, as one an older version wrote", () => {
    window.localStorage.setItem("swe.sv.draft.v1:legacy", JSON.stringify({ t: "no time", f: [] }));
    expect(readDraft("legacy").text).toBe("");
  });

  it("is swept out of storage when the box opens, the old ones only", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z"));
      writeDraft("old", { text: "stale", files: [] });
      window.localStorage.setItem("other", "not a draft");
      vi.setSystemTime(new Date("2026-11-01T10:00:00Z").getTime() + DRAFT_MAX_AGE_MS + 1000);
      writeDraft("fresh", { text: "kept", files: [] });
      sweepDrafts();
      expect(raw("old")).toBeNull();
      expect(raw("fresh")).not.toBeNull();
      expect(window.localStorage.getItem("other")).toBe("not a draft");
    } finally {
      vi.useRealTimers();
    }
  });

  it("is all dropped when the pairing is removed in another tab and no credential remains, and not when something else is", async () => {
    const stop = dropDraftsOnSignOut();
    window.localStorage.setItem("swe.remote", JSON.stringify({ credential: "sk-test" }));
    writeDraft("a", { text: "one", files: [] });
    writeDraft("b", { text: "two", files: [] });
    window.dispatchEvent(new StorageEvent("storage", { key: "something.else", newValue: null }));
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(readDraft("a").text).toBe("one");
    window.localStorage.removeItem("swe.remote");
    window.dispatchEvent(new StorageEvent("storage", { key: "swe.remote", oldValue: "x", newValue: null }));
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(raw("a")).toBeNull();
    expect(raw("b")).toBeNull();
    expect(readDraft("a").text).toBe(""); // nor is it still held in memory
    stop();
    writeDraft("c", { text: "three", files: [] });
    window.dispatchEvent(new StorageEvent("storage", { key: "swe.remote", newValue: null }));
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(raw("c")).not.toBeNull(); // no longer listening
  });

  it("is kept when the legacy key goes while a credential stays: that is a new credential being banked", async () => {
    const stop = dropDraftsOnSignOut();
    // The web app's pairStore: setItem("swe.remote", ...), then removeItem("quiver.remote").
    window.localStorage.setItem("swe.remote", JSON.stringify({ credential: "new" }));
    writeDraft("a", { text: "half a thought", files: [] });
    window.dispatchEvent(new StorageEvent("storage", { key: "quiver.remote", oldValue: JSON.stringify({ credential: "old" }), newValue: null }));
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(readDraft("a").text).toBe("half a thought");
    stop();
  });

  it("is all dropped on demand, files and memory copies with it, and other keys stay", () => {
    writeDraft("a", { text: "x".repeat(DRAFT_KEPT_CHARS + 1), files: [] }); // memory only
    writeDraft("b", { text: "two", files: [file] });
    window.localStorage.setItem("swe.remote", "pairing");
    clearAllDrafts();
    expect(readDraft("a").text).toBe("");
    expect(readDraft("b").text).toBe("");
    expect(window.localStorage.getItem("swe.remote")).toBe("pairing");
  });
});
