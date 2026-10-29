import SwiftUI

/// Apple TV Settings (0.18.21): a menu screen pushed on the main navigation stack like Playlists or Folders (themed background,
/// the system slide in and out, Back on the first page returns to the menu). Left half: title and Now Playing; right half: the
/// settings. Pages and choice lists open inside the right half (an own page stack, no nested NavigationStack); Back goes one
/// page up, also while the Now Playing card has the focus.
enum SettingsRoute: Hashable {
    case theme, server, sound, playback, lyrics, about, equalizer

    /// `-demoRoute` names (CI screenshots) that open Settings, optionally on a page.
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

/// One page in the right half of Settings.
struct SettingsScreen: Identifiable {
    let id = UUID()
    let title: String
    let content: AnyView

    static func page(_ route: SettingsRoute) -> SettingsScreen {
        switch route {
        case .theme: SettingsScreen(title: "Theme", content: AnyView(ThemeSettingsView()))
        case .server: SettingsScreen(title: "Server", content: AnyView(ServerSettingsView()))
        case .sound: SettingsScreen(title: "Sound", content: AnyView(SoundSettingsView()))
        case .playback: SettingsScreen(title: "Playback", content: AnyView(PlaybackSettingsView()))
        case .lyrics: SettingsScreen(title: "Lyrics", content: AnyView(LyricsSettingsView()))
        case .about: SettingsScreen(title: "About", content: AnyView(AboutSettingsView()))
        case .equalizer: SettingsScreen(title: "Custom equalizer", content: AnyView(CustomEqualizerView()))
        }
    }
}

/// Opens / closes pages in the right half of Settings (set by SettingsView).
struct SettingsNavigator {
    var push: (SettingsScreen) -> Void = { _ in }
    var pop: () -> Void = {}
}

private struct SettingsNavigatorKey: EnvironmentKey {
    static let defaultValue = SettingsNavigator()
}

extension EnvironmentValues {
    var settingsNav: SettingsNavigator {
        get { self[SettingsNavigatorKey.self] }
        set { self[SettingsNavigatorKey.self] = newValue }
    }
}

struct SettingsView: View {
    var start: SettingsRoute? = nil
    @EnvironmentObject var engine: PlayerEngine
    /// Pages above the first one. Every page stays mounted (hidden and disabled below the top one), so a page keeps its
    /// state and list position while a choice list is open over it.
    @State private var stack: [SettingsScreen] = []
    /// The Now Playing card is disabled (= not focusable) for a moment whenever Settings or a page opens or closes, so
    /// tvOS puts the focus on the settings list, not on the card (the pattern of the 0.18.18 Settings screen, proven on
    /// the TV).
    @State private var cardReady = false
    @State private var cardTask: Task<Void, Never>?
    @Namespace private var scope

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                Text("Settings")
                    .font(.system(size: 64, weight: .bold))
                if engine.current != nil {
                    NowPlayingSidebar(enabled: cardReady)
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
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            // The whole left half is one focus area: ◀ from any row (also the top ones, above the card) reaches the card.
            // Without it the card was reachable only from rows level with it (UI test, 0.18.21).
            .focusSection()
            pages
                .frame(width: 960)
                .padding(.trailing, 60)
                .focusSection()
        }
        .focusScope(scope)
        .themedScreen(intensity: 0.55)
        .toolbar(.hidden, for: .navigationBar)
        .environment(\.settingsNav, SettingsNavigator(push: push, pop: pop))
        // First page: no handler, so Back is the navigation stack's own (slide back to the menu) wherever the focus is.
        // Deeper pages: Back closes the top page, also with the focus on the Now Playing card.
        .onExitCommand(perform: backAction)
        .onPlayPauseCommand {
            if engine.current != nil { engine.togglePlay(source: "menu") }
        }
        .onAppear {
            if stack.isEmpty, let start {
                stack = start == .equalizer ? [.page(.sound), .page(.equalizer)] : [.page(start)]
            }
            holdCard()
        }
    }

    private var backAction: (() -> Void)? {
        stack.isEmpty ? nil : { pop() }
    }

    private var pages: some View {
        ZStack(alignment: .topLeading) {
            layer(depth: 0, title: nil) {
                SettingsRootView(scope: scope)
            }
            ForEach(Array(stack.enumerated()), id: \.element.id) { index, screen in
                layer(depth: index + 1, title: screen.title) {
                    screen.content
                }
                .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
    }

    private func layer<Content: View>(depth: Int, title: String?, @ViewBuilder content: () -> Content) -> some View {
        let top = depth == stack.count
        return VStack(alignment: .leading, spacing: 6) {
            if let title {
                Text(title)
                    .font(.system(size: 40, weight: .semibold))
                    .accessibilityIdentifier("settings.pageTitle")
                    .padding(.leading, 24)
                    .padding(.top, 20)
            }
            content()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .opacity(top ? 1 : 0)
        .offset(x: depth < stack.count ? -120 : 0)
        .disabled(!top)
        .accessibilityHidden(!top)
    }

    private func push(_ screen: SettingsScreen) {
        holdCard()
        withAnimation(.easeInOut(duration: 0.3)) { stack.append(screen) }
    }

    private func pop() {
        guard !stack.isEmpty else { return }
        holdCard()
        withAnimation(.easeInOut(duration: 0.3)) { _ = stack.removeLast() }
    }

    private func holdCard() {
        cardReady = false
        cardTask?.cancel()
        cardTask = Task {
            try? await Task.sleep(nanoseconds: 700_000_000)
            if !Task.isCancelled { cardReady = true }
        }
    }
}

/// First page: one row per page, "Name ........ current value >", like the tvOS Settings app.
struct SettingsRootView: View {
    let scope: Namespace.ID
    @Environment(\.settingsNav) private var nav
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
                .prefersDefaultFocus(true, in: scope)
            row(.server, "Server", "server.rack", locator.isManual ? Config.remoteAddress : "Automatic")
            row(.sound, "Sound", "speaker.wave.2", soundSummary)
            row(.playback, "Playback", "play.circle", "\(seekBack) s / \(seekForward) s")
            row(.lyrics, "Lyrics", "captions.bubble", lyricsSummary)
            row(.about, "About", "info.circle", AppInfo.version)
        }
        .font(.system(size: 34))
        .environment(\.defaultMinListRowHeight, 78)
    }

    private func row(_ route: SettingsRoute, _ title: String, _ symbol: String, _ value: String) -> some View {
        Button {
            nav.push(.page(route))
        } label: {
            HStack(spacing: 24) {
                Image(systemName: symbol).frame(width: 48)
                Text(title)
                Spacer()
                Text(value).foregroundStyle(.secondary).lineLimit(1)
                Image(systemName: "chevron.right").font(.system(size: 26, weight: .semibold)).foregroundStyle(.secondary)
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
        SettingsPage {
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
