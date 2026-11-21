import type { SVGProps } from "react";

/** The icons the session view draws, as stroke paths on a 16 by 16 grid. */
const PATHS = {
  chevron: "M6 3.5 10.5 8 6 12.5",
  check: "M3.5 8.5 6.5 11.5 12.5 4.5",
  x: "M4 4 12 12M12 4 4 12",
  alert: "M8 2.5 14 13H2L8 2.5ZM8 6.5v3M8 11.2v.1",
  tool: "M9.5 3a3.5 3.5 0 0 0 3.3 4.7L7 13.5a1.4 1.4 0 0 1-2-2l5.8-5.8A3.5 3.5 0 0 0 9.5 3Z",
  terminal: "M2.5 3.5h11v9h-11v-9ZM5 6.5 7 8.5 5 10.5M8.5 10.5H11",
  file: "M4 2.5h5l3 3v8H4v-11ZM9 2.5v3h3",
  thought: "M8 2.5a4 4 0 0 0-2.4 7.2c.3.3.4.6.4 1v.8h4v-.8c0-.4.1-.7.4-1A4 4 0 0 0 8 2.5ZM6.5 13.5h3",
  checkpoint: "M8 2.5v11M8 5.5 12.5 8 8 10.5",
  arrowDown: "M8 3v10M3.5 8.5 8 13l4.5-4.5",
  retry: "M3 8a5 5 0 1 1 1.6 3.7M3 12V8.2h3.8",
  trash: "M3.5 4.5h9M6.5 4.5v-1.5h3v1.5M5 4.5l.5 8h5l.5-8",
  compact: "M3 5.5h10M3 10.5h10M8 2.5v3M8 10.5v3",
  send: "M8 13V3M3.5 7.5 8 3l4.5 4.5",
  attach: "M12.5 7.5 7.8 12.2a2.8 2.8 0 0 1-4-4L8.6 3.4a1.9 1.9 0 0 1 2.7 2.7L6.5 10.9a.9.9 0 0 1-1.3-1.3L9.4 5.4",
  stop: "M4.5 4.5h7v7h-7v-7Z",
  edit: "M3 13l.6-2.8 7.1-7.1a1.4 1.4 0 0 1 2 2l-7.1 7.1L3 13ZM9.5 4.3l2.2 2.2",
  user: "M8 8a2.7 2.7 0 1 0 0-5.4A2.7 2.7 0 0 0 8 8ZM2.8 13.5c.6-2.4 2.6-3.6 5.2-3.6s4.6 1.2 5.2 3.6",
} as const;

export type IconName = keyof typeof PATHS;

interface Props extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: IconName;
}

/** A decorative icon: it takes the colour of the text around it. Pair it with words. */
export function Icon({ name, className, ...rest }: Props) {
  return (
    <svg aria-hidden="true" className={className ?? "size-4"} fill="none" viewBox="0 0 16 16" {...rest}>
      <path d={PATHS[name]} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.4" />
    </svg>
  );
}
