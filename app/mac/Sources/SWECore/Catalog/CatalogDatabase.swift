import CryptoKit
import Foundation

/// SWE's model catalog, one SQLite file of data (not cache, so clearing
/// caches never loses it): the LiteLLM price list with every price a key
/// ever had, Artificial Analysis's models and raw feeds, the join between
/// the two, and a row for every fetch.
///
/// - `fetches`: one row per download attempt, any source. Failures since a
///   source's last success collapse to the latest one.
/// - `prices`: one row per LiteLLM key per price it held. A new row is
///   written only when a key's price fields change; `valid_to` is null on
///   the row in force.
/// - `models`: AA's language models, in AA's order.
/// - `model_aliases`: every price key, and the AA model it names (null
///   when AA has none). Derived: `rebuildAliases` rewrites it from the
///   matcher and the manual links in `config/model-links.json`.
/// - `payloads`: each AA endpoint's last good response body.
///
/// One connection behind a lock, so every method is safe from any thread.
/// A file that cannot be opened leaves the catalog empty (`openError` says
/// why) and callers fall back to the bundled price snapshot.
public final class CatalogDatabase: @unchecked Sendable {
    public static let schemaVersion = 1

    public let url: URL
    /// Why the file could not be opened, or nil.
    public let openError: String?
    /// `config/model-links.json`, read by `rebuildAliases`.
    public let linksFile: URL?
    private let db: SQLiteDatabase?
    private let lock = NSLock()
    /// Held for a whole `rebuildAliases`, reads included, so an older
    /// rebuild (old links, old models) can never land after a newer one.
    private let rebuildLock = NSLock()

    /// `~/.swe/data/catalog/catalog.sqlite`.
    public static func defaultURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home.appending(path: ".swe/data/catalog/catalog.sqlite")
    }

    /// `SWE_CATALOG_DB` wins; a test build that only set `SWE_AA_DATA_DIR`
    /// keeps its catalog in that throwaway directory, so it never touches
    /// the real price history. The `QUIVER_*` spellings still work.
    public static func resolvedURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        let environment = ProcessInfo.processInfo.environment
        if let path = environment["SWE_CATALOG_DB"] ?? environment["QUIVER_CATALOG_DB"], !path.isEmpty {
            return URL(fileURLWithPath: path)
        }
        if let directory = environment["SWE_AA_DATA_DIR"] ?? environment["QUIVER_AA_DATA_DIR"], !directory.isEmpty {
            return URL(fileURLWithPath: directory).appending(path: "catalog.sqlite")
        }
        return defaultURL(home: home)
    }

    public init(url: URL, linksFile: URL? = nil) {
        self.url = url
        self.linksFile = linksFile
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            let db = try SQLiteDatabase(url: url)
            try Self.migrate(db)
            self.db = db
            openError = nil
        } catch {
            db = nil
            openError = error.localizedDescription
        }
    }

    private static func migrate(_ db: SQLiteDatabase) throws {
        try db.execute("""
            PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;
            PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fetches (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                url TEXT,
                fetched_at REAL,
                status INTEGER,
                error TEXT,
                content_sha TEXT,
                row_count INTEGER,
                rate_limit_headers TEXT);
            CREATE INDEX IF NOT EXISTS fetches_source ON fetches (source, id);
            CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                creator_name TEXT NOT NULL,
                creator_slug TEXT NOT NULL,
                aa_price_blended REAL,
                aa_price_input REAL,
                aa_price_output REAL,
                speed REAL,
                ttft REAL,
                ttfat REAL,
                evaluations TEXT NOT NULL,
                raw TEXT NOT NULL,
                fetch_id INTEGER NOT NULL REFERENCES fetches (id));
            CREATE TABLE IF NOT EXISTS model_last_scores (
                model_id TEXT PRIMARY KEY,
                intelligence REAL NOT NULL,
                index_version TEXT,
                scored_at REAL);
            CREATE TABLE IF NOT EXISTS model_aliases (
                name TEXT PRIMARY KEY,
                model_id TEXT REFERENCES models (id) ON DELETE SET NULL,
                method TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS model_aliases_model ON model_aliases (model_id);
            CREATE TABLE IF NOT EXISTS prices (
                key TEXT NOT NULL REFERENCES model_aliases (name),
                valid_from REAL NOT NULL,
                valid_to REAL,
                input REAL NOT NULL,
                output REAL NOT NULL,
                cache_read REAL NOT NULL,
                cache_write REAL NOT NULL,
                cache_write_1h REAL NOT NULL,
                reasoning REAL NOT NULL,
                entry TEXT NOT NULL,
                fetch_id INTEGER NOT NULL REFERENCES fetches (id),
                PRIMARY KEY (key, valid_from));
            CREATE INDEX IF NOT EXISTS prices_current ON prices (key) WHERE valid_to IS NULL;
            CREATE TABLE IF NOT EXISTS payloads (
                source TEXT PRIMARY KEY,
                fetch_id INTEGER NOT NULL REFERENCES fetches (id),
                body BLOB NOT NULL);
            INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', '\(schemaVersion)');
            """)
    }

    /// Runs `body` on the open connection under the lock; nil when the
    /// file never opened or `body` threw.
    private func withDB<T>(_ body: (SQLiteDatabase) throws -> T) -> T? {
        lock.lock()
        defer { lock.unlock() }
        guard let db else { return nil }
        return try? body(db)
    }

    /// `withDB` inside one transaction, rolled back when `body` throws.
    private func transaction<T>(_ body: (SQLiteDatabase) throws -> T) throws -> T {
        lock.lock()
        defer { lock.unlock() }
        guard let db else { throw CatalogError.notOpen(openError ?? "") }
        try db.execute("BEGIN IMMEDIATE")
        do {
            let result = try body(db)
            try db.execute("COMMIT")
            return result
        } catch {
            try? db.execute("ROLLBACK")
            throw error
        }
    }

    static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

public enum CatalogError: Error, Equatable {
    case notOpen(String)
}

// MARK: - Fetches

extension CatalogDatabase {
    /// One download attempt. A success has no `error`; an import from a
    /// file that carried no date has no `fetchedAt`.
    public struct Fetch: Sendable, Equatable {
        public var id: Int64
        public var source: String
        public var url: String?
        public var fetchedAt: Date?
        public var status: Int?
        public var error: String?
        public var contentSHA: String?
        public var rowCount: Int?
        public var rateLimitHeaders: [String: String]?
    }

    private static let fetchColumns =
        "id, source, url, fetched_at, status, error, content_sha, row_count, rate_limit_headers"

    private static func fetch(from row: Statement) -> Fetch {
        Fetch(
            id: row.int(0), source: row.text(1) ?? "", url: row.text(2),
            fetchedAt: row.isNull(3) ? nil : Date(timeIntervalSince1970: row.double(3)),
            status: row.isNull(4) ? nil : Int(row.int(4)),
            error: row.text(5), contentSHA: row.text(6),
            rowCount: row.isNull(7) ? nil : Int(row.int(7)),
            rateLimitHeaders: row.text(8).flatMap {
                try? JSONDecoder().decode([String: String].self, from: Data($0.utf8))
            })
    }

    /// Every fetch, oldest first; one source's when `source` is given.
    public func fetches(source: String? = nil) -> [Fetch] {
        withDB { db in
            let statement = try db.prepare("""
                SELECT \(Self.fetchColumns) FROM fetches
                \(source == nil ? "" : "WHERE source = ?") ORDER BY id
                """)
            statement.bind(source.map { [SQLiteValue.text($0)] } ?? [])
            var result: [Fetch] = []
            while try statement.step() { result.append(Self.fetch(from: statement)) }
            return result
        } ?? []
    }

    /// The latest attempt for `source`, good or bad.
    public func lastAttempt(source: String) -> Fetch? {
        withDB { db in
            let statement = try db.prepare(
                "SELECT \(Self.fetchColumns) FROM fetches WHERE source = ? ORDER BY id DESC LIMIT 1")
            statement.bind([.text(source)])
            return try statement.step() ? Self.fetch(from: statement) : nil
        } ?? nil
    }

    /// The latest successful fetch for `source`.
    public func lastSuccess(source: String) -> Fetch? {
        withDB { db in
            let statement = try db.prepare("""
                SELECT \(Self.fetchColumns) FROM fetches
                WHERE source = ? AND error IS NULL ORDER BY id DESC LIMIT 1
                """)
            statement.bind([.text(source)])
            return try statement.step() ? Self.fetch(from: statement) : nil
        } ?? nil
    }

    /// Whether any fetch, of any source with this prefix, was ever recorded.
    func hasFetch(sourcePrefix: String) -> Bool {
        withDB { db in
            let statement = try db.prepare("SELECT 1 FROM fetches WHERE source LIKE ? || '%' LIMIT 1")
            statement.bind([.text(sourcePrefix)])
            return try statement.step()
        } ?? false
    }

    private static func headersJSON(_ headers: [String: String]?) -> SQLiteValue {
        guard let headers, !headers.isEmpty,
              let data = try? JSONEncoder().encode(headers) else { return .null }
        return .text(String(decoding: data, as: UTF8.self))
    }

    private static func insertFetch(
        _ db: SQLiteDatabase, source: String, url: String?, at date: Date?, status: Int?,
        error: String?, sha: String?, rowCount: Int?, headers: [String: String]?
    ) throws -> Int64 {
        if error != nil {
            // Only the latest failure since the last success is kept, so a
            // long outage is one row, not one per attempt.
            try db.prepare("""
                DELETE FROM fetches WHERE source = ?1 AND error IS NOT NULL
                AND id > COALESCE((SELECT MAX(id) FROM fetches WHERE source = ?1 AND error IS NULL), 0)
                """).bind([.text(source)]).run()
        }
        try db.prepare("""
            INSERT INTO fetches (source, url, fetched_at, status, error, content_sha, row_count, rate_limit_headers)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """).bind([
                .text(source), url.map(SQLiteValue.text), .optionalDouble(date?.timeIntervalSince1970),
                status.map { .int($0) }, error.map(SQLiteValue.text), sha.map(SQLiteValue.text),
                rowCount.map { .int($0) }, headersJSON(headers),
            ]).run()
        return db.lastInsertRowID
    }

    /// Records a download that produced nothing usable. Stored data stays.
    public func recordFailure(source: String, url: URL?, at date: Date, status: Int?, error: String,
                              rateLimitHeaders: [String: String]? = nil) {
        _ = try? transaction { db in
            try Self.insertFetch(db, source: source, url: url?.absoluteString, at: date, status: status,
                                 error: error, sha: nil, rowCount: nil, headers: rateLimitHeaders)
        }
    }
}

// MARK: - Prices

extension CatalogDatabase {
    public static let liteLLMSource = "litellm"

    /// One price a LiteLLM key held, USD per token.
    public struct PriceRow: Sendable, Equatable {
        public var key: String
        public var validFrom: Date
        /// Nil while the price is in force.
        public var validTo: Date?
        public var input: Double
        public var output: Double
        public var cacheRead: Double
        public var cacheWrite: Double
        public var cacheWrite1h: Double
        public var reasoning: Double
    }

    /// One key's history as `PricingCatalog` reads it: the raw entry and
    /// the span it was in force.
    struct PriceSpan {
        var from: Double
        var to: Double?
        var entry: [String: Any]
    }

    /// The fields of a LiteLLM entry that price it. Everything else
    /// (context windows, feature flags) changes often and prices nothing,
    /// so it is neither stored nor compared.
    static func priceFields(of entry: [String: Any]) -> [String: Any] {
        entry.filter { $0.key.contains("cost") || $0.key == "mode" || $0.key == "litellm_provider" }
    }

    static func canonicalJSON(_ object: [String: Any]) -> String? {
        (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
            .map { String(decoding: $0, as: UTF8.self) }
    }

    /// Stores a downloaded price list: a fetch row, then a new price row for
    /// every key whose price fields changed, closing the row it replaces.
    /// Keys gone from the list are closed too. `entries` holds only the
    /// priced chat entries. Returns how many keys changed.
    @discardableResult
    func recordPrices(_ entries: [String: [String: Any]], at date: Date, url: String?,
                      sha: String?) throws -> Int {
        let now = date.timeIntervalSince1970
        return try transaction { db in
            let fetchID = try Self.insertFetch(
                db, source: Self.liteLLMSource, url: url, at: date, status: 200, error: nil,
                sha: sha, rowCount: entries.count, headers: nil)
            var current: [String: String] = [:]
            let select = try db.prepare("SELECT key, entry FROM prices WHERE valid_to IS NULL")
            while try select.step() { current[select.text(0) ?? ""] = select.text(1) ?? "" }
            let close = try db.prepare("UPDATE prices SET valid_to = ? WHERE key = ? AND valid_to IS NULL")
            let alias = try db.prepare(
                "INSERT OR IGNORE INTO model_aliases (name, model_id, method) VALUES (?, NULL, 'none')")
            // REPLACE: a second change at the same instant overwrites the
            // first rather than leaving a zero-length span.
            let insert = try db.prepare("""
                INSERT OR REPLACE INTO prices (key, valid_from, valid_to, input, output, cache_read,
                    cache_write, cache_write_1h, reasoning, entry, fetch_id)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """)
            var changed = 0
            for (key, entry) in entries {
                let fields = Self.priceFields(of: entry)
                guard let json = Self.canonicalJSON(fields), let rates = PricingCatalog.rates(from: fields)
                else { continue }
                if current[key] == json { continue }
                if current[key] != nil { try close.bind([.double(now), .text(key)]).run() }
                try alias.bind([.text(key)]).run()
                try insert.bind([
                    .text(key), .double(now), .double(rates.input), .double(rates.output),
                    .double(rates.cacheRead), .double(rates.cacheWrite), .double(rates.cacheWrite1h),
                    .double(rates.reasoning), .text(json), .int(fetchID),
                ]).run()
                changed += 1
            }
            for key in current.keys where entries[key] == nil {
                try close.bind([.double(now), .text(key)]).run()
                changed += 1
            }
            return changed
        }
    }

    /// The entries in force, LiteLLM-shaped; nil before the first download.
    func currentPriceEntries() -> [String: [String: Any]]? {
        withDB { db -> [String: [String: Any]]? in
            let statement = try db.prepare("SELECT key, entry FROM prices WHERE valid_to IS NULL")
            var result: [String: [String: Any]] = [:]
            var any = false
            while try statement.step() {
                any = true
                guard let key = statement.text(0), let json = statement.text(1),
                      let entry = try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]
                else { continue }
                result[key] = entry
            }
            if !any {
                // Every key closed is still a download, just an empty list.
                let anyRow = try db.prepare("SELECT 1 FROM prices LIMIT 1")
                return try anyRow.step() ? [:] : nil
            }
            return result
        } ?? nil
    }

    /// The history of every key whose price ever changed or that left the
    /// list, oldest span first. Keys priced once and still listed have
    /// nothing to add to the current entries, so they are left out.
    func priceHistory() -> [String: [PriceSpan]] {
        withDB { db in
            let statement = try db.prepare("""
                SELECT key, valid_from, valid_to, entry FROM prices
                WHERE key IN (SELECT key FROM prices GROUP BY key
                              HAVING COUNT(*) > 1 OR MAX(valid_to IS NOT NULL) = 1)
                ORDER BY key, valid_from
                """)
            var result: [String: [PriceSpan]] = [:]
            while try statement.step() {
                guard let key = statement.text(0), let json = statement.text(3),
                      let entry = try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]
                else { continue }
                result[key, default: []].append(PriceSpan(
                    from: statement.double(1), to: statement.isNull(2) ? nil : statement.double(2), entry: entry))
            }
            return result
        } ?? [:]
    }

    private static func priceRow(from row: Statement) -> PriceRow {
        PriceRow(
            key: row.text(0) ?? "", validFrom: Date(timeIntervalSince1970: row.double(1)),
            validTo: row.isNull(2) ? nil : Date(timeIntervalSince1970: row.double(2)),
            input: row.double(3), output: row.double(4), cacheRead: row.double(5),
            cacheWrite: row.double(6), cacheWrite1h: row.double(7), reasoning: row.double(8))
    }

    private static let priceColumns =
        "p.key, p.valid_from, p.valid_to, p.input, p.output, p.cache_read, p.cache_write, p.cache_write_1h, p.reasoning"

    /// Every price row of one key, oldest first.
    public func prices(key: String) -> [PriceRow] {
        withDB { db in
            let statement = try db.prepare(
                "SELECT \(Self.priceColumns) FROM prices p WHERE p.key = ? ORDER BY p.valid_from")
            statement.bind([.text(key)])
            var result: [PriceRow] = []
            while try statement.step() { result.append(Self.priceRow(from: statement)) }
            return result
        } ?? []
    }

    /// The prices of every LiteLLM key that names AA model `id`: one model,
    /// many keys (bare, provider-prefixed, dated), each with its history
    /// when `history` is on, else only the rows in force.
    public func prices(forModel id: String, history: Bool = false) -> [PriceRow] {
        withDB { db in
            let statement = try db.prepare("""
                SELECT \(Self.priceColumns) FROM prices p
                JOIN model_aliases a ON a.name = p.key
                WHERE a.model_id = ? \(history ? "" : "AND p.valid_to IS NULL")
                ORDER BY p.key, p.valid_from
                """)
            statement.bind([.text(id)])
            var result: [PriceRow] = []
            while try statement.step() { result.append(Self.priceRow(from: statement)) }
            return result
        } ?? []
    }
}

extension CatalogDatabase {
    /// The rows in force of every key that names an AA model, by model id:
    /// `prices(forModel:)` for all models in one query.
    public func currentPricesByModel() -> [String: [PriceRow]] {
        withDB { db in
            let statement = try db.prepare("""
                SELECT \(Self.priceColumns), a.model_id FROM prices p
                JOIN model_aliases a ON a.name = p.key
                WHERE a.model_id IS NOT NULL AND p.valid_to IS NULL
                ORDER BY p.key
                """)
            var result: [String: [PriceRow]] = [:]
            while try statement.step() {
                guard let id = statement.text(9) else { continue }
                result[id, default: []].append(Self.priceRow(from: statement))
            }
            return result
        } ?? [:]
    }

    /// The rows in force of every priced key that names no AA model: the
    /// part of LiteLLM's list the Models page's search can still price.
    public func currentPricesUnmatched() -> [PriceRow] {
        withDB { db in
            let statement = try db.prepare("""
                SELECT \(Self.priceColumns) FROM prices p
                JOIN model_aliases a ON a.name = p.key
                WHERE a.model_id IS NULL AND p.valid_to IS NULL
                ORDER BY p.key
                """)
            var result: [PriceRow] = []
            while try statement.step() { result.append(Self.priceRow(from: statement)) }
            return result
        } ?? []
    }
}

// MARK: - Artificial Analysis

extension CatalogDatabase {
    static func aaSource(_ endpoint: AAEndpoint) -> String { "aa:\(endpoint.rawValue)" }

    /// Stores one good AA response: the fetch, the body, and for the
    /// language-model feed the `models` table, replaced to match it.
    func recordAAPayload(_ endpoint: AAEndpoint, body: Data, at date: Date?, status: Int?,
                         rateLimitHeaders: [String: String]?, url: String?) throws {
        let models: [(model: AALanguageModel, raw: String)]? = endpoint == .llms
            ? try Self.decodeModels(body) : nil
        try transaction { db in
            let fetchID = try Self.insertFetch(
                db, source: Self.aaSource(endpoint), url: url, at: date, status: status, error: nil,
                sha: Self.sha256(body), rowCount: models?.count, headers: rateLimitHeaders)
            try db.prepare("INSERT OR REPLACE INTO payloads (source, fetch_id, body) VALUES (?, ?, ?)")
                .bind([.text(Self.aaSource(endpoint)), .int(fetchID), .blob(body)]).run()
            guard let models else { return }
            let ids = Set(models.map(\.model.id))
            var stale: [String] = []
            let existing = try db.prepare("SELECT id FROM models")
            while try existing.step() {
                if let id = existing.text(0), !ids.contains(id) { stale.append(id) }
            }
            let delete = try db.prepare("DELETE FROM models WHERE id = ?")
            let deleteScore = try db.prepare("DELETE FROM model_last_scores WHERE model_id = ?")
            for id in stale {
                try delete.bind([.text(id)]).run()
                try deleteScore.bind([.text(id)]).run()
            }
            // An upsert, not REPLACE: replacing would null every alias
            // pointing at the model until the next rebuild.
            let upsert = try db.prepare("""
                INSERT INTO models (id, position, slug, name, creator_id, creator_name, creator_slug,
                    aa_price_blended, aa_price_input, aa_price_output, speed, ttft, ttfat,
                    evaluations, raw, fetch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET position = excluded.position, slug = excluded.slug,
                    name = excluded.name, creator_id = excluded.creator_id,
                    creator_name = excluded.creator_name, creator_slug = excluded.creator_slug,
                    aa_price_blended = excluded.aa_price_blended, aa_price_input = excluded.aa_price_input,
                    aa_price_output = excluded.aa_price_output, speed = excluded.speed,
                    ttft = excluded.ttft, ttfat = excluded.ttfat, evaluations = excluded.evaluations,
                    raw = excluded.raw, fetch_id = excluded.fetch_id
                """)
            for (position, item) in models.enumerated() {
                let m = item.model
                let evaluations = Self.canonicalJSON(m.evaluations) ?? "{}"
                try upsert.bind([
                    .text(m.id), .int(position), .text(m.slug), .text(m.name), .text(m.creatorID),
                    .text(m.creatorName), .text(m.creatorSlug), .optionalDouble(m.priceBlended),
                    .optionalDouble(m.priceInput), .optionalDouble(m.priceOutput), .optionalDouble(m.speed),
                    .optionalDouble(m.timeToFirstToken), .optionalDouble(m.timeToFirstAnswer),
                    .text(evaluations), .text(item.raw), .int(fetchID),
                ]).run()
            }
            // Keep every score this feed carries: the next index version
            // drops most of them, and the chart still wants the last one.
            try Self.keepScores(db, of: models.map(\.model), version: AAModelsResponse.indexVersion(body),
                                at: date, replacing: true)
        }
    }

    /// Writes each scored model's intelligence index into
    /// `model_last_scores`. `replacing` is the newest feed overwriting the
    /// row; off, a row already there wins (the backfill of older feeds).
    private static func keepScores(_ db: SQLiteDatabase, of models: [AALanguageModel], version: String?,
                                   at date: Date?, replacing: Bool) throws {
        let insert = try db.prepare("""
            INSERT OR \(replacing ? "REPLACE" : "IGNORE") INTO model_last_scores
                (model_id, intelligence, index_version, scored_at) VALUES (?, ?, ?, ?)
            """)
        for model in models {
            guard let score = model.intelligence else { continue }
            try insert.bind([.text(model.id), .double(score), version.map { .text($0) } ?? .null,
                             .optionalDouble(date?.timeIntervalSince1970)]).run()
        }
    }

    /// Fills `model_last_scores` for a catalog built before the table
    /// existed: first from the models already stored, then from the legacy
    /// `llms.json` an older build left in `legacyDirectory`, which holds
    /// the scores from before the current index version. A model already
    /// in the table keeps its row. Runs once.
    public func backfillLastScores(legacyDirectory: URL?) {
        let marker = "last_scores_backfill"
        let done = withDB { db in
            let statement = try db.prepare("SELECT 1 FROM meta WHERE key = ?")
            statement.bind([.text(marker)])
            return try statement.step()
        } ?? true
        guard !done else { return }
        let source = Self.aaSource(.llms)
        let current = languageModels() ?? []
        let version = payload(.llms).flatMap(AAModelsResponse.indexVersion)
        let fetchedAt = lastSuccess(source: source)?.fetchedAt
        var legacy: [AALanguageModel] = []
        var legacyDate: Date?
        if let legacyDirectory {
            let file = legacyDirectory.appending(path: AAEndpoint.llms.fileName)
            if let body = FileManager.default.contents(atPath: file.path),
               let models = try? AAModelsResponse.decode(body) {
                legacy = models
                let meta = FileManager.default.contents(atPath: legacyDirectory.appending(path: "meta.json").path)
                    .flatMap { try? JSONDecoder().decode([String: ArtificialAnalysisStore.Meta].self, from: $0) }
                legacyDate = meta?[AAEndpoint.llms.rawValue]?.fetchedAt.map(Date.init(timeIntervalSince1970:))
                    ?? (try? FileManager.default.attributesOfItem(atPath: file.path))?[.modificationDate] as? Date
            }
        }
        _ = try? transaction { db in
            try Self.keepScores(db, of: current, version: version, at: fetchedAt, replacing: false)
            // Only models the catalog still lists: `models` drops the rest.
            let listed = Set(current.map(\.id))
            try Self.keepScores(db, of: legacy.filter { listed.contains($0.id) }, version: nil,
                                at: legacyDate, replacing: false)
            try db.prepare("INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')")
                .bind([.text(marker)]).run()
        }
    }

    /// The feed's models with each one's raw JSON, in AA's order. Throws
    /// like `AAModelsResponse.decode` on anything but a `data` array.
    static func decodeModels(_ body: Data) throws -> [(model: AALanguageModel, raw: String)] {
        let models = try AAModelsResponse.decode(body)
        guard let root = try JSONSerialization.jsonObject(with: body) as? [String: Any],
              let rows = root["data"] as? [Any], rows.count == models.count
        else { throw AAError.badResponse }
        return try zip(models, rows).map { model, row in
            let raw = try JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])
            return (model, String(decoding: raw, as: UTF8.self))
        }
    }

    /// The last good body for an endpoint.
    func payload(_ endpoint: AAEndpoint) -> Data? {
        withDB { db in
            let statement = try db.prepare("SELECT body FROM payloads WHERE source = ?")
            statement.bind([.text(Self.aaSource(endpoint))])
            return try statement.step() ? statement.blob(0) ?? Data() : nil
        } ?? nil
    }

    /// AA's language models in AA's order; nil before the first good fetch.
    public func languageModels() -> [AALanguageModel]? {
        guard lastSuccess(source: Self.aaSource(.llms)) != nil else { return nil }
        return withDB { db in
            let statement = try db.prepare("""
                SELECT m.raw, s.intelligence, s.index_version, s.scored_at FROM models m
                LEFT JOIN model_last_scores s ON s.model_id = m.id ORDER BY m.position
                """)
            var result: [AALanguageModel] = []
            let decoder = JSONDecoder()
            while try statement.step() {
                guard let raw = statement.text(0),
                      var model = try? decoder.decode(AALanguageModel.self, from: Data(raw.utf8))
                else { continue }
                // A kept score stands in only where the feed has none.
                if model.intelligence == nil, !statement.isNull(1) {
                    model.pastIntelligence = AAPastScore(
                        value: statement.double(1), indexVersion: statement.text(2),
                        scoredAt: statement.isNull(3) ? nil : Date(timeIntervalSince1970: statement.double(3)))
                }
                result.append(model)
            }
            return result
        }
    }
}

// MARK: - Aliases

extension CatalogDatabase {
    /// How an alias row was decided.
    public enum AliasMethod: String, Sendable {
        /// The matcher's rules.
        case auto
        /// A manual link in `config/model-links.json`.
        case manual
        /// Nothing on AA matched.
        case none
    }

    /// The AA model a price key names, and how that was decided.
    public func alias(_ name: String) -> (modelID: String?, method: AliasMethod)? {
        withDB { db in
            let statement = try db.prepare("SELECT model_id, method FROM model_aliases WHERE name = ?")
            statement.bind([.text(name)])
            guard try statement.step() else { return nil }
            return (statement.text(0), AliasMethod(rawValue: statement.text(1) ?? "") ?? .none)
        } ?? nil
    }

    /// Re-decides which AA model every price key names, from the models in
    /// the table and the manual links file. Runs after either side changes.
    public func rebuildAliases() {
        rebuildLock.lock()
        defer { rebuildLock.unlock() }
        let links = linksFile.map(ModelMatcher.loadLinks(from:)) ?? [:]
        let index = ModelMatcher.Index(models: languageModels() ?? [], links: links)
        _ = try? transaction { db in
            var names: [String] = []
            let select = try db.prepare("SELECT name FROM model_aliases")
            while try select.step() { if let name = select.text(0) { names.append(name) } }
            let update = try db.prepare("UPDATE model_aliases SET model_id = ?, method = ? WHERE name = ?")
            for name in names {
                let match = index.match(name)
                let method: AliasMethod = match.map { $0.linked ? .manual : .auto } ?? .none
                try update.bind([match.map { .text($0.model.id) }, .text(method.rawValue), .text(name)]).run()
            }
        }
    }
}

// MARK: - One-time import

extension CatalogDatabase {
    /// Moves the price list the app kept in `cache/pricing/litellm.json`
    /// before this database existed, dated by the file's modification
    /// time. Runs once: any LiteLLM fetch already recorded skips it. The
    /// file is left where it is.
    public func importLegacyPricing(cacheFile: URL) {
        guard openError == nil, !hasFetch(sourcePrefix: Self.liteLLMSource),
              let data = FileManager.default.contents(atPath: cacheFile.path),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        let modified = (try? FileManager.default.attributesOfItem(atPath: cacheFile.path))?[.modificationDate]
            as? Date ?? Date()
        let entries = Self.pricedEntries(object)
        guard !entries.isEmpty,
              (try? recordPrices(entries, at: modified, url: "file://\(cacheFile.path)",
                                 sha: Self.sha256(data))) != nil
        else { return }
        rebuildAliases()
    }

    /// The priced chat entries of a LiteLLM list.
    static func pricedEntries(_ object: [String: Any]) -> [String: [String: Any]] {
        var entries: [String: [String: Any]] = [:]
        for (key, value) in object where key != "sample_spec" {
            guard let entry = value as? [String: Any], PricingCatalog.rates(from: entry) != nil else { continue }
            entries[key] = entry
        }
        return entries
    }

    /// Moves the AA files the app kept in `data/artificial-analysis/` (one
    /// raw response per endpoint plus `meta.json`). Runs once per endpoint:
    /// one with any fetch recorded is skipped, so an import cut short
    /// resumes where it stopped. The files are left where they are.
    public func importLegacyAA(directory: URL) {
        guard openError == nil else { return }
        let meta: [String: ArtificialAnalysisStore.Meta] = FileManager.default
            .contents(atPath: directory.appending(path: "meta.json").path)
            .flatMap { try? JSONDecoder().decode([String: ArtificialAnalysisStore.Meta].self, from: $0) } ?? [:]
        var imported = false
        defer { if imported { rebuildAliases() } }
        for endpoint in AAEndpoint.allCases where !hasFetch(sourcePrefix: Self.aaSource(endpoint)) {
            let entry = meta[endpoint.rawValue]
            if let body = FileManager.default.contents(atPath: directory.appending(path: endpoint.fileName).path) {
                if (try? recordAAPayload(
                    endpoint, body: body, at: entry?.fetchedAt.map(Date.init(timeIntervalSince1970:)),
                    status: entry?.status, rateLimitHeaders: entry?.rateLimitHeaders,
                    url: "file://\(directory.appending(path: endpoint.fileName).path)")) != nil {
                    imported = true
                }
            }
            if let error = entry?.error {
                recordFailure(source: Self.aaSource(endpoint), url: endpoint.url,
                              at: entry?.fetchedAt.map(Date.init(timeIntervalSince1970:)) ?? Date(),
                              status: entry?.status, error: error)
            }
        }
    }
}
