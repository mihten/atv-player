import Foundation

/// /api/auto row: a list built from the play history or the hearts (server 0.16.4+, no-server LocalLibrary).
/// `key` is a collection name, so /api/collection/<key> lists it and play_collection plays it.
struct AutoList: Codable, Identifiable, Hashable {
    let key: String
    let name: String
    let count: Int
    var firstArt: Int? = nil

    var id: String { key }

    /// Every list in menu order (server.py AUTO_LISTS). Loved is always shown, the others only when not empty.
    static let all: [(key: String, name: String)] = [
        ("loved", "Loved"), ("most_played", "Most played"), ("recent", "Recently added"),
        ("forgotten", "Not heard in a while"), ("on_this_day", "On this day"),
    ]

    var symbol: String {
        switch key {
        case "loved": return "heart.fill"
        case "most_played": return "chart.bar.fill"
        case "recent": return "sparkles"
        case "forgotten": return "clock.arrow.circlepath"
        case "on_this_day": return "calendar"
        default: return "music.note.list"
        }
    }

    var detail: String { "\(count) item\(count == 1 ? "" : "s")" }

    /// The lists for the menus; [] when the server is older than 0.16.4 (404) or unreachable.
    static func load() async -> [AutoList] {
        guard let (data, response) = try? await Net.data(from: .api("api/auto")),
              ((response as? HTTPURLResponse)?.statusCode ?? 200) == 200,
              let lists = try? JSONDecoder.server().decode([AutoList].self, from: data) else { return [] }
        return lists
    }

    /// The items of one list, in its order.
    static func items(_ key: String) async throws -> [MediaItem] {
        let (data, _) = try await Net.data(from: .api("api/collection/\(key)"))
        return try JSONDecoder.server().decode(ItemList.self, from: data).items
    }

    /// Stand-in PlaylistInfo for the playlist card views (an id far above real playlist ids; it only picks the colour
    /// of an empty card). items: the whole list.
    func asPlaylistInfo(index: Int, items: [MediaItem]) -> PlaylistInfo {
        PlaylistInfo(id: 1_000_000 + index, name: key == "loved" ? "♥ \(name)" : name, count: count, missing: 0,
                     videos: items.filter { $0.kind == "video" }.count, duration: 0)
    }
}
