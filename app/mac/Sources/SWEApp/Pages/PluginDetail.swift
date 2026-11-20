import AppKit
import SWECore
import SwiftUI

// MARK: - Plugin detail

/// One plugin as a page, the skill detail's shape: a rail on the left
/// (display name, ref and source, a Details fold with the description,
/// harness state and MCP servers, the manifest's fields and the folder's
/// files grouped by kind) beside the picked file in the editor. The
/// manifest fields edit the `plugin.json` draft the editor shows, so one
/// Save writes both and the file has one document.
struct PluginDetailView: View {
    @Environment(AppModel.self) private var app
    var entry: ExplorePlugin
    var market: PluginMarketplace
    var home: URL
    var onBack: () -> Void

    var body: some View {
        let plugin = entry.row.plugin
        let ref = plugin.ref
        let file = PluginDisplayNames.file(home: home)
        PluginDetailContent(
            plugin: plugin, harnesses: entry.harnesses, market: market, home: home,
            navReselects: app.navReselects,
            overlayName: app.primitiveNames.name(forPlugin: ref),
            marketName: app.primitiveNames.name(forMarketplace: market.name) ?? market.name,
            onBack: onBack,
            onFilesChanged: {
                Task {
                    await app.pluginCatalog.load(swe: app.swe, for: app.swePath)
                    await app.skills.load(for: app.swePath)
                }
            },
            saveDisplayName: { name in
                try await app.saveDisplayNames({ $0.set(name, forPlugin: ref) }, to: file)
            },
            // A swe from before `plugins info` fails the call: the Details
            // fold then shows only what the catalog row says.
            loadInfo: { try? await app.swe.pluginInfo($0) },
            saveSecret: { _ = try await app.swe.mcpSecretSet(name: $0, value: $1) })
    }
}

/// What a test watches on a plugin detail it renders offscreen: the loads
/// that have landed and where the two panes sit. Observable, so a test
/// parks on a gate until they change instead of polling.
@MainActor
@Observable
final class PluginDetailProbe {
    /// "folders", "files", "manifest" (plugin.json's check), "info" and
    /// "file:<path>" (the picked file's check), as each lands.
    var loaded: Set<String> = []
    var rail: CGRect = .zero
    var pane: CGRect = .zero
}

/// The plugin detail itself, with what it needs from the app handed in,
/// so a test can render it against a folder of its own.
struct PluginDetailContent: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var plugin: CatalogPlugin
    /// The harnesses whose state the Details fold shows, in the catalog's order.
    var harnesses: [String]
    var market: PluginMarketplace
    var home: URL
    /// `AppModel.navReselects`: the back chevron, ⌘[ and reselecting the
    /// sidebar item bump it, and the detail answers with its leave prompt.
    var navReselects: Int
    /// SWE's own name for the plugin, when it has one.
    var overlayName: String?
    var marketName: String
    var onBack: () -> Void
    /// A save, a new part or a trash changed the folder: the page reloads
    /// its catalog.
    var onFilesChanged: () -> Void
    /// Stores the display name in SWE's overlay; nil clears it.
    var saveDisplayName: @MainActor (String?) async throws -> Void
    /// `swe plugins info <ref>`; nil when the call fails.
    var loadInfo: @MainActor (String) async -> PluginInfo?
    /// `swe mcp secret set`: a key's name, then its value.
    var saveSecret: @MainActor (String, String) async throws -> Void
    var probe: PluginDetailProbe?

    init(plugin: CatalogPlugin, harnesses: [String], market: PluginMarketplace, home: URL,
         navReselects: Int, overlayName: String? = nil, marketName: String,
         onBack: @escaping () -> Void, onFilesChanged: @escaping () -> Void,
         saveDisplayName: @escaping @MainActor (String?) async throws -> Void = { _ in },
         loadInfo: @escaping @MainActor (String) async -> PluginInfo? = { _ in nil },
         saveSecret: @escaping @MainActor (String, String) async throws -> Void = { _, _ in },
         drafts: PrimitiveDrafts = PrimitiveDrafts(), selected: String? = nil,
         detailsOpen: Bool = false, manifestShowsAll: Bool = false,
         expanded: Set<String> = [], probe: PluginDetailProbe? = nil) {
        self.plugin = plugin
        self.harnesses = harnesses
        self.market = market
        self.home = home
        self.navReselects = navReselects
        self.overlayName = overlayName
        self.marketName = marketName
        self.onBack = onBack
        self.onFilesChanged = onFilesChanged
        self.saveDisplayName = saveDisplayName
        self.loadInfo = loadInfo
        self.saveSecret = saveSecret
        self.probe = probe
        _drafts = State(initialValue: drafts)
        _selected = State(initialValue: selected)
        _detailsOpen = State(initialValue: detailsOpen)
        _manifestShowsAll = State(initialValue: manifestShowsAll)
        _expanded = State(initialValue: expanded)
        _name = State(initialValue: overlayName ?? plugin.displayName ?? plugin.name)
    }

    /// The display name as typed; Return saves it.
    @State private var name: String
    @State private var nameError: String?
    /// The last Codex manifest write; the next waits for it, so two quick
    /// renames land in the order they were made.
    @State private var manifestWrite: Task<Void, Never>?
    /// Every opened file's document, for the page's stay: switching files
    /// keeps unsaved edits, and the leave prompt saves them all.
    @State private var drafts: PrimitiveDrafts
    @State private var leaving = false
    @State private var leaveError: String?
    /// The plugin's folders, worked out off the main actor, with the ref
    /// they were worked out for; nil until the first lands.
    @State private var found: (ref: String, folders: PluginFolders)?
    /// Bumped by `reload()` so the folders, the file list, the file checks
    /// and `swe plugins info` are worked out again after a change on disk
    /// that leaves the catalog's entry as it was.
    @State private var refresh = 0
    /// The file in the editor: the README, the first skill or plugin.json
    /// until another is picked (`PluginFileOutline.defaultFile`).
    @State private var selected: String?
    /// The picked file's size, type and inside-the-folder check, run off
    /// the main actor each time a file is picked (a listed file may have
    /// become a link that leaves the folder since); the editor waits for
    /// the check of the file it shows.
    @State private var opened: (path: String, check: PrimitiveFileCheck)?
    /// The same check for plugin.json, which the manifest fields edit
    /// whatever file the editor shows.
    @State private var manifestCheck: (path: String, check: PrimitiveFileCheck)?
    /// The folder's files and the ones a trash button may remove, walked
    /// off the main actor; nil until the first walk lands.
    @State private var files: (path: String, outline: PluginFileOutline, trashable: Set<String>)?
    @State private var expanded: Set<String>
    @State private var detailsOpen: Bool
    /// Whether every manifest field shows, or only the first few.
    @State private var manifestShowsAll: Bool
    /// The Keywords field as typed (`PluginManifestForm.keywords`).
    @State private var keywordsTyped: String?
    /// `swe plugins info`, for the Details fold; nil until it lands, and
    /// for good when the call fails.
    @State private var info: PluginInfo?
    @State private var trashing: SkillFileOutline.Node?
    @State private var filesError: String?
    @State private var newPart: PrimitiveParts.Kind?
    @State private var newName = ""
    @State private var newError: String?
    /// The create in flight; a New opened since makes its answer stale.
    @State private var creating: UUID?
    /// The rail and editor's room together, for the rail's width and
    /// whether the two stack.
    @State private var size: CGSize = .zero
    /// The file list's own height, for its capped scroll view when the
    /// panes stack.
    @State private var fileListHeight: CGFloat = 0
    /// The tallest the file list grows when the panes stack before it
    /// scrolls on its own.
    private static let stackedFileListHeight: CGFloat = 220
    /// MCP servers in the Details fold before a Show more row.
    fileprivate static let serverFoldLimit = 8

    /// What `.task(id:)` recomputes the folders for: a catalog reload can
    /// move the plugin's folder or its marketplace's registration.
    private struct FolderInputs: Equatable {
        var plugin: CatalogPlugin
        var market: PluginMarketplace
        var home: URL
        var refresh: Int
    }

    /// The folders for the plugin on screen; nil while they are worked
    /// out, so a result for a plugin opened before never shows.
    private var folders: PluginFolders? {
        guard let found, found.ref == plugin.ref else { return nil }
        return found.folders
    }
    private var dir: URL? { folders?.dir }
    private var editable: Bool { folders?.editable ?? false }
    private var manifest: String? { folders?.manifest?.path }
    private var outline: PluginFileOutline? {
        guard let files, files.path == dir?.path else { return nil }
        return files.outline
    }
    private var shownName: String { overlayName ?? plugin.displayName ?? plugin.name }
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
                SkeletonRows(rows: 5, shape: .file, label: "Reading the plugin's folder…")
                    .frame(maxHeight: .infinity, alignment: .top)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .onGeometryChange(for: CGSize.self) { $0.size } action: { size = $0 }
        // `realpath` and `fileExists` on a synced or stale folder can
        // block, so the folders are worked out on a GCD queue (#580). A
        // new plugin or catalog reload cancels this task, and a cancelled
        // task's result is dropped.
        .task(id: FolderInputs(plugin: plugin, market: market, home: home, refresh: refresh)) {
            let plugin = plugin, market = market, home = home
            let worked = await offMain { PluginFolders(plugin: plugin, market: market, home: home) }
            guard !Task.isCancelled else { return }
            found = (plugin.ref, worked)
            probe?.loaded.insert("folders")
        }
        // The tree walk is disk work too, and which paths stay inside the
        // folder (the trash buttons) is worked out with it. Keyed on the
        // folder: a catalog reload can move it.
        .task(id: [dir?.path ?? "", manifest ?? "", String(refresh)]) {
            guard let dir else { return }
            let manifest = manifest
            let walked = await offMain {
                let tree = PrimitiveTree(root: dir)
                let manifests = PrimitiveTree.manifestEntries(root: dir)
                // A manifest that links to a file the tree lists keeps its
                // row; the other goes, so the file opens as one document.
                let outline = PluginFileOutline(tree: tree.dropping(filesSharedWith: manifests),
                                                root: dir, manifests: manifests)
                return (outline, tree.trashablePaths(dir))
            }
            guard !Task.isCancelled else { return }
            files = (dir.path, walked.0, walked.1)
            // Nothing picked yet, or the picked file left the folder: the
            // README, the first skill or plugin.json.
            if selected.map({ !walked.0.filePaths.contains($0) }) ?? true {
                select(walked.0.defaultFile(manifest: manifest), in: walked.0)
            }
            probe?.loaded.insert("files")
        }
        .task(id: [selected ?? "", dir?.path ?? "", String(refresh)]) {
            guard let path = selected, let dir else { return }
            let check = await offMain { PrimitiveFileCheck(path: path, root: dir) }
            guard !Task.isCancelled else { return }
            opened = (path, check)
            // Never in the body: document(for:) writes the drafts store,
            // which a body must not do mid-update.
            if check.canPreview { drafts.document(for: path) }
            probe?.loaded.insert("file:" + path)
        }
        .task(id: [manifest ?? "", dir?.path ?? "", String(refresh)]) {
            guard let path = manifest, let dir else { return }
            let check = await offMain { PrimitiveFileCheck(path: path, root: dir) }
            guard !Task.isCancelled else { return }
            manifestCheck = (path, check)
            if check.canPreview { drafts.document(for: path) }
            probe?.loaded.insert("manifest")
        }
        // A refresh keeps the ref while the plugin's files and index
        // counts move on, so it keys the fetch.
        .task(id: [plugin.ref, String(refresh)]) {
            let fetched = await loadInfo(plugin.ref)
            guard !Task.isCancelled else { return }
            info = fetched
            probe?.loaded.insert("info")
        }
        // The page header's back chevron (and its ⌘[) and reselecting the
        // Plugins sidebar item both come through here.
        .onChange(of: navReselects) { leave() }
        .alert("Unsaved changes", isPresented: $leaving) {
            Button("Save all") { saveAllAndLeave() }
            Button("Discard", role: .destructive) { onBack() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("\(drafts.dirty.count) file\(drafts.dirty.count == 1 ? " has" : "s have") unsaved changes.")
        }
        .alert("New \(newPart?.title ?? "")", isPresented: .init(
            get: { newPart != nil }, set: { if !$0 { newPart = nil } })) {
            TextField("name", text: $newName)
            Button("Create") { create() }
            Button("Cancel", role: .cancel) { newPart = nil; newName = ""; newError = nil }
        } message: {
            Text(newError ?? "A new \(newPart?.title ?? "part") under \(dir.map { displayPath($0) } ?? "the plugin's folder").")
        }
        .alert("Move “\(trashing?.name ?? "")” to the Trash?", isPresented: .init(
            get: { trashing != nil }, set: { if !$0 { trashing = nil } })) {
            Button("Move to Trash", role: .destructive) { trash() }
            Button("Cancel", role: .cancel) { trashing = nil }
        } message: {
            Text("You can restore it from the Trash.")
        }
    }

    /// Put `path` in the editor and open the folders its row sits in.
    private func select(_ path: String?, in outline: PluginFileOutline) {
        selected = path
        if let path { expanded.formUnion(outline.folders(containing: path)) }
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

    /// Header, Details, manifest and files. Stacked over the editor, the
    /// file list scrolls within a capped height so the editor stays near
    /// the top.
    private func railSections(stacked: Bool) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            details
            if manifest != nil {
                divider
                manifestSection
            }
            if dir != nil {
                divider
                fileList(stacked: stacked)
            }
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

    private func note(_ text: String) -> some View {
        Text(text).font(Theme.caption).foregroundStyle(Theme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func problem(_ text: String, color: Color = Theme.dangerText) -> some View {
        Label(text, systemImage: "exclamationmark.triangle")
            .font(Theme.caption).foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// The display name (a field when the plugin is the user's), Show in
    /// Finder, the plugin's ref, then its marketplace, source and version.
    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                if editable {
                    TextField("Name", text: $name, onCommit: saveName)
                        .font(Theme.font(size: 16, weight: .semibold))
                        .sweField()
                        .accessibilityLabel("Display name")
                        .help("The name SWE shows for this plugin. Its id stays \(plugin.ref).")
                } else {
                    Text(shownName)
                        .font(Theme.font(size: 16, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 4)
                if let dir {
                    Button { NSWorkspace.shared.activateFileViewerSelecting([dir]) } label: {
                        Image(systemName: "folder")
                            .font(Theme.font(size: 13))
                            .foregroundStyle(Theme.textSecondary)
                            .frame(width: 24, height: editable ? Theme.fieldHeight : 24)
                            .contentShape(.rect)
                    }
                    .plainButton(cornerRadius: Theme.smallRadius)
                    .help("Show in Finder")
                    .accessibilityLabel("Show \(shownName) in Finder")
                }
            }
            if let nameError { problem(nameError) }
            Text(plugin.ref)
                .font(Theme.font(size: 12, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            FlowLayout(spacing: 6, lineSpacing: 6) {
                Text(marketName).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                if let tag = market.harnesses.values.first?.sourceType {
                    Badge(tag == "directory" ? "local" : tag)
                }
                if let version = plugin.version, !version.isEmpty {
                    Badge("v\(version)")
                }
            }
        }
    }

    /// The description, each harness's state and what `swe plugins info`
    /// says, folded away until asked. Folded, a missing MCP key still
    /// says so.
    private var details: some View {
        VStack(alignment: .leading, spacing: 10) {
            RailDisclosure(title: "Details", isOpen: detailsOpen,
                           help: (open: "Show the description, harnesses and MCP servers",
                                  close: "Hide the description, harnesses and MCP servers")) {
                withAnimation(animation) { detailsOpen.toggle() }
            }
            .padding(.horizontal, -8)
            if detailsOpen {
                let states = harnesses.compactMap { harness in plugin.harnesses[harness].map { (harness, $0) } }
                let description = plugin.description ?? ""
                VStack(alignment: .leading, spacing: 10) {
                    if !description.isEmpty {
                        Text(description).font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !states.isEmpty {
                        FlowLayout(spacing: 6, lineSpacing: 6) {
                            ForEach(states, id: \.0) { harness, state in
                                HarnessStateChip(harness: harness, status: state.status.title)
                            }
                        }
                    }
                    if let info {
                        PluginInfoSection(info: info, saveSecret: saveSecret) {
                            let fetched = await loadInfo(plugin.ref)
                            if let fetched { self.info = fetched }
                        }
                    }
                    if description.isEmpty, states.isEmpty, info == nil {
                        note("Nothing more is known about this plugin.")
                    }
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            } else if let missing = info?.mcpServers.reduce(0, { $0 + $1.secretsMissing.count }), missing > 0 {
                problem(missing == 1 ? "1 MCP key is missing." : "\(missing) MCP keys are missing.",
                        color: Theme.warningText)
            }
        }
    }

    // MARK: Manifest

    /// `.claude-plugin/plugin.json`'s fields over the editor's own draft.
    @ViewBuilder private var manifestSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("Manifest")
            if let manifest {
                let check = manifestCheck?.path == manifest ? manifestCheck?.check : nil
                let document = drafts.documents[manifest]
                switch (check?.canPreview, document?.state) {
                case (.some(false), _):
                    note("plugin.json can't be read here: it is too large or not text.")
                case (.some(true), .some(.failed(let message))):
                    problem(message)
                case (.some(true), .some(.ready)):
                    if let document {
                        if PluginManifestForm.isEditable(document.text) {
                            manifestFields(document, editable: check?.isInside == true, manifest: manifest)
                        } else {
                            note("plugin.json isn't a JSON object right now, so its fields can't show. Fix it in the editor.")
                        }
                    }
                default:
                    SkeletonLines(rows: 3, framed: false, label: "Reading plugin.json…")
                }
            }
        }
    }

    @ViewBuilder
    private func manifestFields(_ document: InstructionDocument, editable: Bool, manifest: String) -> some View {
        let all = PluginManifestForm.Field.allCases
        let limit = PluginManifestForm.foldLimit
        ForEach(RowFold.visible(all, limit: limit, expanded: manifestShowsAll)) { field in
            VStack(alignment: .leading, spacing: 4) {
                Text(field.label).font(Theme.control).foregroundStyle(Theme.textSecondary)
                TextField(field.label, text: Self.field(field, of: document, typedKeywords: $keywordsTyped) {
                    // The visible Save is the one that writes the field:
                    // bring plugin.json into the editor.
                    if selected != manifest { withAnimation(animation) { selected = manifest } }
                }, axis: .vertical)
                .lineLimit(field == .description ? 2...5 : 1...3)
                .sweField(multiline: true)
                .disabled(!editable)
                if let text = PluginManifestForm.problem(field, in: document.text) {
                    problem(text, color: Theme.warningText)
                }
            }
            .accessibilityElement(children: .combine)
        }
        if RowFold.hiddenCount(total: all.count, limit: limit) > 0 {
            ExpandRow(isExpanded: manifestShowsAll,
                      hidden: RowFold.hiddenCount(total: all.count, limit: limit),
                      help: (expand: "Show every manifest field",
                             collapse: "Show the first \(limit) fields"),
                      edge: .inset) {
                withAnimation(animation) { manifestShowsAll.toggle() }
            }
            .padding(.horizontal, -8)
        }
        // A folded field's problem still shows.
        if !manifestShowsAll {
            ForEach(all.dropFirst(limit).compactMap { PluginManifestForm.problem($0, in: document.text) },
                    id: \.self) { text in
                problem(text, color: Theme.warningText)
            }
        }
        if !editable {
            note("plugin.json links outside the plugin's folder, so its fields are read-only.")
        }
    }

    /// One manifest field bound to the plugin.json draft's text: it reads
    /// the value out of `document.text` and writes through
    /// `PluginManifestForm.setting`, so the form never holds a copy of the
    /// file that could fight the editor's. `typedKeywords` keeps the
    /// Keywords field's text between keystrokes. `onEdit` runs after a
    /// write that changed the text.
    static func field(_ field: PluginManifestForm.Field, of document: InstructionDocument,
                      typedKeywords: Binding<String?>, onEdit: @escaping () -> Void) -> Binding<String> {
        Binding(get: {
            field == .keywords
                ? PluginManifestForm.keywords(typed: typedKeywords.wrappedValue, in: document.text)
                : PluginManifestForm.value(field, in: document.text)
        }, set: { value in
            if field == .keywords { typedKeywords.wrappedValue = value }
            let text = PluginManifestForm.setting(field, to: value, in: document.text)
            guard text != document.text else { return }
            document.text = text
            onEdit()
        })
    }

    // MARK: Files

    private func fileList(stacked: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                sectionTitle("Files")
                Spacer(minLength: 4)
                if editable {
                    NewPartMenu(kinds: [.skill, .command, .agent]) { creating = nil; newError = nil; newPart = $0 }
                }
            }
            if let outline {
                let rows = VStack(alignment: .leading, spacing: 1) {
                    ForEach(outline.sections) { section in
                        Text(section.title)
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .padding(.leading, 8)
                            .padding(.top, section.id == outline.sections.first?.id ? 0 : 10)
                            .padding(.bottom, 3)
                            .accessibilityAddTraits(.isHeader)
                        ForEach(visibleRows(section.nodes), id: \.node.id) { row in
                            fileRow(row.node, depth: row.depth)
                        }
                    }
                    if outline.sections.isEmpty {
                        note("This plugin's folder has no files.").padding(.leading, 8)
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
                SkeletonRows(rows: 4, framed: false, shape: .file, label: "Reading the plugin's files…")
            }
            if let filesError { problem(filesError) }
        }
    }

    /// A group's nodes flattened to what shows: every node whose folders
    /// are all open, with its depth.
    private func visibleRows(_ nodes: [SkillFileOutline.Node]) -> [(node: SkillFileOutline.Node, depth: Int)] {
        func walk(_ nodes: [SkillFileOutline.Node], _ depth: Int) -> [(node: SkillFileOutline.Node, depth: Int)] {
            nodes.flatMap { node in
                [(node, depth)] + (node.isFolder && expanded.contains(node.path) ? walk(node.children, depth + 1) : [])
            }
        }
        return walk(nodes, 0)
    }

    private func fileRow(_ node: SkillFileOutline.Node, depth: Int) -> some View {
        // A symlink inside the folder that resolves outside it is not the
        // user's to trash, a skill's SKILL.md goes only with the whole
        // skill, and the manifests stay.
        let trashable = editable && (files?.trashable.contains(node.path) ?? false)
        return RailFileRow(node: node, depth: depth,
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

    /// The alert closes after any of its buttons runs, so a failed create
    /// opens it again, name kept, with the reason as its message.
    private func create() {
        guard let kind = newPart, let dir else { return }
        guard PrimitiveParts.isValidName(newName) else {
            newError = "Use kebab-case: lowercase letters, numbers and dashes."
            // After this close lands, not inside it.
            Task { @MainActor in newPart = kind }
            return
        }
        // create resolves the folder with realpath, which a synced or
        // stale folder can block: off the main actor, like the walk.
        let name = newName
        let request = UUID()
        creating = request
        Task {
            let result = await offMain({ Result { try PrimitiveParts.create(kind, named: name, in: dir) } })
            // A New opened while this waited on disk owns the alert now.
            let current = creating == request
            if current { creating = nil }
            switch result {
            case .success(let file):
                filesError = nil
                if current {
                    newName = ""
                    newError = nil
                    // Straight into the editor, as picking the file would;
                    // a new skill's folder opens around it.
                    if kind == .skill { expanded.insert(file.deletingLastPathComponent().path) }
                    withAnimation(animation) { selected = file.path }
                }
                reload()
            case .failure(let error):
                guard current else { return }
                newError = error.localizedDescription
                newPart = kind
            }
        }
    }

    private func trash() {
        guard let node = trashing, let dir else { return }
        trashing = nil
        Task {
            switch await offMain({ Result { try PrimitiveParts.trash(URL(fileURLWithPath: node.path), inside: dir) } }) {
            case .success:
                drafts.remove(under: node.path)
                if let selected, selected == node.path || selected.hasPrefix(node.path + "/") {
                    // The walk that follows picks the default file again.
                    self.selected = nil
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
        if let path = selected, dir != nil {
            let url = URL(fileURLWithPath: path)
            let check = opened?.path == path ? opened?.check : nil
            let fileEditable = editable && check?.isInside == true
            VStack(alignment: .leading, spacing: 12) {
                if check?.canPreview == true, !fileEditable {
                    PrimitiveLockNote(text: editable
                        ? "Only files inside the plugin's own folder can be edited, so this one is read-only."
                        : lockText)
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
                                          rawOnly: !SkillDetailContent.isMarkdown(url), onSaved: reload)
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
            VStack(alignment: .leading, spacing: 12) {
                if !editable { PrimitiveLockNote(text: lockText) }
                Card {
                    Text(emptyText).font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// What the pane says with no file in it.
    private var emptyText: String {
        if dir == nil { return "This plugin has no local folder: its files come from a checkout swe manages." }
        if outline?.sections.isEmpty == true { return "This plugin's folder has no files." }
        return "Pick a file in the list to open it here."
    }

    /// Why a plugin that isn't the user's is read-only.
    private var lockText: String {
        let github = market.harnesses.values.contains { ["github", "git"].contains($0.sourceType ?? "") }
        return github
            ? "Updates from GitHub would undo edits, so this plugin is read-only."
            : "Only plugins in your ~/.swe/plugins hub can be edited, so this one is read-only."
    }

    /// The file under the plugin (`release-tools/skills/tdd/SKILL.md`): the
    /// rail already says which plugin this is, and a full path would wrap
    /// into a column beside the header in a narrow window. Led by the
    /// plugin's name, not its folder's: a cached copy sits in a folder
    /// named for its version (`0.2.4/commands/connect.md`).
    private func fileSubtitle(_ url: URL) -> String {
        guard let dir else { return displayPath(url) }
        return Self.fileSubtitle(url.path, under: dir.path, plugin: plugin.name) ?? displayPath(url)
    }

    /// `path` as `<plugin>/<path under dir>`; nil when it is not under `dir`.
    static func fileSubtitle(_ path: String, under dir: String, plugin: String) -> String? {
        let base = dir.hasSuffix("/") ? dir : dir + "/"
        guard path.hasPrefix(base) else { return nil }
        return plugin + "/" + path.dropFirst(base.count)
    }

    private func loadingCard(_ url: URL) -> some View {
        Card(title: url.lastPathComponent, subtitle: fileSubtitle(url), monoSubtitle: true) {
            SkeletonLines(rows: 12, framed: false)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
    }

    // MARK: Name

    /// Store the display name in SWE's overlay (the plugin's id never
    /// changes, so nothing is reinstalled), and in the Codex manifest's
    /// `interface.displayName` when the plugin ships one.
    private func saveName() {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let codex = editable ? dir?.appending(path: ".codex-plugin/plugin.json") : nil
        let shown = trimmed.isEmpty ? plugin.name : trimmed
        let fallback = shownName
        // Disk work (the overlay under ~/.swe, then the plugin's own
        // folder), so off the main actor, one save after another.
        let previous = manifestWrite
        manifestWrite = Task {
            await previous?.value
            do {
                try await saveDisplayName(trimmed.isEmpty ? nil : trimmed)
                nameError = nil
            } catch {
                // The write left the overlay untouched: show that name
                // again and stop, so a failed rename changes nothing.
                name = fallback
                nameError = "Couldn't save the name: \(error.localizedDescription)"
                return
            }
            // Only a plugin that ships a Codex manifest has one to update;
            // a manifest that exists but cannot be read or written must be
            // reported, or the rename looks saved while the file keeps the
            // old name.
            guard let codex else { return }
            let failure: String? = await offMain {
                guard FileManager.default.fileExists(atPath: codex.path) else { return nil }
                do {
                    let text = try String(contentsOf: codex, encoding: .utf8)
                    let edited = try PluginManifestFields.setCodexDisplayName(shown, in: text)
                    try Data(edited.utf8).write(to: codex, options: .atomic)
                    return nil
                } catch {
                    return error.localizedDescription
                }
            }
            if let failure { nameError = "Renamed, but couldn't update the Codex manifest: \(failure)" }
        }
    }

    // MARK: Leaving

    /// Back with nothing unsaved; otherwise ask first.
    private func leave() {
        if drafts.dirty.isEmpty { onBack() } else { leaving = true }
    }

    /// Save every draft, then leave; a file that will not save keeps the
    /// page open and says why. Either way a file that was written has the
    /// catalog read again: the list and the next open show what
    /// plugin.json now says, as after the editor's own Save.
    private func saveAllAndLeave() {
        Task {
            let result = await drafts.saveAll()
            if result.failed.isEmpty {
                leaveError = nil
                if result.wrote { onFilesChanged() }
                onBack()
            } else {
                leaveError = "Couldn't save \(result.failed.joined(separator: ", "))."
                if result.wrote { reload() }
            }
        }
    }

    private func reload() {
        refresh += 1
        onFilesChanged()
    }
}

// MARK: - Details pieces

/// One harness and the plugin's state in it ("Claude Code · Enabled").
private struct HarnessStateChip: View {
    var harness: String
    var status: String

    var body: some View {
        HStack(spacing: 5) {
            HarnessIcon(harness: harness, size: 12)
            Text("\(HarnessMark(harness).name) · \(status)")
        }
        .font(Theme.font(size: 12, weight: .medium))
        .foregroundStyle(Theme.textLabel)
        .lineLimit(1)
        .padding(.horizontal, 8)
        .padding(.vertical, 1)
        .background(Theme.subtle, in: .capsule)
        .overlay(Capsule().strokeBorder(Theme.borderStrong, lineWidth: 1))
    }
}

/// `swe plugins info` in the Details fold: the declared MCP servers (key
/// names only, never values), per-skill and MCP call counts from the
/// session index, and the newest file change.
private struct PluginInfoSection: View {
    var info: PluginInfo
    /// Writes one key's value through `swe mcp secret set`.
    var saveSecret: @MainActor (String, String) async throws -> Void
    /// Reads `info` again, so a saved key's missing flag clears.
    var reload: @MainActor () async -> Void

    @State private var showsAll = false
    /// The `<server>/<key>` whose value field is open, and its drafts.
    @State private var editingKey: String?
    @State private var keyDrafts: [String: String] = [:]
    @State private var savingKey = false
    @State private var keyError: String?

    var body: some View {
        let limit = PluginDetailContent.serverFoldLimit
        VStack(alignment: .leading, spacing: 10) {
            if !info.mcpServers.isEmpty {
                Text("MCP servers").font(Theme.font(size: 12, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                ForEach(RowFold.visible(info.mcpServers, limit: limit, expanded: showsAll), id: \.name) { server in
                    serverRow(server)
                }
                if RowFold.hiddenCount(total: info.mcpServers.count, limit: limit) > 0 {
                    ExpandRow(isExpanded: showsAll,
                              hidden: RowFold.hiddenCount(total: info.mcpServers.count, limit: limit),
                              help: (expand: "Show every declared server",
                                     collapse: "Show the first \(limit) servers"),
                              edge: .inset) {
                        withAnimation(.easeOut(duration: Theme.fadeDuration)) { showsAll.toggle() }
                    }
                }
            }
            if !info.mcpServers.isEmpty, usageLine != nil {
                Rectangle().fill(Theme.rowDivider).frame(height: Theme.hairline)
            }
            if let line = usageLine {
                Text(line).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let updated = info.updated {
                Text("Last change \(updated.prefix(10))")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
            }
        }
    }

    /// The server's name and calls on one line, where it stands under
    /// it, then its key names and a row per missing key.
    private func serverRow(_ server: PluginInfo.DeclaredServer) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(server.name)
                    .font(Theme.font(size: 12, design: .monospaced)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                if let usage = server.usage, usage.tracked, usage.calls > 0 {
                    Text("\(usage.calls) calls")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize()
                }
            }
            ServerStateView(server: server)
            if !server.envKeys.isEmpty || !server.headerKeys.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    if !server.envKeys.isEmpty {
                        Text("env " + server.envKeys.joined(separator: ", "))
                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .lineLimit(2)
                    }
                    if !server.headerKeys.isEmpty {
                        Text("headers " + server.headerKeys.joined(separator: ", "))
                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .lineLimit(2)
                    }
                    ForEach(server.secretsMissing, id: \.self) { name in
                        KeyRow(server: server.name, name: name, editable: server.inHub,
                               editingKey: $editingKey, drafts: $keyDrafts,
                               saving: savingKey, save: saveSecret(named:))
                    }
                    if let keyError {
                        Text(keyError).font(Theme.caption).foregroundStyle(Theme.dangerText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !server.secretsMissing.isEmpty {
                        Text(server.inHub
                             ? "Saved to ~/.swe/secrets/mcp/.api_keys, never shown after saving. "
                               + "Sync fills a ${NAME} reference when it writes a harness config."
                             : "This server is declared in a harness's own config, so its "
                               + "values live in that file: swe's secrets store does not "
                               + "reach it. Set the key where the plugin is declared.")
                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    /// Where a declared server stands: its swe hub flag and each harness
    /// it is installed in or switched off for, names through
    /// HarnessMarkView like every harness mention. Wraps in a narrow rail.
    private struct ServerStateView: View {
        var server: PluginInfo.DeclaredServer

        var body: some View {
            if server.harnesses.isEmpty && server.disabledIn.isEmpty && !server.inHub {
                Text("declared only").font(Theme.caption).foregroundStyle(Theme.textMuted)
            } else {
                FlowLayout(spacing: 4, lineSpacing: 3) {
                    if server.inHub {
                        Text("in hub").font(Theme.caption).foregroundStyle(Theme.textMuted)
                    }
                    if !server.harnesses.isEmpty {
                        Text("in").font(Theme.caption).foregroundStyle(Theme.textMuted)
                        ForEach(server.harnesses, id: \.self) { harness in
                            HarnessMarkView(harness: harness, size: 12)
                        }
                    }
                    if !server.disabledIn.isEmpty {
                        Text("off in").font(Theme.caption).foregroundStyle(Theme.textMuted)
                        ForEach(server.disabledIn, id: \.self) { harness in
                            HarnessMarkView(harness: harness, size: 12)
                        }
                    }
                }
            }
        }
    }

    /// One line for the usage rollup, or nil when the index is off or
    /// nothing was ever called.
    private var usageLine: String? {
        guard let usage = info.usage, usage.tracked else { return nil }
        let called = info.skills.filter { ($0.usage?.calls ?? 0) > 0 }
        var line: String
        if usage.calls == 0 {
            line = "No calls indexed yet: the index scans existing sessions too."
        } else {
            line = "\(usage.calls) calls across \(usage.sessions) sessions"
            if let last = usage.lastUsed { line += " · last \(last.prefix(10))" }
            if let skillCalls = usage.skillCalls, let mcpCalls = usage.mcpCalls {
                line += " (\(skillCalls) skill, \(mcpCalls) mcp)"
            }
        }
        if !called.isEmpty {
            line += "\n" + called.map { "\($0.name) \($0.usage?.calls ?? 0)" }.joined(separator: " · ")
        }
        return line
    }

    /// Writes one key's value to the secrets store, then reads `info`
    /// again so the key's missing flag clears. The value itself never
    /// comes back: it is write-only.
    private func saveSecret(named name: String) async {
        let value = keyDrafts[name] ?? ""
        guard !value.isEmpty else { return }
        savingKey = true
        keyError = nil
        defer { savingKey = false }
        do {
            try await saveSecret(name, value)
            keyDrafts[name] = ""
            editingKey = nil
            await reload()
        } catch {
            keyError = error.localizedDescription
        }
    }

    /// One unresolved `${NAME}` a server declares: the name, its missing
    /// state, and a Set button opening a write-only field for it. Only a
    /// hub-managed server gets the button (`editable`): a declared-only
    /// server's values live in the harness's own file.
    private struct KeyRow: View {
        /// The server declaring it, so two servers' same-named refs get
        /// their own editor state.
        var server: String
        var name: String
        /// True when swe manages the server (it is in the hub): only
        /// then does its store reach the harness files.
        var editable: Bool
        @Binding var editingKey: String?
        @Binding var drafts: [String: String]
        var saving: Bool
        var save: (String) async -> Void

        private var id: String { "\(server)/\(name)" }
        private var editing: Bool { editable && editingKey == id }

        var body: some View {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(name)
                        .font(Theme.font(size: 11, design: .monospaced))
                        .foregroundStyle(Theme.warningText)
                        .lineLimit(1).truncationMode(.middle)
                    Text("missing")
                        .font(Theme.caption)
                        .foregroundStyle(Theme.warningText)
                        .fixedSize()
                    Spacer(minLength: 4)
                    if editable {
                        Button(editing ? "Cancel" : "Set value") {
                            withAnimation(.easeOut(duration: Theme.fadeDuration)) {
                                editingKey = editing ? nil : id
                            }
                        }
                        .font(Theme.font(size: 11, weight: .medium))
                        .buttonStyle(.plain)
                        .foregroundStyle(Theme.interactive)
                        .fixedSize()
                    }
                }
                if editing {
                    HStack(spacing: 8) {
                        SecureField("Paste the value, it is never shown again",
                                    text: Binding(get: { drafts[name] ?? "" },
                                                  set: { drafts[name] = $0 }))
                            .sweField(monospaced: true)
                        Button(saving ? "Saving…" : "Save") { Task { await save(name) } }
                            .secondaryButton()
                            .disabled(saving || (drafts[name] ?? "").isEmpty)
                    }
                }
            }
            .padding(.vertical, 2)
        }
    }
}
