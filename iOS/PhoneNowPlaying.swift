import SwiftUI

/// The iPhone plays its own queue (separate from the Apple TV's): every control drives the local engine;
/// next/previous go through the server, which owns the queue.
@MainActor
struct Transport {
    let connection: ServerConnection
    let engine: PlayerEngine

    var item: MediaItem? { engine.current }
    var isPlaying: Bool { engine.isPlaying }
    var position: Double { engine.livePosition }
    var duration: Double { engine.liveDuration }

    func toggle() { engine.togglePlay(source: "phone") }
    func next() { engine.next() }
    func previous() { engine.previous() }
    func seek(to seconds: Double) { engine.seek(to: seconds) }
}

struct Artwork: View {
    let item: MediaItem
    var size: CGFloat

    var body: some View {
        RemoteImage(url: item.art == true ? item.artURL : nil) {
            ZStack {
                Color.secondary.opacity(0.15)
                Image(systemName: item.isVideo ? "film" : "music.note")
                    .font(.system(size: size * 0.4))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: size * 0.08))
    }
}

/// Bar above the tab bar: artwork, title, play/pause, next. Tap = full player.
struct MiniPlayer: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.showPlayer) private var showPlayer
    @Environment(\.appTheme) private var theme

    var body: some View {
        if theme.isClassic {
            classic
        } else {
            ThemedMiniPlayer()
        }
    }

    @ViewBuilder private var classic: some View {
        let transport = Transport(connection: connection, engine: engine)
        if let item = transport.item {
            HStack(spacing: 12) {
                Artwork(item: item, size: 44)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                    if let artist = item.artist, !artist.isEmpty {
                        Text(artist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                Button { transport.toggle() } label: {
                    Image(systemName: transport.isPlaying ? "pause.fill" : "play.fill").font(.title2)
                }
                .frame(width: 44, height: 44)
                Button { transport.next() } label: {
                    Image(systemName: "forward.end.fill").font(.title3)
                }
                .frame(width: 44, height: 44)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(.regularMaterial)
            .contentShape(Rectangle())
            .onTapGesture { showPlayer() }
        }
    }
}

/// Floating card above the tab bar (themed looks): art, title, a hairline of progress, play/pause, next.
struct ThemedMiniPlayer: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.showPlayer) private var showPlayer
    @Environment(\.playerNamespace) private var ns
    @Environment(\.appTheme) private var theme
    @AppStorage(AppTheme.motionKey) private var motion = true
    @State private var taps = 0

    var body: some View {
        let transport = Transport(connection: connection, engine: engine)
        if let item = transport.item {
            let accent = theme.accent(artwork.palette)
            HStack(spacing: 12) {
                Group {
                    if theme == .vinyl && !item.isVideo {
                        VinylRecord(image: artwork.art(for: item.id),
                                    spinning: engine.isPlaying && motion, size: 46,
                                    labelTint: Palette.forItem(item.id).vibrant.with(brightness: 0.55).color)
                    } else {
                        CurrentArtwork(item: item, corner: theme == .noir ? 2 : 9, showsTitle: false)
                            .frame(width: item.isVideo ? 64 : 46, height: item.isVideo ? 36 : 46)
                    }
                }
                .zoomSource(ns)
                .id(item.id)
                .transition(.blurFade(CGSize(width: 20, height: 0), blur: 8))
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title).font(theme.font(15, .semibold)).lineLimit(1)
                    Text(item.artist ?? " ").font(theme.font(13)).opacity(0.65).lineLimit(1)
                }
                .id("t\(item.id)")
                .transition(.blurFade(CGSize(width: 0, height: 8), blur: 6))
                Spacer(minLength: 0)
                Button {
                    taps += 1
                    transport.toggle()
                } label: {
                    Image(systemName: transport.isPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 22, weight: .semibold))
                        .contentTransition(.symbolEffect(.replace))
                        .frame(width: 42, height: 42)
                }
                Button {
                    taps += 1
                    transport.next()
                } label: {
                    Image(systemName: "forward.end.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .frame(width: 38, height: 42)
                }
            }
            .buttonStyle(PressScale())
            .foregroundStyle(theme.ink)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(alignment: .bottom) {
                ZStack(alignment: .bottom) {
                    MiniBackground(accentTone: artwork.palette.tones[0].color)
                    MiniProgress(clock: engine.clock, accent: accent)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 6 : 18, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: theme == .noir ? 6 : 18, style: .continuous)
                    .strokeBorder(theme == .vinyl ? accent.opacity(0.3) : .white.opacity(0.12), lineWidth: 1)
            }
            .shadow(color: .black.opacity(0.35), radius: 14, y: 6)
            .contentShape(Rectangle())
            .onTapGesture { showPlayer() }
            .padding(.horizontal, 10)
            .padding(.bottom, 6)
            .animation(.spring(response: 0.5, dampingFraction: 0.85), value: item.id)
            .sensoryFeedback(.impact(weight: .light), trigger: taps)
        }
    }
}

private struct MiniBackground: View {
    let accentTone: Color
    @Environment(\.appTheme) private var theme

    var body: some View {
        switch theme {
        case .noir: Color(white: 0.07)
        case .vinyl:
            ZStack {
                Color(red: 0.17, green: 0.11, blue: 0.07)
                GrainOverlay().opacity(0.07)
            }
        default:
            ZStack {
                Rectangle().fill(.regularMaterial)
                accentTone.opacity(0.35)
            }
        }
    }
}

private struct MiniProgress: View {
    @ObservedObject var clock: PlaybackClock
    let accent: Color

    var body: some View {
        GeometryReader { geo in
            Rectangle()
                .fill(accent)
                .frame(width: geo.size.width * (clock.duration > 0 ? min(clock.position / clock.duration, 1) : 0),
                       height: 2)
                .frame(maxHeight: .infinity, alignment: .bottom)
        }
        .animation(.linear(duration: 0.5), value: clock.position)
    }
}

/// Full player (sheet): video or artwork, title, scrubber, transport, shuffle/repeat.
struct PhoneNowPlayingView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @Environment(\.playerNamespace) private var ns
    @Binding var fullScreen: Bool
    @State private var scrubbing = false
    @State private var scrubStarted = Date.distantPast
    @State private var shown: Double = 0           // position shown by the slider
    @State private var holdUntil = Date.distantPast // after a seek: keep the new value until VLC has caught up
    @State private var forward = true
    @State private var taps = 0
    @State private var prevTaps = 0
    @State private var nextTaps = 0
    /// Lyrics instead of cover / video. Stays on across songs (a song without lyrics shows its info card), is
    /// remembered, and has no setting: it is only the button next to shuffle.
    @AppStorage("lyricsMode") private var lyricsMode = false

    var body: some View {
        let transport = Transport(connection: connection, engine: engine)
        Group {
            if theme.isClassic {
                classicBody(transport)
            } else {
                themedBody(transport)
            }
        }
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(scrubbing)
        .zoomDestination(theme.isClassic ? nil : ns)
        .fullScreenCover(isPresented: $fullScreen) {
            FullScreenVideo(transport: transport)
        }
        .onAppear {
            shown = min(transport.position, max(transport.duration, 1))
            if DemoRoute.argument("lyricsMode") != nil { lyricsMode = true }  // CI screenshots only
            syncLyrics()
        }
        .onChange(of: lyricsMode) { syncLyrics() }
        .onChange(of: fullScreen) { syncLyrics() }
        .onChange(of: engine.current?.id) { syncLyrics() }
        .onReceive(Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()) { _ in
            // safety: an "editing ended" that never arrived must not freeze the slider
            if scrubbing, Date().timeIntervalSince(scrubStarted) > 20 { scrubbing = false }
            guard !scrubbing, Date() >= holdUntil else { return }
            shown = min(transport.position, max(transport.duration, 1))
        }
        .onChange(of: engine.current?.id) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { forward = true }
        }
    }

    /// The lyrics view replaces the picture: switch a video's picture track off while it is up (not in the
    /// landscape full-screen cover, which shows the picture), and fetch the lyrics of this and the next item.
    private func syncLyrics() {
        engine.setLyricsCover(lyricsMode && !fullScreen)
        guard lyricsMode else { return }
        LyricsStore.shared.prefetch(engine.current)
        LyricsStore.shared.prefetch(connection.state?.upcoming?.first)
    }

    // MARK: Classic

    private func classicBody(_ transport: Transport) -> some View {
        VStack(spacing: 22) {
            Spacer(minLength: 0)
            if let item = transport.item {
                if lyricsMode {
                    LyricsPanel(item: item, store: LyricsStore.shared).layoutPriority(1)
                } else if item.isVideo {
                    videoSurface
                } else {
                    Artwork(item: item, size: 300)
                }
                VStack(spacing: 6) {
                    Text(item.title).font(.title3.bold()).multilineTextAlignment(.center).lineLimit(2)
                    if !item.subtitle.isEmpty {
                        Text(item.subtitle).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                scrubber(transport)
                classicControls(transport)
                Button { lyricsMode.toggle() } label: {
                    Label("Lyrics", systemImage: lyricsMode ? "quote.bubble.fill" : "quote.bubble")
                        .foregroundStyle(lyricsMode ? Color.accentColor : .secondary)
                }
                .font(.footnote)
            } else {
                Text("Nothing playing").foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24)
    }

    private var videoSurface: some View {
        VideoSurface(engine: engine, active: !fullScreen)  // the cover above owns the picture while it is up
            .id(fullScreen)  // re-attach the video view here after the full-screen cover closes
            .aspectRatio(16 / 9, contentMode: .fit)
            .overlay { PictureCover() }  // hides the blocky picture until the next keyframe (engine.pictureSettling)
            .clipShape(RoundedRectangle(cornerRadius: theme == .noir ? 2 : 12))
            .overlay(alignment: .bottomTrailing) {
                Button { fullScreen = true } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .padding(10).background(.ultraThinMaterial, in: Circle())
                }
                .padding(8)
            }
    }

    /// The slider is a STABLE view bound to `shown`; a timer only moves `shown` while nobody is dragging.
    /// (It used to sit inside a TimelineView, which rebuilt the slider twice a second: a rebuild during a
    /// drag could cancel it without the "editing ended" callback, and after letting go the value fell back to
    /// VLC's old time before the seek had been applied.)
    private func scrubber(_ transport: Transport) -> some View {
        let duration = max(transport.duration, 1)
        return VStack(spacing: 4) {
            SeekBar(value: $shown, range: duration,
                    onBegan: { beginScrub() },
                    onEnded: { target in endScrub(transport, to: target) })
            times(duration)
        }
    }

    private func beginScrub() {
        scrubbing = true
        scrubStarted = Date()
    }

    /// `target` nil = the touch changed nothing (a tap on the thumb): no seek, so no stale jump back.
    private func endScrub(_ transport: Transport, to target: Double?) {
        if let target {
            transport.seek(to: target)
            holdUntil = Date().addingTimeInterval(1.5)
        }
        scrubbing = false
    }

    private func times(_ duration: Double, font: Font = .caption) -> some View {
        HStack {
            Text(formatTime(shown))
            Spacer()
            Text("-" + formatTime(max(0, duration - shown)))
        }
        .font(font.monospacedDigit())
        .foregroundStyle(.secondary)
    }

    private func classicControls(_ transport: Transport) -> some View {
        let state = connection.state
        let repeatMode = state?.repeatMode ?? "all"
        return HStack {
            Button { connection.command("shuffle") } label: {
                Image(systemName: "shuffle").foregroundStyle(state?.shuffle == true ? Color.accentColor : .secondary)
            }
            Spacer()
            Button { transport.previous() } label: { Image(systemName: "backward.end.fill").font(.title) }
            Spacer()
            Button { transport.toggle() } label: {
                Image(systemName: transport.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 64))
            }
            Spacer()
            Button { transport.next() } label: { Image(systemName: "forward.end.fill").font(.title) }
            Spacer()
            Button { connection.command("repeat") } label: {
                Image(systemName: repeatMode == "one" ? "repeat.1" : "repeat")
                    .foregroundStyle(repeatMode == "off" ? .secondary : Color.accentColor)
            }
        }
        .buttonStyle(.plain)
        .font(.title3)
    }

    // MARK: Themed (Aurora / Noir / Vinyl)

    private func themedBody(_ transport: Transport) -> some View {
        let accent = theme.accent(artwork.palette)
        return GeometryReader { geo in
            let side = min(geo.size.width - 52, geo.size.height * 0.42, 360)
            VStack(spacing: 0) {
                Spacer(minLength: 6)
                if let item = transport.item {
                    if lyricsMode {
                        LyricsPanel(item: item, store: LyricsStore.shared).layoutPriority(1)
                        Spacer(minLength: 12)
                    } else {
                        hero(item, side: side)
                            .frame(height: item.isVideo ? nil : side)
                        Spacer(minLength: 18)
                    }
                    titleBlock(item, accent: accent)
                    ThemedScrubber(value: $shown, duration: max(transport.duration, 1), accent: accent,
                                   onBegin: beginScrub, onEnd: { endScrub(transport, to: $0) })
                        .padding(.top, 14)
                    times(max(transport.duration, 1),
                          font: .system(size: 12, weight: .medium, design: theme == .noir ? .monospaced : theme.fontDesign))
                    themedControls(transport, accent: accent)
                        .padding(.top, 12)
                    secondaryRow(accent: accent)
                        .padding(.top, 16)
                } else {
                    Text("Nothing playing").foregroundStyle(.secondary)
                }
                Spacer(minLength: 10)
            }
            .padding(.horizontal, 26)
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .foregroundStyle(theme.ink)
        .background {
            ThemeBackdrop(intensity: 1, animate: !(engine.current?.isVideo == true && engine.isPlaying))
        }
        .animation(.spring(response: 0.6, dampingFraction: 0.86), value: engine.current?.id)
        .animation(.spring(response: 0.5, dampingFraction: 0.68), value: engine.isPlaying)
        .sensoryFeedback(.impact(weight: .light), trigger: taps)
    }

    @ViewBuilder private func hero(_ item: MediaItem, side: CGFloat) -> some View {
        if item.isVideo {
            videoSurface
                .shadow(color: theme == .aurora ? artwork.palette.vibrant.color.opacity(0.4) : .black.opacity(0.5),
                        radius: 30, y: 10)
        } else {
            switch theme {
            case .vinyl:
                // sleeve + record half out of it = 1.55 × the sleeve, which must fit the hero width
                let sleeve = side / 1.55
                Turntable(item: item, side: sleeve)
                    .frame(width: sleeve * 1.55, height: sleeve)
                    .frame(maxWidth: .infinity)
                    .id(item.id)
                    .transition(.slide(forward: forward, distance: 90))
            case .noir:
                CurrentArtwork(item: item, corner: 2, showsTitle: false)
                    .frame(width: side, height: side)
                    .grayscale(engine.isPlaying ? 0 : 1)
                    .shadow(color: .black, radius: 24, y: 12)
                    .id(item.id)
                    .transition(.slide(forward: forward, distance: 90))
            default:
                CurrentArtwork(item: item, corner: 24)
                    .frame(width: side, height: side)
                    .shadow(color: artwork.palette.vibrant.color.opacity(0.55), radius: 50)
                    .shadow(color: .black.opacity(0.35), radius: 16, y: 12)
                    .scaleEffect(engine.isPlaying ? 1 : 0.86)
                    .id(item.id)
                    .transition(.slide(forward: forward, distance: 90))
            }
        }
    }

    private func titleBlock(_ item: MediaItem, accent: Color) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            switch theme {
            case .noir:
                Text((item.artist ?? " ").uppercased())
                    .font(.system(size: 13, weight: .bold)).tracking(4)
                    .foregroundStyle(accent)
                    .lineLimit(1)
                Text(item.title).font(theme.display(40)).lineLimit(2).minimumScaleFactor(0.6)
            case .vinyl:
                Text(item.title).font(theme.display(28)).lineLimit(2)
                Text(item.artist ?? " ").font(theme.font(19, .medium)).opacity(0.85).lineLimit(1)
                if let album = item.album {
                    Text(album).font(theme.font(14)).opacity(0.5).lineLimit(1)
                }
            default:
                Text(item.title).font(theme.display(26)).lineLimit(2)
                Text(item.subtitle).font(theme.font(17, .medium)).opacity(0.72).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .id(item.id)
        .transition(.blurFade(CGSize(width: forward ? 30 : -30, height: 0), blur: 10))
    }

    private func themedControls(_ transport: Transport, accent: Color) -> some View {
        HStack {
            Button {
                taps += 1
                prevTaps += 1
                forward = false
                transport.previous()
            } label: {
                Image(systemName: "backward.end.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .symbolEffect(.bounce, options: .speed(1.6), value: prevTaps)
                    .frame(width: 70, height: 70)
            }
            Spacer()
            Button {
                taps += 1
                transport.toggle()
            } label: {
                ZStack {
                    switch theme {
                    case .aurora:
                        Circle().fill(.white)
                            .shadow(color: accent.opacity(0.7), radius: transport.isPlaying ? 22 : 8)
                    case .vinyl:
                        Circle().fill(accent)
                    case .noir:
                        Circle().strokeBorder(.white, lineWidth: 2)
                    case .classic:
                        EmptyView()
                    }
                    Image(systemName: transport.isPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 32, weight: .bold))
                        .foregroundStyle(theme == .noir ? Color.white : Color.black.opacity(0.85))
                        .contentTransition(.symbolEffect(.replace))
                        .offset(x: transport.isPlaying ? 0 : 2)
                }
                .frame(width: 84, height: 84)
            }
            Spacer()
            Button {
                taps += 1
                nextTaps += 1
                forward = true
                transport.next()
            } label: {
                Image(systemName: "forward.end.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .symbolEffect(.bounce, options: .speed(1.6), value: nextTaps)
                    .frame(width: 70, height: 70)
            }
        }
        .buttonStyle(PressScale())
        .padding(.horizontal, 6)
    }

    private func secondaryRow(accent: Color) -> some View {
        let state = connection.state
        let repeatMode = state?.repeatMode ?? "all"
        return HStack(spacing: 14) {
            Button { connection.command("shuffle") } label: {
                Image(systemName: "shuffle")
                    .foregroundStyle(state?.shuffle == true ? accent : theme.ink.opacity(0.45))
                    .frame(width: 40, height: 36)
            }
            Button { lyricsMode.toggle() } label: {
                Image(systemName: lyricsMode ? "quote.bubble.fill" : "quote.bubble")
                    .foregroundStyle(lyricsMode ? accent : theme.ink.opacity(0.45))
                    .frame(width: 40, height: 36)
            }
            Spacer(minLength: 0)
            if let next = state?.upcoming?.first {
                HStack(spacing: 6) {
                    Text("NEXT")
                        .font(.system(size: 10, weight: .heavy))
                        .tracking(2)
                        .foregroundStyle(accent)
                    Text(next.title).font(theme.font(13)).opacity(0.7).lineLimit(1)
                }
                .id(next.id)
                .transition(.blurFade(CGSize(width: 0, height: 6), blur: 6))
            }
            Spacer(minLength: 0)
            Button { connection.command("repeat") } label: {
                Image(systemName: repeatMode == "one" ? "repeat.1" : "repeat")
                    .foregroundStyle(repeatMode == "off" ? theme.ink.opacity(0.45) : accent)
                    .frame(width: 40, height: 36)
            }
        }
        .font(.system(size: 17, weight: .semibold))
        .buttonStyle(PressScale())
    }
}

/// Landscape-friendly full-screen video; tap shows/hides the controls.
struct FullScreenVideo: View {
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.dismiss) private var dismiss
    let transport: Transport
    @State private var controls = true
    @State private var hideTask: Task<Void, Never>?
    // Progress slider (same approach as the player sheet: a stable Slider bound to `shown`, a timer moves
    // it only while nobody drags, and it keeps the new value for 1.5 s after a seek).
    @State private var scrubbing = false
    @State private var scrubStarted = Date.distantPast
    @State private var shown: Double = 0
    @State private var holdUntil = Date.distantPast

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VideoSurface(engine: engine).ignoresSafeArea().allowsHitTesting(false)
            PictureCover().ignoresSafeArea()
            // Own tap layer between the picture and the controls: show/hide the controls.
            Color.clear
                .ignoresSafeArea()
                .contentShape(Rectangle())
                .onTapGesture {
                    engine.diag("full screen: tap, controls were \(controls ? "shown" : "hidden")")
                    if controls {
                        hideTask?.cancel()
                        withAnimation { controls = false }
                    } else {
                        showControls()
                    }
                }
            if controls {
                VStack {
                    HStack {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark").padding(12).background(.ultraThinMaterial, in: Circle())
                        }
                        Spacer()
                    }
                    Spacer()
                    progressBar
                    HStack(spacing: 50) {
                        Button { transport.previous(); showControls() } label: { Image(systemName: "backward.end.fill") }
                        Button { transport.toggle(); showControls() } label: {
                            Image(systemName: transport.isPlaying ? "pause.fill" : "play.fill")
                                .contentTransition(.symbolEffect(.replace))
                        }
                        Button { transport.next(); showControls() } label: { Image(systemName: "forward.end.fill") }
                    }
                    .font(.largeTitle)
                    .padding(.bottom, 14)
                }
                .padding()
                .foregroundStyle(.white)
                .buttonStyle(.plain)
                .transition(.opacity)
            }
        }
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        // Tap (layer above): show the controls (they hide again after 3 s); tap while shown hides them at once.
        .onAppear {
            engine.diag("full screen: opened")
            shown = min(transport.position, max(transport.duration, 1))
            showControls()
        }
        .onReceive(Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()) { _ in
            if scrubbing, Date().timeIntervalSince(scrubStarted) > 20 { scrubbing = false }  // lost "editing ended"
            guard !scrubbing, Date() >= holdUntil else { return }
            shown = min(transport.position, max(transport.duration, 1))
        }
        .onDisappear {
            engine.diag("full screen: closed")
            hideTask?.cancel()
        }
        // A new video (next / auto-advance) starts with the controls visible for 3 s.
        .onChange(of: engine.current?.id) {
            engine.diag("full screen: item changed")
            showControls()
        }
        .onChange(of: engine.current?.isVideo == true) { _, isVideo in
            if !isVideo { dismiss() }  // the queue moved on to a song
        }
    }

    /// Slider with elapsed / remaining time. The controls stay up while the finger is on it.
    private var progressBar: some View {
        let duration = max(transport.duration, 1)
        return VStack(spacing: 2) {
            SeekBar(value: $shown, range: duration, tint: .white, track: .white.opacity(0.3),
                    onBegan: {
                        scrubbing = true
                        scrubStarted = Date()
                        hideTask?.cancel()
                    },
                    onEnded: { target in
                        if let target {
                            transport.seek(to: target)
                            holdUntil = Date().addingTimeInterval(1.5)
                        }
                        scrubbing = false
                        showControls()
                    })
            HStack {
                Text(formatTime(shown))
                Spacer()
                Text("-" + formatTime(max(0, duration - shown)))
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(.white.opacity(0.75))
        }
        .padding(.horizontal, 24)
        .padding(.bottom, 10)
    }

    private func showControls() {
        withAnimation { controls = true }
        hideTask?.cancel()
        guard !scrubbing else { return }  // no auto-hide under a dragging finger
        hideTask = Task {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if !Task.isCancelled { withAnimation { controls = false } }
        }
    }
}

/// Covers the video while `engine.pictureSettling`: the picture track came back mid-stream and stays blocky until the
/// next keyframe. Shows the item's artwork on black and fades out when the engine lifts it. Never takes touches.
struct PictureCover: View {
    @EnvironmentObject var engine: PlayerEngine

    var body: some View {
        ZStack {
            if engine.pictureSettling, let item = engine.current {
                ZStack {
                    Color.black
                    RemoteImage(url: item.art == true ? item.artURL : nil) { Color.black }
                        .opacity(0.85)
                }
                .clipped()
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.4), value: engine.pictureSettling)
        .allowsHitTesting(false)
    }
}
