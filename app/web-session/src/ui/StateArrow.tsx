import { cx } from "./cx";

/**
 * The quiver state arrow, the repo's status indicator (AGENTS.md, "State arrows"): red is a critical
 * failure, yellow a partial failure or waiting on someone, green fully operational, grey interrupted
 * or unknown. The shape is the one in `state-<colour>.svg`; the fill is a token, so the arrow follows
 * the light and dark palettes. Always sits beside words, never alone.
 */
export type ArrowColour = "red" | "yellow" | "green" | "grey";

const FILL: Record<ArrowColour, string> = {
  red: "text-danger",
  yellow: "text-warning",
  green: "text-success",
  grey: "text-muted",
};

const SHAPE =
  "M5.67 3.06 L1.25 8.69 L1.17 8.82 L1.10 8.96 L1.04 9.11 L1.01 9.26 L1.00 9.42 L1.01 9.57 L1.04 9.73 L1.09 9.87 L1.16 10.01 L1.24 10.14 L1.34 10.26 L1.46 10.37 L1.59 10.45 L1.73 10.52 L1.88 10.57 L2.03 10.60 L2.19 10.62 L2.35 10.61 L2.50 10.57 L2.65 10.52 L6.61 8.88 L10.57 10.52 L10.72 10.57 L10.87 10.61 L11.03 10.62 L11.19 10.60 L11.34 10.57 L11.49 10.52 L11.63 10.45 L11.76 10.37 L11.88 10.26 L11.98 10.14 L12.06 10.01 L12.13 9.87 L12.18 9.73 L12.21 9.57 L12.22 9.42 L12.21 9.26 L12.18 9.11 L12.12 8.96 L12.05 8.82 L11.97 8.69 L7.55 3.06 L7.44 2.95 L7.33 2.84 L7.20 2.76 L7.06 2.69 L6.91 2.64 L6.76 2.61 L6.61 2.60 L6.46 2.61 L6.31 2.64 L6.16 2.69 L6.02 2.76 L5.89 2.84 L5.78 2.95 Z";

export function StateArrow({ colour, className }: { colour: ArrowColour; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      className={cx("size-3.5 shrink-0", FILL[colour], className)}
      data-arrow={colour}
      viewBox="0 0 13.22 13.22"
    >
      <path d={SHAPE} fill="currentColor" fillRule="evenodd" />
    </svg>
  );
}
