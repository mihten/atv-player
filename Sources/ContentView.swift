import SwiftUI

struct ContentView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var locator: ServerLocator
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @State private var showPlayer = false
    @State private var demoPush = false
    @State private var autoLists: [AutoList] = []
    @State private var libraryVersion = LocalLibrary.shared.version

    var body: some View {
        NavigationStack {
            Group {
                if theme.isClassic {
                    classicMenu
                } else {
                    HomeView()
                }
            }
            .navigationDestination(isPresented: $demoPush) {
                // CI screenshots only: a list screen, or Settings (optionally on a page)
                if let route = SettingsRoute.demo(DemoRoute.screen) {
                    SettingsView(start: route)
                } else {
                    DemoDestination()
                }
            }
        }
        .onAppear {
            if DemoRoute.is("player") {
                DemoRoute.whenLoaded(engine) { showPlayer = true }
            } else if DemoRoute.is("open-player") {
                DemoRoute.whenLoaded(engine, after: 5) { showPlayer = true }
            } else if DemoDestination.pushes || SettingsRoute.demo(DemoRoute.screen) != nil {
                demoPush = true
            }
        }
        // Play/Pause in any menu pauses/resumes in place (user feedback: it must not open the player;
        // the Now Playing card does that).
        .onPlayPauseCommand {
            if engine.current != nil { engine.togglePlay(source: "menu") }
        }
        .fullScreenCover(isPresented: $showPlayer) {
            NowPlayingView()
                .environmentObject(connection)
                .environmentObject(engine)
                .environmentObject(artwork)
                .environment(\.appTheme, theme)
        }
        .onChange(of: engine.startCounter) {
            showPlayer = true
        }
    }

    /// The original main menu (Classic look).
    private var classicMenu: some View {
        List {
            LocalLibraryHint()
            Section("Browse") {
                NavigationLink {
                    PlaylistsView()
                } label: {
                    Label("Playlists", systemImage: "music.note.list")
                }
                NavigationLink {
                    FolderRootsView()
                } label: {
                    Label("Folders", systemImage: "folder")
                }
                NavigationLink {
                    VideoListView()
                } label: {
                    Label("Music videos", systemImage: "play.rectangle")
                }
                NavigationLink {
                    SettingsView()
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
            }
            if !autoLists.isEmpty {
                Section("For you") {
                    ForEach(autoLists) { list in
                        NavigationLink {
                            AutoListView(list: list)
                        } label: {
                            Label(list.name, systemImage: list.symbol)
                        }
                    }
                }
            }
            Section {
                Button { shuffle("all") } label: {
                    Label("Shuffle everything", systemImage: "shuffle")
                }
                Button { shuffle("music") } label: {
                    Label("Shuffle music", systemImage: "music.note")
                }
                Button { shuffle("videos") } label: {
                    Label("Shuffle music videos", systemImage: "film")
                }
            }
        }
        .withNowPlaying()
        .navigationTitle("ATV Player")
        .task(id: "\(locator.localMode)|\(connection.autoVersion)|\(libraryVersion)") { autoLists = await AutoList.load() }
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
    }

    private func shuffle(_ name: String) {
        connection.startPlayback(["collection": name])
    }
}

struct VideoListView: View {
    @EnvironmentObject var connection: ServerConnection
    @State private var libraryVersion = LocalLibrary.shared.version
    @State private var items: [MediaItem] = []
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            LocalLibraryHint()
            Button {
                connection.startPlayback(["collection": "videos"])
            } label: {
                RowLabel(title: PlayLabels.play, detail: PlayLabels.count(items.count), systemImage: "play.fill")
            }
            .accessibilityIdentifier("play")  // UI tests
            ForEach(items) { item in
                Button {
                    connection.startPlayback(["collection": "videos", "start_id": item.id])
                } label: {
                    RowLabel(title: item.title, detail: item.artist ?? "", systemImage: "film", item: item)
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle("Music videos")
        .task(id: libraryVersion) { await load() }
        // no-server mode: reload after a rescan (only the version is observed, not every status change)
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
    }

    private func load() async {
        do {
            let url = Config.serverURL.appendingPathComponent("api/collection/videos")
            let (data, _) = try await Net.data(from: url)
            items = try JSONDecoder.server().decode(ItemList.self, from: data).items
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// Now Playing panel: right-hand sidebar on menu screens, left half in Settings.
struct NowPlayingSidebar: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @AppStorage(AppTheme.motionKey) private var motion = true
    var enabled = true

    var body: some View {
        if let item = engine.current {
            VStack(alignment: .leading, spacing: 20) {
                if theme.isClassic {
                    Text("Now playing").font(.system(size: 24, weight: .semibold)).opacity(0.6)
                } else {
                    HStack(spacing: 12) {
                        EqualizerBars(animating: engine.isPlaying && motion, color: theme.accent(artwork.palette),
                                      width: 4, height: 18)
                        Text(engine.isPlaying ? "NOW PLAYING" : "PAUSED")
                            .font(theme.eyebrow(20)).tracking(4)
                            .foregroundStyle(theme.accent(artwork.palette))
                    }
                }
                card(item)
                    .disabled(!enabled)
                if let upcoming = connection.state?.upcoming, !upcoming.isEmpty {
                    Text("Up next").font(theme.font(24, .semibold)).opacity(0.6).padding(.top, 10)
                    // themed rows are taller (thumbnails): three of them, so Settings still fits the screen
                    ForEach(upcoming.prefix(theme.isClassic ? 4 : 3)) { next in
                        if theme.isClassic {
                            Label(next.title, systemImage: next.isVideo ? "film" : "music.note")
                                .font(.system(size: 22)).lineLimit(1).opacity(0.85)
                        } else {
                            HStack(spacing: 14) {
                                ItemThumb(item: next, corner: 6).frame(width: 36, height: 36)
                                VStack(alignment: .leading, spacing: 0) {
                                    Text(next.title).font(theme.font(20, .medium)).lineLimit(1)
                                    Text(next.artist ?? "").font(theme.font(16)).opacity(0.55).lineLimit(1)
                                }
                            }
                            .opacity(0.9)
                        }
                    }
                }
                Spacer(minLength: 0)
            }
            .frame(width: 440, alignment: .leading)
            .padding(.top, 30)
            .focusSection()
        }
    }

    @ViewBuilder private func card(_ item: MediaItem) -> some View {
        if theme.isClassic {
            Button { engine.presentPlayer() } label: { classicCard(item) }
                .buttonStyle(.card)
        } else {
            Button { engine.presentPlayer() } label: { themedCard(item) }
                .buttonStyle(TileButtonStyle())
        }
    }

    private func classicCard(_ item: MediaItem) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            AsyncImage(url: item.artURL) { image in
                image.resizable().scaledToFill()
            } placeholder: {
                ZStack {
                    Color.white.opacity(0.08)
                    Image(systemName: item.isVideo ? "film" : "music.note").font(.system(size: 80))
                }
            }
            .frame(width: 400, height: item.isVideo ? 225 : 400)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            Text(item.title).font(.system(size: 28, weight: .semibold)).lineLimit(2)
            if !item.subtitle.isEmpty {
                Text(item.subtitle).font(.system(size: 22)).opacity(0.7).lineLimit(1)
            }
            SidebarProgress(clock: engine.clock, playing: engine.isPlaying)
        }
        .frame(width: 400, alignment: .leading)
        .padding(20)
    }

    private func themedCard(_ item: MediaItem) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            ZStack(alignment: .leading) {
                if theme == .vinyl && !item.isVideo {
                    VinylRecord(image: artwork.art(for: item.id),
                                spinning: engine.isPlaying && motion, size: 270,
                                labelTint: Palette.forItem(item.id).vibrant.with(brightness: 0.55).color)
                        .offset(x: 110)
                    CurrentArtwork(item: item, corner: 4)
                        .frame(width: 280, height: 280)
                        .shadow(color: .black.opacity(0.5), radius: 14, x: 8, y: 8)
                } else {
                    CurrentArtwork(item: item, corner: theme.corner * 0.7, showsTitle: !item.isVideo)
                        .frame(width: item.isVideo ? 400 : 320, height: item.isVideo ? 225 : 320)
                        .grayscale(theme == .noir && !engine.isPlaying ? 1 : 0)
                }
            }
            .frame(width: 400, height: item.isVideo ? 225 : (theme == .vinyl ? 280 : 320), alignment: .leading)
            .id(item.id)
            .transition(.blurFade(CGSize(width: 40, height: 0)))
            Text(item.title).font(theme.display(28)).lineLimit(2)
            if !item.subtitle.isEmpty {
                Text(item.subtitle).font(theme.font(21)).opacity(0.7).lineLimit(1)
            }
            SidebarProgress(clock: engine.clock, playing: engine.isPlaying)
        }
        .frame(width: 400, alignment: .leading)
        .padding(18)
        .background(HeroBackground())
        .clipShape(RoundedRectangle(cornerRadius: theme.corner, style: .continuous))
        .foregroundStyle(theme.ink)
        .animation(.spring(response: 0.6, dampingFraction: 0.85), value: item.id)
    }
}

struct SidebarProgress: View {
    @ObservedObject var clock: PlaybackClock
    let playing: Bool
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        let value = clock.duration > 0 ? min(clock.position / clock.duration, 1) : 0
        VStack(alignment: .leading, spacing: 8) {
            if theme.isClassic {
                ProgressView(value: value)
            } else {
                ProgressLine(value: value, color: theme.accent(artwork.palette), height: theme == .noir ? 3 : 6,
                             glow: theme == .aurora)
                    .animation(.linear(duration: 0.5), value: clock.position)
            }
            HStack {
                Image(systemName: playing ? "play.fill" : "pause.fill")
                Text("\(formatTime(clock.position)) / \(formatTime(clock.duration))")
            }
            .font(.system(size: 20))
            .monospacedDigit()
            .opacity(0.75)
        }
    }
}

/// Puts the Now Playing sidebar on the right of a menu screen while something is loaded.
struct WithNowPlaying: ViewModifier {
    @EnvironmentObject var engine: PlayerEngine
    /// The card is not focusable for the first moments, so tvOS puts the initial focus on the list
    /// (same pattern as Settings, proven on the TV); then ◀ reaches the card.
    @State private var cardReady = false

    func body(content: Content) -> some View {
        HStack(alignment: .top, spacing: 50) {
            content
            if engine.current != nil {
                NowPlayingSidebar(enabled: cardReady).padding(.trailing, 40)
            }
        }
        .task {
            try? await Task.sleep(nanoseconds: 700_000_000)
            cardReady = true
        }
    }
}

extension View {
    func withNowPlaying() -> some View { modifier(WithNowPlaying()) }
}

/// Folders → library roots (as configured on the server).
struct FolderRootsView: View {
    @State private var libraryVersion = LocalLibrary.shared.version
    @State private var roots: [RootInfo] = []
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            LocalLibraryHint()
            ForEach(roots) { root in
                NavigationLink {
                    FolderView(root: root.root, folder: "", title: root.name)
                } label: {
                    RowLabel(title: root.name, detail: "\(root.count) items", systemImage: "externaldrive")
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle("Folders")
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
        .task(id: libraryVersion) {
            do {
                let (data, _) = try await Net.data(from: .api("api/browse"))
                roots = try JSONDecoder.server().decode(RootList.self, from: data).roots
                errorText = nil
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

/// One folder: Play (including subfolders / only this folder), subfolders, then its own files.
struct FolderView: View {
    @EnvironmentObject var connection: ServerConnection
    @State private var libraryVersion = LocalLibrary.shared.version
    let root: String
    let folder: String
    let title: String
    @State private var result: BrowseResult?
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            if let result {
                Section {
                    if result.hasSubfolders {
                        Button { play() } label: {
                            RowLabel(title: PlayLabels.includingSubfolders, detail: PlayLabels.count(result.total),
                                     systemImage: "play.fill")
                        }
                        .accessibilityIdentifier("play")  // UI tests
                        if !result.items.isEmpty {
                            Button { play(recursive: false) } label: {
                                RowLabel(title: PlayLabels.onlyThisFolder, detail: PlayLabels.count(result.items.count),
                                         systemImage: "play")
                            }
                            .accessibilityIdentifier("playThisFolder")  // UI tests
                        }
                    } else {
                        Button { play() } label: {
                            RowLabel(title: PlayLabels.play, detail: PlayLabels.count(result.total), systemImage: "play.fill")
                        }
                        .accessibilityIdentifier("play")  // UI tests
                    }
                }
                if !result.subfolders.isEmpty {
                    Section("Folders") {
                        ForEach(result.subfolders) { sub in
                            NavigationLink {
                                FolderView(root: root, folder: sub.folder, title: sub.name)
                            } label: {
                                RowLabel(title: sub.name, detail: "\(sub.count) items", systemImage: "folder")
                            }
                        }
                    }
                }
                if !result.items.isEmpty {
                    Section("Files") {
                        ForEach(result.items) { item in
                            Button { play(recursive: false, startAt: item.id) } label: {
                                RowLabel(
                                    title: item.title,
                                    detail: [item.artist, item.lossless == true ? "lossless" : nil]
                                        .compactMap { $0 }.joined(separator: " · "),
                                    systemImage: item.isVideo ? "film" : "music.note",
                                    item: item
                                )
                            }
                        }
                    }
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .themedScreen()
        .navigationTitle(title)
        .task(id: libraryVersion) { await load() }
        .onReceive(LocalLibrary.shared.$version) { libraryVersion = $0 }
    }

    /// Starts shuffled with repeat all (startPlayback); a picked file plays first.
    private func play(recursive: Bool = true, startAt: Int? = nil) {
        var args: [String: Any] = ["collection": "folder", "root": root, "folder": folder, "recursive": recursive]
        if let startAt { args["start_id"] = startAt }
        connection.startPlayback(args)
    }

    private func load() async {
        do {
            let url = URL.api("api/browse", ["root": root, "folder": folder])
            let (data, _) = try await Net.data(from: url)
            result = try JSONDecoder.server().decode(BrowseResult.self, from: data)
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// No-server mode with an empty library: what to do, or how far the scan is.
struct LocalLibraryHint: View {
    @EnvironmentObject var locator: ServerLocator
    @ObservedObject private var library = LocalLibrary.shared

    var body: some View {
        if locator.localMode && library.items.isEmpty {
            Text(Self.text(library)).foregroundStyle(.secondary)
        }
    }

    @MainActor
    static func text(_ library: LocalLibrary) -> String {
        if library.scanning { return library.status }
        guard let name = LocalSettings.smbName else {
            return "No files yet. Set up the SMB share in Settings → Server."
        }
        return "No songs or videos found in \(name) · \(library.status)"
    }
}

/// Row content for buttons/links: inherits the focus colours of the enclosing button. With `item` and a
/// themed look, the symbol becomes the item's artwork (or the dancing bars while it plays).
struct RowLabel: View {
    let title: String
    let detail: String
    let systemImage: String
    var item: MediaItem? = nil
    @AppStorage(PlayerSettings.listTextSizeKey) private var raw = ListTextSize.compact.rawValue
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var engine: PlayerEngine

    var body: some View {
        let size = ListTextSize(rawValue: raw) ?? .compact
        HStack(spacing: 18) {
            if let item, !theme.isClassic {
                let side = size.rowHeight - 10
                ZStack {
                    ItemThumb(item: item, corner: 6)
                        .frame(width: item.isVideo ? side * 16 / 9 : side, height: side)
                    if engine.current?.id == item.id {
                        Color.black.opacity(0.45)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                        EqualizerBars(animating: engine.isPlaying, color: .white, bars: 3, width: 3,
                                      height: side * 0.42, spacing: 2)
                    }
                }
                .frame(width: item.isVideo ? side * 16 / 9 : side, height: side)
            } else {
                Image(systemName: systemImage)
                    .frame(width: 30)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(size.titleFont).lineLimit(1)
                if !detail.isEmpty {
                    Text(detail).font(size.detailFont).opacity(0.7).lineLimit(1)
                }
            }
        }
    }
}
