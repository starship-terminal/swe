"""Built-in default provider registry (seeded on first run).

Each entry stores *metadata only* — the filename to look for inside
the user's keys directory, the canonical slug, the dashboard URL,
and any env-var names that downstream tools use. No key strings live here.

The set of built-ins is curated against a real-world ``~/.api_keys``
export file (shell-style: ``export OPENAI_API_KEY=sk-...``). Providers
not in your personal file can still be added at runtime with
``swe providers add``.

Display ``name`` and ``alias`` fields are no longer persisted — they
are *derived* from ``env_vars[0]`` at load time (see ``display_name``
and ``derived_alias``) so the catalog has a single source of truth:
the env-var name itself. Manual aliases are not supported.
"""

# Known env-var suffixes, ordered longest-first so the compounds
# (``_ACCESS_TOKEN``, ``_AUTH_TOKEN``, ``_HUB_TOKEN``, ``_API_TOKEN``)
# are stripped before the ``_TOKEN`` they end with can match.
_STRIPPABLE_SUFFIXES = (
    "_ACCESS_TOKEN", "_AUTH_TOKEN", "_HUB_TOKEN", "_API_TOKEN",
    "_API_KEY", "_TOKEN",
)


def _strip_suffix(env_var: str) -> str:
    """Strip a known env-var suffix (``_API_KEY``, ``_HUB_TOKEN``, ``_TOKEN``)."""
    for suffix in _STRIPPABLE_SUFFIXES:
        if env_var.endswith(suffix):
            return env_var[: -len(suffix)]
    return env_var


def display_name(info: dict) -> str:
    """Derive the human-readable name from ``env_vars[0]``.

    Strips a known suffix, replaces underscores with spaces, title-cases
    each word, and preserves the canonical ``AI`` capitalization
    (``TOGETHER_AI`` → ``Together AI``, not ``Together Ai``).
    """
    env_vars = info.get("env_vars") or []
    if not env_vars:
        return ""
    stripped = _strip_suffix(env_vars[0])
    words = stripped.replace("_", " ").title().split(" ")
    # Whole words only: a substring replace turned ``MY_AIRTABLE`` into
    # "My AIrtable".
    return " ".join("AI" if word == "Ai" else word for word in words)


def derived_alias(info: dict) -> str | None:
    """Derive the auto-alias: env-var prefix lowercased, ``_`` → ``-``.

    Returns ``None`` when no env-var is configured (the provider would
    then be matched only by its canonical slug).
    """
    env_vars = info.get("env_vars") or []
    if not env_vars:
        return None
    stripped = _strip_suffix(env_vars[0])
    if not stripped:
        return None
    return stripped.lower().replace("_", "-")


DEFAULT_PROVIDERS = {
    "openai": {
        "description": "OpenAI API (ChatGPT, GPT-4/5, DALL-E)",
        "url": "https://platform.openai.com/api-keys",
        "key_filename": "openai",
        "env_vars": ["OPENAI_API_KEY"],
    },
    "anthropic": {
        "description": "Anthropic Claude API",
        "url": "https://console.anthropic.com/settings/keys",
        "key_filename": "anthropic",
        "env_vars": ["ANTHROPIC_API_KEY"],
    },
    "gemini": {
        "description": "Google Gemini / AI Studio",
        "url": "https://aistudio.google.com/app/apikey",
        "key_filename": "gemini",
        "env_vars": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    },
    "deepseek": {
        "description": "DeepSeek API",
        "url": "https://platform.deepseek.com/api_keys",
        "key_filename": "deepseek",
        "env_vars": ["DEEPSEEK_API_KEY"],
    },
    "zai": {
        "description": "ZAI / 01.ai inference",
        "url": "https://z.ai/",
        "key_filename": "zai",
        "env_vars": ["ZAI_API_KEY"],
    },
    "minimax": {
        "description": "MiniMax inference",
        "url": "https://minimax.io/",
        "key_filename": "minimax",
        "env_vars": ["MINIMAX_API_KEY"],
    },
    "kimi": {
        "description": "Moonshot AI Kimi API",
        "url": "https://platform.moonshot.ai/",
        "key_filename": "kimi",
        # KIMI_API_KEY is the newer Kimi-branded env var; MOONSHOT_API_KEY
        # is the canonical Moonshot SDK env var. Some users have only one.
        "env_vars": ["KIMI_API_KEY", "MOONSHOT_API_KEY"],
    },
    "qwen": {
        "description": "Alibaba Qwen / Bailian / DashScope",
        "url": "https://bailian.console.aliyun.com/",
        "key_filename": "qwen",
        # DASHSCOPE_API_KEY is the Alibaba DashScope SDK env var;
        # QWEN_API_KEY is recognized in some custom wrappers.
        "env_vars": ["QWEN_API_KEY", "DASHSCOPE_API_KEY"],
    },
    "mimo": {
        "description": "Xiaomi MiMo model API",
        "url": "https://platform.xiaomi.com/",
        "key_filename": "mimo",
        "env_vars": ["MIMO_API_KEY"],
    },
    "xai": {
        "description": "xAI (Grok)",
        "url": "https://console.x.ai/",
        "key_filename": "xai",
        "env_vars": ["XAI_API_KEY"],
    },
    "stepfun": {
        "description": "StepFun inference API",
        "url": "https://platform.stepfun.ai/",
        "key_filename": "stepfun",
        "env_vars": ["STEPFUN_API_KEY"],
    },
    "groq": {
        "description": "Groq fast-inference API",
        "url": "https://console.groq.com/keys",
        "key_filename": "groq",
        "env_vars": ["GROQ_API_KEY"],
    },
    "upstage": {
        "description": "Upstage Solar API",
        "url": "https://console.upstage.ai/",
        "key_filename": "upstage",
        "env_vars": ["UPSTAGE_API_KEY"],
    },
    "cerebras": {
        "description": "Cerebras inference cloud",
        "url": "https://cloud.cerebras.ai/",
        "key_filename": "cerebras",
        "env_vars": ["CEREBRAS_API_KEY"],
    },
    "mistral": {
        "description": "Mistral inference API",
        "url": "https://console.mistral.ai/api-keys/",
        "key_filename": "mistral",
        "env_vars": ["MISTRAL_API_KEY"],
    },
    "routing_run": {
        "description": "Routing.Run",
        "url": "https://routing.run/",
        "key_filename": "routing_run",
        "env_vars": ["ROUTING_RUN_API_KEY"],
    },
    "opencode_zen": {
        "description": "Opencode Zen inference",
        "url": "https://opencode.ai/zen",
        "key_filename": "opencode_zen",
        "env_vars": ["OPENCODE_ZEN_API_KEY"],
    },
    "openrouter": {
        "description": "Unified router across many LLM providers",
        "url": "https://openrouter.ai/keys",
        "key_filename": "openrouter",
        "env_vars": ["OPENROUTER_API_KEY"],
    },
    "together_ai": {
        "description": "Together AI inference API",
        "url": "https://api.together.xyz/",
        "key_filename": "together",
        # Both spellings appear in userland: TOGETHER_AI_API_KEY (newer)
        # and TOGETHER_API_KEY (older SDKs).
        "env_vars": ["TOGETHER_AI_API_KEY", "TOGETHER_API_KEY"],
    },
    "fireworks_ai": {
        "description": "Fireworks AI inference cloud",
        "url": "https://fireworks.ai/",
        "key_filename": "fireworks",
        "env_vars": ["FIREWORKS_AI_API_KEY"],
    },
    "vercel_gateway": {
        "description": "Vercel AI Gateway",
        "url": "https://vercel.com/docs/ai-gateway",
        "key_filename": "vercel_gateway",
        "env_vars": ["VERCEL_GATEWAY_API_KEY"],
    },
    "nebius": {
        "description": "Nebius inference studio",
        "url": "https://studio.nebius.com/",
        "key_filename": "nebius",
        "env_vars": ["NEBIUS_API_KEY"],
    },
    "featherless": {
        "description": "Featherless AI inference",
        "url": "https://featherless.ai/",
        "key_filename": "featherless",
        "env_vars": ["FEATHERLESS_API_KEY"],
    },
    "cohere": {
        "description": "Cohere API",
        "url": "https://dashboard.cohere.com/api-keys",
        "key_filename": "cohere",
        "env_vars": ["COHERE_API_KEY"],
    },
    "perplexity": {
        "description": "Perplexity search API",
        "url": "https://www.perplexity.ai/settings/api",
        "key_filename": "perplexity",
        "env_vars": ["PERPLEXITY_API_KEY"],
    },
    "github": {
        "description": "GitHub personal access tokens",
        "url": "https://github.com/settings/tokens",
        "key_filename": "github",
        "env_vars": ["GITHUB_TOKEN", "GH_TOKEN"],
    },
    "huggingface": {
        "description": "Hugging Face Hub tokens",
        "url": "https://huggingface.co/settings/tokens",
        "key_filename": "huggingface",
        # HUGGINGFACE_HUB_TOKEN first so the derived display name reads
        # "Huggingface" (not the awkward "Hf" form of HF_TOKEN).
        "env_vars": ["HUGGINGFACE_HUB_TOKEN", "HF_TOKEN"],
    },
}

# Providers a key deploys and manages resources with rather than runs
# inference on: Vercel, Netlify, Cloudflare and friends. Same shape as
# DEFAULT_PROVIDERS plus ``kind`` (the field that files them under the
# providers page's Infra tab instead of Inference). ``swe providers``
# merges both maps into providers.json.
DEFAULT_INFRA = {
    "vercel": {
        "kind": "infra",
        "description": "Vercel deployments and projects",
        "url": "https://vercel.com/account/tokens",
        "key_filename": "vercel",
        "env_vars": ["VERCEL_TOKEN"],
    },
    "netlify": {
        "kind": "infra",
        "description": "Netlify deploys and sites",
        "url": "https://app.netlify.com/user/applications#personal-access-tokens",
        "key_filename": "netlify",
        "env_vars": ["NETLIFY_AUTH_TOKEN"],
    },
    "cloudflare": {
        "kind": "infra",
        "description": "Cloudflare API (Workers, DNS)",
        "url": "https://dash.cloudflare.com/profile/api-tokens",
        "key_filename": "cloudflare",
        "env_vars": ["CLOUDFLARE_API_TOKEN"],
    },
    "supabase": {
        "kind": "infra",
        "description": "Supabase management API",
        "url": "https://supabase.com/dashboard/account/tokens",
        "key_filename": "supabase",
        "env_vars": ["SUPABASE_ACCESS_TOKEN"],
    },
    "neon": {
        "kind": "infra",
        "description": "Neon serverless Postgres",
        "url": "https://console.neon.tech/app/settings/api-keys",
        "key_filename": "neon",
        "env_vars": ["NEON_API_KEY"],
    },
    "turso": {
        "kind": "infra",
        "description": "Turso / libSQL platform API",
        "url": "https://app.turso.tech",
        "key_filename": "turso",
        "env_vars": ["TURSO_API_TOKEN"],
    },
    "railway": {
        "kind": "infra",
        "description": "Railway deployments",
        "url": "https://railway.com/account/tokens",
        "key_filename": "railway",
        "env_vars": ["RAILWAY_TOKEN"],
    },
    "fly": {
        "kind": "infra",
        "description": "Fly.io apps and machines",
        "url": "https://fly.io/user/personal_access_tokens",
        "key_filename": "fly",
        "env_vars": ["FLY_API_TOKEN"],
    },
    "render": {
        "kind": "infra",
        "description": "Render services and deploys",
        "url": "https://dashboard.render.com/u/settings#api-keys",
        "key_filename": "render",
        "env_vars": ["RENDER_API_KEY"],
    },
    "digitalocean": {
        "kind": "infra",
        "description": "DigitalOcean API (doctl, droplets)",
        "url": "https://cloud.digitalocean.com/account/api/tokens",
        "key_filename": "digitalocean",
        # DIGITALOCEAN_ACCESS_TOKEN is doctl's variable; DIGITALOCEAN_TOKEN
        # is what the Terraform provider and older docs use.
        "env_vars": ["DIGITALOCEAN_ACCESS_TOKEN", "DIGITALOCEAN_TOKEN"],
    },
    "heroku": {
        "kind": "infra",
        "description": "Heroku apps and releases",
        "url": "https://dashboard.heroku.com/account",
        "key_filename": "heroku",
        "env_vars": ["HEROKU_API_KEY"],
    },
}

# Keys for APIs an agent calls rather than deploys onto: search, scraping,
# research and browser services such as Firecrawl or Exa. Same shape as
# DEFAULT_INFRA; ``kind`` is ``services`` and they file under the providers
# page's Services tab.
DEFAULT_SERVICES = {
    "parallel": {
        "kind": "services",
        "description": "Parallel web data API (Search, Task, Extract)",
        "url": "https://platform.parallel.ai/settings?tab=api-keys",
        "key_filename": "parallel",
        "env_vars": ["PARALLEL_API_KEY"],
    },
    "firecrawl": {
        "kind": "services",
        "description": "Firecrawl web scraping API",
        "url": "https://www.firecrawl.dev/app/api-keys",
        "key_filename": "firecrawl",
        "env_vars": ["FIRECRAWL_API_KEY"],
    },
    "tinyfish": {
        "kind": "services",
        "description": "TinyFish web-agent browser API",
        "url": "https://agent.tinyfish.ai/api-keys",
        "key_filename": "tinyfish",
        "env_vars": ["TINYFISH_API_KEY"],
    },
    "browser_use": {
        "kind": "services",
        "description": "Browser Use cloud browser API",
        "url": "https://cloud.browser-use.com/settings?tab=api-keys",
        "key_filename": "browser_use",
        "env_vars": ["BROWSER_USE_API_KEY"],
    },
    "exa": {
        "kind": "services",
        "description": "Exa AI search API",
        "url": "https://dashboard.exa.ai/api-keys",
        "key_filename": "exa",
        "env_vars": ["EXA_API_KEY"],
    },
    "tavily": {
        "kind": "services",
        "description": "Tavily web search API",
        "url": "https://app.tavily.com/home",
        "key_filename": "tavily",
        "env_vars": ["TAVILY_API_KEY"],
    },
    "brave": {
        "kind": "services",
        "description": "Brave Search API",
        "url": "https://api.search.brave.com/app/keys",
        "key_filename": "brave",
        "env_vars": ["BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"],
    },
    "browserbase": {
        "kind": "services",
        "description": "Browserbase hosted browser API",
        "url": "https://www.browserbase.com/settings",
        "key_filename": "browserbase",
        "env_vars": ["BROWSERBASE_API_KEY"],
    },
    "apify": {
        "kind": "services",
        "description": "Apify scraping actors API",
        "url": "https://console.apify.com/settings/integrations",
        "key_filename": "apify",
        "env_vars": ["APIFY_TOKEN", "APIFY_API_TOKEN"],
    },
    "e2b": {
        "kind": "services",
        "description": "E2B code sandbox API",
        "url": "https://e2b.dev/dashboard",
        "key_filename": "e2b",
        "env_vars": ["E2B_API_KEY"],
    },
    "runloop": {
        "kind": "services",
        "description": "Runloop dev-environment API",
        "url": "https://platform.runloop.ai",
        "key_filename": "runloop",
        "env_vars": ["RUNLOOP_API_KEY"],
    },
    "supermemory": {
        "kind": "services",
        "description": "Supermemory agent memory API",
        "url": "https://console.supermemory.ai",
        "key_filename": "supermemory",
        "env_vars": ["SUPERMEMORY_API_KEY"],
    },
    "pinecone": {
        "kind": "services",
        "description": "Pinecone vector database API",
        "url": "https://app.pinecone.io",
        "key_filename": "pinecone",
        "env_vars": ["PINECONE_API_KEY"],
    },
    "langfuse": {
        "kind": "services",
        "description": "Langfuse LLM observability API",
        "url": "https://cloud.langfuse.com",
        "key_filename": "langfuse",
        "env_vars": ["LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"],
    },
    "braintrust": {
        "kind": "services",
        "description": "Braintrust evals and observability API",
        "url": "https://www.braintrust.dev/app",
        "key_filename": "braintrust",
        "env_vars": ["BRAINTRUST_API_KEY"],
    },
    "elevenlabs": {
        "kind": "services",
        "description": "ElevenLabs text-to-speech API",
        "url": "https://elevenlabs.io/app/settings/api-keys",
        "key_filename": "elevenlabs",
        "env_vars": ["ELEVENLABS_API_KEY", "XI_API_KEY"],
    },
    "deepgram": {
        "kind": "services",
        "description": "Deepgram speech-to-text API",
        "url": "https://console.deepgram.com",
        "key_filename": "deepgram",
        "env_vars": ["DEEPGRAM_API_KEY"],
    },
    "assemblyai": {
        "kind": "services",
        "description": "AssemblyAI speech-to-text API",
        "url": "https://www.assemblyai.com/dashboard",
        "key_filename": "assemblyai",
        "env_vars": ["ASSEMBLYAI_API_KEY"],
    },
    "cartesia": {
        "kind": "services",
        "description": "Cartesia text-to-speech API",
        "url": "https://play.cartesia.ai",
        "key_filename": "cartesia",
        "env_vars": ["CARTESIA_API_KEY"],
    },
    "stability": {
        "kind": "services",
        "description": "Stability AI image generation API",
        "url": "https://platform.stability.ai/account/keys",
        "key_filename": "stability",
        "env_vars": ["STABILITY_API_KEY"],
    },
    "replicate": {
        "kind": "services",
        "description": "Replicate model hosting API",
        "url": "https://replicate.com/account/api-tokens",
        "key_filename": "replicate",
        "env_vars": ["REPLICATE_API_TOKEN"],
    },
    "agentmail": {
        "kind": "services",
        "description": "AgentMail inboxes and email API",
        "url": "https://console.agentmail.to",
        "key_filename": "agentmail",
        "env_vars": ["AGENTMAIL_API_KEY"],
    },
}

# Every built-in, inference, infra and services: what the registry seed,
# the default merge and ``providers add``'s re-seed lookup all read.
DEFAULT_ALL = {**DEFAULT_PROVIDERS, **DEFAULT_INFRA, **DEFAULT_SERVICES}
