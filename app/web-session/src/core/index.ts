// What the timeline (G12) and the composer (G13) import: the store, its
// types, and the pure reducer and selectors for views and tests that want
// them. The wire types are generated from the hub's schema.
export { createSessionStore, useSessionStore } from "./store";
export type {
  ActionResult,
  OptionRefusal,
  SendResult,
  SetOptionResult,
  SessionStore,
  SessionStoreOptions,
  SessionStoreState,
} from "./store";
export { fetchTransport, sessionPath } from "./transport";
export type { Transport } from "./transport";
export type { ConnectionState, ConnectionStatus, Environment, Timers } from "./supervisor";
export type { OutboxView, SendInput } from "./outbox";
export { LEGACY_CONFIRM_MS, MAYBE_SENT, OUTBOX_TTL_MS, TERMINAL_KEPT_MS } from "./outbox";
export type { OutboxEntry, SendStatus } from "./outbox-storage";
export { describeSends } from "./sends";
export { describeOptions, FEATURE_SESSION_OPTIONS, OPTION_NAMES } from "./options";
export type {
  CannotChangeReason,
  FailedChange,
  MidTurn,
  OptionName,
  OptionValue,
  OptionView,
  PendingChange,
  SessionOptionsView,
} from "./options";
export type { SendShown, SendView } from "./sends";
export type { Refusal, RefusalReason } from "./gate";
export { readOnlyReason } from "./gate";
export { reduce, reduceAll } from "./reducer";
export { emptySession } from "./state";
export type {
  CheckpointInfo,
  CheckpointItem,
  CommandRecord,
  CompactedItem,
  ErrorItem,
  LiveText,
  MessageItem,
  Notice,
  RequestEvent,
  RequestItem,
  Resolution,
  SessionState,
  TaskInfo,
  ThinkingItem,
  TimelineItem,
  ToolItem,
  TurnInfo,
} from "./state";
export * from "./types";
