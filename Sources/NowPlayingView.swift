import SwiftUI
import UIKit

/// Full-screen player. Siri Remote: down = next, up = previous, left/right = jump back/forward
/// (Settings), play/pause = toggle, click = lyrics on/off (and show the info), click-and-hold = quick panel (shuffle,
/// repeat, lyrics / subtitle track), Back = close the panel / return to the menu.
struct NowPlayingView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool
    @State private var overlay = true
    @State private var hideTask: Task<Void, Never>?
    /// Direction of the last skip: themed screens slide the next song in from that side.
    @State private var forward = true
    /// Lyrics instead of cover / video; remembered on this device, switched with the remote's click button.
    @AppStorage("lyricsMode") private var lyricsMode = false
    @State private var panel = false
    /// The current press already opened the panel, so its release is not a click.
    @State private var heldOpen = false

    var body: some View {
        ZStack {
            player
                .disabled(panel)
            if panel {
                QuickPanel { closePanel() }
                    .transition(.opacity.combined(with: .scale(scale: 0.96)))
            }
        }
        .animation(.easeOut(duration: 0.2), value: panel)
    }

    private var player: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let item = engine.current {
                if item.isVideo {
                    // Stays in place under the lyrics: the picture keeps being decoded, so leaving the lyrics shows
                    // a clean picture at once (no decoder restart, no blocks until the next keyframe).
                    VideoSurface(engine: engine).ignoresSafeArea()
                    PictureCover().ignoresSafeArea()
                }
                if lyricsMode {
                    if item.isVideo { Color.black.ignoresSafeArea() }  // the Classic backdrop is transparent
                    TVLyricsScreen(item: item, forward: forward, hints: overlay)
                        .ignoresSafeArea()
                } else if item.isVideo {
                    EmptyView()
                } else if theme.isClassic {
                    AudioScreen(item: item)
                } else {
                    ThemedAudioScreen(item: item, forward: forward, hints: overlay)
                        .ignoresSafeArea()
                }
            } else {
                Text("Nothing playing").foregroundStyle(.secondary)
            }
            if lyricsMode {
                EmptyView()  // the lyrics screen has its own title and progress
            } else if theme.isClassic {
                if overlay || engine.current?.isVideo != true {
                    VStack {
                        Spacer()
                        InfoBar()
                    }
                    .transition(.opacity)
                }
            } else if overlay, engine.current?.isVideo == true {
                VStack {
                    Spacer()
                    ThemedInfoBar()
                }
                .ignoresSafeArea(edges: theme == .noir ? .bottom : [])
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onAppear {
            focused = true
            if DemoRoute.argument("lyricsMode") != nil { lyricsMode = true }  // CI screenshots only
            syncLyrics()
            flash()
        }
        .onDisappear { engine.setLyricsCover(false) }
        .onChange(of: lyricsMode) { syncLyrics() }
        .onMoveCommand { direction in
            switch direction {
            case .down:
                forward = true
                engine.next()
            case .up:
                forward = false
                engine.previous()
            case .left: engine.seek(by: -PlayerSettings.seekBack)
            case .right: engine.seek(by: PlayerSettings.seekForward)
            default: break
            }
            flash()
        }
        .onPlayPauseCommand {
            engine.togglePlay(source: "player-screen")
            flash()
        }
        // One handler for both, so a hold never also counts as a click: released before 0.5 s = click (lyrics),
        // held 0.5 s = quick panel.
        .onLongPressGesture(minimumDuration: 0.5) {
            heldOpen = true
            panel = true
            remoteLog("remote: centre held -> quick panel open")
        } onPressingChanged: { pressing in
            guard !pressing else { return }
            if heldOpen {
                heldOpen = false
            } else if !panel {
                lyricsMode.toggle()
                remoteLog("remote: centre click -> lyrics \(lyricsMode ? "on" : "off")")
                flash()
            }
        }
        .onExitCommand { dismiss() }
        .onChange(of: engine.current?.id) {
            // auto-advance after a "previous" goes forward again
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { forward = true }
            syncLyrics()
        }
    }

    /// Tells the engine the lyrics are up (on the Apple TV the picture track stays on under them); the lyrics of this
    /// and the next item are fetched.
    private func syncLyrics() {
        engine.setLyricsCover(lyricsMode)
        guard lyricsMode else { return }
        LyricsStore.shared.prefetch(engine.current)
        LyricsStore.shared.prefetch(connection.state?.upcoming?.first)
    }

    /// Remote-input lines for the server log (checks the click / hold handling on the device).
    private func remoteLog(_ message: String) {
        connection.send(["type": "log", "message": message])
    }

    private func closePanel() {
        remoteLog("remote: quick panel closed")
        panel = false
        heldOpen = false
        focused = true
        flash()
    }

    private func flash() {
        withAnimation { overlay = true }
        hideTask?.cancel()
        hideTask = Task {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            if !Task.isCancelled {
                withAnimation { overlay = false }
            }
        }
    }
}

struct InfoBar: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let item = engine.current {
                Text(item.title)
                    .font(.title2)
                    .bold()
                    .lineLimit(1)
                if !item.subtitle.isEmpty {
                    Text(item.subtitle)
                        .font(.headline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            ProgressRow(clock: engine.clock, upNext: connection.state?.upcoming?.first?.title)
            HStack(spacing: 30) {
                Label(engine.isPlaying ? "Playing" : "Paused", systemImage: engine.isPlaying ? "play.fill" : "pause.fill")
                PlayModeIcons(size: 22)
                Text("▼ next   ▲ previous   ◀ −\(Int(PlayerSettings.seekBack)) s   ▶ +\(Int(PlayerSettings.seekForward)) s   ● lyrics   hold ● options")
                    .foregroundStyle(.secondary)
            }
            .font(.caption)
        }
        .padding(40)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24))
        .padding(60)
    }
}

/// Progress bar + times. Observes only the clock, which the engine updates twice a second.
struct ProgressRow: View {
    @ObservedObject var clock: PlaybackClock
    let upNext: String?

    var body: some View {
        VStack(spacing: 14) {
            ProgressView(value: clock.duration > 0 ? min(clock.position / clock.duration, 1) : 0)
            HStack {
                Text(formatTime(clock.position))
                Spacer()
                if let upNext {
                    Text("Next: \(upNext)").lineLimit(1)
                }
                Spacer()
                Text(formatTime(clock.duration))
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}

struct AudioScreen: View {
    let item: MediaItem

    var body: some View {
        ZStack {
            AsyncImage(url: item.artURL) { image in
                image.resizable().scaledToFill().blur(radius: 60).opacity(0.45)
            } placeholder: {
                Color.black
            }
            .ignoresSafeArea()
            .clipped()

            AsyncImage(url: item.artURL) { image in
                image.resizable().scaledToFit()
            } placeholder: {
                Image(systemName: "music.note")
                    .font(.system(size: 220))
                    .foregroundStyle(.secondary)
            }
            .frame(width: 560, height: 560)
            .clipShape(RoundedRectangle(cornerRadius: 20))
            .shadow(radius: 30)
            .offset(y: -150)
        }
        .id(item.id)
    }
}
