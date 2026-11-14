import SWECore
import SwiftUI

/// What a Sessions row's menu and the chat header's More menu offer for a
/// session this Mac's hub lists: the iPhone's swipe-left actions and the
/// web app's More actions.
enum SessionMenuAction {
    case pin(Bool)
    case rename
    case fork
}

/// Pin or Unpin, Rename and Fork as menu items, from the hub's meta for
/// the session. Fork shows only where the hub says the harness can.
struct SessionHubMenuItems: View {
    var meta: HubSessionMeta
    var act: (SessionMenuAction) -> Void

    var body: some View {
        Button(meta.pinned ? "Unpin" : "Pin") { act(.pin(!meta.pinned)) }
        Button("Rename…") { act(.rename) }
        if meta.canFork {
            Button("Fork…") { act(.fork) }
        } else if meta.mirrored {
            // Listed but off, with the reason, as on the web.
            Button("Fork…") {}.disabled(true).help(SessionInfo.mirroredHint)
        }
    }
}

/// Rename or Fork, which need a sheet. Pin runs at once.
struct SessionHubSheet: Identifiable {
    enum Kind { case rename, fork }
    var kind: Kind
    /// The hub key, `harness:id`.
    var key: String
    var row: SessionsModel.Row
    var id: String { "\(kind)-\(key)" }
}

/// The iPhone's `RenameSheet`: a name kept on the hub, so every paired
/// device and the web app show it. The hub's rules apply as you type
/// (`SessionRename`); a renamed session also offers its harness's own
/// title back.
struct SessionRenameSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    var key: String
    /// The title the row showed when the sheet opened.
    var current: String
    @State private var text = ""
    @State private var saving = false
    @State private var error: String?

    private var meta: HubSessionMeta? { app.hub.meta(for: key) }

    /// The harness's own title, the name an empty field falls back to.
    private var placeholder: String {
        guard let harness = meta?.harnessTitle, !harness.isEmpty else { return "Name this session" }
        return harness
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Rename").font(Theme.sectionTitle).foregroundStyle(Theme.textPrimary)
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("Name").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    Spacer()
                    Text("\(SessionRename.length(text))/\(SessionRename.maxLength)")
                        .font(Theme.caption).monospacedDigit().foregroundStyle(Theme.textMuted)
                }
                TextField(placeholder, text: $text)
                    .sweField()
                    .onChange(of: text) { _, new in
                        let clamped = SessionRename.clamp(new)
                        if clamped != new { text = clamped }
                    }
                    .onSubmit(save)
                    .disabled(saving)
                Text("Every device paired with this hub shows this name.")
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
            }
            if let meta, meta.isRenamed {
                VStack(alignment: .leading, spacing: 6) {
                    Button("Use the original name") { submit("") }
                        .secondaryButton()
                        .disabled(saving)
                    if !meta.harnessTitle.isEmpty {
                        Text("Original: \(meta.harnessTitle)")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .lineLimit(2).truncationMode(.middle)
                    }
                }
            }
            if let error {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            HStack(spacing: 10) {
                Spacer()
                Button("Cancel") { dismiss() }
                    .secondaryButton()
                    .keyboardShortcut(.cancelAction)
                Button(saving ? "Saving…" : "Save", action: save)
                    .primaryButton()
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || !SessionRename.canSave(text, current: current))
            }
        }
        .sheetScaffold(width: 480)
        .onSheetDismissRequest { if !saving { dismiss() } }
        .onAppear { text = SessionRename.clamp(current) }
    }

    private func save() {
        guard SessionRename.canSave(text, current: current) else { return }
        submit(SessionRename.normalized(text))
    }

    private func submit(_ title: String) {
        saving = true
        error = nil
        Task {
            do {
                try await app.hub.rename(key, title: title)
                dismiss()
            } catch {
                self.error = (error as? HubError)?.message ?? error.localizedDescription
                saving = false
            }
        }
    }
}

/// The iPhone's `ForkSheet`: a new session that continues from this
/// one's history, with a first message, tracked like a new session; its
/// chat opens once the hub names it. Cancel stops the waiting, not the
/// fork.
struct SessionForkSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    var key: String
    var row: SessionsModel.Row
    var title: String
    @State private var prompt = ""
    @State private var flow = IssueLaunchFlow()
    /// The running launch, so Cancel stops it before it can open a chat
    /// behind a sheet that is gone.
    @State private var launchTask: Task<Void, Never>?

    private var ready: Bool { !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    /// The session's folder name for the footer.
    private var folder: String {
        guard let cwd = row.session.cwd, !cwd.isEmpty else { return "its folder" }
        let name = URL(fileURLWithPath: cwd).lastPathComponent
        return name.isEmpty || name == "/" ? cwd : name
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                HarnessIcon(harness: HarnessMark(row.session.harness).icon ?? row.session.harness, size: 18)
                Text("Fork \(title)")
                    .font(Theme.sectionTitle).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.tail)
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("What should the fork do?").font(Theme.caption).foregroundStyle(Theme.textMuted)
                TextField("Its first message", text: $prompt, axis: .vertical)
                    .lineLimit(4...10)
                    .sweField(multiline: true)
                    .disabled(flow.busy)
            }
            Text("The fork starts from this session's history and leaves the original as it is. It needs a first message: the harness writes nothing until it gets one.")
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            LaunchStatus(flow: flow)
            HStack(spacing: 10) {
                Text("Runs under tmux in \(folder). The chat opens once the fork shows up.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .lineLimit(2)
                Spacer(minLength: 12)
                LaunchFormButtons(flow: flow, ready: ready, startTip: "Start the fork",
                              cancel: { launchTask?.cancel(); dismiss() },
                              start: { launchTask = Task { await start() } })
            }
        }
        .sheetScaffold(width: 560)
        .onSheetDismissRequest { if !flow.busy { dismiss() } }
        .onDisappear { launchTask?.cancel() }
    }

    private func start() async {
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let hub = app.hub
        let source = key
        let forked = await flow.run(
            post: { try await hub.fork(source, prompt: text) },
            fetch: { id in try await hub.withClient { try await $0.launch(id) } })
        guard let forked, !Task.isCancelled else { return }
        dismiss()
        app.openSessionChat(key: forked)
    }
}
