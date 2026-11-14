import Foundation

/// Wire types for the hub's `/v1` routes. Field names match
/// `src/swe/remote/protocol.py` and `catalog.py`; every field the hub
/// may omit or null is optional so a newer hub never breaks decoding.

struct Machine: Codable, Identifiable, Hashable {
    let id: String
    let name: String?
    let os: String?
    let online: Bool?
    let lastSeenAt: Double?
    let harnesses: [String]?
    /// What `POST /v1/sessions` accepts on this machine; nil from a hub
    /// or satellite older than the new-session route.
    var launchable: [String]? = nil
    /// Each launchable harness's registry name, e.g. "Qwen Code".
    var harnessNames: [String: String]? = nil
    /// Launchable harnesses swe can't read sessions of: they start, but
    /// no chat ever opens.
    var untrackedHarnesses: [String]? = nil
    /// Harnesses whose launch honours a `model` pick. Absent on a
    /// satellite older than the field: it would accept the pick and
    /// silently drop it, so the launch sheets hide the Model row rather
    /// than offer one the machine can't apply.
    var modelAtLaunch: [String]? = nil
    /// The same gate for `effort`; decoded now so a later row can use it.
    var effortAtLaunch: [String]? = nil
    /// Harnesses whose launch honours `yolo`, the approvals-off switch.
    /// Absent on a satellite older than the field: it would accept the
    /// flag and silently run the usual checks, so the launch sheets
    /// hide the Autonomy row rather than offer one that can't apply.
    var yoloAtLaunch: [String]? = nil
    /// Per harness, the provider slugs its launch takes whose key is set
    /// on this machine (slugs only, never a key). Absent on a satellite
    /// older than the field: it would ignore the pick, so the launch
    /// sheets hide the Provider row.
    var providersAtLaunch: [String: [String]]? = nil
    /// Each offered provider slug's label, e.g. "OpenRouter".
    var providerNames: [String: String]? = nil
    /// The machine ledger's most recent model per harness, the head of
    /// each `recentModels` list.
    var lastModels: [String: String]? = nil
    /// Up to five recent models per harness, newest first: the Model
    /// menu's entries. Absent on a hub older than the field, where the
    /// menu falls back to `lastModels`.
    var recentModels: [String: [String]]? = nil

    /// "dev-macbook-pro.local" reads as "dev-macbook-pro".
    var displayName: String {
        let n = name ?? id
        return n.hasSuffix(".local") ? String(n.dropLast(6)) : n
    }
}

/// The session list's own freshness window, the same 3 days as the
/// hub's `INBOX_MAX_AGE_S` on `catalog.inbox`. Session rows carry no
/// hub-computed age flag, so the Sessions page keeps applying this to
/// `last_active_at`: a follow-up or error past it still lists, but not
/// in amber or under "Needs you". The Inbox itself no longer re-ages
/// anything: the hub cut off and undismissed rows before they shipped.
enum Staleness {
    static let window: Double = 3 * 86_400
    static func isFresh(_ ts: Double?, now: Double = Date().timeIntervalSince1970) -> Bool {
        (ts ?? 0) >= now - window
    }
}

/// A session's lifecycle as the hub reports it. Unknown values from a
/// newer hub land in `.other` rather than failing the whole list.
enum SessionStatus: Hashable {
    case active, done, followup, error, interrupted, approval, idle
    case other(String)

    init(raw: String?) {
        switch raw ?? "" {
        case "active": self = .active
        case "done": self = .done
        case "followup": self = .followup
        case "error": self = .error
        case "interrupted": self = .interrupted
        case "approval": self = .approval
        case "": self = .idle
        case let s: self = .other(s)
        }
    }

    var label: String {
        switch self {
        case .active: "active"
        case .done: "done"
        case .followup: "needs you"
        case .error: "error"
        case .interrupted: "interrupted"
        case .approval: "approval needed"
        case .idle: "idle"
        case .other(let s): s
        }
    }

    /// Waiting on the person holding the phone.
    var needsYou: Bool { self == .followup || self == .approval }
}

struct SessionRow: Codable, Identifiable, Hashable {
    let key: String
    let harness: String?
    let agent: String?
    let cwd: String?
    /// The name you gave it on the hub, else the harness's own title.
    var title: String?
    let lastActiveAt: Double?
    var status: String?
    let machineId: String?
    var ownership: String?
    let attachable: Bool?
    /// A satellite's session, which the hub forwards to that machine.
    var remote: Bool? = nil
    /// The harness's own title, even when `title` is a name you set.
    var harnessTitle: String? = nil
    var pinned: Bool? = nil
    /// When it was pinned (epoch seconds): the Pinned section's order.
    var pinnedAt: Double? = nil
    /// The harness can start a fork of this session.
    var canFork: Bool? = nil
    /// The model of the newest reply in the transcript, as the harness
    /// names it ("claude-opus-4-5-20251101", "anthropic/claude-opus-4-5").
    /// Nil when the hub doesn't know it, or is older than the field.
    var model: String? = nil
    /// On an attached row, the supervised channel: tmux, headless,
    /// serve or appserver.
    var mode: String? = nil
    /// The channels (`mode` values) in which the hub can switch this
    /// harness's model: `["tmux"]` for Claude Code and Codex today.
    /// Nil on a hub or satellite older than the field.
    var modelModes: [String]? = nil
    /// The hub's unread flag for this device: the row moved since this
    /// phone last read it (`POST .../read`). Nil on a hub older than the
    /// field, which reads as read. `UnreadTracker` decides the dot.
    var unread: Bool? = nil
    /// The harness deleted the transcript and swe lists its read-only
    /// copy (#1918). Nil on a hub older than the field, which reads as
    /// false (`isMirrored`).
    var mirrored: Bool? = nil

    var id: String { key }
    /// The hub's `mirrored` flag; a hub older than the field, or a row
    /// without it, reads as false. Here, not beside `Mirrored`, because the
    /// share and widget targets compile this file but not that one.
    var isMirrored: Bool { mirrored == true }
    /// The chat offers its Model menu: attached through a channel whose
    /// `model` control op works. Everywhere else the op is refused, so
    /// the menu is hidden rather than offered.
    var canSwitchModel: Bool {
        guard isAttached, let mode else { return false }
        return modelModes?.contains(mode) == true
    }
    /// `model` short enough for a header or a list row, or nil.
    var modelLabel: String? { ModelLabel.short(model) }
    var displayTitle: String { (title?.isEmpty == false ? title : nil) ?? "Untitled session" }
    var isPinned: Bool { pinned == true }
    /// A name set on the hub replaces the harness's title.
    var hasCustomTitle: Bool {
        guard let harnessTitle, let title, !title.isEmpty else { return false }
        return title != harnessTitle
    }

    /// The pin and name the hub answered or announced, laid over this row.
    mutating func apply(_ meta: SessionMeta) {
        title = meta.title
        if let h = meta.harnessTitle { harnessTitle = h }
        pinned = meta.pinned
        pinnedAt = meta.pinned == true ? meta.pinnedAt : nil
    }
    var harnessName: String { agent ?? harness ?? "agent" }
    var sessionStatus: SessionStatus { SessionStatus(raw: status) }
    /// Mirrored wins over ownership: a copy of a deleted transcript is
    /// never attached, whatever a stale ownership event says.
    var isAttached: Bool { ownership == "attached" && !isMirrored }
    var canAttach: Bool { !isMirrored && !isAttached && attachable == true && sessionStatus != .active }
    /// Fork is offered: the harness can, and there is a live transcript.
    var offersFork: Bool { canFork == true && !isMirrored }
    /// The hub can hold a send until this session goes idle. A satellite
    /// delivers on arrival or refuses a busy session, so it can't.
    var holdsQueuedSends: Bool { remote != true }

    /// Waiting on you right now: an approval, or a recent follow-up.
    var needsYouNow: Bool {
        sessionStatus == .approval || (sessionStatus == .followup && Staleness.isFresh(lastActiveAt))
    }
}

/// How a session's model reads on screen.
enum ModelLabel {
    /// The model id without its provider path or date stamp:
    /// "anthropic/claude-opus-4-5" and "claude-opus-4-5-20251101" both
    /// read "claude-opus-4-5", "openrouter/nvidia/nemotron-3:free" reads
    /// "nemotron-3:free". Nil for a missing or blank one, so the views
    /// show nothing rather than a placeholder.
    static func short(_ raw: String?) -> String? {
        guard let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else { return nil }
        var name = trimmed.split(separator: "/").last.map(String.init) ?? trimmed
        if let dash = name.lastIndex(of: "-"), dash > name.startIndex {
            let stamp = name[name.index(after: dash)...]
            if stamp.count == 8, stamp.allSatisfy(\.isASCIIDigit) {
                name = String(name[..<dash])
            }
        }
        return name
    }
}

private extension Character {
    var isASCIIDigit: Bool { ("0"..."9").contains(self) }
}

/// The title of a thinking card. The Mac's `ThoughtLabel` (SWECore,
/// `Hub/ThoughtLabel.swift`) and the web app's `Fmt.thought`
/// (`static/format.js`) follow the same rules; change all three together.
enum ThoughtLabel {
    /// "Thought for 12s", "Thought for 1m 5s", "Thought for 1h 2m" from
    /// the hub's `duration_s`, rounded to the second and never under 1s.
    /// Plain "Thought" when the hub sent no usable duration.
    static func text(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "Thought" }
        let total = max(1, Int(seconds.rounded()))
        let (h, m, s) = (total / 3600, total % 3600 / 60, total % 60)
        if h > 0 { return m > 0 ? "Thought for \(h)h \(m)m" : "Thought for \(h)h" }
        if m > 0 { return s > 0 ? "Thought for \(m)m \(s)s" : "Thought for \(m)m" }
        return "Thought for \(s)s"
    }
}

/// `POST .../pin` and `.../title` answer this; `session.meta` carries it.
struct SessionMeta: Codable, Equatable {
    var title: String?
    var harnessTitle: String?
    var pinned: Bool?
    var pinnedAt: Double?
}

/// `POST /v1/sessions/{key}/read` answers the cursor it stored.
struct ReadAck: Codable, Equatable {
    var ok: Bool?
    /// This device's last-read cursor now (epoch seconds): the time
    /// asked for, capped by the hub, and never earlier than before.
    var at: Double?
}

struct SessionsResponse: Codable {
    let sessions: [SessionRow]
    let machines: [Machine]?
    let serverTime: Double?
}

/// One `GET /v1/inbox` row. The hub applies its cutoff and this
/// device's dismissals before these ship: approvals always arrive,
/// follow-ups, errors and interrupts only while fresh, `done` rows
/// (finished since the hub's midnight) only on `?done=1`. A row that
/// would be dismissed or stale never reaches the phone.
struct InboxItem: Codable, Identifiable, Hashable {
    let key: String
    let machineId: String?
    let harness: String?
    let agent: String?
    let title: String?
    let cwd: String?
    let status: String?
    let lastActiveAt: Double?
    let requestId: String?
    /// "github" on an unread GitHub notification row; session rows omit it.
    var kind: String? = nil
    /// GitHub rows: "o/r", the reason word ("review_requested", …) and
    /// the thread URL to open.
    var repo: String? = nil
    var reason: String? = nil
    var url: String? = nil

    var id: String { key }
    var displayTitle: String { (title?.isEmpty == false ? title : nil) ?? "Untitled session" }
    var harnessName: String { agent ?? harness ?? "agent" }
    var sessionStatus: SessionStatus { SessionStatus(raw: status) }
    /// An unread GitHub thread (`gh:<thread id>`): no chat opens, the
    /// row opens `url` instead.
    var isGitHub: Bool { kind == "github" || status == "github" }
    /// What a dismiss posts back as `last_active_at`.
    var dismissStamp: Double { lastActiveAt ?? 0 }
}

struct InboxResponse: Codable { let items: [InboxItem] }

/// The Inbox page's sections. The hub decides which rows ship — an
/// approval at any age, fresh follow-ups and errors, `done` rows when
/// asked, unread GitHub threads — so this only sorts what arrived. An
/// unfamiliar status lands in Waiting on you rather than vanishing.
enum InboxGrouping {
    /// The section a row belongs in, in page order.
    enum Section: CaseIterable {
        case waiting, errors, github, finished
        var title: String {
            switch self {
            case .waiting: "Waiting on you"
            case .errors: "Errors"
            case .github: "GitHub"
            case .finished: "Finished today"
            }
        }
        /// The "Clear …" menu item: "Clear errors", "Clear GitHub".
        var clearTitle: String {
            switch self {
            case .waiting: "waiting on you"
            case .errors: "errors"
            case .github: "GitHub"
            case .finished: "finished"
            }
        }
    }

    static func section(of item: InboxItem) -> Section {
        if item.isGitHub { return .github }
        switch item.sessionStatus {
        case .error, .interrupted: return .errors
        case .done: return .finished
        default: return .waiting
        }
    }

    /// `items` bucketed by section, sections in page order.
    static func groups(_ items: [InboxItem]) -> [(Section, [InboxItem])] {
        var grouped: [Section: [InboxItem]] = [:]
        for item in items { grouped[section(of: item), default: []].append(item) }
        return Section.allCases.compactMap { s in grouped[s].map { (s, $0) } }
    }
}

struct ToolCall: Codable, Hashable {
    let name: String
    let callId: String?
    let argsSummary: String?
    let argsFull: String?
}

/// `data` of a `message.appended` event.
struct MessageData: Codable, Hashable {
    let kind: String?
    let text: String?
    let ts: Double?
    let tool: ToolCall?
    let resultRef: String?
    /// How long a `thinking` message's model thought, in seconds; nil
    /// when the hub could not tell (#1440).
    var durationS: Double? = nil
}

/// The hub's event envelope. `data` is kept raw because its shape
/// depends on `type`; `decode(_:)` reads it as the type that fits.
struct Envelope: Decodable {
    let type: String
    let session: String?
    let seq: Int?
    let ts: Double?
    let data: JSONValue?

    func decode<T: Decodable>(_: T.Type) -> T? {
        guard let data else { return nil }
        return try? Hub.decoder.decode(T.self, from: Hub.encoder.encode(data))
    }
}

struct MessagesResponse: Decodable {
    let session: String?
    let messages: [Envelope]
    let nextBefore: Int?
    let hasMore: Bool?
}

struct StatusData: Decodable { let status: String? }
struct OwnershipData: Decodable {
    let ownership: String?
    /// The channel an attach took (nil once observed again).
    var mode: String? = nil
}
/// One numbered row of the menu an agent's pane prints for an approval
/// ("1. Yes", "2. Yes, and don't ask again", ...), as the hub's tailer
/// sends it on `approval.requested` (`options: [{n, label}]`). The row's
/// number is itself a decision the hub accepts
/// (`protocol.parse_approval_command`).
struct ApprovalOption: Decodable, Hashable, Identifiable {
    let n: String
    let label: String
    var id: String { n }

    init(n: String, label: String) {
        self.n = n
        self.label = label
    }

    private enum CodingKeys: String, CodingKey { case n, label }

    /// `n` is a digit string from the pane; a number is read as one too.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let text = try? c.decode(String.self, forKey: .n) {
            n = text
        } else {
            n = String(try c.decode(Int.self, forKey: .n))
        }
        label = (try? c.decode(String.self, forKey: .label)) ?? ""
    }
}

struct ApprovalRequested: Decodable {
    let requestId: String?
    let prompt: String?
    /// The pane's numbered menu rows, when the hub read any.
    let options: [ApprovalOption]?
}
struct ApprovalResolved: Decodable { let requestId: String?; let decision: String?; let source: String? }
struct CommandResult: Decodable {
    let op: String?
    let ok: Bool?
    let note: String?
    let degraded: Bool?
    let clientId: String?
    let requestId: String?
    /// `when_idle` on a detach's result.
    var mode: String? = nil
    /// A queued send removed before it went out (`POST .../queue` cancel):
    /// `ok` is false, but nothing failed.
    var cancelled: Bool? = nil
}

/// `/v1/pair` and the approved pair-request poll both answer with this.
struct Credential: Codable, Hashable {
    let deviceId: String
    let credential: String
    let name: String?
}

struct PairRequestStarted: Decodable {
    struct Node: Decodable { let name: String?; let login: String? }
    let requestId: String
    let pollSecret: String
    let code: String
    let expiresInS: Int?
    let node: Node?
}

struct PairPoll: Decodable {
    let status: String
    let expiresInS: Int?
    let deviceId: String?
    let credential: String?
    let name: String?
}

struct PendingPairRequest: Decodable, Identifiable, Hashable {
    let requestId: String
    let name: String?
    let nodeName: String?
    let login: String?
    let createdAt: Double?
    let expiresInS: Int?
    var id: String { requestId }

    /// What the Devices page draws for it. The hub counts `expiresInS`
    /// down on every answer, so comparing whole requests would call an
    /// unchanged list new on every poll.
    var drawn: Drawn { Drawn(requestId: requestId, name: name, nodeName: nodeName, login: login, createdAt: createdAt) }

    struct Drawn: Equatable {
        let requestId: String
        let name: String?
        let nodeName: String?
        let login: String?
        let createdAt: Double?
    }
}

struct PairRequestsResponse: Decodable { let requests: [PendingPairRequest] }

/// One `GET /v1/devices` row: the hub's `device_view` of a pairing —
/// every field but `id` optional, since a device paired before a field
/// existed (or a browser that couldn't report it) just lacks it.
struct PairedDevice: Decodable, Identifiable, Hashable {
    let id: String
    /// The name given at pairing.
    let name: String?
    /// A name set later with `POST /v1/devices/rename`; wins over `name`.
    var displayName: String? = nil
    /// iphone | ipad | mac | browser | android | windows | linux | unknown
    var kind: String? = nil
    /// Hardware model a native client reported ("iPhone16,2").
    var model: String? = nil
    /// "SWE Remote 1.0", "Safari 18", …
    var client: String? = nil
    var os: String? = nil
    /// The Tailscale node the pairing request came from.
    var nodeName: String? = nil
    /// phone | satellite; rows minted before roles count as phones.
    var role: String? = nil
    /// A satellite's machine id, when the hub bound one.
    var machineId: String? = nil
    var pairedVia: String? = nil
    var createdAt: Double? = nil
    /// Refreshed on every authenticated request.
    var lastSeenAt: Double? = nil
    var revoked: Bool? = nil
    /// Sent when the route marks the caller's own row.
    var thisDevice: Bool? = nil

    var label: String {
        if let displayName, !displayName.isEmpty { return displayName }
        return name?.isEmpty == false ? name! : "Device"
    }
    var isSatellite: Bool { role == "satellite" || machineId != nil }
    /// The SF Symbol for its kind; a satellite reads as a computer.
    var symbol: String {
        if isSatellite { return "desktopcomputer" }
        switch kind {
        case "iphone": return "iphone"
        case "ipad": return "ipad"
        case "mac": return "macbook"
        case "browser": return "globe"
        case "android", "windows", "linux": return "desktopcomputer"
        default: return "iphone"
        }
    }
}

struct DevicesResponse: Decodable { let devices: [PairedDevice] }

/// The hub's detach modes (`POST .../control {op: "detach", mode}`).
enum DetachMode: String, CaseIterable {
    /// Close tmux and the harness at once, even mid-turn.
    case now
    /// Wait for the turn to end, then close it.
    case whenIdle = "when_idle"
    /// Stop supervising but leave tmux running on the machine.
    case keep
}

struct SendAccepted: Decodable { let ok: Bool?; let waiting: Bool? }

/// One queue-mode send the hub still holds until the session goes idle,
/// from `GET /v1/sessions/{key}/queue`.
struct WaitingSend: Decodable, Equatable {
    let clientId: String
    let text: String?
    /// Attachment refs as sent; kept raw, so an odd entry can't fail the list.
    var attachments: [JSONValue]? = nil
    var queuedAt: Double? = nil

    /// The refs that are strings, for the tray row's file count.
    var attachmentRefs: [String] {
        (attachments ?? []).compactMap { if case .string(let s) = $0 { s } else { nil } }
    }
}

struct QueueResponse: Decodable { let sends: [WaitingSend] }

/// `GET /v1/folders`: one folder's subfolders on the hub machine.
struct FolderListing: Decodable, Equatable {
    struct Entry: Decodable, Equatable, Identifiable, Hashable {
        let name: String
        let path: String
        var id: String { path }
    }
    let path: String
    let parent: String?
    let folders: [Entry]
    let truncated: Bool?
}

/// `POST /v1/sessions` and `GET /v1/launches/<id>` both answer with this.
/// `key` is set once `state` is "started".
struct LaunchStatus: Decodable, Equatable {
    let launchId: String
    let state: String?
    let key: String?
    let note: String?
    let queued: Bool?
}

struct HubErrorBody: Decodable {
    struct Inner: Decodable { let code: String?; let message: String? }
    let error: Inner
}

/// Minimal JSON tree so envelopes can carry any `data` shape.
enum JSONValue: Codable, Hashable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }
}

/// One `/v1/search` result: the catalog row, plus the best transcript
/// snippet when the hit came from the app's search index.
struct SearchResult: Decodable, Identifiable, Hashable {
    let key: String
    let source: String?
    let snippet: String?
    let markedSnippet: String?
    let role: String?
    let session: SessionRow
    var id: String { key }
}

struct SearchResponse: Decodable {
    let results: [SearchResult]
    let indexed: Bool?
}

// MARK: Pull requests (GET /v1/pulls…)

/// What the hub's `gh` probe says: the `github` field every pulls
/// reply carries.
struct GhEnv: Decodable {
    let status: String?
    let login: String?
    let error: String?
}

/// `GET /v1/pulls`: each pinned repo's open pull requests.
struct PullsResponse: Decodable {
    let repos: [PullRepo]
    let machine: String?
    /// The hub's machine id, so Open in session offers its harnesses.
    let machineId: String?
    let github: GhEnv?
    /// Set when `gh` is missing or signed out on the hub: the page
    /// shows this instead of the list.
    let envBlocked: String?
    let fetchedAt: Double?
}

/// One pinned repo's entry in the pulls reply. A repo `gh` failed on
/// lists with `status` and `error` and an empty `pulls`.
struct PullRepo: Decodable, Identifiable {
    let name: String
    let status: String?
    let pulls: [PullRow]?
    let error: String?
    var id: String { name }
}

/// One open pull request's list row (`gh pr list`, shaped on the hub).
struct PullRow: Decodable, Identifiable {
    let number: Int
    let title: String?
    let state: String?
    let draft: Bool?
    let author: String?
    /// The head branch (`headRefName` upstream).
    let branch: String?
    let base: String?
    let labels: [String]?
    let mergeable: String?
    /// approved | changes_requested | review_required | none
    let reviewDecision: String?
    /// passing | failing | running | unknown | none — the rollup the
    /// checks glyph colours by.
    let checks: String?
    /// ISO-8601 as `gh` prints it; `GitHubTime.date` parses it.
    let updatedAt: String?
    let url: String?
    /// The hub's `gh` login authored it (the Mine filter).
    let mine: Bool?
    /// The hub's `gh` login was asked to review. Null when the hub
    /// couldn't ask `gh pr status` — the row still lists.
    let reviewRequested: Bool?

    var id: Int { number }
}

/// `GET /v1/pulls/<o>/<r>/<n>`: the list row plus the body, per-check
/// rows and reviews the detail page draws.
struct PullResponse: Decodable {
    let github: GhEnv?
    let repo: String?
    let machine: String?
    let machineId: String?
    /// "ok", or `gh`'s outcome word ("not-found", "auth-required", …).
    let status: String?
    let pr: PullDetail?
    let error: String?
    let fetchedAt: Double?
}

struct PullDetail: Decodable {
    let number: Int
    let title: String?
    let state: String?
    let draft: Bool?
    let author: String?
    let branch: String?
    let base: String?
    let labels: [String]?
    let mergeable: String?
    let reviewDecision: String?
    let checks: String?
    let updatedAt: String?
    let url: String?
    let repo: String?
    let body: String?
    let mergeState: String?
    let additions: Int?
    let deletions: Int?
    let changedFiles: Int?
    let checkRuns: [CheckRun]?
    let reviews: [PullReview]?
    let reviewRequests: [String]?
    let createdAt: String?
    let mergedAt: String?
    /// Set once auto-merge is armed: GitHub merges when checks pass.
    let autoMerge: PullAutoMerge?
    /// Where the hub's `pr_worktree` puts the checkout — the open
    /// sheet's "Starts in" line before the worktree exists.
    let worktree: String?
    /// The review/fix brief the Open-in-session sheet prefills.
    let suggestedPrompt: String?
}

/// One `statusCheckRollup` entry, folded to `checks`' state words.
/// Not `Identifiable`: several providers can report the same
/// `workflow/name`, so the fields can't key a `ForEach` — the detail
/// page keys rows by their slot in the reply.
struct CheckRun: Decodable, Hashable {
    let name: String?
    let workflow: String?
    let status: String?
    let conclusion: String?
    /// passing | failing | running | none
    let state: String?
    let url: String?
}

/// A PR's armed auto-merge: `method` is squash, rebase or merge.
struct PullAutoMerge: Decodable, Hashable {
    let method: String?
    let enabledAt: String?
}

/// One `latestReviews` entry; `review_requests` decodes as names.
struct PullReview: Decodable, Hashable {
    let author: String?
    /// approved | changes_requested | commented | dismissed | …
    let state: String?
    let submittedAt: String?
}

// MARK: Issues (GET /v1/issues…)

/// `GET /v1/issues`: each pinned repo's open issues.
struct IssuesResponse: Decodable {
    let repos: [IssueRepo]
    let machine: String?
    /// The hub's machine id, so the resolve sheet offers its harnesses.
    let machineId: String?
    let github: GhEnv?
    /// Set when `gh` is missing or signed out on the hub: the page
    /// shows this instead of the list.
    let envBlocked: String?
    let fetchedAt: Double?
}

/// One pinned repo's entry in the issues reply. A repo `gh` failed on
/// lists with `status` and `error` and an empty `issues`.
struct IssueRepo: Decodable, Identifiable {
    let name: String
    let status: String?
    let issues: [IssueRow]?
    let error: String?
    var id: String { name }
}

/// One open issue's list row (`gh issue list`, shaped on the hub).
struct IssueRow: Decodable, Identifiable {
    let number: Int
    let title: String?
    /// "open" on the default list; the Closed chip's `?state=closed`
    /// read brings "closed" rows.
    let state: String?
    let author: String?
    let labels: [String]?
    let assignees: [String]?
    /// The count, not the comments themselves.
    let comments: Int?
    /// ISO-8601 as `gh` prints it; `GitHubTime.date` parses it.
    let updatedAt: String?
    let url: String?
    /// The hub's `gh` login authored it (the Mine filter).
    let mine: Bool?
    /// Sent only when the hub knows an agent is already on the issue;
    /// absent on every older hub — the chip shows on `true` only.
    let agentRunning: Bool?

    var id: Int { number }
}

/// `GET /v1/issues/<o>/<r>/<n>`: one issue in full — the body, comments
/// and the resolve fields the detail page and its sheet draw.
struct IssueResponse: Decodable {
    let github: GhEnv?
    let repo: String?
    let machine: String?
    let machineId: String?
    /// "ok", or `gh`'s outcome word ("not-found", "auth-required", …).
    let status: String?
    let issue: IssueDetail?
    let error: String?
    let fetchedAt: Double?
}

struct IssueDetail: Decodable {
    let number: Int
    let title: String?
    let state: String?
    let author: String?
    let labels: [String]?
    let assignees: [String]?
    let url: String?
    let body: String?
    let createdAt: String?
    let updatedAt: String?
    let comments: [IssueComment]?
    /// `swe/issue-<n>`: the branch a resolve works on.
    let branch: String?
    /// Where the hub's `issue_worktree` puts the checkout — the resolve
    /// sheet's "Starts in" line before the worktree exists.
    let worktree: String?
    /// The resolve brief the sheet's prompt field prefills.
    let suggestedPrompt: String?
}

/// One issue comment; the detail page renders them under the body.
struct IssueComment: Decodable, Hashable {
    let author: String?
    let body: String?
    let createdAt: String?
    let url: String?
}

/// `POST /v1/issues/comment`: the comment as posted.
struct IssueCommentReply: Decodable {
    let ok: Bool?
    let repo: String?
    let number: Int?
    let comment: IssueComment?
}

// MARK: Tasks (GET /v1/tasks…)

/// `GET /v1/tasks`: the hub's work-item store over the sources' open
/// items — GitHub pinned-repo issues and PRs, Linear cards and Notion
/// pages when a key is configured — each row carrying the store's
/// session/launch links.
struct TasksResponse: Decodable {
    let tasks: [TaskRow]
    /// Per-repo fetch failures, the same shape `/v1/pulls` reports;
    /// absent when every repo listed fine.
    let repos: [TaskRepoFailure]?
    let machine: String?
    /// The hub's machine id, so the launch sheet offers its harnesses.
    let machineId: String?
    let github: GhEnv?
    /// Linear's env block, shaped like `github`'s: `disabled` when no
    /// `linear` section is configured, a blocking word with `error`
    /// when the source can't be reached.
    let linear: GhEnv?
    /// Notion's env block, the same shape: `disabled` when no `notion`
    /// section is configured.
    let notion: GhEnv?
    /// Set when `gh` is missing or signed out on the hub. Stored GitHub
    /// tasks still list, marked stale.
    let envBlocked: String?
    let fetchedAt: Double?
}

/// One pinned repo's failed fetch inside the tasks reply: its stored
/// rows still list under their group.
struct TaskRepoFailure: Decodable, Identifiable, Hashable {
    let name: String
    let status: String?
    let error: String?
    var id: String { name }
}

/// The PR fields a task row carries: for an issue the PR its
/// `swe/issue-<n>` branch produced (open or merged), for a pull
/// the PR itself.
struct TaskPr: Decodable, Hashable, Sendable {
    let number: Int?
    let title: String?
    let state: String?
    let draft: Bool?
    let url: String?
}

/// One work item: the TaskRef fields plus the derived `state` ladder
/// (`open` → `in_progress` → `pr_open` → `merged`|`closed`), the
/// store's `sessions`/`launches` links and `agent_running` while a
/// linked session is active or a launch still pending.
struct TaskRow: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    /// github | linear | notion
    let source: String?
    /// issue | pull | card | page — GitHub carries issue and pull only.
    let kind: String?
    /// The source's own id: "5" for GitHub, "QUI-123" for Linear.
    let ref: String?
    let title: String?
    let url: String?
    /// `owner/repo` for GitHub.
    let repo: String?
    /// The team/project key for non-GitHub sources.
    let project: String?
    let state: String?
    /// Session keys linked to this task.
    let sessions: [String]?
    /// Launch ids still preparing or already adopted.
    let launches: [String]?
    /// True while a linked session is `active` or a launch is pending.
    let agentRunning: Bool?
    /// The store has links on this task.
    let linked: Bool?
    let lastActiveAt: Double?
    let createdAt: Double?
    /// The source's own stamp — ISO-8601 from GitHub and Linear both.
    let updatedAt: String?
    let pr: TaskPr?
    /// What the row says, in the source's own words ("Agent running",
    /// "PR open", a Linear workflow state's name). `state` is the
    /// ladder the list sorts and filters by, never shown as text.
    /// A `var` with a default so a hand-built row (tests, previews) can
    /// leave it out; decoding still reads it.
    var status: String? = nil

    /// The list's group key: the repo for GitHub, the project elsewhere;
    /// empty when the row names neither.
    var scope: String { [repo, project].compactMap { $0 }.first { !$0.isEmpty } ?? "" }
    /// "o/r#5" for GitHub, "QUI-123" elsewhere.
    var refLabel: String {
        guard let ref, !ref.isEmpty else { return "" }
        if source == "github", let repo, !repo.isEmpty { return "\(repo)#\(ref)" }
        return ref
    }
    /// The GitHub issue/PR number, when the task is one.
    var number: Int? { Int(ref ?? "") }
    /// Terminal states: merged, or closed without one.
    var isDone: Bool { state == "merged" || state == "closed" }
    var updatedDate: Date? { GitHubTime.date(updatedAt) }

    /// The `task_ref` `POST`/`DELETE /v1/sessions/<key>/task` accept.
    var refBody: [String: Any] {
        var body: [String: Any] = ["source": source ?? "", "kind": kind ?? "",
                                 "ref": ref ?? "", "title": title ?? "",
                                 "url": url ?? ""]
        if let repo { body["repo"] = repo }
        if let project { body["project"] = project }
        return body
    }
}

/// GitHub's ISO-8601 stamps (`updated_at` and friends), with or
/// without fractional seconds.
enum GitHubTime {
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func date(_ text: String?) -> Date? {
        guard let text, !text.isEmpty else { return nil }
        return plain.date(from: text) ?? fractional.date(from: text)
    }
}

/// One path from `GET /v1/sessions/{id}/files`, relative to the
/// session's working folder.
struct FileEntry: Codable, Hashable {
    let path: String
    var kind: String? = nil
    var mtime: Double? = nil

    var isDir: Bool { kind == "dir" }
}

/// `GET /v1/sessions/{id}/files?q=&limit=`: the prompt box's `@` menu.
struct FilesResponse: Codable {
    var cwd: String? = nil
    var query: String? = nil
    var source: String? = nil
    var truncated: Bool? = nil
    let entries: [FileEntry]
}

/// One entry of `GET /v1/sessions/{id}/commands`.
struct SlashCommand: Codable, Hashable {
    let name: String
    var description: String? = nil
    /// `builtin`, `user`, `project`, `skill` or `plugin`.
    var source: String? = nil
    var argumentHint: String? = nil
    var argumentRequired: Bool? = nil
    /// What to put in the prompt box when it isn't `/name ` (Codex
    /// skills are `$name `).
    var insert: String? = nil

    /// What the box gets on a pick, always ending in a space.
    var insertText: String {
        let text = (insert?.isEmpty == false ? insert! : "/\(name)")
        return text.last?.isWhitespace == true ? text : text + " "
    }

    /// How the menu names it: the inserted text without its space.
    var label: String { insertText.trimmingCharacters(in: .whitespaces) }

    /// The source tag, in sentence case.
    var sourceLabel: String? {
        switch source {
        case nil, "": nil
        case "builtin": "Built-in"
        case "user": "User"
        case "project": "Project"
        case "skill": "Skill"
        case "plugin": "Plugin"
        case let s?: s.prefix(1).uppercased() + s.dropFirst()
        }
    }
}

/// `GET /v1/sessions/{id}/commands`: the prompt box's `/` menu.
struct CommandsResponse: Codable {
    var key: String? = nil
    var harness: String? = nil
    var channel: String? = nil
    var attached: Bool? = nil
    var supported: Bool? = nil
    var customAvailable: Bool? = nil
    let commands: [SlashCommand]
    var note: String? = nil
}
