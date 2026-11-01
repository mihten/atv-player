import Foundation
#if os(tvOS)
import TVVLCKit
#else
import MobileVLCKit
#endif

// No-server mode: the library is built on the device from an SMB share (both apps) and, on the iPhone, from the app's
// Documents folder (Files app › On My iPhone › ATV Player). VLC lists the share and reads the tags (preparse).

/// Settings → Server → Library. The SMB password is kept in the Keychain.
enum LocalSettings {
    static let modeKey = "libraryMode"        // "server" (default) or "local"
    static let smbKey = "smbAddress"          // as typed: nas/music, smb://nas/music, \\nas\music
    static let smbUserKey = "smbUser"
    static let smbDomainKey = "smbDomain"     // optional, usually empty
    static let keychainService = "eu.mihten.atvplayer.smb"

    private static var defaults: UserDefaults { .standard }

    static var enabled: Bool { defaults.string(forKey: modeKey) == "local" }

    static func setEnabled(_ on: Bool) {
        defaults.set(on ? "local" : "server", forKey: modeKey)
    }

    static var smbAddress: String { defaults.string(forKey: smbKey) ?? "" }
    static var smbUser: String { defaults.string(forKey: smbUserKey) ?? "" }
    static var smbDomain: String { defaults.string(forKey: smbDomainKey) ?? "" }

    static var smbPassword: String { Keychain.get(keychainService) ?? "" }

    static func setSMBPassword(_ value: String) {
        if value.isEmpty { Keychain.remove(keychainService) } else { Keychain.set(value, for: keychainService) }
    }

    static var smbURL: URL? { normalizeSMB(smbAddress) }

    /// "host/share[/dir]" for display (the SMB root's name in Browse).
    static var smbName: String? { smbURL.map { displayName($0) } }

    static func displayName(_ url: URL) -> String {
        (url.host ?? "") + url.path
    }

    /// iPhone: the app's Documents folder (shared with the Files app). Apple TV: none (SMB only).
    static var deviceFolder: URL? {
        #if os(iOS)
        return FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        #else
        return nil
        #endif
    }

    static var isConfigured: Bool { smbURL != nil || deviceFolder != nil }

    /// VLC options for every media that opens an smb:// URL (listing, tags, playback). Never logged.
    static var smbOptions: [String] {
        var options: [String] = []
        let user = smbUser.trimmingCharacters(in: .whitespaces)
        let password = smbPassword
        let domain = smbDomain.trimmingCharacters(in: .whitespaces)
        if !user.isEmpty { options.append(":smb-user=\(user)") }
        if !password.isEmpty { options.append(":smb-pwd=\(password)") }
        if !domain.isEmpty { options.append(":smb-domain=\(domain)") }
        return options
    }

    /// "host/share", "host/share/sub/dir", "smb://host/share" or "\\host\share\dir" -> smb://host/share[/dir]
    /// (no trailing slash, no login). nil without a host or a share.
    static func normalizeSMB(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "\\", with: "/")
        if let range = text.range(of: "://") {
            guard text[..<range.lowerBound].lowercased() == "smb" else { return nil }
            text = String(text[range.upperBound...])
        }
        var parts = text.split(separator: "/", omittingEmptySubsequences: true).map { part -> String in
            let piece = String(part)
            return piece.removingPercentEncoding ?? piece
        }
        guard parts.count >= 2 else { return nil }
        var host = parts.removeFirst()
        if let at = host.lastIndex(of: "@") { host = String(host[host.index(after: at)...]) }
        var port: Int?
        if let colon = host.lastIndex(of: ":"), let number = Int(host[host.index(after: colon)...]) {
            port = number
            host = String(host[..<colon])
        }
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: ".-_"))
        guard !host.isEmpty, host.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return nil }
        var components = URLComponents()
        components.scheme = "smb"
        components.host = host
        components.port = port
        components.path = "/" + parts.joined(separator: "/")
        return components.url
    }
}

/// Where local files and the no-server data live. Not actor-bound: MediaItem.mediaURL / artURL use it.
enum LocalMedia {
    /// A MediaItem.localPath: an smb:// URL string, or a path relative to LocalSettings.deviceFolder.
    static func url(for localPath: String) -> URL? {
        if localPath.hasPrefix("smb://") { return URL(string: localPath) }
        return LocalSettings.deviceFolder?.appendingPathComponent(localPath)
    }

    /// Library index and queue. tvOS apps may only write to Caches.
    static let dataFolder: URL = {
        #if os(tvOS)
        return LocalMedia.folder(.cachesDirectory, "ATVPlayer")
        #else
        return LocalMedia.folder(.applicationSupportDirectory, "ATVPlayer")
        #endif
    }()

    /// Player log (both apps).
    static let cacheFolder: URL = LocalMedia.folder(.cachesDirectory, "ATVPlayer")

    /// Covers copied out of the files' tags.
    static let artFolder: URL = LocalMedia.folder(.cachesDirectory, "ATVPlayer-art")

    private static func folder(_ directory: FileManager.SearchPathDirectory, _ name: String) -> URL {
        let base = FileManager.default.urls(for: directory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory
        let url = base.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

/// A file found while listing a source.
struct LocalFound: Sendable {
    let root: String       // "device" or "smb"
    let localPath: String
    let folder: String     // directory relative to the root, "" at the top
    let name: String
}

struct LocalFailure: Error {
    let message: String
}

/// File names and extensions (same lists as server/scanner.py).
enum LocalNames {
    static let audioExt: Set<String> = ["flac", "mp3", "m4a", "wv", "ogg", "opus", "wav", "aac", "ape", "aiff", "aif"]
    static let videoExt: Set<String> = ["mkv", "mp4", "m4v", "mov", "webm", "avi"]
    static let losslessExt: Set<String> = ["flac", "wv", "wav", "ape", "aiff", "aif"]

    /// "audio" / "video", nil = not a media file.
    static func kind(of name: String) -> String? {
        let ext = (name as NSString).pathExtension.lowercased()
        if audioExt.contains(ext) { return "audio" }
        if videoExt.contains(ext) { return "video" }
        return nil
    }

    /// Song codec from the extension until the tags are read (scanner fallback names).
    static func audioCodec(_ ext: String) -> String? {
        switch ext {
        case "m4a": return nil
        case "ogg": return "vorbis"
        case "wv": return "wavpack"
        case "aif": return "aiff"
        default: return ext
        }
    }

    private static let discTrack = try! NSRegularExpression(pattern: #"^\s*(\d{1,2})-(\d{1,3})(?:\s*[-._)]\s*|\s+)"#)
    private static let trackSeparator = try! NSRegularExpression(pattern: #"^\s*(\d{1,3})\s*[-._)]\s*"#)
    private static let trackSpace = try! NSRegularExpression(pattern: #"^\s*(\d{2})\s+"#)

    /// "01 - Artist - Title", "1-02 Title", "01. Title" -> artist, title, track, disc.
    static func parse(_ stem: String) -> (artist: String?, title: String, track: Int?, disc: Int?) {
        var rest = stem
        var track: Int?
        var disc: Int?
        let ns = stem as NSString
        let whole = NSRange(location: 0, length: ns.length)
        if let m = discTrack.firstMatch(in: stem, range: whole), m.range.length < ns.length {
            disc = Int(ns.substring(with: m.range(at: 1)))
            track = Int(ns.substring(with: m.range(at: 2)))
            rest = ns.substring(from: m.range.location + m.range.length)
        } else if let m = trackSeparator.firstMatch(in: stem, range: whole) ?? trackSpace.firstMatch(in: stem, range: whole),
                  m.range.length < ns.length {
            track = Int(ns.substring(with: m.range(at: 1)))
            rest = ns.substring(from: m.range.location + m.range.length)
        }
        for separator in [" - ", " – ", " — "] {
            if let range = rest.range(of: separator) {
                let artist = rest[..<range.lowerBound].trimmingCharacters(in: .whitespaces)
                let title = rest[range.upperBound...].trimmingCharacters(in: .whitespaces)
                return (artist.isEmpty ? nil : artist, title.isEmpty ? rest.trimmingCharacters(in: .whitespaces) : title,
                        track, disc)
            }
        }
        let title = rest.trimmingCharacters(in: .whitespaces)
        return (nil, title.isEmpty ? stem : title, track, disc)
    }

    /// The item before its tags are read: everything from the file name and folder.
    static func draft(_ found: LocalFound, id: Int) -> (item: MediaItem, disc: Int?) {
        let ext = (found.name as NSString).pathExtension.lowercased()
        let stem = (found.name as NSString).deletingPathExtension
        let kind = videoExt.contains(ext) ? "video" : "audio"
        let parsed = parse(stem)
        let album = kind == "audio" && !found.folder.isEmpty ? (found.folder as NSString).lastPathComponent : nil
        let codec = kind == "audio" ? audioCodec(ext) : nil
        var draft = LocalDraft(id: id, kind: kind, title: parsed.title, root: found.root, folder: found.folder,
                               localPath: found.localPath)
        draft.artist = parsed.artist
        draft.album = album
        draft.lossless = losslessExt.contains(ext)
        draft.codec = codec
        draft.acodec = codec
        draft.track = parsed.track
        return (draft.item, parsed.disc)
    }
}

/// Mutable copy of a local MediaItem (MediaItem's properties are `let`).
struct LocalDraft {
    var id: Int
    var kind: String
    var title: String
    var artist: String?
    var album: String?
    var folder: String
    var root: String
    var duration: Double?
    var lossless = false
    var codec: String?
    var acodec: String?
    var fps: Double?
    var dyn: String?
    var subs: [SubtitleTrack]?
    var track: Int?
    var year: String?
    var localPath: String
    var localArt: String?

    init(id: Int, kind: String, title: String, root: String, folder: String, localPath: String) {
        self.id = id
        self.kind = kind
        self.title = title
        self.root = root
        self.folder = folder
        self.localPath = localPath
    }

    init(_ item: MediaItem) {
        id = item.id
        kind = item.kind
        title = item.title
        artist = item.artist
        album = item.album
        folder = item.folder ?? ""
        root = item.root ?? ""
        duration = item.duration
        lossless = item.lossless ?? false
        codec = item.codec
        acodec = item.acodec
        fps = item.fps
        dyn = item.dyn
        subs = item.subs
        track = item.track
        year = item.year
        localPath = item.localPath ?? ""
        localArt = item.localArt
    }

    /// No lyrics lookup (lyr 0) and no loudness values without the server.
    var item: MediaItem {
        MediaItem(id: id, kind: kind, title: title, artist: artist, album: album, folder: folder, root: root,
                  duration: duration, lossless: lossless, codec: codec, art: localArt != nil, audioTrack: nil,
                  audioOrd: nil, subTrack: nil, subOrd: nil, subs: subs, track: track, year: year,
                  lufs: nil, tpeak: nil, lyr: 0, acodec: acodec, fps: fps, dyn: dyn, localPath: localPath,
                  localArt: localArt)
    }
}

// MARK: - VLC preparse

@MainActor
enum VLCParse {
    private typealias ParseFunction = @convention(c) (AnyObject, Selector, Int32, Int32) -> Int32

    /// Starts `-[VLCMedia parseWithOptions:timeout:]` (called by selector, independent of its Swift import name) and
    /// polls parsedStatus every 100 ms. flags: 0x01 ParseNetwork, 0x02 FetchLocal.
    static func run(_ media: VLCMedia, flags: Int32, timeout: Int32, wait seconds: Double) async -> VLCMediaParsedStatus {
        let selector = NSSelectorFromString("parseWithOptions:timeout:")
        guard media.responds(to: selector), let imp = media.method(for: selector) else { return .failed }
        let parse = unsafeBitCast(imp, to: ParseFunction.self)
        guard parse(media, selector, flags, timeout) == 0 else { return .failed }
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            let status = media.parsedStatus
            if status == .done || status == .failed || status == .timeout || status == .skipped { return status }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        media.parseStop()
        return .timeout
    }
}

// MARK: - SMB listing

struct SMBEntry {
    let name: String
    let url: URL
    let isFolder: Bool
}

@MainActor
enum SMBBrowser {
    /// One folder of the share (not recursive). An empty folder is not an error.
    static func list(_ url: URL) async -> Result<[SMBEntry], LocalFailure> {
        let media = VLCMedia(url: url)
        for option in LocalSettings.smbOptions { media.addOption(option) }
        let status = await VLCParse.run(media, flags: 0x01, timeout: 15000, wait: 16)
        var entries: [SMBEntry] = []
        if let list = media.subitems {
            for i in 0..<max(list.count, 0) {
                guard let child = list.media(at: .init(i)), let childURL = child.url else { continue }
                let name = childURL.lastPathComponent
                guard !name.isEmpty, name != ".", name != ".." else { continue }
                entries.append(SMBEntry(name: name, url: childURL, isFolder: child.mediaType == .directory))
            }
        }
        if entries.isEmpty && status != .done {
            return .failure(LocalFailure(message: "Could not open \(url.absoluteString). Check the address, user name and password."))
        }
        return .success(entries)
    }

    /// Settings "Test" button: lists the configured root.
    static func test() async -> String {
        guard let url = LocalSettings.smbURL else { return "Enter the share address first (e.g. nas/music)." }
        switch await list(url) {
        case .success(let entries):
            return "Connected: \(entries.count) entr\(entries.count == 1 ? "y" : "ies") in \(LocalSettings.displayName(url))"
        case .failure(let failure):
            return failure.message
        }
    }

    /// URL string stored as localPath: no login in it.
    static func storedString(_ url: URL) -> String {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.user != nil || components.password != nil else { return url.absoluteString }
        components.user = nil
        components.password = nil
        return components.url?.absoluteString ?? url.absoluteString
    }
}

// MARK: - Tags

struct LocalTags {
    var title: String?
    var artist: String?
    var album: String?
    var track: Int?
    var disc: Int?
    var year: String?
    var duration: Double?
    var videoCodec: String?
    var audioCodec: String?
    /// Frame rate of the first video track (nil = VLC gave none).
    var fps: Double?
    var subs: [SubtitleTrack] = []
    var artFile: URL?
}

@MainActor
enum LocalTagReader {
    /// VLC preparse of one file; nil if it could not be read.
    static func read(_ item: MediaItem) async -> LocalTags? {
        guard let path = item.localPath, let url = LocalMedia.url(for: path) else { return nil }
        let media = VLCMedia(url: url)
        if url.scheme == "smb" {
            for option in LocalSettings.smbOptions { media.addOption(option) }
        }
        let status = await VLCParse.run(media, flags: 0x01 | 0x02, timeout: 8000, wait: 10)
        guard status == .done else { return nil }
        var tags = LocalTags()
        let meta = media.metaData
        // VLC returns the file name as the title when the file has none.
        let fileName = url.lastPathComponent
        let stem = (fileName as NSString).deletingPathExtension
        if let title = clean(meta.title), title != fileName, title != stem { tags.title = title }
        tags.artist = clean(meta.artist) ?? clean(meta.albumArtist)
        tags.album = clean(meta.album)
        if meta.trackNumber > 0 { tags.track = Int(meta.trackNumber) }
        if meta.discNumber > 0 { tags.disc = Int(meta.discNumber) }
        if let date = clean(meta.date) {
            let year = String(date.prefix(4))
            if year.count == 4, year.allSatisfy({ $0.isASCII && $0.isNumber }) { tags.year = year }
        }
        let ms = media.length.intValue
        if ms > 0 { tags.duration = Double(ms) / 1000 }
        var textOrdinal = 0
        for case let info as [String: Any] in media.tracksInformation {
            let type = info[VLCMediaTracksInformationType] as? String
            let fourcc = (info[VLCMediaTracksInformationCodec] as? NSNumber)?.uint32Value ?? 0
            if type == VLCMediaTracksInformationTypeVideo {
                if tags.videoCodec == nil { tags.videoCodec = codecName(fourcc) }
                let rate = (info[VLCMediaTracksInformationFrameRate] as? NSNumber)?.doubleValue ?? 0
                let base = (info[VLCMediaTracksInformationFrameRateDenominator] as? NSNumber)?.doubleValue ?? 0
                if tags.fps == nil, rate > 0, base > 0 { tags.fps = rate / base }
            } else if type == VLCMediaTracksInformationTypeAudio {
                if tags.audioCodec == nil { tags.audioCodec = codecName(fourcc) }
            } else if type == VLCMediaTracksInformationTypeText {
                let language = info[VLCMediaTracksInformationLanguage] as? String ?? ""
                let description = info[VLCMediaTracksInformationDescription] as? String ?? ""
                tags.subs.append(SubtitleTrack(ord: textOrdinal, lang: language, title: description, isDefault: false,
                                               forced: false))
                textOrdinal += 1
            }
        }
        if let art = meta.artworkURL, art.isFileURL, FileManager.default.fileExists(atPath: art.path) {
            tags.artFile = art
        }
        return tags
    }

    private static func clean(_ text: String?) -> String? {
        guard let value = text?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { return nil }
        return value
    }

    /// VLC FourCC (a | b<<8 | c<<16 | d<<24) -> the ffmpeg-style names CodecName.label knows.
    static func codecName(_ fourcc: UInt32) -> String? {
        guard fourcc != 0 else { return nil }
        let bytes = [fourcc, fourcc >> 8, fourcc >> 16, fourcc >> 24].map { UInt8(truncatingIfNeeded: $0) }
        let code = String(decoding: bytes, as: UTF8.self).lowercased().trimmingCharacters(in: .whitespaces)
        switch code {
        case "av01": return "av1"
        case "vp90", "vp09": return "vp9"
        case "vp80", "vp08": return "vp8"
        case "h264", "avc1", "x264": return "h264"
        case "hevc", "hvc1", "hev1", "h265": return "hevc"
        case "opus": return "opus"
        case "flac": return "flac"
        case "mp4a": return "aac"
        case "mpga", "mp3", "mpg3": return "mp3"
        case "vorb": return "vorbis"
        case "alac": return "alac"
        case "a52", "ac-3", "ac3": return "ac3"
        case "eac3": return "eac3"
        case "wvpk": return "wavpack"
        case "ape": return "ape"
        case "s16l", "araw", "sowt": return "pcm_s16le"
        case "s16b", "twos": return "pcm_s16be"
        case "s24l": return "pcm_s24le"
        case "s24b", "in24": return "pcm_s24be"
        case "s32l": return "pcm_s32le"
        case "s32b", "in32": return "pcm_s32be"
        case "f32l": return "pcm_f32le"
        case "f32b", "fl32": return "pcm_f32be"
        case "lpcm": return "pcm_s16le"
        default: return nil
        }
    }

    static func isLosslessAudio(_ codec: String?) -> Bool {
        guard let codec else { return false }
        return ["flac", "alac", "wavpack", "ape"].contains(codec) || codec.hasPrefix("pcm_")
    }
}

// MARK: - Library

struct LocalBrowseResult {
    let root: String
    let folder: String
    let subfolders: [SubfolderInfo]
    let items: [MediaItem]
    let total: Int
}

/// Saved index (plain JSONEncoder keys).
private struct LocalIndex: Codable {
    var source: String
    var items: [MediaItem]
    var nextID: Int
    var pathIDs: [String: Int]
    var tagged: [Int]
    var discs: [Int: Int]
    var scannedAt: Double?
}

@MainActor
final class LocalLibrary: ObservableObject {
    static let shared = LocalLibrary()

    /// Sorted like the server (root, folder, disc, track, artist, title).
    @Published private(set) var items: [MediaItem] = []
    private(set) var byID: [Int: MediaItem] = [:]
    @Published private(set) var scanning = false
    @Published private(set) var status = "Not scanned yet"
    /// Bumped whenever `items` changes; library screens reload on it.
    @Published private(set) var version = 0
    /// LocalQueue: re-prune the queue after a rescan.
    var itemsChanged: (@MainActor () -> Void)?

    private var loaded = false
    private var source = ""
    private var nextID = 1
    private var pathIDs: [String: Int] = [:]
    private var tagged: Set<Int> = []
    private var discs: [Int: Int] = [:]
    private var scannedAt: Double?
    /// Bumped by reset(): a running scan stops at its next step.
    private var generation = 0
    /// Group key and label per item (VersionLinks): publish() runs every 25 tags read.
    private let versionCache = VersionLinks.Cache()

    private static let nextIDKey = "localLibraryNextID"
    private static let writer = DispatchQueue(label: "eu.mihten.atvplayer.local-library")
    private static var indexURL: URL { LocalMedia.dataFolder.appendingPathComponent("local-library.json") }

    /// The sources the index is built from; a different one starts a fresh index.
    private static var sourceKey: String {
        (LocalSettings.smbURL?.absoluteString ?? "-") + "|" + (LocalSettings.deviceFolder == nil ? "" : "device")
    }

    /// Loads the saved index (once) and rescans in the background (folders only; tags only for new files).
    func start() {
        if !loaded {
            loaded = true
            load()
        }
        scan()
    }

    func item(_ id: Int) -> MediaItem? { byID[id] }

    /// Settings changed the source: forget the index and scan again (the scan only runs in no-server mode).
    func reset() {
        loaded = true
        generation += 1
        scanning = false
        source = Self.sourceKey
        // pathIDs stay (keyed by fileKey): a file found again keeps its id, and with it its plays and heart
        tagged = []
        discs = [:]
        scannedAt = nil
        try? FileManager.default.removeItem(at: Self.indexURL)
        if let names = try? FileManager.default.contentsOfDirectory(atPath: LocalMedia.artFolder.path) {
            for name in names { try? FileManager.default.removeItem(at: LocalMedia.artFolder.appendingPathComponent(name)) }
        }
        publish([])
        scan()
    }

    func scan() {
        guard Config.localMode, !scanning else { return }
        scanning = true
        let current = generation
        Task { await self.runScan(current) }
    }

    // MARK: Queries (ports of server.py collection / api_browse)

    func collection(_ name: String, root: String?, folder: String?, recursive: Bool = true) -> [MediaItem] {
        switch name {
        case "music":
            return items.filter { $0.kind == "audio" }
        case "videos":
            return Self.videoOrder(items.filter { $0.kind == "video" })
        case "folder":
            let wanted = (folder ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            return items.filter { item in
                guard item.root == root else { return false }
                let f = item.folder ?? ""
                if recursive { return wanted.isEmpty || f == wanted || f.hasPrefix(wanted + "/") }
                return f == wanted
            }
        case "playlist":
            return []
        case "loved", "most_played", "recent", "forgotten", "on_this_day":
            return autoItems(name)
        default:
            return items
        }
    }

    /// Auto-playlists (server.py auto_items): from LocalListening, at most 100 (Loved: all).
    private func autoItems(_ name: String) -> [MediaItem] {
        let listening = LocalListening.shared
        let size = 100
        switch name {
        case "loved":  // once per version group, by its newest loved member
            var seen: Set<Int> = []
            return listening.lovedOrder.compactMap { id -> MediaItem? in
                guard let item = byID[id], !seen.contains(id) else { return nil }
                seen.formUnion(item.versionGroup)
                return item
            }
        case "most_played":
            return Array(listening.mostPlayed.compactMap { byID[$0] }.prefix(size))
        case "recent":  // local ids only grow: the newest files have the highest ids
            return Array(items.sorted { $0.id > $1.id }.prefix(size))
        case "forgotten":  // no version played within 90 days (or never); the same order all day
            let now = Date()
            let cutoff = now.timeIntervalSince1970 - 90 * 86400
            let last = listening.lastPlays
            var ids = items.filter { item in
                item.versionGroup.allSatisfy { (last[$0] ?? 0) < cutoff }
            }.map(\.id).sorted()
            var random = SeededGenerator(seed: UInt64(max(LocalListening.dayNumber(now), 0)))
            ids.shuffle(using: &random)
            return ids.prefix(size).compactMap { byID[$0] }
        case "on_this_day":
            return Array(listening.onThisDay().compactMap { byID[$0] }.prefix(size))
        default:
            return []
        }
    }

    /// /api/auto: the non-empty lists, Loved always.
    func autoLists() -> [AutoList] {
        AutoList.all.compactMap { entry -> AutoList? in
            let list = autoItems(entry.key)
            guard !list.isEmpty || entry.key == "loved" else { return nil }
            return AutoList(key: entry.key, name: entry.name, count: list.count,
                            firstArt: list.first(where: { $0.art == true || $0.localArt != nil })?.id)
        }
    }

    /// A ♥ changed (LocalQueue "love"): items carry the new flag and the queue state follows. No `version` bump (that
    /// reloads every library screen): the auto-list screens and the iPhone's hearts follow autoVersion.
    func lovedChanged() {
        items = Self.markLoved(items)
        byID = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        itemsChanged?()
    }

    /// The heart is per version group (server.py is_loved).
    private static func markLoved(_ list: [MediaItem]) -> [MediaItem] {
        let listening = LocalListening.shared
        return list.map { item -> MediaItem in
            var marked = item
            marked.loved = item.versionGroup.contains { listening.isLoved($0) }
            return marked
        }
    }

    var roots: [RootInfo] {
        var out: [RootInfo] = []
        if LocalSettings.deviceFolder != nil {
            out.append(RootInfo(root: "device", name: "On this iPhone", count: items.filter { $0.root == "device" }.count))
        }
        if let name = LocalSettings.smbName {
            out.append(RootInfo(root: "smb", name: name, count: items.filter { $0.root == "smb" }.count))
        }
        return out
    }

    /// Direct subfolders and the folder's own files; nil for an unknown root.
    func browse(root: String, folder: String?) -> LocalBrowseResult? {
        guard roots.contains(where: { $0.root == root }) else { return nil }
        let wanted = (folder ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let prefix = wanted.isEmpty ? "" : wanted + "/"
        var subs: [String: Int] = [:]
        var own: [MediaItem] = []
        var total = 0
        for item in items where item.root == root {
            let f = item.folder ?? ""
            if f == wanted {
                own.append(item)
                total += 1
            } else if f.hasPrefix(prefix) {
                let name = String(f.dropFirst(prefix.count).split(separator: "/", maxSplits: 1).first ?? "")
                subs[name, default: 0] += 1
                total += 1
            }
        }
        let subfolders = subs.sorted { $0.key.lowercased() < $1.key.lowercased() }
            .map { SubfolderInfo(name: $0.key, folder: prefix + $0.key, count: $0.value) }
        return LocalBrowseResult(root: root, folder: wanted, subfolders: subfolders, items: own, total: total)
    }

    /// server.py video_key: artist, title.
    static func videoOrder(_ list: [MediaItem]) -> [MediaItem] {
        let keyed: [(key: (String, String), item: MediaItem)] = list.map { item in
            let artist = (item.artist ?? "").lowercased()
            return (key: (artist, item.title.lowercased()), item: item)
        }
        return keyed.sorted { $0.key < $1.key }.map { $0.item }
    }

    // MARK: Index

    private func load() {
        nextID = max(1, UserDefaults.standard.integer(forKey: Self.nextIDKey))
        source = Self.sourceKey
        guard let data = try? Data(contentsOf: Self.indexURL),
              let index = try? JSONDecoder().decode(LocalIndex.self, from: data) else { return }
        nextID = max(nextID, index.nextID)
        // Also from another source: the same files keep their ids. Older files are keyed by the full localPath.
        for (path, id) in index.pathIDs {
            let fileKey = Self.fileKey(path)
            pathIDs[fileKey] = min(pathIDs[fileKey] ?? id, id)
        }
        guard index.source == source else { return }
        tagged = Set(index.tagged)
        discs = index.discs
        scannedAt = index.scannedAt
        publish(index.items)
        status = summary([])
    }

    private func save() {
        let index = LocalIndex(source: source, items: items, nextID: nextID, pathIDs: pathIDs, tagged: Array(tagged),
                               discs: discs, scannedAt: scannedAt)
        UserDefaults.standard.set(nextID, forKey: Self.nextIDKey)
        guard let data = try? JSONEncoder().encode(index) else { return }
        let url = Self.indexURL
        Self.writer.async { try? data.write(to: url, options: .atomic) }
    }

    /// pathIDs key of a file: an SMB file by its path on the server without the host (the same share by IP address or
    /// by name is the same file), an iPhone file by its path in Documents. Already a key: returned as it is.
    private static func fileKey(_ localPath: String) -> String {
        guard localPath.hasPrefix("smb://"), let url = URL(string: localPath) else { return localPath }
        return "smb:" + url.path
    }

    /// server.py sort_key: root, folder, disc, track, artist, title.
    private func sortKey(_ item: MediaItem) -> (String, String, Int, Int, String, String) {
        let root = item.root ?? ""
        let folder = (item.folder ?? "").lowercased()
        let disc = discs[item.id] ?? 0
        let track = item.track ?? 0
        let artist = (item.artist ?? "").lowercased()
        return (root, folder, disc, track, artist, item.title.lowercased())
    }

    private func publish(_ list: [MediaItem]) {
        let keyed: [(key: (String, String, Int, Int, String, String), item: MediaItem)] = list.map { item in
            (key: sortKey(item), item: item)
        }
        let sorted = keyed.sorted { $0.key < $1.key }.map { $0.item }
        items = Self.markLoved(VersionLinks.attach(sorted, cache: versionCache, dropTwins: true))  // video links, ♥
        byID = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        version += 1
        itemsChanged?()
    }

    private func summary(_ errors: [String]) -> String {
        var parts: [String] = []
        if !errors.isEmpty { parts.append(errors.joined(separator: " ")) }
        if !LocalSettings.isConfigured {
            parts.append("No SMB share set up yet.")
        } else {
            parts.append("\(items.count.formatted()) file\(items.count == 1 ? "" : "s")")
            if let scannedAt {
                let formatter = DateFormatter()
                formatter.dateFormat = "d MMM HH:mm"
                parts.append("scanned " + formatter.string(from: Date(timeIntervalSince1970: scannedAt)))
            }
        }
        return parts.joined(separator: " · ")
    }

    // MARK: Scan

    private func runScan(_ run: Int) async {
        if source != Self.sourceKey {  // address changed without reset(): fresh index (ids stay, see reset)
            source = Self.sourceKey
            tagged = []
            discs = [:]
            scannedAt = nil
            publish([])
        }
        status = "Scanning…"
        var found: [LocalFound] = []
        var errors: [String] = []
        var keepRoots: Set<String> = []                 // listing failed: keep the old items of that root
        var keepFolders: [(root: String, folder: String)] = []
        if let device = LocalSettings.deviceFolder {
            found += await Task.detached(priority: .utility) { LocalLibrary.listDevice(device) }.value
            guard run == generation else { return }
        }
        if let share = LocalSettings.smbURL {
            let result = await walkSMB(share, run)
            guard run == generation else { return }
            found += result.files
            if let error = result.error {
                errors.append(error)
                keepRoots.insert("smb")
            }
            keepFolders += result.failedFolders.map { (root: "smb", folder: $0) }
        }

        var list: [MediaItem] = []
        var seen: Set<Int> = []
        for file in found {
            let id: Int
            let fileKey = Self.fileKey(file.localPath)
            if let known = pathIDs[fileKey] {
                id = known
            } else {
                id = nextID
                nextID += 1
                pathIDs[fileKey] = id
            }
            guard seen.insert(id).inserted else { continue }
            if let old = byID[id], old.localPath == file.localPath {
                list.append(old)
            } else {
                let draft = LocalNames.draft(file, id: id)
                list.append(draft.item)
                discs[id] = draft.disc
                tagged.remove(id)
            }
        }
        for old in items where !seen.contains(old.id) {
            let root = old.root ?? ""
            let folder = old.folder ?? ""
            let keep = keepRoots.contains(root) || keepFolders.contains { entry in
                entry.root == root && (folder == entry.folder || folder.hasPrefix(entry.folder + "/"))
            }
            if keep {
                list.append(old)
                seen.insert(old.id)
            }
        }
        tagged = tagged.intersection(seen)
        discs = discs.filter { seen.contains($0.key) }
        scannedAt = Date().timeIntervalSince1970
        publish(list)
        save()

        await readTags(run, errors: &errors)
        guard run == generation else { return }
        status = summary(errors)
        scanning = false
    }

    /// Lists the iPhone's Documents folder (off the main thread).
    nonisolated private static func listDevice(_ base: URL) -> [LocalFound] {
        let fm = FileManager.default
        guard let walker = fm.enumerator(atPath: base.path) else { return [] }
        var out: [LocalFound] = []
        while let rel = walker.nextObject() as? String {
            let name = (rel as NSString).lastPathComponent
            var isDirectory: ObjCBool = false
            fm.fileExists(atPath: base.appendingPathComponent(rel).path, isDirectory: &isDirectory)
            if isDirectory.boolValue {
                if name.hasPrefix(".") || walker.level >= 12 { walker.skipDescendants() }
                continue
            }
            guard !name.hasPrefix("."), LocalNames.kind(of: name) != nil else { continue }
            out.append(LocalFound(root: "device", localPath: rel, folder: (rel as NSString).deletingLastPathComponent,
                                  name: name))
        }
        return out
    }

    /// Walks the share folder by folder. error = the root itself could not be opened.
    private func walkSMB(_ base: URL, _ run: Int) async -> (files: [LocalFound], failedFolders: [String], error: String?) {
        var files: [LocalFound] = []
        var failed: [String] = []
        var pending: [(url: URL, folder: String, depth: Int)] = [(base, "", 0)]
        while !pending.isEmpty {
            let next = pending.removeFirst()
            let result = await SMBBrowser.list(next.url)
            guard run == generation else { return (files, failed, nil) }
            switch result {
            case .failure(let failure):
                if next.folder.isEmpty { return ([], [], failure.message) }
                failed.append(next.folder)
            case .success(let entries):
                for entry in entries where !entry.name.hasPrefix(".") {
                    let path = next.folder.isEmpty ? entry.name : next.folder + "/" + entry.name
                    if entry.isFolder {
                        if next.depth < 12 { pending.append((entry.url, path, next.depth + 1)) }
                    } else if LocalNames.kind(of: entry.name) != nil {
                        files.append(LocalFound(root: "smb", localPath: SMBBrowser.storedString(entry.url),
                                                folder: next.folder, name: entry.name))
                    }
                }
                status = "Scanning… \(files.count.formatted()) files"
            }
        }
        return (files, failed, nil)
    }

    /// Reads tags of the items not read yet, one at a time; publishes and saves every 25 items.
    private func readTags(_ run: Int, errors: inout [String]) async {
        // Videos indexed before fps was read are read once more (fps 0 = VLC gave none, not retried).
        let todo = items.filter { !tagged.contains($0.id) || ($0.kind == "video" && $0.fps == nil) }.map(\.id)
        guard !todo.isEmpty else { return }
        var working = byID
        func giveUp(_ ids: [Int]) {
            tagged.formUnion(ids)
            for id in ids {
                guard let unread = working[id], unread.kind == "video", unread.fps == nil else { continue }
                var draft = LocalDraft(unread)
                draft.fps = 0
                working[id] = draft.item
            }
        }
        var done = 0
        var failedInRow: [Int] = []
        for id in todo {
            guard let item = working[id] else { continue }
            status = "Reading tags \(done.formatted()) / \(todo.count.formatted())"
            let tags = await LocalTagReader.read(item)
            guard run == generation else { return }
            done += 1
            if let tags {
                working[id] = applying(tags, to: item)
                tagged.insert(id)
                giveUp(failedInRow)  // single unreadable files: do not retry them on every scan
                failedInRow = []
            } else {
                failedInRow.append(id)
                if failedInRow.count >= 5 {
                    // Stop only when the share no longer answers (retried next scan); else they are unreadable files.
                    var shareDown = false
                    if item.root == "smb", let share = LocalSettings.smbURL {
                        if case .failure = await SMBBrowser.list(share) { shareDown = true }
                        guard run == generation else { return }
                    }
                    if shareDown {
                        errors.append("Could not read the tags (the share stopped answering).")
                        break
                    }
                    giveUp(failedInRow)
                    failedInRow = []
                }
            }
            if done % 25 == 0 {
                publish(Array(working.values))
                save()
            }
        }
        publish(Array(working.values))
        save()
    }

    private func applying(_ tags: LocalTags, to item: MediaItem) -> MediaItem {
        var draft = LocalDraft(item)
        if item.kind == "video" {
            // File names are curated (like the server): "Artist - Title" wins over raw embedded titles.
            if item.artist == nil {
                draft.artist = tags.artist
                draft.title = tags.title ?? item.title
            }
            draft.codec = tags.videoCodec
            draft.acodec = tags.audioCodec
            draft.lossless = LocalTagReader.isLosslessAudio(tags.audioCodec)
            draft.fps = tags.fps ?? 0
        } else {
            draft.title = tags.title ?? item.title
            draft.artist = tags.artist ?? item.artist
            draft.album = tags.album ?? item.album
            if let codec = tags.audioCodec {
                draft.codec = codec
                draft.acodec = codec
            }
            draft.lossless = (item.lossless ?? false) || tags.audioCodec == "alac"
        }
        if let track = tags.track { draft.track = track }
        if let disc = tags.disc { discs[item.id] = disc }
        if let year = tags.year { draft.year = year }
        if let duration = tags.duration { draft.duration = duration }
        draft.subs = tags.subs.isEmpty ? nil : tags.subs
        if let art = tags.artFile ?? folderArt(item), let name = storeArt(art, for: item) {
            draft.localArt = name
        }
        return draft.item
    }

    /// iPhone files: cover.jpg / folder.jpg / front.jpg next to the file.
    private func folderArt(_ item: MediaItem) -> URL? {
        guard item.kind == "audio", item.root == "device", let base = LocalSettings.deviceFolder else { return nil }
        let folder = item.folder ?? ""
        let directory = folder.isEmpty ? base : base.appendingPathComponent(folder)
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: directory.path) else { return nil }
        for wanted in ["cover", "folder", "front", "album"] {
            if let hit = names.first(where: { ["\(wanted).jpg", "\(wanted).jpeg", "\(wanted).png"].contains($0.lowercased()) }) {
                return directory.appendingPathComponent(hit)
            }
        }
        return nil
    }

    /// Copies a cover into the art folder; a new name each time, so image caches never show an old one.
    private func storeArt(_ source: URL, for item: MediaItem) -> String? {
        let fm = FileManager.default
        try? fm.createDirectory(at: LocalMedia.artFolder, withIntermediateDirectories: true)
        let name = "\(item.id)-\(Int(Date().timeIntervalSince1970))"
        let target = LocalMedia.artFolder.appendingPathComponent(name)
        try? fm.removeItem(at: target)
        do {
            try fm.copyItem(at: source, to: target)
        } catch {
            return nil
        }
        if let old = item.localArt, old != name {
            try? fm.removeItem(at: LocalMedia.artFolder.appendingPathComponent(old))
        }
        return name
    }
}
