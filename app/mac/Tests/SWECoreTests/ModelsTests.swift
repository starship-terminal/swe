import Foundation
import Synchronization
import Testing
@testable import SWECore

/// A transport stub that records every request and answers from a handler.
/// Requests arrive off the test's actor, so the handler and the record sit
/// behind a lock.
private final class StubHTTP: HTTPTransport, Sendable {
    typealias Handler = @Sendable (URLRequest) throws -> (Data, Int, [String: String])
    private struct State {
        /// URL path → (body, status, headers); unmatched paths throw.
        var handler: Handler = { _ in ("{}".data(using: .utf8)!, 200, [:]) }
        var requests: [URLRequest] = []
    }
    private let state = Mutex(State())
    var handler: Handler {
        get { state.withLock { $0.handler } }
        set { state.withLock { $0.handler = newValue } }
    }
    var requests: [URLRequest] { state.withLock { $0.requests } }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let handler = state.withLock { s in
            s.requests.append(request)
            return s.handler
        }
        let (data, status, headers) = try handler(request)
        return (data, HTTPURLResponse(url: request.url!, statusCode: status,
                                      httpVersion: nil, headerFields: headers)!)
    }
}

@Suite struct ArtificialAnalysisDecodingTests {
    /// AA writes 0 for a price or speed it never measured; those read as
    /// unknown, not as free or stalled.
    @Test func zeroMeasurementsDecodeAsUnknown() throws {
        let json = """
        {"status": 200, "data": [
          {"id": "z", "name": "Muse Wonder Full (Max)", "slug": "muse",
           "model_creator": {"id": "m", "name": "Meta", "slug": "meta"},
           "evaluations": {"artificial_analysis_intelligence_index": 56.0},
           "pricing": {"price_1m_blended_3_to_1": 0, "price_1m_input_tokens": 0,
                       "price_1m_output_tokens": 0},
           "median_output_tokens_per_second": 0, "median_time_to_first_token_seconds": 0,
           "median_time_to_first_answer_token": 0},
          {"id": "p", "name": "GPT-6 Luna", "slug": "luna",
           "model_creator": {"id": "o", "name": "OpenAI", "slug": "openai"},
           "pricing": {"price_1m_blended_3_to_1": 0.2},
           "median_output_tokens_per_second": 128}
        ]}
        """
        let models = try AAModelsResponse.decode(Data(json.utf8))
        let unknown = try #require(models.first)
        #expect(unknown.priceBlended == nil && unknown.priceInput == nil && unknown.priceOutput == nil)
        #expect(unknown.speed == nil && unknown.timeToFirstToken == nil && unknown.timeToFirstAnswer == nil)
        #expect(unknown.intelligence == 56)
        #expect(models[1].priceBlended == 0.2 && models[1].speed == 128)
    }

    /// The docs' example shape plus a benchmark AA added later.
    private let fixture = """
    {"status": 200, "prompt_options": {},
     "data": [
       {"id": "m1", "name": "Claude Opus 5.5", "slug": "claude-opus-5-5",
        "model_creator": {"id": "c1", "name": "Anthropic", "slug": "anthropic"},
        "evaluations": {"artificial_analysis_intelligence_index": 58.2,
                        "artificial_analysis_coding_index": 71.0,
                        "terminalbench_v4_0": 0.55,
                        "hle": 0.61,
                        "mmlu_pro": 88.0, "brand_new_bench": 12.5,
                        "dropped_null": null},
        "pricing": {"price_1m_blended_3_to_1": 15.0,
                    "price_1m_input_tokens": 10.0,
                    "price_1m_output_tokens": 25.0},
        "median_output_tokens_per_second": 61.4,
        "median_time_to_first_token_seconds": 0.82,
        "median_time_to_first_answer_token": 1.9},
       {"id": "m2", "name": "GPT-6 Astra", "slug": "gpt-6-astra",
        "model_creator": {"id": "c2", "name": "OpenAI", "slug": "openai"},
        "evaluations": {"artificial_analysis_intelligence_index": 55.9,
                        "artificial_analysis_coding_index": 60.0, "terminalbench_v2_1": 0.88,
                        "artificial_analysis_math_index": 40.0},
        "pricing": {"price_1m_blended_3_to_1": 8.0},
        "median_output_tokens_per_second": 130.0}
     ]}
    """.data(using: .utf8)!

    @Test func decodesEveryFieldAndKeepsUnknownEvaluations() throws {
        let models = try AAModelsResponse.decode(fixture)
        #expect(models.count == 2)
        let first = try #require(models.first)
        #expect(first.id == "m1")
        #expect(first.name == "Claude Opus 5.5")
        #expect(first.creatorName == "Anthropic")
        #expect(first.creatorSlug == "anthropic")
        #expect(first.intelligence == 58.2)
        // Coding is Terminal-Bench 4.0 as a percentage, not the stale index.
        #expect(abs((first.coding ?? 0) - 55) < 1e-9)
        // Reasoning is Humanity's Last Exam as a percentage.
        #expect(abs((first.reasoning ?? 0) - 61) < 1e-9)
        #expect(ModelsModel.Metric.reasoning.format(61) == "61%")
        // AA adds benchmarks; the dict must keep them.
        #expect(first.evaluations["brand_new_bench"] == 12.5)
        // Nulls drop out instead of decoding as 0.
        #expect(first.evaluations["dropped_null"] == nil)
        #expect(first.priceBlended == 15.0)
        #expect(first.priceInput == 10.0)
        #expect(first.priceOutput == 25.0)
        #expect(first.speed == 61.4)
        #expect(first.timeToFirstToken == 0.82)
        #expect(first.timeToFirstAnswer == 1.9)
        // Missing sections leave nils, not crashes.
        let second = models[1]
        // Only the old index and Terminal-Bench 2.1: no coding score, since
        // 2.1 is not on 4.0's scale.
        #expect(second.coding == nil)
        // The retired Maths Index is not read as a reasoning score.
        #expect(second.reasoning == nil)
        #expect(ModelsModel.Metric.reasoning.missingNote == AALanguageModel.noReasoningScore)
        #expect(second.priceInput == nil)
        #expect(second.timeToFirstToken == nil)
    }

    /// The V2 Free shape, from AA's docs: speed and latency nested under
    /// `performance` (TTFA gained `_seconds`), no creator slug, no blended
    /// price, and no per-benchmark scores.
    @Test func decodesTheV2FreeShape() throws {
        let json = """
        {"tier": "free", "intelligence_index_version": 5,
         "pagination": {"page": 1, "page_size": 200, "total_pages": 1, "has_more": false},
         "data": [{"id": "cd68", "name": "GLM-5.3 (Max)", "slug": "glm-5-3", "release_date": "2026-08-18",
           "model_creator": {"id": "6743", "name": "Z AI"},
           "evaluations": {"artificial_analysis_intelligence_index": 31.7,
                           "artificial_analysis_coding_index": null},
           "artificial_analysis_intelligence_index_cost": {"total_cost": 3495.62},
           "pricing": {"price_1m_input_tokens": 1.4, "price_1m_output_tokens": 4.4,
                       "price_1m_cache_hit_tokens": 0.26, "price_1m_cache_write_tokens": null},
           "performance": {"median_output_tokens_per_second": 80.07,
                           "median_time_to_first_token_seconds": 2.22,
                           "median_time_to_first_answer_token_seconds": 27.19,
                           "median_end_to_end_response_time_seconds": 33.44}},
          {"id": "p2", "name": "Kimi K3", "slug": "kimi-k3",
           "model_creator": {"id": "m", "name": "Moonshot AI", "country": null},
           "evaluations": {}, "pricing": {"price_1m_input_tokens": 0, "price_1m_output_tokens": 2.0},
           "performance": {"median_output_tokens_per_second": null}}]}
        """
        let models = try AAModelsResponse.decode(Data(json.utf8))
        let glm = try #require(models.first)
        #expect(glm.creatorName == "Z AI" && glm.creatorSlug == "zai")
        #expect(ModelVendor.of(glm).id == "zai")
        #expect(glm.intelligence == 31.7)
        #expect(glm.evaluations["artificial_analysis_coding_index"] == nil)
        #expect(glm.coding == nil && glm.reasoning == nil)
        #expect(glm.priceInput == 1.4 && glm.priceOutput == 4.4)
        // 3:1 blend computed locally: (3 × 1.4 + 4.4) / 4.
        #expect(abs((glm.priceBlended ?? 0) - 2.15) < 1e-9)
        #expect(glm.speed == 80.07 && glm.timeToFirstToken == 2.22 && glm.timeToFirstAnswer == 27.19)
        let kimi = models[1]
        #expect(kimi.creatorSlug == "moonshotai" && ModelVendor.of(kimi).id == "moonshot")
        // An unmeasured input price leaves the blend unknown, not output / 4.
        #expect(kimi.priceBlended == nil && kimi.speed == nil)
    }

    /// After 2026-12-10 AA returns `performance: null` and puts the speed and
    /// latency means under `performance_v3`.
    @Test func decodesPerformanceV3WhenPerformanceIsNull() throws {
        let json = """
        {"data": [{"id": "v3", "name": "V3 Only", "slug": "v3-only",
           "model_creator": {"id": "c", "name": "Anthropic"},
           "evaluations": {}, "pricing": {},
           "performance": null,
           "performance_v3": {"mean_output_tokens_per_second": 91.5,
                              "mean_time_to_first_token_seconds": 1.25,
                              "mean_time_to_first_answer_token_seconds": 14.5}}]}
        """
        let model = try #require(try AAModelsResponse.decode(Data(json.utf8)).first)
        #expect(model.speed == 91.5)
        #expect(model.timeToFirstToken == 1.25)
        #expect(model.timeToFirstAnswer == 14.5)
    }

    /// While both objects are live, `performance_v3` wins; a null v3
    /// field falls back to `performance`, then to the legacy keys.
    @Test func performanceV3WinsOverPerformance() throws {
        let json = """
        {"data": [{"id": "both", "name": "Both", "slug": "both",
           "model_creator": {"id": "c", "name": "Anthropic"},
           "evaluations": {}, "pricing": {},
           "performance": {"median_output_tokens_per_second": 80.0,
                           "median_time_to_first_token_seconds": 2.0,
                           "median_time_to_first_answer_token_seconds": 20.0},
           "performance_v3": {"mean_output_tokens_per_second": 90.0,
                              "mean_time_to_first_token_seconds": null,
                              "mean_time_to_first_answer_token_seconds": 18.0}}]}
        """
        let model = try #require(try AAModelsResponse.decode(Data(json.utf8)).first)
        #expect(model.speed == 90.0)
        #expect(model.timeToFirstToken == 2.0)
        #expect(model.timeToFirstAnswer == 18.0)
    }

    /// A payload with only the old `performance` object still decodes.
    @Test func decodesPerformanceWithoutV3() throws {
        let json = """
        {"data": [{"id": "old", "name": "Old", "slug": "old",
           "model_creator": {"id": "c", "name": "Anthropic"},
           "evaluations": {}, "pricing": {},
           "performance": {"median_output_tokens_per_second": 70.0,
                           "median_time_to_first_token_seconds": 1.5,
                           "median_time_to_first_answer_token_seconds": 9.0}}]}
        """
        let model = try #require(try AAModelsResponse.decode(Data(json.utf8)).first)
        #expect(model.speed == 70.0)
        #expect(model.timeToFirstToken == 1.5)
        #expect(model.timeToFirstAnswer == 9.0)
    }

    /// With `performance_v3` and `performance` both null, or missing a
    /// field, the legacy top-level key supplies the value.
    @Test func legacyTopLevelKeysFillWhatBothPerformanceObjectsLack() throws {
        let json = """
        {"data": [{"id": "n", "name": "Both Null", "slug": "n",
           "model_creator": {"id": "c", "name": "Anthropic"},
           "evaluations": {}, "pricing": {},
           "performance": null, "performance_v3": null,
           "median_output_tokens_per_second": 55.0,
           "median_time_to_first_token_seconds": 0.9,
           "median_time_to_first_answer_token": 3.5},
          {"id": "m", "name": "Missing Fields", "slug": "m",
           "model_creator": {"id": "c", "name": "Anthropic"},
           "evaluations": {}, "pricing": {},
           "performance": {"median_output_tokens_per_second": 60.0},
           "performance_v3": {"mean_time_to_first_token_seconds": 1.1},
           "median_output_tokens_per_second": 1.0,
           "median_time_to_first_token_seconds": 2.0,
           "median_time_to_first_answer_token": 4.5}]}
        """
        let models = try AAModelsResponse.decode(Data(json.utf8))
        #expect(models[0].speed == 55.0)
        #expect(models[0].timeToFirstToken == 0.9)
        #expect(models[0].timeToFirstAnswer == 3.5)
        // Present v3 and `performance` values still win over the legacy keys.
        #expect(models[1].speed == 60.0)
        #expect(models[1].timeToFirstToken == 1.1)
        #expect(models[1].timeToFirstAnswer == 4.5)
    }

    @Test func rejectsAResponseWithNoDataArray() {
        #expect(throws: AAError.badResponse) {
            _ = try AAModelsResponse.decode(#"{"status": 200}"#.data(using: .utf8)!)
        }
    }
}

@Suite struct ArtificialAnalysisStoreTests {
    private func makeStore(_ dir: TempDir, now: Date = Date(timeIntervalSince1970: 1_800_000_000)) -> ArtificialAnalysisStore {
        ArtificialAnalysisStore(directory: dir.url, now: { now })
    }

    private let llmsBody = """
    {"pagination": {"page": 1, "page_size": 200, "total_pages": 1, "has_more": false},
     "data": [{"id": "m1", "name": "Claude Opus 5.5", "slug": "opus",
               "model_creator": {"id": "c", "name": "Anthropic", "slug": "anthropic"},
               "evaluations": {"artificial_analysis_intelligence_index": 58.0},
               "pricing": {"price_1m_blended_3_to_1": 15.0}}]}
    """.data(using: .utf8)!

    @Test func refreshWritesAllSixEndpointsAndMeta() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { _ in (self.llmsBody, 200, ["X-RateLimit-Remaining": "999"]) }
        let store = makeStore(dir)
        let errors = await store.refresh(key: "test-key", transport: transport)
        #expect(errors.isEmpty)
        #expect(transport.requests.count == 6)
        // Every request carried the key.
        #expect(transport.requests.allSatisfy { $0.value(forHTTPHeaderField: "x-api-key") == "test-key" })
        #expect(store.languageModels()?.first?.name == "Claude Opus 5.5")
        let meta = store.meta()[AAEndpoint.llms.rawValue]
        #expect(meta?.status == 200)
        #expect(meta?.rateLimitHeaders?["X-RateLimit-Remaining"] == "999")
        #expect(meta?.fetchedAt == 1_800_000_000)
        for endpoint in AAEndpoint.allCases {
            #expect(store.raw(endpoint) == self.llmsBody)
        }
        #expect(store.database.fetches().count == 6)
    }

    @Test func aFailedRequestKeepsTheStoredDataAndRecordsTheError() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { _ in (self.llmsBody, 200, [:]) }
        let store = makeStore(dir)
        await store.refresh(key: "key", transport: transport)
        // A 429 on the next refresh must not clobber the good bytes.
        transport.handler = { _ in ("rate limited".data(using: .utf8)!, 429, [:]) }
        let errors = await store.refresh(key: "key", transport: transport)
        #expect(errors[.llms] == "HTTP 429")
        #expect(store.languageModels()?.first?.name == "Claude Opus 5.5")
        #expect(store.meta()[AAEndpoint.llms.rawValue]?.error == "HTTP 429")
    }

    @Test func badLLMJSONIsAnErrorNotAWrite() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { request in
            if request.url?.path.contains("/language/models") == true {
                return ("not json".data(using: .utf8)!, 200, [:])
            }
            return ("{}".data(using: .utf8)!, 200, [:])
        }
        let store = makeStore(dir)
        let errors = await store.refresh(key: "key", transport: transport)
        #expect(errors[.llms] == "Unusable response")
        #expect(store.languageModels() == nil)
        #expect(store.meta()[AAEndpoint.llms.rawValue]?.error == "Unusable response")
        // The other endpoints still saved.
        #expect(store.raw(.textToImage) != nil)
        #expect(store.raw(.llms) == nil)
    }

    private func page(_ ids: [String], page: Int, hasMore: Bool) -> Data {
        let rows = ids.map { #"{"id": "\#($0)", "name": "\#($0)", "slug": "\#($0)", "model_creator": {"id": "c", "name": "Anthropic"}, "evaluations": {}, "pricing": {}, "performance": {}}"# }
        return Data("""
        {"tier": "free", "intelligence_index_version": 5,
         "pagination": {"page": \(page), "page_size": 2, "total_pages": 2, "has_more": \(hasMore)},
         "data": [\(rows.joined(separator: ","))]}
        """.utf8)
    }

    /// A Free key gets 403 on the Pro route and drops to /free, then every
    /// page lands: the models table holds the whole feed, not page 1.
    @Test func aFreeKeyFallsBackToFreeAndReadsEveryPage() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { request in
            let url = try #require(request.url)
            if url.path == "/api/v2/language/models" {
                #expect(url.query == "prompt_type=medium")
                return (Data(#"{"error": "Pro required"}"#.utf8), 403, ["X-AA-Tier": "free"])
            }
            if url.path == "/api/v2/language/models/free" {
                return url.query == "page=2"
                    ? (self.page(["m3"], page: 2, hasMore: false), 200,
                       ["X-RateLimit-Remaining": "97", "X-AA-Tier": "free"])
                    : (self.page(["m1", "m2"], page: 1, hasMore: true), 200, ["X-RateLimit-Remaining": "98"])
            }
            return (Data(#"{"tier": "free", "data": []}"#.utf8), 200, [:])
        }
        let store = makeStore(dir)
        // A model from an earlier refresh that is still on page 2 stays.
        try store.database.recordAAPayload(.llms, body: page(["m3"], page: 1, hasMore: false), at: nil,
                                           status: 200, rateLimitHeaders: nil, url: nil)
        let errors = await store.refresh(key: "key", transport: transport)
        #expect(errors.isEmpty)
        let llmsPaths = transport.requests.compactMap(\.url).filter { $0.path.contains("/language/") }
        #expect(llmsPaths.map(\.absoluteString) == [
            "https://artificialanalysis.ai/api/v2/language/models?prompt_type=medium",
            "https://artificialanalysis.ai/api/v2/language/models/free",
            "https://artificialanalysis.ai/api/v2/language/models/free?page=2",
        ])
        #expect(store.languageModels()?.map(\.id) == ["m1", "m2", "m3"])
        let meta = store.meta()[AAEndpoint.llms.rawValue]
        #expect(meta?.rateLimitHeaders == ["X-RateLimit-Remaining": "97", "X-AA-Tier": "free"])
        #expect(store.database.lastSuccess(source: "aa:llms")?.url == AAEndpoint.llms.url.absoluteString)
        #expect(transport.requests.allSatisfy { $0.value(forHTTPHeaderField: "x-api-key") == "key" })
    }

    /// A failed page fails the feed: page 1 alone must not replace the table.
    @Test func aFailedPageKeepsTheStoredModels() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { request in
            let url = try #require(request.url)
            if url.query?.contains("page=2") == true { return (Data(), 429, ["Retry-After": "3600"]) }
            if url.path.contains("/language/") { return (self.page(["m1"], page: 1, hasMore: true), 200, [:]) }
            return (Data("{}".utf8), 200, [:])
        }
        let store = makeStore(dir)
        try store.database.recordAAPayload(.llms, body: page(["old1", "old2"], page: 1, hasMore: false),
                                           at: nil, status: 200, rateLimitHeaders: nil, url: nil)
        let errors = await store.refresh(key: "key", transport: transport)
        #expect(errors[.llms] == "HTTP 429")
        #expect(store.languageModels()?.map(\.id) == ["old1", "old2"])
        #expect(store.meta()[AAEndpoint.llms.rawValue]?.rateLimitHeaders?["Retry-After"] == "3600")
    }

    /// An empty page that claims more, or a page with no `has_more`, is
    /// not a clear end: the feed fails and the stored models stay.
    @Test func anUnclearEndOfPagesKeepsTheStoredModels() async throws {
        for second in [page([], page: 2, hasMore: true),
                       Data(#"{"data": [{"id": "m2", "name": "m2", "slug": "m2"}]}"#.utf8)] {
            let dir = try TempDir()
            let transport = StubHTTP()
            transport.handler = { request in
                let url = try #require(request.url)
                if url.query?.contains("page=2") == true { return (second, 200, [:]) }
                if url.path.contains("/language/") { return (self.page(["m1"], page: 1, hasMore: true), 200, [:]) }
                return (Data("{}".utf8), 200, [:])
            }
            let store = makeStore(dir)
            try store.database.recordAAPayload(.llms, body: page(["old1", "old2"], page: 1, hasMore: false),
                                               at: nil, status: 200, rateLimitHeaders: nil, url: nil)
            let errors = await store.refresh(key: "key", transport: transport)
            #expect(errors[.llms] == "Unusable response")
            #expect(store.languageModels()?.map(\.id) == ["old1", "old2"])
        }
    }

    /// After the legacy routes retire, a 410 says what to do.
    @Test func goneSaysToUpdate() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { _ in (Data(), 410, [:]) }
        let errors = await makeStore(dir).refresh(key: "key", transport: transport)
        #expect(errors[.textToImage] == "HTTP 410: Artificial Analysis retired this API. Update SWE.")
    }

    @Test func everyEndpointIsAV2Route() {
        for endpoint in AAEndpoint.allCases {
            #expect(!endpoint.path.contains("/data/"), "\(endpoint)")
            #expect(endpoint.path.hasSuffix("/models/free"), "\(endpoint)")
        }
    }

    @Test func stalenessTurnsOnMissingAndOldDataOnly() throws {
        let dir = try TempDir()
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        var store = makeStore(dir, now: now)
        #expect(store.llmsIsStale())
        // A good fetch an hour ago is fresh.
        try store.database.recordAAPayload(.llms, body: llmsBody, at: now.addingTimeInterval(-3_600),
                                           status: 200, rateLimitHeaders: nil, url: nil)
        #expect(!store.llmsIsStale())
        // A failure since does not make it stale or fresh.
        store.database.recordFailure(source: "aa:llms", url: nil, at: now, status: 429, error: "HTTP 429")
        #expect(!store.llmsIsStale())
        // Eight days later it is.
        store = ArtificialAnalysisStore(directory: dir.url,
                                        now: { now.addingTimeInterval(8 * 86_400) })
        #expect(store.llmsIsStale())
    }

    @Test func anUndatedImportIsStale() throws {
        let dir = try TempDir()
        // Files from an older build with no meta.json: imported, but stale.
        try llmsBody.write(to: dir.path(AAEndpoint.llms.fileName))
        let store = makeStore(dir)
        #expect(store.languageModels()?.first?.name == "Claude Opus 5.5")
        #expect(store.llmsIsStale())
    }
}

@Suite struct ModelMatcherTests {
    private func aa(_ name: String, slug: String, creator: String = "anthropic",
                    intelligence: Double? = 50) -> AALanguageModel {
        AALanguageModel(id: slug, name: name, slug: slug, creatorSlug: creator,
                        evaluations: ["artificial_analysis_intelligence_index": intelligence].compactMapValues { $0 })
    }

    @Test func parsesProviderPrefixSnapshotDateFastAndEffort() {
        let parsed = ModelMatcher.parse("openai/gpt-6.1-sol-max@20261003-fast")
        #expect(parsed.base == "gpt-6-1-sol")
        #expect(parsed.effort == "max")
        #expect(parsed.fast)
        #expect(ModelMatcher.parse("Claude Opus 5.5 (max)").effort == "max")
        #expect(ModelMatcher.parse("claude-opus-5-5").effort == nil)
        #expect(!ModelMatcher.parse("gpt-6-astra").fast)
    }

    @Test func parsesAAEffortTailsWithANoteAfterTheEffort() {
        let parsed = ModelMatcher.parse("Claude Opus 5.5 (Max, Default Fallback)")
        #expect(parsed.base == "claude-opus-5-5")
        #expect(parsed.effort == "max")
        #expect(ModelMatcher.parse("Claude Fable 5 (Max, Opus 4.8 Fallback)").base == "claude-fable-5")
        // A parenthesis that leads with something else is its own model.
        #expect(ModelMatcher.parse("Claude Sonnet 5 (Non-reasoning, High)").effort == nil)
    }

    /// AA's real naming: the effort-free slug is the headline run.
    private var opusFamily: [AALanguageModel] {
        [aa("Claude Opus 5.5 (Max, Default Fallback)", slug: "claude-opus-5-5", intelligence: 57.6),
         aa("Claude Opus 5.5 (Xhigh, Default Fallback)", slug: "claude-opus-5-5-xhigh", intelligence: 56),
         aa("Claude Opus 5.5 (High, Default Fallback)", slug: "claude-opus-5-5-high", intelligence: 53.6)]
    }

    @Test func harnessVariantsResolveToTheFoundationModel() {
        // Devin and Cursor log the effort and mode they ran; the card is
        // the foundation model's, as AA headlines it.
        for logged in ["claude-opus-5-5-high-fast", "claude-opus-5-5", "anthropic/claude-opus-5-5-xhigh",
                       "claude-opus-5.5-max@20260922"] {
            #expect(ModelMatcher.match(logged, in: opusFamily)?.slug == "claude-opus-5-5")
        }
    }

    @Test func aLinkToAnEffortEntryLandsOnItsFamilysHeadline() {
        // The link sheet lists every effort; the leaderboard only headlines.
        #expect(ModelMatcher.match("my-proxy-model", in: opusFamily,
                                   links: ["my-proxy-model": "claude-opus-5-5-high"])?.slug
                == "claude-opus-5-5")
    }

    @Test func aFamilyWithNoEffortFreeSlugTakesItsHighestEffort() {
        let models = [aa("Grok 9 (High)", slug: "grok-9-high", creator: "x-ai", intelligence: 40),
                      aa("Grok 9 (Max)", slug: "grok-9-max", creator: "x-ai", intelligence: 39),
                      aa("Grok 9 (Low)", slug: "grok-9-low", creator: "x-ai", intelligence: 30)]
        #expect(ModelMatcher.match("grok-9-low", in: models)?.slug == "grok-9-max")
    }

    @Test func aScoredEffortBeatsAnUnscoredEffortFreeSlug() {
        // AA's v5 index: only the Xhigh run of Grok 4.6 was scored.
        let models = [aa("Grok 4.6 (High)", slug: "grok-4-6", creator: "x-ai", intelligence: nil),
                      aa("Grok 4.6 (Xhigh)", slug: "grok-4-6-xhigh", creator: "x-ai", intelligence: 31),
                      aa("Grok 4.6 (Low)", slug: "grok-4-6-low", creator: "x-ai", intelligence: nil)]
        #expect(ModelMatcher.match("grok-4.6-high-fast", in: models)?.slug == "grok-4-6-xhigh")
        #expect(ModelMatcher.headlines(in: models).map(\.slug) == ["grok-4-6-xhigh"])
        // With no score anywhere the effort-free slug still leads.
        let unscored = [models[0], models[2]]
        #expect(ModelMatcher.headlines(in: unscored).map(\.slug) == ["grok-4-6"])
    }

    @Test func amongUnscoredEntriesTheBestOlderScoreLeads() {
        var reasoning = aa("Claude 4.5 Sonnet (Reasoning)", slug: "claude-4-5-sonnet-thinking", intelligence: nil)
        reasoning.pastIntelligence = AAPastScore(value: 20.7)
        var plain = aa("Claude 4.5 Sonnet (Non-reasoning)", slug: "claude-4-5-sonnet", intelligence: nil)
        plain.pastIntelligence = AAPastScore(value: 19.3)
        #expect(ModelMatcher.headlines(in: [plain, reasoning]).map(\.slug) == ["claude-4-5-sonnet-thinking"])
        #expect(ModelMatcher.headlines(in: [reasoning, plain]).map(\.slug) == ["claude-4-5-sonnet-thinking"])
    }

    @Test func aPastScoreTagsWithItsVersionElseItsMonth() {
        let october = utc("2026-10-17T12:00:00Z")
        #expect(AAPastScore(value: 1, indexVersion: "5", scoredAt: october).tag == "v5")
        #expect(AAPastScore(value: 1, scoredAt: october).tag == "Oct ’26")
        #expect(AAPastScore(value: 1).tag == "older")
        #expect(AAPastScore(value: 1, scoredAt: october).note
                == "Scored on an older Intelligence Index version, last published October 2026. Not on the same scale as current scores.")
        #expect(AAPastScore(value: 1, indexVersion: "5").note
                == "Scored on Intelligence Index v5. Not on the same scale as current scores.")
    }

    @MainActor @Test func rowsWithOnlyAnOlderScoreSortAfterScoredOnesBestFirst() {
        func row(_ name: String, _ score: Double?, past: Double? = nil, tokens: Int = 0) -> ModelsModel.Row {
            var model = aa(name, slug: name, intelligence: score)
            model.pastIntelligence = past.map { AAPastScore(value: $0) }
            return ModelsModel.Row(logged: name, aa: model, tokens: tokens, value: score)
        }
        let rows = [row("none", nil, tokens: 9), row("old-low", nil, past: 20), row("new", 30),
                    row("old-high", nil, past: 45)]
        #expect(ModelsModel.sort(rows, by: .intelligence).map(\.logged) == ["new", "old-high", "old-low", "none"])
        // Only the intelligence index keeps an older score.
        #expect(ModelsModel.Metric.speed.pastScore(of: rows[1].aa) == nil)
        #expect(ModelsModel.Metric.intelligence.pastScore(of: rows[2].aa) == nil)
    }

    @Test func headlinesListOneEntryPerFoundationModel() {
        let models = opusFamily + [aa("GPT-6 Astra (Max)", slug: "gpt-6-astra", creator: "openai"),
                                   aa("GPT-6 Astra (High)", slug: "gpt-6-astra-high", creator: "openai")]
        #expect(ModelMatcher.headlines(in: models).map(\.slug) == ["claude-opus-5-5", "gpt-6-astra"])
    }

    @Test func displayNameDropsEffortAndModeTailsButKeepsNamingNotes() {
        #expect(ModelMatcher.displayName(opusFamily[0]) == "Claude Opus 5.5")
        // Reasoning and non-reasoning runs are one family now.
        #expect(ModelMatcher.displayName(aa("GPT-6 Sol (Non-reasoning)", slug: "x")) == "GPT-6 Sol")
        #expect(ModelMatcher.displayName(aa("Claude Opus 4.7 (Non-reasoning, High)", slug: "x")) == "Claude Opus 4.7")
        #expect(ModelMatcher.displayName(aa("Claude 4.5 Sonnet (Reasoning)", slug: "x")) == "Claude 4.5 Sonnet")
        // A release note names which model it is.
        #expect(ModelMatcher.displayName(aa("Grok 2 (Dec '24)", slug: "x")) == "Grok 2 (Dec '24)")
    }

    /// The shapes Devin, Cursor and Grok Bot log, against AA's names for
    /// the same models (from the real lists, 2026-10).
    private var harnessNamedModels: [AALanguageModel] {
        [aa("Claude Opus 5 (Max)", slug: "claude-opus-5", intelligence: 55),
         aa("Claude Opus 5 (High)", slug: "claude-opus-5-high", intelligence: 52),
         aa("Claude 4.5 Sonnet (Reasoning)", slug: "claude-4-5-sonnet-thinking", intelligence: 43),
         aa("Claude 4.5 Sonnet (Non-reasoning)", slug: "claude-4-5-sonnet", intelligence: 37),
         aa("Claude 4.5 Haiku (Reasoning)", slug: "claude-4-5-haiku-reasoning", intelligence: 35),
         aa("Grok 4.5 (High)", slug: "grok-4-5", creator: "x-ai"),
         aa("Grok 4.6 (High)", slug: "grok-4-6", creator: "x-ai"),
         aa("Grok 4.7 (Xhigh)", slug: "grok-4-7", creator: "x-ai"),
         aa("Grok 4.7 (High)", slug: "grok-4-7-high", creator: "x-ai"),
         aa("Gemini 2.5 Flash (Reasoning)", slug: "gemini-2-5-flash-reasoning", creator: "google", intelligence: 27),
         aa("Gemini 2.5 Flash (Non-reasoning)", slug: "gemini-2-5-flash", creator: "google", intelligence: 21),
         aa("Gemini 2.5 Flash Preview (Sep '25) (Reasoning)", slug: "gemini-2-5-flash-preview-09-2025-reasoning",
            creator: "google", intelligence: 30),
         aa("Gemini 3.1 Pro Preview", slug: "gemini-3-1-pro-preview", creator: "google")]
    }

    @Test func thinkingFastHarnessPrefixesAndNotesStillFindTheFamily() {
        let expected: [String: String] = [
            "claude-opus-5-thinking-high-fast": "claude-opus-5",
            "claude-opus-5-thinking-high": "claude-opus-5",
            "claude-4.5-sonnet": "claude-4-5-sonnet-thinking",
            "claude-haiku-4-5-20251001": "claude-4-5-haiku-reasoning",
            "cursor-grok-4.5-high": "grok-4-5",
            "cursor-grok-4.5-high-fast": "grok-4-5",
            "cursor-grok-4.6-high-fast": "grok-4-6",
            "grok-4.7-high (Auto)": "grok-4-7",
            "Grok 4.6 (Auto)": "grok-4-6",
            // The stable release, not the higher-scoring preview.
            "gemini-2.5-flash": "gemini-2-5-flash-reasoning",
            // Only a preview exists, so the preview it is.
            "gemini-3.1-pro": "gemini-3-1-pro-preview",
        ]
        for (logged, slug) in expected {
            #expect(ModelMatcher.match(logged, in: harnessNamedModels)?.slug == slug, "\(logged)")
        }
        #expect(ModelMatcher.headlines(in: harnessNamedModels).map(\.slug)
                == ["claude-opus-5", "claude-4-5-sonnet-thinking", "claude-4-5-haiku-reasoning",
                    "grok-4-5", "grok-4-6", "grok-4-7", "gemini-2-5-flash-reasoning", "gemini-3-1-pro-preview"])
    }

    @Test func aModeWordThatNamesAModelKeepsItApart() {
        let models = [aa("Kimi K2", slug: "kimi-k2", creator: "moonshot"),
                      aa("Kimi K2 Thinking", slug: "kimi-k2-thinking", creator: "moonshot"),
                      aa("Grok 4", slug: "grok-4", creator: "x-ai"),
                      aa("Grok 4 Fast (Reasoning)", slug: "grok-4-fast-reasoning", creator: "x-ai"),
                      aa("Sonar", slug: "sonar", creator: "perplexity"),
                      aa("Sonar Reasoning", slug: "sonar-reasoning", creator: "perplexity"),
                      aa("Command A", slug: "command-a", creator: "cohere"),
                      aa("Command A+", slug: "command-a-plus", creator: "cohere")]
        #expect(ModelMatcher.headlines(in: models).count == 8)
        #expect(ModelMatcher.match("kimi-k2-thinking", in: models)?.slug == "kimi-k2-thinking")
        #expect(ModelMatcher.match("kimi-k2", in: models)?.slug == "kimi-k2")
        #expect(ModelMatcher.match("grok-4-fast", in: models)?.slug == "grok-4-fast-reasoning")
        #expect(ModelMatcher.match("grok-4", in: models)?.slug == "grok-4")
        #expect(ModelMatcher.match("sonar-reasoning", in: models)?.slug == "sonar-reasoning")
        #expect(ModelMatcher.match("command-a+", in: models)?.slug == "command-a-plus")
        // Run fast, a model named for its mode is still that model.
        #expect(ModelMatcher.match("kimi-k2-thinking-fast", in: models)?.slug == "kimi-k2-thinking")
        #expect(ModelMatcher.match("sonar-reasoning-fast", in: models)?.slug == "sonar-reasoning")
    }

    @Test func linksSavedUnderTheOldKeyStillHold() {
        let models = [aa("Grok 4.6 (High)", slug: "grok-4-6", creator: "x-ai"),
                      aa("My Grok", slug: "my-grok", creator: "x-ai")]
        // Before families, `cursor-grok-4.6-high-fast` saved under
        // "cursor-grok-4-6" and `grok-4.7-high (Auto)` under its whole name.
        #expect(ModelMatcher.legacyKey("cursor-grok-4.6-high-fast") == "cursor-grok-4-6")
        #expect(ModelMatcher.legacyKey("grok-4.7-high (Auto)") == "grok-4-7-high-(auto)")
        #expect(ModelMatcher.legacyKey("openai/gpt-6.1-sol-max@20261003-fast") == "gpt-6-1-sol")
        #expect(ModelMatcher.match("cursor-grok-4.6-high-fast", in: models,
                                   links: ["cursor-grok-4-6": "my-grok"])?.slug == "my-grok")
        #expect(ModelMatcher.match("grok-4.7-high (Auto)", in: models,
                                   links: ["grok-4-7-high-(auto)": "my-grok"])?.slug == "my-grok")
        // A link under the new key wins over one under the old.
        #expect(ModelMatcher.match("cursor-grok-4.6-high-fast", in: models,
                                   links: ["grok-4-6": "grok-4-6", "cursor-grok-4-6": "my-grok"])?.slug
                == "grok-4-6")
    }

    @Test func parseSeparatesTheFamilyFromHowItRan() {
        let parsed = ModelMatcher.parse("claude-opus-5-thinking-high-fast")
        // `base` keeps the mode word, so links saved before still hit.
        #expect(parsed.base == "claude-opus-5-thinking")
        #expect(parsed.effort == "high")
        #expect(parsed.fast)
        #expect(parsed.thinking)
        #expect(parsed.looseFamily == ModelMatcher.parse("Claude Opus 5 (Max)").family)
        #expect(ModelMatcher.parse("claude-4.5-sonnet").family == ModelMatcher.parse("claude-sonnet-4-5").family)
        #expect(ModelMatcher.parse("claude-opus-5").family != ModelMatcher.parse("claude-opus-5-5").family)
        #expect(ModelMatcher.parse("cursor-grok-4.6-high-fast").base == "grok-4-6")
        #expect(ModelMatcher.parse("Gemini 3.1 Pro Preview").prerelease)
        #expect(ModelMatcher.parse("Gemini 2.5 Pro Preview (Mar' 25)").prerelease)
        #expect(!ModelMatcher.parse("Claude 4.5 Sonnet (Non-reasoning)").thinking)
        // A bare harness word is a name, not a prefix.
        #expect(ModelMatcher.parse("cursor").base == "cursor")
    }

    @Test func dotsAndDatesNormalizeTheLoggedName() {
        let models = [aa("GPT-6.1 Sol (max)", slug: "gpt-6-1-sol-max", creator: "openai")]
        #expect(ModelMatcher.match("gpt-6.1-sol@20261001", in: models)?.id == "gpt-6-1-sol-max")
    }

    @Test func unmatchedNamesStayUnmatched() {
        let models = [aa("Claude Opus 5.5", slug: "claude-opus-5-5")]
        #expect(ModelMatcher.match("swe-2-medium", in: models) == nil)
        #expect(ModelMatcher.match("grok-bot-abc123", in: models) == nil)
        #expect(ModelMatcher.match("compactor", in: models) == nil)
        #expect(ModelMatcher.match("grok-bot-default", in: models) == nil)
        #expect(ModelMatcher.match("openrouter/free", in: models) == nil)
        #expect(ModelMatcher.match("(auto)", in: models) == nil)
    }

    @Test func aManualLinkBeatsEveryRule() throws {
        let models = [aa("Claude Opus 5.5", slug: "claude-opus-5-5"),
                      aa("GPT-6 Astra", slug: "gpt-6-astra", creator: "openai")]
        // Without the link the name doesn't match; with it the link wins.
        #expect(ModelMatcher.match("my-proxy-model", in: models) == nil)
        #expect(ModelMatcher.match("my-proxy-model", in: models,
                                   links: ["my-proxy-model": "gpt-6-astra"])?.id == "gpt-6-astra")
        // Links survive a save/load round trip.
        let dir = try TempDir()
        let url = dir.path("config/model-links.json")
        try ModelMatcher.saveLinks(["my-proxy-model": "gpt-6-astra"], to: url)
        #expect(ModelMatcher.loadLinks(from: url) == ["my-proxy-model": "gpt-6-astra"])
    }

    /// One hand-edited value that isn't a string skips only itself and
    /// survives the next save; a file that isn't JSON is never replaced.
    @Test func aBadLinksFileIsNeverSavedAway() throws {
        let dir = try TempDir()
        let url = dir.path("config/model-links.json")
        try FileManager.default.createDirectory(at: dir.path("config"), withIntermediateDirectories: true)
        try Data(#"{"a": "aa-1", "note": 3}"#.utf8).write(to: url)
        #expect(ModelMatcher.loadLinks(from: url) == ["a": "aa-1"])
        try ModelMatcher.saveLinks(["a": "aa-1", "b": "aa-2"], to: url)
        let object = try #require(
            JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        #expect(Set(object.keys) == ["a", "b", "note"])
        #expect(object["note"] as? Int == 3)

        let cut = Data(#"{"a": "aa-1", "b": "aa"#.utf8)
        try cut.write(to: url)
        #expect(ModelMatcher.loadLinks(from: url).isEmpty)
        #expect(throws: (any Error).self) { try ModelMatcher.saveLinks(["c": "aa-3"], to: url) }
        #expect(try Data(contentsOf: url) == cut)
    }

    @Test func effortTiesTakeTheHigherIntelligence() {
        let models = [
            aa("Grok 4.7 (high)", slug: "grok-4-7-high", creator: "x-ai", intelligence: 40),
            aa("Grok 4.7 (high)", slug: "grok-4-7-high-2", creator: "x-ai", intelligence: 46),
        ]
        #expect(ModelMatcher.match("grok-4.7-high", in: models)?.id == "grok-4-7-high-2")
    }
}

@Suite struct CostPercentileTests {
    private func priced(_ id: String, _ price: Double?) -> AALanguageModel {
        AALanguageModel(id: id, name: id, slug: id,
                        priceBlended: price)
    }

    @Test func extremesAreZeroAndOneAndTiesShare() {
        let models = [priced("cheap", 1), priced("mid-a", 5), priced("mid-b", 5), priced("dear", 10)]
        let ranks = ModelsModel.costRanks(in: models)
        #expect(ranks["cheap"] == 0)
        // The two $5 models share a spot: (1 cheaper + 0.5 ties) / 3.
        #expect(ranks["mid-a"] == 0.5)
        #expect(ranks["mid-b"] == 0.5)
        #expect(ranks["dear"] == 1)
    }

    @Test func unpricedOrLoneModelsHaveNoRank() {
        let free = priced("free", nil)
        #expect(ModelsModel.costRanks(in: [free, priced("x", 5)])["free"] == nil)
        let one = priced("one", 5)
        #expect(ModelsModel.costRanks(in: [one, free])["one"] == nil)
    }
}

@Suite @MainActor struct ModelsModelTests {
    private func makeModel(dir: TempDir,
                           slices: [ModelUsage] = [],
                           key: String? = "key",
                           searchDelay: Duration = .zero) -> ModelsModel {
        ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                    keyReader: { key },
                    transport: StubHTTP(),
                    linksURL: dir.path("config/model-links.json"),
                    slices: { _ in slices },
                    now: { Date(timeIntervalSince1970: 1_800_000_000) },
                    calendar: Calendar(identifier: .gregorian),
                    searchDelay: searchDelay)
    }

    private func slice(_ model: String, _ harness: String, tokens: Int) -> ModelUsage {
        var totals = UsageTotals()
        totals.outputTokens = tokens
        return ModelUsage(model: model, harness: harness, tier: nil,
                          totals: totals, harnessBilled: false)
    }

    private func writeLLMs(_ dir: TempDir, _ models: [[String: Any]]) throws {
        let aa = dir.path("aa")
        try FileManager.default.createDirectory(at: aa, withIntermediateDirectories: true)
        let body = try JSONSerialization.data(withJSONObject: ["data": models])
        try body.write(to: aa.appending(path: AAEndpoint.llms.fileName))
        try #"{"llms": {"fetchedAt": 1800000000.0, "status": 200}}"#
            .data(using: .utf8)!.write(to: aa.appending(path: "meta.json"))
    }

    private func entry(_ id: String, _ name: String, _ slug: String,
                       creator: String, intelligence: Double? = nil,
                       coding: Double? = nil, price: Double? = nil) -> [String: Any] {
        var evals: [String: Any] = [:]
        if let intelligence { evals["artificial_analysis_intelligence_index"] = intelligence }
        if let coding { evals[AALanguageModel.codingEvaluation] = coding / 100 }
        return ["id": id, "name": name, "slug": slug,
                "model_creator": ["id": creator, "name": creator, "slug": creator],
                "evaluations": evals,
                "pricing": price.map { ["price_1m_blended_3_to_1": $0] } ?? [:]]
    }

    @Test func rowsShowTheLedgersPriceElseTheModelsMainKey() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5", creator: "anthropic",
                  intelligence: 70, price: 8),
            entry("sol", "GPT-6.1 Sol", "gpt-6-1-sol", creator: "openai", intelligence: 60, price: 4),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        func priced(_ input: Double) -> [String: Any] {
            ["input_cost_per_token": input, "output_cost_per_token": input * 5,
             "cache_read_input_token_cost": input / 10]
        }
        try store.database.recordPrices([
            "claude-opus-5-5": priced(4e-06),
            "gpt-6.1-sol": priced(2e-06),
            "openrouter/openai/gpt-6.1-sol": priced(2.5e-06),
            "gpt-6.1-sol-2026-07-09": priced(3e-06),
        ], at: Date(), url: nil, sha: nil)
        store.database.rebuildAliases()
        // The ledger charges a saved price of the user's for Opus.
        let ledgerCatalog = PricingCatalog(liteLLMEntries: ["claude-opus-5-5": priced(4e-06)],
                                           overrides: ["claude-opus-5-5": priced(9e-06)])
        let used = [slice("claude-opus-5-5-high", "claude", tokens: 100)]
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in used },
                                pricing: { ledgerCatalog },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian))
        await model.load(allowRefresh: false)

        let opus = try #require(model.rows.first { $0.aa?.id == "opus" }?.listPrice)
        #expect(opus.key == "claude-opus-5-5")
        #expect(opus.source == .yours)
        #expect(abs(opus.input - 9) < 1e-9)
        // A leaderboard row nobody ran: the bare key, not a reseller's or a dated one.
        let sol = try #require(model.rows.first { $0.aa?.id == "sol" }?.listPrice)
        #expect(sol.key == "gpt-6.1-sol")
        #expect(sol.source == .liteLLM)
        #expect(abs(sol.input - 2) < 1e-9 && abs(sol.output - 10) < 1e-9 && abs(sol.cacheRead - 0.2) < 1e-9)
        // The hover card for the logged name carries the same price.
        #expect(model.card(for: "claude-opus-5-5-high").listPrice == opus)
    }

    @Test func usedModelsMatchAndUnmatchedOnesCollect() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5 (max)", "claude-opus-5-5-max",
                  creator: "anthropic", intelligence: 58, price: 15),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("claude-opus-5-5-max", "claude", tokens: 1000),
            slice("swe-2-medium", "devin", tokens: 400),
        ])
        await model.load()
        #expect(model.hasKey)
        // Unmatched names never reach the chart or table.
        #expect(model.rows.count == 1)
        #expect(model.rows.allSatisfy { $0.aa != nil })
        let used = model.rows.first { $0.logged == "claude-opus-5-5-max" }
        #expect(used?.aa?.id == "opus")
        #expect(used?.tokens == 1000)
        #expect(used?.harnesses == ["claude"])
        #expect(model.unmatched == [ModelsModel.Unmatched(model: "swe-2-medium", tokens: 400)])
    }

    /// Devin's "MODEL_PRIVATE_11" matches by the real name the bundled
    /// price supplement aliases it to.
    @Test func anOpaqueIDMatchesThroughItsAlias() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("haiku", "Claude 4.5 Haiku", "claude-4-5-haiku", creator: "anthropic", intelligence: 40),
        ])
        let model = makeModel(dir: dir, slices: [slice("MODEL_PRIVATE_11", "devin", tokens: 100)])
        await model.load()
        let row = try #require(model.rows.first { $0.used })
        #expect(row.aa?.id == "haiku")
        #expect(row.logged == "MODEL_PRIVATE_11")
        #expect(row.alias == "claude-haiku-4-5")
        #expect(model.unmatched.isEmpty)
    }

    @Test func loggedAliasesOfOneAAModelMergeIntoOneRow() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("grok", "Grok 4.7 (xhigh)", "grok-4-7-xhigh",
                  creator: "xai", intelligence: 46),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("grok-4.7", "grok", tokens: 100),
            slice("grok-4.7-fast", "cursor", tokens: 50),
        ])
        await model.load()
        let used = model.rows.filter(\.used)
        #expect(used.count == 1)
        #expect(used[0].aa?.id == "grok")
        #expect(used[0].tokens == 150)
        #expect(used[0].harnesses == ["grok", "cursor"])
        #expect(model.unmatched.isEmpty)
    }

    @Test func scopeSwitchesBetweenYoursAndTheLeaderboard() async throws {
        let dir = try TempDir()
        var entries = (0..<12).map { i in
            entry("top-\(i)", "Top \(i)", "top-\(i)", creator: "openai",
                  intelligence: Double(90 - i))
        }
        entries.append(entry("mine", "My Model", "mine", creator: "anthropic", intelligence: 10))
        try writeLLMs(dir, entries)
        let model = makeModel(dir: dir, slices: [slice("my-model", "claude", tokens: 10)])
        await model.load()
        // "Your models + top 10": the used row plus ten leaders.
        #expect(model.scope == .yoursPlusTop)
        #expect(model.rows.count == 11)
        #expect(model.rows.contains { $0.logged == "my-model" })
        model.set(scope: .yours)
        #expect(model.rows.count == 1)
        model.set(scope: .top25)
        #expect(model.rows.count == 13)
    }

    @Test func theLeaderboardShowsWhileTheUsageReadIsOut() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
            entry("b", "B", "b", creator: "anthropic", intelligence: 70),
        ])
        // Holds the usage read until the test has looked at the rows.
        let reading = HoldGate()
        let used = [slice("b", "claude", tokens: 50)]
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in
                                    try? await reading.pass()
                                    return used
                                },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian))
        let load = Task { await model.load() }
        #expect(await reading.arrived())
        // Benchmarks are drawn before the ledger answers, tokens pending.
        #expect(model.usagePending)
        #expect(model.rows.map(\.logged) == ["A", "B"])
        #expect(model.rows.allSatisfy { $0.tokens == 0 })
        reading.open()
        await load.value
        #expect(!model.usagePending)
        #expect(model.rows.first { $0.aa?.id == "b" }?.tokens == 50)
    }

    @Test func harnessFilterNarrowsRowsAndDropsDeadFilters() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
            entry("b", "B", "b", creator: "anthropic", intelligence: 70),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("a", "codex", tokens: 100),
            slice("b", "claude", tokens: 50),
        ])
        await model.load()
        #expect(model.harnessOptions == ["codex", "claude"])
        model.set(harnessFilter: ["codex"])
        // Only codex's model: claude's "b" is a top-10 leader, but the
        // filter asks what codex ran, so it no longer pads the chart.
        #expect(model.rows.map(\.logged) == ["a"])
        // A filter whose harness vanished from the period resets itself.
        model.set(harnessFilter: ["pi"])
        #expect(model.harnessFilter.isEmpty)
        #expect(model.rows.map(\.logged) == ["a", "b"])
    }

    @Test func harnessFilterTakesSeveralHarnessesAndHoldsUnderEveryScope() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
            entry("b", "B", "b", creator: "anthropic", intelligence: 70),
            entry("c", "C", "c", creator: "google", intelligence: 60),
            entry("lead", "Lead", "lead", creator: "xai", intelligence: 95),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("a", "codex", tokens: 100),
            slice("b", "claude", tokens: 50),
            slice("c", "gemini", tokens: 20),
        ])
        await model.load()
        model.toggle(harness: "codex")
        model.toggle(harness: "gemini")
        #expect(model.harnessFilter == ["codex", "gemini"])
        #expect(model.rows.map(\.logged) == ["a", "c"])
        model.set(scope: .yours)
        #expect(model.rows.map(\.logged) == ["a", "c"])
        // Top 25 under a filter: the leaders those harnesses ran.
        model.set(scope: .top25)
        #expect(model.rows.map(\.logged) == ["a", "c"])
        // Unticking the last harness is All again: the leader is back.
        model.toggle(harness: "codex")
        model.toggle(harness: "gemini")
        #expect(model.harnessFilter.isEmpty)
        #expect(model.rows.first?.aa?.id == "lead")
        // Of two picks, only the one that left the period drops.
        model.set(scope: .yoursPlusTop)
        model.set(harnessFilter: ["codex", "pi"])
        #expect(model.harnessFilter == ["codex"])
    }

    @Test func aFilteredTop25KeepsARunThatLoggedNoTokens() async throws {
        // Pi and Cline can record a run's cost with zero tokens; the
        // harness still ran that model, so the filtered board keeps it.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
            entry("b", "B", "b", creator: "anthropic", intelligence: 70),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("a", "pi", tokens: 0),
            slice("b", "claude", tokens: 50),
        ])
        await model.load()
        model.set(scope: .top25)
        model.set(harnessFilter: ["pi"])
        #expect(model.rows.map(\.logged) == ["a"])
    }

    @Test func tiedValuesSortTheSameWhateverTheInputOrder() {
        func row(_ name: String, tokens: Int) -> ModelsModel.Row {
            ModelsModel.Row(logged: name, tokens: tokens, value: 50)
        }
        let rows = [row("b", tokens: 10), row("a", tokens: 10), row("big", tokens: 99), row("lead", tokens: 0)]
        let expected = ["big", "b", "a", "lead"]
        #expect(ModelsModel.sort(rows, by: .intelligence).map(\.logged) == expected)
        #expect(ModelsModel.sort(rows.reversed(), by: .intelligence).map(\.logged) == expected)
        #expect(ModelsModel.sort(rows.shuffled(), by: .ttft).map(\.logged) == expected)
    }

    @Test func hasLoadedFlipsOnlyOnceTheFirstLoadLands() async throws {
        // The page shows a loading card until then, so a missing key
        // isn't announced before the key was even read.
        let dir = try TempDir()
        let model = makeModel(dir: dir, key: nil)
        #expect(!model.hasLoaded)
        #expect(!model.hasKey)
        await model.load()
        #expect(model.hasLoaded)
        #expect(!model.hasKey)
    }

    @Test func metricSortRunsBothDirectionsAndNilsSitLast() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("hi", "Hi", "hi", creator: "openai", intelligence: 90, price: 20),
            entry("lo", "Lo", "lo", creator: "openai", intelligence: 10, price: 1),
            // A used model with no benchmark for the metric sorts last.
            entry("noval", "NoVal", "noval", creator: "mistral"),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("noval", "pi", tokens: 5),
            slice("unmatched-x", "pi", tokens: 3),
        ])
        await model.load()
        model.set(scope: .top25)
        // Top 25 is the leaderboard only: "noval" has no intelligence,
        // so it never makes the board even though it ran.
        #expect(model.rows.map(\.logged) == ["Hi", "Lo"])
        // Cost sorts cheapest-first; "noval" is still off the board.
        model.set(metric: .cost)
        #expect(model.rows.map(\.logged) == ["Lo", "Hi"])
        #expect(model.unmatched == [ModelsModel.Unmatched(model: "unmatched-x", tokens: 3)])
    }

    @Test func linkingRematchesTheLoggedName() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [entry("opus", "Claude Opus 5.5", "opus",
                                 creator: "anthropic", intelligence: 58)])
        let model = makeModel(dir: dir, slices: [slice("my-alias", "claude", tokens: 10)])
        await model.load()
        #expect(model.unmatched.count == 1)
        try model.link(logged: "my-alias", to: "opus")
        #expect(model.unmatched.isEmpty)
        #expect(model.rows.first?.aa?.id == "opus")
        #expect(ModelMatcher.loadLinks(from: dir.path("config/model-links.json"))["my-alias"] == "opus")
    }

    /// A link added to the file by hand after the page loaded stays when
    /// the page saves one of its own.
    @Test func linkingKeepsALinkAddedToTheFileSinceLoad() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [entry("opus", "Claude Opus 5.5", "opus",
                                 creator: "anthropic", intelligence: 58)])
        let model = makeModel(dir: dir, slices: [slice("my-alias", "claude", tokens: 10)])
        await model.load()
        let url = dir.path("config/model-links.json")
        try ModelMatcher.saveLinks(["by-hand": "opus"], to: url)
        try model.link(logged: "my-alias", to: "opus")
        #expect(ModelMatcher.loadLinks(from: url) == ["by-hand": "opus", "my-alias": "opus"])
    }

    @Test func noKeyKeepsTheEmptyStateAndMakesNoRequests() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: { nil }, transport: transport,
                                linksURL: dir.path("links.json"),
                                slices: { _ in [] })
        await model.load()
        #expect(!model.hasKey)
        #expect(model.allModels.isEmpty)
        #expect(transport.requests.isEmpty)
    }

    /// A slices closure the test pauses and releases by hand; each entry
    /// is one in-flight call.
    private final class SlicesGate: @unchecked Sendable {
        private let state = Mutex<[CheckedContinuation<[ModelUsage], Never>]>([])
        /// Bumped once a call is parked, for the test to wait on.
        let parked = CountGate()
        var pendingCount: Int { state.withLock { $0.count } }
        func call(_ range: DateInterval) async -> [ModelUsage] {
            _ = range
            let parked = self.parked
            return await withCheckedContinuation { continuation in
                state.withLock { $0.append(continuation) }
                Task { await parked.bump() }
            }
        }
        func resume(_ index: Int, with value: [ModelUsage]) {
            state.withLock { $0[index] }.resume(returning: value)
        }
    }

    private func gatedModel(dir: TempDir, gate: SlicesGate) -> ModelsModel {
        ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                    keyReader: { "key" },
                    transport: StubHTTP(),
                    linksURL: dir.path("config/model-links.json"),
                    slices: { range in await gate.call(range) },
                    now: { Date(timeIntervalSince1970: 1_800_000_000) },
                    calendar: Calendar(identifier: .gregorian))
    }

    private func waitForCalls(_ gate: SlicesGate, _ count: Int) async throws {
        try #require(await gate.parked.reach(count))
    }

    @Test func anOlderPeriodAnswerCannotOverwriteANewerOne() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
            entry("b", "B", "b", creator: "anthropic", intelligence: 70),
        ])
        let gate = SlicesGate()
        let model = gatedModel(dir: dir, gate: gate)
        let loading = Task { await model.load() }
        try await waitForCalls(gate, 1)
        gate.resume(0, with: [slice("b", "claude", tokens: 50)])
        await loading.value
        #expect(model.rows.filter(\.used).map(\.logged) == ["b"])

        // Two period switches: the week's answer lands after today's and
        // must not write its older data over it.
        let older = Task { await model.set(period: .week) }
        try await waitForCalls(gate, 2)
        let newer = Task { await model.set(period: .today) }
        try await waitForCalls(gate, 3)
        gate.resume(2, with: [slice("a", "codex", tokens: 9)])
        await newer.value
        gate.resume(1, with: [slice("b", "claude", tokens: 5000)])
        await older.value
        #expect(model.period == .today)
        let used = model.rows.filter(\.used)
        #expect(used.map(\.logged) == ["a"])
        #expect(used.first?.tokens == 9)
    }

    @Test func aUsageReloadMidLoadDoesNotCancelTheLoad() async throws {
        // The page's two tasks: a revisit's load is waiting on the key
        // when a ledger re-scan reloads the usage. The load must still
        // pick up the key the user just saved.
        let dir = try TempDir()
        try writeLLMs(dir, [entry("a", "A", "a", creator: "openai", intelligence: 80)])
        // The key read answers at once until `held` names a gate; then it
        // waits there, so a usage reload can land in the middle of a load.
        let key = Box<String?>(nil)
        let held = Box<HoldGate?>(nil)
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: {
                                    if let gate = held.value { try? await gate.pass() }
                                    return key.value
                                },
                                transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in [] },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian))
        await model.load()
        #expect(!model.hasKey)

        let gate = HoldGate()
        held.value = gate
        let loading = Task { await model.load(allowRefresh: false) }
        #expect(await gate.arrived())
        await model.loadIfNeeded(usageStamp: 1)
        // A load the reload cancelled has already left the gate with no
        // key, so it can't pick this one up.
        key.value = "key"
        gate.open()
        await loading.value
        #expect(model.hasKey)
        #expect(!model.isLoading)
    }

    /// Answers every endpoint once `gate` opens; a request whose calling
    /// task is cancelled while it waits throws, like URLSession.
    private final class SlowHTTP: HTTPTransport, @unchecked Sendable {
        private let count = Mutex(0)
        let gate = HoldGate()
        var requests: Int { count.withLock { $0 } }
        func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
            count.withLock { $0 += 1 }
            try await gate.pass()
            let body = #"{"pagination":{"has_more":false},"data":[{"id":"m","name":"M","slug":"m","model_creator":{"id":"c","name":"C","slug":"c"},"evaluations":{},"pricing":{}}]}"#
            return (body.data(using: .utf8)!,
                    HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!)
        }
    }

    @Test func leavingThePageMidRefreshRecordsNoCancelledFailures() async throws {
        // No stored data, so the load's refresh runs; the page's task is
        // cancelled (the user switched pages) after the first request.
        let dir = try TempDir()
        let transport = SlowHTTP()
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: { "key" }, transport: transport,
                                linksURL: dir.path("links.json"),
                                slices: { _ in [] })
        let page = Task { await model.load() }
        #expect(await transport.gate.arrived())
        // A refresh tied to the page would throw from the parked request
        // right here; one that outlives the page goes on once opened.
        page.cancel()
        transport.gate.open()
        await page.value
        #expect(model.refreshError == nil)
        #expect(transport.requests == AAEndpoint.allCases.count)
        #expect(model.allModels.map(\.id) == ["m"])
    }

    @Test func aScanLandingAfterTheLoadsReadIsReadAgain() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [entry("a", "A", "a", creator: "openai", intelligence: 80)])
        let gate = SlicesGate()
        let model = gatedModel(dir: dir, gate: gate)
        let loading = Task { await model.loadIfNeeded(usageStamp: 1) }
        try await waitForCalls(gate, 1)
        // Scan 2 finishes while the load's read (from before it) is out.
        await model.loadIfNeeded(usageStamp: 2)
        gate.resume(0, with: [slice("a", "claude", tokens: 10)])
        try await waitForCalls(gate, 2)
        gate.resume(1, with: [slice("a", "claude", tokens: 20)])
        await loading.value
        #expect(model.rows.filter(\.used).first?.tokens == 20)
        // Scan 2 is now read: passing it again re-reads nothing.
        await model.loadIfNeeded(usageStamp: 2)
        #expect(gate.pendingCount == 2)
    }

    @Test func aSecondConcurrentRefreshDoesNotDoubleTheFetches() async throws {
        let dir = try TempDir()
        let transport = StubHTTP()
        transport.handler = { _ in
            (#"{"data":[{"id":"m","name":"M","slug":"m","model_creator":{"id":"c","name":"C","slug":"c"},"evaluations":{},"pricing":{}}]}"#
                .data(using: .utf8)!, 200, [:])
        }
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: { "key" }, transport: transport,
                                linksURL: dir.path("links.json"),
                                slices: { _ in [] })
        // The guard lands before the first await, so whichever call runs
        // second returns immediately instead of fetching six endpoints again.
        async let first: Void = model.refresh()
        async let second: Void = model.refresh()
        await first
        await second
        #expect(transport.requests.count == 6)
    }

    @Test func loadIfNeededReadsTheCacheWithoutAnyRequests() async throws {
        let dir = try TempDir()
        // Stale cache (no meta): a plain load() would refresh first.
        let aa = dir.path("aa")
        try FileManager.default.createDirectory(at: aa, withIntermediateDirectories: true)
        try #"{"data":[{"id":"a","name":"A","slug":"a","model_creator":{"id":"o","name":"O","slug":"o"},"evaluations":{"artificial_analysis_intelligence_index":80},"pricing":{}}]}"#
            .data(using: .utf8)!.write(to: aa.appending(path: AAEndpoint.llms.fileName))
        let transport = StubHTTP()
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: aa),
                                keyReader: { "key" }, transport: transport,
                                linksURL: dir.path("links.json"),
                                slices: { _ in [] })
        await model.loadIfNeeded()
        #expect(transport.requests.isEmpty)
        #expect(model.hasKey)
        #expect(!model.rows.isEmpty)
        // A second hover is a no-op, not another read.
        await model.loadIfNeeded()
        #expect(transport.requests.isEmpty)
    }

    /// A mutable slices answer, so a period switch can return a
    /// different set than the load did.
    private final class SlicesBox: @unchecked Sendable {
        private let state = Mutex<[ModelUsage]>([])
        var current: [ModelUsage] {
            get { state.withLock { $0 } }
            set { state.withLock { $0 = newValue } }
        }
    }

    /// A SlicesBox that also counts its calls.
    private final class SlicesCounter: @unchecked Sendable {
        private let state = Mutex<(rows: [ModelUsage], calls: Int)>(([], 0))
        var current: [ModelUsage] {
            get { state.withLock { $0.rows } }
            set { state.withLock { $0.rows = newValue } }
        }
        var calls: Int { state.withLock { $0.calls } }
        func call() -> [ModelUsage] {
            state.withLock { $0.calls += 1; return $0.rows }
        }
    }

    @Test func aNewUsageStampReloadsTheSlicesOnce() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
        ])
        let counter = SlicesCounter()
        counter.current = [slice("a", "claude", tokens: 100)]
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in counter.call() },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian))
        await model.load()
        #expect(counter.calls == 1)

        // The ledger re-scanned: the stamp moving reloads just the
        // usage, not the AA data.
        counter.current = [slice("a", "claude", tokens: 250)]
        await model.loadIfNeeded(usageStamp: 1)
        #expect(counter.calls == 2)
        #expect(model.rows.filter(\.used).first?.tokens == 250)

        // Same stamp: nothing moved, nothing re-read.
        counter.current = [slice("a", "claude", tokens: 999)]
        await model.loadIfNeeded(usageStamp: 1)
        #expect(counter.calls == 2)
        #expect(model.rows.filter(\.used).first?.tokens == 250)
    }

    @Test func aResetHarnessFilterShowsAllUsageAgain() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("a", "A", "a", creator: "openai", intelligence: 80),
            entry("b", "B", "b", creator: "anthropic", intelligence: 70),
        ])
        let box = SlicesBox()
        box.current = [slice("a", "codex", tokens: 100), slice("b", "claude", tokens: 50)]
        let model = ModelsModel(store: ArtificialAnalysisStore(directory: dir.path("aa")),
                                keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in box.current },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian))
        await model.load()
        model.set(harnessFilter: ["codex"])
        #expect(model.rows.map(\.logged) == ["a"])
        // The new period has no codex rows: the filter drops back to All
        // and the claude usage shows instead of an empty page.
        box.current = [slice("b", "claude", tokens: 50)]
        await model.set(period: .week)
        #expect(model.harnessFilter.isEmpty)
        #expect(model.rows.filter(\.used).map(\.logged) == ["b"])
    }

    @Test func theMergedRowIsWhatTheHoverCardReuses() async throws {
        // Two logged names fold into one row; the card gets that row, not
        // a card(for:) resolve of the dominant name, which would lose the
        // other alias's tokens and harness.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("grok", "Grok 4.7 (xhigh)", "grok-4-7-xhigh",
                  creator: "xai", intelligence: 46),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("grok-4.7", "grok", tokens: 100),
            slice("grok-4.7-fast", "cursor", tokens: 50),
        ])
        await model.load()
        let row = try #require(model.rows.first { $0.used })
        let resolved = model.card(for: row.logged)
        #expect(row.tokens == 150)
        #expect(row.harnesses == ["grok", "cursor"])
        #expect(resolved.tokens == 100)
        #expect(resolved.harnesses == ["grok"])
    }

    @Test func harnessSpelledNamesFoldIntoTheFoundationRow() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus5", "Claude Opus 5 (Max)", "claude-opus-5", creator: "anthropic", intelligence: 55),
            entry("grok46", "Grok 4.6 (High)", "grok-4-6", creator: "xai", intelligence: 44),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("claude-opus-5-thinking-high-fast", "devin", tokens: 100),
            slice("claude-opus-5", "claude", tokens: 40),
            slice("cursor-grok-4.6-high-fast", "cursor", tokens: 30),
        ])
        await model.load()
        let used = model.rows.filter(\.used)
        #expect(used.map(\.aa?.id) == ["opus5", "grok46"])
        #expect(used[0].tokens == 140)
        // The dominant name's run qualifiers show on the row.
        #expect(used[0].qualifier == "Thinking · High · Fast")
        #expect(used[1].qualifier == "High · Fast")
        #expect(model.unmatched.isEmpty)
    }

    // MARK: - Search

    @Test func aQueryFindsCatalogModelsNoScopeShows() async throws {
        // Kimi K3 sits in the AA list without usage or a board spot, the
        // hidden model a search has to reach.
        let dir = try TempDir()
        var entries = (0..<12).map { i in
            entry("top-\(i)", "Top \(i)", "top-\(i)", creator: "openai",
                  intelligence: Double(90 - i))
        }
        entries.append(entry("kimi", "Kimi K3 (Max)", "kimi-k3",
                             creator: "moonshot", intelligence: 45, price: 1.2))
        entries.append(entry("kimi-low", "Kimi K3 (Low)", "kimi-k3-low",
                             creator: "moonshot", intelligence: 40))
        entries.append(entry("mine", "My Model", "mine",
                             creator: "anthropic", intelligence: 10))
        try writeLLMs(dir, entries)
        let model = makeModel(dir: dir, slices: [slice("my-model", "claude", tokens: 10)])
        await model.load()
        #expect(!model.searching)
        #expect(model.rows.allSatisfy { $0.aa?.id != "kimi" })

        model.query = "kimi k3"
        #expect(model.searching)
        // Both effort variants collapse into the family's one headline row.
        let kimiRows = model.rows.filter { $0.aa?.id.hasPrefix("kimi") == true }
        #expect(kimiRows.count == 1)
        #expect(kimiRows[0].aa?.slug == "kimi-k3")
        #expect(kimiRows[0].value == 45)

        // Clearing restores the scoped board.
        model.query = ""
        #expect(!model.searching)
        #expect(model.rows.count == 11)
        #expect(model.rows.allSatisfy { $0.aa?.id != "kimi" })
    }

    @Test func aQueryMatchesSlugAndLabNotOnlyNames() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
            entry("kimi", "Kimi K3 (Max)", "kimi-k3",
                  creator: "moonshot", intelligence: 45),
        ])
        let model = makeModel(dir: dir)
        await model.load()
        // The slug's punctuation is invisible to the query.
        model.query = "KIMI-K3"
        #expect(model.rows.map(\.aa?.id) == ["kimi"])
        // A lab's name reaches its models.
        model.query = "Moonshot"
        #expect(model.rows.map(\.aa?.id) == ["kimi"])
        // Nothing matches: the page's empty state reads the empty rows.
        model.query = "no-such-model"
        #expect(model.rows.isEmpty)
        #expect(model.searching)
    }

    @Test func aQueryListsLiteLLMOnlyKeysWithTheirPrices() async throws {
        // Most of LiteLLM's list names no AA model; the search still
        // prices it — provider-qualified keys included.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        func priced(_ input: Double) -> [String: Any] {
            ["input_cost_per_token": input, "output_cost_per_token": input * 5]
        }
        try store.database.recordPrices([
            "wisp-9": priced(1e-06),
            "wisp-9-pro": priced(2e-06),
            "zeta/wisp-9": priced(3e-06),
        ], at: Date(), url: nil, sha: nil)
        store.database.rebuildAliases()
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in [] },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian),
                                searchDelay: .zero)
        await model.load(allowRefresh: false)
        model.query = "wisp"
        // Bare-name hits rank before provider-prefixed ones.
        #expect(model.rows.map(\.logged) == ["wisp-9", "wisp-9-pro", "zeta/wisp-9"])
        let wisp = try #require(model.rows.first)
        #expect(wisp.aa == nil && !wisp.used)
        let price = try #require(wisp.listPrice)
        #expect(price.key == "wisp-9" && price.source == .liteLLM)
        #expect(abs(price.input - 1) < 1e-9 && abs(price.output - 5) < 1e-9)
        // And the join never leaks them into the scoped board.
        model.query = ""
        #expect(model.rows.count == 1)
        #expect(model.rows[0].aa?.id == "opus")
    }

    @Test func aQueryReachesAnAAModelThroughItsPriceKeys() async throws {
        // "openrouter" names no AA model, but keys carrying it alias to
        // one: the model they price shows too.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70, price: 8),
            entry("sol", "GPT-6.1 Sol", "gpt-6-1-sol",
                  creator: "openai", intelligence: 60, price: 4),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        let priceEntry: [String: Any] = ["input_cost_per_token": 2e-06,
                                         "output_cost_per_token": 1e-05]
        try store.database.recordPrices([
            "gpt-6.1-sol": priceEntry,
            "openrouter/openai/gpt-6.1-sol": priceEntry,
        ], at: Date(), url: nil, sha: nil)
        store.database.rebuildAliases()
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in [] },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian),
                                searchDelay: .zero)
        await model.load(allowRefresh: false)
        model.query = "openrouter"
        #expect(model.rows.map(\.aa?.id) == ["sol"])
    }

    @Test func aQueryKeepsTheUsedRowsAndUnmatchedUsageInIt() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
            entry("opus6", "Claude Opus 6", "claude-opus-6",
                  creator: "anthropic", intelligence: 80),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("claude-opus-5-5", "claude", tokens: 100),
            slice("opus-proxy", "devin", tokens: 40),
        ])
        await model.load()
        model.query = "opus"
        // Both AA models, and the used row keeps its tokens and harness.
        #expect(model.rows.filter { $0.aa != nil }.map(\.aa?.id) == ["opus6", "opus"])
        let used = try #require(model.rows.first { $0.aa?.id == "opus" })
        #expect(used.tokens == 100 && used.harnesses == ["claude"])
        // The name the matcher missed still surfaces when it matches.
        let proxy = try #require(model.rows.first { $0.logged == "opus-proxy" })
        #expect(proxy.aa == nil && proxy.tokens == 40)
    }

    @Test func aQueryMatchesEveryNameAMergedRowFolded() async throws {
        // "grok-4.7" and "grok-4.7-fast" share one row under the dominant
        // name; a query hitting only the other name still finds the row.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("grok", "Grok 4.7 (xhigh)", "grok-4-7-xhigh",
                  creator: "xai", intelligence: 46),
        ])
        let model = makeModel(dir: dir, slices: [
            slice("grok-4.7", "grok", tokens: 100),
            slice("grok-4.7-fast", "cursor", tokens: 50),
        ])
        await model.load()
        model.query = "grok 4.7 fast"
        #expect(model.rows.map(\.aa?.id) == ["grok"])
        #expect(model.rows[0].tokens == 150)
        #expect(model.rows[0].harnesses == ["grok", "cursor"])
    }

    @Test func aUsedNameThatIsALiteLLMKeyShowsOnce() async throws {
        // The usage row already shows "wisp-9"; the price tail must not
        // list it again or count it as overflow.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        let priceEntry: [String: Any] = ["input_cost_per_token": 1e-06,
                                         "output_cost_per_token": 5e-06]
        try store.database.recordPrices(["wisp-9": priceEntry], at: Date(), url: nil, sha: nil)
        store.database.rebuildAliases()
        let used = [slice("wisp-9", "pi", tokens: 60)]
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in used },
                                pricing: {
                                    PricingCatalog(liteLLMEntries: ["wisp-9":
                                        ["input_cost_per_token": 1e-06,
                                         "output_cost_per_token": 5e-06]],
                                                   overrides: [:])
                                },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian),
                                searchDelay: .zero)
        await model.load(allowRefresh: false)
        model.query = "wisp"
        #expect(model.rows.map(\.logged) == ["wisp-9"])
        #expect(model.rows[0].tokens == 60)
        #expect(model.rows[0].listPrice?.key == "wisp-9")
        #expect(model.searchOverflow == 0)
    }

    @Test func aQueryFindsAnUnmatchedIDByItsAliasOnce() async throws {
        // No AA entry covers Haiku: "haiku" still finds the used
        // MODEL_PRIVATE_11 row, and its price key doesn't show again.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        let priceEntry: [String: Any] = ["input_cost_per_token": 1e-06,
                                         "output_cost_per_token": 5e-06]
        try store.database.recordPrices(["claude-haiku-4-5": priceEntry], at: Date(), url: nil, sha: nil)
        store.database.rebuildAliases()
        let used = [slice("MODEL_PRIVATE_11", "devin", tokens: 60)]
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in used },
                                pricing: {
                                    PricingCatalog(liteLLMEntries: ["claude-haiku-4-5":
                                        ["input_cost_per_token": 1e-06,
                                         "output_cost_per_token": 5e-06]],
                                                   overrides: [:])
                                },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian),
                                searchDelay: .zero)
        await model.load(allowRefresh: false)
        model.query = "haiku"
        #expect(model.rows.map(\.logged) == ["MODEL_PRIVATE_11"])
        #expect(model.rows[0].displayName == "claude-haiku-4-5")
        #expect(model.searchOverflow == 0)
    }

    @Test func aQueryCapsTheLiteLLMTailAndCountsTheRest() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        var keys: [String: [String: Any]] = [:]
        for i in 0..<(ModelsModel.searchLimit + 5) {
            keys["vendor-\(i)/flood-1"] = ["input_cost_per_token": 1e-06,
                                           "output_cost_per_token": 2e-06]
        }
        try store.database.recordPrices(keys, at: Date(), url: nil, sha: nil)
        store.database.rebuildAliases()
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in [] },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian),
                                searchDelay: .zero)
        await model.load(allowRefresh: false)
        model.query = "flood"
        #expect(model.rows.count == ModelsModel.searchLimit)
        #expect(model.searchOverflow == 5)
        model.query = ""
        #expect(model.searchOverflow == 0)
    }

    @Test func aDelayedSearchAppliesAfterThePause() async throws {
        // Typing coalesces: the flag moves at once, the rows only once
        // the pause runs out — never mid-query intermediate results.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("kimi", "Kimi K3 (Max)", "kimi-k3",
                  creator: "moonshot", intelligence: 45),
        ])
        let model = makeModel(dir: dir, searchDelay: .milliseconds(60))
        await model.load()
        let scoped = model.rows.map(\.logged)
        model.query = "kimi k3"
        // The flag waits with the rows: nothing flips mid-typing.
        #expect(!model.searching)
        #expect(model.rows.map(\.logged) == scoped)
        await model.settleSearch()
        #expect(model.searching)
        #expect(model.rows.map(\.aa?.id) == ["kimi"])
    }

    @Test func aQueryClearedInsideTheDelayKeepsTheBoard() async throws {
        // Clearing before the pause runs out re-arms the rebuild, and the
        // board comes back without the search's rows ever landing.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("kimi", "Kimi K3 (Max)", "kimi-k3",
                  creator: "moonshot", intelligence: 45),
        ])
        let model = makeModel(dir: dir, searchDelay: .milliseconds(60))
        await model.load()
        let scoped = model.rows.map(\.logged)
        model.query = "kimi"
        model.query = ""
        #expect(!model.searching)
        await model.settleSearch()
        #expect(model.rows.map(\.logged) == scoped)
        #expect(model.searchOverflow == 0)
    }

    @Test func aRevisitWithTheSameFeedKeepsTheMatchersAnswers() async throws {
        // Every page visit runs `load`. With no new models fetch since
        // the last one, it must not decode the models again or drop the
        // matcher's answers: both used to run on the main thread.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
        ])
        let model = makeModel(dir: dir,
                              slices: [slice("claude-opus-5-5", "claude", tokens: 10)])
        await model.load()
        let warmed = model.matcherMisses
        #expect(warmed > 0)
        #expect(model.updatedAt == Date(timeIntervalSince1970: 1_800_000_000))
        await model.load()
        #expect(model.matcherMisses == warmed)
        #expect(model.rows.first?.aa?.id == "opus")
        #expect(model.updatedAt == Date(timeIntervalSince1970: 1_800_000_000))
    }

    @Test func aNewFeedOrLinksFileLandsOnTheNextVisit() async throws {
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
        ])
        let store = ArtificialAnalysisStore(directory: dir.path("aa"))
        let used = [slice("my-alias", "claude", tokens: 10)]
        let model = ModelsModel(store: store, keyReader: { "key" }, transport: StubHTTP(),
                                linksURL: dir.path("config/model-links.json"),
                                slices: { _ in used },
                                now: { Date(timeIntervalSince1970: 1_800_000_000) },
                                calendar: Calendar(identifier: .gregorian), searchDelay: .zero)
        await model.load()
        #expect(model.allModels.map(\.id) == ["opus"])
        #expect(model.unmatched.map(\.model) == ["my-alias"])

        // Another window or the CLI writes a manual link: the next visit
        // rematches against the same models.
        try ModelMatcher.saveLinks(["my-alias": "opus"], to: dir.path("config/model-links.json"))
        await model.load()
        #expect(model.unmatched.isEmpty)
        #expect(model.rows.first?.aa?.id == "opus")

        // A newer models fetch replaces the list and its tables.
        let body = try JSONSerialization.data(withJSONObject: ["data": [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5", creator: "anthropic", intelligence: 70),
            entry("sol", "GPT-6.1 Sol", "gpt-6-1-sol", creator: "openai", intelligence: 80),
        ]])
        try store.database.recordAAPayload(.llms, body: body, at: Date(timeIntervalSince1970: 1_800_000_100),
                                           status: 200, rateLimitHeaders: nil, url: "test")
        await model.load()
        #expect(model.allModels.map(\.id) == ["opus", "sol"])
        #expect(model.updatedAt == Date(timeIntervalSince1970: 1_800_000_100))
        model.set(scope: .top25)
        #expect(model.rows.map(\.aa?.id) == ["sol", "opus"])
    }

    @Test func aRepeatQueryReadsTheCacheNotTheParser() async throws {
        // match answers memoize per name: a needle the search already
        // ran adds zero index lookups, which is what keeps typing cheap.
        let dir = try TempDir()
        try writeLLMs(dir, [
            entry("opus", "Claude Opus 5.5", "claude-opus-5-5",
                  creator: "anthropic", intelligence: 70),
            entry("kimi", "Kimi K3 (Max)", "kimi-k3",
                  creator: "moonshot", intelligence: 45),
        ])
        let model = makeModel(dir: dir,
                              slices: [slice("claude-opus-5-5", "claude", tokens: 10)])
        await model.load()
        model.query = "kimi"
        let warmed = model.matcherMisses
        #expect(warmed > 0)
        // A different needle parses only the names it alone reaches.
        model.query = "opus"
        #expect(model.matcherMisses > warmed)
        let beforeRepeat = model.matcherMisses
        // And the warmed query's names never hit the index again.
        model.query = "kimi"
        #expect(model.rows.map(\.aa?.id) == ["kimi"])
        #expect(model.matcherMisses == beforeRepeat)
    }

    @Test func aBroadQueryCapsTheWholeBoard() async throws {
        // A one-letter needle can hit hundreds of AA models; the page
        // lists the first screenful and counts the rest in the note.
        let dir = try TempDir()
        let entries = (0..<(ModelsModel.searchResultLimit + 20)).map { i in
            entry("flood-\(i)", "Flood \(i)", "flood-\(i)",
                  creator: "openai", intelligence: Double(100 - i))
        }
        try writeLLMs(dir, entries)
        let model = makeModel(dir: dir)
        await model.load()
        model.query = "flood"
        #expect(model.rows.count == ModelsModel.searchResultLimit)
        #expect(model.searchOverflow == 20)
    }
}
