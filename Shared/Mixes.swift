import Foundation

/// The two Home shuffle tiles ("Mix 1" / "Mix 2"): each is a name plus folders and / or playlists of the library, kept on
/// the media server (0.30.0+, GET /api/mixes, PUT /api/mixes/<n>) so every app and the web remote show the same two. A tile
/// shuffles all of them, each file once (`ServerConnection.startMix`). Server only: no-server and Offline mode have no mixes.

/// One folder or playlist of a mix, as the server describes it. A folder is (root, folder, recursive), a playlist its id.
struct MixSource: Decodable, Hashable, Identifiable {
    var type: String
    var root: String? = nil
    var folder: String? = nil
    var recursive: Bool? = nil
    var playlistID: Int? = nil
    /// For display: the folder's or playlist's name, the library folder's name, how many files it holds now.
    var name: String? = nil
    var rootName: String? = nil
    var count: Int? = nil
    var missing: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case type, root, folder, recursive, name, rootName, count, missing
        case playlistID = "id"
    }

    var isPlaylist: Bool { type == "playlist" }
    var id: String { isPlaylist ? "p\(playlistID ?? 0)" : "f\(root ?? "")/\(folder ?? "")|\(recursive == false ? "1" : "r")" }

    static func folder(root: String, folder: String, recursive: Bool) -> MixSource {
        MixSource(type: "folder", root: root, folder: folder, recursive: recursive)
    }

    static func playlist(_ id: Int) -> MixSource {
        MixSource(type: "playlist", playlistID: id)
    }

    /// The JSON the server wants back (the display fields are not sent).
    var requestBody: [String: Any] {
        if isPlaylist { return ["type": "playlist", "id": playlistID ?? 0] }
        return ["type": "folder", "root": root ?? "", "folder": folder ?? "", "recursive": recursive ?? true]
    }

    var title: String { (name ?? (isPlaylist ? "Playlist" : "Folder")) + (missing == true ? " (missing)" : "") }

    /// "Playlist · 12 files", "Folder with subfolders · Music · 340 files".
    var detail: String {
        var parts: [String]
        if isPlaylist {
            parts = ["Playlist"]
        } else {
            parts = [recursive == false ? "Only this folder" : "Folder with subfolders"]
            if let rootName { parts.append(rootName) }
        }
        if let count { parts.append("\(count) file\(count == 1 ? "" : "s")") }
        return parts.joined(separator: " · ")
    }
}

/// One mix with its sources (GET /api/mixes).
struct MixDetail: Decodable, Identifiable, Hashable {
    var number: Int
    var name: String
    var sources: [MixSource]
    var count: Int

    var id: Int { number }
    var summary: String {
        MixSummary(name: name, folders: sources.filter { !$0.isPlaylist }.count,
                   playlists: sources.filter(\.isPlaylist).count).detail
    }
}

private struct MixesReply: Decodable {
    let mixes: [MixDetail]
}

/// The folders under a library folder, for the picker (GET /api/browse).
struct MixBrowse: Decodable {
    let subfolders: [SubfolderInfo]
    let total: Int
}

enum MixesAPI {
    static func load() async -> [MixDetail]? {
        guard let (data, response) = try? await Net.data(from: .api("api/mixes")),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let reply = try? JSONDecoder.server().decode(MixesReply.self, from: data) else { return nil }
        return reply.mixes
    }

    /// PUT: a new name and / or the whole source list. Returns both mixes as the server has them now, nil on a failure.
    static func save(_ number: Int, name: String? = nil, sources: [MixSource]? = nil) async -> [MixDetail]? {
        var body: [String: Any] = [:]
        if let name { body["name"] = name }
        if let sources { body["sources"] = sources.map(\.requestBody) }
        var request = Net.request(URL.api("api/mixes/\(number)"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let reply = try? JSONDecoder.server().decode(MixesReply.self, from: data) else { return nil }
        return reply.mixes
    }

    /// Library folders (no root) or the subfolders of one folder.
    static func roots() async -> [RootInfo]? {
        guard let (data, response) = try? await Net.data(from: .api("api/browse")),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let list = try? JSONDecoder.server().decode(RootList.self, from: data) else { return nil }
        return list.roots
    }

    static func browse(root: String, folder: String) async -> MixBrowse? {
        guard let (data, response) = try? await Net.data(from: .api("api/browse", ["root": root, "folder": folder])),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder.server().decode(MixBrowse.self, from: data)
    }

    static func playlists() async -> [PlaylistInfo]? {
        guard let (data, response) = try? await Net.data(from: .api("api/playlists")),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let list = try? JSONDecoder.server().decode(PlaylistList.self, from: data) else { return nil }
        return list.playlists
    }
}

/// What the two Settings → Mixes screens (iPhone, Apple TV) share: the mixes as the server has them, and the edits.
@MainActor
final class MixEditor: ObservableObject {
    @Published private(set) var mixes: [MixDetail] = []
    @Published private(set) var loaded = false
    @Published var error: String?

    func reload() async {
        if let fresh = await MixesAPI.load() {
            mixes = fresh
            error = nil
        } else if !loaded {
            error = "Could not load the mixes from the server."
        }
        loaded = true
    }

    func mix(_ number: Int) -> MixDetail? { mixes.first { $0.number == number } }

    func rename(_ number: Int, to name: String) async {
        let clean = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let current = mix(number), clean != current.name else { return }
        await apply(number, name: clean)
    }

    func remove(_ number: Int, source: MixSource) async {
        guard let current = mix(number) else { return }
        await apply(number, sources: current.sources.filter { $0.id != source.id })
    }

    func add(_ number: Int, source: MixSource) async {
        guard let current = mix(number), !current.sources.contains(where: { $0.id == source.id }) else { return }
        await apply(number, sources: current.sources + [source])
    }

    private func apply(_ number: Int, name: String? = nil, sources: [MixSource]? = nil) async {
        if let fresh = await MixesAPI.save(number, name: name, sources: sources) {
            mixes = fresh
            error = nil
        } else {
            error = "Could not save the mix."
        }
    }
}
