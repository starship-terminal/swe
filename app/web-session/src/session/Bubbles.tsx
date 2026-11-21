import { MAYBE_SENT, type MessageItem, type SendView } from "../core";
import { Button, cx, Icon, Prose, Spinner, StateArrow, type ArrowColour } from "../ui";
import { asArrived, baseName } from "./format";

/** Attachment chips drawn under one message: the list comes from the hub, and each chip is a node. */
const MAX_CHIPS = 50;

/** A message the person wrote: a quiet bubble on the right. */
export function UserBubble({ text, attachments, muted, tone }: { text: string; attachments?: readonly string[] | undefined; muted?: boolean; tone?: "danger" }) {
  return (
    <div className="flex justify-end">
      <div
        className={cx(
          "max-w-5/6 min-w-0 rounded-card border px-3.5 py-2 text-sm leading-6 break-words whitespace-pre-wrap",
          tone === "danger" ? "border-danger bg-subtle" : "border-border bg-subtle",
          muted && "text-secondary",
        )}
        data-bubble="user"
      >
        {text}
        {attachments !== undefined && attachments.length > 0 ? (
          <ul aria-label="Attachments" className="mt-1.5 flex flex-wrap gap-1.5 whitespace-normal">
            {attachments.slice(0, MAX_CHIPS).map((path) => (
              <li key={path} className="inline-flex items-center gap-1 rounded-control border border-border bg-card px-1.5 py-0.5 text-xs text-secondary" title={path}>
                <Icon className="size-3" name="file" />
                {baseName(path)}
              </li>
            ))}
          </ul>
        ) : null}
        {attachments !== undefined && attachments.length > MAX_CHIPS ? <p className="mt-1 text-xs whitespace-normal text-secondary">and {attachments.length - MAX_CHIPS} more attachments</p> : null}
      </div>
    </div>
  );
}

export function UserMessage({ item }: { item: MessageItem }) {
  return <UserBubble attachments={item.attachments} text={item.text} />;
}

export function AssistantMessage({ item }: { item: MessageItem }) {
  return <Prose text={item.text} />;
}

export function SystemMessage({ item }: { item: MessageItem }) {
  return <p className="text-center text-xs text-muted">{item.text}</p>;
}

/** A send ended in doubt: it may have reached the session, and will not be sent again by itself. */
export function inDoubt(send: SendView): boolean {
  return send.reason.startsWith(MAYBE_SENT) || send.reason.startsWith("This may or may not have been sent");
}

function arrowFor(send: SendView): ArrowColour {
  switch (send.shown) {
    case "failed":
      // While the outbox loads, a failed entry is shown with nothing to act on: it is not news yet, so not red.
      return inDoubt(send) || !send.canRetry ? "grey" : "red";
    case "unconfirmed":
      return "grey";
    default:
      return "yellow";
  }
}

/**
 * A message that is not in the timeline yet, drawn from `describeSends`: its words, one sentence on
 * where it stands, and the two actions the selector allows (Discard, Retry). The selector's flags
 * decide whether a button exists at all: while the outbox loads, or while a post is out, neither
 * is offered.
 */
export function SendBubble({
  send,
  note,
  onDiscard,
  onRetry,
}: {
  send: SendView;
  /** Why the last Retry did not go ahead, when the store refused it. */
  note?: string | undefined;
  onDiscard(id: string): void;
  onRetry(id: string): void;
}) {
  const failed = send.shown === "failed" || send.shown === "unconfirmed";
  return (
    <div className="flex flex-col gap-1" data-send={send.shown} data-command-id={send.commandId}>
      <UserBubble attachments={send.attachments} muted={!failed} text={send.text} {...(send.shown === "failed" && send.canRetry && !inDoubt(send) ? { tone: "danger" as const } : {})} />
      <div className="flex max-w-5/6 flex-col items-end gap-1.5 self-end text-xs text-secondary">
        <p className="flex items-start gap-1.5">
          {send.shown === "sending" ? <Spinner className="mt-0.5" /> : <StateArrow className="mt-0.5" colour={arrowFor(send)} />}
          <span>{asArrived(send.reason)}</span>
        </p>
        {note !== undefined ? (
          <p className="text-danger-text" role="alert">
            {note}
          </p>
        ) : null}
        {send.canRetry || send.canDiscard ? (
          <div className="flex gap-1.5">
            {send.canDiscard ? (
              <Button onClick={() => onDiscard(send.commandId)}>
                <Icon className="size-3.5" name="trash" />
                Discard
              </Button>
            ) : null}
            {send.canRetry ? (
              <Button onClick={() => onRetry(send.commandId)}>
                <Icon className="size-3.5" name="retry" />
                Retry
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
