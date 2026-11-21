import { useEffect, useState } from "react";

import { createSessionStore, fetchTransport, type SessionStore } from "./core";
import { authHeaders, SessionView } from "./session";
import { describeKey } from "./session-key";
import { StateArrow } from "./ui";

export { COMPOSER_PLACEHOLDER } from "./session";

interface Props {
  sessionKey: string;
  /** A store to draw instead of making one over the hub (tests and the screens page). The caller starts it. */
  store?: SessionStore;
}

/** The store of one session over the hub, started while the view is on screen and stopped when it goes. */
function Connected({ sessionKey }: { sessionKey: string }) {
  const [store] = useState(() => createSessionStore({ sessionKey, transport: fetchTransport({ baseUrl: "", headers: authHeaders }) }));
  useEffect(() => {
    store.getState().start();
    return () => store.getState().stop();
  }, [store]);
  return <SessionView sessionKey={sessionKey} store={store} />;
}

/** The page's header when no session was named: nothing to connect to. */
function NoSession() {
  const crumbs = describeKey("");
  return (
    <div className="swe-sv flex h-full min-h-0 flex-col bg-panel text-primary">
      <header className="shrink-0 border-b border-border pt-safe-top pr-safe-right pl-safe-left">
        <nav aria-label="Breadcrumb" className="flex h-12 min-w-0 items-center px-4">
          <ol className="flex min-w-0 items-center gap-1.5 text-sm">
            <li className="shrink-0 text-secondary">{crumbs.project}</li>
            <li aria-hidden="true" className="shrink-0 text-muted">
              /
            </li>
            <li aria-current="page" className="truncate font-medium">
              {crumbs.title}
            </li>
          </ol>
        </nav>
      </header>
      <main aria-label="Timeline" className="flex min-h-0 flex-1 overflow-y-auto">
        <div className="m-auto flex max-w-sm flex-col items-center gap-1.5 px-6 py-12 text-center">
          <p className="flex items-center gap-1.5 text-sm font-medium">
            <StateArrow colour="grey" />
            No session selected
          </p>
          <p className="text-xs text-secondary">Open a session from the list to see its conversation here.</p>
        </div>
      </main>
    </div>
  );
}

/** The session view for a session key: the hub's stream, drawn. An empty key has nothing to draw. */
export function SessionShell({ sessionKey, store }: Props) {
  if (store !== undefined) return <SessionView sessionKey={sessionKey} store={store} />;
  if (sessionKey.trim() === "") return <NoSession />;
  return <Connected key={sessionKey} sessionKey={sessionKey} />;
}
