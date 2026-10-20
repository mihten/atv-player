import SwiftUI
import MobileVLCKit

/// iPhone Settings. Same keys as the Apple TV app (PlayerSettings / SoundSettings), stored on the phone.
struct PhoneSettingsView: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var locator: ServerLocator
    @EnvironmentObject var connection: ServerConnection
    @State private var serverAddress = ServerLocator.manualAddress
    @State private var addressInvalid = false

    @AppStorage(PlayerSettings.keepMusicKey) private var keepMusic = true
    @AppStorage(PlayerSettings.keepVideoAudioKey) private var keepVideoAudio = PlayerSettings.keepVideoAudioDefault
    @AppStorage(PlayerSettings.fastSeekKey) private var fastSeek = true
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @AppStorage(PlayerSettings.subSizeKey) private var size = 0
    @AppStorage(PlayerSettings.subFontKey) private var font = ""
    @AppStorage(PlayerSettings.subColorKey) private var color = PlayerSettings.defaultColor
    @AppStorage(PlayerSettings.subBoldKey) private var bold = false
    @AppStorage(PlayerSettings.subOutlineKey) private var outline = PlayerSettings.defaultOutline
    @AppStorage(PlayerSettings.subBackgroundKey) private var background = 0
    @AppStorage(PlayerSettings.subMarginKey) private var margin = 0
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(SoundSettings.widenKey) private var widen = 0
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue
    @AppStorage(AppTheme.motionKey) private var motion = true

    private var appliedKey: String {
        "\(subsOn)|\(subLang)|\(size)|\(font)|\(color)|\(bold)|\(outline)|\(background)|\(margin)|\(eqPreset)|\(widen)"
    }

    private var appVersion: String {
        let info = Bundle.main.infoDictionary
        return "Version \(info?["CFBundleShortVersionString"] as? String ?? "?") (build \(info?["CFBundleVersion"] as? String ?? "?"))"
    }

    var body: some View {
        Form {
            Section {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 10) {
                        ForEach(AppTheme.allCases) { look in
                            ThemeCard(theme: look, selected: themeRaw == look.rawValue)
                                .onTapGesture { themeRaw = look.rawValue }
                        }
                    }
                    .padding(.vertical, 8)
                    .padding(.horizontal, 2)
                }
                .sensoryFeedback(.selection, trigger: themeRaw)
                Toggle("Moving background and spinning record", isOn: $motion)
            } header: {
                Text("Appearance")
            } footer: {
                Text((AppTheme(rawValue: themeRaw) ?? .aurora).tagline + ".")
            }
            .themedRow()

            Section {
                TextField("Address (empty = automatic)", text: $serverAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .submitLabel(.done)
                    .onSubmit { addressInvalid = !locator.setManual(serverAddress) }
                LabeledContent("Using", value: Config.remoteAddress)
                Text(addressInvalid ? "That is not an address." : locator.status)
                    .font(.footnote)
                    .foregroundStyle(addressInvalid ? .red : .secondary)
            } header: {
                Text("Server")
            } footer: {
                Text("Leave empty to find the server automatically on the Wi-Fi (Bonjour). Type an address (e.g. 10.0.0.5 or 10.0.0.5:8080) only if it is not found.")
            }
            .themedRow()

            Section("Sound") {
                Picker("Equalizer", selection: $eqPreset) {
                    Text("Off").tag("off")
                    ForEach(SoundSettings.appPresets, id: \.id) { Text($0.name).tag("a:\($0.id)") }
                    ForEach(SoundSettings.presets, id: \.index) { Text($0.name).tag("p:\($0.index)") }
                    Text("Custom").tag("custom")
                }
                NavigationLink("Edit custom equalizer") { PhoneEqualizerView() }
                Picker("Stereo widener", selection: $widen) {
                    Text("Off").tag(0)
                    Text("Light").tag(1)
                    Text("Normal").tag(2)
                    Text("Strong").tag(3)
                }
            }
            .themedRow()

            Section {
                Toggle("Music keeps playing in the background", isOn: $keepMusic)
                Toggle("Music videos keep playing as audio", isOn: $keepVideoAudio)
                Toggle("Fast seek", isOn: $fastSeek)
            } header: {
                Text("Playback")
            } footer: {
                Text("Background = Home button or locked screen. With the second switch on, a music video continues as audio only and its picture is back when you return; off = it pauses. Fast seek jumps to the nearest keyframe so video does not stutter after a jump.")
            }
            .themedRow()

            Section("Lyrics / subtitles") {
                Toggle("Show lyrics / subtitles", isOn: $subsOn)
                Picker("Language", selection: $subLang) {
                    Text("Default (as set in the file)").tag("")
                    Text("English").tag("eng")
                    Text("Croatian").tag("hrv")
                }
                Picker("Text size", selection: $size) {
                    Text("Default").tag(0)
                    Text("Small").tag(20)
                    Text("Medium").tag(16)
                    Text("Large").tag(12)
                    Text("Extra large").tag(9)
                }
                Picker("Font", selection: $font) {
                    Text("Default").tag("")
                    ForEach(["Helvetica Neue", "Avenir Next", "Futura", "Georgia", "Menlo"], id: \.self) { Text($0).tag($0) }
                }
                Picker("Text colour", selection: $color) {
                    Text("White").tag(0xFFFFFF)
                    Text("Yellow").tag(0xFFFF00)
                    Text("Light grey").tag(0xC8C8C8)
                    Text("Cyan").tag(0x00FFFF)
                }
                Toggle("Bold text", isOn: $bold)
                Picker("Text outline", selection: $outline) {
                    Text("None").tag(0)
                    Text("Thin").tag(2)
                    Text("Normal").tag(4)
                    Text("Thick").tag(6)
                }
                Picker("Background box", selection: $background) {
                    Text("None").tag(0)
                    Text("Light").tag(96)
                    Text("Dark").tag(176)
                    Text("Solid").tag(255)
                }
                Picker("Position on screen", selection: $margin) {
                    Text("Bottom").tag(0)
                    Text("Slightly higher").tag(40)
                    Text("Higher").tag(100)
                }
            }
            .themedRow()

            Section {
                Text(appVersion).foregroundStyle(.secondary)
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Settings")
        .withMiniPlayer()
        .onChange(of: appliedKey) {
            engine.applySettings()
        }
    }
}

/// Custom 10-band equalizer: one slider per band (±12 dB, 1 dB steps), "Load preset" fills them from a
/// built-in curve. Saved and applied when leaving the screen (as on the Apple TV).
struct PhoneEqualizerView: View {
    @EnvironmentObject var engine: PlayerEngine
    @State private var values: [Float] = []  // [preamp] + bands
    @State private var changed = false
    @State private var loadedFrom: String?

    private var labels: [String] {
        ["Preamp"] + SoundSettings.frequencies.map { f in
            f >= 1000 ? String(format: f.truncatingRemainder(dividingBy: 1000) == 0 ? "%.0f kHz" : "%.1f kHz", f / 1000)
                : String(format: "%.0f Hz", f)
        }
    }

    var body: some View {
        Form {
            Section {
                ForEach(values.indices, id: \.self) { i in
                    HStack {
                        Text(i < labels.count ? labels[i] : "").frame(width: 72, alignment: .leading)
                        Slider(value: Binding(get: { values[i] }, set: { values[i] = $0.rounded(); changed = true }),
                               in: -12...(i == 0 ? 8 : 12), step: 1)
                        Text(values[i] > 0 ? "+\(Int(values[i]))" : "\(Int(values[i]))")
                            .font(.body.monospacedDigit())
                            .frame(width: 36, alignment: .trailing)
                    }
                }
            } footer: {
                Text((loadedFrom.map { "Started from \($0) (rounded to 1 dB). " } ?? "")
                     + "Preamp 0 dB = same loudness as the equalizer off; its maximum is +8 dB. Saved and applied when you go back.")
            }
            .themedRow()
            Section {
                Button("Reset to flat") {
                    values = Array(repeating: 0, count: values.count)
                    changed = true
                    loadedFrom = nil
                }
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Custom equalizer")
        .toolbar {
            Menu("Load preset") {
                ForEach(SoundSettings.appPresets, id: \.id) { p in
                    Button(p.name) { load("a:\(p.id)") }
                }
                ForEach(SoundSettings.presets, id: \.index) { p in
                    Button(p.name) { load("p:\(p.index)") }
                }
            }
        }
        .onAppear {
            guard values.isEmpty else { return }
            var pre: Float, bands: [Float]
            if UserDefaults.standard.string(forKey: SoundSettings.customKey) == nil,
               let fromPreset = SoundSettings.presetValues(SoundSettings.preset) {
                (pre, bands) = fromPreset
            } else {
                (pre, bands) = SoundSettings.customValues()
            }
            values = [pre.rounded()] + bands.map { $0.rounded() }
        }
        .onDisappear {
            guard changed, !values.isEmpty else { return }
            SoundSettings.saveCustom(preamp: values[0], bands: Array(values.dropFirst()))
            UserDefaults.standard.set("custom", forKey: SoundSettings.presetKey)
            changed = false
            engine.applySettings()
        }
    }

    private func load(_ tag: String) {
        guard let (pre, bands) = SoundSettings.presetValues(tag) else { return }
        values = [pre.rounded()] + bands.map { $0.rounded() }
        changed = true
        loadedFrom = SoundSettings.presetName(tag)
    }
}
