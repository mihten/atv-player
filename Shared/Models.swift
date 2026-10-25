import Foundation

enum Config {
    /// Media server, set by ServerLocator (Settings address or Bonjour) before anything talks to it.
    /// Plain HTTP on the LAN (ATS exception in Info.plist).
    nonisolated(unsafe) static var serverURL = URL(string: "http://server.invalid")!
    /// True while the internet address (Settings → Away from home) is in use instead of the home network.
    nonisolated(unsafe) static var isRemote = false
    /// Login for the remote address (HTTP Basic, checked by the reverse proxy).
    nonisolated(unsafe) static var credentials: (user: String, password: String)? = nil
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

    /// A video that is shown and played as a video (not when only its audio is streamed).
    var isVideo: Bool { kind == "video" && !Config.streamAudioOnly }

    var subtitle: String {
        [artist, album].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    var artURL: URL { Config.serverURL.appendingPathComponent("art/\(id)") }
    /// The stream. Remote: the login goes in the URL (VLC sends it), and music videos can be audio only.
    var mediaURL: URL {
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
