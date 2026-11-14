import Foundation

/// A mirrored session (#1918): Claude Code or Codex deleted its transcript
/// and swe kept a read-only copy, so the hub still lists it. The row
/// carries `mirrored: true` with no resume command; it can't be attached,
/// continued, forked or sent to. The wording matches the web app
/// (`sessions.js`, `pages/chat.js`).
enum Mirrored {
    /// The tag in the list row.
    static let tag = "Mirrored"
    /// The chat's one-line notice, and the tag's VoiceOver hint.
    static let notice = "Its transcript was deleted by the harness. swe kept a read-only copy."
    /// What the chat shows when the copy can't be read (yet).
    static let chatUnavailable = "This session's chat is not available yet."
    /// The disabled composer's placeholder.
    static let composerPlaceholder = "Read-only: a copy of a deleted transcript"
    /// What VoiceOver says for the list tag.
    static let tagAccessibility = "Mirrored. \(notice)"
}

/// What a session list row draws besides its title. A live session shows
/// its status and whether the phone holds it; a mirrored one has neither
/// (no process to describe), only the tag. Pure, so it is tested without a view.
struct SessionRowMarks: Equatable {
    /// The "Mirrored" tag, or nil.
    var tag: String?
    /// The status dot and the status word ("idle", "needs you").
    var showsStatus: Bool
    /// The attached or watching-only mark.
    var showsOwnership: Bool

    init(_ row: SessionRow) {
        let mirrored = row.isMirrored
        tag = mirrored ? Mirrored.tag : nil
        showsStatus = !mirrored
        showsOwnership = !mirrored
    }
}

/// What fills the bottom of an open chat.
enum ChatBar: Equatable {
    /// The prompt box: attached and live.
    case composer
    /// "Watching only", with Attach when the harness allows it.
    case attachCard
    /// A disabled prompt box with the reason as its placeholder.
    case mirroredComposer

    /// `mirrored` wins over `attached`: a mirrored copy is read-only even
    /// when the supervisor still holds an attached entry for the key.
    static func of(_ row: SessionRow?) -> ChatBar {
        if row?.isMirrored == true { return .mirroredComposer }
        return row?.isAttached == true ? .composer : .attachCard
    }
}

/// What the transcript area shows when there is nothing to list.
enum ChatEmpty: Equatable {
    case skeleton
    case error(String)
    case noMessages
    /// A mirrored session whose copy can't be read: calm, no error styling.
    case mirroredUnavailable

    /// Nil while there is a transcript (or live output) to draw.
    static func of(row: SessionRow?, loading: Bool, loadError: String?, hasItems: Bool, hasStreaming: Bool) -> ChatEmpty? {
        if loading { return .skeleton }
        guard !hasItems else { return nil }
        if row?.isMirrored == true { return .mirroredUnavailable }
        if let loadError { return .error(loadError) }
        return hasStreaming ? nil : .noMessages
    }
}
