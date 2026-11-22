import type { ComponentProps } from "react";

import { cx, StateArrow } from "../ui";

/**
 * A message the person must read because something went wrong. It has its own solid backing (the card
 * token, not the translucent glass) and ink at full strength, with the red in a border and an arrow: the
 * danger-coloured text token is only 4.2 on a dark card and falls under 3 over a busy backdrop on glass, so
 * no danger-coloured text goes on the glass (`glass.test.ts` checks the sources). Over the card the ink
 * is 15.2 in light and 12.6 in dark, whatever is behind the bar.
 */
export function ErrorNote({ children, className, ...rest }: ComponentProps<"p">) {
  return (
    <p className={cx("pointer-events-auto flex items-start gap-1.5 rounded-control border border-danger bg-card px-2.5 py-1.5 text-xs text-primary", className)} role="alert" {...rest}>
      <StateArrow className="mt-0.5" colour="red" />
      <span className="min-w-0 break-words">{children}</span>
    </p>
  );
}
