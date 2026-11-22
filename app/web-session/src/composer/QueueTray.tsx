import { useEffect, useRef, useState, type ComponentProps, type MouseEvent } from "react";

import { useSessionStore, type SessionStore } from "../core";
import { cx, Icon, Spinner } from "../ui";
import { AnswerButton } from "./AnswerButton";
import { ErrorNote } from "./ErrorNote";
import { Guard } from "./Guard";
import { Menu } from "./Menu";
import { usePresses } from "./press";
import { steerEffectOf, steerWording } from "./steer";
import { clip, plural } from "./text";

type Queued = { command_id: string; text: string; by?: string; mode?: "queue" | "steer"; attachments?: string[] };

/** How many queued messages the tray draws; the hub's own queue is capped well below a screenful. */
export const TRAY_SHOWN = 20;
/** How much of a queued message the tray draws. */
const TEXT_SHOWN = 2000;

interface Edit {
  text: string;
  original: string;
  /** The hub accepted the edit; it is not shown in the queue yet. The words are kept until it is (or the message leaves). */
  pending?: boolean;
  /** The hub accepted the edit and it never showed within `confirmMs`: the words are back in the editor. */
  unconfirmed?: boolean;
  /** The text this person last had the hub accept: when the queue shows it, it is their own save arriving, not another device's change. */
  saved?: string;
}

type Action = "edit" | "cancel" | "steer";

/** A small round button with a picture and a name. */
function IconButton({ label, icon, danger = false, ...rest }: { label: string; icon: "edit" | "trash" | "more" | "check" | "x"; danger?: boolean } & ComponentProps<"button">) {
  return (
    <button
      aria-label={label}
      className={cx(
        "sv-press grid size-8 shrink-0 place-items-center rounded-full motion-safe:transition-colors hover:bg-hover focus-visible:outline-2 focus-visible:outline-interactive disabled:cursor-not-allowed disabled:opacity-40",
        danger ? "text-secondary hover:text-danger-text" : "text-secondary hover:text-primary",
      )}
      title={label}
      type="button"
      {...rest}
    >
      <Icon className="size-4" name={icon} />
    </button>
  );
}

function Item({
  store,
  item,
  edit,
  setEdit,
  steerOk,
  steerEffect,
  refusal,
  settleMs,
  confirmMs,
}: {
  store: SessionStore;
  item: Queued;
  edit: Edit | undefined;
  setEdit(id: string, edit: Edit | undefined): void;
  steerOk: boolean;
  steerEffect: ReturnType<typeof steerEffectOf>;
  refusal: string | null;
  settleMs: number;
  confirmMs: number;
}) {
  const requestOpen = useSessionStore(store, (s) => s.session.pending.length > 0);
  const [busy, setBusy] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const pressing = useRef(false);
  const more = useRef<HTMLButtonElement>(null);
  const trash = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  // Taking a message out is two presses: the first says what the second does, and the second counts after a wait,
  // as a press of its own (see press.ts), so a double click or a held key cannot do both.
  const [removing, setRemoving] = useState(false);
  const [removeReady, setRemoveReady] = useState(false);
  useEffect(() => {
    if (!removing) return;
    setRemoveReady(false);
    const timer = setTimeout(() => setRemoveReady(true), settleMs);
    return () => clearTimeout(timer);
  }, [removing]);
  const presses = usePresses();
  // The trash button is replaced, under the finger, by the confirmation: the keyboard focus goes with it.
  useEffect(() => {
    if (removing) confirmButton.current?.focus();
  }, [removing]);
  const id = item.command_id;
  // Another device changed this message while it is being edited here: Save would write over it unseen.
  // (A message that already says what the person typed is no change under them: a late echo of their own save.)
  const changedUnder = edit !== undefined && edit.pending !== true && item.text !== edit.original && item.text !== edit.text && item.text !== edit.saved;
  // An accepted edit that has not shown in the queue: its words stay (here and in the tray's list of edits) until it does.
  useEffect(() => {
    if (edit === undefined || edit.pending !== true) return;
    const timer = setTimeout(() => setEdit(id, { text: edit.text, original: edit.original, unconfirmed: true, ...(edit.saved === undefined ? {} : { saved: edit.saved }) }), confirmMs);
    return () => clearTimeout(timer);
  }, [edit?.pending, confirmMs]);
  const shown = clip(item.text, TEXT_SHOWN, 60);

  const run = async (action: Action, text?: string): Promise<boolean> => {
    // One press, one request: a double click gets in before `busy` is drawn.
    if (pressing.current) return false;
    pressing.current = true;
    setBusy(action);
    setError(null);
    try {
      const result = await store.getState().queueAction(action, id, text);
      if (!result.ok) setError(result.message);
      return result.ok;
    } finally {
      pressing.current = false;
      setBusy(null);
    }
  };

  const idle = busy === null && refusal === null;
  return (
    <li className="rounded-block bg-subtle px-2 py-1.5" data-queued={id}>
      {edit === undefined || edit.pending === true ? (
        <>
          <div className="flex items-center gap-1">
            <div className="min-w-0 flex-1 px-1">
              <p className="line-clamp-2 text-sm leading-5 break-words whitespace-pre-wrap" data-untrusted="queued">
                {shown.text}
              </p>
              {shown.hidden > 0 ? <p className="text-xs text-muted">{shown.hidden.toLocaleString("en-US")} more characters not shown.</p> : null}
              {item.attachments !== undefined && item.attachments.length > 0 ? (
                <p className="mt-0.5 flex items-center gap-1 text-xs text-secondary">
                  <Icon className="size-3.5" name="attach" />
                  {plural(item.attachments.length, "attachment")}
                </p>
              ) : null}
            </div>
            {busy !== null ? <Spinner /> : null}
            <IconButton disabled={!idle || edit?.pending === true} icon="edit" label="Edit queued message" onClick={() => setEdit(id, { text: item.text, original: item.text })} />
            <IconButton ref={trash} danger disabled={!idle} icon="trash" label="Remove queued message" onClick={() => setRemoving(true)} />
            {steerEffect !== "next_turn" ? (
              <IconButton ref={more} aria-expanded={menu} aria-haspopup="menu" disabled={!idle} icon="more" label="More for queued message" onClick={() => setMenu((now) => !now)} />
            ) : null}
          </div>
          {edit?.pending === true ? (
            <p className="px-1 text-xs text-secondary" data-edit-pending role="status">
              Saving your edit. Your words are kept until it shows here.
            </p>
          ) : null}
          {removing ? (
            <div className="mt-1 flex flex-wrap items-center gap-1.5" data-remove-confirm>
              <AnswerButton
                ref={confirmButton}
                aria-disabled={!idle || !removeReady ? true : undefined}
                onClick={(click: MouseEvent<HTMLButtonElement>) => {
                  if (!presses.counts(click, "remove") || !idle || !removeReady) return;
                  setRemoving(false);
                  void run("cancel");
                }}
                onKeyDown={(key) => presses.key(key, "remove", removeReady)}
                onKeyUp={() => presses.keyUp()}
                onPointerDown={() => presses.down("remove", removeReady)}
                variant="danger"
              >
                Press again to remove
              </AnswerButton>
              <AnswerButton
                onClick={() => {
                  setRemoving(false);
                  queueMicrotask(() => trash.current?.focus());
                }}
                variant="plain"
              >
                Keep it
              </AnswerButton>
              <span className="text-xs text-secondary">{removeReady ? "This takes the message out of the queue." : "One moment, then press again."}</span>
            </div>
          ) : null}
          {menu ? (
            <div className="mt-1 flex">
              <Menu
                items={[
                  {
                    id: "steer",
                    label: steerWording(steerEffect, requestOpen).label,
                    icon: steerWording(steerEffect, requestOpen).stops ? "stop" : "queue",
                    hint: steerWording(steerEffect, requestOpen).sentence,
                    disabled: !steerOk,
                    reason: "This agent cannot be steered.",
                    onSelect: () => void run("steer"),
                  },
                ]}
                label="Queued message options"
                onClose={() => {
                  setMenu(false);
                  // The focus goes back to what opened the menu, as the bar's menus do.
                  queueMicrotask(() => more.current?.focus());
                }}
                placement="inline"
              />
            </div>
          ) : null}
        </>
      ) : (
        <>
          <textarea
            aria-label="Edit queued message"
            className="min-h-16 w-full resize-y rounded-field border border-border bg-card px-2 py-1.5 text-sm leading-6 focus:border-interactive focus:outline-none"
            onChange={(event) => setEdit(id, { ...edit, text: event.currentTarget.value })}
            readOnly={busy !== null}
            value={edit.text}
          />
          {edit.unconfirmed === true ? (
            <p className="mt-1 text-xs text-secondary" data-edit-unconfirmed role="status">
              The hub did not show your edit in the queue. Your words are still here: save again, or cancel.
            </p>
          ) : null}
          {changedUnder ? (
            <div className="mt-1 rounded-block border border-border bg-card px-2 py-1.5" data-edit-conflict role="group" aria-label="The message changed while you were editing">
              <p className="text-xs text-secondary">This message was changed on another device while you were editing it. The new text:</p>
              <p className="mt-1 text-sm leading-6 break-words whitespace-pre-wrap" data-untrusted="queued-newer">
                {clip(item.text, TEXT_SHOWN, 60).text}
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <AnswerButton onClick={() => setEdit(id, { text: item.text, original: item.text })} variant="plain">
                  Use the new text
                </AnswerButton>
                <AnswerButton onClick={() => setEdit(id, { text: edit.text, original: item.text })} variant="plain">
                  Keep my words
                </AnswerButton>
              </div>
            </div>
          ) : null}
          <div className="mt-1 flex items-center justify-end gap-1">
            {busy !== null ? <Spinner /> : null}
            <IconButton
              disabled={!idle || changedUnder || edit.text.trim() === "" || edit.text === edit.original}
              icon="check"
              label="Save the edit"
              onClick={() => {
                void run("edit", edit.text).then((ok) => {
                  // Accepted is not applied: the words stay until the queue shows them.
                  if (ok) setEdit(id, { text: edit.text, original: edit.original, pending: true, saved: edit.text });
                });
              }}
            />
            <IconButton disabled={busy !== null} icon="x" label="Cancel the edit" onClick={() => setEdit(id, undefined)} />
          </div>
        </>
      )}
      {error !== null ? (
        <ErrorNote className="mt-1">{error}</ErrorNote>
      ) : null}
    </li>
  );
}

/** How far back from the end of the timeline a sent message is looked for. */
const SENT_LOOKBACK = 300;

/**
 * An edit whose message left the queue. It says what happened when the session knows: the message shows in the
 * timeline as a user message sent by the same command (it was sent); otherwise it only says it left the queue (sent a
 * moment ago, or taken out on another device), and says it was sent as soon as the timeline shows it.
 */
function Orphan({ store, id, edit, setEdit, onKeep }: { store: SessionStore; id: string; edit: Edit; setEdit(id: string, edit: Edit | undefined): void; onKeep(text: string): void }) {
  const sent = useSessionStore(store, (s) => {
    const items = s.session.items;
    for (let i = items.length - 1; i >= 0 && i >= items.length - SENT_LOOKBACK; i--) {
      const item = items[i];
      if (item !== undefined && item.kind === "message" && item.role === "user" && item.commandId === id) return true;
    }
    return false;
  });
  return (
    <div aria-label="An edit that was not saved" className="mb-1 rounded-block border border-warning bg-subtle px-2.5 py-2" data-orphan={id} data-orphan-sent={sent} role="group">
      <p className="text-xs text-secondary">
        {edit.pending === true
          ? "The hub took your edit, but the message left the queue before it showed: it may have gone as it was. Your words are here."
          : sent
            ? "This message was sent before your edit was saved, so your edit did not change it. Your words are here."
            : "This message left the queue before your edit was saved: it is being sent, or it was taken out on another device. Your words are here."}
      </p>
      <p className="mt-1 text-sm leading-6 break-words whitespace-pre-wrap" data-untrusted="edit">
        {clip(edit.text, TEXT_SHOWN, 60).text}
      </p>
      <div className="mt-1.5 flex gap-1.5">
        <AnswerButton
          onClick={() => {
            onKeep(edit.text);
            setEdit(id, undefined);
          }}
          variant="primary"
        >
          Move to the message box
        </AnswerButton>
        <AnswerButton onClick={() => setEdit(id, undefined)} variant="plain">
          Let it go
        </AnswerButton>
      </div>
    </div>
  );
}

/**
 * The messages the hub holds for after the reply, as a strip just above the bar ("Queued: 2 messages") that
 * opens upward into the list. Each can be edited, taken out, or sent now (in its menu: that stops the reply in
 * progress). The hub has no reorder, so there is none. A message that left the queue while its edit was open
 * (it was sent, or taken out on another device) leaves the person's words in a card of their own, to move into
 * the message box or let go; they are never dropped quietly. The list has its own height and its own scroll.
 */
export function QueueTray({ store, onKeep, settleMs, confirmMs }: { store: SessionStore; onKeep(text: string): void; settleMs: number; confirmMs: number }) {
  const queue = useSessionStore(store, (s) => s.session.queue);
  const steerOk = useSessionStore(store, (s) => s.session.caps?.steer === true);
  const steerEffect = useSessionStore(store, (s) => steerEffectOf(s.session.caps));
  const refusal = useSessionStore(store, (s) => s.refusal()?.message ?? null);
  const [open, setOpen] = useState(false);
  const [made, setMade] = useState(false);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  if (open && !made) setMade(true);

  const setEdit = (id: string, edit: Edit | undefined): void =>
    setEdits((now) => {
      const next = { ...now };
      if (edit === undefined) delete next[id];
      else next[id] = edit;
      return next;
    });

  // An accepted edit that the queue now shows is done: its words are no longer kept aside.
  useEffect(() => {
    for (const item of queue) {
      const kept = edits[item.command_id];
      // Also after the wait gave up on it (a connection that came back late). It is the saved text arriving, and the draft is still that text: a draft the person has changed since is theirs, and typing text that happens to match the queue is not an echo.
      if (kept !== undefined && (kept.pending === true || kept.unconfirmed === true) && kept.saved !== undefined && item.text === kept.saved && kept.text === kept.saved) setEdit(item.command_id, undefined);
    }
  }, [queue, edits]);

  const present = new Set(queue.map((item) => item.command_id));
  const orphans = Object.entries(edits).filter(([id, edit]) => !present.has(id) && edit.text !== edit.original && edit.text.trim() !== "");
  // A message being edited keeps the list open under the person's hands.
  const editing = queue.some((item) => edits[item.command_id] !== undefined);
  if (queue.length === 0 && orphans.length === 0) return null;
  const shown = queue.slice(0, TRAY_SHOWN);
  const expanded = open || editing;

  return (
    <div className="sv-enter sv-glass pointer-events-auto flex shrink-0 flex-col rounded-glass px-1.5 py-1 shadow-sm" data-tray="queued">
      {orphans.map(([id, edit]) => (
        <Orphan key={id} edit={edit} id={id} onKeep={onKeep} setEdit={setEdit} store={store} />
      ))}
      {queue.length > 0 ? (
        <>
          <div className="sv-collapse" data-open={expanded} id="sv-queue-list" inert={!expanded}>
            <div className="sv-collapse-inner">
              {made || editing ? (
                <>
                  {refusal !== null ? (
                    <p className="px-1.5 pb-1 text-xs text-secondary" data-tray-note="refusal">
                      {refusal}
                    </p>
                  ) : null}
                  <ul className={cx("flex flex-col gap-1 overflow-y-auto pb-1", editing ? "max-h-72" : "max-h-44")}>
                    {shown.map((item) => (
                      <Guard key={item.command_id} message="This queued message could not be shown." part="queued" resetKey={`${item.text}\u0000${item.mode ?? ""}`}>
                        <Item edit={edits[item.command_id]} item={item} refusal={refusal} setEdit={setEdit} confirmMs={confirmMs} settleMs={settleMs} steerEffect={steerEffect} steerOk={steerOk} store={store} />
                      </Guard>
                    ))}
                  </ul>
                  {queue.length > shown.length ? <p className="px-1.5 pb-1 text-xs text-muted">{plural(queue.length - shown.length, "more queued message")} not shown.</p> : null}
                </>
              ) : null}
            </div>
          </div>
          <button
            aria-controls="sv-queue-list"
            aria-expanded={expanded}
            className="flex h-8 w-full items-center gap-2 rounded-control px-1.5 text-left text-xs font-medium text-secondary motion-safe:transition-colors hover:bg-hover focus-visible:outline-2 focus-visible:outline-interactive"
            onClick={() => setOpen(!expanded)}
            type="button"
          >
            <span className="flex-1">{`Queued: ${plural(queue.length, "message")}`}</span>
            <Icon className="sv-chev-up size-3.5 shrink-0" data-open={expanded} name="chevron" />
          </button>
        </>
      ) : null}
    </div>
  );
}
