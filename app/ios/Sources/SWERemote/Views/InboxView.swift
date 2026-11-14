import SwiftUI

/// P7: what needs you, what broke, unread GitHub threads and what
/// finished today. The hub applies its age cutoff and this device's
/// dismissals before rows ship (`GET /v1/inbox?done=1`), so the page
/// sorts what arrived and dismisses through `POST /v1/inbox/dismiss` —
/// a cleared row stays cleared on every device until the session or
/// thread moves again.
struct InboxView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.openURL) private var openURL
    @State private var swipes = SwipeCoordinator()

    var body: some View {
        let groups = InboxGrouping.groups(app.inboxRows)
        PageScroll {
            if !app.loadedOnce {
                LoadingRow(text: "Loading inbox")
            } else if groups.isEmpty {
                StateCard(icon: "checkmark.circle", title: "Nothing needs you",
                          message: "Approvals, questions and errors from your agents land here.")
            }
            ForEach(groups, id: \.0) { section, items in
                GroupLabel(text: section.title,
                           color: section == .waiting ? Theme.warningText
                               : section == .errors ? Theme.dangerText
                               : Theme.textSecondary)
                CardList(data: items) { item in row(item) }
            }
        }
        .modifier(CloseSwipesOnScroll(swipes: swipes))
        .refreshable { await app.refresh() }
        .navigationTitle("Inbox")
        .toolbar {
            // Each section clears its non-approval rows — approvals
            // can't be dismissed and must never be swept up in a batch.
            let clearable = groups.map { ($0.0, $0.1.filter { $0.sessionStatus != .approval }) }
                .filter { !$0.1.isEmpty }
            if !clearable.isEmpty {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        ForEach(clearable, id: \.0) { section, items in
                            Button("Clear \(section.clearTitle)") {
                                items.forEach { dismiss($0) }
                            }
                        }
                    } label: { Text("Clear").font(Theme.control) }
                }
            }
        }
    }

    /// A row dismisses on a left swipe, except a pending approval, which
    /// the hub keeps in the inbox no matter what this phone clears.
    @ViewBuilder private func row(_ item: InboxItem) -> some View {
        let clearable = item.sessionStatus != .approval
        SwipeRow(key: item.key, leading: [], trailing: clearable ? [.clear] : [],
                 coordinator: swipes, perform: { _ in dismiss(item) }) {
            if item.isGitHub {
                Button { if let url = item.url.flatMap({ URL(string: $0) }) { openURL(url) } } label: {
                    rowBody(item, detail: [item.repo, GitHubReason.label(item.reason)]
                            .compactMap { $0 }.joined(separator: " · "))
                }
                .buttonStyle(RowButtonStyle())
            } else {
                NavigationLink(value: Route.chat(item.key)) {
                    rowBody(item, detail: item.sessionStatus == .approval ? "Approval needed" : nil)
                }
                .buttonStyle(RowButtonStyle())
            }
        }
        .contextMenu {
            if clearable {
                Button("Clear") { dismiss(item) }
            }
        }
    }

    private func rowBody(_ item: InboxItem, detail: String?) -> some View {
        HStack(spacing: 12) {
            if item.isGitHub {
                Image(systemName: "bell.badge")
                    .font(Theme.font(size: 12)).foregroundStyle(Theme.textSecondary)
                    .iconColumn(Theme.statusDot + 4)
            } else {
                StatusDot(status: item.sessionStatus)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayTitle).font(Theme.rowTitle).foregroundStyle(Theme.textPrimary).lineLimit(1, larger: 3)
                WrappingHStack(spacing: 6) {
                    if !item.isGitHub {
                        HarnessIcon(harness: item.harness, name: item.harnessName, size: 13)
                    }
                    Text(detail ?? (item.isGitHub ? "GitHub" : item.sessionStatus.label))
                    let when = RelativeTime.short(item.lastActiveAt)
                    if !when.isEmpty { Text("· \(when)") }
                }
                .font(Theme.caption).foregroundStyle(Theme.textSecondary).lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if item.sessionStatus == .approval {
                Text("Review").font(Theme.control).foregroundStyle(Theme.textPrimary)
                    .padding(.horizontal, 10).scaledMinHeight(28)
                    .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.borderStrong, lineWidth: 1))
            } else {
                Image(systemName: "chevron.right").font(Theme.chevron).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.vertical, Theme.rowVertical)
        .padding(.horizontal, Theme.rowHorizontal)
        .contentShape(.rect)
    }

    private func dismiss(_ item: InboxItem) {
        Task { await app.dismissInbox(item) }
    }
}
