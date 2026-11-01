import SwiftUI

/// One auto-playlist (Loved, Most played, Recently added, Not heard in a while, On this day): Play in order or
/// Shuffle, then its items; a row plays the list in order from there. Reloads when a ♥ changes
/// (connection.autoVersion) and, without the server, when the on-device library changes.
struct PhoneAutoListView: View {
    @EnvironmentObject var connection: ServerConnection
    let list: AutoList
    @State private var items: [MediaItem]?
    @State private var errorText: String?
    @State private var libraryVersion = LocalLibrary.shared.version

    var body: some View {
        List {
            if let errorText { Text(errorText).foregroundStyle(.secondary) }
            if let items {
                Section {
                    if items.isEmpty {
                        Text(list.key == "loved" ? "Nothing loved yet. Tap ♥ in the player, or long-press a song: Love."
                             : "Nothing here yet.")
                            .foregroundStyle(.secondary)
                    } else {
                        Button { play(shuffle: false) } label: { Label("Play (\(items.count))", systemImage: "play.fill") }
                        Button { play(shuffle: true) } label: { Label("Shuffle", systemImage: "shuffle") }
                    }
                }
                .themedRow()
                if !items.isEmpty {
                    Section {
                        ForEach(items) { item in
                            Button { play(shuffle: false, from: item.id) } label: {
                                ItemRow(item: item)
                            }
                            .itemMenu(item)
                        }
                    }
                    .themedRow()
                }
            }
        }
        .phoneThemed()
        .navigationTitle(list.name)
        .withMiniPlayer()
        .refreshable { await load() }
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
        .task(id: "\(connection.autoVersion)|\(libraryVersion)") { await load() }
    }

    private func play(shuffle: Bool, from id: Int? = nil) {
        var args: [String: Any] = ["collection": list.key, "shuffle": shuffle]
        if let id { args["start_id"] = id }
        connection.command("play_collection", args)
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

/// Row label of an auto-playlist in the Playlists tab: a cover mosaic of its first items (themed looks) or its symbol.
struct AutoListRowLabel: View {
    let list: AutoList
    let index: Int
    let items: [MediaItem]
    @Environment(\.appTheme) private var theme

    var body: some View {
        HStack(spacing: 12) {
            if theme.isClassic {
                Image(systemName: list.symbol).frame(width: 24).foregroundStyle(Color.accentColor)
            } else {
                PlaylistMosaic(playlistID: 1_000_000 + index, items: Array(items.prefix(4)))
                    .frame(width: 52, height: 52)
                    .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 2 : 8))
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(list.key == "loved" ? "♥ \(list.name)" : list.name)
                Text(list.detail).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

/// The auto-playlists and the first items of each (covers), for Home and the Playlists tab.
@MainActor
func loadAutoLists(covers: Bool) async -> (lists: [AutoList], items: [String: [MediaItem]]) {
    let lists = await AutoList.load()
    var found: [String: [MediaItem]] = [:]
    if covers {
        for list in lists where list.count > 0 {
            found[list.key] = Array(((try? await AutoList.items(list.key)) ?? []).prefix(4))
        }
    }
    return (lists, found)
}
