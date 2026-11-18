import CZStd
import Foundation

/// A transcript file as bytes. A zstd frame (the mirror's copy of a
/// transcript whose harness deleted the original) comes back plain.
/// Anything else comes back as stored, so a live JSONL is unchanged.
enum ZstdFile {
    private static let magic = Data([0x28, 0xb5, 0x2f, 0xfd])

    /// The decoded length stored in a frame header, or nil when the file
    /// is not a frame or the header omits the length. Reads the header
    /// only, so an unchanged frame can be skipped without decoding it.
    static func contentSize(at url: URL) -> Int64? {
        let size = url.path.withCString { swe_zstd_content_size($0) }
        return size >= 0 ? size : nil
    }

    /// The plain bytes of a zstd frame, or nil when the file is not one.
    static func plain(at url: URL) throws -> Data? {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let head = try handle.read(upToCount: 4) ?? Data()
        guard head == magic else { return nil }
        var out: UnsafeMutablePointer<UInt8>?
        var count = 0
        let rc = url.path.withCString { swe_zstd_read_file($0, &out, &count) }
        guard rc == 0, let out else { throw CocoaError(.fileReadCorruptFile) }
        return Data(bytesNoCopy: out, count: count, deallocator: .free)
    }
}
