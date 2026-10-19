import SwiftUI

struct ContentView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @State private var showPlayer = false

    private var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "Version \(version) (build \(build))"
    }

    var body: some View {
        NavigationStack {
            List {
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
                Section("Browse") {
                    NavigationLink {
                        VideoListView()
                    } label: {
                        Label("Music videos", systemImage: "play.rectangle")
                    }
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
                        SettingsView()
                    } label: {
                        Label("Settings", systemImage: "gearshape")
                    }
                }
                Section {
                    if let other = connection.otherOutput {
                        Button { connection.setOutput(Config.deviceID) } label: {
                            Label("Playing on \(other.name) · Play here", systemImage: "appletv")
                        }
                    }
                    Text(connection.connected ? "Server connected" : "Connecting to \(Config.remoteAddress)…")
                        .foregroundStyle(.secondary)
                    Text("Phone/PC remote: \(Config.remoteAddress)")
                        .foregroundStyle(.secondary)
                } footer: {
                    Text(appVersion)
                }
            }
            .withNowPlaying()
            .navigationTitle("ATV Player")
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
        }
        .onChange(of: engine.startCounter) {
            showPlayer = true
        }
    }

    private func shuffle(_ name: String) {
        connection.command("play_collection", ["collection": name, "shuffle": true])
    }
}

struct VideoListView: View {
    @EnvironmentObject var connection: ServerConnection
    @State private var items: [MediaItem] = []
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
            Button {
                connection.command("play_collection", ["collection": "videos", "shuffle": true])
            } label: {
                Label("Shuffle all music videos", systemImage: "shuffle")
            }
            ForEach(items) { item in
                Button {
                    connection.command("play_collection", ["collection": "videos", "start_id": item.id])
                } label: {
                    RowLabel(title: item.title, detail: item.artist ?? "", systemImage: "film")
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .navigationTitle("Music videos")
        .task { await load() }
    }

    private func load() async {
        do {
            let url = Config.serverURL.appendingPathComponent("api/collection/videos")
            let (data, _) = try await URLSession.shared.data(from: url)
            items = try JSONDecoder.server().decode(ItemList.self, from: data).items
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// Now Playing panel: right-hand sidebar on menu screens, left half in Settings.
struct NowPlayingSidebar: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    var enabled = true

    var body: some View {
        if let item = engine.current {
            VStack(alignment: .leading, spacing: 20) {
                Text("Now playing").font(.system(size: 24, weight: .semibold)).opacity(0.6)
                Button { engine.presentPlayer() } label: {
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
                .buttonStyle(.card)
                .disabled(!enabled)
                if let upcoming = connection.state?.upcoming, !upcoming.isEmpty {
                    Text("Up next").font(.system(size: 24, weight: .semibold)).opacity(0.6).padding(.top, 10)
                    ForEach(upcoming.prefix(4)) { next in
                        Label(next.title, systemImage: next.isVideo ? "film" : "music.note")
                            .font(.system(size: 22)).lineLimit(1).opacity(0.85)
                    }
                }
                Text("⏯ Play/Pause pauses or resumes · select the card to open the player")
                    .font(.system(size: 18)).opacity(0.5).padding(.top, 10)
                Spacer(minLength: 0)
            }
            .frame(width: 440, alignment: .leading)
            .padding(.top, 30)
            .focusSection()
        }
    }
}

struct SidebarProgress: View {
    @ObservedObject var clock: PlaybackClock
    let playing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ProgressView(value: clock.duration > 0 ? min(clock.position / clock.duration, 1) : 0)
            HStack {
                Image(systemName: playing ? "play.fill" : "pause.fill")
                Text("\(formatTime(clock.position)) / \(formatTime(clock.duration))")
            }
            .font(.system(size: 20))
            .opacity(0.75)
        }
    }
}

/// Puts the Now Playing sidebar on the right of a menu screen while something is loaded.
struct WithNowPlaying: ViewModifier {
    @EnvironmentObject var engine: PlayerEngine

    func body(content: Content) -> some View {
        HStack(alignment: .top, spacing: 50) {
            content
            if engine.current != nil {
                NowPlayingSidebar().padding(.trailing, 40)
            }
        }
    }
}

extension View {
    func withNowPlaying() -> some View { modifier(WithNowPlaying()) }
}

/// Folders → library roots (as configured on the server).
struct FolderRootsView: View {
    @State private var roots: [RootInfo] = []
    @State private var errorText: String?

    var body: some View {
        List {
            if let errorText {
                Text(errorText).foregroundStyle(.secondary)
            }
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
        .navigationTitle("Folders")
        .task {
            do {
                let (data, _) = try await URLSession.shared.data(from: .api("api/browse"))
                roots = try JSONDecoder.server().decode(RootList.self, from: data).roots
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

/// One folder: Play / Shuffle (everything below it), subfolders, then its own files.
struct FolderView: View {
    @EnvironmentObject var connection: ServerConnection
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
                        Button { play(shuffle: true) } label: {
                            Label("Shuffle all, including subfolders (\(result.total))", systemImage: "shuffle")
                        }
                        Button { play(shuffle: false) } label: {
                            Label("Play all, including subfolders", systemImage: "play.fill")
                        }
                        if !result.items.isEmpty {
                            Button { play(shuffle: true, recursive: false) } label: {
                                Label("Shuffle this folder only (\(result.items.count))", systemImage: "shuffle")
                            }
                            Button { play(shuffle: false, recursive: false) } label: {
                                Label("Play this folder only", systemImage: "play")
                            }
                        }
                    } else {
                        Button { play(shuffle: true) } label: {
                            Label("Shuffle all (\(result.total))", systemImage: "shuffle")
                        }
                        Button { play(shuffle: false) } label: {
                            Label("Play all", systemImage: "play.fill")
                        }
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
                            Button { play(shuffle: false, recursive: false, startAt: item.id) } label: {
                                RowLabel(
                                    title: item.title,
                                    detail: [item.artist, item.lossless == true ? "lossless" : nil]
                                        .compactMap { $0 }.joined(separator: " · "),
                                    systemImage: item.isVideo ? "film" : "music.note"
                                )
                            }
                        }
                    }
                }
            }
        }
        .denseList()
        .withNowPlaying()
        .navigationTitle(title)
        .task { await load() }
    }

    private func play(shuffle: Bool, recursive: Bool = true, startAt: Int? = nil) {
        var args: [String: Any] = [
            "collection": "folder", "root": root, "folder": folder, "shuffle": shuffle, "recursive": recursive,
        ]
        if let startAt { args["start_id"] = startAt }
        connection.command("play_collection", args)
    }

    private func load() async {
        do {
            let url = URL.api("api/browse", ["root": root, "folder": folder])
            let (data, _) = try await URLSession.shared.data(from: url)
            result = try JSONDecoder.server().decode(BrowseResult.self, from: data)
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// Row content for buttons/links: inherits the focus colours of the enclosing button.
struct RowLabel: View {
    let title: String
    let detail: String
    let systemImage: String
    @AppStorage(PlayerSettings.listTextSizeKey) private var raw = ListTextSize.compact.rawValue

    var body: some View {
        let size = ListTextSize(rawValue: raw) ?? .compact
        HStack(spacing: 18) {
            Image(systemName: systemImage)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(size.titleFont).lineLimit(1)
                if !detail.isEmpty {
                    Text(detail).font(size.detailFont).opacity(0.7).lineLimit(1)
                }
            }
        }
    }
}
