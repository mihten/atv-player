import SwiftUI

/// Theme background + typeface behind a list or scroll view (no-op in Classic).
struct PhoneThemed: ViewModifier {
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var engine: PlayerEngine
    var intensity = 0.75

    func body(content: Content) -> some View {
        if theme.isClassic {
            content
        } else {
            content
                .fontDesign(theme.fontDesign)
                .scrollContentBackground(.hidden)
                .background {
                    ThemeBackdrop(intensity: intensity,
                                  animate: !(engine.current?.isVideo == true && engine.isPlaying))
                }
        }
    }
}

/// Translucent row background in themed lists.
struct ThemedRow: ViewModifier {
    @Environment(\.appTheme) private var theme

    func body(content: Content) -> some View {
        switch theme {
        case .classic: content
        case .noir: content.listRowBackground(Color.white.opacity(0.05))
        case .vinyl: content.listRowBackground(Color(red: 0.25, green: 0.16, blue: 0.10).opacity(0.55))
        case .aurora: content.listRowBackground(Color.white.opacity(0.08))
        }
    }
}

extension View {
    func phoneThemed(intensity: Double = 0.75) -> some View { modifier(PhoneThemed(intensity: intensity)) }
    func themedRow() -> some View { modifier(ThemedRow()) }
}

/// Namespace of the mini player artwork, for the zoom into the player (iOS 18+).
private struct PlayerNamespaceKey: EnvironmentKey {
    static let defaultValue: Namespace.ID? = nil
}

extension EnvironmentValues {
    var playerNamespace: Namespace.ID? {
        get { self[PlayerNamespaceKey.self] }
        set { self[PlayerNamespaceKey.self] = newValue }
    }
}

extension View {
    @ViewBuilder
    func zoomSource(_ ns: Namespace.ID?) -> some View {
        if #available(iOS 18.0, *), let ns {
            matchedTransitionSource(id: "nowPlaying", in: ns)
        } else {
            self
        }
    }

    @ViewBuilder
    func zoomDestination(_ ns: Namespace.ID?) -> some View {
        if #available(iOS 18.0, *), let ns {
            navigationTransition(.zoom(sourceID: "nowPlaying", in: ns))
        } else {
            self
        }
    }
}

/// Buttons that shrink a little while pressed.
struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.88 : 1)
            .opacity(configuration.isPressed ? 0.75 : 1)
            .animation(.spring(response: 0.25, dampingFraction: 0.6), value: configuration.isPressed)
    }
}

/// Seek bar of the themed player: touch anywhere on it to jump, drag to scrub (the bar thickens while
/// you hold it). The value only moves to VLC when you let go.
struct ThemedScrubber: View {
    @Binding var value: Double
    let duration: Double
    let accent: Color
    let onBegin: () -> Void
    let onEnd: (Double) -> Void
    @Environment(\.appTheme) private var theme
    @State private var dragging = false

    var body: some View {
        let thin: CGFloat = theme == .noir ? 3 : 6
        GeometryReader { geo in
            let frac = duration > 0 ? min(max(value / duration, 0), 1) : 0
            let h = dragging ? thin + 6 : thin
            ZStack(alignment: .leading) {
                Capsule().fill(theme == .vinyl ? accent.opacity(0.25) : Color.white.opacity(0.22))
                    .frame(height: h)
                Capsule().fill(accent)
                    .frame(width: max(h, geo.size.width * frac), height: h)
                    .shadow(color: theme == .aurora ? accent.opacity(0.8) : .clear, radius: dragging ? 10 : 6)
                if theme == .vinyl || dragging {
                    Circle().fill(theme == .noir ? .white : accent)
                        .frame(width: dragging ? 22 : 14, height: dragging ? 22 : 14)
                        .shadow(color: .black.opacity(0.35), radius: 3)
                        .offset(x: geo.size.width * frac - (dragging ? 11 : 7))
                }
            }
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { g in
                        if !dragging {
                            dragging = true
                            onBegin()
                        }
                        value = min(max(0, g.location.x / max(geo.size.width, 1)), 1) * duration
                    }
                    .onEnded { _ in
                        dragging = false
                        onEnd(value)
                    }
            )
        }
        .frame(height: 34)
        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: dragging)
        .sensoryFeedback(.selection, trigger: dragging)
    }
}

/// Small preview card of a look, for Settings → Appearance.
struct ThemeCard: View {
    let theme: AppTheme
    let selected: Bool

    var body: some View {
        VStack(spacing: 8) {
            preview
                .frame(width: 92, height: 138)
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .strokeBorder(selected ? Color.accentColor : Color.white.opacity(0.15), lineWidth: selected ? 3 : 1)
                }
                .overlay(alignment: .topTrailing) {
                    if selected {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.title3)
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(.white, Color.accentColor)
                            .padding(6)
                    }
                }
                .scaleEffect(selected ? 1.04 : 1)
            Text(theme.name).font(.footnote.weight(selected ? .bold : .regular))
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.7), value: selected)
    }

    @ViewBuilder private var preview: some View {
        let p = Palette.from(hue: 0.92)
        switch theme {
        case .aurora:
            ZStack {
                AuroraBackground(palette: p, animate: false)
                VStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 8).fill(LinearGradient(colors: [p.vibrant.color, p.tones[0].color],
                                                                          startPoint: .topLeading, endPoint: .bottomTrailing))
                        .frame(width: 56, height: 56)
                        .shadow(color: p.vibrant.color.opacity(0.7), radius: 10)
                    Capsule().fill(.white.opacity(0.9)).frame(width: 50, height: 5)
                    Capsule().fill(p.vibrant.color).frame(width: 60, height: 3)
                }
            }
        case .noir:
            ZStack(alignment: .bottomLeading) {
                Color.black
                Text("Aa").font(.system(size: 40, weight: .heavy).width(.condensed)).foregroundStyle(.white)
                    .padding(10)
                Rectangle().fill(Color(red: 1, green: 0.3, blue: 0.4)).frame(height: 2).padding(.horizontal, 10)
                    .padding(.bottom, 8)
            }
        case .vinyl:
            ZStack {
                VinylBackground(palette: p)
                VinylRecord(image: nil, spinning: false, size: 70, labelTint: Color(red: 0.85, green: 0.5, blue: 0.2))
            }
        case .classic:
            ZStack(alignment: .top) {
                Color(white: 0.11)
                VStack(alignment: .leading, spacing: 9) {
                    ForEach(0..<6, id: \.self) { _ in
                        Capsule().fill(Color.white.opacity(0.3)).frame(width: 60, height: 5)
                    }
                }
                .padding(.top, 16)
            }
        }
    }
}
