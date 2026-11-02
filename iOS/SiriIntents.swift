import AppIntents
import Foundation

// Siri / Shortcuts (App Intents in the app itself: no extension, no Siri capability, works with a free Apple ID).
// "Play <list> in ATV Player" starts a saved playlist or an auto list through startPlayback (shuffled, repeat all).

/// One list Siri can start: an auto list (Loved, Most played) or a saved playlist.
struct SiriList: Codable, Hashable {
    /// "auto:loved", "auto:most_played", "playlist:<id>"
    let id: String
    let name: String
    /// Items in it; nil = not known (cached entry).
    var count: Int?

    var arguments: [String: Any] {
        if id.hasPrefix("playlist:"), let number = Int(id.dropFirst("playlist:".count)) {
            return ["collection": "playlist", "playlist": number]
        }
        return ["collection": String(id.dropFirst("auto:".count))]
    }

    static let loved = SiriList(id: "auto:loved", name: "Loved", count: nil)
    static let mostPlayed = SiriList(id: "auto:most_played", name: "Most played", count: nil)

    /// Case- and diacritic-insensitive, without a leading "♥ " or "my ".
    static func key(_ text: String) -> String {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("♥") { s = String(s.dropFirst()).trimmingCharacters(in: .whitespaces) }
        s = s.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: nil)
        if s.hasPrefix("my ") { s = String(s.dropFirst(3)) }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// The lists Siri knows: Loved, Most played (when the server lists it), then the saved playlists A–Z. The last list
/// read is kept, so a phrase still resolves while the server is off (the intent then says it can't reach it).
@MainActor
enum SiriCatalog {
    private static let cacheKey = "siriLists"

    static var cached: [SiriList] {
        guard let data = UserDefaults.standard.data(forKey: cacheKey),
              let lists = try? JSONDecoder().decode([SiriList].self, from: data), !lists.isEmpty else { return [.loved] }
        return lists
    }

    /// Short timeout: an unreachable address (home LAN address on mobile data) must not keep Siri waiting for
    /// URLSession's default 60 s.
    private static func fetch(_ path: String) async -> Data? {
        let url = URL.api(path)
        if Config.localMode {
            guard let (data, response) = try? await Net.data(from: url),
                  ((response as? HTTPURLResponse)?.statusCode ?? 200) == 200 else { return nil }
            return data
        }
        var request = Net.request(url)
        request.timeoutInterval = 4
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }

    /// Reads the lists from the queue owner (server, or the on-device library); nil when it does not answer.
    static func live() async -> [SiriList]? {
        guard await PhoneServices.shared.waitForAddress(timeout: 5),
              let data = await fetch("api/auto"),
              let auto = try? JSONDecoder.server().decode([AutoList].self, from: data) else { return nil }
        var lists = [SiriList(id: SiriList.loved.id, name: SiriList.loved.name,
                              count: auto.first { $0.key == "loved" }?.count ?? 0)]
        if let most = auto.first(where: { $0.key == "most_played" }) {
            lists.append(SiriList(id: SiriList.mostPlayed.id, name: SiriList.mostPlayed.name, count: most.count))
        }
        if !Config.localMode {  // playlists need the server
            guard let pdata = await fetch("api/playlists"),
                  let playlists = try? JSONDecoder.server().decode(PlaylistList.self, from: pdata).playlists
            else { return nil }
            lists += playlistEntries(playlists)
        }
        remember(lists)
        return lists
    }

    private static func playlistEntries(_ playlists: [PlaylistInfo]) -> [SiriList] {
        playlists
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            .map { SiriList(id: "playlist:\($0.id)", name: $0.name, count: max(0, $0.count - $0.missing)) }
    }

    /// Live lists, else the cached ones.
    static func lists() async -> [SiriList] {
        await live() ?? cached
    }

    /// Re-read after a (re)connect or a playlist / ♥ change.
    static func refresh() async {
        _ = await live()
    }

    /// A playlist screen loaded the playlists: update the saved ones (keeps the auto lists).
    static func remember(playlists: [PlaylistInfo]) {
        guard !Config.localMode else { return }
        remember(cached.filter { $0.id.hasPrefix("auto:") } + playlistEntries(playlists))
    }

    /// Stores the lists; when the names changed, Siri is told to learn them again.
    private static func remember(_ lists: [SiriList]) {
        let oldNames = cached.map(\.name)
        guard let data = try? JSONEncoder().encode(lists) else { return }
        UserDefaults.standard.set(data, forKey: cacheKey)
        if lists.map(\.name) != oldNames {
            ATVPlayerShortcuts.updateAppShortcutParameters()
        }
    }

    static func find(_ name: String, in lists: [SiriList]) -> [SiriList] {
        let wanted = SiriList.key(name)
        guard !wanted.isEmpty else { return [] }
        let exact = lists.filter { SiriList.key($0.name) == wanted }
        if !exact.isEmpty { return exact }
        if wanted == "loved songs" || wanted == "favorites" || wanted == "favourites" {
            return lists.filter { $0.id == SiriList.loved.id }
        }
        return lists.filter { SiriList.key($0.name).contains(wanted) }
    }
}

enum SiriError: Error, CustomLocalizedStringResourceConvertible {
    case unreachable
    case notFound(String)
    case empty(String)
    case nothingPlaying

    var localizedStringResource: LocalizedStringResource {
        switch self {
        case .unreachable: return "ATV Player can't reach the media server"
        case .notFound(let name): return "No playlist called \(name)"
        case .empty(let name): return "\(name) is empty"
        case .nothingPlaying: return "Nothing is playing in ATV Player"
        }
    }
}

struct PlayListEntity: AppEntity {
    static var typeDisplayRepresentation: TypeDisplayRepresentation = "List"
    static var defaultQuery = PlayListQuery()

    let id: String
    let name: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }

    init(_ list: SiriList) {
        id = list.id
        name = list.name
    }
}

struct PlayListQuery: EntityStringQuery {
    @MainActor
    func entities(for identifiers: [PlayListEntity.ID]) async throws -> [PlayListEntity] {
        let lists = await SiriCatalog.lists()
        return identifiers.compactMap { id in lists.first { $0.id == id }.map(PlayListEntity.init) }
    }

    @MainActor
    func entities(matching string: String) async throws -> [PlayListEntity] {
        let found = SiriCatalog.find(string, in: await SiriCatalog.lists())
        if found.isEmpty { throw SiriError.notFound(string) }
        return found.map(PlayListEntity.init)
    }

    @MainActor
    func suggestedEntities() async throws -> [PlayListEntity] {
        var lists = await SiriCatalog.lists()
        // Most played is offered even before the server lists it (the cache may be older than the first plays).
        if !lists.contains(where: { $0.id == SiriList.mostPlayed.id }) {
            lists.insert(.mostPlayed, at: min(1, lists.count))
        }
        return lists.map(PlayListEntity.init)
    }
}

/// Starts a list and waits until the phone's own player plays it (also with the app in the background or not running).
@MainActor
private func playList(id: String, name: String) async throws -> String {
    let services = PhoneServices.shared
    guard await services.connectForIntent(timeout: 10) else {
        services.engine.diag("siri: play \(name) -> server not reachable")
        throw SiriError.unreachable
    }
    guard let lists = await SiriCatalog.live() else {
        services.engine.diag("siri: play \(name) -> list lookup failed")
        throw SiriError.unreachable
    }
    guard let list = lists.first(where: { $0.id == id }) else {
        services.engine.diag("siri: play \(name) -> not found")
        // Most played is only listed once something was played.
        throw id == SiriList.mostPlayed.id ? SiriError.empty(name) : SiriError.notFound(name)
    }
    guard (list.count ?? 0) > 0 else {
        services.engine.diag("siri: play \(list.name) -> empty")
        throw SiriError.empty(list.name)
    }
    let engine = services.engine
    let before = engine.startCounter
    services.connection.startPlayback(list.arguments)
    // The queue owner answers with a load; wait until VLC really plays it.
    let deadline = Date().addingTimeInterval(10)
    while Date() < deadline, !(engine.startCounter != before && engine.isPlaying) {
        try? await Task.sleep(nanoseconds: 250_000_000)
    }
    let items = services.connection.state?.queue?.count ?? 0
    let loaded = engine.startCounter != before
    let playing = loaded && engine.isPlaying
    engine.diag("siri: play \(list.name) -> \(items) items, player \(playing ? "playing '\(engine.current?.title ?? "")'" : loaded ? "loaded, not playing yet after 10 s" : "got nothing to play after 10 s")")
    // No load from the queue owner: the command did not arrive. Say so instead of "Playing".
    guard loaded else { throw SiriError.unreachable }
    return list.name
}

struct PlayListIntent: AudioPlaybackIntent {
    static var title: LocalizedStringResource = "Play list"
    static var description = IntentDescription("Plays a playlist, Loved or Most played on this iPhone, shuffled.")
    static var openAppWhenRun = false

    @Parameter(title: "List")
    var list: PlayListEntity

    static var parameterSummary: some ParameterSummary {
        Summary("Play \(\.$list)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let name = try await playList(id: list.id, name: list.name)
        return .result(dialog: "Playing \(name)")
    }
}

struct PlayLovedIntent: AudioPlaybackIntent {
    static var title: LocalizedStringResource = "Play Loved"
    static var description = IntentDescription("Plays the ♥ Loved list on this iPhone, shuffled.")
    static var openAppWhenRun = false

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let name = try await playList(id: SiriList.loved.id, name: SiriList.loved.name)
        return .result(dialog: "Playing \(name)")
    }
}

struct PlayMostPlayedIntent: AudioPlaybackIntent {
    static var title: LocalizedStringResource = "Play Most played"
    static var description = IntentDescription("Plays the Most played list on this iPhone, shuffled.")
    static var openAppWhenRun = false

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let name = try await playList(id: SiriList.mostPlayed.id, name: SiriList.mostPlayed.name)
        return .result(dialog: "Playing \(name)")
    }
}

/// ♥ on the current item (same `love` command as the heart in the player).
struct ToggleLoveIntent: AppIntent {
    static var title: LocalizedStringResource = "Love this song"
    static var description = IntentDescription("Hearts or un-hearts what this iPhone is playing.")
    static var openAppWhenRun = false

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let services = PhoneServices.shared
        guard await services.connectForIntent(timeout: 10) else { throw SiriError.unreachable }
        guard let item = services.engine.current else { throw SiriError.nothingPlaying }
        let on = item.loved != true
        services.engine.diag("siri: love \(on ? "on" : "off") '\(item.title)'")
        services.connection.command("love", ["id": item.id, "on": on])
        return .result(dialog: on ? "Loved \(item.title)" : "Removed \(item.title) from Loved")
    }
}

struct ATVPlayerShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: PlayListIntent(),
            phrases: [
                "Play \(\.$list) in \(.applicationName)",
                "Shuffle \(\.$list) in \(.applicationName)",
                "Play my \(\.$list) in \(.applicationName)",
            ],
            shortTitle: "Play list",
            systemImageName: "music.note.list"
        )
        AppShortcut(
            intent: PlayLovedIntent(),
            phrases: [
                "Play Loved in \(.applicationName)",
                "Play my loved songs in \(.applicationName)",
            ],
            shortTitle: "Play Loved",
            systemImageName: "heart.fill"
        )
        AppShortcut(
            intent: PlayMostPlayedIntent(),
            phrases: ["Play Most played in \(.applicationName)"],
            shortTitle: "Play Most played",
            systemImageName: "chart.bar.fill"
        )
        AppShortcut(
            intent: ToggleLoveIntent(),
            phrases: ["Love this song in \(.applicationName)"],
            shortTitle: "Love this song",
            systemImageName: "heart"
        )
    }
}
