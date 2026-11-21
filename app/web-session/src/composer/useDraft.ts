import { useCallback, useEffect, useRef, useState } from "react";

import { EMPTY_DRAFT, readDraft, writeDraft, type Draft, type Kept } from "./draft";

/** How long typing pauses before the draft is written down. A hidden or closing page writes at once. */
export const SAVE_AFTER_MS = 250;

/**
 * The message being typed, held in state and kept in storage (see draft.ts). A change is written after a
 * short pause, and at once when the page is hidden, closed or this part unmounts, so a reload loses at
 * most what was typed in the last instant. `clear` is for after the outbox has the message.
 */
export function useDraft(session: string) {
  const [draft, setDraft] = useState<Draft>(() => readDraft(session));
  const latest = useRef(draft);
  // The texts this part has read from storage or written to it (the last few): a draft it owns. A draft in
  // storage that is none of these was written by another part, newer than anything this one knows of.
  const owned = useRef<string[]>([draft.text]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [kept, setKept] = useState<Kept>("storage");

  const cancel = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  const flush = useCallback(() => {
    cancel();
    owned.current = [...owned.current.slice(-3), latest.current.text];
    setKept(writeDraft(session, latest.current));
  }, [session, cancel]);

  const update = useCallback(
    (change: (now: Draft) => Draft) => {
      latest.current = change(latest.current);
      setDraft(latest.current);
      cancel();
      timer.current = setTimeout(flush, SAVE_AFTER_MS);
    },
    [flush, cancel],
  );

  /**
   * The message the draft held was taken by the outbox: forget it, in the box and in storage, unless more was
   * typed meanwhile (then storage is brought up to date instead). Storage is written here and not left to the
   * pause timer: the stored copy may be older than the box, and an older draft must not come back. But a copy
   * this part did not write (another part, mounted since, saved a newer draft for the session) is left alone:
   * a send that settles late must not erase what was typed after it.
   */
  const clear = useCallback(
    (sentText: string) => {
      cancel();
      if (latest.current.text === sentText) {
        latest.current = EMPTY_DRAFT;
        setDraft(EMPTY_DRAFT);
      }
      const stored = readDraft(session).text;
      if (stored === sentText || owned.current.includes(stored)) {
        owned.current = [...owned.current.slice(-3), latest.current.text];
        setKept(writeDraft(session, latest.current));
      }
    },
    [session, cancel],
  );

  useEffect(() => {
    const hide = (): void => {
      if (document.visibilityState === "hidden") flush();
    };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", hide);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", hide);
      flush();
    };
  }, [flush]);

  return { draft, latest, update, clear, kept };
}
