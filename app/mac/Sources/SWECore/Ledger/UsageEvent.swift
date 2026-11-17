import Foundation

/// One model response, as a harness logged it.
///
/// The five token kinds are disjoint, so they add up to `totalTokens` and each
/// one is priced at its own rate:
///
/// - `inputTokens`: prompt tokens read neither from nor into the cache.
/// - `cacheReadTokens`: prompt tokens served from the cache.
/// - `cacheWriteTokens`: prompt tokens written to the cache. `cacheWrite1hTokens`
///   is the part of it that went to Anthropic's 1-hour cache, which bills at a
///   higher rate than the 5-minute one.
/// - `outputTokens`: generated tokens, not counting reasoning.
/// - `reasoningTokens`: thinking or reasoning tokens. Claude and Codex both log
///   these inside their output count; the parsers move them here.
public struct UsageEvent: Sendable, Equatable, Hashable, Codable {
    /// "claude", "codex", "opencode", "devin", "pi", "droid", "cline",
    /// "cursor" or "grok-bot" (the feed splits Grok Bot out of Cursor's).
    public var harness: String
    /// The config root the event was read from, for example `~/.claude` or a
    /// SWE profile home. It is how usage is split by account.
    public var accountHome: String
    public var sessionId: String
    public var cwd: String?
    public var model: String
    public var timestamp: Date
    public var inputTokens: Int
    public var outputTokens: Int
    public var cacheReadTokens: Int
    public var cacheWriteTokens: Int
    public var cacheWrite1hTokens: Int
    public var reasoningTokens: Int
    /// The cost the log itself recorded, when it records one. It wins over the
    /// pricing catalog.
    public var costUSD: Double?
    /// The speed or service tier the request ran on when it is not the standard
    /// one: "fast" (Claude fast mode), "priority" or "flex" (OpenAI tiers).
    public var serviceTier: String?
    /// Droid and Cline report one total per session, not per request. Such an
    /// event never crosses a model's long-context threshold: its summed input
    /// says nothing about any single request's prompt size. Grok reports one
    /// total per turn, which is one request, so it stays false.
    public var isSessionTotal: Bool

    public init(
        harness: String, accountHome: String, sessionId: String, cwd: String? = nil,
        model: String, timestamp: Date,
        inputTokens: Int = 0, outputTokens: Int = 0,
        cacheReadTokens: Int = 0, cacheWriteTokens: Int = 0, cacheWrite1hTokens: Int = 0,
        reasoningTokens: Int = 0, costUSD: Double? = nil, serviceTier: String? = nil,
        isSessionTotal: Bool = false
    ) {
        self.harness = harness
        self.accountHome = accountHome
        self.sessionId = sessionId
        self.cwd = cwd
        self.model = model
        self.timestamp = timestamp
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.cacheReadTokens = cacheReadTokens
        self.cacheWriteTokens = cacheWriteTokens
        self.cacheWrite1hTokens = cacheWrite1hTokens
        self.reasoningTokens = reasoningTokens
        self.costUSD = costUSD
        self.serviceTier = serviceTier
        self.isSessionTotal = isSessionTotal
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        harness = try container.decode(String.self, forKey: .harness)
        accountHome = try container.decode(String.self, forKey: .accountHome)
        sessionId = try container.decode(String.self, forKey: .sessionId)
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd)
        model = try container.decode(String.self, forKey: .model)
        timestamp = try container.decode(Date.self, forKey: .timestamp)
        inputTokens = try container.decode(Int.self, forKey: .inputTokens)
        outputTokens = try container.decode(Int.self, forKey: .outputTokens)
        cacheReadTokens = try container.decode(Int.self, forKey: .cacheReadTokens)
        cacheWriteTokens = try container.decode(Int.self, forKey: .cacheWriteTokens)
        cacheWrite1hTokens = try container.decode(Int.self, forKey: .cacheWrite1hTokens)
        reasoningTokens = try container.decode(Int.self, forKey: .reasoningTokens)
        costUSD = try container.decodeIfPresent(Double.self, forKey: .costUSD)
        serviceTier = try container.decodeIfPresent(String.self, forKey: .serviceTier)
        isSessionTotal = try container.decodeIfPresent(Bool.self, forKey: .isSessionTotal) ?? false
    }

    public var totalTokens: Int {
        inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens + reasoningTokens
    }
}

/// A parsed event plus what the ledger needs to deduplicate it.
struct LedgerRecord: Sendable, Equatable {
    /// Two records with the same key are the same response logged twice. The
    /// ledger keeps one: the non-sidechain copy, then the larger token total.
    var key: String
    /// Claude only: a sidechain line can replay a parent message under a new
    /// request id, so sidechain records also collide on the message id alone.
    var messageId: String?
    var isSidechain: Bool = false
    /// Session-total sources (Droid, Cline) rewrite one file per session, so
    /// a re-read under the same key replaces whatever the file said before,
    /// even when the totals went down.
    var authoritative: Bool = false
    /// The account in `key` is a real account id that more than one profile
    /// can read — the Cursor feed, when swe names an `account_id`. An
    /// authoritative write then removes rows an old key scheme left for the
    /// same event under any profile's `accountHome`. Without it the account
    /// is the profile itself, so only this profile's stale rows match.
    var accountSpansProfiles: Bool = false
    /// OpenCode: a re-read under an existing key that does not replace its
    /// row (the totals tied) still moves the row to this record's model.
    /// The key is the message id, so a reader that learns to name the model
    /// differently (`provider/model`) renames old rows in place instead of
    /// adding a second copy of each.
    var relabelsModel: Bool = false
    /// Two events from one file can share a timestamp, model and session
    /// (Grok turns that end in the same second). The lookalike cleanup
    /// would delete one of them; these sources never used a counts-bearing
    /// key, so they skip it.
    var skipsLookalikes: Bool = false
    var event: UsageEvent
}

enum JSONValue {
    /// No real response logs more tokens than this. A larger count is a
    /// corrupt line, and capping it keeps every sum of an event's counts
    /// (and the ledger's totals) far from overflowing, which traps.
    /// `swe.ledger.models.MAX_TOKENS` is the same number.
    static let maxTokens = 1 << 40

    /// A non-negative integer from a JSON number, or 0, at most `maxTokens`.
    static func int(_ value: Any?) -> Int {
        guard let number = value as? NSNumber, !(number === kCFBooleanTrue || number === kCFBooleanFalse) else {
            return 0
        }
        let double = number.doubleValue
        guard double.isFinite, double > 0 else { return 0 }
        return double >= Double(maxTokens) ? maxTokens : Int(double)
    }

    static func string(_ value: Any?) -> String? {
        guard let text = value as? String else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
