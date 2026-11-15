import Foundation
import Testing
@testable import SWECore

/// `git -C <dir> <args>`, parked on its exit (`runToExit`) so no thread
/// is held while it runs. The developer's global and system git config
/// stay out (hooks, templates, `commit.gpgsign` would make the commits
/// fail or prompt), and `HOME` is the temp folder so nothing reads or
/// writes the real one.
private func runGit(_ args: [String], in dir: URL) async throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
    process.arguments = ["-C", dir.path] + args
    process.environment = ProcessInfo.processInfo.environment.merging([
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": dir.path,
    ]) { _, isolated in isolated }
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    try await runToExit(process)
    try #require(!process.isRunning && process.terminationStatus == 0)
}

private func git(_ args: String..., in dir: URL) async throws {
    try await runGit(args, in: dir)
}

@Suite struct McpServerRepoTests {
    let fm = FileManager.default
    /// Each test gets its own instance, so its folder goes when the test
    /// ends, after any permissions it changed are put back.
    private let scratch: TempDir
    init() throws { scratch = try TempDir() }

    private func tempDir() throws -> URL { scratch.url }

    @discardableResult
    private func mkdir(_ path: String, in root: URL, marker: String? = nil) throws -> URL {
        let url = root.appending(path: path)
        try fm.createDirectory(at: url, withIntermediateDirectories: true)
        if let marker { try "".write(to: url.appending(path: marker), atomically: true, encoding: .utf8) }
        return url
    }

    // MARK: - scan

    @Test func groupFoldersHoldTheServers() throws {
        let root = try tempDir()
        try mkdir("official/linear-mcp", in: root, marker: "package.json")
        try mkdir("custom/fathom-mcp", in: root)
        let servers = try McpServerRepo.scan(root: root, fileManager: fm)
        #expect(servers.map(\.name) == ["fathom-mcp", "linear-mcp"])
        #expect(servers.map(\.group) == ["custom", "official"])
    }

    @Test func aFolderWithAMarkerIsAServerItself() throws {
        let root = try tempDir()
        for marker in McpServerRepo.serverMarkers {
            let marked = try mkdir("server-\(marker.filter(\.isLetter))", in: root)
            try "".write(to: marked.appending(path: marker), atomically: true, encoding: .utf8)
            try mkdir("\(marked.lastPathComponent)/nested", in: root)
        }
        let servers = try McpServerRepo.scan(root: root, fileManager: fm)
        // Every child had a marker, so there are no group folders: the
        // "nested" folders inside them are never reached.
        #expect(servers.count == McpServerRepo.serverMarkers.count)
        #expect(servers.allSatisfy { $0.group == nil })
    }

    @Test func checkoutsAtTheRootHaveNoGroup() throws {
        let root = try tempDir()
        try mkdir("mcp-hn", in: root, marker: ".git")
        try mkdir("grouped/one", in: root)
        let servers = try McpServerRepo.scan(root: root, fileManager: fm)
        #expect(servers.map(\.name) == ["mcp-hn", "one"])
        #expect(servers.map(\.group) == [nil, "grouped"])
    }

    @Test func hiddenFoldersAndFilesAreIgnored() throws {
        let root = try tempDir()
        try mkdir(".secret", in: root)
        try mkdir("group/.hidden-server", in: root)
        try mkdir("real", in: root, marker: ".git")
        try "x".write(to: root.appending(path: "notes.txt"), atomically: true, encoding: .utf8)
        try "x".write(to: root.appending(path: "group/file.txt"), atomically: true, encoding: .utf8)
        let servers = try McpServerRepo.scan(root: root, fileManager: fm)
        #expect(servers.map(\.name) == ["real"])
    }

    @Test func sortingPutsUngroupedFirstThenGroupsThenNames() throws {
        let root = try tempDir()
        try mkdir("solo", in: root, marker: ".git")
        try mkdir("zebra/zed", in: root)
        try mkdir("apple/bee", in: root)
        try mkdir("apple/Ann", in: root)
        let servers = try McpServerRepo.scan(root: root, fileManager: fm)
        #expect(servers.map(\.name) == ["solo", "Ann", "bee", "zed"])
        #expect(servers.map(\.group) == [nil, "apple", "apple", "zebra"])
    }

    @Test func aMissingRootScansAsEmpty() throws {
        let root = fm.temporaryDirectory.appending(path: "no-such-\(UUID().uuidString)")
        #expect(try McpServerRepo.scan(root: root, fileManager: fm) == [])
    }

    @Test func aFileAsRootThrows() throws {
        let root = try tempDir()
        let file = root.appending(path: "not-a-folder")
        try "x".write(to: file, atomically: true, encoding: .utf8)
        #expect {
            try McpServerRepo.scan(root: file, fileManager: fm)
        } throws: { error in
            error.localizedDescription == "\(file.path) is a file, not a folder."
        }
    }

    @Test(skippedAsRoot) func anUnreadableRootThrows() throws {
        let root = try tempDir()
        try fm.setAttributes([.posixPermissions: 0o000], ofItemAtPath: root.path)
        defer { try? fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: root.path) }
        #expect {
            try McpServerRepo.scan(root: root, fileManager: fm)
        } throws: { error in
            error.localizedDescription.hasPrefix("Couldn't read \(root.path): ")
        }
    }

    // MARK: - hubUsage

    private func repo(_ name: String, at path: String) -> McpRepoServer {
        McpRepoServer(name: name, path: URL(fileURLWithPath: path))
    }

    private func hubServer(_ name: String, command: String? = nil, args: [String] = []) -> McpServer {
        McpServer(name: name, command: command, args: args, harnesses: ["claude"])
    }

    @Test func hubUsageMatchesCommandAndArgsInsideTheFolder() {
        let servers = [repo("linear-mcp", at: "/srv/mcp/linear-mcp"),
                       repo("fathom-mcp", at: "/srv/mcp/fathom-mcp"),
                       repo("direct", at: "/srv/mcp/direct")]
        let hub = [
            hubServer("linear", command: "node", args: ["/srv/mcp/linear-mcp/build/index.js"]),
            hubServer("fathom", command: "uv", args: ["--directory", "/srv/mcp/fathom-mcp", "run", "fathom-mcp"]),
            hubServer("script", command: "/srv/mcp/direct/run.sh"),
            hubServer("line", command: "node /srv/mcp/linear-mcp/serve.js"),
        ]
        let usage = McpServerRepo.hubUsage(servers: servers, hub: hub)
        #expect(usage["/srv/mcp/linear-mcp"] == ["linear", "line"])
        #expect(usage["/srv/mcp/fathom-mcp"] == ["fathom"])
        #expect(usage["/srv/mcp/direct"] == ["script"])
    }

    @Test func aPrefixFolderNameDoesNotMatch() {
        let servers = [repo("mcp-hn", at: "/srv/mcp-hn"), repo("mcp-hnx", at: "/srv/mcp-hnx")]
        let hub = [hubServer("hn", command: "node", args: ["/srv/mcp-hnx/index.js"])]
        let usage = McpServerRepo.hubUsage(servers: servers, hub: hub)
        #expect(usage["/srv/mcp-hn"] == nil)
        #expect(usage["/srv/mcp-hnx"] == ["hn"])
    }

    @Test func pathsWithSpacesMatchQuotedEscapedAndBare() {
        let servers = [repo("my server", at: "/srv/mcp/my server")]
        let hub = [
            hubServer("quoted", command: "node \"/srv/mcp/my server/index.js\""),
            hubServer("bare", command: "/srv/mcp/my server/bin/run"),
            hubServer("escaped", command: "node /srv/mcp/my\\ server/serve.js"),
            hubServer("arg", command: "node", args: ["/srv/mcp/my server/index.js"]),
        ]
        let usage = McpServerRepo.hubUsage(servers: servers, hub: hub)
        #expect(usage["/srv/mcp/my server"] == ["quoted", "bare", "escaped", "arg"])
    }

    @Test func aLiteralBackslashInAnArgIsKept() {
        // An args element is already an argv entry, so `my\server` is a
        // real backslash, not an escape, and never matches `myserver`.
        let servers = [repo("odd", at: "/srv/my\\server"), repo("merged", at: "/srv/myserver")]
        let hub = [hubServer("odd", command: "run", args: ["/srv/my\\server/index.js"])]
        let usage = McpServerRepo.hubUsage(servers: servers, hub: hub)
        #expect(usage["/srv/my\\server"] == ["odd"])
        #expect(usage["/srv/myserver"] == nil)
    }

    @Test func fileURLsAreDecodedBeforeComparing() {
        let servers = [repo("my server", at: "/srv/my server"), repo("one", at: "/srv/one")]
        let hub = [
            hubServer("encoded", command: "open", args: ["file:///srv/my%20server/index.js"]),
            hubServer("plain", command: "open", args: ["file:///srv/one"]),
            hubServer("relative", command: "run", args: ["one", "./one", "srv/one"]),
        ]
        let usage = McpServerRepo.hubUsage(servers: servers, hub: hub)
        #expect(usage["/srv/my server"] == ["encoded"])
        #expect(usage["/srv/one"] == ["plain"])
    }

    // MARK: - grouped

    @Test func commandServersGoLocalAndUrlServersGoRemote() {
        let servers = [repo("linear-mcp", at: "/srv/mcp/linear-mcp")]
        let hub = [
            hubServer("linear", command: "node", args: ["/srv/mcp/linear-mcp/build/index.js"]),
            McpServer(name: "notion", transport: "http", url: "https://mcp.notion.com/mcp",
                      harnesses: ["claude"]),
            hubServer("hn", command: "npx", args: ["-y", "hn-mcp"]),
        ]
        let (local, remote) = McpServerRepo.grouped(servers: hub, folders: servers)
        // An npx server is Local too: it runs on this machine. Only the
        // one in the servers folder carries folders (its checkout tag).
        #expect(local.map(\.name) == ["hn", "linear"])
        #expect(remote.map(\.name) == ["notion"])
        guard case .server(_, let folders) = local[1], case .server(_, let none) = local[0] else {
            Issue.record("hn and linear should be server rows")
            return
        }
        #expect(folders.map(\.name) == ["linear-mcp"])
        #expect(none.isEmpty)
    }

    @Test func anUnusedFolderJoinsLocalOnceWithNoServer() {
        let servers = [repo("unused", at: "/srv/mcp/unused")]
        let hub = [hubServer("notion", command: "npx", args: ["-y", "notion-mcp"])]
        let (local, remote) = McpServerRepo.grouped(servers: hub, folders: servers)
        #expect(local == [.server(hub[0], folders: []), .folder(servers[0])])
        #expect(remote.isEmpty)
    }

    @Test func aFolderBackingTwoServersSitsUnderEachWithoutARowOfItsOwn() {
        let shared = repo("shared-mcp", at: "/srv/mcp/shared-mcp")
        let hub = [
            hubServer("one", command: "node", args: ["/srv/mcp/shared-mcp/one.js"]),
            hubServer("two", command: "node", args: ["/srv/mcp/shared-mcp/two.js"]),
        ]
        let (local, remote) = McpServerRepo.grouped(servers: hub, folders: [shared])
        #expect(local.map(\.name) == ["one", "two"])
        #expect(remote.isEmpty)
        #expect(local.allSatisfy {
            if case .server(_, let folders) = $0 { return folders == [shared] }
            return false
        })
    }

    @Test func severalFoldersBackOneServerAndLocalSortsByName() {
        let a = repo("a-mcp", at: "/srv/mcp/a-mcp")
        let b = repo("b-mcp", at: "/srv/mcp/b-mcp")
        let unused = repo("zzz-unused", at: "/srv/mcp/zzz-unused")
        let hub = [hubServer("aaa-server", command: "node",
                             args: ["/srv/mcp/b-mcp/index.js", "/srv/mcp/a-mcp/helper.js"])]
        let (local, remote) = McpServerRepo.grouped(servers: hub, folders: [a, b, unused])
        // "aaa-server" first, then the unused folder, all in Local.
        #expect(local.map(\.name) == ["aaa-server", "zzz-unused"])
        #expect(remote.isEmpty)
        guard case .server(_, let folders) = local[0] else {
            Issue.record("aaa-server should be a server row")
            return
        }
        #expect(folders.map(\.name) == ["a-mcp", "b-mcp"])
    }

    @Test func aFilteredOutServerStillClaimsItsFolder() {
        let folder = repo("local-mcp", at: "/srv/mcp/local-mcp")
        let hidden = hubServer("hidden", command: "node", args: ["/srv/mcp/local-mcp/index.js"])
        let shown = hubServer("shown", command: "npx", args: ["-y", "x"])
        // The page's filter keeps `hidden` off the list, but it still runs
        // from the folder: claiming it from the full inventory stops the
        // folder coming back as a "Not in the hub" row.
        let (local, remote) = McpServerRepo.grouped(servers: [shown], folders: [folder],
                                                  usageFrom: [shown, hidden])
        #expect(local.map(\.name) == ["shown"])
        #expect(remote.isEmpty)
    }

    // MARK: - GitState.parse

    @Test func parseReadsTrackingCountsAndChanges() {
        let state = GitState.parse(porcelain: "## main...origin/main [ahead 1, behind 2]\n M src/a.swift\n")
        #expect(state == GitState(branch: "main", ahead: 1, behind: 2, hasChanges: true))
    }

    @Test func parseReadsBranchWithoutUpstream() {
        let state = GitState.parse(porcelain: "## feature\n")
        #expect(state == GitState(branch: "feature"))
    }

    @Test func parseReadsDetachedHead() {
        let state = GitState.parse(porcelain: "## HEAD (no branch)\n")
        #expect(state == GitState(branch: nil))
    }

    @Test func parseReadsARepoWithNoCommits() {
        let state = GitState.parse(porcelain: "## No commits yet on main\n?? new-file\n")
        #expect(state == GitState(branch: "main", hasChanges: true))
    }

    @Test func parseCountsAnyFurtherLineAsAChange() {
        let state = GitState.parse(porcelain: "## main\n?? untracked\n")
        #expect(state.hasChanges)
        #expect(!GitState.parse(porcelain: "## main\n").hasChanges)
    }

    // MARK: - gitState

    @Test func gitStateReadsARealRepo() async throws {
        let dir = try tempDir()
        try await git("init", in: dir)
        try await git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "x", in: dir)
        var state = await McpServerRepo.gitState(of: dir)
        #expect(state.isRepo)
        #expect(state.branch != nil)
        #expect(!state.hasChanges)
        try "x".write(to: dir.appending(path: "untracked"), atomically: true, encoding: .utf8)
        state = await McpServerRepo.gitState(of: dir)
        #expect(state.isRepo)
        #expect(state.hasChanges)
    }

    @Test func gitStateNeverRewritesTheIndex() async throws {
        let dir = try tempDir()
        try await git("init", in: dir)
        let file = dir.appending(path: "tracked.txt")
        try "x".write(to: file, atomically: true, encoding: .utf8)
        try await git("add", "tracked.txt", in: dir)
        try await git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "x", in: dir)
        // Stale stat data: a plain status would refresh it and write a
        // new index (a new inode, through index.lock).
        try fm.setAttributes([.modificationDate: Date(timeIntervalSince1970: 1_000_000_000)],
                             ofItemAtPath: file.path)
        let index = dir.appending(path: ".git/index").path
        let before = try fm.attributesOfItem(atPath: index)[.systemFileNumber] as? Int
        let state = await McpServerRepo.gitState(of: dir)
        #expect(state.isRepo)
        #expect(!state.hasChanges)
        let after = try fm.attributesOfItem(atPath: index)[.systemFileNumber] as? Int
        #expect(before != nil && before == after)
    }

    @Test func aPlainFolderIsNotARepo() async throws {
        let dir = try tempDir()
        let state = await McpServerRepo.gitState(of: dir)
        #expect(state == GitState(isRepo: false))
    }

    @Test func aFolderInsideAnotherRepoIsStillNotARepo() async throws {
        let dir = try tempDir()
        try await git("init", in: dir)
        try await git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "x", in: dir)
        let sub = try mkdir("server", in: dir)
        try "x".write(to: sub.appending(path: "notes.txt"), atomically: true, encoding: .utf8)
        // Without its own .git, git status would report the parent repo.
        let state = await McpServerRepo.gitState(of: sub)
        #expect(state == GitState(isRepo: false))
    }

    // MARK: - model

    @Test @MainActor func loadPublishesServersWithGit() async throws {
        let root = try tempDir()
        let checkout = try mkdir("group/linear-mcp", in: root)
        try await git("init", in: checkout)
        let model = McpServerRepoModel(root: root)
        await model.load()
        #expect(model.servers.map(\.name) == ["linear-mcp"])
        #expect(model.servers.first?.group == "group")
        #expect(model.servers.first?.git?.isRepo == true)
        #expect(!model.isLoading)
    }

    @Test @MainActor func everyServerGetsAGitState() async throws {
        let root = try tempDir()
        for index in 0..<9 {
            let checkout = try mkdir("group/server-\(index)", in: root)
            if index.isMultiple(of: 2) { try await git("init", in: checkout) }
        }
        let model = McpServerRepoModel(root: root)
        await model.load()
        // Past the four-at-a-time batch, a load used to drop checks, so
        // some rows never left the spinner.
        #expect(model.servers.count == 9)
        #expect(model.servers.allSatisfy { $0.git != nil })
        #expect(model.servers.filter { $0.git?.isRepo == true }.count == 5)
    }

    @Test @MainActor func aReadFailureSurfacesAsAnError() async throws {
        let root = try tempDir()
        let file = root.appending(path: "not-a-folder")
        try "x".write(to: file, atomically: true, encoding: .utf8)
        let model = McpServerRepoModel(root: file)
        await model.load()
        #expect(model.error == "\(file.path) is a file, not a folder.")
        #expect(model.servers.isEmpty)
        #expect(!model.isLoading)
    }

    // MARK: - CodeEditors

    @Test func installedEditorsKeepTheCandidateOrder() {
        let installed = CodeEditors.installed { url in
            ["Zed.app", "Cursor.app", "Xcode.app"].contains(url.lastPathComponent)
        }
        #expect(installed.map(\.name) == ["Cursor", "Zed", "Xcode"])
        // The system Applications folder wins over the user's.
        #expect(installed.first?.appURL.path == "/Applications/Cursor.app")
    }

    @Test func anEditorOnlyInTheHomeFolderStillCounts() throws {
        // A scratch home with a real app folder in it, read through the
        // real file check: nothing here looks at the user's own home.
        let home = try TempDir()
        try FileManager.default.createDirectory(
            at: home.path("Applications/VSCodium.app"), withIntermediateDirectories: true)
        let installed = CodeEditors.installed(home: home.url) { url in
            url.path.hasPrefix(home.url.path) && FileManager.default.fileExists(atPath: url.path)
        }
        #expect(installed.map(\.name) == ["VSCodium"])
        #expect(installed.first?.appURL.standardizedFileURL
                    == home.path("Applications/VSCodium.app").standardizedFileURL)
    }
}
