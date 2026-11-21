import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { readOnlyReason, useSessionStore, type SessionStore } from "../core";
import { asArrived } from "../session/format";
import { cx, Icon, Spinner, StateArrow } from "../ui";
import { AnswerButton } from "./AnswerButton";
import { FILES_KEPT, type StagedFile } from "./draft";
import { Guard } from "./Guard";
import { enterAction, touchScreen } from "./keys";
import { QueueTray } from "./QueueTray";
import { RequestDock } from "./RequestDock";
import { MAX_MESSAGE_CHARS, submitMessage } from "./submit";
import { cut, plural } from "./text";
import { httpUpload, type Upload } from "./upload";
import { useDraft } from "./useDraft";

export const COMPOSER_PLACEHOLDER = "Message the agent";
const QUEUE_PLACEHOLDER = "Queue a message for when the reply ends";

/** The tallest the box grows before it scrolls. */
const MAX_BOX_PX = 240;
/** What the file picker offers; the hub's own list decides (it answers 422 with its reason for anything else). */
const ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.heic,.pdf,.txt,.md,.csv,.json,.log,.diff,.patch";

export interface ComposerProps {
  store: SessionStore;
  sessionKey: string;
  /** How a file is staged on the hub. Tests pass their own; the default posts to the hub. */
  upload?: Upload;
  /** How long a request's answers stay off when it replaces the one being read. */
  settleMs?: number;
}

type Uploading = { id: number; name: string; error: string | null };

/**
 * What the connection means for a message typed now. Once the session has loaded the banner under the header
 * says it for every state that needs it (offline, reconnecting, hub down, signed out), so this speaks only
 * before that, when the timeline's own state does not mention messages.
 */
function connectionNote(state: string, loaded: boolean): string | null {
  return state === "connecting" && !loaded ? "Not connected to the hub yet. A message is kept on this device and sent once it is." : null;
}

function ReadOnlyBar({ reason }: { reason: string }) {
  return (
    <footer className="shrink-0 pr-safe-right pb-safe-bottom pl-safe-left" data-composer="read-only">
      <div className="mx-auto max-w-3xl px-4 pt-2 pb-4">
        <p className="flex items-start gap-2 rounded-card border border-border bg-subtle px-3 py-2.5 text-sm text-secondary" role="status">
          <StateArrow className="mt-1" colour="grey" />
          <span>{asArrived(reason)}</span>
        </p>
      </div>
    </footer>
  );
}

function Form({ store, sessionKey, upload, settleMs }: Required<ComposerProps>) {
  const { draft, latest, update, clear, kept } = useDraft(sessionKey);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Uploading[]>([]);
  const [stopState, setStopState] = useState<"idle" | "sending" | "sent">("idle");
  const guard = useRef(false);
  const stopGuard = useRef(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const composing = useRef(false);
  const uploadCount = useRef(0);
  const aborts = useRef(new Set<AbortController>());
  const reasonId = useId();

  const busyTurn = useSessionStore(store, (s) => s.session.openTurn !== null || s.session.phase === "running" || s.session.phase === "waiting");
  const steerOk = useSessionStore(store, (s) => s.session.caps?.steer === true);
  const connection = useSessionStore(store, (s) => s.connection.state);
  const refusalReason = useSessionStore(store, (s) => s.refusal()?.reason ?? null);
  const refusalMessage = useSessionStore(store, (s) => s.refusal()?.message ?? null);
  const loaded = useSessionStore(store, (s) => s.session.loaded);
  const loading = useSessionStore(store, (s) => s.outbox.loading);
  const persistent = useSessionStore(store, (s) => s.outbox.persistent);
  const outboxNote = useSessionStore(store, (s) => s.outbox.note);

  const text = draft.text;
  const files = draft.files;
  const uploading = uploads.some((u) => u.error === null);
  const hasContent = text.trim() !== "" || files.length > 0;

  // Why the message cannot go now, for the person: shown beside the box and tied to it for a screen reader.
  let blockedBy: string | null = null;
  if (refusalReason === "gone" || refusalReason === "outdated") blockedBy = refusalMessage;
  else if (loading) blockedBy = "Loading the sends kept on this device.";
  else if (uploading) blockedBy = "Waiting for the attachment to upload.";
  else if (text.length > MAX_MESSAGE_CHARS) blockedBy = `This message is over ${MAX_MESSAGE_CHARS.toLocaleString("en-US")} characters. Shorten it to send it.`;

  // Grow with the text, up to a ceiling, then scroll.
  useLayoutEffect(() => {
    const el = box.current;
    if (el === null) return;
    el.style.height = "auto";
    if (el.scrollHeight > 0) el.style.height = `${Math.min(el.scrollHeight, MAX_BOX_PX)}px`;
  }, [text]);

  useEffect(() => {
    const open = aborts.current;
    return () => {
      for (const controller of open) controller.abort();
    };
  }, []);

  // A stop that was sent stays "sent" until the turn ends, and for no more than a few seconds after.
  useEffect(() => {
    if (stopState !== "sent") return;
    if (!busyTurn) {
      setStopState("idle");
      return;
    }
    const timer = setTimeout(() => setStopState("idle"), 5000);
    return () => clearTimeout(timer);
  }, [stopState, busyTurn]);

  const submit = async (mode: "queue" | "steer"): Promise<void> => {
    // A second press, a held key or a second render gets here before `submitting` is drawn: this is the guard.
    if (guard.current) return;
    guard.current = true;
    const sent = latest.current;
    setSubmitting(true);
    setNotice(null);
    try {
      const outcome = await submitMessage(store, { text: sent.text, attachments: sent.files.map((file) => file.ref), mode });
      if (outcome.ok) {
        clear(sent.text);
        box.current?.focus();
      } else if (outcome.reason !== "empty") {
        // The draft stays exactly as it was.
        setNotice(outcome.message);
      }
    } finally {
      guard.current = false;
      setSubmitting(false);
    }
  };

  const stop = async (): Promise<void> => {
    if (stopGuard.current) return;
    stopGuard.current = true;
    setStopState("sending");
    setNotice(null);
    try {
      const result = await store.getState().interrupt();
      if (result.ok) setStopState("sent");
      else {
        setStopState("idle");
        setNotice(result.message);
      }
    } finally {
      stopGuard.current = false;
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    const action = enterAction(
      {
        key: event.key,
        shiftKey: event.shiftKey,
        altKey: event.altKey,
        metaKey: event.metaKey,
        ctrlKey: event.ctrlKey,
        repeat: event.repeat,
        keyCode: event.keyCode,
        isComposing: event.nativeEvent.isComposing,
      },
      composing.current,
      touchScreen(),
    );
    if (action === "none") return;
    event.preventDefault();
    if (action === "send" && !blockedBy && hasContent) void submit("queue");
  };

  const addFiles = (list: FileList | null): void => {
    if (list === null) return;
    for (const file of Array.from(list)) {
      if (latest.current.files.length + uploadsOpen() >= FILES_KEPT) {
        setNotice(`A message takes at most ${plural(FILES_KEPT, "attachment")}.`);
        break;
      }
      const id = ++uploadCount.current;
      const controller = new AbortController();
      aborts.current.add(controller);
      setUploads((now) => [...now, { id, name: file.name, error: null }]);
      upload(file, controller.signal)
        .then((staged: StagedFile) => {
          update((now) => ({ ...now, files: [...now.files, staged] }));
          setUploads((now) => now.filter((entry) => entry.id !== id));
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          const why = error instanceof Error && error.message !== "" ? error.message : "The upload failed.";
          setUploads((now) => now.map((entry) => (entry.id === id ? { ...entry, error: why } : entry)));
        })
        .finally(() => aborts.current.delete(controller));
    }
    if (picker.current !== null) picker.current.value = "";
  };
  const uploadsOpen = (): number => uploads.filter((u) => u.error === null).length;

  const keep = (words: string): void => {
    update((now) => ({ ...now, text: now.text === "" ? words : `${now.text}\n\n${words}` }));
    box.current?.focus();
  };

  const note = connectionNote(connection, loaded);
  const steerReason = busyTurn && !steerOk ? "This agent cannot take a message while it is replying. Queue it to send when the reply ends." : null;
  const reason = notice ?? blockedBy ?? (hasContent ? steerReason : null) ?? note;
  const stopDisabled = refusalMessage !== null || stopState !== "idle";

  return (
    <footer className="shrink-0 pr-safe-right pb-safe-bottom pl-safe-left" data-composer="form">
      <div className="mx-auto flex max-w-3xl flex-col gap-2 px-4 pt-2 pb-4">
        <Guard message="This request could not be shown. Answer it in the terminal or on another device." part="dock">
          <RequestDock settleMs={settleMs} store={store} />
        </Guard>
        <Guard message="The queue could not be shown. Queued messages are still sent when the reply ends." part="tray">
          <QueueTray onKeep={keep} store={store} />
        </Guard>
        {!persistent && outboxNote !== "" ? (
          <p className="text-xs text-secondary" data-outbox-note role="status">
            {asArrived(outboxNote)}
          </p>
        ) : null}
        {kept === "memory" && text !== "" ? (
          <p className="text-xs text-secondary" data-draft-note role="status">
            This draft cannot be kept on this device, so it will not survive a reload.
          </p>
        ) : null}
        <form
          aria-label="Composer"
          className="flex flex-col gap-1.5 rounded-card border border-border bg-card p-2 focus-within:border-border-strong"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            if (!blockedBy && hasContent) void submit("queue");
          }}
        >
          {files.length > 0 || uploads.length > 0 ? (
            <ul aria-label="Attachments" className="flex flex-wrap gap-1.5 px-1">
              {files.map((file) => {
                const name = cut(file.name, 60);
                return (
                  <li key={file.ref} className="flex items-center gap-1 rounded-full border border-border bg-subtle py-0.5 pr-1 pl-2.5 text-xs" data-file={file.ref}>
                    <Icon className="size-3.5 text-muted" name="file" />
                    <span className="max-w-40 truncate">{name.text}</span>
                    <button
                      aria-label={`Remove ${name.text}`}
                      className="grid size-5 place-items-center rounded-full hover:bg-hover focus-visible:outline-2 focus-visible:outline-interactive"
                      disabled={submitting}
                      onClick={() => update((now) => ({ ...now, files: now.files.filter((entry) => entry.ref !== file.ref) }))}
                      type="button"
                    >
                      <Icon className="size-3" name="x" />
                    </button>
                  </li>
                );
              })}
              {uploads.map((entry) => (
                <li key={`u${entry.id}`} className="flex items-center gap-1 rounded-full border border-border bg-subtle py-0.5 pr-1 pl-2.5 text-xs" data-upload={entry.error === null ? "uploading" : "failed"}>
                  {entry.error === null ? <Spinner /> : <StateArrow colour="red" />}
                  <span className="max-w-40 truncate">{cut(entry.name, 60).text}</span>
                  {entry.error !== null ? (
                    <>
                      <span className="text-danger-text">{entry.error}</span>
                      <button
                        aria-label={`Dismiss ${cut(entry.name, 60).text}`}
                        className="grid size-5 place-items-center rounded-full hover:bg-hover focus-visible:outline-2 focus-visible:outline-interactive"
                        onClick={() => setUploads((now) => now.filter((e) => e.id !== entry.id))}
                        type="button"
                      >
                        <Icon className="size-3" name="x" />
                      </button>
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
          <textarea
            ref={box}
            aria-busy={submitting}
            aria-describedby={reason === null ? undefined : reasonId}
            aria-label="Message"
            className="max-h-60 min-h-10 w-full resize-none bg-transparent px-2 py-2 text-sm leading-6 placeholder:text-muted focus:outline-none"
            onChange={(event) => {
              const value = event.currentTarget.value;
              update((now) => ({ ...now, text: value }));
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onKeyDown={onKeyDown}
            placeholder={busyTurn ? QUEUE_PLACEHOLDER : COMPOSER_PLACEHOLDER}
            readOnly={submitting}
            rows={1}
            value={text}
          />
          <div className="flex flex-wrap items-center gap-1.5">
            <input ref={picker} accept={ACCEPT} aria-label="Attach files" className="hidden" multiple onChange={(event) => addFiles(event.currentTarget.files)} tabIndex={-1} type="file" />
            <AnswerButton aria-label="Attach files" disabled={submitting} onClick={() => picker.current?.click()} variant="plain">
              <Icon className="size-4" name="attach" />
              <span className="hidden sm:inline">Attach</span>
            </AnswerButton>
            <span className="flex-1" />
            {busyTurn ? (
              <>
                <AnswerButton
                  aria-label="Stop the reply"
                  disabled={stopDisabled}
                  onClick={() => void stop()}
                  title={refusalMessage ?? "Stops the reply in progress."}
                  variant="danger"
                >
                  {stopState === "sending" ? <Spinner /> : <Icon className="size-3.5" name="stop" />}
                  {stopState === "sent" ? "Stopping" : "Stop"}
                </AnswerButton>
                <AnswerButton
                  aria-describedby={steerReason === null ? undefined : reasonId}
                  disabled={submitting || !hasContent || blockedBy !== null || !steerOk}
                  onClick={() => void submit("steer")}
                  title={steerOk ? "Stops the current reply, then sends this message now." : "This agent cannot be steered."}
                  variant="plain"
                >
                  Stop and send
                </AnswerButton>
              </>
            ) : null}
            <AnswerButton
              aria-label={busyTurn ? "Queue message" : "Send message"}
              disabled={submitting || !hasContent || blockedBy !== null}
              title={busyTurn ? "Sent when the current reply ends." : undefined}
              type="submit"
              variant="primary"
            >
              {submitting ? <Spinner className="text-card" /> : <Icon className="size-4" name="send" />}
              {submitting ? "Sending" : busyTurn ? "Queue" : "Send"}
            </AnswerButton>
          </div>
        </form>
        <p
          aria-live="polite"
          className={cx("min-h-4 text-xs", notice !== null ? "text-danger-text" : "text-secondary")}
          data-composer-reason={notice !== null ? "error" : blockedBy !== null ? "blocked" : note !== null ? "connection" : "none"}
          id={reasonId}
          role={notice !== null ? "alert" : "status"}
        >
          {reason}
        </p>
      </div>
    </footer>
  );
}

/**
 * The message box, the approval and question dock, and the queued tray: everything under the timeline.
 * A session that cannot be driven gets a bar with the reason in place of all of it.
 */
export function Composer({ store, sessionKey, upload, settleMs = 350 }: ComposerProps) {
  const readOnly = useSessionStore(store, (s) => readOnlyReason(s.session.row ?? null));
  const stage = useMemo<Upload>(() => upload ?? httpUpload(sessionKey), [upload, sessionKey]);
  if (readOnly !== null) return <ReadOnlyBar reason={readOnly} />;
  return <Form key={sessionKey} sessionKey={sessionKey} settleMs={settleMs} store={store} upload={stage} />;
}
