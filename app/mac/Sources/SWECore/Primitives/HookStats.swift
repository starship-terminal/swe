import Foundation
import Observation

// What each hook did: the counts the Hooks page shows beside every script
// and in its side panel. They come from `swe hooks stats --sum-harnesses
// --json` (one row per hook) and `swe hooks log --source=<path> --json` (the
// latest runs); see docs/json-contracts.md, "`swe hooks stats` shape".
//
// Which runs are which script's is swe's call, made once for every client
// (src/swe/hooks/identity.py): each stats row lists the `source_path` of
// the `swe hooks list` rows it belongs to, and `uncertain` the ones a run
// could belong to but swe cannot tell. The app joins on that path and
// nothing else: no names, no stems, no folder scans.

/// The span the Hooks page counts runs over, and the `--since` it asks swe for.
public enum HookStatsWindow: String, CaseIterable, Sendable, Identifiable {
    case day = "24h"
    case week = "7d"
    case month = "30d"

    public var id: String { rawValue }

    /// The value for `swe hooks stats --since=`.
    public var since: String { rawValue }

    /// The picker's label.
    public var title: String {
        switch self {
        case .day: "Last 24 hours"
        case .week: "Last 7 days"
        case .month: "Last 30 days"
        }
    }

    /// The line a hook with no runs in the window gets.
    public var emptyLine: String {
        "No runs in the \(title.lowercased())."
    }
}

/// One reason a hook gave when it did not allow: `decision` is `block`,
/// `warn` or `error`, and `count` how many runs gave it.
public struct HookReason: Decodable, Sendable, Equatable, Identifiable {
    public let decision: String
    public let reason: String
    public let count: Int
    public var id: String { "\(decision)|\(reason)" }

    public init(decision: String, reason: String, count: Int) {
        self.decision = decision
        self.reason = reason
        self.count = count
    }

    enum CodingKeys: String, CodingKey { case decision, reason, count }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        decision = try c.decodeIfPresent(String.self, forKey: .decision) ?? ""
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
        count = try c.decodeIfPresent(Int.self, forKey: .count) ?? 0
    }

    /// "12 blocks", "1 warn", "3 errors".
    public var countLabel: String {
        let noun: String
        switch decision {
        case "block": noun = "block"
        case "warn": noun = "warn"
        case "error": noun = "error"
        default: noun = decision.isEmpty ? "run" : decision
        }
        return "\(count.formatted()) \(noun)\(count == 1 ? "" : "s")"
    }
}

/// One row of `swe hooks stats --json`'s `hooks`: a hook (on a harness, or
/// on all of them under `--sum-harnesses`).
public struct HookStatsRow: Decodable, Sendable, Equatable {
    /// The list's name for the script without its extension, `prose-guard`
    /// or `bundle`; for runs no listed script owns, the name they ran under.
    /// Shown, never matched on.
    public let hook: String
    public let harness: String
    /// The `source_path` of every `swe hooks list` row these runs belong to:
    /// the join. Empty when no listed script owns them.
    public let sourcePaths: [String]
    /// Whether swe sent `source_paths` at all: one from before it did gives
    /// the app nothing to join on.
    public let identified: Bool
    public let fires: Int
    public let blocks: Int
    public let warns: Int
    public let errors: Int
    public let allows: Int
    public let lastBlock: Date?
    public let topReasons: [HookReason]

    public init(hook: String, harness: String = "", sourcePaths: [String] = [],
                fires: Int = 0, blocks: Int = 0, warns: Int = 0, errors: Int = 0,
                allows: Int = 0, lastBlock: Date? = nil, topReasons: [HookReason] = []) {
        self.hook = hook
        self.harness = harness
        self.sourcePaths = sourcePaths
        self.identified = true
        self.fires = fires
        self.blocks = blocks
        self.warns = warns
        self.errors = errors
        self.allows = allows
        self.lastBlock = lastBlock
        self.topReasons = topReasons
    }

    enum CodingKeys: String, CodingKey {
        case hook, harness, sourcePaths, fires, blocks, warns, errors, allows, lastBlock, topReasons
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hook = try c.decode(String.self, forKey: .hook)
        harness = try c.decodeIfPresent(String.self, forKey: .harness) ?? ""
        identified = c.contains(.sourcePaths)
        sourcePaths = try c.decodeIfPresent([String].self, forKey: .sourcePaths) ?? []
        fires = try c.decodeIfPresent(Int.self, forKey: .fires) ?? 0
        blocks = try c.decodeIfPresent(Int.self, forKey: .blocks) ?? 0
        warns = try c.decodeIfPresent(Int.self, forKey: .warns) ?? 0
        errors = try c.decodeIfPresent(Int.self, forKey: .errors) ?? 0
        allows = try c.decodeIfPresent(Int.self, forKey: .allows) ?? 0
        lastBlock = try c.decodeIfPresent(Date.self, forKey: .lastBlock)
        topReasons = try c.decodeIfPresent([HookReason].self, forKey: .topReasons) ?? []
    }
}

/// `swe hooks stats --json`. Fields the page does not read (`totals`,
/// `sources`, `filters`) are left undecoded, so a newer swe adding keys
/// changes nothing here.
public struct HookStatsReport: Decodable, Sendable, Equatable {
    public let window: String
    public let hooks: [HookStatsRow]
    /// The `source_path` of every script a run could belong to that swe
    /// could not tell from another's: their counts may be short, so the
    /// page shows dashes for them.
    public let uncertain: Set<String>

    public init(window: String, hooks: [HookStatsRow], uncertain: Set<String> = []) {
        self.window = window
        self.hooks = hooks
        self.uncertain = uncertain
    }

    enum CodingKeys: String, CodingKey { case window, hooks, uncertain }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        window = try c.decodeIfPresent(String.self, forKey: .window) ?? ""
        // One bad row degrades instead of failing every count.
        hooks = try c.decode([LossyRow<HookStatsRow>].self, forKey: .hooks).compactMap(\.value)
        uncertain = Set(try c.decodeIfPresent([String].self, forKey: .uncertain) ?? [])
    }

    /// Whether the rows say which scripts they belong to. An swe from
    /// before `source_paths` does not, and then no count is any script's.
    public var identified: Bool { hooks.allSatisfy(\.identified) }
}

/// One run from `swe hooks log <name> --json`.
public struct HookRunEvent: Decodable, Sendable, Equatable, Identifiable {
    public let ts: Date
    public let hook: String
    public let harness: String
    public let decision: String
    public let repo: String?
    public let cwd: String?
    public let gitBranch: String?
    /// Where the run sits in the reply it came from. Parallel tool calls
    /// blocked by one hook share a timestamp, so nothing in the run itself
    /// tells two apart; `HookLogReport` numbers them as it decodes.
    public internal(set) var ordinal = 0
    public var id: Int { ordinal }

    public init(ts: Date, hook: String, harness: String = "", decision: String,
                repo: String? = nil, cwd: String? = nil, gitBranch: String? = nil) {
        self.ts = ts
        self.hook = hook
        self.harness = harness
        self.decision = decision
        self.repo = repo
        self.cwd = cwd
        self.gitBranch = gitBranch
    }

    enum CodingKeys: String, CodingKey {
        case ts, hook, harness, decision, repo, cwd, gitBranch
    }

    /// "Block", "Warn", "Allow", "Error".
    public var decisionTitle: String {
        decision.isEmpty ? "Run" : decision.prefix(1).uppercased() + decision.dropFirst()
    }

    /// The project the run happened in: the repository's folder name, else
    /// the working folder's, nil when neither was recorded.
    public var project: String? {
        for path in [repo, cwd] {
            guard let path, !path.isEmpty else { continue }
            let name = path.split(separator: "/").last.map(String.init) ?? path
            if !name.isEmpty { return name }
        }
        return nil
    }

    /// "Block · alpha · feat/x": the pieces a run recorded, in that order.
    public var summary: String {
        var parts = [decisionTitle]
        if let project { parts.append(project) }
        if let gitBranch, !gitBranch.isEmpty { parts.append(gitBranch) }
        return parts.joined(separator: " · ")
    }
}

/// `swe hooks log --source=<path> --json`: the newest runs first.
public struct HookLogReport: Decodable, Sendable, Equatable {
    public let hook: String
    public let events: [HookRunEvent]

    public init(hook: String, events: [HookRunEvent]) {
        self.hook = hook
        self.events = events
    }

    enum CodingKeys: String, CodingKey { case hook, events }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hook = try c.decodeIfPresent(String.self, forKey: .hook) ?? ""
        var decoded = try c.decode([LossyRow<HookRunEvent>].self, forKey: .events).compactMap(\.value)
        for index in decoded.indices { decoded[index].ordinal = index }
        events = decoded
    }
}

/// What one script did in the window: its one `--sum-harnesses` row, every
/// harness and every name its runs were logged under already folded in by
/// swe, before it capped the reasons at five. The app adds nothing up.
public struct HookCounts: Sendable, Equatable {
    public let fires: Int
    public let blocks: Int
    public let warns: Int
    public let errors: Int
    /// The newest block in the window, nil when there was none.
    public let lastBlock: Date?
    /// The reasons that did not allow, most first, as swe ranked them.
    public let topReasons: [HookReason]

    public static let reasonLimit = 5
    public static let none = HookCounts(row: nil)

    public init(row: HookStatsRow?) {
        fires = row?.fires ?? 0
        blocks = row?.blocks ?? 0
        warns = row?.warns ?? 0
        errors = row?.errors ?? 0
        lastBlock = row?.lastBlock
        topReasons = Array((row?.topReasons ?? []).prefix(Self.reasonLimit))
    }

    public var hasRuns: Bool { fires > 0 }

    /// Blocks over fires as a whole percent, nil with no runs.
    public var blockPercent: Int? {
        hasRuns ? Int((Double(blocks) / Double(fires) * 100).rounded()) : nil
    }

    /// "33%", "0%", "100%"; "-" with no runs. A block that rounds to nothing
    /// reads "<1%", so a red block count never sits beside a bare "0%".
    public var rateLabel: String {
        guard let percent = blockPercent else { return "-" }
        return blocks > 0 && percent == 0 ? "<1%" : "\(percent)%"
    }
}

/// What a row's number columns show.
public enum HookStatsCells: Sendable, Equatable {
    /// swe has not answered yet: skeleton bars.
    case loading
    /// swe could not answer, or is too old to know the command: dashes.
    case dashes
    case counts(HookCounts)
}

/// The report for one window, ready to answer for any script on the page
/// by its `Hook.source`.
public struct HookStatsTable: Sendable, Equatable {
    private let rows: [String: HookStatsRow]
    private let uncertain: Set<String>
    /// False for an swe that sends no `source_paths`: then nothing joins.
    public let identified: Bool

    /// - Parameter report: `swe hooks stats --sum-harnesses --json`.
    public init(report: HookStatsReport) {
        var rows: [String: HookStatsRow] = [:]
        for row in report.hooks {
            // swe gives each script one row; a second would be a contract
            // break, and taking the first keeps the page from adding up
            // numbers swe did not.
            for path in row.sourcePaths where rows[path] == nil { rows[path] = row }
        }
        self.rows = rows
        self.uncertain = report.uncertain
        self.identified = report.identified
    }

    /// The script's counts; zeros when no run was its own.
    /// - Parameter source: `Hook.source`, the list row's `source_path`.
    public func counts(for source: String) -> HookCounts {
        HookCounts(row: rows[source])
    }

    /// swe saw a run that may be this script's and could not tell, or it
    /// cannot say whose runs are whose at all: the numbers are not known.
    public func isUnknown(_ source: String) -> Bool {
        !identified || uncertain.contains(source)
    }

    /// What the script's number columns show.
    public func cells(for source: String) -> HookStatsCells {
        isUnknown(source) ? .dashes : .counts(counts(for: source))
    }
}

/// What the Hooks page knows about run counts for the window on show.
public enum HookStatsState: Sendable, Equatable {
    case loading
    case unavailable
    case ready(HookStatsTable)

    /// - Parameter source: the script's source, `Hook.source`.
    public func cells(for source: String) -> HookStatsCells {
        switch self {
        case .loading: .loading
        case .unavailable: .dashes
        case .ready(let table): table.cells(for: source)
        }
    }
}

/// "3h ago", for the side panel's last block and recent runs.
public enum HookStatsFormat {
    public static func relative(_ date: Date, now: Date = Date()) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.unitsStyle = .abbreviated
        formatter.dateTimeStyle = .named
        return formatter.localizedString(for: date, relativeTo: now)
    }
}

extension SweClient {
    /// `swe hooks stats --since=<window> --sum-harnesses --json`: one row
    /// per hook with every harness folded in before swe cuts each row's
    /// reasons to five, so the totals are exact.
    public func hooksStats(window: HookStatsWindow) async throws -> HookStatsReport {
        try await call(["hooks", "stats", "--since=\(window.since)", "--sum-harnesses", "--json"])
    }

    /// `swe hooks log --source=<path> --since=<window> --limit=<n> --json`:
    /// the newest runs of the script whose list row has that `source_path`,
    /// every name and harness included.
    public func hooksLog(source: String, window: HookStatsWindow, limit: Int = 5) async throws -> HookLogReport {
        try await call(["hooks", "log", "--source=\(source)", "--since=\(window.since)",
                        "--limit=\(limit)", "--json"])
    }
}

/// The Hooks page's run counts. Stats come per window and stay until the
/// next answer replaces them, so a refresh keeps the numbers on screen and
/// only a window with none yet shows the loading bars. A failed run (an swe
/// from before `hooks stats`, a timeout) is not an error banner: the page
/// shows dashes and works as before.
@MainActor
@Observable
public final class HookStatsModel {
    private var reports: [HookStatsWindow: HookStatsReport] = [:]
    private var failed: Set<HookStatsWindow> = []
    private var recent: [String: [HookRunEvent]] = [:]
    private var recentFailed: Set<String> = []
    /// Which swe the held answers came from; another swe starts over.
    private var source: String?
    /// The newest request per window: a slower, older answer is dropped.
    private var tickets: [String: Int] = [:]
    private var nextTicket = 0
    /// Bumped whenever a stats answer lands, so a view that wants the
    /// recent runs again can key a task on it.
    public private(set) var completed = 0

    public init() {}

    /// The answer for `window`, as the counts see it: bars until the first
    /// answer, dashes if it failed, otherwise the table.
    public func state(for window: HookStatsWindow) -> HookStatsState {
        if let report = reports[window] { return .ready(HookStatsTable(report: report)) }
        return failed.contains(window) ? .unavailable : .loading
    }

    /// Runs `swe hooks stats` for `window` off the main actor.
    /// - Parameter source: the swe asked, as set in Settings.
    public func load(window: HookStatsWindow, swe: SweClient, for source: String) async {
        reset(ifNot: source)
        let key = window.rawValue
        let ticket = issue(key)
        failed.remove(window)
        do {
            let report = try await swe.hooksStats(window: window)
            guard accept(key, ticket, source) else { return }
            reports[window] = report
        } catch {
            guard accept(key, ticket, source) else { return }
            // A refresh that fails keeps the numbers already on screen.
            if reports[window] == nil { failed.insert(window) }
        }
        completed += 1
    }

    /// The newest runs of the script at `source` in `window`: nil until the
    /// first answer or when swe could not give them.
    public func recentRuns(source: String, window: HookStatsWindow) -> [HookRunEvent]? {
        recent[Self.recentKey(source, window)]
    }

    /// Whether a run list is still on its way (no answer and no failure).
    public func recentIsLoading(source: String, window: HookStatsWindow) -> Bool {
        let key = Self.recentKey(source, window)
        return recent[key] == nil && !recentFailed.contains(key)
    }

    /// Runs `swe hooks log --source=` for the panel's script: one ask, since
    /// swe already gathers a directory hook's executables and a script's
    /// every name under its source.
    public func loadRecent(source: String, window: HookStatsWindow, swe: SweClient,
                           for sweSource: String, limit: Int = 5) async {
        reset(ifNot: sweSource)
        let key = Self.recentKey(source, window)
        let ticket = issue(key)
        recentFailed.remove(key)
        do {
            let log = try await swe.hooksLog(source: source, window: window, limit: limit)
            guard accept(key, ticket, sweSource) else { return }
            recent[key] = Array(log.events.prefix(limit))
        } catch {
            guard accept(key, ticket, sweSource) else { return }
            if recent[key] == nil { recentFailed.insert(key) }
        }
    }

    private static func recentKey(_ source: String, _ window: HookStatsWindow) -> String {
        "\(window.rawValue)|\(source)"
    }

    private func issue(_ key: String) -> Int {
        nextTicket += 1
        tickets[key] = nextTicket
        return nextTicket
    }

    /// True when this answer is still the one wanted: not cancelled, not
    /// replaced by a newer request, and from the swe now selected.
    private func accept(_ key: String, _ ticket: Int, _ source: String) -> Bool {
        !Task.isCancelled && tickets[key] == ticket && self.source == source
    }

    private func reset(ifNot source: String) {
        guard self.source != source else { return }
        self.source = source
        reports = [:]
        failed = []
        recent = [:]
        recentFailed = []
        tickets = [:]
    }
}
