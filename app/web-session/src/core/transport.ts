// The wire: the one place that talks to the network. The supervisor, the
// outbox and the store use the `Transport` interface, so every test drives
// them with a scripted one and never opens a socket.
//
// The real transport reads SSE with fetch streams, as chat.js does:
// EventSource cannot send the Authorization header.
/** How a stream ended. */
export type StreamEnd =
  /** The server closed the stream, or the connection dropped mid-way. */
  | { kind: "closed" }
  /** The server answered the request with this status instead of a stream. */
  | { kind: "http"; status: number }
  /** The request never got an answer: no network, refused, DNS, a TLS failure. */
  | { kind: "error"; message: string };

export interface StreamHandlers {
  /** The response headers arrived with a 200: the stream is open. */
  onOpen(): void;
  /** One parsed JSON value. The supervisor checks that it is a v2 frame. */
  onFrame(frame: unknown): void;
  /** Called once, last. Never called after `close()`. */
  onEnd(end: StreamEnd): void;
}

export interface StreamHandle {
  close(): void;
}

export interface JsonResponse {
  status: number;
  /** The parsed body, or undefined when it was empty or not JSON. */
  body: unknown;
}

export interface JsonRequest {
  method?: "GET" | "POST";
  body?: unknown;
}

export interface Transport {
  /** Open a server-sent-event stream at `path` (with its query). */
  open(path: string, handlers: StreamHandlers): StreamHandle;
  /**
   * A JSON request. Resolves with the status of any answer, 4xx and 5xx
   * included; rejects only when no answer came (the network is down).
   */
  json(path: string, request?: JsonRequest): Promise<JsonResponse>;
}

// ---------------------------------------------------------------------
// SSE parsing

/** Feeds text in, calls `onEvent` for each complete SSE event (its `data:` lines joined). */
export class SseParser {
  private buffer = "";
  private data: string[] = [];

  constructor(private readonly onEvent: (data: string) => void) {}

  push(chunk: string): void {
    this.buffer += chunk;
    for (;;) {
      const cut = this.buffer.search(/\r\n|\r|\n/);
      if (cut < 0) return;
      const match = /^(\r\n|\r|\n)/.exec(this.buffer.slice(cut)) as RegExpExecArray;
      // A lone "\r" at the very end may be half of "\r\n": wait for more.
      if (match[1] === "\r" && cut + 1 === this.buffer.length) return;
      const line = this.buffer.slice(0, cut);
      this.buffer = this.buffer.slice(cut + (match[1] as string).length);
      this.line(line);
    }
  }

  private line(line: string): void {
    if (line === "") {
      if (this.data.length > 0) {
        const joined = this.data.join("\n");
        this.data = [];
        this.onEvent(joined);
      }
      return;
    }
    if (line.startsWith(":")) return; // a comment (the keep-alive)
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    // `event:`, `id:` and `retry:` carry nothing the frame's JSON does not.
  }
}

// ---------------------------------------------------------------------
// The fetch transport

export interface FetchTransportOptions {
  /** Where the hub is: "" for the page's own origin. */
  baseUrl?: string;
  /** The headers of every request (the credential). Read at each request, so a rotated token is picked up. */
  headers?: () => Record<string, string>;
  fetch?: typeof fetch;
  /**
   * How long a JSON request may take, in milliseconds (default 30 s). A request that has not been
   * answered by then is aborted and rejects, as if the network were down: a request that hangs
   * (a device that slept, a network that changed) would otherwise hold the outbox for good.
   */
  timeoutMs?: number;
}

export function fetchTransport(options: FetchTransportOptions = {}): Transport {
  const base = (options.baseUrl ?? "").replace(/\/$/, "");
  const doFetch = options.fetch ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const headers = (extra?: Record<string, string>) => ({ ...(options.headers?.() ?? {}), ...extra });

  return {
    open(path, handlers) {
      const controller = new AbortController();
      let closed = false;
      const end = (value: StreamEnd) => {
        if (closed) return;
        closed = true;
        handlers.onEnd(value);
      };
      void (async () => {
        try {
          const response = await doFetch(base + path, {
            headers: headers({ Accept: "text/event-stream" }),
            signal: controller.signal,
            cache: "no-store",
          });
          if (closed) return;
          if (!response.ok || response.body === null) {
            end({ kind: "http", status: response.status });
            return;
          }
          handlers.onOpen();
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          const parser = new SseParser((data) => {
            if (closed) return;
            let value: unknown;
            try {
              value = JSON.parse(data);
            } catch {
              return;
            }
            handlers.onFrame(value);
          });
          for (;;) {
            const { done, value } = await reader.read();
            if (done || closed) break;
            parser.push(decoder.decode(value, { stream: true }));
          }
          end({ kind: "closed" });
        } catch (error) {
          if (closed) return;
          end({ kind: "error", message: error instanceof Error ? error.message : String(error) });
        }
      })();
      return {
        close() {
          if (closed) return;
          closed = true;
          controller.abort();
        },
      };
    },

    async json(path, request = {}) {
      const body = request.body === undefined ? undefined : JSON.stringify(request.body);
      const controller = new AbortController();
      const timer = globalThis.setTimeout(() => controller.abort(), options.timeoutMs ?? 30_000);
      try {
        const response = await doFetch(base + path, {
          method: request.method ?? (body === undefined ? "GET" : "POST"),
          headers: headers(body === undefined ? undefined : { "Content-Type": "application/json" }),
          ...(body === undefined ? {} : { body }),
          cache: "no-store",
          signal: controller.signal,
        });
        const text = await response.text();
        let parsed: unknown;
        if (text !== "") {
          try {
            parsed = JSON.parse(text);
          } catch {
            parsed = undefined;
          }
        }
        return { status: response.status, body: parsed };
      } finally {
        globalThis.clearTimeout(timer);
      }
    },
  };
}

/** The path of a session route: `/v1/sessions/<key>/<tail>`. */
export function sessionPath(key: string, tail: string): string {
  return `/v1/sessions/${encodeURIComponent(key)}/${tail}`;
}
