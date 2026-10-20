import SwiftUI

// Lyrics view of the iPhone player (Now Playing → the speech-bubble button). Only lyrics embedded in the file are
// shown: a music video's subtitle track, or a song's lyrics tag. The server reads them (GET /api/lyrics/<id>).

struct LyricCue: Decodable, Hashable {
    let s: Double?   // start, seconds (nil = plain text)
    let e: Double?   // end, seconds
    let t: String    // text; "" = a gap between verses (plain text only)
}

struct LyricsTrack: Decodable {
    let ord: Int?
    let lang: String
    let title: String
    let isDefault: Bool
    let forced: Bool
    let synced: Bool
    let cues: [LyricCue]
}

struct LyricsResponse: Decodable {
    let id: Int
    let lyr: Int?
    let tracks: [LyricsTrack]

    /// Same choice as the subtitle track of a video (Settings → Lyrics → Language): the matching language
    /// (default-flagged first), else the file's default, else a forced track, else the first.
    var chosen: LyricsTrack? {
        if tracks.count <= 1 { return tracks.first }
        if let codes = PlayerSettings.languageCodes[PlayerSettings.subtitleLanguage] {
            let matching = tracks.filter { codes.contains($0.lang) }
            if let pick = matching.first(where: { $0.isDefault && !$0.forced })
                ?? matching.first(where: { !$0.forced }) ?? matching.first {
                return pick
            }
        }
        return tracks.first(where: { $0.isDefault }) ?? tracks.first(where: { $0.forced }) ?? tracks.first
    }
}

/// Lyrics fetched so far (in memory). The current and the next item are fetched ahead, so switching songs
/// in the lyrics view does not wait for the network.
@MainActor
final class LyricsStore: ObservableObject {
    static let shared = LyricsStore()

    @Published private(set) var cache: [Int: LyricsResponse] = [:]
    @Published private(set) var failed: Set<Int> = []
    private var order: [Int] = []
    private var inflight: Set<Int> = []

    func prefetch(_ item: MediaItem?) {
        guard let item, item.lyr != 0, cache[item.id] == nil, !inflight.contains(item.id) else { return }
        inflight.insert(item.id)
        failed.remove(item.id)
        Task { await fetch(item.id) }
    }

    private func fetch(_ id: Int) async {
        defer { inflight.remove(id) }
        let url = Config.serverURL.appendingPathComponent("api/lyrics/\(id)")
        guard let (data, response) = try? await Net.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let parsed = try? JSONDecoder.server().decode(LyricsResponse.self, from: data) else {
            failed.insert(id)
            return
        }
        cache[id] = parsed
        order.append(id)
        if order.count > 60 { cache[order.removeFirst()] = nil }
    }
}

/// Takes the place of the cover / video. It stays a lyrics screen for every song (a song without lyrics shows its
/// info card), so skipping tracks never flips back to the regular screen and then to lyrics again.
struct LyricsPanel: View {
    let item: MediaItem
    @ObservedObject var store: LyricsStore

    var body: some View {
        ZStack {
            content
                .id(item.id)
                .transition(.opacity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .animation(.easeInOut(duration: 0.3), value: item.id)
        .animation(.easeInOut(duration: 0.3), value: store.cache[item.id] != nil)
        .task(id: item.id) { store.prefetch(item) }
    }

    @ViewBuilder private var content: some View {
        if let response = store.cache[item.id] {
            if let track = response.chosen {
                if track.synced { SyncedLyrics(track: track) } else { PlainLyrics(track: track) }
            } else {
                SongInfoCard(item: item)
            }
        } else if item.lyr == 0 || store.failed.contains(item.id) {
            SongInfoCard(item: item)
        } else {
            Color.clear  // still loading: stay on the lyrics screen
        }
    }
}

private var edgeFade: LinearGradient {
    LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.09),
                           .init(color: .black, location: 0.9), .init(color: .clear, location: 1)],
                   startPoint: .top, endPoint: .bottom)
}

/// Time-synced lyrics: the current line is bright and centred, the others are dim; scrolls by itself (not for
/// 3 s after you touched it).
struct SyncedLyrics: View {
    let track: LyricsTrack
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.appTheme) private var theme
    @State private var index = -1
    @State private var lit = false
    @State private var touched = Date.distantPast

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 18) {
                    Color.clear.frame(height: 70)
                    ForEach(track.cues.indices, id: \.self) { i in
                        let active = i == index && lit
                        Text(track.cues[i].t)
                            .font(theme.display(27))
                            .foregroundStyle(theme.ink)
                            .opacity(active ? 1 : 0.3)
                            .scaleEffect(active ? 1 : 0.93, anchor: .leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .animation(.easeOut(duration: 0.3), value: active)
                            .id(i)
                    }
                    Color.clear.frame(height: 180)
                }
            }
            .simultaneousGesture(DragGesture(minimumDistance: 4).onChanged { _ in touched = Date() })
            .onReceive(Timer.publish(every: 0.25, on: .main, in: .common).autoconnect()) { _ in
                update(proxy, animated: true)
            }
            .onAppear { update(proxy, animated: false) }
        }
        .mask(edgeFade)
    }

    private func update(_ proxy: ScrollViewProxy, animated: Bool) {
        let t = engine.livePosition
        var found = -1
        for (i, cue) in track.cues.enumerated() {
            if let s = cue.s, s <= t { found = i } else { break }
        }
        let on = found >= 0 && (track.cues[found].e.map { t <= $0 + 1.5 } ?? true)
        if on != lit { lit = on }
        guard found != index else { return }
        index = found
        guard found >= 0, Date().timeIntervalSince(touched) > 3 else { return }
        if animated {
            withAnimation(.easeInOut(duration: 0.5)) { proxy.scrollTo(found, anchor: UnitPoint(x: 0.5, y: 0.36)) }
        } else {
            proxy.scrollTo(found, anchor: UnitPoint(x: 0.5, y: 0.36))
        }
    }
}

/// Lyrics without time stamps: the whole text, scrollable.
struct PlainLyrics: View {
    let track: LyricsTrack
    @Environment(\.appTheme) private var theme

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 8) {
                Text("LYRICS · NOT SYNCED")
                    .font(.system(size: 10, weight: .heavy)).tracking(3)
                    .opacity(0.45)
                    .padding(.bottom, 6)
                ForEach(track.cues.indices, id: \.self) { i in
                    if track.cues[i].t.isEmpty {
                        Color.clear.frame(height: 14)
                    } else {
                        Text(track.cues[i].t)
                            .font(theme.font(20, .medium))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Color.clear.frame(height: 60)
            }
            .foregroundStyle(theme.ink.opacity(0.92))
            .padding(.top, 12)
        }
        .mask(edgeFade)
    }
}

/// Shown instead of lyrics when the file has none: what the app knows about the song.
struct SongInfoCard: View {
    let item: MediaItem
    @Environment(\.appTheme) private var theme

    private var rows: [(String, String)] {
        var rows: [(String, String)] = [("Title", item.title)]
        if let artist = item.artist, !artist.isEmpty { rows.append(("Artist", artist)) }
        if let album = item.album, !album.isEmpty { rows.append(("Album", album)) }
        if let year = item.year, !year.isEmpty { rows.append(("Year", String(year.prefix(4)))) }
        if let track = item.track, track > 0 { rows.append(("Track", "\(track)")) }
        var format = item.kind == "video" ? ["Music video"] : []
        if let codec = item.codec, !codec.isEmpty { format.append(codec.uppercased()) }
        if item.lossless == true { format.append("lossless") }
        if !format.isEmpty { rows.append(("Format", format.joined(separator: " · "))) }
        if let duration = item.duration, duration > 0 { rows.append(("Length", formatTime(duration))) }
        if let folder = item.folder, !folder.isEmpty {
            rows.append(("Folder", folder.split(separator: "/").last.map(String.init) ?? folder))
        }
        return rows
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("NO LYRICS IN THIS FILE")
                .font(.system(size: 10, weight: .heavy)).tracking(3)
                .opacity(0.45)
                .padding(.bottom, 4)
            ForEach(rows, id: \.0) { row in
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.0.uppercased())
                        .font(.system(size: 10, weight: .bold)).tracking(2)
                        .opacity(0.45)
                    Text(row.1)
                        .font(theme.font(20, .medium))
                        .lineLimit(3)
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(theme.ink)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(.top, 12)
    }
}
