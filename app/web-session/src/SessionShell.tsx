import { describeKey } from "./session-key";

export const COMPOSER_PLACEHOLDER = "Ask anything. @ files, / commands";

interface Props {
  sessionKey: string;
}

/**
 * The session view's frame: breadcrumb header, timeline, composer. This
 * milestone (G10) only proves the toolchain, so the timeline is an empty
 * state and the composer is disabled; G12 and G13 fill them in.
 */
export function SessionShell({ sessionKey }: Props) {
  const crumbs = describeKey(sessionKey);
  return (
    <div className="swe-sv flex h-full min-h-0 flex-col bg-panel text-primary">
      <header className="shrink-0 border-b border-border pt-safe-top pr-safe-right pl-safe-left">
        <nav aria-label="Breadcrumb" className="flex h-12 min-w-0 items-center px-4">
          <ol className="flex min-w-0 items-center gap-1.5 text-sm">
            <li className="shrink-0 text-secondary">{crumbs.project}</li>
            <li aria-hidden="true" className="shrink-0 text-muted">
              /
            </li>
            <li aria-current="page" className="truncate font-medium" title={crumbs.title}>
              {crumbs.title}
            </li>
          </ol>
        </nav>
      </header>

      <main aria-label="Timeline" className="flex min-h-0 flex-1 overflow-y-auto pr-safe-right pl-safe-left">
        <div className="m-auto flex max-w-sm flex-col items-center gap-1 px-6 py-12 text-center">
          <p className="text-sm font-medium">No messages yet</p>
          <p className="text-xs text-secondary">The conversation shows here once this view is connected to the session.</p>
        </div>
      </main>

      <footer className="shrink-0 pr-safe-right pb-safe-bottom pl-safe-left">
        <div className="px-4 pt-2 pb-4">
          <form
            aria-label="Composer"
            className="mx-auto flex max-w-3xl items-end gap-2 rounded-card border border-border bg-card p-2 opacity-60"
            onSubmit={(event) => event.preventDefault()}
          >
            <textarea
              aria-label="Message"
              className="min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm placeholder:text-muted focus:outline-none"
              disabled
              placeholder={COMPOSER_PLACEHOLDER}
              rows={1}
            />
            <button
              aria-label="Send"
              className="grid size-8 shrink-0 place-items-center rounded-control bg-primary text-card"
              disabled
              type="submit"
            >
              <svg aria-hidden="true" className="size-4" fill="none" viewBox="0 0 16 16">
                <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
              </svg>
            </button>
          </form>
        </div>
      </footer>
    </div>
  );
}
