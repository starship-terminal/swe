import Foundation

/// Pi session logs (`<home>/agent/sessions/<project>/<ts>_<uuid>.jsonl`).
///
/// Assistant `message` lines carry a `usage` block with per-request counts
/// and a USD `cost` breakdown Pi computed itself. `usage.output` already
/// includes `reasoning` (`totalTokens` = input + output + cacheRead +
/// cacheWrite in the logs), so reasoning is split out of output the way the
/// Codex reader does it. The session's cwd arrives once on the `session`
/// line, so it is kept as line-parser state.
struct PiFileParser: FileLogParser {
    static let harness = "pi"
    static let searchHarness: SearchDecoder.Harness = .pi

    private let file: URL
    private let accountHome: String
    private var state = State()

    struct State: Codable {
        var cwd: String?
    }

    init(file: URL, accountHome: String, savedState: Data?) {
        self.file = file
        self.accountHome = accountHome
        if let savedState, let decoded = try? JSONDecoder().decode(State.self, from: savedState) {
            state = decoded
        }
    }

    /// Only `session` and `message` records carry anything this parser
    /// reads — the same byte markers `SearchPiDecoder` gates on (`"session`
    /// has no closing quote so it covers `session_info` too).
    private static let markers = [#""message""#, #""session"#].map { Data($0.utf8) }

    func wants(line: Data) -> Bool {
        Self.markers.contains(where: { line.range(of: $0) != nil })
    }

    mutating func records(fromObject object: [String: Any]) -> [LedgerRecord] {
        switch object["type"] as? String {
        case "session":
            if let cwd = JSONValue.string(object["cwd"]) { state.cwd = cwd }
            return []
        case "message":
            break
        default:
            return []
        }
        guard let message = object["message"] as? [String: Any],
              message["role"] as? String == "assistant",
              let usage = message["usage"] as? [String: Any],
              let milliseconds = message["timestamp"] as? NSNumber
        else { return [] }

        let input = JSONValue.int(usage["input"])
        let output = JSONValue.int(usage["output"])
        let reasoning = min(JSONValue.int(usage["reasoning"]), output)
        let cost = (usage["cost"] as? [String: Any]).flatMap { $0["total"] as? NSNumber }
            .map { $0.doubleValue }
        let model = JSONValue.string(message["model"]) ?? "unknown"
        let event = UsageEvent(
            harness: Self.harness, accountHome: accountHome,
            sessionId: file.path, cwd: state.cwd,
            model: model,
            timestamp: Date(timeIntervalSince1970: milliseconds.doubleValue / 1000),
            inputTokens: input, outputTokens: output - reasoning,
            cacheReadTokens: JSONValue.int(usage["cacheRead"]),
            cacheWriteTokens: JSONValue.int(usage["cacheWrite"]),
            reasoningTokens: reasoning, costUSD: cost)
        guard event.totalTokens > 0 || (cost ?? 0) > 0 else { return [] }

        // `id` is the line's message id; fall back to the timestamp so a
        // rescan still collides with itself.
        let identity = JSONValue.string(object["id"])
            ?? String(format: "%.3f", milliseconds.doubleValue)
        return [LedgerRecord(key: "pi:\(file.lastPathComponent):\(identity)", event: event)]
    }

    func savedState() -> Data? { try? JSONEncoder().encode(state) }
}

/// What a session-total file says this scan: either the session's current
/// totals, or nothing (its usage is now zero or gone), in which case the
/// previously stored event under `key` must be deleted, not left behind.
enum SessionTotals {
    case record(LedgerRecord)
    /// The session's file parses but reports no usage: remove any event a
    /// previous scan stored under this key.
    case empty(key: String)
    /// The turn numbers this Grok file still lists. The scan compares
    /// them with the numbers its cursor saved last time and deletes the
    /// rest. `prefix` is `grok:<session id>:`.
    case kept(prefix: String, turns: Set<Int>)
}

/// Session-total sources: files that hold one session's totals and are
/// rewritten in place, so they are re-read whole whenever their stamp
/// changes and upserted under one stable, authoritative key.
enum SessionTotalsFile {
    /// The transcript `<uuid>.settings.json` is derived from. Its stamp is
    /// stored in the file's cursor state so a transcript change alone (a
    /// later message, a different cwd) still triggers a re-read.
    static func droidTranscript(for settings: URL) -> URL {
        settings.deletingPathExtension()
            .deletingPathExtension().appendingPathExtension("jsonl")
    }

    /// Droid: `<home>/sessions/<project>/<uuid>.settings.json`.
    /// `inclusiveTokenUsage` (falling back to `tokenUsage`) is the whole
    /// session's totals; `factoryCredits` is not USD, so no cost is logged
    /// and the catalog prices it. The timestamp is the last `timestamp` in
    /// the sibling `<uuid>.jsonl` (the settings file's mtime otherwise).
    /// No Droid file observed carries per-message usage; `thinkingTokens`
    /// is treated as separate from `outputTokens` (see the commit message).
    static func droid(settings url: URL, accountHome: String) throws -> SessionTotals? {
        let sessionId = url.deletingPathExtension()
            .deletingPathExtension().lastPathComponent
        let key = "droid:\(sessionId)"
        let object = try readJSON(url)
        guard let usage = (object["inclusiveTokenUsage"] ?? object["tokenUsage"]) as? [String: Any]
        else { return .empty(key: key) }
        let input = JSONValue.int(usage["inputTokens"])
        let output = JSONValue.int(usage["outputTokens"])
        let cacheRead = JSONValue.int(usage["cacheReadTokens"])
        let cacheWrite = JSONValue.int(usage["cacheCreationTokens"])
        let reasoning = JSONValue.int(usage["thinkingTokens"])
        guard input + output + cacheRead + cacheWrite + reasoning > 0 else { return .empty(key: key) }

        var cwd: String?
        var last = FileInfo(path: url.path).map {
            Date(timeIntervalSince1970: $0.modified)
        }
        // `<uuid>.settings.json` -> `<uuid>.jsonl` (two extensions to drop).
        let transcript = droidTranscript(for: url)
        func consume(_ line: Data) {
            guard let entry = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
            else { return }
            if entry["type"] as? String == "session_start" {
                cwd = cwd ?? JSONValue.string(entry["cwd"])
            }
            if let stamp = JSONValue.string(entry["timestamp"]).flatMap(Timestamp.parse) {
                last = stamp
            }
        }
        // Streamed in 1 MiB chunks, with JSONLReader's cap on one line.
        // A failed open or mid-read failure ends the scan of the
        // transcript with what was read, as swe's reader does.
        let reader = JSONLReader()
        var read: UInt64 = 0
        if let result = try? reader.read(transcript, from: 0, line: consume) {
            read = result.endOffset
        }
        // The reader leaves an unterminated last line for a later scan;
        // this read is whole every time, so that line counts now, as it
        // does in swe (`droid_session_totals`).
        // Droid may finish that line and start another meanwhile, so the
        // rest is read only up to the end seen here and split into lines.
        if let handle = FileHandle(forReadingAtPath: transcript.path) {
            defer { try? handle.close() }
            if let end = try? handle.seekToEnd(), end > read,
               end - read <= UInt64(reader.maxLineBytes),
               (try? handle.seek(toOffset: read)) != nil,
               let tail = try? handle.read(upToCount: Int(end - read)) {
                for line in tail.split(separator: UInt8(ascii: "\n")) { consume(Data(line)) }
            }
        }
        guard let timestamp = last else { throw CocoaError(.coderReadCorrupt) }

        let event = UsageEvent(
            harness: "droid", accountHome: accountHome, sessionId: sessionId, cwd: cwd,
            model: JSONValue.string(object["model"]) ?? "unknown",
            timestamp: timestamp,
            inputTokens: input, outputTokens: output,
            cacheReadTokens: cacheRead, cacheWriteTokens: cacheWrite,
            reasoningTokens: reasoning, isSessionTotal: true)
        return .record(LedgerRecord(key: key, authoritative: true, event: event))
    }

    /// Cline: `<home>/data/sessions/<id>/<id>.json` (the `.messages.json`
    /// transcript is a sibling, not read here). `metadata.tokensIn` is the
    /// input count — `metadata.usage.inputTokens` folds cache reads in —
    /// and `totalCost` is logged only when positive, so a free or unknown
    /// price falls back to the catalog.
    static func cline(session url: URL, accountHome: String) throws -> SessionTotals? {
        let object = try readJSON(url)
        guard let sessionId = JSONValue.string(object["session_id"]) else { return nil }
        let key = "cline:\(sessionId)"
        let metadata = object["metadata"] as? [String: Any] ?? [:]
        let input = JSONValue.int(metadata["tokensIn"])
        let output = JSONValue.int(metadata["tokensOut"])
        let cacheRead = JSONValue.int(metadata["cacheReads"])
        let cacheWrite = JSONValue.int(metadata["cacheWrites"])
        let logged = (metadata["totalCost"] as? NSNumber)?.doubleValue
        let cost = (logged ?? 0) > 0 ? logged : nil
        guard input + output + cacheRead + cacheWrite > 0 || cost != nil
        else { return .empty(key: key) }
        // `ended_at` may be null while the session runs; fall back to start.
        guard let timestamp = JSONValue.string(object["ended_at"]).flatMap(Timestamp.parse)
            ?? JSONValue.string(object["started_at"]).flatMap(Timestamp.parse)
        else { return .empty(key: key) }

        let event = UsageEvent(
            harness: "cline", accountHome: accountHome, sessionId: sessionId,
            cwd: JSONValue.string(object["cwd"] ?? object["workspace_root"]),
            model: JSONValue.string(object["model"]) ?? "unknown",
            timestamp: timestamp,
            inputTokens: input, outputTokens: output,
            cacheReadTokens: cacheRead, cacheWriteTokens: cacheWrite,
            costUSD: cost, isSessionTotal: true)
        return .record(LedgerRecord(key: key, authoritative: true, event: event))
    }

    /// Grok's `costUsdTicks` is USD times this. A session of a few dollars
    /// lands in the hundreds of billions of ticks.
    static let grokTicksPerUSD = 10_000_000_000.0

    /// Grok: `<home>/sessions/<project>/<id>/usage.json`. One record per
    /// finished turn. `session` repeats those turns as a cumulative total,
    /// so it is never stored. `costUsdTicks` is Grok's own dollar figure
    /// and wins over the catalog. `.dropped` names the turns this file
    /// used to store and no longer does, so a rewrite deletes only those.
    static func grok(usage url: URL, accountHome: String) throws -> [SessionTotals] {
        let sessionId = url.deletingLastPathComponent().lastPathComponent
        let prefix = "grok:\(sessionId):"
        let object = try readJSON(url)
        let turns = object["turns"] as? [[String: Any]] ?? []
        let cwd = grokCwd(beside: url)
        var out: [SessionTotals] = []
        var seen = Set<Int>()
        for turn in turns {
            guard let number = turn["turnNumber"] as? Int, number >= 0, seen.insert(number).inserted
            else { continue }
            let key = prefix + String(number)
            guard let timestamp = JSONValue.string(turn["endedAt"]).flatMap(Timestamp.parse)
            else {
                out.append(.empty(key: key))
                continue
            }
            let input = JSONValue.int(turn["inputTokens"])
            let output = JSONValue.int(turn["outputTokens"])
            let cacheRead = JSONValue.int(turn["cachedReadTokens"])
            let cacheWrite = JSONValue.int(turn["cacheCreationTokens"])
            let reasoning = JSONValue.int(turn["reasoningTokens"])
            let cost = grokCost(turn["costUsdTicks"])
            guard input + output + cacheRead + cacheWrite + reasoning > 0 || cost != nil
            else {
                out.append(.empty(key: key))
                continue
            }
            let event = UsageEvent(
                harness: "grok", accountHome: accountHome, sessionId: sessionId, cwd: cwd,
                model: JSONValue.string(turn["primaryModelId"]) ?? "unknown",
                timestamp: timestamp,
                inputTokens: input, outputTokens: output,
                cacheReadTokens: cacheRead, cacheWriteTokens: cacheWrite,
                reasoningTokens: reasoning, costUSD: cost)
            out.append(.record(LedgerRecord(
                key: key, authoritative: true, skipsLookalikes: true, event: event)))
        }
        out.append(.kept(prefix: prefix, turns: seen))
        return out
    }

    /// `prompt_context.json` beside `usage.json`. A missing or unreadable
    /// sibling leaves cwd unset; it is not part of the cursor, so a
    /// context written later waits for the next usage rewrite.
    private static func grokCwd(beside usage: URL) -> String? {
        let context = usage.deletingLastPathComponent().appendingPathComponent("prompt_context.json")
        guard let data = try? Data(contentsOf: context),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return JSONValue.string(object["working_directory"])
    }

    /// USD from `costUsdTicks`, or nil so the catalog prices the turn.
    /// Zero and anything that is not a finite positive number fall through.
    private static func grokCost(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber,
              !(number === kCFBooleanTrue || number === kCFBooleanFalse)
        else { return nil }
        let ticks = number.doubleValue
        guard ticks.isFinite, ticks > 0 else { return nil }
        return ticks / grokTicksPerUSD
    }

    /// A read or parse failure throws, so `scanSessionFile` skips the file
    /// without saving its cursor and retries it on the next scan.
    private static func readJSON(_ url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { throw CocoaError(.coderReadCorrupt) }
        return object
    }
}
