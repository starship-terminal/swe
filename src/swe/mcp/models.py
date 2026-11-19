"""``--json`` payload models for the mcp domain."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerRow:
    """One ``swe mcp list --json`` / ``swe mcp status --json`` server row.

    Env and header values stay out; only their names go into the row. The
    command, arguments and URL are already scrubbed by the caller, since a
    credential can sit in any of them (see ``swe.mcp.secrets.scrub_url``).
    """

    name: str
    transport: str
    command: str | None
    args: list
    url: str | None
    env_keys: list
    header_keys: list
    harnesses: list
    conflict: bool
    variants: list
    # ``${NAME}`` references in the hub's definition the secrets file can't
    # satisfy; [] for a server the hub doesn't hold. Swe fills them from
    # `swe mcp secret set`, and sync refuses until then.
    secrets_missing: list
    # Whether the hub (mcp.json) defines this server. A hub-only row has
    # empty harnesses and variants.
    in_hub: bool
    # The places (harnesses, and the hub under both its names — "quiver"
    # for older apps, "swe" for the renamed one) whose definition is
    # reached at a URL. ``transport`` is the first definition's; a sync
    # copies one place's, so it asks this.
    remote_in: list = field(default_factory=list)
    # Harnesses the server is toggled off for — the entry left their file
    # and the hub keeps the copy ``swe mcp enable`` restores.
    disabled_in: list = field(default_factory=list)
    # The hub entry's ``type`` label (``swe mcp set-type``); None when the
    # hub does not hold the server or its ``type`` is a transport marker.
    type: str | None = None
    # The harness a sync adopted the hub entry from (``swe.adopted_from``);
    # None for a server added straight to the hub or not in it.
    adopted_from: str | None = None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "url": self.url,
            "env_keys": self.env_keys,
            "header_keys": self.header_keys,
            "harnesses": self.harnesses,
            "conflict": self.conflict,
            "variants": self.variants,
            "secrets_missing": self.secrets_missing,
            "in_hub": self.in_hub,
            "remote_in": self.remote_in,
            "disabled_in": self.disabled_in,
            "type": self.type,
            "adopted_from": self.adopted_from,
        }


@dataclass(frozen=True)
class ToggleReply:
    """``swe mcp enable``/``disable --json``: the harnesses the call switched.

    ``disabled_in`` is what a disable wrote off; ``enabled_in`` what an
    enable restored. ``failed_in`` names targets an enable could not
    restore — the server stays marked off there.
    """

    name: str
    enabled_in: list = field(default_factory=list)
    disabled_in: list = field(default_factory=list)
    failed_in: list = field(default_factory=list)

    def to_json(self) -> dict:
        out = {"name": self.name}
        if self.enabled_in:
            out["enabled_in"] = self.enabled_in
        if self.disabled_in:
            out["disabled_in"] = self.disabled_in
        if self.failed_in:
            out["failed_in"] = self.failed_in
        return out


@dataclass(frozen=True)
class SetTypeReply:
    """``swe mcp set-type NAME TYPE``/``--clear --json``: the label left.

    ``type`` is the label written, or None when the call cleared one (or
    found a URL server's ``sse`` marker, which is wire metadata it leaves
    alone).
    """

    name: str
    type: str | None = None

    def to_json(self) -> dict:
        return {"name": self.name, "type": self.type}


@dataclass(frozen=True)
class SetReply:
    """``swe mcp set NAME ... --json``: what an in-place edit reached.

    ``changed`` is False when the hub entry already read that way and
    nothing was written. ``synced`` names the harnesses that hold the
    server and took the new definition; ``failed`` the ones still on the
    old one.
    """

    name: str
    changed: bool = True
    synced: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"name": self.name, "changed": self.changed,
                "synced": self.synced, "failed": self.failed}


@dataclass(frozen=True)
class RenameReply:
    """``swe mcp rename OLD NEW --json``: where the new name landed.

    ``hub`` says whether the hub entry moved; ``renamed_in`` names the
    harness files rewritten. A harness file that cannot be written stops
    the rename before the hub moves: ``failed_in`` names it, ``name``
    stays the old name and nothing is renamed.
    """

    name: str
    renamed_from: str
    hub: bool = True
    renamed_in: list = field(default_factory=list)
    failed_in: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"name": self.name, "renamed_from": self.renamed_from,
                "hub": self.hub, "renamed_in": self.renamed_in,
                "failed_in": self.failed_in}


@dataclass(frozen=True)
class PlaceRow:
    """One place ``swe mcp info`` found a server: a harness file or the hub.

    ``state`` is ``on`` (the file defines it), ``off`` (swe-disabled
    there — the hub keeps the copy) or ``absent`` for harnesses asked
    about that do not define it. ``updated`` is the file's mtime, ISO;
    None when the file does not exist.
    """

    place: str
    path: str
    state: str
    updated: str | None

    def to_json(self) -> dict:
        return {"place": self.place, "path": self.path,
                "state": self.state, "updated": self.updated}


@dataclass(frozen=True)
class InfoReply:
    """``swe mcp info NAME --json``: one server's full detail.

    Identity fields mirror ``ServerRow`` (scrubbed command/args/url, key
    names only). ``places`` lists every file that does or did define it
    with each file's mtime; ``updated`` is the newest of those. ``health``
    is the shallow probe (binary/URL/env) ``mcp status`` uses; ``gateway``
    is the live gateway's row for a shared server, or None.
    """

    name: str
    transport: str
    command: str | None
    args: list
    url: str | None
    env_keys: list
    header_keys: list
    secrets_missing: list
    in_hub: bool
    shared: bool
    harnesses: list
    conflict: bool
    variants: list
    disabled_in: list
    places: list
    health: str
    health_detail: str | None
    updated: str | None
    # The hub entry's ``type`` label (``swe mcp set-type``), or None.
    type: str | None = None
    # The harness the hub entry was adopted from, or None — same field as
    # ``ServerRow.adopted_from``.
    adopted_from: str | None = None
    gateway: dict | None = None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "url": self.url,
            "env_keys": self.env_keys,
            "header_keys": self.header_keys,
            "secrets_missing": self.secrets_missing,
            "in_hub": self.in_hub,
            "harnesses": self.harnesses,
            "shared": self.shared,
            "type": self.type,
            "adopted_from": self.adopted_from,
            "conflict": self.conflict,
            "variants": self.variants,
            "disabled_in": self.disabled_in,
            "places": [p.to_json() for p in self.places],
            "health": self.health,
            "health_detail": self.health_detail,
            "updated": self.updated,
            "gateway": self.gateway,
        }


@dataclass(frozen=True)
class ToolsReply:
    """``swe mcp tools NAME --json``: what a live handshake reported.

    ``server`` is the initialize result's ``serverInfo`` (name/version);
    ``tools`` are name + description pairs — input schemas stay out, they
    dwarf a listing. ``error`` is set instead of the probe fields when the
    handshake failed.
    """

    name: str
    transport: str
    tools: list = field(default_factory=list)
    server: dict | None = None
    protocol_version: str | None = None
    error: str | None = None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "transport": self.transport,
            "server": self.server,
            "protocol_version": self.protocol_version,
            "tools": self.tools,
            "error": self.error,
        }


@dataclass(frozen=True)
class DiscoveryRow:
    """One ``swe mcp discover --json`` row (a bare list, no envelope)."""

    name: str
    tools: list
    status: str
    source_tool: str
    summary: str

    @classmethod
    def of(cls, f, summary: str) -> "DiscoveryRow":
        return cls(
            name=f.name,
            tools=list(f.tools),
            status=f.status,
            source_tool=f.source_tool,
            summary=summary,
        )

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "tools": self.tools,
            "status": self.status,
            "source_tool": self.source_tool,
            "summary": self.summary,
        }
