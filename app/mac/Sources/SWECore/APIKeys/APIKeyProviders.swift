import Foundation

/// A provider the "Add key" sheets offer, mirrored from
/// `src/swe/providers/defaults.py` (`APIKeyProvidersSyncTests` fails
/// when the two drift apart). The slug is defaults.py's key, the env
/// vars its `env_vars` in the same order, and the key URL its `url`.
public struct APIKeyProvider: Equatable, Sendable, Identifiable {
    /// The slug from defaults.py, e.g. "together_ai".
    public let id: String
    /// The proper display name: "OpenAI", "Together AI", "xAI".
    public let name: String
    /// Same order as defaults.py; the first is the default variable.
    public let envVars: [String]
    /// defaults.py's "url": where the provider hands out keys.
    public let keyURL: URL

    public init(id: String, name: String, envVars: [String], keyURL: URL) {
        self.id = id
        self.name = name
        self.envVars = envVars
        self.keyURL = keyURL
    }

    public var logo: String? { APIKeyProviderLogo.logoID(for: name) }
    /// Whether SWE can test a key for this provider.
    public var canCheck: Bool {
        envVars.first.map { APIKeyProbe.probe(forName: $0) != nil } ?? false
    }
}

public enum APIKeyProviders {
    /// Every entry in defaults.py's DEFAULT_PROVIDERS, same order.
    public static let all: [APIKeyProvider] = [
        APIKeyProvider(id: "openai", name: "OpenAI",
                       envVars: ["OPENAI_API_KEY"],
                       keyURL: URL(string: "https://platform.openai.com/api-keys")!),
        APIKeyProvider(id: "anthropic", name: "Anthropic",
                       envVars: ["ANTHROPIC_API_KEY"],
                       keyURL: URL(string: "https://console.anthropic.com/settings/keys")!),
        APIKeyProvider(id: "gemini", name: "Google Gemini",
                       envVars: ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
                       keyURL: URL(string: "https://aistudio.google.com/app/apikey")!),
        APIKeyProvider(id: "deepseek", name: "DeepSeek",
                       envVars: ["DEEPSEEK_API_KEY"],
                       keyURL: URL(string: "https://platform.deepseek.com/api_keys")!),
        APIKeyProvider(id: "zai", name: "ZAI (01.ai)",
                       envVars: ["ZAI_API_KEY"],
                       keyURL: URL(string: "https://z.ai/")!),
        APIKeyProvider(id: "minimax", name: "MiniMax",
                       envVars: ["MINIMAX_API_KEY"],
                       keyURL: URL(string: "https://minimax.io/")!),
        APIKeyProvider(id: "kimi", name: "Kimi (Moonshot AI)",
                       envVars: ["KIMI_API_KEY", "MOONSHOT_API_KEY"],
                       keyURL: URL(string: "https://platform.moonshot.ai/")!),
        APIKeyProvider(id: "qwen", name: "Qwen (Alibaba)",
                       envVars: ["QWEN_API_KEY", "DASHSCOPE_API_KEY"],
                       keyURL: URL(string: "https://bailian.console.aliyun.com/")!),
        APIKeyProvider(id: "mimo", name: "MiMo (Xiaomi)",
                       envVars: ["MIMO_API_KEY"],
                       keyURL: URL(string: "https://platform.xiaomi.com/")!),
        APIKeyProvider(id: "xai", name: "xAI (Grok)",
                       envVars: ["XAI_API_KEY"],
                       keyURL: URL(string: "https://console.x.ai/")!),
        APIKeyProvider(id: "stepfun", name: "StepFun",
                       envVars: ["STEPFUN_API_KEY"],
                       keyURL: URL(string: "https://platform.stepfun.ai/")!),
        APIKeyProvider(id: "groq", name: "Groq",
                       envVars: ["GROQ_API_KEY"],
                       keyURL: URL(string: "https://console.groq.com/keys")!),
        APIKeyProvider(id: "upstage", name: "Upstage",
                       envVars: ["UPSTAGE_API_KEY"],
                       keyURL: URL(string: "https://console.upstage.ai/")!),
        APIKeyProvider(id: "cerebras", name: "Cerebras",
                       envVars: ["CEREBRAS_API_KEY"],
                       keyURL: URL(string: "https://cloud.cerebras.ai/")!),
        APIKeyProvider(id: "mistral", name: "Mistral",
                       envVars: ["MISTRAL_API_KEY"],
                       keyURL: URL(string: "https://console.mistral.ai/api-keys/")!),
        APIKeyProvider(id: "routing_run", name: "Routing.Run",
                       envVars: ["ROUTING_RUN_API_KEY"],
                       keyURL: URL(string: "https://routing.run/")!),
        APIKeyProvider(id: "opencode_zen", name: "Opencode Zen",
                       envVars: ["OPENCODE_ZEN_API_KEY"],
                       keyURL: URL(string: "https://opencode.ai/zen")!),
        APIKeyProvider(id: "openrouter", name: "OpenRouter",
                       envVars: ["OPENROUTER_API_KEY"],
                       keyURL: URL(string: "https://openrouter.ai/keys")!),
        APIKeyProvider(id: "together_ai", name: "Together AI",
                       envVars: ["TOGETHER_AI_API_KEY", "TOGETHER_API_KEY"],
                       keyURL: URL(string: "https://api.together.xyz/")!),
        APIKeyProvider(id: "fireworks_ai", name: "Fireworks AI",
                       envVars: ["FIREWORKS_AI_API_KEY"],
                       keyURL: URL(string: "https://fireworks.ai/")!),
        APIKeyProvider(id: "vercel_gateway", name: "Vercel Gateway",
                       envVars: ["VERCEL_GATEWAY_API_KEY"],
                       keyURL: URL(string: "https://vercel.com/docs/ai-gateway")!),
        APIKeyProvider(id: "nebius", name: "Nebius",
                       envVars: ["NEBIUS_API_KEY"],
                       keyURL: URL(string: "https://studio.nebius.com/")!),
        APIKeyProvider(id: "featherless", name: "Featherless AI",
                       envVars: ["FEATHERLESS_API_KEY"],
                       keyURL: URL(string: "https://featherless.ai/")!),
        APIKeyProvider(id: "cohere", name: "Cohere",
                       envVars: ["COHERE_API_KEY"],
                       keyURL: URL(string: "https://dashboard.cohere.com/api-keys")!),
        APIKeyProvider(id: "perplexity", name: "Perplexity",
                       envVars: ["PERPLEXITY_API_KEY"],
                       keyURL: URL(string: "https://www.perplexity.ai/settings/api")!),
        APIKeyProvider(id: "github", name: "GitHub",
                       envVars: ["GITHUB_TOKEN", "GH_TOKEN"],
                       keyURL: URL(string: "https://github.com/settings/tokens")!),
        APIKeyProvider(id: "huggingface", name: "Hugging Face",
                       envVars: ["HUGGINGFACE_HUB_TOKEN", "HF_TOKEN"],
                       keyURL: URL(string: "https://huggingface.co/settings/tokens")!),
    ]

    /// Every entry in defaults.py's DEFAULT_INFRA, same order: the keys
    /// that deploy and manage resources rather than run inference, shown
    /// on the Providers page's Infra tab instead of Inference.
    public static let infra: [APIKeyProvider] = [
        APIKeyProvider(id: "vercel", name: "Vercel",
                       envVars: ["VERCEL_TOKEN"],
                       keyURL: URL(string: "https://vercel.com/account/tokens")!),
        APIKeyProvider(id: "netlify", name: "Netlify",
                       envVars: ["NETLIFY_AUTH_TOKEN"],
                       keyURL: URL(string: "https://app.netlify.com/user/applications#personal-access-tokens")!),
        APIKeyProvider(id: "cloudflare", name: "Cloudflare",
                       envVars: ["CLOUDFLARE_API_TOKEN"],
                       keyURL: URL(string: "https://dash.cloudflare.com/profile/api-tokens")!),
        APIKeyProvider(id: "supabase", name: "Supabase",
                       envVars: ["SUPABASE_ACCESS_TOKEN"],
                       keyURL: URL(string: "https://supabase.com/dashboard/account/tokens")!),
        APIKeyProvider(id: "neon", name: "Neon",
                       envVars: ["NEON_API_KEY"],
                       keyURL: URL(string: "https://console.neon.tech/app/settings/api-keys")!),
        APIKeyProvider(id: "turso", name: "Turso",
                       envVars: ["TURSO_API_TOKEN"],
                       keyURL: URL(string: "https://app.turso.tech")!),
        APIKeyProvider(id: "railway", name: "Railway",
                       envVars: ["RAILWAY_TOKEN"],
                       keyURL: URL(string: "https://railway.com/account/tokens")!),
        APIKeyProvider(id: "fly", name: "Fly.io",
                       envVars: ["FLY_API_TOKEN"],
                       keyURL: URL(string: "https://fly.io/user/personal_access_tokens")!),
        APIKeyProvider(id: "render", name: "Render",
                       envVars: ["RENDER_API_KEY"],
                       keyURL: URL(string: "https://dashboard.render.com/u/settings#api-keys")!),
        APIKeyProvider(id: "digitalocean", name: "DigitalOcean",
                       envVars: ["DIGITALOCEAN_ACCESS_TOKEN", "DIGITALOCEAN_TOKEN"],
                       keyURL: URL(string: "https://cloud.digitalocean.com/account/api/tokens")!),
        APIKeyProvider(id: "heroku", name: "Heroku",
                       envVars: ["HEROKU_API_KEY"],
                       keyURL: URL(string: "https://dashboard.heroku.com/account")!),
    ]

    /// Every entry in defaults.py's DEFAULT_SERVICES, same order: APIs an
    /// agent calls rather than deploys onto — search, scraping, research
    /// and browser services — shown on the Providers page's Services tab.
    public static let services: [APIKeyProvider] = [
        APIKeyProvider(id: "parallel", name: "Parallel",
                       envVars: ["PARALLEL_API_KEY"],
                       keyURL: URL(string: "https://platform.parallel.ai/settings?tab=api-keys")!),
        APIKeyProvider(id: "firecrawl", name: "Firecrawl",
                       envVars: ["FIRECRAWL_API_KEY"],
                       keyURL: URL(string: "https://www.firecrawl.dev/app/api-keys")!),
        APIKeyProvider(id: "tinyfish", name: "TinyFish",
                       envVars: ["TINYFISH_API_KEY"],
                       keyURL: URL(string: "https://agent.tinyfish.ai/api-keys")!),
        APIKeyProvider(id: "browser_use", name: "Browser Use",
                       envVars: ["BROWSER_USE_API_KEY"],
                       keyURL: URL(string: "https://cloud.browser-use.com/settings?tab=api-keys")!),
        APIKeyProvider(id: "exa", name: "Exa",
                       envVars: ["EXA_API_KEY"],
                       keyURL: URL(string: "https://dashboard.exa.ai/api-keys")!),
        APIKeyProvider(id: "tavily", name: "Tavily",
                       envVars: ["TAVILY_API_KEY"],
                       keyURL: URL(string: "https://app.tavily.com/home")!),
        APIKeyProvider(id: "brave", name: "Brave Search",
                       envVars: ["BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"],
                       keyURL: URL(string: "https://api.search.brave.com/app/keys")!),
        APIKeyProvider(id: "browserbase", name: "Browserbase",
                       envVars: ["BROWSERBASE_API_KEY"],
                       keyURL: URL(string: "https://www.browserbase.com/settings")!),
        APIKeyProvider(id: "apify", name: "Apify",
                       envVars: ["APIFY_TOKEN", "APIFY_API_TOKEN"],
                       keyURL: URL(string: "https://console.apify.com/settings/integrations")!),
        APIKeyProvider(id: "e2b", name: "E2B",
                       envVars: ["E2B_API_KEY"],
                       keyURL: URL(string: "https://e2b.dev/dashboard")!),
        APIKeyProvider(id: "runloop", name: "Runloop",
                       envVars: ["RUNLOOP_API_KEY"],
                       keyURL: URL(string: "https://platform.runloop.ai")!),
        APIKeyProvider(id: "supermemory", name: "Supermemory",
                       envVars: ["SUPERMEMORY_API_KEY"],
                       keyURL: URL(string: "https://console.supermemory.ai")!),
        APIKeyProvider(id: "pinecone", name: "Pinecone",
                       envVars: ["PINECONE_API_KEY"],
                       keyURL: URL(string: "https://app.pinecone.io")!),
        APIKeyProvider(id: "langfuse", name: "Langfuse",
                       envVars: ["LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"],
                       keyURL: URL(string: "https://cloud.langfuse.com")!),
        APIKeyProvider(id: "braintrust", name: "Braintrust",
                       envVars: ["BRAINTRUST_API_KEY"],
                       keyURL: URL(string: "https://www.braintrust.dev/app")!),
        APIKeyProvider(id: "elevenlabs", name: "ElevenLabs",
                       envVars: ["ELEVENLABS_API_KEY", "XI_API_KEY"],
                       keyURL: URL(string: "https://elevenlabs.io/app/settings/api-keys")!),
        APIKeyProvider(id: "deepgram", name: "Deepgram",
                       envVars: ["DEEPGRAM_API_KEY"],
                       keyURL: URL(string: "https://console.deepgram.com")!),
        APIKeyProvider(id: "assemblyai", name: "AssemblyAI",
                       envVars: ["ASSEMBLYAI_API_KEY"],
                       keyURL: URL(string: "https://www.assemblyai.com/dashboard")!),
        APIKeyProvider(id: "cartesia", name: "Cartesia",
                       envVars: ["CARTESIA_API_KEY"],
                       keyURL: URL(string: "https://play.cartesia.ai")!),
        APIKeyProvider(id: "stability", name: "Stability AI",
                       envVars: ["STABILITY_API_KEY"],
                       keyURL: URL(string: "https://platform.stability.ai/account/keys")!),
        APIKeyProvider(id: "replicate", name: "Replicate",
                       envVars: ["REPLICATE_API_TOKEN"],
                       keyURL: URL(string: "https://replicate.com/account/api-tokens")!),
        APIKeyProvider(id: "agentmail", name: "AgentMail",
                       envVars: ["AGENTMAIL_API_KEY"],
                       keyURL: URL(string: "https://console.agentmail.to")!),
    ]

    /// The inference provider one of whose variables is `name`, if any.
    public static func provider(forVariable name: String) -> APIKeyProvider? {
        all.first { $0.envVars.contains(name) }
    }

    /// The infra provider one of whose variables is `name`, if any.
    public static func infraProvider(forVariable name: String) -> APIKeyProvider? {
        infra.first { $0.envVars.contains(name) }
    }

    /// An inference provider from the picker's id, nil for "other" or
    /// none picked.
    public static func provider(forID id: String) -> APIKeyProvider? {
        all.first { $0.id == id }
    }

    /// An infra provider from the picker's id, nil for "custom" or none
    /// picked.
    public static func infraProvider(forID id: String) -> APIKeyProvider? {
        infra.first { $0.id == id }
    }

    /// A services provider from the picker's id, nil for "custom" or
    /// none picked.
    public static func servicesProvider(forID id: String) -> APIKeyProvider? {
        services.first { $0.id == id }
    }

    /// The heading a new key for `provider` goes under: an existing group
    /// in `rows` that already holds one of its variables, else an existing
    /// group whose heading reads as the name or slug ignoring case and
    /// spaces/underscores (so "# ANTHROPIC" and "# Together AI" match),
    /// else `provider.name`.
    public static func heading(for provider: APIKeyProvider, in rows: [APIKeysModel.Row]) -> String {
        if let row = rows.first(where: { provider.envVars.contains($0.name) }) {
            return row.provider
        }
        var groups: [String] = []
        for row in rows where !groups.contains(row.provider) {
            groups.append(row.provider)
        }
        let wanted = Set([provider.name, provider.id].map(normalize))
        if let match = groups.first(where: { wanted.contains(normalize($0)) }) {
            return match
        }
        return provider.name
    }

    /// "Together AI", "together_ai" and "TOGETHER AI" all fold to the same key.
    private static func normalize(_ text: String) -> String {
        text.lowercased().filter { $0 != " " && $0 != "_" }
    }
}
