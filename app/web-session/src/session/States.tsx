import type { ReactNode } from "react";

import { StateArrow, type ArrowColour } from "../ui";

function Frame({ children }: { children: ReactNode }) {
  return <div className="m-auto flex max-w-sm flex-col items-center gap-1.5 px-6 py-12 text-center">{children}</div>;
}

/** The first snapshot has not arrived. */
export function LoadingState() {
  return (
    <div aria-busy="true" className="mx-auto w-full max-w-3xl px-4 py-6" data-state="loading">
      <p className="sr-only" role="status">
        Loading the session
      </p>
      <div aria-hidden="true" className="space-y-5">
        <div className="sv-pulse ml-auto h-9 w-2/5 rounded-card bg-selected" />
        <div className="space-y-2">
          <div className="sv-pulse h-3.5 w-full rounded-control bg-selected" />
          <div className="sv-pulse h-3.5 w-11/12 rounded-control bg-selected" />
          <div className="sv-pulse h-3.5 w-2/3 rounded-control bg-selected" />
        </div>
        <div className="sv-pulse h-10 w-full rounded-card bg-selected" />
      </div>
    </div>
  );
}

/** The session loaded and nothing has been said in it yet. */
export function EmptyState() {
  return (
    <Frame>
      <p className="text-sm font-medium" data-state="empty">
        No messages yet
      </p>
      <p className="text-xs text-secondary">What you and the agent say shows here.</p>
    </Frame>
  );
}

/** Nothing could be loaded, and the connection says why. */
export function UnavailableState({ colour, title, text }: { colour: ArrowColour; title: string; text: string }) {
  return (
    <Frame>
      <p className="flex items-center gap-1.5 text-sm font-medium" data-state="unavailable">
        <StateArrow colour={colour} />
        {title}
      </p>
      <p className="text-xs text-secondary">{text}</p>
    </Frame>
  );
}
