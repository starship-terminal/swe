import AppKit
import Foundation
import Observation
import SWECore
import WidgetKit

/// App-wide state shared by the window and the menu bar panel.
@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    private(set) var swe: SweClient
    /// Carries Quiver.app's preferences over on first launch. Runs before
    /// every other stored default: initializers run in declaration order.
    @ObservationIgnored private let defaultsMigrated: Void = LegacyDefaults.migrate()
    /// The open page, kept across launches. It stays put while a card is
    /// over the window (a notification or the menu bar panel can ask to
    /// switch): the card belongs to this page, and switching would drop it
    /// and anything unsaved in it without its close check.
    var selection: AppSection? {
        get { openSection }
        set {
            guard !SheetOverlayRegistry.shared.anyCovered else { return }
            // Asking for a native page brings This Mac back from a hub.
            machines.showThisMac()
            guard newValue != openSection else { return }
            openSection = newValue
            UserDefaults.standard.set(newValue?.rawValue, forKey: "lastSection")
        }
    }

    private var openSection: AppSection? = UserDefaults.standard.string(forKey: "lastSection")
        .flatMap(AppSection.init(rawValue:)) ?? .overview

    /// Tapping the sidebar item or tab of the view already shown bumps
    /// this; a detail page open there goes back through its own ask.
    var navReselects = 0

    /// A sidebar click or its ⌘-number shortcut: opens the page, or asks
    /// the page already open to go back.
    func select(_ section: AppSection) {
        if selection == section, !machines.showsHub {
            navReselects += 1
        } else {
            selection = section
        }
    }

    /// The ⌘K global search card over the window.
    var showsSearch = false

    /// What the ⌘K search's session pick asks the Sessions page to open:
    /// the list row's id (the same session under two accounts is two
    /// rows) and the hub `harness:id` key for the chat fallback.
    struct PendingSession: Equatable, Sendable {
        var key: String
        var rowId: String
    }
    var pendingSession: PendingSession?
    /// A task id the Tasks page's detail opens on, then clears.
    var pendingTask: String?
    /// The Harnesses page's filter text set by the ⌘K search, then
    /// cleared once the page's model is up.
    var pendingHarnessQuery: String?
    /// A hook's `harness|source` row key the Hooks page opens in its
    /// panel, then clears.
    var pendingHook: String?
    /// A worktree path the Worktrees page opens in its detail pane, then
    /// clears.
    var pendingWorktree: String?

    /// A hub session key whose chat the Sessions page opens next, then
    /// clears. Set through `openSessionChat(key:)`.
    var pendingChat: String?

    /// Switches to Sessions and opens `key`'s chat there, for a session
    /// started from elsewhere in the app (Resolve with agent). The page
    /// switch waits until the card that asked has closed: `selection`
    /// stays put while one covers the window.
    func openSessionChat(key: String) {
        pendingChat = key
        whenUncovered { [weak self] in self?.selection = .sessions }
    }

    /// The ⌘K search's pick: close the card, then apply the target once
    /// the window is uncovered — `selection` stands down under a card.
    func open(_ target: GlobalSearchItem.Target) {
        showsSearch = false
        whenUncovered { [weak self] in self?.routeSearchTarget(target) }
    }

    private func whenUncovered(_ action: @escaping @MainActor () -> Void) {
        guard SheetOverlayRegistry.shared.anyCovered else { return action() }
        withObservationTracking {
            _ = SheetOverlayRegistry.shared.anyCovered
        } onChange: { [self] in
            Task { @MainActor [weak self] in self?.whenUncovered(action) }
        }
    }

    /// A toast's Fix button: land on Settings › General, where the
    /// Doctor section lists the issue's row live from the same store.
    func openDoctor() {
        UserDefaults.standard.set(SettingsSection.general.rawValue, forKey: "settingsSection")
        select(.settings)
    }

    /// The sidebar pinned to an icon rail; hovering it floats it open.
    var sidebarCollapsed = UserDefaults.standard.bool(forKey: "sidebarCollapsed") {
        didSet { UserDefaults.standard.set(sidebarCollapsed, forKey: "sidebarCollapsed") }
    }

    /// The pinned sidebar's width, clamped on load so a stale value
    /// cannot leave the window unusable.
    var sidebarWidth: Double = {
        let stored = UserDefaults.standard.object(forKey: "sidebarWidth") as? Double
        return stored.map(SidebarWidth.clamped) ?? SidebarWidth.standard
    }() {
        didSet { UserDefaults.standard.set(sidebarWidth, forKey: "sidebarWidth") }
    }

    /// Every non-drag width change goes through here, so menu commands
    /// and VoiceOver share the clamp and the persisted write.
    func adjustSidebarWidth(by delta: Double) {
        sidebarWidth = SidebarWidth.clamped(sidebarWidth + delta)
    }

    func resetSidebarWidth() {
        sidebarWidth = SidebarWidth.standard
    }

    /// Opens (or reopens) the main window. Captured from the window's scene,
    /// because the menu bar panel lives outside SwiftUI's scene graph and
    /// its own `openWindow` cannot reach the `Window` scene.
    @ObservationIgnored var openMainWindow: (() -> Void)?

    /// Clicked a GitHub banner: raise the window and land on the repo's
    /// board — the tab its subject type belongs to — or the Inbox when
    /// the repo isn't pinned (boards only exist for pins).
    func openGitHub(repo: String, type: String) {
        NSApp.activate()
        if let open = openMainWindow {
            open()
        } else if let window = NSApp.windows.first(where: { $0.identifier?.rawValue == "main" }) {
            window.makeKeyAndOrderFront(nil)
        }
        // The click can precede the Repositories page — and so its pin
        // fetch. An empty pin list is "not loaded yet", not "unpinned":
        // load before routing, or a pinned repo would land on Inbox.
        if github.pins.isEmpty {
            Task {
                await github.load(swe: swe)
                routeGitHub(repo: repo, type: type)
            }
        } else {
            routeGitHub(repo: repo, type: type)
        }
    }

    /// A service status banner's click.
    func openServiceStatus() {
        NSApp.activate()
        if let open = openMainWindow {
            open()
        } else if let window = NSApp.windows.first(where: { $0.identifier?.rawValue == "main" }) {
            window.makeKeyAndOrderFront(nil)
        }
        selection = .serviceStatus
    }

    private func routeGitHub(repo: String, type: String) {
        if github.pins.contains(where: { $0.name == repo }) {
            github.selected = repo
            // A click-through lands on the repo's board section, never
            // inside a focus page left open for another repo.
            github.focused = nil
            // A filter that hid the repo would make the click-through
            // land on nothing; the target section also needs scrolling
            // to once it's rendered.
            github.filter = ""
            github.scrollTarget = repo
            // Before the tab: a one-repo tab (Releases) needs the list
            // narrowed first, or going back to all repos resets it.
            github.shownRepo = repo
            github.tab = switch type {
            case "PullRequest": .pulls
            case "Issue": .issues
            case "CheckSuite", "CheckRun": .actions
            case "Release": .releases
            default: .pulls
            }
            selection = .repositories
        } else {
            selection = .inbox
        }
    }

    /// The swe chosen in Settings; empty means whatever PATH finds.
    var swePath: String {
        didSet {
            UserDefaults.standard.set(swePath, forKey: SweLocation.defaultsKey)
            swe = Self.client(for: swePath)
            skills.use(swe, for: swePath)
            hub = Self.localHub(swe: swe)
            others = OtherLimitsModel(swe: swe, extraAccounts: extraAccountsProvider,
                                      issues: issues)
            // A new model starts at swe's five minutes; keep the window
            // Settings chose, as start() and its watcher set it.
            others.cacheTTL = limits.refreshFreshSeconds
            Task { await others.load() }
            doctor = DoctorModel(swe: swe, work: plugins.work)
            accountSync = AccountSyncModel(swe: swe)
            // The "swe too old" verdicts were about the old build.
            Task { [codexUsage, cursorFeed] in
                await codexUsage?.swePathChanged()
                await cursorFeed?.swePathChanged()
            }
        }
    }

    /// Which terminal Open in Terminal and Sign in use.
    /// Saved menu bar choices brought up to date before they're read.
    @ObservationIgnored private let migrated: Void = MenuBarMigrations.run(on: .standard)

    /// What the menu bar item shows, and whether it's there at all.
    var menuBar = MenuBarDisplay(defaults: .standard) {
        didSet {
            menuBar.save(to: .standard)
            if menuBar.isVisible != oldValue.isVisible { applyDockPresence() }
        }
    }

    /// Which tabs the menu bar popup has, in what order, and which is open.
    var popupTabs = PopupTabs(defaults: .standard) {
        didSet { popupTabs.save(to: .standard) }
    }
    /// What the popup's Usage tab lists.
    var popup = MenuBarPopup(defaults: .standard) {
        didSet { popup.save(to: .standard) }
    }
    /// What the popup's Compute tab charts.
    var computePopup = ComputePopup(defaults: .standard) {
        didSet { computePopup.save(to: .standard) }
    }
    /// What each desktop widget lists; a change rewrites their snapshot
    /// and redraws them right away.
    var widgetSelection = WidgetSelection(defaults: .standard) {
        didSet {
            widgetSelection.save(to: .standard)
            let style = widgetHarnessStyle
            limits.writeWidgets(overview: overview, selection: widgetSelection,
                                harnessOrder: style.order, harnessColors: style.colors,
                                compute: widgetCompute, force: true)
        }
    }

    /// The Compute widget's own section: the running harnesses' cost at
    /// the newest trend tick, with the chosen temperature lines.
    var widgetCompute: WidgetSnapshot.ComputeSection {
        WidgetSnapshot.ComputeSection(history: compute.history,
                                      order: compute.matchers.map(\.harness),
                                      temperatureLines: compute.temperatureLines,
                                      selection: widgetSelection)
    }

    /// The Settings provider order and each harness's colour as hex, as
    /// the widget snapshot's per-harness lines want them.
    var widgetHarnessStyle: (order: [String], colors: [String: String]) {
        let order = providers.rows.map(\.provider.id)
        var harnesses = Set(order)
        harnesses.formUnion((overview?.activityRows ?? []).map(\.harness))
        let colors = Dictionary(uniqueKeysWithValues: harnesses.map { ($0, HarnessColor.of($0).hex) })
        return (order, colors)
    }

    var terminal: TerminalApp {
        didSet { UserDefaults.standard.set(terminal.rawValue, forKey: "terminalApp") }
    }

    /// The first-run setup window: open at launch on a Mac with no shared
    /// AGENTS.md that hasn't finished or skipped it, or from Settings.
    var showsFirstRunSetup = false

    /// Settings › Integrations › Remote opens its setup checklist the next
    /// time it shows, then clears this.
    var pendingRemoteSetup = false

    /// Setup finished or skipped: never offered at launch again. When it
    /// ran, every page whose data it changed loads afresh (stars,
    /// harnesses, skills links, MCP servers), since their own 60-second
    /// staleness checks would keep showing what was there before.
    ///
    /// `remoteSetup` is the wizard's "Set up now" on its phone step: it
    /// opens Settings › Integrations, whose Remote section then opens the
    /// setup checklist (`pendingRemoteSetup`).
    func closeFirstRunSetup(reload: Bool = false, remoteSetup: Bool = false) {
        UserDefaults.standard.set(true, forKey: FirstRun.defaultsKey)
        showsFirstRunSetup = false
        if remoteSetup {
            UserDefaults.standard.set(SettingsSection.integrations.rawValue, forKey: "settingsSection")
            // Past `selection`'s covered-window guard: the card covering
            // the window is this wizard, which is closing, not a page's.
            openSection = .settings
            UserDefaults.standard.set(AppSection.settings.rawValue, forKey: "lastSection")
            pendingRemoteSetup = true
        }
        guard reload else { return }
        Task { [others, providers, skills, mcp, swe, swePath] in
            await others.load()
            await providers.load()
            await skills.load(for: swePath)
            await mcp.load(swe: swe, for: swePath)
        }
    }

    /// Light, dark, or macOS's own setting (the default).
    var appearance: AppearanceMode {
        didSet {
            UserDefaults.standard.set(appearance.rawValue, forKey: AppearanceMode.defaultsKey)
            applyAppearance()
        }
    }

    /// Every window and the menu bar panel follow NSApp's appearance; the
    /// Theme colours resolve against it.
    func applyAppearance() {
        NSApp.appearance = switch appearance {
        case .system: nil
        case .light: NSAppearance(named: .aqua)
        case .dark: NSAppearance(named: .darkAqua)
        }
    }

    /// On by default. Off makes SWE a menu bar app: no Dock icon and no
    /// place in the app switcher, unless the menu bar item is hidden too.
    var showsInDock: Bool {
        didSet {
            UserDefaults.standard.set(showsInDock, forKey: DockPresence.defaultsKey)
            applyDockPresence()
        }
    }

    /// Whether the Dock icon actually shows, after the menu bar item rule.
    var dockIconShown: Bool {
        DockPresence.showsIcon(requested: showsInDock, menuBarVisible: menuBar.isVisible)
    }

    func applyDockPresence() {
        let policy: NSApplication.ActivationPolicy = dockIconShown ? .regular : .accessory
        guard NSApp.activationPolicy() != policy else { return }
        NSApp.setActivationPolicy(policy)
        // The switch sends the open window behind other apps, so bring it back.
        NSApp.activate()
    }

    let accounts: AccountsModel
    /// Usage failures the user can act on, persisted under ~/.swe so
    /// toasts deduplicate across polls and restarts; Doctor's rows and
    /// the Settings badge read it. Rebuilt never — the store is file-backed,
    /// so a new swe keeps the same issues.
    let issues: IssuesModel
    /// Limits of the harnesses with no SWE account, from swe.
    private(set) var others: OtherLimitsModel
    /// Settings › Doctor's report and its runs. Held here, not by the
    /// section, so an install keeps its row (and the one-at-a-time guard)
    /// while you look at another section.
    private(set) var doctor: DoctorModel
    /// The Accounts page's "Shared content" switches (accounts.sync.*).
    private(set) var accountSync: AccountSyncModel
    /// Account cards whose harness is starred in swe's registry: an
    /// unstarred Claude or Codex hides its card, and its limit with it,
    /// until it is starred again. Extra Devin/Cursor accounts are harness
    /// cards instead (`others`), so they never enter this list.
    var starredAccountCards: [AccountsModel.Card] {
        accounts.cards.filter {
            $0.profile.vendor.hasDefaultProfile && others.isStarred($0.profile.vendor.rawValue)
        }
    }
    /// Every limit together: the status item, notifications and widgets.
    let limits: LimitsMonitor
    /// Where each harness's desktop app is installed, if anywhere.
    let apps = AppLocator()
    /// Extra Devin/Cursor profiles with their account dirs, shared by every
    /// OtherLimitsModel a swe-path change creates.
    @ObservationIgnored private var extraAccountsProvider: @MainActor () -> [(Profile, ProfilePaths)] = { [] }
    /// Every Codex account's CODEX_HOME: the default `~/.codex` plus
    /// each Codex profile's home in the profile store.
    @ObservationIgnored private(set) var codexHomes: @MainActor () -> [String] = { [] }
    /// App-wide so unsaved AGENTS.md edits outlive a trip to another page.
    let agentsMd = AgentsModel()
    /// The same for chapter edits under ~/.swe/AGENTS_MD.
    let chapters = ChaptersModel()
    private(set) var apiKeys: APIKeysModel

    /// The shell file of API keys; empty means ~/.swe/secrets/providers/.api_keys.
    var apiKeysPath: String {
        didSet {
            UserDefaults.standard.set(apiKeysPath, forKey: "apiKeysPath")
            apiKeys = Self.keysModel(path: apiKeysPath)
        }
    }

    /// The managed MCP servers folder; empty means ~/.swe/mcp/servers.
    var mcpServersPath: String {
        didSet { UserDefaults.standard.set(mcpServersPath, forKey: "mcpServersPath") }
    }

    /// The persisted value; defaults on when nothing was stored yet.
    private static func hidesTrivialSessions() -> Bool {
        UserDefaults.standard.object(forKey: "hideTrivialSessions") == nil
            || UserDefaults.standard.bool(forKey: "hideTrivialSessions")
    }

    /// Settings → "Hide accidental sessions": drops empty and probe-like
    /// sessions from the Sessions page and the calendar. Default on.
    var hideTrivialSessions: Bool {
        didSet {
            UserDefaults.standard.set(hideTrivialSessions, forKey: "hideTrivialSessions")
            sessions?.hideTrivial = hideTrivialSessions
            ledger?.setHidesTrivialSessions(hideTrivialSessions)
            calendar?.noteScanCompleted()
            if let calendar { Task { await calendar.load() } }
        }
    }

    /// The app "Open in editor" uses, kept by name; empty means the first found.
    var codeEditor: String {
        didSet { UserDefaults.standard.set(codeEditor, forKey: "codeEditor") }
    }

    /// The editor "Open in editor" uses: the chosen one, or the first installed.
    var editor: CodeEditor? {
        let installed = CodeEditors.installed()
        return installed.first { $0.name == codeEditor } ?? installed.first
    }
    /// Nil when the ledger database could not be opened; `overviewError` says why.
    let overview: OverviewModel?
    let overviewError: String?
    let serviceStatus: ServiceStatusModel
    /// Which providers show, in what order (Settings).
    let providers: ProviderVisibilityModel
    /// Which pages the sidebar lists, and their order inside each group.
    let sidebar = SidebarVisibilityModel(store: UserDefaultsStore())
    /// The sidebar's machine switcher: This Mac, or another machine's hub
    /// whose web app then fills the window.
    let machines = MachineSwitcher(store: UserDefaultsStore())
    /// One web view per hub, kept while the app runs so switching back and
    /// forth keeps the page where it was.
    let hubWebViews = HubWebViews()
    /// The order of Settings › Providers › Inference's rows.
    let inferenceOrder = InferenceOrder(store: UserDefaultsStore())
    /// Per-tab row orders and "moved here" marks for the non-inference
    /// tabs, each under its own store key so they never entangle.
    private let keyOrders = Dictionary(uniqueKeysWithValues: KeysSection.allCases.map {
        ($0, KeysOrder(store: UserDefaultsStore(), section: $0))
    })
    private let keyMarks = Dictionary(uniqueKeysWithValues: KeysSection.allCases.map {
        ($0, MarkedKeys(store: UserDefaultsStore(), section: $0))
    })

    /// The row order for one of the non-inference Providers tabs.
    func order(for section: KeysSection) -> KeysOrder { keyOrders[section]! }
    /// The variables filed to that tab that no catalogue claims.
    func marks(for section: KeysSection) -> MarkedKeys { keyMarks[section]! }
    /// What the starred harnesses are running right now (the Compute page).
    let compute: ComputeModel
    /// The Compute page's fan popover; a manual speed lives in a root
    /// helper that `fan/control.json` steers.
    let fans: FanControl
    /// Nil when the ledger is unavailable, like `overview`.
    let sessions: SessionsModel?
    /// Kept for `hideTrivialSessions`; nil with `sessions`.
    private let ledger: Ledger?
    /// The Calendar page's activity blocks; nil with `sessions`.
    let calendar: CalendarModel?
    /// Cursor's per-event usage feed into the ledger; nil with `sessions`.
    let cursorFeed: CursorUsageFeed?
    /// Devin cloud sessions from Devin's API, for Sessions and the
    /// calendar; nil with `sessions`.
    let devinCloud: DevinCloudSync?
    /// Each Codex account's daily totals against what this Mac logged;
    /// nil with `sessions`.
    let codexUsage: CodexAccountUsage?
    /// Every account card's weekly and monthly tokens on the Accounts
    /// page, from the ledger; nil with `sessions`.
    let accountHistory: AccountHistory?
    /// The Pricing page's per-model rates; nil with `sessions`.
    let pricingModel: PricingModel?
    /// The Models page's benchmarks crossed with usage; nil with `sessions`.
    let models: ModelsModel?
    /// The Settings > Services section's keys and probe results.
    let services: ServicesModel
    /// Settings › Integrations › Remote's setup checklist. Kept for the
    /// app's life, not the page's: a `--apply` keeps running after the
    /// sheet or page closes, and its `applying` guard must still stop a
    /// second one from a reopened page. Asks for the current swe per call.
    let remoteSetup = RemoteSetupModel(swe: { AppModel.shared.swe })
    /// The Settings > Experimental section's session-search switch: it hands
    /// the ledger a `SearchIndexer` while on, and keeps the index current.
    let search: SearchIndexModel
    /// The Skills, MCP servers and Plugins pages' last lists, kept while the app runs.
    let skills: SkillsModel
    let mcp: McpModel
    /// The GitHub pages: pins, per-repo boards and the inbox, read
    /// through `swe gh`. The model gets the current swe per call, so a
    /// path change needs no rebuild here. Boards and the inbox persist
    /// to ~/.swe/cache for a warm start.
    let github: GitHubModel
    /// The Tasks page: the work items the hub's agents are on, read
    /// through this Mac's hub (`GET /v1/app/tasks`).
    let tasks = TasksModel()
    /// Polls the inbox while the app runs — regardless of which page is
    /// open — and turns new notifications into banners plus board
    /// invalidations. Assigned in init, where `~/.swe` is known.
    let githubNotify: GitHubNotifier

    /// Whether the main window can be seen: open, not minimised, not
    /// hidden with the app and not fully covered. Polls that only serve
    /// someone looking follow it.
    var mainWindowOnScreen = false {
        didSet { githubNotify.windowVisible = mainWindowOnScreen }
    }
    /// Banners provider incidents from the Service status poll, which it
    /// also drives. Assigned in init, where `~/.swe` is known.
    let serviceNotify: ServiceStatusNotifier
    /// The MCP servers page's "Server folders" section.
    let mcpRepo = McpServerRepoModel()
    /// The Settings > General Updates card's build and release state.
    let updates: UpdatesModel
    let plugins: PluginsModel
    /// The Hooks page's list: the scripts under ~/.swe/hooks, their link
    /// states and the check's findings. It asks for the current swe per
    /// call, so a path change needs no rebuild here.
    let hooks: HooksModel
    /// The Hooks page's run counts (`swe hooks stats`), kept per window so
    /// a trip to another page shows the last numbers at once.
    let hookStats = HookStatsModel()
    /// The Worktrees page's list, from `swe wt list`. A debug build started
    /// with `SWE_WT_FIXTURE` answers from `WorktreesFixture` instead.
    let worktrees: WorktreesModel
    /// Every plugin in every marketplace: the Plugins page's Explore tab,
    /// and what the Skills page moves plugin skills out by.
    let pluginCatalog: PluginCatalogModel
    /// The Plugins page's tab, so another page can open Explore.
    var pluginsTab: PluginsTab = .explore
    /// SWE's display names for marketplaces and plugins, from
    /// ~/.swe/config/plugin-display-names.json.
    var primitiveNames: PluginDisplayNames
    /// The last display-name save, which the next one waits for.
    @ObservationIgnored var displayNamesSave: Task<Void, Never>?
    private let userHome = FileManager.default.homeDirectoryForCurrentUser
    /// This Mac's hub, which the Sessions page's chat talks to.
    private(set) var hub: LocalHub

    static func localHub(swe: SweClient) -> LocalHub {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return LocalHub(locator: swe, store: HubDeviceStore(url: HubDeviceStore.defaultURL(home: home)))
    }

    /// `~/.quiver` → `~/.swe`, mirroring `swe.paths._resolve_data_dir`.
    ///
    /// The app reaches the data root before any bundled `swe` command may
    /// have run, so the move has to happen here too: when `~/.swe` does
    /// not exist and `~/.quiver` is a real directory, rename it in place
    /// and leave a `~/.quiver` → `.swe` symlink so harness links and older
    /// copies of the CLI keep resolving. A `~/.quiver` that is itself a
    /// link is mirrored as `~/.swe`, never moved out from under whatever
    /// put it there, unless it points at `~/.swe` itself (the link an
    /// earlier move left, after `~/.swe` was deleted): mirroring that would
    /// make `~/.swe` a link to itself, and every path under it would fail.
    /// On any failure the old root keeps serving.
    /// `path` with every link in its folder followed, for a last part
    /// that need not exist. realpath(3) on the folder, since Foundation's
    /// resolving standardizes first and folds "link/.." away before the
    /// link is followed. Python's `os.path.realpath` twin.
    private static func realPath(_ path: String) -> String {
        let ns = path as NSString
        guard let folder = realpath(ns.deletingLastPathComponent, nil) else {
            return ns.standardizingPath
        }
        defer { free(folder) }
        return String(cString: folder) + "/" + ns.lastPathComponent
    }

    static func migrateDataRoot(home: URL) {
        let fm = FileManager.default
        let new = home.appending(path: ".swe")
        let old = home.appending(path: ".quiver")
        if (try? fm.attributesOfItem(atPath: new.path)) != nil { return }
        guard let attrs = try? fm.attributesOfItem(atPath: old.path),
              let type = attrs[.type] as? FileAttributeType else { return }
        if type == .typeSymbolicLink {
            if let target = try? fm.destinationOfSymbolicLink(atPath: old.path) {
                // Links followed, not just "..": "link/../.swe" through a
                // linked folder is somewhere else entirely.
                let full = target.hasPrefix("/") ? target : home.path + "/" + target
                guard Self.realPath(full) != Self.realPath(new.path) else { return }
                try? fm.createSymbolicLink(atPath: new.path, withDestinationPath: target)
            }
            return
        }
        guard type == .typeDirectory else { return }
        do {
            try fm.moveItem(at: old, to: new)
            try? fm.createSymbolicLink(atPath: old.path, withDestinationPath: ".swe")
        } catch {
            // The old root keeps serving; the CLI can still move it later.
        }
    }

    init() {
        Self.migrateDataRoot(home: FileManager.default.homeDirectoryForCurrentUser)
        let saved = UserDefaults.standard.string(forKey: SweLocation.defaultsKey) ?? ""
        swePath = saved
        let client = Self.client(for: saved)
        swe = client
        hub = Self.localHub(swe: client)
        terminal = UserDefaults.standard.string(forKey: "terminalApp")
            .flatMap(TerminalApp.init(rawValue:)) ?? .terminal
        appearance = AppearanceMode(saved: UserDefaults.standard.string(forKey: AppearanceMode.defaultsKey))
        showsInDock = UserDefaults.standard.object(forKey: DockPresence.defaultsKey) == nil
            || UserDefaults.standard.bool(forKey: DockPresence.defaultsKey)

        let keysPath = UserDefaults.standard.string(forKey: "apiKeysPath") ?? ""
        apiKeysPath = keysPath
        apiKeys = Self.keysModel(path: keysPath)
        mcpServersPath = UserDefaults.standard.string(forKey: "mcpServersPath") ?? ""
        hideTrivialSessions = Self.hidesTrivialSessions()
        codeEditor = UserDefaults.standard.string(forKey: "codeEditor") ?? ""

        // Everything SWE keeps lives under ~/.swe (see AGENTS.md).
        let home = FileManager.default.homeDirectoryForCurrentUser
        let swe = home.appending(path: ".swe")
        primitiveNames = PluginDisplayNames.load(from: PluginDisplayNames.file(home: home))
        limits = LimitsMonitor(swe: swe)
        github = GitHubModel(
            snapshotFile: swe.appending(path: "cache/github-snapshot.json"),
            historyFile: swe.appending(path: "cache/github-history.db"))
        githubNotify = GitHubNotifier(swe: swe, model: github)
        serviceNotify = ServiceStatusNotifier(swe: swe)
        let usage = UsageService(
            transport: URLSessionTransport(),
            secrets: KeychainCLI(),
            files: LocalFiles(),
            cacheURL: swe.appending(path: "cache/app-usage.json"))
        // SWE's own service keys live in Keychain under their own
        // service, written and read in-process so the value never sits
        // on a tool's argv; only dates go in config/services.json. A
        // test build swaps the keychain service so it never touches the
        // real items.
        let serviceCredentials = ServiceCredentials(
            secrets: SecItemKeychain(),
            service: ProcessInfo.processInfo.environment["SWE_SERVICES_KEYCHAIN"]
                ?? ProcessInfo.processInfo.environment["QUIVER_SERVICES_KEYCHAIN"]
                ?? ServiceCredentials.keychainService,
            metadataURL: swe.appending(path: "config/services.json"))
        services = ServicesModel(
            credentials: serviceCredentials,
            transport: URLSessionTransport())
        let store = ProfileStore(root: swe.appending(path: "accounts"), userHome: home)
        // Non-default Devin/Cursor profiles are accounts of a harness: swe
        // reads their usage from the account's own directory.
        extraAccountsProvider = {
            ((try? store.load()) ?? [])
                .filter { !$0.isDefault && !$0.vendor.hasDefaultProfile }
                .map { ($0, store.paths(for: $0)) }
        }
        issues = IssuesModel(store: IssueStore(url: IssueStore.file(home: home)))
        others = OtherLimitsModel(swe: client, extraAccounts: extraAccountsProvider,
                                  issues: issues)
        // One plugin change at a time, whichever Plugins tab or Doctor
        // repair starts it.
        let pluginWork = PluginWork()
        doctor = DoctorModel(swe: client, work: pluginWork)
        accountSync = AccountSyncModel(swe: client)
        codexHomes = {
            var homes = [home.appending(path: ".codex").path]
            for profile in (try? store.load()) ?? []
            where profile.vendor == .codex && !profile.isDefault {
                homes.append(store.paths(for: profile).home.path)
            }
            return homes
        }
        var terminalChoice: () -> TerminalApp = { .terminal }
        accounts = AccountsModel(store: store, usage: usage, launcher: ProcessLauncher(),
                                 terminal: { terminalChoice() },
                                 terminalInstalled: { $0.isInstalled },
                                 installedApp: { [apps] in apps.installedApp(for: $0) })

        // Built once per profiles.json mtime, not per call: the calendar
        // asks for account labels once per rendered block.
        let directoryCache = CachedAccountDirectory(
            file: swe.appending(path: "accounts/profiles.json")) {
                AccountDirectory(profiles: (try? store.load()) ?? [],
                                 paths: store.paths(for:), home: home)
            }
        let directory: @Sendable () -> AccountDirectory = { directoryCache.current }
        let hidesTrivial = Self.hidesTrivialSessions()
        do {
            // Prices with their history, AA's models, and the join between
            // them: data, so clearing caches never loses price history.
            let catalog = CatalogDatabase(url: CatalogDatabase.resolvedURL(home: home),
                                          linksFile: swe.appending(path: "config/model-links.json"))
            // User prices (config/pricing.json) win over both published lists.
            let pricing = PricingStore(database: catalog,
                                       legacyCacheFile: swe.appending(path: "cache/pricing/litellm.json"),
                                       overridesFile: swe.appending(path: "config/pricing.json"))
            // Data, not cache: its rows outlive the logs they came from.
            // Earlier builds kept it in cache/ledger.
            let ledgerData = swe.appending(path: "data/ledger")
            let ledgerDirectory = LedgerLocation.prepare(directory: ledgerData,
                                                         legacy: swe.appending(path: "cache/ledger"))
            let ledger = try Ledger(directory: ledgerDirectory, backups: ledgerData.appending(path: "backups"),
                                    pricing: (try? pricing.load()) ?? .empty)
            self.ledger = ledger
            ledger.setHidesTrivialSessions(hidesTrivial)
            overview = OverviewModel(ledger: ledger, roots: { directory().roots },
                                     accountLabel: { directory().label(for: $0) },
                                     refreshPricing: { await pricing.refreshIfStale() },
                                     scanWatchInterval: .seconds(1))
            overviewError = nil
            sessions = SessionsModel(source: CurrentSwe(), ledger: ledger,
                                     defaultHomes: [home.appending(path: ".claude").path, home.appending(path: ".codex").path],
                                     snapshots: SessionSnapshotFile(url: swe.appending(path: "cache/sessions.json")),
                                     // Which desktop session started each Claude session, from the
                                     // default desktop root and every Claude profile's own one.
                                     parents: {
                                         var roots = [("claude-desktop-sessions",
                                                       ClaudeDesktopSessions.defaultRoot(home: home))]
                                         roots += ((try? store.load()) ?? [])
                                             .filter { $0.vendor == .claude && !$0.isDefault }
                                             .compactMap { profile in
                                                 store.paths(for: profile).desktopData.map {
                                                     ("claude-desktop-sessions-\(profile.id)",
                                                      $0.appending(path: "claude-code-sessions"))
                                                 }
                                             }
                                         var parents: [String: SessionParent] = [:]
                                         for (name, root) in roots {
                                             for (id, parent) in ClaudeDesktopSessions(
                                                 root: root,
                                                 cacheFile: swe.appending(path: "cache/\(name).json"))
                                                 .parents() {
                                                 parents[id] = parent
                                             }
                                         }
                                         return parents
                                     },
                                     accountLabel: { directory().extraLabel(for: $0) })
            let calendarCache = swe.appending(path: "cache/calendar")
            let devinCloudDirectory = DevinCloud.resolvedDirectory(home: home)
            let calendarSnapshots = SnapshotFile<CalendarSnapshot>(
                url: swe.appending(path: "cache/app-calendar.json"))
            let fileProviders: [any ActivityProviding] = [
                DroidActivity(sessionsRoot: home.appending(path: ".factory/sessions"),
                              accountHome: home.appending(path: ".factory").path,
                              index: ActivityFileIndex(url: calendarCache.appending(path: "droid.json")),
                              filter: { await ledger.filterTrivialActivitySessions($0, harness: "droid") }),
                PiActivity(sessionsRoot: home.appending(path: ".pi/agent/sessions"),
                           accountHome: home.appending(path: ".pi").path,
                           index: ActivityFileIndex(url: calendarCache.appending(path: "pi.json")),
                           filter: { await ledger.filterTrivialActivitySessions($0, harness: "pi") }),
                FreebuffActivity(projectsRoot: home.appending(path: ".config/manicode/projects"),
                                 accountHome: home.appending(path: ".config/manicode").path,
                                 index: ActivityFileIndex(url: calendarCache.appending(path: "freebuff.json"))),
                DevinCloudActivity(directory: devinCloudDirectory),
                CursorAppActivity(userDataDirs: {
                    var dirs = [home.appending(path: "Library/Application Support/Cursor")]
                    dirs += ((try? store.load()) ?? [])
                        .filter { $0.vendor == .cursor && !$0.isDefault }
                        .compactMap { store.paths(for: $0).desktopData }
                    return dirs
                }, index: ActivityFileIndex(url: calendarCache.appending(path: "cursor.json"))),
            ]
            calendar = CalendarModel(providers: [ledger] + fileProviders,
                                     accountLabel: { directory().label(for: $0) },
                                     knownAccounts: { directory().homes(for: $0) },
                                     snapshots: calendarSnapshots,
                                     restored: calendarSnapshots.load())
            // Cursor's account usage feed covers the editor, cursor-agent,
            // cloud agents and Grok Bot; each account's user-data dir is
            // both the ledger's account dim and swe's --account-dir.
            // An ingest that wrote rows refreshes the same way a scan does.
            cursorFeed = CursorUsageFeed(
                // The swe chosen in Settings right now, like
                // ProviderVisibility: the feed can't capture `self` here,
                // and a SweClient is only the path, so reading the saved
                // path at call time is the live client.
                ledger: ledger,
                swe: { SweClient(runner: ProcessRunner(
                    executable: SweLocation.executable(
                        override: UserDefaults.standard.string(
                            forKey: SweLocation.defaultsKey)))) },
                accounts: {
                    var accounts = [CursorUsageFeed.Account(
                        home: home.appending(path: "Library/Application Support/Cursor").path)]
                    for profile in (try? store.load()) ?? []
                    where profile.vendor == .cursor && !profile.isDefault {
                        if let dir = store.paths(for: profile).desktopData {
                            accounts.append(
                                CursorUsageFeed.Account(home: dir.path, accountDir: dir.path))
                        }
                    }
                    return accounts
                },
                progressFile: swe.appending(path: "cache/cursor-feed.json"),
                onIngested: { [overview] in await overview?.reloadAfterLedgerWrite() })
            // The user's own Devin cloud sessions, with the Services token.
            // A sync that changed them rejoins the Sessions list and
            // redraws the calendar.
            devinCloud = DevinCloudSync(
                directory: devinCloudDirectory,
                credentials: {
                    guard let token = await serviceCredentials.value(for: "DEVIN_API_KEY") else { return nil }
                    return .init(token: token, orgId: await serviceCredentials.value(for: "DEVIN_ORG_ID"))
                },
                transport: URLSessionTransport(),
                onChanged: { [sessions, calendar] cloud in
                    await sessions?.updateCloud(cloud)
                    // The redraw reads the whole visible range; the sync
                    // doesn't wait for it.
                    Task { @MainActor in
                        calendar?.noteScanCompleted()
                        await calendar?.load(force: true)
                    }
                },
                onStatus: { [sessions] error in await sessions?.noteCloudSync(error: error) })
            codexUsage = CodexAccountUsage(
                cacheFile: swe.appending(path: "cache/codex-account-usage.json"),
                fetch: { home in
                    try await Self.client(for: UserDefaults.standard.string(
                        forKey: SweLocation.defaultsKey) ?? "")
                        .codexUsageDaily(accountDir: home)
                },
                macDays: { home, range in
                    await ledger.dailyTokens(harness: "codex", accountHome: home,
                                             in: range, calendar: .current)
                })
            accountHistory = AccountHistory(days: { keys, range in
                await ledger.dailyTokens(for: keys, in: range, calendar: .current)
            })
            let overviewModel = overview
            pricingModel = PricingModel(
                ledger: ledger,
                overrides: PricingOverrides(url: swe.appending(path: "config/pricing.json")),
                makeCatalog: { try pricing.load() },
                // The ledger takes the catalog at once; the Overview's
                // figures (seconds of queries) redraw behind the closed
                // price sheet rather than holding it open.
                apply: { catalog in
                    ledger.setPricing(catalog)
                    Task { @MainActor in await overviewModel?.pricesChanged() }
                })
            // Benchmark data + the user's own per-model usage, for the
            // Models page and the hover cards everywhere else.
            models = ModelsModel(
                store: ArtificialAnalysisStore(
                    database: catalog,
                    legacyDirectory: ArtificialAnalysisStore.resolvedDirectory(home: home)),
                keyReader: {
                    await serviceCredentials.value(for: "ARTIFICIAL_ANALYSIS_API_KEY")
                },
                transport: URLSessionTransport(),
                linksURL: swe.appending(path: "config/model-links.json"),
                slices: { range in await ledger.byModelHarness(in: range) },
                pricing: { await ledger.catalog() })
            // model-links.json may have changed while the app was closed.
            Task.detached(priority: .utility) { catalog.rebuildAliases() }
        } catch {
            overview = nil
            sessions = nil
            ledger = nil
            calendar = nil
            cursorFeed = nil
            devinCloud = nil
            codexUsage = nil
            accountHistory = nil
            pricingModel = nil
            models = nil
            overviewError = "Couldn't open the usage ledger: \(error.localizedDescription)"
        }
        sessions?.hideTrivial = hidesTrivial
        // Built whether or not the ledger opened: a nil ledger shows the
        // reason under the switch instead of hiding the feature.
        search = SearchIndexModel(swe: swe, ledger: ledger, overview: overview)
        // A new Devin token or org id syncs the cloud sessions at once.
        services.onFieldChanged = { [devinCloud] field in
            guard field.hasPrefix("DEVIN_"), let devinCloud else { return }
            Task { await devinCloud.sync() }
        }
        skills = SkillsModel(swe: client, snapshots: SnapshotFile(url: swe.appending(path: "cache/skills.json")))
        // The Skills page's hub tab is now Plugins > Explore, kept in
        // plugin-catalog.json; its old snapshot has no reader left.
        try? FileManager.default.removeItem(at: swe.appending(path: "cache/skills-hub.json"))
        skills.use(client, for: saved)
        mcp = McpModel(snapshots: SnapshotFile(url: swe.appending(path: "cache/mcp-servers.json")))
        plugins = PluginsModel(snapshots: SnapshotFile(url: swe.appending(path: "cache/plugins.json")),
                               work: pluginWork)
        hooks = HooksModel(snapshots: SnapshotFile(url: swe.appending(path: "cache/hooks.json")))
        worktrees = WorktreesModel(client: WorktreesDebug.fixtureClient())
        pluginCatalog = PluginCatalogModel(snapshots: SnapshotFile(url: swe.appending(path: "cache/plugin-catalog.json")),
                                           work: pluginWork)
        updates = UpdatesModel(
            build: BuildInfo(infoDictionary: Bundle.main.infoDictionary),
            preferences: UpdatePreferences(),
            releaseCheck: ReleaseCheck(
                transport: URLSessionTransport(),
                cacheURL: swe.appending(path: "cache/latest-release.json")),
            swe: {
                let path = UserDefaults.standard.string(forKey: SweLocation.defaultsKey)
                return SweClient(runner: ProcessRunner(
                    executable: SweLocation.executable(override: path)))
            })
        serviceStatus = ServiceStatusModel(service: ServiceStatusService(
            transport: URLSessionTransport(), cacheURL: swe.appending(path: "cache/service-status.json")),
            store: CustomStatusServices())
        providers = ProviderVisibilityModel(store: UserDefaultsStore(), detect: {
            // swe's installed harnesses, plus the extra providers' own signs.
            let path = UserDefaults.standard.string(forKey: SweLocation.defaultsKey)
            let swe = SweClient(runner: ProcessRunner(executable: SweLocation.executable(override: path)))
            // A failure is passed on, not read as "nothing installed".
            let harnesses = try await swe.harnesses(withUsage: false)
            let shellPath = await LoginShell.cachedEnvironment()["PATH"] ?? ""
            return Set(ProviderDetection.installed(
                sweHarnesses: ProviderDetection.installedHarnesses(harnesses),
                commandExists: { ProviderDetection.commandExists($0, path: shellPath) },
                pathExists: { FileManager.default.fileExists(atPath: $0) }))
        })
        fans = FanControl(control: swe.appending(path: "fan/control.json"))
        compute = ComputeModel(sampler: ProcessSampler(), matchers: { [apps] in
            // The swe chosen in Settings right now, like ProviderVisibility.
            let path = UserDefaults.standard.string(forKey: SweLocation.defaultsKey)
            let swe = SweClient(runner: ProcessRunner(executable: SweLocation.executable(override: path)))
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            return try await swe.harnesses(withUsage: false)
                .filter { $0.state == .starred }
                .map { harness in
                    var commands = Set(harness.aliases)
                    commands.insert((harness.command as NSString).lastPathComponent)
                    return HarnessMatcher(harness: harness.name,
                                          appPath: apps.installedApp(for: harness.name),
                                          commands: commands,
                                          homeFolder: home + "/." + harness.name)
                }
        }, managedPIDs: { await Self.launchdManagedPIDs() }, defaults: .standard)
        terminalChoice = { [unowned self] in self.terminal }
        compute.onTemperature = { [fans] reading in
            Task { await fans.checkTemperature(reading[.cpu]) }
        }
        // With every trend view off screen the sampler reads only what
        // auto clean, the fan threshold and a placed widget need.
        compute.watchesTemperature = fans.thresholdEnabled
        compute.startTrends()
        // A page hidden in Settings › Sidebar never comes back as the
        // open one on launch.
        if let selection, sidebar.isHidden(selection) { self.selection = .overview }
        // GitHub banners open the Inbox, so they stop when it is hidden.
        if sidebar.isHidden(.inbox) { githubNotify.bannersEnabled = false }
        sidebar.onHide = { [githubNotify] section in
            if section == .inbox { githubNotify.bannersEnabled = false }
        }
    }

    /// Pids launchd manages, from `launchctl list`: user agents look like
    /// orphans (ppid 1, a script runner) but are services, not leftovers.
    /// Anything going wrong answers with an empty set.
    private nonisolated static func launchdManagedPIDs() async -> Set<Int32> {
        await offMain { Self.launchdManagedPIDsSync() }
    }

    /// Synchronous so it can block on the child, off the caller's actor.
    /// Five seconds, then the process is let go and the list is empty.
    private nonisolated static func launchdManagedPIDsSync() -> Set<Int32> {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["list"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return [] }
        // Read to EOF while it runs, so a long list can't fill the pipe and
        // stall it; a hung launchctl is stopped after five seconds, which
        // ends the read.
        let deadline = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 5, execute: deadline)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        deadline.cancel()
        guard process.terminationStatus == 0 else { return [] }
        return launchctlListPIDs(String(decoding: data, as: UTF8.self))
    }

    /// The pids in `launchctl list` output. Columns are tab-separated: PID
    /// (or "-" for a job that is not running), status, label; the first
    /// line is the header. Pid 0 and 1 are never a service to spare.
    nonisolated static func launchctlListPIDs(_ text: String) -> Set<Int32> {
        Set(text.split(whereSeparator: \.isNewline)
            .compactMap { line in
                line.split(separator: "\t", omittingEmptySubsequences: false).first
                    .flatMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
            }
            .filter { $0 > 1 })
    }

    private var refreshing: Task<Void, Never>?
    private var backingUp: Task<Void, Never>?

    private var watchers: [Task<Void, Never>] = []

    /// Keep the ledger scanned and every limit fetched while the app runs.
    /// One set of loops serves the window, the menu bar and the widgets.
    func start() {
        if statusPolling == nil {
            statusPolling = Task { [serviceStatus, serviceNotify] in
                await CommandActivity.background { await serviceNotify.keepFresh(model: serviceStatus) }
            }
        }
        guard watchers.isEmpty else { return }
        if FirstRun.shouldOffer(
            done: UserDefaults.standard.bool(forKey: FirstRun.defaultsKey),
            agentsFileExists: FileManager.default.fileExists(atPath: FirstRun.agentsFile().path)) {
            showsFirstRunSetup = true
        }
        if let overview { refreshing = Task { await overview.keepFresh() } }
        if let ledger, backingUp == nil { backingUp = Task { await ledger.keepBackedUp() } }
        // Applies the persisted switch: attaches the indexer and starts the
        // reconcile only when the feature is on.
        search.start()
        watchers = [
            Task { await CommandActivity.background { await accounts.keepFresh() } },
            // The inbox polls whether or not its page is open: banners
            // and board invalidations come from this stream.
            Task { [githubNotify, weak self] in
                await CommandActivity.background { await githubNotify.keepFresh { self?.swe } }
            },
            // The last session list shows at once; swe lists again behind it,
            // so the page is usually ready before it is opened.
            // The other swe pages only read what they showed last; swe runs
            // when one is opened.
            Task { [skills] in await CommandActivity.background { await skills.restore() } },
            Task { [mcp] in await CommandActivity.background { await mcp.restore() } },
            Task { [plugins] in await CommandActivity.background { await plugins.restore() } },
            Task { [hooks] in await CommandActivity.background { await hooks.restore() } },
            Task { [pluginCatalog] in await CommandActivity.background { await pluginCatalog.restore() } },
        ]
        if let cursorFeed { watchers.append(Task { await CommandActivity.background { await cursorFeed.keepFresh() } }) }
        if let devinCloud { watchers.append(Task { await CommandActivity.background { await devinCloud.keepFresh() } }) }
        if let codexUsage {
            watchers.append(Task { await CommandActivity.background { await codexUsage.keepFresh(homes: codexHomes) } })
            // The "This Mac" side follows the ledger: rebuild after every
            // completed scan or out-of-scan write, the same counters the
            // Overview's own figures listen to. No swe fetch.
            watchers.append(Task { [weak self] in
                let changes = Observations { [weak self] in
                    self.map { ($0.overview?.completedScans ?? 0,
                                $0.overview?.ledgerWrites ?? 0) }
                }
                for await _ in changes { await codexUsage.recompute() }
            })
        }
        if let accountHistory {
            // Same counters: only the keys the Accounts page shows rebuild.
            watchers.append(Task { [weak self] in
                let changes = Observations { [weak self] in
                    self.map { ($0.overview?.completedScans ?? 0,
                                $0.overview?.ledgerWrites ?? 0) }
                }
                for await _ in changes { await accountHistory.recompute() }
            })
        }
        watchers += watchCatalogPlacement()
        watchers.append(watchSessions())
        watchers += watchLimits()
        watchers.append(watchActivation())

        // The refresh window feeds the account reads and swe's cache TTL.
        accounts.freshFor = limits.refreshFreshSeconds
        others.cacheTTL = limits.refreshFreshSeconds
    }

    /// Skills and Plugins > Explore place one inventory against one
    /// catalog, so each skill shows on exactly one of them: each model is
    /// handed the other's latest.
    private func watchCatalogPlacement() -> [Task<Void, Never>] {
        [
            Task { [weak self] in
                let changes = Observations { [weak self] in self?.skills.inventory }
                for await _ in changes {
                    guard let self else { return }
                    pluginCatalog.skills = skills.allSkills
                }
            },
            Task { [weak self] in
                let changes = Observations { [weak self] in self?.pluginCatalog.revision }
                for await _ in changes {
                    guard let self else { return }
                    skills.plugins = pluginCatalog.lookup
                }
            },
        ]
    }

    private func watchSessions() -> Task<Void, Never> {
        Task { [weak self] in
            guard let sessions = self?.sessions else { return }
            await sessions.restore()
            await sessions.loadIfStale(for: self?.swePath ?? "")
        }
    }

    /// Limits change on a fetch; whether a window has reset changes with
    /// the clock, so look again every minute as well.
    private func watchLimits() -> [Task<Void, Never>] {
        [
            Task { [weak self] in
                while !Task.isCancelled {
                    await CommandActivity.background { await self?.others.load() }
                    try? await Task.sleep(for: .seconds(300))
                }
            },
            Task { [weak self] in
                let changes = Observations { [weak self] in
                    self.map { ($0.starredAccountCards, $0.others.rows) }
                }
                for await _ in changes { self?.refreshLimits() }
            },
            Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(60))
                    self?.refreshLimits()
                }
            },
            Task { [weak self] in
                // The refresh window changing re-times both readers.
                let changes = Observations { [weak self] in
                    self?.limits.refreshFreshMinutes ?? 0
                }
                for await _ in changes {
                    guard let self else { continue }
                    accounts.freshFor = limits.refreshFreshSeconds
                    others.cacheTTL = limits.refreshFreshSeconds
                }
            },
            Task { [weak self] in
                let changes = Observations { [weak self] in
                    // The series order and colours also feed the file:
                    // a provider reorder or a newly loaded primary colour
                    // must rewrite it like a reading change does. The
                    // compute widget's matcher order and chosen
                    // temperature lines feed it the same way.
                    self.map { ($0.overview?.activity, $0.overview?.recentModels,
                               $0.limits.readings, $0.providers.rows, $0.compute.matchers,
                               $0.compute.temperatureLines) }
                }
                for await _ in changes {
                    guard let self else { continue }
                    let style = widgetHarnessStyle
                    limits.writeWidgets(overview: overview, selection: widgetSelection,
                                        harnessOrder: style.order, harnessColors: style.colors,
                                        compute: widgetCompute)
                }
            },
            Task { [weak self] in
                // The fan threshold's switch decides whether the trend
                // sampler reads temperatures with the windows closed.
                let changes = Observations { [weak self] in self?.fans.thresholdEnabled ?? false }
                for await enabled in changes { self?.compute.watchesTemperature = enabled }
            },
            Task { [weak self] in
                // The compute figures move every trend tick, far faster
                // than the stream above fires; the file picks them up on
                // a minute cadence while the pushes stay floored. Every
                // fifth minute (and at launch) it also asks WidgetKit
                // whether a Compute widget is placed, since only then
                // does the sampler keep reading with the windows closed.
                var minute = 0
                while !Task.isCancelled {
                    if minute % 5 == 0 { await self?.checkComputeWidget() }
                    minute += 1
                    try? await Task.sleep(for: .seconds(60))
                    guard let self else { return }
                    let style = widgetHarnessStyle
                    limits.writeWidgets(overview: overview, selection: widgetSelection,
                                        harnessOrder: style.order, harnessColors: style.colors,
                                        compute: widgetCompute)
                }
            },
        ]
    }

    /// Whether a Compute widget sits on the desktop or in Notification
    /// Center; a failed read leaves the last answer.
    func checkComputeWidget() async {
        let kinds: Set<String>? = await withCheckedContinuation { continuation in
            WidgetCenter.shared.getCurrentConfigurations { result in
                continuation.resume(returning: (try? result.get()).map { Set($0.map(\.kind)) })
            }
        }
        if let kinds { compute.widgetPlaced = kinds.contains("compute") }
    }

    /// Notifications may be switched on or off in System Settings while
    /// SWE is in the background, and a harness installed from a
    /// terminal; the provider check runs again, quietly.
    private func watchActivation() -> Task<Void, Never> {
        Task { [weak self] in
            await self?.limits.checkNotificationPermission()
            for await _ in NotificationCenter.default.notifications(named: NSApplication.didBecomeActiveNotification) {
                await self?.limits.checkNotificationPermission()
                if let self { await self.providers.recheck(for: self.swePath) }
            }
        }
    }

    private func refreshLimits() {
        limits.update(LimitReading.all(cards: starredAccountCards, others: others.rows, now: .now,
                                     sweFreshFor: others.acceptedCacheTTL))
    }

    /// Keeps the Service status page and the menu bar line current.
    private var statusPolling: Task<Void, Never>?
    /// Resume a session in the chosen terminal, in the account it ran under.
    func resume(_ row: SessionsModel.Row) async throws {
        // A cloud session with a web page opens there instead.
        if row.session.resume == nil, let web = row.session.webURL.flatMap(URL.init(string:)),
           web.isWebURL {
            NSWorkspace.shared.open(web)
            return
        }
        guard let resume = row.session.resume else { return }
        let home = row.session.accountHome ?? row.usage?.accountHome
        let defaults = [".claude", ".codex"].map { userHome.appending(path: $0).path }
        let line = SessionResume.shellLine(resume, harness: row.session.harness, accountHome: home,
                                           isDefaultHome: home.map(defaults.contains) ?? true)
        try await accounts.runInTerminal(line: line)
    }

    /// Open a new agent session on a PR's head: `swe gh pr worktree`
    /// checks the head out under ~/.swe/worktrees, then the chosen
    /// terminal gets `cd <checkout> && <harness cli>`.
    func openPrSession(repo: String, number: Int, harness: Harness) async {
        guard let path = await github.prWorktree(repo: repo, pr: number, swe: swe) else { return }
        try? await accounts.runInTerminal(line: Self.prSessionLine(path: path, command: harness.command))
    }

    /// `cd <checkout> && <harness cli>`. The registry command is pasted
    /// into a shell line: quote it word by word like a resume argv, so a
    /// metacharacter inside it can't break out of the `cd` chain. A
    /// leading ~ still expands.
    nonisolated static func prSessionLine(path: String, command: String) -> String {
        let quoted = command
            .split(whereSeparator: \.isWhitespace)
            .map { word -> String in
                let word = String(word)
                let expanded = word.hasPrefix("~")
                    ? (word as NSString).expandingTildeInPath : word
                return LaunchPlanner.shellQuote(expanded)
            }
            .joined(separator: " ")
        return "cd \(LaunchPlanner.shellQuote(path))"
            + (quoted.isEmpty ? "" : " && \(quoted)")
    }

    static func keysFile(path: String) -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let trimmed = path.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty,
              PathSafety.rejection(of: trimmed) == nil else {
            return APIKeysStore.defaultURL(home: home)
        }
        return URL(fileURLWithPath: (trimmed as NSString).expandingTildeInPath)
    }

    static func mcpServersFolder(path: String) -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let trimmed = path.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty,
              PathSafety.rejection(of: trimmed) == nil else {
            return home.appending(path: ".swe/mcp/servers")
        }
        return URL(fileURLWithPath: (trimmed as NSString).expandingTildeInPath)
    }

    private static func keysModel(path: String) -> APIKeysModel {
        // One backup of the previous version, private to you, under ~/.swe.
        let backups = FileManager.default.homeDirectoryForCurrentUser.appending(path: ".swe/backups/api-keys")
        let home = FileManager.default.homeDirectoryForCurrentUser
        return APIKeysModel(store: APIKeysStore(url: keysFile(path: path), backupDirectory: backups,
                                                sealedFolder: APIKeysStore.secretsFolder(home: home)),
                            shell: LoginShellEnvironment(),
                            checkCache: APIKeyCheckStore(url: FileManager.default.homeDirectoryForCurrentUser
                                .appending(path: ".swe/cache/api-key-checks.json")))
    }

    private static func client(for path: String) -> SweClient {
        SweClient(runner: ProcessRunner(executable: SweLocation.executable(override: path)))
    }
}

/// Provider visibility and the Explore tab's open state live in the app's own preferences.
private struct UserDefaultsStore: KeyValueStoring {
    func strings(forKey key: String) -> [String]? { UserDefaults.standard.stringArray(forKey: key) }
    func set(_ strings: [String], forKey key: String) { UserDefaults.standard.set(strings, forKey: key) }
}

/// The swe chosen in Settings at the moment of each call.
private struct CurrentSwe: SessionListing {
    func sessions(days: Int?, limit: Int) async throws -> SessionList {
        let path = UserDefaults.standard.string(forKey: SweLocation.defaultsKey)
        return try await SweClient(runner: ProcessRunner(executable: SweLocation.executable(override: path)))
            .sessions(days: days, limit: limit)
    }
}
