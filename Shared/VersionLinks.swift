import Foundation

/// Song <-> music video versions on the device (no-server mode), the rules of server.py link_versions: same main
/// artist (without "feat. ...") and title without its "(...)" / "[...]" parts; label and class from the bracket text.
enum VersionLinks {
    private static let noise: Set<String> = ["official", "music", "video", "hd", "4k", "hq"]
    private static let bracket = try! NSRegularExpression(pattern: #"[\(\[]([^\)\]]*)[\)\]]"#)
    private static let feat = try! NSRegularExpression(pattern: #"\s+(?:feat\.|ft\.|featuring\s).*$"#,
                                                       options: .caseInsensitive)
    private static let bracketPart = try! NSRegularExpression(pattern: #"[\(\[].*?[\)\]]"#)
    private static let other = try! NSRegularExpression(pattern: "[^a-z0-9]+")
    private static let alphanumerics = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789")

    private static func replacing(_ regex: NSRegularExpression, in text: String, with template: String) -> String {
        regex.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: template)
    }

    /// scanner.norm: accents dropped, lower case, "(...)" / "[...]" parts dropped, other characters -> one space.
    static func norm(_ text: String) -> String {
        let scalars = text.decomposedStringWithCompatibilityMapping.unicodeScalars
            .filter { $0.properties.canonicalCombiningClass == .notReordered }
        var out = String(String.UnicodeScalarView(scalars)).lowercased()
        out = replacing(bracketPart, in: out, with: " ")
        out = replacing(other, in: out, with: " ")
        return out.trimmingCharacters(in: .whitespaces)
    }

    static func key(_ item: MediaItem) -> String? {
        let artist = norm(replacing(feat, in: item.artist ?? "", with: ""))
        let title = norm(replacing(feat, in: item.title, with: ""))
        return artist.isEmpty || title.isEmpty ? nil : artist + "\u{1F}" + title
    }

    /// Group key, label and class per item id, worked out again only when the item's artist, title or kind changes
    /// (LocalLibrary publishes the whole library every 25 tags read).
    final class Cache {
        fileprivate var entries: [Int: Entry] = [:]
    }

    fileprivate struct Entry {
        let artist: String?
        let title: String
        let kind: String
        let key: String?
        /// scanner.dedupe's key (artist + title without the feat. step): audio twins share it.
        let twin: String
        let label: String
        let cls: Int
    }

    private static func entry(_ item: MediaItem, _ cache: Cache?) -> Entry {
        if let hit = cache?.entries[item.id], hit.artist == item.artist, hit.title == item.title, hit.kind == item.kind {
            return hit
        }
        let info = label(item.title, kind: item.kind)
        let made = Entry(artist: item.artist, title: item.title, kind: item.kind, key: key(item),
                         twin: norm(item.artist ?? "") + "\u{1F}" + norm(item.title), label: info.label, cls: info.cls)
        cache?.entries[item.id] = made
        return made
    }

    /// Label and class (0 official / plain / remaster, 1 lyric / visualizer / audio, 2 live / acoustic / other).
    static func label(_ title: String, kind: String) -> (label: String, cls: Int) {
        var parts: [String] = []
        var official = false
        let ns = title as NSString
        for match in bracket.matches(in: title, range: NSRange(location: 0, length: ns.length)) {
            let words = ns.substring(with: match.range(at: 1)).split(whereSeparator: { $0.isWhitespace }).map(String.init)
            guard var lead = words.first?.lowercased() else { continue }
            while lead.hasSuffix(".") { lead.removeLast() }
            if ["feat", "ft", "featuring", "with"].contains(lead) { continue }
            if words.contains(where: { $0.lowercased() == "official" }) { official = true }
            let rest = words.filter { !noise.contains($0.lowercased()) }.joined(separator: " ")
            if !rest.isEmpty { parts.append(rest) }
        }
        var text = parts.joined(separator: " · ")
        let low = text.lowercased()
        let tokens = low.components(separatedBy: alphanumerics.inverted).filter { !$0.isEmpty }
        let cls: Int
        if tokens.contains(where: { $0 == "live" || $0.hasPrefix("acoustic") || $0.hasPrefix("unplugged") || $0.hasPrefix("remix") }) {
            cls = 2
        } else if tokens.contains(where: { $0 == "audio" || $0.hasPrefix("lyric") || $0.hasPrefix("visuali") }) {
            cls = 1
        } else if tokens.isEmpty || tokens.contains(where: { $0.hasPrefix("remaster") }) {
            cls = 0
        } else {
            cls = 2
        }
        if text.isEmpty {
            text = kind == "video" ? (official ? "Official video" : "Video") : "Song"
        } else if kind == "video" && (low == "lyric" || low == "lyrics") {
            text = "Lyric video"
        }
        return (String(text.prefix(1)).uppercased() + String(text.dropFirst()), cls)
    }

    static func versionClass(_ item: MediaItem) -> Int { label(item.title, kind: item.kind).cls }

    /// `items` (same order) with `videos` (songs) and `versions` (groups of 2+, songs first, then videos by class and
    /// label) filled in. dropTwins (no-server mode, which has no scanner.dedupe): an audio copy of a song already in the
    /// group (same artist and title, durations within 3 s; lossless, then the shorter path stays) is not listed as a
    /// version, but still gets the group's links (the device shows it in its folder and album).
    static func attach(_ items: [MediaItem], cache: Cache? = nil, dropTwins: Bool = false) -> [MediaItem] {
        let entries = items.map { entry($0, cache) }
        if let cache, cache.entries.count > items.count * 2 {  // ids gone from the library
            let ids = Set(items.map(\.id))
            cache.entries = cache.entries.filter { ids.contains($0.key) }
        }
        var groups: [String: [Int]] = [:]
        for (n, info) in entries.enumerated() {
            if let groupKey = info.key { groups[groupKey, default: []].append(n) }
        }
        var out = items
        for n in out.indices {
            out[n].videos = nil
            out[n].versions = nil
        }
        for group in groups.values where group.count > 1 {
            let twins = dropTwins ? twinsIn(group, items, entries) : []
            let members = twins.isEmpty ? group : group.filter { !twins.contains($0) }
            guard members.count > 1 else { continue }
            let rows: [(n: Int, label: String, cls: Int)] = members.map { n in
                (n: n, label: entries[n].label, cls: entries[n].cls)
            }
            func order(_ row: (n: Int, label: String, cls: Int)) -> (Int, Int, String, Int) {
                let item = items[row.n]
                return (item.kind == "audio" ? 0 : 1, row.cls, row.label.lowercased(), item.id)
            }
            let sorted = rows.sorted { order($0) < order($1) }
            let versions = sorted.map { row in
                MediaVersion(id: items[row.n].id, kind: items[row.n].kind, label: row.label, duration: items[row.n].duration)
            }
            let videoCount = versions.filter { $0.kind == "video" }.count
            for n in members + twins {
                out[n].versions = versions
                if out[n].kind == "audio" && videoCount > 0 { out[n].videos = videoCount }
            }
        }
        return out
    }

    /// scanner.dedupe on a group's songs (channels are not known on the device: treated as equal): the twins it drops.
    private static func twinsIn(_ members: [Int], _ items: [MediaItem], _ entries: [Entry]) -> [Int] {
        let songs = members.filter { items[$0].kind == "audio" }.sorted { a, b in
            let la = items[a].lossless == true, lb = items[b].lossless == true
            if la != lb { return la }
            return (items[a].localPath?.count ?? 0) < (items[b].localPath?.count ?? 0)
        }
        var kept: [Int] = []
        var twins: [Int] = []
        for n in songs {
            let isTwin = kept.contains { k in
                entries[k].twin == entries[n].twin && abs((items[k].duration ?? 0) - (items[n].duration ?? 0)) <= 3
            }
            if isTwin { twins.append(n) } else { kept.append(n) }
        }
        return twins
    }
}

extension ServerConnection {
    /// Plays another version of the current item in its queue slot; the queue owner keeps the position when both are
    /// the same cut.
    func switchVersion(to id: Int, position: Double) {
        command("switch_version", ["id": id, "position": position])
    }
}
