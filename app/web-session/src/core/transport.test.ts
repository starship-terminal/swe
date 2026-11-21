import { describe, expect, it, vi } from "vitest";

import { fetchTransport, SseParser, sessionPath, type StreamEnd } from "./transport";

const events = (...chunks: string[]) => {
  const out: string[] = [];
  const parser = new SseParser((data) => out.push(data));
  for (const chunk of chunks) parser.push(chunk);
  return out;
};

describe("SseParser", () => {
  it("reads one event per blank line, its data lines joined", () => {
    expect(events('event: ping\ndata: {"a":1}\n\n', "data: one\ndata: two\n\n")).toEqual(['{"a":1}', "one\ntwo"]);
  });

  it("reassembles an event split anywhere, even inside a line", () => {
    expect(events("da", 'ta: {"x"', ":1}\n", "\n")).toEqual(['{"x":1}']);
  });

  it("accepts CRLF and CR line ends", () => {
    // A final CR waits for the next byte, which may be the "\n" of a CRLF.
    expect(events("data: a\r\n\r\n", "data: b\r\r")).toEqual(["a"]);
    expect(events("data: a\r\n\r\n", "data: b\r\r", ": next")).toEqual(["a", "b"]);
    // A CR at the end of a chunk may be the first half of CRLF.
    expect(events("data: c\r", "\n\r", "\n")).toEqual(["c"]);
  });

  it("ignores comments (the keep-alive), event, id and retry lines, and empty events", () => {
    expect(events(": ka\n\nid: 7\nevent: x\nretry: 5\ndata: kept\n\n\n\n")).toEqual(["kept"]);
  });

  it("takes only one leading space off a value", () => {
    expect(events("data:  two spaces\n\ndata:none\n\n")).toEqual([" two spaces", "none"]);
  });

  it("does not deliver an event that has not ended", () => {
    expect(events("data: half\n")).toEqual([]);
  });
});

function streamOf(chunks: string[], then: "end" | "hang" = "end"): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(encoder.encode(chunks[i++]));
      else if (then === "end") controller.close();
      else return new Promise(() => {});
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

function collect() {
  const seen: unknown[] = [];
  const ends: StreamEnd[] = [];
  let opened = 0;
  return {
    seen,
    ends,
    get opened() {
      return opened;
    },
    handlers: {
      onOpen: () => void opened++,
      onFrame: (frame: unknown) => void seen.push(frame),
      onEnd: (end: StreamEnd) => void ends.push(end),
    },
  };
}

describe("fetchTransport.open", () => {
  it("sends the credential and Accept, and hands every JSON frame on as it arrives", async () => {
    const requests: { url: string; init: RequestInit }[] = [];
    const transport = fetchTransport({
      baseUrl: "http://hub.local:8477/",
      headers: () => ({ Authorization: "Bearer tok" }),
      fetch: async (url, init) => {
        requests.push({ url: String(url), init: init ?? {} });
        return streamOf(['data: {"v":2,"type":"ping"}\n\n: ka\n\nda', 'ta: {"v":2,"type":"x"}\n\n', "data: not json\n\n"]);
      },
    });
    const c = collect();
    transport.open("/v1/sessions/a/events?v=2", c.handlers);
    await settle();
    await settle();
    expect(requests[0]?.url).toBe("http://hub.local:8477/v1/sessions/a/events?v=2");
    expect(requests[0]?.init.headers).toEqual({ Authorization: "Bearer tok", Accept: "text/event-stream" });
    expect(c.opened).toBe(1);
    expect(c.seen).toEqual([
      { v: 2, type: "ping" },
      { v: 2, type: "x" },
    ]);
    expect(c.ends).toEqual([{ kind: "closed" }]);
  });

  it("reports an HTTP error status instead of a stream", async () => {
    const transport = fetchTransport({ fetch: async () => new Response("no", { status: 401 }) });
    const c = collect();
    transport.open("/x", c.handlers);
    await settle();
    expect(c.opened).toBe(0);
    expect(c.ends).toEqual([{ kind: "http", status: 401 }]);
  });

  it("reports a request that never got an answer", async () => {
    const transport = fetchTransport({
      fetch: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const c = collect();
    transport.open("/x", c.handlers);
    await settle();
    expect(c.ends).toEqual([{ kind: "error", message: "Failed to fetch" }]);
  });

  it("reports a stream that breaks in the middle", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"v":2,"type":"ping"}\n\n'));
        controller.error(new TypeError("network error"));
      },
    });
    const transport = fetchTransport({ fetch: async () => new Response(body) });
    const c = collect();
    transport.open("/x", c.handlers);
    await settle();
    await settle();
    expect(c.ends).toEqual([{ kind: "error", message: "network error" }]);
  });

  it("close() aborts the request and says nothing more", async () => {
    let signal: AbortSignal | null | undefined;
    const transport = fetchTransport({
      fetch: async (_url, init) => {
        signal = init?.signal;
        return streamOf(['data: {"v":2,"type":"ping"}\n\n'], "hang");
      },
    });
    const c = collect();
    const handle = transport.open("/x", c.handlers);
    await settle();
    await settle();
    expect(c.seen).toHaveLength(1);
    handle.close();
    expect(signal?.aborted).toBe(true);
    await settle();
    expect(c.ends).toEqual([]);
  });
});

describe("fetchTransport.json", () => {
  it("posts a JSON body with the credential and parses the answer, whatever the status", async () => {
    const seen: RequestInit[] = [];
    const transport = fetchTransport({
      headers: () => ({ Authorization: "Bearer tok" }),
      fetch: async (_url, init) => {
        seen.push(init ?? {});
        return new Response(JSON.stringify({ error: { message: "no" } }), { status: 409 });
      },
    });
    const out = await transport.json("/v1/sessions/a/queue", { method: "POST", body: { op: "cancel" } });
    expect(out).toEqual({ status: 409, body: { error: { message: "no" } } });
    expect(seen[0]).toMatchObject({
      method: "POST",
      body: '{"op":"cancel"}',
      headers: { Authorization: "Bearer tok", "Content-Type": "application/json" },
    });
  });

  it("defaults to GET and returns an undefined body for an empty or unreadable one", async () => {
    const answers = [new Response(null, { status: 204 }), new Response("<html>", { status: 502 })];
    const methods: (string | undefined)[] = [];
    const transport = fetchTransport({
      fetch: async (_url, init) => {
        methods.push(init?.method);
        return answers.shift() as Response;
      },
    });
    expect(await transport.json("/a")).toEqual({ status: 204, body: undefined });
    expect(await transport.json("/b")).toEqual({ status: 502, body: undefined });
    expect(methods).toEqual(["GET", "GET"]);
  });

  it("rejects when no answer came", async () => {
    const transport = fetchTransport({
      fetch: async () => {
        throw new TypeError("offline");
      },
    });
    await expect(transport.json("/a")).rejects.toThrow("offline");
  });
});

describe("fetchTransport.json timeout", () => {
  it("aborts a request that is not answered in time and rejects, so nothing waits on it for good", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | null | undefined;
      const transport = fetchTransport({
        timeoutMs: 5000,
        fetch: (_url, init) => {
          signal = init?.signal;
          return new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
          });
        },
      });
      const out = transport.json("/a", { method: "POST", body: {} });
      const caught = out.catch((error: Error) => error.name);
      await vi.advanceTimersByTimeAsync(4999);
      expect(signal?.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect(signal?.aborted).toBe(true);
      expect(await caught).toBe("AbortError");
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears its timer when the answer comes", async () => {
    vi.useFakeTimers();
    try {
      const transport = fetchTransport({ fetch: async () => new Response("{}") });
      await transport.json("/a");
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("sessionPath", () => {
  it("encodes the key the way the hub's routes expect", () => {
    expect(sessionPath("claude:3f2a", "events")).toBe("/v1/sessions/claude%3A3f2a/events");
  });
});
