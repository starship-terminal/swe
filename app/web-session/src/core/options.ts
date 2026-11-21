// A running session's options (G32): the model, effort, permission mode
// and plan mode it offers, the value in force for each, and the changes
// this page has asked for.
//
// The hub says what a session offers in `session.options` (and the
// snapshot's `options`); `setOption` in the store sends `set_option`
// through `POST .../command`. Nothing here is optimistic: `value` is what
// the hub last said, and a change in flight is shown as `pending` beside
// it until the hub's `command.result` settles it. A refused or failed
// change is kept as `error` with the hub's own words.
//
// `describeOptions` is pure: the store calls it whenever what it reads
// changes and keeps the answer as `sessionOptions`, so a view reads one
// stable object.
import { gate } from "./gate";
import type { SessionState } from "./state";
import type { ConnectionStatus } from "./supervisor";
import type { OptionChoice, SessionOptions } from "./types";

/** `/v1/status` lists this when the hub journals `session.options` and takes `set_option` (G32). */
export const FEATURE_SESSION_OPTIONS = "session_options";

export type OptionName = keyof SessionOptions;
export const OPTION_NAMES: readonly OptionName[] = ["model", "effort", "permission_mode", "plan_mode"];

/** Plan mode is on or off; every other option is one of the agent's own ids. */
export type OptionValue<N extends OptionName = OptionName> = N extends "plan_mode" ? boolean : string;

/** What a change does to a turn in progress: held for the next turn, or sent at once with an effect nobody recorded. */
export type MidTurn = NonNullable<SessionOptions["model"]>["mid_turn"];

/** A change this page asked for and the hub has not settled. */
export interface PendingChange<V> {
  value: V;
  /** The command id it was sent under; null while it waits for the change before it to settle. */
  commandId: string | null;
}

/** The last change that did not take: the value asked for and the hub's reason, as the hub put it. */
export interface FailedChange<V> {
  value: V;
  message: string;
}

/** Why an option cannot be changed now. `CannotChange.message` is one sentence, ready to show. */
export type CannotChangeReason =
  | "not_live"
  | "catching_up"
  | "read_only"
  | "unauthorized"
  | "gone"
  | "outdated"
  | "unsupported_hub"
  | "not_kept"
  | "not_allowed";

export interface OptionView<V = string | boolean> {
  name: OptionName;
  /** The value in force, as the hub last said. "" for a string option the agent has not named yet. */
  value: V;
  /** The agent's own spelling of `value` when it differs (Claude's `claude-haiku-5-5` for `haiku`). */
  reported?: string;
  /** The values the agent lists, in its order; null when it lists none (show `value` alone). */
  choices: OptionChoice[] | null;
  midTurn: MidTurn;
  pending: PendingChange<V> | null;
  error: FailedChange<V> | null;
  /** Whether `setOption` would send a change now. A change in flight does not stop another: the last one wins. */
  canChange: boolean;
  reason: CannotChangeReason | null;
  /** One sentence saying why not, or null when it can change. */
  message: string | null;
}

export interface SessionOptionsView {
  /** The hub has said what this session offers. False before that, and always with a hub that has no G32. */
  known: boolean;
  /** A turn is in progress: a change is then labelled by its `midTurn`. */
  turnOpen: boolean;
  model?: OptionView<string>;
  effort?: OptionView<string>;
  permission_mode?: OptionView<string>;
  plan_mode?: OptionView<boolean>;
}

/** What the store tracks per option between `setOption` and the hub's answer. */
export interface OptionWork {
  /** The change sent: its command id, value, and whether the hub has taken the post (a receipt came back). */
  sending: { commandId: string; value: string | boolean; posted: boolean } | null;
  /** The latest change asked for while one was in flight; sent once that one settles. */
  next?: string | boolean;
  error: FailedChange<string | boolean> | null;
}

export type OptionsWork = Partial<Record<OptionName, OptionWork>>;

export interface DescribeOptionsInput {
  session: SessionState;
  connection: ConnectionStatus;
  /** Whether `/v1/status` lists `session_options`; null while the features are not known. */
  supported: boolean | null;
  work: OptionsWork;
}

const NAMES: Record<OptionName, string> = {
  model: "the model",
  effort: "the effort",
  permission_mode: "the permission mode",
  plan_mode: "plan mode",
};

/** Whether the session's caps let `set_option` change `name`. */
export function capsAllow(session: SessionState, name: OptionName): boolean {
  const caps = session.caps;
  if (caps === null) return false;
  return name === "plan_mode" ? caps.plan_mode : caps.options.includes(name);
}

/** Why no option of the session can change now, or null when they can (the caps are checked per option). */
export function optionsRefusal(
  session: SessionState,
  connection: ConnectionStatus,
  supported: boolean | null,
): { reason: CannotChangeReason; message: string } | null {
  const refusal = gate(connection, session);
  if (refusal !== null) return { reason: refusal.reason, message: refusal.message };
  if (supported !== true) {
    return {
      reason: "unsupported_hub",
      message:
        supported === null
          ? "Checking what the hub supports. Try again in a moment."
          : "The hub is too old to change this. Update SWE on that machine.",
    };
  }
  if (session.row?.mode !== "keeper") {
    return { reason: "not_kept", message: "Only a session SWE runs itself can change this." };
  }
  return null;
}

function view<V extends string | boolean>(
  name: OptionName,
  offered: { value: V; mid_turn: MidTurn; choices?: OptionChoice[]; reported?: string },
  work: OptionWork | undefined,
  refusal: { reason: CannotChangeReason; message: string } | null,
  allowed: boolean,
): OptionView<V> {
  let pending: PendingChange<V> | null = null;
  if (work?.next !== undefined) pending = { value: work.next as V, commandId: null };
  else if (work?.sending) pending = { value: work.sending.value as V, commandId: work.sending.commandId };
  const why = refusal ?? (allowed ? null : { reason: "not_allowed" as const, message: `This session cannot change ${NAMES[name]}.` });
  return {
    name,
    value: offered.value,
    ...(offered.reported === undefined ? {} : { reported: offered.reported }),
    choices: offered.choices ?? null,
    midTurn: offered.mid_turn,
    pending,
    error: (work?.error ?? null) as FailedChange<V> | null,
    canChange: why === null,
    reason: why?.reason ?? null,
    message: why?.message ?? null,
  };
}

/** What a view renders the options from. Only the options the hub says the session offers are there. */
export function describeOptions(input: DescribeOptionsInput): SessionOptionsView {
  const { session, connection, supported, work } = input;
  const offered = session.options;
  const out: SessionOptionsView = { known: offered !== null, turnOpen: session.openTurn !== null };
  if (offered === null) return out;
  const refusal = optionsRefusal(session, connection, supported);
  if (offered.model) out.model = view("model", offered.model, work.model, refusal, capsAllow(session, "model"));
  if (offered.effort) out.effort = view("effort", offered.effort, work.effort, refusal, capsAllow(session, "effort"));
  if (offered.permission_mode) {
    out.permission_mode = view(
      "permission_mode",
      offered.permission_mode,
      work.permission_mode,
      refusal,
      capsAllow(session, "permission_mode"),
    );
  }
  if (offered.plan_mode) {
    out.plan_mode = view("plan_mode", offered.plan_mode, work.plan_mode, refusal, capsAllow(session, "plan_mode"));
  }
  return out;
}

/** Two views say the same thing: the store keeps the old object then, so a view does not render again. */
export function sameOptions(a: SessionOptionsView, b: SessionOptionsView): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
