import SwiftUI

/// Tabs: Library (shuffle, search, music videos, folders), Playlists, Queue, Settings.
/// A mini player sits above the tab bar whenever something is in the queue.
struct PhoneRootView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    @Environment(\.appTheme) private var theme
    @Namespace private var playerNS
    @State private var showPlayer = false
    /// Full-screen video while the player sheet is up: the cover is presented FROM INSIDE the sheet
    /// (PhoneNowPlayingView), so leaving full screen simply uncovers the sheet with its controls.
    @State private var fullScreen = false
    /// Full-screen video while the sheet is closed (mini player / library): presented here, so landscape
    /// never shows the sheet first. When it closes in portrait, the sheet opens (cover's onDismiss).
    @State private var rootFullScreen = false
    /// A cover can only be presented from the sheet once its own presentation has finished.
    @State private var sheetShownAt = Date.distantPast
    @State private var tab = PhoneDemo.initialTab

    /// iPhones have a compact vertical size class exactly in landscape (follows the rotation lock too).
    private var landscape: Bool { verticalSizeClass == .compact }
    private var hasVideo: Bool { engine.current?.isVideo == true }

    var body: some View {
        TabView(selection: $tab) {
            LibraryTab()
                .tabItem { Label("Library", systemImage: "music.note.house") }
                .tag(0)
            NavigationStack { PhonePlaylistsView().demoPush("playlist") }
                .tabItem { Label("Playlists", systemImage: "music.note.list") }
                .tag(1)
            QueueTab()
                .tabItem { Label("Queue", systemImage: "list.number") }
                .tag(2)
            NavigationStack {
                PhoneSettingsView()
                    .demoPush("eq").demoPush("theme").demoPush("server")
                    .demoPush("sound").demoPush("playback").demoPush("lyrics").demoPush("about")
            }
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(3)
        }
        .environment(\.showPlayer, { showPlayer = true })
        .environment(\.playerNamespace, playerNS)
        .onAppear {
            if DemoRoute.is("player") { DemoRoute.whenLoaded(engine) { showPlayer = true } }
            if DemoRoute.is("open-player") { DemoRoute.whenLoaded(engine, after: 5) { showPlayer = true } }
        }
        .sheet(isPresented: $showPlayer) {
            PhoneNowPlayingView(fullScreen: $fullScreen, leaveFullScreen: leaveFullScreen)
                .environmentObject(artwork)
                .environment(\.appTheme, theme)
                .environment(\.playerNamespace, playerNS)
        }
        .fullScreenCover(isPresented: $rootFullScreen, onDismiss: {
            if !landscape, engine.current != nil { showPlayer = true }
        }) {
            FullScreenVideo(transport: Transport(connection: connection, engine: engine), leave: leaveFullScreen)
                .environmentObject(engine)
                .onAppear { engine.setLyricsCover(false) }  // the closed sheet may have left the lyrics cover on
        }
        // A video picked here starts on this iPhone: in landscape straight in full screen, in portrait the
        // player sheet.
        .onChange(of: engine.startCounter) {
            guard hasVideo else { return }
            OrientationLock.shared.release("video started")
            if landscape {
                openFullScreen()
            } else if OrientationLock.shared.rotatingToLandscape {
                engine.diag("full screen: video started with the phone sideways, waiting for the rotation")
            } else if !rootFullScreen {
                showPlayer = true
            }
        }
        // In landscape a video is only ever shown full screen. Back to portrait: the cover goes away and the
        // portrait player sheet shows the controls.
        .onChange(of: verticalSizeClass) {
            if landscape {
                if hasVideo { openFullScreen() }
            } else {
                let wasRootCover = rootFullScreen
                fullScreen = false
                rootFullScreen = false
                if hasVideo, !wasRootCover { showPlayer = true }  // root cover: its onDismiss opens the sheet
            }
        }
        .onChange(of: showPlayer) {
            if showPlayer { sheetShownAt = Date() }
        }
        // next / previous / auto-advance (no startCounter), and the same item turning audio only
        .onChange(of: engine.current?.id) { itemChanged() }
        .onChange(of: hasVideo) { itemChanged() }
    }

    private func itemChanged() {
        guard let item = engine.current else { return }
        if item.isVideo {
            OrientationLock.shared.release("video changed")
            if landscape { openFullScreen() }  // e.g. a song in landscape moved on to a video
        } else if fullScreen || rootFullScreen {
            leaveFullScreen("no picture (song or audio only) in full screen, back to portrait")
        }
    }

    private func openFullScreen() {
        guard hasVideo, !fullScreen, !rootFullScreen else { return }
        if showPlayer {
            let wait = 0.6 - Date().timeIntervalSince(sheetShownAt)
            if wait > 0 {
                // the sheet is still sliding up: UIKit would refuse the cover and the flag would stay set
                engine.diag("full screen: waiting \(Int(wait * 1000)) ms for the player sheet")
                DispatchQueue.main.asyncAfter(deadline: .now() + wait) {
                    if landscape { openFullScreen() }
                }
                return
            }
            fullScreen = true
        } else {
            rootFullScreen = true
        }
    }

    /// Portrait button in full screen, or a song reached in full screen: rotate to portrait and hold it
    /// while the phone is still sideways; the rotation closes the cover (onChange of verticalSizeClass).
    private func leaveFullScreen(_ reason: String) {
        engine.diag("full screen: \(reason)")
        guard landscape else {
            // full screen opened in portrait (button in the sheet): just close it
            fullScreen = false
            rootFullScreen = false
            return
        }
        OrientationLock.shared.holdPortrait()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            guard fullScreen || rootFullScreen else { return }
            engine.diag("full screen: still \(landscape ? "landscape" : "portrait") after the portrait request, closing")
            fullScreen = false
            rootFullScreen = false
        }
    }
}

private struct ShowPlayerKey: EnvironmentKey {
    static let defaultValue: () -> Void = {}
}

extension EnvironmentValues {
    var showPlayer: () -> Void {
        get { self[ShowPlayerKey.self] }
        set { self[ShowPlayerKey.self] = newValue }
    }
}

extension View {
    /// Mini player above the tab bar, on every tab's root and pushed screens.
    func withMiniPlayer() -> some View {
        safeAreaInset(edge: .bottom, spacing: 0) { MiniPlayer() }
    }
}

// MARK: - Library

struct LibraryTab: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var library: LibraryStore
    @State private var query = ""

    @Environment(\.appTheme) private var theme

    var body: some View {
        NavigationStack {
            Group {
                if !theme.isClassic && query.trimmingCharacters(in: .whitespaces).isEmpty {
                    PhoneHome()
                } else {
                    classicList
                }
            }
            .searchable(text: $query, prompt: "Title, artist, album, folder")
            .navigationTitle("ATV Player")
            .demoPush("videos")
            .demoPush("folder")
            .withMiniPlayer()
            .task { await library.load() }
        }
    }

    private var classicList: some View {
            List {
                if query.trimmingCharacters(in: .whitespaces).isEmpty {
                    Section("Browse") {
                        NavigationLink { PhoneFolderRootsView() } label: {
                            Label("Folders", systemImage: "folder")
                        }
                        NavigationLink { PhoneVideoListView() } label: {
                            Label("Music videos", systemImage: "play.rectangle")
                        }
                    }
                    Section {
                        Button { shuffle("all") } label: { Label("Shuffle everything", systemImage: "shuffle") }
                        Button { shuffle("music") } label: { Label("Shuffle music", systemImage: "music.note") }
                        Button { shuffle("videos") } label: { Label("Shuffle music videos", systemImage: "film") }
                    }
                } else {
                    SearchResults(query: query).themedRow()
                }
            }
            .phoneThemed()
    }

    private func shuffle(_ name: String) {
        connection.command("play_collection", ["collection": name, "shuffle": true])
    }
}

struct SearchResults: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var library: LibraryStore
    let query: String

    var body: some View {
        let results = Array(library.search(query).prefix(300))
        if let error = library.errorText {
            Text(error).foregroundStyle(.secondary)
        } else if results.isEmpty {
            Text(library.items.isEmpty ? "Loading the library…" : "Nothing found.").foregroundStyle(.secondary)
        } else {
            Section("\(results.count == 300 ? "First 300" : "\(results.count)") results") {
                ForEach(Array(results.enumerated()), id: \.element.id) { index, item in
                    Button {
                        connection.command("play_ids", ["ids": results.map(\.id), "start_index": index])
                    } label: {
                        ItemRow(item: item)
                    }
                    .itemMenu(item)
                }
            }
        }
    }
}

/// Long press on a song or video: play next / add to the end of the queue.
struct ItemMenu: ViewModifier {
    @EnvironmentObject var connection: ServerConnection
    let item: MediaItem

    func body(content: Content) -> some View {
        content.contextMenu {
            Button { connection.command("play_ids", ["ids": [item.id]]) } label: {
                Label("Play now", systemImage: "play")
            }
            Button { connection.command("enqueue", ["ids": [item.id], "mode": "next"]) } label: {
                Label("Play next", systemImage: "text.insert")
            }
            Button { connection.command("enqueue", ["ids": [item.id], "mode": "end"]) } label: {
                Label("Add to queue", systemImage: "text.append")
            }
        }
    }
}

extension View {
    func itemMenu(_ item: MediaItem) -> some View { modifier(ItemMenu(item: item)) }
}

struct ItemRow: View {
    let item: MediaItem
    var highlighted = false
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var engine: PlayerEngine

    var body: some View {
        HStack(spacing: 12) {
            if theme.isClassic {
                Image(systemName: highlighted ? "speaker.wave.2.fill" : (item.isVideo ? "film" : "music.note"))
                    .frame(width: 24)
                    .foregroundStyle(highlighted ? Color.accentColor : .secondary)
            } else {
                ZStack {
                    ItemThumb(item: item, corner: theme == .noir ? 2 : 6)
                        .frame(width: item.isVideo ? 64 : 44, height: item.isVideo ? 36 : 44)
                    if highlighted || engine.current?.id == item.id {
                        RoundedRectangle(cornerRadius: 6).fill(.black.opacity(0.45))
                        EqualizerBars(animating: engine.isPlaying, color: .white, bars: 3, width: 3, height: 16, spacing: 2)
                    }
                }
                .frame(width: item.isVideo ? 64 : 44, height: item.isVideo ? 36 : 44)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).lineLimit(1).foregroundStyle(highlighted ? Color.accentColor : .primary)
                let detail = [item.artist, item.lossless == true ? "lossless" : nil].compactMap { $0 }
                    .filter { !$0.isEmpty }.joined(separator: " · ")
                if !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            if let duration = item.duration, duration > 0 {
                Text(formatTime(duration)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
    }
}

struct PhoneVideoListView: View {
    @EnvironmentObject var connection: ServerConnection
    @State private var items: [MediaItem] = []
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText { Text(errorText).foregroundStyle(.secondary) }
            Button {
                connection.command("play_collection", ["collection": "videos", "shuffle": true])
            } label: {
                Label("Shuffle all music videos", systemImage: "shuffle")
            }
            ForEach(items) { item in
                Button {
                    connection.command("play_collection", ["collection": "videos", "start_id": item.id])
                } label: {
                    ItemRow(item: item)
                }
                .itemMenu(item)
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Music videos")
        .withMiniPlayer()
        .task {
            do {
                let (data, _) = try await Net.data(from: .api("api/collection/videos"))
                items = try JSONDecoder.server().decode(ItemList.self, from: data).items
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

struct PhoneFolderRootsView: View {
    @State private var roots: [RootInfo] = []
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText { Text(errorText).foregroundStyle(.secondary) }
            ForEach(roots) { root in
                NavigationLink {
                    PhoneFolderView(root: root.root, folder: "", title: root.name)
                } label: {
                    Label("\(root.name) (\(root.count))", systemImage: "externaldrive")
                }
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Folders")
        .withMiniPlayer()
        .task {
            do {
                let (data, _) = try await Net.data(from: .api("api/browse"))
                roots = try JSONDecoder.server().decode(RootList.self, from: data).roots
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

/// One folder: Play / Shuffle (everything below it or only this folder), subfolders, files.
struct PhoneFolderView: View {
    @EnvironmentObject var connection: ServerConnection
    let root: String
    let folder: String
    let title: String
    @State private var result: BrowseResult?
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText { Text(errorText).foregroundStyle(.secondary) }
            if let result {
                Section {
                    Button { play(shuffle: true) } label: {
                        Label(result.hasSubfolders ? "Shuffle all, with subfolders (\(result.total))" : "Shuffle (\(result.total))",
                              systemImage: "shuffle")
                    }
                    Button { play(shuffle: false) } label: {
                        Label(result.hasSubfolders ? "Play all, with subfolders" : "Play", systemImage: "play.fill")
                    }
                    if result.hasSubfolders && !result.items.isEmpty {
                        Button { play(shuffle: true, recursive: false) } label: {
                            Label("Shuffle this folder only (\(result.items.count))", systemImage: "shuffle")
                        }
                    }
                }
                .themedRow()
                if !result.subfolders.isEmpty {
                    Section("Folders") {
                        ForEach(result.subfolders) { sub in
                            NavigationLink {
                                PhoneFolderView(root: root, folder: sub.folder, title: sub.name)
                            } label: {
                                Label("\(sub.name) (\(sub.count))", systemImage: "folder")
                            }
                        }
                    }
                    .themedRow()
                }
                if !result.items.isEmpty {
                    Section("Files") {
                        ForEach(result.items) { item in
                            Button { play(shuffle: false, recursive: false, startAt: item.id) } label: {
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
        .navigationTitle(title)
        .withMiniPlayer()
        .task {
            do {
                let (data, _) = try await Net.data(from: .api("api/browse", ["root": root, "folder": folder]))
                result = try JSONDecoder.server().decode(BrowseResult.self, from: data)
            } catch {
                errorText = error.localizedDescription
            }
        }
    }

    private func play(shuffle: Bool, recursive: Bool = true, startAt: Int? = nil) {
        var args: [String: Any] = [
            "collection": "folder", "root": root, "folder": folder, "shuffle": shuffle, "recursive": recursive,
        ]
        if let startAt { args["start_id"] = startAt }
        connection.command("play_collection", args)
    }
}

// MARK: - Playlists

struct PhonePlaylistsView: View {
    @EnvironmentObject var connection: ServerConnection
    @Environment(\.appTheme) private var theme
    @State private var playlists: [PlaylistInfo]?
    @State private var covers: [Int: [MediaItem]] = [:]
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText { Text(errorText).foregroundStyle(.secondary) }
            if let playlists {
                if playlists.isEmpty {
                    Text("No playlists yet. Create them in the web remote: \(Config.remoteAddress) → Playlists.")
                        .foregroundStyle(.secondary)
                }
                ForEach(playlists) { playlist in
                    NavigationLink {
                        PhonePlaylistView(playlistID: playlist.id, title: playlist.name)
                    } label: {
                        HStack(spacing: 12) {
                            if !theme.isClassic {
                                PlaylistMosaic(playlistID: playlist.id, items: covers[playlist.id] ?? [])
                                    .frame(width: 52, height: 52)
                                    .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 2 : 8))
                            }
                            VStack(alignment: .leading, spacing: 2) {
                                Text(playlist.name)
                                Text(playlist.detail).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle("Playlists")
        .withMiniPlayer()
        .refreshable { await load() }
        .task(id: connection.playlistsVersion) { await load() }
    }

    private func load() async {
        do {
            let (data, _) = try await Net.data(from: .api("api/playlists"))
            let list = try JSONDecoder.server().decode(PlaylistList.self, from: data).playlists
            playlists = list
            errorText = nil
            guard !theme.isClassic else { return }
            for playlist in list.prefix(40) {
                if let (d, _) = try? await Net.data(from: .api("api/playlists/\(playlist.id)")),
                   let detail = try? JSONDecoder.server().decode(PlaylistDetail.self, from: d) {
                    covers[playlist.id] = detail.items.filter { !$0.isMissing }.prefix(4).map(\.asMediaItem)
                }
            }
        } catch {
            errorText = error.localizedDescription
        }
    }
}

struct PhonePlaylistView: View {
    @EnvironmentObject var connection: ServerConnection
    @Environment(\.appTheme) private var theme
    let playlistID: Int
    let title: String
    @State private var detail: PlaylistDetail?
    @State private var gone = false
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText { Text(errorText).foregroundStyle(.secondary) }
            if gone { Text("This playlist was deleted.").foregroundStyle(.secondary) }
            if let detail {
                let playable = detail.items.filter { !$0.isMissing }.count
                Section {
                    if playable > 0 {
                        Button { play(shuffle: false) } label: { Label("Play (\(playable))", systemImage: "play.fill") }
                        Button { play(shuffle: true) } label: { Label("Shuffle", systemImage: "shuffle") }
                    } else {
                        Text("Empty. Add songs and videos in the web remote.").foregroundStyle(.secondary)
                    }
                }
                .themedRow()
                Section("Songs and videos") {
                    // By position: the same song can be in a playlist twice.
                    ForEach(Array(detail.items.enumerated()), id: \.offset) { index, entry in
                        if entry.isMissing {
                            Label("\(entry.title) — file was deleted or moved", systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.secondary)
                        } else {
                            Button { play(shuffle: false, startIndex: index) } label: {
                                if theme.isClassic {
                                    HStack(spacing: 12) {
                                        Image(systemName: entry.isVideo ? "film" : "music.note")
                                            .frame(width: 24).foregroundStyle(.secondary)
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(entry.title).lineLimit(1)
                                            if let artist = entry.artist, !artist.isEmpty {
                                                Text(artist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                            }
                                        }
                                    }
                                } else {
                                    ItemRow(item: entry.asMediaItem)
                                }
                            }
                        }
                    }
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle(detail?.name ?? title)
        .withMiniPlayer()
        .task(id: connection.playlistsVersion) { await load() }
    }

    /// startIndex = position in the stored playlist (the server maps it past missing entries).
    private func play(shuffle: Bool, startIndex: Int? = nil) {
        var args: [String: Any] = ["collection": "playlist", "playlist": playlistID, "shuffle": shuffle]
        if let startIndex { args["start_index"] = startIndex }
        connection.command("play_collection", args)
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
            gone = false
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}

// MARK: - Queue

struct QueueTab: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var library: LibraryStore

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                List {
                    let queue = connection.state?.queue ?? []
                    let current = connection.state?.index ?? -1
                    if queue.isEmpty {
                        Text("The queue is empty. Pick something in Library or Playlists.").foregroundStyle(.secondary)
                    }
                    ForEach(Array(queue.enumerated()), id: \.offset) { index, id in
                        Button { connection.command("jump", ["index": index]) } label: {
                            if let item = library.byID[id] ?? (index == current ? connection.state?.current : nil) {
                                ItemRow(item: item, highlighted: index == current)
                            } else {
                                Text("Item #\(id)").foregroundStyle(.secondary)
                            }
                        }
                        .id(index)
                        .themedRow()
                        .swipeActions {
                            if index != current {
                                Button(role: .destructive) {
                                    connection.command("remove", ["index": index])
                                } label: {
                                    Label("Remove", systemImage: "trash")
                                }
                            }
                        }
                    }
                }
                .phoneThemed()
                .navigationTitle("Queue")
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Clear") { connection.command("clear") }
                            .disabled((connection.state?.queue ?? []).count < 2)
                    }
                }
                .withMiniPlayer()
                .task { await library.load() }
                .onAppear {
                    if let index = connection.state?.index, index >= 0 { proxy.scrollTo(index, anchor: .center) }
                }
            }
        }
    }
}
