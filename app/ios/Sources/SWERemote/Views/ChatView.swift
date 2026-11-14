import SwiftUI

/// P5 and P6: a session's transcript, live, with the composer when
/// attached and the attach card when only watching.
struct ChatView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.scenePhase) private var phase
    let key: String
    @State private var model: ChatModel?
    @State private var streamGate = ChatStreamGate()
    @State private var otherModelShown = false
    @State private var otherModelText = ""

    var body: some View {
        Group {
            if let model { ChatBody(model: model) } else { Theme.wash }
        }
        .background(Theme.wash)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.hidden, for: .tabBar)
        .toolbar {
            ToolbarItem(placement: .principal) {
                let row = app.session(key)
                VStack(spacing: 1) {
                    Text(row?.displayTitle ?? "Session").font(Theme.barTitle).lineLimit(1)
                    HStack(spacing: 4) {
                        HarnessIcon(harness: row?.harness, name: row?.harnessName ?? "", size: 11)
                        Text(SessionGrouping.shortDir(row?.cwd))
                        // The folder gives way first: the model shows
                        // nowhere else on this screen.
                        if let pending = ModelLabel.short(model?.pendingModel) {
                            // Asked for, not yet in the transcript.
                            Text("· → \(pending)").layoutPriority(1)
                                .accessibilityLabel("Switching model to \(pending)")
                        } else if let current = row?.modelLabel {
                            Text("· \(current)").layoutPriority(1)
                                .accessibilityLabel("Model \(current)")
                        }
                    }
                    .font(Theme.small).foregroundStyle(Theme.textSecondary).lineLimit(1)
                }
                .frame(maxWidth: 240)
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button { UIPasteboard.general.string = key } label: { Label("Copy session id", systemImage: "doc.on.doc") }
                    if let model, app.session(key)?.canSwitchModel == true {
                        ModelSwitchMenu(model: model) { otherModelShown = true }
                    }
                    if let model, app.session(key)?.isAttached == true {
                        Button(role: .destructive) { Task { await model.interrupt() } } label: { Label("Interrupt", systemImage: "stop.circle") }
                    }
                } label: { Image(systemName: "ellipsis.circle") }
            }
        }
        .task(id: key) {
            let m = app.chat(key)
            model = m
            await m.start()
        }
        .alert("Switch model", isPresented: $otherModelShown) {
            TextField("Model id", text: $otherModelText)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("Switch") {
                let text = otherModelText
                otherModelText = ""
                Task { await model?.switchModel(to: text) }
            }
            .disabled(ModelSwitchOptions.validValue(otherModelText) == nil)
            Button("Cancel", role: .cancel) { otherModelText = "" }
        } message: {
            Text("Typed into the session as /model <id>. The header shows it once a reply runs on it.")
        }
        .onDisappear { model?.stop() }
        // Opened from a link before the list loaded: the row arrives
        // later and may say the session is a mirrored copy.
        .onChange(of: app.session(key)?.isMirrored == true) { _, mirrored in
            if mirrored { model?.mirroredLearned() }
        }
        .onChange(of: phase) { _, new in
            guard let model else { return }
            switch streamGate.phase(new) {
            case .stop: model.stop()
            case .start: Task { await model.start() }
            case .none: break
            }
        }
    }
}

/// What a scene-phase change does to an open chat's live stream. Only a
/// trip to the background stops it, and only that brings a restart.
/// Inactive and back (Control Center, a notification, the app switcher
/// peek) leaves it running, so it isn't torn down and replayed for
/// nothing.
struct ChatStreamGate {
    enum Action: Equatable { case none, stop, start }
    private(set) var stoppedInBackground = false

    mutating func phase(_ new: ScenePhase) -> Action {
        if new == .background {
            stoppedInBackground = true
            return .stop
        }
        if new == .active, stoppedInBackground {
            stoppedInBackground = false
            return .start
        }
        return .none
    }
}

/// The chat menu's Model submenu, on sessions whose channel can switch
/// it (`SessionRow.canSwitchModel`): the machine's recent models for the
/// harness, the current one checked, then Other… for a typed id.
private struct ModelSwitchMenu: View {
    let model: ChatModel
    let other: () -> Void

    var body: some View {
        let current = model.pendingModel ?? model.row?.model
        Menu {
            ForEach(model.modelChoices, id: \.self) { choice in
                Button {
                    Task { await model.switchModel(to: choice) }
                } label: {
                    if choice == current {
                        Label(choice, systemImage: "checkmark")
                    } else {
                        Text(choice)
                    }
                }
            }
            Button("Other…", action: other)
        } label: {
            Label("Model", systemImage: "cpu")
        }
        .accessibilityIdentifier("chat-model-menu")
    }
}

private struct ChatBody: View {
    @Environment(AppModel.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Bindable var model: ChatModel

    /// Scrolling within this many entries of the oldest one loaded asks
    /// for the next page, so it's usually in before you reach the top.
    static let earlierThreshold = 15

    var body: some View {
        let entries = ChatEntry.fold(model.items)
        let empty = ChatEmpty.of(row: model.row, loading: model.loading, loadError: model.loadError,
                                 hasItems: !model.items.isEmpty, hasStreaming: !model.streaming.isEmpty)
        ScrollView(.vertical) {
            LazyVStack(alignment: .leading, spacing: 10) {
                switch empty {
                case .skeleton:
                    ChatSkeleton()
                case .error(let error):
                    StateCard(icon: "exclamationmark.triangle", title: "Couldn't load this session", message: error, tone: .error)
                case .noMessages:
                    StateCard(icon: "text.bubble", title: "No messages yet", message: "New messages appear here as the agent works.")
                case .mirroredUnavailable:
                    // Calm: nothing failed, the copy just isn't readable yet.
                    StateCard(icon: "doc.on.doc", title: Mirrored.chatUnavailable)
                        .transition(.opacity)
                case nil:
                    if model.hasEarlier {
                        EarlierRow(model: model).onAppear { model.wantsEarlier = true }
                    }
                }
                ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                    Group {
                        switch entry {
                        case .item(let item): ChatItemView(item: item, model: model)
                        case .toolRun(_, let items): ToolRunCard(items: items, model: model)
                        }
                    }
                    .onAppear { if index < Self.earlierThreshold, model.hasEarlier { model.wantsEarlier = true } }
                    .transition(.asymmetric(insertion: arrival(12), removal: .opacity))
                }
                // Replies still being written. Each one's final message
                // takes its place in the transcript above in the same
                // update, so it leaves without a transition.
                ForEach(model.streaming) { item in
                    ChatItemView(item: item, model: model)
                        .transition(.asymmetric(insertion: arrival(12), removal: .identity))
                }
                if model.working, !model.loading {
                    WorkingIndicator(harness: model.row?.harness,
                                     name: model.row?.harnessName ?? "agent",
                                     since: model.workingSince ?? Date())
                        .transition(arrival(8))
                }
            }
            .animation(.easeOut(duration: 0.2), value: model.working)
            .scrollTargetLayout()
            .padding(.horizontal, Theme.gutter)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .defaultScrollAnchor(.bottom)
        .modifier(LoadsEarlierAtRest(model: model, oldest: entries.first?.id))
        .scrollDismissesKeyboard(.interactively)
        // A tap in the transcript closes the prompt box's menu.
        .simultaneousGesture(TapGesture().onEnded {
            model.mentions.dismiss()
            model.commands.dismiss()
        })
        .safeAreaInset(edge: .top, spacing: 0) {
            if model.isMirrored {
                MirroredNotice()
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(.easeOut(duration: 0.2), value: model.isMirrored)
        .safeAreaInset(edge: .bottom) { bottomBar }
        .sheet(item: $model.previewing) { target in
            PagePreviewSheet(target: target) { try await model.loadPreview($0) }
        }
    }

    /// New entries fade in, rising `rise` points unless Reduce Motion is on.
    private func arrival(_ rise: CGFloat) -> AnyTransition {
        reduceMotion ? .opacity : .opacity.combined(with: .offset(y: rise))
    }

    @ViewBuilder private var bottomBar: some View {
        let row = model.row
        VStack(spacing: 0) {
            Rectangle().fill(Theme.border).frame(height: 1)
            switch ChatBar.of(row) {
            case .composer: Composer(model: model)
            case .attachCard: AttachCard(model: model, row: row)
            case .mirroredComposer: MirroredComposer()
            }
        }
        .background(Theme.wash)
    }
}

/// Top of the transcript while older pages remain: a spinner where they
/// load on their own (iOS 18), a button on iOS 17.
private struct EarlierRow: View {
    let model: ChatModel

    var body: some View {
        if #available(iOS 18, *) {
            LoadingRow(text: "Loading earlier messages")
        } else if model.loadingEarlier {
            LoadingRow(text: "Loading earlier messages")
        } else {
            Button("Load earlier messages") { Task { await model.loadEarlier() } }
                .secondaryButton(compact: true)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
        }
    }
}

/// Loads the page `model.wantsEarlier` asked for once scrolling is at
/// rest, then puts the entry that was at the top of the screen back
/// there, so what you were reading stays put (iOS 18; iOS 17 gets the
/// button in `EarlierRow`).
///
/// Loading while a flick still moves, or keeping your place by offset,
/// both failed in the simulator: a page landing above keeps the offset
/// from the top, the new first rows show and ask for the next page, and
/// a decelerating scroll overrides any move made to compensate, so it
/// raced to the start of the session. At rest, restoring by entry holds,
/// and the new rows land off screen so nothing asks again until you
/// scroll up to them.
private struct LoadsEarlierAtRest: ViewModifier {
    let model: ChatModel
    /// The oldest entry's id. Never the one to restore: when it's a run
    /// of tool calls and the older page ends in more of them, they join
    /// the run and its id (taken from its first call) changes.
    let oldest: String?

    func body(content: Content) -> some View {
        if #available(iOS 18, *) {
            content.modifier(Tracking(model: model, oldest: oldest))
        } else {
            content
        }
    }

    @available(iOS 18, *)
    private struct Tracking: ViewModifier {
        let model: ChatModel
        let oldest: String?
        @State private var position = ScrollPosition(idType: String.self)
        @State private var idle = true
        /// The entries on screen, top first.
        @State private var visible: [String] = []
        /// Bumped each time you start a scroll, so a page that lands after
        /// you moved on doesn't pull you back.
        @State private var drags = 0

        func body(content: Content) -> some View {
            content
                .scrollPosition($position, anchor: .top)
                .onScrollTargetVisibilityChange(idType: String.self) { visible = $0 }
                .onScrollPhaseChange { _, phase in
                    idle = phase == .idle
                    if phase == .interacting { drags += 1 }
                    load()
                }
                .onChange(of: model.wantsEarlier) { load() }
        }

        private func load() {
            guard idle, model.wantsEarlier, model.hasEarlier, !model.loadingEarlier else { return }
            model.wantsEarlier = false
            let keep = visible.first { $0 != oldest }
            let dragsAtStart = drags
            Task {
                if await model.loadEarlier(), let keep, drags == dragsAtStart {
                    position.scrollTo(id: keep, anchor: .top)
                }
            }
        }
    }
}

// MARK: - Items

/// The transcript's shape while the first page loads: message-shaped grey
/// blocks where a reply, a prompt and tool cards will sit.
private struct ChatSkeleton: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            lines(trailing: [60, 140, 100])
            userBubble(210)
            SkeletonBlock(height: 40, radius: Theme.blockRadius)
            lines(trailing: [30, 190])
            userBubble(140)
            lines(trailing: [80, 250, 160])
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading transcript")
    }

    /// A few assistant reply lines, each short of the full width.
    private func lines(trailing: [CGFloat]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(trailing, id: \.self) { SkeletonBlock(height: 11).padding(.trailing, $0) }
        }
    }

    /// A right-aligned bubble where the user's own messages sit.
    private func userBubble(_ width: CGFloat) -> some View {
        HStack {
            Spacer(minLength: 40)
            SkeletonBlock(width: width, height: 37, radius: 16)
        }
    }
}

/// Three or more tool calls in a row fold into one "Used N tools" card,
/// so a long agent turn doesn't bury what it said.
enum ChatEntry: Identifiable {
    case item(ChatItem)
    case toolRun(String, [ChatItem])

    var id: String {
        switch self {
        case .item(let i): i.id
        case .toolRun(let id, _): id
        }
    }

    static func fold(_ items: [ChatItem], minRun: Int = 3) -> [ChatEntry] {
        var out: [ChatEntry] = []
        var run: [ChatItem] = []
        func flush() {
            if run.count >= minRun { out.append(.toolRun("run-\(run[0].id)", run)) } else { out += run.map(ChatEntry.item) }
            run = []
        }
        for item in items {
            if case .tool = item.kind { run.append(item); continue }
            flush()
            out.append(.item(item))
        }
        flush()
        return out
    }
}

private struct ToolRunCard: View {
    let items: [ChatItem]
    let model: ChatModel
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } } label: {
                HStack(spacing: 8) {
                    Image(systemName: "wrench.and.screwdriver").font(Theme.font(size: 12)).foregroundStyle(Theme.textSecondary)
                        .iconColumn(16)
                    Text("Used \(items.count) tools").font(Theme.control).foregroundStyle(Theme.textPrimary)
                    Text(names).font(Theme.caption).foregroundStyle(Theme.textMuted).lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: open ? "chevron.up" : "chevron.down").font(Theme.disclosure)
                        .foregroundStyle(Theme.textMuted)
                }
                .padding(.horizontal, 12).frame(minHeight: 40)
                .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
                .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            if open {
                ForEach(items) { ChatItemView(item: $0, model: model) }
                    .padding(.leading, 12)
            }
        }
    }

    /// "Bash ×4, Edit, Read": what ran, most used first.
    private var names: String {
        var counts: [String: Int] = [:]
        var order: [String] = []
        for item in items {
            guard case .tool(let call, _) = item.kind else { continue }
            let name = ToolArgs.displayName(call.name)
            if counts[name] == nil { order.append(name) }
            counts[name, default: 0] += 1
        }
        return order.sorted { counts[$0]! > counts[$1]! }
            .map { counts[$0]! > 1 ? "\($0) ×\(counts[$0]!)" : $0 }
            .joined(separator: ", ")
    }
}

private struct ChatItemView: View {
    let item: ChatItem
    let model: ChatModel

    var body: some View {
        switch item.kind {
        case .user(let text, let send):
            // An echo the hub wrote with files starts with their "@path"s.
            let echo = AttachmentRules.splitEcho(text)
            UserBubble(text: echo.text, files: echo.names.isEmpty ? model.fileNames(for: item.id) : echo.names,
                       send: send) { Task { await model.retry(item) } }
                .disabled(model.isMirrored)
        case .assistant(let text):
            AssistantReply(text: text)
        case .thinking(let text, let seconds):
            ThinkingCard(text: text, seconds: seconds)
        case .tool(let call, let result):
            ToolCard(call: call, result: result, pages: model.pageTargets(for: call)) { model.previewing = $0 }
        case .approval(let rid, let prompt, let state):
            ApprovalCard(prompt: prompt, state: state, options: model.approvalOptions[rid] ?? []) { decision in
                Task { await model.decide(rid, decision: decision) }
            }
            .disabled(model.isMirrored)
            .accessibilityHint(model.isMirrored ? Mirrored.composerPlaceholder : "")
        case .compaction:
            HStack(spacing: 8) {
                Rectangle().fill(Theme.border).frame(height: 1)
                Text("Context compacted").font(Theme.caption).foregroundStyle(Theme.textMuted).fixedSize()
                Rectangle().fill(Theme.border).frame(height: 1)
            }
            .padding(.vertical, 4)
        case .system(let text):
            Text(text).font(Theme.caption).foregroundStyle(Theme.textMuted).frame(maxWidth: .infinity, alignment: .center)
                .multilineTextAlignment(.center)
        case .error(let text):
            Label(text, systemImage: "exclamationmark.triangle").font(Theme.caption).foregroundStyle(Theme.dangerText)
        }
    }
}

/// The harness's logo beside a cycling working verb, under the last
/// message while the agent works; new messages land above it. `since`
/// anchors the clock counting the turn's elapsed time under the pill.
struct WorkingIndicator: View {
    let harness: String?
    let name: String
    let since: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                HarnessIcon(harness: harness, name: name, size: 16)
                WorkingVerb()
            }
            .font(Theme.caption)
            .padding(.leading, 9).padding(.trailing, 12).scaledMinHeight(30)
            .background(Theme.card, in: .capsule)
            .overlay(Capsule().strokeBorder(Theme.border, lineWidth: 1))
            WorkingClock(since: since)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement()
        .accessibilityLabel("\(name) is working")
    }
}

private struct UserBubble: View {
    let text: String
    var files: [String] = []
    let send: SendState?
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            if !files.isEmpty { SentFiles(names: files).opacity(send == .sending ? 0.6 : 1) }
            Text(text)
                .font(Theme.body).foregroundStyle(Theme.textPrimary)
                .textSelection(.enabled)
                .padding(.horizontal, 12).padding(.vertical, 9)
                // The Mac app's user bubble: `subtle` with a hairline. In
                // light mode `selected` is the page's own colour, so a
                // fill alone left the message with no bubble at all.
                .background(Theme.subtle, in: .rect(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Theme.border, lineWidth: 1))
                .opacity(send == .sending ? 0.6 : 1)
            switch send {
            case .sending?: Text("Sending").font(Theme.small).foregroundStyle(Theme.textMuted)
            case .queued(let note)?: Text(note).font(Theme.small).foregroundStyle(Theme.textMuted)
            case .delivered(let note?)?: Text(note).font(Theme.small).foregroundStyle(Theme.textMuted)
            case .delivered(nil)?: EmptyView()
            case .failed(let note)?:
                Button(action: retry) {
                    Label("\(note). Tap to retry", systemImage: "arrow.clockwise").font(Theme.small)
                }
                .foregroundStyle(Theme.dangerText)
            case nil: EmptyView()
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
    }
}

/// Agent text as Markdown: headings, lists, quotes, rules, code blocks
/// and tables, with inline styling inside each. Prose wraps to the screen;
/// a table too wide for it scrolls sideways inside its own box, so the
/// chat itself never does.
struct RichText: View {
    let blocks: [Markdown.Block]

    init(text: String) { blocks = Markdown.parse(text) }

    /// Blocks parsed already, as a reply still streaming in keeps them.
    init(blocks: [Markdown.Block]) { self.blocks = blocks }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                // Equatable: a block that didn't change between repaints
                // of a streaming reply isn't laid out again.
                BlockView(block: block).equatable()
            }
        }
    }

    /// `s`'s inline Markdown styled, from `inlineCache` when this text
    /// was styled before. A streaming reply repaints every 80 ms, and all
    /// but its last block come back unchanged each time.
    static func inline(_ s: String) -> AttributedString {
        let key = s as NSString
        if let hit = inlineCache.object(forKey: key) { return hit.value }
        let styled = styleInline(s)
        inlineCache.setObject(Styled(styled), forKey: key, cost: s.utf8.count)
        return styled
    }

    private final class Styled {
        let value: AttributedString
        init(_ value: AttributedString) { self.value = value }
    }

    /// Styled text by its source, up to about 4 MB of source text.
    private static let inlineCache: NSCache<NSString, Styled> = {
        let cache = NSCache<NSString, Styled>()
        cache.totalCostLimit = 4 << 20
        return cache
    }()

    private static func styleInline(_ s: String) -> AttributedString {
        let source = Markdown.inlineImagesAsText(s)
        var out = (try? AttributedString(markdown: source, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(source)
        // A link to the agent's own files (`/Users/…`, `src/x.py`) opens
        // nothing on the phone: show its text as a file name instead.
        for run in out.runs {
            guard let url = run.link, !Self.opensOnPhone(url) else { continue }
            out[run.range].link = nil
            out[run.range].inlinePresentationIntent = .code
        }
        return out
    }

    static func opensOnPhone(_ url: URL) -> Bool {
        ["http", "https", "mailto", "tel"].contains(url.scheme?.lowercased() ?? "")
    }
}

private struct BlockView: View, Equatable {
    let block: Markdown.Block

    var body: some View {
        switch block {
        case .heading(let level, let text):
            Text(RichText.inline(text))
                .font(Theme.font(size: level == 1 ? 20 : level == 2 ? 17 : 15, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, level <= 2 ? 6 : 2)
        case .paragraph(let text):
            prose(text)
        case .listItem(let indent, let marker, let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if let (box, rest) = Markdown.taskBox(text) {
                    Image(systemName: box == .done ? "checkmark.square.fill" : "square")
                        .font(Theme.font(size: 14))
                        .foregroundStyle(box == .done ? Theme.accent : Theme.textSecondary)
                        .frame(minWidth: 14, alignment: .trailing)
                        .accessibilityLabel(box == .done ? "Done" : "To do")
                    prose(rest)
                } else {
                    Text(marker)
                        .font(marker == "•" ? Theme.body : Theme.body.monospacedDigit())
                        .foregroundStyle(Theme.textSecondary)
                        .frame(minWidth: 14, alignment: .trailing)
                    prose(text)
                }
            }
            .padding(.leading, CGFloat(min(indent, 4)) * 18)
        case .quote(let text):
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 1).fill(Theme.borderStrong).frame(width: 3)
                Text(RichText.inline(text)).font(Theme.body).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .code(_, let text):
            Text(text).font(Theme.mono).foregroundStyle(Theme.textLabel)
                .textSelection(.enabled)
                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.code, in: .rect(cornerRadius: Theme.controlRadius))
                .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.border, lineWidth: 1))
        case .table(let table):
            TableBlock(table: table)
        case .rule:
            Rectangle().fill(Theme.border).frame(height: 1).padding(.vertical, 4)
        case .image(let alt, let source):
            NoteChip(icon: "photo", title: alt.isEmpty ? "Image" : alt,
                     detail: (source as NSString).lastPathComponent + " · on the machine")
        case .rendered:
            NoteChip(icon: "rectangle.3.group", title: "Rendered view",
                     detail: "Open the session on the machine to see it")
        }
    }

    private func prose(_ text: String) -> some View {
        Text(RichText.inline(text)).font(Theme.body).foregroundStyle(Theme.textPrimary)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A Markdown table in a hairline box. Columns size to their content up
/// to a cap and wrap past it; when the table is wider than the screen it
/// scrolls sideways inside the box, the way Codex shows it.
struct TableBlock: View {
    let table: Markdown.Table
    static let maxColumnWidth: CGFloat = 240
    /// The grid is built eagerly, so a malformed or huge table is cut to
    /// this many columns and rows, with a note saying what was left out.
    static let maxColumns = 24
    static let maxRows = 200

    var body: some View {
        let columns = min(table.columnCount, Self.maxColumns)
        let rows = table.rows.prefix(Self.maxRows)
        let hiddenRows = table.rows.count - rows.count
        let hiddenColumns = table.columnCount - columns
        VStack(alignment: .leading, spacing: 4) {
            grid(columns: columns, rows: Array(rows))
            if let note = Self.hiddenNote(rows: hiddenRows, columns: hiddenColumns) {
                Text(note).font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
        }
    }

    /// "3 more rows and 1 more column not shown", or nil when nothing was cut.
    static func hiddenNote(rows: Int, columns: Int) -> String? {
        let parts = [rows > 0 ? "\(rows) more \(rows == 1 ? "row" : "rows")" : nil,
                     columns > 0 ? "\(columns) more \(columns == 1 ? "column" : "columns")" : nil]
            .compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " and ") + " not shown"
    }

    private func grid(columns: Int, rows: [[String]]) -> some View {
        ScrollView(.horizontal, showsIndicators: true) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(0..<columns, id: \.self) { c in
                        cell(c < table.header.count ? table.header[c] : "", column: c, header: true)
                    }
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Rectangle().fill(Theme.border).frame(height: 1).gridCellUnsizedAxes(.horizontal)
                    GridRow {
                        ForEach(0..<columns, id: \.self) { c in
                            cell(c < row.count ? row[c] : "", column: c, header: false)
                        }
                    }
                }
            }
        }
        .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
        // The chat sets .defaultScrollAnchor(.bottom) so it opens at the
        // newest message; scroll views inside inherit it, and for a sideways
        // one "bottom" is the horizontal middle. Tables start at their left.
        .defaultScrollAnchor(.leading)
        .background(Theme.card, in: .rect(cornerRadius: Theme.controlRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.border, lineWidth: 1))
        .clipShape(.rect(cornerRadius: Theme.controlRadius))
    }

    private func cell(_ text: String, column: Int, header: Bool) -> some View {
        let align = column < table.align.count ? table.align[column] : .leading
        return CappedWidth(max: Self.maxColumnWidth) {
            Text(RichText.inline(text))
                .font(header ? Theme.control : Theme.caption)
                .foregroundStyle(header ? Theme.textPrimary : Theme.textLabel)
                .multilineTextAlignment(align == .trailing ? .trailing : align == .center ? .center : .leading)
        }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            // Fill the column so the header shading spans the whole row.
            .frame(maxWidth: .infinity, alignment: align == .trailing ? .trailing : align == .center ? .center : .leading)
            .background(header ? Theme.subtle : .clear)
            .gridColumnAlignment(align == .trailing ? .trailing : align == .center ? .center : .leading)
    }
}

/// A quiet one-line card for content the phone can't show itself.
private struct NoteChip: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon).font(Theme.font(size: 14)).foregroundStyle(Theme.textSecondary).iconColumn(18)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(Theme.control).foregroundStyle(Theme.textPrimary).lineLimit(2)
                Text(detail).font(Theme.caption).foregroundStyle(Theme.textMuted).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
    }
}

/// Its content at its natural width, up to `max`, wrapping past it. A
/// horizontal ScrollView offers unlimited width, and a plain
/// `.frame(maxWidth:)` would let a long cell run on one line forever.
struct CappedWidth: Layout {
    var max: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let child = subviews.first else { return .zero }
        let ideal = child.sizeThatFits(.unspecified)
        let width = min(ideal.width, max)
        let size = child.sizeThatFits(ProposedViewSize(width: width, height: nil))
        return CGSize(width: width, height: size.height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: bounds.origin, proposal: ProposedViewSize(width: bounds.width, height: bounds.height))
    }
}

private struct ThinkingCard: View {
    let text: String
    let seconds: Double?
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } } label: {
                HStack(spacing: 6) {
                    Image(systemName: "brain").font(Theme.font(size: 12))
                    Text(ThoughtLabel.text(seconds)).font(Theme.control)
                    Image(systemName: open ? "chevron.up" : "chevron.down").font(Theme.disclosure)
                }
                .foregroundStyle(Theme.textSecondary)
            }
            .buttonStyle(.plain)
            if open {
                Text(RichText.inline(text)).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct ToolCard: View {
    let call: ToolCall
    let result: String?
    /// Pages this call wrote that the hub can serve: each gets Preview.
    var pages: [PagePreview.Target] = []
    var preview: (PagePreview.Target) -> Void = { _ in }
    @State private var open = false

    var body: some View {
        let diff = ToolDiff.from(call)
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 0) {
                Button { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } } label: {
                    HStack(spacing: 8) {
                        Image(systemName: Self.icon(call.name)).font(Theme.font(size: 12)).foregroundStyle(Theme.textSecondary)
                            .iconColumn(16)
                        Text(ToolArgs.summary(call))
                            .font(Theme.mono).foregroundStyle(Theme.textPrimary)
                            .lineLimit(1).truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        if let diff {
                            Text("+\(diff.added)").font(Theme.mono).foregroundStyle(Theme.success)
                            Text("−\(diff.removed)").font(Theme.mono).foregroundStyle(Theme.dangerText)
                        }
                        Image(systemName: open ? "chevron.up" : "chevron.down").font(Theme.disclosure)
                            .foregroundStyle(Theme.textMuted)
                    }
                    .padding(.leading, 12).padding(.trailing, pages.isEmpty ? 12 : 8).frame(minHeight: 40)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                if !pages.isEmpty {
                    previewButton.padding(.trailing, 6)
                }
            }
            if open {
                Rectangle().fill(Theme.border).frame(height: 1)
                VStack(alignment: .leading, spacing: 0) {
                    if let diff {
                        DiffView(lines: Array(diff.lines.prefix(200)))
                    } else if let args = ToolArgs.readable(call), !args.isEmpty {
                        codeBlock(String(args.prefix(4000)))
                    }
                    if let result, !result.isEmpty {
                        Rectangle().fill(Theme.border).frame(height: 1)
                        codeBlock(String(result.prefix(4000)))
                    }
                }
            }
        }
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
        .clipShape(.rect(cornerRadius: Theme.blockRadius))
    }

    /// One page opens straight away; a patch that wrote several asks which.
    @ViewBuilder private var previewButton: some View {
        if pages.count == 1, let page = pages.first {
            Button { preview(page) } label: { previewLabel }
                .buttonStyle(.plain)
                .accessibilityLabel("Preview \(page.name)")
        } else {
            Menu {
                ForEach(pages) { page in
                    Button(page.path) { preview(page) }
                }
            } label: { previewLabel }
        }
    }

    /// The compact secondary button's look, written out so a Menu label
    /// matches a Button.
    private var previewLabel: some View {
        Label("Preview", systemImage: "eye").labelStyle(.titleAndIcon)
            .font(Theme.control).foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 10).scaledMinHeight(Theme.controlHeight - 4)
            .background(Theme.card, in: .rect(cornerRadius: Theme.controlRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.borderStrong, lineWidth: 1))
            .contentShape(.rect)
    }

    private func codeBlock(_ s: String) -> some View {
        Text(s).font(Theme.mono).foregroundStyle(Theme.textLabel).textSelection(.enabled)
            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
    }

    static func icon(_ name: String) -> String {
        switch name.lowercased() {
        case "bash", "shell", "exec", "exec_command", "run_shell_command": "terminal"
        case "read", "view", "read_file": "doc.text"
        case "edit", "multiedit", "write", "apply_patch", "patch", "file_edit", "notebookedit": "pencil"
        case "grep", "glob", "search", "find": "magnifyingglass"
        case "webfetch", "websearch", "fetch": "globe"
        case "task", "agent": "person.2"
        default: "wrench.and.screwdriver"
        }
    }
}

struct DiffView: View {
    let lines: [DiffLine]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .top, spacing: 6) {
                    Text(sign(line.kind)).foregroundStyle(color(line.kind)).iconColumn(8)
                    Text(line.text.isEmpty ? " " : line.text)
                        .foregroundStyle(line.kind == .meta ? Theme.textMuted : Theme.textLabel)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(Theme.mono)
                .padding(.horizontal, 10).padding(.vertical, 1)
                .background(background(line.kind))
            }
        }
        .padding(.vertical, 6)
    }

    private func sign(_ k: DiffLine.Kind) -> String {
        switch k { case .added: "+"; case .removed: "−"; default: " " }
    }
    private func color(_ k: DiffLine.Kind) -> Color {
        switch k { case .added: Theme.success; case .removed: Theme.dangerText; default: Theme.textMuted }
    }
    private func background(_ k: DiffLine.Kind) -> Color {
        switch k { case .added: Theme.diffAdded; case .removed: Theme.diffRemoved; default: .clear }
    }
}

private struct ApprovalCard: View {
    let prompt: String
    let state: ApprovalState
    /// The pane's menu rows; empty keeps Allow / Always / Deny.
    let options: [ApprovalOption]
    let decide: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Circle().fill(Theme.warning).frame(width: 7, height: 7)
                Text(title).font(Theme.control).foregroundStyle(isOpen ? Theme.warningText : Theme.textSecondary)
            }
            // Verbatim, never markdown: `rm *.log *.tmp` must not lose its
            // stars to italics, nor a link's text hide where it goes. This
            // is what Allow agrees to.
            Text(verbatim: prompt).font(Theme.monoBody).foregroundStyle(Theme.textPrimary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if isOpen && !options.isEmpty {
                // The pane's own rows, so an answer the three buttons
                // can't name (always in this project, edit, describe)
                // is still reachable. Its number is the decision.
                VStack(spacing: 6) {
                    ForEach(options) { option in
                        Button { decide(option.n) } label: {
                            Text(verbatim: ApprovalMenu.buttonTitle(option))
                                .multilineTextAlignment(.leading)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.vertical, 6)
                        }
                        .buttonStyle(MenuRowButtonStyle())
                    }
                }
                .disabled(state == .submitting)
            } else if isOpen {
                HStack(spacing: 8) {
                    Button("Allow") { decide("allow") }.primaryButton(compact: true).frame(maxWidth: .infinity)
                    Button("Always") { decide("always") }.secondaryButton(compact: true).frame(maxWidth: .infinity)
                    Button("Deny") { decide("deny") }.secondaryButton(compact: true, destructive: true).frame(maxWidth: .infinity)
                }
                .disabled(state == .submitting)
            }
            if case .failed(let note) = state {
                Text(note).font(Theme.caption).foregroundStyle(Theme.dangerText)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius)
            .strokeBorder(isOpen ? Theme.warning.opacity(0.5) : Theme.border, lineWidth: 1))
    }

    private var isOpen: Bool {
        switch state { case .pending, .submitting, .failed: true; default: false }
    }

    private var title: String { ApprovalMenu.title(state, options: options) }
}

/// A menu row answer: the secondary button's hairline and fill, but
/// left-aligned and as tall as a wrapped label needs.
private struct MenuRowButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.control)
            .foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 12)
            .frame(minHeight: Theme.controlHeight)
            .background(configuration.isPressed ? Theme.selected : Theme.card,
                        in: .rect(cornerRadius: Theme.controlRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
                .strokeBorder(Theme.borderStrong, lineWidth: 1))
            .opacity(enabled ? 1 : 0.45)
    }
}

// MARK: - Mirrored sessions

/// The one-line notice above a mirrored chat's transcript. It is
/// information, not a warning: muted text on the page, no alert colour.
private struct MirroredNotice: View {
    var body: some View {
        VStack(spacing: 0) {
            Label {
                Text(Mirrored.notice)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "doc.on.doc")
            }
            .labelStyle(.titleAndIcon)
            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, Theme.gutter)
            .padding(.vertical, 10)
            Rectangle().fill(Theme.border).frame(height: 1)
        }
        .background(Theme.wash)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
    }
}

/// A mirrored chat's prompt box: present and dimmed, so the screen keeps
/// its shape, with the reason as its placeholder. Nothing here can type or
/// send; VoiceOver hears the same reason.
private struct MirroredComposer: View {
    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField(Mirrored.composerPlaceholder, text: .constant(""), axis: .vertical)
                .font(Theme.body)
                .lineLimit(1...3)
                .disabled(true)
                .padding(.horizontal, 14).padding(.vertical, 11)
                .background(Theme.field, in: .rect(cornerRadius: 22))
                .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Theme.border, lineWidth: 1))
                .accessibilityLabel("Message the session")
                .accessibilityValue(Mirrored.composerPlaceholder)
            Button {} label: {
                ZStack {
                    Circle().fill(Theme.primaryBackground)
                    Image(systemName: "arrow.up").font(Theme.font(size: 16, weight: .semibold))
                        .foregroundStyle(Theme.primaryForeground)
                }
                .frame(width: 40, height: 40)
                .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                .opacity(0.4)
            }
            .buttonStyle(.plain)
            .disabled(true)
            .accessibilityLabel("Send")
            .accessibilityHint(Mirrored.composerPlaceholder)
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }
}

// MARK: - Bottom bar

private struct AttachCard: View {
    let model: ChatModel
    let row: SessionRow?
    @Environment(\.dynamicTypeSize) private var size

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // At accessibility sizes the button goes under the words (beside
            // them it left the words a column a few letters wide), and the
            // bar sheds what the button already says, so the transcript
            // keeps most of the screen.
            let large = size.isAccessibilitySize
            let layout = large
                ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8))
                : AnyLayout(HStackLayout(spacing: 12))
            layout {
                HStack(spacing: 12) {
                    if !large {
                        Image(systemName: "eye").font(Theme.font(size: 16)).foregroundStyle(Theme.textSecondary)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Watching only").font(Theme.rowTitle).foregroundStyle(Theme.textPrimary)
                        if !(large && row?.canAttach == true && !model.attachQueued) {
                            Text(subtitle).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if model.attachQueued {
                    ProgressView().controlSize(.small)
                } else if row?.canAttach == true {
                    Button {
                        Task { await model.attach() }
                    } label: {
                        if model.attaching { ProgressView().controlSize(.small).tint(Theme.primaryForeground) } else { Text("Attach") }
                    }
                    .primaryButton(compact: true)
                    .disabled(model.attaching)
                }
            }
            if let error = model.attachError {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
            }
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.vertical, 12)
    }

    private var subtitle: String {
        if model.attachQueued { return "Attach sent. Waiting for the machine to confirm." }
        guard let row else { return "Read-only." }
        if row.canAttach { return "Attach to send messages." }
        if row.sessionStatus == .active && row.attachable == true { return "Mid-turn. Attach once it's idle." }
        return "This harness can't be attached from the phone."
    }
}

/// Beside the prompt box: send what's typed, or, with nothing typed while
/// the agent works, a red stop that interrupts the turn.
struct SendOrStopButton: View {
    let action: ChatModel.ComposerAction
    var onSend: () -> Void = {}
    var onStop: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The icons swap with a small scale, or a plain fade under Reduce Motion.
    private var swap: AnyTransition { reduceMotion ? .opacity : .scale.combined(with: .opacity) }

    var body: some View {
        Button {
            switch action {
            case .send: onSend()
            case .stop: onStop()
            case .disabled: break
            }
        } label: {
            ZStack {
                Circle().fill(action == .stop ? Theme.dangerFill : Theme.primaryBackground)
                if action == .stop {
                    RoundedRectangle(cornerRadius: 3).fill(.white).frame(width: 13, height: 13)
                        .transition(swap)
                } else {
                    Image(systemName: "arrow.up").font(Theme.font(size: 16, weight: .semibold))
                        .foregroundStyle(Theme.primaryForeground)
                        .transition(swap)
                }
            }
            .frame(width: 40, height: 40)
            // The circle is a fixed 40pt, so its arrow stops growing at
            // xxxLarge; a long press shows it large instead.
            .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
            .opacity(action == .disabled ? 0.4 : 1)
            .animation(.easeOut(duration: 0.15), value: action)
        }
        .buttonStyle(.plain)
        .disabled(action == .disabled)
        .accessibilityLabel(action == .stop ? "Stop the agent" : "Send")
        .accessibilityShowsLargeContentViewer()
    }
}

private struct Composer: View {
    @Bindable var model: ChatModel
    @FocusState private var focused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The caret in the prompt box (nil: the end), and where a pick moves it.
    @State private var cursor: Int?
    @State private var moveCaret: Int?

    private var arrival: AnyTransition { reduceMotion ? .opacity : .opacity.combined(with: .offset(y: 8)) }

    /// The `@token` being typed, while the prompt box has focus.
    private var mention: MentionToken? {
        focused ? Mention.token(in: model.draft, cursor: cursor) : nil
    }

    /// The `/command` being typed at the start of the box, while it has focus.
    private var slash: SlashToken? {
        focused ? Slash.token(in: model.draft, cursor: cursor) : nil
    }

    var body: some View {
        let tray = model.tray
        let editing = model.editingId != nil
        let mentions = model.mentions
        let commands = model.commands
        let files = model.attachments
        VStack(spacing: 8) {
            if !tray.isEmpty {
                QueuedTray(sends: tray, note: model.trayNote, editingId: model.editingId,
                           onEdit: { cid in model.beginEdit(cid) },
                           onRemove: { cid in Task { await model.remove(cid) } },
                           onSendNow: { cid in Task { await model.sendNow(cid) } },
                           onRemoveAll: { model.removeAllQueued() })
                    .transition(arrival)
            }
            if editing || model.editNote != nil {
                EditBar(editing: editing, saving: model.savingEdit, note: model.editNote) { model.cancelEdit() }
                    .transition(arrival)
            }
            if commands.isOpen {
                commandMenu(commands).transition(arrival)
            } else if mentions.isOpen {
                MentionMenu(entries: mentions.entries, answered: mentions.answered, error: mentions.error) { pick($0) }
                    .transition(arrival)
            }
            // An edit changes a queued message's text only; the files
            // picked for the next message wait, as the draft does.
            if !editing, !files.isEmpty || files.notice != nil {
                AttachmentChips(attachments: files,
                                needsText: model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .transition(arrival)
            }
            HStack(alignment: .bottom, spacing: 8) {
                AttachButton(attachments: files)
                    .disabled(editing)
                PromptField(placeholder: editing ? "Edit the queued message" : "Message the session",
                            text: $model.draft, cursor: $cursor, moveCaret: $moveCaret)
                    .font(Theme.body)
                    .lineLimit(1...5)
                    .focused($focused)
                    .padding(.horizontal, 14).padding(.vertical, 11)
                    .background(Theme.field, in: .rect(cornerRadius: 22))
                    .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Theme.border, lineWidth: 1))
                SendOrStopButton(action: ChatModel.composerAction(draft: model.draft, agentWorking: model.canInterrupt,
                                                                  editing: editing, files: files.items.count,
                                                                  filesBusy: files.blocksSend),
                                 onSend: { Task { await model.send() } },
                                 onStop: { Task { await model.interrupt() } })
                    .disabled(model.savingEdit)
            }
        }
        .animation(.easeOut(duration: 0.2), value: tray.isEmpty)
        .animation(.easeOut(duration: 0.2), value: editing)
        .animation(.easeOut(duration: 0.15), value: mentions.isOpen)
        .animation(.easeOut(duration: 0.15), value: commands.isOpen)
        .animation(.easeOut(duration: 0.2), value: files.items.map(\.id))
        .onChange(of: model.editingId) { _, id in
            // The queued message's text replaced the draft: caret to its end.
            cursor = nil
            if id != nil { focused = true }
        }
        .onChange(of: mention, initial: true) { _, token in mentions.track(token) }
        .onChange(of: slash, initial: true) { _, token in commands.track(token) }
        .padding(.horizontal, Theme.gutter)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }

    private func commandMenu(_ commands: CommandMenu) -> some View {
        let row = model.row
        let notes = commands.response.map {
            Slash.notes($0, harness: row?.harnessName ?? "this harness", machine: model.machineName,
                        remote: row?.remote == true)
        }
        return CommandMenuView(commands: commands.matches, loading: commands.response == nil, error: commands.error,
                               empty: notes?.empty, footnotes: notes?.footnotes ?? [],
                               query: commands.token?.query ?? "",
                               refresh: { await commands.refresh() }) { pick($0) }
    }

    /// Puts the picked command in place of the `/word` at the start.
    private func pick(_ command: SlashCommand) {
        guard let token = slash else { return }
        let next = Slash.insert(command, replacing: token, in: model.draft)
        model.draft = next.text
        cursor = Caret.afterPick(next.cursor, tracksCaret: Caret.tracked)
        moveCaret = cursor
    }

    /// Puts the picked path in place of the `@token`.
    private func pick(_ entry: FileEntry) {
        guard let token = mention else { return }
        let next = Mention.insert(entry.path, isDir: entry.isDir, replacing: token, in: model.draft)
        model.draft = next.text
        cursor = Caret.afterPick(next.cursor, tracksCaret: Caret.tracked)
        moveCaret = cursor
    }
}

/// Above the prompt box while a queued message is being edited, or to
/// say why an edit didn't save.
private struct EditBar: View {
    let editing: Bool
    let saving: Bool
    let note: String?
    let cancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            if editing {
                Image(systemName: "pencil").font(Theme.badge).foregroundStyle(Theme.textSecondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Editing queued message").font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    if let note { Text(note).font(Theme.small).foregroundStyle(Theme.dangerText).lineLimit(1) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if saving { ProgressView().controlSize(.mini) }
                Button("Cancel", action: cancel)
                    .font(Theme.control).foregroundStyle(Theme.textPrimary)
                    .frame(minHeight: 32)
                    .disabled(saving)
            } else if let note {
                Text(note).font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 4)
    }
}

/// Sends made while the agent works, pinned above the prompt box until
/// they go out. Each can be edited, removed, or sent now (written into
/// the running turn); the header's button removes them all.
struct QueuedTray: View {
    let sends: [QueuedSend]
    var note: String? = nil
    var editingId: String? = nil
    var onEdit: (String) -> Void = { _ in }
    var onRemove: (String) -> Void = { _ in }
    var onSendNow: (String) -> Void = { _ in }
    var onRemoveAll: () -> Void = {}
    /// Rows shown before the card scrolls.
    var visibleRows = 3
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var size
    @State private var confirmRemoveAll = false

    static let rowHeight: CGFloat = 44

    static func countLabel(_ n: Int) -> String { n == 1 ? "1 message queued" : "\(n) messages queued" }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 0) {
                Text(Self.countLabel(sends.count)).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                TrayIconButton(symbol: "xmark", label: "Remove all") { confirmRemoveAll = true }
            }
            .padding(.leading, 14)
            .padding(.trailing, 2)
            if let note {
                Text(note).font(Theme.small).foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 14).padding(.bottom, 4)
            }
            // At most its default height over the keyboard: grown rows
            // scroll, as many sends do.
            if sends.count > visibleRows || size.isAccessibilitySize {
                ScrollView(.vertical) { rows }
                    // The chat anchors its scroll views at the bottom; the
                    // tray starts at its oldest send.
                    .defaultScrollAnchor(.top)
                    .scaledHeight(CGFloat(min(sends.count, visibleRows)) * Self.rowHeight,
                                  cap: CGFloat(visibleRows) * Self.rowHeight)
            } else {
                rows
            }
        }
        .padding(.bottom, 6)
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
        .clipShape(.rect(cornerRadius: Theme.blockRadius))
        .animation(.easeOut(duration: 0.2), value: sends.map(\.id))
        .confirmationDialog(sends.count == 1 ? "Remove 1 queued message?" : "Remove \(sends.count) queued messages?",
                            isPresented: $confirmRemoveAll, titleVisibility: .visible) {
            Button("Remove all", role: .destructive, action: onRemoveAll)
        }
    }

    private var rows: some View {
        VStack(spacing: 0) {
            ForEach(sends) { send in
                QueuedRow(send: send, editing: send.id == editingId,
                          onEdit: { onEdit(send.clientId) },
                          onRemove: { onRemove(send.clientId) },
                          onSendNow: { onSendNow(send.clientId) })
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .move(edge: .bottom)))
            }
        }
    }
}

struct QueuedRow: View {
    let send: QueuedSend
    let editing: Bool
    let onEdit: () -> Void
    let onRemove: () -> Void
    let onSendNow: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Group {
                if send.phase == .sendingNow {
                    SendingSpinner()
                } else {
                    Image(systemName: editing ? "pencil.circle" : "circle.dashed")
                        .font(Theme.rowIcon)
                        .foregroundStyle(Theme.textMuted)
                }
            }
            .iconColumn(18)
            HStack(spacing: 6) {
                Text(send.text.replacingOccurrences(of: "\n", with: " "))
                    .font(Theme.body).foregroundStyle(editing ? Theme.textSecondary : Theme.textPrimary)
                    .lineLimit(1).truncationMode(.tail)
                if !send.attachments.isEmpty {
                    // The files go out with it; an edit keeps them.
                    Label("\(send.attachments.count)", systemImage: "paperclip")
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                        .labelStyle(.titleAndIcon).fixedSize()
                        .accessibilityLabel(send.attachments.count == 1 ? "1 file" : "\(send.attachments.count) files")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .opacity(send.phase == .posting ? 0.6 : 1)
            if let note = send.note {
                Text(note).font(Theme.caption)
                    .foregroundStyle(send.phase == .left ? Theme.textMuted : Theme.dangerText)
                    .lineLimit(1)
                    .padding(.trailing, 12)
                // A send that already left can still be cleared from the tray.
                if send.phase == .left { TrayIconButton(symbol: "xmark", label: "Remove", action: onRemove) }
            } else if send.phase == .sendingNow {
                Text("Sending now").font(Theme.caption).foregroundStyle(Theme.textMuted).padding(.trailing, 12)
            } else {
                HStack(spacing: 0) {
                    TrayIconButton(symbol: "pencil", label: "Edit", action: onEdit)
                    TrayIconButton(symbol: "xmark", label: "Remove", action: onRemove)
                    TrayIconButton(symbol: "return", label: "Send now", action: onSendNow)
                }
                .disabled(send.phase == .posting)
            }
        }
        .padding(.leading, 14)
        .padding(.trailing, 2)
        // Taller with the text: a fixed 44 pt clipped the message.
        .scaledMinHeight(QueuedTray.rowHeight)
    }
}

/// A bare muted glyph about 15 pt, with a 44 pt square to hit.
private struct TrayIconButton: View {
    let symbol: String
    let label: String
    let action: () -> Void
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(Theme.rowIcon)
                .foregroundStyle(Theme.textSecondary)
                .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                .frame(width: 44, height: 44)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .opacity(enabled ? 1 : 0.4)
        .accessibilityLabel(label)
    }
}

/// A thin turning arc the size of the dashed circle it replaces while a
/// row is being sent now; still under Reduce Motion. One turn every
/// 0.9 s is a single repeating rotation, so SwiftUI turns it without
/// evaluating the body each frame.
private struct SendingSpinner: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var turning = false

    var body: some View {
        Circle().trim(from: 0, to: 0.7)
            .stroke(Theme.textSecondary, style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
            .frame(width: 14, height: 14)
            .rotationEffect(.degrees(turning && !reduceMotion ? 360 : 0))
            .animation(turning && !reduceMotion
                       ? .linear(duration: 0.9).repeatForever(autoreverses: false)
                       : nil,
                       value: turning && !reduceMotion)
            .onAppear { turning = true }
            .accessibilityLabel("Sending now")
    }
}
