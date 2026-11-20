import Foundation

/// A plugin folder's files as the detail rail lists them: `PrimitiveTree`'s
/// groups (Skills, Commands, Agents, Hooks, Other files), each with its
/// count and its files nested the way they sit on disk under the group's
/// folder. A skill is one folder that opens to its files, SKILL.md first.
/// Built from the tree, so the same files are listed (dotfiles and `.git`
/// never are) and no disk call runs here.
public struct PluginFileOutline: Sendable, Equatable {
    public struct Section: Sendable, Equatable, Identifiable {
        public var kind: PrimitiveTree.Kind
        /// How many skills, commands, agents, hooks or other files.
        public var count: Int
        public var nodes: [SkillFileOutline.Node]
        public var id: String { kind.rawValue }

        /// "Skills · 2"; the main file is one file, with no count to give.
        public var title: String {
            kind == .main ? kind.title : "\(kind.title) · \(count)"
        }
    }

    public var sections: [Section]

    public init(tree: PrimitiveTree, root: URL) {
        sections = tree.sections.map { section in
            let folder: URL
            let skillFileDepth: Int?
            switch section.kind {
            case .main: (folder, skillFileDepth) = (root, 0)
            case .other: (folder, skillFileDepth) = (root, nil)
            case .skills: (folder, skillFileDepth) = (root.appending(path: section.kind.rawValue), 1)
            case .commands, .agents, .hooks:
                (folder, skillFileDepth) = (root.appending(path: section.kind.rawValue), nil)
            }
            return Section(kind: section.kind, count: section.entries.count,
                           nodes: SkillFileOutline.nodes(for: section.entries, under: folder,
                                                         skillFileDepth: skillFileDepth))
        }
    }

    /// Every file's path, in the order the rail lists them.
    public var filePaths: [String] {
        func walk(_ nodes: [SkillFileOutline.Node]) -> [String] {
            nodes.flatMap { $0.isFolder ? walk($0.children) : [$0.path] }
        }
        return sections.flatMap { walk($0.nodes) }
    }

    /// The file the editor shows when a plugin opens: its README, else the
    /// first skill's SKILL.md, else `manifest` (plugin.json, when the
    /// plugin is the user's), else the first file listed.
    public func defaultFile(manifest: String?) -> String? {
        readme ?? firstSkillFile ?? manifest ?? filePaths.first
    }

    /// A README at the top of the folder, Markdown before any other.
    private var readme: String? {
        let top = sections.first { $0.kind == .other }?.nodes.filter { !$0.isFolder } ?? []
        for name in ["readme.md", "readme.markdown", "readme.txt", "readme"] {
            if let file = top.first(where: { $0.name.lowercased() == name }) { return file.path }
        }
        return nil
    }

    private var firstSkillFile: String? {
        let skills = sections.first { $0.kind == .skills }?.nodes ?? []
        return skills.lazy.compactMap { skill in
            skill.children.first { !$0.isFolder && $0.name == "SKILL.md" }?.path
        }.first
    }

    /// The folders to open so `path`'s row shows, outermost first; empty
    /// for a file at the top of its group or one the outline lacks.
    public func folders(containing path: String) -> [String] {
        func find(_ nodes: [SkillFileOutline.Node], _ trail: [String]) -> [String]? {
            for node in nodes {
                if node.isFolder {
                    if let found = find(node.children, trail + [node.path]) { return found }
                } else if node.path == path {
                    return trail
                }
            }
            return nil
        }
        return sections.lazy.compactMap { find($0.nodes, []) }.first ?? []
    }
}

/// The Manifest section of a plugin's detail rail. Like
/// `SkillFrontmatterForm`, it reads from and writes to the text of the
/// `.claude-plugin/plugin.json` draft the editor beside it shows, so one
/// Save writes both and nothing holds a second copy of the file. Writes go
/// through `PluginManifestFields.apply`, which changes only the field
/// asked for and leaves every other key and the file's layout alone.
public enum PluginManifestForm {
    public enum Field: String, Sendable, CaseIterable, Identifiable {
        case description, version, authorName, authorEmail, authorURL
        case homepage, repository, license, keywords

        public var id: String { rawValue }

        public var label: String {
            switch self {
            case .description: "Description"
            case .version: "Version"
            case .authorName: "Author"
            case .authorEmail: "Email"
            case .authorURL: "Author URL"
            case .homepage: "Homepage"
            case .repository: "Repository"
            case .license: "License"
            case .keywords: "Keywords"
            }
        }

        var keyPath: WritableKeyPath<PluginManifestFields, String> {
            switch self {
            case .description: \.description
            case .version: \.version
            case .authorName: \.authorName
            case .authorEmail: \.authorEmail
            case .authorURL: \.authorURL
            case .homepage: \.homepage
            case .repository: \.repository
            case .license: \.license
            case .keywords: \.keywords
            }
        }
    }

    /// The fields the section shows before its "Show more" row.
    public static let foldLimit = 3

    /// Whether `text` is a JSON object, so the fields can read and write
    /// it. A draft broken mid-edit in the editor is not, until it is fixed
    /// there.
    public static func isEditable(_ text: String) -> Bool {
        guard let data = text.data(using: .utf8) else { return false }
        return (try? JSONSerialization.jsonObject(with: data)) is [String: Any]
    }

    /// `field`'s value in the manifest `text`; empty when the file lacks it.
    public static func value(_ field: Field, in text: String) -> String {
        PluginManifestFields(parsing: text)[keyPath: field.keyPath]
    }

    /// `text` with `field` set to `value`; `text` itself when it is not a
    /// JSON object the form can write.
    public static func setting(_ field: Field, to value: String, in text: String) -> String {
        var fields = PluginManifestFields(parsing: text)
        fields[keyPath: field.keyPath] = value
        return (try? fields.apply(to: text)) ?? text
    }

    /// What the Keywords field shows. The file keeps a list, so text typed
    /// on the way to the next keyword ("docs, ") reads back shorter
    /// ("docs") and would jump under the cursor: `typed` shows for as long
    /// as it still names the file's keywords.
    public static func keywords(typed: String?, in text: String) -> String {
        let saved = value(.keywords, in: text)
        guard let typed,
              PluginManifestFields.keywordList(typed) == PluginManifestFields.keywordList(saved)
        else { return saved }
        return typed
    }

    /// "Version should look like 1.2.3.": what `field` holds that a
    /// harness may reject, the same checks as `PluginManifestFields.problems`.
    /// Shown under the field; the editor's Save still writes the file as
    /// typed.
    public static func problem(_ field: Field, in text: String) -> String? {
        let value = value(field, in: text)
        guard !value.isEmpty else { return nil }
        switch field {
        case .version:
            return PluginManifestFields.isVersion(value) ? nil : "Version should look like 1.2.3."
        case .homepage, .repository, .authorURL:
            return PluginManifestFields.isURL(value) ? nil : "\(field.label) is not a URL."
        case .description, .authorName, .authorEmail, .license, .keywords:
            return nil
        }
    }
}
