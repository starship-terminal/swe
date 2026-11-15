import Foundation
import Testing

/// The helpers other tests lean on to stay off the real home.
@Suite struct TestSupportTests {
    @Test(.timeLimit(.minutes(1))) func aScratchSuiteIsWrittenUnderTheTempFolder() async {
        let defaults = TestDefaults("support")
        defaults.set(42, forKey: "answer")
        #expect(UserDefaults(suiteName: defaults.suite)?.integer(forKey: "answer") == 42)
        // The plist lands in this process's temp folder, not beside the
        // user's own preferences. cfprefsd writes it in its own time (a
        // flush doesn't wait for the file under load), so wait for it.
        let file = URL(filePath: defaults.suite + ".plist")
        #expect(file.deletingLastPathComponent().path == TestDefaults.folder.path)
        let appeared = await fileAppears(file.lastPathComponent, in: TestDefaults.folder)
        // Held to here: its deinit deletes the plist being waited for.
        withExtendedLifetime(defaults) {}
        #expect(appeared)
        let preferences = FileManager.default.homeDirectoryForCurrentUser
            .appending(path: "Library/Preferences/\(file.lastPathComponent)")
        #expect(!FileManager.default.fileExists(atPath: preferences.path))
    }

    /// A wait that ignores cancellation still ends at the timeout: a
    /// task group used to sit on it until it finished, which was never.
    @MainActor
    @Test(.timeLimit(.minutes(1))) func withinTimeoutDoesNotWaitForAWaitThatIgnoresCancellation() async {
        let never = HoldGate()
        let stuck = Task { try? await never.pass() }
        // A task's value can't be cancelled from outside the task.
        let answered = await withinTimeout(.milliseconds(50)) {
            await stuck.value
            return true
        }
        #expect(!answered)
        never.open()
    }

    @MainActor
    @Test(.timeLimit(.minutes(1))) func withinTimeoutAnswersWithTheWaitWhenItFinishesFirst() async {
        #expect(await withinTimeout { true })
        #expect(await withinTimeout { false } == false)
    }

    /// Cancelling the caller answers false at once, not at the timeout,
    /// even while its wait ignores cancellation.
    @MainActor
    @Test(.timeLimit(.minutes(1))) func withinTimeoutAnswersFalseWhenTheCallerIsCancelled() async {
        let never = HoldGate()
        let stuck = Task { try? await never.pass() }
        let entered = CountGate()
        let caller = Task { @MainActor in
            await withinTimeout(.seconds(3600)) {
                await entered.bump()
                await stuck.value
                return true
            }
        }
        #expect(await entered.reach(1))
        caller.cancel()
        #expect(await caller.value == false)
        never.open()
    }

    /// A child that outlives the bound is killed along with what it
    /// started, and the wait reports it as an issue.
    @Test(.timeLimit(.minutes(1))) func waitForExitKillsAChildTreeThatOutlivesTheTimeout() async throws {
        let temp = try TempDir()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        // Written whole, then moved into place, so the file never shows half a pid.
        let file = temp.path("grandchild").path
        process.arguments = ["-c", "sleep 30 & echo $! > '\(file).tmp' && mv '\(file).tmp' '\(file)'; wait"]
        let child = try ChildProcess(starting: process)
        // The grandchild exists once its pid is written.
        #expect(await fileAppears("grandchild", in: temp.url))
        let text = (try? String(contentsOf: temp.path("grandchild"), encoding: .utf8)) ?? ""
        let grandchild = try #require(pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)))
        await withKnownIssue("the child is still running at the timeout") {
            await child.waitForExit(timeout: .milliseconds(200))
        }
        #expect(!process.isRunning)
        #expect(await processEnds(grandchild))
    }

    @Test(.timeLimit(.minutes(1))) func runToExitReturnsOnceTheChildEnds() async throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        try await runToExit(process)
        #expect(!process.isRunning)
        #expect(process.terminationStatus == 0)
    }

    /// More waits at once than the pool has threads all end: a wait parks
    /// rather than holding a thread, so none needs another to finish.
    @Test(.timeLimit(.minutes(1))) func manyWaitsAtOnceAllEnd() async throws {
        let runs = ProcessInfo.processInfo.activeProcessorCount * 3
        try await withThrowingTaskGroup(of: Void.self) { group in
            for _ in 0..<runs {
                group.addTask {
                    let process = Process()
                    process.executableURL = URL(fileURLWithPath: "/usr/bin/true")
                    try await runToExit(process)
                    #expect(process.terminationStatus == 0)
                }
            }
            try await group.waitForAll()
        }
    }

    /// An exit that lands before anyone waits is still seen: the handler
    /// is on before the child starts.
    @Test(.timeLimit(.minutes(1))) func anExitBeforeTheWaitIsStillSeen() async throws {
        let temp = try TempDir()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "touch '\(temp.path("ran").path)'"]
        let child = try ChildProcess(starting: process)
        #expect(await fileAppears("ran", in: temp.url))
        #expect(await processEnds(process.processIdentifier))
        #expect(await child.exited())
        #expect(process.terminationStatus == 0)
    }
}
