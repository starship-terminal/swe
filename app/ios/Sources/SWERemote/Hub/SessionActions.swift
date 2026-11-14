import Foundation

/// What a list row offers on a swipe or a long press. Session rows use
/// the attach/open/pin/rename/fork set, which `leading`/`trailing`/`menu`
/// build from a `SessionRow`; an issue row's only action is `resolve`,
/// which the Issues page wires itself; a task row's is `newSession`,
/// which the Tasks page wires; an inbox row's is `clear`, which the
/// Inbox page wires. Pure, so the rules for which action shows when are
/// tested without a view.
enum SessionAction: String, Hashable, Identifiable {
    case attach, detach, open, pin, unpin, rename, fork, clear, resolve, newSession

    var id: String { rawValue }

    var title: String {
        switch self {
        case .attach: "Attach"
        case .detach: "Detach"
        case .open: "Open"
        case .pin: "Pin"
        case .unpin: "Unpin"
        case .rename: "Rename"
        case .fork: "Fork"
        case .clear: "Clear"
        case .resolve: "Resolve"
        case .newSession: "New session"
        }
    }

    var symbol: String {
        switch self {
        case .attach: "link"
        case .detach: "eye"
        case .open: "text.bubble"
        case .pin: "pin"
        case .unpin: "pin.slash"
        case .rename: "pencil"
        case .fork: "arrow.triangle.branch"
        case .clear: "archivebox"
        case .resolve, .newSession: "sparkles"
        }
    }

    /// Swipe right: take the session over or hand it back, else open the
    /// chat — the edge action stays the takeover, with Open beside it on
    /// a half swipe; a row that can't attach offers Open alone.
    static func leading(_ row: SessionRow) -> [SessionAction] {
        // A mirrored copy can only be read.
        if row.isMirrored { return [.open] }
        if row.isAttached { return [.detach, .open] }
        if row.canAttach { return [.attach, .open] }
        return [.open]
    }

    /// Swipe left, the first one at the edge (a full swipe runs it).
    static func trailing(_ row: SessionRow) -> [SessionAction] {
        var out: [SessionAction] = [row.isPinned ? .unpin : .pin, .rename]
        if row.offersFork { out.append(.fork) }
        return out
    }

    /// The long-press menu: the same actions, swipe-right ones first.
    static func menu(_ row: SessionRow) -> [SessionAction] { leading(row) + trailing(row) }
}

/// How a Detach from the list goes. A row the app knows is working asks
/// first, since `now` would cut its turn. Any other row detaches with
/// `when_idle`, never `now`: the cached status can be a poll old, and the
/// hub detaches at once when it sees the session idle and waits otherwise.
enum DetachPlan: Equatable {
    case detach(DetachMode)
    case ask

    static func of(_ row: SessionRow) -> DetachPlan {
        switch row.sessionStatus {
        case .active, .approval: .ask
        default: .detach(.whenIdle)
        }
    }

    /// The modes the dialog offers, the default first.
    static let choices: [DetachMode] = [.whenIdle, .keep, .now]
}

/// The rename sheet's rules, matching `sessionmeta.parse_title` on the hub:
/// one line, spaces collapsed, at most 120 characters. The hub counts
/// Unicode code points (a flag is two, an accented letter written with a
/// combining mark is two), so these do too.
enum RenameRules {
    static let maxLength = 120

    /// The length the hub measures.
    static func length(_ text: String) -> Int { text.unicodeScalars.count }

    /// What the field holds after a keystroke or paste: newlines become
    /// spaces and anything past the limit is cut.
    static func clamp(_ text: String) -> String {
        let oneLine = text.replacingOccurrences(of: "\n", with: " ")
        return prefix(oneLine, maxLength)
    }

    /// What Save sends: runs of whitespace collapsed, cut to the limit,
    /// trailing space dropped, as the hub would store it.
    static func normalized(_ text: String) -> String {
        let collapsed = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        var cut = prefix(collapsed, maxLength)
        while cut.last?.isWhitespace == true { cut.removeLast() }
        return cut
    }

    /// Save is on once the name is non-empty and differs from the current
    /// one. Clearing the name goes through "Use the original name".
    static func canSave(_ text: String, current: String?) -> Bool {
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

/// Pins chosen on this phone that the hub may not reflect yet. Each choice
/// gets a version; a reply counts only for the latest choice on its key,
/// and a list fetched before the latest choice was answered keeps the
/// choice instead of the hub's older answer.
struct PinLedger {
    struct Choice: Equatable {
        var pinned: Bool
        var pinnedAt: Double?
        var version: Int
        /// The clock when the hub answered it; nil while it is out.
        var answeredAt: Int?
    }

    private(set) var clock = 0
    private(set) var choices: [String: Choice] = [:]

    /// Records a choice; returns its version.
    mutating func choose(_ key: String, pinned: Bool, now: Double) -> Int {
        clock += 1
        choices[key] = Choice(pinned: pinned, pinnedAt: pinned ? now : nil, version: clock, answeredAt: nil)
        return clock
    }

    /// The hub took `version`. False when a newer choice replaced it, so
    /// its reply must not be applied.
    mutating func answered(_ key: String, version: Int) -> Bool {
        guard choices[key]?.version == version else { return false }
        clock += 1
        choices[key]?.answeredAt = clock
        return true
    }

    /// The hub refused `version`. True when it was still the latest
    /// choice, so the row goes back.
    mutating func refused(_ key: String, version: Int) -> Bool {
        guard choices[key]?.version == version else { return false }
        choices[key] = nil
        return true
    }

    /// Mark taken when a list fetch starts.
    var mark: Int { clock }

    /// A list fetched from `mark` on: rows whose latest choice was not yet
    /// answered when the fetch started keep that choice. Choices the
    /// fetch already reflects are forgotten.
    mutating func overlay(_ rows: [SessionRow], fetchedFrom mark: Int) -> [SessionRow] {
        var out = rows
        for (key, choice) in choices {
            if let at = choice.answeredAt, at <= mark {
                choices[key] = nil
                continue
            }
            guard let i = out.firstIndex(where: { $0.key == key }) else { continue }
            out[i].pinned = choice.pinned
            out[i].pinnedAt = choice.pinned ? (choice.pinnedAt ?? out[i].pinnedAt) : nil
        }
        return out
    }

    /// A `session.meta` event, with the pin a still-unanswered choice holds.
    func overlay(_ meta: SessionMeta, key: String) -> SessionMeta {
        guard let choice = choices[key], choice.answeredAt == nil else { return meta }
        var m = meta
        m.pinned = choice.pinned
        m.pinnedAt = choice.pinnedAt
        return m
    }
}

extension SessionGrouping {
    /// Pinned rows first, in the order they were pinned, then the usual
    /// folder groups without them.
    static func groupsWithPins(_ rows: [SessionRow], machines: [Machine],
                               now: Double = Date().timeIntervalSince1970) -> [SessionGroup] {
        let pinned = rows.filter(\.isPinned).sorted {
            ($0.pinnedAt ?? .infinity, $0.key) < ($1.pinnedAt ?? .infinity, $1.key)
        }
        let rest = groups(rows.filter { !$0.isPinned }, machines: machines, now: now)
        guard !pinned.isEmpty else { return rest }
        return [SessionGroup(id: "pinned", title: "Pinned", rows: pinned)] + rest
    }

    /// Where a pinned row runs, since no folder heading says it: the
    /// folder, after the machine's name when sessions come from more
    /// than one machine.
    static func pinnedPlace(_ row: SessionRow, rows: [SessionRow], machines: [Machine]) -> String {
        let folder = shortDir(row.cwd)
        guard Set(rows.map { $0.machineId ?? "local" }).count > 1 else { return folder }
        let machine = machines.first { $0.id == row.machineId }?.displayName ?? row.machineId ?? "local"
        return "\(machine) · \(folder)"
    }
}
