import SwiftUI

/// Quick menu over the player: Volume (atv-audio only), ♥ Love, shuffle, repeat, the lyrics / subtitle track,
/// equalizer, loudness, boost, fast seek, "Prefer videos" and the version of the song. Opens on a click of the centre
/// button with Volume focused (Love without atv-audio; row clicks are ignored for a moment so that click never
/// mutes or hearts); ▲▼ moves between rows, Back closes it, ⏯ keeps pausing / resuming.
/// Row kinds: toggle (click flips), ◀▶ value (◀ previous / ▶ next, stop at the ends), ◀▶ level bar (Volume, Boost),
/// inline list (click opens a short list under the row; ▲▼ picks, click confirms, Back closes only the list).
/// The rows are full-width buttons in one column, so ◀▶ never has a focusable neighbour and reaches onMoveCommand.
///
/// Height (1080p safe area = 960 pt): rows ~52 pt (30 pt text + 2 × 8) + 6 spacing. 11 rows + title + hint + padding
/// ≈ 64 + 29 + 11 × 52 + 12 × 6 + 30 ≈ 770 pt; an open list adds ≤ 166 pt (≈ 4 entries of 41 pt, it scrolls inside)
/// ≈ 940 pt at most.
struct QuickPanel: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var artwork: ArtworkStore
    @Environment(\.appTheme) private var theme
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(SoundSettings.boostKey) private var boost = 0
    @AppStorage(SoundSettings.loudnessKey) private var loudness = SoundSettings.Loudness.raise.rawValue
    @AppStorage(PlayerSettings.fastSeekKey) private var fastSeek = true
    @AppStorage(AtvAudio.outputKey) private var soundOutput = AtvAudio.tvOutput
    @FocusState private var focus: Row?
    @FocusState private var listFocus: Int?
    /// The row whose inline list is open (Equalizer / Version).
    @State private var openList: Row?
    /// Volume set here, shown until the sink's status has caught up (◀▶ can be faster than its answer).
    @State private var volumeEdit: VolumeEdit?
    /// Repeat chosen here, until the queue state echoes it (two quick ▶ go All → One → Off).
    @State private var repeatEdit: (index: Int, at: Date)?
    /// When the panel appeared: the click that opened it must not reach the first row (see `guarded`).
    @State private var openedAt = Date()
    let close: () -> Void

    enum Row: String {
        case volume, love, shuffle, repeatMode, subtitles, equalizer, loudness, boost, fastSeek, preferVideos, version
    }

    struct VolumeEdit: Equatable {
        let percent: Int
        let muted: Bool
        let at: Date
    }

    /// Repeat values in ◀▶ order.
    private static let repeatModes: [(String, String)] = [("all", "All"), ("one", "One"), ("off", "Off")]

    /// Lyrics / subtitle choices in ◀▶ order: (shown, subsOn, language).
    private static let subtitleChoices: [(String, Bool, String)] = [
        ("File default", true, ""), ("English", true, "eng"), ("Croatian", true, "hrv"), ("Off", false, ""),
    ]

    /// Inline list entries: ≈ 41 pt each, at most ~4 visible (see the height note above).
    private static let entryHeight: CGFloat = 41
    private static let listMaxHeight: CGFloat = 166

    private var shuffleOn: Bool { connection.state?.shuffle == true }
    private var preferVideosOn: Bool { connection.state?.preferVideos == true }
    /// ♥ of the playing item (kept current by the queue state, PlayerEngine.refreshLinks).
    private var lovedOn: Bool { engine.current?.loved == true }
    /// The playing item's versions (song first, then its videos); empty when it has no other version.
    private var versions: [MediaVersion] {
        guard let item = engine.current, item.hasVersions else { return [] }
        return item.versions ?? []
    }
    private var loudnessOn: Bool { loudness != SoundSettings.Loudness.off.rawValue }
    private var repeatIndex: Int {
        if let edit = repeatEdit, Date().timeIntervalSince(edit.at) < 1.5 { return edit.index }
        return Self.repeatModes.firstIndex { $0.0 == (connection.state?.repeatMode ?? "all") } ?? 0
    }
    private var subtitleIndex: Int {
        guard subsOn else { return 3 }
        return Self.subtitleChoices.firstIndex { $0.1 && $0.2 == subLang } ?? 0
    }
    /// The Volume row exists while the sound plays through atv-audio.
    private var volumeShown: Bool { soundOutput == AtvAudio.sinkOutput && !Config.localMode }
    /// (percent, muted) of the sink's output; nil while it has none.
    private var volume: (percent: Int, muted: Bool)? {
        guard let sink = connection.state?.sink, sink.isUsable, let percent = sink.output?.volume else { return nil }
        if let edit = volumeEdit, Date().timeIntervalSince(edit.at) < 1.5 { return (edit.percent, edit.muted) }
        return (percent, sink.output?.muted == true)
    }

    var body: some View {
        let accent = theme.accent(artwork.palette)
        VStack(alignment: .leading, spacing: 6) {
            Text("QUICK MENU").font(theme.eyebrow(24)).tracking(5).foregroundStyle(accent)
            playbackRows
            soundRows
            otherRows
            Text(openList == nil ? "Click changes  ·  ◀ ▶ adjust  ·  Back closes  ·  ⏯ pauses"
                                 : "▲ ▼ picks  ·  Click confirms  ·  Back closes the list")
                .font(theme.font(20)).opacity(0.5).padding(.top, 6)
        }
        .padding(.horizontal, 44)
        .padding(.vertical, 32)
        .frame(width: 760)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .focusSection()
        .onAppear {
            openedAt = Date()
            focus = volumeShown ? .volume : .love  // the first row
        }
        .onExitCommand {
            if openList != nil { closeList() } else { close() }  // Back closes only an open list
        }
        .onPlayPauseCommand { engine.togglePlay(source: "quick-panel") }
        .onChange(of: engine.current?.id) {
            if openList == .version { closeList() }  // another item: its versions differ
        }
        .onChange(of: versions.isEmpty) {
            if openList == .version, versions.isEmpty { closeList() }  // nothing left to pick: rows usable again
        }
    }

    // Rows in three parts (keeps body small for the type checker); the VStack lays them out as one list.
    @ViewBuilder private var playbackRows: some View {
        if volumeShown { volumeRow }
        row(.love, "Love", lovedOn ? "heart.fill" : "heart", lovedOn ? "On" : "Off") {
            guard let item = engine.current else { return }
            connection.command("love", ["id": item.id, "on": !lovedOn])
            log("love \(item.id) -> \(lovedOn ? "off" : "on")")
        }
        row(.shuffle, "Shuffle", "shuffle", shuffleOn ? "On" : "Off") {
            connection.command("shuffle", ["on": !shuffleOn])
        }
        let repeatAt = repeatIndex
        row(.repeatMode, "Repeat", Self.repeatModes[repeatAt].0 == "one" ? "repeat.1" : "repeat",
            Self.repeatModes[repeatAt].1, steps: (repeatAt > 0, repeatAt < Self.repeatModes.count - 1),
            step: { delta in
                let next = repeatAt + delta
                guard Self.repeatModes.indices.contains(next) else { return }
                let at = Date()
                repeatEdit = (next, at)
                connection.command("repeat", ["mode": Self.repeatModes[next].0])
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
                    if repeatEdit?.at == at { repeatEdit = nil }  // from now on the queue state
                }
            }, action: {})
        let subAt = subtitleIndex
        row(.subtitles, "Lyrics / subtitles", "captions.bubble", Self.subtitleChoices[subAt].0,
            steps: (subAt > 0, subAt < Self.subtitleChoices.count - 1),
            step: { delta in
                let next = subAt + delta
                guard Self.subtitleChoices.indices.contains(next) else { return }
                subsOn = Self.subtitleChoices[next].1
                subLang = Self.subtitleChoices[next].2
                engine.applySettings()  // switches the track without restarting the video
            }, action: {})
    }

    /// Volume: ◀▶ −/+5 % (on the 5 % grid), click = mute / unmute. The sink's output volume (the headphones' own).
    @ViewBuilder private var volumeRow: some View {
        let current = volume
        let percent = current?.percent ?? 0
        row(.volume, "Volume", current?.muted == true ? "speaker.slash.fill" : "speaker.wave.2.fill",
            current.map { "\($0.percent) %" } ?? "—",
            bar: current.map { (Double($0.percent) / 100, $0.muted) },
            steps: (current != nil && percent > 0, current != nil && percent < 100),
            step: { delta in
                guard let current else { return }
                let next = delta > 0 ? min(100, (current.percent / 5 + 1) * 5)
                                     : max(0, ((current.percent + 4) / 5 - 1) * 5)
                setVolume(next, muted: current.muted)
            }, action: {
                guard let current else { return }
                setVolume(current.percent, muted: !current.muted)
            })
    }

    @ViewBuilder private var soundRows: some View {
        row(.equalizer, "Equalizer", "slider.vertical.3", SoundSettings.presetName(eqPreset), opens: true) {
            showList(.equalizer, current: SoundSettings.presetChoices.firstIndex { $0.value == eqPreset } ?? 0)
        }
        if openList == .equalizer {
            inlineList(.equalizer, SoundSettings.presetChoices.map { ($0.label, $0.value == eqPreset) }) { index in
                let value = SoundSettings.presetChoices[index].value
                guard value != eqPreset else { return }
                // "custom" without a saved curve plays flat at unity gain (SoundSettings.customValues), as in Settings.
                eqPreset = value
                log("equalizer -> \(value)")
                engine.applySettings()  // new curve on the running player (rebuilds only to / from unprocessed)
            }
        }
        row(.loudness, "Loudness", "waveform", loudnessOn ? "On" : "Off") {
            let on = !loudnessOn
            loudness = SoundSettings.loudnessValue(on: on)
            log("loudness -> \(on ? "on" : "off")")
            engine.applySettings()  // new gain on the running player (rebuilds only to / from unprocessed)
        }
        let range = SoundSettings.boostRange
        let boostNow = min(range.upperBound, max(range.lowerBound, boost))
        row(.boost, "Boost", "speaker.wave.3", SoundSettings.dbLabel(Float(boostNow)),
            bar: (Double(boostNow - range.lowerBound) / Double(range.upperBound - range.lowerBound), false),
            steps: (boostNow > range.lowerBound, boostNow < range.upperBound),
            step: { delta in
                let next = min(range.upperBound, max(range.lowerBound, boostNow + delta))
                guard next != boostNow else { return }
                boost = next
                log("boost -> +\(next) dB")
                engine.applySettings()  // new gain on the running player
            }, action: {})
    }

    @ViewBuilder private var otherRows: some View {
        row(.fastSeek, "Fast seek", "forward", fastSeek ? "On" : "Off") {
            fastSeek.toggle()
            log("fast seek -> \(fastSeek ? "on" : "off")")
            // PlayerEngine applies it at the next seek (VLC reads it only when an item starts).
        }
        row(.preferVideos, "Prefer videos", "play.rectangle", preferVideosOn ? "On" : "Off") {
            connection.command("prefer_videos", ["on": !preferVideosOn])
            log("prefer videos -> \(preferVideosOn ? "off" : "on")")
        }
        let list = versions
        row(.version, "Version", "rectangle.stack", list.isEmpty ? "—" : (engine.current?.versionLabel ?? "—"),
            opens: !list.isEmpty) {
            guard let item = engine.current, !list.isEmpty else { return }
            showList(.version, current: list.firstIndex { $0.id == item.id } ?? 0)
        }
        if openList == .version, !list.isEmpty {
            let playing = engine.current?.id
            inlineList(.version, list.map { ("\($0.label) · \($0.isVideo ? "Video" : "Song")", $0.id == playing) }) { index in
                // Once, on confirm: every switch restarts playback.
                guard let item = engine.current, list.indices.contains(index), list[index].id != item.id else { return }
                let next = list[index]
                log("version \(item.id) -> \(next.id) (\(next.label))")
                connection.switchVersion(to: next.id, position: engine.livePosition)
            }
        }
    }

    // MARK: - Pieces

    private func log(_ text: String) {
        connection.send(["type": "log", "message": "quick panel: \(text)"])
    }

    private func setVolume(_ percent: Int, muted: Bool) {
        let edit = VolumeEdit(percent: percent, muted: muted, at: Date())
        volumeEdit = edit
        engine.setSinkVolume(percent, muted: muted)
        log("volume -> \(percent) %\(muted ? " (muted)" : "")")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
            if volumeEdit == edit { volumeEdit = nil }  // from now on the sink's status
        }
    }

    /// Runs a row's action, except within 0.5 s of opening: tvOS may deliver the select of the click that opened the
    /// panel to the newly focused first row (Volume or Love).
    private func guarded(_ id: Row, _ action: () -> Void) {
        let age = Date().timeIntervalSince(openedAt)
        guard age >= 0.5 else {
            log("select on \(id.rawValue) \(Int(age * 1000)) ms after opening ignored")
            return
        }
        action()
    }

    private func showList(_ id: Row, current: Int) {
        openList = id
        listFocus = nil
        // The rows are disabled while the list is open, so tvOS can only focus inside it; start on the current value.
        // Again after 0.35 s whatever tvOS picked meanwhile (as ChoiceList: an earlier set alone was overridden).
        DispatchQueue.main.async { listFocus = current }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { if openList == id { listFocus = current } }
    }

    private func closeList() {
        guard let id = openList else { return }
        openList = nil
        listFocus = nil
        DispatchQueue.main.async { focus = id }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { if openList == nil, focus == nil { focus = id } }
    }

    /// The list under an Equalizer / Version row: (label, current) entries, at most ~4 visible, scrolls inside. Its own
    /// focus section and the disabled rows keep ▲▼ inside it until it closes.
    private func inlineList(_ id: Row, _ entries: [(String, Bool)], pick: @escaping (Int) -> Void) -> some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(spacing: 2) {
                ForEach(entries.indices, id: \.self) { index in
                    Button {
                        closeList()
                        pick(index)
                    } label: {
                        HStack(spacing: 16) {
                            Text(entries[index].0).lineLimit(1)
                            Spacer()
                            if entries[index].1 {
                                Image(systemName: "checkmark").font(.system(size: 22, weight: .semibold))
                            }
                        }
                        .font(theme.font(26))
                        .padding(.horizontal, 24)
                        .frame(maxWidth: .infinity, minHeight: Self.entryHeight - 2, maxHeight: Self.entryHeight - 2)
                    }
                    .buttonStyle(QuickEntryStyle())
                    .focused($listFocus, equals: index)
                    .accessibilityLabel(entries[index].0)
                    .accessibilityValue(entries[index].1 ? "current" : "")
                    .accessibilityIdentifier("quickPanel.\(id.rawValue).entry")  // UI tests
                }
            }
            .padding(.vertical, 4)
            .padding(.horizontal, 12)
        }
        .frame(height: min(Self.listMaxHeight, CGFloat(entries.count) * Self.entryHeight + 8))
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.white.opacity(0.05)))
        .focusSection()
        .accessibilityIdentifier("quickPanel.\(id.rawValue).list")
    }

    /// One row. `steps` = (◀ possible, ▶ possible) for ◀▶ rows (shown as marks while focused; nil = no ◀▶),
    /// `bar` = (fraction 0…1, dimmed) for level rows, `opens` = click opens an inline list.
    private func row(_ id: Row, _ title: String, _ symbol: String, _ value: String,
                     bar: (Double, Bool)? = nil, steps: (Bool, Bool)? = nil, step: ((Int) -> Void)? = nil,
                     opens: Bool = false, action: @escaping () -> Void) -> some View {
        let focused = focus == id
        return Button(action: { guarded(id, action) }) {
            HStack(spacing: 22) {
                Image(systemName: symbol).frame(width: 44)
                Text(title)
                Spacer(minLength: 12)
                if let bar { LevelBar(fraction: bar.0, dimmed: bar.1) }
                if let steps, focused {
                    Image(systemName: "chevron.left").font(.system(size: 20, weight: .bold)).opacity(steps.0 ? 0.9 : 0.2)
                }
                Text(value).bold().monospacedDigit()
                if let steps, focused {
                    Image(systemName: "chevron.right").font(.system(size: 20, weight: .bold)).opacity(steps.1 ? 0.9 : 0.2)
                }
                if opens {
                    Image(systemName: openList == id ? "chevron.up" : "chevron.down")
                        .font(.system(size: 20, weight: .bold)).opacity(0.6)
                }
            }
            .font(theme.font(30, .medium))
            .padding(.horizontal, 28)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(QuickRowStyle(open: openList == id))
        .focused($focus, equals: id)
        .disabled(openList != nil)  // an open list keeps the focus (▲▼ stay inside it)
        .onMoveCommand { direction in
            guard let step else { return }
            switch direction {
            case .left: step(-1)
            case .right: step(1)
            default: break
            }
        }
        .accessibilityLabel(title)
        .accessibilityValue(value)
        .accessibilityIdentifier("quickPanel.\(id.rawValue)")  // UI tests
    }
}

/// Ten segments for a level (Volume, Boost); `dimmed` (muted) keeps the length so the level that comes back shows.
private struct LevelBar: View {
    let fraction: Double
    let dimmed: Bool

    var body: some View {
        let lit = Int((min(1, max(0, fraction)) * 10).rounded())
        HStack(spacing: 4) {
            ForEach(0..<10, id: \.self) { i in
                RoundedRectangle(cornerRadius: 2, style: .continuous)
                    .frame(width: 9, height: 22)
                    .opacity(i < lit ? (dimmed ? 0.35 : 1) : 0.18)
            }
        }
        .accessibilityHidden(true)
    }
}

/// Quick panel row: a platter like the system button's (white with dark text when focused) without the system style's
/// own tall insets, so the row is only as high as its text plus the label padding. `open`: its inline list is open.
private struct QuickRowStyle: ButtonStyle {
    var open = false

    func makeBody(configuration: Configuration) -> some View {
        QuickRowBody(configuration: configuration, open: open)
    }
}

private struct QuickRowBody: View {
    let configuration: ButtonStyleConfiguration
    let open: Bool
    @Environment(\.isFocused) private var focused
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        configuration.label
            .foregroundStyle(focused ? Color.black : Color.white)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(focused ? Color.white : Color.white.opacity(open ? 0.22 : 0.08)))
            .opacity(enabled || open ? 1 : 0.45)
            .scaleEffect(configuration.isPressed ? 0.98 : (focused ? 1.03 : 1))
            .shadow(color: .black.opacity(focused ? 0.35 : 0), radius: 14, y: 8)
            .animation(.easeOut(duration: 0.15), value: focused)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }
}

/// Inline list entry: like a row, smaller, no scale (the list scrolls inside a fixed height).
private struct QuickEntryStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        QuickEntryBody(configuration: configuration)
    }
}

private struct QuickEntryBody: View {
    let configuration: ButtonStyleConfiguration
    @Environment(\.isFocused) private var focused

    var body: some View {
        configuration.label
            .foregroundStyle(focused ? Color.black : Color.white)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(focused ? Color.white : Color.clear))
            .opacity(configuration.isPressed ? 0.8 : 1)
            .animation(.easeOut(duration: 0.12), value: focused)
    }
}

/// Small shuffle / repeat state icons for the player's info lines (lit = on), and a ♥ when the playing item is loved.
struct PlayModeIcons: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var engine: PlayerEngine
    var accent: Color = .accentColor
    var size: CGFloat = 24

    var body: some View {
        let repeatMode = connection.state?.repeatMode ?? "all"
        HStack(spacing: 14) {
            Image(systemName: "shuffle")
                .foregroundStyle(connection.state?.shuffle == true ? accent : Color.secondary.opacity(0.5))
            Image(systemName: repeatMode == "one" ? "repeat.1" : "repeat")
                .foregroundStyle(repeatMode == "off" ? Color.secondary.opacity(0.5) : accent)
            if engine.current?.loved == true {
                Image(systemName: "heart.fill")
                    .foregroundStyle(accent)
                    .accessibilityLabel("Loved")
            }
        }
        .font(.system(size: size, weight: .semibold))
    }
}
