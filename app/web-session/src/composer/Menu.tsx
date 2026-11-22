import { useEffect, useId, useRef, type ReactNode } from "react";

import { cx, Icon, type IconName } from "../ui";

export interface MenuItem {
  id: string;
  label: string;
  icon?: IconName;
  /** A line under the label that says what the item does. */
  hint?: string;
  /**
   * May take the focus when the menu opens: a choice that only sets something up or leaves things as they are
   * (Queue, choose a file). An item that sends, stops, steers or removes is never `safe`: the menu opens with
   * the focus on the menu itself, so a key still down from opening it cannot choose it.
   */
  safe?: boolean;
  /** Not available now: shown, with `reason`, and never selected. */
  disabled?: boolean;
  reason?: string;
  onSelect(): void;
}

interface Props {
  /** The menu's name for a screen reader. */
  label: string;
  items: MenuItem[];
  /** Closes the menu: an item was chosen, Escape was pressed, or the person went elsewhere. */
  onClose(): void;
  /** `above` floats over the page above its anchor; `inline` sits in the flow (inside a scrolling list). */
  placement?: "above-start" | "above-end" | "inline";
  children?: ReactNode;
}

// Which keys are down now, kept for the whole page: a menu opened by a key press asks which keys were still down
// when it opened, and ignores them until they are let go (a held or a doubled Enter chooses nothing).
const down = new Set<string>();
if (typeof document !== "undefined") {
  document.addEventListener("keydown", (event) => void down.add(event.key), true);
  document.addEventListener("keyup", (event) => void down.delete(event.key), true);
  window.addEventListener("blur", () => down.clear());
}

/** Tests only: forget which keys are down. */
export function forgetKeysDown(): void {
  down.clear();
}

/**
 * A small menu on the glass surface. Opens with the focus on its first item that is `safe` and can be chosen,
 * or on the menu itself when there is none (an item that sends, stops, steers or removes is never the one a
 * key press that opened the menu can reach). Moves with the arrow keys, closes on Escape, Tab or a press
 * elsewhere. An item that cannot be chosen is drawn with the reason and skipped by the keys. A key that was
 * down when the menu opened, and a repeat of any key, chooses nothing.
 */
export function Menu({ label, items, onClose, placement = "above-start" }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const id = useId();
  const held = useRef(new Set(down));

  useEffect(() => {
    const first = root.current?.querySelector<HTMLElement>('[role="menuitem"][data-safe="true"]:not([aria-disabled="true"])');
    (first ?? root.current)?.focus();
  }, []);

  useEffect(() => {
    const up = (event: KeyboardEvent): void => void held.current.delete(event.key);
    const gone = (): void => held.current.clear();
    document.addEventListener("keyup", up, true);
    window.addEventListener("blur", gone);
    return () => {
      document.removeEventListener("keyup", up, true);
      window.removeEventListener("blur", gone);
    };
  }, []);

  useEffect(() => {
    // A press elsewhere closes the menu once its click has happened, not when the pointer goes down: the menu is in the flow, so
    // closing it at pointerdown moves what is under the finger, and the press lands on nothing. The press that opened
    // the menu (a hold's release and its click) began before it was there, so it closes nothing.
    let armed = false;
    const outside = (event: Event): boolean => root.current !== null && !root.current.contains(event.target as Node);
    const down = (event: Event): void => {
      armed = outside(event);
    };
    const click = (event: Event): void => {
      if (armed && outside(event)) onClose();
      armed = false;
    };
    const cancel = (): void => {
      armed = false;
    };
    // On the next turn of the loop: the press that opened the menu must not close it.
    const timer = setTimeout(() => {
      document.addEventListener("pointerdown", down, true);
      document.addEventListener("click", click, true);
      document.addEventListener("pointercancel", cancel, true);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("click", click, true);
      document.removeEventListener("pointercancel", cancel, true);
    };
  }, [onClose]);

  // Escape closes the menu wherever the focus is: a finger lifting off the button that opened it can leave the focus on that
  // button (a touch makes the browser focus it), and the menu must not stay open under a key that cannot reach it.
  useEffect(() => {
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape" && !event.defaultPrevented) onClose();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);

  const move = (step: number, from: number): void => {
    const live = Array.from(root.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? []);
    if (live.length === 0) return;
    const at = live.indexOf(document.activeElement as HTMLElement);
    const next = from === -1 ? (step > 0 ? 0 : live.length - 1) : (at + step + live.length) % live.length;
    live[next]?.focus();
  };

  return (
    <div
      ref={root}
      aria-label={label}
      className={cx(
        "sv-enter sv-glass pointer-events-auto z-20 flex min-w-48 flex-col rounded-glass p-1.5 focus:outline-none",
        placement === "inline" ? "relative" : "absolute bottom-full mb-2",
        placement === "above-start" && "left-0",
        placement === "above-end" && "right-0",
      )}
      data-menu={id}
      onKeyDown={(event) => {
        // A key held down repeats: the repeat is never a choice (a button takes Enter on the key's going down).
        if (event.repeat) {
          event.preventDefault();
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          onClose();
        } else if (event.key === "Tab") onClose();
        else if (event.key === "ArrowDown") {
          event.preventDefault();
          move(1, 0);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          move(-1, 0);
        } else if (event.key === "Home") {
          event.preventDefault();
          move(1, -1);
        } else if (event.key === "End") {
          event.preventDefault();
          move(-1, -1);
        }
      }}
      role="menu"
      tabIndex={-1}
    >
      {items.map((item) => (
        <button
          key={item.id}
          aria-disabled={item.disabled === true}
          className={cx(
            "flex min-h-10 w-full items-center gap-2.5 rounded-control px-2.5 py-1.5 text-left text-sm focus-visible:outline-2 focus-visible:outline-interactive",
            item.disabled === true ? "cursor-not-allowed opacity-60" : "hover:bg-hover",
          )}
          data-menu-item={item.id}
          data-safe={item.safe === true}
          onClick={(event) => {
            if (item.disabled === true) return;
            // The click a key makes (no pointer: `detail` is 0) while Enter or Space is still down from before the menu opened.
            if (event.detail === 0 && (held.current.has("Enter") || held.current.has(" "))) return;
            onClose();
            item.onSelect();
          }}
          role="menuitem"
          title={item.disabled === true ? item.reason : item.hint}
          type="button"
        >
          {item.icon !== undefined ? <Icon className="size-4 shrink-0 text-secondary" name={item.icon} /> : null}
          <span className="flex min-w-0 flex-col">
            <span>{item.label}</span>
            {item.disabled === true && item.reason !== undefined ? (
              <span className="text-xs text-secondary">{item.reason}</span>
            ) : item.hint !== undefined ? (
              <span className="text-xs text-secondary">{item.hint}</span>
            ) : null}
          </span>
        </button>
      ))}
    </div>
  );
}
