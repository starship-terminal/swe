import Foundation
import Testing
@testable import SWECore

/// Stands in for the `swe` process: records what it was asked and replies
/// with canned output.
final class FakeRunner: CommandRunning, @unchecked Sendable {
    var reply: CommandResult
    private(set) var calls: [[String]] = []
    /// stdin per call, matching `calls` by index; nil when none was sent.
    private(set) var inputs: [Data?] = []

    init(_ reply: CommandResult) { self.reply = reply }

    func run(_ arguments: [String]) async throws -> CommandResult {
        calls.append(arguments)
        inputs.append(nil)
        return reply
    }

    func run(_ arguments: [String], input: Data?) async throws -> CommandResult {
        calls.append(arguments)
        inputs.append(input)
        return reply
    }
}

func fixture(_ name: String) throws -> Data {
    let url = try #require(Bundle.module.url(
        forResource: name, withExtension: "json", subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

@Suite struct SweClientTests {
    @Test func listDecodesHarnessesAndTheirLimits() async throws {
        let runner = FakeRunner(.init(status: 0, stdout: try fixture("list-usage"), stderr: ""))
        let harnesses = try await SweClient(runner: runner).harnesses(withUsage: true)

        #expect(harnesses.map(\.name) == ["claude", "codex", "gemini"])
        let claude = try #require(harnesses.first)
        #expect(claude.state == .starred)
        #expect(claude.aliases == ["cc"])
        let limit = try #require(claude.rateLimit)
        #expect(limit.status == .ok)
        #expect(limit.usedPercent == 28)
        #expect(limit.remainingPercent == 72)
        #expect(limit.window == "5h")
        #expect(limit.resetAt == Date(timeIntervalSince1970: 1_791_718_000))
        #expect(limit.fetchedAt != nil)
        #expect(harnesses[1].rateLimit == nil)
        #expect(harnesses[2].state == .active)
    }

    @Test func usageIsOnlyRequestedWhenAsked() async throws {
        let runner = FakeRunner(.init(status: 0, stdout: try fixture("list-usage"), stderr: ""))
        let client = SweClient(runner: runner)
        _ = try await client.harnesses(withUsage: false)
        _ = try await client.harnesses(withUsage: true)
        #expect(runner.calls == [["list", "--json"], ["list", "--usage", "--json"]])
    }

    @Test func aNewerSchemaIsRefusedRatherThanMisread() async throws {
        let body = Data(#"{"schema": 2, "data": {"harnesses": []}}"#.utf8)
        let client = SweClient(runner: FakeRunner(.init(status: 0, stdout: body, stderr: "")))
        await #expect(throws: SweError.unsupportedSchema(2)) {
            _ = try await client.harnesses(withUsage: false)
        }
    }

    @Test func aFailingCommandSurfacesItsStderr() async throws {
        let client = SweClient(runner: FakeRunner(.init(status: 1, stdout: Data(), stderr: "boom\n")))
        await #expect(throws: SweError.commandFailed(status: 1, message: "boom")) {
            _ = try await client.harnesses(withUsage: false)
        }
    }

    @Test func aMissingSweReadsAsNotInstalled() async throws {
        // `/usr/bin/env swe` answers 127 with env's own message.
        let runner = FakeRunner(.init(status: 127, stdout: Data(),
                                      stderr: "env: swe: No such file or directory\n"))
        await #expect(throws: SweError.notInstalled("swe")) {
            _ = try await SweClient(runner: runner).harnesses(withUsage: false)
        }
    }

    @Test func aMissingSweOnAPlainRunReadsAsNotInstalled() async throws {
        // Doctor's Run on an init row goes through runPlain, not call.
        let runner = FakeRunner(.init(status: 127, stdout: Data(),
                                      stderr: "env: /gone/swe: No such file or directory\n"))
        await #expect(throws: SweError.notInstalled("/gone/swe")) {
            try await SweClient(runner: runner).runInit()
        }
    }

    @Test func a127WithoutEnvsMessageIsNotNotInstalled() async throws {
        // swe itself failing hard keeps its real message.
        let runner = FakeRunner(.init(status: 127, stdout: Data(), stderr: "swe blew up"))
        await #expect(throws: SweError.commandFailed(status: 127, message: "swe blew up")) {
            _ = try await SweClient(runner: runner).harnesses(withUsage: false)
        }
    }

    @Test func notInstalledPointsAtSettingsNotPip() {
        let text = SweError.notInstalled("swe").localizedDescription
        #expect(text.contains("Settings"))
        #expect(!text.contains("pip"))
    }

    @Test func anOlderSweWithoutJSONIsNamedAsSuch() async throws {
        // A swe from before --json existed rejects the flag in plain text.
        let old = Data("\u{1B}[31m  Unexpected args: --json\u{1B}[0m\n".utf8)
        let client = SweClient(runner: FakeRunner(.init(status: 0, stdout: old, stderr: "")))
        await #expect(throws: SweError.jsonUnsupported) {
            _ = try await client.harnesses(withUsage: false)
        }
        let viaStderr = SweClient(runner: FakeRunner(.init(status: 1, stdout: Data(), stderr: "Unknown flag: --json")))
        await #expect(throws: SweError.jsonUnsupported) {
            _ = try await viaStderr.harnesses(withUsage: false)
        }
    }

    @Test func aFailureWithOnlyStdoutStillSaysWhy() async throws {
        // Older swe printed refusals to stdout.
        let client = SweClient(runner: FakeRunner(.init(status: 1, stdout: Data("\u{1B}[31mUnknown flag: --bogus\u{1B}[0m\n".utf8), stderr: "")))
        await #expect(throws: SweError.commandFailed(status: 1, message: "Unknown flag: --bogus")) {
            _ = try await client.harnesses(withUsage: false)
        }
    }

    @Test func errorMessagesCarryNoTerminalColourCodes() async throws {
        let client = SweClient(runner: FakeRunner(.init(status: 1, stdout: Data(), stderr: "\u{1B}[31mboom\u{1B}[0m")))
        await #expect(throws: SweError.commandFailed(status: 1, message: "boom")) {
            _ = try await client.harnesses(withUsage: false)
        }
    }

    @Test func nonJSONOutputIsReportedAsSuch() async throws {
        let client = SweClient(runner: FakeRunner(.init(status: 0, stdout: Data("Unknown flag".utf8), stderr: "")))
        // Not a --json refusal (no "--json" in it): unreadable output,
        // quoted back.
        await #expect(throws: SweError.malformedOutput("Unknown flag")) {
            _ = try await client.harnesses(withUsage: false)
        }
    }

    @Test func aMalformedRateLimitDegradesThatRowOnly() async throws {
        // codex's reading is not an object at all; gemini's has garbage
        // inside it. Neither may cost the page or the good row.
        let doc = Data(#"""
        {"schema": 1, "data": {"harnesses": [
         {"name": "claude", "command": "claude", "description": "d",
          "aliases": [], "tags": [], "state": "starred",
          "rate_limit": {"status": "ok", "used_percent": 28,
                         "remaining_percent": 72, "limit_reached": false,
                         "window": "5h"}},
         {"name": "codex", "command": "codex", "description": "d",
          "aliases": [], "tags": [], "state": "active",
          "rate_limit": "oops"},
         {"name": "gemini", "command": "gemini", "description": "d",
          "aliases": [], "tags": [], "state": "active",
          "rate_limit": {"status": "bogus", "used_percent": "x",
                         "remaining_percent": null, "limit_reached": "?"}}]}}
        """#.utf8)
        let harnesses = try await SweClient(
            runner: FakeRunner(.init(status: 0, stdout: doc, stderr: "")))
            .harnesses(withUsage: false)
        #expect(harnesses.map(\.name) == ["claude", "codex", "gemini"])
        #expect(harnesses[0].rateLimit?.usedPercent == 28)
        // Not an object: no reading at all rather than a made-up one.
        #expect(harnesses[1].rateLimit == nil)
        let degraded = try #require(harnesses[2].rateLimit)
        #expect(degraded.status == .timeout)   // unreadable -> the unread state
        #expect(degraded.usedPercent == 0)
        #expect(degraded.remainingPercent == 100)
        #expect(degraded.limitReached == false)
    }

    @Test func anExtremeUsedPercentCannotTrapTheDerivedFallback() async throws {
        // Int.min is a valid Int, so it decodes — and 100 - Int.min traps
        // if the missing remaining_percent is derived without clamping.
        // try? catches decode errors, never arithmetic.
        let doc = Data(#"""
        {"schema": 1, "data": {"harnesses": [
         {"name": "claude", "command": "claude", "description": "d",
          "aliases": [], "tags": [], "state": "active",
          "rate_limit": {"status": "ok", "used_percent": -9223372036854775808,
                         "limit_reached": false}},
         {"name": "codex", "command": "codex", "description": "d",
          "aliases": [], "tags": [], "state": "active",
          "rate_limit": {"status": "ok", "used_percent": 250}}]}}
        """#.utf8)
        let harnesses = try await SweClient(
            runner: FakeRunner(.init(status: 0, stdout: doc, stderr: "")))
            .harnesses(withUsage: false)
        #expect(harnesses.map(\.name) == ["claude", "codex"])
        #expect(harnesses[0].rateLimit?.remainingPercent == 100)
        #expect(harnesses[1].rateLimit?.remainingPercent == 0)
        #expect(harnesses[1].rateLimit?.limitReached == true)
    }

    @Test func aBrokenHarnessRowIsDroppedNotFatal() async throws {
        let doc = Data(#"""
        {"schema": 1, "data": {"harnesses": [
         {"name": "claude", "command": "claude", "description": "d",
          "aliases": [], "tags": [], "state": "active"},
         {"name": 5},
         {"name": "codex", "command": "codex", "description": "d",
          "aliases": [], "tags": [], "state": "active"}]}}
        """#.utf8)
        let harnesses = try await SweClient(
            runner: FakeRunner(.init(status: 0, stdout: doc, stderr: "")))
            .harnesses(withUsage: false)
        #expect(harnesses.map(\.name) == ["claude", "codex"])
    }

    @Test func aNewerSchemaIsNamedEvenWhenThePayloadIsBad() async throws {
        // The single-pass decode throws on the payload, but the header is
        // still readable, so the answer is the schema mismatch, not junk.
        let doc = Data(#"{"schema": 2, "data": "x"}"#.utf8)
        let client = SweClient(runner: FakeRunner(.init(status: 0, stdout: doc, stderr: "")))
        await #expect(throws: SweError.unsupportedSchema(2)) {
            _ = try await client.harnesses(withUsage: false)
        }
    }
}

@Suite struct SweLocationTests {
    @Test func aChosenPathWinsOverTheBundledOneAndPATH() {
        // A real executable: anything missing, non-executable, or planted
        // under a world-writable folder falls back instead of running.
        #expect(SweLocation.executable(override: "/bin/ls", bundled: "/A/swe") == "/bin/ls")
        #expect(SweLocation.executable(override: "/bin/ls", bundled: nil) == "/bin/ls")
    }

    @Test func blankOrMissingOverrideMeansTheBundledSwe() {
        #expect(SweLocation.executable(override: nil, bundled: "/A/swe") == "/A/swe")
        #expect(SweLocation.executable(override: "  ", bundled: "/A/swe") == "/A/swe")
    }

    @Test func withoutABundledSweItIsWhateverPATHFinds() {
        #expect(SweLocation.executable(override: nil, bundled: nil) == "swe")
        #expect(SweLocation.executable(override: "  ", bundled: nil) == "swe")
    }

    @Test func theBundledSweIsFoundOnlyWhenItIsExecutable() throws {
        let app = FileManager.default.temporaryDirectory
            .appending(path: "SweLocationTests-\(UUID().uuidString).app")
        defer { try? FileManager.default.removeItem(at: app) }
        let bin = app.appending(path: "Contents/Resources/swe/bin")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        try Data("""
            <?xml version="1.0" encoding="UTF-8"?>
            <plist version="1.0"><dict>
            <key>CFBundleIdentifier</key><string>com.example.swe-location-tests</string>
            </dict></plist>
            """.utf8).write(to: app.appending(path: "Contents/Info.plist"))
        let shim = bin.appending(path: "swe")
        try Data("#!/bin/sh\n".utf8).write(to: shim)
        let bundle = try #require(Bundle(url: app))
        #expect(SweLocation.bundled(in: bundle) == nil)

        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: shim.path)
        #expect(SweLocation.bundled(in: bundle) == shim.path)
    }
}

@Suite struct McpSecretSetTests {
    private let reply = CommandResult(
        status: 0,
        stdout: Data(#"{"schema":1,"data":{"name":"MY_TOKEN","path":"/x/.api_keys","created":true}}"#.utf8),
        stderr: "")

    @Test func theValueTravelsOnStdinNeverArgv() async throws {
        let runner = FakeRunner(reply)
        try await SweClient(runner: runner).mcpSecretSet(name: "MY_TOKEN", value: "s3cr3t-fixture")
        #expect(runner.calls == [["mcp", "secret", "set", "MY_TOKEN", "--json"]])
        #expect(runner.inputs == [Data("s3cr3t-fixture\n".utf8)])
        // Nowhere on argv.
        #expect(runner.calls.flatMap { $0 }.allSatisfy { !$0.contains("s3cr3t") })
    }

    @Test func aRefusalRaisesCommandFailed() async throws {
        let runner = FakeRunner(.init(status: 1, stdout: Data(),
                                      stderr: "empty value; pipe one in"))
        await #expect(throws: SweError.commandFailed(status: 1, message: "empty value; pipe one in")) {
            try await SweClient(runner: runner).mcpSecretSet(name: "A", value: "")
        }
    }

    @Test func theResultSaysWhetherAValueWasReplaced() async throws {
        let fresh = CommandResult(status: 0, stdout: Data(
            #"{"schema":1,"data":{"name":"A","path":"/x","created":false,"replaced":false}}"#.utf8), stderr: "")
        let set = try await SweClient(runner: FakeRunner(fresh)).mcpSecretSet(name: "A", value: "v")
        #expect(set.replaced == false)
        // An older swe says nothing, which must not read as "new".
        let old = try await SweClient(runner: FakeRunner(reply)).mcpSecretSet(name: "MY_TOKEN", value: "v")
        #expect(old.replaced == nil)
    }

    @Test func removeCallsSecretRemove() async throws {
        let runner = FakeRunner(CommandResult(status: 0, stdout: Data(
            #"{"schema":1,"data":{"name":"A","removed":true}}"#.utf8), stderr: ""))
        #expect(try await SweClient(runner: runner).mcpSecretRemove(name: "A"))
        #expect(runner.calls == [["mcp", "secret", "remove", "A", "--json"]])
    }
}

@Suite struct McpServerSecretsMissingTests {
    private func decode(_ json: String) throws -> McpServer {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(McpServer.self, from: Data(json.utf8))
    }

    @Test func secretsMissingDecodes() throws {
        let server = try decode(#"{"name":"a","transport":"stdio","harnesses":["claude"],"secrets_missing":["FOO","BAR"]}"#)
        #expect(server.secretsMissing == ["FOO", "BAR"])
    }

    @Test func anOlderSweWithoutTheFieldDecodesAsNone() throws {
        let server = try decode(#"{"name":"a","transport":"stdio","harnesses":["claude"]}"#)
        #expect(server.secretsMissing == [])
    }
}

@Suite struct McpAddRemoveTests {
    private func swe() -> (SweClient, FakeRunner) {
        let runner = FakeRunner(.init(
            status: 0,
            stdout: Data(#"{"schema":1,"data":{"name":"s","hub":"added","targets":[],"removed":[],"absent":[]}}"#.utf8),
            stderr: ""))
        return (SweClient(runner: runner), runner)
    }

    @Test func addCommandArgv() async throws {
        let (swe, runner) = swe()
        _ = try await swe.mcpAdd(McpServerSpec(
            name: "gh", kind: .command(command: "npx", args: ["-y", "gh-mcp"],
                                       env: [("TOK", "${TOK}")])),
                               to: ["claude", "codex"], force: true)
        #expect(runner.calls == [[
            "mcp", "add", "gh", "--env=TOK=${TOK}", "--json",
            "--to=claude,codex", "--force", "--", "npx", "-y", "gh-mcp"]])
        // The `--` tail is last: swe reads everything after it verbatim.
        let call = try #require(runner.calls.first)
        let dash = try #require(call.firstIndex(of: "--"))
        #expect(Array(call[dash...]) == ["--", "npx", "-y", "gh-mcp"])
        #expect(call[..<dash].contains("--json"))
    }

    @Test func addUrlArgvWithSseAndHeaders() async throws {
        let (swe, runner) = swe()
        _ = try await swe.mcpAdd(McpServerSpec(
            name: "r", kind: .url(url: "https://x/mcp", transport: "sse",
                                  headers: [("Authorization", "Bearer ${TOK}")])),
                               to: [], force: false)
        #expect(runner.calls == [[
            "mcp", "add", "r", "--url=https://x/mcp", "--transport=sse",
            "--header=Authorization=Bearer ${TOK}", "--json"]])
        #expect(!runner.calls[0].contains("--"))
    }

    @Test func removeArgv() async throws {
        let (swe, runner) = swe()
        _ = try await swe.mcpRemove(name: "s", from: ["swe", "claude"])
        #expect(runner.calls == [["mcp", "remove", "s", "--from=swe,claude", "--json"]])
    }

    @Test func setTypeArgvAndReply() async throws {
        let runner = FakeRunner(CommandResult(status: 0, stdout: Data(
            #"{"schema":1,"data":{"name":"s","type":"dev"}}"#.utf8), stderr: ""))
        let result = try await SweClient(runner: runner).mcpSetType("dev", server: "s")
        #expect(runner.calls == [["mcp", "set-type", "s", "dev", "--json"]])
        #expect(result.name == "s")
        #expect(result.type == "dev")
    }

    @Test func setArgvPutsTheCommandAfterTheDoubleDash() async throws {
        let runner = FakeRunner(CommandResult(status: 0, stdout: Data(
            #"{"schema":1,"data":{"name":"s","changed":true,"synced":["claude"],"failed":[]}}"#.utf8), stderr: ""))
        let client = SweClient(runner: runner)
        let result = try await client.mcpSet(server: "s", url: "https://h.example:8108/mcp", command: nil)
        _ = try await client.mcpSet(server: "s", url: nil, command: ["npx", "-y", "srv", "--json"])
        #expect(runner.calls == [
            ["mcp", "set", "s", "--json", "--url=https://h.example:8108/mcp"],
            ["mcp", "set", "s", "--json", "--", "npx", "-y", "srv", "--json"],
        ])
        #expect(result == McpSetResult(name: "s", changed: true, synced: ["claude"], failed: []))
    }

    /// A harness that kept the old definition exits 1 with its document.
    @Test func setReplyDecodesOnAPartialFailure() async throws {
        let runner = FakeRunner(CommandResult(status: 1, stdout: Data(
            #"{"schema":1,"data":{"name":"s","changed":true,"synced":[],"failed":["codex"]}}"#.utf8), stderr: ""))
        let result = try await SweClient(runner: runner).mcpSet(server: "s", url: "https://h.example/mcp", command: nil)
        #expect(result.failed == ["codex"])
    }

    @Test func renameArgvAndReply() async throws {
        let runner = FakeRunner(CommandResult(status: 0, stdout: Data(
            #"{"schema":1,"data":{"name":"new","renamed_from":"old","hub":true,"renamed_in":["claude"],"failed_in":[]}}"#.utf8), stderr: ""))
        let result = try await SweClient(runner: runner).mcpRename("old", to: "new")
        #expect(runner.calls == [["mcp", "rename", "old", "new", "--json"]])
        #expect(result == McpRenameResult(name: "new", renamedFrom: "old", hub: true,
                                          renamedIn: ["claude"], failedIn: []))
    }

    @Test func rowCheckIsFullOnlyWhenEveryPossibleHarnessIsOn() {
        let compared = ["claude", "claude-desktop", "cursor"]
        let everywhere = McpServer(name: "s", harnesses: compared, inHub: true)
        #expect(everywhere.rowCheck(among: compared) == .all)
        #expect(everywhere.offTargets(among: compared).isEmpty)

        let partial = McpServer(name: "s", harnesses: ["cursor"], inHub: true, disabledIn: ["claude"])
        #expect(partial.rowCheck(among: compared) == .some)
        #expect(partial.offTargets(among: compared) == ["claude", "claude-desktop"])

        // In the hub, in no harness: every box is clickable, none is on.
        let hubOnly = McpServer(name: "s", harnesses: [], inHub: true)
        #expect(hubOnly.rowCheck(among: compared) == .none)
        #expect(hubOnly.offTargets(among: compared) == compared)

        // A URL server has no place in Claude Desktop, so the other two
        // being on is the whole row.
        let remote = McpServer(name: "s", transport: "http", url: "https://h.example/mcp",
                               harnesses: ["claude", "cursor"], inHub: true,
                               remoteIn: ["claude", "cursor", "swe"])
        #expect(!remote.canBeOn(in: "claude-desktop"))
        #expect(remote.rowCheck(among: compared) == .all)

        // Outside the hub only a kept copy can come back.
        let harnessOnly = McpServer(name: "s", harnesses: ["claude"], disabledIn: ["cursor"])
        #expect(harnessOnly.offTargets(among: compared) == ["cursor"])
        #expect(harnessOnly.rowCheck(among: compared) == .some)
    }

    /// The Skills and Plugins rows count places: nothing on is never a
    /// checked row, even with nowhere to turn it on.
    @Test func rowCheckFromCounts() {
        #expect(McpRowCheck.of(on: 0, possible: 0) == .none)
        #expect(McpRowCheck.of(on: 0, possible: 3) == .none)
        #expect(McpRowCheck.of(on: 2, possible: 3) == .some)
        #expect(McpRowCheck.of(on: 3, possible: 3) == .all)
    }

    /// A Skills row: folders on, off, and one that is off and blocked.
    @Test func skillRowTurnsOnOnlyWhatIsLeftThenTurnsOff() {
        // on, off-switchable, off-blocked, on-but-fixed
        let mixed = GridRowSwitch.places([(true, true), (false, true), (false, false), (true, false)])
        #expect(mixed.check == .some)
        #expect(mixed.turnsOn)
        #expect(mixed.targets == [1])

        // The blocked folder stays off and does not keep the row from full.
        let full = GridRowSwitch.places([(true, true), (true, true), (false, false), (true, false)])
        #expect(full.check == .all)
        #expect(!full.turnsOn)
        #expect(full.targets == [0, 1])

        let off = GridRowSwitch.places([(false, true), (false, false)])
        #expect(off.check == .none)
        #expect(off.turnsOn)
        #expect(off.targets == [0])

        // Nothing switchable: no targets, so the box sits dimmed.
        #expect(GridRowSwitch.places([(false, false)]).targets.isEmpty)
        #expect(GridRowSwitch.places([(true, false)]) == GridRowSwitch(check: .all, turnsOn: false, targets: []))
    }

    /// A Plugins row: a click enables only the harnesses with an enable
    /// waiting; a full row runs the disables. A copy with no on/off
    /// action is in neither list.
    @Test func pluginRowPicksEnablesThenDisables() {
        let mixed = GridRowSwitch.plugin(isOn: true, enables: [0], disables: [0, 1])
        #expect(mixed == GridRowSwitch(check: .some, turnsOn: true, targets: [0]))

        let full = GridRowSwitch.plugin(isOn: true, enables: [], disables: [0, 1])
        #expect(full == GridRowSwitch(check: .all, turnsOn: false, targets: [0, 1]))

        let off = GridRowSwitch.plugin(isOn: false, enables: [0, 1], disables: [])
        #expect(off == GridRowSwitch(check: .none, turnsOn: true, targets: [0, 1]))

        // Installed with no on/off action anywhere: on, nothing to run.
        let fixed = GridRowSwitch.plugin(isOn: true, enables: [], disables: [])
        #expect(fixed == GridRowSwitch(check: .all, turnsOn: false, targets: []))
    }

    @Test func editPlanOnlyCarriesWhatChanged() throws {
        let remote = McpServer(name: "design-systems", transport: "http",
                               url: "https://h.example:8110/api/mcp", harnesses: ["claude"], inHub: true,
                               remoteIn: ["claude", "swe"])
        let same = try McpEditPlan.make(for: remote, name: "design-systems",
                                        definition: "https://h.example:8110/api/mcp", taken: [])
        #expect(same.isEmpty)
        let both = try McpEditPlan.make(for: remote, name: " constellations ",
                                        definition: "https://h.example:8108/api/mcp", taken: ["other"])
        #expect(both == McpEditPlan(rename: "constellations", url: "https://h.example:8108/api/mcp"))
        #expect(throws: McpEditRefusal.self) {
            try McpEditPlan.make(for: remote, name: "other", definition: "", taken: ["other"])
        }
        #expect(throws: McpEditRefusal.self) {
            try McpEditPlan.make(for: remote, name: "-bad", definition: "", taken: [])
        }
        #expect(throws: McpEditRefusal.self) {
            try McpEditPlan.make(for: remote, name: "design-systems", definition: "h.example:8108", taken: [])
        }
    }

    @Test func editPlanSplitsACommandAndNeverSavesAMask() throws {
        let local = McpServer(name: "gh", command: "npx", args: ["-y", "a b"], harnesses: [], inHub: true)
        #expect(McpEditPlan.prefill(for: local) == "npx -y 'a b'")
        let plan = try McpEditPlan.make(for: local, name: "gh", definition: "uvx 'my server' --flag", taken: [])
        #expect(plan.command == ["uvx", "my server", "--flag"])
        #expect(plan.url == nil)

        // A hidden part leaves the field empty; empty keeps the definition
        // and still lets the name change.
        let masked = McpServer(name: "k", transport: "http", url: "https://h.example/mcp?key=***",
                               harnesses: [], inHub: true, remoteIn: ["swe"])
        #expect(McpEditPlan.prefill(for: masked).isEmpty)
        #expect(try McpEditPlan.make(for: masked, name: "k2", definition: "", taken: [])
                == McpEditPlan(rename: "k2"))
        #expect(throws: McpEditRefusal.self) {
            try McpEditPlan.make(for: masked, name: "k", definition: "https://h.example/mcp?key=***", taken: [])
        }
    }

    /// A shared server's row is its harness copy, the gateway URL, while
    /// the hub holds the command: the form must edit a command and must
    /// not offer the gateway URL back as the definition.
    @Test func editPlanFollowsTheHubNotTheHarnessCopy() throws {
        let shared = McpServer(name: "gh", transport: "http", url: "http://127.0.0.1:8478/mcp/gh",
                               harnesses: ["claude"], inHub: true, remoteIn: ["claude"])
        #expect(!McpEditPlan.hubIsRemote(shared))
        #expect(McpEditPlan.prefill(for: shared).isEmpty)
        let plan = try McpEditPlan.make(for: shared, name: "gh", definition: "npx -y gh-mcp", taken: [])
        #expect(plan == McpEditPlan(command: ["npx", "-y", "gh-mcp"]))
        #expect(try McpEditPlan.make(for: shared, name: "gh", definition: "", taken: []).isEmpty)

        // Harnesses that disagree: none of them speaks for the hub.
        let split = McpServer(name: "s", transport: "http", url: "https://a.example/mcp",
                              harnesses: ["claude", "cursor"], conflict: true,
                              variants: [["claude"], ["cursor"]], inHub: true,
                              remoteIn: ["claude", "cursor", "swe"])
        #expect(McpEditPlan.prefill(for: split).isEmpty)
    }

    /// A cleared label (or a live `sse` marker swe leaves alone) decodes nil.
    @Test func setTypeReplyDecodesNilType() async throws {
        let runner = FakeRunner(CommandResult(status: 0, stdout: Data(
            #"{"schema":1,"data":{"name":"s","type":null}}"#.utf8), stderr: ""))
        let result = try await SweClient(runner: runner).mcpSetType("other", server: "s")
        #expect(result.type == nil)
    }

    @Test func secretNameDerivesAShellNameScopedToTheServer() {
        #expect(mcpSecretName(server: "my-srv", key: "X-Api-Key") == "MY_SRV_X_API_KEY")
        #expect(mcpSecretName(server: "1password", key: "Authorization") == "MCP_1PASSWORD_AUTHORIZATION")
        // set_secret takes ASCII names only: accents collapse like any
        // other non-alphanumeric run.
        #expect(mcpSecretName(server: "café", key: "clé") == "CAF_CL")
        #expect(mcpSecretName(server: "é", key: "ü") == "MCP_SECRET")
    }

    /// The app hides what swe hides (src/swe/mcp/secrets.py's
    /// _SECRET_FLAG_RE): cookies, signatures, bearer values, any passw-,
    /// and the British spelling of authorization, plus a --headers value.
    @Test func secretNamesMatchSwesRule() {
        for name in ["--session-cookie", "--signature", "--bearer", "--passwd", "--passwords",
                     "--authorisation", "--authorization", "--keyfile", "--api_key", "--auth",
                     "X_SESSION_COOKIE", "--headers"] {
            #expect(mcpIsSecretName(name), "\(name)")
        }
        for name in ["--port", "--verbose", "--url", "--name"] {
            #expect(!mcpIsSecretName(name), "\(name)")
        }
        let shown = McpRedaction.args(["--cookie", "sid=abc", "--signature=xyz",
                                       "--headers", "X-Token: t0k", "--port", "8080"])
        #expect(shown == ["--cookie", "…", "--signature=…", "--headers", "X-Token:…", "--port", "8080"])
    }

    @Test func addToSummaryCountsPicksStillListed() {
        let harnesses = ["claude", "codex", "cursor"]
        #expect(mcpTargetsSummary(["claude", "codex"], of: harnesses) == "2 of 3 harnesses")
        #expect(mcpTargetsSummary([], of: harnesses) == "0 of 3 harnesses")
        // A pick that left the list isn't counted.
        #expect(mcpTargetsSummary(["claude", "gone"], of: harnesses) == "1 of 3 harnesses")
        #expect(mcpTargetsSummary(["claude"], of: ["claude"]) == "1 of 1 harness")
    }

    @Test func commandLineSplitsLikeAShell() {
        #expect(mcpCommandWords("npx -y @a/b --flag=x") == ["npx", "-y", "@a/b", "--flag=x"])
        #expect(mcpCommandWords("run 'two words'") == ["run", "two words"])
        #expect(mcpCommandWords("--x=\"a b\" y") == ["--x=a b", "y"])
        #expect(mcpCommandWords("a\"b\"c") == ["abc"])
        #expect(mcpCommandWords("a\\ b") == ["a b"])
        #expect(mcpCommandWords("\"\\$v \\`w\\`\"") == ["$v `w`"])
        #expect(mcpCommandWords("\"\\x\"") == ["\\x"])
        #expect(mcpCommandWords("run 'C:\\'") == ["run", "C:\\"])
        #expect(mcpCommandWords("'a\\'") == ["a\\"])
        #expect(mcpCommandWords("'open") == nil)
        #expect(mcpCommandWords("\"open") == nil)
    }

    @Test func theHubIsARealSyncSource() async {
        let model = await McpModel()
        let hubOnly = McpServer(name: "s", harnesses: [], inHub: true)
        #expect(await model.sources(for: hubOnly) == ["swe"])
        let shared = McpServer(name: "s", harnesses: ["claude"],
                               variants: [["claude"]], inHub: true)
        // The harness group wins; the hub is the default only when no
        // harness defines the server.
        #expect(await model.sources(for: shared) == ["claude", "swe"])
        let targets = await model.targets(for: hubOnly, from: "swe",
                                          in: ["claude", "codex"])
        #expect(targets.missing == ["claude", "codex"])
        #expect(targets.differing == [])
        let targets2 = await model.targets(for: shared, from: "swe",
                                           in: ["claude", "codex"])
        #expect(targets2.missing == ["codex"])
        #expect(targets2.differing == ["claude"])
        // "quiver", the hub's name before the rename, reads the same.
        let legacy = await model.targets(for: shared, from: "quiver", in: ["claude", "codex"])
        #expect(legacy.missing == ["codex"])
        #expect(legacy.differing == ["claude"])
    }

    @Test func aRemoteServerIsNotOfferedToClaudeDesktop() async {
        let model = await McpModel()
        let remote = McpServer(name: "r", transport: "http", url: "https://mcp.example.com/mcp",
                               harnesses: [], inHub: true)
        let local = McpServer(name: "l", harnesses: [], inHub: true)
        let all = ["claude", "claude-desktop", "codex"]
        #expect(await model.targets(for: remote, from: "swe", in: all).missing == ["claude", "codex"])
        #expect(await model.targets(for: local, from: "swe", in: all).missing == all)
        // Local in claude, remote in the hub: the copied definition decides.
        let mixed = McpServer(name: "m", harnesses: ["claude"], variants: [["claude"]], inHub: true,
                              remoteIn: ["swe", "quiver"])
        #expect(await model.targets(for: mixed, from: "claude", in: all).missing == ["claude-desktop", "codex"])
        #expect(await model.targets(for: mixed, from: "swe", in: all).missing == ["codex"])
        // An older swe lists the hub as "quiver" alone.
        let older = McpServer(name: "m", harnesses: ["claude"], variants: [["claude"]], inHub: true,
                              remoteIn: ["quiver"])
        #expect(await model.targets(for: older, from: "swe", in: all).missing == ["codex"])
    }

    @Test func inHubDefaultsFalseForAnOlderSwe() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let server = try decoder.decode(
            McpServer.self,
            from: Data(#"{"name":"a","transport":"stdio","harnesses":["claude"]}"#.utf8))
        #expect(server.inHub == false)
    }
}

@Suite struct LoginShellPathTests {
    @Test func pathIsReadFromTheLoginShellEnvironment() {
        let env = "HOME=/Users/k\nPATH=/opt/homebrew/bin:/usr/bin\nSHELL=/bin/zsh\n"
        #expect(LoginShell.path(fromEnvOutput: env) == "/opt/homebrew/bin:/usr/bin")
    }

    @Test func missingPathFallsBackToNil() {
        #expect(LoginShell.path(fromEnvOutput: "HOME=/Users/k\n") == nil)
    }
}

/// swe's harness discover/add envelopes for the Harnesses page sheets.
@Suite struct HarnessAddTests {
    private let findingsJSON = #"""
    "findings": [
     {"name": "newcli", "command": "newcli", "path": "/opt/homebrew/bin/newcli",
      "confidence": "high", "status": "new"},
     {"name": "guess", "command": "guess", "path": "/h/.guess",
      "confidence": "low", "status": "new"},
     {"name": "claude", "command": "claude", "path": "/opt/hb/claude",
      "confidence": "high", "status": "registered"}]
    """#

    private func discovery(applied: Bool = false) -> CommandResult {
        let appliedJSON = applied
            ? #""applied": {"added": ["newcli", "guess"]},"# : ""
        let doc = "{\"schema\": 1, \"data\": {" + appliedJSON + findingsJSON + "}}"
        return CommandResult(status: 0, stdout: Data(doc.utf8), stderr: "")
    }

    @Test func discoverArgvAndDefaultTicking() async throws {
        let runner = ScriptedRunner([(["harness", "discover"], discovery(applied: true))])
        let swe = SweClient(runner: runner)
        let preview = try await swe.discoverHarnesses()
        #expect(runner.calls == [["harness", "discover", "--json"]])
        let (rows, checked) = HarnessDiscovery.selection(preview.findings)
        #expect(rows.map(\.name) == ["newcli", "guess"])
        #expect(checked == ["newcli"])
        _ = try await swe.registerHarnesses(["newcli", "guess"])
        #expect(runner.calls.last ==
            ["harness", "discover", "--apply", "newcli", "guess", "--json"])
    }

    @Test func aRefusedNameStillReturnsTheSkippedReason() async throws {
        // swe exits 1 when a named pick is refused, with the reason in the
        // body; the sheet needs that body rather than a bare failure.
        let doc = #"{"schema": 1, "data": {"applied": {"added": [], "skipped": "#
            + #"[{"name": "newcli", "reason": "already registered"}], "refused": true},"#
            + findingsJSON + "}}"
        let runner = ScriptedRunner([(["harness", "discover"],
            CommandResult(status: 1, stdout: Data(doc.utf8), stderr: ""))])
        let reply = try await SweClient(runner: runner).registerHarnesses(["newcli"])
        #expect(reply.applied?.added == [])
        #expect(reply.applied?.skipped.map(\.name) == ["newcli"])
    }

    @Test func addHarnessArgvOmitsEmptyOptionals() async throws {
        let ok = CommandResult(status: 0, stdout: Data(#"""
        {"schema": 1, "data": {"name": "t", "command": "t", "action": "added",
         "installed": true, "aliases": ["x"], "tags": ["agentic"]}}
        """#.utf8), stderr: "")
        let runner = ScriptedRunner([(["add"], ok)])
        let swe = SweClient(runner: runner)
        let reply = try await swe.addHarness(name: "t", command: "t",
            description: "desc", aliases: ["x"], tags: ["agentic"])
        #expect(reply.installed && reply.action == "added")
        #expect(runner.calls.first ==
            ["add", "t", "t", "--description=desc", "--aliases=x", "--tags=agentic",
             "--no-replace", "--json"])
        _ = try await swe.addHarness(name: "t2", command: "t2", description: "",
                                     aliases: [], tags: [])
        #expect(runner.calls.last == ["add", "t2", "t2", "--no-replace", "--json"])
    }
}
