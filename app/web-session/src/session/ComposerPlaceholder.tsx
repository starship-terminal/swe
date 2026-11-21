import { readOnlyReason, useSessionStore, type SessionStore } from "../core";

export const COMPOSER_PLACEHOLDER = "Ask anything. @ files, / commands";

/**
 * PLACEHOLDER, replaced by the composer milestone (G13): the message box, the approvals dock and the
 * queued tray go here. It is a disabled box, so the frame of the page is the final one; nothing in it
 * sends. The one live thing is the outbox's own note, which the core asks a view to show beside the
 * composer (storage that does not survive a reload).
 */
export function ComposerPlaceholder({ store }: { store: SessionStore }) {
  const note = useSessionStore(store, (s) => (s.outbox.persistent ? "" : s.outbox.note));
  const readOnly = useSessionStore(store, (s) => readOnlyReason(s.session.row));
  return (
    <footer className="shrink-0 pr-safe-right pb-safe-bottom pl-safe-left" data-placeholder="composer">
      <div className="mx-auto max-w-3xl px-4 pt-2 pb-4">
        {note !== "" ? (
          <p className="mb-2 text-xs text-secondary" data-outbox-note role="status">
            {note}.
          </p>
        ) : null}
        <form
          aria-label="Composer"
          className="flex items-end gap-2 rounded-card border border-border bg-card p-2 opacity-60"
          onSubmit={(event) => event.preventDefault()}
        >
          <textarea
            aria-label="Message"
            className="min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm placeholder:text-muted focus:outline-none"
            disabled
            placeholder={readOnly ?? COMPOSER_PLACEHOLDER}
            rows={1}
          />
          <button aria-label="Send" className="grid size-8 shrink-0 place-items-center rounded-control bg-primary text-card" disabled type="submit">
            <svg aria-hidden="true" className="size-4" fill="none" viewBox="0 0 16 16">
              <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
            </svg>
          </button>
        </form>
      </div>
    </footer>
  );
}
