import Foundation
import Observation

// What each hook did: the counts the Hooks page shows beside every script
// and in its side panel. They come from `swe hooks stats --json` (one row
// per hook and harness) and `swe hooks log <name> --json` (the latest runs),
// shipped in the CLI half of #1888; see docs/json-contracts.md, "`swe hooks
// stats` shape".

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

/// One row of `swe hooks stats --json`'s `hooks`: a hook on a harness.
public struct HookStatsRow: Decodable, Sendable, Equatable {
    /// The script name without its extension, `prose-guard`; a hook another
    /// installer declared can carry a folder, `claude-plugin/on-stop`.
    public let hook: String
    public let harness: String
    public let fires: Int
    public let blocks: Int
    public let warns: Int
    public let errors: Int
    public let allows: Int
    public let lastBlock: Date?
    public let topReasons: [HookReason]

    public init(hook: String, harness: String, fires: Int = 0, blocks: Int = 0,
                warns: Int = 0, errors: Int = 0, allows: Int = 0,
                lastBlock: Date? = nil, topReasons: [HookReason] = []) {
        self.hook = hook
        self.harness = harness
        self.fires = fires
        self.blocks = blocks
        self.warns = warns
        self.errors = errors
        self.allows = allows
        self.lastBlock = lastBlock
        self.topReasons = topReasons
    }

    enum CodingKeys: String, CodingKey {
        case hook, harness, fires, blocks, warns, errors, allows, lastBlock, topReasons
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hook = try c.decode(String.self, forKey: .hook)
        harness = try c.decodeIfPresent(String.self, forKey: .harness) ?? ""
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

    public init(window: String, hooks: [HookStatsRow]) {
        self.window = window
        self.hooks = hooks
    }

    enum CodingKeys: String, CodingKey { case window, hooks }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        window = try c.decodeIfPresent(String.self, forKey: .window) ?? ""
        // One bad row degrades instead of failing every count.
        hooks = try c.decode([LossyRow<HookStatsRow>].self, forKey: .hooks).compactMap(\.value)
    }
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

/// `swe hooks log <name> --json`: the newest runs first.
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

/// What one script did in the window, summed over every row that names it
/// (the same shared script runs on several harnesses).
public struct HookCounts: Sendable, Equatable {
    public let fires: Int
    public let blocks: Int
    public let warns: Int
    public let errors: Int
    /// The newest block in the window, nil when there was none.
    public let lastBlock: Date?
    /// The reasons that did not allow, most first, at most `reasonLimit`.
    public let topReasons: [HookReason]

    public static let reasonLimit = 5
    public static let none = HookCounts(rows: [])

    public init(rows: [HookStatsRow]) {
        fires = rows.reduce(0) { $0 + $1.fires }
        blocks = rows.reduce(0) { $0 + $1.blocks }
        warns = rows.reduce(0) { $0 + $1.warns }
        errors = rows.reduce(0) { $0 + $1.errors }
        lastBlock = rows.compactMap(\.lastBlock).max()
        var merged: [String: HookReason] = [:]
        var order: [String] = []
        for reason in rows.flatMap(\.topReasons) {
            if let seen = merged[reason.id] {
                merged[reason.id] = HookReason(decision: seen.decision, reason: seen.reason,
                                               count: seen.count + reason.count)
            } else {
                merged[reason.id] = reason
                order.append(reason.id)
            }
        }
        // Count first; ties keep the order swe gave them in.
        topReasons = Array(order.enumerated()
            .sorted { a, b in
                let ca = merged[a.element]?.count ?? 0, cb = merged[b.element]?.count ?? 0
                return ca != cb ? ca > cb : a.offset < b.offset
            }
            .compactMap { merged[$0.element] }
            .prefix(Self.reasonLimit))
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

/// One script on the page, for matching: `id` is its source file or
/// folder (two scripts with one basename in different places are two), and
/// `keys` the `hook` names its runs are logged under, which swe works out
/// (`stats_keys` on the inventory row). A directory hook is listed by its
/// folder (`bundle`) while its runs carry its executable's stem (`guard`),
/// so the list name alone would find nothing.
public struct HookStatsScript: Sendable, Equatable {
    public let id: String
    public let keys: [String]

    public init(id: String, keys: [String]) {
        self.id = id
        self.keys = keys
    }

    /// One entry per distinct source: a shared script is a row per harness
    /// it deploys to, all with the one source. A row from an swe that sends
    /// no `stats_keys` falls back to its file stem.
    public static func scripts(for hooks: [Hook]) -> [HookStatsScript] {
        var seen: Set<String> = []
        var out: [HookStatsScript] = []
        for hook in hooks where seen.insert(hook.source).inserted {
            out.append(HookStatsScript(id: hook.source,
                                       keys: hook.statsKeys ?? [HookStatsNames.stem(hook.script)]))
        }
        return out
    }
}

/// The script names swe's stats use drop the file extension.
public enum HookStatsNames {
    /// The extensions swe's `hook_stem` drops.
    static let extensions = [".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs",
                             ".ts", ".rb", ".pl"]

    /// `prose-guard` for `prose-guard.py`; folders stay: `a/b.py` is `a/b`.
    public static func stem(_ name: String) -> String {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        let lowered = trimmed.lowercased()
        for ext in extensions where lowered.hasSuffix(ext) && trimmed.count > ext.count {
            return String(trimmed.dropLast(ext.count))
        }
        return trimmed
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

/// The report for one window, ready to answer for any script on the page.
/// A script's rows are the stats rows named by its keys, summed.
public struct HookStatsTable: Sendable, Equatable {
    private let counts: [String: HookCounts]
    private let names: [String: [String]]
    private let keyless: Set<String>

    /// - Parameters:
    ///   - report: `swe hooks stats --json`.
    ///   - scripts: every script on the page, each with the keys swe gave it.
    public init(report: HookStatsReport, scripts: [HookStatsScript]) {
        var byName: [String: [HookStatsRow]] = [:]
        for row in report.hooks { byName[row.hook, default: []].append(row) }
        var counts: [String: HookCounts] = [:]
        var names: [String: [String]] = [:]
        var keyless: Set<String> = []
        for script in scripts {
            if script.keys.isEmpty { keyless.insert(script.id); continue }
            let found = Set(script.keys).filter { byName[$0] != nil }.sorted()
            names[script.id] = found
            counts[script.id] = HookCounts(rows: found.flatMap { byName[$0] ?? [] })
        }
        self.counts = counts
        self.names = names
        self.keyless = keyless
    }

    /// The script's counts, summed over its keys and harnesses; zeros when
    /// nothing was logged under them.
    public func counts(for id: String) -> HookCounts {
        counts[id] ?? .none
    }

    /// The stats names with runs for the script, for `swe hooks log`.
    public func statsNames(for id: String) -> [String] {
        names[id] ?? []
    }

    /// swe could name no log name for the script, so no count is its own.
    public func hasNoKeys(_ id: String) -> Bool {
        keyless.contains(id)
    }
}

/// What the Hooks page knows about run counts for the window on show.
public enum HookStatsState: Sendable, Equatable {
    case loading
    case unavailable
    case ready(HookStatsTable)

    /// - Parameter id: the script's source, `Hook.source`.
    public func cells(for id: String) -> HookStatsCells {
        switch self {
        case .loading: .loading
        case .unavailable: .dashes
        case .ready(let table): table.hasNoKeys(id) ? .dashes : .counts(table.counts(for: id))
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

    /// `swe hooks log <name> --since=<window> --limit=<n> --json`, newest first.
    public func hooksLog(name: String, window: HookStatsWindow, limit: Int = 5) async throws -> HookLogReport {
        try await call(["hooks", "log", name, "--since=\(window.since)", "--limit=\(limit)", "--json"])
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
    /// answer, dashes if it failed, otherwise the table for `scripts`.
    public func state(for window: HookStatsWindow, scripts: [HookStatsScript]) -> HookStatsState {
        if let report = reports[window] { return .ready(HookStatsTable(report: report, scripts: scripts)) }
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

    /// The newest runs across `names` in `window`: nil until the first
    /// answer or when swe could not give them.
    public func recentRuns(names: [String], window: HookStatsWindow) -> [HookRunEvent]? {
        recent[Self.recentKey(names, window)]
    }

    /// Whether a run list is still on its way (no answer and no failure).
    public func recentIsLoading(names: [String], window: HookStatsWindow) -> Bool {
        let key = Self.recentKey(names, window)
        return recent[key] == nil && !recentFailed.contains(key)
    }

    /// Runs `swe hooks log` for each of the panel's names, then keeps the
    /// newest five of all of them together: a directory hook's scripts
    /// are logged apart, and the latest block can be any of them.
    public func loadRecent(names: [String], window: HookStatsWindow, swe: SweClient,
                           for source: String, limit: Int = 5) async {
        reset(ifNot: source)
        let key = Self.recentKey(names, window)
        let ticket = issue(key)
        recentFailed.remove(key)
        do {
            var merged: [HookRunEvent] = []
            for name in names {
                merged += try await swe.hooksLog(name: name, window: window, limit: limit).events
            }
            guard accept(key, ticket, source) else { return }
            var newest = Array(merged.sorted { $0.ts > $1.ts }.prefix(limit))
            for index in newest.indices { newest[index].ordinal = index }
            recent[key] = newest
        } catch {
            guard accept(key, ticket, source) else { return }
            if recent[key] == nil { recentFailed.insert(key) }
        }
    }

    private static func recentKey(_ names: [String], _ window: HookStatsWindow) -> String {
        "\(window.rawValue)|\(names.joined(separator: "\u{1f}"))"
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
