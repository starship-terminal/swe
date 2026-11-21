import type { ButtonHTMLAttributes } from "react";

import { cx } from "./cx";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: "plain" | "danger";
}

/** A small text button. A disabled one stays in the layout and says nothing is possible. */
export function Button({ tone = "plain", className, type = "button", ...rest }: Props) {
  return (
    <button
      className={cx(
        "inline-flex h-7 items-center gap-1.5 rounded-control border border-border px-2.5 text-xs font-medium motion-safe:transition-colors",
        "hover:bg-hover focus-visible:outline-2 focus-visible:outline-interactive disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent",
        tone === "danger" ? "text-danger-text" : "text-primary",
        className,
      )}
      type={type}
      {...rest}
    />
  );
}
