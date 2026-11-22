import { describe, expect, it } from "vitest";

import { cut, label, LABEL_SHOWN } from "./text";

describe("label", () => {
  it("takes off whitespace at the two ends and nothing inside the text", () => {
    expect(label("  Allow\n")).toBe("Allow");
    expect(label("\t a   b\u00a0c\n d  ")).toBe("a   b\u00a0c\n d");
    expect(label("Allow\u200bnow")).toBe("Allow\u200bnow"); // an invisible mark inside stays: it is the hub's text
  });

  it("cuts a long one at the limit, ends it with an ellipsis, and changes no letter before it", () => {
    const long = `${"x".repeat(LABEL_SHOWN)}tail`;
    expect(label(long)).toBe(`${"x".repeat(LABEL_SHOWN)}…`);
    expect(label("x".repeat(LABEL_SHOWN))).toBe("x".repeat(LABEL_SHOWN));
  });

  it("never ends in half a pair of surrogates", () => {
    expect(cut(`${"a".repeat(LABEL_SHOWN - 1)}😀`, LABEL_SHOWN)).toEqual({ text: "a".repeat(LABEL_SHOWN - 1), cut: true });
  });
});
