import Foundation

/// Saved plays and hearts (plain JSONEncoder keys): item id -> start times, item id -> time of the heart.
private struct LocalListeningFile: Codable {
    var plays: [String: [Double]]
    var loved: [String: Double]
    /// The local queue's 30-day shuffle memory was imported once.
    var imported: Bool
}

/// No-server mode: every start (kept for good) and the hearts, for the auto-playlists (server.py listening.db).
/// LocalQueue writes it; LocalLibrary answers the list names (loved, most_played, forgotten, on_this_day) from it.
@MainActor
final class LocalListening {
    static let shared = LocalListening()

    private(set) var plays: [Int: [Double]] = [:]
    private(set) var loved: [Int: Double] = [:]
    private var imported = false

    private static let writer = DispatchQueue(label: "eu.mihten.atvplayer.local-listening")
    private static var fileURL: URL { LocalMedia.dataFolder.appendingPathComponent("local-listening.json") }

    private init() {
        load()
    }

    func isLoved(_ id: Int) -> Bool { loved[id] != nil }

    /// First start with this file: the plays the local queue remembers (last start per item, 30 days) become plays.
    func importHistory(_ played: [Int: Double]) {
        guard !imported else { return }
        imported = true
        for (id, at) in played { plays[id, default: []].append(at) }
        save()
        LocalLog.write("listening history: \(played.count) plays imported")
    }

    func notePlay(_ id: Int) {
        plays[id, default: []].append(Date().timeIntervalSince1970)
        save()
    }

    /// Heart on / off. False when nothing changed.
    func setLoved(_ id: Int, _ on: Bool) -> Bool {
        guard on != isLoved(id) else { return false }
        loved[id] = on ? Date().timeIntervalSince1970 : nil
        save()
        return true
    }

    /// Newest heart first.
    var lovedOrder: [Int] {
        loved.sorted { $0.value != $1.value ? $0.value > $1.value : $0.key > $1.key }.map(\.key)
    }

    /// item id -> last start.
    var lastPlays: [Int: Double] {
        var out: [Int: Double] = [:]
        for (id, times) in plays { if let last = times.max() { out[id] = last } }
        return out
    }

    /// Most starts first, ties by the last start.
    var mostPlayed: [Int] {
        let last = lastPlays
        return plays.filter { !$0.value.isEmpty }
            .sorted { a, b in
                if a.value.count != b.value.count { return a.value.count > b.value.count }
                return (last[a.key] ?? 0) > (last[b.key] ?? 0)
            }
            .map(\.key)
    }

    /// Played within 3 days of today's date in earlier years: newest year first, in a year the last played first
    /// (server.py on_this_day).
    func onThisDay(now: Date = Date()) -> [Int] {
        let calendar = Calendar.current
        guard let first = plays.values.compactMap({ $0.min() }).min() else { return [] }
        let today = calendar.dateComponents([.year, .month, .day], from: now)
        guard let thisYear = today.year, let month = today.month, let day = today.day else { return [] }
        let firstYear = calendar.component(.year, from: Date(timeIntervalSince1970: first))
        let limit = now.timeIntervalSince1970 - 300 * 86400  // never this year's plays
        var out: [Int] = []
        var seen: Set<Int> = []
        var year = thisYear - 1
        while year >= firstYear - 1 {
            defer { year -= 1 }
            // 29 February in a year without one: the calendar rolls it to 1 March, close enough for ± 3 days
            guard let target = calendar.date(from: DateComponents(year: year, month: month, day: day)),
                  let lo = calendar.date(byAdding: .day, value: -3, to: target),
                  let hi = calendar.date(byAdding: .day, value: 4, to: target) else { continue }
            let from = lo.timeIntervalSince1970
            let to = min(hi.timeIntervalSince1970, limit)
            var hits: [(id: Int, last: Double)] = []
            for (id, times) in plays {
                if let last = times.filter({ $0 >= from && $0 < to }).max() { hits.append((id: id, last: last)) }
            }
            for hit in hits.sorted(by: { $0.last > $1.last }) where !seen.contains(hit.id) {
                seen.insert(hit.id)
                out.append(hit.id)
            }
        }
        return out
    }

    /// Day number in the local time zone (the same "Not heard in a while" order all day).
    static func dayNumber(_ date: Date = Date()) -> Int {
        let local = date.timeIntervalSince1970 + Double(TimeZone.current.secondsFromGMT(for: date))
        return Int((local / 86400).rounded(.down))
    }

    private func load() {
        guard let data = try? Data(contentsOf: Self.fileURL),
              let saved = try? JSONDecoder().decode(LocalListeningFile.self, from: data) else { return }
        for (key, times) in saved.plays {
            if let id = Int(key) { plays[id] = times }
        }
        for (key, at) in saved.loved {
            if let id = Int(key) { loved[id] = at }
        }
        imported = saved.imported
    }

    private func save() {
        var playKeys: [String: [Double]] = [:]
        for (id, times) in plays { playKeys[String(id)] = times }
        var lovedKeys: [String: Double] = [:]
        for (id, at) in loved { lovedKeys[String(id)] = at }
        let file = LocalListeningFile(plays: playKeys, loved: lovedKeys, imported: imported)
        guard let data = try? JSONEncoder().encode(file) else { return }
        let url = Self.fileURL
        Self.writer.async { try? data.write(to: url, options: .atomic) }
    }
}

/// Seeded random numbers (SplitMix64): a shuffle that is the same for the same seed.
struct SeededGenerator: RandomNumberGenerator {
    private var state: UInt64

    init(seed: UInt64) {
        state = seed
    }

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
}
