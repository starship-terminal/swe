import { cx } from "./cx";

/** A small turning ring (it stops under reduced motion; the label next to it says what is going on). */
export function Spinner({ className }: { className?: string }) {
  return (
    <svg aria-hidden="true" className={cx("sv-spin size-3.5 shrink-0 text-muted", className)} fill="none" viewBox="0 0 16 16">
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeLinecap="round" strokeWidth="2" />
    </svg>
  );
}
