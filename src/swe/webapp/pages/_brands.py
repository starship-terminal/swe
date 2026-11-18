"""Model brands for the Pricing and Models pages (ports ``ModelBrands.swift``).

One table turns a logged model name into the company behind it: a display
name, a chart colour and a logo. Keep it in step with the Swift table, row
for row. The leading underscore keeps it from being served as a page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from swe.ledger.pricing import SUPPLEMENT, alias_target
from swe.webapp.static import STATIC_DIR

# (id, name, colour or None, icon or None, keywords, model-name patterns)
# Patterns match the start of a logged name, lower-cased, after any
# ``provider/`` prefix. A brand with no colour charts in "Other" grey.
BRANDS: list[tuple[str, str, str | None, str | None, tuple[str, ...], tuple[str, ...]]] = [
    # Model labs. Anthropic takes the app's Claude harness colour.
    ("anthropic", "Anthropic", "#d97757", "claude", ("anthropic", "claude"), ("claude",)),
    ("openai", "OpenAI", "#c9c9cf", "openai", ("openai",), ("gpt-", "chatgpt", "codex", r"o\d")),
    ("google", "Google", "#3fa35b", "gemini", ("google", "gemini"), ("gemini", "gemma")),
    # Grok Bot is Cursor's bot, not xAI's API, so `grok` skips it.
    ("xai", "XAI", "#8a73e8", "grok", ("xai", "x-ai", "grok"), ("grok(?!-bot)",)),
    ("grok-bot", "Grok Bot", "#8a73e8", "grok", ("grok-bot",), ("grok-bot",)),
    ("meta", "Meta", "#2f7bf6", "meta", ("meta", "meta-llama", "llama", "muse"), ("llama", "muse")),
    ("zai", "Z.ai", "#e3cf4a", "zai", ("zai", "z-ai", "z.ai", "zhipu"), ("glm",)),
    ("deepseek", "DeepSeek", "#5b6ef0", "deepseek", ("deepseek", "deepseek-ai"), ("deepseek",)),
    ("moonshot", "Moonshot", "#b8864b", "kimi",
     ("moonshot", "moonshot-ai", "moonshotai", "kimi"), ("kimi", "moonshot")),
    ("minimax", "MiniMax", "#e2466e", "minimax", ("minimax",), ("minimax",)),
    ("alibaba", "Alibaba", "#ff6a00", "qwen-code", ("alibaba", "qwen"), ("qwen",)),
    ("mistral", "Mistral", "#f26b1d", "mistral-vibe", ("mistral", "mistral-ai"),
     ("mistral", "devstral", "codestral", "magistral")),
    ("xiaomi", "Xiaomi", "#f5a524", "xiaomimimo", ("xiaomi", "mimo"), ("mimo",)),
    ("stepfun", "StepFun", "#14c8c8", "stepfun", ("stepfun",), (r"step[- ]?\d",)),
    # InclusionAI is Ant Group's lab; Ant Group's mark stands for it.
    ("inclusionai", "InclusionAI", "#c45bdc", "antgroup", ("inclusionai", "antgroup"), ("ling", "ring-")),
    ("nvidia", "NVIDIA", "#76b900", "nvidia", ("nvidia",), ("nemotron",)),
    ("upstage", "Upstage", None, "upstage", ("upstage",), ("solar",)),
    ("yi", "01.AI", None, "yi", ("01", "yi"), ("yi-",)),
    ("cognition", "Cognition", "#9aa0aa", "devin", ("cognition", "cognition-ai", "cognition_ai"),
     ("swe-", "compactor", "summarizer")),
    ("cursor", "Cursor", None, "cursor", ("cursor",), ("composer", "cursor-")),
    # API providers: keys and gateways with no models of their own, so a
    # logged name never resolves to them. Listed so the table stays a
    # mirror of the Swift one.
    ("groq", "Groq", None, "groq", ("groq",), ()),
    ("cerebras", "Cerebras", None, "cerebras", ("cerebras",), ()),
    ("opencode", "OpenCode", None, "opencode", ("opencode",), ()),
    ("openrouter", "OpenRouter", None, "openrouter", ("openrouter",), ()),
    ("together", "Together AI", None, "together", ("together",), ()),
    ("fireworks", "Fireworks AI", None, "fireworks", ("fireworks",), ()),
    ("vercel", "Vercel", None, "vercel", ("vercel",), ()),
    ("nebius", "Nebius", None, "nebius", ("nebius",), ()),
    ("featherless", "Featherless AI", None, "featherless", ("featherless",), ()),
    ("cohere", "Cohere", None, "cohere", ("cohere",), ()),
    ("perplexity", "Perplexity", None, "perplexity", ("perplexity",), ()),
    ("github", "GitHub", None, "github", ("github",), ()),
    ("huggingface", "Hugging Face", None, "huggingface", ("huggingface", "hugging"), ()),
    ("routing-run", "Routing.Run", None, "routing-run", ("routing", "routingrun"), ()),
    # Infra services: keys that deploy and manage resources, on the
    # Providers page's Infra tab.
    ("netlify", "Netlify", None, "netlify", ("netlify",), ()),
    ("cloudflare", "Cloudflare", None, "cloudflare", ("cloudflare",), ()),
    ("supabase", "Supabase", None, "supabase", ("supabase",), ()),
    ("neon", "Neon", None, "neon", ("neon",), ()),
    ("turso", "Turso", None, "turso", ("turso",), ()),
    ("railway", "Railway", None, "railway", ("railway",), ()),
    ("flyio", "Fly.io", None, "flyio", ("fly", "flyio"), ()),
    ("render", "Render", None, "render", ("render",), ()),
    ("digitalocean", "DigitalOcean", None, "digitalocean",
     ("digitalocean", "digital ocean", "doctl"), ()),
    ("heroku", "Heroku", None, "heroku", ("heroku",), ()),
    # Services: agent-facing APIs — search, scraping and browser services —
    # on the Providers page's Services tab.
    ("parallel", "Parallel", None, "parallel", ("parallel",), ()),
    ("firecrawl", "Firecrawl", None, "firecrawl", ("firecrawl",), ()),
    ("tinyfish", "TinyFish", None, "tinyfish", ("tinyfish",), ()),
    ("browseruse", "Browser Use", None, "browseruse", ("browseruse", "browser use"), ()),
    ("exa", "Exa", None, "exa", ("exa",), ()),
    ("tavily", "Tavily", None, "tavily", ("tavily",), ()),
    ("brave", "Brave Search", None, "brave", ("brave", "brave search"), ()),
    ("browserbase", "Browserbase", None, "browserbase", ("browserbase",), ()),
    ("apify", "Apify", None, "apify", ("apify",), ()),
    ("e2b", "E2B", None, "e2b", ("e2b",), ()),
    ("runloop", "Runloop", None, "runloop", ("runloop",), ()),
    ("supermemory", "Supermemory", None, "supermemory",
     ("supermemory", "super memory"), ()),
    ("pinecone", "Pinecone", None, "pinecone", ("pinecone",), ()),
    ("langfuse", "Langfuse", None, "langfuse", ("langfuse",), ()),
    ("braintrust", "Braintrust", None, "braintrust",
     ("braintrust", "brain trust"), ()),
    ("deepgram", "Deepgram", None, "deepgram", ("deepgram",), ()),
    ("assemblyai", "AssemblyAI", None, "assemblyai",
     ("assemblyai", "assembly"), ()),
    ("cartesia", "Cartesia", None, "cartesia", ("cartesia",), ()),
    ("stability", "Stability AI", None, "stability",
     ("stability", "stabilityai", "stable diffusion"), ()),
    ("replicate", "Replicate", None, "replicate", ("replicate",), ()),
    ("agentmail", "AgentMail", None, "agentmail", ("agentmail",), ()),
    # Inference plan vendors and API key providers.
    ("tencent", "Tencent", None, "tencentcloud", ("tencent", "hunyuan", "tokenhub"), ()),
    ("baidu", "Baidu", None, "baiducloud", ("baidu", "qianfan", "ernie"), ()),
    ("huawei", "Huawei", None, "huaweicloud", ("huawei", "pangu", "modelarts"), ()),
    ("iflytek", "Astron (iFlytek)", None, "iflytek", ("iflytek", "xfyun", "astron"), ()),
    ("sensetime", "SenseTime", None, "sensenova", ("sensetime", "sensenova"), ()),
    ("kuaishou", "Kuaishou", None, "kwaikat", ("kuaishou", "kwaikat", "kwaipilot", "streamlake"), ()),
    ("ollama", "Ollama", None, "ollama", ("ollama",), ()),
    ("poe", "Poe", None, "poe", ("poe",), ()),
    ("synthetic", "Synthetic", None, "synthetic", ("synthetic",), ()),
    ("zenmux", "ZenMux", None, "zenmux", ("zenmux",), ()),
    ("atlascloud", "Atlas Cloud", None, "atlascloud", ("atlascloud",), ()),
    ("taotoken", "TaoToken", None, "taotoken", ("taotoken",), ()),
    ("elevenlabs", "ElevenLabs", None, "elevenlabs", ("elevenlabs",), ()),
    ("jdcloud", "JD Cloud", None, "jdcloud", ("jdcloud", "joybuilder"), ()),
    ("mthreads", "Moore Threads", None, "mthreads", ("mthreads", "moorethreads", "kuae"), ()),
    ("volcengine", "Volcengine", None, "ark", ("volcengine", "byteplus", "ark"), ()),
    ("ucloud", "UCloud", None, "ucloud", ("ucloud", "compshare"), ()),
    ("unicom", "China Unicom", None, "unicom", ("unicom", "cucloud"), ()),
    ("ctyun", "China Telecom", None, "ctyun", ("ctyun", "telecom"), ()),
    ("scnet", "SCNet", None, "scnet", ("scnet",), ()),
    ("charm", "Charm", None, "charm", ("charm", "hyper"), ()),
    ("llmgateway", "LLM Gateway", None, "llmgateway", ("llmgateway", "devpass"), ()),
    ("standardcompute", "Standard Compute", None, "standardcompute", ("stdcmpt",), ()),
    ("cheapestinference", "CheapestInference", None, "cheapestinference", ("cheapestinference",), ()),
    # The Models page's benchmark source; a key filed under its name shows
    # the mark too. "artificial" alone is too generic to claim, so only
    # the run-together id matches.
    ("artificialanalysis", "Artificial Analysis", None, "artificial-analysis",
     ("artificialanalysis",), ()),
]

OTHER = ("other", "Other", "#45454b", None, (), ())

_PATTERNS = [(re.compile("^(?:" + p + ")"), b) for b in BRANDS for p in b[5]]


def _load_aliases() -> dict[str, str]:
    try:
        raw = json.loads(SUPPLEMENT.read_text(encoding="utf-8")).get("aliases") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {k.lower(): v for k, v in raw.items() if isinstance(v, str)}


# The supplement's aliases, keyed lower-cased as ``PricingCatalog`` keys them.
_ALIASES = _load_aliases()


def logo_file(icon: str | None) -> str | None:
    """The logo shipped with the web app for a brand icon id, colour
    version first (``HarnessIcon.exists``), or ``None`` when it has none."""
    if not icon:
        return None
    for name in (f"harness-{icon}-color.svg", f"harness-{icon}.svg"):
        if (Path(STATIC_DIR) / name).is_file():
            return name
    return None


def resolved_name(model: str) -> str | None:
    """``ModelBrands.resolvedName``: the real name the bundled price
    supplement aliases a logged id to, such as ``claude-haiku-4-5`` for
    Devin's ``MODEL_PRIVATE_11``; ``None`` when the name is its own."""
    return alias_target(model, _ALIASES)


def brand_for(model: str) -> dict:
    """``ModelBrands.forModel``: the brand of a logged name such as
    ``claude-opus-5-5`` or ``openrouter/qwen3-coder``, through any alias
    first; "Other" when no pattern matches. ``color`` falls back to
    Other's grey, as ``ModelVendor`` does, and ``logo`` names the file
    beside the web app's page."""
    name = (resolved_name(model) or model).lower()
    name = name[name.rfind("/") + 1:]
    brand = next((b for pattern, b in _PATTERNS if pattern.match(name)), OTHER)
    bid, title, color, icon, _keywords, _models = brand
    logo = logo_file(icon)
    return {"id": bid, "name": title, "color": color or OTHER[2], "logo": logo,
            "mono": bool(logo) and not logo.endswith("-color.svg")}
