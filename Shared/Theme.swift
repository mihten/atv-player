import SwiftUI

/// The app's look ("Settings → Appearance"). Same choices on the Apple TV and the iPhone, stored per device.
/// Classic = the original stock-SwiftUI look, kept unchanged as a fallback.
enum AppTheme: String, CaseIterable, Identifiable {
    case aurora, noir, vinyl, classic

    static let key = "appTheme"
    /// Moving backgrounds (Aurora) and spinning records (Vinyl). Off = still pictures.
    static let motionKey = "themeMotion"

    var id: String { rawValue }

    static var stored: AppTheme {
        AppTheme(rawValue: UserDefaults.standard.string(forKey: key) ?? "") ?? .aurora
    }

    static var motion: Bool { UserDefaults.standard.object(forKey: motionKey) as? Bool ?? true }

    var name: String {
        switch self {
        case .aurora: "Aurora"
        case .noir: "Noir"
        case .vinyl: "Vinyl"
        case .classic: "Classic"
        }
    }

    var tagline: String {
        switch self {
        case .aurora: "Colours from the cover art, glass and slow light"
        case .noir: "Black, big type, one accent colour"
        case .vinyl: "Warm and analogue, the record spins while it plays"
        case .classic: "The original look"
        }
    }

    var symbol: String {
        switch self {
        case .aurora: "sparkles"
        case .noir: "circle.lefthalf.filled"
        case .vinyl: "opticaldisc"
        case .classic: "list.bullet.rectangle"
        }
    }

    var isClassic: Bool { self == .classic }

    var fontDesign: Font.Design {
        switch self {
        case .aurora: .rounded
        // Vinyl uses Noir's typeface (the serif was not liked); it keeps its own colours and record.
        case .noir, .vinyl, .classic: .default
        }
    }

    /// Body / UI text in the theme's typeface.
    func font(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: fontDesign)
    }

    /// Big titles: Noir and Vinyl use heavy condensed type, Aurora rounded.
    func display(_ size: CGFloat) -> Font {
        switch self {
        case .noir, .vinyl: .system(size: size, weight: .heavy, design: .default).width(.condensed)
        case .aurora: .system(size: size, weight: .bold, design: .rounded)
        case .classic: .system(size: size, weight: .bold)
        }
    }

    /// Small caps-style labels above sections ("NOW PLAYING", "UP NEXT").
    func eyebrow(_ size: CGFloat) -> Font {
        .system(size: size, weight: .bold, design: fontDesign)
    }

    /// Accent colour: Aurora and Noir take it from the cover art, Vinyl is always amber.
    func accent(_ palette: Palette) -> Color {
        switch self {
        case .aurora: palette.vibrant.color
        case .noir: palette.vibrant.saturated(0.9, brightness: 1).color
        case .vinyl: Color(red: 0.93, green: 0.66, blue: 0.29)
        case .classic: .accentColor
        }
    }

    /// Text colour on the theme's backgrounds.
    var ink: Color {
        switch self {
        case .vinyl: Color(red: 0.98, green: 0.93, blue: 0.85)
        default: .white
        }
    }

    var corner: CGFloat {
        switch self {
        case .noir: 4
        case .vinyl: 10
        default: 22
        }
    }
}

private struct AppThemeKey: EnvironmentKey {
    static let defaultValue: AppTheme = .aurora
}

extension EnvironmentValues {
    var appTheme: AppTheme {
        get { self[AppThemeKey.self] }
        set { self[AppThemeKey.self] = newValue }
    }
}

/// Reads the stored theme (live, via @AppStorage) and hands it down the view tree; keeps the artwork
/// store on the item that is loaded.
struct ThemeRoot: ViewModifier {
    @AppStorage(AppTheme.key) private var raw = AppTheme.aurora.rawValue
    @EnvironmentObject var artwork: ArtworkStore
    @EnvironmentObject var engine: PlayerEngine

    func body(content: Content) -> some View {
        let theme = AppTheme(rawValue: raw) ?? .aurora
        content
            .environment(\.appTheme, theme)
            .tint(theme.isClassic ? nil : theme.accent(artwork.palette))
            .animation(.easeInOut(duration: 0.6), value: raw)
            .onAppear { artwork.follow(engine.current) }
            .onChange(of: engine.current?.id) { artwork.follow(engine.current) }
    }
}

extension View {
    func themeRoot() -> some View { modifier(ThemeRoot()) }
}
