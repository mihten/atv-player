import SwiftUI

/// Art of the item that is playing now (ArtworkStore keeps it with its palette), or a designed placeholder.
struct CurrentArtwork: View {
    let item: MediaItem
    var corner: CGFloat
    var showsTitle = true
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        ZStack {
            if artwork.itemID == item.id, let image = artwork.image {
                Image(uiImage: image).resizable().scaledToFill().transition(.opacity)
            } else {
                ArtPlaceholder(item: item, palette: artwork.itemID == item.id ? artwork.palette : .forItem(item.id),
                               showsTitle: showsTitle)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: corner, style: .continuous))
    }
}

/// A generated "cover" for songs without art (most of a real library): a gradient in the song's own
/// colour, a large faint symbol and the title set in the theme's typeface.
struct ArtPlaceholder: View {
    let item: MediaItem
    let palette: Palette
    var showsTitle = true
    @Environment(\.appTheme) private var theme

    var body: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height)
            ZStack(alignment: .bottomLeading) {
                LinearGradient(colors: [palette.vibrant.with(brightness: 0.72).color, palette.tones[0].color,
                                        palette.base.color],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                Image(systemName: item.symbol)
                    .resizable()
                    .scaledToFit()
                    .frame(width: s * 0.46, height: s * 0.46)
                    .foregroundStyle(.white.opacity(0.13))
                    .position(x: geo.size.width * 0.7, y: geo.size.height * 0.34)
                if showsTitle && s > 110 {
                    VStack(alignment: .leading, spacing: s * 0.015) {
                        Text(item.title)
                            .font(theme.display(s * 0.085))
                            .lineLimit(3)
                            .minimumScaleFactor(0.6)
                        if let artist = item.artist, !artist.isEmpty {
                            Text(artist).font(theme.font(s * 0.05, .medium)).opacity(0.78).lineLimit(1)
                        }
                    }
                    .foregroundStyle(.white)
                    .padding(s * 0.07)
                }
            }
        }
    }
}

/// Row/tile thumbnail for any item: server art, or a small coloured placeholder with its symbol.
struct ItemThumb: View {
    let item: MediaItem
    var corner: CGFloat = 8

    var body: some View {
        ArtworkImage(url: item.artURLIfAny, cornerRadius: corner, symbol: item.symbol,
                     tint: Palette.forItem(item.id).vibrant.with(brightness: 0.62, saturation: 0.55).color)
    }
}

extension PlaylistEntry {
    /// Enough of a MediaItem for thumbnails (art URL, symbol, title).
    var asMediaItem: MediaItem {
        MediaItem(id: id, kind: kind ?? "audio", title: title, artist: artist, album: nil, folder: nil, root: nil,
                  duration: nil, lossless: lossless, codec: nil, art: nil, audioTrack: nil, audioOrd: nil,
                  subTrack: nil, subOrd: nil, subs: nil, track: nil, year: nil)
    }
}

extension MediaItem {
    static func placeholder(_ id: Int, _ title: String) -> MediaItem {
        MediaItem(id: id, kind: "audio", title: title, artist: nil, album: nil, folder: nil, root: nil, duration: nil,
                  lossless: nil, codec: nil, art: false, audioTrack: nil, audioOrd: nil, subTrack: nil, subOrd: nil,
                  subs: nil, track: nil, year: nil)
    }
}
