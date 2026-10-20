import SwiftUI

/// Focus look for tiles, posters and the Now Playing card, per theme:
/// Aurora lifts and glows in the cover colour, Noir draws a hard white frame, Vinyl a warm amber rim.
struct TileButtonStyle: ButtonStyle {
    var corner: CGFloat? = nil
    var glow: Color? = nil
    /// Focus scale; full-width cards use a small one so they stay inside the screen.
    var lift: CGFloat = 1.08

    func makeBody(configuration: Configuration) -> some View {
        TileBody(configuration: configuration, corner: corner, glow: glow, lift: lift)
    }
}

private struct TileBody: View {
    let configuration: ButtonStyleConfiguration
    let corner: CGFloat?
    let glow: Color?
    let lift: CGFloat
    @Environment(\.isFocused) private var focused
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore

    var body: some View {
        let radius = corner ?? theme.corner
        let accent = glow ?? theme.accent(artwork.palette)
        configuration.label
            .overlay {
                switch theme {
                case .noir:
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(.white, lineWidth: focused ? 5 : 0)
                case .vinyl:
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(accent.opacity(focused ? 0.95 : 0), lineWidth: 4)
                default:
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(.white.opacity(focused ? 0.55 : 0), lineWidth: 2)
                }
            }
            .scaleEffect(configuration.isPressed ? 0.97 : (focused ? (theme == .noir ? 1 + (lift - 1) / 2 : lift) : 1))
            .shadow(color: focused ? (theme == .noir ? .black : accent.opacity(0.55)) : .black.opacity(0.35),
                    radius: focused ? 36 : 12, y: focused ? 22 : 8)
            .brightness(focused ? 0.04 : (theme == .noir ? -0.08 : 0))
            .animation(.spring(response: 0.32, dampingFraction: 0.72), value: focused)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Theme background + typeface for a whole screen (no-op in Classic).
struct ThemedScreen: ViewModifier {
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var engine: PlayerEngine
    var intensity = 0.7

    func body(content: Content) -> some View {
        if theme.isClassic {
            content
        } else {
            content
                .fontDesign(theme.fontDesign)
                .background {
                    // frozen while a music video plays (VLC needs the CPU for AV1)
                    ThemeBackdrop(intensity: intensity, animate: !(engine.current?.isVideo == true && engine.isPlaying))
                }
        }
    }
}

extension View {
    func themedScreen(intensity: Double = 0.7) -> some View { modifier(ThemedScreen(intensity: intensity)) }
}

/// Section title above a shelf.
struct ShelfTitle: View {
    let text: String
    @Environment(\.appTheme) private var theme

    var body: some View {
        Group {
            switch theme {
            case .noir:
                Text(text.uppercased()).font(.system(size: 26, weight: .heavy)).tracking(5)
            case .vinyl:
                Text(text).font(.system(size: 34, weight: .medium, design: .serif).italic())
            default:
                Text(text).font(theme.font(34, .bold))
            }
        }
        .foregroundStyle(theme.ink.opacity(0.92))
    }
}
