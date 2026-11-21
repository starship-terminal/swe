import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { generate, OUTPUT_PATH, readSchema } from "../../scripts/gen-types.mjs";
import { PROTOCOL } from "./generated/session-v2";

describe("the generated types", () => {
  it("are what the schema makes now (run `pnpm gen:types` when this fails)", () => {
    expect(readFileSync(OUTPUT_PATH, "utf8")).toBe(generate(readSchema()));
  });

  it("carry the schema's constants, so nothing is copied by hand", () => {
    const schema = readSchema();
    expect(PROTOCOL).toEqual(schema["x-swe"]);
    expect(PROTOCOL.silence_s).toBe(25);
    expect(PROTOCOL.ping_s).toBe(10);
  });

  it("have a type for every definition in the schema", () => {
    const text = generate(readSchema());
    for (const name of Object.keys(readSchema().$defs)) {
      expect(text, name).toContain(`export type ${name} =`);
    }
  });

  it("refuse a schema construct the generator does not read", () => {
    const schema = { $defs: { Odd: { type: "object", patternProperties: {} } } };
    expect(() => generate(schema)).toThrow(/patternProperties/);
  });
});
