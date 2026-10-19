import SwiftUI

/// Fade + blur + slight drift: used for titles changing between songs.
struct BlurFade: ViewModifier {
    var blur: CGFloat
    var opacity: Double
    var offset: CGSize

    func body(content: Content) -> some View {
        content.blur(radius: blur).opacity(opacity).offset(offset)
    }
}

extension AnyTransition {
    static func blurFade(_ offset: CGSize = .zero, blur: CGFloat = 24) -> AnyTransition {
        .modifier(active: BlurFade(blur: blur, opacity: 0, offset: offset),
                  identity: BlurFade(blur: 0, opacity: 1, offset: .zero))
    }

    /// Next song: the new one comes from the trailing side, the old one leaves to the leading side
    /// (and the other way round for previous).
    static func slide(forward: Bool, distance: CGFloat = 120) -> AnyTransition {
        .asymmetric(
            insertion: .blurFade(CGSize(width: forward ? distance : -distance, height: 0), blur: 18)
                .combined(with: .scale(scale: 0.92)),
            removal: .blurFade(CGSize(width: forward ? -distance : distance, height: 0), blur: 18)
                .combined(with: .scale(scale: 0.92))
        )
    }

    /// Text that rises into place (Noir).
    static var rise: AnyTransition {
        .asymmetric(insertion: .blurFade(CGSize(width: 0, height: 60), blur: 12),
                    removal: .blurFade(CGSize(width: 0, height: -40), blur: 12))
    }
}

/// Small capsule with a fact ("FLAC", "Lossless", "2021").
struct FactChip: View {
    let text: String
    var theme: AppTheme
    var size: CGFloat = 20

    var body: some View {
        Text(text)
            .font(theme == .noir ? .system(size: size, weight: .semibold, design: .monospaced) : theme.font(size, .semibold))
            .padding(.horizontal, size * 0.7)
            .padding(.vertical, size * 0.3)
            .background {
                switch theme {
                case .noir: Capsule().stroke(.white.opacity(0.5), lineWidth: 1.5)
                case .vinyl: Capsule().fill(Color(red: 0.93, green: 0.66, blue: 0.29).opacity(0.18))
                default: Capsule().fill(.white.opacity(0.14))
                }
            }
            .foregroundStyle(theme.ink.opacity(0.85))
    }
}
