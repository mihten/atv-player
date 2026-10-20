import SwiftUI

/// Apple TV Now Playing with lyrics: the cover / video is replaced by the lyrics (Siri Remote click switches it on
/// and off). Title, progress and up-next stay on the left; the lyrics fill the right half. It is ONE screen for every
/// song and video, so skipping never flips back to the regular player and then to lyrics again: only the text
/// cross-fades (an info card when the file has no lyrics). The lyrics views take no focus and do not scroll by
/// themselves: ▲▼◀▶ and ⏯ keep working through NowPlayingView.
struct TVLyricsScreen: View {
    let item: MediaItem
    let forward: Bool
    let hints: Bool
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @AppStorage(AppTheme.motionKey) private var motion = true

    private var progressStyle: PlayerProgress.Style {
        switch theme {
        case .noir: .hairline
        case .vinyl: .needle
        default: .glow
        }
    }

    var body: some View {
        let accent = theme.accent(artwork.palette)
        ZStack {
            ThemeBackdrop(intensity: 1, animate: motion && !(item.isVideo && engine.isPlaying))
            HStack(alignment: .top, spacing: 90) {
                VStack(alignment: .leading, spacing: 22) {
                    StatusEyebrow(accent: accent)
                    VStack(alignment: .leading, spacing: 12) {
                        Text(item.title)
                            .font(theme.display(62))
                            .lineLimit(3)
                            .minimumScaleFactor(0.55)
                        if let artist = item.artist, !artist.isEmpty {
                            Text(artist).font(theme.font(36, .semibold)).opacity(0.82).lineLimit(1)
                        }
                        if let album = item.album, !album.isEmpty {
                            Text(album).font(theme.font(28)).opacity(0.55).lineLimit(1)
                        }
                        Facts(item: item, theme: theme).padding(.top, 4)
                    }
                    .id(item.id)
                    .transition(.blurFade(CGSize(width: forward ? 60 : -60, height: 0), blur: 12))
                    PlayerProgress(clock: engine.clock, accent: accent, style: progressStyle)
                        .padding(.top, 20)
                    UpNext(accent: accent, numbered: true).padding(.top, 20)
                    Spacer(minLength: 0)
                }
                .frame(width: 720, alignment: .leading)
                LyricsPanel(item: item, store: LyricsStore.shared)
                    .frame(maxWidth: .infinity)
            }
            .padding(.horizontal, 100)
            .padding(.top, 90)
            .padding(.bottom, 110)
            RemoteHints(visible: hints)
        }
        .foregroundStyle(theme.ink)
        .animation(.spring(response: 0.75, dampingFraction: 0.86), value: item.id)
    }
}
