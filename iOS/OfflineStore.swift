import Combine
import Foundation
import UIKit

/// A downloaded list: its name and item order as a snapshot (Offline plays it without the server).
struct OfflineList: Codable, Identifiable, Equatable {
    var key: String  // "loved", "most_played", "playlist:<id>"
    var name: String
    var collection: String
    var playlist: Int?
    var stamp: String
    var entries: [OfflineEntry]

    var id: String { key }
    var sources: [Int] { entries.map(\.source) }
    /// Loved first, then Most played, then playlists A–Z.
    var rank: Int { key == "loved" ? 0 : key == "most_played" ? 1 : 2 }
    var displayName: String { key == "loved" ? "♥ \(name)" : name }

    static func key(collection: String, playlist: Int?) -> String {
        playlist.map { "playlist:\($0)" } ?? collection
    }
}

/// One downloaded file (by the id of the version that is downloaded).
struct OfflineFile: Codable {
    let id: Int
    /// Revision the lists want (server /api/offline rev) and the one on disk.
    var rev: String
    var downloadedRev: String?
    /// The item as the server sent it; played as audio only (OfflineStore.playable).
    var item: MediaItem
    /// File names in LocalMedia.offlineFolder; audio nil = not downloaded yet.
    var audio: String?
    var art: String?
    var lyrics: String?
    /// Bytes on disk, or the expected size until downloaded.
    var size: Int64
    /// Server X-Audio-Format, e.g. "flac 24/48000".
    var format: String?
    var failures = 0

    var isCurrent: Bool { audio != nil && downloadedRev == rev }
}

private struct OfflineManifest: Codable {
    var lists: [OfflineList] = []
    var files: [String: OfflineFile] = [:]
    /// ♥ changes made offline, sent with the `love` command on the next Media server connection.
    var pendingLoves: [String: Bool] = [:]
}

/// Server /api/offline/<collection> (server.py offline_list).
struct OfflineReply: Decodable {
    struct Row: Decodable {
        let id: Int
        let source: Int
        let rev: String
        let size: Int64?
        let sizeExact: Bool?
        let format: String?
        let item: MediaItem
    }

    let collection: String
    let playlist: Int?
    let name: String
    let stamp: String
    let items: [Row]

    var entries: [OfflineEntry] { items.map { OfflineEntry(id: $0.id, source: $0.source, rev: $0.rev) } }
}

/// What turning a list on would download (shown before it starts).
struct OfflinePlanInfo {
    let reply: OfflineReply
    let files: Int
    let bytes: Int64
    let estimated: Bool
    let free: Int64

    var fits: Bool { OfflinePlan.fits(bytes, free: free) }
}

struct OfflineError: LocalizedError {
    let errorDescription: String?
    init(_ text: String) { errorDescription = text }
}

/// iPhone downloads for the Offline library (task 22): lists turned on in Media server mode are downloaded as audio
/// only, best version (/media/<id>?audio=best), in a background URLSession, into LocalMedia.offlineFolder. While
/// connected in Media server mode the lists are kept current. In Offline mode (Config.offlineMode) this is the
/// LocalQueue item source and answers the API requests; nothing talks to the server then.
@MainActor
final class OfflineStore: NSObject, ObservableObject {
    static let shared = OfflineStore()
    nonisolated static let onlyWiFiKey = "offlineOnlyWiFi"
    nonisolated static let sessionID = "eu.mihten.atvplayer.offline"

    /// Downloaded lists, Loved first, then Most played, then playlists A–Z.
    @Published private(set) var lists: [OfflineList] = []
    /// Every downloaded item (playable), artist / title order.
    @Published private(set) var items: [MediaItem] = []
    /// Bumped when the downloads change (screens reload).
    @Published private(set) var version = 0
    /// Files with a download running.
    @Published private(set) var downloading: Set<Int> = []
    /// Lists being checked against the server.
    @Published private(set) var busy: Set<String> = []
    @Published var onlyWiFi: Bool = UserDefaults.standard.object(forKey: "offlineOnlyWiFi") as? Bool ?? true {
        didSet {
            UserDefaults.standard.set(onlyWiFi, forKey: Self.onlyWiFiKey)
            restartDownloads()
        }
    }

    private(set) var byID: [Int: MediaItem] = [:]
    var itemsChanged: (@MainActor () -> Void)?
    /// The system woke the app for finished background downloads (PhoneAppDelegate).
    var backgroundCompletion: (() -> Void)?

    private var manifest = OfflineManifest()
    private var session: URLSession!
    private var resumeData: [Int: Data] = [:]
    /// Session task numbers whose end was reported: a reconcile snapshot taken before that never counts them as running.
    private var ended: Set<Int> = []
    private weak var connection: ServerConnection?
    private var watchers: Set<AnyCancellable> = []
    private var syncTask: Task<Void, Never>?
    private var syncing = false
    private var syncAgain = false

    private static var folder: URL { LocalMedia.offlineFolder }
    private static var manifestURL: URL { folder.appendingPathComponent("manifest.json") }
    nonisolated private static var incoming: URL { LocalMedia.offlineFolder.appendingPathComponent("incoming") }

    override private init() {
        super.init()
        load()
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        config.httpMaximumConnectionsPerHost = 2  // one or two files at a time
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
        OfflineHooks.source = self
        OfflineHooks.api = { [weak self] url in self?.respond(url) }
        rebuild()
    }

    // MARK: - Wiring

    /// PhoneApp: follow the connection (sync on connect, after list / ♥ changes pushed by the server).
    func attach(_ connection: ServerConnection) {
        guard self.connection !== connection else { return }
        self.connection = connection
        watchers = []
        connection.$connected.removeDuplicates().sink { [weak self] up in
            guard up else { return }
            Task { @MainActor in self?.scheduleSync(after: 1) }
        }.store(in: &watchers)
        connection.$playlistsVersion.dropFirst().sink { [weak self] _ in
            Task { @MainActor in self?.scheduleSync(after: 2) }
        }.store(in: &watchers)
        connection.$autoVersion.dropFirst().sink { [weak self] _ in
            Task { @MainActor in self?.scheduleSync(after: 2) }
        }.store(in: &watchers)
        modeChanged()
    }

    /// Library mode changed: downloads run only in Media server mode (Offline makes no requests at all).
    func modeChanged() {
        if Config.localMode {
            pauseDownloads()
        } else {
            scheduleSync(after: 1)  // the sync starts the downloads once connected
        }
    }

    private var serverMode: Bool { !Config.localMode }

    private func log(_ message: String) {
        if let connection {
            connection.send(["type": "log", "message": "offline: " + message])
        } else {
            LocalLog.write("offline: " + message)
        }
    }

    // MARK: - Queries for the screens

    func isOn(_ key: String) -> Bool { manifest.lists.contains { $0.key == key } }

    func list(_ key: String) -> OfflineList? { manifest.lists.first { $0.key == key } }

    func file(_ id: Int) -> OfflineFile? { manifest.files[String(id)] }

    /// Items of a list in its order (downloaded ones only).
    func items(of key: String) -> [MediaItem] {
        (list(key)?.sources ?? []).compactMap { byID[$0] }
    }

    struct Status {
        let total: Int
        let done: Int
        let failed: Int
        let bytes: Int64
        /// Offline / No server: nothing downloads.
        var paused = false

        var text: String {
            if done == total { return "Up to date" }
            if paused { return "\(done) of \(total) downloaded · paused (Media server only)" }
            if failed > 0 && done + failed == total { return "\(failed) failed – Retry" }
            let base = "Downloading \(done) of \(total)"
            return failed > 0 ? "\(base) · \(failed) failed" : base
        }
    }

    func status(_ list: OfflineList) -> Status {
        let ids = Set(list.sources)
        var done = 0, failed = 0
        var bytes: Int64 = 0
        for id in ids {
            guard let f = file(id) else { continue }
            if f.audio != nil { bytes += f.size }
            if f.isCurrent { done += 1 } else if f.failures >= 3 { failed += 1 }
        }
        return Status(total: ids.count, done: done, failed: failed, bytes: bytes, paused: Config.localMode)
    }

    /// Bytes of everything in the downloads folder.
    func usedBytes() -> Int64 {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: Self.folder.path) else { return 0 }
        var total: Int64 = 0
        for name in names {
            let attributes = try? fm.attributesOfItem(atPath: Self.folder.appendingPathComponent(name).path)
            total += (attributes?[.size] as? NSNumber)?.int64Value ?? 0
        }
        return total
    }

    static func freeBytes() -> Int64 {
        let values = try? URL(fileURLWithPath: NSHomeDirectory())
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage ?? 0
    }

    // MARK: - Turning lists on and off

    /// What the list needs (sizes from the server) before anything is downloaded.
    func plan(collection: String, playlist: Int?) async throws -> OfflinePlanInfo {
        guard serverMode else { throw OfflineError("Downloads need the media server.") }
        let reply = try await fetchList(collection: collection, playlist: playlist)
        let entries = reply.entries
        var sizes: [Int: Int64] = [:]
        var estimated = false
        for row in reply.items {
            sizes[row.source] = row.size ?? 0
            if row.sizeExact != true { estimated = true }
        }
        return OfflinePlanInfo(reply: reply, files: Set(entries.map(\.source)).count,
                               bytes: OfflinePlan.bytesNeeded(entries, sizes: sizes, have: haveRevs),
                               estimated: estimated, free: Self.freeBytes())
    }

    func enable(_ plan: OfflinePlanInfo) {
        let key = OfflineList.key(collection: plan.reply.collection, playlist: plan.reply.playlist)
        log("list '\(plan.reply.name)' on: \(plan.files) files, \(plan.bytes / 1_000_000) MB")
        apply(plan.reply, key: key)
    }

    /// Off: the list goes; its files are deleted unless another downloaded list needs them.
    func disable(_ key: String) {
        guard let gone = list(key) else { return }
        manifest.lists.removeAll { $0.key == key }
        let orphans = OfflinePlan.orphans(removing: gone.sources, keeping: manifest.lists.map(\.sources))
        for id in orphans { deleteFile(id) }
        log("list '\(gone.name)' off: \(orphans.count) files deleted")
        save()
        rebuild()
    }

    func removeAll() {
        for list in manifest.lists { disable(list.key) }
        session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
        manifest.files = [:]
        resumeData = [:]
        if let names = try? FileManager.default.contentsOfDirectory(atPath: Self.folder.path) {
            for name in names where name != "manifest.json" {
                try? FileManager.default.removeItem(at: Self.folder.appendingPathComponent(name))
            }
        }
        save()
        rebuild()
    }

    /// "3 failed – Retry": try the list's failed files again.
    func retry(_ key: String) {
        for id in Set(list(key)?.sources ?? []) {
            guard var f = file(id), !f.isCurrent else { continue }
            f.failures = 0
            manifest.files[String(id)] = f
        }
        save()
        rebuild()
        reconcile()
    }

    // MARK: - Keeping lists current

    private func fetchList(collection: String, playlist: Int?) async throws -> OfflineReply {
        var query: [String: String] = [:]
        if let playlist { query["playlist"] = String(playlist) }
        let (data, response) = try await Net.data(from: .api("api/offline/\(collection)", query))
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 404 { throw OfflineError("This list is not on the server (or the server is older than 0.16.9).") }
        guard code == 200 else { throw OfflineError("The server answered \(code).") }
        return try JSONDecoder.server().decode(OfflineReply.self, from: data)
    }

    func scheduleSync(after seconds: Double) {
        syncTask?.cancel()
        syncTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.sync()
        }
    }

    /// Media server mode, connected: send the ♥ changes made offline, then update every list whose stamp changed.
    func sync() async {
        guard serverMode, connection?.connected == true else { return }
        if syncing {
            syncAgain = true
            return
        }
        syncing = true
        defer { syncing = false }
        repeat {
            syncAgain = false
            flushLoves()
            for list in manifest.lists {
                guard serverMode else { return }
                busy.insert(list.key)
                defer { busy.remove(list.key) }
                do {
                    let reply = try await fetchList(collection: list.collection, playlist: list.playlist)
                    guard self.list(list.key) != nil else { continue }  // turned off meanwhile
                    let missing = Set(reply.entries.map(\.source)).contains { file($0)?.isCurrent != true }
                    if reply.stamp != self.list(list.key)?.stamp || missing || reply.name != list.name {
                        apply(reply, key: list.key)
                    } else {
                        refreshItems(reply)  // ♥ / loudness changes do not change the stamp
                    }
                } catch {
                    log("list '\(list.name)' not checked: \(error.localizedDescription)")
                }
            }
            reconcile()
        } while syncAgain
    }

    /// Stores the list's new snapshot, fetches new / changed files and deletes the ones no list needs any more.
    private func apply(_ reply: OfflineReply, key: String) {
        let entries = reply.entries
        let old = list(key)?.entries ?? []
        let others = manifest.lists.filter { $0.key != key }.map(\.sources)
        let change = OfflinePlan.diff(old: old, new: entries, others: others, have: haveRevs)
        let needed = OfflinePlan.bytesNeeded(entries, sizes: Dictionary(reply.items.map { ($0.source, $0.size ?? 0) },
                                                                        uniquingKeysWith: { a, _ in a }),
                                             have: haveRevs)
        if !old.isEmpty, !OfflinePlan.fits(needed, free: Self.freeBytes()) {
            log("list '\(reply.name)' not updated: \(needed / 1_000_000) MB needed, not enough space")
            return
        }
        for row in reply.items {
            if var f = file(row.source) {
                if f.rev != row.rev { f.failures = 0 }  // a new file version gets fresh tries; "failed" stays failed
                f.rev = row.rev
                f.item = row.item
                if f.audio == nil { f.size = row.size ?? 0 }
                manifest.files[String(row.source)] = f
            } else {
                manifest.files[String(row.source)] = OfflineFile(id: row.source, rev: row.rev, item: row.item,
                                                                 size: row.size ?? 0, format: row.format)
            }
        }
        let snapshot = OfflineList(key: key, name: reply.name, collection: reply.collection, playlist: reply.playlist,
                                   stamp: reply.stamp, entries: entries)
        if let at = manifest.lists.firstIndex(where: { $0.key == key }) {
            manifest.lists[at] = snapshot
        } else {
            manifest.lists.append(snapshot)
        }
        for id in change.remove { deleteFile(id) }
        if !old.isEmpty && (!change.fetch.isEmpty || !change.remove.isEmpty) {
            log("list '\(reply.name)' changed: \(change.fetch.count) to fetch, \(change.remove.count) deleted")
        }
        save()
        rebuild()
        var extras: [Int] = []
        for id in entries.map(\.source) where !extras.contains(id) {
            if file(id)?.art == nil || change.fetch.contains(id) { extras.append(id) }
        }
        Task { await fetchExtras(extras) }
        reconcile()
    }

    /// The stored items from a list reply (no download): the server's current ♥, loudness, tags.
    private func refreshItems(_ reply: OfflineReply) {
        var changed = false
        for row in reply.items {
            guard var f = file(row.source), f.item != row.item else { continue }
            f.item = row.item
            manifest.files[String(row.source)] = f
            changed = true
        }
        if changed {
            save()
            rebuild()
        }
    }

    /// Cover and lyrics (small: fetched right away, not in the background session).
    private func fetchExtras(_ ids: [Int]) async {
        for id in ids {
            guard serverMode, let start = file(id) else { continue }
            var art = start.art, lyrics = start.lyrics
            if art == nil || !start.isCurrent,
               let (data, response) = try? await Net.data(from: .api("art/\(id)")),
               (response as? HTTPURLResponse)?.statusCode == 200, !data.isEmpty {
                let name = "\(id).jpg"
                if file(id) != nil, (try? data.write(to: Self.folder.appendingPathComponent(name), options: .atomic)) != nil { art = name }
            }
            if start.item.lyr != 0, lyrics == nil || !start.isCurrent,
               let (data, response) = try? await Net.data(from: .api("api/lyrics/\(id)")),
               (response as? HTTPURLResponse)?.statusCode == 200,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let tracks = json["tracks"] as? [Any], !tracks.isEmpty {
                let name = "\(id).lyrics.json"
                if file(id) != nil, (try? data.write(to: Self.folder.appendingPathComponent(name), options: .atomic)) != nil { lyrics = name }
            }
            guard var f = file(id) else { continue }  // removed meanwhile
            f.art = art
            f.lyrics = lyrics
            manifest.files[String(id)] = f
        }
        save()
        rebuild()
    }

    private func flushLoves() {
        guard serverMode, let connection, connection.connected, !manifest.pendingLoves.isEmpty else { return }
        for (key, on) in manifest.pendingLoves {
            guard let id = Int(key) else { continue }
            connection.command("love", ["id": id, "on": on])
        }
        log("\(manifest.pendingLoves.count) ♥ changes made offline sent")
        manifest.pendingLoves = [:]
        save()
    }

    private var haveRevs: [Int: String] {
        var out: [Int: String] = [:]
        for f in manifest.files.values where f.audio != nil { out[f.id] = f.downloadedRev }
        return out
    }

    private func deleteFile(_ id: Int) {
        guard let f = manifest.files.removeValue(forKey: String(id)) else { return }
        for name in [f.audio, f.art, f.lyrics].compactMap({ $0 }) {
            try? FileManager.default.removeItem(at: Self.folder.appendingPathComponent(name))
        }
        resumeData[id] = nil
        let tag = "\(id)|"
        session.getAllTasks { tasks in
            for task in tasks where task.taskDescription?.hasPrefix(tag) == true { task.cancel() }
        }
    }

    // MARK: - Background downloads

    /// Starts a download for every file that needs one and has none running (also after a relaunch).
    func reconcile() {
        guard serverMode, connection?.connected == true else { return }
        session.getAllTasks { tasks in
            let live = tasks.filter { $0.state == .running || $0.state == .suspended }
                .compactMap { task in task.taskDescription.flatMap(Self.taskID).map { (task.taskIdentifier, $0) } }
            Task { @MainActor in
                let store = OfflineStore.shared
                let running = Set(live.filter { !store.ended.contains($0.0) }.map(\.1))
                store.downloading.formUnion(running)  // after a relaunch; never drops a task started meanwhile
                guard store.serverMode else { return }
                let wanted = Set(store.manifest.lists.flatMap(\.sources))
                for id in wanted.sorted() where !running.contains(id) {
                    guard let f = store.file(id), !f.isCurrent, f.failures < 3 else { continue }
                    store.startDownload(id)
                }
            }
        }
    }

    private func startDownload(_ id: Int) {
        guard serverMode, let f = file(id), !downloading.contains(id) else { return }
        let task: URLSessionDownloadTask
        if let data = resumeData.removeValue(forKey: id) {
            task = session.downloadTask(withResumeData: data)
        } else {
            var request = Net.request(.api("media/\(id)", ["audio": "best"]))
            request.allowsCellularAccess = !onlyWiFi
            request.timeoutInterval = 120
            task = session.downloadTask(with: request)
        }
        task.taskDescription = "\(id)|\(f.rev)"
        downloading.insert(id)
        task.resume()
    }

    nonisolated private static func taskID(_ description: String) -> Int? {
        Int(description.split(separator: "|").first ?? "")
    }

    /// Offline mode: stop every download, keeping what arrived so far (resume data).
    private func pauseDownloads() {
        session.getAllTasks { tasks in
            for case let task as URLSessionDownloadTask in tasks {
                let id = task.taskDescription.flatMap(Self.taskID)
                task.cancel(byProducingResumeData: { data in
                    guard let id, let data else { return }
                    Task { @MainActor in OfflineStore.shared.resumeData[id] = data }
                })
            }
        }
        downloading = []
    }

    /// The Wi-Fi switch changed: running downloads start again with the new setting.
    private func restartDownloads() {
        session.getAllTasks { tasks in
            tasks.forEach { $0.cancel() }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                OfflineStore.shared.downloading = []
                OfflineStore.shared.resumeData = [:]
                OfflineStore.shared.reconcile()
            }
        }
    }

    fileprivate func finished(_ description: String, task: Int, status: Int, staged: URL?, format: String?,
                              contentType: String?, source: String?) {
        ended.insert(task)
        guard let id = Self.taskID(description) else { return }
        let rev = description.split(separator: "|", maxSplits: 1).dropFirst().first.map(String.init)
        downloading.remove(id)
        guard var f = file(id) else {  // the list went meanwhile
            if let staged { try? FileManager.default.removeItem(at: staged) }
            return
        }
        guard (200..<300).contains(status), let staged else {
            if let staged { try? FileManager.default.removeItem(at: staged) }
            failed(id, resume: nil, why: "HTTP \(status)")
            return
        }
        let name = "\(id).\(Self.fileExtension(contentType, f.item))"
        let fm = FileManager.default
        if let old = f.audio, old != name { try? fm.removeItem(at: Self.folder.appendingPathComponent(old)) }
        let dest = Self.folder.appendingPathComponent(name)
        try? fm.removeItem(at: dest)
        do {
            try fm.moveItem(at: staged, to: dest)
        } catch {
            failed(id, resume: nil, why: "file not stored: \(error.localizedDescription)")
            return
        }
        f.audio = name
        f.downloadedRev = rev ?? f.rev
        f.size = ((try? fm.attributesOfItem(atPath: dest.path))?[.size] as? NSNumber)?.int64Value ?? f.size
        f.format = format ?? f.format
        f.failures = 0
        manifest.files[String(id)] = f
        if let source, source != String(id) { log("\(id): server sent version \(source)") }
        save()
        rebuild()
    }

    fileprivate func completed(_ description: String, task: Int, error: Bool, cancelled: Bool, resume: Data?) {
        ended.insert(task)
        guard let id = Self.taskID(description) else { return }
        downloading.remove(id)
        guard error else {
            // downloaded, but the list wants a newer version meanwhile (an HTTP failure retries on its own delay)
            if let f = file(id), f.audio != nil, !f.isCurrent, f.failures < 3 { reconcile() }
            return
        }
        if cancelled {  // paused / Wi-Fi switch / removed
            if let resume { resumeData[id] = resume }
            return
        }
        failed(id, resume: resume, why: "network error")
    }

    /// Up to 3 tries per file (resumed where it stopped), then "failed – Retry".
    private func failed(_ id: Int, resume: Data?, why: String) {
        guard var f = file(id) else { return }
        f.failures += 1
        manifest.files[String(id)] = f
        save()
        rebuild()
        log("\(id) '\(f.item.title)' failed (\(why)), try \(f.failures) of 3")
        guard f.failures < 3 else { return }
        if let resume { resumeData[id] = resume }
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(5 * f.failures) * 1_000_000_000)
            self?.startDownload(id)
        }
    }

    private static func fileExtension(_ contentType: String?, _ item: MediaItem) -> String {
        let type = (contentType ?? "").lowercased()
        let known: [(String, String)] = [("flac", "flac"), ("matroska", "mka"), ("mpeg", "mp3"), ("mp4", "m4a"),
                                         ("x-m4a", "m4a"), ("ogg", "ogg"), ("opus", "opus"), ("wav", "wav"),
                                         ("wavpack", "wv"), ("aiff", "aiff"), ("aac", "aac")]
        if let hit = known.first(where: { type.contains($0.0) }) { return hit.1 }
        if item.kind == "video" { return "mka" }
        return (item.acodec ?? item.codec ?? "audio").lowercased()
    }

    // MARK: - Offline playback (LocalQueue source, API answers)

    /// The stored item as played offline: audio only (a video's download is its default track alone), local paths,
    /// one version (no video links), lyrics from the stored file.
    private func playable(_ f: OfflineFile) -> MediaItem? {
        guard let audio = f.audio else { return nil }
        let s = f.item
        let fromVideo = s.kind == "video"
        let loved = manifest.pendingLoves[String(f.id)] ?? s.loved
        return MediaItem(id: s.id, kind: "audio", title: s.title, artist: s.artist, album: s.album, folder: s.folder,
                         root: s.root, duration: s.duration, lossless: s.lossless, codec: fromVideo ? s.acodec : s.codec,
                         art: f.art != nil, audioTrack: s.audioTrack, audioOrd: fromVideo ? nil : s.audioOrd,
                         subTrack: nil, subOrd: nil, subs: nil, track: s.track, year: s.year, lufs: s.lufs,
                         tpeak: s.tpeak, lyr: f.lyrics != nil ? max(s.lyr ?? 1, 1) : 0, acodec: s.acodec, fps: nil,
                         dyn: nil, videos: nil, versions: nil, loved: loved,
                         localPath: LocalMedia.offlinePrefix + audio,
                         localArt: f.art.map { LocalMedia.offlinePrefix + $0 })
    }

    private func rebuild() {
        let playableItems = manifest.files.values.compactMap(playable).sorted {
            let a = (($0.artist ?? "").lowercased(), $0.title.lowercased())
            let b = (($1.artist ?? "").lowercased(), $1.title.lowercased())
            return a == b ? $0.id < $1.id : a < b
        }
        items = playableItems
        byID = Dictionary(playableItems.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        lists = manifest.lists.sorted {
            $0.rank != $1.rank ? $0.rank < $1.rank : $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
        version += 1
        itemsChanged?()
    }

    /// Offline mode API (LocalAPI): the downloads only; no playlists, no For you lists.
    private func respond(_ url: URL) -> (Data, URLResponse)? {
        let path = url.path
        if path == "/api/library" { return LocalAPI.items(url, items) }
        if path == "/api/collection/loved" { return LocalAPI.items(url, items.filter { $0.loved == true }) }
        if path.hasPrefix("/api/collection/") { return LocalAPI.items(url, []) }
        if path == "/api/auto" { return LocalAPI.reply(url, 200, [AutoList]()) }
        if path == "/api/playlists" { return LocalAPI.reply(url, 200, ["playlists": [Int]()]) }
        let lyricsPrefix = "/api/lyrics/"
        if path.hasPrefix(lyricsPrefix), let id = Int(path.dropFirst(lyricsPrefix.count)),
           let name = file(id)?.lyrics, let data = try? Data(contentsOf: Self.folder.appendingPathComponent(name)) {
            return (data, LocalAPI.response(url, 200))
        }
        return nil
    }

    // MARK: - Persistence

    private func load() {
        guard let data = try? Data(contentsOf: Self.manifestURL),
              let saved = try? JSONDecoder().decode(OfflineManifest.self, from: data) else { return }
        manifest = saved
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(manifest) else { return }
        try? data.write(to: Self.manifestURL, options: .atomic)
    }
}

extension OfflineStore: LocalItemSource {
    var queueFile: String { "offline-queue.json" }  // the No-server queue stays untouched
    var notesPlays: Bool { false }  // play counts offline are not kept

    /// {"collection": "offline", "list": <key>} = one downloaded list; "offline_all" = every download.
    func collectionIDs(_ msg: [String: Any]) -> [Int]? {
        switch msg["collection"] as? String {
        case "offline":
            guard let key = msg["list"] as? String, let list = list(key) else { return nil }
            return list.sources.filter { byID[$0] != nil }
        case "offline_all":
            return items.map(\.id)
        default:
            return nil
        }
    }

    /// The heart on a download: kept here and sent to the server on the next Media server connection.
    func love(_ id: Int, on wanted: Bool?) -> Bool {
        guard var f = file(id), byID[id] != nil else { return false }
        let now = byID[id]?.loved == true
        let on = wanted ?? !now
        guard on != now else { return false }
        manifest.pendingLoves[String(id)] = on
        f.item.loved = on
        manifest.files[String(id)] = f
        save()
        rebuild()
        LocalLog.write("offline heart \(on ? "on" : "off"): \(id)")
        return true
    }

    func rescan() {}
}

extension OfflineStore: URLSessionDownloadDelegate {
    nonisolated func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                                didFinishDownloadingTo location: URL) {
        // The file must be moved before this returns (the system deletes it afterwards).
        let response = downloadTask.response as? HTTPURLResponse
        let status = response?.statusCode ?? 0
        var staged: URL?
        if (200..<300).contains(status) {  // 206 when a resumed download completes
            let fm = FileManager.default
            try? fm.createDirectory(at: Self.incoming, withIntermediateDirectories: true)
            let dest = Self.incoming.appendingPathComponent(UUID().uuidString)
            if (try? fm.moveItem(at: location, to: dest)) != nil { staged = dest }
        }
        let description = downloadTask.taskDescription ?? ""
        let taskNumber = downloadTask.taskIdentifier
        let format = response?.value(forHTTPHeaderField: "X-Audio-Format")
        let type = response?.value(forHTTPHeaderField: "Content-Type")
        let source = response?.value(forHTTPHeaderField: "X-Audio-Source")
        Task { @MainActor in
            OfflineStore.shared.finished(description, task: taskNumber, status: status, staged: staged, format: format,
                                         contentType: type, source: source)
        }
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let description = task.taskDescription ?? ""
        let taskNumber = task.taskIdentifier
        let nsError = error as NSError?
        let resume = nsError?.userInfo[NSURLSessionDownloadTaskResumeData] as? Data
        let cancelled = nsError?.code == NSURLErrorCancelled
        Task { @MainActor in
            OfflineStore.shared.completed(description, task: taskNumber, error: error != nil, cancelled: cancelled,
                                          resume: resume)
        }
    }

    nonisolated func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        Task { @MainActor in
            OfflineStore.shared.backgroundCompletion?()
            OfflineStore.shared.backgroundCompletion = nil
        }
    }
}
