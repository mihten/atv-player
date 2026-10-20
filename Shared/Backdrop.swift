import SwiftUI
import UIKit

/// Full-screen background of the current theme, coloured by the cover art of what is playing.
/// `intensity` < 1 dims it for menus. `animate` = false while a video plays: on the Apple TV, VLC decodes
/// AV1 in software, so nothing should compete with it (and the menus are hidden behind the video anyway).
struct ThemeBackdrop: View {
    @Environment(\.appTheme) private var theme
    @EnvironmentObject private var artwork: ArtworkStore
    @AppStorage(AppTheme.motionKey) private var motion = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var intensity: Double = 0.8
    var animate = true

    var body: some View {
        Group {
            switch theme {
            case .aurora: AuroraBackground(palette: artwork.palette, animate: motion && animate && !reduceMotion,
                                           intensity: intensity)
            case .noir: NoirBackground(palette: artwork.palette, intensity: intensity)
            case .vinyl: VinylBackground(palette: artwork.palette, intensity: intensity)
            case .classic: Color.clear
            }
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
    }
}

/// Slowly drifting mesh gradient in the cover's colours (iOS/tvOS 18+; soft blurred circles before).
struct AuroraBackground: View {
    let palette: Palette
    let animate: Bool
    var intensity = 1.0

    var body: some View {
        ZStack {
            palette.base.color
            mesh
                .id(palette)
                .transition(.opacity)
            LinearGradient(colors: [.black.opacity(0.10), .black.opacity(0.35), .black.opacity(0.62)],
                           startPoint: .top, endPoint: .bottom)
            Color.black.opacity(max(0, 1 - intensity) * 0.8)
        }
        .animation(.easeInOut(duration: 1.6), value: palette)
    }

    private var colors: [Color] {
        let t = palette.tones.map(\.color)
        let b = palette.base.color
        return [t[2], t[0], t[2],
                t[1], t[3], t[0],
                b, t[1], t[2]]
    }

    @ViewBuilder private var mesh: some View {
        if #available(iOS 18.0, tvOS 18.0, *) {
            TimelineView(.animation(minimumInterval: 1.0 / 20, paused: !animate)) { context in
                let t = animate ? context.date.timeIntervalSinceReferenceDate : 0
                MeshGradient(width: 3, height: 3, points: Self.points(t), colors: colors)
            }
        } else {
            GeometryReader { geo in
                let s = max(geo.size.width, geo.size.height)
                ZStack {
                    ForEach(0..<4, id: \.self) { i in
                        Circle()
                            .fill(palette.tones[i].color)
                            .frame(width: s * 0.7, height: s * 0.7)
                            .offset(x: [-0.3, 0.3, -0.2, 0.25][i] * s, y: [-0.25, -0.2, 0.3, 0.25][i] * s)
                    }
                }
                .frame(width: geo.size.width, height: geo.size.height)
                .blur(radius: s * 0.12)
            }
        }
    }

    /// Corners fixed, edge midpoints slide along their edge, the centre wanders: a slow, organic drift.
    static func points(_ t: Double) -> [SIMD2<Float>] {
        func wave(_ speed: Double, _ phase: Double, _ amp: Double) -> Float {
            Float(0.5 + amp * sin(t * speed + phase))
        }
        return [
            [0, 0], [wave(0.13, 0, 0.28), 0], [1, 0],
            [0, wave(0.11, 1, 0.28)], [wave(0.17, 2, 0.22), wave(0.15, 3, 0.22)], [1, wave(0.09, 4, 0.28)],
            [0, 1], [wave(0.12, 5, 0.28), 1], [1, 1],
        ]
    }
}

/// Pure black with two faint glows in the cover's colours.
struct NoirBackground: View {
    let palette: Palette
    var intensity = 1.0

    var body: some View {
        GeometryReader { geo in
            let r = max(geo.size.width, geo.size.height)
            ZStack {
                Color.black
                RadialGradient(colors: [palette.vibrant.color.opacity(0.20 * intensity), .clear],
                               center: .topTrailing, startRadius: 0, endRadius: r * 0.75)
                RadialGradient(colors: [palette.tones[0].color.opacity(0.22 * intensity), .clear],
                               center: .bottomLeading, startRadius: 0, endRadius: r * 0.6)
            }
        }
        .animation(.easeInOut(duration: 1.2), value: palette)
    }
}

/// Warm dark brown, a soft lamp light from above, a hint of the cover colour and film grain.
struct VinylBackground: View {
    let palette: Palette
    var intensity = 1.0

    var body: some View {
        GeometryReader { geo in
            let r = max(geo.size.width, geo.size.height)
            ZStack {
                LinearGradient(colors: [Color(red: 0.17, green: 0.11, blue: 0.07), Color(red: 0.06, green: 0.04, blue: 0.03)],
                               startPoint: .top, endPoint: .bottom)
                RadialGradient(colors: [Color(red: 1, green: 0.74, blue: 0.42).opacity(0.20 * intensity), .clear],
                               center: UnitPoint(x: 0.28, y: -0.05), startRadius: 0, endRadius: r * 0.7)
                RadialGradient(colors: [palette.tones[0].color.opacity(0.20 * intensity), .clear],
                               center: UnitPoint(x: 0.95, y: 1.05), startRadius: 0, endRadius: r * 0.55)
                GrainOverlay().opacity(0.08)
            }
        }
        .animation(.easeInOut(duration: 1.2), value: palette)
    }
}

/// Tiled monochrome noise (generated once).
struct GrainOverlay: View {
    static let image: UIImage = {
        let side = 160
        var bytes = [UInt8](repeating: 0, count: side * side)
        var seed: UInt32 = 0x9E37_79B9
        for i in bytes.indices {
            seed = seed &* 1_664_525 &+ 1_013_904_223
            bytes[i] = UInt8(truncatingIfNeeded: seed >> 24)
        }
        let data = Data(bytes)
        guard let provider = CGDataProvider(data: data as CFData),
              let cg = CGImage(width: side, height: side, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: side,
                               space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: 0),
                               provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
        else { return UIImage() }
        return UIImage(cgImage: cg)
    }()

    var body: some View {
        Image(uiImage: Self.image)
            .resizable(resizingMode: .tile)
            .blendMode(.overlay)
    }
}
