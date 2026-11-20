import AppKit
import SWECore
import SwiftUI

// MARK: - Skill detail

/// One skill as a page, the AGENTS.md chapter editor's shape: a rail on
/// the left (name, source and path, a Details fold with the description,
/// harness links and usage, the SKILL.md frontmatter and the folder's
/// files) beside the picked file in the editor, SKILL.md until another is
/// picked. The frontmatter fields edit the SKILL.md draft the editor
/// shows, so one Save writes both and the file has one document.
struct SkillDetailView: View {
    @Environment(AppModel.self) private var app
    var skill: Skill
    var home: URL
    var onBack: () -> Void
    /// The link chips a shared skill carries, from the Skills page.
    var links: AnyView?
    /// The call-count block from the session index, same source.
    var usage: AnyView?

    var body: some View {
        SkillDetailContent(skill: skill, home: home, navReselects: app.navReselects,
                           onBack: onBack,
                           onFilesChanged: { Task { await app.skills.load(for: app.swePath) } },
                           links: links, usage: usage)
    }
}

/// What a test watches on a skill detail it renders offscreen: the loads
/// that have landed and where the two panes sit.
@MainActor
final class SkillDetailProbe {
    /// "folders", "files", "skill" (SKILL.md's check) and "file:<path>"
    /// (the picked file's check), as each lands.
    var loaded: Set<String> = []
    var rail: CGRect = .zero
    var pane: CGRect = .zero
}

/// The skill detail itself, with what it needs from the app handed in, so
/// a test can render it against a folder of its own.
struct SkillDetailContent: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var skill: Skill
    var home: URL
    /// `AppModel.navReselects`: the back chevron, ⌘[ and reselecting the
    /// sidebar item bump it, and the detail answers with its leave prompt.
    var navReselects: Int
    var onBack: () -> Void
    /// A save or a trash changed the folder: the page reloads its list.
    var onFilesChanged: () -> Void
    var links: AnyView?
    var usage: AnyView?
    var probe: SkillDetailProbe?

    init(skill: Skill, home: URL, navReselects: Int, onBack: @escaping () -> Void,
         onFilesChanged: @escaping () -> Void, links: AnyView? = nil, usage: AnyView? = nil,
         drafts: PrimitiveDrafts = PrimitiveDrafts(), selected: String? = nil,
         detailsOpen: Bool = false, expanded: Set<String> = [], probe: SkillDetailProbe? = nil) {
        self.skill = skill
        self.home = home
        self.navReselects = navReselects
        self.onBack = onBack
        self.onFilesChanged = onFilesChanged
        self.links = links
        self.usage = usage
        self.probe = probe
        _drafts = State(initialValue: drafts)
        _selected = State(initialValue: selected)
        _detailsOpen = State(initialValue: detailsOpen)
        _expanded = State(initialValue: expanded)
    }

    /// Every opened file's document, for the page's stay: switching files
    /// keeps unsaved edits, and the leave prompt saves them all.
    @State private var drafts: PrimitiveDrafts
    @State private var leaving = false
    @State private var leaveError: String?
    /// The skill's folders, worked out off the main actor, with the path
    /// they were worked out for; nil until the first lands.
    @State private var found: (path: String, folders: SkillFolders)?
    /// Bumped by `reload()` so the folders, the file list and the file
    /// checks are worked out again after a change on disk that leaves the
    /// skill's path as it was.
    @State private var refresh = 0
    /// The file in the editor; SKILL.md until another is picked.
    @State private var selected: String?
    /// The picked file's size, type and inside-the-folder check, run off
    /// the main actor each time a file is picked (a listed file may have
    /// become a link that leaves the folder since); the editor waits for
    /// the check of the file it shows.
    @State private var opened: (path: String, check: PrimitiveFileCheck)?
    /// The same check for SKILL.md, which the frontmatter fields edit
    /// whatever file the editor shows.
    @State private var skillCheck: (path: String, check: PrimitiveFileCheck)?
    /// The folder's files and the ones a trash button may remove, walked
    /// off the main actor; nil until the first walk lands.
    @State private var files: (path: String, outline: SkillFileOutline, trashable: Set<String>)?
    @State private var expanded: Set<String>
    @State private var detailsOpen: Bool
    @State private var trashing: SkillFileOutline.Node?
    @State private var filesError: String?
    /// The rail and editor's room together, for the rail's width and
    /// whether the two stack.
    @State private var size: CGSize = .zero
    /// The file list's own height, for its capped scroll view when the
    /// panes stack.
    @State private var fileListHeight: CGFloat = 0
    /// The tallest the file list grows when the panes stack before it
    /// scrolls on its own.
    private static let stackedFileListHeight: CGFloat = 220

    private var root: URL { URL(fileURLWithPath: skill.path) }
    /// The folders for the skill on screen; nil while they are worked
    /// out, so a result for a skill opened before never shows.
    private var folders: SkillFolders? {
        guard let found, found.path == skill.path else { return nil }
        return found.folders
    }
    private var editable: Bool { folders?.editable ?? false }
    private var skillFile: String? { folders?.skillFile?.path }
    private var outline: SkillFileOutline? {
        guard let files, files.path == skill.path else { return nil }
        return files.outline
    }
    private var animation: Animation? { reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let leaveError {
                ErrorCard(message: leaveError)
            }
            if folders != nil {
                let zoom = TextZoomModel.shared.zoom
                if SkillDetailLayout.stacks(available: Double(size.width), zoom: zoom) {
                    // Too narrow for two panes (a 900pt window with a wide
                    // sidebar): the rail over the editor, both full width,
                    // scrolling together.
                    ScrollView {
                        VStack(alignment: .leading, spacing: CGFloat(SkillDetailLayout.gap)) {
                            Strip(padded: false) { railSections(stacked: true).padding(railPadding) }
                                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { probe?.rail = $0 }
                            filePane
                                .frame(height: max(CGFloat(SkillDetailLayout.stackedEditorHeight * zoom),
                                                   size.height * 0.8))
                                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { probe?.pane = $0 }
                        }
                    }
                    .scrollIndicators(.automatic)
                    .frame(maxHeight: .infinity)
                } else {
                    HStack(alignment: .top, spacing: CGFloat(SkillDetailLayout.gap)) {
                        rail
                            .frame(width: CGFloat(SkillDetailLayout.railWidth(available: Double(size.width), zoom: zoom)))
                            .frame(maxHeight: .infinity)
                            .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { probe?.rail = $0 }
                        filePane
                            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                            .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { probe?.pane = $0 }
                    }
                    .frame(maxHeight: .infinity)
                }
            } else {
                SkeletonRows(rows: 5, shape: .file, label: "Reading the skill's folder…")
                    .frame(maxHeight: .infinity, alignment: .top)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .onGeometryChange(for: CGSize.self) { $0.size } action: { size = $0 }
        // `realpath` and `fileExists` on a synced or stale folder can
        // block, so the folders are worked out on a GCD queue (#580). A
        // new skill or a reload cancels this task, and a cancelled task's
        // result is dropped. `refresh` is in the key because a reload
        // (trashing a file, say) can leave the path unchanged.
        .task(id: [skill.path, home.path, String(refresh)]) {
            let path = skill.path, home = home
            let worked = await offMain { SkillFolders(skillPath: path, home: home) }
            guard !Task.isCancelled else { return }
            found = (path, worked)
            if selected == nil { selected = worked.skillFile?.path }
            probe?.loaded.insert("folders")
        }
        // The tree walk is disk work too, and which paths stay inside the
        // folder (the trash buttons) is worked out with it.
        .task(id: [skill.path, String(refresh)]) {
            let path = skill.path, root = root
            let walked = await offMain {
                let tree = PrimitiveTree(root: root)
                return (SkillFileOutline(tree: tree, root: root), tree.trashablePaths(root))
            }
            guard !Task.isCancelled else { return }
            files = (path, walked.0, walked.1)
            // A picked file that left the folder falls back to SKILL.md.
            if let selected, selected != skillFile, !walked.0.filePaths.contains(selected) {
                self.selected = skillFile
            }
            probe?.loaded.insert("files")
        }
        .task(id: [selected ?? "", String(refresh)]) {
            guard let path = selected else { return }
            let root = root
            let check = await offMain { PrimitiveFileCheck(path: path, root: root) }
            guard !Task.isCancelled else { return }
            opened = (path, check)
            // Never in the body: document(for:) writes the drafts store,
            // which a body must not do mid-update.
            if check.canPreview { drafts.document(for: path) }
            probe?.loaded.insert("file:" + path)
        }
        .task(id: [skillFile ?? "", String(refresh)]) {
            guard let path = skillFile else { return }
            let root = root
            let check = await offMain { PrimitiveFileCheck(path: path, root: root) }
            guard !Task.isCancelled else { return }
            skillCheck = (path, check)
            if check.canPreview { drafts.document(for: path) }
            probe?.loaded.insert("skill")
        }
        // The page header's back chevron (and its ⌘[) and reselecting the
        // Skills sidebar item both come through here.
        .onChange(of: navReselects) { leave() }
        .preference(key: DetailOpenKey.self, value: true)
        .alert("Unsaved changes", isPresented: $leaving) {
            Button("Save all") { saveAllAndLeave() }
            Button("Discard", role: .destructive) { onBack() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("\(drafts.dirty.count) file\(drafts.dirty.count == 1 ? " has" : "s have") unsaved changes.")
        }
        .alert("Move “\(trashing?.name ?? "")” to the Trash?", isPresented: .init(
            get: { trashing != nil }, set: { if !$0 { trashing = nil } })) {
            Button("Move to Trash", role: .destructive) { trash() }
            Button("Cancel", role: .cancel) { trashing = nil }
        } message: {
            Text("You can restore it from the Trash.")
        }
    }

    // MARK: Rail

    /// The rail beside the editor: its sections scroll on their own.
    private var rail: some View {
        Strip(padded: false) {
            ScrollView {
                railSections(stacked: false).padding(railPadding)
            }
            .clipShape(.rect(cornerRadius: Theme.blockRadius))
            .frame(maxHeight: .infinity)
        }
        .frame(maxHeight: .infinity)
    }

    private var railPadding: EdgeInsets {
        EdgeInsets(top: 14, leading: 16, bottom: 14, trailing: 16)
    }

    /// Header, Details, frontmatter and files. Stacked over the editor,
    /// the file list scrolls within a capped height so the editor stays
    /// near the top.
    private func railSections(stacked: Bool) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            details
            divider
            frontmatter
            divider
            fileList(stacked: stacked)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var divider: some View {
        Rectangle().fill(Theme.rowDivider).frame(height: Theme.hairline)
    }

    private func sectionTitle(_ title: String) -> some View {
        Text(title).font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
            .accessibilityAddTraits(.isHeader)
    }

    /// Name and source on one line when they fit, the path under them, and
    /// Show in Finder.
    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) {
                        name.fixedSize()
                        badges
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        name.fixedSize(horizontal: false, vertical: true)
                        badges
                    }
                }
                Spacer(minLength: 4)
                Button { NSWorkspace.shared.activateFileViewerSelecting([root]) } label: {
                    Image(systemName: "folder")
                        .font(Theme.font(size: 13))
                        .foregroundStyle(Theme.textSecondary)
                        .frame(width: 24, height: 24)
                        .contentShape(.rect)
                }
                .plainButton(cornerRadius: Theme.smallRadius)
                .help("Show in Finder")
                .accessibilityLabel("Show \(skill.name) in Finder")
            }
            Text(displayPath(skill.path))
                .font(Theme.font(size: 12, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var name: some View {
        Text(skill.name).font(Theme.font(size: 16, weight: .semibold)).foregroundStyle(Theme.textPrimary)
    }

    private var badges: some View {
        FlowLayout(spacing: 6, lineSpacing: 6) {
            Badge(skill.source.title)
            // A skill adoption carried into the shared library names its
            // donor (#1882).
            if let donor = skill.adoptedFrom {
                Badge("Added from \(HarnessMark(donor).name)")
            }
        }
    }

    /// The description, harness links and usage, folded away until asked.
    private var details: some View {
        VStack(alignment: .leading, spacing: 10) {
            RailDisclosure(title: "Details", isOpen: detailsOpen,
                           help: (open: "Show the description, harness links and usage",
                                  close: "Hide the description, harness links and usage")) {
                withAnimation(animation) { detailsOpen.toggle() }
            }
            .padding(.horizontal, -8)
            if detailsOpen {
                VStack(alignment: .leading, spacing: 10) {
                    if !skill.description.isEmpty {
                        Text(skill.description).font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let links { links }
                    if let usage { usage }
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }

    // MARK: Frontmatter

    /// SKILL.md's frontmatter as fields over the editor's own draft.
    @ViewBuilder private var frontmatter: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("Frontmatter")
            if let skillFile {
                let check = skillCheck?.path == skillFile ? skillCheck?.check : nil
                let document = drafts.documents[skillFile]
                switch (check?.canPreview, document?.state) {
                case (.some(false), _):
                    note("SKILL.md can't be read here: it is too large or not text.")
                case (.some(true), .some(.failed(let message))):
                    Label(message, systemImage: "exclamationmark.triangle")
                        .font(Theme.caption).foregroundStyle(Theme.dangerText)
                        .fixedSize(horizontal: false, vertical: true)
                case (.some(true), .some(.ready)):
                    if let document {
                        fields(document, editable: folders?.skillFileEditable == true && check?.isInside == true,
                               skillFile: skillFile)
                    }
                default:
                    SkeletonLines(rows: 3, framed: false, label: "Reading SKILL.md…")
                }
            } else {
                note("This skill has no SKILL.md.")
            }
        }
    }

    @ViewBuilder
    private func fields(_ document: InstructionDocument, editable: Bool, skillFile: String) -> some View {
        let rows = SkillFrontmatterForm.rows(in: document.text, name: skill.name)
        ForEach(rows) { row in
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 4) {
                    Text(row.key).font(Theme.control).foregroundStyle(Theme.textSecondary)
                    if row.locked {
                        Image(systemName: "lock.fill")
                            .font(Theme.font(size: 9))
                            .foregroundStyle(Theme.textMuted)
                            .accessibilityHidden(true)
                    }
                }
                if row.locked {
                    Text(row.value).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                        .help("The name is the skill's id, so it can't be changed here.")
                } else {
                    TextField(row.key, text: Self.field(row.key, of: document, name: skill.name) {
                        // The visible Save is the one that writes the
                        // field: bring SKILL.md into the editor.
                        if selected != skillFile { withAnimation(animation) { selected = skillFile } }
                    }, axis: .vertical)
                    .lineLimit(row.multiline ? 3...6 : 1...4)
                    .sweField(multiline: true)
                    .disabled(!editable)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityHint(row.locked ? "Read-only: the name is the skill's id." : "")
        }
        let locked = SkillFrontmatterForm.lockedKeys(in: document.text)
        if !locked.isEmpty {
            note("\(locked.joined(separator: ", ")) \(locked.count == 1 ? "is" : "are") edited in SKILL.md itself.")
        }
    }

    /// One frontmatter field bound to the SKILL.md draft's text: it reads
    /// the value out of `document.text` and writes through
    /// `MarkdownFrontmatter.update`, so the form never holds a copy of
    /// the file that could fight the editor's. `onEdit` runs after a write
    /// that changed the text.
    static func field(_ key: String, of document: InstructionDocument, name: String,
                      onEdit: @escaping () -> Void) -> Binding<String> {
        Binding(get: {
            SkillFrontmatterForm.rows(in: document.text, name: name).first { $0.key == key }?.value ?? ""
        }, set: { value in
            let text = SkillFrontmatterForm.setting(key, to: value, in: document.text)
            guard text != document.text else { return }
            document.text = text
            onEdit()
        })
    }

    private func note(_ text: String) -> some View {
        Text(text).font(Theme.caption).foregroundStyle(Theme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: Files

    private func fileList(stacked: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Files")
            if let outline {
                let rows = VStack(alignment: .leading, spacing: 1) {
                    ForEach(visibleRows(outline), id: \.node.id) { row in
                        fileRow(row.node, depth: row.depth)
                    }
                }
                if stacked {
                    ScrollView {
                        rows.padding(.horizontal, 8)
                            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { fileListHeight = $0 }
                    }
                    .frame(height: min(fileListHeight, Self.stackedFileListHeight))
                    .padding(.horizontal, -8)
                } else {
                    rows.padding(.horizontal, -8)
                }
            } else {
                SkeletonRows(rows: 4, framed: false, shape: .file, label: "Reading the skill's files…")
            }
            if let filesError {
                Label(filesError, systemImage: "exclamationmark.triangle")
                    .font(Theme.caption).foregroundStyle(Theme.dangerText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// The outline flattened to what shows: every node whose folders are
    /// all open, with its depth.
    private func visibleRows(_ outline: SkillFileOutline) -> [(node: SkillFileOutline.Node, depth: Int)] {
        func walk(_ nodes: [SkillFileOutline.Node], _ depth: Int) -> [(node: SkillFileOutline.Node, depth: Int)] {
            nodes.flatMap { node in
                [(node, depth)] + (node.isFolder && expanded.contains(node.path) ? walk(node.children, depth + 1) : [])
            }
        }
        return walk(outline.nodes, 0)
    }

    private func fileRow(_ node: SkillFileOutline.Node, depth: Int) -> some View {
        let trashable = editable && (files?.trashable.contains(node.path) ?? false)
        return SkillFileRow(node: node, depth: depth,
                            isSelected: !node.isFolder && selected == node.path,
                            isExpanded: expanded.contains(node.path),
                            isDirty: drafts.documents[node.path]?.isDirty == true,
                            onTrash: trashable ? { trashing = node } : nil) {
            withAnimation(animation) {
                if node.isFolder {
                    if expanded.contains(node.path) { expanded.remove(node.path) } else { expanded.insert(node.path) }
                } else {
                    selected = node.path
                }
            }
        }
    }

    private func trash() {
        guard let node = trashing else { return }
        trashing = nil
        let root = root
        Task {
            switch await offMain({ Result { try PrimitiveParts.trash(URL(fileURLWithPath: node.path), inside: root) } }) {
            case .success:
                drafts.remove(under: node.path)
                if let selected, selected == node.path || selected.hasPrefix(node.path + "/") {
                    self.selected = skillFile
                }
                filesError = nil
                reload()
            case .failure(let failure):
                filesError = "Couldn't move \(node.name) to the Trash: \(failure.localizedDescription)"
            }
        }
    }

    // MARK: File pane

    /// The picked file in the editor, once its check lands: the lock note
    /// when it may not be edited, "Can't preview" for a binary or a file
    /// over a megabyte, skeletons while it reads.
    @ViewBuilder private var filePane: some View {
        if let path = selected {
            let url = URL(fileURLWithPath: path)
            let check = opened?.path == path ? opened?.check : nil
            let fileEditable = editable && check?.isInside == true
            VStack(alignment: .leading, spacing: 12) {
                if check?.canPreview == true, !fileEditable {
                    PrimitiveLockNote(text: editable
                        ? "Only files inside the skill's own folder can be edited, so this one is read-only."
                        : "Only skills that live under ~/.swe can be edited, so \(skill.name) is read-only.")
                }
                switch check?.canPreview {
                case .some(false):
                    Card(title: url.lastPathComponent, subtitle: fileSubtitle(url), monoSubtitle: true) {
                        Text("Can't preview this file.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                case .some(true):
                    if let document = drafts.documents[path] {
                        InstructionEditor(document: document, title: url.lastPathComponent,
                                          subtitle: fileSubtitle(url), monoSubtitle: true,
                                          readOnly: !fileEditable,
                                          rawOnly: !Self.isMarkdown(url), onSaved: reload)
                    } else {
                        loadingCard(url)
                    }
                case .none:
                    loadingCard(url)
                }
            }
            .id(path)
            .transition(.opacity)
        } else {
            Card {
                Text(folders?.skillFile == nil && outline?.filePaths.isEmpty == true
                     ? "This skill's folder has no files."
                     : "Pick a file in the list to open it here.")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
            }
        }
    }

    /// The file under the skill's folder (`code-review/scripts/run.sh`):
    /// the rail already shows where the folder is, and a full path would
    /// wrap into a column beside the header in a narrow window.
    private func fileSubtitle(_ url: URL) -> String {
        let base = skill.path.hasSuffix("/") ? skill.path : skill.path + "/"
        guard url.path.hasPrefix(base) else { return displayPath(url) }
        return root.lastPathComponent + "/" + url.path.dropFirst(base.count)
    }

    /// Prose gets Raw and Rich; a script or data file only the raw editor,
    /// since Rich would read it as Markdown.
    static func isMarkdown(_ url: URL) -> Bool {
        ["md", "markdown", "mdx", "txt"].contains(url.pathExtension.lowercased())
    }

    private func loadingCard(_ url: URL) -> some View {
        Card(title: url.lastPathComponent, subtitle: fileSubtitle(url), monoSubtitle: true) {
            SkeletonLines(rows: 12, framed: false)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
    }

    // MARK: Leaving

    private func leave() {
        if drafts.dirty.isEmpty { onBack() } else { leaving = true }
    }

    private func saveAllAndLeave() {
        Task {
            var failed: [String] = []
            for (path, document) in drafts.dirty {
                // A save that leaves the file dirty (typed while it ran)
                // hasn't kept everything either.
                if await document.save() != .saved || document.isDirty {
                    failed.append(URL(fileURLWithPath: path).lastPathComponent)
                }
            }
            if failed.isEmpty {
                leaveError = nil
                onBack()
            } else {
                leaveError = "Couldn't save \(failed.joined(separator: ", "))."
            }
        }
    }

    private func reload() {
        refresh += 1
        onFilesChanged()
    }
}

// MARK: - Rail rows

/// A fold row inside a rail: a chevron that turns, the title, a hover fill.
private struct RailDisclosure: View {
    var title: String
    var isOpen: Bool
    var help: (open: String, close: String)
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(Theme.font(size: 10, weight: .semibold))
                    .rotationEffect(.degrees(isOpen ? 90 : 0))
                    .frame(width: 12)
                Text(title).font(Theme.control)
                Spacer(minLength: 0)
            }
            .foregroundStyle(hovering ? Theme.textPrimary : Theme.textSecondary)
            .padding(.vertical, 5)
            .padding(.horizontal, 8)
            .background(hovering ? Theme.panelHover : .clear, in: .rect(cornerRadius: Theme.controlRadius))
            .contentShape(.rect)
        }
        .plainButton(cornerRadius: Theme.controlRadius)
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: Theme.fadeDuration), value: hovering)
        .help(isOpen ? help.close : help.open)
        .accessibilityLabel(title)
        .accessibilityValue(isOpen ? "Expanded" : "Collapsed")
    }
}

/// One row of the skill's file list: a folder that opens in place, or a
/// file that opens in the editor, with the selected fill, a hover fill,
/// an unsaved dot and, for a row the user may remove, a trash button
/// that shows under the pointer or on the selected row.
private struct SkillFileRow: View {
    var node: SkillFileOutline.Node
    var depth: Int
    var isSelected: Bool
    var isExpanded: Bool
    var isDirty: Bool
    var onTrash: (() -> Void)?
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 4) {
            Button(action: action) {
                HStack(spacing: 6) {
                    if node.isFolder {
                        Image(systemName: "chevron.right")
                            .font(Theme.font(size: 9, weight: .semibold))
                            .foregroundStyle(Theme.textMuted)
                            .rotationEffect(.degrees(isExpanded ? 90 : 0))
                            .frame(width: 10)
                    } else {
                        Color.clear.frame(width: 10, height: 1)
                    }
                    Image(systemName: node.isFolder ? "folder.fill" : "doc.text")
                        .font(Theme.font(size: 12))
                        .foregroundStyle(node.isFolder ? Theme.accent : Theme.textSecondary)
                        .frame(width: 16)
                    Text(node.name)
                        .font(Theme.bodySmall)
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if isDirty {
                        Circle().fill(Theme.warning)
                            .frame(width: Theme.statusDot, height: Theme.statusDot)
                            .help("Unsaved changes")
                    }
                    Spacer(minLength: 0)
                }
                .padding(.leading, CGFloat(depth) * 14)
                .padding(.vertical, 4)
                .padding(.leading, 8)
                .contentShape(.rect)
            }
            .plainButton(cornerRadius: Theme.controlRadius)
            .accessibilityLabel(node.name)
            .accessibilityValue(value)
            .accessibilityAddTraits(isSelected ? .isSelected : [])
            .accessibilityActions {
                if let onTrash { Button("Move to Trash", action: onTrash) }
            }
            if let onTrash {
                let shown = hovering || isSelected
                Button(action: onTrash) {
                    Image(systemName: "trash")
                        .font(Theme.font(size: 11))
                        .foregroundStyle(Theme.textSecondary)
                        .frame(width: 22, height: 22)
                        .contentShape(.rect)
                }
                .plainButton(cornerRadius: Theme.smallRadius)
                .help("Move \(node.name) to the Trash")
                .accessibilityLabel("Move \(node.name) to the Trash")
                .opacity(shown ? 1 : 0)
                .allowsHitTesting(shown)
                .accessibilityHidden(true)
            }
        }
        .padding(.trailing, 4)
        .background(isSelected ? Theme.selected : hovering ? Theme.panelHover : .clear,
                    in: .rect(cornerRadius: Theme.controlRadius))
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: Theme.fadeDuration), value: hovering)
        .animation(.easeOut(duration: Theme.fadeDuration), value: isSelected)
        .help(displayPath(node.path))
    }

    private var value: String {
        var parts: [String] = []
        if node.isFolder { parts.append(isExpanded ? "Expanded" : "Collapsed") }
        if isDirty { parts.append("Unsaved changes") }
        return parts.joined(separator: ", ")
    }
}
