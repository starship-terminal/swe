import Foundation

/// Maps a model name the logs carry (`claude-opus-5-thinking-high-fast`,
/// `cursor-grok-4.6-high-fast`, `openai/gpt-6.1-sol@20261003`) to an
/// Artificial Analysis model.
///
/// Both sides parse the same way. A name is a family (the vendor and
/// model words plus the version number) followed by optional run
/// qualifiers: an effort (`high`, `(max)`), a mode (`thinking`,
/// `reasoning`, `(Auto)`), fast inference (`-fast`), and release notes
/// (`preview`, `(Sep '25)`, a snapshot date). The qualifiers are pulled
/// out and the family is compared as a key, sorted words plus ordered
/// numbers, so `claude-4.5-sonnet` and `claude-sonnet-4-5` meet.
///
/// A mode word can also be part of a model's name: Kimi K2 Thinking,
/// Sonar Reasoning and Grok 4 Fast are models of their own. So an AA
/// name keeps those words, and a logged name tries them first
/// (`kimi-k2-thinking`, `grok-4-fast`) before falling back to the family
/// without them (`claude-opus-5-thinking-high-fast` is Claude Opus 5).
///
/// A logged name resolves to the foundation model, not to the effort or
/// mode a harness ran it at: `claude-opus-5-thinking-high-fast` is Claude
/// Opus 5, scored and priced as AA's headline entry for it. The effort and
/// mode stay on the row as its qualifier.
public struct ModelMatcher: Sendable {
    /// A name's comparable shape: the family plus the qualifiers that pick
    /// between variants of it.
    public struct Parsed: Sendable, Equatable {
        /// The name in reading order without its run qualifiers but with
        /// any mode word inside it, dash-joined: "claude-opus-5-thinking".
        /// The key manual links are saved under.
        public var base: String
        /// minimal|low|medium|high|xhigh|max, when the name said one.
        public var effort: String?
        /// The name ended `-fast`, which AA models never spell out.
        public var fast: Bool
        /// The name said thinking or reasoning.
        public var thinking: Bool = false
        /// A preview or dated release: a family's stable entry beats it.
        public var prerelease: Bool = false

        /// The name as a model of its own: `base` plus "fast" when it
        /// ended so. What an AA entry groups by.
        public var family: String {
            ModelMatcher.familyKey(base.split(separator: "-").map(String.init) + (fast ? ["fast"] : []))
        }

        /// The family without the fast-run suffix: a logged
        /// `kimi-k2-thinking-fast` is Kimi K2 Thinking run fast.
        public var slowFamily: String {
            ModelMatcher.familyKey(base.split(separator: "-").map(String.init))
        }

        /// The family with every mode word dropped: what a logged name
        /// falls back to when no model carries those words.
        public var looseFamily: String {
            ModelMatcher.familyKey(base.split(separator: "-").map(String.init)
                .filter { !ModelMatcher.modeWords.contains($0) && $0 != "non" })
        }
    }

    /// What two names must share to be one model: the words sorted, the
    /// numbers in order. Word order differs between vendors
    /// (`claude-4.5-sonnet`, `claude-sonnet-4-5`); version order never does.
    static func familyKey(_ tokens: [String]) -> String {
        let words = tokens.filter { !$0.allSatisfy(\.isNumber) }.sorted()
        let numbers = tokens.filter { $0.allSatisfy(\.isNumber) }
        return words.joined(separator: "-") + "|" + numbers.joined(separator: "-")
    }

    public static let efforts = ["minimal", "low", "medium", "high", "xhigh", "max"]
    /// A family with no effort-free slug: which variant stands for it.
    private static let effortPreference = ["max", "xhigh", "high", "medium", "low", "minimal"]
    /// An AA name's effort tail, case-insensitive: "(max)",
    /// "(Xhigh, Default Fallback)".
    private static let effortTail =
        #"(?i)\s*\((minimal|low|medium|high|xhigh|max)(,[^)]*)?\)\s*$"#
    /// Words that say how a model ran, not which model it is.
    private static let thinkingWords: Set<String> = ["thinking", "reasoning"]
    static let modeWords: Set<String> = thinkingWords.union(["adaptive", "auto", "latest"])
    /// Harness names some tools put in front of the model's own name.
    private static let harnessPrefixes: Set<String> = ["cursor", "windsurf"]
    private static let months: Set<String> = [
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    ]

    /// Splits a logged or AA-listed name into its family and qualifiers.
    public static func parse(_ name: String) -> Parsed {
        var text = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        var effort: String?
        var thinking = false
        var prerelease = false
        // The effort tail: "Claude Opus 5.5 (max)", or with a note after
        // the effort, "(Max, Default Fallback)".
        if let range = text.range(of: effortTail, options: .regularExpression) {
            let tail = text[range].drop { $0 != "(" }.dropFirst()
            effort = String(tail.prefix { $0.isLetter })
            text.removeSubrange(range)
        }
        // A provider prefix: "openai/gpt-6.1-sol".
        if let slash = text.lastIndex(of: "/") {
            text = String(text[text.index(after: slash)...])
        }
        // Other bracketed notes: a mode ("(Auto)", "(Reasoning)",
        // "(Non-reasoning, High)") or a release ("(Sep '25)") goes; any
        // other note is part of the name and stays as words.
        while let range = text.range(of: #"\(([^()]*)\)"#, options: .regularExpression) {
            let words = tokens(String(text[range].dropFirst().dropLast()))
            if words.contains(where: months.contains) {
                prerelease = true
            } else if words.allSatisfy({ modeWords.contains($0) || efforts.contains($0) || $0 == "non" }) {
                if words.contains(where: thinkingWords.contains), !words.contains("non") { thinking = true }
            } else {
                text.replaceSubrange(range, with: " " + words.joined(separator: " ") + " ")
                continue
            }
            text.replaceSubrange(range, with: " ")
        }
        text = text.trimmingCharacters(in: .whitespaces)
        // Fast, effort and snapshot dates trail the family and stack in
        // any order ("model-max@20261003-fast"): strip until nothing moves.
        var fast = false
        var changed = true
        while changed {
            changed = false
            if let range = text.range(of: #"[-\s]fast$"#, options: .regularExpression) {
                fast = true
                text.removeSubrange(range)
                changed = true
            }
            if effort == nil,
               let range = text.range(of: #"[-\s](minimal|low|medium|high|xhigh|max)$"#,
                                      options: .regularExpression) {
                effort = String(text[range].dropFirst())
                text.removeSubrange(range)
                changed = true
            }
            // Snapshot dates: "@20251001", "-2025-10-01", "-20251001".
            for pattern in [#"@\d{8}$"#, #"-\d{4}-\d{2}-\d{2}$"#, #"-\d{8}$"#] {
                if let range = text.range(of: pattern, options: .regularExpression) {
                    text.removeSubrange(range)
                    changed = true
                }
            }
        }
        // Words inside the name: "preview" is a release note and goes;
        // a mode word ("claude-opus-5-thinking") stays in `base` and
        // only `looseFamily` drops it.
        var words = tokens(text)
        for (index, word) in words.enumerated()
        where thinkingWords.contains(word) && (index == 0 || words[index - 1] != "non") {
            thinking = true
        }
        if words.contains("preview") { prerelease = true }
        words.removeAll { $0 == "preview" }
        if words.count > 1, let first = words.first, harnessPrefixes.contains(first) {
            words.removeFirst()
        }
        return Parsed(base: words.joined(separator: "-"), effort: effort, fast: fast,
                      thinking: thinking, prerelease: prerelease)
    }

    /// Lowercase alphanumeric runs: "Gemini 2.5 Flash-Lite" is gemini, 2,
    /// 5, flash, lite. "+" is a word ("Command A+" is not Command A).
    private static func tokens(_ text: String) -> [String] {
        text.lowercased().replacingOccurrences(of: "+", with: " plus ")
            .split { !$0.isLetter && !$0.isNumber }.map(String.init)
    }

    /// What an AA entry's name parses to; the slug's trailing token is a
    /// fallback effort when the name has no qualifier.
    private static func parseAA(_ model: AALanguageModel) -> Parsed {
        var parsed = parse(model.name)
        if parsed.effort == nil,
           let range = model.slug.lowercased().range(of: #"-(minimal|low|medium|high|xhigh|max)$"#,
                                                     options: .regularExpression) {
            parsed.effort = String(model.slug.lowercased()[range].dropFirst())
        }
        return parsed
    }

    /// `~/.swe/config/model-links.json`: `{"logged base": "aa id"}`.
    /// A manual link beats every rule. Read entry by entry, so a value
    /// that isn't a string skips only itself; a missing file, or one that
    /// isn't a JSON object, is no links.
    public static func loadLinks(from url: URL) -> [String: String] {
        guard let object = try? readLinks(url) else { return [:] }
        return object.compactMapValues { $0 as? String }
    }

    /// Write `links` over the file's own object: an entry the reader
    /// skips stays as it is, and a file that is there but isn't a JSON
    /// object throws instead of being replaced, so one bad character
    /// never costs every saved link.
    public static func saveLinks(_ links: [String: String], to url: URL) throws {
        var object = (try readLinks(url) ?? [:]).filter { !($0.value is String) }
        for (base, id) in links { object[base] = id }
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }

    /// The links file's object; nil when there is no file (or it is
    /// blank). Throws `CocoaError(.fileReadCorruptFile)` when it is there
    /// but isn't a JSON object.
    private static func readLinks(_ url: URL) throws -> [String: Any]? {
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile
                    || error.code == .fileNoSuchFile {
            return nil
        }
        if data.allSatisfy({ [0x20, 0x09, 0x0A, 0x0D].contains($0) }) { return nil }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CocoaError(.fileReadCorruptFile, userInfo: [NSFilePathErrorKey: url.path])
        }
        return object
    }

    /// The AA model `logged` names, or nil: the headline entry of the
    /// family a manual link points into, else of the family `logged`
    /// parses to. A link saved against "Claude Opus 5.5 (High)" lands on
    /// the same Opus 5.5 entry the leaderboard shows.
    public static func match(_ logged: String, in models: [AALanguageModel],
                             links: [String: String] = [:]) -> AALanguageModel? {
        Index(models: models, links: links).match(logged)?.model
    }

    /// `match` for many names against one model list: each AA model
    /// parses once, not once per name.
    public struct Index: Sendable {
        private let models: [AALanguageModel]
        private let links: [String: String]
        private let byFamily: [String: [AALanguageModel]]

        public init(models: [AALanguageModel], links: [String: String] = [:]) {
            self.models = models
            self.links = links
            var byFamily: [String: [AALanguageModel]] = [:]
            for model in models { byFamily[parseAA(model).family, default: []].append(model) }
            self.byFamily = byFamily
        }

        /// The model `logged` names, and whether a manual link decided it.
        public func match(_ logged: String) -> (model: AALanguageModel, linked: Bool)? {
            let parsed = parse(logged)
            if let linked = links[parsed.base] ?? links[legacyKey(logged)],
               let model = models.first(where: { $0.id == linked }) {
                return (headline(of: byFamily[parseAA(model).family] ?? []) ?? model, true)
            }
            guard !parsed.base.isEmpty else { return nil }
            // Most specific first: as named, then without the fast-run
            // suffix, then without any mode word.
            for key in [parsed.family, parsed.slowFamily, parsed.looseFamily] {
                if let hits = byFamily[key], let best = headline(of: hits) { return (best, false) }
            }
            return nil
        }
    }

    /// The key links were saved under before names parsed into families:
    /// effort tail, provider prefix, then trailing fast/effort/date off,
    /// dots and spaces to dashes. Read as a fallback so those links hold.
    static func legacyKey(_ name: String) -> String {
        var base = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        var effort = false
        if let range = base.range(of: effortTail, options: .regularExpression) {
            effort = true
            base.removeSubrange(range)
            base = base.trimmingCharacters(in: .whitespaces)
        }
        if let slash = base.lastIndex(of: "/") {
            base = String(base[base.index(after: slash)...])
        }
        var changed = true
        while changed {
            changed = false
            if let range = base.range(of: #"-fast$"#, options: .regularExpression) {
                base.removeSubrange(range)
                changed = true
            }
            if !effort,
               let range = base.range(of: #"-(minimal|low|medium|high|xhigh|max)$"#,
                                      options: .regularExpression) {
                effort = true
                base.removeSubrange(range)
                changed = true
            }
            for pattern in [#"@\d{8}$"#, #"-\d{4}-\d{2}-\d{2}$"#, #"-\d{8}$"#] {
                if let range = base.range(of: pattern, options: .regularExpression) {
                    base.removeSubrange(range)
                    changed = true
                }
            }
        }
        return base.replacingOccurrences(of: ".", with: "-").replacingOccurrences(of: " ", with: "-")
    }

    /// One entry per foundation model, in the order each first appears:
    /// what the leaderboard lists, so Opus 5.5 is one bar, not four.
    public static func headlines(in models: [AALanguageModel]) -> [AALanguageModel] {
        var order: [String] = []
        var families: [String: [AALanguageModel]] = [:]
        for model in models {
            let family = parseAA(model).family
            if families[family] == nil { order.append(family) }
            families[family, default: []].append(model)
        }
        return order.compactMap { families[$0].flatMap(headline) }
    }

    /// The entry AA headlines a family with: a stable release before a
    /// preview or dated one; within that, the slug with no effort suffix
    /// (`claude-opus-5-5`, AA's own default run), else the highest effort
    /// listed. Ties, and a reasoning run against a non-reasoning one,
    /// take the highest intelligence index.
    ///
    /// An entry AA has scored beats one it hasn't: each new Intelligence
    /// Index version is run on only some efforts of an older model (v5
    /// scored Grok 4.6 at Xhigh and left `grok-4-6` empty), and the family
    /// should show the score it has.
    private static func headline(of family: [AALanguageModel]) -> AALanguageModel? {
        // Among entries with no current score, the best kept one leads.
        func rank(_ model: AALanguageModel) -> (Double, Double) {
            (model.intelligence ?? -1, model.pastIntelligence?.value ?? 0)
        }
        func best(_ pool: [AALanguageModel]) -> AALanguageModel? {
            pool.max { rank($0) < rank($1) }
        }
        let stable = family.filter { !parseAA($0).prerelease }
        for candidates in [stable, family] where !candidates.isEmpty {
            let scored = candidates.filter { $0.intelligence != nil }
            let pool = scored.isEmpty ? candidates : scored
            if let plain = best(pool.filter { !slugHasEffort($0) }) { return plain }
            for wanted in effortPreference {
                if let hit = best(pool.filter { parseAA($0).effort == wanted }) { return hit }
            }
            return best(pool)
        }
        return nil
    }

    private static func slugHasEffort(_ model: AALanguageModel) -> Bool {
        model.slug.lowercased()
            .range(of: #"-(minimal|low|medium|high|xhigh|max)$"#, options: .regularExpression) != nil
    }

    /// The foundation model's name: "Claude Opus 5.5" for "Claude Opus
    /// 5.5 (Max, Default Fallback)", "Claude Opus 4.5" for "Claude Opus
    /// 4.5 (Reasoning)". A note that names the model ("Grok 2 (Dec '24)")
    /// stays.
    public static func displayName(_ model: AALanguageModel) -> String {
        var name = model.name
        if let range = name.range(of: effortTail, options: .regularExpression) {
            name = String(name[..<range.lowerBound])
        }
        while let range = name.range(of: #"\s*\(([^()]*)\)\s*$"#, options: .regularExpression) {
            let words = tokens(String(name[range]))
            guard words.allSatisfy({ modeWords.contains($0) || efforts.contains($0) || $0 == "non" })
            else { break }
            name = String(name[..<range.lowerBound])
        }
        return name
    }
}
