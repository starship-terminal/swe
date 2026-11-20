import Foundation

/// A skill folder's files as the detail rail lists them: the folders and
/// files under the skill's root, nested the way they sit on disk, with the
/// skill's own SKILL.md first, then folders, then files, each level in
/// name order. Built from `PrimitiveTree`, so the same files are listed
/// (dotfiles and `.git` never are) and no disk call runs here.
public struct SkillFileOutline: Sendable, Equatable {
    public struct Node: Sendable, Equatable, Identifiable {
        /// The last path component.
        public var name: String
        /// The absolute path, symlinks unresolved, as the tree has it.
        public var path: String
        public var isFolder: Bool
        public var children: [Node] = []
        public var id: String { path }
    }

    public var nodes: [Node]

    public init(tree: PrimitiveTree, root: URL) {
        let base = root.path.hasSuffix("/") ? root.path : root.path + "/"
        func relative(_ path: String) -> [String]? {
            guard path.hasPrefix(base), path.count > base.count else { return nil }
            return path.dropFirst(base.count).split(separator: "/").map(String.init)
        }
        var items: [(parts: [String], isFolder: Bool)] = []
        for entry in tree.sections.flatMap(\.entries) {
            if entry.isFolder {
                // A skill folder shows even when it is empty.
                if let parts = relative(entry.path) { items.append((parts, true)) }
                items += entry.children.compactMap { relative($0.path).map { ($0, false) } }
            } else if let parts = relative(entry.path) {
                items.append((parts, false))
            }
        }
        nodes = Self.build(items, base: base, prefix: "")
    }

    private static func build(_ items: [(parts: [String], isFolder: Bool)],
                              base: String, prefix: String) -> [Node] {
        var files: [Node] = []
        var folders: [String: [(parts: [String], isFolder: Bool)]] = [:]
        for item in items {
            guard let first = item.parts.first else { continue }
            if item.parts.count > 1 {
                folders[first, default: []].append((Array(item.parts.dropFirst()), item.isFolder))
            } else if item.isFolder {
                folders[first, default: []] += []
            } else if !files.contains(where: { $0.name == first }) {
                files.append(Node(name: first, path: base + prefix + first, isFolder: false))
            }
        }
        let folderNodes = folders.keys.sorted().map { name in
            Node(name: name, path: base + prefix + name, isFolder: true,
                 children: build(folders[name] ?? [], base: base, prefix: prefix + name + "/"))
        }
        files.sort { $0.name < $1.name }
        // The skill's own SKILL.md leads: it is the skill.
        if prefix.isEmpty, let main = files.firstIndex(where: { $0.name == "SKILL.md" }) {
            return [files.remove(at: main)] + folderNodes + files
        }
        return folderNodes + files
    }

    /// Every file's path, in the order the rail lists them.
    public var filePaths: [String] {
        func walk(_ nodes: [Node]) -> [String] {
            nodes.flatMap { $0.isFolder ? walk($0.children) : [$0.path] }
        }
        return walk(nodes)
    }
}

/// How the skill detail lays out its rail and file editor: side by side,
/// the rail full width when the editor keeps `editorMinimum` beside it and
/// narrow otherwise (both scaled by the text zoom, never more than 45% of
/// the room); stacked, rail over editor, once even the narrow rail would
/// leave the editor under `stackedBelow`. A 900pt window with the sidebar
/// at its widest (`SidebarWidth.range`, 360) leaves 460pt of page, which
/// stacks; with the standard 196pt sidebar it leaves 624pt, which doesn't.
public enum SkillDetailLayout {
    public static let railWidth = 300.0
    public static let narrowRailWidth = 232.0
    /// The space between the rail and the editor.
    public static let gap = 12.0
    /// The narrowest editor at 100% zoom beside the full rail.
    public static let editorMinimum = 480.0
    /// The narrowest editor at 100% zoom beside the narrow rail.
    public static let stackedBelow = 380.0
    /// The stacked editor's least height, so it never shrinks to a strip.
    public static let stackedEditorHeight = 360.0

    /// Rail over editor, each the full width, for an `available` width
    /// too narrow for the two side by side.
    public static func stacks(available: Double, zoom: Double = 1) -> Bool {
        let zoom = TextZoom.clamped(zoom)
        guard available > 0, available.isFinite else { return false }
        return available - gap - narrowRailWidth * zoom < stackedBelow * zoom
    }

    /// The rail's width for an `available` width of rail, gap and editor
    /// together; an unmeasured container (0) gets the full rail.
    public static func railWidth(available: Double, zoom: Double = 1) -> Double {
        let zoom = TextZoom.clamped(zoom)
        let wide = railWidth * zoom
        guard available > 0, available.isFinite else { return wide }
        let width = available - gap - wide >= editorMinimum * zoom ? wide : narrowRailWidth * zoom
        return min(width, max(narrowRailWidth, available * 0.45))
    }
}

/// The Frontmatter section of a skill's detail rail. It reads from and
/// writes to the text of the SKILL.md draft the editor beside it shows, so
/// one Save writes both and nothing holds a second copy of the file.
public enum SkillFrontmatterForm {
    public struct Row: Sendable, Equatable, Identifiable {
        public var key: String
        public var value: String
        /// The description: a field a few lines tall.
        public var multiline: Bool
        /// Read-only whatever the skill: `name` is the skill's id.
        public var locked: Bool
        public var id: String { key }
    }

    /// `name` (read-only, the folder's name when the file has none), then
    /// `description` (empty when the file has none, left out when it is in
    /// a form the form can't edit), then every other field in file order.
    public static func rows(in text: String, name: String) -> [Row] {
        let front = MarkdownFrontmatter(text: text)
        let fields = front?.fields ?? []
        func value(_ key: String) -> String? { fields.first { $0.key == key }?.value }
        var rows = [Row(key: "name", value: value("name") ?? name, multiline: false, locked: true)]
        if !(front?.lockedKeys.contains("description") ?? false) {
            rows.append(Row(key: "description", value: value("description") ?? "", multiline: true, locked: false))
        }
        var seen: Set<String> = ["name", "description"]
        for field in fields where seen.insert(field.key).inserted {
            rows.append(Row(key: field.key, value: field.value, multiline: false, locked: false))
        }
        return rows
    }

    /// The keys the file holds in a form the form can't edit, in file
    /// order, once each.
    public static func lockedKeys(in text: String) -> [String] {
        var seen: Set<String> = []
        return (MarkdownFrontmatter(text: text)?.lockedKeys ?? []).filter { seen.insert($0).inserted }
    }

    /// `text` with `key` set to `value`. `name` never changes here.
    public static func setting(_ key: String, to value: String, in text: String) -> String {
        guard key != "name" else { return text }
        return MarkdownFrontmatter.update(text, set: [(key: key, value: value)])
    }
}
