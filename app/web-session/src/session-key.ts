/** What the breadcrumb shows for a session before any data loads. */
export interface Crumbs {
  project: string;
  title: string;
}

/** The `key` query parameter of a page URL's search string, or "". */
export function keyFromSearch(search: string): string {
  return (new URLSearchParams(search).get("key") ?? "").trim();
}

/**
 * The breadcrumb for a hub session key (`tool:sid`, or `tool:sid@machine`
 * for a satellite's; see `protocol.session_key` and `remote_key`). This
 * view loads no session data yet (G11 adds the snapshot), so the harness
 * stands where the project will go and the session id where its title will.
 */
export function describeKey(key: string): Crumbs {
  let trimmed = key.trim();
  const at = trimmed.lastIndexOf("@");
  if (at > 0 && at < trimmed.length - 1) trimmed = trimmed.slice(0, at);
  if (!trimmed) return { project: "SWE", title: "No session" };
  const colon = trimmed.indexOf(":");
  if (colon <= 0 || colon === trimmed.length - 1) return { project: "SWE", title: trimmed };
  return { project: trimmed.slice(0, colon), title: trimmed.slice(colon + 1) };
}
