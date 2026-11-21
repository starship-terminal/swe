import { describe, expect, it } from "vitest";

import { STORAGE_DOWN, type OutboxView } from "./outbox";
import type { OutboxEntry } from "./outbox-storage";
import { describeSends, type SendContext } from "./sends";
import type { ConnectionStatus } from "./supervisor";

const entry = (over: Partial<OutboxEntry>): OutboxEntry => ({
  commandId: "c1", session: "s", text: "hi", attachments: [], mode: "queue", createdAt: 1, status: "waiting", attempts: 0, ...over,
});
const connection = (state: ConnectionStatus["state"]): ConnectionStatus => ({ state, attempt: 0, since: 0, retryAt: null, detail: "" });
const outbox = (entries: OutboxEntry[], over: Partial<OutboxView> = {}): OutboxView => ({
  entries, persistent: true, note: "", paused: null, inFlight: null, owner: "me", loading: false, storageDown: false, ...over,
});
const one = (context: Partial<SendContext> & { entries: OutboxEntry[] }) =>
  describeSends({
    outbox: context.outbox ?? outbox(context.entries),
    connection: context.connection ?? connection("live"),
    loaded: context.loaded ?? true,
    online: context.online ?? true,
  })[0];

describe("describeSends", () => {
  it("says what a waiting send waits for", () => {
    expect(one({ entries: [entry({})], online: false })?.reason).toContain("offline");
    expect(one({ entries: [entry({})], connection: connection("unauthorized") })?.reason).toContain("not signed in");
    expect(one({ entries: [entry({})], outbox: outbox([entry({})], { paused: "unauthorized" }) })?.reason).toContain("not signed in");
    expect(one({ entries: [entry({})], loaded: false })?.reason).toContain("Waiting for the hub");
    expect(one({ entries: [entry({ attempts: 1, lastError: "The hub did not answer" })] })?.reason).toBe("The hub did not answer. Trying again.");
    expect(one({ entries: [entry({})], connection: connection("hub_down") })?.reason).toContain("not answering");
    expect(one({ entries: [entry({})] })).toMatchObject({ shown: "waiting", canDiscard: true, canRetry: false });
    expect(one({ entries: [entry({ attempts: 1 })] })?.canDiscard).toBe(false);
  });

  it("shows a send on its way, one the hub took, and the terminal ones with their reasons", () => {
    expect(one({ entries: [entry({})], outbox: outbox([entry({})], { inFlight: "c1" }) })).toMatchObject({ shown: "sending" });
    expect(one({ entries: [entry({ status: "sent", path: "keeper", receipt: "accepted" })] })?.reason).toContain("Sent");
    expect(one({ entries: [entry({ status: "sent", path: "keeper", receipt: "queued" })] })?.reason).toContain("Queued");
    expect(one({ entries: [entry({ status: "sent", path: "keeper", receipt: "held" })] })?.reason).toContain("Held");
    expect(one({ entries: [entry({ status: "failed", error: "no" })] })).toMatchObject({ shown: "failed", reason: "no", canRetry: true, canDiscard: true });
    expect(one({ entries: [entry({ status: "unconfirmed", error: "maybe" })] })).toMatchObject({ shown: "unconfirmed", reason: "maybe", canRetry: true });
  });

  it("says the database is down for every waiting send while it is", () => {
    const at = (storageDown: boolean) =>
      describeSends({ outbox: outbox([entry({})], { storageDown }), connection: connection("live"), loaded: true, online: true })[0]?.reason;
    expect(at(true)).toBe(`${STORAGE_DOWN}. Trying again.`);
    expect(at(false)).toBe("Waiting to be sent.");
  });

  it("offers nothing while the outbox is loading what storage holds", () => {
    const entries = [entry({ commandId: "a" }), entry({ commandId: "b", status: "failed", error: "no" })];
    const views = describeSends({ outbox: outbox(entries, { loading: true }), connection: connection("live"), loaded: true, online: true });
    expect(views.map((v) => [v.commandId, v.shown, v.canDiscard, v.canRetry])).toEqual([
      ["a", "waiting", false, false],
      ["b", "failed", false, false],
    ]);
    expect(views[0]?.reason).toBe("Loading the sends kept on this device.");
  });
});
