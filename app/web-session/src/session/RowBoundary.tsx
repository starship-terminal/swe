import { Component, type ReactNode } from "react";

interface Props {
  /** The thing the boundary guards: when it changes, a row that failed is tried again. */
  resetKey: unknown;
  children: ReactNode;
}

/**
 * Catches an error while one row is drawn, so one bad item shows "This item could not be shown." and the
 * rest of the timeline stays. Without it a render error unmounts the whole view, and since the item is in
 * the journal it would do so again on every load of the session.
 */
export class RowBoundary extends Component<Props, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidUpdate(previous: Props): void {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <p className="rounded-card border border-border px-3 py-2 text-xs text-muted" data-row="failed" role="note">
          This item could not be shown.
        </p>
      );
    }
    return this.props.children;
  }
}

/**
 * The last line of defence: an error the rows' own boundaries did not catch (in the timeline itself)
 * shows a line in the timeline's place and leaves the header and the composer's place standing.
 */
export class TimelineBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <main aria-label="Timeline" className="flex min-h-0 flex-1 items-center justify-center px-6 text-center text-sm text-secondary" data-state="failed">
          The timeline could not be shown. Reload the page to try again.
        </main>
      );
    }
    return this.props.children;
  }
}
