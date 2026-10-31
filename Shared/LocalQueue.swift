import Foundation
import os

/// Saved queue (plain JSONEncoder keys). history: item id -> time it last started playing.
private struct LocalQueueFile: Codable {
    var queue: [Int]
    var original: [Int]
    var index: Int
    var shuffle: Bool
    var repeatMode: String
    var position: Double
    var history: [String: Double]
}

/// No-server mode: this device's queue on the device itself. A port of the server's Session for one player
/// (server.py set_queue, advance, handle_cmd, on_message, smart_shuffle). ServerConnection hands it the "cmd"
/// messages and the player's reports instead of sending them over the WebSocket.
@MainActor
final class LocalQueue {
    private weak var engine: PlayerEngine?
    private let publish: @MainActor (SessionState) -> Void
    private var queue: [Int] = []
    private var original: [Int] = []
    private var index = -1
    private var shuffle = false
    /// off | all | one; "all" by default like the server.
    private var repeatMode = "all"
    private var playing = false
    private var position: Double = 0
    private var duration: Double = 0
    private var errors = 0
    /// Item whose current start is already in the play history.
    private var noted: Int?
    private var played: [Int: Double] = [:]
    private var lastSave = Date.distantPast

    private static let historyDays: Double = 30
    private static var fileURL: URL { LocalMedia.dataFolder.appendingPathComponent("local-queue.json") }

    init(engine: PlayerEngine?, publish: @escaping @MainActor (SessionState) -> Void) {
        self.engine = engine
        self.publish = publish
    }

    private var library: LocalLibrary { LocalLibrary.shared }

    private var currentID: Int? { index >= 0 && index < queue.count ? queue[index] : nil }
    private var current: MediaItem? { currentID.flatMap { library.byID[$0] } }

    /// Restores the saved queue; the current item is loaded paused at its saved position.
    func start() {
        load()
        prune()
        broadcast()
        if let item = current {
            duration = item.duration ?? 0
            engine?.load(item, position: position, autoplay: false, present: false, force: true)
        } else {
            engine?.unload()
        }
        library.itemsChanged = { [weak self] in self?.libraryChanged() }
    }

    // MARK: - Commands (server.py handle_cmd)

    func handleCommand(_ msg: [String: Any]) {
        guard let cmd = msg["cmd"] as? String else { return }
        switch cmd {
        case "play_ids":
            setQueue(Self.ints(msg["ids"]), startIndex: Self.int(msg["start_index"]), shuffle: Self.bool(msg["shuffle"]))
        case "play_collection":
            let name = msg["collection"] as? String ?? "all"
            guard name != "playlist" else { return }  // playlists need the server
            let recursive = (msg["recursive"] as? Bool) != false
            let ids = library.collection(name, root: msg["root"] as? String, folder: msg["folder"] as? String,
                                         recursive: recursive).map(\.id)
            var start: Int?
            if let startIndex = Self.int(msg["start_index"]) {
                start = startIndex
            } else if let startID = Self.int(msg["start_id"]) {
                start = ids.firstIndex(of: startID)
            }
            LocalLog.write("play \(name): \(ids.count) items, shuffle \(Self.bool(msg["shuffle"]))")
            setQueue(ids, startIndex: start, shuffle: Self.bool(msg["shuffle"]))
        case "enqueue":
            let ids = Self.ints(msg["ids"]).filter { library.byID[$0] != nil }
            guard !ids.isEmpty else { return }
            if queue.isEmpty {
                setQueue(ids, startIndex: nil, shuffle: false)
                return
            }
            let at = (msg["mode"] as? String) == "next" ? index + 1 : queue.count
            queue.insert(contentsOf: ids, at: min(max(at, 0), queue.count))
            original.append(contentsOf: ids)
            changed()
        case "next":
            advance(user: true)
        case "prev":
            let at = Self.double(msg["position"]) ?? position
            if at > 5 || index <= 0 {
                engine?.control("seek", position: 0)
            } else {
                index -= 1
                startCurrent()
            }
        case "jump":
            let target = Self.int(msg["index"]) ?? -1
            if target >= 0 && target < queue.count {
                index = target
                startCurrent(present: true)
            }
        case "toggle", "play", "pause":
            engine?.control(cmd, position: nil)
        case "seek":
            engine?.control("seek", position: Self.double(msg["position"]) ?? 0)
        case "shuffle":
            let on = msg["on"].map { Self.bool($0) } ?? !shuffle
            let cur = currentID
            if on && !shuffle {
                let rest = smartShuffle(Array(queue.dropFirst(index + 1)))
                queue = Array(queue.prefix(index + 1)) + rest
            } else if !on && shuffle && !original.isEmpty {
                queue = original
                index = cur.flatMap { queue.firstIndex(of: $0) } ?? 0
            }
            shuffle = on
            changed()
        case "repeat":
            var mode = msg["mode"] as? String ?? ""
            if !["off", "all", "one"].contains(mode) {
                mode = ["off": "all", "all": "one", "one": "off"][repeatMode] ?? "all"
            }
            repeatMode = mode
            changed()
        case "remove":
            let target = Self.int(msg["index"]) ?? -1
            if target >= 0 && target < queue.count && target != index {
                queue.remove(at: target)
                if target < index { index -= 1 }
                changed()
            }
        case "clear":
            let cur = currentID
            queue = cur.map { [$0] } ?? []
            original = queue
            index = queue.isEmpty ? -1 : 0
            changed()
        case "rescan":
            library.scan()
        default:
            break
        }
    }

    // MARK: - Player reports (server.py on_message, role player)

    func playerMessage(_ msg: [String: Any]) {
        let type = msg["type"] as? String
        if type == "cmd" {
            handleCommand(msg)
            return
        }
        let item = Self.int(msg["item"])
        switch type ?? "" {
        case "status":
            guard let item, item == currentID else { return }
            let wasPlaying = playing
            playing = Self.bool(msg["playing"])
            position = Self.double(msg["position"]) ?? 0
            if let value = Self.double(msg["duration"]), value > 0 { duration = value }
            if playing {
                errors = 0
                if noted != item {
                    noted = item
                    notePlayed(item)
                }
            }
            // The last position survives the app being killed: saved on pause and at most every 10 s.
            if wasPlaying != playing || Date().timeIntervalSince(lastSave) >= 10 { save() }
            broadcast()
        case "ended":
            if let item, item == currentID { advance(user: false) }
        case "error":
            guard let item, item == currentID else { return }
            errors += 1
            LocalLog.write("player error on \(item): \(msg["message"] as? String ?? "")")
            if errors >= 3 {
                stopPlayback()
            } else {
                advance(user: false)
            }
        case "log":
            LocalLog.write(msg["message"] as? String ?? "")
        default:
            break
        }
    }

    // MARK: - Queue logic

    /// startIndex nil = beginning (or a random first item when shuffling).
    private func setQueue(_ raw: [Int], startIndex: Int?, shuffle on: Bool) {
        let ids = raw.filter { library.byID[$0] != nil }
        guard !ids.isEmpty else { return }
        let start = startIndex.map { min(max($0, 0), ids.count - 1) }
        original = ids
        if on {
            let rest = smartShuffle(ids.enumerated().filter { $0.offset != start }.map(\.element))
            queue = (start.map { [ids[$0]] } ?? []) + rest
            index = 0
        } else {
            queue = ids
            index = start ?? 0
        }
        shuffle = on
        errors = 0
        startCurrent(present: true)
    }

    private func advance(user: Bool) {
        guard !queue.isEmpty else { return }
        if repeatMode == "one" && !user {
            // same item again
        } else if index + 1 < queue.count {
            index += 1
        } else if repeatMode == "all" {
            if shuffle { queue = smartShuffle(queue) }  // new lap: the items just heard go to the back
            index = 0
        } else {
            index = queue.count - 1
            stopPlayback()
            return
        }
        startCurrent()
    }

    private func startCurrent(position start: Double = 0, autoplay: Bool = true, present: Bool = false) {
        guard let item = current else { return }
        position = start
        duration = item.duration ?? 0
        playing = false
        noted = nil
        save()
        engine?.load(item, position: start, autoplay: autoplay, present: present)
        broadcast()
    }

    private func stopPlayback() {
        playing = false
        engine?.control("stop", position: nil)
        save()
        broadcast()
    }

    private func changed() {
        save()
        broadcast()
    }

    /// Drops ids that vanished from the library (after a rescan); keeps the current item where it is.
    private func prune() {
        let known = library.byID
        let cur = currentID
        // Entries kept before the current one: the new index of the same entry (the queue may hold an id twice).
        let before = queue[..<min(max(index, 0), queue.count)].filter { known[$0] != nil }.count
        let kept = queue.filter { known[$0] != nil }
        let removed = kept.count != queue.count
        queue = kept
        original = original.filter { known[$0] != nil }
        if cur != nil && !removed {
            // nothing removed: the index still points at the same entry
        } else if let cur, known[cur] != nil {
            index = before
        } else if removed && !queue.isEmpty {
            index = min(before, queue.count - 1)
        } else if !queue.isEmpty {
            index = min(max(index, 0), queue.count - 1)
        } else {
            index = -1
        }
    }

    private func libraryChanged() {
        let before = (queue, index)
        prune()
        if queue != before.0 || index != before.1 { save() }
        broadcast()  // tags read meanwhile change titles and covers
    }

    /// Items not heard within 30 days first (random), then the recently heard ones split by age into an older and a
    /// newer half, each shuffled: the items heard last come last, also on a repeat-all lap.
    private func smartShuffle(_ ids: [Int]) -> [Int] {
        let cutoff = Date().timeIntervalSince1970 - Self.historyDays * 86400
        var fresh: [Int] = []
        var recent: [Int] = []
        for id in ids {
            if (played[id] ?? 0) > cutoff { recent.append(id) } else { fresh.append(id) }
        }
        fresh.shuffle()
        recent.sort { (played[$0] ?? 0) < (played[$1] ?? 0) }
        let half = recent.count / 2
        var older = Array(recent[..<half])
        var newer = Array(recent[half...])
        older.shuffle()
        newer.shuffle()
        return fresh + older + newer
    }

    private func notePlayed(_ id: Int) {
        let now = Date().timeIntervalSince1970
        played[id] = now
        let cutoff = now - Self.historyDays * 86400
        played = played.filter { $0.value > cutoff }
        save()
    }

    private func broadcast() {
        let upcoming = queue.dropFirst(index + 1).prefix(5).compactMap { library.byID[$0] }
        publish(SessionState(queue: queue, index: index, shuffle: shuffle, repeatMode: repeatMode, playing: playing,
                             position: position, duration: duration, current: current, upcoming: upcoming,
                             players: 1, version: nil))
    }

    // MARK: - Persistence

    private func load() {
        guard let data = try? Data(contentsOf: Self.fileURL),
              let saved = try? JSONDecoder().decode(LocalQueueFile.self, from: data) else { return }
        queue = saved.queue
        original = saved.original
        index = saved.index
        shuffle = saved.shuffle
        repeatMode = ["off", "all", "one"].contains(saved.repeatMode) ? saved.repeatMode : "all"
        position = saved.position
        let cutoff = Date().timeIntervalSince1970 - Self.historyDays * 86400
        played = [:]
        for (key, value) in saved.history where value > cutoff {
            if let id = Int(key) { played[id] = value }
        }
    }

    private func save() {
        lastSave = Date()
        var history: [String: Double] = [:]
        for (id, value) in played { history[String(id)] = value }
        let file = LocalQueueFile(queue: queue, original: original, index: index, shuffle: shuffle,
                                  repeatMode: repeatMode, position: position, history: history)
        guard let data = try? JSONEncoder().encode(file) else { return }
        try? data.write(to: Self.fileURL, options: .atomic)
    }

    // MARK: - JSON values (numbers may arrive as Int, Double or NSNumber)

    private static func int(_ value: Any?) -> Int? {
        guard let value else { return nil }
        if let number = value as? Int { return number }
        if let number = value as? Double { return number.isFinite ? Int(number) : nil }
        if let number = value as? NSNumber { return number.intValue }
        if let text = value as? String { return Int(text) }
        return nil
    }

    private static func double(_ value: Any?) -> Double? {
        guard let value else { return nil }
        if let number = value as? Double { return number }
        if let number = value as? Int { return Double(number) }
        if let number = value as? NSNumber { return number.doubleValue }
        if let text = value as? String { return Double(text) }
        return nil
    }

    /// Python truthiness, like the server's bool(...).
    private static func bool(_ value: Any?) -> Bool {
        guard let value else { return false }
        if let flag = value as? Bool { return flag }
        if let number = int(value) { return number != 0 }
        if let text = value as? String { return !text.isEmpty }
        return false
    }

    private static func ints(_ value: Any?) -> [Int] {
        if let list = value as? [Int] { return list }
        if let list = value as? [Any] { return list.compactMap { int($0) } }
        return []
    }
}

/// No-server mode: the player's log lines (normally sent to the server log) go to the system log and to a small
/// rotating file, Caches/ATVPlayer/player-log.txt (max ~512 KB, one older file kept).
@MainActor
enum LocalLog {
    private static let logger = Logger(subsystem: "eu.mihten.atvplayer", category: "player")
    private static let limit = 512 * 1024
    private static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    static var fileURL: URL { LocalMedia.cacheFolder.appendingPathComponent("player-log.txt") }

    static func write(_ message: String) {
        let text = String(message.prefix(1500))
        logger.info("\(text, privacy: .public)")
        let line = Data("\(stamp.string(from: Date())) \(text)\n".utf8)
        let fm = FileManager.default
        let url = fileURL
        let size = (try? fm.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        if size > limit {
            let older = url.deletingLastPathComponent().appendingPathComponent("player-log.1.txt")
            try? fm.removeItem(at: older)
            try? fm.moveItem(at: url, to: older)
        }
        if let handle = try? FileHandle(forWritingTo: url) {
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: line)
            try? handle.close()
        } else {
            try? line.write(to: url)
        }
    }
}
