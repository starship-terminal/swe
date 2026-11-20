import Foundation
import Observation

/// A detail page's open file documents, one per path for the page's
/// life: picking another file or stepping away and back never drops an
/// unsaved edit.
@MainActor
@Observable
public final class PrimitiveDrafts {
    public private(set) var documents: [String: InstructionDocument] = [:]

    public init() {}

    /// Reads still running, by path, for `settled()`.
    private var loading: [String: (id: UUID, task: Task<Void, Never>)] = [:]

    /// The document for `path`, opened on first ask. It reads in the
    /// background, off the main actor (a file under a synced or stale
    /// folder can block a read), and shows as `.loading` until then.
    @discardableResult
    public func document(for path: String) -> InstructionDocument {
        if let document = documents[path] { return document }
        let document = InstructionDocument(url: URL(fileURLWithPath: path))
        documents[path] = document
        let id = UUID()
        loading[path] = (id, Task { [weak self] in
            await document.load()
            // A reopened path's newer read owns the entry now.
            if self?.loading[path]?.id == id { self?.loading[path] = nil }
        })
        return document
    }

    /// Waits for every read `document(for:)` started.
    public func settled() async {
        while let next = loading.values.first { await next.task.value }
    }

    /// The paths with unsaved text.
    public var dirty: [String: InstructionDocument] {
        documents.filter { $0.value.isDirty }
    }

    /// Saves every document with unsaved text, for a page's "Save all"
    /// before it leaves.
    /// - Returns: `wrote`, whether any file was written, so the page can
    ///   have its lists read the disk again even when another file failed;
    ///   and `failed`, the names of the files that still hold unsaved text
    ///   (a refused or failed save, or text typed while the save ran), in
    ///   path order.
    public func saveAll() async -> (wrote: Bool, failed: [String]) {
        var wrote = false
        var failed: [String] = []
        for (path, document) in dirty.sorted(by: { $0.key < $1.key }) {
            let outcome = await document.save()
            if outcome == .saved { wrote = true }
            if outcome != .saved || document.isDirty {
                failed.append(URL(fileURLWithPath: path).lastPathComponent)
            }
        }
        return (wrote, failed)
    }

    /// Drop the document at `path` and every document under it, for a
    /// trashed file or folder: a later save-all must not write a file
    /// that no longer exists.
    public func remove(under path: String) {
        documents = documents.filter { key, _ in
            key != path && !key.hasPrefix(path + "/")
        }
    }
}
