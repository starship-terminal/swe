// The session store: ties the reducer, the connection supervisor and the
// outbox together. It is the only thing the timeline (G12) and the composer
// (G13) import from the core.
//
//   const store = createSessionStore({ sessionKey, transport });
//   store.getState().start();
//   const items = useSessionStore(store, (s) => s.session.items);
//
// Everything with a side effect is injected (transport, timers, randomness,
// environment, IndexedDB), so a test builds a store over scripted ones.
import { createStore, type StoreApi } from "zustand/vanilla";
import { useStore } from "zustand";

import { gate, gateSend, type Refusal } from "./gate";
import { Outbox, randomOwner, SESSION_GONE, type OutboxView, type SendInput, type SendResultData } from "./outbox";
import { describeSends, type SendView } from "./sends";
import { DeferredStorage, MemoryStorage, openOutboxStorage, type OutboxEntry, type OutboxStorage } from "./outbox-storage";
import { applyHistory, applyLive, beginConnection, reduce } from "./reducer";
import { emptySession, type SessionState } from "./state";
import {
  backoffDelay,
  browserEnvironment,
  realTimers,
  Supervisor,
  type BackoffOptions,
  type ConnectionStatus,
  type Environment,
  type Timers,
} from "./supervisor";
import { sessionPath, type JsonResponse, type Transport } from "./transport";
import type { Frame, MessagesPage, Snapshot } from "./types";

export interface SessionStoreOptions {
  sessionKey: string;
  transport: Transport;
  timers?: Timers;
  random?: () => number;
  environment?: Environment;
  /** Opens the outbox's storage. Default: IndexedDB, or memory when that is missing or throws. */
  openStorage?: () => Promise<OutboxStorage>;
  /** A fresh command id: 1 to 64 characters of letters, digits, `_` and `-`. */
  newId?: () => string;
  backoff?: BackoffOptions;
  silenceMs?: number;
}

/** What an action that reaches the hub answers with. */
export type ActionResult =
  | { ok: true; commandId: string; receipt: string }
  | Refusal
  | { ok: false; reason: "refused"; message: string; status: number }
  | { ok: false; reason: "network"; message: string };

export type SendResult = { ok: true; entry: OutboxEntry } | Refusal;

export interface SessionStoreState {
  /** What the frames add up to. */
  session: SessionState;
  /** The state of the connection. */
  connection: ConnectionStatus;
  /** Sends not yet finished, and whether they survive a reload. */
  outbox: OutboxView;
  /** What each unfinished send shows, with its reason: what a message bubble reads. */
  sends: SendView[];
  /** `start` has run and not been stopped. */
  started: boolean;

  /** Open the stream and the outbox. Safe to call twice. */
  start(): void;
  /** Close everything. The store can be started again. */
  stop(): void;
  /** Check the connection now: the page came back, the network changed, or the native shell woke the app. */
  wake(): void;

  /** Queue a message for the session. It is kept until the hub has it, through a reload. */
  send(input: SendInput): Promise<SendResult>;
  /** Answer an approval or a question. `decision` is allow, deny, always, or the number of a menu row. Needs a live connection. */
  respond(requestId: string, decision: string): Promise<ActionResult>;
  /** Stop the turn. Needs a live connection. */
  interrupt(): Promise<ActionResult>;
  /** Restore the files to a checkpoint. Needs a live connection. */
  restore(checkpointId: string): Promise<ActionResult>;
  /** Edit, cancel, or send now (steer) a message in the queue. Needs a live connection. */
  queueAction(op: "edit" | "cancel" | "steer", commandId: string, text?: string): Promise<ActionResult>;
  /** Load the page of timeline before the oldest one held. */
  loadOlder(): Promise<void>;
  /** Forget a send (a failed one the person dismissed). */
  discardSend(commandId: string): Promise<void>;
  /** Send a failed message again as a new one. */
  retrySend(commandId: string): Promise<SendResult | undefined>;
  /** Why a gated action cannot run right now, or null when it can: for a view to disable a control and say why. */
  refusal(): Refusal | null;
}

export type SessionStore = StoreApi<SessionStoreState>;

/** A hook over a store, for the React views: `useSessionStore(store, (s) => s.session.items)`. */
export function useSessionStore<T>(store: SessionStore, selector: (state: SessionStoreState) => T): T {
  return useStore(store, selector);
}

function defaultNewId(): string {
  const bytes = new Uint8Array(12);
  const crypto = globalThis.crypto as Crypto | undefined;
  if (crypto?.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  // randomUUID needs a secure context, and the hub is often plain http on a LAN.
  return `w-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function refusedFrom(response: JsonResponse): ActionResult {
  const body = response.body as { error?: { message?: unknown } } | undefined;
  const text = body?.error?.message;
  const message = typeof text === "string" && text !== "" ? text : `The hub answered ${response.status}`;
  if (response.status === 401 || response.status === 403) {
    return { ok: false, reason: "unauthorized", message: "This device is not signed in to the hub." };
  }
  return { ok: false, reason: "refused", message, status: response.status };
}

const SNAPSHOT_TRIES = 5;

/** What the outbox needs from a `command.result`. */
function sendResult(data: { ok: boolean; note?: string; cancelled?: boolean }): SendResultData {
  return {
    ok: data.ok,
    ...(data.note === undefined ? {} : { note: data.note }),
    ...(data.cancelled === undefined ? {} : { cancelled: data.cancelled }),
  };
}

export function createSessionStore(options: SessionStoreOptions): SessionStore {
  const timers = options.timers ?? realTimers;
  const random = options.random ?? Math.random;
  const newId = options.newId ?? defaultNewId;
  const environment = options.environment ?? browserEnvironment();
  const key = options.sessionKey;
  const transport = options.transport;

  let supervisor: Supervisor | null = null;
  let outbox: Outbox | null = null;
  // What the outbox held when the store was stopped: a store started again keeps unsent messages
  // even when there is no database to read them back from.
  let carried: OutboxEntry[] = [];
  const owner = randomOwner();
  let generation = 0;
  let wasLive = false;
  let snapshotBusy = false;
  let snapshotWanted: { reason: "hole" | "epoch" } | null = null;
  let snapshotTimer: unknown = null;
  // Messages the stream ended while a snapshot was on its way: the snapshot's text for them is old.
  let endedMeanwhile = new Set<string>();

  const store = createStore<SessionStoreState>()((set, get) => {
    const setSession = (session: SessionState) => set({ session });

    /** Each unfinished send with what its bubble shows (`sends`): recomputed whenever what it reads changes. */
    const refreshSends = (): void => {
      const state = get();
      set({
        sends: describeSends({
          outbox: state.outbox,
          connection: state.connection,
          loaded: state.session.loaded,
          online: environment.online(),
        }),
      });
    };

    const setOutbox = (view: OutboxView): void => {
      set({ outbox: view });
      refreshSends();
    };

    const ingest = (frame: Frame): void => {
      const before = get().session;
      const { state, effects } = reduce(before, frame);
      set({ session: state });
      // The session's row is known now: sends that waited for that may go.
      if (!before.loaded && state.loaded) {
        refreshSends();
        outbox?.wake();
      }
      if (snapshotBusy) {
        const still = new Set(state.live.map((entry) => entry.id));
        for (const entry of before.live) if (!still.has(entry.id)) endedMeanwhile.add(entry.id);
      }
      if (frame.type === "command.result" && frame.command_id !== undefined && frame.command_id !== "") {
        outbox?.noteResult(frame.command_id, sendResult(frame.data));
      }
      if (frame.type === "command.receipt" && frame.command_id !== undefined && frame.command_id !== "") {
        outbox?.noteReceipt(frame.command_id, frame.data.status, frame.data.reason);
      }
      for (const effect of effects) {
        if (effect.type === "fetch_snapshot") wantSnapshot(effect.reason);
      }
    };

    // -- the snapshot a hole or an epoch change asks for -----------------

    const wantSnapshot = (reason: "hole" | "epoch"): void => {
      // An epoch change needs the whole snapshot, which covers a hole too.
      if (snapshotBusy) {
        snapshotWanted = { reason: snapshotWanted?.reason === "epoch" ? "epoch" : reason };
        return;
      }
      void fetchSnapshot(reason, 1);
    };

    const fetchSnapshot = async (reason: "hole" | "epoch", attempt: number): Promise<void> => {
      if (!snapshotBusy || attempt === 1) endedMeanwhile = new Set();
      snapshotBusy = true;
      const mine = generation;
      let body: unknown;
      try {
        const response = await transport.json(sessionPath(key, "snapshot"));
        if (response.status >= 200 && response.status < 300) body = response.body;
      } catch {
        // Retried below.
      }
      if (mine !== generation) return;
      if (body !== undefined && body !== null && typeof body === "object") {
        const snapshot = body as Snapshot;
        const held = get().session;
        if (reason === "epoch") {
          // A snapshot older than what the stream has already given is not taken.
          if (snapshot.epoch === held.epoch && snapshot.head < held.cursor) {
            snapshotBusy = false;
            return;
          }
          ingest({ v: 2, type: "session.snapshot", session: key, ts: timers.now() / 1000, data: snapshot });
        } else {
          setSession(applyLive(held, snapshot.live, endedMeanwhile));
        }
        snapshotBusy = false;
        const next = snapshotWanted;
        snapshotWanted = null;
        if (next !== null) wantSnapshot(next.reason);
        return;
      }
      if (attempt >= SNAPSHOT_TRIES) {
        // The next catch-up's state frame restores the text; so does the message's completion.
        snapshotBusy = false;
        snapshotWanted = null;
        return;
      }
      snapshotTimer = timers.setTimeout(() => {
        snapshotTimer = null;
        if (mine === generation) void fetchSnapshot(reason, attempt + 1);
      }, backoffDelay(attempt, random, options.backoff));
    };

    // -- the connection --------------------------------------------------

    const onStatus = (connection: ConnectionStatus): void => {
      const before = get().connection.state;
      set({ connection });
      refreshSends();
      const live = connection.state === "live";
      if (live && !wasLive) outbox?.wake();
      wasLive = live;
      // States nothing will send out of: every send still open ends now, with the reason.
      if (connection.state !== before) {
        if (connection.state === "gone") outbox?.sessionGone(SESSION_GONE);
        else if (connection.state === "outdated") outbox?.sessionGone("Not sent: the hub is too old for this view.");
      }
    };

    const gated = async (
      send: (commandId: string) => Promise<JsonResponse>,
    ): Promise<ActionResult> => {
      const refusal = gate(get().connection, get().session);
      if (refusal !== null) return refusal;
      const commandId = newId();
      let response: JsonResponse;
      try {
        response = await send(commandId);
      } catch (error) {
        return { ok: false, reason: "network", message: error instanceof Error ? error.message : "The hub could not be reached" };
      }
      if (response.status >= 200 && response.status < 300) {
        const receipt = (response.body as { receipt?: unknown } | undefined)?.receipt;
        return { ok: true, commandId, receipt: typeof receipt === "string" ? receipt : "accepted" };
      }
      return refusedFrom(response);
    };

    const post = (tail: string, body: Record<string, unknown>) =>
      transport.json(sessionPath(key, tail), { method: "POST", body });

    return {
      session: emptySession(),
      connection: { state: "connecting", attempt: 0, since: timers.now(), retryAt: null, detail: "" },
      outbox: { entries: [], persistent: false, note: "", paused: null, inFlight: null, owner: "", loading: false, storageDown: false },
      sends: [],
      started: false,

      start() {
        if (get().started) return;
        generation++;
        set({ started: true });
        const opening = (options.openStorage ?? (() => openOutboxStorage(undefined, () => outbox?.refresh())))();
        // `openOutboxStorage` never fails (it falls back to memory itself); a custom `openStorage` can.
        const storage = new DeferredStorage(
          Promise.resolve(opening).catch((error: unknown) => {
            const text = error instanceof Error ? error.message : String(error);
            return new MemoryStorage(`The outbox storage did not open (${text}); sends are kept in memory and will not survive a reload`);
          }),
        );
        outbox = new Outbox({
          initial: carried,
          session: key,
          storage,
          transport,
          timers,
          random,
          newId,
          // Nothing is posted before the session's row is known, while offline, or while the hub refuses.
          canPost: () =>
            environment.online() &&
            get().session.loaded &&
            !["unauthorized", "gone", "outdated", "offline"].includes(get().connection.state),
          // One owner for every outbox this store makes: a claim from before a stop() is this page's own.
          owner,
          // A post that was out when the store stopped answers to the outbox that replaced it.
          onLate: (entry, settled) => {
            // What a later start() hands over must not undo it: a settled send is not sent again.
            carried = carried.flatMap((e) => (e.commandId !== entry.commandId ? [e] : settled ? [] : [entry]));
            outbox?.adoptLate(entry, settled);
          },
          // A result this store took in before the outbox loaded its stored sends (a stop() and start(), say).
          knownReceipt: (commandId) => get().session.commands[commandId]?.receipt,
          knownResult: (commandId) => {
            const result = get().session.commands[commandId]?.result;
            return result === undefined ? undefined : sendResult(result);
          },
          ...(options.backoff === undefined ? {} : { backoff: options.backoff }),
          onChange: (view) => setOutbox(view),
        });
        // The new outbox's view at once, loading: the old one's would offer Discard on entries it no longer holds.
        setOutbox(outbox.view);
        void outbox.init();
        supervisor = new Supervisor({
          transport,
          timers,
          random,
          environment,
          sessionKey: key,
          cursor: () => {
            const { cursor, epoch } = get().session;
            return { last: cursor, epoch };
          },
          onFrame: ingest,
          onConnecting: () => setSession(beginConnection(get().session)),
          onStatus,
          ...(options.backoff === undefined ? {} : { backoff: options.backoff }),
          ...(options.silenceMs === undefined ? {} : { silenceMs: options.silenceMs }),
        });
        supervisor.start();
        set({ connection: supervisor.status });
      },

      stop() {
        if (!get().started) return;
        generation++;
        supervisor?.stop();
        supervisor = null;
        carried = outbox?.carry() ?? carried;
        outbox?.close();
        outbox = null;
        if (snapshotTimer !== null) timers.clearTimeout(snapshotTimer);
        snapshotTimer = null;
        snapshotBusy = false;
        snapshotWanted = null;
        wasLive = false;
        set({ started: false });
      },

      wake() {
        supervisor?.wake();
        outbox?.wake();
      },

      async send(input) {
        const refusal = gateSend(get().connection, get().session);
        if (refusal !== null) return refusal;
        if (outbox === null) {
          return { ok: false, reason: "not_live", message: "The session view has not started." };
        }
        return { ok: true, entry: await outbox.send(input) };
      },

      respond: (requestId, decision) =>
        gated((commandId) => post("approval", { request_id: requestId, decision, command_id: commandId })),

      interrupt: () => gated((commandId) => post("control", { op: "interrupt", command_id: commandId })),

      restore: (checkpointId) =>
        gated((commandId) => post("restore", { checkpoint_id: checkpointId, command_id: commandId })),

      queueAction: (op, queuedId, text) =>
        gated((commandId) =>
          post("queue", {
            client_id: queuedId,
            op,
            command_id: commandId,
            ...(op === "edit" && text !== undefined ? { text } : {}),
          }),
        ),

      async loadOlder() {
        const { hasMore, nextBefore, epoch, bootId } = get().session;
        if (!hasMore || nextBefore === null) return;
        const mine = generation;
        try {
          const response = await transport.json(`${sessionPath(key, "messages")}?v=2&before=${nextBefore}&limit=100`);
          if (mine !== generation || response.status < 200 || response.status >= 300) return;
          // A new journal began while the page was out (the page belongs to the old one), or a snapshot moved
          // the page marker (applying the page would leave a stretch later calls step over): drop the page.
          const now = get().session;
          if (now.epoch !== epoch || now.bootId !== bootId || now.nextBefore !== nextBefore) return;
          setSession(applyHistory(get().session, response.body as MessagesPage));
        } catch {
          // The button stays; try again.
        }
      },

      async discardSend(commandId) {
        await outbox?.discard(commandId);
      },

      async retrySend(commandId) {
        const refusal = gateSend(get().connection, get().session);
        if (refusal !== null) return refusal;
        const entry = await outbox?.retry(commandId);
        return entry === undefined ? undefined : { ok: true, entry };
      },

      refusal: () => gate(get().connection, get().session),
    };
  });

  return store;
}
