import Foundation

/// The editable fields of a `.claude-plugin/plugin.json` manifest as one
/// flat form. `apply(to:)` writes them back through `JSONTextEdit`, so
/// every other key, the key order and the file's layout are left alone:
/// only the fields the form showed change.
public struct PluginManifestFields: Sendable, Equatable {
    public var description = ""
    public var version = ""
    public var authorName = ""
    public var authorEmail = ""
    public var authorURL = ""
    public var homepage = ""
    public var repository = ""
    public var license = ""
    /// Keywords separated by ", ".
    public var keywords = ""

    public init() {}

    /// Read the fields out of manifest text; absent fields stay empty.
    public init(parsing text: String) {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        description = object["description"] as? String ?? ""
        version = object["version"] as? String ?? ""
        homepage = object["homepage"] as? String ?? ""
        repository = object["repository"] as? String ?? ""
        license = object["license"] as? String ?? ""
        keywords = (object["keywords"] as? [String] ?? []).joined(separator: ", ")
        if let author = object["author"] as? [String: Any] {
            authorName = author["name"] as? String ?? ""
            authorEmail = author["email"] as? String ?? ""
            authorURL = author["url"] as? String ?? ""
        } else if let author = object["author"] as? String {
            authorName = author
        }
    }

    /// "not a version", "not a URL": what stops a save.
    public var problems: [String] {
        var problems: [String] = []
        if !version.isEmpty, !PluginManifestFields.isVersion(version) {
            problems.append("Version should look like 1.2.3.")
        }
        for (label, url) in [("Homepage", homepage), ("Repository", repository), ("Author URL", authorURL)] {
            if !url.isEmpty, !PluginManifestFields.isURL(url) {
                problems.append("\(label) is not a URL.")
            }
        }
        return problems
    }

    /// `1.2.3` with an optional suffix.
    public static func isVersion(_ version: String) -> Bool {
        version.range(of: #"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$"#, options: .regularExpression) != nil
    }

    public static func isURL(_ string: String) -> Bool {
        guard let url = URL(string: string) else { return false }
        return url.scheme != nil && url.host != nil
    }

    /// The manifest text with the fields this form changed applied. A
    /// field still reading as it did in `text` is not written, so a key the
    /// file lacks isn't added empty and a value the form can't show (a
    /// `repository` object, a string `author`) stays as it is. A field
    /// cleared on purpose is written empty, matching how `swe` writes them.
    public func apply(to text: String) throws -> String {
        let before = PluginManifestFields(parsing: text)
        var text = text
        for (value, old, key) in [(description, before.description, "description"),
                                  (version, before.version, "version"),
                                  (homepage, before.homepage, "homepage"),
                                  (repository, before.repository, "repository"),
                                  (license, before.license, "license")] where value != old {
            try set(value, at: [key], in: &text)
        }
        let words = Self.keywordList(keywords)
        if words != Self.keywordList(before.keywords) {
            try set(words, at: ["keywords"], in: &text)
        }
        let nameChanged = authorName != before.authorName
        let detailChanged = authorEmail != before.authorEmail || authorURL != before.authorURL
        if Self.authorIsString(text), !detailChanged {
            // `"author": "Name"` stays a string when only the name moves.
            if nameChanged { try set(authorName, at: ["author"], in: &text) }
        } else {
            // A string author becoming an object takes its name along.
            if nameChanged || (detailChanged && Self.authorIsString(text)) {
                try set(authorName, at: ["author", "name"], in: &text)
            }
            if authorEmail != before.authorEmail { try set(authorEmail, at: ["author", "email"], in: &text) }
            if authorURL != before.authorURL { try set(authorURL, at: ["author", "url"], in: &text) }
        }
        return text
    }

    static func keywordList(_ keywords: String) -> [String] {
        keywords.components(separatedBy: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    private static func authorIsString(_ text: String) -> Bool {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return object["author"] is String
    }

    private func set(_ value: Any, at path: [String], in text: inout String) throws {
        text = try JSONTextEdit.set(value, at: path, in: text)
    }

    /// Write `interface.displayName` into a `.codex-plugin/plugin.json`,
    /// keeping every other key. Codex documents the field; Claude's files
    /// do not get keys they do not know.
    public static func setCodexDisplayName(_ name: String, in text: String) throws -> String {
        try JSONTextEdit.set(name, at: ["interface", "displayName"], in: text)
    }
}

/// The `---`-fenced frontmatter of a markdown file, kept as raw lines.
/// A key whose value is a string the form can show is an editable field:
/// a `key: value` line, a block scalar (`|` or `>`, with `-`/`+`
/// chomping and an indentation digit), or a plain value carried over
/// indented lines. Anything else (comments, lists, nested maps, a quoted
/// value over several lines) stays byte for byte so nothing the harness
/// relies on is rewritten. Only strings: this is not a YAML parser.
///
/// Lines end in LF or CRLF; each keeps its own ending, and a rewritten
/// field takes the ending of the line it replaces.
public struct MarkdownFrontmatter: Sendable, Equatable {
    /// A key whose string value the form can show and write back.
    public struct Field: Sendable, Equatable {
        public var key: String
        public var value: String
    }

    public var fields: [Field]
    /// Lines inside the fences that are not editable fields.
    public var other: [String]
    /// Keys whose value is in a form the form can't edit (a nested map, a
    /// list, a quoted value over several lines): `update` leaves them be.
    public var lockedKeys: [String]

    /// The frontmatter of `text`, or nil when the file has none.
    public init?(text: String) {
        guard let found = MarkdownFrontmatter.range(in: text) else { return nil }
        let lines = MarkdownFrontmatter.lines(text[found.range]).map(\.text)
        fields = []
        other = []
        lockedKeys = []
        for entry in MarkdownFrontmatter.entries(lines) {
            if let key = entry.key, let value = entry.value {
                fields.append(Field(key: key, value: value))
            } else {
                other.append(contentsOf: lines[entry.lines])
                if let key = entry.key { lockedKeys.append(key) }
            }
        }
    }

    // MARK: Lines and entries

    /// One line of the frontmatter and the ending that closes it ("\n" or
    /// "\r\n"; Swift reads CRLF as one Character).
    private struct Line {
        var text: String
        var end: String
    }

    /// `inside` split into lines, each with its ending. The text between
    /// the fences always ends in one, so joining them gives it back.
    private static func lines(_ inside: Substring) -> [Line] {
        var out: [Line] = []
        var current = ""
        for ch in inside {
            if ch == "\n" || ch == "\r\n" {
                out.append(Line(text: current, end: String(ch)))
                current = ""
            } else {
                current.append(ch)
            }
        }
        if !current.isEmpty { out.append(Line(text: current, end: "")) }
        return out
    }

    /// One key and the lines it spans, or one line that is not a key.
    private struct Entry {
        var key: String?
        var lines: Range<Int>
        /// What the form shows; nil when the value is in a form it can't edit.
        var value: String?
        /// The comment after the value or block header (" # note"), kept
        /// when the value changes.
        var comment = ""
        /// A multi-line value's indentation, reused when it is written again.
        var indent: Int?
    }

    private static func isBlank(_ line: String) -> Bool {
        line.allSatisfy { $0 == " " || $0 == "\t" }
    }

    private static func isIndented(_ line: String) -> Bool {
        line.first == " " || line.first == "\t"
    }

    private static func leadingSpaces(_ line: some StringProtocol) -> Int {
        line.prefix { $0 == " " }.count
    }

    /// The frontmatter's lines as keys and their extents, in file order.
    private static func entries(_ lines: [String]) -> [Entry] {
        var out: [Entry] = []
        var i = 0
        while i < lines.count {
            guard let (key, raw) = parseField(lines[i]) else {
                out.append(Entry(key: nil, lines: i..<i + 1))
                i += 1
                continue
            }
            // What hangs off the key: indented lines, and blank lines
            // between them. Blank lines after the last one are not its.
            var end = i + 1
            var j = i + 1
            while j < lines.count, isBlank(lines[j]) || isIndented(lines[j]) {
                if !isBlank(lines[j]) { end = j + 1 }
                j += 1
            }
            let entry: Entry
            if let header = BlockHeader(raw) {
                let block = decodeBlock(lines, from: i + 1, header: header)
                // An indented line the block did not take (indented less
                // than its content) is YAML the form can't read. The block's
                // own rules decide its extent: a line of only spaces deeper
                // than its indentation is its content, though blank above.
                entry = block.contentEnd >= end
                    ? Entry(key: key, lines: i..<block.consumed, value: block.value,
                            comment: header.comment, indent: block.indent)
                    : Entry(key: key, lines: i..<end)
            } else if end == i + 1 {
                let (value, comment) = parts(raw)
                entry = Entry(key: key, lines: i..<i + 1, value: value, comment: comment)
            } else if let plain = plainLines(raw, Array(lines[(i + 1)..<end])) {
                entry = Entry(key: key, lines: i..<end, value: plain.value, indent: plain.indent)
            } else {
                entry = Entry(key: key, lines: i..<end)
            }
            out.append(entry)
            i = entry.lines.upperBound
        }
        return out
    }

    // MARK: Block scalars

    /// A block scalar's header: `|` or `>`, then at most one chomping
    /// indicator and one indentation digit in either order, then an
    /// optional comment.
    private struct BlockHeader {
        enum Chomp { case clip, strip, keep }
        var folded: Bool
        var chomp = Chomp.clip
        var indent: Int?
        var comment = ""

        init?(_ raw: String) {
            guard let first = raw.first, first == "|" || first == ">" else { return nil }
            folded = first == ">"
            var rest = Substring(raw.dropFirst())
            var chompSet = false
            for _ in 0..<2 {
                guard let ch = rest.first else { break }
                if !chompSet, ch == "-" || ch == "+" {
                    chomp = ch == "-" ? .strip : .keep
                    chompSet = true
                } else if indent == nil, let digit = ch.wholeNumberValue, ch.isASCII, digit >= 1 {
                    indent = digit
                } else {
                    break
                }
                rest = rest.dropFirst()
            }
            if !rest.isEmpty {
                guard rest.range(of: #"^[ \t]+#"#, options: .regularExpression) != nil else { return nil }
                comment = String(rest)
            }
        }
    }

    /// A line of only spaces, no deeper than `indent`: an empty line of
    /// the block. A deeper one is content (spaces a literal keeps).
    private static func isBreak(_ line: String, _ indent: Int) -> Bool {
        line.allSatisfy { $0 == " " } && line.count <= indent
    }

    private static func isContent(_ line: String, _ indent: Int) -> Bool {
        leadingSpaces(line) >= indent && !isBreak(line, indent)
    }

    /// The block scalar starting at `start`, read the way PyYAML and
    /// libyaml read one under a top-level key: its value, the line after
    /// its last content line, the line after the empty lines that follow
    /// (a `+` block keeps them), and the content's indentation.
    private static func decodeBlock(_ lines: [String], from start: Int, header: BlockHeader)
        -> (value: String, contentEnd: Int, consumed: Int, indent: Int) {
        var k = start
        var breaks = 0
        let indent: Int
        if let explicit = header.indent {
            indent = explicit
            while k < lines.count, isBreak(lines[k], indent) { breaks += 1; k += 1 }
        } else {
            // The deepest of the leading empty lines and the first
            // content line sets the indentation.
            var deepest = 0
            while k < lines.count, lines[k].allSatisfy({ $0 == " " }) {
                deepest = max(deepest, lines[k].count)
                breaks += 1
                k += 1
            }
            if k < lines.count { deepest = max(deepest, leadingSpaces(lines[k])) }
            indent = max(1, deepest)
        }
        var value = ""
        var lineBreak = ""
        var contentEnd = start
        while k < lines.count, isContent(lines[k], indent) {
            value += String(repeating: "\n", count: breaks)
            let text = lines[k].dropFirst(indent)
            let leadingNonSpace = !(text.first == " " || text.first == "\t")
            value += text
            lineBreak = "\n"
            k += 1
            contentEnd = k
            breaks = 0
            while k < lines.count, isBreak(lines[k], indent) { breaks += 1; k += 1 }
            guard k < lines.count, isContent(lines[k], indent) else { break }
            let next = lines[k].dropFirst(indent)
            if header.folded, leadingNonSpace, !(next.first == " " || next.first == "\t") {
                // Folded: a break between two plain lines is a space, and
                // only the empty lines between them stay as breaks.
                if breaks == 0 { value += " " }
            } else {
                value += lineBreak
            }
        }
        switch header.chomp {
        case .strip: break
        case .clip: value += lineBreak
        case .keep: value += lineBreak + String(repeating: "\n", count: breaks)
        }
        return (value, contentEnd, header.chomp == .keep ? k : contentEnd, indent)
    }

    /// A plain value carried over indented lines (`raw` on the key line,
    /// or empty when the value starts on the next one), folded as YAML
    /// folds it: lines join with a space, an empty line is a break. Nil
    /// for anything that reads as a list, a map, a comment or a non-string
    /// YAML value instead.
    private static func plainLines(_ raw: String, _ rest: [String]) -> (value: String, indent: Int)? {
        func startsPlain(_ s: String) -> Bool {
            guard let first = s.first else { return false }
            if "\"'[]{}&*!|>%@`#,".contains(first) { return false }
            if s == "-" || s == "?" || s == ":" { return false }
            return !(s.hasPrefix("- ") || s.hasPrefix("? ") || s.hasPrefix(": "))
        }
        // A list item, a key or a comment on a line of its own is not
        // more of the value, whatever YAML would make of it.
        func continues(_ s: String) -> Bool {
            !s.hasPrefix("#") && !s.contains(": ") && !s.contains(" #") && !s.hasSuffix(":")
                && !s.contains("\t#") && !s.hasPrefix("- ") && s != "-"
        }
        if !raw.isEmpty {
            guard startsPlain(raw), continues(raw), parts(raw).comment.isEmpty else { return nil }
        }
        var value = raw
        var blanks = 0
        var indent: Int?
        for line in rest {
            if isBlank(line) {
                blanks += 1
                continue
            }
            // Tabs never indent YAML.
            guard line.first == " " else { return nil }
            let text = line.trimmingCharacters(in: .whitespaces)
            guard continues(text) else { return nil }
            if indent == nil { indent = leadingSpaces(line) }
            if value.isEmpty {
                guard startsPlain(text) else { return nil }
                value = text
            } else {
                value += blanks == 0 ? " " + text : String(repeating: "\n", count: blanks) + text
            }
            blanks = 0
        }
        guard let indent, !value.isEmpty else { return nil }
        return (value, indent)
    }

    // MARK: Writing

    /// The lines that write `key: value`: one line when the value has no
    /// break (or can't go in a block), else a `|` block at `indent`.
    private static func render(_ key: String, _ value: String, comment: String, indent: Int)
        -> (lines: [String], block: Bool, keeps: Bool) {
        if value.contains("\n"), let block = literal(value, indent: indent) {
            return (["\(key): \(block.header)\(comment)"] + block.lines, true, block.header.hasSuffix("+"))
        }
        return ([(value.isEmpty ? key + ":" : key + ": " + scalar(value)) + comment], false, false)
    }

    /// `value` as a literal block: the header (`|`, then the indentation
    /// digit when the first line starts with a space, then the chomping
    /// that gives back its trailing breaks) and the indented lines. Nil
    /// when a block can't hold it exactly: a value of only breaks, a
    /// carriage return or other control character, a line YAML 1.1 reads
    /// as a break, or a `---` line, which would close the frontmatter.
    static func literal(_ value: String, indent: Int) -> (header: String, lines: [String])? {
        let unsafe: Set<Unicode.Scalar> = ["\r", "\u{85}", "\u{2028}", "\u{2029}", "\u{FEFF}"]
        guard !value.unicodeScalars.contains(where: {
            unsafe.contains($0) || ($0.properties.generalCategory == .control && $0 != "\n" && $0 != "\t")
        }) else { return nil }
        var body = Substring(value)
        var trailing = 0
        while body.hasSuffix("\n") {
            body = body.dropLast()
            trailing += 1
        }
        let rows = body.split(separator: "\n", omittingEmptySubsequences: false)
        guard let first = rows.first(where: { !$0.isEmpty }),
              !rows.contains(where: { $0.trimmingCharacters(in: .whitespaces) == "---" })
        else { return nil }
        let indent = (1...9).contains(indent) ? indent : 2
        let pad = String(repeating: " ", count: indent)
        let chomp = trailing == 0 ? "-" : trailing == 1 ? "" : "+"
        let header = "|" + (first.first == " " ? String(indent) : "") + chomp
        let lines = rows.map { $0.isEmpty ? "" : pad + $0 }
            + Array(repeating: "", count: max(0, trailing - 1))
        // Read it back the way the form will: a block that would not give
        // back exactly `value`, or would lock the field, is not written.
        let back = entries(["k: " + header] + lines)
        guard back.count == 1, back[0].value == value else { return nil }
        return (header, lines)
    }

    /// A YAML scalar's text as the form shows it: a double-quoted value
    /// has its escapes read, a single-quoted one its `''`, and a plain
    /// value is as written. A trailing ` # comment` is not part of it.
    static func unquote(_ raw: String) -> String { parts(raw).value }

    /// `raw` split into the value YAML reads and the trailing comment it
    /// ignores (with the blanks before the `#`; "" when there is none). A
    /// quoted value YAML can't read (unclosed, a bad escape, text after
    /// the closing quote) comes back whole, so nothing is lost.
    static func parts(_ raw: String) -> (value: String, comment: String) {
        // Scalars, not Characters: a combining mark after a quote would
        // otherwise fuse with it and hide the quote.
        let chars = Array(raw.unicodeScalars)
        guard let quote = chars.first, quote == "\"" || quote == "'" else {
            // Plain: a `#` at the start or after a blank starts a comment
            // (parseField already took the blank before a leading one).
            if raw.hasPrefix("#") { return ("", " " + raw) }
            guard let hash = raw.range(of: #"[ \t]+#"#, options: .regularExpression)
            else { return (raw, "") }
            return (String(raw[..<hash.lowerBound]), String(raw[hash.lowerBound...]))
        }
        var out = String.UnicodeScalarView()
        var i = 1
        var closed = false
        while i < chars.count {
            let ch = chars[i]
            if ch == quote {
                if quote == "'", i + 1 < chars.count, chars[i + 1] == "'" {
                    out.append(quote)
                    i += 2
                    continue
                }
                closed = true
                i += 1
                break
            }
            if quote == "\"", ch == "\\" {
                guard let (text, used) = escape(chars, at: i + 1) else { return (raw, "") }
                out.append(contentsOf: text.unicodeScalars)
                i += 1 + used
                continue
            }
            out.append(ch)
            i += 1
        }
        let rest = String(String.UnicodeScalarView(chars[i...]))
        guard closed else { return (raw, "") }
        guard rest.isEmpty || rest.range(of: #"^[ \t]+#"#, options: .regularExpression) != nil
        else { return (raw, "") }
        return (String(out), rest)
    }

    /// The text a double-quoted YAML escape stands for, and how many
    /// characters after the backslash it used; nil when YAML rejects it.
    private static func escape(_ chars: [Unicode.Scalar], at i: Int) -> (String, Int)? {
        guard i < chars.count else { return nil }
        let simple: [Unicode.Scalar: String] = [
            "0": "\0", "a": "\u{07}", "b": "\u{08}", "t": "\t", "\t": "\t", "n": "\n",
            "v": "\u{0B}", "f": "\u{0C}", "r": "\r", "e": "\u{1B}", " ": " ",
            "\"": "\"", "/": "/", "\\": "\\", "N": "\u{85}", "_": "\u{A0}",
            "L": "\u{2028}", "P": "\u{2029}",
        ]
        if let text = simple[chars[i]] { return (text, 1) }
        let digits: [Unicode.Scalar: Int] = ["x": 2, "u": 4, "U": 8]
        guard let count = digits[chars[i]], i + count < chars.count,
              let code = UInt32(String(String.UnicodeScalarView(chars[(i + 1)...(i + count)])), radix: 16)
        else { return nil }
        if let scalar = Unicode.Scalar(code) { return (String(scalar), 1 + count) }
        // A UTF-16 surrogate pair written as two `\u` escapes.
        guard chars[i] == "u", (0xD800...0xDBFF).contains(code), i + 10 < chars.count,
              chars[i + 5] == "\\", chars[i + 6] == "u",
              let low = UInt32(String(String.UnicodeScalarView(chars[(i + 7)...(i + 10)])), radix: 16),
              (0xDC00...0xDFFF).contains(low),
              let pair = Unicode.Scalar(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
        else { return nil }
        return (String(pair), 11)
    }

    /// `value` as a YAML scalar: plain when YAML reads it back as this
    /// same string, else double-quoted. A plain `Fix: the bug`, `#tag`,
    /// `yes` or `1.0` would read as a mapping, a comment, a bool or a
    /// number, and a harness would get the wrong description.
    static func scalar(_ value: String) -> String {
        guard !value.isEmpty else { return value }
        let indicators = Set("-?:,[]{}#&*!|>'\"%@`")
        let reserved: Set = ["true", "false", "yes", "no", "on", "off", "y", "n",
                             "null", "~", ".inf", "-.inf", "+.inf", ".nan"]
        let plain = !indicators.contains(value.first!)
            && !(value.first!.isWhitespace || value.last!.isWhitespace)
            && value.last != ":"
            && !value.contains(": ") && !value.contains(" #")
            && !value.unicodeScalars.contains { $0.properties.generalCategory == .control }
            && !reserved.contains(value.lowercased())
            && value.range(of: #"^[-+]?(\.[0-9]|[0-9])"#, options: .regularExpression) == nil
        guard !plain else { return value }
        var out = "\""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\\": out += "\\\\"
            case "\"": out += "\\\""
            case "\n": out += "\\n"
            case "\t": out += "\\t"
            case "\r": out += "\\r"
            default:
                if scalar.properties.generalCategory == .control {
                    out += String(format: "\\u%04X", scalar.value)
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        return out + "\""
    }

    /// `(key, value)` for a `key: value` line, else nil.
    private static func parseField(_ line: String) -> (key: String, value: String)? {
        guard let colon = line.firstIndex(of: ":") else { return nil }
        let key = line[line.startIndex..<colon]
        guard !key.isEmpty, !key.contains(" "), !key.contains("\t"),
              key.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" })
        else { return nil }
        var value = line[line.index(after: colon)...]
        if value.first == " " { value = value.dropFirst() }
        return (String(key), String(value).trimmingCharacters(in: .whitespaces))
    }

    /// The frontmatter's range inside `text` (between the fences), and
    /// the ending of the opening fence's line, for lines the form adds.
    private static func range(in text: String) -> (range: Range<String.Index>, end: String)? {
        guard text.hasPrefix("---") else { return nil }
        func isEnd(_ ch: Character) -> Bool { ch == "\n" || ch == "\r\n" }
        // The first line must be exactly "---" (or "---" then end).
        guard let firstEnd = text.firstIndex(where: isEnd),
              text[text.startIndex..<firstEnd].trimmingCharacters(in: .whitespaces) == "---"
        else { return nil }
        let index = text.index(after: firstEnd)
        var lineStart = index
        while lineStart < text.endIndex {
            let lineEnd = text[lineStart...].firstIndex(where: isEnd) ?? text.endIndex
            let line = text[lineStart..<lineEnd].trimmingCharacters(in: .whitespaces)
            if line == "---" { return (index..<lineStart, String(text[firstEnd])) }
            lineStart = lineEnd < text.endIndex ? text.index(after: lineEnd) : text.endIndex
        }
        return nil
    }

    /// `text` with `values`' keys set, in the order given: a field keeps
    /// its lines when its value is unchanged and is rewritten in place
    /// when not (a value with a break as a `|` block at the field's own
    /// indentation, 2 when new), a key the file lacks is added before the
    /// closing fence, and every other byte stays. With no frontmatter one
    /// is created at the top.
    public static func update(_ text: String, set values: [(key: String, value: String)]) -> String {
        guard let found = range(in: text) else {
            let end = text.contains("\r\n") ? "\r\n" : "\n"
            let block = values.flatMap { render($0.key, $0.value, comment: "", indent: 2).lines }
            return (["---"] + block + ["---", ""]).joined(separator: end) + end + text
        }
        let lines = Self.lines(text[found.range])
        var out: [Line] = []
        var left = values
        let all = entries(lines.map(\.text))
        var next = 0
        while next < all.count {
            let entry = all[next]
            next += 1
            let original = lines[entry.lines]
            guard let key = entry.key,
                  let at = left.firstIndex(where: { $0.key == key }) else {
                out += original
                continue
            }
            // Consume the key either way so a complex field is never
            // duplicated: a key the file already has in a form the form
            // can't show stays exactly as it is. Unchanged keeps its
            // lines, quoting and comment included; a changed value keeps
            // its comment.
            let value = left.remove(at: at).value
            guard let old = entry.value, value != old else {
                out += original
                continue
            }
            let end = original.first?.end ?? found.end
            let rendered = render(key, value, comment: entry.comment, indent: entry.indent ?? 2)
            out += rendered.lines.map { Line(text: $0, end: end) }
            guard rendered.block else { continue }
            // A block reads on over the blank lines after it: a `+` block
            // would take them into its value, and a line of spaces deeper
            // than its indentation is content to any block. Take them in
            // here: a `+` block's own empty lines already end it, and other
            // blocks get them back as plain empty lines.
            var blanks: [Line] = []
            while next < all.count, all[next].key == nil, isBlank(lines[all[next].lines.lowerBound].text) {
                blanks.append(lines[all[next].lines.lowerBound])
                next += 1
            }
            if !rendered.keeps { out += blanks.map { Line(text: "", end: $0.end) } }
        }
        // Before the closing fence.
        out += left.flatMap { render($0.key, $0.value, comment: "", indent: 2).lines }
            .map { Line(text: $0, end: found.end) }
        return String(text[..<found.range.lowerBound])
            + out.map { $0.text + $0.end }.joined()
            + String(text[found.range.upperBound...])
    }
}
