import Foundation
import Synchronization
import Testing
@testable import SWECore

@Suite struct SessionListTests {
    @Test func sessionsDecodeWithTheirResumeCommand() async throws {
        let runner = FakeRunner(.init(status: 0, stdout: try fixture("session-list"), stderr: ""))
        let list = try await SweClient(runner: runner).sessions(days: 90, limit: 2000)
        #expect(runner.calls == [["session", "2000", "--days=90", "--json"]])
        #expect(list.sessions.map(\.id) == ["aaa-111", "bbb-222", "ccc-333", "crush|/Users/k/code/notes|1791400000"])
        let first = try #require(list.sessions.first)
        #expect(first.status == "active")
        #expect(first.lastActiveAt == Date(timeIntervalSince1970: 1_791_694_778.5))
        #expect(first.resume?.argv == ["claude", "--resume", "aaa-111"])
        #expect(list.sessions[2].resume == nil)
        #expect(list.sessions[2].cwd == nil)
        #expect(list.failures == ["droid: unreadable"])
    }

    @Test func aWorktreeBelongsToItsRepository() {
        #expect(SessionInfo.project(of: "/Users/k/code/quiver/.claude/worktrees/m4-sessions") == "quiver")
        #expect(SessionInfo.project(of: "/Users/k/code/quiver/.worktrees/x") == "quiver")
        #expect(SessionInfo.project(of: "/Users/k/code/site") == "site")
        #expect(SessionInfo.project(of: nil) == nil)
    }
}

@Suite struct ResumeCommandTests {
    let resume = SessionInfo.Resume(argv: ["claude", "--resume", "aaa 111"], cwd: "/Users/k/my code")

    /// A resumed rollout's stem ends in its own uuid after the thread's;
    /// `codex resume` takes the thread's, the first (as swe does).
    @MainActor @Test func codexResumesTheThreadNotTheResumesRollout() {
        let thread = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
        let rollout = "0199ffff-0000-7111-8222-333344445555"
        #expect(SessionsModel.resumeArgv(harness: "codex", id: "rollout-2026-10-01T09-30-00-\(thread)")
                == ["codex", "resume", thread])
        #expect(SessionsModel.resumeArgv(harness: "codex",
                                         id: "rollout-2026-10-01T09-30-00-\(thread.uppercased())_\(rollout)")
                == ["codex", "resume", thread])
        #expect(SessionsModel.resumeArgv(harness: "codex", id: "rollout-no-uuid") == nil)
    }

    @Test func theDefaultAccountClearsAnyOverride() {
        let line = SessionResume.shellLine(resume, harness: "claude", accountHome: "/Users/k/.claude", isDefaultHome: true)
        // Every step depends on the one before: a missing folder stops it all.
        #expect(line == "cd '/Users/k/my code' && unset CLAUDE_CONFIG_DIR && 'claude' '--resume' 'aaa 111'")
    }

    @Test func anotherAccountResumesInItsOwnHome() {
        let line = SessionResume.shellLine(resume, harness: "claude", accountHome: "/Users/k/.swe/accounts/claude/w/home",
                                           isDefaultHome: false)
        #expect(line == "cd '/Users/k/my code' && CLAUDE_CONFIG_DIR='/Users/k/.swe/accounts/claude/w/home' 'claude' '--resume' 'aaa 111'")
        let codex = SessionResume.shellLine(.init(argv: ["codex", "resume", "x"], cwd: nil), harness: "codex",
                                            accountHome: "/h", isDefaultHome: false)
        #expect(codex == "CODEX_HOME='/h' 'codex' 'resume' 'x'")
    }

    @Test func otherHarnessesRunAsGiven() {
        let line = SessionResume.shellLine(.init(argv: ["droid", "-r", "x"], cwd: "/p"), harness: "droid",
                                           accountHome: nil, isDefaultHome: true)
        #expect(line == "cd '/p' && 'droid' '-r' 'x'")
    }
}

/// Lists canned sessions.
struct FakeSessionSource: SessionListing {
    var list: SessionList
    var error: Error?
    func sessions(days: Int?, limit: Int) async throws -> SessionList {
        if let error { throw error }
        return list
    }
}

actor FakeSessionLedger: SessionUsageQuerying {
    var rows: [SessionUsage] = []
    var days: [DailyUsage] = []
    private(set) var ranges: [DateInterval] = []
    /// The next `daily` call captures its rows, then waits at `dailyGate`
    /// until `releaseDaily` opens it.
    private var holdDaily = false
    private var dailyGate: HoldGate?
    /// Bumped once a held `daily` reaches its gate, so a test waits for it
    /// without spinning. It can't pass until released.
    let dailyParks = CountGate()
    func set(_ rows: [SessionUsage]) { self.rows = rows }
    func setDaily(_ rows: [DailyUsage]) { self.days = rows }
    func holdNextDaily() {
        holdDaily = true
        dailyGate = HoldGate()
    }
    func releaseDaily() {
        holdDaily = false
        dailyGate?.open()
        dailyGate = nil
    }
    func bySession(in range: DateInterval, harness: String?) async -> [SessionUsage] {
        ranges.append(range)
        return rows
    }
    func daily(in range: DateInterval, calendar: Calendar) async -> [DailyUsage] {
        let result = days
        if holdDaily, let gate = dailyGate {
            holdDaily = false
            await dailyParks.bump()
            // A cancelled read stops waiting, as a cancelled query would.
            try? await gate.pass()
        }
        return result
    }
}

/// Holds the listing until released, to test what happens meanwhile.
actor GatedSessionSource: SessionListing {
    let list: SessionList
    /// Where each listing waits; `arrived` says one is parked.
    nonisolated let gate = HoldGate()
    /// Whether anything asked for the listing.
    private(set) var waiting = false
    init(_ list: SessionList) { self.list = list }
    func sessions(days: Int?, limit: Int) async throws -> SessionList {
        waiting = true
        try await gate.pass()
        return list
    }
    nonisolated func release() { gate.open() }
}

/// Counts how often the session list was fetched.
final class CountingSessionSource: SessionListing, Sendable {
    let list: SessionList
    private let count = Mutex(0)
    /// Listings arrive off the test's actor, so the count is locked.
    var calls: Int { count.withLock { $0 } }
    init(_ list: SessionList) { self.list = list }
    func sessions(days: Int?, limit: Int) async throws -> SessionList {
        count.withLock { $0 += 1 }
        return list
    }
}

@MainActor
@Suite struct SessionsModelTests {
    let now = Date(timeIntervalSince1970: 1_791_700_000)

    func session(_ id: String, _ harness: String, cwd: String?, at: Double, title: String? = nil) -> SessionInfo {
        SessionInfo(id: id, harness: harness, agent: harness.capitalized, cwd: cwd, title: title ?? id,
                    titleSource: nil, lastActiveAt: Date(timeIntervalSince1970: at), status: nil,
                    resume: .init(argv: [harness, id], cwd: cwd))
    }

    func usage(_ id: String, _ harness: String, models: [String], cost: Double, home: String = "/Users/k/.claude") -> SessionUsage {
        var t = UsageTotals()
        t.inputTokens = 100
        // Three replies keeps the row clear of the trivial-session rule.
        t.eventCount = 3
        t.costUSD = cost
        return SessionUsage(sessionId: id, harness: harness, accountHome: home, cwd: nil, models: models, totals: t,
                            firstTimestamp: now, lastTimestamp: now)
    }

    var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "Asia/Singapore")!
        return c
    }

    func model(_ sessions: [SessionInfo], _ usage: [SessionUsage], ledger: FakeSessionLedger = FakeSessionLedger(),
               limit: Int = 100_000) async -> SessionsModel {
        await ledger.set(usage)
        return SessionsModel(source: FakeSessionSource(list: SessionList(sessions: sessions, failures: [])),
                             ledger: ledger, defaultHomes: ["/Users/k/.claude", "/Users/k/.codex"],
                             limit: limit, now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false)
    }

    @Test func sessionsCarryTheirTokensAndCost() async throws {
        let model = await model(
            [session("a", "claude", cwd: "/c/quiver", at: 1_791_699_000), session("b", "codex", cwd: "/c/site", at: 1_791_698_000)],
            [usage("a", "claude", models: ["claude-opus-5-5"], cost: 4.5)])
        await model.load()
        #expect(model.rows.map(\.id) == ["a", "b"])
        #expect(model.rows[0].usage?.totals.costUSD == 4.5)
        #expect(model.rows[1].usage == nil)
        #expect(model.models.map(\.model) == ["claude-opus-5-5"])
        #expect(model.projects.map(\.name) == ["quiver", "site"])
        #expect(model.harnesses == ["claude", "codex"])
    }

    @Test func filtersNarrowTheList() async throws {
        let model = await model([
            session("a", "claude", cwd: "/c/quiver", at: 1_791_699_000, title: "Ledger work"),
            session("b", "codex", cwd: "/c/site", at: 1_791_698_000),
            session("c", "claude", cwd: "/c/site", at: 1_791_000_000),
        ], [usage("a", "claude", models: ["opus"], cost: 1),
            usage("b", "codex", models: ["gpt"], cost: 1, home: "/Users/k/.codex"),
            usage("c", "claude", models: ["sonnet"], cost: 1)])
        await model.load()

        model.filter.harness = "claude"
        #expect(model.visible.map(\.id) == ["a", "c"])
        model.filter.window = .week
        #expect(model.visible.map(\.id) == ["a"])
        model.filter = .init()
        model.filter.project = "site"
        #expect(model.visible.map(\.id) == ["b", "c"])
        model.filter = .init()
        model.filter.model = "sonnet"
        #expect(model.visible.map(\.id) == ["c"])
        model.filter = .init()
        model.filter.search = "ledger"
        #expect(model.visible.map(\.id) == ["a"])
        // Search covers the project and the models too.
        model.filter.search = "sonnet"
        #expect(model.visible.map(\.id) == ["c"])
        model.filter.search = "quiver"
        #expect(model.visible.map(\.id) == ["a"])
    }

    @Test func visibleSessionsAreSummedAndGroupedByDay() async throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Singapore")!
        // now is 2026-10-11 12:26 SGT; a and b fall on Oct 11, c on Oct 2.
        let model = await model([
            session("a", "claude", cwd: "/c/quiver", at: 1_791_699_000),
            session("b", "codex", cwd: "/c/site", at: 1_791_650_000),
            session("c", "claude", cwd: "/c/site", at: 1_790_900_000),
        ], [usage("a", "claude", models: ["opus"], cost: 4),
            usage("b", "codex", models: ["gpt"], cost: 0, home: "/Users/k/.codex"),
            usage("c", "claude", models: ["sonnet"], cost: 1.5)])
        await model.load()

        let summary = model.summary
        #expect(summary.sessions == 3)
        #expect(summary.totals.totalTokens == 300)
        #expect(summary.totals.costUSD == 5.5)

        let groups = model.groupedByDay(calendar: calendar)
        #expect(groups.map { $0.rows.map(\.id) } == [["a", "b"], ["c"]])
        #expect(groups.first?.day == Date(timeIntervalSince1970: 1_791_648_000))

        model.filter.harness = "codex"
        #expect(model.summary.sessions == 1)
        #expect(model.summary.totals.totalTokens == 100)
    }

    @Test func aSessionsDurationSpansItsFirstAndLastEvent() {
        var usage = usage("a", "claude", models: [], cost: 0)
        usage.firstTimestamp = now
        usage.lastTimestamp = now.addingTimeInterval(2 * 3600 + 5 * 60)
        #expect(SessionsModel.Row(session: session("a", "claude", cwd: nil, at: 0), usage: usage).duration == 7500)
    }

    @Test func todayStartsAtLocalMidnight() async {
        // now is 12:26 SGT; 23:00 the evening before is within 24 hours but not today.
        let model = await model([
            session("a", "claude", cwd: "/c/q", at: 1_791_699_000),
            session("y", "claude", cwd: "/c/q", at: 1_791_644_400),
        ], [usage("a", "claude", models: [], cost: 0)])
        await model.load()
        model.filter.window = .today
        #expect(model.visible.map(\.id) == ["a"])
    }

    @Test func usageCoversTheWholeHistoryOfEachSession() async throws {
        let ledger = FakeSessionLedger()
        let model = await model([session("a", "claude", cwd: nil, at: 1_791_699_000)], [], ledger: ledger)
        await model.load()
        let range = try #require(await ledger.ranges.first)
        #expect(range.start <= Date(timeIntervalSince1970: 0))
    }

    @Test func sessionsFromExtraAccountsComeFromTheLedger() async throws {
        var work = usage("w1", "claude", models: ["opus"], cost: 3, home: "/Users/k/.swe/accounts/claude/w/home")
        work.cwd = "/c/quiver"
        work.lastTimestamp = now.addingTimeInterval(-3600)
        var old = usage("w0", "codex", models: ["gpt"], cost: 1, home: "/Users/k/.swe/accounts/codex/x/home")
        old.lastTimestamp = now.addingTimeInterval(-200 * 86_400)
        // A default-home session swe didn't list stays out: swe is the source there.
        let stray = usage("d1", "claude", models: [], cost: 1)
        let model = await model([session("a", "claude", cwd: nil, at: 1_791_699_000)],
                                [work, old, stray, usage("a", "claude", models: [], cost: 0)])
        await model.load()
        // The listing is unbounded, so extra-account sessions join at any
        // age; the window filter still keeps the old one out of view.
        #expect(model.rows.map(\.session.id) == ["a", "w1", "w0"])
        #expect(model.visible.map(\.session.id) == ["a", "w1"])
        model.filter.window = .all
        #expect(model.visible.map(\.session.id) == ["a", "w1", "w0"])
        let row = try #require(model.rows.first { $0.session.id == "w1" })
        #expect(row.session.harness == "claude")
        #expect(row.session.cwd == "/c/quiver")
        #expect(row.session.project == "quiver")
        #expect(row.session.resume == SessionInfo.Resume(argv: ["claude", "--resume", "w1"], cwd: "/c/quiver"))
        #expect(row.session.lastActiveAt == work.lastTimestamp)
        #expect(row.usage?.accountHome == "/Users/k/.swe/accounts/claude/w/home")
    }

    @Test func extraAccountRowsCarryTheirAccountLabel() async throws {
        var work = usage("w1", "claude", models: ["opus"], cost: 3, home: "/Users/k/.swe/accounts/claude/w/home")
        work.cwd = "/c/quiver"
        let ledger = FakeSessionLedger()
        await ledger.set([work, usage("a", "claude", models: [], cost: 0)])
        let model = SessionsModel(
            source: FakeSessionSource(list: SessionList(
                sessions: [session("a", "claude", cwd: nil, at: 1_791_699_000)], failures: [])),
            ledger: ledger, defaultHomes: ["/Users/k/.claude", "/Users/k/.codex"],
            now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false,
            accountLabel: { $0.hasSuffix("/w/home") ? "Claude: Work" : nil })
        await model.load()

        let row = try #require(model.rows.first { $0.session.id == "w1" })
        #expect(row.account == "Claude: Work")
        #expect(row.session.accountHome == "/Users/k/.swe/accounts/claude/w/home")
        // The default-home row keeps the bare harness name.
        #expect(model.rows.first { $0.session.id == "a" }?.account == nil)
        // The search matches the label, so "work" finds the account's rows.
        model.filter.search = "work"
        #expect(model.visible.map(\.session.id) == ["w1"])
    }

    @Test func aListedAccountSessionJoinsItsOwnUsageAndKeepsOneRow() async throws {
        // swe lists account-home sessions with `account_home` set: the row
        // takes that account's usage, its own title, and the ledger adds
        // no second row for it.
        var listed = session("w1", "claude", cwd: "/c/quiver", at: 1_791_699_000, title: "Account work")
        listed.accountHome = "/Users/k/.swe/accounts/claude/w/home"
        var work = usage("w1", "claude", models: ["opus"], cost: 3, home: "/Users/k/.swe/accounts/claude/w/home")
        work.cwd = "/c/quiver"
        let ledger = FakeSessionLedger()
        await ledger.set([work])
        let model = SessionsModel(
            source: FakeSessionSource(list: SessionList(sessions: [listed], failures: [])),
            ledger: ledger, defaultHomes: ["/Users/k/.claude", "/Users/k/.codex"],
            now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false,
            accountLabel: { _ in "Claude: Work" })
        await model.load()

        #expect(model.rows.count == 1)
        let row = model.rows[0]
        #expect(row.session.title == "Account work")
        #expect(row.account == "Claude: Work")
        #expect(row.usage?.totals.costUSD == 3)
    }

    @Test func anAccountSessionKeepsItsRowIdFromLedgerOnlyToListed() async throws {
        // Before swe lists a profile session the ledger-only row carries
        // `account|session`; once listed it must keep that id or a rail
        // holding the open row sees a new session and shows it twice.
        let home = "/Users/k/.swe/accounts/claude/w/home"
        var work = usage("w1", "claude", models: ["opus"], cost: 3, home: home)
        work.cwd = "/c/quiver"
        let ledgerOnlyStore = FakeSessionLedger()
        await ledgerOnlyStore.set([work])
        let ledgerOnly = SessionsModel(
            source: FakeSessionSource(list: SessionList(sessions: [], failures: [])),
            ledger: ledgerOnlyStore, defaultHomes: ["/Users/k/.claude"],
            now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false)
        var listed = session("w1", "claude", cwd: "/c/quiver", at: 1_791_699_000, title: "Account work")
        listed.accountHome = home
        let listedStore = FakeSessionLedger()
        await listedStore.set([work])
        let listedModel = SessionsModel(
            source: FakeSessionSource(list: SessionList(sessions: [listed], failures: [])),
            ledger: listedStore, defaultHomes: ["/Users/k/.claude"],
            now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false)

        await ledgerOnly.load()
        await listedModel.load()

        #expect(ledgerOnly.rows.map(\.id) == ["\(home)|w1"])
        #expect(listedModel.rows.map(\.id) == ["\(home)|w1"])
    }

    @Test func feedSessionsWithoutATranscriptComeFromTheLedger() async throws {
        let cursorHome = "/Users/k/Library/Application Support/Cursor"
        var cloud = usage("bc-1111", "cursor", models: ["claude-opus-5-5"], cost: 5, home: cursorHome)
        cloud.lastTimestamp = now.addingTimeInterval(-60)
        var bot = usage("conv-9", "grok-bot", models: ["grok-bot-default"], cost: 2, home: cursorHome)
        bot.lastTimestamp = now.addingTimeInterval(-120)
        var editor = usage("e-7", "cursor", models: ["gpt-5.6"], cost: 1, home: cursorHome)
        editor.lastTimestamp = now.addingTimeInterval(-180)
        // swe lists this one from its transcript, so the ledger adds no second row.
        let listed = usage("t-1", "cursor", models: ["gpt-5.6"], cost: 3, home: cursorHome)
        let model = await model([session("t-1", "cursor", cwd: "/c/quiver", at: 1_791_700_000)],
                                [cloud, bot, editor, listed])
        await model.load()

        #expect(model.rows.map(\.session.id) == ["t-1", "bc-1111", "conv-9", "e-7"])
        #expect(model.rows.map(\.location) == [.local, .cloud, .cloud, .local])
        #expect(model.rows[0].usage?.totals.costUSD == 3)
        let row = model.rows[2]
        #expect(row.session.harness == "grok-bot")
        #expect(row.session.agent == "Grok Bot")
        #expect(row.session.title == nil)
        #expect(row.session.resume == nil)
        #expect(row.session.lastActiveAt == bot.lastTimestamp)
        #expect(model.harnesses == ["cursor", "grok-bot"])
        #expect(model.summary.totals.costUSD == 11)
        model.filter.search = "cloud"
        #expect(model.visible.map(\.session.id) == ["bc-1111", "conv-9"])
    }

    @Test func aFeedConversationPolledByTwoProfilesIsOneRow() async throws {
        // Two profiles on one Cursor account took turns polling: older
        // events kept the first home, newer ones the second.
        let first = "/Users/k/Library/Application Support/Cursor"
        let second = "/Users/k/.swe/accounts/cursor/w/home"
        var old = usage("bc-1", "cursor", models: ["opus"], cost: 2, home: first)
        old.firstTimestamp = now.addingTimeInterval(-7200)
        old.lastTimestamp = now.addingTimeInterval(-3600)
        var new = usage("bc-1", "cursor", models: ["gpt"], cost: 3, home: second)
        new.firstTimestamp = now.addingTimeInterval(-600)
        new.lastTimestamp = now.addingTimeInterval(-60)
        var listedOld = usage("t-1", "cursor", models: [], cost: 1, home: first)
        listedOld.lastTimestamp = now.addingTimeInterval(-3600)
        let listedNew = usage("t-1", "cursor", models: [], cost: 4, home: second)
        let model = await model([session("t-1", "cursor", cwd: nil, at: 1_791_700_000)],
                                [old, new, listedOld, listedNew])
        await model.load()

        #expect(model.rows.map(\.session.id) == ["t-1", "bc-1"])
        #expect(model.rows.map(\.usage?.totals.costUSD) == [5, 5])
        let row = model.rows[1]
        #expect(row.usage?.accountHome == second)
        #expect(row.usage?.models == ["gpt", "opus"])
        #expect(row.duration == 7140)
    }

    @Test func dayBucketsFromTwoAccountsStayApart() async throws {
        // Events Cursor gave no conversation id fall into a per-day bucket,
        // which is one account's day, not one conversation.
        let first = usage("cursor-2026-10-11", "cursor", models: [], cost: 1, home: "/a")
        var second = usage("cursor-2026-10-11", "cursor", models: [], cost: 2, home: "/b")
        second.lastTimestamp = now.addingTimeInterval(-60)
        let model = await model([], [first, second])
        await model.load()
        #expect(model.rows.map(\.usage?.accountHome) == ["/a", "/b"])
        #expect(model.rows.map(\.usage?.totals.costUSD) == [1, 2])
        #expect(CursorUsageFeed.isDayBucket("cursor-2026-10-11"))
        #expect(!CursorUsageFeed.isDayBucket("cursor-agent-1"))
    }

    @Test func aSessionsLocationFollowsItsHarnessAndId() {
        #expect(SessionLocation(harness: "grok-bot", id: "4f1c") == .cloud)
        #expect(SessionLocation(harness: "cursor", id: "bc-ec6482ef") == .cloud)
        #expect(SessionLocation(harness: "cursor", id: "sand-subagent-afb1") == .cloud)
        #expect(SessionLocation(harness: "cursor", id: "4f1c") == .local)
        #expect(SessionLocation(harness: "claude", id: "bc-1") == .local)
    }

    @Test func codexSessionsResumeByTheirThreadId() async throws {
        let home = "/Users/k/.swe/accounts/codex/x/home"
        var rollout = usage("rollout-2026-09-09T14-52-42-0199ABCD-1111-2222-3333-444455556666", "codex",
                            models: [], cost: 1, home: home)
        rollout.lastTimestamp = now
        var odd = usage("no-uuid-here", "codex", models: [], cost: 1, home: home)
        odd.lastTimestamp = now.addingTimeInterval(-60)
        let model = await model([], [rollout, odd])
        await model.load()
        #expect(model.rows.map { $0.session.resume?.argv } == [
            ["codex", "resume", "0199abcd-1111-2222-3333-444455556666"], nil,
        ])
    }

    @Test func aSessionCopiedAcrossAccountsKeepsEachAccountApart() async throws {
        let extra = "/Users/k/.swe/accounts/claude/w/home"
        var mine = usage("s1", "claude", models: [], cost: 1)
        mine.lastTimestamp = now
        var theirs = usage("s1", "claude", models: [], cost: 2, home: extra)
        theirs.lastTimestamp = now.addingTimeInterval(-3600)   // before swe's row
        let model = await model([session("s1", "claude", cwd: nil, at: 1_791_699_000)], [theirs, mine])
        await model.load()
        #expect(model.rows.count == 2)
        #expect(Set(model.rows.map(\.id)).count == 2)
        #expect(model.rows.map(\.usage?.accountHome) == ["/Users/k/.claude", extra])
        #expect(model.rows.map(\.usage?.totals.costUSD) == [1, 2])
    }

    @Test func usageRefreshesWithoutListingAgain() async throws {
        let ledger = FakeSessionLedger()
        let source = CountingSessionSource(SessionList(sessions: [session("a", "claude", cwd: nil, at: 1_791_699_000)], failures: []))
        let model = SessionsModel(source: source, ledger: ledger, now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false)
        await model.load()
        #expect(model.rows.first?.usage == nil)
        await ledger.set([usage("a", "claude", models: ["opus"], cost: 2)])
        await model.refreshUsage()
        #expect(model.rows.first?.usage?.totals.costUSD == 2)
        #expect(source.calls == 1)
    }

    @Test func aUsageRefreshDuringTheFirstLoadDoesNotStrandIt() async throws {
        let source = GatedSessionSource(SessionList(sessions: [session("a", "claude", cwd: nil, at: 1_791_699_000)], failures: []))
        let model = SessionsModel(source: source, ledger: FakeSessionLedger(), now: { [now] in now }, calendar: calendar, searchIndexingEnabled: false)
        let load = Task { await model.load() }
        try #require(await source.gate.arrived())
        await model.refreshUsage()
        source.release()
        await load.value
        #expect(model.rows.map(\.id) == ["a"])
        #expect(!model.isLoading)
    }

    @Test func aFullListSaysItIsCapped() async {
        let model = await model([session("a", "claude", cwd: nil, at: 1_791_699_000),
                                 session("b", "claude", cwd: nil, at: 1_791_698_000)], [], limit: 2)
        await model.load()
        #expect(model.isCapped)
    }

    @Test func aFailedListingIsShown() async {
        let model = SessionsModel(
            source: FakeSessionSource(list: SessionList(sessions: [], failures: []), error: SweError.commandFailed(status: 1, message: "boom")),
            ledger: FakeSessionLedger(), now: { [now] in now }, searchIndexingEnabled: false)
        await model.load()
        #expect(model.error == "boom")
    }

    @Test func projectChoicesMergeCaseAndSortByCount() async {
        let model = await model([
            session("a", "claude", cwd: "/c/Quiver", at: 1_791_699_000),
            session("b", "codex", cwd: "/c/quiver", at: 1_791_698_000),
            session("c", "claude", cwd: "/c/site", at: 1_791_697_000),
        ], [usage("a", "claude", models: [], cost: 0),
            usage("b", "codex", models: [], cost: 0, home: "/Users/k/.codex"),
            usage("c", "claude", models: [], cost: 0)])
        await model.load()
        #expect(model.projects.map(\.name) == ["Quiver", "site"])
        #expect(model.projects.map(\.count) == [2, 1])
        model.filter.project = "quiver"
        #expect(model.visible.map(\.id) == ["a", "b"])
    }

    @Test func hashLikeProjectsFoldIntoOther() async {
        let model = await model([
            session("a", "claude", cwd: "/c/site", at: 1_791_699_000),
            session("b", "claude", cwd: "/c/3f2a9c1", at: 1_791_698_000),
            session("c", "claude", cwd: "/c/0199abcd-1111-2222-3333-444455556666", at: 1_791_697_000),
        ], [usage("a", "claude", models: [], cost: 0),
            usage("b", "claude", models: [], cost: 0),
            usage("c", "claude", models: [], cost: 0)])
        await model.load()
        // Other carries every generated name and always sorts last.
        #expect(model.projects.map(\.name) == ["site", "Other"])
        #expect(model.projects.last?.count == 2)
        model.filter.project = SessionsModel.otherProjectKey
        #expect(model.visible.map(\.id) == ["b", "c"])
    }

    @Test func modelsSortByPeriodTokens() async {
        let ledger = FakeSessionLedger()
        var big = UsageTotals()
        big.inputTokens = 500
        var small = UsageTotals()
        small.inputTokens = 100
        await ledger.setDaily([
            DailyUsage(day: now, harness: "claude", model: "sonnet", totals: small),
            DailyUsage(day: now, harness: "claude", model: "opus", totals: big),
        ])
        let model = await model(
            [session("a", "claude", cwd: nil, at: 1_791_699_000)],
            [usage("a", "claude", models: ["sonnet", "opus"], cost: 1)],
            ledger: ledger)
        await model.load()
        #expect(model.models.map(\.model) == ["opus", "sonnet"])
        #expect(model.models.first?.tokens == 500)
        // A model the ledger never saw still lists, just without a count.
        await ledger.set([usage("a", "claude", models: ["sonnet", "opus", "haiku"], cost: 1)])
        await model.refreshUsage()
        #expect(model.models.map(\.model) == ["opus", "sonnet", "haiku"])
    }

    @Test func modelCountsCountWholeDaysAtTheWindowEdge() async {
        let ledger = FakeSessionLedger()
        var tokens = UsageTotals()
        tokens.inputTokens = 300
        // Midnight on the day the 7-day window opens on; a time-of-day
        // cutoff would drop this midnight-labelled row.
        let cutoff = calendar.startOfDay(for: now.addingTimeInterval(-7 * 86_400))
        await ledger.setDaily([DailyUsage(day: cutoff, harness: "claude", model: "opus", totals: tokens)])
        let model = await model([session("a", "claude", cwd: nil, at: 1_791_699_000)],
                                [usage("a", "claude", models: ["opus"], cost: 1)], ledger: ledger)
        await model.load()
        model.filter.window = .week
        #expect(model.models.first?.tokens == 300)
    }

    @Test func aUsageRefreshMidLoadKeepsItsDailyRows() async throws {
        let ledger = FakeSessionLedger()
        var stale = UsageTotals()
        stale.inputTokens = 100
        var fresh = UsageTotals()
        fresh.inputTokens = 900
        await ledger.setDaily([DailyUsage(day: now, harness: "claude", model: "opus", totals: stale)])
        let model = await model([session("a", "claude", cwd: nil, at: 1_791_699_000)],
                                [usage("a", "claude", models: ["opus"], cost: 1)], ledger: ledger)
        // Hold the load mid-read so a refresh can commit newer daily rows.
        await ledger.holdNextDaily()
        let loading = Task { await model.load() }
        try #require(await ledger.dailyParks.reach(1))
        await ledger.setDaily([DailyUsage(day: now, harness: "claude", model: "opus", totals: fresh)])
        await model.refreshUsage()
        await ledger.releaseDaily()
        await loading.value
        // The load's stale read must not overwrite the refresh's rows.
        #expect(model.models.first?.tokens == 900)
    }

    /// A mirrored session (#1918) with no ledger usage is never hidden as
    /// accidental: its copy can predate the ledger, so missing usage proves
    /// nothing about it, as on the web.
    @Test func aMirroredSessionIsNeverHiddenAsAccidental() async {
        var copy = session("m", "claude", cwd: "/c/q", at: 1_791_699_000)
        copy.mirrored = true
        copy.resume = nil
        let model = await model([copy, session("a", "claude", cwd: "/c/q", at: 1_791_698_000)], [])
        await model.load()
        await model.refreshUsage(ledgerScanned: true)
        #expect(model.visible.map(\.id) == ["m"])
        #expect(model.visible.first?.session.mirrored == true)
        #expect(model.hiddenTrivialCount == 1)
    }

    @Test func emptyListedSessionsHideAfterTheFirstScanWithACountUntilShown() async {
        // A session swe lists that the ledger never saw ran zero
        // responses — once the ledger has actually scanned. Gemini has
        // no usage log, so its rows always stay.
        let model = await model([
            session("a", "claude", cwd: "/c/q", at: 1_791_699_000),
            session("b", "claude", cwd: "/c/q", at: 1_791_698_000),
            session("g", "gemini", cwd: "/c/q", at: 1_791_697_000),
        ], [usage("b", "claude", models: [], cost: 0)])
        await model.load()
        // Before the first scan completes, missing usage proves nothing:
        // the ledger may just not have read the logs yet.
        #expect(model.visible.map(\.id) == ["a", "b", "g"])
        #expect(model.hiddenTrivialCount == 0)

        await model.refreshUsage(ledgerScanned: true)
        #expect(model.visible.map(\.id) == ["b", "g"])
        #expect(model.hiddenTrivialCount == 1)
        model.showTrivial = true
        #expect(model.visible.map(\.id) == ["a", "b", "g"])
        #expect(model.hiddenTrivialCount == 1)
    }

    @Test func feedHarnessRowsStayVisibleWithoutUsage() async {
        // Cursor and Grok Bot usage comes from polling an account feed,
        // not scanning logs: a missing row means unpolled, never empty.
        // Real zero-token stats still hide them.
        var grokTotals = UsageTotals()
        grokTotals.eventCount = 3
        let model = await model([
            session("c", "cursor", cwd: "/c/q", at: 1_791_699_000),
            session("b", "grok-bot", cwd: nil, at: 1_791_698_000),
            session("z", "grok-bot", cwd: nil, at: 1_791_697_000),
        ], [SessionUsage(sessionId: "z", harness: "grok-bot",
                         accountHome: "/h", cwd: nil, models: [],
                         totals: grokTotals,
                         firstTimestamp: now, lastTimestamp: now)])
        await model.load()
        await model.refreshUsage(ledgerScanned: true)
        // c and b have no usage at all yet stay visible; z's real
        // zero-token stats hide it.
        #expect(model.visible.map(\.id) == ["c", "b"])
        #expect(model.hiddenTrivialCount == 1)
    }

    @Test func everyMatchHiddenStillCountsForTheFooter() async {
        // When the rule hides every matching row the list is empty, but
        // the footer data stays: the count is there and "Show" reveals
        // the rows.
        let model = await model([
            session("a", "claude", cwd: "/c/q", at: 1_791_699_000),
            session("b", "codex", cwd: "/c/q", at: 1_791_698_000),
        ], [])
        await model.load()
        await model.refreshUsage(ledgerScanned: true)
        #expect(model.visible.isEmpty)
        #expect(model.hiddenTrivialCount == 2)
        model.showTrivial = true
        #expect(model.visible.map(\.id) == ["a", "b"])
    }

    @Test func probeLikeSessionsHideAndTheSettingBringsThemBack() async {
        var probeTotals = UsageTotals()
        probeTotals.inputTokens = 20
        probeTotals.outputTokens = 10
        probeTotals.eventCount = 1
        let probe = SessionUsage(sessionId: "p", harness: "claude",
                                 accountHome: "/Users/k/.claude", cwd: nil,
                                 models: ["opus"], totals: probeTotals,
                                 firstTimestamp: now, lastTimestamp: now)
        let model = await model([
            session("p", "claude", cwd: "/c/q", at: 1_791_699_000),
            session("b", "claude", cwd: "/c/q", at: 1_791_698_000),
        ], [probe, usage("b", "claude", models: [], cost: 0)])
        await model.load()
        #expect(model.visible.map(\.id) == ["b"])
        #expect(model.hiddenTrivialCount == 1)
        // Settings' "Hide accidental sessions" off shows everything.
        model.hideTrivial = false
        #expect(model.visible.map(\.id) == ["p", "b"])
        #expect(model.hiddenTrivialCount == 0)
    }

    @Test func aLongSessionIsNotTrivialBecauseStatsCoverItsWholeHistory() async {
        // The usage row is whole-history, so a session with plenty of
        // earlier replies stays visible however probe-like its last day
        // looks.
        var long = usage("l", "claude", models: [], cost: 0)
        long.totals.eventCount = 40
        let model = await model([session("l", "claude", cwd: "/c/q", at: 1_791_699_000)], [long])
        await model.load()
        #expect(model.visible.map(\.id) == ["l"])
        #expect(model.hiddenTrivialCount == 0)
    }

    // MARK: - Cached lists

    @Test func cachedListsFollowEveryInput() async {
        let ledger = FakeSessionLedger()
        let model = await model([
            session("a", "claude", cwd: "/c/quiver", at: 1_791_699_000),
            session("b", "codex", cwd: "/c/site", at: 1_791_698_000),
        ], [usage("a", "claude", models: ["opus"], cost: 1),
            usage("b", "codex", models: ["gpt"], cost: 0, home: "/Users/k/.codex")], ledger: ledger)
        await model.load()
        // Read twice, so the second answer comes from the cache.
        #expect(model.visible.map(\.id) == ["a", "b"])
        #expect(model.summary.sessions == 2)
        #expect(model.groupedByDay(calendar: calendar).flatMap { $0.rows.map(\.id) } == ["a", "b"])
        #expect(model.projects.map(\.name) == ["quiver", "site"])

        model.filter.harness = "codex"
        #expect(model.visible.map(\.id) == ["b"])
        #expect(model.summary.sessions == 1)
        #expect(model.groupedByDay(calendar: calendar).flatMap { $0.rows.map(\.id) } == ["b"])

        // b becomes trivial once its usage reads as empty after a scan.
        var empty = usage("b", "codex", models: ["gpt"], cost: 0, home: "/Users/k/.codex")
        empty.totals = UsageTotals()
        await model.refreshUsage(afterScans: 1, ledgerRevision: 0)
        #expect(model.visible.map(\.id) == ["b"])
        await ledger.set([
            usage("a", "claude", models: ["opus"], cost: 1), empty])
        await model.refreshUsage(afterScans: 2, ledgerRevision: 0)
        #expect(model.visible.isEmpty)
        #expect(model.hiddenTrivialCount == 1)
        model.showTrivial = true
        #expect(model.visible.map(\.id) == ["b"])
        model.hideTrivial = false
        #expect(model.hiddenTrivialCount == 0)
    }

    @Test func aWindowSlidesWithTheClockDespiteTheCache() async {
        // a is 7 days less 30 seconds old: inside the week, until the
        // clock moves on two minutes.
        let clock = TestClock(Date(timeIntervalSince1970: 1_791_700_000))
        let model = SessionsModel(
            source: FakeSessionSource(list: SessionList(sessions: [
                session("a", "claude", cwd: nil, at: 1_791_700_000 - 7 * 86_400 + 30)], failures: [])),
            ledger: FakeSessionLedger(), now: { clock.now }, calendar: calendar, searchIndexingEnabled: false)
        await model.load()
        model.filter.window = .week
        #expect(model.visible.map(\.id) == ["a"])
        clock.advance(120)
        #expect(model.visible.isEmpty)
        #expect(model.summary.sessions == 0)
    }

    @Test func aViewReadingACachedListHearsOfChanges() async {
        let model = await model([session("a", "claude", cwd: "/c/q", at: 1_791_699_000)],
                                [usage("a", "claude", models: ["opus"], cost: 1)])
        await model.load()
        _ = model.summary
        // This read is a cache hit: it must still register what it reads.
        let fired = Box(false)
        withObservationTracking {
            _ = model.summary
            _ = model.projects
        } onChange: { fired.value = true }
        model.filter.search = "nothing matches this"
        #expect(fired.value)

        let rowsFired = Box(false)
        withObservationTracking { _ = model.projects } onChange: { rowsFired.value = true }
        await model.refreshUsage()
        #expect(rowsFired.value)
    }

    @Test func aRepeatVisitWithNoNewScanReadsNoUsage() async {
        let ledger = FakeSessionLedger()
        let model = await model([session("a", "claude", cwd: nil, at: 1_791_699_000)], [], ledger: ledger)
        await model.load()
        let afterLoad = await ledger.ranges.count
        await model.refreshUsage(afterScans: 3, ledgerRevision: 5)
        await model.refreshUsage(afterScans: 3, ledgerRevision: 5)
        #expect(await ledger.ranges.count == afterLoad + 1)
        #expect(model.ledgerScanned)

        await ledger.set([usage("a", "claude", models: ["opus"], cost: 2)])
        await model.refreshUsage(afterScans: 4, ledgerRevision: 5)
        #expect(await ledger.ranges.count == afterLoad + 2)
        #expect(model.rows.first?.usage?.totals.costUSD == 2)

        // A feed import or a price change moves the revision, not the
        // scan count: that rereads too.
        await ledger.set([usage("a", "claude", models: ["opus"], cost: 3)])
        await model.refreshUsage(afterScans: 4, ledgerRevision: 6)
        #expect(await ledger.ranges.count == afterLoad + 3)
        #expect(model.rows.first?.usage?.totals.costUSD == 3)
    }

    @Test func allTimeStaysPutAsTheClockMoves() async {
        let clock = TestClock(Date(timeIntervalSince1970: 1_791_700_000))
        let model = SessionsModel(
            source: FakeSessionSource(list: SessionList(sessions: [
                session("a", "claude", cwd: nil, at: 1_000)], failures: [])),
            ledger: FakeSessionLedger(), now: { clock.now }, calendar: calendar, searchIndexingEnabled: false)
        await model.load()
        model.filter.window = .all
        let first = model.visible
        clock.advance(3600)
        #expect(model.visible.map(\.id) == first.map(\.id))
        #expect(model.summary.sessions == 1)
    }
}

/// The "Hide accidental sessions" rule itself: empty sessions are always
/// trivial, and a one-shot probe under a minute with a nearly empty reply
/// is too — except on harnesses whose logs carry no per-reply detail.
@Suite struct SessionFilterTests {
    private func usage(harness: String = "claude", responses: Int = 2,
                       span: TimeInterval = 59, input: Int = 500,
                       output: Int = 99, reasoning: Int = 0) -> SessionUsage {
        var totals = UsageTotals()
        totals.inputTokens = input
        totals.outputTokens = output
        totals.reasoningTokens = reasoning
        totals.eventCount = responses
        let start = Date(timeIntervalSince1970: 1_791_700_000)
        return SessionUsage(sessionId: "s", harness: harness, accountHome: "/h",
                            cwd: nil, models: ["m"], totals: totals,
                            firstTimestamp: start,
                            lastTimestamp: start.addingTimeInterval(span))
    }

    @Test func aTwoResponseMinuteWithAnAlmostEmptyReplyIsTrivial() {
        #expect(SessionFilter.isTrivial(usage: usage(responses: 2, span: 59, output: 99)))
    }

    @Test func aThirdResponseMakesItReal() {
        #expect(!SessionFilter.isTrivial(usage: usage(responses: 3, span: 59, output: 99)))
    }

    @Test func aHundredOutputTokensMakesItReal() {
        #expect(!SessionFilter.isTrivial(usage: usage(responses: 2, span: 59, output: 100)))
    }

    @Test func aMinuteLongSpanMakesItReal() {
        #expect(!SessionFilter.isTrivial(usage: usage(responses: 2, span: 60, output: 99)))
    }

    @Test func reasoningCountsTowardTheReply() {
        #expect(SessionFilter.isTrivial(usage: usage(output: 50, reasoning: 49)))
        #expect(!SessionFilter.isTrivial(usage: usage(output: 50, reasoning: 50)))
    }

    @Test func aSessionWithNoTokensIsTrivialHoweverLongItRan() {
        #expect(SessionFilter.isTrivial(usage: usage(responses: 50, span: 3600, input: 0, output: 0)))
    }

    @Test func aSessionWithNoResponsesIsTrivial() {
        #expect(SessionFilter.isTrivial(usage: usage(responses: 0, span: 3600, input: 5000, output: 5000)))
    }

    @Test func harnessesWithoutReplyDetailOnlyDropWhenEmpty() {
        for harness in ["cursor", "droid", "cline"] {
            // One short reply is indistinguishable from a real quick answer.
            #expect(!SessionFilter.isTrivial(usage: usage(harness: harness, responses: 1, span: 0, output: 5)))
            #expect(SessionFilter.isTrivial(usage: usage(harness: harness, responses: 1, span: 0, input: 0, output: 0)))
        }
    }
}

@Suite struct SessionProjectsTests {
    @Test func aWorktreesGitFileResolvesToTheMainRepository() {
        let probe: (String) -> SessionProjects.GitDir? = { folder in
            folder == "/Users/k/code/quiver/.worktrees/m4"
                ? .worktree("gitdir: /Users/k/code/quiver/.git/worktrees/m4\n") : nil
        }
        #expect(SessionProjects.project(of: "/Users/k/code/quiver/.worktrees/m4", gitDir: probe) == "quiver")
        // A path inside the worktree walks up to the same file.
        #expect(SessionProjects.project(of: "/Users/k/code/quiver/.worktrees/m4/src/deep", gitDir: probe) == "quiver")
    }

    @Test func aRelativeGitdirResolvesBesideTheWorktree() {
        // The submodule's `gitdir: ../.git/modules/sub` resolves to
        // vendor/.git, so vendor is the repository it belongs to.
        let probe: (String) -> SessionProjects.GitDir? = { folder in
            folder == "/Users/k/code/site/vendor/sub" ? .worktree("gitdir: ../.git/modules/sub\n") : nil
        }
        #expect(SessionProjects.project(of: "/Users/k/code/site/vendor/sub", gitDir: probe) == "vendor")
    }

    @Test func aPlainRepositoryTakesItsFolderName() {
        let probe: (String) -> SessionProjects.GitDir? = { folder in
            folder == "/Users/k/code/quiver" ? .repository : nil
        }
        #expect(SessionProjects.project(of: "/Users/k/code/quiver/app", gitDir: probe) == "quiver")
    }

    @Test func worktreeShapesResolveForPathsThatAreGone() {
        let missing: (String) -> SessionProjects.GitDir? = { _ in nil }
        #expect(SessionProjects.project(of: "/Users/k/code/quiver/.claude/worktrees/m4-sessions", gitDir: missing) == "quiver")
        #expect(SessionProjects.project(of: "/Users/k/.codex/worktrees/0199abcd/myrepo/src", gitDir: missing) == "myrepo")
        #expect(SessionProjects.project(of: "/Users/k/code/quiver/worktrees/feat-x", gitDir: missing) == "quiver")
        #expect(SessionProjects.project(of: "/Users/k/code/site", gitDir: missing) == "site")
    }

    @Test func hashLikeNamesAreDetected() {
        #expect(SessionProjects.isHashLike("3f2a9c1"))
        #expect(SessionProjects.isHashLike("0199ABCD-1111-2222-3333-444455556666"))
        #expect(SessionProjects.isHashLike("a1b2c3d4e5"))
        #expect(!SessionProjects.isHashLike("selflearn"))
        #expect(!SessionProjects.isHashLike("quiver"))
        #expect(!SessionProjects.isHashLike("api-v2"))
    }
}
