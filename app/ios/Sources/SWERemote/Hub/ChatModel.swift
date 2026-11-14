import Foundation
import Observation
import SwiftUI

/// One entry in a chat transcript, already folded for display: tool
/// results are merged into their call, approvals carry their state.
struct ChatItem: Identifiable, Equatable {
    enum Kind: Equatable {
        case user(text: String, send: SendState?)
        case assistant(String)
        /// `seconds` is how long the model thought, when the hub knew.
        case thinking(String, seconds: Double?)
        case tool(ToolCall, result: String?)
        case approval(requestId: String, prompt: String, state: ApprovalState)
        case compaction
        case system(String)
        case error(String)
    }

    let id: String
    var kind: Kind
}

enum SendState: Equatable {
    case sending, queued(String), failed(String)
    /// The machine took it, but the transcript may never echo it (a slash
    /// command such as /rename isn't a user message), so don't wait for that.
    case delivered(String?)

    /// Still on its way: a late reply to the send may change it.
    var inFlight: Bool {
        switch self {
        case .sending, .queued: true
        case .failed, .delivered: false
        }
    }

    /// What a `command.result` for this send turns it into; nil leaves it.
    static func settled(_ r: CommandResult, current: SendState, agentActive: Bool) -> SendState? {
        guard current.inFlight else { return nil }
        if r.ok == false { return .failed(r.note ?? "Send failed") }
        if r.degraded == true { return .queued(r.note ?? "Queued: mid-turn input isn't supported here") }
        // Mid-turn input waits for the agent's next step (Claude Code reads
        // it between tool calls), so say so rather than look ignored.
        return .delivered(agentActive ? "Delivered. The agent reads it at its next step" : nil)
    }
}

/// A send waiting in the Queued tray above the prompt box: made while the
/// agent worked, held by the hub until the session goes idle. It stays out
/// of the transcript until it goes out.
struct QueuedSend: Identifiable, Equatable {
    enum Phase: Equatable {
        /// The POST is on its way; the hub can't act on it yet.
        case posting
        case waiting
        /// Send now was tapped; its result or echo moves it to the transcript.
        case sendingNow
        /// Remove was tapped; hidden while the hub confirms.
        case removing
        /// The hub says it's no longer waiting (409 not_waiting); its
        /// result or echo is still to come.
        case left
    }

    let clientId: String
    /// Changes when the message is edited in place.
    var text: String
    var phase: Phase
    /// Why the last action on it didn't go through, shown under the text.
    var note: String?
    /// Refs of the files it carries; the hub keeps them through an edit.
    var attachments: [String] = []

    var id: String { clientId }
}

enum ApprovalState: Equatable {
    case pending, submitting, queued, resolved(String?), failed(String)
}

/// The open chat: the newest page of history from `/messages`, older
/// pages as you scroll up, then live events from the session's SSE
/// stream, plus optimistic sends.
@MainActor @Observable
final class ChatModel {
    let key: String
    private(set) var items: [ChatItem] = []
    private(set) var live = false
    private(set) var loading = true
    private(set) var loadError: String?
    /// A backfill was cancelled before it landed (the chat closed while
    /// it ran): no error to show, but the next start must reload, since a
    /// `journal.truncated` may already have moved `cursor` past history
    /// this chat never got.
    @ObservationIgnored private var backfillCut = false
    /// The hub holds messages older than the oldest one shown.
    private(set) var hasEarlier = false
    private(set) var loadingEarlier = false
    /// Each approval's numbered menu rows by request id, from its
    /// `approval.requested`; the card answers with them when present.
    private(set) var approvalOptions: [String: [ApprovalOption]] = [:]
    /// The view scrolled near the oldest loaded entry; it loads the next
    /// page once scrolling comes to rest.
    var wantsEarlier = false
    var draft = ""
    /// Sends the hub holds until the agent goes idle, oldest first. Ones
    /// being removed stay here, hidden, until the hub confirms; the tray
    /// shows `tray`.
    private(set) var queued: [QueuedSend] = []
    /// Sends that left the hub's queue while the chat was closed (no longer
    /// in its list). Hidden; kept so a replayed result or echo can still
    /// show them in the transcript, e.g. a slash command that never echoes.
    private var departed: [QueuedSend] = []
    /// The queued send whose text is in the prompt box to be edited; the
    /// send button saves it instead of sending.
    private(set) var editingId: String?
    /// What was typed before Edit took over the prompt box; Cancel and
    /// Save put it back.
    private var draftBeforeEdit = ""
    private(set) var savingEdit = false
    /// A short note in the bar above the prompt box: why an edit didn't save.
    private(set) var editNote: String?
    /// A short note under the tray's header, e.g. that an edit moved the
    /// message to the end.
    private(set) var trayNote: String?
    /// Client ids of sends that left the tray (went out, failed or were
    /// removed): a queue list fetched before that can't bring them back.
    private var settledSends = Set<String>()
    /// History is being reloaded: its user messages are old, not echoes
    /// of what's in the tray now.
    private var replayingHistory = false

    private weak var app: AppModel?
    /// The hub this chat belongs to. Its key means nothing on another
    /// hub, so once the app switches (or re-pairs), every request this
    /// chat still has to make, even halfway through an action, stops.
    private let openedOn: PairedHub?
    private var hubClient: HubClient? {
        guard let app, app.hub == openedOn else { return nil }
        return app.client
    }
    private var seen = Set<Int>()
    /// The seq the next stream resumes after.
    private(set) var cursor = 0
    /// Tool call id to the id of its card (ids, not positions: removing a
    /// send bubble would shift positions under a result still to come).
    private var toolCards: [String: String] = [:]
    /// Tool results whose call is on a page not loaded yet, by call id.
    private var orphanResults: [String: String] = [:]
    /// The seq to page back from: the oldest message loaded.
    private var nextBefore: Int?
    private var streamTask: Task<Void, Never>?
    /// Live events not yet shown. The hub polls a busy transcript about
    /// once a second, so several messages arrive together; they're shown
    /// one at a time instead of as a block.
    private var pending: [Envelope] = [] {
        didSet { updateWorkingSince() }
    }
    private var revealTask: Task<Void, Never>?
    /// When the last live entry was shown.
    private var lastReveal: ContinuousClock.Instant?
    /// How the reveal waits between entries; tests swap in a gate.
    var pause: (Duration) async -> Void = { try? await Task.sleep(for: $0) }
    /// A send went through and nothing from the agent has come back yet.
    private(set) var awaitingReply = false {
        didSet { updateWorkingSince() }
    }
    /// When `working` last turned on — the clock under the working pill.
    /// Living on the (cached) model, it keeps counting across a reopen
    /// of the chat instead of resetting with the view.
    private(set) var workingSince: Date?

    /// Recompute the working clock after any input to `working` moves.
    private func updateWorkingSince() {
        if working {
            if workingSince == nil { workingSince = Date() }
        } else {
            workingSince = nil
        }
    }
    /// Bumped by every start() and stop(), so a start() still fetching
    /// history doesn't open a stream after the chat was left.
    private var generation = 0
    /// On screen, between start() and stop(): what it shows counts as read.
    private(set) var isOpen = false
    /// The newest message stamp loaded (epoch seconds), which can be
    /// newer than the list's `last_active_at` while the chat streams.
    private(set) var newestActivity: Double?

    // MARK: Live reply text

    /// Replies still being written (`message.delta`, #1444), drawn after
    /// `items` until their final message takes their place. Rendered with
    /// the same Markdown as the final message: the text only grows, so a
    /// half-written block (an unclosed fence, a `**` not yet closed) is
    /// the only flicker, and switching to Markdown at the end would
    /// reflow the whole reply at once instead.
    private(set) var streaming: [ChatItem] = []
    @ObservationIgnored private var replies = ReplyStreams()
    /// Live finals not yet revealed, by journal seq, to the bubble each
    /// takes. Matched as they arrive (the contract's order is the wire's),
    /// shown when the reveal queue gets to them; the bubble stays until then.
    @ObservationIgnored private var claims: [Int: String] = [:]
    /// The pending repaint while text streams in: one per `repaintEvery`.
    @ObservationIgnored private(set) var repaintTask: Task<Void, Never>?
    static let repaintEvery: Duration = .milliseconds(80)
    /// How long a bubble outlives its session going idle: the final
    /// message comes from the transcript, which can land a poll later.
    static let idleGrace: Duration = .seconds(3)
    /// How repaints and the idle grace wait; tests swap in a no-op.
    var streamSleep: (Duration) async -> Void = { try? await Task.sleep(for: $0) }

    /// Messages per `/messages` page. A long session's 500-message page
    /// was ~850 KB and took seconds to lay out on the phone.
    static let pageSize = 100

    /// The prompt box's `@` menu.
    let mentions = MentionSearch()
    /// The prompt box's `/` menu.
    let commands = CommandMenu()
    /// Files picked for the next message, uploading or uploaded.
    let attachments = ComposerAttachments()
    /// Refs each send carried, by client id: a retry sends them again and
    /// the bubble names them until the transcript's echo replaces it.
    private(set) var sentFiles: [String: [String]] = [:]

    init(key: String, app: AppModel) {
        self.key = key
        self.app = app
        self.openedOn = app.hub
        mentions.fetch = { [weak self] query in
            guard let self, let client = self.hubClient else { throw HubError.network(CancellationError()) }
            return try await client.files(self.key, query: query, limit: MentionSearch.limit)
        }
        mentions.machine = { [weak self] in self?.machineName ?? "that machine" }
        commands.cacheKey = key
        // Sessions with no id share a path digest, so the kept lists
        // are scoped to the hub this chat was opened on too.
        commands.scope = { [weak self] in self?.openedOn?.id ?? "" }
        commands.fetch = { [weak self] in
            guard let self, let client = self.hubClient else { throw HubError.network(CancellationError()) }
            return try await client.commands(self.key)
        }
        attachments.upload = { [weak self] name, file, progress in
            guard let self, let client = self.hubClient else { throw HubError.network(CancellationError()) }
            return try await client.uploadAttachment(self.key, name: name, file: file, progress: progress)
        }
    }

    /// The names of the files a send bubble carries (`send-<client id>`).
    func fileNames(for itemId: String) -> [String] {
        guard itemId.hasPrefix("send-") else { return [] }
        return (sentFiles[String(itemId.dropFirst(5))] ?? []).map(AttachmentRules.displayName(ref:))
    }

    var row: SessionRow? { app?.session(key) }
    /// A copy of a transcript the harness deleted (#1918): read-only, with
    /// no live stream, queue or approvals. Read from the row each time, so
    /// a chat opened before the list loaded settles when the row arrives.
    var isMirrored: Bool { row?.isMirrored == true }

    /// The page a tool card's Preview opened. The sheet hangs off the
    /// chat, not the card, so a card folding into a run of tool calls
    /// while you look doesn't close it.
    var previewing: PagePreview.Target?

    /// The pages this tool call wrote that the hub can serve, relative to
    /// the session's working folder.
    func pageTargets(for call: ToolCall) -> [PagePreview.Target] {
        let files = PagePreview.files(of: call)
        // Only a page write looks up the row: reading the sessions list
        // from every card would redraw them all on each list refresh.
        guard !files.isEmpty else { return [] }
        return PagePreview.targets(files, cwd: row?.cwd)
    }

    /// The page as it is on disk now, from the hub.
    func loadPreview(_ target: PagePreview.Target) async throws -> PagePreview.Page {
        guard let client = hubClient else { throw HubError.network(CancellationError()) }
        return try await client.preview(key, path: target.path)
    }

    /// The name of the machine the session runs on, as the Sessions list
    /// shows it.
    var machineName: String {
        let id = row?.machineId
        return app?.machines.first { $0.id == id }?.displayName ?? id ?? "that machine"
    }

    /// First open loads history; a resume (back from the background) keeps
    /// what's on screen and replays events after `cursor`.
    func start() async {
        guard let client = hubClient else { return }
        // On screen: its unread dot goes and the hub hears it was read.
        isOpen = true
        app?.chatOpened(key, newest: newestActivity)
        generation += 1
        let gen = generation
        streamTask?.cancel()
        streamTask = nil
        // The / menu's list is fetched now, not on the first /, so the
        // menu opens filled; a list kept from an earlier open shows at
        // once and is still refreshed. A mirrored copy has no prompt box.
        if !isMirrored { commands.warm() }
        if items.isEmpty || loadError != nil || backfillCut { await backfill(client) }
        guard gen == generation else { return }
        // Nothing live to follow for a mirrored copy: no sends queue, no
        // approvals, no stream. The history above is all there is.
        if isMirrored { return }
        // The tray survives the app closing: the hub holds the sends. Read
        // after history, so the list is no older than what's shown; a send
        // that leaves after this has its echo and result in the replay.
        let sends = try? await client.queue(key)
        guard gen == generation else { return }
        if let sends { mergeQueue(sends.sends) }
        // The turn may already be running when the chat opens; the row
        // is current by now, so the clock can start from this open.
        updateWorkingSince()
        // The prompt box's menu may still be open from before stop().
        mentions.resume()
        streamTask = Task { [weak self, cursor] in
            for await event in EventStream.events(client: client, key: self?.key ?? "", last: cursor) {
                guard let self else { return }
                switch event {
                case .connected:
                    self.live = true
                    // A dropped stream may have lost deltas and finals alike.
                    self.clearStreams()
                case .disconnected: self.live = false
                case .unauthorized: self.app?.unpair(from: client); return
                case .gone(let error):
                    self.live = false
                    self.items.append(ChatItem(id: UUID().uuidString, kind: .system(error.message)))
                    return
                case .truncated(let head):
                    self.adoptJournalHead(head)
                    self.dropPending()
                    await self.backfill(client)
                case .event(let env): self.receive(env)
                }
            }
        }
    }

    /// The sessions list just said this chat is mirrored, after the live
    /// stream had started (the chat opened before the list loaded): end
    /// the stream, since a mirrored copy has no events.
    func mirroredLearned() {
        guard isMirrored else { return }
        streamTask?.cancel()
        streamTask = nil
        live = false
        mentions.reset()
        commands.reset()
    }

    func stop() {
        generation += 1
        streamTask?.cancel()
        streamTask = nil
        mentions.reset()
        // The kept list fills the menu at once after the chat reopens.
        commands.reset()
        flushPending()
        clearStreams()
        live = false
        // Only a chat that was on screen was read. A cached one the app
        // drops later, or one from a hub no longer on screen, says nothing.
        if isOpen {
            isOpen = false
            if hubClient != nil { app?.chatClosed(key, newest: newestActivity) }
        }
    }

    /// The agent is mid-turn, or about to start on a send: worth a stop.
    var canInterrupt: Bool { Self.canInterrupt(status: row?.sessionStatus, awaitingReply: awaitingReply) }

    /// Messages still being revealed after the turn ended don't count:
    /// there's nothing left to stop.
    static func canInterrupt(status: SessionStatus?, awaitingReply: Bool) -> Bool {
        status == .active || awaitingReply
    }

    enum ComposerAction { case send, stop, disabled }

    /// What the button beside the prompt box does: send whatever is typed
    /// (with the files above the box), stop the agent when nothing is
    /// typed and it's working, else nothing. `files` is how many chips sit
    /// above the box: with any, there's something to send once text is
    /// typed (the hub needs text), so stop stays out of the way; while
    /// `filesBusy` (one still uploading or failed) Send waits. An edit
    /// saves text only, so the chips don't count while editing, and an
    /// emptied edit has nothing to save.
    static func composerAction(draft: String, agentWorking: Bool, editing: Bool = false,
                               files: Int = 0, filesBusy: Bool = false) -> ComposerAction {
        let typed = !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if editing { return typed ? .send : .disabled }
        if files > 0 { return typed && !filesBusy ? .send : .disabled }
        if typed { return .send }
        return agentWorking ? .stop : .disabled
    }

    /// The agent is mid-turn, a send waits for its reply, or messages
    /// that already arrived are still being revealed.
    var working: Bool {
        row?.sessionStatus == .active || awaitingReply || !pending.isEmpty
    }

    // MARK: Pacing

    /// The pause after showing one live entry, given how many events are
    /// still waiting: a short beat for a few, shorter as more pile up, and
    /// nil (show the rest at once) for a replay after reconnecting.
    static func revealDelay(waiting: Int) -> Duration? {
        guard waiting > 0, waiting <= 24 else { return nil }
        return .milliseconds(min(180, 1200 / waiting))
    }

    func enqueue(_ env: Envelope) {
        pending.append(env)
        guard revealTask == nil else { return }
        revealTask = Task { [weak self] in
            // Events often arrive a few ms apart, so the queue empties
            // between them: keep the gap from the last entry shown. Only
            // for a new entry; a result or status change goes straight in.
            if let self, let first = self.pending.first, Self.addsEntry(first), let last = self.lastReveal,
               let delay = Self.revealDelay(waiting: self.pending.count) {
                let gap = delay - (ContinuousClock.now - last)
                if gap > .zero { await self.pause(gap) }
            }
            while let self, !Task.isCancelled, !self.pending.isEmpty {
                guard let delay = Self.revealDelay(waiting: self.pending.count) else {
                    self.flushPending()
                    break
                }
                let before = self.items.count
                let env = self.pending.removeFirst()
                // A final taking its bubble's place swaps in as it is: the
                // text is already on screen, so no arrival and no pause.
                let replaces = env.seq.map { self.claims[$0] != nil } ?? false
                if replaces { self.apply(env) } else { withAnimation(.easeOut(duration: 0.25)) { self.apply(env) } }
                // Only a new entry earns a pause; a status change or a
                // result landing on its card goes straight through.
                if self.items.count > before, !replaces {
                    self.lastReveal = .now
                    if !self.pending.isEmpty { await self.pause(delay) }
                }
            }
            // A cancelled pass was already replaced, maybe by a newer one.
            if !Task.isCancelled { self?.revealTask = nil }
        }
    }

    /// Whether an event would add an entry to the transcript, rather than
    /// update one or change the session's status.
    static func addsEntry(_ env: Envelope) -> Bool {
        switch env.type {
        case "approval.requested": return true
        case "message.appended": return env.decode(MessageData.self)?.kind != "tool_result"
        default: return false
        }
    }

    /// Shows everything still waiting, unanimated.
    private func flushPending() {
        revealTask?.cancel()
        revealTask = nil
        let all = pending
        pending = []
        for env in all { apply(env) }
    }

    /// A backfill is about to reload history, which holds these.
    private func dropPending() {
        revealTask?.cancel()
        revealTask = nil
        pending = []
    }

    /// Reloads history. Approvals still open and sends not yet echoed
    /// aren't in `/messages`, so they carry over.
    private func backfill(_ client: HubClient) async {
        do {
            let body = try await client.messages(key, limit: Self.pageSize)
            let keep = items.filter { item in
                switch item.kind {
                case .approval(_, _, let state):
                    if case .resolved = state { return false }
                    return true
                case .user(_, let send?):
                    // Delivered ones are in history (or never will be).
                    if case .delivered = send { return false }
                    return true
                default: return false
                }
            }
            items = []
            seen = []
            toolCards = [:]
            orphanResults = [:]
            clearStreams()
            replayingHistory = true
            for env in body.messages { apply(env) }
            replayingHistory = false
            hasEarlier = body.hasMore ?? false
            nextBefore = body.nextBefore ?? body.messages.first?.seq
            for item in keep where !items.contains(where: { $0.id == item.id }) { items.append(item) }
            // Opened from the inbox while an approval waits: the request
            // may predate the stream's replay window, so show it now.
            if let rid = app?.inbox.first(where: { $0.key == key })?.requestId, !rid.isEmpty, !hasApproval(rid) {
                items.append(ChatItem(id: "approval-\(rid)", kind: .approval(requestId: rid, prompt: "Approval requested", state: .pending)))
            }
            loadError = nil
            backfillCut = false
        } catch is CancellationError {
            // The chat closed mid-fetch: nothing failed, but the history
            // is still owed.
            backfillCut = true
        } catch {
            loadError = error.localizedDescription
        }
        // The newest page held nothing to show (only results of older
        // calls): fetch back until something does, rather than "No
        // messages yet" over a session that has them.
        if items.isEmpty, hasEarlier { await loadEarlier() }
        loading = false
    }

    /// Prepends the page of history just older than what's loaded. One
    /// page at a time; returns whether anything was added.
    @discardableResult
    func loadEarlier() async -> Bool {
        guard hasEarlier, !loadingEarlier, nextBefore != nil, let client = hubClient else { return false }
        loadingEarlier = true
        defer { loadingEarlier = false }
        // A page of only tool results whose calls are further back adds
        // nothing to show, and nothing on screen would ask again: keep
        // going until a page shows something or history runs out.
        while hasEarlier, let before = nextBefore {
            let body: MessagesResponse
            do {
                body = try await client.messages(key, limit: Self.pageSize, before: before)
            } catch {
                // Leave hasEarlier set: the next scroll to the top tries again.
                return false
            }
            // A backfill (stream truncated) replaced the history meanwhile.
            guard nextBefore == before else { return false }
            if prepend(body, askedBefore: before) { return true }
        }
        return false
    }

    /// Puts an older page in front of what's loaded; returns whether it
    /// added anything to show. `askedBefore` is the cursor the page was
    /// asked with: a page that claims more history but whose cursor
    /// didn't move back past it would be asked for again forever, so
    /// history counts as ended there (the Mac's twin, #1177).
    func prepend(_ body: MessagesResponse, askedBefore: Int? = nil) -> Bool {
        var older: [ChatItem] = []
        var cards: [String: String] = [:]
        for env in body.messages where env.type == "message.appended" {
            guard let seq = env.seq, !seen.contains(seq), let msg = env.decode(MessageData.self) else { continue }
            seen.insert(seq)
            Self.add(msg, id: "m\(seq)", to: &older, cards: &cards, orphans: &orphanResults)
        }
        // A call on this page may still be running; its live result has
        // to find the card.
        toolCards.merge(cards) { newer, _ in newer }
        items.insert(contentsOf: older, at: 0)
        hasEarlier = body.hasMore ?? false
        nextBefore = body.nextBefore ?? body.messages.first?.seq
        if let askedBefore, hasEarlier, (nextBefore ?? askedBefore) >= askedBefore { hasEarlier = false }
        return !older.isEmpty
    }

    // MARK: Events

    /// A `journal.truncated` names where to resume: the journal's head
    /// after a wipe, or this chat's own cursor when the ring overflowed.
    /// It wins even below the cursor. A lower head means the journal
    /// numbers from the start again, so the seqs already seen belong to
    /// the old journal and must not drop new messages that reuse them,
    /// even if the backfill that follows fails (the next start retries
    /// it, as for any failed load).
    func adoptJournalHead(_ head: Int?) {
        guard let head else { return }
        if head < cursor { seen = [] }
        cursor = head
    }

    func apply(_ env: Envelope) {
        if let seq = env.seq {
            if env.type == "message.appended" {
                guard !seen.contains(seq) else { return }
                seen.insert(seq)
            }
            cursor = max(cursor, seq)
        }
        switch env.type {
        case "message.appended":
            if let msg = env.decode(MessageData.self) { append(msg, seq: env.seq ?? 0) }
            if let seq = env.seq, let replaced = claims.removeValue(forKey: seq) {
                replies.release(replaced)
                repaintStreams()
            }
        case "session.status":
            let status = env.decode(StatusData.self)?.status ?? ""
            app?.patch(key, status: status)
            updateWorkingSince()
            // The turn ended (or stopped to ask) without a message to show.
            if !["", "active"].contains(status) { awaitingReply = false }
        case "session.ownership":
            if let event = env.decode(OwnershipData.self) { app?.patch(key, ownershipEvent: event) }
            attachQueued = false
        case "session.meta":
            // A pin or rename from any device.
            if let meta = env.decode(SessionMeta.self) { app?.patch(key, meta: meta) }
        case "approval.requested":
            guard let req = env.decode(ApprovalRequested.self), let rid = req.requestId else { return }
            let rows = ApprovalMenu.rows(req.options)
            if !rows.isEmpty { approvalOptions[rid] = rows }
            if let i = items.firstIndex(where: { $0.id == "approval-\(rid)" }) {
                // The inbox placeholder learns its real prompt.
                guard case .approval(_, _, let state) = items[i].kind else { return }
                let card = ChatItem(id: "approval-\(rid)", kind: .approval(
                    requestId: rid, prompt: req.prompt ?? "Approval requested", state: state))
                if case .resolved = state {
                    items[i] = card
                } else {
                    // Asked again under the same id (a satellite resends a
                    // refused batch whole): the open card moves to the
                    // newest ask instead of waiting mid-transcript (the
                    // Mac's SessionChatModel and the web chat do the same).
                    items.remove(at: i)
                    items.append(card)
                }
                return
            }
            items.append(ChatItem(id: "approval-\(rid)", kind: .approval(requestId: rid, prompt: req.prompt ?? "Approval requested", state: .pending)))
        case "approval.resolved":
            let res = env.decode(ApprovalResolved.self)
            resolveApprovals(requestId: res?.requestId, decision: res?.decision)
        case "command.result":
            if let result = env.decode(CommandResult.self) { settle(result) }
        default:
            break
        }
    }

    /// One live event off the stream. Deltas skip the reveal queue (its
    /// pacing would fight the stream's own); a final message is matched
    /// to its bubble now and shown in its turn.
    func receive(_ env: Envelope) {
        if env.type == "message.delta" {
            guard let delta = env.decode(ReplyDelta.self), replies.apply(delta), repaintTask == nil else { return }
            let sleep = streamSleep
            repaintTask = Task { [weak self] in
                await sleep(Self.repaintEvery)
                guard let self, !Task.isCancelled else { return }
                self.repaintTask = nil
                self.repaintStreams()
            }
            return
        }
        if env.type == "message.appended", let seq = env.seq, !seen.contains(seq), claims[seq] == nil,
           let id = replies.claim(kind: env.decode(MessageData.self)?.kind) {
            claims[seq] = id
        }
        // The idle sweep takes the bubbles open as the status arrives: by
        // the time the queue shows it, the next turn's may have started.
        if env.type == "session.status", !["", "active"].contains(env.decode(StatusData.self)?.status ?? "") {
            sweepStreams()
        }
        enqueue(env)
    }

    /// Draws the bubbles as they are now.
    func repaintStreams() {
        repaintTask?.cancel()
        repaintTask = nil
        let shown = replies.shown.map { ChatItem(id: "live-\($0.id)", kind: .assistant($0.text)) }
        if shown != streaming { streaming = shown }
    }

    /// The session went idle: bubbles open now whose final message never
    /// came (a dropped stream) go after `idleGrace`.
    private func sweepStreams() {
        let ids = replies.openIds
        guard !ids.isEmpty else { return }
        let gen = generation, sleep = streamSleep
        Task { [weak self] in
            await sleep(Self.idleGrace)
            guard let self, gen == self.generation else { return }
            if self.replies.sweep(ids) { self.repaintStreams() }
        }
    }

    /// Reconnect, history reload or close: every bubble goes, and finals
    /// still queued show as plain messages.
    private func clearStreams() {
        replies.clear()
        claims = [:]
        repaintStreams()
    }

    private func append(_ msg: MessageData, seq: Int) {
        if msg.kind == "user", let text = msg.text {
            // The hub echoes a queued send back as a user message: drop the
            // optimistic bubble it replaces (attachments prefix "@path ").
            if let i = items.firstIndex(where: {
                if case .user(let t, let s) = $0.kind, s != nil, !Self.isFailed(s) { return Self.echoes(text, t) }
                return false
            }) {
                // Still the newest entry: settle the bubble where it is, so
                // it doesn't leave and come back in.
                if i == items.count - 1 {
                    items[i].kind = .user(text: text, send: nil)
                    return
                }
                items.remove(at: i)
            } else if !replayingHistory, let j = departed.firstIndex(where: { Self.echoes(text, $0.text) }) {
                settledSends.insert(departed.remove(at: j).clientId)
            } else if !replayingHistory, let j = queued.firstIndex(where: { Self.echoes(text, $0.text) }) {
                // A queued send went out: it leaves the tray and the echo
                // lands in the transcript where it really is.
                settledSends.insert(queued[j].clientId)
                queued.remove(at: j)
            }
        } else if msg.kind != "tool_result" {
            awaitingReply = false
        }
        // History a backfill replays is never a reply to the switch.
        if !replayingHistory, ["assistant", "thinking", "tool_use"].contains(msg.kind ?? "assistant") {
            modelSwitch?.noteReply(at: Date(), messageTs: msg.ts)
        }
        if let ts = msg.ts { newestActivity = max(newestActivity ?? ts, ts) }
        // Seen live on screen: the read cursor follows, debounced.
        if isOpen, !replayingHistory { app?.chatGotMessage(key) }
        Self.add(msg, id: "m\(seq)", to: &items, cards: &toolCards, orphans: &orphanResults)
    }

    /// Adds one message to the end of `list`. A tool result lands on its
    /// call's card; one whose call is on an older page not loaded yet
    /// waits in `orphans` until that page brings the call.
    static func add(_ msg: MessageData, id: String, to list: inout [ChatItem],
                    cards: inout [String: String], orphans: inout [String: String]) {
        let text = msg.text ?? ""
        switch msg.kind ?? "assistant" {
        case "user":
            list.append(ChatItem(id: id, kind: .user(text: text, send: nil)))
        case "thinking":
            list.append(ChatItem(id: id, kind: .thinking(text, seconds: msg.durationS)))
        case "tool_use":
            guard let tool = msg.tool else { return }
            var result: String?
            if let callId = tool.callId {
                cards[callId] = id
                result = orphans.removeValue(forKey: callId)
            }
            list.append(ChatItem(id: id, kind: .tool(tool, result: result)))
        case "tool_result":
            guard let ref = msg.resultRef else { return }
            let result = String(text.prefix(8000))
            if let cardId = cards[ref], let i = list.firstIndex(where: { $0.id == cardId }),
               case .tool(let call, _) = list[i].kind {
                list[i].kind = .tool(call, result: result)
            } else {
                orphans[ref] = result
            }
        case "compaction":
            list.append(ChatItem(id: id, kind: .compaction))
        case "system":
            list.append(ChatItem(id: id, kind: .system(text)))
        case "error":
            list.append(ChatItem(id: id, kind: .error(text)))
        default:
            guard !text.isEmpty else { return }
            list.append(ChatItem(id: id, kind: .assistant(text)))
        }
    }

    /// The transcript's user message `echo` is the send `sent` coming back:
    /// the same text, or with the hub's attachment prefix, one "@path "
    /// per attachment (`TmuxInjector.send`). Nothing else, so another
    /// device's "please run tests" isn't taken for a waiting "run tests".
    static func echoes(_ echo: String, _ sent: String) -> Bool {
        if echo == sent { return true }
        // Staged files the hub prefixed, read whole: their home folder can
        // hold a space, which the word split below would break on.
        let staged = AttachmentRules.splitEcho(echo)
        if !staged.names.isEmpty, staged.text == sent { return true }
        guard echo.hasSuffix(" " + sent) else { return false }
        let prefix = echo.dropLast(sent.count)
        return prefix.split(separator: " ", omittingEmptySubsequences: false).dropLast().allSatisfy {
            $0.count > 1 && $0.first == "@"
        } && prefix.hasPrefix("@")
    }

    private func hasApproval(_ rid: String) -> Bool {
        items.contains { if case .approval(rid, _, _) = $0.kind { true } else { false } }
    }

    private static func isFailed(_ s: SendState?) -> Bool {
        if case .failed = s { return true }
        return false
    }

    private func settle(_ r: CommandResult) {
        if r.op == "message", let cid = r.clientId, let i = queued.firstIndex(where: { $0.clientId == cid }) {
            let entry = queued.remove(at: i)
            showSettled(entry, r)
            return
        }
        if r.op == "message", let cid = r.clientId, let i = departed.firstIndex(where: { $0.clientId == cid }) {
            showSettled(departed.remove(at: i), r)
            return
        }
        if r.op == "message", r.cancelled == true {
            // Removed from another device, or after this one stopped
            // showing it: never a failure.
            if let cid = r.clientId {
                settledSends.insert(cid)
                items.removeAll { $0.id == "send-\(cid)" }
            }
            return
        }
        settleShown(r)
    }

    /// A queued send's result: into the transcript as a settled send,
    /// or, when it was removed, gone without a failure.
    private func showSettled(_ entry: QueuedSend, _ r: CommandResult) {
        let cid = entry.clientId
        settledSends.insert(cid)
        // Removed before it went out: gone, and not a failure.
        if r.cancelled == true { return }
        // A send restored from the hub's list carries its refs only here:
        // keep them for the bubble and a retry.
        if !entry.attachments.isEmpty, sentFiles[cid] == nil { sentFiles[cid] = entry.attachments }
        // It went out (or failed): show it in the transcript as any send,
        // settled by the existing rules.
        let next = SendState.settled(r, current: .sending, agentActive: row?.sessionStatus == .active)
        items.append(ChatItem(id: "send-\(cid)", kind: .user(text: entry.text, send: next ?? .delivered(nil))))
    }

    /// A result for something already in the transcript: a send's bubble,
    /// an attach, an approval, or another command.
    private func settleShown(_ r: CommandResult) {
        if r.op == "message", let cid = r.clientId, let i = items.firstIndex(where: { $0.id == "send-\(cid)" }),
           case .user(let text, let send?) = items[i].kind {
            // An echo already settled it if `send` is nil.
            guard let next = SendState.settled(r, current: send, agentActive: row?.sessionStatus == .active) else { return }
            if case .failed = next { awaitingReply = false }
            items[i].kind = .user(text: text, send: next)
            return
        }
        if r.op == "attach" || (r.op == "control" && attachQueued) {
            if r.ok == false {
                attachQueued = false
                attachError = r.note ?? "Attach failed"
            }
            return
        }
        if r.op == "detach" {
            // A detach from the Sessions list that waited for the turn.
            app?.detachSettled(key)
            if r.cancelled == true || r.ok != false { return }
            items.append(ChatItem(id: UUID().uuidString, kind: .system("Didn't detach: \(r.note ?? "unknown error")")))
            return
        }
        if r.op == "approval", let rid = r.requestId {
            updateApproval(rid) { r.ok == false ? .failed(r.note ?? "The machine rejected it") : .resolved(nil) }
            return
        }
        if r.ok == false {
            // A satellite answers a model switch here, matched by the
            // client id it echoes; other controls' failures leave it.
            if modelSwitch?.failed(by: r) == true { modelSwitch = nil }
            items.append(ChatItem(id: UUID().uuidString, kind: .system("\(r.op ?? "Command") failed: \(r.note ?? "unknown error")")))
        }
    }

    private func resolveApprovals(requestId: String?, decision: String?) {
        for i in items.indices {
            guard case .approval(let rid, let prompt, let old) = items[i].kind else { continue }
            if requestId == nil || requestId == "" || requestId == rid {
                // The tailer's later "the prompt closed" carries no
                // decision: the answer already recorded stays named.
                var said = decision
                if said == nil, case .resolved(let earlier) = old { said = earlier }
                items[i].kind = .approval(requestId: rid, prompt: prompt, state: .resolved(said))
            }
        }
    }

    private func updateApproval(_ rid: String, _ state: () -> ApprovalState) {
        guard let i = items.firstIndex(where: { if case .approval(rid, _, _) = $0.kind { true } else { false } }),
              case .approval(_, let prompt, let old) = items[i].kind else { return }
        if case .resolved = old { return }
        items[i].kind = .approval(requestId: rid, prompt: prompt, state: state())
    }

    // MARK: Actions

    func send() async {
        guard !isMirrored else { return }
        if editingId != nil { return await saveEdit() }
        guard !attachments.blocksSend else { return }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        // The hub refuses a message with no text, files or not.
        guard !text.isEmpty else { return }
        let files = attachments.refs
        draft = ""
        attachments.clear()
        editNote = nil
        trayNote = nil
        await send(text, files: files)
    }

    /// Where a send goes and in which mode.
    struct SendRoute: Equatable {
        let toTray: Bool
        let mode: String
    }

    /// Queue mode: it goes out at once when the agent is idle, else the hub
    /// holds it and the tray shows it (Send now on a tray row is the steer).
    /// A satellite's session can't be held, and a queue-mode send to it
    /// mid-turn is refused, so mid-turn it steers straight in, no tray.
    static func route(agentActive: Bool, holdsSends: Bool) -> SendRoute {
        guard agentActive else { return SendRoute(toTray: false, mode: "queue") }
        return holdsSends ? SendRoute(toTray: true, mode: "queue") : SendRoute(toTray: false, mode: "steer")
    }

    private func send(_ text: String, files: [String] = []) async {
        guard let client = hubClient else { return }
        let cid = UUID().uuidString
        let route = Self.route(agentActive: row?.sessionStatus == .active, holdsSends: row?.holdsQueuedSends ?? true)
        stage(text, clientId: cid, toTray: route.toTray, files: files)
        do {
            let accepted = try await client.sendMessage(key, text: text, mode: route.mode, clientId: cid,
                                                        attachments: files)
            posted(cid, waiting: accepted.waiting == true)
        } catch {
            postFailed(cid, error)
        }
    }

    /// Shows a send before the hub answers: in the tray while the agent
    /// works, else as a bubble in the transcript.
    func stage(_ text: String, clientId cid: String, toTray: Bool, files: [String] = []) {
        if !files.isEmpty { sentFiles[cid] = files }
        withAnimation(.easeOut(duration: 0.25)) {
            if toTray {
                queued.append(QueuedSend(clientId: cid, text: text, phase: .posting, attachments: files))
            } else {
                items.append(ChatItem(id: "send-\(cid)", kind: .user(text: text, send: .sending)))
            }
        }
        if !toTray { awaitingReply = true }
    }

    /// The hub took the send; `waiting` says whether it holds it for idle.
    /// Where it shows follows the hub, not the status the phone last saw.
    func posted(_ cid: String, waiting: Bool) {
        if let i = queued.firstIndex(where: { $0.clientId == cid }) {
            guard queued[i].phase == .posting else { return }
            if waiting {
                queued[i].phase = .waiting
            } else {
                // The agent was idle after all: it goes out now.
                let entry = queued.remove(at: i)
                withAnimation(.easeOut(duration: 0.25)) {
                    items.append(ChatItem(id: "send-\(cid)", kind: .user(text: entry.text, send: .sending)))
                }
                awaitingReply = true
            }
        } else if waiting, !settledSends.contains(cid), let i = items.firstIndex(where: { $0.id == "send-\(cid)" }),
                  case .user(let text, .sending?) = items[i].kind {
            // The agent was busy after all: it waits in the tray.
            withAnimation(.easeOut(duration: 0.25)) {
                items.remove(at: i)
                queued.append(QueuedSend(clientId: cid, text: text, phase: .waiting, attachments: sentFiles[cid] ?? []))
            }
            awaitingReply = false
        }
    }

    /// The POST failed: a failed bubble in the transcript, to retry.
    func postFailed(_ cid: String, _ error: Error) {
        let note = (error as? HubError)?.message ?? "Send failed"
        if let i = queued.firstIndex(where: { $0.clientId == cid }) {
            let entry = queued.remove(at: i)
            settledSends.insert(cid)
            withAnimation(.easeOut(duration: 0.25)) {
                items.append(ChatItem(id: "send-\(cid)", kind: .user(text: entry.text, send: .failed(note))))
            }
        } else if let i = pendingSend("send-\(cid)"), case .user(let text, _) = items[i].kind {
            awaitingReply = false
            items[i].kind = .user(text: text, send: .failed(note))
        }
    }

    // MARK: Queued tray

    /// What the tray shows: everything queued but what's being removed.
    var tray: [QueuedSend] { queued.filter { $0.phase != .removing } }

    /// Takes the hub's list of waiting sends after (re)opening the chat.
    func mergeQueue(_ sends: [WaitingSend]) {
        var known = settledSends
        for item in items where item.id.hasPrefix("send-") { known.insert(String(item.id.dropFirst(5))) }
        let merged = Self.mergeQueue(local: queued, hub: sends, settled: known)
        departed += merged.departed.filter { d in !departed.contains { $0.clientId == d.clientId } }
        guard merged.queued != queued else { return }
        withAnimation(.easeOut(duration: 0.25)) { queued = merged.queued }
    }

    /// The hub's list wins: what it holds shows in its order (keeping what
    /// this phone knows about each), and what it no longer holds leaves the
    /// tray for `departed`, except a send whose POST hasn't been answered
    /// yet. `settled` are sends already shown elsewhere or gone (their
    /// echo or result seen), which a list can't bring back.
    static func mergeQueue(local: [QueuedSend], hub: [WaitingSend], settled: Set<String>)
        -> (queued: [QueuedSend], departed: [QueuedSend]) {
        var out: [QueuedSend] = []
        for send in hub where !settled.contains(send.clientId) && !out.contains(where: { $0.clientId == send.clientId }) {
            if let mine = local.first(where: { $0.clientId == send.clientId }) {
                var kept = mine
                if kept.phase == .posting {
                    kept.phase = .waiting
                    kept.note = nil
                }
                out.append(kept)
            } else {
                out.append(QueuedSend(clientId: send.clientId, text: send.text ?? "", phase: .waiting,
                                      attachments: send.attachmentRefs))
            }
        }
        var departed: [QueuedSend] = []
        for mine in local where !out.contains(where: { $0.clientId == mine.clientId }) {
            if mine.phase == .posting {
                out.append(mine)
            } else if !settled.contains(mine.clientId) {
                departed.append(mine)
            }
        }
        return (out, departed)
    }

    /// Send now: the hub stops holding it and writes it into the running
    /// turn. It shows "Sending now" until its result or echo arrives.
    func sendNow(_ cid: String) async {
        guard !isMirrored else { return }
        guard let client = hubClient, markSendingNow(cid) else { return }
        trayNote = nil
        if editingId == cid { cancelEdit() }
        do {
            try await client.queueAction(key, clientId: cid, op: "steer")
        } catch {
            steerFailed(cid, error)
        }
    }

    func markSendingNow(_ cid: String) -> Bool {
        guard let i = queued.firstIndex(where: { $0.clientId == cid }), queued[i].phase == .waiting else { return false }
        queued[i].phase = .sendingNow
        queued[i].note = nil
        return true
    }

    func steerFailed(_ cid: String, _ error: Error) {
        guard let i = queued.firstIndex(where: { $0.clientId == cid }), queued[i].phase == .sendingNow else { return }
        queued[i].phase = (error as? HubError)?.code == "not_waiting" ? .left : .waiting
        queued[i].note = Self.queueNote(error)
    }

    /// Remove: gone from the tray at once, back with a note if the hub
    /// refuses. Its `command.result` says `cancelled`, never a failure.
    func remove(_ cid: String) async {
        guard let i = queued.firstIndex(where: { $0.clientId == cid }) else { return }
        switch queued[i].phase {
        case .posting, .sendingNow, .removing:
            return
        case .left:
            // The hub no longer holds it: nothing to ask.
            dropQueued(cid)
            return
        case .waiting:
            break
        }
        guard let client = hubClient else { return }
        trayNote = nil
        if editingId == cid { cancelEdit() }
        markRemoving(cid)
        do {
            try await client.queueAction(key, clientId: cid, op: "cancel")
            dropQueued(cid)
        } catch {
            removeFailed(cid, error)
        }
    }

    func markRemoving(_ cid: String) {
        guard let i = queued.firstIndex(where: { $0.clientId == cid }) else { return }
        withAnimation(.easeOut(duration: 0.2)) {
            queued[i].phase = .removing
            queued[i].note = nil
        }
    }

    func removeFailed(_ cid: String, _ error: Error) {
        guard let i = queued.firstIndex(where: { $0.clientId == cid }), queued[i].phase == .removing else { return }
        let code = (error as? HubError)?.code
        // The hub never held it, or forgot it: there's nothing to remove.
        if code == "unknown_send" { return dropQueued(cid) }
        withAnimation(.easeOut(duration: 0.2)) {
            queued[i].phase = code == "not_waiting" ? .left : .waiting
            queued[i].note = Self.queueNote(error)
        }
    }

    private func dropQueued(_ cid: String) {
        settledSends.insert(cid)
        withAnimation(.easeOut(duration: 0.2)) { queued.removeAll { $0.clientId == cid } }
    }

    /// The short note a tray row shows when the hub refuses Send now or
    /// Remove.
    static func queueNote(_ error: Error) -> String {
        guard let e = error as? HubError else { return "Something went wrong" }
        switch e.code {
        case "not_waiting":
            let why = (e.reason ?? e.message).lowercased()
            if why.contains("removed") { return "Already removed" }
            if why.contains("failed") { return "Already failed" }
            return "Already sent"
        case "not_attached": return "Attach to send now"
        case "queue_unsupported": return "Not available on this machine"
        case "backlog_full": return "Too many waiting. Try again shortly"
        case "unknown_send": return "The hub lost this message"
        case "network": return "Can't reach the hub"
        default: return e.message
        }
    }

    /// Remove all: every row the hub still holds, each as its own remove.
    func removeAllQueued() {
        for send in tray { Task { await remove(send.clientId) } }
    }

    // MARK: Editing a queued send

    /// Edit: the message's text goes into the prompt box, and the send
    /// button saves it back. Only a send the hub still holds can be edited.
    @discardableResult
    func beginEdit(_ cid: String) -> Bool {
        guard let send = queued.first(where: { $0.clientId == cid }), send.phase == .waiting else { return false }
        if editingId == nil { draftBeforeEdit = draft }
        editingId = cid
        draft = send.text
        editNote = nil
        return true
    }

    func cancelEdit() {
        guard editingId != nil else { return }
        editingId = nil
        draft = draftBeforeEdit
        draftBeforeEdit = ""
        editNote = nil
    }

    /// What a refused `edit` means for the edit.
    enum EditOutcome: Equatable {
        /// The hub doesn't know the op (an older hub): remove it and queue
        /// the new text instead, at the end.
        case requeue
        /// It went out before the edit landed.
        case wentOut
        /// Keep editing, with this note.
        case failed(String)
    }

    static func editOutcome(_ error: Error) -> EditOutcome {
        guard let e = error as? HubError else { return .failed(queueNote(error)) }
        if e.code == "not_waiting" { return .wentOut }
        if e.status == 400 || e.status == 422 { return .requeue }
        return .failed(queueNote(e))
    }

    func saveEdit() async {
        guard let cid = editingId, !savingEdit else { return }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        guard queued.contains(where: { $0.clientId == cid && $0.phase == .waiting }) else { return editWentOut(cid, text: text) }
        guard let client = hubClient else { return }
        savingEdit = true
        defer { savingEdit = false }
        do {
            try await client.queueAction(key, clientId: cid, op: "edit", text: text)
            editSaved(cid, text: text)
        } catch {
            editRefused(cid, text: text, error)
            if Self.editOutcome(error) == .requeue { await requeueEdit(cid, text: text) }
        }
    }

    /// The hub took the edit: the row keeps its place with the new text.
    func editSaved(_ cid: String, text: String) {
        if let i = queued.firstIndex(where: { $0.clientId == cid }) {
            queued[i].text = text
            queued[i].note = nil
        }
        if editingId == cid { cancelEdit() }
    }

    /// The hub refused the edit. A requeue is left to the caller, which
    /// has the network; the rest is settled here.
    func editRefused(_ cid: String, text: String, _ error: Error) {
        switch Self.editOutcome(error) {
        case .requeue: break
        case .wentOut: editWentOut(cid, text: text)
        case .failed(let note): editNote = note
        }
    }

    /// Too late to edit: the new text stays in the prompt box to send as a
    /// new message.
    func editWentOut(_ cid: String, text: String) {
        if let i = queued.firstIndex(where: { $0.clientId == cid }), queued[i].phase == .waiting {
            queued[i].phase = .left
            queued[i].note = "Already sent"
        }
        editingId = nil
        draftBeforeEdit = ""
        draft = text
        editNote = "That message already went out"
    }

    /// An older hub can't edit in place: remove the message, then queue the
    /// new text, which lands at the end.
    private func requeueEdit(_ cid: String, text: String) async {
        guard let client = hubClient else { return }
        // The replacement carries the same files: still staged on the hub.
        let files = queued.first { $0.clientId == cid }?.attachments ?? sentFiles[cid] ?? []
        do {
            try await client.queueAction(key, clientId: cid, op: "cancel")
        } catch {
            editRefused(cid, text: text, error)
            if Self.editOutcome(error) == .requeue { editNote = Self.queueNote(error) }
            return
        }
        dropQueued(cid)
        requeued(cid)
        await send(text, files: files)
    }

    /// The old message is gone; its replacement goes to the end.
    func requeued(_ cid: String) {
        if editingId == cid { cancelEdit() }
        trayNote = Self.requeuedNote
    }

    static let requeuedNote = "Edited. It moved to the end of the queue"

    /// The bubble of a send the hub hasn't echoed yet. An echoed one keeps
    /// its id but is delivered, so a late reply to the POST leaves it alone.
    private func pendingSend(_ id: String) -> Int? {
        items.firstIndex { if case .user(_, let s?) = $0.kind, s.inFlight { $0.id == id } else { false } }
    }

    /// Sends a failed bubble again, with the files it carried: they stay
    /// staged on the hub, so nothing uploads twice.
    func retry(_ item: ChatItem) async {
        guard case .user(let text, .failed) = item.kind else { return }
        items.removeAll { $0.id == item.id }
        let files = item.id.hasPrefix("send-") ? sentFiles.removeValue(forKey: String(item.id.dropFirst(5))) : nil
        await send(text, files: files ?? [])
    }

    func decide(_ requestId: String, decision: String) async {
        guard let client = hubClient else { return }
        updateApproval(requestId) { .submitting }
        do {
            let queued = try await client.approve(key, requestId: requestId, decision: decision)
            updateApproval(requestId) { queued ? .queued : .resolved(decision) }
        } catch {
            updateApproval(requestId) { .failed((error as? HubError)?.message ?? "Approval failed") }
        }
    }

    // MARK: Model

    /// The switch sent from the Model menu, until the transcript shows
    /// it (`ModelSwitch` says when that is).
    private(set) var modelSwitch: ModelSwitch?

    /// The model the header shows as asked for, while the transcript
    /// hasn't caught up; nil once it has.
    var pendingModel: String? {
        guard let modelSwitch, row?.canSwitchModel == true,
              !modelSwitch.settled(currentModel: row?.model, listRefreshedAt: app?.refreshedAt) else { return nil }
        return modelSwitch.requested
    }

    /// The Model menu's entries, newest first.
    var modelChoices: [String] {
        guard let row else { return [] }
        return ModelSwitchOptions.choices(for: row, on: app?.machines.first { $0.id == row.machineId })
    }

    /// Types `/model <value>` into the session's pane through the hub.
    /// Picking the model it already runs (with nothing pending) sends
    /// nothing; an invalid id never leaves the phone.
    func switchModel(to text: String) async {
        guard !isMirrored, let client = hubClient, let value = ModelSwitchOptions.validValue(text) else { return }
        if pendingModel == nil, value == row?.model { return }
        // Pending before the POST: a satellite's failed result can reach
        // the event stream before the hub's 202 does, and must find the
        // switch it cancels rather than be overwritten by it.
        let pending = ModelSwitch(requested: value, from: row?.model, sentAt: Date())
        modelSwitch = pending
        do {
            try await client.control(key, op: "model", value: value, clientId: pending.clientId)
        } catch {
            if modelSwitch?.clientId == pending.clientId { modelSwitch = nil }
            items.append(ChatItem(id: UUID().uuidString, kind: .system((error as? HubError)?.message ?? "Couldn't switch the model")))
        }
    }

    func interrupt() async {
        guard !isMirrored, let client = hubClient else { return }
        do { try await client.control(key, op: "interrupt") } catch {
            items.append(ChatItem(id: UUID().uuidString, kind: .system((error as? HubError)?.message ?? "Interrupt failed")))
        }
    }

    private(set) var attaching = false
    /// A satellite took the attach (202) but hasn't confirmed it; the
    /// composer waits for its `session.ownership` event.
    private(set) var attachQueued = false
    private(set) var attachError: String?

    /// Through `AppModel.attach`, as the list's swipe does: an answer
    /// from a hub the phone has since left is dropped, and the attach
    /// cancels a `when_idle` detach still waiting on the turn.
    func attach() async {
        // hubClient is nil once the chat's hub is no longer on screen.
        guard let app, hubClient != nil, !isMirrored else { return }
        attaching = true
        attachError = nil
        defer { attaching = false }
        do {
            if try await app.attach(key) { attachQueued = true }
        } catch {
            attachError = (error as? HubError)?.message ?? "Attach failed"
        }
    }
}
