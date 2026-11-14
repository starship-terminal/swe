import Foundation

// Kept identical in app/ios/Sources/SWERemote/Hub/ReplyStreams.swift
// and ported to pages/chat.js's `replyStreams` (docs/ported-logic.md).

/// One `message.delta`'s `data` (#1444): live text for a reply still
/// being written. The envelope carries no journal seq; `seq` here counts
/// this `id`'s deltas from 1.
public struct ReplyDelta: Decodable, Equatable, Sendable {
    public let id: String
    public let kind: String?
    public let text: String?
    public let seq: Int
    public let done: Bool?

    public init(id: String, kind: String? = "assistant", text: String?, seq: Int, done: Bool? = false) {
        self.id = id
        self.kind = kind
        self.text = text
        self.seq = seq
        self.done = done
    }
}

/// The streaming bubbles of one open chat, by the contract in
/// docs/json-contracts.md (`message.delta`):
///
/// - Deltas append per `id`, in `seq` order. A gap (or a stream first
///   heard mid-way) stops drawing that bubble, but it keeps its place in
///   line so its final message still finds it.
/// - The transcript doesn't carry the channel's id, so a live
///   `message.appended` claims the oldest open bubble of its kind
///   (`claim`). Deltas for a claimed id are ignored from then on. A
///   claimed bubble stays drawn until the final message is shown in its
///   place (`release`), so a reveal queue can hold the final for a beat.
/// - `done` closes the id; the bubble still waits for its final message.
///   One that never comes (a dropped stream) goes when the session goes
///   idle (`sweep`) or the stream reconnects (`clear`).
public struct ReplyStreams: Equatable, Sendable {
    public struct Bubble: Equatable, Sendable {
        public let id: String
        public let kind: String
        public var text: String
        public var seq: Int
        public var done: Bool
        /// A delta went missing: not drawn, kept in line for matching.
        public var broken: Bool
        /// A final message took it; shown until that message is.
        public var claimed: Bool
    }

    /// Every bubble not yet released, oldest first.
    public private(set) var bubbles: [Bubble] = []
    /// Ids claimed, broken off or cleared, newest last: their late deltas
    /// are dropped rather than starting a new bubble.
    private var ended: [String] = []
    static let endedLimit = 256

    public init() {}

    /// What to draw, oldest first: assistant text only. Other kinds keep
    /// their place for matching but aren't drawn (the hub streams only
    /// `assistant` for now).
    public var shown: [Bubble] {
        bubbles.filter { !$0.broken && $0.kind == "assistant" && !$0.text.isEmpty }
    }

    /// Ids still waiting for their final message.
    public var openIds: [String] { bubbles.filter { !$0.claimed }.map(\.id) }

    /// Takes one delta; returns whether what's drawn changed.
    @discardableResult
    public mutating func apply(_ d: ReplyDelta) -> Bool {
        guard !d.id.isEmpty, !ended.contains(d.id) else { return false }
        let before = shown
        if let i = bubbles.firstIndex(where: { $0.id == d.id }) {
            if d.done == true { bubbles[i].done = true }
            guard !bubbles[i].broken else { return false }
            if d.seq != bubbles[i].seq + 1 {
                // One went missing: stop drawing, wait for the final.
                bubbles[i].broken = true
                bubbles[i].text = ""
            } else {
                bubbles[i].text += d.text ?? ""
                bubbles[i].seq = d.seq
            }
        } else {
            bubbles.append(Bubble(id: d.id, kind: d.kind ?? "assistant", text: d.seq == 1 ? d.text ?? "" : "",
                                  seq: d.seq, done: d.done == true, broken: d.seq != 1, claimed: false))
        }
        return shown != before
    }

    /// A live `message.appended` of `kind` arrived: the id of the oldest
    /// open bubble of that kind it replaces, or nil.
    public mutating func claim(kind: String?) -> String? {
        let kind = kind ?? "assistant"
        guard let i = bubbles.firstIndex(where: { !$0.claimed && $0.kind == kind }) else { return nil }
        bubbles[i].claimed = true
        end(bubbles[i].id)
        return bubbles[i].id
    }

    /// The final message for `id` is on screen: the bubble goes.
    @discardableResult
    public mutating func release(_ id: String) -> Bool {
        let before = shown
        bubbles.removeAll { $0.id == id }
        return shown != before
    }

    /// The session went idle: of `ids` (the bubbles open when it did),
    /// those no final message claimed go. Claimed ones wait for theirs.
    @discardableResult
    public mutating func sweep(_ ids: [String]) -> Bool {
        let before = shown
        let gone = Set(ids)
        for b in bubbles where gone.contains(b.id) && !b.claimed { end(b.id) }
        bubbles.removeAll { gone.contains($0.id) && !$0.claimed }
        return shown != before
    }

    /// Reconnect, history reload or close: every bubble goes.
    @discardableResult
    public mutating func clear() -> Bool {
        let had = !shown.isEmpty
        for b in bubbles { end(b.id) }
        bubbles = []
        return had
    }

    private mutating func end(_ id: String) {
        guard !ended.contains(id) else { return }
        ended.append(id)
        if ended.count > Self.endedLimit { ended.removeFirst(ended.count - Self.endedLimit) }
    }
}
