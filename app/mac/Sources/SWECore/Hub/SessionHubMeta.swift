import Foundation

// The hub's pins, names, fork flag and unread flag laid over the
// Sessions list.
//
// The Mac's list comes from `swe session --json` (tokens, cost, filters),
// not from the hub. When this Mac's hub is running and the app is paired,
// `LocalHub.refreshKeys` reads `GET /v1/sessions` on the list's cadence
// anyway; each listed row whose `harness:id` key the hub also lists picks
// up the hub's pin, name and `can_fork` from that read. A row with no hub
// match, and every row while the hub is down, simply has none of them.
// The same read's `unread` and `last_active_at` feed the unread dots
// (`UnreadDots.swift`), on these rows and the Other machines group alike.
// The web app's Sessions page (#1228) and the iPhone's swipe actions call
// the same routes: `POST /v1/sessions/{key}/pin|title|fork`.

/// What `POST /v1/sessions/{key}/pin` and `/title` answer and a
/// `session.meta` event carries (`sessionmeta.fields` on the hub).
public struct HubMetaReply: Codable, Sendable, Equatable {
    public var title: String?
    public var harnessTitle: String?
    public var pinned: Bool?
    public var pinnedAt: Double?

    public init(title: String? = nil, harnessTitle: String? = nil, pinned: Bool? = nil, pinnedAt: Double? = nil) {
        self.title = title
        self.harnessTitle = harnessTitle
        self.pinned = pinned
        self.pinnedAt = pinnedAt
    }

    /// The pin as the reply states it; an absent `pinned` reads as unpinned.
    var pin: SessionPinLedger.Pin {
        SessionPinLedger.Pin(pinned: pinned == true, pinnedAt: pinned == true ? pinnedAt : nil)
    }
}

/// The hub's pin, name and fork flag for one session it lists.
public struct HubSessionMeta: Equatable, Sendable {
    /// The hub's title: the name set on the hub, else the harness's own.
    public var title: String
    /// Always the harness's own title.
    public var harnessTitle: String
    public var pinned: Bool
    /// Epoch seconds; nil when unpinned. The Pinned group sorts by it.
    public var pinnedAt: Double?
    /// The harness's CLI can fork the session.
    public var canFork: Bool
    /// The hub lists swe's read-only copy of a pruned transcript: Fork is
    /// shown off, with `SessionInfo.mirroredHint`.
    public var mirrored: Bool
    /// The hub's `unread` for this device as of the read. What a row
    /// shows also depends on the chat open here and the reads this app
    /// posted since: `LocalHub.isUnread` (`UnreadDots.swift`).
    public var unread: Bool
    /// The row's `last_active_at` (epoch seconds) from the same read,
    /// which a read posted here is weighed against.
    public var lastActiveAt: Double?

    public init(title: String = "", harnessTitle: String = "", pinned: Bool = false,
                pinnedAt: Double? = nil, canFork: Bool = false, unread: Bool = false,
                lastActiveAt: Double? = nil, mirrored: Bool = false) {
        self.mirrored = mirrored
        self.title = title
        self.harnessTitle = harnessTitle
        self.pinned = pinned
        self.pinnedAt = pinned ? pinnedAt : nil
        self.canFork = canFork && !mirrored
        self.unread = unread
        self.lastActiveAt = lastActiveAt
    }

    /// From a `/v1/sessions` row. A hub older than names sends no
    /// `harness_title`; its title then counts as the harness's own, so
    /// nothing reads as renamed.
    public init(_ row: HubSessionRow) {
        let title = row.title ?? ""
        self.init(title: title, harnessTitle: row.harnessTitle ?? title, pinned: row.pinned == true,
                  pinnedAt: row.pinnedAt, canFork: row.canFork == true, unread: row.unread == true,
                  lastActiveAt: row.lastActiveAt, mirrored: row.isMirrored)
    }

    /// A name set on the hub replaces the harness's title (the web app's
    /// `hasCustomTitle`).
    public var isRenamed: Bool { !title.isEmpty && title != harnessTitle }

    /// The title a row shows: the hub's name when one is set, else the
    /// title swe listed.
    public func title(over local: String?) -> String? { isRenamed ? title : local }

    /// A pin or rename the hub answered or announced. `can_fork` and the
    /// read state never ride one, so they stay.
    public mutating func apply(_ reply: HubMetaReply) {
        if let harness = reply.harnessTitle { harnessTitle = harness }
        // The hub's `title` is the override or the harness's own; an empty
        // one means neither is known, so the harness's title stands.
        if let new = reply.title { title = new.isEmpty ? harnessTitle : new }
        if let pin = reply.pinned {
            pinned = pin
            pinnedAt = pin ? reply.pinnedAt : nil
        }
    }
}

/// Every hub row's meta by its key, from one `/v1/sessions` read: what
/// the Sessions page lays over the rows swe listed.
public struct HubSessionOverlay: Equatable, Sendable {
    public private(set) var byKey: [String: HubSessionMeta]

    public init(_ rows: [HubSessionRow] = []) {
        byKey = Dictionary(rows.map { ($0.key, HubSessionMeta($0)) }, uniquingKeysWith: { _, newest in newest })
    }

    public var isEmpty: Bool { byKey.isEmpty }

    public subscript(key: String) -> HubSessionMeta? { byKey[key] }

    /// A Sessions row's hub key, `harness:id`; nil for a cloud row, which
    /// no hub lists.
    public static func key(_ row: SessionsModel.Row) -> String? {
        row.location == .local ? HubSessionRow.key(harness: row.session.harness, id: row.session.id) : nil
    }

    /// The hub's meta for a listed row; nil when the hub doesn't list it.
    public func meta(for row: SessionsModel.Row) -> HubSessionMeta? {
        guard !byKey.isEmpty, let key = Self.key(row) else { return nil }
        return byKey[key]
    }

    /// The title the row shows: a hub name over swe's title.
    public func title(for row: SessionsModel.Row) -> String? {
        meta(for: row).map { $0.title(over: row.session.title) } ?? row.session.title
    }

    /// Adds a row a later read of the hub found (an open chat's), or
    /// refreshes one already held: the chat's read can carry a pin or
    /// rename another device landed after this read ran, and the stream
    /// the chat joins will not send its `session.meta` again. A pin
    /// chosen here unanswered wins back in `noteRow` just after.
    mutating func adopt(_ row: HubSessionRow) {
        byKey[row.key] = HubSessionMeta(row)
    }

    /// Changes `key`'s meta in place; a key the hub didn't list stays out.
    mutating func update(_ key: String, _ change: (inout HubSessionMeta) -> Void) {
        guard var meta = byKey[key] else { return }
        change(&meta)
        byKey[key] = meta
    }
}

/// Pins chosen here that the hub may not reflect yet: the web app's
/// `pinLedger` and the iPhone's `PinLedger`. Each choice gets a version;
/// a reply counts only for the latest choice on its key, and a list
/// fetched before the latest choice was answered keeps the choice
/// instead of the hub's older answer.
public struct SessionPinLedger: Sendable {
    public struct Pin: Equatable, Sendable {
        public var pinned: Bool
        public var pinnedAt: Double?

        public init(pinned: Bool, pinnedAt: Double?) {
            self.pinned = pinned
            self.pinnedAt = pinned ? pinnedAt : nil
        }
    }

    struct Choice: Equatable, Sendable {
        var pin: Pin
        var version: Int
        /// The clock when the hub answered it; nil while it is out.
        var answeredAt: Int?
        /// The pin the hub last confirmed, which a refusal goes back to.
        var base: Pin
    }

    public private(set) var clock = 0
    private(set) var choices: [String: Choice] = [:]

    public init() {}

    /// Records a choice and returns its version. `base` is the row's pin
    /// as shown; a choice made over one still unanswered keeps that one's
    /// base instead, which is what the hub last confirmed.
    public mutating func choose(_ key: String, pinned: Bool, now: Double, base: Pin) -> Int {
        let prior = choices[key]
        clock += 1
        let kept = prior.flatMap { $0.answeredAt == nil ? $0.base : nil } ?? base
        choices[key] = Choice(pin: Pin(pinned: pinned, pinnedAt: now), version: clock, answeredAt: nil, base: kept)
        return clock
    }

    /// The hub took `version`, answering `reply`. False when a newer
    /// choice replaced it, so the reply must not land; that older answer
    /// is then what a refusal of the newer choice goes back to.
    public mutating func answered(_ key: String, version: Int, reply: Pin) -> Bool {
        guard var choice = choices[key] else { return false }
        guard choice.version == version else {
            if choice.answeredAt == nil {
                choice.base = reply
                choices[key] = choice
            }
            return false
        }
        clock += 1
        choice.pin = reply
        choice.answeredAt = clock
        choices[key] = choice
        return true
    }

    /// The hub refused `version`: the pin to put back when it was still
    /// the latest choice, else nil.
    public mutating func refused(_ key: String, version: Int) -> Pin? {
        guard let choice = choices[key], choice.version == version else { return nil }
        choices[key] = nil
        return choice.base
    }

    /// The choice on `key` the hub hasn't answered yet.
    public func held(_ key: String) -> Pin? {
        choices[key].flatMap { $0.answeredAt == nil ? $0.pin : nil }
    }

    /// Taken when a list fetch starts.
    public var mark: Int { clock }

    /// A list fetched from `mark` on: each choice the hub hadn't answered
    /// when the fetch started stays over it. Choices the fetch already
    /// reflects are forgotten.
    public mutating func overlay(_ overlay: inout HubSessionOverlay, fetchedFrom mark: Int) {
        for (key, choice) in choices {
            if let at = choice.answeredAt, at <= mark {
                choices[key] = nil
                continue
            }
            overlay.update(key) { meta in
                meta.pinned = choice.pin.pinned
                meta.pinnedAt = choice.pin.pinned ? (choice.pin.pinnedAt ?? meta.pinnedAt) : nil
            }
        }
    }
}

/// The rename sheet's rules, matching `sessionmeta.parse_title` on the
/// hub and the iPhone's `RenameRules`: one line, spaces collapsed, at
/// most 120 characters. The hub counts Unicode code points (a flag is
/// two), so these do too.
public enum SessionRename {
    public static let maxLength = 120

    /// The length the hub measures.
    public static func length(_ text: String) -> Int { text.unicodeScalars.count }

    /// What the field holds after a keystroke or paste: newlines become
    /// spaces and anything past the limit is cut.
    public static func clamp(_ text: String) -> String {
        let oneLine = text.replacingOccurrences(of: "\r\n", with: " ")
            .replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\r", with: " ")
        return prefix(oneLine, maxLength)
    }

    /// What Save sends: whitespace runs collapsed, cut to the limit,
    /// trailing space dropped, as the hub would store it.
    public static func normalized(_ text: String) -> String {
        let collapsed = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        var cut = prefix(collapsed, maxLength)
        while cut.last?.isWhitespace == true { cut.removeLast() }
        return cut
    }

    /// Save is on once the name is non-empty and differs from the current
    /// one. Clearing it goes through "Use the original name".
    public static func canSave(_ text: String, current: String?) -> Bool {
        let name = normalized(text)
        return !name.isEmpty && name != (current ?? "")
    }

    private static func prefix(_ text: String, _ n: Int) -> String {
        guard text.unicodeScalars.count > n else { return text }
        var scalars = String.UnicodeScalarView()
        scalars.append(contentsOf: text.unicodeScalars.prefix(n))
        return String(scalars)
    }
}

extension HubClient {
    /// `POST /v1/sessions/{key}/pin {pinned}`: kept on the hub, so every
    /// paired device shows it.
    public func pin(_ key: String, pinned: Bool) async throws -> HubMetaReply {
        try await send(request("POST", Self.sessionPath(key, "pin"), body: ["pinned": pinned]))
    }

    /// `POST /v1/sessions/{key}/title {title}`; "" goes back to the
    /// harness's own title.
    public func rename(_ key: String, title: String) async throws -> HubMetaReply {
        try await send(request("POST", Self.sessionPath(key, "title"), body: ["title": title]))
    }

    /// `POST /v1/sessions/{key}/fork {prompt}`: a new session from this
    /// one's history, the original left as it was. Answers the launch to
    /// poll, as a new session does.
    public func fork(_ key: String, prompt: String) async throws -> HubLaunchStatus {
        try await send(request("POST", Self.sessionPath(key, "fork"), body: ["prompt": prompt]))
    }
}

extension LocalHub {
    /// The hub's meta for `key`, as the last list read left it plus the
    /// choices made here since.
    public func meta(for key: String) -> HubSessionMeta? { overlay[key] }

    /// Pins or unpins `key` at once, then tells the hub. Returns what to
    /// say when the hub refused it and the row went back; nil otherwise,
    /// and for a key the hub doesn't list.
    @discardableResult
    public func setPinned(_ key: String, _ pinned: Bool,
                          now: Double = Date().timeIntervalSince1970) async -> String? {
        guard let shown = overlay[key] else { return nil }
        let version = pins.choose(key, pinned: pinned, now: now,
                                  base: .init(pinned: shown.pinned, pinnedAt: shown.pinnedAt))
        let pin = SessionPinLedger.Pin(pinned: pinned, pinnedAt: now)
        overlay.update(key) { meta in
            meta.pinned = pin.pinned
            meta.pinnedAt = pin.pinnedAt
        }
        do {
            let reply = try await withClient { try await $0.pin(key, pinned: pinned) }
            // A newer choice on this row is still out: this answer is stale.
            if pins.answered(key, version: version, reply: reply.pin) { noteMeta(key, reply) }
            return nil
        } catch {
            guard let base = pins.refused(key, version: version) else { return nil }
            overlay.update(key) { meta in
                meta.pinned = base.pinned
                meta.pinnedAt = base.pinnedAt
            }
            let reason = (error as? HubError)?.message ?? error.localizedDescription
            return "\(pinned ? "Couldn't pin it" : "Couldn't unpin it"). \(reason)"
        }
    }

    /// Names `key` on the hub; "" goes back to the harness's own title.
    /// Throws the hub's refusal for the sheet to show.
    public func rename(_ key: String, title: String) async throws {
        let reply = try await withClient { try await $0.rename(key, title: title) }
        noteMeta(key, reply)
    }

    /// Starts a fork of `key` with `prompt` as its first message.
    public func fork(_ key: String, prompt: String) async throws -> HubLaunchStatus {
        try await withClient { try await $0.fork(key, prompt: prompt) }
    }

    /// An open chat's own read of its row: the list's read may predate a
    /// session the hub started since, whose header still gets its menu.
    public func noteRow(_ row: HubSessionRow) {
        overlay.adopt(row)
        if let held = pins.held(row.key) {
            overlay.update(row.key) { meta in
                meta.pinned = held.pinned
                meta.pinnedAt = held.pinnedAt
            }
        }
    }

    /// A pin or rename the hub answered or announced (`session.meta`, so
    /// a rename from another device shows here), with the pin a choice
    /// still out holds.
    public func noteMeta(_ key: String, _ reply: HubMetaReply) {
        metaClock += 1
        metaLog.append((metaClock, key, reply))
        let held = pins.held(key)
        overlay.update(key) { meta in
            meta.apply(reply)
            if let held {
                meta.pinned = held.pinned
                meta.pinnedAt = held.pinnedAt
            }
        }
    }
}
