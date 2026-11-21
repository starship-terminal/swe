import { describe, expect, it } from "vitest";

import { INLINE_READ_MAX, MAX_DEPTH, TABLE_CELL_BUDGET, TABLE_MAX_COLS, TABLE_MAX_ROWS, parseInline, parseMarkdown, safeHref, type Block } from "./markdown";

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

describe("tables", () => {
  it("reads formatting in every header cell, however many columns there are", () => {
    const cols = Array.from({ length: 12 }, (_, i) => `**h${i}**`);
    const [table] = parseMarkdown(`| ${cols.join(" | ")} |\n|${"---|".repeat(12)}\n| ${cols.map(() => "x").join(" | ")} |`);
    if (table?.t !== "table") throw new Error("not a table");
    expect(table.head).toHaveLength(12);
    for (const cell of table.head) expect(cell[0]?.t).toBe("strong");
  });
});

describe("the read limit on one run of text", () => {
  it("formats what is inside the limit and leaves what is past it as typed", () => {
    const pad = "x".repeat(INLINE_READ_MAX);
    const out = parseInline(`**in** ${pad} **past** and \`code\``);
    expect(out[0]).toEqual({ t: "strong", children: [{ t: "text", text: "in" }] });
    // Past the limit the marks are text, bold and code alike.
    expect(out.map((n) => n.t)).toEqual(["strong", "text"]);
    expect(JSON.stringify(out)).toContain("**past** and `code`");
    expect(out.filter((n) => n.t === "code")).toHaveLength(0);
  });

  it("does not cut a run that fits", () => {
    const out = parseInline("`a` ".repeat(INLINE_READ_MAX / 8));
    expect(out.filter((n) => n.t === "code")).toHaveLength(INLINE_READ_MAX / 8);
  });
});

describe("a table from a few bytes", () => {
  const cellsOf = (table: Extract<Block, { t: "table" }>) => table.head.length + table.rows.reduce((sum, row) => sum + row.length, 0);

  it("keeps at most the column and row caps, and says what it left out", () => {
    const text = `|${"a|".repeat(2000)}\n|${"-|".repeat(2000)}\n${"|\n".repeat(100)}`;
    expect(text.length).toBeLessThan(10_000);
    const [table] = parseMarkdown(text);
    if (table?.t !== "table") throw new Error("not a table");
    expect(table.head).toHaveLength(TABLE_MAX_COLS);
    expect(table.align).toHaveLength(TABLE_MAX_COLS);
    expect(cellsOf(table)).toBeLessThanOrEqual(TABLE_CELL_BUDGET);
    // 5000 cells is 100 lines of 50: the header and 99 rows; one row of the 100 is over the reply's budget.
    expect(cellsOf(table)).toBe(TABLE_MAX_COLS * 100);
    expect(table.cut).toMatchObject({ cols: 1950, rows: 1 });
  });

  it("keeps the first rows of a tall table, counts the rest, and keeps the start of them as text", () => {
    const rows = Array.from({ length: 2000 }, (_, i) => `| r${i} | x |`).join("\n");
    const [table] = parseMarkdown(`| a | b |\n|---|---|\n${rows}`);
    if (table?.t !== "table") throw new Error("not a table");
    expect(table.rows).toHaveLength(TABLE_MAX_ROWS);
    expect(table.cut?.rows).toBe(1500);
    expect(table.cut?.cols).toBe(0);
    expect(table.cut?.text.startsWith(`| r${TABLE_MAX_ROWS} | x |`)).toBe(true);
    expect(table.cut?.text.length).toBeLessThanOrEqual(1000);
  });

  it("does not cut a table inside the caps", () => {
    const [table] = parseMarkdown("| a | b |\n|---|---|\n| 1 | 2 |");
    expect(table).toMatchObject({ t: "table", cut: null });
  });
});

describe("the budget of cells for one whole reply", () => {
  /** Every cell drawn by every table in the blocks, nested ones included. */
  const cells = (blocks: Block[]): number =>
    blocks.reduce(
      (sum, b) =>
        sum +
        (b.t === "table"
          ? b.head.length + b.rows.reduce((n, row) => n + row.length, 0)
          : b.t === "quote"
            ? cells(b.blocks)
            : b.t === "list"
              ? b.items.reduce((n, item) => n + cells(item.blocks), 0)
              : 0),
      0,
    );
  const tables = (blocks: Block[]): Extract<Block, { t: "table" }>[] =>
    blocks.flatMap((b) => (b.t === "table" ? [b] : b.t === "quote" ? tables(b.blocks) : b.t === "list" ? b.items.flatMap((item) => tables(item.blocks)) : []));
  /** A table at the per-table caps: 50 columns, 500 rows, about 1.2 KB of input. */
  const maxTable = `|${"a|".repeat(50)}\n|${"-|".repeat(50)}\n${"|\n".repeat(500)}`;

  it("draws no more cells in total than the budget when a reply has thirty-four tables at their caps, and still draws the text after them", () => {
    const reply = `${Array.from({ length: 34 }, () => maxTable).join("\n")}\nafter the last table`;
    expect(reply.length).toBeLessThan(45_000);
    const blocks = parseMarkdown(reply);
    expect(cells(blocks)).toBeLessThanOrEqual(TABLE_CELL_BUDGET);
    expect(tables(blocks)).toHaveLength(34);
    // The first table spent the whole budget; the rest are left out whole.
    const [first, second] = tables(blocks);
    expect(first?.head).toHaveLength(50);
    expect(second?.head).toHaveLength(0);
    expect(second?.cut).not.toBeNull();
    expect(JSON.stringify(blocks[blocks.length - 1])).toContain("after the last table");
  });

  it("is spent by tables inside quotes inside lists, in document order", () => {
    const nested = (rows: number) => ["- item", "", "  > | a | b | c | d | e | f | g | h | i | j |", "  > |---|---|---|---|---|---|---|---|---|---|", ...Array.from({ length: rows }, () => "  > | 1 |")].join("\n");
    // The nested table asks for 10 columns x 401 lines = 4010 cells; the top-level one then has 990.
    const flat = `| a | b | c | d | e | f | g | h | i | j |\n|---|---|---|---|---|---|---|---|---|---|\n${"| 1 |\n".repeat(400)}`;
    const blocks = parseMarkdown(`${nested(400)}\n\n${flat}`);
    const [inner, outer] = tables(blocks);
    expect(inner?.head).toHaveLength(10);
    expect(inner?.rows).toHaveLength(400);
    expect(outer?.rows.length).toBe(98); // 990 cells is 99 lines of 10: the header and 98 rows
    expect(outer?.cut?.rows).toBe(302);
    expect(cells(blocks)).toBeLessThanOrEqual(TABLE_CELL_BUDGET);
    expect(cells(blocks)).toBe(4010 + 990);
  });

  it("leaves an ordinary table alone, and a second one after it", () => {
    const small = (n: number) => `| a | b | c | d | e |\n|---|---|---|---|---|\n${Array.from({ length: 20 }, (_, i) => `| ${i} | ${n} | x | y | z |`).join("\n")}`;
    const blocks = parseMarkdown(`${small(1)}\n\n${small(2)}`);
    const [one, two] = tables(blocks);
    expect(one).toMatchObject({ cut: null });
    expect(one?.head).toHaveLength(5);
    expect(one?.rows).toHaveLength(20);
    expect(two).toMatchObject({ cut: null });
    expect(cells(blocks)).toBe(2 * 5 * 21);
  });

  it("starts each reply with a full budget", () => {
    const one = parseMarkdown(maxTable);
    const again = parseMarkdown(maxTable);
    expect(cells(one)).toBe(cells(again));
  });
});
