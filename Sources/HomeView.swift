import SwiftUI

/// Home screen of the themed looks (Aurora / Noir / Vinyl): Now Playing hero, then shelves.
/// Classic keeps the original list (ContentView.classicMenu).
struct HomeView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @State private var videos: [MediaItem] = []
    @State private var playlists: [PlaylistInfo] = []
    @State private var covers: [Int: [MediaItem]] = [:]  // playlist id → first items, for the mosaic

    private let width: CGFloat = 1760  // 1920 minus the side margins

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 50) {
                header
                hero.focusSection()
                shelf("Browse") { browseTiles }
                if !playlists.isEmpty { shelf("Playlists") { playlistCards } }
                shelf("Play") { playTiles }
                if !videos.isEmpty { shelf("Music videos") { videoPosters } }
                footer
            }
            .padding(.horizontal, 80)
            .padding(.top, 40)
            .padding(.bottom, 70)
        }
        .scrollClipDisabled()
        .foregroundStyle(theme.ink)
        .themedScreen(intensity: 0.9)
        .toolbar(.hidden, for: .navigationBar)
        .task { await loadVideos() }
        .task(id: connection.playlistsVersion) { await loadPlaylists() }
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            wordmark
            Spacer()
            HStack(spacing: 12) {
                Circle()
                    .fill(connection.connected ? Color.green : Color.orange)
                    .frame(width: 14, height: 14)
                    .shadow(color: connection.connected ? .green : .orange, radius: 6)
                Text(connection.connected ? "Connected" : "Connecting…")
                    .font(theme.font(24, .medium))
                    .opacity(0.75)
            }
        }
    }

    @ViewBuilder private var wordmark: some View {
        switch theme {
        case .noir:
            HStack(spacing: 0) {
                Text("ATV").font(.system(size: 64, weight: .black).width(.condensed))
                Text("PLAYER").font(.system(size: 64, weight: .thin).width(.condensed))
                    .foregroundStyle(theme.accent(artwork.palette))
            }
            .tracking(4)
        case .vinyl:
            HStack(spacing: 18) {
                Image(systemName: "opticaldisc").font(.system(size: 44)).foregroundStyle(theme.accent(artwork.palette))
                HStack(spacing: 0) {
                    Text("ATV").font(.system(size: 64, weight: .black).width(.condensed))
                    Text("PLAYER").font(.system(size: 64, weight: .thin).width(.condensed))
                        .foregroundStyle(theme.accent(artwork.palette))
                }
                .tracking(4)
            }
        default:
            Text("ATV Player")
                .font(.system(size: 62, weight: .heavy, design: .rounded))
                .foregroundStyle(LinearGradient(colors: [.white, theme.accent(artwork.palette)],
                                                startPoint: .leading, endPoint: .trailing))
        }
    }

    // MARK: - Now Playing hero

    @ViewBuilder private var hero: some View {
        if let item = engine.current {
            Button { engine.presentPlayer() } label: {
                HeroCard(item: item)
            }
            .buttonStyle(TileButtonStyle(corner: theme.corner + 6, lift: 1.025))
        } else {
            Button { shuffle("all") } label: {
                HStack(spacing: 40) {
                    Image(systemName: "shuffle")
                        .font(.system(size: 90, weight: .semibold))
                        .foregroundStyle(theme.accent(artwork.palette))
                        .frame(width: 200, height: 200)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Nothing playing yet").font(theme.display(54))
                        Text("Select to shuffle everything").font(theme.font(28)).opacity(0.7)
                    }
                    Spacer(minLength: 0)
                }
                .padding(36)
                .frame(width: width, alignment: .leading)
                .background(HeroBackground())
                .clipShape(RoundedRectangle(cornerRadius: theme.corner + 6, style: .continuous))
            }
            .buttonStyle(TileButtonStyle(corner: theme.corner + 6, lift: 1.025))
        }
    }

    // MARK: - Shelves

    private func shelf<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 24) {
            ShelfTitle(text: title)
            content()
        }
        .focusSection()
    }

    private var playTiles: some View {
        let w = (width - 80) / 3
        return HStack(spacing: 40) {
            ActionTile(title: "Shuffle everything", subtitle: "Songs and music videos", symbol: "shuffle",
                       variant: 0, size: CGSize(width: w, height: 200)) { shuffle("all") }
            ActionTile(title: "Shuffle music", subtitle: "Songs only", symbol: "music.note",
                       variant: 1, size: CGSize(width: w, height: 200)) { shuffle("music") }
            ActionTile(title: "Shuffle music videos", subtitle: "Videos only", symbol: "film",
                       variant: 2, size: CGSize(width: w, height: 200)) { shuffle("videos") }
        }
    }

    private var browseTiles: some View {
        let size = CGSize(width: (width - 120) / 4, height: 130)
        return HStack(spacing: 40) {
            NavigationLink { PlaylistsView() } label: {
                BrowseTileLabel(title: "Playlists", symbol: "music.note.list", size: size)
            }
            NavigationLink { FolderRootsView() } label: {
                BrowseTileLabel(title: "Folders", symbol: "folder", size: size)
            }
            NavigationLink { VideoListView() } label: {
                BrowseTileLabel(title: "Music videos", symbol: "play.rectangle", size: size)
            }
            NavigationLink { SettingsView() } label: {
                BrowseTileLabel(title: "Settings", symbol: "gearshape", size: size)
            }
        }
        .buttonStyle(TileButtonStyle())
    }

    private var videoPosters: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(spacing: 40) {
                ForEach(videos.prefix(40)) { video in
                    Button {
                        connection.startPlayback(["collection": "videos", "start_id": video.id])
                    } label: {
                        PosterLabel(item: video, size: CGSize(width: 400, height: 225))
                    }
                    .buttonStyle(TileButtonStyle())
                }
            }
            .padding(.vertical, 24)
        }
        .scrollClipDisabled()
    }

    private var playlistCards: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(spacing: 40) {
                ForEach(playlists) { playlist in
                    NavigationLink {
                        PlaylistView(playlistID: playlist.id, title: playlist.name)
                    } label: {
                        PlaylistCardLabel(playlist: playlist, items: covers[playlist.id] ?? [], side: 280)
                    }
                    .buttonStyle(TileButtonStyle())
                }
            }
            .padding(.vertical, 24)
        }
        .scrollClipDisabled()
    }

    private var footer: some View {
        let info = Bundle.main.infoDictionary
        let version = "Version \(info?["CFBundleShortVersionString"] as? String ?? "?") (build \(info?["CFBundleVersion"] as? String ?? "?"))"
        return Text("\(version)  ·  Phone/PC remote: \(Config.remoteAddress)  ·  ⏯ pauses or resumes anywhere")
            .font(theme.font(20))
            .opacity(0.45)
    }

    // MARK: - Data

    private func shuffle(_ name: String) {
        connection.startPlayback(["collection": name])
    }

    private func loadVideos() async {
        guard let (data, _) = try? await Net.data(from: .api("api/collection/videos")),
              let list = try? JSONDecoder.server().decode(ItemList.self, from: data) else { return }
        videos = list.items
    }

    private func loadPlaylists() async {
        guard let (data, _) = try? await Net.data(from: .api("api/playlists")),
              let list = try? JSONDecoder.server().decode(PlaylistList.self, from: data) else { return }
        playlists = list.playlists
        for playlist in list.playlists.prefix(30) {
            guard let (d, _) = try? await Net.data(from: .api("api/playlists/\(playlist.id)")),
                  let detail = try? JSONDecoder.server().decode(PlaylistDetail.self, from: d) else { continue }
            covers[playlist.id] = detail.items.filter { !$0.isMissing }.prefix(4).map(\.asMediaItem)
        }
    }
}

/// The big Now Playing card on the home screen.
struct HeroCard: View {
    let item: MediaItem
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @AppStorage(AppTheme.motionKey) private var motion = true

    var body: some View {
        let accent = theme.accent(artwork.palette)
        HStack(spacing: 46) {
            art
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 14) {
                    EqualizerBars(animating: engine.isPlaying && motion, color: accent, width: 5, height: 24)
                    Text(engine.isPlaying ? "NOW PLAYING" : "PAUSED")
                        .font(theme.eyebrow(22))
                        .tracking(theme == .vinyl ? 1 : 4)
                        .foregroundStyle(accent)
                }
                Text(item.title)
                    .font(theme.display(58))
                    .lineLimit(2)
                    .minimumScaleFactor(0.7)
                    .id("t\(item.id)")
                    .transition(.blurFade(CGSize(width: 0, height: 24)))
                if !item.subtitle.isEmpty {
                    Text(item.subtitle).font(theme.font(28)).opacity(0.72).lineLimit(1)
                        .id("s\(item.id)")
                        .transition(.blurFade())
                }
                HeroProgress(clock: engine.clock, accent: accent, glow: theme == .aurora)
                    .padding(.top, 8)
            }
            .frame(maxWidth: 700, alignment: .leading)
            Spacer(minLength: 0)
            if let upcoming = connection.state?.upcoming, !upcoming.isEmpty {
                VStack(alignment: .leading, spacing: 16) {
                    Text(theme == .vinyl ? "Up next" : "UP NEXT")
                        .font(theme.eyebrow(20)).tracking(theme == .vinyl ? 0 : 4)
                        .opacity(0.6)
                    ForEach(upcoming.prefix(3)) { next in
                        HStack(spacing: 16) {
                            ItemThumb(item: next, corner: theme == .noir ? 2 : 8)
                                .frame(width: next.isVideo ? 96 : 54, height: 54)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(next.title).font(theme.font(22, .semibold)).lineLimit(1)
                                Text(next.artist ?? "").font(theme.font(18)).opacity(0.6).lineLimit(1)
                            }
                        }
                    }
                }
                .frame(width: 400, alignment: .leading)
                .padding(.leading, 20)
                .overlay(alignment: .leading) {
                    Rectangle().fill(theme.ink.opacity(0.12)).frame(width: 1.5)
                }
            }
        }
        .padding(34)
        .frame(width: 1760, alignment: .leading)
        .background(HeroBackground())
        .clipShape(RoundedRectangle(cornerRadius: theme.corner + 6, style: .continuous))
        .foregroundStyle(theme.ink)
        .animation(.spring(response: 0.6, dampingFraction: 0.85), value: item.id)
    }

    @ViewBuilder private var art: some View {
        let side: CGFloat = 300
        if item.isVideo {
            CurrentArtwork(item: item, corner: theme.corner, showsTitle: false)
                .frame(width: side * 16 / 9, height: side)
                .overlay(alignment: .bottomTrailing) {
                    Image(systemName: "play.rectangle.fill").font(.system(size: 34)).padding(14).opacity(0.85)
                }
                .id(item.id)
                .transition(.slide(forward: true, distance: 60))
        } else if theme == .vinyl {
            ZStack(alignment: .leading) {
                VinylRecord(image: artwork.art(for: item.id),
                            spinning: engine.isPlaying && motion, size: side - 16,
                            labelTint: Palette.forItem(item.id).vibrant.with(brightness: 0.6).color)
                    .offset(x: side * 0.42)
                CurrentArtwork(item: item, corner: 4)
                    .frame(width: side, height: side)
                    .shadow(color: .black.opacity(0.5), radius: 16, x: 8, y: 8)
            }
            .frame(width: side * 1.42, height: side, alignment: .leading)
            .id(item.id)
            .transition(.slide(forward: true, distance: 60))
        } else {
            CurrentArtwork(item: item, corner: theme.corner)
                .frame(width: side, height: side)
                .shadow(color: theme == .aurora ? artwork.palette.vibrant.color.opacity(0.45) : .clear, radius: 40)
                .scaleEffect(engine.isPlaying || theme != .aurora ? 1 : 0.94)
                .animation(.spring(response: 0.5, dampingFraction: 0.7), value: engine.isPlaying)
                .id(item.id)
                .transition(.slide(forward: true, distance: 60))
        }
    }
}

struct HeroProgress: View {
    @ObservedObject var clock: PlaybackClock
    let accent: Color
    var glow = false
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ProgressLine(value: clock.duration > 0 ? clock.position / clock.duration : 0, color: accent,
                         height: theme == .noir ? 3 : 7, glow: glow)
                .frame(width: 640)
                .animation(.linear(duration: 0.5), value: clock.position)
            HStack {
                Text(formatTime(clock.position))
                Spacer()
                Text(formatTime(clock.duration))
            }
            .font(.system(size: 22, weight: .medium, design: theme == .noir ? .monospaced : theme.fontDesign))
            .monospacedDigit()
            .opacity(0.6)
            .frame(width: 640)
        }
    }
}

/// Big coloured tile for the shuffle actions.
struct ActionTile: View {
    let title: String
    let subtitle: String
    let symbol: String
    let variant: Int
    let size: CGSize
    let action: () -> Void
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        Button(action: action) {
            ZStack(alignment: .bottomLeading) {
                background
                Image(systemName: symbol)
                    .font(.system(size: theme == .aurora ? 120 : 52, weight: .bold))
                    .foregroundStyle(symbolColor.opacity(theme == .aurora ? 0.22 : 0.9))
                    .frame(maxWidth: .infinity, maxHeight: .infinity,
                           alignment: theme == .aurora ? .topTrailing : .topLeading)
                    .padding(theme == .aurora ? 14 : 28)
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).font(theme.font(32, .bold))
                    Text(subtitle).font(theme.font(22)).opacity(0.75)
                }
                .padding(28)
            }
            .frame(width: size.width, height: size.height)
            .clipShape(RoundedRectangle(cornerRadius: theme.corner, style: .continuous))
            .foregroundStyle(theme.ink)
        }
        .buttonStyle(TileButtonStyle())
    }

    private var symbolColor: Color {
        theme == .aurora ? .white : theme.accent(artwork.palette)
    }

    @ViewBuilder private var background: some View {
        let p = artwork.palette
        switch theme {
        case .noir:
            Color(white: 0.07)
        case .vinyl:
            ZStack {
                LinearGradient(colors: [[Color(red: 0.36, green: 0.20, blue: 0.10), Color(red: 0.18, green: 0.10, blue: 0.05)],
                                        [Color(red: 0.30, green: 0.22, blue: 0.12), Color(red: 0.14, green: 0.10, blue: 0.06)],
                                        [Color(red: 0.32, green: 0.14, blue: 0.12), Color(red: 0.15, green: 0.07, blue: 0.06)]][variant % 3],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                GrainOverlay().opacity(0.07)
            }
        default:
            let pairs: [(RGB, RGB)] = [(p.vibrant.with(brightness: 0.75), p.tones[0]), (p.tones[1], p.tones[3]),
                                      (p.tones[3], p.tones[2])]
            let pair = pairs[variant % 3]
            ZStack {
                LinearGradient(colors: [pair.0.color, pair.1.color], startPoint: .topLeading, endPoint: .bottomTrailing)
                LinearGradient(colors: [.white.opacity(0.18), .clear], startPoint: .top, endPoint: .center)
            }
        }
    }
}

struct BrowseTileLabel: View {
    let title: String
    let symbol: String
    let size: CGSize
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        HStack(spacing: 22) {
            Image(systemName: symbol)
                .font(.system(size: 40, weight: .semibold))
                .foregroundStyle(theme.accent(artwork.palette))
                .frame(width: 56)
            Text(title).font(theme.font(30, .semibold))
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 30)
        .frame(width: size.width, height: size.height)
        .background {
            switch theme {
            case .noir: Color(white: 0.07)
            case .vinyl: Color(red: 0.2, green: 0.13, blue: 0.08).opacity(0.85)
            default: Rectangle().fill(.ultraThinMaterial)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: theme.corner, style: .continuous))
        .foregroundStyle(theme.ink)
    }
}

/// 16:9 video poster with the title on a dark fade.
struct PosterLabel: View {
    let item: MediaItem
    let size: CGSize
    @Environment(\.appTheme) private var theme

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            ItemThumb(item: item, corner: 0)
            LinearGradient(colors: [.clear, .black.opacity(0.85)], startPoint: .center, endPoint: .bottom)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title).font(theme.font(24, .semibold)).lineLimit(1)
                if let artist = item.artist {
                    Text(artist).font(theme.font(19)).opacity(0.75).lineLimit(1)
                }
            }
            .foregroundStyle(.white)
            .padding(18)
        }
        .frame(width: size.width, height: size.height)
        .clipShape(RoundedRectangle(cornerRadius: theme.corner, style: .continuous))
    }
}

/// Square playlist card: 2×2 mosaic of its first items, name and size on a fade.
struct PlaylistCardLabel: View {
    let playlist: PlaylistInfo
    let items: [MediaItem]
    let side: CGFloat
    @Environment(\.appTheme) private var theme

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            mosaic
            LinearGradient(colors: [.clear, .black.opacity(0.88)], startPoint: .center, endPoint: .bottom)
            VStack(alignment: .leading, spacing: 4) {
                Text(playlist.name).font(theme.font(26, .bold)).lineLimit(2)
                Text(playlist.detail).font(theme.font(18)).opacity(0.75).lineLimit(1)
            }
            .foregroundStyle(.white)
            .padding(20)
        }
        .frame(width: side, height: side)
        .clipShape(RoundedRectangle(cornerRadius: theme.corner, style: .continuous))
    }

    @ViewBuilder private var mosaic: some View {
        if items.count >= 4 {
            let half = side / 2
            VStack(spacing: 0) {
                HStack(spacing: 0) {
                    ItemThumb(item: items[0], corner: 0).frame(width: half, height: half)
                    ItemThumb(item: items[1], corner: 0).frame(width: half, height: half)
                }
                HStack(spacing: 0) {
                    ItemThumb(item: items[2], corner: 0).frame(width: half, height: half)
                    ItemThumb(item: items[3], corner: 0).frame(width: half, height: half)
                }
            }
        } else if let first = items.first {
            ItemThumb(item: first, corner: 0)
        } else {
            ArtPlaceholder(item: MediaItem.placeholder(playlist.id, playlist.name), palette: .forItem(playlist.id * 7),
                           showsTitle: false)
        }
    }
}
