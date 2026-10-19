import SwiftUI
import TVVLCKit

struct SettingsView: View {
    @EnvironmentObject var engine: PlayerEngine
    @EnvironmentObject var locator: ServerLocator
    @State private var serverAddress = ServerLocator.manualAddress
    @State private var addressInvalid = false

    @AppStorage(PlayerSettings.keepMusicKey) private var keepMusic = true
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @AppStorage(PlayerSettings.subSizeKey) private var size = 0
    @AppStorage(PlayerSettings.subFontKey) private var font = ""
    @AppStorage(PlayerSettings.subColorKey) private var color = PlayerSettings.defaultColor
    @AppStorage(PlayerSettings.subBoldKey) private var bold = false
    @AppStorage(PlayerSettings.subOutlineKey) private var outline = PlayerSettings.defaultOutline
    @AppStorage(PlayerSettings.subBackgroundKey) private var background = 0
    @AppStorage(PlayerSettings.subMarginKey) private var margin = 0
    @AppStorage(PlayerSettings.listTextSizeKey) private var listTextSize = ListTextSize.compact.rawValue
    @AppStorage(PlayerSettings.fastSeekKey) private var fastSeek = true
    @AppStorage(PlayerSettings.seekBackKey) private var seekBack = 10
    @AppStorage(PlayerSettings.seekForwardKey) private var seekForward = 30
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(SoundSettings.widenKey) private var widen = 0
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue
    @AppStorage(AppTheme.motionKey) private var motion = true

    /// Changes to any of these re-apply immediately (pickers open their own screen, so
    /// "apply on leaving Settings" would fire every time a picker opens).
    private var appliedKey: String {
        "\(subsOn)|\(subLang)|\(size)|\(font)|\(color)|\(bold)|\(outline)|\(background)|\(margin)"
    }

    /// The Now Playing card is disabled (= not focusable, proven on the TV in build 11) while the
    /// screen opens, so tvOS puts the initial focus on the first setting; then it is enabled and ◀
    /// reaches it. defaultFocus/focused on the List did NOT work (build 12).
    @State private var panelReady = false
    @Namespace private var settingsScope

    var body: some View {
        // tvOS Settings layout: left half shows the title and Now Playing (or a big symbol), rows on
        // the right half. No navigation title: tvOS centres it and the list scrolls under it.
        HStack(alignment: .top, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                Text("Settings")
                    .font(.system(size: 64, weight: .bold))
                if engine.current != nil {
                    NowPlayingSidebar(enabled: panelReady)
                } else {
                    VStack(spacing: 30) {
                        Image(systemName: "gearshape.fill")
                            .font(.system(size: 300, weight: .regular))
                            .foregroundStyle(.secondary)
                        Text("ATV Player")
                            .font(.title3)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.top, 60)
                }
            }
            // Title left-aligned with the card/symbol below it, the column centred in the left half.
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            settingsList
                .frame(width: 960)
                .padding(.trailing, 60)
                .focusSection()
        }
        .focusScope(settingsScope)
        .themedScreen(intensity: 0.55)
        .toolbar(.hidden, for: .navigationBar)
        .onAppear {
            panelReady = false
            Task {
                try? await Task.sleep(nanoseconds: 700_000_000)
                panelReady = true
            }
        }
        .onPlayPauseCommand {
            if engine.current != nil { engine.togglePlay(source: "menu") }
        }
        .onChange(of: appliedKey) {
            engine.applySettings()
        }
    }

    private var settingsList: some View {
        List {
            Section("Sound") {
                NavigationLink {
                    SoundSettingsView()
                } label: {
                    HStack {
                        Text("Equalizer & stereo widener")
                        Spacer()
                        Text(soundSummary).foregroundStyle(.secondary)
                    }
                }
                .prefersDefaultFocus(true, in: settingsScope)
            }

            Section {
                Picker("Look", selection: $themeRaw) {
                    ForEach(AppTheme.allCases) { look in
                        Text(look.name).tag(look.rawValue)
                    }
                }
                Toggle("Moving background and spinning record", isOn: $motion)
                Picker("Text size in music & folder lists", selection: $listTextSize) {
                    ForEach(ListTextSize.allCases, id: \.rawValue) { size in
                        Text(size.label).tag(size.rawValue)
                    }
                }
            } header: {
                Text("Appearance")
            } footer: {
                Text("Aurora takes its colours from the cover art; Noir is black with big type; Vinyl is warm, with a record that spins while it plays; Classic is the original look. With motion off, backgrounds and records stand still.")
            }

            Section {
                Toggle("Keep music playing after leaving the app", isOn: $keepMusic)
                Picker("Left button jumps back", selection: $seekBack) {
                    ForEach([5, 10, 15, 30], id: \.self) { Text("\($0) seconds").tag($0) }
                }
                Picker("Right button jumps forward", selection: $seekForward) {
                    ForEach([10, 15, 30, 60], id: \.self) { Text("\($0) seconds").tag($0) }
                }
                Toggle("Fast seek", isOn: $fastSeek)
            } header: {
                Text("Playback")
            } footer: {
                Text("Music videos always pause when you leave the app. Fast seek jumps to the nearest keyframe (can be a few seconds off) so video does not stutter after a jump.")
            }

            Section {
                TextField("Server address (empty = find automatically)", text: $serverAddress)
                    .autocorrectionDisabled()
                    .onSubmit { addressInvalid = !locator.setManual(serverAddress) }
                Text("Using \(Config.remoteAddress) · \(locator.status)\(addressInvalid ? " · that is not an address" : "")")
                    .foregroundStyle(.secondary)
            } header: {
                Text("Server")
            } footer: {
                Text("Leave the address empty to find the server automatically (Bonjour). Type an address (e.g. 10.0.0.5 or 10.0.0.5:8080) only if it is not found.")
            }

            Section {
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
                    Text("Helvetica Neue").tag("Helvetica Neue")
                    Text("Avenir Next").tag("Avenir Next")
                    Text("Futura").tag("Futura")
                    Text("Georgia").tag("Georgia")
                    Text("Menlo").tag("Menlo")
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
            } header: {
                Text("Lyrics / subtitles")
            } footer: {
                Text("Language: Default shows the track the file marks as default (or its forced track). English or Croatian picks that language when the video has it, otherwise Default. Style changes restart a playing video at the same position; a language change switches the track without restarting.")
            }
        }
        // Each picker is a row "Name ............ Value" that opens a list of choices,
        // instead of a full-width segmented control with no title.
        .pickerStyle(.navigationLink)
        .font(.system(size: 34))
        .environment(\.defaultMinListRowHeight, 78)
    }

    private var soundSummary: String {
        _ = eqPreset; _ = widen  // refresh when either changes
        return SoundSettings.summary
    }
}

/// Text size for menus and lists. tvOS defaults are sized for small TVs; Compact fits more rows.
enum ListTextSize: Int, CaseIterable {
    case normal = 0, compact = 1, small = 2

    var label: String {
        switch self {
        case .normal: "Normal (tvOS default)"
        case .compact: "Compact"
        case .small: "Small"
        }
    }

    var titleFont: Font {
        switch self {
        case .normal: .body
        case .compact: .system(size: 26)
        case .small: .system(size: 22)
        }
    }

    var detailFont: Font {
        switch self {
        case .normal: .caption
        case .compact: .system(size: 19)
        case .small: .system(size: 16)
        }
    }

    var rowHeight: CGFloat {
        switch self {
        case .normal: 66
        case .compact: 50
        case .small: 42
        }
    }

    static var current: ListTextSize {
        ListTextSize(rawValue: UserDefaults.standard.object(forKey: PlayerSettings.listTextSizeKey) as? Int
            ?? ListTextSize.compact.rawValue) ?? .compact
    }
}

struct DenseList: ViewModifier {
    @AppStorage(PlayerSettings.listTextSizeKey) private var raw = ListTextSize.compact.rawValue

    func body(content: Content) -> some View {
        let size = ListTextSize(rawValue: raw) ?? .compact
        content
            .font(size.titleFont)
            .environment(\.defaultMinListRowHeight, size.rowHeight)
    }
}

extension View {
    func denseList() -> some View { modifier(DenseList()) }
}
