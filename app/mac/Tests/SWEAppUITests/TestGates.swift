import Foundation
import Observation
import Testing

// The bounded waits this target's tests share. Test targets can't share
// sources, so `gateTimeout`, `CountGate`, `ChildProcess`, `runToExit` and
// `killTree` repeat `SWECoreTests/TestGates.swift`; keep the two in step.

/// How long a test waits on a gate before giving up, so a regression
/// fails the test instead of hanging the suite. A passing test never
/// waits this long, so it is sized for a starved machine, not a typical
/// one.
let gateTimeout: Duration = .seconds(60)

/// Counts events (`bump`) and lets a test wait for the count to reach a
/// target (`reach`). Checking the count and parking the caller happen in
/// one actor step, so a `bump` can never slip between them and leave a
/// waiter stranded.
actor CountGate {
    private(set) var count = 0
    private var nextID = 0
    private var waiters: [Int: (target: Int,
                              cont: CheckedContinuation<Bool, Never>)] = [:]

    func bump() {
        count += 1
        let ready = waiters.filter { count >= $0.value.target }
        for id in ready.keys { waiters[id] = nil }
        for waiter in ready.values { waiter.cont.resume(returning: true) }
    }

    /// True once the count reaches `target`, false after `timeout`.
    func reach(_ target: Int, within timeout: Duration = gateTimeout) async -> Bool {
        await withCheckedContinuation { cont in
            if count >= target {
                cont.resume(returning: true)
                return
            }
            nextID += 1
            let id = nextID
            waiters[id] = (target, cont)
            Task { await self.expire(id, after: timeout) }
        }
    }

    /// The timeout resumes only its own waiter, and only if a `bump` has
    /// not already answered it.
    private func expire(_ id: Int, after timeout: Duration) async {
        try? await Task.sleep(for: timeout)
        waiters.removeValue(forKey: id)?.cont.resume(returning: false)
    }
}

/// A child process a test started, whose exit is awaited without holding
/// any thread: its termination handler, set before it starts, bumps a
/// gate the test parks on.
///
/// Never wait on a child by blocking (`waitUntilExit`, a semaphore, a
/// read to EOF) inside a test. Every test runs on the cooperative pool,
/// which has one thread per core: three on a CI runner. Three tests
/// blocked at once stop the whole suite, and they also hold back every
/// default-QoS `DispatchQueue.global()` block, so a wait whose answer
/// comes through one never ends.
final class ChildProcess: Sendable {
    let process: Process
    private let exits = CountGate()

    /// Starts `process`. The handler goes on first, so an exit that lands
    /// before anyone waits is still counted, and once it has run the
    /// process is reaped and `terminationStatus` can be read.
    init(starting process: Process) throws {
        self.process = process
        let exits = self.exits
        process.terminationHandler = { _ in Task { await exits.bump() } }
        try process.run()
    }

    /// True once the child has exited, false after `timeout`.
    func exited(within timeout: Duration = gateTimeout) async -> Bool {
        await exits.reach(1, within: timeout)
    }

    /// Waits for the exit, at most `timeout`. Past that the child and
    /// every process under it are killed and the test fails, rather than
    /// hang the suite on a child that never ends.
    func waitForExit(timeout: Duration = gateTimeout) async {
        guard await !exited(within: timeout) else { return }
        killTree(process.processIdentifier)
        Issue.record("a child was still running after \(timeout); killed it")
        // SIGKILL cannot be ignored, so this is the kernel reaping,
        // bounded all the same.
        _ = await exited(within: .seconds(10))
    }
}

/// Starts `process` and waits for it to exit, at most `timeout`, as
/// `ChildProcess.waitForExit` does.
func runToExit(_ process: Process, timeout: Duration = gateTimeout) async throws {
    try await ChildProcess(starting: process).waitForExit(timeout: timeout)
}

/// SIGKILLs `pid` and every process under it, leaves first, so nothing it
/// started reparents to launchd and keeps running. Each process is tagged
/// with its start time when listed and signalled only if the tag still
/// matches, so a number freed and reused in between is left alone (as
/// `ProcessRunner.stopDescendants` does).
func killTree(_ pid: pid_t) {
    var tree: [(pid: pid_t, started: StartTime)] = []
    var queue = [pid]
    while let parent = queue.popLast() {
        if let started = StartTime(parent) { tree.append((parent, started)) }
        var children = [pid_t](repeating: 0, count: 256)
        let count = proc_listchildpids(parent, &children,
                                       Int32(MemoryLayout<pid_t>.stride * children.count))
        guard count > 0 else { continue }
        queue += children.prefix(min(Int(count), children.count)).filter { $0 > 0 }
    }
    for member in tree.reversed() where StartTime(member.pid) == member.started {
        kill(member.pid, SIGKILL)
    }
}

/// When a process started, or nil once it is gone.
struct StartTime: Equatable {
    let seconds: UInt64
    let microseconds: UInt64

    init?(_ pid: pid_t) {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        seconds = info.pbi_start_tvsec
        microseconds = info.pbi_start_tvusec
    }
}

/// Runs the main run loop until `done` holds, true if it did before
/// `gateTimeout`. SwiftUI lays out and AppKit delivers events only from
/// run loop turns, and a synchronous test on the main thread has no other
/// way to give it those turns, so this loop stays. Each turn returns as
/// soon as the run loop handles a source.
@MainActor
func spinMainRunLoop(until done: () -> Bool) -> Bool {
    let (seconds, attoseconds) = gateTimeout.components
    let deadline = Date().addingTimeInterval(Double(seconds) + Double(attoseconds) / 1e18)
    while !done() && Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
    }
    return done()
}

/// True once `condition` holds, false after `gateTimeout`. Waits on the
/// observation tracking of what `condition` reads, so nothing spins:
/// each change wakes one re-check, and a change landing between a check
/// and the next tracking is caught because tracking starts with a check.
/// A condition over state that isn't observed never wakes, and fails at
/// the timeout even if it came true unseen: the point is the signal.
@MainActor
func observed(_ condition: @escaping @MainActor () -> Bool) async -> Bool {
    let deadline = ContinuousClock.now + gateTimeout
    while !condition() {
        let changed = CountGate()
        let holds = withObservationTracking {
            condition()
        } onChange: {
            Task { await changed.bump() }
        }
        if holds { return true }
        guard await changed.reach(1), ContinuousClock.now < deadline else { return false }
    }
    return true
}
