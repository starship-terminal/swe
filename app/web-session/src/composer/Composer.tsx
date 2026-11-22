import "./composer.css";

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type FormEvent, type KeyboardEvent, type PointerEvent, type ReactNode, type RefObject } from "react";

import { readOnlyReason, useSessionStore, type SessionStore } from "../core";
import { asArrived } from "../session/format";
import { cx, Icon, Spinner, StateArrow, type IconName } from "../ui";
import { dropDraftsOnSignOut, fileKept, FILES_KEPT, sweepDrafts, type StagedFile } from "./draft";
import { ErrorNote } from "./ErrorNote";
import { Guard } from "./Guard";
import { enterAction, touchScreen } from "./keys";
import { Menu, type MenuItem } from "./Menu";
import { stepPrimary, type PrimaryAction, type PrimaryEvent, type PrimaryState } from "./primary";
import { QueueTray } from "./QueueTray";
import { RequestDock, SETTLE_MS } from "./RequestDock";
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
/** The default wait for an accepted answer or edit to show. */
export const CONFIRM_MS = 15_000;

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
   * How long an accepted answer or edit waits to be seen taking effect (an answer's request ending, an edit showing in the
   * queue) before the view stops waiting and says it was not confirmed. The hub accepts a command before an agent on
   * another machine has run it, and the store has no later result for these, so what the view sees is all it has.
   */
  confirmMs?: number;
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

/** Keeps a scroll area at its end while its content changes size, unless the person has scrolled away from the end. */
function useStickToBottom(area: RefObject<HTMLDivElement | null>): void {
  useLayoutEffect(() => {
    const el = area.current;
    if (el === null || typeof ResizeObserver === "undefined") return;
    let atEnd = true;
    const onScroll = (): void => {
      atEnd = el.scrollHeight - el.clientHeight - el.scrollTop <= 4;
    };
    const follow = (): void => {
      if (atEnd) el.scrollTop = el.scrollHeight;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    // Its size changes when the room does; its content's when a request, the tray or a note comes or goes.
    const watcher = new ResizeObserver(follow);
    watcher.observe(el);
    for (const child of Array.from(el.children)) watcher.observe(child);
    const content = new MutationObserver(() => {
      for (const child of Array.from(el.children)) watcher.observe(child);
      follow();
    });
    content.observe(el, { childList: true, subtree: true });
    follow();
    return () => {
      el.removeEventListener("scroll", onScroll);
      watcher.disconnect();
      content.disconnect();
    };
  }, [area]);
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

function Form({ store, sessionKey, upload, settleMs, holdMs, confirmMs, chip }: Required<Omit<ComposerProps, "chip">> & { chip: ReactNode }) {
  const { draft, latest, update, clear, kept } = useDraft(sessionKey);
  const { host, stack } = useFloating();
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // Attachments the restored draft lost because the hub no longer keeps them: said once, until the person dismisses it.
  const [dropped, setDropped] = useState<string[]>(() => draft.dropped ?? []);
  const [uploads, setUploads] = useState<Uploading[]>([]);
  const [stopState, setStopState] = useState<"idle" | "sending" | "sent">("idle");
  const [menu, setMenu] = useState<"attach" | "send" | null>(null);
  const guard = useRef(false);
  const stopGuard = useRef(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const above = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const primary = useRef<HTMLButtonElement>(null);
  const plus = useRef<HTMLButtonElement>(null);
  const composing = useRef(false);
  const uploadCount = useRef(0);
  const openUploads = useRef(0);
  const aborts = useRef(new Set<AbortController>());
  const hold = useRef<ReturnType<typeof setTimeout> | null>(null);
  const primaryState = useRef<PrimaryState>("idle");
  // Whether the menu behind the primary button applies now (a reply is running and has a second way to send), read when the hold timer runs.
  const holdApplies = useRef(false);
  const reasonId = useId();

  const busyTurn = useSessionStore(store, (s) => s.session.openTurn !== null || s.session.phase === "running" || s.session.phase === "waiting");
  const steerOk = useSessionStore(store, (s) => s.session.caps?.steer === true);
  const steerEffect = useSessionStore(store, (s) => steerEffectOf(s.session.caps));
  const requestOpen = useSessionStore(store, (s) => s.session.pending.length > 0);
  const queue = useSessionStore(store, (s) => s.session.queue);
  const steerWords = steerWording(steerEffect, requestOpen);
  // A steer that lands when the reply ends is what Queue does: it is not offered as a second way to do the same thing.
  const menuOffered = steerEffect !== "next_turn";
  holdApplies.current = busyTurn && menuOffered;
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

  // Drafts that were not touched for two weeks are dropped when the box opens, and all of them when the device signs out.
  useEffect(() => {
    sweepDrafts();
    return dropDraftsOnSignOut();
  }, []);

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
    // A file the hub has probably deleted (the page was left open for days) is taken out, the text is kept, and nothing is sent.
    const stale = sent.files.filter((file) => !fileKept(file));
    if (stale.length > 0) {
      guard.current = false;
      update((now) => ({ ...now, files: now.files.filter((file) => fileKept(file)) }));
      setDropped(stale.map((file) => file.name));
      return;
    }
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
  // arrow-up key opens a menu with the other way to send. The press that opened the menu never also sends, in
  // whatever order the browser delivers the events of its release: `primary.ts` is the table, this is its wiring.
  const stopHold = (): void => {
    if (hold.current !== null) clearTimeout(hold.current);
    hold.current = null;
  };
  const feed = (event: PrimaryEvent): PrimaryAction => {
    const step = stepPrimary(primaryState.current, event);
    primaryState.current = step.state;
    if (step.action === "stop-hold") stopHold();
    return step.action;
  };
  const openSendMenu = (): void => {
    stopHold();
    if (menuOffered) setMenu("send");
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
          update((now) => ({ ...now, files: [...now.files, { ...staged, at: staged.at ?? Date.now() }] }));
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

  // The area above the bar scrolls when the room is short (the keyboard is up, a menu is open). It stays at its bottom, where
  // the answers are, as its content changes size.
  useStickToBottom(above);

  const note = connectionNote(connection, loaded);
  const steerReason = busyTurn && !steerOk ? "This agent cannot send a message ahead of the queue. A message sent now waits and goes when the reply ends." : null;
  const reason = notice ?? blockedBy ?? (hasContent ? steerReason : null) ?? note;
  const stopDisabled = refusalMessage !== null || stopState !== "idle";
  const canSend = !submitting && hasContent && blockedBy === null;

  const attachItems: MenuItem[] = [{ id: "files", label: "Choose files", icon: "folder", safe: true, onSelect: () => choose("files") }];
  // A photo library is something a phone or a tablet has; a desktop browser's file picker already shows pictures.
  if (touchScreen()) attachItems.push({ id: "photos", label: "Photo library", icon: "photo", safe: true, onSelect: () => choose("photos") });
  // The safe item first: the menu opens on it, and the one that stops the reply is never the one it opens on.
  const sendItems: MenuItem[] = [
    { id: "queue", label: "Queue", icon: "queue", safe: true, hint: "Sent when the current reply ends.", disabled: !canSend, reason: "Write a message first.", onSelect: () => void submit("queue") },
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
        // The focus goes back to what opened the menu, unless it is somewhere the person put it (the box, the button).
        queueMicrotask(() => {
          if (document.activeElement === null || document.activeElement === document.body) (now === "attach" ? plus.current : primary.current)?.focus();
        });
        return null;
      });
    },
    [],
  );

  return (
    <div ref={host} className="sv-composer-host pointer-events-none absolute inset-0 z-10 flex flex-col justify-end" data-composer="form" data-composer-host>
      <div ref={stack} className={cx("sv-composer mx-auto flex w-full max-w-3xl flex-col gap-2", menu === "send" ? "max-h-3/4" : "max-h-1/2")}>
        {/* While the send menu is open it and the bar can use the whole height limit: what is above keeps at least 5 rem (the request's answers),
            the menu scrolls inside what is left of it, and the stack may stand a little over its limit rather than hide a waiting request. */}
        <div ref={above} className={cx("flex flex-col gap-2 overflow-y-auto", menu === "send" ? "min-h-20 empty:min-h-0" : "min-h-0")} data-composer-above>
          {(!persistent && outboxNote !== "") || (kept === "memory" && text !== "") || reason !== null || dropped.length > 0 ? (
            <div className="flex shrink-0 flex-col gap-1.5">
              {!persistent && outboxNote !== "" ? (
                <p className="sv-glass pointer-events-auto rounded-glass px-3 py-1.5 text-xs text-secondary" data-outbox-note role="status">
                  {asArrived(outboxNote)}
                </p>
              ) : null}
              {dropped.length > 0 ? (
                <p className="sv-glass pointer-events-auto flex items-start gap-2 rounded-glass px-3 py-1.5 text-xs text-secondary" data-dropped-files role="status">
                  <span className="min-w-0 flex-1 break-words">{`The hub no longer keeps ${plural(dropped.length, "attachment")} from this draft (${dropped.slice(0, 3).map((name) => cut(name, 40).text).join(", ")}${dropped.length > 3 ? ", ..." : ""}), so ${dropped.length === 1 ? "it was" : "they were"} left out. Your text is kept. Attach ${dropped.length === 1 ? "it" : "them"} again.`}</span>
                  <button aria-label="Dismiss this note" className="shrink-0 underline focus-visible:outline-2 focus-visible:outline-interactive" onClick={() => setDropped([])} type="button">
                    Dismiss
                  </button>
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
          <Guard message="The queue could not be shown. Queued messages are still sent when the reply ends." part="tray" resetKey={queue}>
            <QueueTray confirmMs={confirmMs} onKeep={keep} settleMs={settleMs} store={store} />
          </Guard>
          <Guard message="This request could not be shown. Answer it in the terminal or on another device." part="dock">
            <RequestDock confirmMs={confirmMs} settleMs={settleMs} store={store} />
          </Guard>
        </div>
        {/* In the stack, between what is above the bar and the bar: it does not cover a request that is waiting (the dock
            makes room and stays in view) or the draft (a draft of several lines has its first line level with the button). */}
        {menu === "send" ? (
          <div className="flex min-h-16 justify-end overflow-y-auto" data-send-menu>
            <Menu items={sendItems} label="Send options" onClose={closeMenu} placement="inline" />
          </div>
        ) : null}
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
                onBlur={() => void feed({ type: "blur" })}
                onClick={(event) => {
                  if (feed({ type: "click", detail: event.detail }) === "swallow") {
                    // The tail of a hold that opened the menu: not a press.
                    event.preventDefault();
                    return;
                  }
                  if (canSend) void submit("queue");
                }}
                onContextMenu={(event) => {
                  if (!busyTurn || !menuOffered) return;
                  event.preventDefault();
                  if (feed({ type: "contextmenu", menuOffered }) === "open-menu") openSendMenu();
                }}
                onKeyDown={(event) => {
                  feed({ type: "key" });
                  if (busyTurn && (event.key === "ArrowUp" || (event.shiftKey && event.key === "F10"))) {
                    event.preventDefault();
                    openSendMenu();
                  }
                }}
                onPointerCancel={() => void feed({ type: "cancel" })}
                onPointerDown={(event) => {
                  if (event.button !== 0) return;
                  const holdable = busyTurn && menuOffered;
                  if (feed({ type: "down", holdable }) === "start-hold") {
                    stopHold();
                    hold.current = setTimeout(() => {
                      hold.current = null;
                      // The reply may have ended during the hold (the button became Send): then it is only a long press, and no menu applies.
                      if (!holdApplies.current) return;
                      if (feed({ type: "hold" }) === "open-menu") openSendMenu();
                    }, holdMs);
                  }
                }}
                onPointerLeave={() => void feed({ type: "leave" })}
                onPointerUp={() => void feed({ type: "up" })}
                title={busyTurn ? "Queue this message (hold for more)" : "Send"}
                tone="primary"
              >
                {submitting ? <Spinner className="text-card" /> : <Icon className="size-4.5" name={busyTurn ? "queue" : "send"} />}
              </RoundButton>
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
export function Composer({ store, sessionKey, upload, settleMs = SETTLE_MS, holdMs = HOLD_MS, confirmMs = CONFIRM_MS, chip = null }: ComposerProps) {
  const readOnly = useSessionStore(store, (s) => readOnlyReason(s.session.row ?? null));
  const stage = useMemo<Upload>(() => upload ?? httpUpload(sessionKey), [upload, sessionKey]);
  if (readOnly !== null) return <ReadOnlyBar reason={readOnly} />;
  return <Form key={sessionKey} chip={chip} confirmMs={confirmMs} holdMs={holdMs} sessionKey={sessionKey} settleMs={settleMs} store={store} upload={stage} />;
}
