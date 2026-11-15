import Foundation
import Synchronization
import Testing
@testable import SWECore

/// How long a test waits on a gate before giving up, so a regression
/// fails the test instead of hanging the suite. A passing test never
/// waits this long, so it is sized for a starved machine, not a typical
/// one: with many agents building at once, a full run of the suite has
/// taken over 18 seconds, and a 10-second bound failed a healthy wait.
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

/// Holds the code under test at a known point until the test lets it go.
/// A fake calls `pass()` where the real dependency would block; the test
/// awaits `arrived(n)` to know `n` callers are parked there, then calls
/// `open()`. Once open, later callers pass straight through.
///
/// A parked caller whose task is cancelled throws `CancellationError`,
/// as `Task.sleep` would, so cancellation paths can be tested too.
final class HoldGate: Sendable {
    private struct State {
        var isOpen = false
        var nextID = 0
        var parked: [Int: CheckedContinuation<Void, any Error>] = [:]
        /// Cancelled before its continuation was stored.
        var cancelled: Set<Int> = []
    }

    private let state = Mutex(State())
    private let arrivals = CountGate()

    /// How many callers have reached the gate so far, parked or not.
    var arrivalCount: Int { get async { await arrivals.count } }

    /// True once `n` callers have reached the gate (each parked, unless
    /// the gate was already open), false after `gateTimeout`.
    func arrived(_ n: Int = 1) async -> Bool { await arrivals.reach(n) }

    /// Lets every parked caller through, and every later one.
    func open() {
        let waiting = state.withLock { s -> [CheckedContinuation<Void, any Error>] in
            s.isOpen = true
            defer { s.parked = [:] }
            return Array(s.parked.values)
        }
        for cont in waiting { cont.resume() }
    }

    /// Called by a fake: counts the arrival, then waits for `open()`.
    func pass() async throws {
        let id = state.withLock { s -> Int in
            s.nextID += 1
            return s.nextID
        }
        let arrivals = self.arrivals
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, any Error>) in
                enum Outcome { case parked, open, cancelled }
                let outcome = state.withLock { s -> Outcome in
                    if s.cancelled.remove(id) != nil { return .cancelled }
                    if s.isOpen { return .open }
                    s.parked[id] = cont
                    return .parked
                }
                // Counted only once the caller is parked (or let through),
                // so `arrived` never answers early.
                Task { await arrivals.bump() }
                switch outcome {
                case .parked: break
                case .open: cont.resume()
                case .cancelled: cont.resume(throwing: CancellationError())
                }
            }
        } onCancel: {
            let cont = state.withLock { s -> CheckedContinuation<Void, any Error>? in
                if let cont = s.parked.removeValue(forKey: id) { return cont }
                s.cancelled.insert(id)
                return nil
            }
            cont?.resume(throwing: CancellationError())
        }
    }
}

/// Waits until `name` exists in `directory`, woken by the folder's own
/// change events (kqueue) instead of a polling timer: for a child process
/// that leaves a marker file, where no in-process gate can reach. False
/// after `timeout`.
func fileAppears(_ name: String, in directory: URL, timeout: Duration = gateTimeout) async -> Bool {
    let path = directory.appending(path: name).path
    let fd = open(directory.path, O_EVTONLY)
    guard fd >= 0 else { return FileManager.default.fileExists(atPath: path) }
    /// Touched only on `queue`, so the first answer wins without a lock.
    final class Once: @unchecked Sendable { var answered = false }
    let once = Once()
    let queue = DispatchQueue(label: "fileAppears")
    let source = DispatchSource.makeFileSystemObjectSource(
        fileDescriptor: fd, eventMask: [.write, .link, .rename], queue: queue)
    source.setCancelHandler { close(fd) }
    return await withCheckedContinuation { cont in
        @Sendable func answer(_ value: Bool) {
            guard !once.answered else { return }
            once.answered = true
            source.cancel()
            cont.resume(returning: value)
        }
        source.setEventHandler {
            if FileManager.default.fileExists(atPath: path) { answer(true) }
        }
        let (seconds, attoseconds) = timeout.components
        let interval = Double(seconds) + Double(attoseconds) / 1e18
        queue.asyncAfter(deadline: .now() + interval) {
            answer(FileManager.default.fileExists(atPath: path))
        }
        source.activate()
        // The file may have landed before the watch started.
        queue.async {
            if FileManager.default.fileExists(atPath: path) { answer(true) }
        }
    }
}

/// Waits until process `pid` has exited, woken by the kernel's exit event
/// (kqueue) instead of polling `kill(pid, 0)`: for a grandchild a test
/// expects dead, where no in-process gate can reach. A process that has
/// exited but is not yet reaped (a zombie, reaped on launchd's schedule
/// once orphaned) counts as ended. False after `timeout`.
func processEnds(_ pid: pid_t, timeout: Duration = gateTimeout) async -> Bool {
    /// Touched only on `queue`, so the first answer wins without a lock.
    final class Once: @unchecked Sendable { var answered = false }
    let once = Once()
    let queue = DispatchQueue(label: "processEnds")
    let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: queue)
    return await withCheckedContinuation { cont in
        @Sendable func answer(_ value: Bool) {
            guard !once.answered else { return }
            once.answered = true
            source.cancel()
            cont.resume(returning: value)
        }
        source.setEventHandler { answer(true) }
        let (seconds, attoseconds) = timeout.components
        let interval = Double(seconds) + Double(attoseconds) / 1e18
        queue.asyncAfter(deadline: .now() + interval) { answer(!isLive(pid)) }
        source.activate()
        // It may have exited before the watch started.
        queue.async { if !isLive(pid) { answer(true) } }
    }
}

/// Whether `pid` names a running process: neither gone nor a zombie.
func isLive(_ pid: pid_t) -> Bool {
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return false }
    return info.pbi_status != SZOMB
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
/// comes through one never ends. v1.2.3's release run sat out that
/// deadlock for minutes, 60 seconds per wave.
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
    /// hang the suite on a child that never ends (a freshly written
    /// executable can sit in `_dyld_start` for minutes on a loaded
    /// machine).
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
/// matches, so a number freed and reused in between is left alone.
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

/// Answers call `i` with `replies[i]`, each held at its own gate until the
/// test opens it, so a test picks the order replies land in. A call past
/// the last reply hangs until cancelled, like a command that never ends.
final class GatedReplies: CommandRunning, Sendable {
    let gates: [HoldGate]
    private let replies: [CommandResult]
    private let next = Mutex(0)
    private let overflow = HoldGate()

    init(_ replies: [CommandResult]) {
        self.replies = replies
        gates = replies.map { _ in HoldGate() }
    }

    /// True once a call past the last reply is hanging.
    func hanging() async -> Bool { await overflow.arrived() }

    func run(_ arguments: [String]) async throws -> CommandResult {
        let index = next.withLock { n -> Int in
            defer { n += 1 }
            return n
        }
        guard index < replies.count else {
            try await overflow.pass()
            throw CancellationError()
        }
        try await gates[index].pass()
        return replies[index]
    }
}

/// True once `condition` holds, re-checked on every change to the
/// observable state it reads, false after `gateTimeout`: a wait on the
/// model's own change stream rather than a polling loop.
@MainActor
func observe(until condition: @escaping @MainActor @Sendable () -> Bool) async -> Bool {
    await withinTimeout {
        for await met in Observations({ condition() }) {
            if met { return true }
            if Task.isCancelled { return false }
        }
        return false
    }
}

/// Races `wait` against `timeout`: the first to finish answers, and the
/// other is cancelled but never awaited. A task group waits for every
/// child before it returns, so a `wait` that ignores cancellation (such as
/// `await task.value` on a task that never ends) used to hold the group,
/// and the test, past the timeout for good. Cancelling the caller answers
/// false at once and cancels both racers.
@MainActor
func withinTimeout(_ timeout: Duration = gateTimeout,
                   _ wait: @escaping @MainActor @Sendable () async -> Bool) async -> Bool {
    let race = FirstAnswer()
    return await withTaskCancellationHandler {
        await withCheckedContinuation { continuation in
            race.start(continuation, racers: [
                Task { @MainActor in race.answer(await wait()) },
                Task {
                    try? await Task.sleep(for: timeout)
                    race.answer(false)
                },
            ])
        }
    } onCancel: {
        race.answer(false)
    }
}

/// The answer `withinTimeout` gives: whichever comes first of a racer's
/// `answer` or the caller's cancellation resumes the caller and cancels
/// both racers; later answers are dropped.
private final class FirstAnswer: Sendable {
    private struct State {
        var continuation: CheckedContinuation<Bool, Never>?
        var racers: [Task<Void, Never>] = []
        /// The first answer, once there is one.
        var answer: Bool?
    }

    private let state = Mutex(State())

    /// Hands over the caller and the racers. If an answer (a cancellation)
    /// already came, the caller gets it now and the racers are cancelled.
    func start(_ continuation: CheckedContinuation<Bool, Never>, racers: [Task<Void, Never>]) {
        let early = state.withLock { s -> Bool? in
            if let answer = s.answer { return answer }
            s.continuation = continuation
            s.racers = racers
            return nil
        }
        guard let early else { return }
        racers.forEach { $0.cancel() }
        continuation.resume(returning: early)
    }

    func answer(_ value: Bool) {
        let taken = state.withLock { s -> (CheckedContinuation<Bool, Never>?, [Task<Void, Never>])? in
            if s.answer != nil { return nil }
            s.answer = value
            defer { s.continuation = nil; s.racers = [] }
            return (s.continuation, s.racers)
        }
        guard let (continuation, racers) = taken else { return }
        racers.forEach { $0.cancel() }
        continuation?.resume(returning: value)
    }
}
