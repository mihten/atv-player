import SwiftUI

/// Card background behind the hero, per theme.
struct HeroBackground: View {
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        switch theme {
        case .noir:
            ZStack {
                Color.white.opacity(0.035)
                RoundedRectangle(cornerRadius: theme.corner + 6).strokeBorder(.white.opacity(0.14), lineWidth: 1.5)
            }
        case .vinyl:
            ZStack {
                LinearGradient(colors: [Color(red: 0.24, green: 0.16, blue: 0.10).opacity(0.85),
                                        Color(red: 0.12, green: 0.08, blue: 0.05).opacity(0.85)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                GrainOverlay().opacity(0.06)
                RoundedRectangle(cornerRadius: theme.corner + 6)
                    .strokeBorder(Color(red: 0.93, green: 0.66, blue: 0.29).opacity(0.25), lineWidth: 1.5)
            }
        default:
            ZStack {
                Rectangle().fill(.ultraThinMaterial)
                LinearGradient(colors: [artwork.palette.tones[0].color.opacity(0.35), .clear],
                               startPoint: .leading, endPoint: .trailing)
            }
        }
    }
}

/// Sleeve with the cover; the record slides out of it after the song starts, the tonearm follows play/pause.
struct Turntable: View {
    let item: MediaItem
    var side: CGFloat = 600
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @AppStorage(AppTheme.motionKey) private var motion = true
    @State private var out = false

    var body: some View {
        ZStack(alignment: .leading) {
            VinylRecord(image: artwork.itemID == item.id ? artwork.image : nil,
                        spinning: engine.isPlaying && motion && out, size: side - 20,
                        labelTint: Palette.forItem(item.id).vibrant.with(brightness: 0.55).color)
                .offset(x: out ? side * 0.5 : 10)
            CurrentArtwork(item: item, corner: 4)
                .frame(width: side, height: side)
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(.white.opacity(0.1), lineWidth: 2))
                .shadow(color: .black.opacity(0.6), radius: 26, x: 14, y: 14)
        }
        .frame(width: side * 1.55, height: side, alignment: .leading)
        .overlay(alignment: .topTrailing) {
            Tonearm(engaged: engine.isPlaying && out, length: side * 0.66)
                .frame(width: side * 0.12, height: side * 0.66)
                .offset(x: -side * 0.02, y: -side * 0.04)
                .opacity(out ? 1 : 0)
        }
        .onAppear {
            withAnimation(.spring(response: 1.1, dampingFraction: 0.82).delay(0.45)) { out = true }
        }
    }
}

/// 2×2 mosaic of a playlist's first items (one item fills it; none = a coloured placeholder).
struct PlaylistMosaic: View {
    let playlistID: Int
    let items: [MediaItem]

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width / 2, h = geo.size.height / 2
            if items.count >= 4 {
                VStack(spacing: 0) {
                    HStack(spacing: 0) {
                        ItemThumb(item: items[0], corner: 0).frame(width: w, height: h)
                        ItemThumb(item: items[1], corner: 0).frame(width: w, height: h)
                    }
                    HStack(spacing: 0) {
                        ItemThumb(item: items[2], corner: 0).frame(width: w, height: h)
                        ItemThumb(item: items[3], corner: 0).frame(width: w, height: h)
                    }
                }
            } else if let first = items.first {
                ItemThumb(item: first, corner: 0)
            } else {
                ArtPlaceholder(item: .placeholder(playlistID, ""), palette: .forItem(playlistID * 7), showsTitle: false)
            }
        }
    }
}
