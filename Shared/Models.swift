import Foundation

enum Config {
    /// Media server, set by ServerLocator (Settings address or Bonjour) before anything talks to it.
    /// Plain HTTP on the LAN (ATS exception in Info.plist).
    nonisolated(unsafe) static var serverURL = URL(string: "http://server.invalid")!
    /// True while the internet address (Settings → Away from home) is in use instead of the home network.
    nonisolated(unsafe) static var isRemote = false
    /// Login for the remote address (HTTP Basic, checked by the reverse proxy).
    nonisolated(unsafe) static var credentials: (user: String, password: String)? = nil
    /// No-server mode (Settings → Server → Library): queue, library and files on the device. Set by ServerLocator.
    nonisolated(unsafe) static var localMode = false
    /// Away from home with "audio only": music videos are streamed as audio and shown like songs.
    static var streamAudioOnly: Bool { isRemote && RemoteSettings.audioOnly }

    static var remoteAddress: String {
        "http://\(serverURL.host ?? ""):\(serverURL.port ?? 80)"
    }

    /// Which queue on the server this app plays: the Apple TV and the iPhone each have their own.
    #if os(tvOS)
    static let deviceKind = "tv"
    #else
    static let deviceKind = "phone"
    #endif
}

struct MediaItem: Codable, Identifiable, Hashable {
    let id: Int
    let kind: String
    let title: String
    let artist: String?
    let album: String?
    let folder: String?
    let root: String?
    let duration: Double?
    let lossless: Bool?
    let codec: String?
    let art: Bool?
    let audioTrack: String?
    let audioOrd: Int?
    let subTrack: String?
    let subOrd: Int?
    /// All subtitle tracks in file order (server 0.8.0+; nil from older servers).
    let subs: [SubtitleTrack]?
    let track: Int?
    let year: String?
    /// Measured loudness (LUFS) and true peak (dBFS) from the server (0.12.0+, nil until measured).
    var lufs: Double? = nil
    var tpeak: Double? = nil
    /// Embedded lyrics (server 0.13.0+): nil = not looked at yet, 0 = none, 1 = plain text, 2 = synced.
    var lyr: Int? = nil
    /// Codec of the default audio track (server 0.16.1+; nil from older servers). Songs: same as `codec`.
    var acodec: String? = nil
    /// Video frame rate (server 0.16.2+ / no-server preparse; nil or 0 = unknown) and dynamic range of a video
    /// ("sdr" / "pq" / "hlg" / "dv"; server only, nil = unknown).
    var fps: Double? = nil
    var dyn: String? = nil
    /// Song <-> music video links (server 0.16.3+ / no-server VersionLinks): number of music videos of a song (lists),
    /// and every member of the item's group, songs first (the item being played; nil = no other version).
    var videos: Int? = nil
    var versions: [MediaVersion]? = nil
    /// ♥ (server 0.16.4+ / no-server LocalListening; nil from older servers). Per version group: true when the item or
    /// one of its versions is loved.
    var loved: Bool? = nil
    /// No-server mode only (never sent by the server): where the file is. `smb://...` URL string, or a path relative
    /// to LocalSettings.deviceFolder (the iPhone app's Documents folder, whose absolute path changes with app updates).
    var localPath: String? = nil
    /// No-server mode only: file name of the cover image in LocalMedia.artFolder (nil = no cover).
    var localArt: String? = nil

    /// A video that is shown and played as a video (not when only its audio is streamed).
    var isVideo: Bool { kind == "video" && !Config.streamAudioOnly }

    var subtitle: String {
        [artist, album].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    var artURL: URL {
        if let localArt { return LocalMedia.artFolder.appendingPathComponent(localArt) }
        return Config.serverURL.appendingPathComponent("art/\(id)")
    }
    /// The stream. Remote: the login goes in the URL (VLC sends it), and music videos can be audio only.
    var mediaURL: URL {
        if let localPath, let local = LocalMedia.url(for: localPath) { return local }
        var components = URLComponents(url: Config.serverURL.appendingPathComponent("media/\(id)"), resolvingAgainstBaseURL: false)!
        if Config.isRemote, let login = Config.credentials {
            components.user = login.user
            components.password = login.password
        }
        if kind == "video" && Config.streamAudioOnly {
            components.queryItems = [URLQueryItem(name: "audio", value: "1")]
        }
        return components.url!
    }
}

extension MediaItem {
    /// Short codec badges for the player, e.g. ["AV1", "OPUS"]; a song shows its codec once.
    var codecBadges: [String] {
        if kind == "video" {
            let video = isVideo ? codec.flatMap(CodecName.label) : nil  // audio-only stream: no video codec
            return [video, acodec.flatMap(CodecName.label), isVideo ? hdrBadge : nil].compactMap { $0 }
        }
        return [(acodec ?? codec).flatMap(CodecName.label)].compactMap { $0 }
    }

    /// HDR format of a video file (played as SDR by VLC 3); nil for SDR or unknown.
    var hdrBadge: String? {
        switch dyn ?? "" {
        case "pq": return "HDR10"
        case "hlg": return "HLG"
        case "dv": return "DV"
        default: return nil
        }
    }
}

extension MediaItem {
    /// The item's own entry in `versions` ("Official video", "Live at Wembley 1986", "Song" ...).
    var versionLabel: String? { versions?.first(where: { $0.id == id })?.label }
    /// ids sharing the item's ♥: its versions and the item itself (a no-server audio twin is not listed in them).
    var versionGroup: [Int] {
        let ids = versions?.map(\.id) ?? []
        return ids.contains(id) ? ids : ids + [id]
    }
    /// Linked versions besides this one (the song and / or other videos).
    var hasVersions: Bool { (versions?.count ?? 0) > 1 }
    var videoCount: Int {
        if let versions { return versions.filter { $0.kind == "video" }.count }
        return videos ?? 0
    }
    /// Chip for the player: "▶ 2 videos" on a song with music videos, the version label on a video with siblings.
    var versionBadge: String? {
        if kind == "audio" {
            let count = videoCount
            return count > 0 ? "▶ \(count) video\(count == 1 ? "" : "s")" : nil
        }
        return hasVersions ? versionLabel : nil
    }
}

/// One version of a song: the song itself or one of its music videos (MediaItem.versions).
struct MediaVersion: Codable, Hashable, Identifiable {
    let id: Int
    let kind: String
    let label: String
    let duration: Double?

    var isVideo: Bool { kind == "video" }
}

/// ffmpeg codec names (and the scanner's file-extension fallbacks for songs) -> short display labels.
enum CodecName {
    static func label(_ name: String) -> String? {
        let n = name.trimmingCharacters(in: .whitespaces).lowercased()
        if n.isEmpty { return nil }
        if n.hasPrefix("pcm_") || ["wav", "aiff", "aif"].contains(n) { return "PCM" }
        if n.hasPrefix("mp4a") { return "AAC" }  // MP4 audio as mutagen reports it ("mp4a.40.2")
        switch n {
        case "av1": return "AV1"
        case "vp9": return "VP9"
        case "vp8": return "VP8"
        case "h264", "avc1": return "H.264"
        case "hevc", "h265": return "HEVC"
        case "opus": return "OPUS"
        case "flac": return "FLAC"
        case "aac": return "AAC"
        case "mp3": return "MP3"
        case "vorbis": return "VORBIS"
        case "alac": return "ALAC"
        case "ac3": return "AC3"
        case "eac3": return "E-AC3"
        case "wavpack", "wv": return "WAVPACK"
        default: return n.uppercased()
        }
    }
}

struct SubtitleTrack: Codable, Hashable {
    let ord: Int
    let lang: String
    let title: String
    let isDefault: Bool
    let forced: Bool
}

struct SessionState: Codable {
    let queue: [Int]?
    let index: Int?
    let shuffle: Bool?
    let repeatMode: String?
    let playing: Bool?
    let position: Double?
    let duration: Double?
    let current: MediaItem?
    let upcoming: [MediaItem]?
    let players: Int?
    let version: String?
    /// "Prefer videos" for this queue (server 0.16.3+; nil = older server).
    var preferVideos: Bool? = nil
    /// The atv-audio sink of the Apple TV's session (nil for the iPhone's, older servers and no-server mode).
    var sink: SinkState? = nil
}

/// state.sink: the optional atv-audio program that plays the TV's sound. All optional: a decode failure would drop
/// the whole state message.
struct SinkState: Codable, Equatable {
    var connected: Bool? = nil
    var name: String? = nil
    var output: SinkOutput? = nil

    var isConnected: Bool { connected == true }
    /// Connected and its output device present and usable.
    var isUsable: Bool { isConnected && output?.ready == true }
}

/// The sink's output device: kind "bluetooth" | "usb" | "other", codec nil when unknown (USB dongles).
struct SinkOutput: Codable, Equatable {
    var kind: String? = nil
    var name: String? = nil
    var codec: String? = nil
    var ready: Bool? = nil
}

/// Every server push uses this one shape; unused fields are nil.
struct ServerMessage: Decodable {
    let type: String
    let item: MediaItem?
    let position: Double?
    let autoplay: Bool?
    /// Server: the user picked this (show the player). Absent/false for auto-advance etc.
    let present: Bool?
    let action: String?
    let state: SessionState?
    /// pong: ping number and server time (monotonic seconds); sink_ready: the item id the sink has ready.
    let n: Int?
    let t: Double?
    let id: Int?
}

struct ItemList: Decodable {
    let items: [MediaItem]
}

struct FolderInfo: Decodable, Identifiable, Hashable {
    let root: String
    let folder: String
    let audio: Int
    let video: Int

    var id: String { root + "/" + folder }
    var name: String { folder.isEmpty ? "(\(root))" : folder }
}

struct FolderList: Decodable {
    let folders: [FolderInfo]
}

struct RootInfo: Decodable, Identifiable, Hashable {
    let root: String
    let name: String
    let count: Int

    var id: String { root }
}

struct RootList: Decodable {
    let roots: [RootInfo]
}

struct SubfolderInfo: Decodable, Identifiable, Hashable {
    let name: String
    let folder: String
    let count: Int

    var id: String { folder }
}

struct BrowseResult: Decodable {
    let subfolders: [SubfolderInfo]
    let items: [MediaItem]
    let total: Int

    var hasSubfolders: Bool { !subfolders.isEmpty }
}

/// /api/playlists row.
struct PlaylistInfo: Decodable, Identifiable, Hashable {
    let id: Int
    let name: String
    let count: Int
    let missing: Int
    let videos: Int
    let duration: Double

    var detail: String {
        var parts = ["\(count) item\(count == 1 ? "" : "s")"]
        if videos > 0 { parts.append("\(videos) video\(videos == 1 ? "" : "s")") }
        if duration > 0 { parts.append(formatTime(duration)) }
        if missing > 0 { parts.append("\(missing) missing") }
        return parts.joined(separator: " · ")
    }
}

struct PlaylistList: Decodable {
    let playlists: [PlaylistInfo]
}

/// One playlist entry. Entries whose file was deleted come as {id, missing: true, title} (no kind),
/// so this is not a MediaItem. The same song may appear twice: identify rows by position.
struct PlaylistEntry: Decodable, Hashable {
    let id: Int
    let kind: String?
    let title: String
    let artist: String?
    let lossless: Bool?
    let missing: Bool?

    var isMissing: Bool { missing == true }
    var isVideo: Bool { kind == "video" }
}

struct PlaylistDetail: Decodable {
    let id: Int
    let name: String
    let items: [PlaylistEntry]
}

extension URL {
    static func api(_ path: String, _ query: [String: String] = [:]) -> URL {
        var components = URLComponents(url: Config.serverURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        return components.url!
    }
}

extension JSONDecoder {
    static func server() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}

func formatTime(_ seconds: Double) -> String {
    guard seconds.isFinite, seconds > 0 else { return "0:00" }
    let total = Int(seconds)
    let h = total / 3600, m = (total % 3600) / 60, s = total % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
}
