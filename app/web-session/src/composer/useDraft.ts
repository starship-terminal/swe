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
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [kept, setKept] = useState<Kept>("storage");

  const cancel = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  const flush = useCallback(() => {
    cancel();
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
   * typed meanwhile (then storage is brought up to date instead). Storage is written here and not left to
   * the pause timer: the value stored may be older than the box, and an older draft must not come back.
   */
  const clear = useCallback(
    (sentText: string) => {
      cancel();
      if (latest.current.text === sentText) {
        latest.current = EMPTY_DRAFT;
        setDraft(EMPTY_DRAFT);
      }
      // Also when this part is gone by now: `latest` still holds what was sent.
      setKept(writeDraft(session, latest.current));
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
