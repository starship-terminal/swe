import Foundation
import Testing
@testable import SWECore

/// A LiteLLM-shaped list: `opus` and `o3` priced as given, plus whatever
/// `extra` adds.
private func priceList(opusInput: Double, o3: Bool = true,
                       extra: [String: [String: Any]] = [:]) -> [String: [String: Any]] {
    var list: [String: [String: Any]] = [
        "claude-opus-5-5": ["input_cost_per_token": opusInput, "output_cost_per_token": 2e-05,
                            "litellm_provider": "anthropic", "max_tokens": 64_000],
        "anthropic/claude-opus-5-5": ["input_cost_per_token": opusInput, "output_cost_per_token": 2e-05,
                                      "litellm_provider": "anthropic"],
        "dall-e-3": ["mode": "image_generation", "output_cost_per_image": 0.04],
    ]
    if o3 { list["o3"] = ["input_cost_per_token": 2e-06, "output_cost_per_token": 8e-06] }
    list.merge(extra) { _, new in new }
    return list
}

private func record(_ database: CatalogDatabase, _ list: [String: [String: Any]], at date: Date) throws {
    try database.recordPrices(CatalogDatabase.pricedEntries(list), at: date, url: nil, sha: nil)
}

private let llmsBody = Data("""
    {"data": [
      {"id": "aa-opus", "name": "Claude Opus 5.5", "slug": "claude-opus-5-5",
       "model_creator": {"id": "c", "name": "Anthropic", "slug": "anthropic"},
       "evaluations": {"artificial_analysis_intelligence_index": 70.0}, "pricing": {}},
      {"id": "aa-o3", "name": "o3", "slug": "o3",
       "model_creator": {"id": "o", "name": "OpenAI", "slug": "openai"},
       "evaluations": {"artificial_analysis_intelligence_index": 60.0}, "pricing": {}}
    ]}
    """.utf8)

@Suite struct CatalogDatabaseTests {
    let first = utc("2026-10-01T00:00:00Z")
    let second = utc("2026-10-10T00:00:00Z")

    @Test func aPriceChangeClosesTheOldRowAndKeepsIt() throws {
        let temp = try TempDir()
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        try record(database, priceList(opusInput: 5e-06), at: first)
        // Only a field that prices nothing changed: no new row.
        try record(database, priceList(opusInput: 5e-06, extra: [
            "claude-opus-5-5": ["input_cost_per_token": 5e-06, "output_cost_per_token": 2e-05,
                                "litellm_provider": "anthropic", "max_tokens": 128_000],
        ]), at: first.addingTimeInterval(86_400))
        #expect(database.prices(key: "claude-opus-5-5").count == 1)

        let changed = try database.recordPrices(
            CatalogDatabase.pricedEntries(priceList(opusInput: 4e-06, o3: false)), at: second, url: nil, sha: nil)
        // Both opus keys moved and o3 left the list.
        #expect(changed == 3)
        let opus = database.prices(key: "claude-opus-5-5")
        #expect(opus.map(\.input) == [5e-06, 4e-06])
        #expect(opus.map(\.validTo) == [second, nil])
        #expect(database.prices(key: "o3").map(\.validTo) == [second])
        #expect(database.currentPriceEntries()?["o3"] == nil)
        // The image model is never priced per token, so never stored.
        #expect(database.prices(key: "dall-e-3").isEmpty)
    }

    @Test func theCatalogPricesAKeyAtTheRateOfTheTime() throws {
        let temp = try TempDir()
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        try record(database, priceList(opusInput: 5e-06), at: first)
        try record(database, priceList(opusInput: 4e-06), at: second)
        let catalog = try PricingStore(database: database, minimumModels: 1).load()
        #expect(catalog.rates(for: "claude-opus-5-5")?.input == 4e-06)
        let middle = first.addingTimeInterval(3_600).timeIntervalSince1970
        #expect(catalog.historicalRates(entry: "claude-opus-5-5", at: middle)?.input == 5e-06)
        // Before the first download: the first known price, not nothing.
        #expect(catalog.historicalRates(entry: "claude-opus-5-5", at: 0)?.input == 5e-06)
        #expect(catalog.historicalRates(entry: "claude-opus-5-5", at: second.timeIntervalSince1970)?.input
                == 4e-06)
        // A key priced once has no history to consult.
        #expect(catalog.historicalRates(entry: "o3", at: middle) == nil)
        #expect(catalog.priceChanges(for: ["claude-opus-5-5", "o3"]) == [second.timeIntervalSince1970])
    }

    @Test func theLedgerChargesEachEventTheRateOfItsTime() async throws {
        let temp = try TempDir()
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        try record(database, priceList(opusInput: 5e-06), at: first)
        try record(database, priceList(opusInput: 4e-06), at: second)
        let store = try LedgerStore(url: temp.path("cache/ledger.sqlite"))
        for (index, day) in [first.addingTimeInterval(86_400), second.addingTimeInterval(86_400)].enumerated() {
            try store.upsert(LedgerRecord(key: "t:\(index)", event: UsageEvent(
                harness: "claude", accountHome: "", sessionId: "s", model: "claude-opus-5-5",
                timestamp: day, inputTokens: 1_000_000)))
        }
        let ledger = try Ledger(directory: temp.path("cache"),
                                pricing: try PricingStore(database: database, minimumModels: 1).load())
        // $5 before the change plus $4 after, not $8 at today's price.
        let total = try #require(await ledger.totals(in: october2026).costUSD)
        #expect(abs(total - 9) < 1e-9)
        let sessions = await ledger.bySession(in: october2026)
        #expect(abs((sessions.first?.totals.costUSD ?? 0) - 9) < 1e-9)
        // A range after the change sees only the new rate.
        let late = DateInterval(start: second, end: october2026.end)
        #expect(abs((await ledger.totals(in: late).costUSD ?? 0) - 4) < 1e-9)
    }

    @Test func eachModelHasThePricesOfEveryKeyThatNamesIt() throws {
        let temp = try TempDir()
        let links = temp.path("config/model-links.json")
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"), linksFile: links)
        let store = ArtificialAnalysisStore(database: database, now: { self.first })
        try database.recordAAPayload(.llms, body: llmsBody, at: first, status: 200,
                                     rateLimitHeaders: nil, url: nil)
        try record(database, priceList(opusInput: 5e-06, extra: [
            "mystery-model": ["input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06],
        ]), at: first)
        try record(database, priceList(opusInput: 4e-06, extra: [
            "mystery-model": ["input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06],
        ]), at: second)
        database.rebuildAliases()

        #expect(store.languageModels()?.map(\.id) == ["aa-opus", "aa-o3"])
        #expect(database.alias("anthropic/claude-opus-5-5")?.modelID == "aa-opus")
        #expect(database.alias("anthropic/claude-opus-5-5")?.method == .auto)
        #expect(database.alias("mystery-model")?.modelID == nil)
        #expect(database.alias("mystery-model")?.method == CatalogDatabase.AliasMethod.none)

        let current = database.prices(forModel: "aa-opus")
        #expect(Set(current.map(\.key)) == ["claude-opus-5-5", "anthropic/claude-opus-5-5"])
        #expect(current.allSatisfy { $0.input == 4e-06 })
        #expect(database.prices(forModel: "aa-opus", history: true).count == 4)

        // A manual link wins over the rules.
        try ModelMatcher.saveLinks(["mystery-model": "aa-o3"], to: links)
        database.rebuildAliases()
        #expect(database.alias("mystery-model")?.modelID == "aa-o3")
        #expect(database.alias("mystery-model")?.method == .manual)
        #expect(Set(database.prices(forModel: "aa-o3").map(\.key)) == ["o3", "mystery-model"])
    }

    @Test func aModelAAStopsListingLeavesItsPriceKeysUnmatched() throws {
        let temp = try TempDir()
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        try database.recordAAPayload(.llms, body: llmsBody, at: first, status: 200,
                                     rateLimitHeaders: nil, url: nil)
        try record(database, priceList(opusInput: 5e-06), at: first)
        database.rebuildAliases()
        #expect(database.alias("o3")?.modelID == "aa-o3")
        let onlyOpus = Data("""
            {"data": [{"id": "aa-opus", "name": "Claude Opus 5.5", "slug": "claude-opus-5-5",
              "model_creator": {"id": "c", "name": "Anthropic", "slug": "anthropic"}}]}
            """.utf8)
        try database.recordAAPayload(.llms, body: onlyOpus, at: second, status: 200,
                                     rateLimitHeaders: nil, url: nil)
        #expect(database.alias("o3")?.modelID == nil)
        #expect(database.alias("claude-opus-5-5")?.modelID == "aa-opus")
        #expect(database.languageModels()?.count == 1)
    }

    /// The v5 feed: Opus re-scored, o3 left without a score.
    private let v5Body = Data("""
        {"tier": "free", "intelligence_index_version": 5, "data": [
          {"id": "aa-opus", "name": "Claude Opus 5.5", "slug": "claude-opus-5-5",
           "model_creator": {"id": "c", "name": "Anthropic"},
           "evaluations": {"artificial_analysis_intelligence_index": 59.0}, "pricing": {}},
          {"id": "aa-o3", "name": "o3", "slug": "o3",
           "model_creator": {"id": "o", "name": "OpenAI"},
           "evaluations": {"artificial_analysis_intelligence_index": null}, "pricing": {}}
        ]}
        """.utf8)

    @Test func aScoreAANoLongerPublishesIsKeptWithItsVersionAndDate() throws {
        let temp = try TempDir()
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        let v4 = Data(String(decoding: llmsBody, as: UTF8.self)
            .replacingOccurrences(of: #"{"data""#, with: #"{"intelligence_index_version": "4.3", "data""#).utf8)
        try database.recordAAPayload(.llms, body: v4, at: first, status: 200, rateLimitHeaders: nil, url: nil)
        // While the feed scores a model there is no past score to show.
        #expect(database.languageModels()?.map(\.pastIntelligence) == [nil, nil])

        try database.recordAAPayload(.llms, body: v5Body, at: second, status: 200, rateLimitHeaders: nil, url: nil)
        let models = try #require(database.languageModels())
        #expect(models.map(\.intelligence) == [59, nil])
        #expect(models[0].pastIntelligence == nil)
        #expect(models[1].pastIntelligence == AAPastScore(value: 60, indexVersion: "4.3", scoredAt: first))
    }

    @Test func aCatalogFromBeforeTheTableBackfillsFromTheLegacyFileOnce() throws {
        let temp = try TempDir()
        let legacy = temp.path("legacy")
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        try llmsBody.write(to: legacy.appending(path: "llms.json"))
        try Data(#"{"llms": {"fetchedAt": 1000, "status": 200}}"#.utf8)
            .write(to: legacy.appending(path: "meta.json"))
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        try database.recordAAPayload(.llms, body: v5Body, at: second, status: 200, rateLimitHeaders: nil, url: nil)
        // An older build stored the v5 feed without keeping any score.
        try SQLiteDatabase(url: temp.path("catalog.sqlite")).execute("DELETE FROM model_last_scores")

        database.backfillLastScores(legacyDirectory: legacy)
        let models = try #require(database.languageModels())
        // Opus has a current score; o3 takes the legacy one, version unknown.
        #expect(models[0].pastIntelligence == nil)
        #expect(models[1].pastIntelligence
                == AAPastScore(value: 60, indexVersion: nil, scoredAt: Date(timeIntervalSince1970: 1000)))

        // Once only: a second run does not bring a deleted row back.
        try SQLiteDatabase(url: temp.path("catalog.sqlite")).execute("DELETE FROM model_last_scores")
        database.backfillLastScores(legacyDirectory: legacy)
        #expect(database.languageModels()?[1].pastIntelligence == nil)
    }

    @Test func failuresSinceTheLastSuccessCollapseToTheLatest() throws {
        let temp = try TempDir()
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        let source = CatalogDatabase.liteLLMSource
        database.recordFailure(source: source, url: nil, at: first, status: nil, error: "offline")
        try record(database, priceList(opusInput: 5e-06), at: first.addingTimeInterval(60))
        for (index, error) in ["HTTP 500", "HTTP 502", "offline"].enumerated() {
            database.recordFailure(source: source, url: nil, at: second.addingTimeInterval(Double(index)),
                                   status: nil, error: error)
        }
        #expect(database.fetches(source: source).map(\.error) == ["offline", nil, "offline"])
        #expect(database.lastAttempt(source: source)?.fetchedAt == second.addingTimeInterval(2))
        #expect(database.lastSuccess(source: source)?.fetchedAt == first.addingTimeInterval(60))
    }

    @Test func anUnopenableFileLeavesAnEmptyCatalog() throws {
        let temp = try TempDir()
        // A directory where the file should be.
        let path = temp.path("catalog.sqlite")
        try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true)
        let database = CatalogDatabase(url: path)
        #expect(database.openError != nil)
        #expect(database.currentPriceEntries() == nil)
        #expect(database.fetches().isEmpty)
        // Pricing still loads, from the bundled snapshot.
        #expect(try PricingStore(database: database).load().rates(for: "claude-opus-5-5") != nil)
    }
}

@Suite struct CatalogImportTests {
    @Test func theOldPriceCacheImportsOnceDatedByItsFile() throws {
        let temp = try TempDir()
        let cache = temp.path("cache/pricing/litellm.json")
        try FileManager.default.createDirectory(at: cache.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: priceList(opusInput: 5e-06)).write(to: cache)
        let modified = utc("2026-10-05T12:00:00Z")
        try FileManager.default.setAttributes([.modificationDate: modified], ofItemAtPath: cache.path)

        let database = CatalogDatabase(url: temp.path("data/catalog/catalog.sqlite"))
        let store = PricingStore(database: database, legacyCacheFile: cache, minimumModels: 1)
        #expect(try store.load().rates(for: "o3")?.input == 2e-06)
        let fetches = database.fetches(source: CatalogDatabase.liteLLMSource)
        #expect(fetches.count == 1)
        #expect(fetches.first?.fetchedAt == modified)
        #expect(database.prices(key: "o3").first?.validFrom == modified)
        // The file stays; a second start does not import it again.
        #expect(FileManager.default.fileExists(atPath: cache.path))
        _ = PricingStore(database: database, legacyCacheFile: cache, minimumModels: 1)
        #expect(database.fetches().count == 1)
    }

    @Test func theOldAAFilesImportWithTheirMeta() throws {
        let temp = try TempDir()
        let legacy = temp.path("data/artificial-analysis")
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        try llmsBody.write(to: legacy.appending(path: AAEndpoint.llms.fileName))
        try Data(#"{"images": []}"#.utf8).write(to: legacy.appending(path: AAEndpoint.textToImage.fileName))
        try Data("""
            {"llms": {"fetchedAt": 1791000000, "status": 200, "rateLimitHeaders": {"X-RateLimit-Remaining": "7"}},
             "text-to-image": {"fetchedAt": 1791000001, "status": 429, "error": "HTTP 429"}}
            """.utf8).write(to: legacy.appending(path: "meta.json"))

        let database = CatalogDatabase(url: temp.path("data/catalog/catalog.sqlite"))
        let store = ArtificialAnalysisStore(database: database, legacyDirectory: legacy,
                                            now: { Date(timeIntervalSince1970: 1_791_000_100) })
        #expect(store.languageModels()?.map(\.name) == ["Claude Opus 5.5", "o3"])
        #expect(!store.llmsIsStale())
        let meta = store.meta()
        #expect(meta["llms"]?.fetchedAt == 1_791_000_000)
        #expect(meta["llms"]?.rateLimitHeaders?["X-RateLimit-Remaining"] == "7")
        #expect(meta["text-to-image"]?.error == "HTTP 429")
        #expect(store.raw(.textToImage) == Data(#"{"images": []}"#.utf8))
        #expect(store.raw(.textToVideo) == nil)

        // Importing again is a no-op.
        let count = database.fetches().count
        _ = ArtificialAnalysisStore(database: database, legacyDirectory: legacy)
        #expect(database.fetches().count == count)
    }
}

@Suite struct CatalogImportResumeTests {
    @Test func anAAImportCutShortResumesWithTheEndpointsLeft() throws {
        let temp = try TempDir()
        let legacy = temp.path("aa")
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        try llmsBody.write(to: legacy.appending(path: AAEndpoint.llms.fileName))
        try Data(#"{"voices": []}"#.utf8).write(to: legacy.appending(path: AAEndpoint.textToSpeech.fileName))
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"))
        // A launch that stopped after storing only the models feed.
        try database.recordAAPayload(.llms, body: llmsBody, at: nil, status: nil,
                                     rateLimitHeaders: nil, url: nil)
        database.importLegacyAA(directory: legacy)
        #expect(database.payload(.textToSpeech) == Data(#"{"voices": []}"#.utf8))
        #expect(database.fetches(source: "aa:llms").count == 1)
    }

    @Test func concurrentRebuildsAllFinishOnTheLatestLinks() async throws {
        let temp = try TempDir()
        let links = temp.path("model-links.json")
        let database = CatalogDatabase(url: temp.path("catalog.sqlite"), linksFile: links)
        try database.recordAAPayload(.llms, body: llmsBody, at: nil, status: 200, rateLimitHeaders: nil, url: nil)
        try database.recordPrices(CatalogDatabase.pricedEntries([
            "mystery-model": ["input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06],
        ]), at: Date(), url: nil, sha: nil)
        try ModelMatcher.saveLinks(["mystery-model": "aa-o3"], to: links)
        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<8 { group.addTask { database.rebuildAliases() } }
        }
        #expect(database.alias("mystery-model")?.modelID == "aa-o3")
    }
}
