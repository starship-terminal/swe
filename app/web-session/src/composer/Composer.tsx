import "./composer.css";

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type FormEvent, type KeyboardEvent, type PointerEvent, type ReactNode, type RefObject } from "react";

import { readOnlyReason, useSessionStore, type SessionStore } from "../core";
import { asArrived } from "../session/format";
import { cx, Icon, Spinner, StateArrow, type IconName } from "../ui";
import { FILES_KEPT, type StagedFile } from "./draft";
import { ErrorNote } from "./ErrorNote";
import { Guard } from "./Guard";
import { enterAction, touchScreen } from "./keys";
import { Menu, type MenuItem } from "./Menu";
import { QueueTray } from "./QueueTray";
import { RequestDock } from "./RequestDock";
import { steerEffectOf, steerWording } from "./steer";
import { MAX_MESSAGE_CHARS, submitMessage } from "./submit";
import { cut, plural } from "./text";
import { httpUpload, type Upload } from "./upload";
import { useDraft } from "./useDraft";

export const COMPOSER_PLACEHOLDER = "Ask anything";
const QUEUE_PLACEHOLDER = "Queue a message";

/** The tallest the box grows before it scrolls. */
const MAX_BOX_PX = 200;
/** What the file picker offers; the hub's own list decides (it answers 422 with its reason for anything else). */
const ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.heic,.pdf,.txt,.md,.csv,.json,.log,.diff,.patch";
/** How long the primary button is held, while a reply runs, before its menu opens. */
export const HOLD_MS = 450;

export interface ComposerProps {
  store: SessionStore;
  sessionKey: string;
  /** How a file is staged on the hub. Tests pass their own; the default posts to the hub. */
  upload?: Upload;
  /** How long a request's answers stay off when it replaces the one being read. */
  settleMs?: number;
  /** How long the primary button is held before its menu opens. */
  holdMs?: number;
  /**
   * The slot between the box and the buttons, for the model chip a later milestone brings (the store has no
   * action to set a model yet, so nothing is drawn there now; see the spec).
   */
  chip?: ReactNode;
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

/**
 * Where the composer sits: over the bottom of the view, not in its flow. Reports its height as
 * `--sv-composer-h` on the view's host (its parent), which the timeline leaves as padding at its end.
 */
function useFloating(): { host: RefObject<HTMLDivElement | null>; stack: RefObject<HTMLDivElement | null> } {
  const host = useRef<HTMLDivElement>(null);
  const stack = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const stackEl = stack.current;
    const parent = host.current?.parentElement;
    if (stackEl === null || parent === null || parent === undefined) return;
    // From the top of the stack to the bottom of the view: the stack, and the keyboard or home indicator it rides above.
    const report = (): void => parent.style.setProperty("--sv-composer-h", `${Math.max(0, Math.ceil(parent.getBoundingClientRect().bottom - stackEl.getBoundingClientRect().top))}px`);
    report();
    if (typeof ResizeObserver === "undefined") return () => parent.style.removeProperty("--sv-composer-h");
    // The stack changes size with its content; the host's content box changes when the keyboard or the home
    // indicator changes the padding it rides on, with the stack the same size and in a new place.
    const watcher = new ResizeObserver(report);
    watcher.observe(stackEl);
    if (host.current !== null) watcher.observe(host.current);
    return () => {
      watcher.disconnect();
      parent.style.removeProperty("--sv-composer-h");
    };
  }, []);
  return { host, stack };
}

function ReadOnlyBar({ reason }: { reason: string }) {
  const { host, stack } = useFloating();
  return (
    <div ref={host} className="sv-composer-host pointer-events-none absolute inset-0 z-10 flex flex-col justify-end" data-composer="read-only" data-composer-host>
      <div ref={stack} className="sv-composer mx-auto flex w-full max-w-3xl flex-col">
        <p className="sv-glass pointer-events-auto flex items-start gap-2 rounded-glass px-3.5 py-2.5 text-sm text-secondary shadow-sm" role="status">
          <StateArrow className="mt-1" colour="grey" />
          <span>{asArrived(reason)}</span>
        </p>
      </div>
    </div>
  );
}

/** A round button with a picture and a name: the bar has no visible words. */
function RoundButton({
  label,
  icon,
  tone = "ghost",
  className,
  children,
  ...rest
}: {
  label: string;
  icon?: IconName;
  tone?: "ghost" | "primary" | "danger";
} & ComponentProps<"button">) {
  return (
    <button
      aria-label={label}
      className={cx(
        "sv-press grid size-9 shrink-0 place-items-center rounded-full motion-safe:transition-colors focus-visible:outline-2 focus-visible:outline-interactive disabled:cursor-not-allowed disabled:opacity-40",
        tone === "ghost" && "text-secondary hover:bg-hover hover:text-primary",
        tone === "primary" && "bg-primary text-card",
        tone === "danger" && "bg-danger text-card",
        className,
      )}
      title={rest.title ?? label}
      type="button"
      {...rest}
    >
      {children ?? (icon !== undefined ? <Icon className="size-4.5" name={icon} /> : null)}
    </button>
  );
}

function Form({ store, sessionKey, upload, settleMs, holdMs, chip }: Required<Omit<ComposerProps, "chip">> & { chip: ReactNode }) {
  const { draft, latest, update, clear, kept } = useDraft(sessionKey);
  const { host, stack } = useFloating();
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Uploading[]>([]);
  const [stopState, setStopState] = useState<"idle" | "sending" | "sent">("idle");
  const [menu, setMenu] = useState<"attach" | "send" | null>(null);
  const guard = useRef(false);
  const stopGuard = useRef(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const primary = useRef<HTMLButtonElement>(null);
  const plus = useRef<HTMLButtonElement>(null);
  const composing = useRef(false);
  const uploadCount = useRef(0);
  const openUploads = useRef(0);
  const aborts = useRef(new Set<AbortController>());
  const hold = useRef<ReturnType<typeof setTimeout> | null>(null);
  const swallowClick = useRef(false);
  const pointerDown = useRef(false);
  const reasonId = useId();

  const busyTurn = useSessionStore(store, (s) => s.session.openTurn !== null || s.session.phase === "running" || s.session.phase === "waiting");
  const steerOk = useSessionStore(store, (s) => s.session.caps?.steer === true);
  const steerEffect = useSessionStore(store, (s) => steerEffectOf(s.session.caps));
  const requestOpen = useSessionStore(store, (s) => s.session.pending.length > 0);
  const steerWords = steerWording(steerEffect, requestOpen);
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

  // Why the message cannot go now, for the person: shown above the bar and tied to it for a screen reader.
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
      if (hold.current !== null) clearTimeout(hold.current);
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

  // A reply that ends while the menu for it is open takes the menu away.
  useEffect(() => {
    if (!busyTurn && menu === "send") setMenu(null);
  }, [busyTurn, menu]);

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

  // The primary button: a press sends (or queues). While a reply runs, holding it, a right click, or the
  // arrow-up key opens a menu with the other way to send. The press that opened the menu never also sends.
  const stopHold = (): void => {
    if (hold.current !== null) clearTimeout(hold.current);
    hold.current = null;
  };
  // Only a menu opened by a pointer that is still down has a release to swallow. One opened by the keyboard (the
  // arrow key, the menu key) has none, and a flag left set would swallow the next real press.
  const openSendMenu = (): void => {
    stopHold();
    swallowClick.current = pointerDown.current;
    setMenu("send");
  };
  const onPrimaryDown = (event: PointerEvent<HTMLButtonElement>): void => {
    swallowClick.current = false;
    pointerDown.current = true;
    if (!busyTurn || event.button !== 0) return;
    stopHold();
    hold.current = setTimeout(openSendMenu, holdMs);
  };
  const onPrimaryUp = (): void => {
    pointerDown.current = false;
    stopHold();
    // The click that follows this release belongs to the hold that opened the menu: it is left unanswered, then forgotten.
    if (swallowClick.current) setTimeout(() => (swallowClick.current = false), 80);
  };

  const addFiles = (list: FileList | null): void => {
    if (list === null) return;
    for (const file of Array.from(list)) {
      // Slots are taken here, one file at a time, not read back from state: a pick of many files would
      // otherwise all see the same count.
      if (latest.current.files.length + openUploads.current >= FILES_KEPT) {
        setNotice(`A message takes at most ${plural(FILES_KEPT, "attachment")}.`);
        break;
      }
      openUploads.current++;
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
        .finally(() => {
          openUploads.current--;
          aborts.current.delete(controller);
        });
    }
    if (picker.current !== null) picker.current.value = "";
  };

  const choose = (kind: "files" | "photos"): void => {
    const input = picker.current;
    if (input === null) return;
    input.accept = kind === "photos" ? "image/*" : ACCEPT;
    input.click();
  };

  const keep = (words: string): void => {
    update((now) => ({ ...now, text: now.text === "" ? words : `${now.text}\n\n${words}` }));
    box.current?.focus();
  };

  const note = connectionNote(connection, loaded);
  const steerReason = busyTurn && !steerOk ? "This agent cannot take a message while it is replying. Queue it to send when the reply ends." : null;
  const reason = notice ?? blockedBy ?? (hasContent ? steerReason : null) ?? note;
  const stopDisabled = refusalMessage !== null || stopState !== "idle";
  const canSend = !submitting && hasContent && blockedBy === null;

  const attachItems: MenuItem[] = [{ id: "files", label: "Choose files", icon: "folder", onSelect: () => choose("files") }];
  // A photo library is something a phone or a tablet has; a desktop browser's file picker already shows pictures.
  if (touchScreen()) attachItems.push({ id: "photos", label: "Photo library", icon: "photo", onSelect: () => choose("photos") });
  // The safe item first: the menu opens on it, and the one that stops the reply is never the one it opens on.
  const sendItems: MenuItem[] = [
    { id: "queue", label: "Queue", icon: "queue", hint: "Sent when the current reply ends.", disabled: !canSend, reason: "Write a message first.", onSelect: () => void submit("queue") },
    {
      id: "steer",
      label: steerWords.label,
      icon: steerWords.stops ? "stop" : "queue",
      hint: steerWords.sentence,
      disabled: !canSend || !steerOk,
      reason: !steerOk ? "This agent cannot be steered." : "Write a message first.",
      onSelect: () => void submit("steer"),
    },
  ];
  const closeMenu = useMemo(
    () => () => {
      setMenu((now) => {
        // The focus goes back to what opened the menu.
        queueMicrotask(() => (now === "attach" ? plus.current : primary.current)?.focus());
        return null;
      });
    },
    [],
  );

  return (
    <div ref={host} className="sv-composer-host pointer-events-none absolute inset-0 z-10 flex flex-col justify-end" data-composer="form" data-composer-host>
      <div ref={stack} className="sv-composer mx-auto flex max-h-1/2 w-full max-w-3xl flex-col gap-2">
        <div className="flex min-h-0 flex-col gap-2 overflow-y-auto" data-composer-above>
          {(!persistent && outboxNote !== "") || (kept === "memory" && text !== "") || reason !== null ? (
            <div className="flex flex-col gap-1.5">
              {!persistent && outboxNote !== "" ? (
                <p className="sv-glass pointer-events-auto rounded-glass px-3 py-1.5 text-xs text-secondary" data-outbox-note role="status">
                  {asArrived(outboxNote)}
                </p>
              ) : null}
              {kept === "memory" && text !== "" ? (
                <p className="sv-glass pointer-events-auto rounded-glass px-3 py-1.5 text-xs text-secondary" data-draft-note role="status">
                  This draft cannot be kept on this device, so it will not survive a reload.
                </p>
              ) : null}
              {reason !== null && notice !== null ? (
                <ErrorNote aria-live="polite" data-composer-reason="error" id={reasonId}>
                  {reason}
                </ErrorNote>
              ) : reason !== null ? (
                <p
                  aria-live="polite"
                  className="sv-glass pointer-events-auto rounded-glass px-3 py-1.5 text-xs text-secondary"
                  data-composer-reason={blockedBy !== null ? "blocked" : note !== null ? "connection" : "none"}
                  id={reasonId}
                  role="status"
                >
                  {reason}
                </p>
              ) : null}
            </div>
          ) : null}
          <Guard message="The queue could not be shown. Queued messages are still sent when the reply ends." part="tray">
            <QueueTray onKeep={keep} store={store} />
          </Guard>
          <Guard message="This request could not be shown. Answer it in the terminal or on another device." part="dock">
            <RequestDock settleMs={settleMs} store={store} />
          </Guard>
        </div>
        <form
          aria-label="Composer"
          className="sv-glass pointer-events-auto relative flex shrink-0 flex-col gap-1 rounded-bar px-1.5 py-1.5 shadow-sm focus-within:border-border-strong"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            if (canSend) void submit("queue");
          }}
        >
          {files.length > 0 || uploads.length > 0 ? (
            <ul aria-label="Attachments" className="flex flex-wrap gap-1.5 px-2 pt-1">
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
                      <span>{entry.error}</span>
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
          <div className="flex items-end gap-1">
            <input ref={picker} accept={ACCEPT} aria-label="Choose files to attach" className="hidden" multiple onChange={(event) => addFiles(event.currentTarget.files)} tabIndex={-1} type="file" />
            <div className="relative">
              <RoundButton
                ref={plus}
                aria-expanded={menu === "attach"}
                aria-haspopup="menu"
                disabled={submitting}
                icon="plus"
                label="Attach"
                onClick={() => setMenu((now) => (now === "attach" ? null : "attach"))}
                title="Attach files"
              />
              {menu === "attach" ? <Menu items={attachItems} label="Attach" onClose={closeMenu} placement="above-start" /> : null}
            </div>
            <textarea
              ref={box}
              aria-busy={submitting}
              aria-describedby={reason === null ? undefined : reasonId}
              aria-label="Message"
              className="max-h-50 min-h-9 min-w-0 flex-1 resize-none bg-transparent px-1.5 py-1.5 text-sm leading-6 placeholder:text-muted focus:outline-none"
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
            {chip !== null && chip !== undefined ? <div data-slot="model-chip">{chip}</div> : null}
            {busyTurn ? (
              <RoundButton
                aria-describedby={stopDisabled && refusalMessage !== null ? reasonId : undefined}
                disabled={stopDisabled}
                label="Stop the reply"
                onClick={() => void stop()}
                title={refusalMessage ?? (stopState === "sent" ? "Stopping" : "Stop the reply in progress")}
                tone="danger"
              >
                {stopState === "sending" ? <Spinner className="text-card" /> : <Icon className="size-4" name="stop" />}
              </RoundButton>
            ) : null}
            <div className="relative">
              <RoundButton
                ref={primary}
                aria-expanded={busyTurn ? menu === "send" : undefined}
                aria-haspopup={busyTurn ? "menu" : undefined}
                disabled={submitting || !hasContent || blockedBy !== null}
                label={busyTurn ? "Queue message" : "Send message"}
                onClick={(event) => {
                  if (swallowClick.current) {
                    // The release of a hold that opened the menu: not a press.
                    event.preventDefault();
                    swallowClick.current = false;
                    return;
                  }
                  if (canSend) void submit("queue");
                }}
                onContextMenu={(event) => {
                  if (!busyTurn) return;
                  event.preventDefault();
                  openSendMenu();
                }}
                onKeyDown={(event) => {
                  if (busyTurn && (event.key === "ArrowUp" || (event.shiftKey && event.key === "F10"))) {
                    event.preventDefault();
                    openSendMenu();
                  }
                }}
                onPointerCancel={onPrimaryUp}
                onPointerDown={onPrimaryDown}
                onPointerLeave={() => {
                  stopHold();
                }}
                onPointerUp={onPrimaryUp}
                title={busyTurn ? "Queue this message (hold for more)" : "Send"}
                tone="primary"
              >
                {submitting ? <Spinner className="text-card" /> : <Icon className="size-4.5" name={busyTurn ? "queue" : "send"} />}
              </RoundButton>
              {menu === "send" ? <Menu items={sendItems} label="Send options" onClose={closeMenu} placement="above-end" /> : null}
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

/**
 * The message bar, the approval and question dock, and the queued tray: everything over the bottom of the
 * timeline. A session that cannot be driven gets a bar with the reason in place of all of it.
 */
export function Composer({ store, sessionKey, upload, settleMs = 350, holdMs = HOLD_MS, chip = null }: ComposerProps) {
  const readOnly = useSessionStore(store, (s) => readOnlyReason(s.session.row ?? null));
  const stage = useMemo<Upload>(() => upload ?? httpUpload(sessionKey), [upload, sessionKey]);
  if (readOnly !== null) return <ReadOnlyBar reason={readOnly} />;
  return <Form key={sessionKey} chip={chip} holdMs={holdMs} sessionKey={sessionKey} settleMs={settleMs} store={store} upload={stage} />;
}
