import { useEffect, useId, useRef, type ReactNode } from "react";

import { cx, Icon, type IconName } from "../ui";

export interface MenuItem {
  id: string;
  label: string;
  icon?: IconName;
  /** A line under the label that says what the item does. */
  hint?: string;
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

/**
 * A small menu on the glass surface. Opens with the focus on its first item that can be chosen (never a
 * destructive one: put the safe item first), moves with the arrow keys, closes on Escape, Tab or a press
 * elsewhere. An item that cannot be chosen is drawn with the reason and skipped by the keys.
 */
export function Menu({ label, items, onClose, placement = "above-start" }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    const first = root.current?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])');
    first?.focus();
  }, []);

  useEffect(() => {
    const away = (event: Event): void => {
      if (root.current !== null && !root.current.contains(event.target as Node)) onClose();
    };
    // On the next turn of the loop: the press that opened the menu must not close it.
    const timer = setTimeout(() => document.addEventListener("pointerdown", away, true), 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("pointerdown", away, true);
    };
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
        "sv-enter sv-glass z-20 flex min-w-48 flex-col rounded-glass p-1.5",
        placement === "inline" ? "relative" : "absolute bottom-full mb-2",
        placement === "above-start" && "left-0",
        placement === "above-end" && "right-0",
      )}
      data-menu={id}
      onKeyDown={(event) => {
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
          onClick={() => {
            if (item.disabled === true) return;
            onClose();
            item.onSelect();
          }}
          role="menuitem"
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
