import { useId, useState, type ReactNode } from "react";

import { cx } from "./cx";
import { Icon } from "./Icon";

interface Props {
  /** The always-visible row: what it holds is the button's label. */
  header: ReactNode;
  open: boolean;
  onToggle(open: boolean): void;
  children: ReactNode;
  className?: string;
  buttonClassName?: string;
}

/**
 * A disclosure: a header button and a panel that grows and shrinks (the `.sv-collapse` rules in
 * session.css animate it, and do not under reduced motion). The panel's content is made the first
 * time it is opened, so a long session of closed work groups draws only their headers; once made it
 * stays, so closing it can animate. A closed panel is `inert`: its controls are out of the tab order. It is not a landmark (a long session would fill the screen reader's list); the button's `aria-expanded` and `aria-controls` say what it is.
 */
export function Collapsible({ header, open, onToggle, children, className, buttonClassName }: Props) {
  const id = useId();
  const [made, setMade] = useState(open);
  if (open && !made) setMade(true);
  return (
    <div className={className} data-open={open}>
      <button
        aria-controls={id}
        aria-expanded={open}
        className={cx(
          "group flex w-full min-w-0 items-center gap-2 rounded-control text-left motion-safe:transition-colors hover:bg-hover focus-visible:outline-2 focus-visible:outline-interactive",
          buttonClassName,
        )}
        onClick={() => onToggle(!open)}
        type="button"
      >
        <Icon className={cx("sv-chevron size-3.5 shrink-0 text-muted", open && "sv-chevron-open")} name="chevron" />
        {header}
      </button>
      <div className="sv-collapse" data-open={open} id={id} inert={!open}>
        <div className="sv-collapse-inner">{made ? children : null}</div>
      </div>
    </div>
  );
}
