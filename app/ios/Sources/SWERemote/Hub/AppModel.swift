import Foundation
import Observation

enum Connection: Equatable { case connecting, online, offline }

/// App-wide state: the paired hubs, the sessions and inbox of the one
/// on screen, and the pairing flows that get a device there.
@MainActor @Observable
final class AppModel {
    /// Every hub this phone is paired with; `hub` is the one on screen.
    private(set) var hubs: HubList
    var hub: PairedHub? { hubs.active }
    private(set) var sessions: [SessionRow] = []
    private(set) var machines: [Machine] = []
    private(set) var inbox: [InboxItem] = []
    private(set) var connection: Connection = .connecting
    private(set) var loadError: String?
    private(set) var loadedOnce = false
    /// When the lists last loaded; an open search reruns when it moves.
    private(set) var refreshedAt: Date?
    var pairing = PairingFlow()
    /// Bumped by cancelPairing(); a pairing reply from before it is dropped.
    private var pairingGen = 0

    private var refreshTask: Task<Void, Never>?
    @ObservationIgnored private let store: HubStore
    /// The console's saved targets, one per hub; an unpaired hub's goes.
    @ObservationIgnored private let sshTargets: SSHTargetStore
    /// How Unpair tells the hub to sign this phone out; tests swap it.
    @ObservationIgnored var revokeOnHub: (HubClient, String) async -> Void = { client, deviceId in
        try? await client.revokeDevice(id: deviceId, confirmThisDevice: true)
    }

    /// Keeps the home screen widget's snapshot current; nil in tests,
    /// so they never write the App Group's copy.
    @ObservationIgnored private let widgets: WidgetPublisher?

    init(store: HubStore = KeychainHubStore(), sshTargets: SSHTargetStore = SSHTargetStore(),
         widgets: WidgetPublisher? = nil) {
        self.store = store
        self.sshTargets = sshTargets
        self.widgets = widgets
        hubs = store.load()
    }

    /// A model on `hub` without reading the Keychain, for tests.
    convenience init(hub: PairedHub?) {
        var list = HubList()
        if let hub { list.adopt(hub) }
        self.init(store: MemoryHubStore(list))
    }

    var client: HubClient? {
        hub.map { HubClient(baseURL: $0.baseURL, credential: $0.credential.credential) }
    }

    var hubName: String {
        machines.first?.displayName ?? hub.map { hubs.name(of: $0) } ?? "Hub"
    }

    // MARK: Refresh

    /// Polls sessions and inbox every 15 s while the app is in front,
    /// the cadence the remote PWA used.
    func startRefreshing() {
        refreshTask?.cancel()
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                // Nil once the model is gone: the loop ends with it.
                guard await self?.refresh() != nil else { return }
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }

    func stopRefreshing() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    func refresh() async {
        guard let client else { return }
        // A re-pair can land while this is in flight; answers from the old
        // hub must not overwrite (or unpair) the new one.
        let pairedAtStart = hub
        let pinMark = pins.mark
        let attachedAtStart = Set(sessions.filter(\.isAttached).map(\.key))
        do {
            async let s = client.sessions()
            // done=1 so the Inbox's "Finished today" rows ship with it.
            async let i = client.inbox(done: true)
            let (sessionsBody, inboxBody) = try await (s, i)
            guard hub == pairedAtStart else { return }
            let fetched = pins.overlay(sessionsBody.sessions, fetchedFrom: pinMark)
            applyLists(sessions: fetched, machines: sessionsBody.machines, inbox: inboxBody.items,
                       attachedAtStart: attachedAtStart)
            rememberName(machines.first?.displayName, of: pairedAtStart)
            if let paired = pairedAtStart {
                widgets?.publish(hubId: paired.id, hubName: hubName, sessions: sessions, inbox: inbox)
            }
        } catch is CancellationError {
            // The app left the screen, or another hub went on it: not a
            // hub that can't be reached.
            return
        } catch let error as HubError {
            guard hub == pairedAtStart, !Task.isCancelled else { return }
            if error.isUnauthorized { unpair(from: client); return }
            if connection != .offline { connection = .offline }
            if loadError != error.message { loadError = error.message }
        } catch {
            guard hub == pairedAtStart, !Task.isCancelled else { return }
            if connection != .offline { connection = .offline }
        }
        if !loadedOnce { loadedOnce = true }
    }

    /// Stores one poll's lists. `@Observable` reports every write, equal
    /// or not, and the 15 s poll mostly brings back what is already
    /// here, so each list is written only when it changed: an unchanged
    /// poll leaves the Sessions page's grouping, its search and the inbox
    /// alone. Only `refreshedAt` moves every time (the model switch reads
    /// it).
    func applyLists(sessions fetched: [SessionRow], machines fetchedMachines: [Machine]?,
                    inbox items: [InboxItem], attachedAtStart: Set<String>, now: Date = Date()) {
        // Attached both when this refresh asked and now, and the hub
        // says it isn't: detached elsewhere. One attached while the
        // request was out isn't judged by an answer older than that.
        let attachedNow = Set(sessions.filter(\.isAttached).map(\.key))
        if sessions != fetched { sessions = fetched }
        var reconciled = unread
        reconciled.reconcile(fetched)
        if reconciled != unread { unread = reconciled }
        for key in attachedAtStart.intersection(attachedNow)
        where fetched.first(where: { $0.key == key })?.isAttached != true {
            handedBack(key)
        }
        let detaches = pendingDetaches.filter { key, at in
            now.timeIntervalSince(at) < Self.detachWaitCap && fetched.first { $0.key == key }?.isAttached != false
        }
        if detaches != pendingDetaches { pendingDetaches = detaches }
        if let fetchedMachines, fetchedMachines != machines { machines = fetchedMachines }
        // Overlay dismissals still in flight: a response captured
        // before the hub saw the POST would resurrect a cleared row.
        let shown = dismissals.overlay(items)
        if shown != inbox { inbox = shown }
        refreshedAt = now
        if connection != .online { connection = .online }
        if loadError != nil { loadError = nil }
    }

    /// Sessions as the list shows them: a session with an approval open is
    /// "approval" even while its transcript status still says active (the
    /// hub marks that only on inbox items).
    var displaySessions: [SessionRow] {
        let waiting = Set(inbox.filter { $0.sessionStatus == .approval }.map(\.key))
        guard !waiting.isEmpty else { return sessions }
        return sessions.map { r in
            guard waiting.contains(r.key) else { return r }
            var row = r
            row.status = "approval"
            return row
        }
    }

    func session(_ key: String) -> SessionRow? { sessions.first { $0.key == key } }

    // MARK: Unread

    /// The dots on session rows: the hub's per-device flag, with the
    /// chat on screen and the reads this phone made laid over it.
    private(set) var unread = UnreadTracker()
    /// Sends the open chat's read cursor: at once on open, debounced
    /// while messages arrive, flushed on close.
    @ObservationIgnored private(set) lazy var reads = ReadPoster { [weak self] key in self?.postRead(key) }
    /// How a read reaches the hub; tests swap it.
    @ObservationIgnored var markReadOnHub: (HubClient, String) async throws -> ReadAck = { client, key in
        try await client.markRead(key)
    }

    func isUnread(_ row: SessionRow) -> Bool { unread.isUnread(row) }

    /// A chat came on screen holding activity up to `newest`: its dot
    /// goes now, and the hub hears at once.
    func chatOpened(_ key: String, newest: Double?) {
        unread.open(key, at: activityStamp(key, newest))
        reads.opened(key)
    }

    /// A message arrived in the chat on screen.
    func chatGotMessage(_ key: String) { reads.message(key) }

    /// The chat left the screen after showing activity up to `newest`.
    func chatClosed(_ key: String, newest: Double?) {
        reads.closed(key)
        unread.close(key, at: activityStamp(key, newest))
    }

    /// The newest activity the phone knows of: the row's stamp, or a
    /// message the stream brought after the list last loaded.
    private func activityStamp(_ key: String, _ newest: Double?) -> Double? {
        let listed = session(key)?.lastActiveAt
        guard let newest else { return listed }
        return max(listed ?? newest, newest)
    }

    /// Best effort: an older hub, an unknown session or no network
    /// leaves the dot to the next list. The hub's answer is its cursor,
    /// which clears everything up to it.
    private func postRead(_ key: String) {
        // The hub on screen now, not when the task runs: a hub switch in
        // between must not send this chat's read to the new hub.
        guard let client, let start = hub else { return }
        let call = markReadOnHub
        Task {
            guard let ack = try? await call(client, key), hub == start, let at = ack.at else { return }
            unread.mark(key, at: at)
        }
    }

    // MARK: Row actions

    /// `when_idle` detaches the hub is still waiting on, by key, with when
    /// they were asked for. One clears when the row stops being attached,
    /// when its `command.result` arrives in an open chat, or after the
    /// hub's 30 min cap.
    private(set) var pendingDetaches: [String: Date] = [:]
    static let detachWaitCap: TimeInterval = 31 * 60
    /// Pins chosen here that a list fetched too early must not undo.
    private(set) var pins = PinLedger()

    func isDetachPending(_ key: String, now: Date = Date()) -> Bool {
        guard let at = pendingDetaches[key] else { return false }
        return now.timeIntervalSince(at) < Self.detachWaitCap
    }

    /// The detach a chat heard the result of (`command.result`, op detach).
    func detachSettled(_ key: String) { pendingDetaches[key] = nil }

    /// Runs `call` against the hub paired now. Nil when a re-pair landed
    /// while it was out: its answer, or its error, belongs to a hub this
    /// phone no longer shows.
    func onSameHub<T>(_ call: (HubClient) async throws -> T) async throws -> T? {
        guard let client, let start = hub else { return nil }
        do {
            let value = try await call(client)
            return hub == start ? value : nil
        } catch {
            guard hub == start else { return nil }
            throw error
        }
    }

    /// Pins or unpins at once, then tells the hub. Returns what to say
    /// when the hub refused it and the row went back. `animate` wraps the
    /// moves between sections, both ways.
    func setPinned(_ key: String, _ pinned: Bool,
                   animate: (() -> Void) -> Void = { $0() }) async -> String? {
        guard let i = sessions.firstIndex(where: { $0.key == key }) else { return nil }
        let before = sessions[i]
        let version = pins.choose(key, pinned: pinned, now: Date().timeIntervalSince1970)
        animate {
            sessions[i].pinned = pinned
            sessions[i].pinnedAt = pins.choices[key]?.pinnedAt
        }
        do {
            guard let meta = try await onSameHub({ try await $0.pin(key, pinned: pinned) }) else { return nil }
            // A newer choice on this row is still out: this answer is stale.
            if pins.answered(key, version: version) { patch(key, meta: meta) }
            return nil
        } catch {
            guard pins.refused(key, version: version) else { return nil }
            if let j = sessions.firstIndex(where: { $0.key == key }) {
                animate {
                    sessions[j].pinned = before.pinned
                    sessions[j].pinnedAt = before.pinnedAt
                }
            }
            let reason = error.localizedDescription
            return "\(pinned ? "Couldn't pin it" : "Couldn't unpin it"). \(reason)"
        }
    }

    /// An empty `title` goes back to the harness's own name.
    func rename(_ key: String, title: String) async throws {
        guard let meta = try await onSameHub({ try await $0.rename(key, title: title) }) else { return }
        patch(key, meta: meta)
    }

    /// Returns true when the hub queued it for a satellite (202).
    @discardableResult
    func attach(_ key: String) async throws -> Bool {
        // A mirrored copy has no process to attach to; no caller offers it,
        // and a stale one reaching here must not call the hub.
        if session(key)?.isMirrored == true {
            throw HubError(status: 409, code: "mirrored", message: Mirrored.notice)
        }
        guard let queued = try await onSameHub({ try await $0.control(key, op: "attach") }) else { return false }
        // An attach also cancels a detach waiting on the turn.
        pendingDetaches[key] = nil
        if !queued { patch(key, ownership: "attached") }
        return queued
    }

    enum DetachOutcome: Equatable {
        /// Handed back already.
        case done
        /// A `when_idle` detach the hub runs once it sees the turn over.
        case waiting
        /// Forwarded to a satellite, which reports back later.
        case forwarded
        /// The phone paired with another hub while it was out.
        case stale
    }

    func detach(_ key: String, mode: DetachMode) async throws -> DetachOutcome {
        guard let accepted = try await onSameHub({ try await $0.detach(key, mode: mode) }) else { return .stale }
        if mode == .whenIdle {
            pendingDetaches[key] = Date()
            return .waiting
        }
        // A now or keep detach replaces a waiting one.
        pendingDetaches[key] = nil
        if accepted { return .forwarded }
        patch(key, ownership: "observed")
        return .done
    }

    // MARK: Chats

    /// Chats opened recently, newest last. Going back into one shows what
    /// it already had and replays only the events since, instead of
    /// fetching and laying out the transcript again.
    private var chats: [ChatModel] = []
    static let chatCacheSize = 8

    func chat(_ key: String) -> ChatModel {
        if let i = chats.firstIndex(where: { $0.key == key }) {
            let chat = chats.remove(at: i)
            chats.append(chat)
            return chat
        }
        let chat = ChatModel(key: key, app: self)
        chats.append(chat)
        if chats.count > Self.chatCacheSize { chats.removeFirst().stop() }
        return chat
    }

    /// A session's status or ownership changed on the live stream; patch
    /// the cached row so the list agrees before the next poll.
    func patch(_ key: String, status: String? = nil, ownership: String? = nil) {
        if let ownership, ownership != "attached" { handedBack(key) }
        guard let i = sessions.firstIndex(where: { $0.key == key }) else { return }
        if let status { sessions[i].status = status }
        if let ownership {
            sessions[i].ownership = ownership
            // Handed back: a detach waiting for the turn to end is done.
            if ownership != "attached" { pendingDetaches[key] = nil }
        }
    }

    /// The session is no longer attached. The hub deletes its staged
    /// files on detach, so a cached chat's picked files (even one not on
    /// screen) would name files that are gone: drop them.
    private func handedBack(_ key: String) {
        chats.first { $0.key == key }?.attachments.clear()
    }

    /// A `session.ownership` event: the owner and, with it, the channel,
    /// which decides whether the chat offers its Model menu.
    func patch(_ key: String, ownershipEvent event: OwnershipData) {
        patch(key, ownership: event.ownership)
        guard event.ownership != nil, let i = sessions.firstIndex(where: { $0.key == key }) else { return }
        sessions[i].mode = event.mode
    }

    /// A pin or rename answered or announced (`session.meta`).
    func patch(_ key: String, meta: SessionMeta) {
        guard let i = sessions.firstIndex(where: { $0.key == key }) else { return }
        sessions[i].apply(pins.overlay(meta, key: key))
    }

    // MARK: Inbox

    /// Items still waiting on you — the same set the Inbox page files
    /// under "Waiting on you", so the badge and the page always agree.
    /// The hub already dropped stale rows and this device's dismissals.
    var waitingOnYou: [InboxItem] {
        inbox.filter { InboxGrouping.section(of: $0) == .waiting }
    }

    var inboxBadge: Int { waitingOnYou.count }

    /// Dismissals posted but possibly not yet reflected in a fetched
    /// list (the same race `PinLedger` covers for pins). Mirrors the
    /// hub's rule: a fetched row whose activity stamp is at or before
    /// the dismissed stamp is filtered; one that moved past it has new
    /// activity and legitimately returns, which retires the entry.
    private var dismissals = DismissalLedger()

    /// Clears an inbox row for this device (`POST /v1/inbox/dismiss`):
    /// gone at once, and the hub keeps it out until the row's
    /// `last_active_at` moves past the stamp sent — new activity brings
    /// it back on every device. A refused post drops the ledger entry
    /// and refetches, restoring the row.
    func dismissInbox(_ item: InboxItem) async {
        inbox.removeAll { $0.key == item.key }
        dismissals.choose(item.key, stamp: item.dismissStamp)
        do {
            _ = try await onSameHub { try await $0.dismissInbox(item.key, lastActiveAt: item.dismissStamp) }
        } catch {
            dismissals.drop(item.key)
            await refresh()
        }
    }

    // MARK: Pairing

    /// A newly paired hub joins the list (or replaces the one at the same
    /// address) and goes on screen; the other hubs stay paired. False,
    /// with `pairing.error` saying so, when the Keychain wouldn't store
    /// the credential: listing the hub anyway would lose it at the next
    /// launch, so the pairing is dropped here, where it can be retried.
    @discardableResult
    func adopt(_ hub: PairedHub) -> Bool {
        guard store.saveCredential(hub) else {
            // The hub took the code; it won't take it twice.
            pairing.error = "Paired, but the Keychain wouldn't store the credential, so this phone would forget the hub. Make a new pair code on the hub and pair again."
            pairing.codeSpent = true
            return false
        }
        hubs.adopt(hub)
        store.saveIndex(hubs)
        pairing = PairingFlow()
        showHub()
        widgets?.hubChanged()
        return true
    }

    /// Puts another paired hub on screen.
    func switchHub(to id: String) {
        guard id != hub?.id, hubs.hubs.contains(where: { $0.id == id }) else { return }
        hubs.activeId = id
        store.saveIndex(hubs)
        showHub()
        widgets?.hubChanged()
    }

    /// Unpair, from the hub menu: drops the hub on screen and shows the
    /// next one, if any. The hub is told to sign this phone out as well
    /// (`POST /v1/devices/revoke`), which ends the credential and takes
    /// the phone's SSH key out of `authorized_keys`; forgetting only the
    /// local copy left both working on the hub. Best effort: a hub that
    /// can't be reached keeps the row, for Pair requests to remove.
    func unpair() {
        guard let current = hub, let client else { return }
        let revoke = revokeOnHub
        Task { await revoke(client, current.credential.deviceId) }
        forget(current)
    }

    /// The hub refused this phone's credential: drops it the same way,
    /// but only if `client` still talks to the hub on screen. A refused
    /// answer from a hub this phone has switched away from must not
    /// unpair the one it switched to.
    func unpair(from client: HubClient) {
        guard let current = hub else { return }
        if client.baseURL != current.baseURL || client.credential != current.credential.credential { return }
        forget(current)
    }

    private func forget(_ current: PairedHub) {
        store.deleteCredential(current.id)
        sshTargets.remove(current.id)
        hubs.remove(current.id)
        store.saveIndex(hubs)
        widgets?.hubChanged(dropping: current.id)
        if hub == nil {
            stopRefreshing()
            clearHubState()
        } else {
            showHub()
        }
    }

    /// Starts afresh on the hub now on screen: nothing from the last one
    /// may show, and the list loads again.
    private func showHub() {
        stopRefreshing()
        clearHubState()
        connection = .connecting
        startRefreshing()
    }

    private func clearHubState() {
        sessions = []
        machines = []
        inbox = []
        dismissals = DismissalLedger()
        loadError = nil
        refreshedAt = nil
        pendingDetaches = [:]
        pins = PinLedger()
        dropChats()
        reads.reset()
        unread = UnreadTracker()
        loadedOnce = false
    }

    /// Keeps the machine name a hub reports, so the menu can name it while
    /// another hub is on screen.
    private func rememberName(_ name: String?, of paired: PairedHub?) {
        guard let name, !name.isEmpty, let paired, hubs.names[paired.id] != name else { return }
        hubs.names[paired.id] = name
        store.saveIndex(hubs)
    }

    /// Opens pairing from the hub menu: an empty form, since the address
    /// filled in would be the hub already paired.
    func beginPairingAnother() {
        pairing = PairingFlow(host: "")
    }

    private func dropChats() {
        for chat in chats { chat.stop() }
        chats = []
    }

    /// A `swe://issue/<o>/<r>/<n>` link the tabs haven't pushed yet;
    /// MainTabs consumes it (back to nil once on the Issues stack). It
    /// survives being set before a hub is paired: the tabs take it
    /// when they appear.
    var pendingIssue: IssueLink?

    /// A `swe://session/<key>` link (the widget's rows) the tabs
    /// haven't opened yet; MainTabs consumes it like `pendingIssue`.
    var pendingSession: String?

    /// Today's spend the Usage tab loaded from `hubId`, for the widget.
    /// The widget keeps it only on that hub's snapshot.
    func publishSpend(_ usd: Double?, hubId: String) {
        widgets?.publishSpend(hubId: hubId, usd: usd)
    }

    /// A `swe://pair` (or legacy `quiver://pair`) link that came from outside the app (the Camera
    /// app, a tapped link), held until the person confirms the hub it
    /// names. Anyone can craft one, and a hub paired by it sees every
    /// prompt and approval, so nothing goes to that hub before then.
    var pairOffer: PairLink?

    /// A `swe://pair` deep link waits in `pairOffer`; a
    /// `swe://issue/…` deep link goes onto the Issues stack, and a
    /// `swe://session/…` one opens the chat, on the hub it names
    /// when that hub is paired here.
    func handle(_ url: URL) async {
        if let link = PairLink(url) {
            pairing.error = nil
            pairing.codeSpent = false
            pairOffer = link
            return
        }
        if let issue = IssueLink(url) {
            pendingIssue = issue
        }
        if let link = SessionLink(url) {
            // A hub no longer paired here: the app just opens.
            if let id = link.hubId {
                guard hubs.hubs.contains(where: { $0.id == id }) else { return }
                switchHub(to: id)
            }
            pendingSession = link.key
        }
    }

    /// Pairs with the hub `pairOffer` names, which the person confirmed.
    /// The offer stays up on failure so its error shows where they are.
    func acceptPairOffer() async {
        guard let link = pairOffer, !pairing.codeSpent else { return }
        let gen = pairingGen
        offerPairingGen = gen
        // Only this pair's own mark: a cancelled one finishing late must
        // not clear the mark of a newer offer's pair still out.
        defer { if offerPairingGen == gen { offerPairingGen = nil } }
        if await pair(with: link) { pairOffer = nil }
    }

    /// The offer's own pair, if one is out, can no longer add the hub. A
    /// Tailscale pairing under way behind the sheet carries on.
    func declinePairOffer() {
        if offerPairingGen != nil {
            offerPairingGen = nil
            pairingGen += 1
            pairing.busy = false
        }
        pairOffer = nil
        pairing.error = nil
    }

    /// The `pairingGen` an `acceptPairOffer` pair still waiting on the
    /// hub started under.
    @ObservationIgnored private var offerPairingGen: Int?

    /// A scanned QR, or a confirmed `pairOffer`. True once the hub is
    /// paired and on screen. A second call while one is out is dropped:
    /// the scanner can read the same code twice before it closes.
    @discardableResult
    func pair(with link: PairLink) async -> Bool {
        guard !pairing.busy else { return false }
        let gen = pairingGen
        pairing.busy = true
        pairing.error = nil
        pairing.codeSpent = false
        let client = HubClient(baseURL: link.baseURL, credential: nil)
        do {
            let cred = try await client.pair(token: link.token, name: link.name ?? pairing.deviceName)
            guard gen == pairingGen else { return false }
            pairing.busy = false
            return adopt(PairedHub(baseURL: link.baseURL, credential: cred))
        } catch {
            guard gen == pairingGen else { return false }
            pairing.busy = false
            pairFailed(error, link: link)
            return false
        }
    }

    /// A pair with `link` failed with `error`. The code counts as spent
    /// only for the link still on offer (or a scanned one, with no
    /// offer): a pair that was out when a fresh link replaced the offer
    /// must not take Pair away from the new token, which was never sent.
    func pairFailed(_ error: Error, link: PairLink) {
        pairing.error = error.localizedDescription
        pairing.codeSpent = PairingFlow.spendsCode(error) && (pairOffer == nil || pairOffer == link)
    }

    func pair(token: String) async {
        guard let base = hubURL(from: pairing.host) else {
            pairing.error = "Enter the hub's Tailscale name or address."
            return
        }
        let gen = pairingGen
        pairing.busy = true
        pairing.error = nil
        do {
            let cred = try await HubClient(baseURL: base, credential: nil)
                .pair(token: token.trimmingCharacters(in: .whitespaces), name: pairing.deviceName)
            guard gen == pairingGen else { return }
            pairing.busy = false
            adopt(PairedHub(baseURL: base, credential: cred))
        } catch {
            guard gen == pairingGen else { return }
            pairing.busy = false
            pairing.error = error.localizedDescription
        }
    }

    /// Tailnet-identity pairing: ask the hub for a code, then poll until
    /// another device approves or denies it.
    func startTailscalePairing() async {
        guard let base = hubURL(from: pairing.host) else {
            pairing.error = "Enter the hub's Tailscale name or address."
            return
        }
        let gen = pairingGen
        pairing.busy = true
        pairing.error = nil
        let client = HubClient(baseURL: base, credential: nil)
        do {
            let started = try await client.startPairRequest(name: pairing.deviceName)
            guard gen == pairingGen else { return }
            pairing.busy = false
            pairing.pending = PendingPairing(baseURL: base, started: started,
                                             expiresAt: Date().addingTimeInterval(TimeInterval(started.expiresInS ?? 300)))
            await pollPairing(client: client, started: started)
        } catch {
            guard gen == pairingGen else { return }
            pairing.busy = false
            pairing.error = error.localizedDescription
        }
    }

    private func pollPairing(client: HubClient, started: PairRequestStarted) async {
        while pairing.pending?.started.requestId == started.requestId {
            try? await Task.sleep(for: .seconds(2))
            guard pairing.pending?.started.requestId == started.requestId else { return }
            do {
                let poll = try await client.pollPairRequest(id: started.requestId, secret: started.pollSecret)
                // Cancelled, or replaced by a new request, while that poll
                // was out: its answer belongs to nothing on screen now.
                guard pairing.pending?.started.requestId == started.requestId else { return }
                switch poll.status {
                case "approved":
                    guard let deviceId = poll.deviceId, let credential = poll.credential else { continue }
                    if !adopt(PairedHub(baseURL: client.baseURL,
                                        credential: Credential(deviceId: deviceId, credential: credential, name: poll.name))) {
                        // Back to the form, where the error shows.
                        pairing.pending = nil
                    }
                    return
                case "denied":
                    pairing.pending?.outcome = .denied
                    return
                default:
                    if let s = poll.expiresInS { pairing.pending?.expiresAt = Date().addingTimeInterval(TimeInterval(s)) }
                }
            } catch let error as HubError where error.status == 404 {
                guard pairing.pending?.started.requestId == started.requestId else { return }
                pairing.pending?.outcome = .expired
                return
            } catch {
                // A dropped poll is retried; the countdown says when to give up.
            }
        }
    }

    /// Abandons whatever pairing is under way (Cancel, or the pair sheet
    /// swiped away): its request still out, or its approval still being
    /// polled, can no longer add a hub.
    func cancelPairing() {
        pairingGen += 1
        pairing.pending = nil
        pairing.busy = false
    }
}

struct PairingFlow {
    var host: String = KeychainHubStore.lastHost ?? ""
    /// The pair code can't pair again, so pairing needs a new one: the
    /// hub accepted it but this phone couldn't keep the credential, or
    /// the hub turned it down as expired or already used.
    var codeSpent = false
    var deviceName: String = PairingFlow.defaultDeviceName
    var busy = false
    var error: String?
    var pending: PendingPairing?

    static var defaultDeviceName: String { "iPhone" }

    /// Whether a failed pair used the code up: the hub answers
    /// `pair_failed` for a token it doesn't know or that expired, and
    /// sending it again gets the same answer. Anything else (the hub
    /// unreachable, rate limited) can work on a retry.
    static func spendsCode(_ error: Error) -> Bool {
        (error as? HubError)?.code == "pair_failed"
    }
}

struct PendingPairing {
    enum Outcome { case denied, expired }
    let baseURL: URL
    let started: PairRequestStarted
    var expiresAt: Date
    var outcome: Outcome?

    /// "482913" shown as "482 913".
    var spacedCode: String {
        let c = started.code
        guard c.count == 6 else { return c }
        return "\(c.prefix(3)) \(c.suffix(3))"
    }
}

// MARK: Grouping

/// Dismissals this phone posted but a concurrently fetched inbox list
/// may not reflect yet — the same race `PinLedger` covers for pins. An
/// entry filters fetched rows whose `lastActiveAt` has not moved past
/// the stamp posted to `POST /v1/inbox/dismiss`; a row that did move
/// past it has new activity, returns legitimately, and retires the entry.
struct DismissalLedger {
    private(set) var stamps: [String: Double] = [:]

    mutating func choose(_ key: String, stamp: Double) { stamps[key] = stamp }
    mutating func drop(_ key: String) { stamps[key] = nil }

    /// `items` minus rows a still-in-flight dismiss already cleared;
    /// entries retire once the hub ships a row newer than the stamp. A
    /// pending approval always passes: the hub ships one whatever was
    /// dismissed, and a session cleared moments ago can ask for approval
    /// without its `last_active_at` moving past the stamp.
    mutating func overlay(_ items: [InboxItem]) -> [InboxItem] {
        guard !stamps.isEmpty else { return items }
        var out: [InboxItem] = []
        out.reserveCapacity(items.count)
        for item in items {
            if item.sessionStatus == .approval { out.append(item); continue }
            guard let stamp = stamps[item.key] else { out.append(item); continue }
            if item.dismissStamp > stamp {
                stamps[item.key] = nil
                out.append(item)
            }
        }
        return out
    }
}

/// What an unread GitHub notification row says it is.
enum GitHubReason {
    static func label(_ reason: String?) -> String {
        switch reason {
        case "review_requested": "Review requested"
        case "assignee", "assigned": "Assigned to you"
        case "mention": "Mentioned"
        case "comment": "New comment"
        case "ci_fail": "CI failing"
        case nil: "GitHub"
        default: reason!.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

struct SessionGroup: Identifiable {
    let id: String
    let title: String
    let rows: [SessionRow]
}

enum SessionFilter: String, CaseIterable, Identifiable {
    case all, active, needsYou
    var id: String { rawValue }
    var title: String {
        switch self {
        case .all: "All"
        case .active: "Active"
        case .needsYou: "Needs you"
        }
    }

    func includes(_ row: SessionRow) -> Bool {
        switch self {
        case .all: true
        case .active: row.sessionStatus == .active
        case .needsYou: row.needsYouNow || (row.sessionStatus == .error && Staleness.isFresh(row.lastActiveAt))
        }
    }
}

enum SessionGrouping {
    /// Sessions active in the last day go under their project folder;
    /// everything older folds into "Earlier", newest first.
    static func groups(_ rows: [SessionRow], machines: [Machine], now: Double = Date().timeIntervalSince1970) -> [SessionGroup] {
        let recentCutoff = now - 86_400
        let sorted = rows.sorted { ($0.lastActiveAt ?? 0) > ($1.lastActiveAt ?? 0) }
        let multiMachine = Set(rows.map { $0.machineId ?? "local" }).count > 1
        // Keyed by machine and full folder; only the heading is shortened.
        var order: [String] = []
        var buckets: [String: [SessionRow]] = [:]
        var earlier: [SessionRow] = []
        for row in sorted {
            let isLive = row.sessionStatus == .active || row.needsYouNow
            guard isLive || (row.lastActiveAt ?? 0) >= recentCutoff else { earlier.append(row); continue }
            let key = "\(row.machineId ?? "local")|\(row.cwd ?? "")"
            if buckets[key] == nil { order.append(key) }
            buckets[key, default: []].append(row)
        }
        // Two folders that shorten alike ("a/projects/api", "b/projects/api")
        // keep their full paths so the headings differ.
        let short = Dictionary(grouping: order) { shortDir(buckets[$0]![0].cwd) }
        var out = order.map { key -> SessionGroup in
            let row = buckets[key]![0]
            var title = shortDir(row.cwd)
            if (short[title]?.count ?? 0) > 1 { title = abbreviateHome(row.cwd ?? title) }
            if multiMachine {
                let m = machines.first { $0.id == row.machineId }?.displayName ?? row.machineId ?? "local"
                title = "\(m) · \(title)"
            }
            return SessionGroup(id: key, title: title, rows: buckets[key]!)
        }
        if !earlier.isEmpty { out.append(SessionGroup(id: "earlier", title: "Earlier", rows: earlier)) }
        return out
    }

    static func abbreviateHome(_ path: String) -> String {
        let parts = path.split(separator: "/", omittingEmptySubsequences: false)
        // /Users/<name>/... and /home/<name>/... read as ~/...
        if parts.count > 3, parts[1] == "Users" || parts[1] == "home" {
            return "~/" + parts[3...].joined(separator: "/")
        }
        return path
    }

    /// The last two path components, as the web client shows them.
    static func shortDir(_ path: String?) -> String {
        guard let path, !path.isEmpty else { return "No folder" }
        let parts = path.split(separator: "/")
        return parts.suffix(2).joined(separator: "/")
    }
}

enum RelativeTime {
    static func short(_ ts: Double?, now: Double = Date().timeIntervalSince1970) -> String {
        guard let ts, ts > 0 else { return "" }
        let s = max(0, now - ts)
        if s < 60 { return "now" }
        if s < 3600 { return "\(Int(s / 60))m" }
        if s < 86_400 { return "\(Int(s / 3600))h" }
        return "\(Int(s / 86_400))d"
    }

    /// `short` worded to stand alone, as the web app's `Fmt.ago`:
    /// "just now", "5m ago". `short` reads "now" for the first minute,
    /// which can't take an "ago" after it. Empty for a missing time.
    static func ago(_ ts: Double?, now: Double = Date().timeIntervalSince1970) -> String {
        let s = short(ts, now: now)
        if s.isEmpty { return "" }
        return s == "now" ? "just now" : "\(s) ago"
    }
}
