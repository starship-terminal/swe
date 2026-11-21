// The waiting helpers the other tests stand on.
import { describe, expect, it } from "vitest";

import { drain, until } from "./test-support";

describe("drain", () => {
  it("waits out a chain of storage steps, however long", async () => {
    let steps = 0;
    const step = () => {
      if (++steps < 200) setImmediate(step);
    };
    setImmediate(step);
    await drain();
    expect(steps).toBe(200);
  });

  it("fails, saying what is queued, when the queue never empties", async () => {
    let stop = false;
    const step = () => {
      if (!stop) setImmediate(step);
    };
    setImmediate(step);
    await expect(drain(50)).rejects.toThrow("drain: still busy after 50 ticks");
    stop = true;
  });
});

describe("until", () => {
  it("returns once the condition holds, and fails naming what never happened", async () => {
    let n = 0;
    const step = () => {
      if (++n < 30) setImmediate(step);
    };
    setImmediate(step);
    await until(() => n === 30, "thirty steps");
    await expect(until(() => false, "the impossible", 5)).rejects.toThrow("until: the impossible did not happen within 5 ticks");
  });
});
