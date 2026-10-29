import SwiftUI
import TVVLCKit

/// Layout shared by the Settings pages in the right half of the Settings screen.
struct SettingsPage<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        List { content }
            .font(.system(size: 30))
            .environment(\.defaultMinListRowHeight, 66)
    }
}

/// "Name ........ Value >" row; its choices open as a list in the right half of Settings (a system Picker would push a
/// full-screen list on the main navigation stack). Picking a value goes back.
struct ChoiceRow<Value: Hashable>: View {
    let title: String
    @Binding var selection: Value
    let options: [(label: String, value: Value)]
    @Environment(\.settingsNav) private var nav

    init(_ title: String, selection: Binding<Value>, options: [(label: String, value: Value)]) {
        self.title = title
        _selection = selection
        self.options = options
    }

    var body: some View {
        Button {
            nav.push(SettingsScreen(title: title, content: AnyView(ChoiceList(selection: $selection, options: options))))
        } label: {
            HStack(spacing: 20) {
                Text(title)
                Spacer()
                Text(options.first { $0.value == selection }?.label ?? "").foregroundStyle(.secondary).lineLimit(1)
                Image(systemName: "chevron.right").font(.system(size: 24, weight: .semibold)).foregroundStyle(.secondary)
            }
        }
    }
}

struct ChoiceList<Value: Hashable>: View {
    @Binding var selection: Value
    let options: [(label: String, value: Value)]
    @Environment(\.settingsNav) private var nav
    @FocusState private var focused: Int?

    var body: some View {
        SettingsPage {
            ForEach(options.indices, id: \.self) { index in
                Button {
                    selection = options[index].value
                    nav.pop()
                } label: {
                    HStack {
                        Text(options[index].label)
                        Spacer()
                        if options[index].value == selection {
                            Image(systemName: "checkmark").font(.system(size: 26, weight: .semibold))
                        }
                    }
                }
                .focused($focused, equals: index)
            }
        }
        .onAppear {
            // start on the current value, like the tvOS picker
            let current = options.firstIndex { $0.value == selection } ?? 0
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { focused = current }
        }
    }
}

/// Settings → Theme.
struct ThemeSettingsView: View {
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue
    @AppStorage(AppTheme.motionKey) private var motion = true
    @AppStorage(PlayerSettings.listTextSizeKey) private var listTextSize = ListTextSize.compact.rawValue

    var body: some View {
        SettingsPage {
            Section {
                ChoiceRow("Look", selection: $themeRaw, options: AppTheme.allCases.map { ($0.name, $0.rawValue) })
                Toggle("Moving background and spinning record", isOn: $motion)
                ChoiceRow("Text size in music & folder lists", selection: $listTextSize,
                          options: ListTextSize.allCases.map { ($0.label, $0.rawValue) })
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
        SettingsPage {
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
        SettingsPage {
            Section {
                Toggle("Keep music playing after leaving the app", isOn: $keepMusic)
                ChoiceRow("Left button jumps back", selection: $seekBack,
                          options: [5, 10, 15, 30].map { ("\($0) seconds", $0) })
                ChoiceRow("Right button jumps forward", selection: $seekForward,
                          options: [10, 15, 30, 60].map { ("\($0) seconds", $0) })
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
        SettingsPage {
            Section {
                Toggle("Show lyrics / subtitles", isOn: $subsOn)
                ChoiceRow("Language", selection: $subLang, options: [
                    ("Default (as set in the file)", ""),
                    ("English", "eng"),
                    ("Croatian", "hrv"),
                ])
            }
            Section {
                ChoiceRow("Text size", selection: $size, options: [
                    ("Default", 0),
                    ("Small", 20),
                    ("Medium", 16),
                    ("Large", 12),
                    ("Extra large", 9),
                ])
                ChoiceRow("Font", selection: $font, options: [
                    ("Default", ""),
                    ("Helvetica Neue", "Helvetica Neue"),
                    ("Avenir Next", "Avenir Next"),
                    ("Futura", "Futura"),
                    ("Georgia", "Georgia"),
                    ("Menlo", "Menlo"),
                ])
                ChoiceRow("Text colour", selection: $color, options: [
                    ("White", 0xFFFFFF),
                    ("Yellow", 0xFFFF00),
                    ("Light grey", 0xC8C8C8),
                    ("Cyan", 0x00FFFF),
                ])
                Toggle("Bold text", isOn: $bold)
                ChoiceRow("Text outline", selection: $outline, options: [
                    ("None", 0),
                    ("Thin", 2),
                    ("Normal", 4),
                    ("Thick", 6),
                ])
                ChoiceRow("Background box", selection: $background, options: [
                    ("None", 0),
                    ("Light", 96),
                    ("Dark", 176),
                    ("Solid", 255),
                ])
                ChoiceRow("Position on screen", selection: $margin, options: [
                    ("Bottom", 0),
                    ("Slightly higher", 40),
                    ("Higher", 100),
                ])
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
