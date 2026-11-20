import Foundation
import Observation

/// `swe mcp list --json` / `swe mcp status --json`.
///
/// Env and header values never reach the app: swe sends only their names,
/// and scrubs credentials out of commands, arguments and URLs.
public struct McpInventory: SnapshotValue {
    public static let snapshotVersion = 1

    /// Every harness config swe read, servers or not, in its order.
    public let harnesses: [String]
    public let servers: [McpServer]

    public init(harnesses: [String], servers: [McpServer]) {
        self.harnesses = harnesses
        self.servers = servers
    }

    enum CodingKeys: String, CodingKey { case harnesses, servers }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        servers = try container.decode([McpServer].self, forKey: .servers)
        // A swe from before `harnesses` existed: the ones with servers will do.
        if let named = try container.decodeIfPresent([String].self, forKey: .harnesses) {
            harnesses = named
        } else {
            var seen: [String] = []
            for name in servers.flatMap(\.harnesses) where !seen.contains(name) { seen.append(name) }
            harnesses = seen
        }
    }

    /// What may be saved: every server as `McpServer.saved` keeps it.
    public var saved: McpInventory {
        McpInventory(harnesses: harnesses, servers: servers.map(\.saved))
    }

    /// Harnesses that configure at least one server. One with none is not
    /// using MCP, so it gets no column and does not make every server look
    /// out of sync; it can still be a sync target.
    public var activeHarnesses: [String] {
        harnesses.filter { harness in servers.contains {
            $0.harnesses.contains(harness) || $0.disabledIn.contains(harness) } }
    }

    public var idleHarnesses: [String] {
        harnesses.filter { !activeHarnesses.contains($0) }
    }

    /// The servers `filter` keeps, judged across `compared` (default: the
    /// active harnesses). A conflict counts only between compared harnesses.
    public func servers(_ filter: McpFilter, across compared: [String]? = nil) -> [McpServer] {
        let compared = compared ?? activeHarnesses
        switch filter {
        case .all:
            return servers
        case .drift:
            return servers.filter { server in
                !server.missing(from: compared).isEmpty || server.conflicts(among: compared)
            }
        case .conflicts:
            return servers.filter { $0.conflicts(among: compared) }
        }
    }
}

public enum McpFilter: String, CaseIterable, Sendable {
    case all, drift, conflicts

    public var title: String {
        switch self {
        case .all: "All"
        case .drift: "Out of sync"
        case .conflicts: "Conflicts"
        }
    }
}

public struct McpServer: Codable, Sendable, Equatable, Identifiable {
    public var id: String { name }
    public let name: String
    public let transport: String
    public let command: String?
    public let args: [String]
    public let url: String?
    public let envKeys: [String]
    public let headerKeys: [String]
    /// Harnesses that define this server, in swe's order.
    public let harnesses: [String]
    public let conflict: Bool
    /// The harnesses grouped by identical definition. Absent from an older
    /// swe, which only says whether there is a conflict.
    public let variants: [[String]]?
    /// From `swe mcp status` only: ok, error or unknown.
    public let health: String?
    public let detail: String?
    /// `${NAME}` references in the hub's definition with no value in the
    /// secrets file. Absent from an older swe, decoded as none missing.
    public let secretsMissing: [String]

    /// Reached at a URL (http, sse) rather than launched with a command on
    /// this machine; the MCP page's Remote group.
    public var isRemote: Bool { transport == "http" || transport == "sse" }

    /// Whether the definition a sync copies from `source` is at a URL: a
    /// server can be local in one harness and remote in another. The hub
    /// answers to both names: swe lists it as `swe` and `quiver` (for apps
    /// from before the rename), and an older swe wrote `quiver` alone.
    public func isRemote(from source: String) -> Bool {
        guard let remoteIn else { return isRemote }
        if McpServer.isHubSource(source) {
            return remoteIn.contains(where: McpServer.isHubSource)
        }
        return remoteIn.contains(source)
    }

    /// Harnesses whose config launches local servers only, as swe's
    /// `local_only` marks them: Claude Desktop adds a remote server in the
    /// app itself (Settings › Connectors).
    public static let localOnly: Set<String> = ["claude-desktop"]
    /// The hub's name as a sync source or remove place. `quiver`, its
    /// name before the rename, is still read wherever a source is named:
    /// an older swe's `remote_in` says only that.
    public static let hubSource = "swe"
    public static func isHubSource(_ source: String) -> Bool {
        source == hubSource || source == "quiver"
    }
    /// Whether ~/.swe/mcp.json defines this server. A hub-only row has
    /// no harnesses; an older swe sends nothing and decodes false.
    public let inHub: Bool
    /// The places (harnesses, "swe" and "quiver" for the hub) whose definition is at
    /// a URL. Absent from an older swe, which leaves `transport` to say.
    public let remoteIn: [String]?
    /// Harnesses the server is switched off for: the entry left their
    /// config and the hub keeps the copy `swe mcp enable` restores.
    /// Absent from an older swe, which decodes as nothing disabled.
    public let disabledIn: [String]
    /// What the server is for (`dev`, `productivity`, `research`,
    /// `social`, `search`, `other`), set in mcp.json. Absent from a swe
    /// before the field existed.
    public let type: String?
    /// Set only on a saved server, which keeps `summary` in place of the
    /// command line or URL.
    let savedSummary: String?

    /// The values `type` can carry, in the order the page's filter
    /// lists them. `other` is also what anything outside this list —
    /// or no `type` at all — reads as.
    public static let knownTypes = ["dev", "productivity", "research", "social", "search", "other"]

    /// `type` as one of `knownTypes`: lowercase matched, anything else
    /// (a value a newer swe invented, none at all) reads as `other`.
    public var typeTag: String {
        let raw = type?.lowercased() ?? ""
        return Self.knownTypes.contains(raw) ? raw : "other"
    }

    public init(name: String, transport: String = "stdio", command: String? = nil, args: [String] = [],
                url: String? = nil, envKeys: [String] = [], headerKeys: [String] = [],
                harnesses: [String], conflict: Bool = false, variants: [[String]]? = nil,
                health: String? = nil, detail: String? = nil, secretsMissing: [String] = [],
                inHub: Bool = false, savedSummary: String? = nil, remoteIn: [String]? = nil,
                disabledIn: [String] = [], type: String? = nil) {
        self.name = name
        self.transport = transport
        self.command = command
        self.args = args
        self.url = url
        self.envKeys = envKeys
        self.headerKeys = headerKeys
        self.harnesses = harnesses
        self.conflict = conflict
        self.variants = variants
        self.health = health
        self.detail = detail
        self.secretsMissing = secretsMissing
        self.inHub = inHub
        self.savedSummary = savedSummary
        self.remoteIn = remoteIn
        self.disabledIn = disabledIn
        self.type = type
    }

    private enum CodingKeys: String, CodingKey {
        case name, transport, command, args, url, envKeys, headerKeys
        case harnesses, conflict, variants, health, detail
        case secretsMissing, inHub, savedSummary, remoteIn, disabledIn, type
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        transport = try container.decode(String.self, forKey: .transport)
        command = try container.decodeIfPresent(String.self, forKey: .command)
        args = try container.decodeIfPresent([String].self, forKey: .args) ?? []
        url = try container.decodeIfPresent(String.self, forKey: .url)
        envKeys = try container.decodeIfPresent([String].self, forKey: .envKeys) ?? []
        headerKeys = try container.decodeIfPresent([String].self, forKey: .headerKeys) ?? []
        harnesses = try container.decode([String].self, forKey: .harnesses)
        conflict = try container.decodeIfPresent(Bool.self, forKey: .conflict) ?? false
        variants = try container.decodeIfPresent([[String]].self, forKey: .variants)
        health = try container.decodeIfPresent(String.self, forKey: .health)
        detail = try container.decodeIfPresent(String.self, forKey: .detail)
        secretsMissing = try container.decodeIfPresent([String].self, forKey: .secretsMissing) ?? []
        inHub = try container.decodeIfPresent(Bool.self, forKey: .inHub) ?? false
        savedSummary = try container.decodeIfPresent(String.self, forKey: .savedSummary)
        remoteIn = try container.decodeIfPresent([String].self, forKey: .remoteIn)
        disabledIn = try container.decodeIfPresent([String].self, forKey: .disabledIn) ?? []
        type = try container.decodeIfPresent(String.self, forKey: .type)
    }

    /// The server as it may be written to disk. No command line or URL:
    /// a short key in an argument or URL path slips past both swe's and
    /// the app's scrubbers, so only the program or the scheme and host
    /// are kept, with "…" for the rest. No health either, a probe of the
    /// moment whose detail may quote a URL. Env and header values never
    /// reach the app at all.
    public var saved: McpServer {
        McpServer(name: name, transport: transport, envKeys: envKeys, headerKeys: headerKeys, harnesses: harnesses,
                  conflict: conflict, variants: variants, secretsMissing: secretsMissing,
                  inHub: inHub, savedSummary: savedSummary ?? outline, remoteIn: remoteIn,
                  disabledIn: disabledIn, type: type)
    }

    /// The program or the scheme and host, and "…" when there is more.
    var outline: String {
        if let url {
            let redacted = McpRedaction.url(url)
            let start = redacted.range(of: "://")?.upperBound ?? redacted.startIndex
            guard let end = redacted[start...].firstIndex(where: { "/?#".contains($0) }) else { return redacted }
            return redacted[..<end] + "/…"
        }
        let words = McpRedaction.words(command: command ?? "", args: args)
        guard let program = words.first else { return "" }
        let shown = McpRedaction.args([program]).first ?? "…"
        return words.count > 1 ? shown + " …" : shown
    }

    /// Definitions grouped by who agrees. Without `variants`, everyone is
    /// one group when there is no conflict and nothing can be said when there is.
    public var groups: [[String]] {
        if let variants { return variants }
        return conflict ? harnesses.map { [$0] } : [harnesses]
    }

    /// Which group a harness's definition is in, nil when it lacks the server.
    public func group(of harness: String) -> Int? {
        groups.firstIndex { $0.contains(harness) }
    }

    public func missing(from all: [String]) -> [String] {
        all.filter { !harnesses.contains($0) }
    }


    /// Two of `harnesses` hold different definitions.
    public func conflicts(among harnesses: [String]) -> Bool {
        Set(harnesses.compactMap { group(of: $0) }).count > 1
    }

    /// The command line or URL, for a one-line summary, with anything that
    /// may be a secret hidden. swe swaps known keys for `${NAME}`, but a
    /// token it doesn't know would otherwise show as typed.
    public var summary: String {
        if let savedSummary { return savedSummary }
        if let url { return McpRedaction.url(url) }
        let words = McpRedaction.words(command: command ?? "", args: args)
        return McpRedaction.args(words).joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }

    /// `--only` takes fnmatch globs, comma separated. A comma cannot be
    /// escaped there, so such a name cannot be synced on its own; nor can an
    /// empty one, since `--only=` with no pattern selects every server.
    public var canSyncAlone: Bool { !name.isEmpty && !name.contains(",") }

    /// The name as an fnmatch pattern matching only itself.
    public var onlyPattern: String {
        var out = ""
        for character in name {
            switch character {
            case "[": out += "[[]"
            case "*", "?": out += "[\(character)]"
            default: out.append(character)
            }
        }
        return out
    }
}

/// Hides the parts of an MCP command line or URL that may hold a secret.
enum McpRedaction {
    /// Words that mark a secret anywhere in a flag or key name, compared
    /// without dashes, underscores or case: `--api-key`, `--apiKey`,
    /// `--keyfile`, `SESSION_COOKIE`. The same words as swe's
    /// `_SECRET_FLAG_RE` (src/swe/mcp/secrets.py), so the app hides
    /// whatever `swe mcp` hides.
    static let secretNameParts = ["key", "token", "secret", "passw", "bearer", "credential", "cookie",
                                  "signature", "authorization", "authorisation"]
    /// Endings that mark one too: `--auth`, and a header flag's value
    /// (`--header`, `--headers`, `--proxy-header`).
    static let secretNameEndings = ["auth", "header", "headers"]

    static func isSecretName(_ name: String) -> Bool {
        let name = name.filter { $0 != "-" && $0 != "_" }.lowercased()
        return !name.isEmpty && (secretNameParts.contains { name.contains($0) }
                                 || secretNameEndings.contains { name.hasSuffix($0) })
    }

    static func isSecretFlag(_ flag: Substring) -> Bool {
        let flag = bare(flag)
        guard flag.hasPrefix("-") else { return false }
        if flag == "-H" { return true }
        return isSecretName(flag)
    }

    /// The flag as the shell reads it: `--"token"` and `--\token` are `--token`.
    static func bare(_ flag: Substring) -> String {
        flag.filter { $0 != "\"" && $0 != "'" && $0 != "\\" }
    }

    /// The command line as words, so every flag and its value can be judged.
    /// A whole command line typed into `command` is split the way a shell
    /// would, keeping a quoted value with spaces as one word. An argument
    /// holding a flag and its value (`--token abc`) becomes two.
    static func words(command: String, args: [String]) -> [String] {
        var words = shellWords(command)
        for arg in args {
            let space = arg.firstIndex(where: \.isWhitespace)
            if arg.hasPrefix("-"), let space, !arg[..<space].contains("=") {
                words.append(String(arg[..<space]))
                words.append(arg[space...].trimmingCharacters(in: .whitespaces))
            } else {
                words.append(arg)
            }
        }
        return words
    }

    /// Words split at unquoted whitespace, quotes kept so the text reads as
    /// typed. With an unclosed quote every space splits, so no word hides a
    /// later flag.
    static func shellWords(_ line: String) -> [String] {
        var words: [String] = []
        var word = ""
        var quote: Character?
        var escaped = false
        for character in line {
            if escaped {
                word.append(character)
                escaped = false
            } else if character == "\\", quote != "'" {
                word.append(character)
                escaped = true
            } else if let open = quote {
                word.append(character)
                if character == open { quote = nil }
            } else if character == "\"" || character == "'" {
                word.append(character)
                quote = character
            } else if character.isWhitespace {
                if !word.isEmpty { words.append(word) }
                word = ""
            } else {
                word.append(character)
            }
        }
        if quote != nil { return looseWords(line) }
        if !word.isEmpty { words.append(word) }
        return words
    }

    /// Split at every space and quote mark, for text the shell split can't
    /// handle, so a flag glued to a quote (`"--token`) is still seen.
    static func looseWords(_ line: String) -> [String] {
        line.split { $0.isWhitespace || $0 == "\"" || $0 == "'" }.map(String.init)
    }

    static func args(_ args: [String]) -> [String] {
        var out: [String] = []
        var hideNext: Substring?
        for arg in args {
            if let flag = hideNext {
                out.append(secretValue(arg[...], after: flag))
                hideNext = nil
            } else if let equals = arg.firstIndex(of: "="), isSecretFlag(arg[..<equals]) {
                let flag = arg[..<equals]
                out.append(flag + "=" + secretValue(arg[arg.index(after: equals)...], after: flag))
            } else if isSecretFlag(arg[...]) {
                out.append(arg)
                hideNext = arg[...]
            } else if let match = arg.firstMatch(of: /^[A-Za-z_][A-Za-z0-9_]*=/) {
                // An environment assignment, as `env NAME=value cmd` takes.
                out.append(match.output + "…")
            } else if let redacted = urlWord(arg) {
                out.append(redacted)
            } else if let quote = arg.first, quote == "\"" || quote == "'", arg.count >= 2, arg.last == quote {
                // A quoted word may be a whole command, as `bash -c '…'` takes.
                out.append(String(quote) + line(String(arg.dropFirst().dropLast())) + String(quote))
            } else if arg.contains(where: \.isWhitespace) {
                out.append(line(arg))
            } else if arg.contains("://") {
                out.append(url(arg))
            } else {
                out.append(SecretText.maskingKeys(arg[...]))
            }
        }
        return out
    }

    /// A word that is a URL, quoted or not, redacted before anything splits
    /// it. A URL holds no spaces, so everything after one is hidden: it may
    /// be the rest of a query value.
    private static func urlWord(_ arg: String) -> String? {
        var quote = ""
        var inner = Substring(arg)
        if let first = arg.first, first == "\"" || first == "'", arg.count >= 2, arg.last == first {
            quote = String(first)
            inner = inner.dropFirst().dropLast()
        }
        guard inner.contains(/^[A-Za-z][A-Za-z0-9+.\-]*:\/\//) else { return nil }
        guard let space = inner.firstIndex(where: \.isWhitespace) else { return quote + url(String(inner)) + quote }
        return quote + url(String(inner[..<space])) + " …" + quote
    }

    /// A command line inside one word, redacted word by word. A word the
    /// shell split can't break up (`x"a b"`) is split at every space and
    /// quote mark, so each pass works on shorter text and no flag hides.
    private static func line(_ text: String) -> String {
        var words = shellWords(text)
        if words == [text] { words = looseWords(text) }
        return args(words).joined(separator: " ")
    }

    /// A header keeps its name (`Authorization:…`); any other value is hidden.
    private static func secretValue(_ value: Substring, after flag: Substring) -> String {
        let flag = bare(flag)
        let header = flag == "-H" || flag.lowercased().hasSuffix("header")
            || flag.lowercased().hasSuffix("headers")
        if header, let colon = value.firstIndex(of: ":") { return value[...colon] + "…" }
        return "…"
    }

    /// The URL with its userinfo, query values and fragment hidden, and any
    /// key-like path segment too, since some services put the key in the path.
    static func url(_ url: String) -> String {
        var rest = Substring(url)
        var out = ""
        if let scheme = rest.range(of: "://") {
            out += rest[..<scheme.upperBound]
            rest = rest[scheme.upperBound...]
        }
        let authorityEnd = rest.firstIndex(where: { "/?#".contains($0) }) ?? rest.endIndex
        var authority = rest[..<authorityEnd]
        if let at = authority.lastIndex(of: "@") {
            out += "…@"
            authority = authority[authority.index(after: at)...]
        }
        out += authority
        rest = rest[authorityEnd...]
        var fragment: Substring?
        if let hash = rest.firstIndex(of: "#") {
            fragment = rest[rest.index(after: hash)...]
            rest = rest[..<hash]
        }
        var query: Substring?
        if let mark = rest.firstIndex(of: "?") {
            query = rest[rest.index(after: mark)...]
            rest = rest[..<mark]
        }
        // A segment is judged decoded, so `sk%2Dproj%2D…` reads as `sk-proj-…`.
        out += rest.split(separator: "/", omittingEmptySubsequences: false).map { segment in
            let decoded = segment.removingPercentEncoding ?? String(segment)
            if decoded != segment, SecretText.maskingKeys(decoded[...]) != decoded { return "…" }
            return SecretText.maskingKeys(segment)
        }.joined(separator: "/")
        if let query {
            out += "?" + query.split(separator: "&", omittingEmptySubsequences: false).map { pair in
                if pair.isEmpty { return "" }
                guard let equals = pair.firstIndex(of: "=") else { return "…" }
                return pair[...equals] + "…"
            }.joined(separator: "&")
        }
        if let fragment, !fragment.isEmpty { out += "#…" }
        return out
    }
}

/// `swe mcp sync … --json`: which server names moved where.
public struct McpSyncReport: Decodable, Sendable, Equatable {
    public let source: String?
    public let dryRun: Bool
    public let selected: [String]
    public let targets: [Target]

    public struct Target: Decodable, Sendable, Equatable {
        public let name: String
        public let added: [String]
        public let updated: [String]
        public let skipped: [String]
        public let pruned: [String]

        public init(name: String, added: [String] = [], updated: [String] = [],
                    skipped: [String] = [], pruned: [String] = []) {
            self.name = name
            self.added = added
            self.updated = updated
            self.skipped = skipped
            self.pruned = pruned
        }
    }

    public init(source: String?, dryRun: Bool, selected: [String], targets: [Target]) {
        self.source = source
        self.dryRun = dryRun
        self.selected = selected
        self.targets = targets
    }

    /// One sentence for the page: "Added to cursor and cline. Updated codex."
    public var sentence: String {
        func names(_ list: [String]) -> String {
            switch list.count {
            case 0: ""
            case 1: list[0]
            default: list.dropLast().joined(separator: ", ") + " and " + list.last!
            }
        }
        let added = targets.filter { !$0.added.isEmpty }.map(\.name)
        let updated = targets.filter { !$0.updated.isEmpty }.map(\.name)
        let skipped = targets.filter { !$0.skipped.isEmpty }.map(\.name)
        var parts: [String] = []
        if !added.isEmpty { parts.append((dryRun ? "Would add to " : "Added to ") + names(added) + ".") }
        if !updated.isEmpty { parts.append((dryRun ? "Would update " : "Updated ") + names(updated) + ".") }
        if !skipped.isEmpty { parts.append((dryRun ? "Would leave " : "Left ") + names(skipped) + " as it was.") }
        return parts.isEmpty ? "Nothing to change." : parts.joined(separator: " ")
    }
}

extension SweClient {
    public func mcpServers(health: Bool) async throws -> McpInventory {
        try await call(["mcp", health ? "status" : "list", "--json"])
    }

    /// Copies one server from `source` to `targets` through `swe mcp sync`.
    /// `overwrite` replaces a different definition a target already has.
    public func mcpSync(server: McpServer, from source: String, to targets: [String],
                        overwrite: Bool, dryRun: Bool) async throws -> McpSyncReport {
        var args = ["mcp", "sync", source] + targets
        args += ["--only=" + server.onlyPattern, "--no-interactive", "--json"]
        if overwrite { args.append("--force") }
        if dryRun { args.append("--dry-run") }
        return try await call(args)
    }

    /// `swe mcp secret set NAME` with the value on stdin: never in argv,
    /// logs or the error text, where it would leak.
    @discardableResult
    public func mcpSecretSet(name: String, value: String) async throws -> McpSecretSetResult {
        try await call(["mcp", "secret", "set", name, "--json"],
                       input: Data((value + "\n").utf8))
    }

    /// `swe mcp secret remove NAME`: drops a stored value. True when one
    /// was there.
    @discardableResult
    public func mcpSecretRemove(name: String) async throws -> Bool {
        let result: McpSecretRemoveResult = try await call(["mcp", "secret", "remove", name, "--json"])
        return result.removed
    }

    /// Adds `spec` to the hub, and to each of `harnesses` when asked.
    public func mcpAdd(_ spec: McpServerSpec, to harnesses: [String],
                       force: Bool) async throws -> McpAddResult {
        var args = ["mcp", "add", spec.name] + spec.flags
            + ["--json"]
        if !harnesses.isEmpty { args += ["--to=" + harnesses.joined(separator: ",")] }
        if force { args.append("--force") }
        args += spec.tail
        return try await call(args)
    }

    /// Removes `name` from each place; swe reports `swe` for the hub.
    public func mcpRemove(name: String, from places: [String]) async throws -> McpRemoveResult {
        try await call(["mcp", "remove", name, "--from=" + places.joined(separator: ","), "--json"])
    }

    /// `swe mcp set-type NAME TYPE --json`: the hub entry's label after
    /// the call. The picker's word goes verbatim; a server outside the
    /// hub, a transport word or a live `sse` marker is refused and
    /// throws `commandFailed` with swe's reason.
    public func mcpSetType(_ type: String, server name: String) async throws -> McpSetTypeResult {
        try await call(["mcp", "set-type", name, type, "--json"])
    }

    /// `swe mcp set NAME --url=…` or `-- command args…`: rewrites the hub
    /// entry in place and pushes it to every harness holding the server.
    /// Exactly one of `url` and `command`. A harness that kept the old
    /// definition comes back in `failed` on a nonzero exit.
    public func mcpSet(server name: String, url: String?,
                       command: [String]?) async throws -> McpSetResult {
        var args = ["mcp", "set", name, "--json"]
        if let url { args.append("--url=" + url) }
        // Last, after `--`: the command's own flags stay the command's.
        if let command, !command.isEmpty { args += ["--"] + command }
        return try await call(args, allowFailure: true)
    }

    /// `swe mcp rename OLD NEW`: the hub entry, its switches and every
    /// harness's own entry move to the new name.
    public func mcpRename(_ name: String, to newName: String) async throws -> McpRenameResult {
        try await call(["mcp", "rename", name, newName, "--json"], allowFailure: true)
    }

    /// `swe mcp enable`/`disable NAME --for=...`: a per-harness switch.
    /// `on` false pulls the entry out of the harnesses' configs (the hub
    /// keeps the copy), true restores it. Empty `harnesses` means every
    /// place the server is defined (off) or marked off (on).
    public func mcpSetEnabled(_ on: Bool, server name: String,
                            in harnesses: [String]) async throws -> McpToggleResult {
        var args = ["mcp", on ? "enable" : "disable", name, "--json"]
        if !harnesses.isEmpty { args.append("--for=" + harnesses.joined(separator: ",")) }
        // A partial switch still prints its document on a nonzero exit.
        return try await call(args, allowFailure: true)
    }

    /// `swe mcp info NAME --json`: files, switches, secrets and live
    /// gateway state for the server sheet. File reads only — no spawn.
    public func mcpInfo(server name: String) async throws -> McpServerInfo {
        try await call(["mcp", "info", name, "--json"])
    }

    /// `swe mcp tools NAME --json`: spawns a command server (or asks a
    /// URL one over http) for a handshake, so
    /// this is lazy in the UI. A failed handshake still prints its
    /// document — `error` — on a nonzero exit.
    public func mcpTools(server name: String) async throws -> McpToolsResult {
        try await call(["mcp", "tools", name, "--json"], allowFailure: true)
    }
}

/// `swe mcp info NAME --json`: one server's detail beyond the row.
///
/// Everything here is optional-tolerant — a swe from before `info`
/// exists can't be asked for it, and newer fields must not break an
/// older decode.
public struct McpServerInfo: Decodable, Sendable, Equatable {
    /// One file that defines the server or holds its off-mark.
    public struct Place: Decodable, Sendable, Equatable {
        /// Harness name, or `swe` for the hub file.
        public let place: String
        public let path: String
        /// `on` (the file defines it), `off` (swe-disabled) or `hub`.
        public let state: String
        /// The file's mtime, ISO; nil when it does not exist.
        public let updated: String?
    }

    /// The live gateway's `/status` row for a shared server.
    public struct GatewayRow: Decodable, Sendable, Equatable {
        public let name: String
        /// `running`, `idle` or `error`.
        public let state: String
        public let pid: Int?
        public let uptimeS: Int?
        public let starts: Int?
        public let error: String?
        /// `tools/call` replies proxied since counting began; persists
        /// across gateway restarts through `stats.json`.
        public let calls: Int
        public let callsByTool: [String: Int]
        public let callErrors: Int
        /// ISO timestamp of the last proxied call; local, no zone.
        public let lastCall: String?

        private enum CodingKeys: String, CodingKey {
            case name, state, pid, starts, error, uptimeS
            case calls, callsByTool, callErrors, lastCall
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
            state = try c.decodeIfPresent(String.self, forKey: .state) ?? ""
            pid = try c.decodeIfPresent(Int.self, forKey: .pid)
            uptimeS = try c.decodeIfPresent(Int.self, forKey: .uptimeS)
            starts = try c.decodeIfPresent(Int.self, forKey: .starts)
            error = try c.decodeIfPresent(String.self, forKey: .error)
            calls = try c.decodeIfPresent(Int.self, forKey: .calls) ?? 0
            callsByTool = try c.decodeIfPresent([String: Int].self, forKey: .callsByTool) ?? [:]
            callErrors = try c.decodeIfPresent(Int.self, forKey: .callErrors) ?? 0
            lastCall = try c.decodeIfPresent(String.self, forKey: .lastCall)
        }
    }

    public let name: String
    public let transport: String
    public let command: String?
    public let args: [String]
    public let url: String?
    public let envKeys: [String]
    public let headerKeys: [String]
    public let secretsMissing: [String]
    public let inHub: Bool
    /// Served through the mcp gateway.
    public let shared: Bool
    public let harnesses: [String]
    public let conflict: Bool
    public let variants: [[String]]
    public let disabledIn: [String]
    public let places: [Place]
    /// The `mcp status` probe result: `ok`, `error` or `unknown`.
    public let health: String
    public let healthDetail: String?
    /// The newest place mtime.
    public let updated: String?
    public let gateway: GatewayRow?

    private enum CodingKeys: String, CodingKey {
        case name, transport, command, args, url, shared, conflict, variants, places, health, updated, gateway
        case envKeys, headerKeys, secretsMissing, inHub, harnesses, disabledIn, healthDetail
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        transport = try c.decodeIfPresent(String.self, forKey: .transport) ?? "unknown"
        command = try c.decodeIfPresent(String.self, forKey: .command)
        args = try c.decodeIfPresent([String].self, forKey: .args) ?? []
        url = try c.decodeIfPresent(String.self, forKey: .url)
        envKeys = try c.decodeIfPresent([String].self, forKey: .envKeys) ?? []
        headerKeys = try c.decodeIfPresent([String].self, forKey: .headerKeys) ?? []
        secretsMissing = try c.decodeIfPresent([String].self, forKey: .secretsMissing) ?? []
        inHub = try c.decodeIfPresent(Bool.self, forKey: .inHub) ?? false
        shared = try c.decodeIfPresent(Bool.self, forKey: .shared) ?? false
        harnesses = try c.decodeIfPresent([String].self, forKey: .harnesses) ?? []
        conflict = try c.decodeIfPresent(Bool.self, forKey: .conflict) ?? false
        variants = try c.decodeIfPresent([[String]].self, forKey: .variants) ?? []
        disabledIn = try c.decodeIfPresent([String].self, forKey: .disabledIn) ?? []
        places = try c.decodeIfPresent([Place].self, forKey: .places) ?? []
        health = try c.decodeIfPresent(String.self, forKey: .health) ?? "unknown"
        healthDetail = try c.decodeIfPresent(String.self, forKey: .healthDetail)
        updated = try c.decodeIfPresent(String.self, forKey: .updated)
        gateway = try c.decodeIfPresent(GatewayRow.self, forKey: .gateway)
    }
}

/// `swe mcp tools NAME --json`: what a live handshake reported.
public struct McpToolsResult: Decodable, Sendable, Equatable {
    public struct Tool: Decodable, Sendable, Equatable {
        public let name: String
        public let description: String
    }

    /// The initialize result's `serverInfo` (name/version keys, if given).
    public struct ServerInfo: Decodable, Sendable, Equatable {
        public let name: String?
        public let version: String?
    }

    public let name: String
    public let transport: String
    public let server: ServerInfo?
    public let protocolVersion: String?
    public let tools: [Tool]
    /// Why the handshake failed; nil on success.
    public let error: String?

    private enum CodingKeys: String, CodingKey {
        case name, transport, server, tools, error, protocolVersion
    }

    public init(name: String, transport: String, server: ServerInfo? = nil,
                protocolVersion: String? = nil, tools: [Tool] = [],
                error: String? = nil) {
        self.name = name
        self.transport = transport
        self.server = server
        self.protocolVersion = protocolVersion
        self.tools = tools
        self.error = error
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        transport = try c.decodeIfPresent(String.self, forKey: .transport) ?? "unknown"
        server = try c.decodeIfPresent(ServerInfo.self, forKey: .server)
        protocolVersion = try c.decodeIfPresent(String.self, forKey: .protocolVersion)
        tools = try c.decodeIfPresent([Tool].self, forKey: .tools) ?? []
        error = try c.decodeIfPresent(String.self, forKey: .error)
    }
}

/// `swe mcp enable/disable --json`: the harnesses the call switched.
public struct McpToggleResult: Decodable, Sendable, Equatable {
    public let name: String
    /// Harnesses a disable wrote off, or an enable restored.
    public let places: [String]
    /// Harnesses an enable could not restore — the server stays marked
    /// off there. Absent from a swe before `failed_in` existed.
    public let failedIn: [String]

    private enum CodingKeys: String, CodingKey { case name, disabledIn, enabledIn, failedIn }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        places = try container.decodeIfPresent([String].self, forKey: .disabledIn)
            ?? container.decodeIfPresent([String].self, forKey: .enabledIn) ?? []
        failedIn = try container.decodeIfPresent([String].self, forKey: .failedIn) ?? []
    }
}

/// `swe mcp set --json`: whether the hub entry changed and which
/// harnesses took the new definition.
public struct McpSetResult: Decodable, Sendable, Equatable {
    public let name: String
    public let changed: Bool
    public let synced: [String]
    /// Harnesses still holding the definition from before the edit.
    public let failed: [String]

    public init(name: String, changed: Bool, synced: [String], failed: [String]) {
        self.name = name
        self.changed = changed
        self.synced = synced
        self.failed = failed
    }
}

/// `swe mcp rename --json`: where the new name landed.
public struct McpRenameResult: Decodable, Sendable, Equatable {
    public let name: String
    public let renamedFrom: String
    public let hub: Bool
    public let renamedIn: [String]
    /// Harnesses whose file still holds the old name.
    public let failedIn: [String]

    public init(name: String, renamedFrom: String, hub: Bool, renamedIn: [String], failedIn: [String]) {
        self.name = name
        self.renamedFrom = renamedFrom
        self.hub = hub
        self.renamedIn = renamedIn
        self.failedIn = failedIn
    }
}

/// Where a server row's checkbox stands across the compared harnesses.
public enum McpRowCheck: Sendable, Equatable {
    /// On in every harness it can be on in.
    case all
    /// On in some of them.
    case some
    /// On in none.
    case none

    /// A row's state from how many places it is on in, out of the places
    /// a click could have it on in. Nothing on is `none` whatever the
    /// total, so a row with nowhere to go never reads as checked.
    public static func of(on: Int, possible: Int) -> McpRowCheck {
        if on <= 0 { return .none }
        return on >= possible ? .all : .some
    }
}

/// What a table row's checkbox shows and what a click on it does, for a
/// row made of places that are each on or off and may or may not be
/// switchable (the Skills folders, the Plugins harnesses).
public struct GridRowSwitch: Sendable, Equatable {
    public let check: McpRowCheck
    /// A click turns the remaining places on; false turns the row off.
    public let turnsOn: Bool
    /// Indexes of the places a click changes, in the order given.
    public let targets: [Int]

    /// A row of places. A place that is off and can't be switched does
    /// not count against a full row, and a click only ever touches the
    /// switchable places that are not already where it is heading.
    public static func places(_ places: [(on: Bool, toggleable: Bool)]) -> GridRowSwitch {
        let check = McpRowCheck.of(on: places.filter { $0.on }.count,
                                   possible: places.filter { $0.on || $0.toggleable }.count)
        let turnsOn = check != .all
        let targets = places.indices.filter { places[$0].toggleable && places[$0].on != turnsOn }
        return GridRowSwitch(check: check, turnsOn: turnsOn, targets: targets)
    }

    /// A plugin row: `enables` and `disables` are the harnesses with
    /// that action waiting. With an enable waiting somewhere the row is
    /// not full, so a click enables exactly those; a full row disables
    /// where a harness allows it. A copy with neither action is left out
    /// of both and never targeted.
    public static func plugin(isOn: Bool, enables: [Int], disables: [Int]) -> GridRowSwitch {
        let check: McpRowCheck = !isOn ? .none : enables.isEmpty ? .all : .some
        let turnsOn = check != .all
        return GridRowSwitch(check: check, turnsOn: turnsOn, targets: turnsOn ? enables : disables)
    }
}

extension McpServer {
    /// Whether this server can be on in `harness`: it is already, or swe
    /// has something to write there. A URL server has no place in a
    /// harness that only launches commands, and a server outside the hub
    /// can only come back where a copy was kept.
    public func canBeOn(in harness: String) -> Bool {
        if harnesses.contains(harness) { return true }
        if McpServer.localOnly.contains(harness), isRemote(from: McpServer.hubSource) {
            return false
        }
        return inHub || disabledIn.contains(harness)
    }

    /// The compared harnesses this server is off in and could be on in:
    /// what checking the row's box switches on.
    public func offTargets(among compared: [String]) -> [String] {
        compared.filter { !harnesses.contains($0) && canBeOn(in: $0) }
    }

    /// The row checkbox: checked only when the server is on in every
    /// compared harness it can be on in.
    public func rowCheck(among compared: [String]) -> McpRowCheck {
        let possible = compared.filter(canBeOn(in:))
        let on = possible.filter(harnesses.contains)
        if on.isEmpty { return .none }
        return on.count == possible.count ? .all : .some
    }
}

/// What the server sheet's edit form would run: nothing, an edit of the
/// definition, a rename, or both.
public struct McpEditPlan: Sendable, Equatable {
    /// The new name, nil when it is unchanged.
    public var rename: String?
    /// The new URL, nil when it is unchanged or the server runs a command.
    public var url: String?
    /// The new command and its arguments, nil when unchanged.
    public var command: [String]?

    public init(rename: String? = nil, url: String? = nil, command: [String]? = nil) {
        self.rename = rename
        self.url = url
        self.command = command
    }

    public var isEmpty: Bool { rename == nil && url == nil && command == nil }

    /// The marker swe prints over a hidden part of a URL or argument.
    public static let mask = "***"

    /// Whether the hub entry `swe mcp set` rewrites is at a URL. The
    /// row's own `url` cannot say: it describes the first harness's
    /// copy, which for a shared server is the gateway's loopback URL
    /// while the hub holds the command.
    public static func hubIsRemote(_ server: McpServer) -> Bool {
        server.isRemote(from: McpServer.hubSource)
    }

    /// Whether the row's command or URL can stand for the hub's: the
    /// row was built from the hub entry itself (no harness holds the
    /// server), or every harness agrees and holds the same kind of
    /// definition as the hub.
    static func rowShowsHubDefinition(_ server: McpServer) -> Bool {
        if server.harnesses.isEmpty { return true }
        return server.groups.count <= 1 && (server.url != nil) == hubIsRemote(server)
    }

    /// The text an edit field starts with: the definition as swe shows
    /// it, or nothing when the row is not the hub's definition or part
    /// of it is hidden. A masked value saved back would overwrite the
    /// real one with asterisks, and a harness's gateway URL saved back
    /// would replace the hub's command.
    public static func prefill(for server: McpServer) -> String {
        guard rowShowsHubDefinition(server) else { return "" }
        let shown = server.url ?? ([server.command ?? ""] + server.args)
            .map(Self.quoted).joined(separator: " ")
        return shown.contains(mask) ? "" : shown
    }

    /// A word as the shell would need it typed, so a prefilled command
    /// line splits back into the same words.
    static func quoted(_ word: String) -> String {
        let plain = word.allSatisfy { $0.isLetter || $0.isNumber || "@%+=:,./_-".contains($0) }
        if plain && !word.isEmpty { return word }
        return "'" + word.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// The plan for the form's `name` and `definition` fields, or why
    /// they cannot be saved. An untouched or empty definition changes
    /// nothing, so a server with a hidden value can still be renamed.
    public static func make(for server: McpServer, name: String, definition: String,
                            taken: Set<String>) throws -> McpEditPlan {
        var plan = McpEditPlan()
        let name = name.trimmingCharacters(in: .whitespaces)
        if name != server.name {
            guard name.range(of: "^[A-Za-z0-9][A-Za-z0-9_.@-]*$", options: .regularExpression) != nil else {
                throw McpEditRefusal("A name is letters, digits, . _ @ - and starts with a letter or digit.")
            }
            guard !taken.contains(name) else {
                throw McpEditRefusal("A server named \(name) already exists.")
            }
            plan.rename = name
        }
        let definition = definition.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !definition.isEmpty, definition != prefill(for: server) else { return plan }
        guard !definition.contains(mask) else {
            throw McpEditRefusal("Type the full value in place of \(mask).")
        }
        if hubIsRemote(server) {
            guard let parsed = URL(string: definition), let scheme = parsed.scheme,
                  ["http", "https"].contains(scheme.lowercased()), parsed.host != nil else {
                throw McpEditRefusal("A URL starts with http:// or https://.")
            }
            plan.url = definition
        } else {
            guard let words = mcpCommandWords(definition), !words.isEmpty else {
                throw McpEditRefusal("The command has an unclosed quote.")
            }
            plan.command = words
        }
        return plan
    }
}

/// Why the edit form's fields cannot be saved, in words for the sheet.
public struct McpEditRefusal: LocalizedError, Equatable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

/// `swe mcp set-type --json`: the label left on the hub entry (nil when
/// the call cleared it, or the entry keeps its `sse` transport marker).
public struct McpSetTypeResult: Decodable, Sendable, Equatable {
    public let name: String
    public let type: String?
}

/// `swe mcp secret set --json`: where the value landed, never the value.
public struct McpSecretSetResult: Decodable, Sendable, Equatable {
    public let name: String
    public let path: String
    public let created: Bool
    /// Whether the name held a value before. Nil from a swe too old to
    /// say, which a caller undoing the set must read as true.
    public let replaced: Bool?
}

/// `swe mcp secret remove --json`.
public struct McpSecretRemoveResult: Decodable, Sendable, Equatable {
    public let name: String
    public let removed: Bool
}

/// What `swe mcp add` writes, before it becomes argv.
public struct McpServerSpec: Sendable {
    public enum Kind: Sendable {
        case command(command: String, args: [String], env: [(String, String)])
        case url(url: String, transport: String, headers: [(String, String)])
    }

    public let name: String
    public let kind: Kind

    public init(name: String, kind: Kind) {
        self.name = name
        self.kind = kind
    }

    /// The `--env`/`--url`/`--transport`/`--header` flags, which all come
    /// before any `--` tail.
    public var flags: [String] {
        switch kind {
        case .command(_, _, let env):
            return env.flatMap { ["--env=\($0.0)=\($0.1)"] }
        case .url(let url, let transport, let headers):
            var out = ["--url=\(url)"]
            if transport != "http" { out.append("--transport=\(transport)") }
            return out + headers.flatMap { ["--header=\($0.0)=\($0.1)"] }
        }
    }

    /// The bare `--` plus command and args, or empty for a URL server.
    /// Must be the last words on the line: swe never interprets them.
    public var tail: [String] {
        switch kind {
        case .command(let command, let args, _): return ["--", command] + args
        case .url: return []
        }
    }
}

/// `swe mcp add --json`: what landed where.
public struct McpAddResult: Decodable, Sendable, Equatable {
    public struct Target: Decodable, Sendable, Equatable {
        public let name: String
        public let status: String
    }
    public let name: String
    public let hub: String
    public let targets: [Target]
}

/// `swe mcp remove --json`.
public struct McpRemoveResult: Decodable, Sendable, Equatable {
    public let name: String
    public let removed: [String]
    public let absent: [String]
    /// Places whose config could not be written — the server still sits
    /// there, and a harness copy adopts back into the hub on the next
    /// sync. Absent from a swe before `failed` existed.
    public let failed: [String]

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        removed = try container.decode([String].self, forKey: .removed)
        absent = try container.decode([String].self, forKey: .absent)
        failed = try container.decodeIfPresent([String].self, forKey: .failed) ?? []
    }

    private enum CodingKeys: String, CodingKey { case name, removed, absent, failed }
}

/// A typed MCP command line as command + args, split like a shell: quotes
/// group and unquote, backslash escapes outside double quotes also escape
/// `"` `\` `$` ``` ` ``` inside, and nil means an unterminated quote.
public func mcpCommandWords(_ line: String) -> [String]? {
    var words: [String] = []
    var word = ""
    var open = false
    var quote: Character = " "
    var chars = line[line.startIndex...]
    while let ch = chars.first {
        chars = chars.dropFirst()
        switch ch {
        case "\\":
            if quote == "'" {
                // \ is literal inside '…' and doesn't escape: 'C:\' is a
                // complete quoted word ending on its own quote.
                word += "\\"
                open = true
                continue
            }
            guard let next = chars.first else { word += "\\"; continue }
            chars = chars.dropFirst()
            if quote == "\"" && next != "\"" && next != "\\"
                && next != "$" && next != "`" {
                word += "\\" + String(next)
            } else {
                word += String(next)
            }
            open = true
        case "'":
            if quote == "\"" { word += "'"; open = true; continue }
            if quote == "'" { quote = " " } else if !open || quote == " " { quote = "'" }
            open = true
        case "\"":
            if quote == "'" { word += "\""; open = true; continue }
            if quote == "\"" { quote = " " } else if quote == " " { quote = "\"" }
            open = true
        case " ", "\t", "\n":
            // The `where` on a multi-pattern case binds only the last
            // pattern, so the quote check lives in the body.
            if quote == " " {
                if open { words.append(word); word = ""; open = false }
            } else {
                word.append(ch)
                open = true
            }
        default:
            word.append(ch)
            open = true
        }
    }
    if quote != " " { return nil }
    if open { words.append(word) }
    return words
}

/// Whether a flag or key name looks like it holds a credential.
public func mcpIsSecretName(_ name: String) -> Bool {
    McpRedaction.isSecretName(name)
}

/// The ${NAME} an env var or header stores its value under:
/// SERVER_KEY in caps, runs of anything but ASCII letters and digits
/// collapsed to one `_`, and `MCP_` prepended when it would start with a
/// digit. set_secret takes only `[A-Za-z_][A-Za-z0-9_]*`, so an accented
/// server name (`café`) must not reach it as `CAFÉ_KEY`. Server-scoped,
/// so two servers can't collide.
public func mcpSecretName(server: String, key: String) -> String {
    var name = (server + "_" + key)
        .map { $0.isASCII && ($0.isLetter || $0.isNumber) ? Character($0.uppercased()) : "_" }
        .reduce(into: "") { out, ch in if ch != "_" || out.last != "_" { out.append(ch) } }
        .trimmingCharacters(in: CharacterSet(charactersIn: "_"))
    if name.isEmpty { return "MCP_SECRET" }
    if name.first?.isNumber == true { name = "MCP_" + name }
    return name
}

/// The face of an add sheet's "Add to" dropdown: "4 of 10 harnesses".
/// Counts only picks that are still in the list.
public func mcpTargetsSummary(_ targets: Set<String>, of harnesses: [String]) -> String {
    let picked = harnesses.filter(targets.contains).count
    return "\(picked) of \(harnesses.count) \(harnesses.count == 1 ? "harness" : "harnesses")"
}

@MainActor
@Observable
public final class McpModel {
    public var state: Loadable<McpInventory> { loader.state }
    /// True while `swe mcp status` probes every server.
    public private(set) var checkingHealth = false
    /// The list on show came with health, from `swe mcp status`.
    public var hasHealth: Bool {
        guard case .loaded(let inventory) = state else { return false }
        return inventory.servers.contains { $0.health != nil }
    }
    private let loader: SnapshotLoader<McpInventory>
    private var generation = 0

    /// - Parameter snapshots: where the last list is kept between launches,
    ///   as `McpInventory.saved` has it.
    public init(snapshots: (any SnapshotStoring<Snapshot<McpInventory>>)? = nil, maxAge: TimeInterval = 60,
                timeout: Duration = .seconds(60), now: @escaping @Sendable () -> Date = Date.init) {
        loader = SnapshotLoader(snapshots: snapshots, maxAge: maxAge, timeout: timeout, now: now,
                                saving: \.saved)
    }

    /// Shows the last saved list, without health, until swe answers.
    public func restore() async {
        await loader.restore()
    }

    /// Reads the list again unless this swe's is fresh; joins a read in flight.
    public func loadIfStale(swe: SweClient, for source: String) async {
        await loader.loadIfStale(from: source) { try await swe.mcpServers(health: false) }
    }

    /// The list, or with `health` the list plus a probe of every server,
    /// which takes a few seconds per unreachable URL, so it gets longer
    /// before it counts as hung.
    /// - Parameter source: the swe asked, as set in Settings; nil keeps the last one.
    public func load(swe: SweClient, for source: String? = nil, health: Bool = false) async {
        generation += 1
        let mine = generation
        if health { checkingHealth = true }
        defer { if mine == generation { checkingHealth = false } }
        await loader.load(from: source, timeout: health ? .seconds(300) : nil) {
            try await swe.mcpServers(health: health)
        }
    }

    /// The server as the latest load has it.
    public func server(named name: String) -> McpServer? {
        guard case .loaded(let inventory) = state else { return nil }
        return inventory.servers.first { $0.name == name }
    }

    /// Copies `server` from `source`: to `adding` without `--force`, so a
    /// definition that appeared there since is left alone, and to the
    /// approved `replacing` targets with it. One report for both.
    public func sync(_ server: McpServer, from source: String, adding: [String], replacing: [String],
                     dryRun: Bool, swe: SweClient) async throws -> McpSyncReport {
        var parts: [McpSyncReport] = []
        func merged() -> McpSyncReport {
            McpSyncReport(source: source, dryRun: dryRun, selected: parts.first?.selected ?? [],
                          targets: parts.flatMap(\.targets))
        }
        let runs = [(adding, false), (replacing, true)].filter { !$0.0.isEmpty }
        for (targets, overwrite) in runs {
            do {
                parts.append(try await swe.mcpSync(server: server, from: source, to: targets,
                                                   overwrite: overwrite, dryRun: dryRun))
            } catch {
                // What already happened has to stay visible.
                if parts.isEmpty { throw error }
                throw McpPartialSync(report: merged(), message: error.localizedDescription)
            }
        }
        return merged()
    }

    /// What a sync of `server` from `source` can reach: harnesses without
    /// the server (picked by default) and ones with a different definition
    /// (left for the user to opt into, since that overwrites theirs).
    /// The source choices a sheet offers: each definition group, plus the
    /// hub when mcp.json holds the server.
    public func sources(for server: McpServer) -> [String] {
        var out = server.groups.map { $0.first ?? "" }.filter { !$0.isEmpty }
        if server.inHub && !out.contains(where: McpServer.isHubSource) { out.append(McpServer.hubSource) }
        return out
    }

    public func targets(for server: McpServer, from source: String, in harnesses: [String]) -> SyncTargets {
        // A config that launches local servers only can't take a URL one.
        let harnesses = server.isRemote(from: source) ? harnesses.filter { !McpServer.localOnly.contains($0) } : harnesses
        if McpServer.isHubSource(source) {
            // The hub isn't a matrix column: every harness lacks it, and
            // one with its own copy gets it replaced only with approval.
            var missing: [String] = []
            var differing: [String] = []
            for harness in harnesses {
                if server.harnesses.contains(harness) { differing.append(harness) }
                else { missing.append(harness) }
            }
            return SyncTargets(missing: missing, differing: differing)
        }
        let sourceGroup = server.group(of: source)
        var missing: [String] = []
        var differing: [String] = []
        for harness in harnesses where harness != source {
            switch server.group(of: harness) {
            case nil: missing.append(harness)
            case sourceGroup: continue
            default: differing.append(harness)
            }
        }
        return SyncTargets(missing: missing, differing: differing)
    }
}

/// A sync whose first call went through and a later one failed.
public struct McpPartialSync: Error, LocalizedError, Sendable {
    public let report: McpSyncReport
    public let message: String
    public var errorDescription: String? { message }
}

public struct SyncTargets: Equatable, Sendable {
    public let missing: [String]
    public let differing: [String]

    /// `--force` is needed exactly when a chosen target already has its own copy.
    public func needsOverwrite(_ chosen: Set<String>) -> Bool {
        !chosen.isDisjoint(with: differing)
    }
}
