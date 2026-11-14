import Foundation
import Observation

/// One entry of a chat transcript, already folded for display: a tool's
/// output sits on its call, an approval carries its outcome.
public struct HubChatItem: Identifiable, Equatable, Sendable {
    public enum Kind: Equatable, Sendable {
        /// `send` is set on a message typed here until the transcript
        /// echoes it (and stays when it failed, or went out degraded).
        case user(String, send: HubSendState?)
        case assistant(String)
        /// `seconds` is how long the model thought, when the hub knew.
        case thinking(String, seconds: Double?)
        case tool(HubToolCall, result: String?)
        /// `options` is the pane's numbered menu when the ask carried it;
        /// `picked` is the decision the card lights as chosen (the row
        /// answered here, or the resolved one once it names a row).
        case approval(requestId: String, prompt: String, options: [HubApprovalOption],
                      picked: String?, state: HubApprovalState)
        case compaction
        case system(String)
        case error(String)
    }

    public let id: String
    public var kind: Kind

    public init(id: String, kind: Kind) {
        self.id = id
        self.kind = kind
    }
}

/// Where an approval the agent asked for stands.
public enum HubApprovalState: Equatable, Sendable {
    case open
    /// An answer from here is on its way to the hub.
    case submitting
    /// The hub passed the answer to a satellite; its result settles it.
    case queued
    case failed(String)
    /// Answered, from here or anywhere else: `allow`, `always`, `deny`,
    /// or nil when the hub didn't say.
    case resolved(String?)

    /// Still waiting for an answer from someone.
    public var answerable: Bool {
        switch self {
        case .open, .failed: true
        case .submitting, .queued, .resolved: false
        }
    }
}

/// Where a message typed in the chat stands.
public enum HubSendState: Equatable, Sendable {
    case sending
    /// The hub took it; `waiting` while it holds it for the turn to end.
    case accepted(waiting: Bool)
    /// It went out, but not the way it was asked (a steer queued because
    /// the harness can't take input mid-turn): the note stays.
    case degraded(String)
    case failed(String)
    /// It went out; the transcript's echo, if any, replaces it. A slash
    /// command never echoes, so it may stay like this.
    case delivered

    /// Still on its way: its echo or result may settle it.
    public var inFlight: Bool {
        switch self {
        case .sending, .accepted: true
        case .degraded, .failed, .delivered: false
        }
    }

    /// The line under the bubble.
    public var note: String {
        switch self {
        case .sending: "Sending…"
        case .accepted(let waiting): waiting ? "Queued until the agent is idle" : "Sent"
        case .degraded(let note), .failed(let note): note
        case .delivered: ""
        }
    }
}

/// The Sessions page's chat: the newest page of history from the hub,
/// older pages on request, then the session's live events. A port of the
/// web app's `static/pages/chat.js` and the iPhone app's `ChatModel`.
@MainActor
@Observable
public final class SessionChatModel {
    public enum Connection: Equatable, Sendable { case connecting, live, reconnecting }

    public let key: String
    public private(set) var items: [HubChatItem] = []
    /// The hub's row for the session: status and ownership, kept current
    /// by the stream.
    public private(set) var row: HubSessionRow?
    public private(set) var connection: Connection = .connecting
    public private(set) var loading = true
    public private(set) var loadError: String?
    /// The hub holds messages older than the oldest one shown.
    public private(set) var hasEarlier = false
    public private(set) var loadingEarlier = false
    /// The hub doesn't list the session (an older one, or one it never
    /// read): the page opens its details instead.
    public private(set) var missing = false
    /// A send went out and nothing from the agent has come back yet.
    public var awaitingReply: Bool { !awaiting.isEmpty }
    /// A Stop is on its way to the hub.
    public private(set) var stopping = false
    public private(set) var attaching = false
    /// The prompt box's `/` menu: the commands this session's harness
    /// will run, fetched once while the menu is open.
    public let commands = SlashCommandMenu()
    /// The prompt box's `@` menu: files under the session's folder,
    /// searched as the `@word` is typed.
    public let mentions = MentionSearch()
    /// Queue-mode sends the hub holds until the turn ends, above the
    /// prompt box with Edit, Remove and Send now; it holds the box's
    /// text too, which Edit swaps a queued message into.
    public let tray = QueuedTray()

    /// Messages per `/messages` page.
    public static let pageSize = 100

    private let hub: LocalHub
    private var seen = Set<Int>()
    private var cursor = 0
    /// Tool call id to the id of its card.
    private var toolCards: [String: String] = [:]
    /// Tool results whose call is on a page not loaded yet, by call id.
    private var orphanResults: [String: Orphan] = [:]
    /// Stream events up to this seq are history already on screen, replayed
    /// only to find the ask of an approval the hub still waits on.
    private var replayFloor = 0
    /// The seq to page back from: the oldest message loaded.
    private var nextBefore: Int?
    private var streamTask: Task<Void, Never>?
    private var modelTask: Task<Void, Never>?
    /// Waits between the rereads of the model after a turn ends, past the
    /// hub's 10 s catalog refresh. Tests pass none, or a sleeper that
    /// returns at once.
    var modelRetryDelays: [Duration] = [.seconds(6), .seconds(6)]
    var modelSleep: @Sendable (Duration) async -> Void = { try? await Task.sleep(for: $0) }
    /// How long a read waits after a live message, so a burst of them
    /// posts one `POST .../read` (`UnreadDots.swift`).
    static let readDebounce: Duration = .seconds(3)
    /// The wait behind `readDebounce`; tests pass one they release.
    var readSleep: @Sendable (Duration) async -> Void = { try? await Task.sleep(for: $0) }
    /// The read a live message scheduled, until it posts. Set only
    /// while the chat is open.
    private(set) var readTask: Task<Void, Never>?
    /// The read `start()` sent once the hub listed the row; never
    /// awaited by the chat, so a slow write can't hold up history.
    private(set) var readOnOpen: Task<Void, Never>?
    /// The read `stop()` sent, covering what the chat showed; for tests
    /// to await.
    private(set) var readFlush: Task<Void, Never>?
    /// The chat counts as open for read state: between the first
    /// `start()` and `stop()`.
    private var readOpen = false
    /// The newest activity this chat has shown (epoch seconds): the
    /// row's `last_active_at` and every message's `ts`. Its close marks
    /// the row read up to here.
    private var newestActivity: Double?
    /// Bumped by every start() and stop(), so a start() still fetching
    /// history doesn't open a stream after the chat was closed.
    private var generation = 0
    /// History is being loaded: its user messages are old, never the
    /// echo of a send.
    private var replaying = false
    /// Sends still waiting for the agent's first reply, by client id.
    private var awaiting = Set<String>()
    /// The entry each send became once the transcript echoed it, by client
    /// id: its result can land after the echo.
    private var echoed: [String: String] = [:]
    /// When each send was typed, so a reload takes only a history message
    /// from after it as its echo.
    private var sentAt: [String: Double] = [:]
    /// The user messages a history load added: echoes of sends it carries.
    private var loadedUsers: [(id: String, text: String, ts: Double?)] = []
    /// Each approval's state once answered from here, and the decision
    /// sent: a satellite's result doesn't repeat it.
    private var answers: [String: HubApprovalState] = [:]
    private var decisions: [String: String] = [:]
    /// Approvals whose answer from here is still outstanding: set as the
    /// `decide` POST leaves (a satellite's `command.result` can beat the
    /// reply), cleared when a non-queued reply or that result closes it.
    /// The web app's `dataset.queued`: a result settles only a request in
    /// this set, and a rejection after the card resolved re-opens it.
    private var pendingAnswers = Set<String>()

    struct Orphan: Equatable {
        var seq: Int
        var text: String
    }

    // MARK: Live reply text

    /// Replies still being written (`message.delta`, #1444), drawn after
    /// `items` until their final message takes their place. Rendered with
    /// the same Markdown as the final message: the text only grows, so a
    /// half-written block (an unclosed fence, a `**` not yet closed) is
    /// the only flicker, and switching to Markdown at the end would
    /// reflow the whole reply at once instead.
    public private(set) var streaming: [HubChatItem] = []
    @ObservationIgnored private var replies = ReplyStreams()
    /// The pending repaint while text streams in: one per `repaintEvery`.
    @ObservationIgnored private(set) var repaintTask: Task<Void, Never>?
    static let repaintEvery: Duration = .milliseconds(80)
    /// How long a bubble outlives its session going idle: the final
    /// message comes from the transcript, which can land a poll later.
    static let idleGrace: Duration = .seconds(3)
    var streamSleep: @Sendable (Duration) async -> Void = { try? await Task.sleep(for: $0) }

    public init(key: String, hub: LocalHub) {
        self.key = key
        self.hub = hub
        commands.cacheKey = key
        // Sessions with no id share a path digest, so the kept lists
        // are scoped to the connected hub too.
        commands.scope = { [hub] in hub.connectedHub ?? "" }
        commands.fetch = { [hub, key] in
            try await hub.withClient { try await $0.commands(key) }
        }
        mentions.fetch = { [hub, key] query in
            try await hub.withClient { try await $0.files(key, query: query) }
        }
        tray.act = { [hub, key] cid, op, text in
            try await hub.withClient { try await $0.queueAction(key, clientId: cid, op: op, text: text) }
        }
        mentions.machine = { [weak self] in self?.row?.machine ?? self?.row?.machineId ?? "that machine" }
    }

    /// The agent is mid-turn, or a send waits for its first reply.
    public var working: Bool { row?.status == "active" || awaitingReply }

    /// Only an attached session takes messages; an observed one belongs to
    /// its terminal, and a mirrored one has no harness transcript at all.
    public var canSend: Bool { row?.isAttached == true && row?.isMirrored != true }

    /// The hub reads swe's read-only copy of a pruned transcript (#1918):
    /// history only, no stream, approvals or queue.
    public var isMirrored: Bool { row?.isMirrored == true }

    /// Loads the row and history, then follows the live stream until
    /// `stop()`.
    public func start() async {
        generation += 1
        let gen = generation
        // On screen now: its row's dot goes at once.
        if !readOpen {
            readOpen = true
            hub.chatOpened(key)
        }
        streamTask?.cancel()
        streamTask = nil
        // The / menu's list is fetched now, not on the first /, so the
        // menu opens filled; a list kept from an earlier open shows at
        // once and is still refreshed.
        commands.warm()
        loading = items.isEmpty
        do {
            let rows = try await hub.withClient { try await $0.sessions() }
            guard gen == generation else { return }
            guard let row = rows.first(where: { $0.key == key }) else {
                missing = true
                throw HubError(status: 404, code: "unknown_session", message: HubError.friendly(
                    code: "unknown_session", fallback: nil, status: 404))
            }
            self.row = row
            hub.noteRow(row)
            noteActivity(row.lastActiveAt)
            // The hub lists it, so it takes a read now, beside the
            // history read rather than before it. A failure changes
            // nothing, since the tracker already hid the dot.
            readOnOpen = Task { [hub, key] in await hub.postRead(key) }
            let page = try await hub.withClient { [key] in try await $0.messages(key, limit: Self.pageSize) }
            guard gen == generation else { return }
            load(page)
            loadError = nil
            // A copy never changes and nothing runs it: no stream, no
            // approval or queue to ask about, as on the web.
            if row.isMirrored {
                loading = false
                return
            }
            // An approval asked before the newest message isn't in the
            // page or after it (codex journals the ask before its tool
            // call): replay the journal from the start for its ask, taking
            // nothing else from the part already shown.
            if let rid = try? await hub.withClient({ [key] in try await $0.pendingApproval(key) }),
               gen == generation, !hasApproval(rid) {
                items.append(HubChatItem(id: "approval-\(rid)", kind: .approval(
                    requestId: rid, prompt: "Approval requested", options: [],
                    picked: decisions[rid], state: answerState(rid))))
                replayFloor = cursor
            }
            // The tray outlives the page: the hub holds the sends. Read
            // after history, so the list is no older than what's shown;
            // a send that leaves after this has its echo and result in
            // the stream.
            await refreshQueue(gen) { [hub, key] in try await hub.withClient { try await $0.queue(key) } }
        } catch is CancellationError {
            return
        } catch {
            guard gen == generation else { return }
            loadError = error.localizedDescription
            loading = false
            return
        }
        loading = false
        follow(gen, from: replayFloor > 0 ? 0 : cursor)
    }

    public func stop() {
        generation += 1
        streamTask?.cancel()
        streamTask = nil
        if readOpen {
            readOpen = false
            hub.chatClosed(key, activity: newestActivity)
            // The opening read stops here: one retried after a re-pair
            // would land after the close and cover replies nobody saw.
            readOnOpen?.cancel()
            readOnOpen = nil
            // One more read, due or not: it covers whatever the chat
            // showed, history that landed after the opening read included.
            readTask?.cancel()
            readTask = nil
            readFlush = Task { [hub, key] in await hub.postRead(key) }
        }
        modelTask?.cancel()
        modelTask = nil
        clearStreams()
        connection = .connecting
        // The prompt box's menu may still be open from before stop();
        // the kept list fills it again at once on the next open.
        commands.reset()
        mentions.reset()
    }

    /// Reads the hub's row again for the model alone. The chat reads its
    /// row once on `start()` and no event carries the model, so a model
    /// that first appears with the opening reply, or changes from another
    /// client or the terminal, would stay as it was until a reopen.
    ///
    /// The hub answers `/v1/sessions` from a catalog it rebuilds every 10
    /// seconds, and a turn's end can reach the stream before that, so one
    /// read may still hold the old model. Until the model differs, this
    /// reads again after each of `modelRetryDelays`. A failed read leaves
    /// the header as it is.
    @discardableResult
    func refreshModel() -> Task<Void, Never> {
        let gen = generation
        modelTask?.cancel()
        let task = Task { [weak self, key] in
            var later = self?.modelRetryDelays[...] ?? []
            while true {
                guard let self, gen == generation, !Task.isCancelled else { return }
                if let rows = try? await hub.withClient({ try await $0.sessions() }),
                   gen == generation, !Task.isCancelled,
                   let model = rows.first(where: { $0.key == key })?.model, row?.model != model {
                    row?.model = model
                    return
                }
                guard let delay = later.popFirst() else { return }
                await modelSleep(delay)
            }
        }
        modelTask = task
        return task
    }

    private func follow(_ gen: Int, from last: Int) {
        streamTask?.cancel()
        streamTask = Task { [weak self, key] in
            // A stop() before the client is ready opens no stream.
            guard let client = try? await self?.hub.client(), !Task.isCancelled else { return }
            for await event in client.events(key, last: last) {
                guard let self, gen == self.generation else { return }
                switch event {
                case .connected:
                    self.connection = .live
                    // A dropped stream may have lost deltas and finals alike.
                    self.clearStreams()
                case .disconnected: self.connection = .reconnecting
                case .unauthorized:
                    // Pairs again and reloads; a task of its own, since
                    // start() cancels this one.
                    self.hub.refused(client)
                    self.connection = .reconnecting
                    Task { await self.start() }
                    return
                case .truncated(let seq):
                    // The journal dropped events past the cursor and now
                    // replays what it kept, oldest first: show the newest
                    // page instead and follow on from its end. A journal
                    // wiped under the cursor restarts lower, at its head:
                    // the cursor only rises, so take that seq by hand, or
                    // the follow below asks from the old cursor and the
                    // hub answers truncated again, over and over.
                    if let seq, seq < self.cursor {
                        self.cursor = seq
                        self.seen = self.seen.filter { $0 <= seq }
                    }
                    // Until the newer history lands, nothing shown can be
                    // trusted, and with the cursor lowered a reconnect won't
                    // say truncated again: retry, backing off, rather than
                    // go on applying events over the old transcript.
                    var delay = Duration.seconds(1)
                    while true {
                        let page = try? await client.messages(key, limit: Self.pageSize)
                        guard gen == self.generation, !Task.isCancelled else { return }
                        if let page {
                            self.load(page)
                            break
                        }
                        try? await Task.sleep(for: delay)
                        delay = min(delay * 2, .seconds(15))
                    }
                    // Results and echoes past the cursor were dropped too:
                    // the tray may hold sends that left meanwhile.
                    await self.refreshQueue(gen) { try await client.queue(key) }
                    self.follow(gen, from: self.cursor)
                    return
                case .event(let env): self.apply(env)
                }
            }
        }
    }

    /// Takes the hub's list of held sends into the tray. Read after
    /// history, so the list is no older than what's shown; a send held
    /// while it was asked for stays.
    private func refreshQueue(_ gen: Int, _ fetch: () async throws -> [HubWaitingSend]) async {
        let asked = tray.holds
        guard let sends = try? await fetch(), gen == generation else { return }
        tray.merge(sends, known: Set(items.compactMap {
            $0.id.hasPrefix("send-") ? String($0.id.dropFirst("send-".count)) : nil
        }), asked: asked)
    }

    /// Replaces what's shown with the newest page of history.
    func load(_ page: HubMessagesPage) {
        // Sends not yet echoed aren't in history: they carry over.
        let unsent = items.filter {
            if case .user(_, let send?) = $0.kind { send.inFlight || send == .delivered } else { false }
        }
        items = []
        seen = []
        toolCards = [:]
        orphanResults = [:]
        replayFloor = 0
        clearStreams()
        // Before the messages: whether a result without its call can still
        // find it on an older page.
        hasEarlier = page.hasMore ?? false
        loadedUsers = []
        replaying = true
        for env in page.messages { apply(env) }
        replaying = false
        items += unsent.filter { !echoedInHistory($0) }
        nextBefore = page.nextBefore ?? page.messages.first?.seq
        showOrphans()
    }

    /// Prepends the page of history just older than what's loaded,
    /// skipping pages that hold only results of calls further back.
    public func loadEarlier() async {
        guard hasEarlier, !loadingEarlier else { return }
        loadingEarlier = true
        defer { loadingEarlier = false }
        while hasEarlier, let before = nextBefore {
            guard let page = try? await hub.withClient({ [key] in
                try await $0.messages(key, limit: Self.pageSize, before: before)
            }) else { return }
            // A truncation reload replaced the history meanwhile.
            guard nextBefore == before else { return }
            if prepend(page) { return }
            // A cursor that didn't move back would ask for the same page
            // forever: treat it as the start of the history.
            if hasEarlier, (nextBefore ?? before) >= before {
                hasEarlier = false
                showOrphans()
            }
        }
    }

    /// Puts an older page in front; returns whether it added anything.
    func prepend(_ page: HubMessagesPage) -> Bool {
        var older: [HubChatItem] = []
        var cards: [String: String] = [:]
        for env in page.messages where env.type == "message.appended" {
            guard let seq = env.seq, !seen.contains(seq), let msg = env.decode(HubMessage.self) else { continue }
            seen.insert(seq)
            Self.add(msg, seq: seq, to: &older, cards: &cards, orphans: &orphanResults)
        }
        // A call on this page may still be running; its live result has
        // to find the card.
        toolCards.merge(cards) { newer, _ in newer }
        items.insert(contentsOf: older, at: 0)
        hasEarlier = page.hasMore ?? false
        nextBefore = page.nextBefore ?? page.messages.first?.seq
        let shown = items.count
        showOrphans()
        return !older.isEmpty || items.count > shown
    }

    /// With no older page left, a result whose call the hub no longer
    /// keeps shows on its own card, where it was in the transcript.
    private func showOrphans() {
        guard !hasEarlier else { return }
        for orphan in orphanResults.values.sorted(by: { $0.seq < $1.seq }) {
            insert(HubChatItem(id: "m\(orphan.seq)", kind: .tool(HubToolCall(name: "Tool output"), result: orphan.text)),
                   seq: orphan.seq)
        }
        orphanResults = [:]
    }

    /// Puts `item` before the first message newer than `seq`.
    private func insert(_ item: HubChatItem, seq: Int) {
        let at = items.firstIndex { Self.seq(of: $0).map { $0 > seq } ?? false } ?? items.endIndex
        items.insert(item, at: at)
    }

    /// The journal seq of a message's entry (ids are "m<seq>").
    private static func seq(of item: HubChatItem) -> Int? {
        item.id.hasPrefix("m") ? Int(item.id.dropFirst()) : nil
    }

    private func hasApproval(_ rid: String) -> Bool {
        items.contains { $0.id == "approval-\(rid)" }
    }

    // MARK: Read state

    /// Waits out the reads `start()` and `stop()` sent, so a test's
    /// stub sees none land after it moves on.
    func settleReads() async {
        await readOnOpen?.value
        await readFlush?.value
    }

    private func noteActivity(_ at: Double?) {
        guard let at else { return }
        newestActivity = max(newestActivity ?? at, at)
    }

    /// Posts a read `readDebounce` after the first live message since the
    /// last one posted; messages meanwhile ride along. Nothing once
    /// closed.
    private func scheduleRead() {
        guard readOpen, readTask == nil else { return }
        let sleep = readSleep
        readTask = Task { [weak self] in
            await sleep(Self.readDebounce)
            guard let self, !Task.isCancelled, readOpen else { return }
            readTask = nil
            await hub.postRead(key)
        }
    }

    // MARK: Events

    func apply(_ env: HubEnvelope) {
        if let seq = env.seq, seq <= replayFloor {
            replayApproval(env, seq: seq)
            return
        }
        if let seq = env.seq {
            if env.type == "message.appended" {
                guard !seen.contains(seq) else { return }
                seen.insert(seq)
            }
            cursor = max(cursor, seq)
        }
        switch env.type {
        case "message.delta":
            // Live only: history never holds one, and a replay's are stale.
            guard !replaying, let delta = env.decode(ReplyDelta.self) else { return }
            receive(delta)
        case "message.appended":
            if let msg = env.decode(HubMessage.self) {
                // The final message takes the oldest open bubble of its
                // kind, in the same update, so nothing jumps.
                let replaced = replaying ? nil : replies.claim(kind: msg.kind)
                append(msg, seq: env.seq ?? 0)
                if let replaced {
                    replies.release(replaced)
                    repaintStreams()
                }
                showOrphans()
                noteActivity(msg.ts)
                // Seen live on screen: the row stays read.
                if !replaying { scheduleRead() }
            }
        case "session.status":
            let status = env.decode(HubStatusData.self)?.status ?? ""
            row?.status = status
            // The turn ended (or stopped to ask) without a message to show.
            if !replaying, !status.isEmpty, status != "active" {
                awaiting.removeAll()
                sweepStreams()
                // A reply just finished: the header's model may be new.
                refreshModel()
            }
        case "command.result":
            if let result = env.decode(HubCommandResult.self) { settle(result) }
        case "session.ownership":
            if let ownership = env.decode(HubOwnershipData.self)?.ownership { row?.ownership = ownership }
        case "session.meta":
            // A pin or rename, from here or another device: the header's
            // title and menu read the hub's overlay.
            if let meta = env.decode(HubMetaReply.self) {
                if let title = meta.title { row?.title = title.isEmpty ? meta.harnessTitle : title }
                if let harness = meta.harnessTitle { row?.harnessTitle = harness }
                if let pinned = meta.pinned {
                    row?.pinned = pinned
                    row?.pinnedAt = pinned ? meta.pinnedAt : nil
                }
                hub.noteMeta(key, meta)
            }
        case "approval.requested":
            guard let req = env.decode(HubApprovalRequested.self), let rid = req.requestId else { return }
            let prompt = req.prompt ?? "Approval requested"
            let options = req.options ?? []
            if let i = items.firstIndex(where: { $0.id == "approval-\(rid)" }) {
                // The placeholder for a waiting approval learns its prompt and menu.
                guard case .approval(_, _, _, _, let state) = items[i].kind else { return }
                let card = HubChatItem(id: "approval-\(rid)", kind: .approval(
                    requestId: rid, prompt: prompt, options: options,
                    picked: decisions[rid], state: state))
                if case .resolved = state {
                    items[i] = card
                } else {
                    // Asked again under the same id (a satellite resends a
                    // refused batch whole): the open card moves to the newest
                    // ask, below what it came after, instead of waiting
                    // mid-transcript where nobody scrolls.
                    items.remove(at: i)
                    insert(card, seq: env.seq ?? .max)
                }
                return
            }
            items.append(HubChatItem(id: "approval-\(rid)", kind: .approval(
                requestId: rid, prompt: prompt, options: options,
                picked: decisions[rid], state: answerState(rid))))
        case "approval.resolved":
            let res = env.decode(HubApprovalResolved.self)
            for i in items.indices {
                guard case .approval(let rid, let prompt, let options, let picked, _) = items[i].kind else { continue }
                if (res?.requestId ?? "").isEmpty || res?.requestId == rid {
                    // The event's decision wins over a row picked here;
                    // a bare resolve falls back to it (the web app's
                    // [data-dec] match, then the .chosen mark).
                    let state: HubApprovalState = .resolved(res?.decision ?? decisions[rid])
                    items[i].kind = .approval(requestId: rid, prompt: prompt, options: options,
                                              picked: lit(res?.decision, on: options, picked: picked,
                                                          rid: rid),
                                              state: state)
                    answers[rid] = state
                }
            }
        default:
            break
        }
    }

    /// A replayed event from history already shown: an approval's ask
    /// goes where it was asked, with its prompt; its answer settles it.
    private func replayApproval(_ env: HubEnvelope, seq: Int) {
        cursor = max(cursor, seq)
        switch env.type {
        case "approval.requested":
            guard let req = env.decode(HubApprovalRequested.self), let rid = req.requestId else { return }
            var state = answerState(rid)
            if let i = items.firstIndex(where: { $0.id == "approval-\(rid)" }) {
                if case .approval(_, _, _, _, let shown) = items[i].kind { state = shown }
                items.remove(at: i)
            }
            insert(HubChatItem(id: "approval-\(rid)", kind: .approval(
                requestId: rid, prompt: req.prompt ?? "Approval requested",
                options: req.options ?? [], picked: decisions[rid], state: state)), seq: seq)
        case "approval.resolved":
            let res = env.decode(HubApprovalResolved.self)
            guard let rid = res?.requestId, let i = items.firstIndex(where: { $0.id == "approval-\(rid)" }),
                  case .approval(_, let prompt, let options, let picked, _) = items[i].kind else { return }
            let state: HubApprovalState = .resolved(res?.decision ?? decisions[rid])
            items[i].kind = .approval(requestId: rid, prompt: prompt, options: options,
                                      picked: lit(res?.decision, on: options, picked: picked, rid: rid),
                                      state: state)
            answers[rid] = state
        default:
            break
        }
    }

    private func receive(_ delta: ReplyDelta) {
        guard replies.apply(delta), repaintTask == nil else { return }
        let sleep = streamSleep
        repaintTask = Task { [weak self] in
            await sleep(Self.repaintEvery)
            guard let self, !Task.isCancelled else { return }
            self.repaintTask = nil
            self.repaintStreams()
        }
    }

    /// Draws the bubbles as they are now.
    func repaintStreams() {
        repaintTask?.cancel()
        repaintTask = nil
        let shown = replies.shown.map { HubChatItem(id: "live-\($0.id)", kind: .assistant($0.text)) }
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

    private func clearStreams() {
        replies.clear()
        repaintStreams()
    }

    private func append(_ msg: HubMessage, seq: Int) {
        if replaying, msg.kind == "user", let text = msg.text { loadedUsers.append(("m\(seq)", text, msg.ts)) }
        var echoOf: String?
        var carried: HubSendState?
        if !replaying, msg.kind == "user", let text = msg.text,
           let i = items.lastIndex(where: {
               guard $0.id.hasPrefix("send-"), case .user(let sent, let send?) = $0.kind, !Self.isFailed(send)
               else { return false }
               return Self.echoes(text, sent)
           }), case .user(_, let send?) = items[i].kind {
            // The hub echoes a send back as a user message: it replaces the
            // bubble drawn when it was typed. A degraded send keeps its note.
            let cid = String(items[i].id.dropFirst("send-".count))
            echoOf = cid
            if case .degraded = send { carried = send }
            if i == items.count - 1 {
                // Still the newest entry: settle it where it is.
                items[i].kind = .user(text, send: carried)
                echoed[cid] = items[i].id
                return
            }
            items.remove(at: i)
        } else if !replaying, msg.kind == "user", let text = msg.text {
            // A queued send went out: it leaves the tray, and the echo
            // lands in the transcript where it really is.
            tray.noteEcho(text, echoes: Self.echoes)
        } else if !replaying, msg.kind != "user", msg.kind != "tool_result" {
            awaiting.removeAll()
        }
        let before = items.count
        Self.add(msg, seq: seq, to: &items, cards: &toolCards, orphans: &orphanResults)
        if let cid = echoOf, items.count > before, case .user(let text, _) = items[items.count - 1].kind {
            echoed[cid] = items[items.count - 1].id
            items[items.count - 1].kind = .user(text, send: carried)
        }
    }

    /// A send carried over a history reload whose echo is in that history
    /// (it landed while the stream was down): the echo stands for it.
    private func echoedInHistory(_ item: HubChatItem) -> Bool {
        guard case .user(let text, let send?) = item.kind else { return false }
        let cid = String(item.id.dropFirst("send-".count))
        let typed = sentAt[cid] ?? 0
        let claimed = Set(echoed.values)
        guard let hit = loadedUsers.last(where: {
            !claimed.contains($0.id) && Self.echoes($0.text, text) && ($0.ts ?? .infinity) >= typed - 5
        }) else { return false }
        echoed[cid] = hit.id
        if case .degraded = send, let i = items.firstIndex(where: { $0.id == hit.id }) {
            items[i].kind = .user(hit.text, send: send)
        }
        return true
    }

    /// The transcript's user message `echo` is the send `sent` coming
    /// back: the same text, or with the hub's attachment prefix, one
    /// "@path " per attachment.
    static func echoes(_ echo: String, _ sent: String) -> Bool {
        if echo == sent { return true }
        guard echo.hasSuffix(" " + sent) else { return false }
        let prefix = echo.dropLast(sent.count)
        return prefix.hasPrefix("@") && prefix.split(separator: " ").allSatisfy { $0.count > 1 && $0.first == "@" }
    }

    private static func isFailed(_ send: HubSendState) -> Bool {
        if case .failed = send { return true }
        return false
    }

    /// A command's outcome: a send's bubble settles; any other failure, or
    /// a degraded result, gets a line of its own.
    private func settle(_ r: HubCommandResult) {
        if r.op == "message", let cid = r.clientId, let held = tray.take(cid) {
            // A queued send's result: removed, it's simply gone; sent and
            // already echoed, the echo stands for it; otherwise it shows
            // in the transcript as any send, settled below (an echo still
            // replaces it).
            if r.cancelled == true || (r.ok != false && held.echoed) { return }
            items.append(HubChatItem(id: "send-\(cid)", kind: .user(held.send.text, send: .accepted(waiting: false))))
            sentAt[cid] = Date().timeIntervalSince1970
        }
        if r.op == "message", let cid = r.clientId {
            // Its bubble, or the entry the echo turned it into.
            guard let i = items.firstIndex(where: { $0.id == (echoed[cid] ?? "send-\(cid)") }),
                  case .user(let text, let send) = items[i].kind else { return }
            // Failed or degraded already: that outcome stands.
            if let send, !send.inFlight, send != .delivered { return }
            if r.ok == false {
                items[i].kind = .user(text, send: .failed(r.cancelled == true ? "Removed before sending"
                                                          : r.note ?? "Send failed"))
                awaiting.remove(cid)
            } else if r.degraded == true {
                items[i].kind = .user(text, send: .degraded(r.note ?? "Sent (queued: mid-turn input isn't supported here)"))
            } else if echoed[cid] == nil {
                // Kept as a send, so its echo still replaces it.
                items[i].kind = .user(text, send: .delivered)
            }
            return
        }
        if r.op == "approval", let rid = r.requestId {
            // The result closes only an answer sent from here; a request
            // another client answered settles on approval.resolved, not
            // on a result this card never asked for.
            if pendingAnswers.remove(rid) != nil {
                if r.ok == false {
                    // Rejected after the card folded: it re-opens,
                    // answerable with the failure note, and the stale
                    // pick lights no row (the web app clears .chosen).
                    decisions.removeValue(forKey: rid)
                    setApproval(rid) { _ in
                        .failed("Approval failed: \(r.note ?? "the satellite rejected it")")
                    }
                } else {
                    setApproval(rid) { _ in .resolved(decisions[rid]) }
                }
            }
            return
        }
        guard r.op != "message", r.ok == false || r.degraded == true else { return }
        let op = r.op.map { $0.prefix(1).uppercased() + $0.dropFirst() } ?? "Command"
        items.append(HubChatItem(id: "result-\(UUID().uuidString)",
                                 kind: .system(r.ok == false ? "\(op) failed: \(r.note ?? "unknown error")"
                                               : r.note ?? "Queued")))
    }

    // MARK: Actions

    /// Sends `text` in `mode` (`queue` or `steer`), drawn at once as a
    /// bubble the hub's answer, echo and result then settle.
    public func send(_ text: String, mode: String) async {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, canSend else { return }
        let cid = UUID().uuidString
        let id = "send-\(cid)"
        items.append(HubChatItem(id: id, kind: .user(text, send: .sending)))
        awaiting.insert(cid)
        sentAt[cid] = Date().timeIntervalSince1970
        do {
            let waiting = try await hub.withClient { [key] in
                try await $0.sendMessage(key, text: text, mode: mode, clientId: cid)
            }
            // An echo or result may already have settled it.
            if let i = items.firstIndex(where: { $0.id == id }), case .user(_, .sending?) = items[i].kind {
                if waiting {
                    // The hub holds it until the turn ends: it waits in
                    // the tray, out of the transcript.
                    items.remove(at: i)
                    awaiting.remove(cid)
                    tray.hold(cid, text: text)
                } else {
                    items[i].kind = .user(text, send: .accepted(waiting: false))
                }
            }
        } catch {
            refusal(error)
            awaiting.remove(cid)
            if let i = items.firstIndex(where: { $0.id == id }), case .user(_, let send?) = items[i].kind, send.inFlight {
                items[i].kind = .user(text, send: .failed(error.localizedDescription))
            }
        }
    }

    /// Stops the agent's turn.
    public func interrupt() async {
        guard canSend, !stopping else { return }
        stopping = true
        defer { stopping = false }
        do {
            _ = try await hub.withClient { [key] in try await $0.control(key, op: "interrupt") }
        } catch {
            refusal(error)
            items.append(HubChatItem(id: "stop-\(UUID().uuidString)",
                                     kind: .error("Stop failed: \(error.localizedDescription)")))
        }
    }

    /// Answers an approval: `allow`, `always`, `deny`, or a menu row's
    /// digit, posted to the hub as given.
    public func decide(_ requestId: String, _ decision: String) async {
        guard let i = items.firstIndex(where: { $0.id == "approval-\(requestId)" }),
              case .approval(_, _, _, _, let state) = items[i].kind, state.answerable else { return }
        decisions[requestId] = decision
        // Marked before the reply arrives: a satellite's command.result
        // can beat it and must find the card waiting on it.
        pendingAnswers.insert(requestId)
        setApproval(requestId) { _ in .submitting }
        do {
            let queued = try await hub.withClient { [key] in
                try await $0.approve(key, requestId: requestId, decision: decision)
            }
            if !queued { pendingAnswers.remove(requestId) }
            // An approval.resolved or command.result that raced the
            // answer already settled it: theirs wins.
            setApproval(requestId) { state in
                guard state == .submitting else { return state }
                return queued ? .queued : .resolved(decision)
            }
        } catch {
            pendingAnswers.remove(requestId)
            setApproval(requestId) { state in
                guard state == .submitting else { return state }
                // The refused pick lights no row on the re-opened card.
                decisions.removeValue(forKey: requestId)
                return .failed("Approval failed: \(error.localizedDescription)")
            }
        }
    }

    /// The decision a settled card lights as picked: the event's own
    /// when it names a row the card shows (a menu digit on a menu card,
    /// a semantic word without one), else the row already lit (the web
    /// app's `[data-dec]` match falling back to `.chosen`), else the row
    /// answered here.
    private func lit(_ decision: String?, on options: [HubApprovalOption],
                     picked: String?, rid: String) -> String? {
        // The rows the card draws: a labelled row without a number the
        // hub takes is no button, and none left means Allow, Always, Deny.
        let menu = ApprovalRows.menu(options)
        let rows = menu.isEmpty ? ["allow", "always", "deny"] : menu.map(\.n)
        if let decision, rows.contains(decision) { return decision }
        return picked ?? decisions[rid]
    }

    private func setApproval(_ rid: String, _ next: (HubApprovalState) -> HubApprovalState) {
        guard let i = items.firstIndex(where: { $0.id == "approval-\(rid)" }),
              case .approval(_, let prompt, let options, let picked, let state) = items[i].kind else { return }
        let new = next(state)
        // A transition that keeps the state (a raced event already
        // settled the card) leaves the lit pick alone — a stale local
        // choice must not light the losing row under another verdict.
        items[i].kind = .approval(requestId: rid, prompt: prompt, options: options,
                                  picked: new == state ? picked : decisions[rid], state: new)
        answers[rid] = new
    }

    /// A card drawn again (a history reload rebuilds the transcript) keeps
    /// the answer given from here.
    private func answerState(_ rid: String) -> HubApprovalState {
        answers[rid] ?? .open
    }

    /// Takes an observed session over: the hub stops the harness in its
    /// terminal and resumes it under SWE, so it takes messages here.
    public func attach() async {
        guard row?.isAttached == false, !isMirrored, !attaching else { return }
        attaching = true
        defer { attaching = false }
        do {
            let queued = try await hub.withClient { [key] in try await $0.control(key, op: "attach") }
            if queued {
                items.append(HubChatItem(id: "attach-\(UUID().uuidString)",
                                         kind: .system("Attach queued: waiting for the satellite")))
            } else {
                // The hub also emits session.ownership; its answer is
                // enough to open the composer now.
                row?.ownership = "attached"
            }
        } catch {
            items.append(HubChatItem(id: "attach-\(UUID().uuidString)",
                                     kind: .error("Attach failed: \(error.localizedDescription)")))
        }
    }

    /// Attach can take this session now: observed, resumable, and not
    /// mid-turn (the hub refuses that with not_idle).
    public var canAttach: Bool {
        guard let row, !row.isMirrored else { return false }
        return !row.isAttached && row.attachable == true && row.status != "active"
    }

    /// The hub says the session is observed now: its word wins.
    private func refusal(_ error: Error) {
        if (error as? HubError)?.code == "not_attached" { row?.ownership = "observed" }
    }

    /// Adds one message to the end of `list`. A tool result lands on its
    /// call's card; one whose call is on an older page not loaded yet
    /// waits in `orphans` until that page brings the call.
    static func add(_ msg: HubMessage, seq: Int, to list: inout [HubChatItem],
                    cards: inout [String: String], orphans: inout [String: Orphan]) {
        let id = "m\(seq)"
        let text = msg.text ?? ""
        switch msg.kind ?? "assistant" {
        case "user":
            list.append(HubChatItem(id: id, kind: .user(text, send: nil)))
        case "thinking":
            list.append(HubChatItem(id: id, kind: .thinking(text, seconds: msg.durationS)))
        case "tool_use":
            guard let tool = msg.tool else { return }
            var result: String?
            if let callId = tool.callId {
                cards[callId] = id
                result = orphans.removeValue(forKey: callId)?.text
            }
            list.append(HubChatItem(id: id, kind: .tool(tool, result: result)))
        case "tool_result":
            guard let ref = msg.resultRef else { return }
            let result = String(text.prefix(8000))
            if let cardId = cards[ref], let i = list.firstIndex(where: { $0.id == cardId }),
               case .tool(let call, _) = list[i].kind {
                list[i].kind = .tool(call, result: result)
            } else {
                orphans[ref] = Orphan(seq: seq, text: result)
            }
        case "compaction":
            list.append(HubChatItem(id: id, kind: .compaction))
        case "system":
            list.append(HubChatItem(id: id, kind: .system(text)))
        case "error":
            list.append(HubChatItem(id: id, kind: .error(text)))
        default:
            guard !text.isEmpty else { return }
            list.append(HubChatItem(id: id, kind: .assistant(text)))
        }
    }
}
