import SwiftUI
import UIKit

/// Full-screen player. Siri Remote: down = next, up = previous, left/right = jump back/forward
/// (Settings), play/pause = toggle, click = show info, Back = return to the menu.
struct NowPlayingView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool
    @State private var overlay = true
    @State private var hideTask: Task<Void, Never>?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let item = engine.current {
                if item.isVideo {
                    VideoSurface(engine: engine).ignoresSafeArea()
                } else {
                    AudioScreen(item: item)
                }
            } else {
                Text("Nothing playing").foregroundStyle(.secondary)
            }
            if overlay || engine.current?.isVideo != true {
                VStack {
                    Spacer()
                    InfoBar()
                }
                .transition(.opacity)
            }
        }
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onAppear {
            focused = true
            flash()
        }
        .onMoveCommand { direction in
            switch direction {
            case .down: engine.next()
            case .up: engine.previous()
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
        .onTapGesture { flash() }
        .onExitCommand { dismiss() }
        // Another device took over the queue (iPhone "play here"): back to the menu.
        .onChange(of: engine.current == nil) { _, released in
            if released { dismiss() }
        }
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
                if connection.state?.shuffle == true {
                    Label("Shuffle", systemImage: "shuffle")
                }
                Text("▼ next   ▲ previous   ◀ −\(Int(PlayerSettings.seekBack)) s   ▶ +\(Int(PlayerSettings.seekForward)) s")
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
