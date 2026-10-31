import SwiftUI

/// Apple TV: every start of playback (folder, playlist, collection, a single item in a list) begins shuffled with
/// repeat all; both can be changed in the player's quick panel (hold the remote's centre button).
extension ServerConnection {
    func startPlayback(_ arguments: [String: Any]) {
        var args = arguments
        args["shuffle"] = true
        command("play_collection", args)
        command("repeat", ["mode": "all"])
    }
}

/// Quick panel over the player: shuffle, repeat, the lyrics / subtitle track, equalizer and fast seek. Opens on a
/// click-and-hold of the centre button; ▲▼ moves, click changes the focused row, Back closes it. ⏯ keeps
/// pausing / resuming.
struct QuickPanel: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(PlayerSettings.fastSeekKey) private var fastSeek = true
    @FocusState private var focus: Row?
    let close: () -> Void

    enum Row: String { case shuffle, repeatMode, subtitles, equalizer, fastSeek }

    /// Equalizer choices in click order (same values as Settings → Sound). Any other preset (a VLC one chosen in
    /// Settings) is shown by name and the next click goes to the first of these.
    private static let equalizerChoices = ["a:momentum4", "custom", "a:amplify", "off"]

    /// Lyrics / subtitle choices in click order: (shown, subsOn, language).
    private static let subtitleChoices: [(String, Bool, String)] = [
        ("File default", true, ""), ("English", true, "eng"), ("Croatian", true, "hrv"), ("Off", false, ""),
    ]

    private var shuffleOn: Bool { connection.state?.shuffle == true }
    private var repeatMode: String { connection.state?.repeatMode ?? "all" }
    private var subtitleIndex: Int {
        guard subsOn else { return 3 }
        return Self.subtitleChoices.firstIndex { $0.1 && $0.2 == subLang } ?? 0
    }

    var body: some View {
        let accent = theme.accent(artwork.palette)
        VStack(alignment: .leading, spacing: 18) {
            Text("PLAYBACK").font(theme.eyebrow(24)).tracking(5).foregroundStyle(accent)
            row(.shuffle, "Shuffle", "shuffle", shuffleOn ? "On" : "Off") {
                connection.command("shuffle", ["on": !shuffleOn])
            }
            row(.repeatMode, "Repeat", repeatMode == "one" ? "repeat.1" : "repeat",
                ["all": "All", "one": "One", "off": "Off"][repeatMode] ?? "All") {
                let next = ["all": "one", "one": "off", "off": "all"][repeatMode] ?? "all"
                connection.command("repeat", ["mode": next])
            }
            row(.subtitles, "Lyrics / subtitles", "captions.bubble", Self.subtitleChoices[subtitleIndex].0) {
                let next = Self.subtitleChoices[(subtitleIndex + 1) % Self.subtitleChoices.count]
                subsOn = next.1
                subLang = next.2
                engine.applySettings()  // switches the track without restarting the video
            }
            row(.equalizer, "Equalizer", "slider.vertical.3", SoundSettings.presetName(eqPreset)) {
                let choices = Self.equalizerChoices
                let next = choices.firstIndex(of: eqPreset).map { choices[($0 + 1) % choices.count] } ?? choices[0]
                // "custom" without a saved curve plays flat at unity gain (SoundSettings.customValues), as in Settings.
                eqPreset = next
                connection.send(["type": "log", "message": "quick panel: equalizer -> \(next)"])
                engine.applySettings()  // rebuilds the VLC player and resumes at the same position
            }
            row(.fastSeek, "Fast seek", "forward", fastSeek ? "On" : "Off") {
                fastSeek.toggle()
                connection.send(["type": "log", "message": "quick panel: fast seek -> \(fastSeek ? "on" : "off")"])
                // PlayerEngine applies it at the next seek (VLC reads it only when an item starts).
            }
            Text("Click changes  ·  Back closes  ·  ⏯ pauses").font(theme.font(20)).opacity(0.5).padding(.top, 6)
        }
        .padding(44)
        .frame(width: 760)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .focusSection()
        .onAppear { focus = .shuffle }
        .onExitCommand { close() }
        .onPlayPauseCommand { engine.togglePlay(source: "quick-panel") }
    }

    private func row(_ id: Row, _ title: String, _ symbol: String, _ value: String,
                     action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 22) {
                Image(systemName: symbol).frame(width: 44)
                Text(title)
                Spacer()
                Text(value).bold()
            }
            .font(theme.font(30, .medium))
            .padding(.horizontal, 28)
            .padding(.vertical, 18)
            .frame(maxWidth: .infinity)
        }
        .focused($focus, equals: id)
        .accessibilityLabel(title)
        .accessibilityValue(value)
        .accessibilityIdentifier("quickPanel.\(id.rawValue)")  // UI tests
    }
}

/// Small shuffle / repeat state icons for the player's info lines (lit = on).
struct PlayModeIcons: View {
    @EnvironmentObject var connection: ServerConnection
    var accent: Color = .accentColor
    var size: CGFloat = 24

    var body: some View {
        let repeatMode = connection.state?.repeatMode ?? "all"
        HStack(spacing: 14) {
            Image(systemName: "shuffle")
                .foregroundStyle(connection.state?.shuffle == true ? accent : Color.secondary.opacity(0.5))
            Image(systemName: repeatMode == "one" ? "repeat.1" : "repeat")
                .foregroundStyle(repeatMode == "off" ? Color.secondary.opacity(0.5) : accent)
        }
        .font(.system(size: size, weight: .semibold))
    }
}
