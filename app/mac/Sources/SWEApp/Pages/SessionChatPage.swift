import SWECore
import SwiftUI

/// One session read live from this Mac's hub, swapped in over the
/// Sessions list the way the web app opens its chat: the transcript so
/// far, then each message as the agent writes it.
///
/// Back is the header's chevron, ⌘[, or picking the Sessions sidebar
/// item again.
struct SessionChatPage: View {
    @Environment(AppModel.self) private var app
    /// The Sessions row it opened from: the header until the hub's row
    /// loads, and what Details shows.
    var row: SessionsModel.Row
    var onBack: () -> Void
    var onDetails: () -> Void
    /// The hub turned out not to list it (found only once paired).
    var onMissing: () -> Void
    /// Pin, Rename or Fork from the header's More menu, run by the page.
    var act: (SessionMenuAction) -> Void
    /// A refused pin's message, shared with the list so it shows here too.
    var note: String? = nil
    /// Restore transcript, for a mirrored session, and what the last one did.
    var restore: (() -> Void)? = nil
    var restoreNote: (text: String, ok: Bool)? = nil
    @State private var model: SessionChatModel?

    /// swe lists the session's read-only copy (#1918), by the list's row or
    /// the hub's.
    private var mirrored: Bool { row.session.mirrored || model?.isMirrored == true }

    private var key: String { HubSessionRow.key(harness: row.session.harness, id: row.session.id) }

    /// The hub's pin, name and fork flag: the list's read, which the
    /// chat's own read and its `session.meta` events keep current.
    private var meta: HubSessionMeta? { app.hub.meta(for: key) }

    /// A name set on the hub wins, from here or another device; else the
    /// title swe listed, then the hub's.
    private var title: String {
        let local = row.session.title ?? model?.row?.title
        return meta?.title(over: local) ?? local ?? row.session.id
    }

    var body: some View {
        Page(title: title,
             subtitle: subtitle,
             subtitleTail: model?.row?.modelLabel,
             back: ("Back to all sessions", onBack), maxWidth: 960, scrolls: false) {
            if mirrored {
                MirroredTag()
            } else if let model {
                ChatBadges(model: model)
            }
            if let meta {
                EllipsisMenu { SessionHubMenuItems(meta: meta, act: act) }
            }
            RailIconButton(symbol: "info.circle", help: "Details: folder, usage and the resume command",
                           action: onDetails)
        } content: {
            if let model {
                ChatTranscript(model: model, note: note, mirrored: mirrored, restore: restore,
                               restoreNote: restoreNote)
            } else {
                Spacer()
            }
        }
        .task(id: key) {
            model?.stop()
            let chat = SessionChatModel(key: key, hub: app.hub)
            model = chat
            await chat.start()
        }
        .onDisappear { model?.stop() }
        .onChange(of: model?.missing == true) { _, missing in
            if missing { onMissing() }
        }
    }

    /// harness · folder · model, as the web app's chat bar has it. The
    /// model is the hub's row for the newest reply, left out while unknown;
    /// it rides as the subtitle's tail, so it never breaks at a hyphen.
    private var subtitle: String {
        let folder = (model?.row?.cwd ?? row.session.cwd).map { displayPath($0) }
        let lead = [HarnessMark(row.session.harness).name, folder]
            .compactMap { $0 }.joined(separator: " · ")
        return model?.row?.modelLabel == nil ? lead : lead + " ·"
    }
}

/// The session's status and the stream's state, beside the header's
/// buttons. Ownership needs no chip: an observed session already says
/// so in the banner below, and an attached one shows an enabled
/// composer. The stream's dot gets its words only when it isn't live.
private struct ChatBadges: View {
    var model: SessionChatModel

    var body: some View {
        if let row = model.row, let status = HubSessionRow.statusTitle(row.status) { Badge(status) }
        HStack(spacing: 6) {
            Circle()
                .fill(model.connection == .live ? Theme.success
                      : model.connection == .reconnecting ? Theme.warning : Theme.textMuted)
                .frame(width: Theme.statusDot, height: Theme.statusDot)
            if model.connection != .live {
                Text(model.connection == .reconnecting ? "Reconnecting…" : "Connecting…")
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
            }
        }
        .padding(.horizontal, 4)
        .hoverTip(model.connection == .live ? "Streaming the session live."
                  : model.connection == .reconnecting ? "Reconnecting to the session stream."
                  : "Connecting to the session stream.")
    }

}

private struct ChatTranscript: View {
    var model: SessionChatModel
    var note: String?
    /// The read-only copy of a pruned transcript: a notice in place of the
    /// observed banner, no composer, and a calm line when the hub cannot
    /// read the copy yet.
    var mirrored = false
    var restore: (() -> Void)? = nil
    var restoreNote: (text: String, ok: Bool)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let note {
                Card {
                    Label(note, systemImage: "exclamationmark.triangle")
                        .font(Theme.bodySmall).foregroundStyle(Theme.warningText)
                }
            }
            if let restoreNote {
                RestoreNoteCard(note: restoreNote)
            }
            if mirrored {
                MirroredBanner(harness: model.row?.harness, restore: restore)
            } else if let error = model.loadError {
                ErrorCard(message: error, actionTitle: "Try again") {
                    Task { await model.start() }
                }
            }
            if !mirrored, let row = model.row, !row.isAttached {
                ObservedBanner(model: model, row: row)
            }
            if model.loading {
                ChatSkeleton()
            } else if mirrored, model.loadError != nil || (model.items.isEmpty && !model.hasEarlier) {
                Text("This session's chat is not available yet.")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                Spacer()
            } else if model.loadError == nil {
                log
                if !mirrored { Composer(model: model) }
            } else {
                Spacer()
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
    }

    private var log: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                if model.hasEarlier {
                    Button(model.loadingEarlier ? "Loading…" : "Show earlier messages") {
                        Task { await model.loadEarlier() }
                    }
                    .secondaryButton()
                    .disabled(model.loadingEarlier)
                    .frame(maxWidth: .infinity)
                }
                if model.items.isEmpty, model.streaming.isEmpty {
                    Text("No messages yet.")
                        .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                }
                ForEach(ChatGroup.fold(model.items)) { group in
                    switch group {
                    case .item(let item):
                        ChatEntry(item: item) { rid, decision in
                            Task { await model.decide(rid, decision) }
                        }
                    case .toolRun(let items):
                        ToolRunCard(items: items) { rid, decision in
                            Task { await model.decide(rid, decision) }
                        }
                    }
                }
                // Replies still being written, newest text last; each one's
                // final message takes its place in the transcript above.
                ForEach(model.streaming) { item in
                    ChatEntry(item: item) { _, _ in }
                }
                if model.working {
                    WorkingDots().accessibilityLabel("Agent is working")
                }
            }
            .padding(.vertical, 4)
            .padding(.trailing, 12)
        }
        .scrollContentBackground(.hidden)
        // Opens on the newest message, and stays there as more arrive.
        .defaultScrollAnchor(.bottom)
        .defaultScrollAnchor(.bottom, for: .sizeChanges)
    }
}

/// The transcript's shape before history arrives: message shapes sweeping
/// the way Overview's skeleton cards do, with the composer's outline where
/// it lands.
private struct ChatSkeleton: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            lines(trailing: [140, 60, 320])
            userBubble(230)
            SkeletonBlock(height: 34, radius: Theme.controlRadius)
            lines(trailing: [40, 210])
            userBubble(150)
            lines(trailing: [90, 270, 160])
            Spacer(minLength: 24)
            composer
        }
        .padding(.vertical, 4)
        .padding(.trailing, 12)
        .frame(maxHeight: .infinity, alignment: .top)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading the transcript")
    }

    /// A few assistant reply lines, each short of the full width.
    private func lines(trailing: [CGFloat]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(trailing, id: \.self) { SkeletonBlock(height: 12).padding(.trailing, $0) }
        }
    }

    /// A right-aligned bubble where the user's own messages sit.
    private func userBubble(_ width: CGFloat) -> some View {
        HStack {
            Spacer(minLength: 120)
            SkeletonBlock(width: width, height: 38, radius: Theme.blockRadius)
        }
    }

    /// The composer's outline: the message field and Send.
    private var composer: some View {
        HStack(spacing: 8) {
            SkeletonBlock(height: Theme.fieldHeight, radius: Theme.controlRadius)
            SkeletonBlock(width: 66, height: Theme.fieldHeight, radius: Theme.controlRadius)
        }
    }
}

/// A mirrored session's chat is swe's read-only copy: the one wording,
/// neutral, with Restore beside it when the page offers it.
private struct MirroredBanner: View {
    var harness: String?
    var restore: (() -> Void)?

    var body: some View {
        Card {
            HStack(spacing: 16) {
                Label(SessionInfo.mirroredHint, systemImage: "doc.on.doc")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                Spacer()
                if let restore {
                    Button("Restore transcript", action: restore)
                        .secondaryButton()
                        .help("Put swe's copy back where \(HarnessMark(harness ?? "").name) kept it, so it resumes again")
                }
            }
        }
    }
}

/// An observed session is read-only here; the web app says why above
/// its transcript.
private struct ObservedBanner: View {
    var model: SessionChatModel
    var row: HubSessionRow
    /// Attach was clicked: the banner asks before stopping the terminal.
    @State private var confirming = false

    var body: some View {
        Card {
            if confirming, model.canAttach {
                HStack(alignment: .top, spacing: 16) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Attach this session?").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                        Text("Attach stops \(row.agent ?? HarnessMark(row.harness ?? "").name) in the terminal "
                             + "running this session, then resumes the session under SWE so you can send "
                             + "to it from here.")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    }
                    Spacer()
                    Button("Cancel") { confirming = false }
                        .secondaryButton()
                        .keyboardShortcut(.cancelAction)
                    Button(model.attaching ? "Attaching…" : "Stop it and attach") {
                        Task {
                            await model.attach()
                            confirming = false
                        }
                    }
                    .primaryButton()
                    .disabled(model.attaching)
                }
            } else {
                HStack(spacing: 16) {
                    Label(text, systemImage: "eye")
                        .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                    Spacer()
                    if model.canAttach {
                        Button("Attach") { confirming = true }.primaryButton()
                    }
                }
            }
        }
    }

    private var text: String {
        if row.attachable != true { return "Observed session: read-only (this harness can't be attached)." }
        if row.status == "active" { return "Observed session: mid-turn. Attach once it is idle." }
        return "Observed session: read-only until attached."
    }
}

private struct ChatEntry: View {
    var item: HubChatItem
    /// Answers an approval: request id, then the decision — `allow`,
    /// `always`, `deny`, or a menu row's digit.
    var decide: (String, String) -> Void

    var body: some View {
        switch item.kind {
        case .user(let text, let send):
            HStack {
                Spacer(minLength: 120)
                VStack(alignment: .trailing, spacing: 4) {
                    MarkdownBlocks(blocks: ChatRender.blocks(text), inline: ChatRender.inline)
                        .font(Theme.bodySmall)
                        .foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .background(Theme.subtle, in: .rect(cornerRadius: Theme.blockRadius))
                        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius)
                            .strokeBorder(send.map(Self.failed) == true ? Theme.danger : Theme.border, lineWidth: 1))
                        .opacity(send?.inFlight == true ? 0.7 : 1)
                    if let send, !send.note.isEmpty {
                        Text(send.note)
                            .font(Theme.caption)
                            .foregroundStyle(Self.failed(send) ? Theme.dangerText : Theme.textSecondary)
                    }
                }
            }
        case .assistant(let text):
            MarkdownBlocks(blocks: ChatRender.blocks(text), inline: ChatRender.inline)
                .font(Theme.bodySmall)
                .foregroundStyle(Theme.textPrimary)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .thinking(let text, let seconds):
            Fold(title: Label(ThoughtLabel.text(seconds), systemImage: "brain").foregroundStyle(Theme.textSecondary)) {
                MarkdownBlocks(blocks: ChatRender.blocks(text), inline: ChatRender.inline)
                    .font(Theme.bodySmall)
                    .foregroundStyle(Theme.textSecondary)
            }
        case .tool(let call, let result):
            ToolCard(call: call, result: result)
        case .approval(let rid, let prompt, let options, let picked, let state):
            ApprovalCard(prompt: prompt, options: options, picked: picked, state: state) { decide(rid, $0) }
        case .compaction:
            Text("Context compacted")
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
                .frame(maxWidth: .infinity)
        case .system(let text):
            Text(text)
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .error(let text):
            VStack(alignment: .leading, spacing: 4) {
                Label("Error", systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.dangerText)
                MarkdownBlocks(blocks: ChatRender.blocks(text), inline: ChatRender.inline)
                    .font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
            }
        }
    }
}

extension ChatEntry {
    static func failed(_ send: HubSendState) -> Bool {
        if case .failed = send { return true }
        return false
    }
}

/// The prompt box under the transcript: Queue or Steer, the message,
/// Stop and Send. Disabled with the reason as its placeholder while the
/// session is observed, as in the web app. Typing `/` first opens the
/// command menu: the hub lists what this session's harness will run,
/// a pick puts the command's text in the box, and Send delivers it as
/// a plain message the harness expands on arrival. Typing `@` opens the
/// file menu: paths under the session's folder, a pick puts `@path` in
/// the box (a folder stays open to pick inside it). A queue-mode send
/// the hub holds until the turn ends waits in the Queued tray above the
/// box, where Edit brings its text back into the box to save.
private struct Composer: View {
    var model: SessionChatModel
    /// The box's text, kept on the tray so Edit can swap a queued
    /// message into it.
    private var draft: String {
        get { model.tray.draft }
        nonmutating set { model.tray.draft = newValue }
    }
    /// The caret or selection, so Shift-Return breaks the line there.
    @State private var selection: TextSelection?
    /// The `/` menu opens only while the box is being typed in.
    @FocusState private var focused: Bool
    /// The row the arrows lit in the open `/` or `@` menu.
    @State private var active = 0
    /// Queue waits for the agent's turn to end; Steer writes now.
    @AppStorage("sessionChatSendMode") private var mode = "queue"

    /// The caret's Character offset into `draft`; nil is the end.
    private var caret: Int? {
        guard let selection, case .selection(let range) = selection.indices else { return nil }
        return Caret.offset(range.upperBound, in: draft)
    }

    /// The `/command` being typed at the start of the box: a `/` first,
    /// the caret still inside that first word, while it has focus.
    private var slash: SlashToken? {
        focused && model.canSend ? Slash.token(in: draft, cursor: caret) : nil
    }

    /// The `@path` being typed where the caret is, while the box has
    /// focus and the `/` menu doesn't hold it.
    private var mention: MentionToken? {
        focused && model.canSend && slash == nil ? Mention.token(in: draft, cursor: caret) : nil
    }

    /// The `@` menu's highlighted file, while it's open with rows.
    private var activeEntry: FileEntry? {
        let rows = MentionMenuCard.rows(model.mentions)
        guard !model.commands.isOpen, model.mentions.isOpen, !rows.isEmpty else { return nil }
        return rows[min(active, rows.count - 1)]
    }

    /// The menu's highlighted command, while it's open with rows.
    private var activeCommand: SlashCommand? {
        let rows = model.commands.matches
        guard model.commands.isOpen, !rows.isEmpty else { return nil }
        return rows[min(active, rows.count - 1)]
    }

    var body: some View {
        let observed = !model.canSend
        let tray = model.tray
        let editing = tray.editingId != nil
        VStack(alignment: .leading, spacing: 8) {
            if !tray.sends.isEmpty {
                QueuedTrayCard(tray: tray,
                               onEdit: { edit($0) },
                               onRemove: { cid in Task { await tray.remove(cid) } },
                               onSendNow: { cid in Task { await tray.sendNow(cid) } },
                               onRemoveAll: { Task { await tray.removeAll() } })
                    .transition(.opacity)
            }
            if editing || tray.editNote != nil {
                QueuedEditBar(editing: editing, note: tray.editNote) { tray.cancelEdit() }
                    .transition(.opacity)
            }
            if model.commands.isOpen {
                SlashCommandMenuCard(menu: model.commands, row: model.row, active: $active) { pick($0) }
                    .transition(.opacity)
            } else if model.mentions.isOpen {
                MentionMenuCard(search: model.mentions, active: $active) { pick($0) }
                    .transition(.opacity)
            }
            HStack(alignment: .bottom, spacing: 8) {
                if !observed {
                    SelectMenu(options: [(value: "queue", title: "Queue"), (value: "steer", title: "Steer")],
                               selection: $mode, width: 96)
                        .hoverTip("Queue waits for the agent to finish its turn; Steer writes now.", title: "Send mode")
                }
                TextField(observed ? "Read-only until this session is attached"
                            : editing ? "Edit the queued message" : "Message the session",
                          text: Bindable(tray).draft, selection: $selection, axis: .vertical)
                    .lineLimit(1...8)
                    .sweField(multiline: true)
                    .disabled(observed)
                    .focused($focused)
                    // Shift-Return always starts a line. With the `/`
                    // or `@` menu open Return or Tab picks, the arrows
                    // move the highlight and Escape closes it;
                    // otherwise Return sends.
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) {
                            newline()
                        } else if model.commands.isOpen {
                            switch Slash.ReturnAction.action(
                                row: activeCommand != nil,
                                loading: model.commands.loading
                            ) {
                            case .pick:
                                if let command = activeCommand { pick(command) }
                            case .send:
                                submit()
                            case .wait:
                                break
                            }
                        } else if model.mentions.isOpen {
                            switch Slash.ReturnAction.action(
                                row: activeEntry != nil,
                                loading: model.mentions.firstLoad
                            ) {
                            case .pick:
                                if let entry = activeEntry { pick(entry) }
                            case .send:
                                submit()
                            case .wait:
                                break
                            }
                        } else {
                            submit()
                        }
                        return .handled
                    }
                    .onKeyPress(.tab, phases: .down) { _ in
                        if let command = activeCommand {
                            pick(command)
                        } else if let entry = activeEntry {
                            pick(entry)
                        } else {
                            return .ignored
                        }
                        return .handled
                    }
                    .onKeyPress(.upArrow, phases: .down) { _ in move(-1) }
                    .onKeyPress(.downArrow, phases: .down) { _ in move(1) }
                    .onKeyPress(.escape, phases: .down) { _ in
                        if model.commands.isOpen {
                            model.commands.dismiss()
                        } else if model.mentions.isOpen {
                            model.mentions.dismiss()
                        } else if editing {
                            tray.cancelEdit()
                        } else {
                            return .ignored
                        }
                        return .handled
                    }
                Button { Task { await model.interrupt() } } label: {
                    Image(systemName: "stop.fill")
                }
                .iconButton()
                .disabled(observed || model.stopping)
                .hoverTip("Stop the agent's turn")
                .accessibilityLabel("Stop")
                Button(editing ? "Save" : "Send", action: submit)
                    .primaryButton()
                    .disabled(observed || tray.savingEdit
                              || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .animation(.easeOut(duration: 0.15), value: model.commands.isOpen)
        .animation(.easeOut(duration: 0.15), value: model.mentions.isOpen)
        .animation(.easeOut(duration: 0.2), value: tray.sends.isEmpty)
        .animation(.easeOut(duration: 0.2), value: editing)
        .onChange(of: slash, initial: true) { _, token in
            model.commands.track(token)
            active = 0
        }
        .onChange(of: mention, initial: true) { _, token in
            model.mentions.track(token)
            active = 0
        }
        // New results light their first row, as the web app does.
        .onChange(of: model.mentions.entries) { active = 0 }
    }

    /// An arrow key with the `/` or `@` menu open relights its rows;
    /// with both closed the caret moves as usual.
    private func move(_ delta: Int) -> KeyPress.Result {
        let count = model.commands.isOpen ? model.commands.matches.count
            : model.mentions.isOpen ? MentionMenuCard.rows(model.mentions).count : 0
        guard count > 0 else { return .ignored }
        active = (active + delta + count) % count
        return .handled
    }

    /// The picked command's text replaces the `/word`; the caret lands
    /// just after it, ready for an argument.
    private func pick(_ command: SlashCommand) {
        guard let token = Slash.token(in: draft, cursor: caret) else { return }
        let next = Slash.insert(command, replacing: token, in: draft)
        draft = next.text
        selection = TextSelection(insertionPoint: Caret.index(next.cursor, in: draft))
    }

    /// The picked path replaces the `@word`: a file's caret lands after
    /// its space, a folder's after its slash so the menu lists inside it.
    private func pick(_ entry: FileEntry) {
        guard let token = Mention.token(in: draft, cursor: caret) else { return }
        let next = Mention.insert(entry.path, isDir: entry.isDir, replacing: token, in: draft)
        draft = next.text
        selection = TextSelection(insertionPoint: Caret.index(next.cursor, in: draft))
    }

    /// A line break in place of the selection, or at the end when the
    /// field reports none.
    private func newline() {
        let next = Caret.newline(in: draft, replacing: selection)
        draft = next.text
        selection = TextSelection(insertionPoint: Caret.index(next.cursor, in: draft))
    }

    /// Edit: the queued message's text replaces the box's, caret at its end.
    private func edit(_ cid: String) {
        guard model.tray.beginEdit(cid) else { return }
        selection = TextSelection(insertionPoint: draft.endIndex)
        focused = true
    }

    private func submit() {
        let text = draft
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, model.canSend else { return }
        if model.tray.editingId != nil {
            Task { await model.tray.saveEdit() }
            return
        }
        draft = ""
        model.tray.clearEditNote()
        Task { await model.send(text, mode: mode) }
    }
}

/// The `/` menu above the prompt box: the commands this session's
/// harness will run, filtered by what follows the `/`. A port of the
/// iPhone app's `CommandMenuView` (app/ios) at desktop density, with
/// the arrows' lit row standing in for touch.
private struct SlashCommandMenuCard: View {
    var menu: SlashCommandMenu
    /// The session's hub row: the notes' harness and machine names.
    var row: HubSessionRow?
    /// The highlighted row, which hovering also sets.
    @Binding var active: Int
    var onPick: (SlashCommand) -> Void

    /// The row's height at the current text zoom: its two text lines
    /// grow with it, so the scroll window's cap must match.
    private var rowHeight: CGFloat {
        TextZoom.pointSize(40, at: TextZoomModel.shared.zoom)
    }
    static let visibleRows = 6

    var body: some View {
        let notes = menu.response.map {
            Slash.notes($0, harness: row?.harness.map { HarnessMark($0).name } ?? "this harness",
                        machine: row?.machineId ?? "that machine", remote: row?.remote == true)
        }
        VStack(alignment: .leading, spacing: 0) {
            list(notes: notes)
            if let notes, !notes.footnotes.isEmpty {
                Rectangle().fill(Theme.border).frame(height: 1).padding(.top, 4)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(notes.footnotes, id: \.self) {
                        Text($0).font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 4)
            }
        }
        .padding(.vertical, 4)
        .background(Theme.card, in: .rect(cornerRadius: Theme.blockRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius).strokeBorder(Theme.border, lineWidth: 1))
        .clipShape(.rect(cornerRadius: Theme.blockRadius))
        .accessibilityLabel("Commands")
    }

    /// The rows, capped at `visibleRows` then scrolling; the lit row
    /// stays in view while the arrows move.
    @ViewBuilder
    private func list(notes: (empty: String?, footnotes: [String])?) -> some View {
        // A failed load shows above whatever list is kept from before.
        let rows = menu.matches
        let shown = menu.loading ? 0 : max(1, rows.count)
        let count = max(1, shown + (menu.error == nil ? 0 : 1))
        if count > Self.visibleRows {
            ScrollViewReader { proxy in
                ScrollView(.vertical) { stack(rows: rows, notes: notes) }
                    .defaultScrollAnchor(.top)
                    .frame(height: CGFloat(Self.visibleRows) * rowHeight)
                    .onChange(of: active) { _, i in proxy.scrollTo(min(i, rows.count - 1)) }
            }
        } else {
            stack(rows: rows, notes: notes)
        }
    }

    private func stack(rows: [SlashCommand], notes: (empty: String?, footnotes: [String])?) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if let error = menu.error {
                Button { Task { await menu.refresh() } } label: { note(error, icon: "exclamationmark.circle") }
                    .buttonStyle(.plain)
                    .hoverTip("Fetch the command list again")
            }
            if menu.loading {
                if menu.error == nil { note("Loading commands") }
            } else if let empty = notes?.empty {
                note(empty)
            } else if rows.isEmpty {
                note("No command matches /\(menu.token?.query ?? "")")
            } else {
                ForEach(Array(rows.enumerated()), id: \.offset) { i, command in
                    Button { onPick(command) } label: { row(command) }
                        .buttonStyle(.plain)
                        .background(i == lit(in: rows) ? Theme.hover : .clear)
                        .onHover { if $0 { active = i } }
                        .id(i)
                }
            }
        }
    }

    /// A muted line in the menu: still loading, nothing matched, or why
    /// the menu can't help here.
    private func note(_ text: String, icon: String? = nil) -> some View {
        HStack(spacing: 10) {
            if let icon {
                Image(systemName: icon).font(Theme.font(size: 13)).foregroundStyle(Theme.textMuted).frame(width: 18)
            }
            Text(text).font(Theme.caption).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 12)
        .frame(minHeight: rowHeight)
    }

    /// The lit row clamped into the list.
    private func lit(in rows: [SlashCommand]) -> Int {
        min(active, max(rows.count - 1, 0))
    }

    private func row(_ command: SlashCommand) -> some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(command.label).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                        .lineLimit(1).truncationMode(.middle)
                        .layoutPriority(1)
                    if let hint = command.argumentHint, !hint.isEmpty {
                        Text(hint).font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .lineLimit(1).truncationMode(.tail)
                    }
                }
                if let description = command.description, !description.isEmpty {
                    Text(description).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .lineLimit(1).truncationMode(.tail)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if let tag = command.sourceLabel {
                Text(tag).font(Theme.font(size: 11, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 7).frame(height: 20)
                    .background(Theme.subtle, in: .capsule)
                    .overlay(Capsule().strokeBorder(Theme.border, lineWidth: 1))
                    .fixedSize()
            }
        }
        .padding(.horizontal, 12)
        .frame(height: rowHeight)
        .contentShape(.rect)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel([command.label, command.argumentHint, command.description, command.sourceLabel]
            .compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: ", "))
        .accessibilityHint("Inserts the command")
    }
}

/// A chevron and a title that open what's under them, closed at first.
private struct Fold<Title: View, Content: View>: View {
    var title: Title
    @ViewBuilder var content: Content
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.easeInOut(duration: Theme.fadeDuration)) { open.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(Theme.font(size: 9, weight: .semibold))
                        .foregroundStyle(Theme.textMuted)
                        .rotationEffect(open ? .degrees(90) : .zero)
                    title.font(Theme.caption)
                }
                .contentShape(.rect)
            }
            .plainButton()
            if open {
                content.padding(.leading, 15)
            }
        }
    }
}

/// Three or more tool calls in a row fold into one "Used N tools" card,
/// so a long agent turn doesn't bury what it said (the phone's
/// ChatEntry.fold, same rule).
private enum ChatGroup: Identifiable {
    case item(HubChatItem)
    case toolRun([HubChatItem])

    var id: String {
        switch self {
        case .item(let item): item.id
        case .toolRun(let items): "run-\(items[0].id)"
        }
    }

    static func fold(_ items: [HubChatItem], minRun: Int = 3) -> [ChatGroup] {
        var out: [ChatGroup] = []
        var run: [HubChatItem] = []
        func flush() {
            if run.count >= minRun { out.append(.toolRun(run)) } else { out += run.map(ChatGroup.item) }
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

/// A run of tool calls folded into one card: the count and which tools
/// ran, opening to each call's own card (the phone's ToolRunCard).
private struct ToolRunCard: View {
    var items: [HubChatItem]
    var decide: (String, String) -> Void

    var body: some View {
        Fold(title: HStack(spacing: 8) {
            Image(systemName: "wrench.and.screwdriver")
                .font(Theme.font(size: 12)).foregroundStyle(Theme.textSecondary)
                .frame(width: 16)
            Text("Used \(items.count) tools")
                .font(Theme.font(size: 12, weight: .medium)).foregroundStyle(Theme.textPrimary)
            Text(names)
                .font(Theme.font(size: 12, design: .monospaced))
                .foregroundStyle(Theme.textSecondary)
                .lineLimit(1).truncationMode(.middle)
        }) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(items) { ChatEntry(item: $0, decide: decide) }
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.border, lineWidth: 1))
    }

    /// "Bash ×4, Edit, Read": what ran, most used first.
    private var names: String {
        var counts: [String: Int] = [:]
        var order: [String] = []
        for item in items {
            guard case .tool(let call, _) = item.kind else { continue }
            let name = ToolCard.displayName(call.name)
            if counts[name] == nil { order.append(name) }
            counts[name, default: 0] += 1
        }
        return order.sorted { counts[$0]! > counts[$1]! }
            .map { counts[$0]! > 1 ? "\($0) ×\(counts[$0]!)" : $0 }
            .joined(separator: ", ")
    }
}

/// A tool call: its kind icon, name and argument summary, opening to the
/// full arguments and the output once it lands.
private struct ToolCard: View {
    var call: HubToolCall
    var result: String?

    var body: some View {
        let diff = ToolDiff.from(call)
        Fold(title: HStack(spacing: 8) {
            Image(systemName: Self.icon(call.name))
                .font(Theme.font(size: 12)).foregroundStyle(Theme.textSecondary)
                .frame(width: 16)
            Text(Self.displayName(call.name))
                .font(Theme.font(size: 12, weight: .medium)).foregroundStyle(Theme.textPrimary)
            if let summary = Self.summary(call), !summary.isEmpty {
                Text(summary)
                    .font(Theme.font(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.textSecondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            if let diff {
                Text("+\(diff.added)").font(Theme.font(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.success)
                Text("−\(diff.removed)").font(Theme.font(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.dangerText)
            }
        }) {
            VStack(alignment: .leading, spacing: 8) {
                if let diff {
                    ToolDiffView(diff: diff)
                } else if let readable = ToolDisplay.readable(call), !readable.text.isEmpty {
                    CodeBlock(text: String(readable.text.prefix(8000)), language: readable.language)
                }
                if let result {
                    Text("Output").font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    CodeBlock(text: result.isEmpty ? "(no output)" : String(result.prefix(8000)),
                              language: result.isEmpty ? nil : ToolDisplay.outputLanguage(call, result: result))
                }
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.border, lineWidth: 1))
    }
}

extension ToolCard {
    /// The hub's summary leads with the tool's name ("Bash · make test"),
    /// which the card already shows.
    static func summary(_ call: HubToolCall) -> String? {
        guard let summary = call.argsSummary else { return nil }
        let lead = call.name + " · "
        return summary.hasPrefix(lead) ? String(summary.dropFirst(lead.count)) : summary
    }

    /// The phone's icon per tool kind (`ToolCard.icon` in the remote app).
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

    /// `mcp__server__tool` reads as `server · tool`, the way the phone
    /// shows an MCP tool; other names pass through.
    static func displayName(_ name: String) -> String {
        guard name.hasPrefix("mcp__") else { return name }
        let parts = name.dropFirst(5).components(separatedBy: "__").filter { !$0.isEmpty }
        guard parts.count >= 2 else { return parts.first ?? name }
        return parts[0] + " · " + parts[1...].joined(separator: "__")
    }
}

/// An edit call's changes: the file on top, then sign-column rows tinted
/// added/removed with the file's own language inside (the web app's
/// `.diff` panel). Removed rows scan the old file's code, added and
/// context rows the new one's, so a comment or string open on one row
/// still colours the next.
private struct ToolDiffView: View {
    var diff: ToolDiff
    /// Sections are capped at 200 rows until tapped open — a Write's
    /// thousand added rows would bury everything after them.
    @State private var expandedSections: Set<Int> = []

    private static let rowCap = 200

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if !diff.file.isEmpty {
                Text(diff.file)
                    .font(Theme.font(size: 11, design: .monospaced))
                    .foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(alignment: .bottom) { Divider().overlay(Theme.border) }
            }
            ForEach(Array(diff.sections.enumerated()), id: \.offset) { si, lines in
                let shown = expandedSections.contains(si) ? lines : Array(lines.prefix(Self.rowCap))
                ForEach(Array(rows(shown).enumerated()), id: \.offset) { _, row in
                    HStack(alignment: .top, spacing: 6) {
                        Text(sign(row.kind))
                            .foregroundStyle(tint(row.kind))
                            .frame(width: 8)
                        if row.kind == .meta {
                            Text(row.text.isEmpty ? " " : row.text)
                                .foregroundStyle(Theme.textMuted)
                        } else {
                            Text(AttributedString(tokens: row.toks))
                        }
                        Spacer(minLength: 0)
                    }
                    .font(Theme.font(size: 11, design: .monospaced))
                    .padding(.horizontal, 10).padding(.vertical, 1)
                    .background(background(row.kind))
                }
                if !expandedSections.contains(si), lines.count > Self.rowCap {
                    Button {
                        expandedSections.insert(si)
                    } label: {
                        Text("Show \(lines.count - Self.rowCap) more rows")
                            .font(Theme.font(size: 11, design: .monospaced))
                            .foregroundStyle(Theme.textSecondary)
                            .padding(.horizontal, 10).padding(.vertical, 3)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 6)
        }
        .background(Theme.code, in: .rect(cornerRadius: 6))
    }

    /// Each row's styled runs, computed once per body so multiline
    /// comment and string state carries across rows — a meta row jumps
    /// to unrelated code, so an open token resets there.
    private func rows(_ lines: [DiffLine]) -> [(kind: DiffLine.Kind, text: String, toks: [SyntaxToken])] {
        let lang = diff.language ?? ""
        var stOld: ScanState?
        var stNew: ScanState?
        return lines.map { line in
            let text = line.text.isEmpty ? " " : line.text
            switch line.kind {
            case .meta:
                // An @@ or *** header jumps to unrelated code; an
                // "N unchanged lines" row is elided context in the
                // same region, so the open token carries through it.
                if line.text.hasPrefix("@@") || line.text.hasPrefix("***") {
                    stOld = nil
                    stNew = nil
                }
                return (.meta, line.text, [])
            case .removed:
                return (.removed, line.text,
                        SyntaxHighlight.tokens(text, language: lang, state: &stOld))
            case .added:
                return (.added, line.text,
                        SyntaxHighlight.tokens(text, language: lang, state: &stNew))
            case .context:
                // The same line sits in both files: show it through the
                // new side's state, and walk the old side over it too.
                let toks = SyntaxHighlight.tokens(text, language: lang, state: &stNew)
                _ = SyntaxHighlight.tokens(text, language: lang, state: &stOld)
                return (.context, line.text, toks)
            }
        }
    }

    private func sign(_ kind: DiffLine.Kind) -> String {
        switch kind { case .added: "+"; case .removed: "−"; default: " " }
    }
    private func tint(_ kind: DiffLine.Kind) -> Color {
        switch kind { case .added: Theme.success; case .removed: Theme.dangerText; default: Theme.textMuted }
    }
    private func background(_ kind: DiffLine.Kind) -> Color {
        switch kind { case .added: Theme.diffAdded; case .removed: Theme.diffRemoved; default: .clear }
    }
}

private struct CodeBlock: View {
    var text: String
    var language: String? = nil

    var body: some View {
        Text(AttributedString(highlighting: text, language: language))
            .font(Theme.font(size: 11, design: .monospaced))
            .foregroundStyle(Theme.textPrimary)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.code, in: .rect(cornerRadius: 6))
    }
}

/// An approval the agent asked for: the pane's numbered menu rows when
/// the event carried them (the web app's `.approval-actions.menu`), else
/// Allow, Always and Deny. An answer folds the card to a
/// "Approval · {what was picked}" row that opens back to the full card,
/// the picked row lit and the rest greyed.
private struct ApprovalCard: View {
    var prompt: String
    var options: [HubApprovalOption]
    /// The decision the card lights as chosen: the row answered here,
    /// or the resolved decision once it names a row. The model computes
    /// it (the web app's `.chosen` / `[data-dec]` mark).
    var picked: String?
    var state: HubApprovalState
    var decide: (String) -> Void
    /// A resolved card re-opened to the full prompt and rows.
    @State private var open = false

    private var menu: [(n: String, label: String)] { ApprovalRows.menu(options) }

    /// The generic fallback repeats the title, so only a real question
    /// or context goes under it (the web app's textRow rule).
    private var showsPrompt: Bool {
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return !text.isEmpty && text != "Approval requested"
    }

    private var resolved: Bool {
        if case .resolved = state { return true }
        return false
    }

    /// Folded to the verdict row: answered and not re-opened.
    private var folded: Bool { resolved && !open }

    /// A row draws enabled while the card is answerable, or once settled
    /// only when it is the picked one; `allowsHitTesting` on the rows
    /// keeps a settled card's lit row unclickable.
    private func lit(_ n: String) -> Bool {
        state.answerable || picked == n
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if case .resolved(let decision) = state {
                Button {
                    withAnimation(.easeInOut(duration: Theme.fadeDuration)) { open.toggle() }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "chevron.right")
                            .font(Theme.font(size: 9, weight: .semibold))
                            .foregroundStyle(Theme.textMuted)
                            .rotationEffect(open ? .degrees(90) : .zero)
                        Text("Approval · \(verdict(decision))")
                            .font(Theme.caption).foregroundStyle(Theme.textPrimary)
                    }
                    .contentShape(.rect)
                }
                .plainButton()
            }
            if !folded {
                Text("Approval needed").font(Theme.caption).foregroundStyle(Theme.warningText)
                if showsPrompt {
                    // Verbatim, never markdown: `rm *.log *.tmp` must not
                    // lose its stars to italics, nor a link's text hide
                    // where it goes. This is what Allow agrees to.
                    Text(verbatim: prompt)
                        .font(Theme.font(size: 12, design: .monospaced)).foregroundStyle(Theme.textPrimary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                rows
                if let note {
                    Text(note).font(Theme.caption)
                        .foregroundStyle(isFailure ? Theme.dangerText : Theme.textSecondary)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(RoundedRectangle(cornerRadius: Theme.blockRadius)
            .strokeBorder(state.answerable ? Theme.warning : Theme.border, lineWidth: 1))
        // A rejection landing after the card folded re-opens it (the web
        // app clears .open), so the settle that follows folds it afresh.
        .onChange(of: state) { _, new in
            if case .failed = new { open = false }
        }
    }

    /// The answer rows: the pane's menu stacked like the TUI prints it,
    /// or the three semantic buttons when the ask carried no menu.
    @ViewBuilder private var rows: some View {
        if menu.isEmpty {
            HStack(spacing: 8) {
                semantic("Allow", decision: "allow")
                semantic("Always", decision: "always")
                    .hoverTip("Allow this and don't ask again for the same kind of action.")
                semantic("Deny", decision: "deny", destructive: true)
            }
            .allowsHitTesting(state.answerable)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(menu.indices, id: \.self) { i in
                    Button { decide(menu[i].n) } label: {
                        Text(menu[i].label)
                    }
                    .buttonStyle(MenuOptionStyle())
                    .focusRing(cornerRadius: Theme.controlRadius)
                    .disabled(!lit(menu[i].n))
                }
            }
            .allowsHitTesting(state.answerable)
        }
    }

    private func semantic(_ title: String, decision: String, destructive: Bool = false) -> some View {
        Button(title, role: destructive ? .destructive : nil) { decide(decision) }
            .secondaryButton()
            .disabled(!lit(decision))
    }

    /// "Approval · {what was picked}": the semantic word for a semantic
    /// decision, else the row the decision names (it survives a reload
    /// where the lit pick was forgotten), else the lit row's label, else
    /// "Answered" (the web app's `DECISIONS` lookup, then the chosen
    /// button's text).
    private func verdict(_ decision: String?) -> String {
        switch decision {
        case "allow": "Allowed"
        case "always": "Always allowed"
        case "deny": "Denied"
        default: rowTitle(decision) ?? rowTitle(picked) ?? "Answered"
        }
    }

    /// A row's caption as the card lists it: the pane label on a menu
    /// card, the button's title on a semantic one.
    private func rowTitle(_ n: String?) -> String? {
        guard let n else { return nil }
        if menu.isEmpty {
            switch n {
            case "allow": return "Allow"
            case "always": return "Always"
            case "deny": return "Deny"
            default: return nil
            }
        }
        return menu.first { $0.n == n }?.label
    }

    private var isFailure: Bool {
        if case .failed = state { return true }
        return false
    }

    private var note: String? {
        switch state {
        case .open: nil
        case .submitting: "Sending your answer…"
        case .queued: "Queued: waiting for the satellite"
        case .failed(let why): why
        // The verdict sits on the toggle row once answered.
        case .resolved: nil
        }
    }
}

/// A pane menu row in the secondary button's chrome, wrapping and
/// left-aligned like the web app's `.approval-opt`: the labels run long
/// ("Yes, always allow `sudo` commands in all projects").
private struct MenuOptionStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        MenuOptionBody(configuration: configuration)
    }
}

private struct MenuOptionBody: View {
    let configuration: ButtonStyleConfiguration
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    var body: some View {
        configuration.label
            .font(Theme.bodySmall)
            .multilineTextAlignment(.leading)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .foregroundStyle(hovering ? Theme.textStrong : Theme.textPrimary)
            .background(hovering ? Theme.hover : Theme.secondaryBackground,
                        in: .rect(cornerRadius: Theme.controlRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius)
                .strokeBorder(Theme.borderStrong, lineWidth: 1))
            .opacity(isEnabled ? (configuration.isPressed ? 0.85 : 1) : 0.45)
            .animation(.easeOut(duration: 0.2), value: hovering)
            .contentShape(.rect(cornerRadius: Theme.controlRadius))
            .onHover { hovering = $0 }
    }
}

/// Three dots that pulse while the agent works.
private struct WorkingDots: View {
    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.35)) { context in
            let step = Int(context.date.timeIntervalSinceReferenceDate / 0.35) % 3
            HStack(spacing: 5) {
                ForEach(0..<3, id: \.self) { i in
                    Circle()
                        .fill(Theme.textMuted)
                        .frame(width: 6, height: 6)
                        .opacity(i == step ? 1 : 0.35)
                }
            }
        }
        .padding(.vertical, 4)
    }
}
