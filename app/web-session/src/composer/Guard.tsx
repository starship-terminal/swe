import { Component, type ReactNode } from "react";

interface Props {
  /** What the person is told when the guarded part could not be drawn. */
  message: string;
  /** Which part failed, for tests and for a screen reader. */
  part: string;
  children: ReactNode;
  /** When this changes, a part that failed is tried again (the queue changed: the item that broke it may be gone). */
  resetKey?: unknown;
}

/**
 * An error boundary for one part of the composer: a dock, the tray, the box. A request or a queued message
 * that breaks the drawing code costs its own part, with a sentence saying so, and not the timeline, the
 * header or the rest of the composer. Remount it with a `key`, or change `resetKey`, to try again.
 */
export class Guard extends Component<Props, { failed: boolean }> {
  override state: { failed: boolean; resetKey: unknown } = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  static getDerivedStateFromProps(props: Props, state: { failed: boolean; resetKey: unknown }): { failed: boolean; resetKey: unknown } | null {
    return props.resetKey !== state.resetKey ? { failed: false, resetKey: props.resetKey } : null;
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <p className="rounded-card border border-border px-3 py-2 text-xs text-secondary" data-composer-failed={this.props.part} role="note">
          {this.props.message}
        </p>
      );
    }
    return this.props.children;
  }
}
