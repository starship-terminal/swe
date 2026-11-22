// The device credential the hub's /v1 routes want, read the way the web app does (app.js): the
// saved pairing under `swe.remote` (or the name it had before the rename). Inside the Mac and iPhone
// shells there is none here and none needed: their scheme handler adds the bearer itself.
const KEYS = ["swe.remote", "quiver.remote"];

function saved(): string {
  try {
    for (const key of KEYS) {
      const raw = window.localStorage.getItem(key);
      if (raw === null) continue;
      const credential = (JSON.parse(raw) as { credential?: unknown } | null)?.credential;
      if (typeof credential === "string" && credential !== "") return credential;
    }
  } catch {
    // Storage off, or not JSON: there is no credential to send.
  }
  return "";
}

/** Whether this browser still holds a device credential (inside the Mac and iPhone shells there is none and none is needed). */
export function hasCredential(): boolean {
  return saved() !== "";
}

/** `X-Swe-Pair` marks a request as this page's own (the hub only honours a tailnet identity with it). */
export function authHeaders(): Record<string, string> {
  const credential = saved();
  return { "X-Swe-Pair": "1", ...(credential === "" ? {} : { Authorization: `Bearer ${credential}` }) };
}
