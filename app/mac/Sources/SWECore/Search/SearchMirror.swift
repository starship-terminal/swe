import Foundation

/// Search over transcript mirror copies (#1918): the Swift port of swe's
/// `search_writer._index_copies` and `_copied_rows`, so a Mac whose hub
/// runs no Python index pass still searches the sessions Claude Code and
/// Codex pruned, and so the two writers never fight over them.
///
/// The rule is written out in docs/ported-logic.md and pinned on the
/// Python side by `tests/test_search_mirror.py`; `SearchMirrorTests` is its
/// Swift twin. In short:
///
/// - A copy is used only when no live source carries its session and its
///   source path is gone. Its rows are filed under that source path, never
///   the copy's, and every commit records `mirror_copy:<path>`.
/// - Per session the newest copy (mtime, then path) is the candidate.
/// - It is read only when no row of the session already holds as much,
///   and only when it holds at least as much as every row it would stand
///   beside; those rows go in the same commit. Anything else leaves the
///   index as it is.
/// - A live file's read retires the rows a copy put in for its session
///   under other, gone paths (`copiedRows`); rows read from live files are
///   never retired that way.
/// - Every delete re-checks, inside its transaction, that each row is
///   unchanged and its path still gone: swe writes the same index.
enum SearchMirror {
    /// What one copy pass did, swe's `PassResult` counts for its part.
    struct Counts: Equatable {
        var files = 0
        var current = 0
        var indexed = 0
        var moved = 0
        var failed = 0
    }

    /// Whether `row` already holds at least what a file at `stat` would
    /// give: a whole read of a session, of a file at least as new and as
    /// long (swe's `_covers`).
    static func covers(_ row: SearchFileRow?, _ stat: SearchFileStat) -> Bool {
        guard let row, row.state == .current, row.sessionRowID != nil,
              row.byteOffset != SearchStore.partialFileOffset else { return false }
        return row.mtimeMs >= stat.mtimeMs && (row.sizeBytes ?? 0) >= (stat.sizeBytes ?? 0)
    }

    /// Whether a file at `stat` holds at least what `row` was read from:
    /// as new and as long (swe's `_dominates`). Transcripts only grow; a
    /// newer but shorter file was rewritten and stands for nothing it lost.
    static func dominates(_ stat: SearchFileStat, _ row: SearchFileRow) -> Bool {
        stat.mtimeMs >= row.mtimeMs && (stat.sizeBytes ?? 0) >= size(of: row)
    }

    /// What a row was read from, as a stat `dominates` compares.
    static func rowStat(_ row: SearchFileRow) -> SearchFileStat {
        SearchFileStat(ino: row.ino, mtimeMs: row.mtimeMs, sizeBytes: size(of: row))
    }

    private static func size(of row: SearchFileRow) -> Int64 {
        row.sizeBytes ?? max(row.byteOffset, 0)
    }

    /// One copy's stat as the index records it, and its mtime in
    /// nanoseconds for the newest-copy pick.
    static func fileStat(_ path: String) -> (stat: SearchFileStat, mtimeNs: Int64)? {
        var info = stat()
        guard stat(path, &info) == 0 else { return nil }
        let sec = info.st_mtimespec.tv_sec, nsec = info.st_mtimespec.tv_nsec
        // Double(sec) + Double(nsec) / 1e9, then * 1000: the float both
        // writers store, so an unchanged file compares equal on both sides.
        let mtimeMs = (Double(sec) + Double(nsec) / 1e9) * 1000
        return (SearchFileStat(ino: Int64(info.st_ino), mtimeMs: mtimeMs, sizeBytes: Int64(info.st_size)),
                Int64(sec) * 1_000_000_000 + Int64(nsec))
    }

    /// The rows a mirror copy put in for the session of the live file at
    /// `path` under other paths that are gone, while their mark still names
    /// their session (swe's `_copied_rows`). A copy never speaks for a
    /// session a live file carries, so the live file's commit takes them
    /// out. A row the index read from a live file has no mark and is never
    /// in this list.
    static func copiedRows(store: SearchStore,
                           marks: [TranscriptMirror.SessionIdentity: [(path: String, session: Int64)]],
                           harness: String, path: String) -> [SearchFileRow] {
        guard !marks.isEmpty, TranscriptMirror.harnesses.contains(harness),
              let marked = marks[TranscriptMirror.SessionIdentity(harness: harness, path: path)]
        else { return [] }
        var out: [SearchFileRow] = []
        for (other, session) in marked where other != path {
            if let row = try? store.fileRow(for: other), row.sessionRowID == session,
               !TranscriptMirror.exists(other) {
                out.append(row)
            }
        }
        return out
    }

    private struct Candidate {
        var pruned: TranscriptMirror.Pruned
        var stat: SearchFileStat
        var mtimeNs: Int64
    }

    /// Makes the sessions the harnesses pruned searchable from their mirror
    /// copies, without ever counting one twice or losing a row (swe's
    /// `_index_copies`, step by step). `live` is every `(harness, identity)`
    /// this scan's Claude and Codex enumeration carries.
    ///
    /// Per session identity no live source carries:
    /// 1. candidates are its copies whose source path is gone and known;
    ///    the newest (mtime, then path) is the one used;
    /// 2. every row of the session is fetched once per pass; a row at a
    ///    path that exists means the session is live where this scan does
    ///    not look, so it is left alone;
    /// 3. a row covering the copy settles it: nothing is read, and the
    ///    session's other rows that keeper dominates are retired in one
    ///    fenced commit;
    /// 4. a row the copy does not dominate leaves it alone;
    /// 5. otherwise the copy is read whole under its source path, staged
    ///    when it commits in pieces, and the session's rows under other
    ///    paths go in the final commit.
    static func indexCopies(indexer: SearchIndexer, mirror: TranscriptMirror,
                            live: Set<TranscriptMirror.SessionIdentity>,
                            shouldStop: () -> Bool = { false }) -> Counts {
        let store = indexer.store
        return indexCopies(indexer: indexer,
                           pruned: mirror.pruned(live: live, known: { (try? store.fileRow(for: $0)) != nil }),
                           shouldStop: shouldStop)
    }

    /// The same, over a candidate list a full scan already took
    /// (`TranscriptMirror.pruned`), so the mirror is walked once per scan.
    static func indexCopies(indexer: SearchIndexer, pruned: [TranscriptMirror.Pruned],
                            shouldStop: () -> Bool = { false }) -> Counts {
        var counts = Counts()
        let store = indexer.store
        var groups: [TranscriptMirror.SessionIdentity: [Candidate]] = [:]
        for copy in pruned {
            guard let stat = fileStat(copy.copy.path) else { continue }
            groups[copy.identity, default: []].append(Candidate(pruned: copy, stat: stat.stat,
                                                               mtimeNs: stat.mtimeNs))
        }
        guard !groups.isEmpty,
              let rowsByIdentity = try? store.sessionRows(agents: TranscriptMirror.harnesses)
        else { return counts }
        for identity in groups.keys.sorted() {
            if shouldStop() { return counts }
            guard let harness = SearchDecoder.Harness(rawValue: identity.harness),
                  let best = groups[identity]?.max(by: { a, b in
                      a.mtimeNs != b.mtimeNs ? a.mtimeNs < b.mtimeNs
                          : a.pruned.copy.path.utf8.lexicographicallyPrecedes(b.pruned.copy.path.utf8)
                  })
            else { continue }
            counts.files += 1
            let key = best.pruned.source
            let row = try? store.fileRow(for: key)
            let rows = rowsByIdentity[identity] ?? []
            // Live where this scan does not look.
            if rows.contains(where: { TranscriptMirror.exists($0.path) }) { continue }
            let keeper = rows.filter { covers($0, best.stat) }.max { a, b in
                if a.mtimeMs != b.mtimeMs { return a.mtimeMs < b.mtimeMs }
                if (a.sizeBytes ?? 0) != (b.sizeBytes ?? 0) { return (a.sizeBytes ?? 0) < (b.sizeBytes ?? 0) }
                return a.path.utf8.lexicographicallyPrecedes(b.path.utf8)
            }
            if let keeper {
                // Nothing to read. Rows of the session the keeper holds at
                // least as much as (a session that came back at another
                // path and was pruned again) go, so it is not counted twice.
                let spare = rows.filter { $0.path != keeper.path && dominates(rowStat(keeper), $0) }
                if !spare.isEmpty { _ = try? store.retire(keep: keeper, rows: spare) }
                counts.current += 1
                continue
            }
            if let row, SearchIndexer.readNeed(row: row, stat: best.stat) == .current {
                // The copy holds no session to file.
                counts.current += 1
                continue
            }
            let others = rows.filter { $0.path != key }
            if others.contains(where: { !dominates(best.stat, $0) })
                || row.map({ !dominates(best.stat, $0) }) == true {
                continue  // the copy cannot stand for a row it does not hold
            }
            switch indexer.indexCopy(path: key, copy: best.pruned.copy.path, harness: harness,
                                     stat: best.stat, row: row, drop: others) {
            case .indexed: counts.indexed += 1
            case .current: counts.current += 1
            case .moved: counts.moved += 1
            case .failed: counts.failed += 1
            case .skip: break
            }
        }
        return counts
    }
}
