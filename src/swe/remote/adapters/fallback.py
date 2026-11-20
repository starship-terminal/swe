"""``SESSION_FALLBACK``: the read-only floor under ``capabilities.session``.

harness.json's ``capabilities.session`` is the source of truth for how
SWE drives a harness's sessions::

    "capabilities": {"session": {
        "adapter": "codex_appserver",
        "argv": ["codex", "app-server"],
        "min_version": "0.40.0",
        "steer": false, "plan_mode": false, "rollback": false,
        "fork": false, "stop_task": false,
        "options": ["model", "effort"]
    }}

or ``{"session": {"supported": false, "reason": "..."}}`` for a harness
with no structured protocol. This table only fires for a harness the
registry has never described, the same role ``acp.ACP_FALLBACK``,
``launch_model.MODEL_FALLBACK`` and ``HARNESS_ROOTS`` play
(ARCHITECTURE.md, Capabilities-first). A registry entry wins field by
field over a row here.

Rows name only channels that exist in the code today: Claude's planned
stream adapter (G7), Codex's app-server and opencode's serve
(``remote/inject.py``), and the ACP harnesses of ``acp.ACP_FALLBACK``
(claude excepted: its ACP adapter stays opt-in). Every optional feature
is off: an adapter turns one on in harness.json once a recorded fixture
proves it. ``argv`` None lets the adapter use its own default; the ACP
adapter then probes ``acp.ACP_FALLBACK``'s candidates through
``acp.command_for``.

``registry.adapter_for`` is the only reader.

``SESSION_SEED`` is not a fallback: it is what the hub writes into
harness.json, once, for a registered harness whose entry declares no
``capabilities.session`` when ``remote.session_view`` is turned on
(``runtime.SessionRuntime.seed_capabilities``). From then on the file
is the authority and this table is never read for that harness again.
A row lists exactly what a recorded fixture proves
(docs/specs/G7-claude-stream-adapter.md, "Proven by fixture").
"""

from __future__ import annotations

from types import MappingProxyType

# One ACP row; every ACP harness shares it.
_ACP: dict = {"adapter": "acp", "argv": None}

SESSION_FALLBACK = MappingProxyType({
    # The Agent SDK's own spawn line; G7's fixture settles the exact one.
    "claude": MappingProxyType({
        "adapter": "claude_stream",
        "argv": ("claude", "--output-format", "stream-json", "--verbose",
                 "--input-format", "stream-json"),
    }),
    "codex": MappingProxyType({"adapter": "codex_appserver",
                               "argv": ("codex", "app-server")}),
    "opencode": MappingProxyType({"adapter": "opencode_serve",
                                  "argv": ("opencode", "serve")}),
    "devin": MappingProxyType(dict(_ACP)),
    "cursor": MappingProxyType(dict(_ACP)),
    "copilot": MappingProxyType(dict(_ACP)),
    "gemini": MappingProxyType(dict(_ACP)),
    "qwen-code": MappingProxyType(dict(_ACP)),
    "kimi": MappingProxyType(dict(_ACP)),
})

# Written into harness.json once (see the module docstring), never read
# as a fallback. Claude: the proven spawn line and the features the
# 2.1.328 recordings prove; ``rollback`` and the ``effort`` option have
# no wire form and stay off.
SESSION_SEED = MappingProxyType({
    "claude": MappingProxyType({
        "adapter": "claude_stream",
        "argv": ["claude", "--output-format", "stream-json", "--verbose",
                 "--input-format", "stream-json",
                 "--permission-prompt-tool", "stdio",
                 "--include-partial-messages"],
        "min_version": "2.0.0",
        "steer": True, "plan_mode": True, "rollback": False,
        "fork": True, "stop_task": True,
        "options": ["model", "permission_mode"],
    }),
})
