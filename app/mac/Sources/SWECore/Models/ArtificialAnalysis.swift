import Foundation

/// One language model from Artificial Analysis's `/api/v2/language/models`
/// (Pro) or `/api/v2/language/models/free`. Rows stored before the V2
/// migration hold the legacy `/api/v2/data/llms/models` shape, so the
/// decoder reads both.
/// `id` is AA's stable key; names and slugs change. `evaluations` keeps
/// every metric AA publishes — the dict grows when they add a benchmark,
/// so the Models page reads keys rather than a fixed field list.
public struct AALanguageModel: Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var slug: String
    /// model_creator's id, name and slug (the slug drives provider colours).
    /// The V2 API dropped the slug, so a new row's slug is its creator name
    /// lowercased with everything but letters and digits removed.
    public var creatorID: String
    public var creatorName: String
    public var creatorSlug: String
    /// evaluation key → score; AA's nulls are dropped on decode.
    public var evaluations: [String: Double]
    /// USD per 1M tokens, 3:1 input:output blend. Pro-only on the V2 API;
    /// computed from the input and output prices when absent.
    public var priceBlended: Double?
    public var priceInput: Double?
    public var priceOutput: Double?
    /// Output tokens per second: the `performance_v3` mean, or the median in older stored rows.
    public var speed: Double?
    /// Seconds to first token: the `performance_v3` mean, or the median in older stored rows.
    public var timeToFirstToken: Double?
    /// Seconds to first answer token (reasoning models): the `performance_v3` mean, or the median in older stored rows.
    public var timeToFirstAnswer: Double?
    /// The last intelligence index AA published for the model, when the
    /// current feed has none. Not part of AA's row: the catalog fills it
    /// from the scores it kept (see `AAPastScore`).
    public var pastIntelligence: AAPastScore?

    public init(id: String, name: String, slug: String,
                creatorID: String = "", creatorName: String = "", creatorSlug: String = "",
                evaluations: [String: Double] = [:],
                priceBlended: Double? = nil, priceInput: Double? = nil, priceOutput: Double? = nil,
                speed: Double? = nil, timeToFirstToken: Double? = nil,
                timeToFirstAnswer: Double? = nil) {
        self.id = id
        self.name = name
        self.slug = slug
        self.creatorID = creatorID
        self.creatorName = creatorName
        self.creatorSlug = creatorSlug
        self.evaluations = evaluations
        self.priceBlended = priceBlended
        self.priceInput = priceInput
        self.priceOutput = priceOutput
        self.speed = speed
        self.timeToFirstToken = timeToFirstToken
        self.timeToFirstAnswer = timeToFirstAnswer
    }
}

/// An intelligence index from a feed older than the current one. AA
/// re-runs only some models when it moves the index to a new version
/// (v5 left most models released before August 2026 without a score), and
/// versions are not on one scale: Grok 4.6 (Xhigh) scored 44 before v5 and
/// 31 on it. So a kept score is drawn apart from current ones, never
/// ranked against them.
public struct AAPastScore: Sendable, Equatable {
    public var value: Double
    /// The feed's `intelligence_index_version` ("5"); nil for the legacy
    /// feed, which never said.
    public var indexVersion: String?
    /// When the feed that carried the score was downloaded.
    public var scoredAt: Date?

    public init(value: Double, indexVersion: String? = nil, scoredAt: Date? = nil) {
        self.value = value
        self.indexVersion = indexVersion
        self.scoredAt = scoredAt
    }

    /// The short mark under a bar's number: "v5", else "Oct ’26", else
    /// "older".
    public var tag: String {
        if let indexVersion { return "v\(indexVersion)" }
        if let scoredAt { return Self.format(scoredAt, "MMM ’yy") }
        return "older"
    }

    /// The sentence the model card and VoiceOver give.
    public var note: String {
        var text = "Scored on "
        text += indexVersion.map { "Intelligence Index v\($0)" } ?? "an older Intelligence Index version"
        if let scoredAt { text += ", last published \(Self.format(scoredAt, "MMMM yyyy"))" }
        return text + ". Not on the same scale as current scores."
    }

    private static func format(_ date: Date, _ pattern: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = pattern
        return formatter.string(from: date)
    }
}

extension AALanguageModel {
    /// The intelligence index AA headlines with.
    public var intelligence: Double? { evaluations["artificial_analysis_intelligence_index"] }
    /// The coding score: AA's Terminal-Bench 4.0 pass rate as 0–100, run on
    /// every new model. AA's Coding Index (`artificial_analysis_coding_index`)
    /// is built on Terminal-Bench 2.1, which AA stopped running when
    /// Intelligence Index v4.3 (September 2026) moved to 4.0, so it is empty
    /// for every model released since. The versions are not on one scale
    /// (2.1 scores run about 50 points higher for the same model), so a
    /// model AA never ran on 4.0 has no score rather than an older one.
    public var coding: Double? { evaluations[Self.codingEvaluation].map { $0 * 100 } }
    public static let codingEvaluation = "terminalbench_v4_0"
    /// The hover note where `coding` is nil.
    public static let noCodingScore = "Artificial Analysis hasn't run Terminal-Bench 4.0 on this model, or your API key is on the Free tier, which doesn't include it"
    /// The reasoning score: AA's Humanity's Last Exam accuracy as 0–100,
    /// run on every new model. It stands in for AA's Maths Index
    /// (`artificial_analysis_math_index`, built on AIME 2025), which AA
    /// retired with Intelligence Index v4.0 (January 2026), so no model
    /// released since has one. HLE is the closest current evaluation:
    /// expert-level questions, about 40% of them maths, on one question set.
    public var reasoning: Double? { evaluations[Self.reasoningEvaluation].map { $0 * 100 } }
    public static let reasoningEvaluation = "hle"
    /// The hover note where `reasoning` is nil.
    public static let noReasoningScore = "Artificial Analysis hasn't run Humanity's Last Exam on this model, or your API key is on the Free tier, which doesn't include it"
}

extension AALanguageModel: Decodable {
    private enum CodingKeys: String, CodingKey {
        case id, name, slug
        case creator = "model_creator"
        case evaluations, pricing, performance
        // `performance` goes null on 2026-12-10; `performance_v3` replaces it.
        case performanceV3 = "performance_v3"
        // Legacy top-level keys; V2 nests them under `performance`.
        case speed = "median_output_tokens_per_second"
        case ttft = "median_time_to_first_token_seconds"
        case ttfat = "median_time_to_first_answer_token"
    }

    private enum PerformanceKeys: String {
        case speed = "median_output_tokens_per_second"
        case ttft = "median_time_to_first_token_seconds"
        case ttfat = "median_time_to_first_answer_token_seconds"
    }

    /// `performance_v3` reports means where `performance` reported medians.
    private enum PerformanceV3Keys: String {
        case speed = "mean_output_tokens_per_second"
        case ttft = "mean_time_to_first_token_seconds"
        case ttfat = "mean_time_to_first_answer_token_seconds"
    }

    private enum PricingKeys: String, CodingKey {
        case blended = "price_1m_blended_3_to_1"
        case input = "price_1m_input_tokens"
        case output = "price_1m_output_tokens"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? id
        slug = try c.decodeIfPresent(String.self, forKey: .slug) ?? id
        // Optional values: Pro adds `country`, which may be null.
        let creator = (try c.decodeIfPresent([String: String?].self, forKey: .creator) ?? [:])
            .compactMapValues { $0 }
        creatorID = creator["id"] ?? ""
        creatorName = creator["name"] ?? ""
        creatorSlug = creator["slug"] ?? Self.derivedSlug(creatorName)
        // [String: Double?] decodes every key AA adds; nulls drop out.
        let raw = try c.decodeIfPresent([String: Double?].self, forKey: .evaluations) ?? [:]
        evaluations = raw.compactMapValues { $0 }
        // AA writes 0 where it has no measurement: no API price (277 of
        // 704 models in October 2026) or no speed run. Zero is "unknown",
        // so every page shows "—" and the cost rank leaves them out rather
        // than ranking them cheapest.
        func measured(_ value: Double?) -> Double? { value.flatMap { $0 > 0 ? $0 : nil } }
        let pricing = try c.decodeIfPresent([String: Double?].self, forKey: .pricing) ?? [:]
        priceInput = measured(pricing[PricingKeys.input.rawValue] ?? nil)
        priceOutput = measured(pricing[PricingKeys.output.rawValue] ?? nil)
        // The Free tier has no blended price: blend 3:1 the way AA does.
        priceBlended = measured(pricing[PricingKeys.blended.rawValue] ?? nil)
            ?? priceInput.flatMap { input in priceOutput.map { (3 * input + $0) / 4 } }
        // Prefer `performance_v3` (means), then `performance` (medians, null
        // from 2026-12-10), then the legacy top-level keys.
        let v3 = try c.decodeIfPresent([String: Double?].self, forKey: .performanceV3) ?? [:]
        let performance = try c.decodeIfPresent([String: Double?].self, forKey: .performance) ?? [:]
        speed = measured(try (v3[PerformanceV3Keys.speed.rawValue] ?? nil)
            ?? (performance[PerformanceKeys.speed.rawValue] ?? nil)
            ?? c.decodeIfPresent(Double.self, forKey: .speed))
        timeToFirstToken = measured(try (v3[PerformanceV3Keys.ttft.rawValue] ?? nil)
            ?? (performance[PerformanceKeys.ttft.rawValue] ?? nil)
            ?? c.decodeIfPresent(Double.self, forKey: .ttft))
        timeToFirstAnswer = measured(try (v3[PerformanceV3Keys.ttfat.rawValue] ?? nil)
            ?? (performance[PerformanceKeys.ttfat.rawValue] ?? nil)
            ?? c.decodeIfPresent(Double.self, forKey: .ttfat))
    }

    /// "Moonshot AI" -> "moonshotai", "Z AI" -> "zai": keys `ModelBrands`
    /// already matches AA's old creator slugs on.
    static func derivedSlug(_ name: String) -> String {
        String(String.UnicodeScalarView(name.lowercased().unicodeScalars.filter {
            CharacterSet.alphanumerics.contains($0)
        }))
    }
}

/// The list endpoint's envelope: V2's `{tier, intelligence_index_version,
/// pagination, data: [...]}` or the legacy `{status, prompt_options, data}`.
public struct AAModelsResponse: Sendable {
    public var models: [AALanguageModel]

    public init(models: [AALanguageModel]) { self.models = models }

    /// The feed's `intelligence_index_version` as text ("5", "4.3"); nil
    /// for the legacy feed, which has none.
    public static func indexVersion(_ data: Data) -> String? {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        switch root["intelligence_index_version"] {
        case let text as String: return text.isEmpty ? nil : text
        case let number as NSNumber: return number.stringValue
        default: return nil
        }
    }

    /// Decodes the response body; throws on anything but a `data` array.
    public static func decode(_ data: Data) throws -> [AALanguageModel] {
        let root = try JSONSerialization.jsonObject(with: data)
        guard let dict = root as? [String: Any], let rows = dict["data"] as? [Any] else {
            throw AAError.badResponse
        }
        let decoded = try JSONDecoder().decode([AALanguageModel].self, from: JSONSerialization.data(withJSONObject: rows))
        return decoded
    }
}

public enum AAError: Error, Equatable {
    case noKey
    case http(Int)
    case badResponse
    case network(String)
}

/// The endpoints a refresh downloads. Media feeds are stored for a later
/// page; only `llms` is read today. `fileName` is the raw-response file
/// older builds wrote, read only by the one-time import. Each points at
/// the V2 Free route, which every key can read; `llms` tries Pro first.
public enum AAEndpoint: String, CaseIterable, Sendable {
    case llms = "llms"
    case textToImage = "text-to-image"
    case imageEditing = "image-editing"
    case textToSpeech = "text-to-speech"
    case textToVideo = "text-to-video"
    case imageToVideo = "image-to-video"

    public var fileName: String { "\(rawValue).json" }

    /// The path under https://artificialanalysis.ai.
    public var path: String {
        switch self {
        case .llms: "/api/v2/language/models/free"
        case .textToImage: "/api/v2/media/text-to-image/models/free"
        case .imageEditing: "/api/v2/media/image-editing/models/free"
        case .textToSpeech: "/api/v2/media/text-to-speech/models/free"
        case .textToVideo: "/api/v2/media/text-to-video/models/free"
        case .imageToVideo: "/api/v2/media/image-to-video/models/free"
        }
    }

    public var url: URL { URL(string: "https://artificialanalysis.ai\(path)")! }

    /// The Pro language feed: adds per-benchmark scores (Coding, Reasoning)
    /// and the blended price. A Free key gets 403 and the refresh falls
    /// back to `url`. `prompt_type=medium` (1k-token prompts) matches the
    /// legacy feed's speeds; the Pro default is `long`.
    public static let proLLMsURL = URL(string: "https://artificialanalysis.ai/api/v2/language/models?prompt_type=medium")!

    /// The language feed pages at 200 models; this caps a runaway loop.
    static let maxPages = 25
}

/// Downloaded Artificial Analysis data, kept in the catalog database:
/// each endpoint's last good response, the language models as rows, and a
/// fetch row per attempt (status, error, rate-limit headers).
public struct ArtificialAnalysisStore: Sendable {
    /// When the models feed is this old the app quietly refreshes at launch.
    public static let staleAfter: TimeInterval = 7 * 86_400

    /// Per-endpoint bookkeeping: the last good fetch's time, and the latest
    /// attempt's status, error and headers. Also the shape of the
    /// `meta.json` older builds wrote.
    public struct Meta: Sendable, Equatable, Codable {
        public var fetchedAt: TimeInterval?
        /// The last HTTP status, nil when no request completed.
        public var status: Int?
        /// A failed refresh's message; cleared by the next success.
        public var error: String?
        /// Any X-RateLimit-* headers the last response carried.
        public var rateLimitHeaders: [String: String]?

        public init(fetchedAt: TimeInterval? = nil, status: Int? = nil,
                    error: String? = nil, rateLimitHeaders: [String: String]? = nil) {
            self.fetchedAt = fetchedAt
            self.status = status
            self.error = error
            self.rateLimitHeaders = rateLimitHeaders
        }
    }

    public let database: CatalogDatabase
    private let now: @Sendable () -> Date

    /// Where older builds kept the raw files; imported once.
    public static func defaultDirectory(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home.appending(path: ".swe/data/artificial-analysis")
    }

    /// `SWE_AA_DATA_DIR` wins (`QUIVER_AA_DATA_DIR` kept as a fallback),
    /// so a test build never touches the real data.
    public static func resolvedDirectory(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        let environment = ProcessInfo.processInfo.environment
        if let override = environment["SWE_AA_DATA_DIR"] ?? environment["QUIVER_AA_DATA_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        return defaultDirectory(home: home)
    }

    /// - Parameter legacyDirectory: the `data/artificial-analysis/` folder
    ///   older builds wrote; imported into `database` once if it holds no
    ///   AA data yet.
    public init(database: CatalogDatabase, legacyDirectory: URL? = nil,
                now: @escaping @Sendable () -> Date = Date.init) {
        self.database = database
        self.now = now
        if let legacyDirectory { database.importLegacyAA(directory: legacyDirectory) }
        database.backfillLastScores(legacyDirectory: legacyDirectory)
    }

    /// A store over `directory/catalog.sqlite` that imports any raw files
    /// already in `directory`: for tests and previews.
    public init(directory: URL, now: @escaping @Sendable () -> Date = Date.init) {
        self.init(database: CatalogDatabase(url: directory.appending(path: "catalog.sqlite")),
                  legacyDirectory: directory, now: now)
    }

    public func raw(_ endpoint: AAEndpoint) -> Data? {
        database.payload(endpoint)
    }

    /// The decoded language models, or nil when no good fetch has landed.
    public func languageModels() -> [AALanguageModel]? {
        database.languageModels()
    }

    public func meta() -> [String: Meta] {
        var result: [String: Meta] = [:]
        for endpoint in AAEndpoint.allCases {
            let source = CatalogDatabase.aaSource(endpoint)
            guard let latest = database.lastAttempt(source: source) else { continue }
            result[endpoint.rawValue] = Meta(
                fetchedAt: database.lastSuccess(source: source)?.fetchedAt?.timeIntervalSince1970,
                status: latest.status, error: latest.error, rateLimitHeaders: latest.rateLimitHeaders)
        }
        return result
    }

    /// The models feed's last good fetch: its time is the page's
    /// "Updated" stamp, and a new id means the models table changed.
    public func llmsFetch() -> CatalogDatabase.Fetch? {
        database.lastSuccess(source: CatalogDatabase.aaSource(.llms))
    }

    /// Whether the models feed is missing or past `staleAfter`, which is
    /// when a launch may refresh without being asked.
    public func llmsIsStale() -> Bool {
        guard let fetchedAt = llmsFetch()?.fetchedAt else { return true }
        return now().timeIntervalSince(fetchedAt) > Self.staleAfter
    }

    /// Stores one endpoint's good response and records the fetch.
    private func save(_ endpoint: AAEndpoint, data: Data, status: Int,
                      rateLimitHeaders: [String: String], url: URL) throws {
        try database.recordAAPayload(
            endpoint, body: data, at: now(), status: status,
            rateLimitHeaders: rateLimitHeaders.isEmpty ? nil : rateLimitHeaders,
            url: url.absoluteString)
    }

    /// A failure is only recorded; stored data is never clobbered.
    private func recordFailure(_ endpoint: AAEndpoint, error: String, status: Int?,
                               rateLimitHeaders: [String: String]? = nil, url: URL? = nil) {
        database.recordFailure(source: CatalogDatabase.aaSource(endpoint), url: url ?? endpoint.url, at: now(),
                               status: status, error: error,
                               rateLimitHeaders: rateLimitHeaders?.isEmpty == true ? nil : rateLimitHeaders)
    }

    /// One endpoint's outcome: the body to store, or why there is none.
    private enum Fetched {
        case body(Data, status: Int, headers: [String: String], url: URL)
        case failure(String, status: Int?, headers: [String: String], url: URL)
    }

    private static func message(forStatus status: Int) -> String {
        status == 410 ? "HTTP 410: Artificial Analysis retired this API. Update SWE." : "HTTP \(status)"
    }

    /// The headers worth keeping: the X-RateLimit-* quota (Reset is a Unix
    /// time), the key's X-AA-Tier, and a 429's Retry-After.
    private static func keptHeaders(_ response: HTTPURLResponse) -> [String: String] {
        response.allHeaderFields.reduce(into: [String: String]()) { out, pair in
            guard let name = pair.key as? String, let value = pair.value as? String else { return }
            let lower = name.lowercased()
            if lower.hasPrefix("x-ratelimit-") || lower == "x-aa-tier" || lower == "retry-after" {
                out[name] = value
            }
        }
    }

    private func send(_ url: URL, key: String, transport: any HTTPTransport) async throws
        -> (Data, HTTPURLResponse) {
        var request = URLRequest(url: url)
        request.setValue(key, forHTTPHeaderField: "x-api-key")
        return try await transport.send(request)
    }

    /// One media feed: a single unpaged request.
    private func fetchMedia(_ endpoint: AAEndpoint, key: String, transport: any HTTPTransport) async throws
        -> Fetched {
        let (data, response) = try await send(endpoint.url, key: key, transport: transport)
        let headers = Self.keptHeaders(response)
        guard (200..<300).contains(response.statusCode) else {
            return .failure(Self.message(forStatus: response.statusCode), status: response.statusCode,
                            headers: headers, url: endpoint.url)
        }
        return .body(data, status: response.statusCode, headers: headers, url: endpoint.url)
    }

    /// The language feed: the Pro route first (a Free key's 403 drops to
    /// the Free route), then every page while `pagination.has_more`. The
    /// pages merge into one body, so the `models` table is replaced only
    /// by the whole feed; a failed page fails the feed.
    private func fetchLLMs(key: String, transport: any HTTPTransport) async throws -> Fetched {
        var base = AAEndpoint.proLLMsURL
        var rows: [Any] = []
        var first: [String: Any]?
        var firstBody = Data()
        var page = 1
        var last: (status: Int, headers: [String: String]) = (200, [:])
        while true {
            let url = page == 1 ? base : Self.url(base, page: page)
            let (data, response) = try await send(url, key: key, transport: transport)
            let headers = Self.keptHeaders(response)
            if page == 1, base == AAEndpoint.proLLMsURL, response.statusCode == 403 {
                base = AAEndpoint.llms.url
                continue
            }
            guard (200..<300).contains(response.statusCode) else {
                return .failure(Self.message(forStatus: response.statusCode), status: response.statusCode,
                                headers: headers, url: url)
            }
            guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let pageRows = object["data"] as? [Any]
            else { return .failure("Unusable response", status: response.statusCode, headers: headers, url: url) }
            if first == nil { first = object; firstBody = data }
            rows += pageRows
            last = (response.statusCode, headers)
            // Only a clear end saves: a page without `has_more`, or an
            // empty page that claims more, would let a partial feed
            // delete the stored models missing from it.
            guard let hasMore = (object["pagination"] as? [String: Any])?["has_more"] as? Bool,
                  !(hasMore && pageRows.isEmpty)
            else { return .failure("Unusable response", status: response.statusCode, headers: headers, url: url) }
            guard hasMore else { break }
            guard page < AAEndpoint.maxPages else {
                return .failure("Too many pages", status: response.statusCode, headers: headers, url: url)
            }
            page += 1
        }
        var body = firstBody
        if page > 1, var merged = first {
            merged["data"] = rows
            merged["pagination"] = nil
            body = try JSONSerialization.data(withJSONObject: merged, options: [.sortedKeys])
        }
        guard (try? AAModelsResponse.decode(body)) != nil else {
            return .failure("Unusable response", status: last.status, headers: last.headers, url: base)
        }
        return .body(body, status: last.status, headers: last.headers, url: base)
    }

    private static func url(_ base: URL, page: Int) -> URL {
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.queryItems = (components.queryItems ?? []) + [URLQueryItem(name: "page", value: String(page))]
        return components.url!
    }

    /// Downloads all six endpoints. Each succeeds or records its own
    /// error; the errors dict tells the UI which ones failed.
    @discardableResult
    public func refresh(key: String, transport: any HTTPTransport) async -> [AAEndpoint: String] {
        var errors: [AAEndpoint: String] = [:]
        for endpoint in AAEndpoint.allCases {
            do {
                let fetched = endpoint == .llms
                    ? try await fetchLLMs(key: key, transport: transport)
                    : try await fetchMedia(endpoint, key: key, transport: transport)
                switch fetched {
                case let .body(data, status, headers, url):
                    try save(endpoint, data: data, status: status, rateLimitHeaders: headers, url: url)
                case let .failure(message, status, headers, url):
                    recordFailure(endpoint, error: message, status: status, rateLimitHeaders: headers, url: url)
                    errors[endpoint] = message
                }
            } catch {
                let message = error.localizedDescription
                recordFailure(endpoint, error: message, status: nil)
                errors[endpoint] = message
            }
        }
        if errors[.llms] == nil { database.rebuildAliases() }
        return errors
    }
}
