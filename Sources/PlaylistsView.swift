import SwiftUI

/// Playlists (created and edited in the web remote): list → one playlist → play / shuffle / start
/// from a song. Same layout as the folder browser. Reloads when the server announces a change
/// (connection.playlistsVersion), never on a timer. On top: the auto-playlists ("For you").
struct PlaylistsView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var locator: ServerLocator
    @State private var playlists: [PlaylistInfo]?
    @State private var errorText: String?
    @State private var autoLists: [AutoList] = []
    @State private var libraryVersion = LocalLibrary.shared.version

    var body: some View {
        List {
            if !autoLists.isEmpty {
                Section("For you") {
                    ForEach(autoLists) { list in
                        NavigationLink {
                            AutoListView(list: list)
                        } label: {
                            RowLabel(title: list.name, detail: list.detail, systemImage: list.symbol)
                        }
                    }
                }
            }
            Section {
                if let errorText {
                    Text(errorText).foregroundStyle(.secondary)
                }
                if let playlists {
                    if playlists.isEmpty {
                        Text(locator.localMode ? "Playlists need the media server."
                             : "No playlists yet. Create them in the web remote: \(Config.remoteAddress) → Playlists.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(playlists) { playlist in
                        NavigationLink {
                            PlaylistView(playlistID: playlist.id, title: playlist.name)
                        } label: {
                            RowLabel(title: playlist.name, detail: playlist.detail, systemImage: "music.note.list")
                        }
                    }
                }
            } header: {
                if !autoLists.isEmpty { Text("Playlists") }
            }
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle("Playlists")
        .task(id: connection.playlistsVersion) { await load() }
        .task(id: "\(locator.localMode)|\(connection.autoVersion)|\(libraryVersion)") { autoLists = await AutoList.load() }
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
    }

    private func load() async {
        do {
            let (data, _) = try await Net.data(from: .api("api/playlists"))
            playlists = try JSONDecoder.server().decode(PlaylistList.self, from: data).playlists
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// One playlist: Play, then its entries in order. Selecting an entry plays it first, the rest of the playlist follows
/// shuffled. Missing files (deleted on the NAS) are shown greyed out and skipped by the server.
struct PlaylistView: View {
    @EnvironmentObject var connection: ServerConnection
    let playlistID: Int
    let title: String
    @State private var detail: PlaylistDetail?
    @State private var gone = false
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            if gone {
                Text("This playlist was deleted.").foregroundStyle(.secondary)
            }
            if let detail {
                let playable = detail.items.filter { !$0.isMissing }.count
                Section {
                    if playable > 0 {
                        Button { play() } label: {
                            RowLabel(title: PlayLabels.play, detail: PlayLabels.count(playable), systemImage: "play.fill")
                        }
                        .accessibilityIdentifier("play")  // UI tests
                    } else {
                        Text("Empty. Add songs and videos in the web remote: \(Config.remoteAddress)")
                            .foregroundStyle(.secondary)
                    }
                }
                if !detail.items.isEmpty {
                    Section("Songs and videos") {
                        // By position: the same song can be in a playlist twice.
                        ForEach(Array(detail.items.enumerated()), id: \.offset) { index, entry in
                            if entry.isMissing {
                                RowLabel(title: entry.title, detail: "file was deleted or moved",
                                         systemImage: "exclamationmark.triangle")
                                    .opacity(0.45)
                            } else {
                                Button { play(startIndex: index) } label: {
                                    RowLabel(
                                        title: entry.title,
                                        detail: [entry.artist, entry.lossless == true ? "lossless" : nil]
                                            .compactMap { $0 }.joined(separator: " · "),
                                        systemImage: entry.isVideo ? "film" : "music.note",
                                        item: entry.asMediaItem
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle(detail?.name ?? title)
        .task(id: connection.playlistsVersion) { await load() }
    }

    /// startIndex = position in the stored playlist (the server maps it past missing entries).
    /// Starts shuffled with repeat all (startPlayback); a picked entry plays first.
    private func play(startIndex: Int? = nil) {
        var args: [String: Any] = ["collection": "playlist", "playlist": playlistID]
        if let startIndex { args["start_index"] = startIndex }
        connection.startPlayback(args)
    }

    private func load() async {
        do {
            let (data, response) = try await Net.data(from: .api("api/playlists/\(playlistID)"))
            if (response as? HTTPURLResponse)?.statusCode == 404 {
                gone = true
                detail = nil
                return
            }
            detail = try JSONDecoder.server().decode(PlaylistDetail.self, from: data)
            connection.send(["type": "log", "message": "playlist \(playlistID) shown: \(detail?.items.count ?? 0) entries (update \(connection.playlistsVersion))"])
            gone = false
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// One auto-playlist (Loved, Most played, Recently added, Not heard in a while, On this day): Play (shuffled), then its
/// items; an item plays first, the rest of the list follows shuffled. Reloads when a ♥ changes (connection.autoVersion)
/// and, without the server, after a rescan.
struct AutoListView: View {
    @EnvironmentObject var connection: ServerConnection
    let list: AutoList
    @State private var items: [MediaItem]?
    @State private var errorText: String?
    @State private var libraryVersion = LocalLibrary.shared.version

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            if let items {
                Section {
                    if items.isEmpty {
                        // A button that does nothing: a Text cannot take focus, and a screen without a focusable view
                        // (nothing playing: no Now Playing card) leaves Back to the system.
                        Button {} label: {
                            Text(list.key == "loved" ? "Nothing loved yet. Hold the centre button in the player: Love."
                                 : "Nothing here yet.")
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        Button { play() } label: {
                            RowLabel(title: PlayLabels.play, detail: PlayLabels.count(items.count), systemImage: "play.fill")
                        }
                        .accessibilityIdentifier("play")  // UI tests
                    }
                }
                if !items.isEmpty {
                    Section(list.name) {
                        ForEach(items) { item in
                            Button { play(from: item.id) } label: {
                                RowLabel(title: item.title, detail: item.artist ?? "",
                                         systemImage: item.kind == "video" ? "film" : "music.note", item: item)
                            }
                        }
                    }
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle(list.name)
        .task(id: "\(connection.autoVersion)|\(libraryVersion)") { await load() }
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
    }

    /// Starts shuffled with repeat all (startPlayback); a picked item plays first.
    private func play(from id: Int? = nil) {
        var args: [String: Any] = ["collection": list.key]
        if let id { args["start_id"] = id }
        connection.startPlayback(args)
    }

    private func load() async {
        do {
            items = try await AutoList.items(list.key)
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}
