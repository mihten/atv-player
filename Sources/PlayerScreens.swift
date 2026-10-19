import SwiftUI

/// Player screen for songs in the themed looks. `forward` = direction of the last skip (transitions slide
/// that way), `hints` = the remote-button hints are visible (they fade with the 5 s overlay timer).
struct ThemedAudioScreen: View {
    let item: MediaItem
    let forward: Bool
    let hints: Bool
    @Environment(\.appTheme) private var theme

    var body: some View {
        switch theme {
        case .noir: NoirPlayer(item: item, forward: forward, hints: hints)
        case .vinyl: VinylPlayer(item: item, forward: forward, hints: hints)
        default: AuroraPlayer(item: item, forward: forward, hints: hints)
        }
    }
}

// MARK: - Aurora

struct AuroraPlayer: View {
    let item: MediaItem
    let forward: Bool
    let hints: Bool
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var artwork: ArtworkStore
    @AppStorage(AppTheme.motionKey) private var motion = true
    @State private var appeared = false

    var body: some View {
        let theme = AppTheme.aurora
        let accent = theme.accent(artwork.palette)
        ZStack {
            AuroraBackground(palette: artwork.palette, animate: motion, intensity: 1)
            HStack(spacing: 100) {
                CurrentArtwork(item: item, corner: 30)
                    .frame(width: 640, height: 640)
                    .shadow(color: artwork.palette.vibrant.color.opacity(0.55), radius: 90)
                    .shadow(color: .black.opacity(0.45), radius: 30, y: 24)
                    .scaleEffect(engine.isPlaying ? 1 : 0.9)
                    .id(item.id)
                    .transition(.slide(forward: forward, distance: 280))
                VStack(alignment: .leading, spacing: 20) {
                    StatusEyebrow(accent: accent)
                    VStack(alignment: .leading, spacing: 14) {
                        Text(item.title)
                            .font(theme.display(76))
                            .lineLimit(3)
                            .minimumScaleFactor(0.55)
                        if let artist = item.artist, !artist.isEmpty {
                            Text(artist).font(theme.font(40, .semibold)).opacity(0.82).lineLimit(1)
                        }
                        if let album = item.album, !album.isEmpty {
                            Text(album).font(theme.font(30)).opacity(0.55).lineLimit(1)
                        }
                        Facts(item: item, theme: theme).padding(.top, 6)
                    }
                    .id(item.id)
                    .transition(.slide(forward: forward, distance: 160))
                    PlayerProgress(clock: engine.clock, accent: accent, style: .glow)
                        .padding(.top, 26)
                    UpNext(accent: accent).padding(.top, 26)
                }
                .frame(width: 900, alignment: .leading)
            }
            .padding(.horizontal, 100)
            .scaleEffect(appeared ? 1 : 0.94)
            .opacity(appeared ? 1 : 0)
            RemoteHints(visible: hints)
        }
        .foregroundStyle(.white)
        .animation(.spring(response: 0.75, dampingFraction: 0.86), value: item.id)
        .animation(.spring(response: 0.6, dampingFraction: 0.68), value: engine.isPlaying)
        .onAppear { withAnimation(.spring(response: 0.9, dampingFraction: 0.85)) { appeared = true } }
    }
}

// MARK: - Noir

struct NoirPlayer: View {
    let item: MediaItem
    let forward: Bool
    let hints: Bool
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var artwork: ArtworkStore
    @State private var appeared = false

    var body: some View {
        let theme = AppTheme.noir
        let accent = theme.accent(artwork.palette)
        ZStack(alignment: .topLeading) {
            NoirBackground(palette: artwork.palette, intensity: 1)
            HStack {
                Spacer()
                CurrentArtwork(item: item, corner: 2, showsTitle: false)
                    .frame(width: 540, height: 540)
                    .grayscale(engine.isPlaying ? 0 : 1)  // paused: the art goes black and white
                    .shadow(color: .black, radius: 40, y: 20)
                    .id(item.id)
                    .transition(.blurFade(CGSize(width: forward ? 80 : -80, height: 0)))
            }
            .padding(.top, 90)
            .padding(.trailing, 100)
            .opacity(appeared ? 1 : 0)

            VStack(alignment: .leading, spacing: 0) {
                Spacer()
                VStack(alignment: .leading, spacing: 6) {
                    Text((item.artist ?? " ").uppercased())
                        .font(.system(size: 30, weight: .bold))
                        .tracking(9)
                        .foregroundStyle(accent)
                        .lineLimit(1)
                    Text(item.title)
                        .font(theme.display(132))
                        .lineLimit(2)
                        .minimumScaleFactor(0.42)
                        .frame(maxWidth: 1180, alignment: .leading)
                    Text(([item.album].compactMap { $0 } + item.facts).joined(separator: "   /   "))
                        .font(.system(size: 24, weight: .medium, design: .monospaced))
                        .opacity(0.5)
                        .lineLimit(1)
                }
                .id(item.id)
                .transition(.rise)
                HStack(alignment: .lastTextBaseline) {
                    NoirClock(clock: engine.clock)
                    Spacer()
                    if let next = connection.state?.upcoming?.first {
                        VStack(alignment: .trailing, spacing: 6) {
                            Text("NEXT").font(.system(size: 20, weight: .heavy)).tracking(6).foregroundStyle(accent)
                            Text(next.title).font(theme.font(28, .semibold)).lineLimit(1)
                            Text(next.artist ?? "").font(theme.font(22)).opacity(0.55).lineLimit(1)
                        }
                        .frame(maxWidth: 520, alignment: .trailing)
                    }
                }
                .padding(.top, 34)
                PlayerProgress(clock: engine.clock, accent: .white, style: .hairline)
                    .padding(.top, 30)
            }
            .padding(.horizontal, 100)
            .padding(.bottom, 80)
            .offset(y: appeared ? 0 : 60)
            .opacity(appeared ? 1 : 0)
            RemoteHints(visible: hints, topPlacement: true)
        }
        .foregroundStyle(.white)
        .animation(.spring(response: 0.6, dampingFraction: 0.9), value: item.id)
        .animation(.easeInOut(duration: 0.8), value: engine.isPlaying)
        .onAppear { withAnimation(.spring(response: 0.8, dampingFraction: 0.9)) { appeared = true } }
    }
}

/// Elapsed time in big thin digits that roll (numeric content transition).
struct NoirClock: View {
    @ObservedObject var clock: PlaybackClock

    var body: some View {
        HStack(alignment: .lastTextBaseline, spacing: 14) {
            Text(formatTime(clock.position))
                .font(.system(size: 96, weight: .ultraLight, design: .monospaced))
                .contentTransition(.numericText())
                .animation(.snappy, value: Int(clock.position))
            Text("/ " + formatTime(clock.duration))
                .font(.system(size: 30, weight: .regular, design: .monospaced))
                .opacity(0.45)
        }
    }
}

// MARK: - Vinyl

struct VinylPlayer: View {
    let item: MediaItem
    let forward: Bool
    let hints: Bool
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var artwork: ArtworkStore
    @State private var appeared = false

    var body: some View {
        let theme = AppTheme.vinyl
        let accent = theme.accent(artwork.palette)
        ZStack {
            VinylBackground(palette: artwork.palette, intensity: 1)
            HStack(spacing: 40) {
                Turntable(item: item)
                    .id(item.id)
                    .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity),
                                            removal: .move(edge: .top).combined(with: .opacity)))
                VStack(alignment: .leading, spacing: 18) {
                    Text(sideLabel)
                        .font(.system(size: 28, weight: .regular, design: .serif).italic())
                        .foregroundStyle(accent)
                    VStack(alignment: .leading, spacing: 12) {
                        Text(item.title)
                            .font(theme.display(68))
                            .lineLimit(3)
                            .minimumScaleFactor(0.55)
                        if let artist = item.artist, !artist.isEmpty {
                            Text(artist).font(.system(size: 38, weight: .regular, design: .serif).italic()).opacity(0.85)
                        }
                        Text(([item.album].compactMap { $0 } + (item.year.map { [String($0.prefix(4))] } ?? []))
                                .joined(separator: ", "))
                            .font(theme.font(28)).opacity(0.55).lineLimit(1)
                    }
                    .id(item.id)
                    .transition(.blurFade(CGSize(width: 0, height: 30)))
                    PlayerProgress(clock: engine.clock, accent: accent, style: .needle)
                        .padding(.top, 26)
                    UpNext(accent: accent, numbered: true).padding(.top, 22)
                }
                .frame(width: 760, alignment: .leading)
                .foregroundStyle(theme.ink)
            }
            .padding(.horizontal, 80)
            .opacity(appeared ? 1 : 0)
            RemoteHints(visible: hints)
        }
        .animation(.spring(response: 0.9, dampingFraction: 0.85), value: item.id)
        .onAppear { withAnimation(.easeOut(duration: 0.6)) { appeared = true } }
    }

    private var sideLabel: String {
        if let track = item.track, track > 0 { return "Side A  ·  Track \(track)" }
        return engine.isPlaying ? "Now spinning" : "Paused"
    }
}

// MARK: - Shared pieces

struct StatusEyebrow: View {
    let accent: Color
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.appTheme) private var theme
    @AppStorage(AppTheme.motionKey) private var motion = true

    var body: some View {
        HStack(spacing: 14) {
            EqualizerBars(animating: engine.isPlaying && motion, color: accent, width: 6, height: 28)
            Text(engine.isPlaying ? "NOW PLAYING" : "PAUSED")
                .font(theme.eyebrow(24))
                .tracking(5)
                .foregroundStyle(accent)
        }
    }
}

struct Facts: View {
    let item: MediaItem
    let theme: AppTheme

    var body: some View {
        HStack(spacing: 12) {
            ForEach(item.facts, id: \.self) { FactChip(text: $0, theme: theme, size: 20) }
        }
    }
}

/// Big progress bar with times. Styles: glow (Aurora), hairline (Noir), needle (Vinyl).
struct PlayerProgress: View {
    enum Style { case glow, hairline, needle }
    @ObservedObject var clock: PlaybackClock
    let accent: Color
    let style: Style
    @Environment(\.appTheme) private var theme

    var body: some View {
        let value = clock.duration > 0 ? clock.position / clock.duration : 0
        VStack(alignment: .leading, spacing: 12) {
            switch style {
            case .glow:
                ProgressLine(value: value, color: accent, height: 10, glow: true)
            case .hairline:
                ProgressLine(value: value, color: accent, height: 3, track: .white.opacity(0.2))
            case .needle:
                ProgressLine(value: value, color: accent, height: 3, knob: true, track: accent.opacity(0.22))
            }
            if style != .hairline {
                HStack {
                    Text(formatTime(clock.position))
                    Spacer()
                    Text("-" + formatTime(max(0, clock.duration - clock.position)))
                }
                .font(.system(size: 24, weight: .medium, design: theme.fontDesign))
                .monospacedDigit()
                .opacity(0.65)
            }
        }
        .animation(.linear(duration: 0.5), value: clock.position)
    }
}

/// The next three items with small thumbnails (Aurora) or as a numbered list (Vinyl).
struct UpNext: View {
    let accent: Color
    var numbered = false
    @EnvironmentObject var connection: ServerConnection
    @Environment(\.appTheme) private var theme

    var body: some View {
        if let upcoming = connection.state?.upcoming, !upcoming.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                Text(numbered ? "Up next" : "UP NEXT")
                    .font(theme.eyebrow(20))
                    .tracking(numbered ? 0 : 4)
                    .opacity(0.6)
                if numbered {
                    ForEach(Array(upcoming.prefix(3).enumerated()), id: \.offset) { index, next in
                        HStack(spacing: 14) {
                            Text("\(index + 1).").font(theme.font(24)).foregroundStyle(accent).frame(width: 34, alignment: .leading)
                            Text(next.title).font(theme.font(26)).lineLimit(1)
                            if let artist = next.artist {
                                Text("— " + artist).font(theme.font(24).italic()).opacity(0.55).lineLimit(1)
                            }
                        }
                    }
                } else {
                    HStack(spacing: 22) {
                        ForEach(upcoming.prefix(3)) { next in
                            HStack(spacing: 14) {
                                ItemThumb(item: next, corner: 10).frame(width: 72, height: 72)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(next.title).font(theme.font(22, .semibold)).lineLimit(1)
                                    Text(next.artist ?? "").font(theme.font(18)).opacity(0.6).lineLimit(1)
                                }
                            }
                            .frame(width: 280, alignment: .leading)
                        }
                    }
                }
            }
        }
    }
}

/// Remote button hints, shown for 5 s after a press.
struct RemoteHints: View {
    let visible: Bool
    var topPlacement = false
    @Environment(\.appTheme) private var theme

    var body: some View {
        VStack {
            if !topPlacement { Spacer() }
            Text("▼ next     ▲ previous     ◀ −\(Int(PlayerSettings.seekBack)) s     ▶ +\(Int(PlayerSettings.seekForward)) s     ⏯ pause")
                .font(theme.font(22, .medium))
                .padding(.horizontal, 26)
                .padding(.vertical, 12)
                .background(.black.opacity(0.35), in: Capsule())
                .opacity(visible ? 0.8 : 0)
                .animation(.easeInOut(duration: 0.5), value: visible)
            if topPlacement { Spacer() }
        }
        .padding(.vertical, 40)
        .foregroundStyle(.white)
    }
}

/// Info bar over a playing music video, per theme.
struct ThemedInfoBar: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme

    var body: some View {
        let accent = theme.accent(artwork.palette)
        if let item = engine.current {
            HStack(spacing: 32) {
                if theme != .noir {
                    CurrentArtwork(item: item, corner: theme.corner * 0.6, showsTitle: false)
                        .frame(width: 240, height: 135)
                }
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline, spacing: 18) {
                        Text(item.title).font(theme.display(44)).lineLimit(1)
                        if let artist = item.artist {
                            Text(artist).font(theme.font(30)).opacity(0.7).lineLimit(1)
                        }
                    }
                    PlayerProgress(clock: engine.clock, accent: accent, style: theme == .vinyl ? .needle : .glow)
                    HStack(spacing: 26) {
                        if let next = connection.state?.upcoming?.first {
                            Text("Next: \(next.title)").lineLimit(1)
                        }
                        Spacer()
                        Text("▼ next   ▲ previous   ◀ −\(Int(PlayerSettings.seekBack)) s   ▶ +\(Int(PlayerSettings.seekForward)) s")
                    }
                    .font(theme.font(20))
                    .opacity(0.55)
                }
            }
            .padding(34)
            .foregroundStyle(theme.ink)
            .background {
                switch theme {
                case .noir:
                    LinearGradient(colors: [.clear, .black.opacity(0.9)], startPoint: .top, endPoint: .bottom)
                case .vinyl:
                    RoundedRectangle(cornerRadius: 14).fill(Color(red: 0.12, green: 0.08, blue: 0.05).opacity(0.86))
                        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(accent.opacity(0.35), lineWidth: 1.5))
                default:
                    RoundedRectangle(cornerRadius: 30, style: .continuous).fill(.ultraThinMaterial)
                        .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous)
                            .fill(artwork.palette.tones[0].color.opacity(0.25)))
                }
            }
            .padding(theme == .noir ? 0 : 60)
        }
    }
}
