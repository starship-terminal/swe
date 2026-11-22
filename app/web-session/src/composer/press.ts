// What counts as a press of an answer that cannot be taken back (an answer to a request, a removal).
//
// A click from a pointer is a press only when the pointer went down on that button, after the button could be
// pressed: a click whose pointer went down somewhere else, or before the button was there, is not a press of it.
// A click with no pointer (`detail` 0) is a key's, or an assistive technology's (a screen reader or voice control
// activates a button with a bare click). It is a press unless it comes from a key that went down on the button
// while it could not be pressed and is still being held or let go late (a held Enter, a Space released after the
// wait). A key that repeats is never a press. Nothing here knows time: the caller keeps the button off
// (`disabled`) for as long as it should not be pressed, so a press cannot start in that time.
import { useRef, type KeyboardEvent, type MouseEvent } from "react";

export interface Presses {
  /** A pointer went down on the button `id`; it counts only if the button could be pressed (`ok`). */
  down(id: string, ok: boolean): void;
  /** A key went down on the button `id`; a repeat is kept from pressing anything. */
  key(event: KeyboardEvent<HTMLButtonElement>, id: string, ok: boolean): void;
  /** The key was let go: a click that follows it in the same turn is still the key's. */
  keyUp(): void;
  /** Whether this click is a press of the button `id`. It spends what the press was made of. */
  counts(click: MouseEvent<HTMLButtonElement>, id: string): boolean;
  /** Forget every press that started: the button was just armed, or changed what it does. */
  clear(): void;
}

export function usePresses(): Presses {
  const pointerOn = useRef<string | null>(null);
  // Set when a key went down while the button could not be pressed: a click that comes from it is no press.
  const stale = useRef(false);
  const api = useRef<Presses>({
    down(id, ok) {
      pointerOn.current = ok ? id : null;
    },
    key(event, _id, ok) {
      if (event.repeat) event.preventDefault();
      else if (event.key === "Enter" || event.key === " ") stale.current = !ok;
    },
    keyUp() {
      // A Space's click comes right after its key is let go, in the same turn; forget the key after that.
      setTimeout(() => (stale.current = false), 0);
    },
    counts(click, id) {
      // A pointer's click spends the pointer's start; a key's (or an assistive technology's) spends the key's.
      if (click.detail > 0) {
        const counted = pointerOn.current === id;
        pointerOn.current = null;
        return counted;
      }
      const counted = !stale.current;
      stale.current = false;
      return counted;
    },
    clear() {
      pointerOn.current = null;
      stale.current = false;
    },
  });
  return api.current;
}
