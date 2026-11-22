import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent, type MutableRefObject } from "react";

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

/**
 * The three meanings an answer can have for every agent: the hub gives each option a `kind` (allow, always, deny),
 * whatever the agent's own ids and labels are (Claude and Codex name their options `allow`, `always`, `deny`; an ACP
 * agent's are `o1`, `o2`...). Those options are the dock's fixed answers: pinned, in our own words, with what the
 * agent called the option as small text under them. An option with any other kind, or none, is the agent's own choice.
 */
type Fixed = "allow" | "always" | "deny";
const FIXED_WORD: Record<Fixed, string> = { allow: "Allow", always: "Always allow", deny: "Deny" };
/** How many options of one kind are pinned (so six at most); further ones are listed as the agent's choices. */
const FIXED_PER_KIND = 2;
/** How much of the agent's name for a fixed answer shows under our word. */
const FIXED_LABEL_SHOWN = 60;
/** Past this many characters a choice's label is cut to two lines on screen, and listed in full under "Options in full". */
const CHOICE_LONG = 80;

function fixedKind(option: Option): Fixed | null {
  if (option.standing === true) return "always";
  return option.kind === "allow" || option.kind === "always" || option.kind === "deny" ? option.kind : null;
}

/** What the agent called a fixed answer, unless that is just our word again. */
function agentName(option: Option, word: string): string | null {
  const given = option.label.trim();
  // The hub's own short names for the meanings ("Allow", "Always", "Always allow", "Deny") add nothing under ours.
  if (given === "" || [word.toLowerCase(), word.toLowerCase().replace(/ allow$/, "")].includes(given.toLowerCase())) return null;
  const shown = cut(given, FIXED_LABEL_SHOWN);
  return shown.cut ? `${shown.text}…` : shown.text;
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

function Panel({ store, event, waiting, settleMs, focusKept }: { store: SessionStore; event: RequestEvent; waiting: number; settleMs: number; focusKept: MutableRefObject<boolean> }) {
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
  // Keyboard focus: a request that replaces one the person had the focus in takes it on its heading (never on an answer),
  // so Tab does not start again from the top of the page. The old panel says it had the focus as it goes.
  const section = useRef<HTMLElement>(null);
  const heading = useRef<HTMLParagraphElement>(null);
  useLayoutEffect(() => {
    const node = section.current;
    if (focusKept.current) {
      focusKept.current = false;
      heading.current?.focus();
    }
    return () => {
      if (node !== null && node.contains(document.activeElement)) focusKept.current = true;
    };
  }, []);

  const all = optionsOf(event);
  // The fixed answers are the options whose `kind` says allow, always or deny (at most two of a kind),
  // wherever they are in the list: an agent cannot push Deny out of sight by listing forty choices before it. Every
  // other option is the agent's own choice, and only those are cut to `OPTIONS_SHOWN`.
  const numbered = all.map((option, index) => ({ option, index, fixed: fixedKind(option) }));
  const perKind: Record<string, number> = {};
  const fixed = numbered.filter((entry) => {
    if (entry.fixed === null || (perKind[entry.fixed] ?? 0) >= FIXED_PER_KIND) return false;
    perKind[entry.fixed] = (perKind[entry.fixed] ?? 0) + 1;
    return true;
  });
  const rest = numbered.filter((entry) => !fixed.includes(entry));
  const choices = rest.slice(0, OPTIONS_SHOWN);
  const hidden = rest.length - choices.length;
  const textQuestion = !approval && event.data.kind === "text";
  const blocked = settling || busy || sent || refusal !== null;
  const kind = approval ? (KIND_WORD[event.data.kind] ?? "Request") : "";
  // One filled answer: the first that lets the agent go on. The others (a standing grant included) are plain.
  const firstAllow = fixed.find((entry) => entry.fixed === "allow")?.index;
  const longChoices = choices.some(({ option }) => option.label.trim().length > CHOICE_LONG);

  const pick = (click: MouseEvent<HTMLButtonElement>, option: Option, index: number): void => {
    if (!presses.counts(click, option.id) || blocked) return;
    if (confirming === option.id && !confirmReady) return;
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

  // One line that is always there (so a screen reader has a live region before its text changes) and that says
  // what the answers can do now: wait, or go.
  const status =
    sent ? "Answer sent. Waiting for the agent."
    : refusal !== null ? refusal
    : settling ? "A new request just appeared. Read it before you answer."
    : confirming !== null ? (confirmReady ? "Ready. Press again to confirm." : "One moment, then press again.")
    : busy ? "Sending your answer."
    : "You can answer now.";

  const renderAnswer = ({ option, index, fixed: meaning }: { option: Option; index: number; fixed: Fixed | null }, kindOf: "fixed" | "choice") => {
    const confirm = confirming === option.id;
    const deny = meaning === "deny";
    const full = label(option.label);
    const word = meaning === null ? full : FIXED_WORD[meaning];
    const name = kindOf === "fixed" ? agentName(option, word) : null;
    const words = confirm ? "Press again to always allow" : word;
    return (
      <AnswerButton
        key={`${index}:${option.id}`}
        aria-label={kindOf === "choice" ? `Option ${index + 1}: ${full}` : name !== null && !confirm ? `${word}: ${name}` : undefined}
        className={kindOf === "choice" ? "w-full justify-start border-dashed text-left" : undefined}
        data-armed={confirm ? true : undefined}
        data-option={option.id}
        data-option-kind={kindOf}
        aria-disabled={blocked || (confirm && !confirmReady) ? true : undefined}
        onClick={(click) => pick(click, option, index)}
        onKeyDown={(key) => presses.key(key, option.id, !blocked && (confirming !== option.id || confirmReady))}
        onKeyUp={() => presses.keyUp()}
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
          <span className="flex min-w-0 flex-col items-center leading-tight">
            {option.standing === true ? (
              // Both texts take the same place, so arming the button changes neither its size nor its row.
              <span className="grid" data-fixed-word>
                <span className={cx("col-start-1 row-start-1", confirm && "invisible")}>{FIXED_WORD[meaning ?? "always"]}</span>
                <span aria-hidden={!confirm} className={cx("col-start-1 row-start-1", !confirm && "invisible")}>Press again to always allow</span>
              </span>
            ) : (
              <span data-fixed-word>{words}</span>
            )}
            {name !== null && !confirm ? <span className="line-clamp-2 text-xs font-normal break-words opacity-80" data-agent-name>{name}</span> : null}
          </span>
        )}
      </AnswerButton>
    );
  };

  return (
    <section
      aria-label={approval ? "Approval needed" : "Question from the agent"}
      className="sv-enter sv-glass pointer-events-auto flex flex-col rounded-glass px-3.5 py-3 shadow-sm"
      data-dock={approval ? "approval" : "input"}
      data-request-id={event.data.request_id}
      data-request-seq={event.seq}
      ref={section}
      role="group"
    >
      {/* What it is, then the request's text in an area of its own (it scrolls past 10 rem), then the answers. The dock does not
          shrink below its content: when the stack is short the area above the bar scrolls, anchored at its bottom, where the answers are. */}
      <p ref={heading} aria-live="polite" className="flex items-center gap-1.5 text-xs font-medium text-primary focus:outline-none" tabIndex={-1}>
        <StateArrow colour="yellow" />
        {approval ? "Approval needed" : "The agent has a question"}
        {approval || waiting > 0 ? <span className="font-normal text-muted">{`${approval ? ` · ${kind}` : ""}${waiting > 0 ? ` · ${waiting} more waiting` : ""}`}</span> : null}
      </p>
      <div className="max-h-40 overflow-y-auto" data-dock-text>
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
        {/* Above the answers, so that what comes and goes here moves the text up and never an answer. */}
        <div aria-live="polite" className="min-h-4 text-xs text-secondary" data-dock-status role="status">
          {message !== null ? <ErrorNote>{message}</ErrorNote> : status}
        </div>
        {confirming !== null ? (
          <p className="mt-1 text-xs text-secondary" data-note="standing">
            {`Always allow ${tool?.name !== undefined && tool.name !== "" ? cut(tool.name, NAME_SHOWN).text : "this kind of call"}? Pressing again turns off later questions of the same kind for this session. `}
            <button className="underline focus-visible:outline-2 focus-visible:outline-interactive" onClick={() => setConfirming(null)} type="button">
              Cancel
            </button>
          </p>
        ) : null}
        <div className="mt-1.5 flex flex-wrap gap-2">
          {fixed.map((entry) => renderAnswer(entry, "fixed"))}
          {textQuestion ? (
            <AnswerButton aria-disabled={blocked ? true : undefined} data-option="skip" onClick={() => !blocked && void press("deny")} variant="plain">
              Skip this question
            </AnswerButton>
          ) : null}
          {busy ? <Spinner className="self-center" /> : null}
        </div>
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
  // Set by a panel that goes while the keyboard focus is in it: the one that takes its place takes the focus.
  const focusKept = useRef(false);
  useEffect(() => {
    // No request took its place: nobody is owed the focus later.
    if (first === undefined) focusKept.current = false;
  });
  if (first === undefined) return null;
  return (
    <Guard key={`${first.data.request_id}:${first.seq}`} message="This request could not be shown. Answer it in the terminal or on another device." part="dock">
      <Panel event={first} focusKept={focusKept} settleMs={settleMs} store={store} waiting={waiting} />
    </Guard>
  );
}
