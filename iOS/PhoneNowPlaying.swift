import SwiftUI

/// Playback as seen from this iPhone: local (this iPhone is the output → the engine) or remote
/// (another device plays → the server state, commands go over the connection).
@MainActor
struct Transport {
    let connection: ServerConnection
    let engine: PlayerEngine

    var isLocal: Bool { connection.isThisDeviceOutput && engine.current != nil }
    var item: MediaItem? { isLocal ? engine.current : connection.state?.current }
    var isPlaying: Bool { isLocal ? engine.isPlaying : connection.state?.playing == true }

    var position: Double { isLocal ? engine.livePosition : connection.estimatedPosition() }

    var duration: Double {
        if isLocal { return engine.liveDuration }
        return connection.state?.duration ?? item?.duration ?? 0
    }

    func toggle() {
        if isLocal { engine.togglePlay(source: "phone") } else { connection.command("toggle") }
    }

    func next() { connection.command("next") }

    func previous() { connection.command("prev", ["position": position]) }

    func seek(to seconds: Double) {
        if isLocal { engine.seek(to: seconds) } else { connection.command("seek", ["position": seconds]) }
    }
}

struct Artwork: View {
    let item: MediaItem
    var size: CGFloat

    var body: some View {
        AsyncImage(url: item.art == true ? item.artURL : nil) { image in
            image.resizable().scaledToFill()
        } placeholder: {
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

/// Bar above the tab bar: artwork, title, where it plays, play/pause, next. Tap = full player.
struct MiniPlayer: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.showPlayer) private var showPlayer

    var body: some View {
        let transport = Transport(connection: connection, engine: engine)
        if let item = transport.item {
            HStack(spacing: 12) {
                Artwork(item: item, size: 44)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                    Text(subtitle(item)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
                Button { transport.toggle() } label: {
                    Image(systemName: transport.isPlaying ? "pause.fill" : "play.fill").font(.title2)
                }
                .frame(width: 44, height: 44)
                Button { transport.next() } label: {
                    Image(systemName: "forward.fill").font(.title3)
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

    private func subtitle(_ item: MediaItem) -> String {
        let on = connection.otherOutput.map { "on \($0.name)" }
        return [item.artist, on].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// Picks the device that plays the queue (server 0.9.0 "output"). Playback moves at the same position.
struct OutputMenu: View {
    @EnvironmentObject var connection: ServerConnection

    var body: some View {
        let outputs = connection.outputs
        let active = connection.activeOutput
        Menu {
            ForEach(outputs) { output in
                Button {
                    connection.setOutput(output.device)
                } label: {
                    if output.device == active {
                        Label(output.label, systemImage: "checkmark")
                    } else {
                        Label(output.label, systemImage: output.symbol)
                    }
                }
            }
        } label: {
            let current = outputs.first { $0.device == active }
            Label(current.map { "Playing on \($0.label)" } ?? "Not playing on any device",
                  systemImage: current?.symbol ?? "hifispeaker")
                .font(.subheadline.weight(.medium))
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(.thinMaterial, in: Capsule())
        }
        .disabled(outputs.count < 2)
    }
}

/// Full player (sheet): video or artwork, title, scrubber, transport, shuffle/repeat, output picker.
struct PhoneNowPlayingView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @State private var fullScreen = false
    @State private var scrubbing = false
    @State private var scrubValue: Double = 0

    var body: some View {
        let transport = Transport(connection: connection, engine: engine)
        VStack(spacing: 22) {
            OutputMenu().padding(.top, 24)
            Spacer(minLength: 0)
            if let item = transport.item {
                if item.isVideo && transport.isLocal {
                    VideoSurface(engine: engine)
                        .id(fullScreen)  // re-attach the video view after the full-screen cover closes
                        .aspectRatio(16 / 9, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .overlay(alignment: .bottomTrailing) {
                            Button { fullScreen = true } label: {
                                Image(systemName: "arrow.up.left.and.arrow.down.right")
                                    .padding(10).background(.ultraThinMaterial, in: Circle())
                            }
                            .padding(8)
                        }
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
                controls(transport)
            } else {
                Text("Nothing playing").foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24)
        .presentationDragIndicator(.visible)
        .fullScreenCover(isPresented: $fullScreen) {
            FullScreenVideo(transport: transport)
        }
    }

    private func scrubber(_ transport: Transport) -> some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { _ in
            let duration = max(transport.duration, 1)
            let position = scrubbing ? scrubValue : min(transport.position, duration)
            VStack(spacing: 4) {
                Slider(value: Binding(get: { position }, set: { scrubValue = $0 }), in: 0...duration,
                       onEditingChanged: { editing in
                    if editing {
                        scrubValue = position
                        scrubbing = true
                    } else {
                        transport.seek(to: scrubValue)
                        scrubbing = false
                    }
                })
                HStack {
                    Text(formatTime(position))
                    Spacer()
                    Text("-" + formatTime(max(0, duration - position)))
                }
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            }
        }
    }

    private func controls(_ transport: Transport) -> some View {
        let state = connection.state
        let repeatMode = state?.repeatMode ?? "all"
        return HStack {
            Button { connection.command("shuffle") } label: {
                Image(systemName: "shuffle").foregroundStyle(state?.shuffle == true ? Color.accentColor : .secondary)
            }
            Spacer()
            Button { transport.previous() } label: { Image(systemName: "backward.fill").font(.title) }
            Spacer()
            Button { transport.toggle() } label: {
                Image(systemName: transport.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 64))
            }
            Spacer()
            Button { transport.next() } label: { Image(systemName: "forward.fill").font(.title) }
            Spacer()
            Button { connection.command("repeat") } label: {
                Image(systemName: repeatMode == "one" ? "repeat.1" : "repeat")
                    .foregroundStyle(repeatMode == "off" ? .secondary : Color.accentColor)
            }
        }
        .buttonStyle(.plain)
        .font(.title3)
    }
}

/// Landscape-friendly full-screen video; tap shows/hides the controls.
struct FullScreenVideo: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.dismiss) private var dismiss
    let transport: Transport
    @State private var controls = true

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VideoSurface(engine: engine).ignoresSafeArea()
            if controls {
                VStack {
                    HStack {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark").padding(12).background(.ultraThinMaterial, in: Circle())
                        }
                        Spacer()
                    }
                    Spacer()
                    HStack(spacing: 50) {
                        Button { transport.previous() } label: { Image(systemName: "backward.fill") }
                        Button { transport.toggle() } label: {
                            Image(systemName: transport.isPlaying ? "pause.fill" : "play.fill")
                        }
                        Button { transport.next() } label: { Image(systemName: "forward.fill") }
                    }
                    .font(.largeTitle)
                    .padding(.bottom, 30)
                }
                .padding()
                .foregroundStyle(.white)
                .buttonStyle(.plain)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { withAnimation { controls.toggle() } }
        .onChange(of: engine.current?.isVideo == true) { _, isVideo in
            if !isVideo { dismiss() }  // the queue moved on to a song (or another device took over)
        }
    }
}
