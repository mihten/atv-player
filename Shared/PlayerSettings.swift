import Foundation

/// User settings, stored on the Apple TV (UserDefaults). Keys are shared with the @AppStorage
/// bindings in SettingsView; the defaults here must match the ones there.
enum PlayerSettings {
    static let keepMusicKey = "keepMusicInBackground"
    static let keepVideoAudioKey = "keepVideoAudioInBackground"
    static let subsOnKey = "subsOn"
    static let subLangKey = "subLang"  // "" = file default, "eng", "hrv"
    static let subSizeKey = "subSize"
    static let subFontKey = "subFont"
    static let subColorKey = "subColor"
    static let subBoldKey = "subBold"
    static let subOutlineKey = "subOutline"
    static let subBackgroundKey = "subBackground"
    static let subMarginKey = "subMargin"
    static let listTextSizeKey = "listTextSize"
    static let fastSeekKey = "fastSeek"
    static let seekBackKey = "seekBack"
    static let seekForwardKey = "seekForward"

    static let defaultColor = 0xFFFFFF
    static let defaultOutline = 4

    private static var defaults: UserDefaults { .standard }

    static var keepMusicInBackground: Bool { defaults.object(forKey: keepMusicKey) as? Bool ?? true }
    /// Music videos keep playing as audio only when the app is left (iPhone). The Apple TV pauses them.
    static let keepVideoAudioDefault: Bool = {
        #if os(iOS)
        return true
        #else
        return false
        #endif
    }()
    static var keepVideoAudioInBackground: Bool {
        defaults.object(forKey: keepVideoAudioKey) as? Bool ?? keepVideoAudioDefault
    }
    static var subtitlesOn: Bool { defaults.object(forKey: subsOnKey) as? Bool ?? true }
    static var subtitleLanguage: String { defaults.string(forKey: subLangKey) ?? "" }

    /// Language codes a file may use for each choice (ISO 639-2/B and /T, 639-1, old Serbo-Croatian).
    static let languageCodes: [String: Set<String>] = [
        "eng": ["eng", "en"],
        "hrv": ["hrv", "hr", "scr", "cro", "hbs", "sh"],
    ]

    /// Which subtitle track (ordinal among the file's subtitle streams) to show, or nil for none.
    /// Language choice: the matching track (default-flagged first, then a full track before a forced one).
    /// Default, or no track in the chosen language: what the file asks for (good practice, like
    /// mpv/VLC/Plex): the default-flagged track, else a forced track. A file with neither still shows
    /// its first track: in this library every subtitle track is lyrics, which are wanted.
    static func subtitleOrdinal(for item: MediaItem) -> Int? {
        guard let subs = item.subs, !subs.isEmpty else { return item.subOrd }
        let language = subtitleLanguage
        if let codes = languageCodes[language] {
            let matching = subs.filter { codes.contains($0.lang) }
            if let pick = matching.first(where: { $0.isDefault && !$0.forced })
                ?? matching.first(where: { !$0.forced })
                ?? matching.first {
                return pick.ord
            }
        }
        return (subs.first(where: { $0.isDefault }) ?? subs.first(where: { $0.forced }) ?? subs.first)?.ord
    }
    static var fastSeek: Bool { defaults.object(forKey: fastSeekKey) as? Bool ?? true }
    static var seekBack: Double { Double(defaults.object(forKey: seekBackKey) as? Int ?? 10) }
    static var seekForward: Double { Double(defaults.object(forKey: seekForwardKey) as? Int ?? 10) }

    /// libVLC options for the subtitle renderer (freetype). Empty = VLC defaults.
    static func vlcOptions() -> [String] {
        var options: [String] = []
        let size = defaults.integer(forKey: subSizeKey)
        if size > 0 { options.append("--freetype-rel-fontsize=\(size)") }
        if let font = defaults.string(forKey: subFontKey), !font.isEmpty {
            options.append("--freetype-font=\(font)")
        }
        let color = defaults.object(forKey: subColorKey) as? Int ?? defaultColor
        if color != defaultColor { options.append("--freetype-color=\(color)") }
        if defaults.bool(forKey: subBoldKey) { options.append("--freetype-bold") }
        let outline = defaults.object(forKey: subOutlineKey) as? Int ?? defaultOutline
        if outline != defaultOutline { options.append("--freetype-outline-thickness=\(outline)") }
        let background = defaults.integer(forKey: subBackgroundKey)
        if background > 0 {
            options.append("--freetype-background-color=0")
            options.append("--freetype-background-opacity=\(background)")
        }
        let margin = defaults.integer(forKey: subMarginKey)
        if margin > 0 { options.append("--sub-margin=\(margin)") }
        return options
    }
}
