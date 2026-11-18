import Foundation

/// Reads newline-terminated lines from a byte offset in fixed-size chunks, so
/// a file of any size costs one chunk of memory plus the longest line kept.
///
/// Only complete lines are handed out. A trailing line without its newline
/// is still being written, so `endOffset` stops before it and the next scan
/// reads it whole. A line longer than `maxLineBytes` is skipped without being
/// buffered and counted in `skippedLines`.
struct JSONLReader {
    struct Result: Equatable {
        /// Offset just past the last complete line consumed.
        var endOffset: UInt64
        var skippedLines: Int
    }

    var chunkSize = 1 << 20
    var maxLineBytes = 64 << 20

    /// The plain bytes when `url` is a zstd frame, so a caller that
    /// fingerprints the file can hash those bytes. Nil for a live JSONL.
    func plainBytes(at url: URL) throws -> Data? {
        try ZstdFile.plain(at: url)
    }

    func read(_ url: URL, from offset: UInt64, line body: (Data) throws -> Void) throws -> Result {
        // A mirror copy whose harness deleted the original is a zstd
        // frame. Decompress it once and read the plain bytes; a live
        // JSONL is not a frame and is read from disk as before.
        if let plain = try plainBytes(at: url) {
            return try read(plain, from: offset, line: body)
        }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        try handle.seek(toOffset: offset)

        var chunkStart = offset
        var committed = offset
        var skipped = 0
        var pending = Data()
        // True while discarding the rest of an oversized line.
        var discarding = false

        // JSONSerialization hands back autoreleased objects; without a pool per
        // chunk they pile up until the whole file is done.
        while try autoreleasepool(invoking: { () throws -> Bool in
            let chunk = try handle.read(upToCount: chunkSize) ?? Data()
            if chunk.isEmpty { return false }
            var cursor = chunk.startIndex
            while cursor < chunk.endIndex {
                guard let newline = chunk[cursor...].firstIndex(of: UInt8(ascii: "\n")) else {
                    if !discarding {
                        pending.append(chunk[cursor...])
                        if pending.count > maxLineBytes {
                            pending = Data()
                            discarding = true
                        }
                    }
                    break
                }
                if discarding {
                    discarding = false
                    skipped += 1
                } else if pending.isEmpty {
                    if newline - cursor <= maxLineBytes {
                        try body(chunk[cursor..<newline])
                    } else {
                        skipped += 1
                    }
                } else {
                    pending.append(chunk[cursor..<newline])
                    if pending.count <= maxLineBytes { try body(pending) } else { skipped += 1 }
                    pending = Data()
                }
                committed = chunkStart + UInt64(newline - chunk.startIndex + 1)
                cursor = newline + 1
            }
            chunkStart += UInt64(chunk.count)
            return true
        }) {}
        return Result(endOffset: committed, skippedLines: skipped)
    }

    /// The same line walk over bytes already in memory. `endOffset` is a
    /// position in those bytes, which is what a cursor saved for a
    /// compressed copy records.
    func read(_ data: Data, from offset: UInt64, line body: (Data) throws -> Void) throws -> Result {
        var committed = offset
        var skipped = 0
        var index = Int(min(offset, UInt64(data.count)))
        while index < data.count {
            let rest = data[index...]
            guard let newline = rest.firstIndex(of: UInt8(ascii: "\n")) else { break }
            let line = data[index..<newline]
            if line.count <= maxLineBytes {
                try body(line)
            } else {
                skipped += 1
            }
            committed = UInt64(newline + 1)
            index = newline + 1
        }
        return Result(endOffset: committed, skippedLines: skipped)
    }
}
