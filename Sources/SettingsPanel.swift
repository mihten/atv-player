import SwiftUI

/// Apple TV Settings: a full screen split in half (0.18.20) - Now Playing on the left, the settings on the right. Every page and
/// picker opens in the right half; Back goes one level up and leaves Settings from the first page.
enum SettingsRoute: Hashable {
    case theme, server, sound, playback, lyrics, about, equalizer

    /// `-demoRoute` names (CI screenshots) that open the panel, optionally on a page.
    static func demo(_ screen: String?) -> SettingsRoute?? {
        switch screen {
        case "settings": .some(nil)
        case "theme": .theme
        case "server": .server
        case "sound": .sound
        case "playback": .playback
        case "lyrics": .lyrics
        case "about": .about
        case "eq": .equalizer
        default: nil
        }
    }
}

private struct OpenSettingsKey: EnvironmentKey {
    static let defaultValue: () -> Void = {}
}

extension EnvironmentValues {
    /// Opens the Settings panel (set by ContentView).
    var openSettings: () -> Void {
        get { self[OpenSettingsKey.self] }
        set { self[OpenSettingsKey.self] = newValue }
    }
}

struct SettingsPanel: View {
    let start: SettingsRoute?
    let close: () -> Void
    @EnvironmentObject var engine: PlayerEngine
    @Environment(\.appTheme) private var theme
    @State private var path: [SettingsRoute] = []
    /// The Now Playing card is disabled (= not focusable) while the screen opens, so tvOS puts the initial focus on the
    /// first setting; then it is enabled and ◀ reaches it (the pattern of the menus, proven on the TV).
    @State private var cardReady = false

    var body: some View {
        // Half and half: Now Playing (or a big symbol) on the left, the settings on the right. Pages and pickers
        // open inside the right half; Back goes one level up and leaves Settings from the first page.
        ZStack {
            Color.black.ignoresSafeArea()
            HStack(alignment: .top, spacing: 0) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Settings").font(.system(size: 64, weight: .bold))
                    if engine.current != nil {
                        NowPlayingSidebar(enabled: cardReady)
                    } else {
                        VStack(spacing: 30) {
                            Image(systemName: "gearshape.fill")
                                .font(.system(size: 300, weight: .regular))
                                .foregroundStyle(.secondary)
                            Text("ATV Player").font(.title3).foregroundStyle(.secondary)
                        }
                        .padding(.top, 60)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 60)
                NavigationStack(path: $path) {
                    SettingsRootView(close: close)
                        .navigationDestination(for: SettingsRoute.self) { route in
                            switch route {
                            case .theme: ThemeSettingsView()
                            case .server: ServerSettingsView()
                            case .sound: SoundSettingsView()
                            case .playback: PlaybackSettingsView()
                            case .lyrics: LyricsSettingsView()
                            case .about: AboutSettingsView()
                            case .equalizer: CustomEqualizerView()
                            }
                        }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(.trailing, 40)
                .focusSection()
            }
        }
        .themedScreen(intensity: 0.55)
        .fontDesign(theme.fontDesign)
        .onPlayPauseCommand {
            if engine.current != nil { engine.togglePlay(source: "settings") }
        }
        .onAppear {
            if let start { path = start == .equalizer ? [.sound, .equalizer] : [start] }
            cardReady = false
            Task {
                try? await Task.sleep(nanoseconds: 700_000_000)
                cardReady = true
            }
        }
    }
}

/// First page of the panel: one row per page, "Name ........ current value >".
struct SettingsRootView: View {
    let close: () -> Void
    @EnvironmentObject var locator: ServerLocator
    @AppStorage(PlayerSettings.subsOnKey) private var subsOn = true
    @AppStorage(PlayerSettings.subLangKey) private var subLang = ""
    @AppStorage(PlayerSettings.seekBackKey) private var seekBack = 10
    @AppStorage(PlayerSettings.seekForwardKey) private var seekForward = 10
    @AppStorage(SoundSettings.presetKey) private var eqPreset = "off"
    @AppStorage(SoundSettings.loudnessKey) private var loudness = SoundSettings.Loudness.raise.rawValue
    @AppStorage(AppTheme.key) private var themeRaw = AppTheme.aurora.rawValue

    var body: some View {
        List {
            row(.theme, "Theme", "paintpalette", (AppTheme(rawValue: themeRaw) ?? .aurora).name)
            row(.server, "Server", "server.rack", locator.isManual ? Config.remoteAddress : "Automatic")
            row(.sound, "Sound", "speaker.wave.2", soundSummary)
            row(.playback, "Playback", "play.circle", "\(seekBack) s / \(seekForward) s")
            row(.lyrics, "Lyrics", "captions.bubble", lyricsSummary)
            row(.about, "About", "info.circle", AppInfo.version)
        }
        .font(.system(size: 30))
        .environment(\.defaultMinListRowHeight, 70)
        .toolbar(.hidden, for: .navigationBar)
        .onExitCommand { close() }
    }

    private func row(_ route: SettingsRoute, _ title: String, _ symbol: String, _ value: String) -> some View {
        NavigationLink(value: route) {
            HStack(spacing: 22) {
                Image(systemName: symbol).frame(width: 44)
                Text(title)
                Spacer()
                Text(value).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }

    private var soundSummary: String {
        _ = eqPreset; _ = loudness  // refresh when any changes
        return SoundSettings.summary
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

enum AppInfo {
    static var version: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    }

    static var build: String {
        Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
    }
}

/// Settings → About: what the app is, where its server is, how the remote works. The explanations that used to sit
/// under menus and settings live here, once.
struct AboutSettingsView: View {
    @EnvironmentObject var connection: ServerConnection
    @EnvironmentObject var locator: ServerLocator

    var body: some View {
        SettingsPage(title: "About") {
            Section("ATV Player") {
                info("Version", "\(AppInfo.version) (build \(AppInfo.build))")
                info("Plays", "Songs and music videos from your own server, original files")
            }
            Section("Server") {
                info("Address", Config.remoteAddress)
                info("Status", connection.connected
                     ? "Connected" + (connection.state?.version.map { " · server \($0)" } ?? "") : locator.status)
                info("Web remote", "\(Config.remoteAddress) on a phone or PC")
                info("Queues", "The Apple TV and the iPhone each have their own")
            }
            Section("Siri Remote in the player") {
                info("▼  ▲", "Next / previous")
                info("◀  ▶", "Jump back / forward (Settings → Playback)")
                info("Click", "Lyrics on / off")
                info("Hold", "Shuffle, repeat, lyrics language")
                info("⏯", "Pause / resume, also in the menus")
            }
            Section("Good to know") {
                info("Loudness", "The server measures each file once; new files play unchanged until then")
                info("Sound and lyrics style", "A playing video restarts at the same position")
                info("Leaving the app", "Music videos pause; music keeps playing if set")
            }
        }
    }

    /// Text rows are not focusable on tvOS, so a long page could not scroll to them: each row is an empty button.
    private func info(_ title: String, _ value: String) -> some View {
        Button {} label: {
            HStack(alignment: .firstTextBaseline, spacing: 20) {
                Text(title)
                Spacer(minLength: 20)
                Text(value).foregroundStyle(.secondary).multilineTextAlignment(.trailing)
            }
            .font(.system(size: 26))
        }
    }
}
