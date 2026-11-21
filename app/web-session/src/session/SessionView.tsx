import "./session.css";

import { useSessionStore, type SessionStore } from "../core";
import { describeKey } from "../session-key";
import { ComposerPlaceholder } from "./ComposerPlaceholder";
import { describeConnection } from "./connection";
import { Header } from "./Header";
import { rowLabels } from "./model";
import { Timeline } from "./Timeline";
import { TimelineBoundary } from "./RowBoundary";
import { StateArrow } from "../ui";

/**
 * The session view over a store: header, connection banner, timeline, and the composer's place. The
 * caller owns the store's life (`start()` when it mounts, `stop()` when it goes: SessionShell does).
 */
export function SessionView({ store, sessionKey, now = Date.now }: { store: SessionStore; sessionKey: string; now?: () => number }) {
  const row = useSessionStore(store, (s) => s.session.row);
  const phase = useSessionStore(store, (s) => s.session.phase);
  const caughtUp = useSessionStore(store, (s) => s.session.caughtUp);
  const connection = useSessionStore(store, (s) => s.connection);
  const labels = rowLabels(row);
  const fallback = describeKey(sessionKey);
  const crumbs = { project: labels.project ?? fallback.project, title: labels.title ?? fallback.title };
  const loaded = useSessionStore(store, (s) => s.session.loaded);
  const look = describeConnection(connection, caughtUp);
  // Before anything loaded, the timeline's own state says what is wrong; the banner is for a view with something on it.
  const banner = loaded ? look.banner : null;

  return (
    <div className="swe-sv flex h-full min-h-0 flex-col bg-panel text-primary">
      <Header caughtUp={caughtUp} connection={connection} crumbs={crumbs} now={now} phase={phase} />
      {banner !== null ? (
        <div className="sv-banner-enter shrink-0 border-b border-border bg-subtle pr-safe-right pl-safe-left" data-banner={connection.state} role="status">
          <p className="mx-auto flex max-w-3xl items-start gap-2 px-4 py-2 text-xs text-secondary">
            <StateArrow className="mt-0.5" colour={look.colour} />
            {banner}
          </p>
        </div>
      ) : null}
      <TimelineBoundary>
        <Timeline store={store} />
      </TimelineBoundary>
      <ComposerPlaceholder store={store} />
    </div>
  );
}
