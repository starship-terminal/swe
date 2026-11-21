import { useEffect, useRef, useState } from "react";

import { useSessionStore, type RequestEvent, type SessionStore, type TimelineItem, type ToolItem } from "../core";
import { Collapsible, cx, InlineText, Spinner, StateArrow } from "../ui";
import { AnswerButton } from "./AnswerButton";
import { answer, decisionFor } from "./answer";
import { Guard } from "./Guard";
import { clip, cut, inputText, label, NAME_SHOWN, OPTIONS_SHOWN } from "./text";

/**
 * What a button says. A standing option the hub calls just "Always" says what it grants: "Always allow". Any
 * other label is the hub's, cut to a length and not changed.
 */
function wording(option: Option): string {
  if (option.standing === true && /^always$/i.test(option.label.trim())) return "Always allow";
  return label(option.label);
}

type Option = { id: string; label: string; kind?: string; standing?: boolean };

/** How far back from the end of the timeline the tool call a request asks about is looked for. */
const LOOKBACK = 300;

function findTool(items: readonly TimelineItem[], callId: string | undefined): ToolItem | undefined {
  if (callId === undefined || callId === "") return undefined;
  for (let i = items.length - 1; i >= 0 && i >= items.length - LOOKBACK; i--) {
    const item = items[i] as TimelineItem;
    if (item.kind === "tool" && item.callId === callId) return item;
  }
  return undefined;
}

const KIND_WORD: Record<string, string> = { command: "Command", plan: "Plan", question: "Question", tool: "Tool call" };

/** Text from the agent, cut to a size the page can afford, drawn as one text node in one block. */
function Clipped({ text, className }: { text: string; className?: string }) {
  const { text: shown, hidden } = clip(text);
  return (
    <>
      <p className={cx("max-h-32 overflow-auto text-sm leading-6 break-words whitespace-pre-wrap", className)} data-untrusted="prompt">
        <InlineText text={shown} />
      </p>
      {hidden > 0 ? <p className="mt-1 text-xs text-muted">{hidden.toLocaleString("en-US")} more characters not shown.</p> : null}
    </>
  );
}

function ToolDetails({ tool }: { tool: ToolItem | undefined }) {
  const [open, setOpen] = useState(false);
  if (tool === undefined) return null;
  const name = cut(tool.name ?? "Tool call", NAME_SHOWN);
  const written = inputText(tool.input);
  const body = clip(written !== "" ? written : (tool.summary ?? ""));
  if (body.text === "") return null;
  return (
    <Collapsible
      buttonClassName="px-1 py-1 text-xs text-secondary"
      header={<span className="min-w-0 truncate">{`Details: ${name.text}${name.cut ? "…" : ""}`}</span>}
      onToggle={setOpen}
      open={open}
    >
      <pre className="mt-1 max-h-48 overflow-auto rounded-block border border-border bg-subtle p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap break-words" data-untrusted="input">
        <code>{body.text}</code>
      </pre>
      {body.hidden > 0 ? <p className="mt-1 text-xs text-muted">{body.hidden.toLocaleString("en-US")} more characters not shown.</p> : null}
    </Collapsible>
  );
}

/**
 * Answers a request: one press, one answer, on the request the person saw. A second press while the first
 * is on its way (a double click, a held key) is refused by `answer`, which keeps a set of the answers in
 * flight, and is not an error to show: the first press is still doing its work.
 */
function useAnswering(store: SessionStore, event: RequestEvent) {
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const shown = { requestId: event.data.request_id, seq: event.seq };
  const press = async (decision: string): Promise<void> => {
    setBusy(true);
    setMessage(null);
    try {
      const outcome = await answer(store, shown, decision);
      if (outcome.ok) setSent(true);
      else if (outcome.reason !== "busy") setMessage(outcome.message);
    } finally {
      setBusy(false);
    }
  };
  return { busy, sent, message, press };
}

function optionsOf(event: RequestEvent): Option[] {
  const given = event.data.options;
  if (given !== undefined && given.length > 0) return given.map((option) => ({ ...option }));
  return event.type === "approval.requested" ? [{ id: "allow", label: "Allow", kind: "allow" }, { id: "deny", label: "Deny", kind: "deny" }] : [];
}

function Panel({ store, event, waiting, settleMs }: { store: SessionStore; event: RequestEvent; waiting: number; settleMs: number }) {
  const approval = event.type === "approval.requested";
  const [settling, setSettling] = useState(settleMs > 0);
  useEffect(() => {
    if (!settling) return;
    const timer = setTimeout(() => setSettling(false), settleMs);
    return () => clearTimeout(timer);
    // Armed once, when this request appears.
  }, []);
  const refusal = useSessionStore(store, (s) => s.refusal()?.message ?? null);
  const callId = event.type === "approval.requested" ? event.data.call_id : undefined;
  const tool = useSessionStore(store, (s) => findTool(s.session.items, callId));
  const { busy, sent, message, press } = useAnswering(store, event);
  const [confirming, setConfirming] = useState<string | null>(null);

  const all = optionsOf(event);
  const options = all.slice(0, OPTIONS_SHOWN);
  const textQuestion = !approval && event.data.kind === "text";
  const blocked = settling || busy || sent || refusal !== null;
  const kind = approval ? (KIND_WORD[event.data.kind] ?? "Request") : "";
  // One filled answer: the first that lets the agent go on. The others (a standing grant included) are plain.
  const firstAllow = options.findIndex((option) => option.kind === "allow" || option.id === "allow");

  const pick = (option: Option, index: number): void => {
    if (blocked) return;
    if (option.standing === true && confirming !== option.id) {
      setConfirming(option.id);
      return;
    }
    setConfirming(null);
    void press(decisionFor(option, index));
  };

  const status =
    message ??
    (sent ? "Answer sent. Waiting for the agent." : refusal !== null ? refusal : settling ? "A new request just appeared. Read it before you answer." : null);

  return (
    <section
      aria-label={approval ? "Approval needed" : "Question from the agent"}
      className="sv-enter max-h-72 overflow-y-auto rounded-card border border-warning bg-card px-3 py-2.5"
      data-dock={approval ? "approval" : "input"}
      data-request-id={event.data.request_id}
      data-request-seq={event.seq}
      role="group"
    >
      <p aria-live="polite" className="flex items-center gap-1.5 text-xs font-medium text-warning-text">
        <StateArrow colour="yellow" />
        {approval ? "Approval needed" : "The agent has a question"}
        {approval || waiting > 0 ? <span className="font-normal text-muted">{`${approval ? ` · ${kind}` : ""}${waiting > 0 ? ` · ${waiting} more waiting` : ""}`}</span> : null}
      </p>
      <Clipped className="mt-1.5" text={event.data.prompt} />
      <ToolDetails tool={tool} />
      {textQuestion ? (
        <p className="mt-1.5 text-xs text-secondary" data-note="text-answer">
          This view cannot send a typed answer yet. Answer it in the terminal, or skip the question.
        </p>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2">
        {options.map((option, index) => {
          const confirm = confirming === option.id;
          const deny = option.kind === "deny" || option.id === "deny";
          return (
            <AnswerButton
              key={`${index}:${option.id}`}
              data-option={option.id}
              disabled={blocked}
              onClick={() => pick(option, index)}
              variant={deny ? "danger" : index === firstAllow ? "primary" : "plain"}
            >
              {confirm ? "Press again to always allow" : wording(option)}
            </AnswerButton>
          );
        })}
        {textQuestion ? (
          <AnswerButton data-option="skip" disabled={blocked} onClick={() => void press("deny")} variant="plain">
            Skip this question
          </AnswerButton>
        ) : null}
        {confirming !== null ? (
          <AnswerButton onClick={() => setConfirming(null)} variant="plain">
            Cancel
          </AnswerButton>
        ) : null}
        {busy ? <Spinner className="self-center" /> : null}
      </div>
      {all.length > options.length ? (
        <p className="mt-1.5 text-xs text-muted">{`${all.length - options.length} more options are not shown. Answer in the terminal.`}</p>
      ) : null}
      {confirming !== null ? (
        <p className="mt-1.5 text-xs text-secondary" data-note="standing">
          {`Always allow ${tool?.name !== undefined && tool.name !== "" ? cut(tool.name, NAME_SHOWN).text : "this kind of call"}? Pressing again turns off later questions of the same kind for this session. Cancel to go back.`}
        </p>
      ) : null}
      {status !== null ? (
        <p aria-live="polite" className={cx("mt-1.5 text-xs", message !== null ? "text-danger-text" : "text-secondary")} data-dock-status role="status">
          {status}
        </p>
      ) : null}
    </section>
  );
}

/**
 * The oldest open request, docked above the message box with its answers. Others wait behind it. Nothing
 * here takes focus, so a key the person is typing never lands on an answer; a request that replaces the
 * one being read keeps its answers off for a moment, so a press aimed at the old one cannot land on it.
 */
export function RequestDock({ store, settleMs }: { store: SessionStore; settleMs: number }) {
  const first = useSessionStore(store, (s) => s.session.pending[0]);
  const waiting = useSessionStore(store, (s) => Math.max(0, s.session.pending.length - 1));
  const key = first === undefined ? null : `${first.data.request_id}:${first.seq}`;
  const last = useRef<{ key: string; at: number } | null>(null);
  // A request that comes in right after another one was on screen is a replacement, not a first appearance.
  const replacing = key !== null && last.current !== null && last.current.key !== key && Date.now() - last.current.at < 5000;
  useEffect(() => {
    if (key !== null) last.current = { key, at: Date.now() };
  });
  if (first === undefined || key === null) return null;
  return (
    <Guard key={key} message="This request could not be shown. Answer it in the terminal or on another device." part="dock">
      <Panel event={first} settleMs={replacing ? settleMs : 0} store={store} waiting={waiting} />
    </Guard>
  );
}
