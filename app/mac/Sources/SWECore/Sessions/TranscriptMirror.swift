import Foundation

/// The read side of swe's transcript mirror (#1918): the copies swe keeps
/// under `~/.swe/data/transcripts/` of every Claude Code and Codex
/// transcript, made while the source exists so a session outlives the
/// harnesses' 30-day pruning. The bundled `swe` makes and owns the copies
/// (`src/swe/sessions/transcript_mirror.py`); the app only reads them, and
/// never writes, moves or deletes anything under the root.
///
/// Layout, as swe writes it:
///
///     transcripts/claude/<project-slug>/<uuid>.jsonl
///     transcripts/codex/<path under sessions/ or archived_sessions/>
///     transcripts/accounts/<vendor>/<account id>/<same, per SWE account home>
///     transcripts/manifest.json   {version, files: {<rel>: {harness, source, account_home, ...}}}
///
/// The manifest is the only record of which source a copy came from, and
/// it can be missing or unreadable: every read here falls back to the
/// layout, and a lost manifest never hides or changes a live session.
public struct TranscriptMirror: Sendable, Equatable {
    /// One harness home whose transcripts swe copies, and the folder under
    /// the root its copies go to.
    public struct Home: Sendable, Equatable {
        /// `claude` or `codex`.
        public var harness: String
        /// The harness home: `~/.claude`, `~/.codex`, or an SWE account's
        /// `~/.swe/accounts/<vendor>/<id>/home`. Ledger events read from
        /// its copies are filed under this path, as a live scan files them.
        public var home: URL
        /// `claude` / `codex` for the harness's own home,
        /// `accounts/<vendor>/<id>` for an account's (swe's `roots()`).
        public var prefix: String

        public init(harness: String, home: URL, prefix: String) {
            self.harness = harness
            self.home = home
            self.prefix = prefix
        }
    }

    /// One source directory and the mirror folder its files copy into:
    /// swe's `transcript_mirror.Root`.
    struct Root: Equatable {
        var harness: String
        /// Resolved, as the app resolves the paths it indexes.
        var path: String
        var prefix: String
        /// The harness home the directory sits in.
        var home: String
        /// Codex's `archived_sessions/`, which holds rollouts flat.
        var archived: Bool
    }

    /// One manifest entry: what a copy came from.
    public struct Entry: Sendable, Equatable {
        public var harness: String
        public var source: String
        /// "" for the harness's own home, else the account home's path.
        public var accountHome: String
    }

    /// One copy found under the root.
    public struct Copy: Sendable, Equatable {
        public var harness: String
        /// The copy's resolved path.
        public var path: String
        /// Its path under the root (the manifest's key).
        public var rel: String
    }

    /// The harnesses whose transcripts swe mirrors (`MIRRORED`).
    public static let harnesses = ["claude", "codex"]
    /// The env override that moves the root, for a scratch launch or a
    /// test; see docs/app-architecture.md.
    public static let rootEnvironment = "SWE_TRANSCRIPTS_DIR"

    public var root: URL
    public var homes: [Home]
    /// SWE's accounts root (`~/.swe/accounts`). Every
    /// `<vendor>/<id>/home` under it counts for liveness, registered in
    /// profiles.json or not, as swe's `account_homes_for` counts it. Nil
    /// counts none, so a mirror built for a test never sweeps real accounts.
    public var accounts: URL?

    public init(root: URL, homes: [Home], accounts: URL? = nil) {
        self.root = root
        self.homes = homes
        self.accounts = accounts
    }

    /// `<home>/.swe/data/transcripts`, or `SWE_TRANSCRIPTS_DIR` when set.
    public static func defaultRoot(home: URL,
                                   environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let path = environment[rootEnvironment], !path.isEmpty {
            return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        }
        return home.appending(path: ".swe/data/transcripts")
    }

    /// The mirror for the ledger's Claude and Codex homes. The first of
    /// each list is the harness's own home; every other one is an SWE
    /// account home (`.../accounts/<vendor>/<id>/home`), whose copies sit
    /// under `accounts/<vendor>/<id>`. `accounts` is SWE's accounts root,
    /// whose every account home counts for liveness (`pruned`).
    public static func standard(root: URL, claudeHomes: [URL], codexHomes: [URL],
                                accounts: URL? = nil) -> TranscriptMirror {
        func homes(_ harness: String, _ urls: [URL]) -> [Home] {
            urls.enumerated().map { index, url in
                Home(harness: harness, home: url, prefix: index == 0 ? harness
                     : "accounts/\(harness)/\(url.deletingLastPathComponent().lastPathComponent)")
            }
        }
        return TranscriptMirror(root: root,
                                homes: homes("claude", claudeHomes) + homes("codex", codexHomes),
                                accounts: accounts)
    }

    // MARK: - Identity

    private static let uuid = try! NSRegularExpression(
        pattern: "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        options: [.caseInsensitive])

    /// The session a Claude Code or Codex transcript belongs to, from its
    /// file name alone (swe's `search_writer.transcript_identity`): a
    /// Claude stem that is a uuid, a Codex rollout's first uuid (the thread
    /// id), lower-cased. A name with no session uuid is its own identity,
    /// its path, so two such files are never taken for one session.
    public static func identity(harness: String, path: String) -> String {
        var stem = (path as NSString).lastPathComponent
        if stem.hasSuffix(".jsonl") { stem.removeLast(".jsonl".count) }
        let range = NSRange(stem.startIndex..., in: stem)
        if harness == "codex" {
            if let match = uuid.firstMatch(in: stem, range: range),
               let found = Range(match.range, in: stem) {
                return stem[found].lowercased()
            }
            return path
        }
        if let match = uuid.firstMatch(in: stem, range: range), match.range == range {
            return stem.lowercased()
        }
        return path
    }

    // MARK: - Paths

    /// Whether anything, a dangling link included, sits at `path`: Python's
    /// `os.path.lexists`.
    public static func exists(_ path: String) -> Bool {
        guard !path.isEmpty else { return false }
        var info = stat()
        return lstat(path, &info) == 0
    }

    /// `url` with its links followed, the form the app files paths under.
    static func resolved(_ url: URL) -> String {
        url.resolvingSymlinksInPath().path
    }

    /// realpath(3), swe's `os.path.realpath`; nil when it fails.
    static func realPath(_ path: String) -> String? {
        guard let real = realpath(path, nil) else { return nil }
        defer { free(real) }
        return String(cString: real)
    }

    /// The root, resolved; nil when it is not a directory.
    var resolvedRoot: String? {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &isDirectory),
              isDirectory.boolValue else { return nil }
        return Self.resolved(root)
    }

    /// Every source directory swe mirrors, with its mirror prefix.
    var roots: [Root] {
        homes.flatMap { home -> [Root] in
            let subdirs = home.harness == "claude" ? ["projects"] : ["sessions", "archived_sessions"]
            return subdirs.map { sub in
                Root(harness: home.harness, path: Self.resolved(home.home.appending(path: sub)),
                     prefix: home.prefix, home: home.home.path, archived: sub == "archived_sessions")
            }
        }
    }

    // MARK: - Manifest

    /// `{rel: entry}`, empty when the manifest is missing or unreadable.
    /// Never throws: a lost manifest only means the layout answers.
    public func manifest() -> [String: Entry] {
        guard let data = try? Data(contentsOf: root.appending(path: "manifest.json")),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let files = object["files"] as? [String: Any] else { return [:] }
        var out: [String: Entry] = [:]
        for (rel, value) in files {
            guard let entry = value as? [String: Any] else { continue }
            out[rel] = Entry(harness: entry["harness"] as? String ?? "",
                             source: entry["source"] as? String ?? "",
                             accountHome: entry["account_home"] as? String ?? "")
        }
        return out
    }

    /// Where the copy at `rel` came from, from the layout alone: the
    /// inverse of swe's `_mirror_rel`, most likely first
    /// (`source_candidates`). A Claude prefix maps to one folder; a Codex
    /// one to `sessions/` and `archived_sessions/`, a dated path naming
    /// `sessions/` first and a flat one only `archived_sessions/`. Empty
    /// for an external transcript (its folder is a hash) or a prefix no
    /// current home has.
    func sourceCandidates(_ rel: String) -> [String] {
        var out: [String] = []
        for root in roots {
            guard rel.hasPrefix(root.prefix + "/") else { continue }
            let rest = String(rel.dropFirst(root.prefix.count + 1))
            guard !rest.isEmpty else { continue }
            if root.harness == "claude",
               rest.split(separator: "/", maxSplits: 1).first?.hasPrefix("-external-") == true {
                return []
            }
            if root.harness == "codex" {
                if rest.hasPrefix("external/") { return [] }
                if !rest.contains("/") && !root.archived { continue }
            }
            out.append(root.path + "/" + rest)
        }
        return out
    }

    /// The source path a copy stands for (swe's `_copy_key`): the
    /// manifest's record, else the layout's answer, preferring a candidate
    /// `known` says the caller already holds a row for. Nil when neither
    /// can say.
    func sourceKey(for copy: Copy, manifest: [String: Entry], known: (String) -> Bool) -> String? {
        if let entry = manifest[copy.rel], entry.harness == copy.harness, !entry.source.isEmpty {
            return entry.source
        }
        let candidates = sourceCandidates(copy.rel)
        if candidates.count > 1, let held = candidates.first(where: known) { return held }
        return candidates.first
    }

    /// The home whose tree holds the copy at `rel`, by the longest prefix.
    func home(for rel: String, harness: String) -> Home? {
        homes.filter { $0.harness == harness && rel.hasPrefix($0.prefix + "/") }
            .max { $0.prefix.count < $1.prefix.count }
    }

    // MARK: - Walk

    /// Every copy under the root, in swe's walk order (`_copy_trees`, then
    /// `_walk`): each harness's own folder, then each account folder under
    /// `accounts/<harness>/`. Hidden names and `subagents/` folders are
    /// pruned, a linked folder is not entered (a linked tree, or one under
    /// a linked `accounts/`, is not read at all), and a copy outside the
    /// root (a link pointing elsewhere) is left out.
    public func copies() -> [Copy] {
        guard let real = resolvedRoot else { return [] }
        var trees: [(String, String)] = Self.harnesses.map { ($0, real + "/" + $0) }
        for harness in Self.harnesses {
            let accounts = real + "/accounts/" + harness
            for name in Self.names(in: accounts) where !name.hasPrefix(".") {
                if Self.kind(accounts + "/" + name) == .directory {
                    trees.append((harness, accounts + "/" + name))
                }
            }
        }
        var out: [Copy] = []
        // A linked tree, or one behind a link, reads as absent, as swe's
        // `_copy_trees` and the listing read it: a link must never file
        // one account's copies twice.
        for (harness, tree) in trees where Self.resolved(URL(fileURLWithPath: tree)) == tree {
            for path in Self.walk(tree) where path.hasPrefix(real + "/") {
                out.append(Copy(harness: harness, path: path, rel: String(path.dropFirst(real.count + 1))))
            }
        }
        return out
    }

    private enum Kind { case file, directory, linkedFile, linkedDirectory, other }

    private static func kind(_ path: String) -> Kind {
        var info = stat()
        guard lstat(path, &info) == 0 else { return .other }
        switch info.st_mode & S_IFMT {
        case S_IFDIR: return .directory
        case S_IFREG: return .file
        case S_IFLNK:
            var target = stat()
            guard stat(path, &target) == 0 else { return .other }
            return target.st_mode & S_IFMT == S_IFDIR ? .linkedDirectory : .linkedFile
        default: return .other
        }
    }

    private static func names(in directory: String) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: directory)) ?? [])
            .sorted { $0.utf8.lexicographicallyPrecedes($1.utf8) }
    }

    /// `*.jsonl` files under `directory`: a folder's files by name, then
    /// its subfolders by name.
    static func walk(_ directory: String) -> [String] {
        var found: [String] = []
        func visit(_ directory: String) {
            var subdirs: [String] = []
            for name in names(in: directory) {
                let path = directory + "/" + name
                switch kind(path) {
                case .directory:
                    if !name.hasPrefix("."), name != "subagents" { subdirs.append(path) }
                case .linkedDirectory:
                    continue
                case .file where name.hasSuffix(".jsonl") && !name.hasPrefix("."):
                    found.append(path)
                case .linkedFile where name.hasSuffix(".jsonl") && !name.hasPrefix("."):
                    found.append(resolved(URL(fileURLWithPath: path)))
                default:
                    continue
                }
            }
            subdirs.forEach(visit)
        }
        if kind(directory) == .directory { visit(directory) }
        return found
    }

    // MARK: - Account homes

    /// Every account home on disk for `harness`, by name: each
    /// `<accounts>/<harness>/<id>/home` that is a directory once links are
    /// followed, hidden ids included (swe's `account_homes_for`, a
    /// `glob("*/home")` kept by `is_dir()`).
    func accountHomes(_ harness: String) -> [URL] {
        guard let accounts else { return [] }
        let base = accounts.appending(path: harness)
        return Self.names(in: base.path).compactMap { name in
            let home = base.appending(path: name).appending(path: "home")
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: home.path, isDirectory: &isDirectory),
                  isDirectory.boolValue else { return nil }
            return home
        }
    }

    /// `(harness, identity)` of every transcript in an account home that
    /// `homes` leaves out (one no profile registers). The scan never reads
    /// such a home but swe's pass does, so its sessions are live for
    /// `pruned`. Each is walked as swe walks a home (`_walk` over its
    /// resolved `projects/`, `sessions/` and `archived_sessions/`).
    func liveInUnlistedHomes() -> Set<SessionIdentity> {
        var live = Set<SessionIdentity>()
        for harness in Self.harnesses {
            let listed = Set(homes.filter { $0.harness == harness }.map { Self.resolved($0.home) })
            let subdirs = harness == "claude" ? ["projects"] : ["sessions", "archived_sessions"]
            for home in accountHomes(harness) where !listed.contains(Self.resolved(home)) {
                for sub in subdirs {
                    for path in Self.walk(Self.resolved(home.appending(path: sub))) {
                        live.insert(SessionIdentity(harness: harness, path: path))
                    }
                }
            }
        }
        return live
    }

    // MARK: - Pruned sessions

    /// A copy whose source the harness deleted, with the source path its
    /// rows and events stand for.
    public struct Pruned: Sendable, Equatable {
        public var copy: Copy
        /// The gone source path (the manifest's, else the layout's).
        public var source: String
        /// `(harness, identity)` of the source.
        public var identity: SessionIdentity
    }

    /// A session's identity: its harness and `identity(harness:path:)`.
    public struct SessionIdentity: Hashable, Sendable, Comparable {
        public var harness: String
        public var id: String

        public init(harness: String, id: String) {
            self.harness = harness
            self.id = id
        }

        public init(harness: String, path: String) {
            self.init(harness: harness, id: TranscriptMirror.identity(harness: harness, path: path))
        }

        /// Python's tuple order: code point by code point, which UTF-8
        /// byte order matches.
        public static func < (a: Self, b: Self) -> Bool {
            if a.harness != b.harness { return a.harness.utf8.lexicographicallyPrecedes(b.harness.utf8) }
            return a.id.utf8.lexicographicallyPrecedes(b.id.utf8)
        }
    }

    /// The copies of sessions no live file carries and whose source is
    /// gone: swe's candidate step, shared by search and the ledger. `live`
    /// is the caller's enumeration of `homes`; the sessions of every
    /// account home `homes` leaves out count too (`liveInUnlistedHomes`),
    /// as swe lists every account home on disk. A copy named for a live
    /// session is dropped before any lookup (a copy keeps its source's
    /// file name), then again once its source path is known. A copy with
    /// no known source, or one that points inside the root, is left alone.
    public func pruned(live: Set<SessionIdentity>, known: (String) -> Bool = { _ in false }) -> [Pruned] {
        guard let real = resolvedRoot else { return [] }
        let found = copies()
        guard !found.isEmpty else { return [] }
        let live = live.union(liveInUnlistedHomes())
        let manifest = manifest()
        // swe records `realpath` forms, which keep `/private`; the app's
        // resolution drops it. A key inside the root in either form is the
        // mirror's own file, never a source.
        let inside = Set([real, Self.realPath(root.path) ?? real].map { $0 + "/" })
        var out: [Pruned] = []
        for copy in found {
            if live.contains(SessionIdentity(harness: copy.harness, path: copy.path)) { continue }
            guard let key = sourceKey(for: copy, manifest: manifest, known: known),
                  !inside.contains(where: key.hasPrefix), !Self.exists(key) else { continue }
            let identity = SessionIdentity(harness: copy.harness, path: key)
            if live.contains(identity) { continue }
            out.append(Pruned(copy: copy, source: key, identity: identity))
        }
        return out
    }
}
