import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useSessionStore, type SessionStore } from "../core";
import { Button, cx, Icon } from "../ui";
import { AssistantMessage, SendBubble, SystemMessage, UserMessage } from "./Bubbles";
import { buildRows, type Row } from "./model";
import { FooterRow, LiveRow, NoticeRow, RequestRow, WorkingRow } from "./Rows";
import { describeConnection } from "./connection";
import { EmptyState, LoadingState, UnavailableState } from "./States";
import { useToggles, WorkGroupView, type Toggles } from "./WorkGroup";

/** How far from the end still counts as "reading the latest". */
const STICK_PX = 64;

const reducedMotion = (): boolean =>
  typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function RowView({
  row,
  toggles,
  onDiscard,
  onRetry,
}: {
  row: Row;
  toggles: Toggles;
  onDiscard(id: string): void;
  onRetry(id: string): void;
}) {
  switch (row.kind) {
    case "user":
      return <UserMessage item={row.item} />;
    case "assistant":
      return <AssistantMessage item={row.item} />;
    case "system":
      return <SystemMessage item={row.item} />;
    case "work":
      return <WorkGroupView group={row} toggles={toggles} />;
    case "request":
      return <RequestRow item={row.item} />;
    case "notice":
      return <NoticeRow item={row.item} />;
    case "live":
      return <LiveRow live={row.live} stopped={row.stopped} />;
    case "working":
      return <WorkingRow />;
    case "footer":
      return <FooterRow footer={row} />;
    case "send":
      return <SendBubble onDiscard={onDiscard} onRetry={onRetry} send={row.send} />;
  }
}

/** What makes a row the same row across a change of kind: a reply is one thing while it streams and after. */
function identity(row: Row): string {
  if (row.kind === "live") return `reply-${row.live.id}`;
  if (row.kind === "assistant") return `reply-${row.item.id}`;
  return row.key;
}

/**
 * The turns of the session: what the person and the agent said, the work between, live text as it is
 * written, and each send that has not reached the timeline yet. It follows the newest content while
 * the person is reading the end, and stays put once they scroll up (a button takes them back).
 */
export function Timeline({ store }: { store: SessionStore }) {
  const items = useSessionStore(store, (s) => s.session.items);
  const live = useSessionStore(store, (s) => s.session.live);
  const turns = useSessionStore(store, (s) => s.session.turns);
  const stopped = useSessionStore(store, (s) => s.session.stopped);
  const openTurn = useSessionStore(store, (s) => s.session.openTurn);
  const phase = useSessionStore(store, (s) => s.session.phase);
  const loaded = useSessionStore(store, (s) => s.session.loaded);
  const hasMore = useSessionStore(store, (s) => s.session.hasMore);
  const caughtUp = useSessionStore(store, (s) => s.session.caughtUp);
  const sends = useSessionStore(store, (s) => s.sends);
  const connection = useSessionStore(store, (s) => s.connection);

  const rows = useMemo(
    () => buildRows({ items, live, turns, stopped, openTurn, phase }, sends),
    [items, live, turns, stopped, openTurn, phase, sends],
  );
  const toggles = useToggles();

  const scroller = useRef<HTMLElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  const [atEnd, setAtEnd] = useState(true);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);

  const toEnd = useCallback((smooth: boolean) => {
    const el = scroller.current;
    if (el === null) return;
    if (smooth && !reducedMotion() && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    else el.scrollTop = el.scrollHeight;
  }, []);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (el === null) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
    stuck.current = near;
    setAtEnd(near);
  }, []);

  // New rows, or new text in the last one: stay at the end if that is where the person is. After older
  // history was added at the top, keep what they were looking at where it was.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el === null) return;
    if (anchor.current !== null) {
      el.scrollTop = anchor.current.top + (el.scrollHeight - anchor.current.height);
      anchor.current = null;
    } else if (stuck.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [rows]);

  // A group opening or closing changes the height with no new row: follow that too.
  useEffect(() => {
    const target = content.current;
    if (target === null || typeof ResizeObserver === "undefined") return;
    const watcher = new ResizeObserver(() => {
      if (stuck.current && scroller.current !== null) scroller.current.scrollTop = scroller.current.scrollHeight;
    });
    watcher.observe(target);
    return () => watcher.disconnect();
  }, [loaded]);

  // Rows that appear after the first draw fade in; the first draw (a whole history) does not. A reply that
  // finishes keeps its place without a second entrance: its live row and its message share an identity.
  const seen = useRef<Set<string> | null>(null);
  const fresh = seen.current;
  useEffect(() => {
    if (!loaded) return;
    seen.current = new Set(rows.flatMap((row) => [row.key, identity(row)]));
  }, [rows, loaded]);

  const discard = useCallback((id: string) => void store.getState().discardSend(id), [store]);
  const retry = useCallback((id: string) => void store.getState().retrySend(id), [store]);
  const loadOlder = useCallback(async () => {
    const el = scroller.current;
    if (el !== null) anchor.current = { height: el.scrollHeight, top: el.scrollTop };
    setLoadingOlder(true);
    try {
      await store.getState().loadOlder();
    } finally {
      setLoadingOlder(false);
      // Nothing was added: do not hold the anchor for the next unrelated render.
      window.setTimeout(() => (anchor.current = null), 0);
    }
  }, [store]);

  let body;
  if (!loaded && rows.length === 0) {
    const look = describeConnection(connection, caughtUp);
    if (["gone", "outdated", "unauthorized"].includes(connection.state)) {
      body = <UnavailableState colour={look.colour} text={look.banner ?? ""} title={look.label} />;
    } else if (["reconnecting", "hub_down", "offline"].includes(connection.state)) {
      body = <UnavailableState colour={look.colour} text={look.banner ?? ""} title="Cannot load this session yet" />;
    } else {
      body = <LoadingState />;
    }
  } else if (rows.length === 0) {
    body = <EmptyState />;
  } else {
    body = (
      <div ref={content} aria-label="Conversation" className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-5" role="log">
        {hasMore ? (
          <div className="flex justify-center">
            <Button disabled={loadingOlder} onClick={() => void loadOlder()}>
              {loadingOlder ? "Loading" : "Load earlier messages"}
            </Button>
          </div>
        ) : null}
        {rows.map((row) => (
          <div key={row.key} className={cx(fresh !== null && !fresh.has(row.key) && !fresh.has(identity(row)) && "sv-enter")} data-row-kind={row.kind}>
            <RowView onDiscard={discard} onRetry={retry} row={row} toggles={toggles} />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <main ref={scroller} aria-label="Timeline" className="flex min-h-0 flex-1 flex-col overflow-y-auto pr-safe-right pl-safe-left" onScroll={onScroll}>
        {body}
      </main>
      {!atEnd ? (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <Button
            className="sv-enter pointer-events-auto bg-card shadow-sm"
            onClick={() => {
              stuck.current = true;
              toEnd(true);
            }}
          >
            <Icon className="size-3.5" name="arrowDown" />
            Jump to latest
          </Button>
        </div>
      ) : null}
    </div>
  );
}
