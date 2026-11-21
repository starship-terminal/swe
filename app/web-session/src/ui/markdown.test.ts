import { describe, expect, it } from "vitest";

import { INLINE_READ_MAX, MAX_DEPTH, parseInline, parseMarkdown, safeHref, type Block } from "./markdown";

describe("inline Markdown", () => {
  it("reads code, bold, italic and links, and keeps the words around them", () => {
    expect(parseInline("a `b` **c** *d* [e](https://x.test/y) f")).toEqual([
      { t: "text", text: "a " },
      { t: "code", text: "b" },
      { t: "text", text: " " },
      { t: "strong", children: [{ t: "text", text: "c" }] },
      { t: "text", text: " " },
      { t: "em", children: [{ t: "text", text: "d" }] },
      { t: "text", text: " " },
      { t: "link", href: "https://x.test/y", children: [{ t: "text", text: "e" }] },
      { t: "text", text: " f" },
    ]);
  });

  it("does not read formatting inside code", () => {
    expect(parseInline("`**x**`")).toEqual([{ t: "code", text: "**x**" }]);
  });

  it("leaves a half-written mark as typed", () => {
    expect(parseInline("see `src/ap")).toEqual([{ t: "text", text: "see `src/ap" }]);
    expect(parseInline("so **bol")).toEqual([{ t: "text", text: "so **bol" }]);
  });

  it("closes a code span with a run of backticks as long as the one that opened it", () => {
    expect(parseInline("a ``x ` y`` b")).toEqual([
      { t: "text", text: "a " },
      { t: "code", text: "x ` y" },
      { t: "text", text: " b" },
    ]);
    expect(parseInline("`` `tick` ``")).toEqual([{ t: "code", text: "`tick`" }]);
    expect(parseInline("a `` b")).toEqual([{ t: "text", text: "a `` b" }]);
  });

  it("keeps snake_case words whole", () => {
    expect(parseInline("a snake_case_name here")).toEqual([{ t: "text", text: "a snake_case_name here" }]);
  });

  it("turns an unsafe link into its words, never an href", () => {
    expect(parseInline("[x](javascript:alert(1))")).toEqual([{ t: "text", text: "x" }]);
    expect(parseInline("[x](/relative)")).toEqual([{ t: "text", text: "x" }]);
    expect(safeHref("data:text/html,hi")).toBeNull();
    expect(safeHref("mailto:a@b.test")).toBe("mailto:a@b.test");
  });
});

describe("block Markdown", () => {
  it("reads paragraphs, headings and rules", () => {
    const blocks = parseMarkdown("# Title\n\nOne\ntwo\n\n---\n\nThree");
    expect(blocks.map((b) => b.t)).toEqual(["heading", "paragraph", "rule", "paragraph"]);
    expect(blocks[1]).toEqual({ t: "paragraph", children: [{ t: "text", text: "One two" }] });
  });

  it("reads a fenced code block, open or closed", () => {
    expect(parseMarkdown("```py\nprint(1)\n```\nafter")).toEqual([
      { t: "code", lang: "py", text: "print(1)", open: false },
      { t: "paragraph", children: [{ t: "text", text: "after" }] },
    ]);
    // A reply still being written: the fence is open and holds the rest.
    expect(parseMarkdown("```\nx = 1\ny = ")).toEqual([{ t: "code", lang: "", text: "x = 1\ny = ", open: true }]);
  });

  it("reads lists with nesting, and ordered lists with their start", () => {
    const [list] = parseMarkdown("- a\n  - b\n- c");
    expect(list).toMatchObject({ t: "list", ordered: false });
    if (list?.t !== "list") throw new Error("not a list");
    expect(list.items).toHaveLength(2);
    expect(list.items[0]?.blocks[0]).toMatchObject({ t: "list", items: [{ children: [{ t: "text", text: "b" }] }] });
    const [ordered] = parseMarkdown("3. x\n4. y");
    expect(ordered).toMatchObject({ t: "list", ordered: true, start: 3 });
  });

  it("removes a list item's real indent from what it holds, so code keeps its own", () => {
    const [list] = parseMarkdown("1. Run:\n\n   ```py\n   x = 1\n     y = 2\n   ```\n10. Next\n- Dash:\n\n  ```\n  z\n  ```");
    if (list?.t !== "list") throw new Error("not a list");
    expect(list.items[0]?.blocks[0]).toEqual({ t: "code", lang: "py", text: "x = 1\n  y = 2", open: false });
    const [dash] = parseMarkdown("- Dash:\n\n  ```\n  z\n  ```");
    if (dash?.t !== "list") throw new Error("not a list");
    expect(dash.items[0]?.blocks[0]).toEqual({ t: "code", lang: "", text: "z", open: false });
  });

  it("reads quotes and tables", () => {
    expect(parseMarkdown("> a\n> b")[0]).toMatchObject({ t: "quote" });
    const [table] = parseMarkdown("| a | b |\n|---|--:|\n| 1 | 2 |\n| 3 |");
    expect(table).toMatchObject({ t: "table", align: [null, "right"] });
    if (table?.t !== "table") throw new Error("not a table");
    expect(table.rows).toHaveLength(2);
    expect(table.rows[1]?.[1]).toEqual([]);
  });

  it("does not take a lone pipe for a table", () => {
    expect(parseMarkdown("a | b\nc")[0]?.t).toBe("paragraph");
  });

  it("ends a paragraph where a list starts", () => {
    expect(parseMarkdown("Steps:\n- one\n- two").map((b) => b.t)).toEqual(["paragraph", "list"]);
  });
});

describe("hostile text", () => {
  const depthOf = (blocks: Block[]): number =>
    1 + Math.max(0, ...blocks.map((b) => (b.t === "quote" ? depthOf(b.blocks) : b.t === "list" ? Math.max(0, ...b.items.map((i) => depthOf(i.blocks))) : 0)));

  it("reads ten thousand quote markers without overflowing the stack, nesting no deeper than the cap", () => {
    const blocks = parseMarkdown(">".repeat(10_000) + " hi");
    expect(depthOf(blocks)).toBeLessThanOrEqual(MAX_DEPTH + 1);
    // What is deeper than the cap stays as text, so nothing the person wrote is lost.
    expect(JSON.stringify(blocks)).toContain("hi");
  });

  it("reads deeply nested lists, and quotes mixed with lists, within the cap", () => {
    const lists = Array.from({ length: 5000 }, (_, i) => `${"  ".repeat(i)}- item`).join("\n");
    expect(depthOf(parseMarkdown(lists))).toBeLessThanOrEqual(MAX_DEPTH + 1);
    const mixed = Array.from({ length: 3000 }, () => "> - ").join("") + "x";
    expect(() => parseMarkdown(mixed)).not.toThrow();
  });

  it("reads formatting inside formatting only so deep", () => {
    const nested = "[".repeat(2000) + "x" + "](https://a.test)".repeat(1);
    expect(() => parseInline(nested)).not.toThrow();
    expect(() => parseInline("**".repeat(5000))).not.toThrow();
  });

  it("reads a hundred kilobytes of code spans in well under a second, and leaves what is past the cap as text", () => {
    const big = "`a` ".repeat(25_000);
    const t0 = performance.now();
    const out = parseInline(big);
    expect(performance.now() - t0).toBeLessThan(500);
    expect(out.filter((n) => n.t === "code").length).toBeLessThanOrEqual(INLINE_READ_MAX / 4 + 1);
    const last = out[out.length - 1];
    expect(last?.t).toBe("text");
    const full = parseMarkdown("`a` ".repeat(25_000));
    expect(full).toHaveLength(1);
  });

  it("does not take long over many marks that never close", () => {
    const t0 = performance.now();
    parseInline("* [ ` _ ".repeat(12_000));
    expect(performance.now() - t0).toBeLessThan(1000);
  });
});
