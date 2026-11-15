import SwiftUI
import MobileVLCKit

/// iPhone Settings: one row per page (Theme, Server, Sound, Playback, Lyrics, About). Same keys as the Apple TV
/// app (PlayerSettings / SoundSettings), stored on the phone.
struct PhoneSettingsView: View {
    @EnvironmentObject var locator: ServerLocator
    @EnvironmentObject var connection: ServerConnection
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue
    @AppStorage(AtvAudio.outputKey) private var soundOutput = AtvAudio.tvOutput
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(SoundSettings.loudnessKey) private var loudness = SoundSettings.Loudness.raise.rawValue
    @AppStorage(SoundSettings.boostKey) private var boost = 0
    @AppStorage(PlayerSettings.keepMusicKey) private var keepMusic = true
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @ObservedObject private var offline = OfflineStore.shared

    var body: some View {
        Form {
            Section {
                row("Theme", "paintpalette", (AppTheme(rawValue: themeRaw) ?? .aurora).name) { PhoneThemeSettingsView() }
                row("Server", "server.rack", serverSummary) { PhoneServerSettingsView() }
                if !locator.localMode, let mixes = connection.state?.mixes {
                    row("Mixes", "shuffle", mixes.compactMap(\.name).joined(separator: ", ")) { PhoneMixesView().withMiniPlayer() }
                }
                row("Downloads", "arrow.down.circle", downloadsSummary) { PhoneDownloadsSettingsView() }
                row("Sound", "speaker.wave.2", soundSummary) { PhoneSoundSettingsView() }
                row("Playback", "play.circle", keepMusic ? "Background on" : "Background off") { PhonePlaybackSettingsView() }
                row("Lyrics", "captions.bubble", lyricsSummary) { PhoneLyricsSettingsView() }
            }
            .themedRow()

            Section {
                row("About", "info.circle", AppVersion.short) { PhoneAboutView() }
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Settings")
        .withMiniPlayer()
    }

    private func row<Destination: View>(_ title: String, _ symbol: String, _ value: String,
                                        @ViewBuilder destination: @escaping () -> Destination) -> some View {
        NavigationLink {
            destination()
        } label: {
            LabeledContent {
                Text(value).lineLimit(1)
            } label: {
                Label(title, systemImage: symbol)
            }
        }
    }

    private var downloadsSummary: String {
        let count = offline.lists.count
        return count == 0 ? "None" : count == 1 ? "1 list" : "\(count) lists"
    }

    private var serverSummary: String {
        if locator.offlineMode { return "Offline" }
        if locator.localMode { return "No server" }
        if locator.isRemote { return "Internet" }
        return locator.isManual ? Config.remoteAddress : "Automatic"
    }

    private var soundSummary: String {
        _ = eqPreset; _ = loudness; _ = boost; _ = soundOutput  // refresh when any changes
        return (AtvAudio.selected && !locator.localMode ? "ATV-Server · " : "") + SoundSettings.summary
    }

    private var lyricsSummary: String {
        guard subsOn else { return "Off" }
        switch subLang {
        case "eng": return "English"
        case "hrv": return "Croatian"
        default: return "On"
        }
    }
}

/// Settings → Theme.
struct PhoneThemeSettingsView: View {
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue
    @AppStorage(AppTheme.motionKey) private var motion = true

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
                // Aurora moves its background, Vinyl spins the record: nothing to switch on the other looks
                Toggle("Theme effects", isOn: $motion)
                    .disabled(!(AppTheme(rawValue: themeRaw) ?? .aurora).hasEffects)
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Theme")
        .withMiniPlayer()
    }
}

/// Settings → Server: library source (media server or no server), home server address and the internet address
/// for away from home; in no-server mode the SMB share and the iPhone's own folder.
struct PhoneServerSettingsView: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var locator: ServerLocator
    @EnvironmentObject var connection: ServerConnection
    @ObservedObject private var localLibrary = LocalLibrary.shared
    @State private var smbAddress = LocalSettings.smbAddress
    @State private var smbUser = LocalSettings.smbUser
    @State private var smbPassword = LocalSettings.smbPassword
    /// The stored password (Keychain), so the page does not read the Keychain on every redraw.
    @State private var savedSMBPassword = LocalSettings.smbPassword
    @State private var smbDomain = LocalSettings.smbDomain
    @State private var smbInvalid = false
    @State private var smbTest = ""
    @State private var smbTesting = false
    @State private var serverAddress = ServerLocator.manualAddress
    @State private var addressInvalid = false
    @AppStorage(RemoteSettings.urlKey) private var remoteURL = ""
    @AppStorage(RemoteSettings.userKey) private var remoteUser = ""
    @AppStorage(RemoteSettings.modeKey) private var remoteMode = "audio"
    @State private var remotePassword = RemoteSettings.password
    @State private var remoteTest = ""
    @State private var testing = false

    /// Saves the password (Keychain) and applies address/login; reconnects when the internet address is in use.
    private func applyRemote() {
        RemoteSettings.setPassword(remotePassword)
        locator.remoteSettingsChanged()
        if locator.isRemote { connection.restart() }
    }

    private var libraryMode: Binding<LibraryMode> {
        Binding(get: { locator.libraryMode }, set: { locator.setLibraryMode($0) })
    }

    private var smbChanged: Bool {
        smbAddress.trimmingCharacters(in: .whitespacesAndNewlines) != LocalSettings.smbAddress
            || smbUser.trimmingCharacters(in: .whitespaces) != LocalSettings.smbUser
            || smbDomain.trimmingCharacters(in: .whitespaces) != LocalSettings.smbDomain
            || smbPassword != savedSMBPassword
    }

    /// Stores the share and login; a changed share starts a fresh index. Returns false for an invalid address.
    @discardableResult
    private func saveSMB() -> Bool {
        let address = smbAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        if !address.isEmpty && LocalSettings.normalizeSMB(address) == nil {
            smbInvalid = true
            return false
        }
        smbInvalid = false
        guard smbChanged else { return true }
        let newSource = LocalSettings.normalizeSMB(address) != LocalSettings.smbURL
        let defaults = UserDefaults.standard
        defaults.set(address, forKey: LocalSettings.smbKey)
        defaults.set(smbUser.trimmingCharacters(in: .whitespaces), forKey: LocalSettings.smbUserKey)
        defaults.set(smbDomain.trimmingCharacters(in: .whitespaces), forKey: LocalSettings.smbDomainKey)
        LocalSettings.setSMBPassword(smbPassword)
        savedSMBPassword = smbPassword
        smbTest = ""
        // Only a new share needs a fresh index; a new login keeps ids, tags, covers and the queue.
        if newSource { LocalLibrary.shared.reset() } else { LocalLibrary.shared.scan() }
        return true
    }

    var body: some View {
        Form {
            Section {
                Picker("Library", selection: libraryMode) {
                    Text("Media server").tag(LibraryMode.server)
                    Text("No server").tag(LibraryMode.local)
                    Text("Offline (downloads)").tag(LibraryMode.offline)
                }
            }
            .themedRow()

            if locator.offlineMode {
                Section {
                    NavigationLink { PhoneDownloadsSettingsView() } label: {
                        Label("Downloads", systemImage: "arrow.down.circle")
                    }
                }
                .themedRow()
            } else if locator.localMode {
                localSections
            } else {
                serverSections
            }
        }
        .phoneThemed()
        .navigationTitle("Server")
        .withMiniPlayer()
        .onDisappear {
            applyRemote()
            if locator.libraryMode == .local { saveSMB() }
        }
        .onChange(of: remoteMode) { engine.streamChanged() }
    }

    @ViewBuilder private var localSections: some View {
        Section {
            TextField("smb://nas/music or nas/music", text: $smbAddress)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .submitLabel(.next)
            TextField("User name", text: $smbUser)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.next)
            SecureField("Password", text: $smbPassword)
                .submitLabel(.done)
                .onSubmit { saveSMB() }
            TextField("Domain (optional)", text: $smbDomain)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.done)
                .onSubmit { saveSMB() }
            Button("Save") { saveSMB() }
                .disabled(!smbChanged)
            Button {
                guard saveSMB() else { return }
                smbTesting = true
                Task {
                    smbTest = await SMBBrowser.test()
                    smbTesting = false
                }
            } label: {
                HStack {
                    Text(smbTesting ? "Testing…" : "Test")
                    if smbTesting { ProgressView() }
                }
            }
            .disabled(smbTesting)
            if smbInvalid {
                Text("That is not an SMB address.").font(.footnote).foregroundStyle(.red)
            } else if !smbTest.isEmpty {
                Text(smbTest).font(.footnote).foregroundStyle(.secondary)
            }
        } header: {
            Text("SMB share")
        }
        .themedRow()

        Section {
            Button {
                LocalLibrary.shared.scan()
            } label: {
                HStack {
                    Text(localLibrary.scanning ? "Scanning…" : "Scan again")
                    if localLibrary.scanning { ProgressView() }
                }
            }
            .disabled(localLibrary.scanning)
            Text(localLibrary.status)
                .font(.footnote)
                .foregroundStyle(.secondary)
            // what the scan found, this iPhone and the SMB share apart, songs and videos apart
            ForEach(localLibrary.sourceCounts, id: \.self) { line in
                Text(line).font(.footnote).foregroundStyle(.secondary)
            }
        } header: {
            Text("Library")
        }
        .themedRow()
    }

    @ViewBuilder private var serverSections: some View {
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
            Text("Home network")
        }
        .themedRow()

        Section {
            TextField("Internet address (e.g. music.example.org)", text: $remoteURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .submitLabel(.next)
            TextField("User name", text: $remoteUser)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.next)
            SecureField("Password", text: $remotePassword)
                .submitLabel(.done)
                .onSubmit { applyRemote() }
            Picker("Music videos when away", selection: $remoteMode) {
                Text("Audio only").tag("audio")
                Text("Audio + video").tag("video")
            }
            Button {
                applyRemote()
                testing = true
                Task {
                    remoteTest = await RemoteSettings.test()
                    testing = false
                }
            } label: {
                HStack {
                    Text(testing ? "Testing…" : "Test connection")
                    if testing { ProgressView() }
                }
            }
            if !remoteTest.isEmpty {
                Text(remoteTest).font(.footnote).foregroundStyle(.secondary)
            }
            LabeledContent("Connected through", value: locator.connectionSummary)
        } header: {
            Text("Away from home")
        }
        .themedRow()
    }
}

/// Settings → Sound.
struct PhoneSoundSettingsView: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var locator: ServerLocator
    @AppStorage(AtvAudio.outputKey) private var soundOutput = AtvAudio.tvOutput
    @AppStorage(AtvAudio.lipSyncKey) private var lipSync = AtvAudio.defaultLipSync
    @State private var volume: Double = 0
    @State private var volumeDragging = false
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(SoundSettings.loudnessKey) private var loudness = SoundSettings.Loudness.raise.rawValue
    @AppStorage(SoundSettings.boostKey) private var boost = 0
    @AppStorage(SoundProfiles.activeNameKey) private var profileName = SoundProfiles.defaultName
    @State private var savedProfiles: [SoundProfiles.Saved] = []

    var body: some View {
        Form {
            if !locator.localMode { outputSection }
            Section {
                LabeledContent("Sound profile", value: profileName)
            }
            .themedRow()
            Section {
                Picker("Equalizer", selection: $eqPreset) {
                    Text("Off").tag("off")
                    ForEach(SoundSettings.appPresets, id: \.id) { Text($0.name).tag("a:\($0.id)") }
                    Text("Custom").tag("custom")
                }
                NavigationLink("Edit custom equalizer") { PhoneEqualizerView() }
            }
            .themedRow()
            Section {
                Stepper(value: $boost, in: SoundSettings.boostRange) {
                    LabeledContent("Boost", value: SoundSettings.dbLabel(Float(boost)))
                }
            } footer: {
                Text(SoundSettings.gainNote(loudnessGain: engine.levelGain))
            }
            .themedRow()
            Section {
                Toggle("Loudness", isOn: Binding(get: { loudness != SoundSettings.Loudness.off.rawValue },
                                                 set: { loudness = SoundSettings.loudnessValue(on: $0) }))
            } footer: {
                Text(SoundSettings.loudnessNote)
            }
            .themedRow()
            if !savedProfiles.isEmpty {
                Section {
                    ForEach(savedProfiles) { Text($0.name) }
                        .onDelete { offsets in
                            for index in offsets { SoundProfiles.forget(savedProfiles[index].key) }
                            savedProfiles = SoundProfiles.saved
                        }
                } header: {
                    Text("Other saved profiles")
                } footer: {
                    Text("Swipe to delete")
                }
                .themedRow()
            }
        }
        .phoneThemed()
        .navigationTitle("Sound")
        .withMiniPlayer()
        .onAppear { savedProfiles = SoundProfiles.saved }
        .onChange(of: profileName) { savedProfiles = SoundProfiles.saved }
        .onChange(of: "\(eqPreset)|\(loudness)|\(boost)") {
            engine.applySettings()
        }
        .onChange(of: soundOutput) { engine.soundOutputChanged() }
        .onChange(of: lipSync) { engine.lipSyncChanged() }
        .onAppear { volume = Double(connection.state?.sink?.output?.volume ?? 0) }
        .onChange(of: connection.state?.sink?.output?.volume) { _, new in
            if !volumeDragging, let new { volume = Double(new) }  // the headphones' own buttons, the web page
        }
    }

    private var sink: SinkState? { connection.state?.sink }

    /// iPhone or ATV-Server (0.19.1): the server's atv-audio program plays the sound on its headphones, the iPhone shows
    /// the video muted and stays in sync. The headphones go to whoever pressed Play last (Apple TV, iPhone, TV Player).
    /// ATV-Server can be chosen while it is connected; a stored choice stays (and is shown) without it.
    @ViewBuilder private var outputSection: some View {
        let chosen = soundOutput == AtvAudio.sinkOutput
        Section {
            Picker("Sound output", selection: $soundOutput) {
                Text("iPhone").tag(AtvAudio.tvOutput)
                if sink?.isConnected == true || chosen {
                    Text(sink?.isConnected == true ? "ATV-Server" : "ATV-Server (not connected)").tag(AtvAudio.sinkOutput)
                }
            }
            .disabled(locator.isRemote)
            Text(locator.isRemote ? "Away from home the iPhone plays the sound" : AtvAudio.statusText(sink))
                .font(.footnote)
                .foregroundStyle(.secondary)
            if chosen, !locator.isRemote {
                Picker("Lip sync", selection: Binding(get: { AtvAudio.clampLipSync(lipSync) }, set: { lipSync = $0 })) {
                    ForEach(AtvAudio.lipSyncValues, id: \.self) { Text(AtvAudio.lipSyncLabel($0)).tag($0) }
                }
                if sink?.isUsable == true, sink?.output?.volume != nil {
                    HStack(spacing: 12) {
                        Image(systemName: "headphones")
                        Slider(value: $volume, in: 0...100, step: 1, onEditingChanged: { editing in
                            volumeDragging = editing
                            if !editing { engine.setSinkVolume(Int(volume), muted: false) }
                        })
                        Text("\(Int(volume)) %").monospacedDigit().frame(width: 52, alignment: .trailing)
                    }
                    .accessibilityLabel("Headphones volume")
                }
            }
        }
        .themedRow()
    }
}

/// Settings → Playback.
struct PhonePlaybackSettingsView: View {
    @AppStorage(PlayerSettings.keepMusicKey) private var keepMusic = true
    @AppStorage(PlayerSettings.keepVideoAudioKey) private var keepVideoAudio = PlayerSettings.keepVideoAudioDefault
    @AppStorage(PlayerSettings.fastSeekKey) private var fastSeek = true

    var body: some View {
        Form {
            Section {
                Toggle("Music keeps playing in the background", isOn: $keepMusic)
                Toggle("Music videos keep playing as audio", isOn: $keepVideoAudio)
                Toggle("Fast seek", isOn: $fastSeek)
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("Playback")
        .withMiniPlayer()
    }
}

/// Settings → Lyrics (subtitles in music videos).
struct PhoneLyricsSettingsView: View {
    @EnvironmentObject var engine: PlayerEngine
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @AppStorage(PlayerSettings.subSizeKey) private var size = 0
    @AppStorage(PlayerSettings.subFontKey) private var font = ""
    @AppStorage(PlayerSettings.subColorKey) private var color = PlayerSettings.defaultColor
    @AppStorage(PlayerSettings.subBoldKey) private var bold = false
    @AppStorage(PlayerSettings.subOutlineKey) private var outline = PlayerSettings.defaultOutline
    @AppStorage(PlayerSettings.subBackgroundKey) private var background = 0
    @AppStorage(PlayerSettings.subMarginKey) private var margin = 0

    private var appliedKey: String {
        "\(subsOn)|\(subLang)|\(size)|\(font)|\(color)|\(bold)|\(outline)|\(background)|\(margin)"
    }

    var body: some View {
        Form {
            Section {
                Toggle("Show lyrics / subtitles", isOn: $subsOn)
                Picker("Language", selection: $subLang) {
                    Text("Default (as set in the file)").tag("")
                    Text("English").tag("eng")
                    Text("Croatian").tag("hrv")
                }
            }
            .themedRow()
            Section("Style") {
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
        }
        .phoneThemed()
        .navigationTitle("Lyrics")
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
                if let loadedFrom { Text("Started from \(loadedFrom)") }
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

enum AppVersion {
    static var short: String { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?" }
    static var build: String { Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?" }
}

/// Settings → About: what the app is, where its server is. The explanations that used to sit under the home screen
/// and the settings live here, once.
struct PhoneAboutView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var locator: ServerLocator
    @ObservedObject private var localLibrary = LocalLibrary.shared

    var body: some View {
        Form {
            Section("ATV Player") {
                LabeledContent("Version", value: "\(AppVersion.short) (build \(AppVersion.build))")
                LabeledContent("Plays", value: locator.offlineMode ? "Your downloads (audio only)"
                               : locator.localMode ? "Songs and music videos from this iPhone and an SMB share"
                               : "Songs and music videos from your own server")
            }
            .themedRow()
            if locator.offlineMode {
                Section("Server") {
                    LabeledContent("Status", value: "Offline · \(PlayLabels.count(OfflineStore.shared.items.count)) downloaded")
                }
                .themedRow()
            } else if locator.localMode {
                Section("Server") {
                    LabeledContent("Status", value: "No server · " + localLibrary.status)
                    LabeledContent("SMB share", value: LocalSettings.smbName ?? "Not set up")
                    LabeledContent("Not available", value: "Playlists, lyrics lookup, loudness levelling, web remote")
                }
                .themedRow()
            } else {
                Section("Server") {
                    LabeledContent("Address", value: Config.remoteAddress)
                    LabeledContent("Status", value: connection.connected
                                   ? "Connected" + (connection.state?.version.map { " · server \($0)" } ?? "") : locator.status)
                    LabeledContent("Connected through", value: locator.connectionSummary)
                    LabeledContent("Web remote", value: Config.remoteAddress)
                }
                .themedRow()
            }
            Section("Good to know") {
                Text("This iPhone has its own queue; the Apple TV's is separate.")
                if locator.offlineMode {
                    Text("Loudness: downloads keep the server's measurement.")
                } else if locator.localMode {
                    Text("Loudness: files play unchanged without the server (no levelling).")
                } else {
                    Text("Loudness: the server measures each file once; new files play unchanged until then.")
                }
            }
            .font(.footnote)
            .themedRow()
            Section("Help") {
                ForEach(HelpText.iPhone) { topic in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(topic.title).font(.subheadline.weight(.semibold))
                        Text(topic.text).font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            .themedRow()
        }
        .phoneThemed()
        .navigationTitle("About")
        .withMiniPlayer()
    }
}
