// Staging a file for a message: the hub keeps it under the session and answers a `ref` the message lists
// (`POST /v1/sessions/<key>/attachments?name=...`, the raw bytes as the body). The store has no action for
// it, so the box does the request itself, with the same credential the stream uses.
import { sessionPath } from "../core";
import { authHeaders } from "../session/credential";
import type { StagedFile } from "./draft";

/** The hub's cap on one file. */
export const MAX_FILE_BYTES = 20 << 20;

export type Upload = (file: File, signal: AbortSignal) => Promise<StagedFile>;

export function httpUpload(sessionKey: string): Upload {
  return async (file, signal) => {
    if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name} is over 20 MB.`);
    const response = await fetch(`${sessionPath(sessionKey, "attachments")}?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": file.type !== "" ? file.type : "application/octet-stream" },
      body: file,
      signal,
    });
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = undefined;
    }
    const value = body as { ref?: unknown; name?: unknown; size?: unknown; error?: { message?: unknown } } | undefined;
    if (!response.ok) {
      const why = typeof value?.error?.message === "string" && value.error.message !== "" ? value.error.message : `The hub answered ${response.status}`;
      throw new Error(`${file.name}: ${why}`);
    }
    if (typeof value?.ref !== "string" || value.ref === "") throw new Error(`${file.name}: the hub did not name the staged file`);
    return { ref: value.ref, name: typeof value.name === "string" && value.name !== "" ? value.name : file.name, size: typeof value.size === "number" ? value.size : file.size };
  };
}
