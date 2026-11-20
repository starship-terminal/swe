import Foundation

/// Where an opened plugin's files are and whether they are the user's, for
/// the plugin detail page. Resolves symlinks and checks files on disk, so
/// work it out off the main actor (`offMain`): a synced or stale folder
/// can block those calls for as long as it likes.
public struct PluginFolders: Sendable, Equatable {
    /// The plugin's own folder when the user may edit it, else nil.
    public let editableDir: URL?
    /// The folder whose files the page lists: the editable one, else the
    /// first installed or cached copy that exists; nil when there is none.
    public let dir: URL?
    /// `.claude-plugin/plugin.json` in the editable folder, when it exists.
    public let manifest: URL?

    public var editable: Bool { editableDir != nil }

    public init(plugin: CatalogPlugin, market: PluginMarketplace, home: URL,
                resolve: (String) -> String = FolderOwners.realPath,
                exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }) {
        let editableDir = PrimitiveOwnership.pluginDirectory(plugin, in: market, home: home, resolve: resolve)
        self.editableDir = editableDir
        // No editable folder: an installed or cached copy still shows its
        // files, read-only.
        dir = editableDir ?? plugin.folders.first(where: exists).map { URL(fileURLWithPath: $0) }
        manifest = editableDir.map { $0.appending(path: ".claude-plugin/plugin.json") }
            .flatMap { exists($0.path) ? $0 : nil }
    }
}

/// Whether an opened skill is the user's and where its SKILL.md is, for
/// the skill detail page. Like `PluginFolders`, work it out off the main
/// actor.
public struct SkillFolders: Sendable, Equatable {
    /// The skill's folder is the user's own (`PrimitiveOwnership.isEditable`).
    public let editable: Bool
    /// The folder's SKILL.md, when it exists.
    public let skillFile: URL?
    /// SKILL.md may be edited: the folder is editable and the file stays
    /// inside it once symlinks resolve.
    public let skillFileEditable: Bool

    public init(skillPath: String, home: URL,
                resolve: (String) -> String = FolderOwners.realPath,
                exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }) {
        let editable = PrimitiveOwnership.isEditable(skillPath: skillPath, home: home, resolve: resolve)
        let file = URL(fileURLWithPath: skillPath).appending(path: "SKILL.md")
        let skillFile = exists(file.path) ? file : nil
        self.editable = editable
        self.skillFile = skillFile
        skillFileEditable = editable && skillFile.map {
            PrimitiveOwnership.isInside($0.path, root: skillPath, resolve: resolve)
        } == true
    }
}

/// What the file sheet needs to know about a picked file, checked when it
/// opens rather than taken from the tree walk: a listed file may have been
/// swapped for a link that points outside the folder since. Reads the
/// file and resolves symlinks, so work it out off the main actor.
public struct PrimitiveFileCheck: Sendable, Equatable {
    /// Small enough and UTF-8, so it opens in the editor
    /// (`PrimitiveTree.isTextFile`).
    public let canPreview: Bool
    /// The file stays inside `root` once symlinks resolve, so it may be
    /// edited when the folder is the user's.
    public let isInside: Bool

    public init(path: String, root: URL,
                resolve: (String) -> String = FolderOwners.realPath) {
        canPreview = PrimitiveTree.isTextFile(URL(fileURLWithPath: path))
        isInside = PrimitiveOwnership.isInside(path, root: root.path, resolve: resolve)
    }
}

extension PrimitiveTree {
    /// The paths of every row, and of every file a row opens, that stay
    /// inside `root` once symlinks resolve: the ones a detail page may
    /// trash or edit. A link inside the folder that points outside it is
    /// left out. Resolves each path on disk, so call it off the main actor.
    public func pathsInside(_ root: URL,
                            resolve: (String) -> String = FolderOwners.realPath) -> Set<String> {
        let entries = sections.flatMap(\.entries).flatMap { [$0] + $0.children }
        let paths = Set(entries.flatMap { [$0.path, $0.previewPath] })
        return paths.filter { PrimitiveOwnership.isInside($0, root: root.path, resolve: resolve) }
    }

    /// The tree without the files `entries` already list under another
    /// name: a `.mcp.json` that links to `config/mcp.json` is one file, and
    /// two rows for it would open as two documents whose saves conflict.
    /// The plugin detail drops them for its manifests, which keep their
    /// row. A group left with no files goes too. Resolves each path on
    /// disk, so call it off the main actor.
    public func dropping(filesSharedWith entries: [Entry],
                         resolve: (String) -> String = FolderOwners.realPath) -> PrimitiveTree {
        let taken = Set(entries.map { resolve($0.path) })
        guard !taken.isEmpty else { return self }
        var tree = self
        tree.sections = sections.compactMap { section in
            var section = section
            section.entries = section.entries.compactMap { entry in
                guard entry.isFolder else { return taken.contains(resolve(entry.path)) ? nil : entry }
                var entry = entry
                entry.children.removeAll { taken.contains(resolve($0.path)) }
                return entry
            }
            return section.entries.isEmpty ? nil : section
        }
        return tree
    }

    /// The rows a detail page offers a trash button: the ones
    /// `pathsInside` keeps, less every `skillFilePaths` SKILL.md. Resolves
    /// each path on disk, so call it off the main actor.
    public func trashablePaths(_ root: URL,
                               resolve: (String) -> String = FolderOwners.realPath) -> Set<String> {
        pathsInside(root, resolve: resolve).subtracting(skillFilePaths)
    }
}
