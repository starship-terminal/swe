import { useEffect, useState } from "react";

import type { ConnectionStatus } from "../core";
import { StateArrow } from "../ui";
import { describeConnection, phaseLabel, secondsUntil } from "./connection";
import type { Crumbs } from "../session-key";

/** Seconds to the next try, ticking once a second while there is one. */
function useCountdown(retryAt: number | null, active: boolean, now: () => number): number | null {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!active || retryAt === null) return;
    const id = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [active, retryAt]);
  return active ? secondsUntil(retryAt, now()) : null;
}

export function ConnectionDot({ connection, caughtUp, now }: { connection: ConnectionStatus; caughtUp: boolean; now: () => number }) {
  const look = describeConnection(connection, caughtUp);
  const left = useCountdown(connection.retryAt, look.counts, now);
  return (
    <span className="flex shrink-0 items-center gap-1.5 text-xs text-secondary" data-connection={connection.state} title={connection.detail === "" ? undefined : connection.detail}>
      <StateArrow colour={look.colour} />
      <span>{left === null ? look.label : `${look.label}, retrying in ${left}s`}</span>
    </span>
  );
}

export function Header({
  crumbs,
  connection,
  caughtUp,
  phase,
  now,
}: {
  crumbs: Crumbs;
  connection: ConnectionStatus;
  caughtUp: boolean;
  phase: string | null;
  now: () => number;
}) {
  const state = phaseLabel(phase);
  return (
    <header className="shrink-0 border-b border-border pt-safe-top pr-safe-right pl-safe-left">
      <div className="flex h-12 min-w-0 items-center justify-between gap-3 px-4">
        <nav aria-label="Breadcrumb" className="min-w-0">
          <ol className="flex min-w-0 items-center gap-1.5 text-sm">
            <li className="shrink-0 text-secondary">{crumbs.project}</li>
            <li aria-hidden="true" className="shrink-0 text-muted">
              /
            </li>
            <li aria-current="page" className="truncate font-medium" title={crumbs.title}>
              {crumbs.title}
            </li>
          </ol>
        </nav>
        <div className="flex shrink-0 items-center gap-3">
          {state !== null ? (
            <span className="hidden text-xs text-secondary sm:inline" data-phase={phase}>
              {state}
            </span>
          ) : null}
          <ConnectionDot caughtUp={caughtUp} connection={connection} now={now} />
        </div>
      </div>
    </header>
  );
}
