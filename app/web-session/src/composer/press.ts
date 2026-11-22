// What counts as a press of an answer that cannot be taken back (an answer to a request, a removal).
//
// A click is a press only when its own start was seen on that button, after the button could be pressed: the
// pointer went down on it (a click whose pointer went down somewhere else, before the button was there, is not a
// press of it), or, for a click a key makes, the key went down on it (a key held through the wait, or let go late,
// is not). A key that repeats is never a press. The second press of a two-press answer needs `needKey`: its
// click from a key must have its own key going down. Nothing here knows time: the caller keeps the button off
// (`disabled`) for as long as it should not be pressed, so a press cannot start in that time.
import { useRef, type KeyboardEvent, type MouseEvent } from "react";

export interface Presses {
  /** A pointer went down on the button `id`; it counts only if the button could be pressed (`ok`). */
  down(id: string, ok: boolean): void;
  /** A key went down on the button `id`; a repeat is kept from pressing anything. */
  key(event: KeyboardEvent<HTMLButtonElement>, id: string, ok: boolean): void;
  /** Whether this click is a press of the button `id`. It spends what the press was made of. */
  counts(click: MouseEvent<HTMLButtonElement>, id: string, needKey?: boolean): boolean;
  /** Forget every press that started: the button was just armed, or changed what it does. */
  clear(): void;
}

export function usePresses(): Presses {
  const pointerOn = useRef<string | null>(null);
  const keyOn = useRef<string | null>(null);
  const api = useRef<Presses>({
    down(id, ok) {
      pointerOn.current = ok ? id : null;
    },
    key(event, id, ok) {
      if (event.repeat) event.preventDefault();
      else if (event.key === "Enter" || event.key === " ") keyOn.current = ok ? id : null;
    },
    counts(click, id, needKey = false) {
      const fromPointer = click.detail > 0;
      const counted = fromPointer ? pointerOn.current === id : !needKey || keyOn.current === id;
      pointerOn.current = null;
      keyOn.current = null;
      return counted;
    },
    clear() {
      pointerOn.current = null;
      keyOn.current = null;
    },
  });
  return api.current;
}
