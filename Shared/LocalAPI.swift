import Foundation

private struct ItemsReply: Encodable {
    let items: [MediaItem]
}

private struct RootRow: Encodable {
    let root: String
    let name: String
    let count: Int
}

private struct RootsReply: Encodable {
    let roots: [RootRow]
}

private struct SubfolderRow: Encodable {
    let name: String
    let folder: String
    let count: Int
}

private struct BrowseReply: Encodable {
    let root: String
    let folder: String
    let subfolders: [SubfolderRow]
    let items: [MediaItem]
    let total: Int
}

private struct PlaylistsReply: Encodable {
    let playlists: [Int]
}

private struct ErrorReply: Encodable {
    let error: String
}

/// iPhone Offline library (iOS/OfflineStore.swift registers itself; tvOS leaves both nil).
@MainActor
enum OfflineHooks {
    /// The downloads as a queue source.
    static var source: LocalItemSource?
    /// Answers the API requests in Offline mode (nil = 404).
    static var api: (@MainActor (URL) -> (Data, URLResponse)?)?
}

/// No-server mode: answers the apps' HTTP API requests (Net.data) from LocalLibrary, in the server's JSON shapes
/// (server.py api_library, api_collection, api_browse, api_auto). Playlists are an empty list; everything else is a 404.
enum LocalAPI {
    /// Config.serverURL in no-server mode.
    static let baseURL = URL(string: "http://local.invalid")!

    @MainActor
    static func respond(to url: URL) -> (Data, URLResponse) {
        if Config.offlineMode {  // never the on-device library or the server
            return OfflineHooks.api?(url) ?? notAvailable(url)
        }
        let library = LocalLibrary.shared
        let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        var query: [String: String] = [:]
        for item in queryItems where query[item.name] == nil { query[item.name] = item.value ?? "" }
        let path = url.path

        if path == "/api/library" {
            let kind = query["kind"].flatMap { $0.isEmpty ? nil : $0 }
            var items = library.items.filter { kind == nil || $0.kind == kind }
            if kind == "video" { items = LocalLibrary.videoOrder(items) }
            return reply(url, 200, ItemsReply(items: items))
        }
        let collectionPrefix = "/api/collection/"
        if path.hasPrefix(collectionPrefix) {
            let name = String(path.dropFirst(collectionPrefix.count))
            let items = library.collection(name, root: query["root"], folder: query["folder"], recursive: true)
            return reply(url, 200, ItemsReply(items: items))
        }
        if path == "/api/browse" {
            guard let root = query["root"], !root.isEmpty else {
                let roots = library.roots.map { RootRow(root: $0.root, name: $0.name, count: $0.count) }
                return reply(url, 200, RootsReply(roots: roots))
            }
            guard let result = library.browse(root: root, folder: query["folder"]) else { return notAvailable(url) }
            let subfolders = result.subfolders.map { SubfolderRow(name: $0.name, folder: $0.folder, count: $0.count) }
            return reply(url, 200, BrowseReply(root: result.root, folder: result.folder, subfolders: subfolders,
                                               items: result.items, total: result.total))
        }
        if path == "/api/playlists" {
            return reply(url, 200, PlaylistsReply(playlists: []))
        }
        if path == "/api/auto" {  // auto-playlists from the plays and hearts on the device
            return reply(url, 200, library.autoLists())
        }
        return notAvailable(url)
    }

    static func notAvailable(_ url: URL) -> (Data, URLResponse) {
        reply(url, 404, ErrorReply(error: "not available without the server"))
    }

    /// {"items": [...]} in the server's shape (Offline answers too).
    static func items(_ url: URL, _ items: [MediaItem]) -> (Data, URLResponse) {
        reply(url, 200, ItemsReply(items: items))
    }

    static func reply<T: Encodable>(_ url: URL, _ status: Int, _ value: T) -> (Data, URLResponse) {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = (try? encoder.encode(value)) ?? Data("{}".utf8)
        return (data, response(url, status))
    }

    static func response(_ url: URL, _ status: Int) -> HTTPURLResponse {
        HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1",
                        headerFields: ["Content-Type": "application/json"])!
    }
}
