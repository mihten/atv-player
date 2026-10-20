import SwiftUI

/// Library tab home in the themed looks: Now Playing card, shuffle tiles, music video and playlist
/// carousels, browse rows. Searching switches back to the result list.
struct PhoneHome: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @Environment(\.showPlayer) private var showPlayer
    @AppStorage(AppTheme.motionKey) private var motion = true
    @State private var videos: [MediaItem] = []
    @State private var playlists: [PlaylistInfo] = []
    @State private var covers: [Int: [MediaItem]] = [:]
    @State private var taps = 0

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                if let item = engine.current {
                    nowPlaying(item)
                }
                shuffleTiles
                if !videos.isEmpty {
                    shelf("Music videos") { videoShelf }
                }
                if !playlists.isEmpty {
                    shelf("Playlists") { playlistShelf }
                }
                shelf("Browse") { browseRows }
                VStack(alignment: .leading, spacing: 4) {
                    Text(connection.connected ? "Connected to \(Config.remoteAddress)" : "Connecting to \(Config.remoteAddress)…")
                    Text("This iPhone has its own queue; the Apple TV's is separate.")
                }
                .font(theme.font(12))
                .opacity(0.5)
            }
            .padding(.horizontal, 18)
            .padding(.top, 4)
            .padding(.bottom, 24)
        }
        .phoneThemed()
        .foregroundStyle(theme.ink)
        .sensoryFeedback(.impact(weight: .light), trigger: taps)
        .task { await loadVideos() }
        .task(id: connection.playlistsVersion) { await loadPlaylists() }
    }

    // MARK: - Now playing

    private func nowPlaying(_ item: MediaItem) -> some View {
        let accent = theme.accent(artwork.palette)
        return HStack(spacing: 14) {
            Group {
                if theme == .vinyl && !item.isVideo {
                    VinylRecord(image: artwork.art(for: item.id),
                                spinning: engine.isPlaying && motion, size: 92,
                                labelTint: Palette.forItem(item.id).vibrant.with(brightness: 0.55).color)
                } else {
                    CurrentArtwork(item: item, corner: theme == .noir ? 2 : 14, showsTitle: false)
                        .frame(width: item.isVideo ? 128 : 92, height: item.isVideo ? 72 : 92)
                        .shadow(color: theme == .aurora ? artwork.palette.vibrant.color.opacity(0.5) : .clear, radius: 16)
                        .grayscale(theme == .noir && !engine.isPlaying ? 1 : 0)
                }
            }
            .id(item.id)
            .transition(.blurFade(CGSize(width: 24, height: 0), blur: 10))
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 8) {
                    EqualizerBars(animating: engine.isPlaying && motion, color: accent, width: 3, height: 13, spacing: 2)
                    Text(engine.isPlaying ? "NOW PLAYING" : "PAUSED")
                        .font(theme.eyebrow(11)).tracking(2)
                        .foregroundStyle(accent)
                }
                Text(item.title).font(theme.display(20)).lineLimit(2)
                    .id("t\(item.id)")
                    .transition(.blurFade(CGSize(width: 0, height: 8), blur: 8))
                Text(item.artist ?? " ").font(theme.font(14)).opacity(0.7).lineLimit(1)
                CardProgress(clock: engine.clock, accent: accent)
                    .padding(.top, 4)
            }
            Spacer(minLength: 0)
            Button {
                taps += 1
                engine.togglePlay(source: "phone")
            } label: {
                Image(systemName: engine.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 20, weight: .bold))
                    .contentTransition(.symbolEffect(.replace))
                    .foregroundStyle(theme == .noir ? Color.white : Color.black.opacity(0.85))
                    .frame(width: 50, height: 50)
                    .background {
                        if theme == .noir {
                            Circle().strokeBorder(.white, lineWidth: 1.5)
                        } else {
                            Circle().fill(theme == .vinyl ? accent : .white)
                        }
                    }
            }
            .buttonStyle(PressScale())
        }
        .padding(14)
        .background(HeroBackground())
        .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 6 : 22, style: .continuous))
        .contentShape(Rectangle())
        .onTapGesture { showPlayer() }
        .animation(.spring(response: 0.55, dampingFraction: 0.85), value: item.id)
    }

    // MARK: - Shuffle tiles

    private var shuffleTiles: some View {
        VStack(spacing: 12) {
            PhoneTile(title: "Shuffle everything", subtitle: "Songs and music videos", symbol: "shuffle",
                      variant: 0, height: 92) { shuffle("all") }
            HStack(spacing: 12) {
                PhoneTile(title: "Music", subtitle: "Shuffle songs", symbol: "music.note", variant: 1, height: 84) {
                    shuffle("music")
                }
                PhoneTile(title: "Videos", subtitle: "Shuffle music videos", symbol: "film", variant: 2, height: 84) {
                    shuffle("videos")
                }
            }
        }
    }

    // MARK: - Shelves

    private func shelf<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Group {
                switch theme {
                case .noir, .vinyl: Text(title.uppercased()).font(.system(size: 13, weight: .heavy)).tracking(3)
                default: Text(title).font(theme.font(22, .bold))
                }
            }
            content()
        }
    }

    private var videoShelf: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(alignment: .top, spacing: 12) {
                ForEach(videos.prefix(30)) { video in
                    Button {
                        taps += 1
                        connection.command("play_collection", ["collection": "videos", "start_id": video.id])
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            ItemThumb(item: video, corner: theme == .noir ? 2 : 12)
                                .frame(width: 220, height: 124)
                            Text(video.title).font(theme.font(14, .semibold)).lineLimit(1)
                            Text(video.artist ?? " ").font(theme.font(12)).opacity(0.6).lineLimit(1)
                        }
                        .frame(width: 220, alignment: .leading)
                    }
                    .buttonStyle(PressScale())
                    .itemMenu(video)
                }
            }
            .padding(.horizontal, 18)
        }
        .padding(.horizontal, -18)
    }

    private var playlistShelf: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(alignment: .top, spacing: 12) {
                ForEach(playlists) { playlist in
                    NavigationLink {
                        PhonePlaylistView(playlistID: playlist.id, title: playlist.name)
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            PlaylistMosaic(playlistID: playlist.id, items: covers[playlist.id] ?? [])
                                .frame(width: 140, height: 140)
                                .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 2 : 14, style: .continuous))
                            Text(playlist.name).font(theme.font(14, .semibold)).lineLimit(1)
                            Text("\(playlist.count) items").font(theme.font(12)).opacity(0.6)
                        }
                        .frame(width: 140, alignment: .leading)
                    }
                    .buttonStyle(PressScale())
                }
            }
            .padding(.horizontal, 18)
        }
        .padding(.horizontal, -18)
    }

    private var browseRows: some View {
        VStack(spacing: 0) {
            NavigationLink { PhoneVideoListView() } label: {
                BrowseRow(title: "All music videos", symbol: "play.rectangle", detail: videos.isEmpty ? "" : "\(videos.count)")
            }
            Divider().opacity(0.4).padding(.leading, 52)
            NavigationLink { PhoneFolderRootsView() } label: {
                BrowseRow(title: "Folders", symbol: "folder", detail: "")
            }
        }
        .buttonStyle(.plain)
        .background(HeroBackground())
        .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 6 : 18, style: .continuous))
    }

    // MARK: - Data

    private func shuffle(_ name: String) {
        taps += 1
        connection.command("play_collection", ["collection": name, "shuffle": true])
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

private struct CardProgress: View {
    @ObservedObject var clock: PlaybackClock
    let accent: Color

    var body: some View {
        ProgressLine(value: clock.duration > 0 ? clock.position / clock.duration : 0, color: accent, height: 3)
            .animation(.linear(duration: 0.5), value: clock.position)
    }
}

private struct BrowseRow: View {
    let title: String
    let symbol: String
    let detail: String
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(theme.accent(artwork.palette))
                .frame(width: 26)
            Text(title).font(theme.font(16, .medium))
            Spacer()
            if !detail.isEmpty { Text(detail).font(theme.font(14)).opacity(0.5) }
            Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).opacity(0.4)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .contentShape(Rectangle())
        .foregroundStyle(theme.ink)
    }
}

/// Coloured action tile (shuffle everything / music / videos).
struct PhoneTile: View {
    let title: String
    let subtitle: String
    let symbol: String
    let variant: Int
    let height: CGFloat
    let action: () -> Void
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        Button(action: action) {
            ZStack(alignment: .bottomLeading) {
                background
                Image(systemName: symbol)
                    .font(.system(size: theme == .aurora ? 64 : 26, weight: .bold))
                    .foregroundStyle(theme == .aurora ? Color.white.opacity(0.22) : theme.accent(artwork.palette))
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
                    .padding(theme == .aurora ? 8 : 14)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(theme.font(17, .bold))
                    Text(subtitle).font(theme.font(12)).opacity(0.75)
                }
                .padding(14)
            }
            .frame(maxWidth: .infinity)
            .frame(height: height)
            .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 6 : 18, style: .continuous))
            .overlay {
                if theme == .noir {
                    RoundedRectangle(cornerRadius: 6).strokeBorder(.white.opacity(0.18), lineWidth: 1)
                }
            }
            .foregroundStyle(theme.ink)
        }
        .buttonStyle(PressScale())
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
                LinearGradient(colors: [.white.opacity(0.16), .clear], startPoint: .top, endPoint: .center)
            }
        }
    }
}
