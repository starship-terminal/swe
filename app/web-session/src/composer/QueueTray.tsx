import { useRef, useState } from "react";

import { useSessionStore, type SessionStore } from "../core";
import { Collapsible, Icon, Spinner } from "../ui";
import { AnswerButton } from "./AnswerButton";
import { Guard } from "./Guard";
import { clip, plural } from "./text";

type Queued = { command_id: string; text: string; by?: string; mode?: "queue" | "steer"; attachments?: string[] };

/** How many queued messages the tray draws; the hub's own queue is capped well below a screenful. */
export const TRAY_SHOWN = 20;
/** How much of a queued message the tray draws. */
const TEXT_SHOWN = 2000;

interface Edit {
  text: string;
  original: string;
}

type Action = "edit" | "cancel" | "steer";

function Item({
  store,
  item,
  edit,
  setEdit,
  steerOk,
  refusal,
}: {
  store: SessionStore;
  item: Queued;
  edit: Edit | undefined;
  setEdit(id: string, edit: Edit | undefined): void;
  steerOk: boolean;
  refusal: string | null;
}) {
  const [busy, setBusy] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pressing = useRef(false);
  const id = item.command_id;
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
    <li className="rounded-block border border-border bg-subtle px-2.5 py-2" data-queued={id}>
      {edit === undefined ? (
        <>
          <p className="line-clamp-3 text-sm leading-6 break-words whitespace-pre-wrap" data-untrusted="queued">
            {shown.text}
          </p>
          {shown.hidden > 0 ? <p className="text-xs text-muted">{shown.hidden.toLocaleString("en-US")} more characters not shown.</p> : null}
          {item.attachments !== undefined && item.attachments.length > 0 ? (
            <p className="mt-0.5 flex items-center gap-1 text-xs text-secondary">
              <Icon className="size-3.5" name="attach" />
              {plural(item.attachments.length, "attachment")}
            </p>
          ) : null}
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <AnswerButton aria-label="Edit queued message" disabled={!idle} onClick={() => setEdit(id, { text: item.text, original: item.text })} variant="plain">
              <Icon className="size-3.5" name="edit" />
              <span className="hidden sm:inline">Edit</span>
            </AnswerButton>
            <AnswerButton
              aria-label="Stop the reply and send this now"
              disabled={!idle || !steerOk}
              onClick={() => void run("steer")}
              title={steerOk ? "Stops the current reply and sends this message now." : "This agent cannot be steered."}
              variant="plain"
            >
              Stop and send
            </AnswerButton>
            <AnswerButton aria-label="Remove queued message" disabled={!idle} onClick={() => void run("cancel")} variant="danger">
              <Icon className="size-3.5" name="trash" />
              <span className="hidden sm:inline">Remove</span>
            </AnswerButton>
            {busy !== null ? <Spinner /> : null}
          </div>
        </>
      ) : (
        <>
          <textarea
            aria-label="Edit queued message"
            className="min-h-16 w-full resize-y rounded-field border border-border bg-card px-2 py-1.5 text-sm leading-6 focus:border-interactive focus:outline-none"
            onChange={(event) => setEdit(id, { text: event.currentTarget.value, original: edit.original })}
            readOnly={busy !== null}
            value={edit.text}
          />
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <AnswerButton
              disabled={!idle || edit.text.trim() === "" || edit.text === edit.original}
              onClick={() => {
                void run("edit", edit.text).then((ok) => {
                  if (ok) setEdit(id, undefined);
                });
              }}
              variant="primary"
            >
              Save
            </AnswerButton>
            <AnswerButton disabled={busy !== null} onClick={() => setEdit(id, undefined)} variant="plain">
              Cancel
            </AnswerButton>
            {busy !== null ? <Spinner /> : null}
          </div>
        </>
      )}
      {error !== null ? (
        <p className="mt-1.5 text-xs text-danger-text" role="alert">
          {error}
        </p>
      ) : null}
    </li>
  );
}

/**
 * The messages the hub holds for after the reply: each can be edited, taken out, or sent now (which stops the
 * reply in progress). The hub has no reorder, so there is none. A message that left the queue while its edit
 * was open (it was sent, or taken out on another device) leaves the person's words in a card of their own,
 * to move into the message box or let go, and is never dropped quietly.
 */
export function QueueTray({ store, onKeep }: { store: SessionStore; onKeep(text: string): void }) {
  const queue = useSessionStore(store, (s) => s.session.queue);
  const steerOk = useSessionStore(store, (s) => s.session.caps?.steer === true);
  const refusal = useSessionStore(store, (s) => s.refusal()?.message ?? null);
  const [open, setOpen] = useState(true);
  const [edits, setEdits] = useState<Record<string, Edit>>({});

  const setEdit = (id: string, edit: Edit | undefined): void =>
    setEdits((now) => {
      const next = { ...now };
      if (edit === undefined) delete next[id];
      else next[id] = edit;
      return next;
    });

  const present = new Set(queue.map((item) => item.command_id));
  const orphans = Object.entries(edits).filter(([id, edit]) => !present.has(id) && edit.text !== edit.original && edit.text.trim() !== "");
  if (queue.length === 0 && orphans.length === 0) return null;
  const shown = queue.slice(0, TRAY_SHOWN);

  return (
    <div className="sv-enter rounded-card border border-border bg-card px-2.5 py-1.5" data-tray="queued">
      {queue.length > 0 ? (
        <Collapsible
          buttonClassName="px-1 py-1 text-xs font-medium text-secondary"
          header={<span>{`Queued: ${plural(queue.length, "message")}, sent when the reply ends`}</span>}
          onToggle={setOpen}
          open={open}
        >
          {refusal !== null ? (
            <p className="px-1 pb-1.5 text-xs text-secondary" data-tray-note="refusal">
              {refusal}
            </p>
          ) : !steerOk && queue.length > 0 ? (
            <p className="px-1 pb-1.5 text-xs text-secondary" data-tray-note="no-steer">
              This agent cannot stop a reply to send a message sooner. Queued messages are sent when the reply ends.
            </p>
          ) : null}
          <ul className="flex max-h-48 flex-col gap-1.5 overflow-y-auto pb-1.5">
            {shown.map((item) => (
              <Guard key={item.command_id} message="This queued message could not be shown." part="queued">
                <Item edit={edits[item.command_id]} item={item} refusal={refusal} setEdit={setEdit} steerOk={steerOk} store={store} />
              </Guard>
            ))}
          </ul>
          {queue.length > shown.length ? <p className="px-1 pb-1.5 text-xs text-muted">{plural(queue.length - shown.length, "more queued message")} not shown.</p> : null}
        </Collapsible>
      ) : null}
      {orphans.map(([id, edit]) => (
        <div key={id} aria-label="An edit that was not saved" className="mb-1.5 rounded-block border border-warning bg-subtle px-2.5 py-2" data-orphan={id} role="group">
          <p className="text-xs text-secondary">This message left the queue before your edit was saved. Your edit is here.</p>
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
      ))}
    </div>
  );
}
