import SwiftUI
import TVVLCKit

/// Layout shared by the Settings sub-pages inside the Settings panel: a list, pickers as "Name .... Value >" rows
/// that open their own list in the panel.
struct SettingsPage<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        List { content }
            .pickerStyle(.navigationLink)
            .font(.system(size: 30))
            .environment(\.defaultMinListRowHeight, 66)
            .navigationTitle(title)
    }
}

/// Settings → Theme.
struct ThemeSettingsView: View {
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue
    @AppStorage(AppTheme.motionKey) private var motion = true
    @AppStorage(PlayerSettings.listTextSizeKey) private var listTextSize = ListTextSize.compact.rawValue

    var body: some View {
        SettingsPage(title: "Theme") {
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
            }
        }
    }
}

/// Settings → Server.
struct ServerSettingsView: View {
    @EnvironmentObject var locator: ServerLocator
    @State private var serverAddress = ServerLocator.manualAddress
    @State private var addressInvalid = false

    var body: some View {
        SettingsPage(title: "Server") {
            Section {
                TextField("Server address (empty = find automatically)", text: $serverAddress)
                    .autocorrectionDisabled()
                    .onSubmit { addressInvalid = !locator.setManual(serverAddress) }
                Text("Using \(Config.remoteAddress) · \(locator.status)\(addressInvalid ? " · that is not an address" : "")")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Settings → Playback.
struct PlaybackSettingsView: View {
    @AppStorage(PlayerSettings.keepMusicKey) private var keepMusic = true
    @AppStorage(PlayerSettings.fastSeekKey) private var fastSeek = true
    @AppStorage(PlayerSettings.seekBackKey) private var seekBack = 10
    @AppStorage(PlayerSettings.seekForwardKey) private var seekForward = 10

    var body: some View {
        SettingsPage(title: "Playback") {
            Section {
                Toggle("Keep music playing after leaving the app", isOn: $keepMusic)
                Picker("Left button jumps back", selection: $seekBack) {
                    ForEach([5, 10, 15, 30], id: \.self) { Text("\($0) seconds").tag($0) }
                }
                Picker("Right button jumps forward", selection: $seekForward) {
                    ForEach([10, 15, 30, 60], id: \.self) { Text("\($0) seconds").tag($0) }
                }
                Toggle("Fast seek", isOn: $fastSeek)
            }
        }
    }
}

/// Settings → Lyrics (subtitles in music videos).
struct LyricsSettingsView: View {
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

    /// Changes re-apply immediately (pickers open their own screen, so "apply on leaving"
    /// would fire every time a picker opens).
    private var appliedKey: String {
        "\(subsOn)|\(subLang)|\(size)|\(font)|\(color)|\(bold)|\(outline)|\(background)|\(margin)"
    }

    var body: some View {
        SettingsPage(title: "Lyrics") {
            Section {
                Toggle("Show lyrics / subtitles", isOn: $subsOn)
                Picker("Language", selection: $subLang) {
                    Text("Default (as set in the file)").tag("")
                    Text("English").tag("eng")
                    Text("Croatian").tag("hrv")
                }
            }
            Section {
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
                Text("Style")
            }
        }
        .onChange(of: appliedKey) {
            engine.applySettings()
        }
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
