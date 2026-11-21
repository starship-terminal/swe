import type { ButtonHTMLAttributes } from "react";

import { cx } from "../ui";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** `primary` is the filled answer that lets the agent go on; `danger` is a refusal or a removal. */
  variant: "primary" | "plain" | "danger";
}

/**
 * A button for an answer in a dock or the tray: tall enough for a thumb, one set of colours per variant (a
 * class that sets the colour is never added on top of the plain one, since stylesheet order, not class
 * order, would decide which wins).
 */
export function AnswerButton({ variant, className, type = "button", ...rest }: Props) {
  return (
    <button
      className={cx(
        "inline-flex h-auto min-h-9 max-w-full items-center justify-center gap-1.5 rounded-control border px-3 py-1.5 text-center text-sm font-medium break-words whitespace-normal motion-safe:transition-colors",
        "focus-visible:outline-2 focus-visible:outline-interactive disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" && "border-transparent bg-primary text-card",
        variant === "plain" && "border-border text-primary hover:bg-hover disabled:hover:bg-transparent",
        variant === "danger" && "border-danger text-primary hover:bg-hover disabled:hover:bg-transparent",
        className,
      )}
      type={type}
      {...rest}
    />
  );
}
