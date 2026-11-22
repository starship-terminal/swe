import type { CompactedItem, ErrorItem, LiveText, RequestItem } from "../core";
import { cx, Icon, InlineText, Prose, Spinner, StateArrow } from "../ui";
import { clip, plural } from "./format";
import type { TurnFooter } from "./model";

/** A reply still being written: the text so far, as prose, with a caret that stops under reduced motion. */
export function LiveRow({ live, stopped }: { live: LiveText; stopped: boolean }) {
  if (live.kind === "tool_use") {
    return (
      <p aria-live="off" className="flex items-center gap-2 text-sm text-secondary" data-live="tool_use">
        <Spinner />
        Preparing a tool call
      </p>
    );
  }
  if (live.kind === "thinking") {
    return (
      <div aria-live="off" className="rounded-card border border-border bg-subtle px-3 py-2" data-live="thinking">
        <p className="mb-1 flex items-center gap-2 text-xs text-muted">
          <Spinner />
          Thinking
        </p>
        <p className="text-sm leading-6 whitespace-pre-wrap text-secondary break-words">{live.text}</p>
      </div>
    );
  }
  return (
    <div aria-busy="true" aria-live="off" className="sv-live" data-live="assistant">
      <Prose text={live.text} />
      {stopped ? <p className="mt-1 text-xs text-muted">Catching up with the rest of this reply.</p> : null}
    </div>
  );
}

/** The agent is between steps: no text is streaming, and no work group is open. */
export function WorkingRow() {
  return (
    <p aria-live="polite" className="flex items-center gap-2 px-1.5 text-sm text-secondary" data-row="working">
      <Spinner />
      <span className="sv-pulse">Working</span>
    </p>
  );
}

const DECISION: Record<string, string> = { allow: "Allowed", always: "Always allowed", deny: "Denied" };

function answerText(item: RequestItem): { text: string; arrow: "green" | "grey" | "yellow" | "red" } {
  const resolution = item.resolution;
  if (resolution === undefined) return { text: "Waiting for your answer", arrow: "yellow" };
  const decision = resolution.decision === undefined ? undefined : DECISION[resolution.decision];
  switch (resolution.source) {
    case "user": {
      // A menu row the person chose: say which, by the label the agent offered.
      const chosen = resolution.optionId === undefined ? undefined : item.event.data.options?.find((option) => option.id === resolution.optionId);
      if (chosen !== undefined) return { text: `Chose: ${chosen.label}`, arrow: "green" };
      if (resolution.optionId !== undefined && decision === undefined && resolution.text === undefined) return { text: "Answered with an option", arrow: "green" };
      if (decision !== undefined) return { text: decision, arrow: resolution.decision === "deny" ? "grey" : "green" };
      return { text: resolution.text !== undefined && resolution.text !== "" ? `Answered: ${resolution.text}` : "Answered", arrow: "green" };
    }
    case "autonomy":
      return { text: decision === undefined ? "Answered automatically" : `${decision} automatically`, arrow: "green" };
    case "dropped":
      return { text: "No longer waiting for an answer", arrow: "grey" };
    case "expired":
      return { text: "Expired before it was answered", arrow: "grey" };
    case "superseded":
      return { text: "Replaced by a newer request", arrow: "grey" };
    case "turn_ended":
      return { text: "The turn ended before it was answered", arrow: "grey" };
    case "agent_exit":
      return { text: "The agent stopped before it was answered", arrow: "grey" };
    case "closed":
      return { text: "The session closed before it was answered", arrow: "grey" };
    case "harness":
      return { text: "Answered in the agent's own prompt", arrow: "grey" };
  }
}

const KIND_WORD: Record<string, string> = { command: "Command", plan: "Plan", question: "Question", tool: "Tool" };

/**
 * An approval or a question the agent asked, with how it ended. The answer controls are the
 * approvals dock's (the composer milestone); a request still open only says that it waits.
 */
export function RequestRow({ item }: { item: RequestItem }) {
  const { text, arrow } = answerText(item);
  const data = item.event.data;
  const kind = item.event.type === "approval.requested" ? KIND_WORD[item.event.data.kind] : "Question";
  const open = item.resolution === undefined;
  // The same cut the timeline gives any text that came from outside: a request's prompt is the agent's, of any size.
  const shown = clip(data.prompt);
  return (
    <div className={cx("rounded-card border px-3 py-2", open ? "border-warning bg-subtle" : "border-border")} data-request={open ? "open" : "closed"}>
      <p className="text-xs text-muted">{kind ?? "Request"}</p>
      <p className="mt-0.5 text-sm leading-6 break-words whitespace-pre-wrap">
        <InlineText text={shown.text} />
      </p>
      {shown.hidden > 0 ? <p className="text-xs text-muted">{`${shown.hidden.toLocaleString("en-US")} more characters not shown.`}</p> : null}
      <p className="mt-1.5 flex items-center gap-1.5 text-xs text-secondary">
        <StateArrow colour={arrow} />
        {text}
      </p>
    </div>
  );
}

function tokens(count: number): string {
  return count >= 1000 ? `${Math.round(count / 100) / 10}k tokens` : plural(count, "token");
}

/** A notice in the flow: the context was compacted, or the agent hit an error. */
export function NoticeRow({ item }: { item: CompactedItem | ErrorItem }) {
  if (item.kind === "compacted") {
    return (
      <div className="flex items-center gap-3 text-xs text-muted" data-row="compacted">
        <span aria-hidden="true" className="h-px flex-1 bg-border" />
        <span className="flex items-center gap-1.5">
          <Icon className="size-3.5" name="compact" />
          Context compacted{item.tokensBefore !== undefined ? ` from ${tokens(item.tokensBefore)}` : ""}
        </span>
        <span aria-hidden="true" className="h-px flex-1 bg-border" />
      </div>
    );
  }
  return (
    <div className="rounded-card border border-danger bg-subtle px-3 py-2" data-row="error" role="alert">
      <p className="flex items-center gap-1.5 text-sm font-medium text-danger-text">
        <StateArrow colour="red" />
        {item.fatal === true ? "The agent hit an error and stopped" : "The agent hit an error"}
      </p>
      <p className="mt-1 text-sm leading-6 break-words whitespace-pre-wrap">{item.message}</p>
      <p className="mt-1 text-xs text-muted">{item.code}</p>
    </div>
  );
}

const ENDED: Record<string, string> = {
  failed: "The turn failed",
  interrupted: "Stopped before it finished",
  keeper_lost: "The session's process was lost mid-turn",
};

/** Under a finished turn: how long it took, what it changed, and how it ended when that was not well. */
export function FooterRow({ footer }: { footer: TurnFooter }) {
  const ended = footer.turn.status !== undefined && footer.turn.status !== "completed" ? ENDED[footer.turn.status] : undefined;
  const bad = footer.turn.status === "failed" || footer.turn.status === "keeper_lost";
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted" data-row="footer">
      {footer.files > 0 ? (
        <span>
          {plural(footer.files, "file")} <span className="text-success">+{footer.added}</span> <span className="text-danger-text">−{footer.removed}</span>
        </span>
      ) : null}
      {ended !== undefined ? (
        <span className={cx("flex items-center gap-1.5", bad ? "text-danger-text" : "text-secondary")}>
          <StateArrow colour={bad ? "red" : "grey"} />
          {ended}
          {footer.turn.reason !== undefined && footer.turn.reason !== "" ? `: ${footer.turn.reason}` : ""}
        </span>
      ) : null}
    </p>
  );
}
