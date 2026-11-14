import Foundation

/// What `swe session restore <id> --json` says it did: the mirror copy it
/// read and the source path it put the transcript back at
/// (docs/json-contracts.md).
public struct RestoredTranscript: Decodable, Equatable, Sendable {
    public var harness: String
    public var sessionId: String
    /// "" or nil for the harness's own home, else the account home.
    public var accountHome: String?
    /// The mirror copy read (the one the listing shows).
    public var copy: String
    /// The source path the manifest recorded, which now holds the file.
    public var target: String
    public var bytes: Int
    /// `cloned` or `copied`.
    public var how: String
}

struct RestorePayload: Decodable {
    var restored: RestoredTranscript
}

/// Restoring a mirrored session (#1918): the bundled `swe` puts the copy
/// back where its harness kept it, so the session lists live and resumes
/// again. swe owns every write; the app builds the command, runs it off
/// the main actor, and says what came back. Purging a copy stays CLI-only.
public enum SessionRestore {
    /// `swe session restore <id> --account=<id|default> --json`. The
    /// account picks the home when one id has rows in two: `default` for
    /// the harness's own home, else the `<id>` in
    /// `~/.swe/accounts/<vendor>/<id>/home`.
    public static func arguments(id: String, accountHome: String?, defaultHomes: Set<String> = []) -> [String] {
        ["session", "restore", id, "--account=\(account(accountHome, defaultHomes: defaultHomes))", "--json"]
    }

    static func account(_ home: String?, defaultHomes: Set<String>) -> String {
        guard let home, !home.isEmpty, !defaultHomes.contains(home) else { return "default" }
        let url = URL(fileURLWithPath: home)
        // `.../accounts/<vendor>/<id>/home`
        return url.lastPathComponent == "home" ? url.deletingLastPathComponent().lastPathComponent : home
    }

    /// The line the page shows once swe restored it.
    public static func success(_ restored: RestoredTranscript) -> String {
        "Restored the transcript to \((restored.target as NSString).abbreviatingWithTildeInPath). "
            + "The session lists as live again and can be resumed."
    }

    /// What a failed restore says: swe's own refusal as it printed it, or
    /// plainly that this swe has no restore command yet.
    public static func failure(_ error: Error) -> String {
        let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        if message.contains("Unknown argument: restore") || message.contains("Unknown option: restore")
            || message.contains("Unknown command") {
            return "This swe can't restore transcripts yet. Update swe, then try again."
        }
        return "Couldn't restore: \(message)"
    }
}

extension SweClient {
    /// Runs the restore. `@concurrent` through `call`, so the child and the
    /// decode never run on the main actor.
    public func restoreSession(id: String, accountHome: String?,
                               defaultHomes: Set<String> = []) async throws -> RestoredTranscript {
        let payload: RestorePayload = try await call(
            SessionRestore.arguments(id: id, accountHome: accountHome, defaultHomes: defaultHomes))
        return payload.restored
    }
}
