import Foundation
import Testing
@testable import SWEApp

/// Two pieces of AppModel that feed other processes: the `launchctl list`
/// pids Compute spares from cleanup, and the shell line a PR session pastes
/// into a terminal.
@Suite struct AppModelCommandsTests {

    // MARK: launchctl list

    @Test func readsRunningJobPidsAndSkipsHeaderAndStoppedJobs() {
        let text = """
            PID\tStatus\tLabel
            -\t0\tcom.apple.SafariHistoryServiceAgent
            512\t0\tcom.apple.Finder
            8811\t-9\tcom.example.crashy
            -\t78\tcom.example.stopped
            """
        #expect(AppModel.launchctlListPIDs(text) == [512, 8811])
    }

    @Test func neverSparesPidZeroOneOrNegatives() {
        let text = "PID\tStatus\tLabel\n0\t0\tzero\n1\t0\tlaunchd\n-5\t0\tneg\n2\t0\ttwo\n"
        #expect(AppModel.launchctlListPIDs(text) == [2])
    }

    @Test func toleratesEmptyGarbageAndCRLF() {
        #expect(AppModel.launchctlListPIDs("").isEmpty)
        #expect(AppModel.launchctlListPIDs("\n\n\t\t\n").isEmpty)
        #expect(AppModel.launchctlListPIDs("launchctl: not a pid\nabc\t0\tx\n99999999999\t0\toverflow").isEmpty)
        #expect(AppModel.launchctlListPIDs("PID\tStatus\tLabel\r\n42\t0\tcom.example.a\r\n43\t0\tb\r\n") == [42, 43])
    }

    @Test func aLabelThatLooksLikeAPidIsNotRead() {
        #expect(AppModel.launchctlListPIDs("-\t0\t777").isEmpty)
    }

    // MARK: PR session line

    @Test func quotesThePathAndEachWordOfTheCommand() {
        #expect(AppModel.prSessionLine(path: "/Users/me/.swe/worktrees/o/r/pr-7", command: "claude --model opus")
                == "cd '/Users/me/.swe/worktrees/o/r/pr-7' && 'claude' '--model' 'opus'")
    }

    @Test func anEmptyCommandOnlyChangesDirectory() {
        #expect(AppModel.prSessionLine(path: "/tmp/x", command: "") == "cd '/tmp/x'")
        #expect(AppModel.prSessionLine(path: "/tmp/x", command: "  \t ") == "cd '/tmp/x'")
    }

    @Test func aLeadingTildeExpands() {
        let home = NSHomeDirectory()
        #expect(AppModel.prSessionLine(path: "/tmp/x", command: "~/bin/agent run")
                == "cd '/tmp/x' && '\(home)/bin/agent' 'run'")
    }

    /// The line runs as the argv it shows: a quote, `;` or `$()` in the
    /// checkout path or the registry command never runs as shell.
    @Test func metacharactersStayDataWhenTheLineRuns() async throws {
        let scratch = try TempDir()
        let checkout = scratch.url.appending(path: "it's; touch pwned1; $(touch pwned2)")
        try FileManager.default.createDirectory(at: checkout, withIntermediateDirectories: true)
        let line = AppModel.prSessionLine(
            path: checkout.path, command: "/bin/echo $(touch pwned3) ;touch pwned4 `id`")

        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = ["-c", line]
        task.currentDirectoryURL = scratch.url
        // A small pipe, read once the shell has ended: the wait parks on
        // the exit rather than blocking a pool thread.
        let out = Pipe()
        task.standardOutput = out
        try await runToExit(task)
        let printed = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)

        #expect(task.terminationStatus == 0)
        #expect(printed == "$(touch pwned3) ;touch pwned4 `id`\n")
        let made = try FileManager.default.contentsOfDirectory(atPath: scratch.url.path)
            + FileManager.default.contentsOfDirectory(atPath: checkout.path)
        #expect(!made.contains { $0.hasPrefix("pwned") })
    }
}
