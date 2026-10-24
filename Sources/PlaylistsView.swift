import SwiftUI

/// Playlists (created and edited in the web remote): list → one playlist → play / shuffle / start
/// from a song. Same layout as the folder browser. Reloads when the server announces a change
/// (connection.playlistsVersion), never on a timer.
struct PlaylistsView: View {
    @EnvironmentObject var connection: ServerConnection
    @State private var playlists: [PlaylistInfo]?
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            if let playlists {
                if playlists.isEmpty {
                    Text("No playlists yet. Create them in the web remote: \(Config.remoteAddress) → Playlists.")
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
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle("Playlists")
        .task(id: connection.playlistsVersion) { await load() }
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

/// One playlist: Play / Shuffle, then its entries in order. Selecting an entry plays the playlist
/// from there. Missing files (deleted on the NAS) are shown greyed out and skipped by the server.
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
                            Label("Play (\(playable))", systemImage: "play.fill")
                        }
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
