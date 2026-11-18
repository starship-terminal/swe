import CryptoKit
import Foundation

/// Whether one key still works, asked of its own provider with the
/// cheapest request that checks auth. Each probe is one HTTPS request to
/// the provider's API only, and the transport never follows redirects, so
/// a key can only reach a host the table lists.
public struct APIKeyProbe: Equatable, Sendable {
    public enum Auth: Sendable, Equatable {
        /// `Authorization: Bearer <key>`.
        case bearer
        /// `x-api-key: <key>` plus `anthropic-version`.
        case xApiKey
        /// `x-goog-api-key: <key>`.
        case googApiKey
        /// `Authorization: Token <key>` (Deepgram, Replicate).
        case token
        /// `<name>: <key>` — services that take the key under their own
        /// header (x-api-key, X-Browser-Use-API-Key).
        case header(String)
    }

    /// Hosts to try in order; the first that accepts the key wins.
    public let hosts: [URL]
    public let method: String
    public let body: Data?
    public let auth: Auth
    /// A provider that answers a working key with a request error:
    /// routing.run refuses the probe body (no model) before anything runs,
    /// so its 400 and 422 mean works.
    public let worksOnBadRequest: Bool

    public init(hosts: [URL], method: String, body: Data?, auth: Auth,
                worksOnBadRequest: Bool) {
        self.hosts = hosts
        self.method = method
        self.body = body
        self.auth = auth
        self.worksOnBadRequest = worksOnBadRequest
    }

    private static func get(_ auth: Auth, _ hosts: String...) -> APIKeyProbe {
        APIKeyProbe(hosts: hosts.map { URL(string: $0)! }, method: "GET", body: nil,
                    auth: auth, worksOnBadRequest: false)
    }

    /// The settled table, verified by sending a fake key to every host:
    /// each one rejects it as listed, so an accept means the key works.
    /// OpenRouter's /models is public, so the probe hits /key instead.
    static let byName: [String: APIKeyProbe] = {
        var table: [String: APIKeyProbe] = [:]
        func put(_ probe: APIKeyProbe, _ names: String...) {
            for name in names { table[name] = probe }
        }
        put(get(.bearer, "https://api.openai.com/v1/models"), "OPENAI_API_KEY")
        put(get(.xApiKey, "https://api.anthropic.com/v1/models"), "ANTHROPIC_API_KEY")
        put(get(.googApiKey, "https://generativelanguage.googleapis.com/v1beta/models"),
            "GEMINI_API_KEY", "GOOGLE_API_KEY")
        put(get(.bearer, "https://api.deepseek.com/models"), "DEEPSEEK_API_KEY")
        put(get(.bearer, "https://api.x.ai/v1/models"), "XAI_API_KEY")
        put(get(.bearer, "https://api.groq.com/openai/v1/models"), "GROQ_API_KEY")
        put(get(.bearer, "https://api.mistral.ai/v1/models"), "MISTRAL_API_KEY")
        put(get(.bearer, "https://api.cerebras.ai/v1/models"), "CEREBRAS_API_KEY")
        put(get(.bearer, "https://api.together.xyz/v1/models"),
            "TOGETHER_AI_API_KEY", "TOGETHER_API_KEY")
        put(get(.bearer, "https://api.fireworks.ai/inference/v1/models"),
            "FIREWORKS_AI_API_KEY", "FIREWORKS_API_KEY")
        put(get(.bearer, "https://openrouter.ai/api/v1/key"), "OPENROUTER_API_KEY")
        put(get(.bearer, "https://api.moonshot.ai/v1/models",
                "https://api.moonshot.cn/v1/models"),
            "KIMI_API_KEY", "MOONSHOT_API_KEY")
        put(get(.bearer, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
                "https://dashscope.aliyuncs.com/compatible-mode/v1/models"),
            "QWEN_API_KEY", "DASHSCOPE_API_KEY")
        put(get(.bearer, "https://api.z.ai/api/paas/v4/models",
                "https://open.bigmodel.cn/api/paas/v4/models"),
            "ZAI_API_KEY", "ZHIPU_API_KEY")
        put(get(.bearer, "https://api.minimax.io/v1/models",
                "https://api.minimaxi.com/v1/models"), "MINIMAX_API_KEY")
        put(get(.bearer, "https://api.xiaomimimo.com/v1/models"), "MIMO_API_KEY")
        put(get(.bearer, "https://api.stepfun.ai/v1/models",
                "https://api.stepfun.com/v1/models"), "STEPFUN_API_KEY")
        put(get(.bearer, "https://api.upstage.ai/v1/models"), "UPSTAGE_API_KEY")
        put(get(.bearer, "https://opencode.ai/zen/v1/models"), "OPENCODE_ZEN_API_KEY")
        put(get(.bearer, "https://ai-gateway.vercel.sh/v1/models"),
            "VERCEL_GATEWAY_API_KEY", "AI_GATEWAY_API_KEY")
        put(get(.bearer, "https://api.tokenfactory.nebius.com/v1/models",
                "https://api.studio.nebius.com/v1/models"), "NEBIUS_API_KEY")
        put(get(.bearer, "https://api.featherless.ai/v1/models"), "FEATHERLESS_API_KEY")
        put(APIKeyProbe(hosts: [URL(string: "https://api.routing.run/v1/chat/completions")!],
                        method: "POST", body: Data("{}".utf8), auth: .bearer,
                        worksOnBadRequest: true),
            "ROUTING_RUN_API_KEY")
        // The infra seeds, each verified against a fake key: a bare GET
        // on the service's cheapest auth-reading endpoint refuses it as
        // the table lists. Cloudflare's purpose-built tokens/verify
        // answers 400, so the probe reads /accounts instead, which
        // refuses a bad token with 403. Fly.io answers its Machines API
        // 404 without an app in the path, Railway's GraphQL answers a
        // bad key with a 200 body of errors, and Heroku needs a
        // versioned Accept header the table can't send: all three have
        // no check rather than a wrong one.
        put(get(.bearer, "https://api.vercel.com/v2/user"), "VERCEL_TOKEN")
        put(get(.bearer, "https://api.cloudflare.com/client/v4/accounts"), "CLOUDFLARE_API_TOKEN")
        put(get(.bearer, "https://api.netlify.com/api/v1/user"), "NETLIFY_AUTH_TOKEN")
        put(get(.bearer, "https://api.supabase.com/v1/projects"), "SUPABASE_ACCESS_TOKEN")
        put(get(.bearer, "https://console.neon.tech/api/v2/projects"), "NEON_API_KEY")
        put(get(.bearer, "https://api.turso.tech/v1/organizations"), "TURSO_API_TOKEN")
        put(get(.bearer, "https://api.render.com/v1/services?limit=1"), "RENDER_API_KEY")
        put(get(.bearer, "https://api.digitalocean.com/v2/account"),
            "DIGITALOCEAN_ACCESS_TOKEN", "DIGITALOCEAN_TOKEN")
        // The services, each verified against a fake key the same way:
        // a bare GET on the cheapest auth-reading endpoint refuses it
        // as the table lists. TinyFish and Browser Use take the key
        // under their own header. Parallel and Exa answer a GET with
        // auth middleware before routing, so a valid key earns a 404
        // after it passes: like Tavily they check on a POST, where a
        // real key's empty body is a request error that counts as
        // works and a fake still 401s.
        put(APIKeyProbe(hosts: [URL(string: "https://api.parallel.ai/v1/tasks/runs")!],
                        method: "POST", body: Data("{}".utf8),
                        auth: .header("x-api-key"), worksOnBadRequest: true),
            "PARALLEL_API_KEY")
        put(get(.bearer, "https://api.firecrawl.dev/v2/team/credit-usage"), "FIRECRAWL_API_KEY")
        put(get(.header("X-API-Key"), "https://agent.tinyfish.ai/v1/runs"), "TINYFISH_API_KEY")
        put(get(.header("X-Browser-Use-API-Key"), "https://api.browser-use.com/api/v3/sessions"),
            "BROWSER_USE_API_KEY")
        put(APIKeyProbe(hosts: [URL(string: "https://api.exa.ai/search")!],
                        method: "POST", body: Data("{}".utf8),
                        auth: .header("x-api-key"), worksOnBadRequest: true),
            "EXA_API_KEY")
        // The common agent services. Tavily only reads the key on a
        // POST, and a real key's empty body earns a request error that
        // counts as works; Supermemory's search body is a complete call
        // a real key answers 200. Both refuse a fake with 401.
        // AssemblyAI takes the bare key in `authorization`; Browserbase,
        // ElevenLabs, Pinecone, Cartesia and E2B each take their own
        // header; Deepgram and Replicate take `Token <key>`. Brave
        // answers a bad key with a 422, which reads as a request error
        // rather than a rejection, and Langfuse's API wants the
        // public/secret pair as basic auth: both have no check.
        put(APIKeyProbe(hosts: [URL(string: "https://api.tavily.com/search")!],
                        method: "POST", body: Data("{}".utf8), auth: .bearer,
                        worksOnBadRequest: true), "TAVILY_API_KEY")
        put(APIKeyProbe(hosts: [URL(string: "https://api.supermemory.ai/v4/search")!],
                        method: "POST", body: Data("{\"q\":\"ping\"}".utf8),
                        auth: .bearer, worksOnBadRequest: false), "SUPERMEMORY_API_KEY")
        put(get(.header("x-bb-api-key"), "https://api.browserbase.com/v1/projects"),
            "BROWSERBASE_API_KEY")
        put(get(.bearer, "https://api.apify.com/v2/acts?limit=1"),
            "APIFY_TOKEN", "APIFY_API_TOKEN")
        put(get(.header("X-API-KEY"), "https://api.e2b.dev/sandboxes"), "E2B_API_KEY")
        put(get(.bearer, "https://api.runloop.ai/v1/devboxes"), "RUNLOOP_API_KEY")
        put(get(.header("Api-Key"), "https://api.pinecone.io/indexes"), "PINECONE_API_KEY")
        put(get(.bearer, "https://api.braintrust.dev/v1/project?limit=1"), "BRAINTRUST_API_KEY")
        put(get(.header("xi-api-key"), "https://api.elevenlabs.io/v1/user"),
            "ELEVENLABS_API_KEY", "XI_API_KEY")
        put(get(.token, "https://api.deepgram.com/v1/projects"), "DEEPGRAM_API_KEY")
        put(get(.header("authorization"), "https://api.assemblyai.com/v2/transcript?limit=1"),
            "ASSEMBLYAI_API_KEY")
        put(get(.header("X-API-Key"), "https://api.cartesia.ai/voices"), "CARTESIA_API_KEY")
        put(get(.bearer, "https://api.stability.ai/v1/user/account"), "STABILITY_API_KEY")
        put(get(.token, "https://api.replicate.com/v1/models"), "REPLICATE_API_TOKEN")
        put(get(.bearer, "https://api.agentmail.to/v0/inboxes"), "AGENTMAIL_API_KEY")
        for plan in InferenceCatalog.all {
            if let probe = InferenceCatalog.probe(for: plan) { put(probe, plan.keyName) }
        }
        return table
    }()

    public static func probe(forName name: String) -> APIKeyProbe? { byName[name] }

    /// Why a key can't be checked: NVIDIA's API answers a free request
    /// without reading the key, or SWE doesn't know the provider.
    public static func noCheckReason(forName name: String) -> String {
        if let plan = InferenceCatalog.plan(forKey: name) {
            return "\(plan.name) reads the request before the key, so an empty request can't check it."
        }
        if name == "NVIDIA_API_KEY" {
            return "NVIDIA's API doesn't check the key on a free request."
        }
        switch name {
        case "FLY_API_TOKEN":
            return "Fly.io's API needs an app in the path, so a bare request can't check the key."
        case "RAILWAY_TOKEN":
            return "Railway's GraphQL API answers a bad key with a 200 body of errors, which can't tell the key apart."
        case "HEROKU_API_KEY":
            return "Heroku needs a versioned Accept header SWE doesn't send."
        case "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY":
            return "Brave's API answers a bad key with a 422, which can't tell the key apart."
        case "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY":
            return "Langfuse's API wants the public/secret pair as basic auth, so one key alone can't check."
        default:
            return "SWE doesn't know how to check this key."
        }
    }
}

/// What one check learned about a key.
public enum APIKeyCheckResult: Sendable, Equatable {
    /// The provider accepted the key.
    case works
    /// The key is valid but the account has no balance.
    case noCredit
    /// The provider refused the key.
    case rejected(status: Int, message: String?)
    /// The answer didn't prove anything about the key.
    case couldNotCheck(String)
    /// SWE has no check for this provider.
    case noCheck(String)
}

/// A check's answer, when it ran, and a fingerprint of what it ran
/// against. Persisted under ~/.swe/cache; the fingerprint is a hash
/// of the machine's salt then the value, so the value itself is never
/// written down.
public struct APIKeyCheck: Sendable, Equatable, Codable {
    public var result: APIKeyCheckResult
    public var checkedAt: Date
    /// SHA-256 of the salt followed by the value checked; an edit makes
    /// the result old.
    var fingerprint: String

    public init(result: APIKeyCheckResult, checkedAt: Date, value: String, salt: Data) {
        self.result = result
        self.checkedAt = checkedAt
        fingerprint = Self.fingerprint(of: value, salt: salt)
    }

    /// The result still describes this value.
    public func isCurrent(for value: String, salt: Data) -> Bool {
        fingerprint == Self.fingerprint(of: value, salt: salt)
    }

    static func fingerprint(of value: String, salt: Data) -> String {
        SHA256.hash(data: salt + Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

extension APIKeyCheckResult: Codable {
    private enum Kind: String, Codable {
        case works, noCredit, rejected, couldNotCheck, noCheck
    }

    private enum CodingKeys: String, CodingKey {
        case kind, status, message, reason
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .kind) {
        case .works:
            self = .works
        case .noCredit:
            self = .noCredit
        case .rejected:
            self = .rejected(status: try container.decode(Int.self, forKey: .status),
                             message: try container.decodeIfPresent(String.self, forKey: .message))
        case .couldNotCheck:
            self = .couldNotCheck(try container.decode(String.self, forKey: .reason))
        case .noCheck:
            self = .noCheck(try container.decode(String.self, forKey: .reason))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .works:
            try container.encode(Kind.works, forKey: .kind)
        case .noCredit:
            try container.encode(Kind.noCredit, forKey: .kind)
        case .rejected(let status, let message):
            try container.encode(Kind.rejected, forKey: .kind)
            try container.encode(status, forKey: .status)
            try container.encodeIfPresent(message, forKey: .message)
        case .couldNotCheck(let reason):
            try container.encode(Kind.couldNotCheck, forKey: .kind)
            try container.encode(reason, forKey: .reason)
        case .noCheck(let reason):
            try container.encode(Kind.noCheck, forKey: .kind)
            try container.encode(reason, forKey: .reason)
        }
    }
}

extension APIKeyCheckResult {
    /// The result in words, as the status label's tooltip says it.
    public var words: String {
        switch self {
        case .works: "Works"
        case .noCredit: "No credit on this account"
        case .rejected(let status, let message):
            "Rejected: HTTP \(status)\(message.map { ": \($0)" } ?? "")"
        case .couldNotCheck(let reason): "Couldn't check: \(reason)"
        case .noCheck(let reason): "No check: \(reason)"
        }
    }
}

/// How a check result paints a row's status label: works and no credit
/// turn it green, a rejection turns it red behind `xmark.circle`, and
/// anything else leaves it exactly as it was.
public enum APIKeyCheckStyle: Equatable, Sendable {
    /// Icon and label in the success colour.
    case success
    /// `xmark.circle` and label in the danger colour.
    case rejected
    /// The label looks as if no check ran.
    case unchanged

    public init(_ result: APIKeyCheckResult?) {
        switch result {
        case .works, .noCredit: self = .success
        case .rejected: self = .rejected
        case .couldNotCheck, .noCheck, nil: self = .unchanged
        }
    }
}

/// `~/.swe/cache/api-key-checks.json`, shaped `{version: 2, files:
/// {"<standardized keys-file path>": {checks: {NAME: {fingerprint,
/// result, checkedAt}}}}}`: each keys file owns one section, so models
/// over different files never clobber each other. A missing, damaged or
/// version 1 file reads as empty. Writes hold the shared file lock for a
/// read-modify-write of only their own section, then rename into place.
public struct APIKeyCheckStore: Sendable {
    private struct File: Codable {
        var version: Int
        var files: [String: Section]
    }

    private struct Section: Codable {
        var checks: [String: APIKeyCheck]
    }

    /// The 32-byte salt fingerprints are hashed against, made once per
    /// machine and kept in defaults, so a copied cache matches nothing.
    public static let saltKey = "apiKeyCheckSalt"

    public let url: URL

    public init(url: URL) {
        self.url = url
    }

    /// The salt, creating and storing it on first use.
    public static func salt(from defaults: UserDefaults) -> Data {
        if let salt = defaults.data(forKey: saltKey), salt.count == 32 {
            return salt
        }
        let salt = Data((0..<32).map { _ in UInt8.random(in: .min ... .max) })
        defaults.set(salt, forKey: saltKey)
        return salt
    }

    /// The section one keys file owns inside the shared cache.
    private static func sectionKey(for keysFile: URL) -> String {
        keysFile.standardizedFileURL.path
    }

    /// The stored checks for one keys file.
    public func load(for keysFile: URL) -> [String: APIKeyCheck] {
        guard let data = try? Data(contentsOf: url),
              let file = try? JSONDecoder().decode(File.self, from: data),
              file.version == 2
        else { return [:] }
        return file.files[Self.sectionKey(for: keysFile)]?.checks ?? [:]
    }

    /// Merge `checks` into the section `keysFile` owns, under the lock so
    /// a save can never overwrite another model's section or a fresher
    /// result in its own: a stored check newer than the incoming one
    /// stays. Stored names missing from `currentNames` are gone from the
    /// keys file and are dropped; names this save does not carry are
    /// left as they were.
    public func save(_ checks: [String: APIKeyCheck], for keysFile: URL,
                     currentNames: Set<String>) {
        let manager = FileManager.default
        try? manager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                                     attributes: [.posixPermissions: 0o700])
        guard let lock = try? FileLock(at: url.appendingPathExtension("lock")) else { return }
        defer { lock.release() }
        var file = ((try? Data(contentsOf: url))
            .flatMap { try? JSONDecoder().decode(File.self, from: $0) })
            .flatMap { $0.version == 2 ? $0 : nil }
            ?? File(version: 2, files: [:])
        let key = Self.sectionKey(for: keysFile)
        var section = file.files[key]?.checks ?? [:]
        for name in section.keys where !currentNames.contains(name) {
            section[name] = nil
        }
        for (name, check) in checks
        where (section[name]?.checkedAt ?? .distantPast) < check.checkedAt {
            section[name] = check
        }
        file.files[key] = Section(checks: section)
        guard let data = try? JSONEncoder().encode(file) else { return }
        let temporary = url.deletingLastPathComponent()
            .appending(path: ".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        guard manager.createFile(atPath: temporary.path, contents: data,
                                 attributes: [.posixPermissions: 0o600])
        else { return }
        if rename(temporary.path, url.path) != 0 {
            try? manager.removeItem(at: temporary)
        }
    }
}

extension APIKeyProbe {
    /// One answer, per the settled rules: 200 and 429 (rate limited, so
    /// the key was read) work; 402 works but has no credit; 401 and 403
    /// reject; a 400 whose body names the key rejects (Gemini's "API key
    /// not valid", xAI's "Incorrect API key"); routing.run's 400 or 422
    /// works; anything else couldn't be checked.
    static func classify(status: Int, body: Data, probe: APIKeyProbe,
                         key: String) -> APIKeyCheckResult {
        switch status {
        case 200, 429:
            return .works
        case 402:
            return .noCredit
        case 401, 403:
            return .rejected(status: status, message: errorMessage(in: body, key: key))
        case 400 where probe.worksOnBadRequest, 422 where probe.worksOnBadRequest:
            return .works
        case 400 where isKeyError(body):
            return .rejected(status: status, message: errorMessage(in: body, key: key))
        default:
            let detail = errorMessage(in: body, key: key)
            return .couldNotCheck("HTTP \(status)" + (detail.map { ": \($0)" } ?? ""))
        }
    }

    /// A 400 that names the key, the way Gemini says "API key not valid"
    /// and xAI says "Incorrect API key".
    private static func isKeyError(_ body: Data) -> Bool {
        let text = String(decoding: body, as: UTF8.self)
        return text.contains("API key not valid") || text.contains("Incorrect API key")
    }

    /// Hosts tried in order stop at the first accepted answer; the key is
    /// rejected only when every host rejected it, and any mix of
    /// rejections and couldn't-check is couldn't-check (a region host
    /// being down is not proof the key is dead).
    static func combine(_ outcomes: [APIKeyCheckResult]) -> APIKeyCheckResult {
        var rejected: APIKeyCheckResult?
        var failed: APIKeyCheckResult?
        for outcome in outcomes {
            switch outcome {
            case .works, .noCredit:
                return outcome
            case .rejected:
                rejected = rejected ?? outcome
            case .couldNotCheck:
                failed = failed ?? outcome
            case .noCheck:
                break
            }
        }
        if let failed { return failed }
        return rejected ?? .couldNotCheck("no answer")
    }

    /// The provider's error text from `error.message`, `message` or
    /// `detail`, with any echo of the key cut out and the rest cut to 120
    /// characters. A masked echo like `****0000` survives.
    static func errorMessage(in body: Data, key: String) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
        else { return nil }
        var candidates: [String?] = []
        if let error = object["error"] as? [String: Any] {
            candidates.append(error["message"] as? String)
        }
        candidates.append(object["message"] as? String)
        candidates.append(object["detail"] as? String)
        candidates.append(object["error"] as? String)
        for candidate in candidates {
            guard var text = candidate?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !text.isEmpty else { continue }
            if !key.isEmpty { text = text.replacingOccurrences(of: key, with: "…") }
            if text.count > 120 { text = String(text.prefix(120)) + "…" }
            return text
        }
        return nil
    }
}

/// A counting slot pool: `acquire` parks callers as continuations until
/// a `release` frees a slot. No blocking calls, so a stalled request
/// can't wedge the actor.
actor CheckSlots {
    private var free: Int
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(capacity: Int) { free = capacity }

    func acquire() async {
        if free > 0 {
            free -= 1
            return
        }
        await withCheckedContinuation { waiters.append($0) }
    }

    func release() {
        if waiters.isEmpty {
            free += 1
        } else {
            waiters.removeFirst().resume()
        }
    }
}

/// Sends each key only to its own provider's hosts, through the shared
/// transport so tests inject a fake.
public struct APIKeyChecker: Sendable {
    public var transport: HTTPTransport
    /// Per request.
    public var timeout: TimeInterval

    public init(transport: HTTPTransport, timeout: TimeInterval = 10) {
        self.transport = transport
        self.timeout = timeout
    }

    /// One request per host until one accepts the key, then the combined
    /// answer.
    public func check(key: String, probe: APIKeyProbe) async -> APIKeyCheckResult {
        var outcomes: [APIKeyCheckResult] = []
        for host in probe.hosts {
            let outcome = await request(host, key: key, probe: probe)
            outcomes.append(outcome)
            if case .works = outcome { break }
            if case .noCredit = outcome { break }
        }
        return APIKeyProbe.combine(outcomes)
    }

    private func request(_ host: URL, key: String, probe: APIKeyProbe) async -> APIKeyCheckResult {
        var request = URLRequest(url: host)
        request.httpMethod = probe.method
        request.httpBody = probe.body
        request.timeoutInterval = timeout
        if probe.body != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        switch probe.auth {
        case .bearer:
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        case .xApiKey:
            request.setValue(key, forHTTPHeaderField: "x-api-key")
            request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        case .googApiKey:
            request.setValue(key, forHTTPHeaderField: "x-goog-api-key")
        case .token:
            request.setValue("Token \(key)", forHTTPHeaderField: "Authorization")
        case .header(let name):
            request.setValue(key, forHTTPHeaderField: name)
        }
        do {
            let (body, response) = try await transport.send(request)
            return APIKeyProbe.classify(status: response.statusCode, body: body,
                                        probe: probe, key: key)
        } catch {
            var reason = error.localizedDescription
            if !key.isEmpty { reason = reason.replacingOccurrences(of: key, with: "…") }
            if reason.count > 120 { reason = String(reason.prefix(120)) + "…" }
            return .couldNotCheck(reason)
        }
    }
}
