import { useEffect, useState, type MouseEvent } from "react";

import { useSessionStore, type RequestEvent, type SessionStore, type TimelineItem, type ToolItem } from "../core";
import { Collapsible, cx, InlineText, Spinner, StateArrow } from "../ui";
import { AnswerButton } from "./AnswerButton";
import { answer, decisionFor } from "./answer";
import { ErrorNote } from "./ErrorNote";
import { Guard } from "./Guard";
import { usePresses } from "./press";
import { clip, cut, inputText, label, NAME_SHOWN, OPTIONS_SHOWN } from "./text";

/**
 * How long a request that has just appeared, first or replacing another, keeps its answers off, and how long the
 * second press of "Always allow" waits before it counts. A press aimed at what was under the finger a moment ago
 * (a tray strip, a line of the timeline) lands on a dock that pushed the page up: nothing answers in that time.
 */
export const SETTLE_MS = 500;

/** The words the hub reads as a decision itself; an option with any other id is one the agent made up. */
const DECISION_IDS = new Set(["allow", "always", "deny"]);
const FIXED_WORD: Record<string, string> = { allow: "Allow", always: "Always allow", deny: "Deny" };
/** A label this long is the hub's own word for the decision; a longer one is somebody's paragraph. */
const FIXED_LABEL_MAX = 40;
/** Past this many characters a choice's label is cut to two lines on screen, and listed in full under "Options in full". */
const CHOICE_LONG = 80;

/**
 * What a button says. A standing option the hub calls just "Always" says what it grants: "Always allow". Any
 * other label is the hub's, cut to a length and not changed.
 */
function wording(option: Option): string {
  if (option.standing === true && /^always$/i.test(option.label.trim())) return "Always allow";
  // A decision's button is ours: the hub's short word for it, or our own when the label is long. Whatever a
  // hostile label says, the fixed answers stay as short as they look.
  if (DECISION_IDS.has(option.id) && option.label.trim().length > FIXED_LABEL_MAX) return FIXED_WORD[option.id] as string;
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
      <p className={cx("text-sm leading-6 break-words whitespace-pre-wrap", className)} data-untrusted="prompt">
        <InlineText text={shown} />
      </p>
      {hidden > 0 ? <p className="mt-1 text-xs text-muted">{hidden.toLocaleString("en-US")} more characters not shown.</p> : null}
    </>
  );
}

/** The tool's input as one cut text. Made only when the disclosure is open: a hidden input costs nothing. */
function ToolInput({ tool }: { tool: ToolItem }) {
  const written = inputText(tool.input);
  const body = clip(written !== "" ? written : (tool.summary ?? ""));
  if (body.text === "") return null;
  return (
    <>
      <pre className="mt-1 max-h-48 overflow-auto rounded-block border border-border bg-subtle p-2.5 font-mono text-xs leading-5 whitespace-pre-wrap break-words" data-untrusted="input">
        <code>{body.text}</code>
      </pre>
      {body.hidden > 0 ? <p className="mt-1 text-xs text-muted">{body.hidden.toLocaleString("en-US")} more characters not shown.</p> : null}
    </>
  );
}

function ToolDetails({ tool }: { tool: ToolItem | undefined }) {
  const [open, setOpen] = useState(false);
  if (tool === undefined || (tool.input === undefined && (tool.summary === undefined || tool.summary === ""))) return null;
  const name = cut(tool.name ?? "Tool call", NAME_SHOWN);
  return (
    <Collapsible
      buttonClassName="px-1 py-1 text-xs text-secondary"
      header={<span className="min-w-0 truncate">{`Details: ${name.text}${name.cut ? "…" : ""}`}</span>}
      onToggle={setOpen}
      open={open}
    >
      <ToolInput tool={tool} />
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
  // The standing option that was pressed once, and whether the second press counts yet.
  const [confirming, setConfirming] = useState<string | null>(null);
  const [confirmReady, setConfirmReady] = useState(false);
  useEffect(() => {
    if (confirming === null) return;
    setConfirmReady(false);
    const timer = setTimeout(() => setConfirmReady(true), settleMs);
    return () => clearTimeout(timer);
  }, [confirming]);
  const presses = usePresses();

  const all = optionsOf(event);
  // The decisions are the first option with each of the words the hub reads as one (allow, always, deny), wherever
  // they are in the list: an agent cannot push Deny out of sight by listing forty choices before it. Every other option
  // is the agent's own choice, and only those are cut to `OPTIONS_SHOWN`.
  const numbered = all.map((option, index) => ({ option, index }));
  const seen = new Set<string>();
  const fixed = numbered.filter(({ option }) => DECISION_IDS.has(option.id) && !seen.has(option.id) && seen.add(option.id));
  const rest = numbered.filter((entry) => !fixed.includes(entry));
  const choices = rest.slice(0, OPTIONS_SHOWN);
  const hidden = rest.length - choices.length;
  const textQuestion = !approval && event.data.kind === "text";
  const blocked = settling || busy || sent || refusal !== null;
  const kind = approval ? (KIND_WORD[event.data.kind] ?? "Request") : "";
  // One filled answer: the first that lets the agent go on. The others (a standing grant included) are plain.
  const firstAllow = fixed.find(({ option }) => option.kind === "allow" || option.id === "allow")?.index;
  const longChoices = choices.some(({ option }) => option.label.trim().length > CHOICE_LONG);

  const pick = (click: MouseEvent<HTMLButtonElement>, option: Option, index: number): void => {
    // The confirming press of a standing grant needs a key of its own if it came from a key.
    const confirms = option.standing === true && confirming === option.id;
    if (!presses.counts(click, option.id, confirms) || blocked) return;
    if (option.standing === true) {
      if (confirming !== option.id) {
        // Arming: the press that did it is spent. Confirming needs a press that starts after the wait.
        setConfirming(option.id);
        return;
      }
    }
    setConfirming(null);
    void press(decisionFor(option, index));
  };

  const status =
    message ??
    (sent ? "Answer sent. Waiting for the agent." : refusal !== null ? refusal : settling ? "A new request just appeared. Read it before you answer." : null);

  const renderAnswer = ({ option, index }: { option: Option; index: number }, kindOf: "fixed" | "choice") => {
    const confirm = confirming === option.id;
    const deny = option.kind === "deny" || option.id === "deny";
    const full = label(option.label);
    const words = confirm ? "Press again to always allow" : kindOf === "choice" ? full : wording(option);
    return (
      <AnswerButton
        key={`${index}:${option.id}`}
        aria-label={kindOf === "choice" ? `Option ${index + 1}: ${full}` : undefined}
        className={kindOf === "choice" ? "w-full justify-start border-dashed text-left" : undefined}
        data-option={option.id}
        data-option-kind={kindOf}
        disabled={blocked || (confirm && !confirmReady)}
        onClick={(click) => pick(click, option, index)}
        onKeyDown={(key) => presses.key(key, option.id, !blocked)}
        onPointerDown={() => presses.down(option.id, !blocked)}
        title={kindOf === "choice" ? full : undefined}
        variant={kindOf === "choice" ? "plain" : deny ? "danger" : index === firstAllow ? "primary" : "plain"}
      >
        {kindOf === "choice" ? (
          <>
            <span className="shrink-0 text-muted">{`${index + 1}.`}</span>
            <span className="line-clamp-2 min-w-0 text-left break-words">{words}</span>
          </>
        ) : (
          words
        )}
      </AnswerButton>
    );
  };

  return (
    <section
      aria-label={approval ? "Approval needed" : "Question from the agent"}
      className="sv-enter sv-glass pointer-events-auto flex min-h-0 flex-col rounded-glass px-3.5 py-3 shadow-sm"
      data-dock={approval ? "approval" : "input"}
      data-request-id={event.data.request_id}
      data-request-seq={event.seq}
      role="group"
    >
      <div className="min-h-0 flex-1 overflow-y-auto" data-dock-text>
        <p aria-live="polite" className="flex items-center gap-1.5 text-xs font-medium text-primary">
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
        {choices.length > 0 ? (
          <div className="mt-2 flex flex-col gap-1.5" data-choices>
            {choices.map((entry) => renderAnswer(entry, "choice"))}
            {longChoices ? (
              <ChoicesInFull choices={choices.map(({ option, index }) => ({ n: index + 1, text: label(option.label) }))} />
            ) : null}
          </div>
        ) : null}
        {hidden > 0 ? (
          <p className="mt-1.5 text-xs text-muted">{`${hidden} more options are not shown. Answer in the terminal.`}</p>
        ) : null}
      </div>
      <div className="mt-2 shrink-0" data-dock-answers>
        <div className="flex flex-wrap gap-2">
          {fixed.map((entry) => renderAnswer(entry, "fixed"))}
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
        {confirming !== null ? (
          <p className="mt-1.5 text-xs text-secondary" data-note="standing">
            {`Always allow ${tool?.name !== undefined && tool.name !== "" ? cut(tool.name, NAME_SHOWN).text : "this kind of call"}? Pressing again turns off later questions of the same kind for this session. Cancel to go back.${confirmReady ? "" : " One moment, then press again."}`}
          </p>
        ) : null}
        {message !== null ? (
          <ErrorNote aria-live="polite" className="mt-1.5" data-dock-status>
            {message}
          </ErrorNote>
        ) : status !== null ? (
          <p aria-live="polite" className="mt-1.5 text-xs text-secondary" data-dock-status role="status">
            {status}
          </p>
        ) : null}
      </div>
    </section>
  );
}

/** Every choice's label in full, as plain text, for the ones the buttons cut to two lines. */
function ChoicesInFull({ choices }: { choices: { n: number; text: string }[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible buttonClassName="px-1 py-1 text-xs text-secondary" header={<span>Options in full</span>} onToggle={setOpen} open={open}>
      <ol className="max-h-40 list-none overflow-y-auto text-xs leading-5 break-words whitespace-pre-wrap" data-untrusted="options">
        {choices.map((choice) => (
          <li key={choice.n}>{`${choice.n}. ${choice.text}`}</li>
        ))}
      </ol>
    </Collapsible>
  );
}

/**
 * The oldest open request, docked above the message box with its answers. Others wait behind it. Nothing
 * here takes focus, so a key the person is typing never lands on an answer; every request that appears, the first
 * or one that replaces another, keeps its answers off for `settleMs`, so a press aimed at what was there a moment
 * ago cannot land on it. The decisions (Allow, Always allow, Deny) are pinned under the request's text, which
 * scrolls above them in its own area with the agent's own choices.
 */
export function RequestDock({ store, settleMs }: { store: SessionStore; settleMs: number }) {
  const first = useSessionStore(store, (s) => s.session.pending[0]);
  const waiting = useSessionStore(store, (s) => Math.max(0, s.session.pending.length - 1));
  if (first === undefined) return null;
  return (
    <Guard key={`${first.data.request_id}:${first.seq}`} message="This request could not be shown. Answer it in the terminal or on another device." part="dock">
      <Panel event={first} settleMs={settleMs} store={store} waiting={waiting} />
    </Guard>
  );
}
