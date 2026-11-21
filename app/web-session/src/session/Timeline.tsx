import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useSessionStore, type SessionStore } from "../core";
import { Button, cx, Icon } from "../ui";
import { AssistantMessage, SendBubble, SystemMessage, UserMessage } from "./Bubbles";
import { buildRows, rowAnchorIds, rowIdentity, rowsEqual, type Row } from "./model";
import { RowBoundary } from "./RowBoundary";
import { FooterRow, LiveRow, NoticeRow, RequestRow, WorkingRow } from "./Rows";
import { describeConnection } from "./connection";
import { EmptyState, LoadingState, UnavailableState } from "./States";
import { useToggles, WorkGroupView, type Toggles } from "./WorkGroup";

/** How far from the end still counts as "reading the latest". */
const STICK_PX = 64;

/** How long after the person opens or closes something until the window is asked where they are (the disclosure animates for 200 ms). */
const SETTLE_MS = 350;

const reducedMotion = (): boolean =>
  typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Drawn again only when its row, the toggles, or the sends' refusals change (see `rowsEqual`). */
const RowView = memo(function RowView({
  row,
  toggles,
  onDiscard,
  onRetry,
  refused,
}: {
  row: Row;
  toggles: Toggles;
  onDiscard(id: string): void;
  onRetry(id: string): void;
  refused: Record<string, string>;
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
      return <SendBubble note={refused[row.send.commandId]} onDiscard={onDiscard} onRetry={onRetry} send={row.send} />;
  }
}, (before, after) => rowsEqual(before.row, after.row) && before.toggles === after.toggles && before.refused === after.refused && before.onDiscard === after.onDiscard && before.onRetry === after.onRetry);

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
  const allSends = useSessionStore(store, (s) => s.sends);
  const queue = useSessionStore(store, (s) => s.session.queue);
  const connection = useSessionStore(store, (s) => s.connection);

  // A send the hub holds in its queue is drawn by the composer's queued tray (G13), not twice.
  const sends = useMemo(() => {
    if (queue.length === 0) return allSends;
    const held = new Set(queue.map((item) => item.command_id));
    return allSends.filter((send) => !held.has(send.commandId));
  }, [allSends, queue]);
  const rows = useMemo(
    () => buildRows({ items, live, turns, stopped, openTurn, phase }, sends),
    [items, live, turns, stopped, openTurn, phase, sends],
  );
  const scroller = useRef<HTMLElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  const [atEnd, setAtEnd] = useState(true);
  // The row the person was reading when older history was asked for, where it sat in the window, and the
  // first item then. The position is restored by that row, and only once a page has arrived in front of
  // the first item: what a reply growing at the bottom adds meanwhile does not move it.
  const anchor = useRef<{ ids: string[]; offset: number; first: number | undefined } | null>(null);
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

  // Opening or closing something is the person's own doing: the height it changes is not a reason to jump to
  // the end (which would carry the header they just pressed off the top). They stop following the end until
  // the change has settled and the window says where they are.
  const settling = useRef<number | null>(null);
  const onUserToggle = useCallback(() => {
    stuck.current = false;
    if (settling.current !== null) window.clearTimeout(settling.current);
    settling.current = window.setTimeout(() => {
      settling.current = null;
      onScroll();
    }, SETTLE_MS);
  }, [onScroll]);
  useEffect(
    () => () => {
      if (settling.current !== null) window.clearTimeout(settling.current);
    },
    [],
  );
  const toggles = useToggles(onUserToggle);

  // New rows, or new text in the last one: stay at the end if that is where the person is. After older
  // history was added at the top, keep the row they were reading where it was.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el === null) return;
    const held = anchor.current;
    if (held !== null) {
      if (items[0]?.seq !== held.first) {
        restore(el, held);
        anchor.current = null;
      }
      return;
    }
    if (stuck.current) el.scrollTop = el.scrollHeight;
  }, [rows, items]);

  // A change of height with no new row (a reply wrapping, an image) follows the end too.
  useEffect(() => {
    const target = content.current;
    if (target === null || typeof ResizeObserver === "undefined") return;
    const watcher = new ResizeObserver(() => {
      if (stuck.current && anchor.current === null && scroller.current !== null) scroller.current.scrollTop = scroller.current.scrollHeight;
    });
    watcher.observe(target);
    return () => watcher.disconnect();
  }, [rows.length > 0]);

  // Rows that appear after the first draw fade in; the first draw (a whole history) does not. A reply that
  // finishes keeps its place without a second entrance: its live row and its message share an identity.
  const seen = useRef<Set<string> | null>(null);
  const fresh = seen.current;
  useEffect(() => {
    if (!loaded) return;
    seen.current = new Set(rows.flatMap((row) => [row.key, rowIdentity(row)]));
  }, [rows, loaded]);

  const discard = useCallback((id: string) => void store.getState().discardSend(id), [store]);
  // A Retry the store refuses (the session became read-only, or is gone) says why, under that send.
  const [refused, setRefused] = useState<Record<string, string>>({});
  const retry = useCallback(
    (id: string) => {
      setRefused(({ [id]: _gone, ...rest }) => rest);
      void store
        .getState()
        .retrySend(id)
        .then((result) => {
          if (result !== undefined && !result.ok) setRefused((held) => ({ ...held, [id]: result.message }));
        });
    },
    [store],
  );
  const loadOlder = useCallback(async () => {
    const el = scroller.current;
    const reading = el === null || content.current === null ? null : firstVisible(el, content.current);
    if (reading !== null) anchor.current = { ...reading, first: items[0]?.seq };
    setLoadingOlder(true);
    try {
      await store.getState().loadOlder();
    } finally {
      setLoadingOlder(false);
      // Nothing was added: do not hold the anchor for the next unrelated render.
      window.setTimeout(() => (anchor.current = null), 0);
    }
  }, [store, items]);

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
      <div ref={content} aria-label="Conversation" className="sv-pad-end mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 pt-5" role="log">
        {hasMore ? (
          <div className="flex justify-center">
            <Button disabled={loadingOlder} onClick={() => void loadOlder()}>
              {loadingOlder ? "Loading" : "Load earlier messages"}
            </Button>
          </div>
        ) : null}
        {rows.map((row) => (
          <div
            key={row.key}
            className={cx(fresh !== null && !fresh.has(row.key) && !fresh.has(rowIdentity(row)) && "sv-enter")}
            data-row-key={row.key}
            data-row-ids={rowAnchorIds(row).join(" ")}
            data-row-kind={row.kind}
          >
            <RowBoundary resetKey={subject(row)}>
              <RowView onDiscard={discard} onRetry={retry} refused={refused} row={row} toggles={toggles} />
            </RowBoundary>
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
        <div className="sv-jump pointer-events-none absolute inset-x-0 flex justify-center">
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

const objectIds = new WeakMap<object, number>();
let nextObjectId = 1;
/** A number for an object that stays with it, so a list of objects can be compared as a string. */
function objectId(value: object): number {
  let id = objectIds.get(value);
  if (id === undefined) objectIds.set(value, (id = nextObjectId++));
  return id;
}

/** What a row is made of, for telling a changed row from the same one made again. */
export function subject(row: Row): unknown {
  switch (row.kind) {
    case "live":
      return row.live;
    case "work":
      // Any step changing is a change: a failed row is tried again when any of them is.
      return row.entries.map(objectId).join(",");
    case "footer":
      return row.turn;
    case "send":
      return row.send.reason + row.send.shown;
    case "working":
      return row.key;
    default:
      return row.item;
  }
}

/** The first row with any of it in the window, how far below the window's top it starts, and what to find it by. */
export function firstVisible(scroller: HTMLElement, content: HTMLElement): { ids: string[]; offset: number } | null {
  const top = scroller.getBoundingClientRect().top;
  for (const el of Array.from(content.querySelectorAll<HTMLElement>("[data-row-ids]"))) {
    const box = el.getBoundingClientRect();
    if (box.bottom > top) return { ids: (el.dataset["rowIds"] ?? "").split(" ").filter(Boolean), offset: box.top - top };
  }
  return null;
}

/**
 * Scroll so the row sits as far below the window's top as it did. The row is found by any of its names (a work
 * group that older steps joined has another key, and still holds the steps it had). A row that is gone leaves the
 * window as it is.
 */
function restore(scroller: HTMLElement, held: { ids: string[]; offset: number }): void {
  const row = Array.from(scroller.querySelectorAll<HTMLElement>("[data-row-ids]")).find((el) =>
    (el.dataset["rowIds"] ?? "").split(" ").some((id) => held.ids.includes(id)),
  );
  if (row === undefined) return;
  const now = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
  scroller.scrollTop += now - held.offset;
}
