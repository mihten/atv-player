import Foundation

/// One entry of a downloaded list: the list's own item id, the version that is downloaded for it (server
/// /api/offline: `source`, the best audio of the item's group) and that file's revision (changes on a rescan).
struct OfflineEntry: Codable, Equatable, Hashable {
    let id: Int
    let source: Int
    let rev: String
}

/// Pure bookkeeping for the downloads (no files, no network): which files the lists need, what a list change fetches
/// and deletes. Checked by tools/offline_plan_check (CI, swiftc).
enum OfflinePlan {
    /// How many downloaded lists use each file (a file is listed once per list, also when the list holds it twice).
    static func references(_ lists: [[Int]]) -> [Int: Int] {
        var count: [Int: Int] = [:]
        for list in lists {
            for source in Set(list) { count[source, default: 0] += 1 }
        }
        return count
    }

    /// Files to delete when a list goes: the ones no other downloaded list still needs.
    static func orphans(removing list: [Int], keeping others: [[Int]]) -> Set<Int> {
        let needed = Set(others.flatMap { $0 })
        return Set(list).subtracting(needed)
    }

    /// A list changed on the server. fetch: files of the new list that are not downloaded or whose revision changed
    /// (first-appearance order, once each); remove: files of the old list that neither the new list nor another
    /// downloaded list needs. have: downloaded file -> its revision.
    static func diff(old: [OfflineEntry], new: [OfflineEntry], others: [[Int]],
                     have: [Int: String]) -> (fetch: [Int], remove: Set<Int>) {
        var fetch: [Int] = []
        var seen: Set<Int> = []
        for entry in new where !seen.contains(entry.source) {
            seen.insert(entry.source)
            if have[entry.source] != entry.rev { fetch.append(entry.source) }
        }
        let remove = orphans(removing: old.map(\.source), keeping: others + [new.map(\.source)])
        return (fetch, remove)
    }

    /// Bytes still to download for a list: entries not downloaded at that revision, each file once.
    static func bytesNeeded(_ entries: [OfflineEntry], sizes: [Int: Int64], have: [Int: String]) -> Int64 {
        var total: Int64 = 0
        var seen: Set<Int> = []
        for entry in entries where !seen.contains(entry.source) {
            seen.insert(entry.source)
            if have[entry.source] != entry.rev { total += sizes[entry.source] ?? 0 }
        }
        return total
    }

    /// Room to keep free on the phone besides the download.
    static let reserve: Int64 = 1_000_000_000

    static func fits(_ bytes: Int64, free: Int64) -> Bool { bytes + reserve <= free }

    /// "FLAC 24/48" from the server's X-Audio-Format ("flac 24/48000"); lossy "OPUS 48" / "MP3".
    static func formatLabel(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        let parts = raw.split(separator: " ")
        let codec = parts.first.map { $0.uppercased() } ?? ""
        guard parts.count > 1 else { return codec }
        let numbers = parts[1].split(separator: "/").map(String.init)
        func khz(_ text: String) -> String {
            guard let hz = Double(text), hz > 0 else { return text }
            let k = hz / 1000
            return k == k.rounded() ? String(Int(k)) : String(format: "%.1f", k)
        }
        if numbers.count == 2 { return "\(codec) \(numbers[0])/\(khz(numbers[1]))" }
        return "\(codec) \(khz(numbers[0]))"
    }
}
